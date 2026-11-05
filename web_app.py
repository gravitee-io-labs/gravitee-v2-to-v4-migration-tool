#!/usr/bin/env python3
"""
Web interface for the Gravitee APIM V2 -> V4 migration tool.

- Lists the V2 APIs of an environment (using GraviteeClient from
  gravitee_v4_migration.py).
- Lets the user pick APIs and run `gravitee_v4_migration.py migrate` on them,
  streaming the tool's output back to the browser.

Standard library only, like the migration script itself.
"""
import csv
import json
import os
import re
import subprocess
import sys
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
SCRIPT = APP_DIR / "gravitee_v4_migration.py"
INDEX_HTML = APP_DIR / "static" / "index.html"
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
RUNS_DIR = DATA_DIR / "runs"
HOST = os.environ.get("WEB_HOST", "0.0.0.0")
PORT = int(os.environ.get("WEB_PORT", "8080"))

RUNS_DIR.mkdir(parents=True, exist_ok=True)
# The migration module configures a file log handler at import time.
os.environ.setdefault("LOG_FILE", str(DATA_DIR / "gravitee_migration.log"))
sys.path.insert(0, str(APP_DIR))
import gravitee_v4_migration as gm  # noqa: E402

INPUT_FIELDS = [
    "api_id", "api_name", "api_version", "definition_version",
    "state", "lifecycle_state", "migrate_to_v4", "auto_deploy", "auto_start",
    "force_migration",
]
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
API_LINE_RE = re.compile(r"\[(\d+)/(\d+)\]\s+(.*)\s+\(([^()]*)\)\s*$")
STEP_RE = re.compile(r"(Migration|Deploy|Start) (SUCCESS|FAILED)(?::\s*(.*))?$")

RUNS = {}            # run_id -> run dict
RUNS_LOCK = threading.Lock()
ACTIVE = {"id": None}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# All connection settings are entered in the Connection page and sent with
# each request; the browser remembers the non-secret ones. Nothing is read
# from .env, which only sets the port the UI is published on (docker compose).
#
# Credentials (token, or username and password) are never stored: they are
# only kept in memory for the duration of a request, or handed to the
# migration process that needs them.
CREDENTIAL_KEYS = ("GRAVITEE_TOKEN", "GRAVITEE_USERNAME", "GRAVITEE_PASSWORD")

# Advanced settings of the Connection page: default, minimum, maximum, label.
ADVANCED = {
    "retry_attempts": (3, 1, 10, "Retry attempts"),
    "retry_delay_s":  (2, 0, 60, "Retry delay (seconds)"),
    "page_size":      (100, 1, 1000, "Page size"),
}


def env_defaults():
    """Built-in defaults of the Connection page."""
    return {
        # From inside Docker, the host machine (where Gravitee usually runs
        # during a migration) is host.docker.internal.
        "base_url":   "http://host.docker.internal:8083",
        "org_id":     "DEFAULT",
        "env_id":     "DEFAULT",
        "token":      "",
        "username":   "",
        "password":   "",
        "verify_ssl": True,
        **{key: spec[0] for key, spec in ADVANCED.items()},
    }


def public_defaults():
    """Non-secret defaults for the Connection page."""
    d = env_defaults()
    return {
        "base_url": d["base_url"], "org_id": d["org_id"], "env_id": d["env_id"],
        "verify_ssl": d["verify_ssl"],
        **{key: spec[0] for key, spec in ADVANCED.items()},
    }


def resolve_config(payload):
    """Connection settings for one request: the fields of the Connection page
    over the .env defaults. Credentials must come from the page."""
    payload = payload or {}
    cfg = env_defaults()
    for key in ("base_url", "org_id", "env_id"):
        value = str(payload.get(key) or "").strip()
        if value:
            cfg[key] = value
    for key, (default, low, high, label) in ADVANCED.items():
        raw = payload.get(key)
        if raw in (None, ""):
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{label} must be a whole number.")
        if not low <= value <= high:
            raise ValueError(f"{label} must be between {low} and {high}.")
        cfg[key] = value
    if "verify_ssl" in payload:
        cfg["verify_ssl"] = bool(payload["verify_ssl"])
    if payload.get("auth_mode") == "basic":
        cfg["username"] = str(payload.get("username") or "").strip()
        cfg["password"] = str(payload.get("password") or "")
        if not cfg["username"] or not cfg["password"]:
            raise ValueError("Enter a username and password in the Connection page.")
    else:
        cfg["token"] = str(payload.get("token") or "").strip()
        if not cfg["token"]:
            raise ValueError("Enter a token in the Connection page.")
    if not cfg["base_url"].startswith(("http://", "https://")):
        raise ValueError("Base URL must start with http:// or https://")
    return cfg


def config_env(cfg):
    return {
        "GRAVITEE_BASE_URL": cfg["base_url"],
        "GRAVITEE_ORG_ID": cfg["org_id"],
        "GRAVITEE_ENV_ID": cfg["env_id"],
        "GRAVITEE_VERIFY_SSL": "true" if cfg["verify_ssl"] else "false",
        "GRAVITEE_RETRY_ATTEMPTS": str(cfg["retry_attempts"]),
        "GRAVITEE_RETRY_DELAY_S": str(cfg["retry_delay_s"]),
        "GRAVITEE_PAGE_SIZE": str(cfg["page_size"]),
    }


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------
def _context_path(api):
    if api.get("context_path"):
        return api["context_path"]
    proxy = api.get("proxy") or {}
    vhosts = proxy.get("virtual_hosts") or proxy.get("virtualHosts") or []
    if vhosts and isinstance(vhosts[0], dict):
        return vhosts[0].get("path", "")
    return ""


def sync_state(client, api_id):
    """Deployment sync state of one API.

    Returns {"state": IN_SYNC | OUT_OF_SYNC | NEVER_DEPLOYED | UNKNOWN,
             "deployed_at": ..., "error": ...}.
    Gravitee refuses (CAN_BE_FORCED) to migrate an API that is out of sync.
    """
    status, body = client._request("GET", client._url_v2(f"apis/{api_id}"))
    if status == 200:
        try:
            data = json.loads(body)
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        deployment = str(data.get("deploymentState") or "").upper()
        deployed_at = data.get("deployedAt")
        if deployment == "DEPLOYED":
            return {"state": "IN_SYNC", "deployed_at": deployed_at}
        if deployment == "NEED_REDEPLOY":
            return {"state": "OUT_OF_SYNC" if deployed_at else "NEVER_DEPLOYED", "deployed_at": deployed_at}
    # Fallback: older Management API endpoint
    s2, b2 = client._request("GET", client._url_v1(f"apis/{api_id}/state"))
    if s2 == 200:
        try:
            data2 = json.loads(b2)
            synced = data2.get("is_synchronized") if isinstance(data2, dict) else None
        except ValueError:
            synced = None
        if synced is True:
            return {"state": "IN_SYNC", "deployed_at": None}
        if synced is False:
            return {"state": "OUT_OF_SYNC", "deployed_at": None}
    return {"state": "UNKNOWN", "deployed_at": None,
            "error": f"HTTP {status}: {client._parse_error(status, body)}" if status else "No response"}


def list_v2_apis(cfg):
    client = gm.GraviteeClient(cfg)
    all_apis = client.list_all_apis()
    v2, others = [], {}
    for api in all_apis:
        version = gm.get_definition_version(api)
        if version != "V2":
            others[version] = others.get(version, 0) + 1
            continue
        owner = api.get("owner") or {}
        v2.append({
            "id": api.get("id", ""),
            "name": api.get("name", ""),
            "version": api.get("version", ""),
            "description": api.get("description", ""),
            "state": api.get("state", ""),
            "lifecycle_state": api.get("lifecycleState", api.get("lifecycle_state", "")),
            "visibility": api.get("visibility", ""),
            "context_path": _context_path(api),
            "owner": owner.get("displayName", "") if isinstance(owner, dict) else str(owner),
            "updated_at": api.get("updated_at", api.get("updatedAt", "")),
        })
    v2.sort(key=lambda a: a["name"].lower())
    if v2:
        with ThreadPoolExecutor(max_workers=min(8, len(v2))) as pool:
            def safe_sync(a):
                try:
                    return sync_state(client, a["id"])
                except Exception as exc:  # never let one API break the list
                    return {"state": "UNKNOWN", "deployed_at": None, "error": str(exc)}
            for api, sync in zip(v2, pool.map(safe_sync, v2)):
                api["sync"] = sync
    attempts = last_attempts(cfg)
    for api in v2:
        api["last_migration"] = attempts.get(api["id"])
    return {"apis": v2, "total": len(all_apis), "other_versions": others}


def last_attempts(cfg):
    """Latest migration attempt per API id, from the run history of the same
    Gravitee target (base URL + org + env)."""
    target = (cfg["base_url"].rstrip("/"), cfg["org_id"], cfg["env_id"])
    out = {}
    with RUNS_LOCK:
        runs = sorted(RUNS.values(), key=lambda r: r["id"])
        for run in runs:
            c = run.get("config") or {}
            if (str(c.get("base_url", "")).rstrip("/"), c.get("org_id"), c.get("env_id")) != target:
                continue
            base = {"run_id": run["id"], "force": bool(run.get("force")),
                    "at": run.get("finished_at") or run.get("created_at")}
            if run.get("status") == "running":
                for a in run.get("apis", []):
                    step = (run.get("live", {}).get(a["id"]) or {}).get("migration")
                    out[a["id"]] = dict(base, status=step["status"] if step else "RUNNING",
                                        error=(step or {}).get("error", ""))
                continue
            for r in run.get("results") or []:
                status = r.get("migration_status", "")
                if status not in ("SUCCESS", "FAILED"):
                    continue
                out[r.get("api_id", "")] = dict(base, status=status,
                                                error=r.get("migration_error", ""),
                                                at=r.get("timestamp") or base["at"])
    return out


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------
def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_dir(run_id):
    return RUNS_DIR / run_id


def _public_run(run, include_results=True):
    keys = ["id", "status", "created_at", "finished_at", "exit_code", "total",
            "progress", "current", "config", "auto_deploy", "auto_start", "keep_running",
            "summary", "error", "force"]
    out = {k: run.get(k) for k in keys}
    out["count"] = len(run.get("apis", []))
    if include_results:
        out["apis"] = run.get("apis", [])
        out["live"] = run.get("live", {})
        out["results"] = run.get("results")
    return out


def _save_meta(run):
    meta = {k: v for k, v in run.items() if k not in ("lines", "proc")}
    (_run_dir(run["id"]) / "meta.json").write_text(json.dumps(meta, indent=2))


def _summarize(results):
    summary = {}
    for step, key in (("migration", "migration_status"),
                      ("deploy", "deploy_status"),
                      ("start", "start_status")):
        summary[step] = {
            "success": sum(1 for r in results if r.get(key) == "SUCCESS"),
            "failed": sum(1 for r in results if r.get(key) == "FAILED"),
        }
    return summary


def _track_line(run, line):
    m = API_LINE_RE.search(line)
    if m:
        run["progress"] = int(m.group(1)) - 1
        run["total"] = int(m.group(2))
        run["current"] = m.group(4)
        run["live"].setdefault(m.group(4), {})
        return
    m = STEP_RE.search(line)
    if m and run.get("current"):
        step = {"Migration": "migration", "Deploy": "deploy", "Start": "start"}[m.group(1)]
        run["live"][run["current"]][step] = {"status": m.group(2), "error": m.group(3) or ""}
    elif "Start skipped" in line and run.get("current"):
        run["live"][run["current"]]["start"] = {"status": "NOT_RUN", "error": "Skipped - deploy failed"}


def _execute(run, cfg):
    run_dir = _run_dir(run["id"])
    env = {k: v for k, v in os.environ.items() if k not in CREDENTIAL_KEYS}
    env.update(config_env(cfg))
    env.update({
        "INPUT_CSV": str(run_dir / "input.csv"),
        "OUTPUT_CSV": str(run_dir / "results.csv"),
        "LOG_FILE": str(run_dir / "migration.log"),
        "PYTHONUNBUFFERED": "1",
    })
    console_path = run_dir / "console.log"
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", str(SCRIPT), "migrate", "--credentials-stdin",
             "--input", env["INPUT_CSV"], "--output", env["OUTPUT_CSV"]],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", bufsize=1, env=env, cwd=str(run_dir),
        )
        # Credentials go to the process on stdin: never in its environment,
        # its command line or any file.
        proc.stdin.write(json.dumps({"token": cfg["token"], "username": cfg["username"],
                                     "password": cfg["password"]}) + "\n")
        proc.stdin.close()
        cfg.update(token="", username="", password="")  # drop them from this process's memory
        with open(console_path, "w", encoding="utf-8") as console:
            for raw in proc.stdout:
                line = raw.rstrip("\n")
                console.write(line + "\n")
                console.flush()
                with RUNS_LOCK:
                    run["lines"].append(line)
                    _track_line(run, line)
        run["exit_code"] = proc.wait()
        results_path = run_dir / "results.csv"
        if results_path.exists():
            with open(results_path, newline="", encoding="utf-8") as f:
                run["results"] = list(csv.DictReader(f))
            run["summary"] = _summarize(run["results"])
        run["progress"] = run["total"] if run["exit_code"] == 0 else run["progress"]
        run["status"] = "completed" if run["exit_code"] == 0 and run.get("results") is not None else "failed"
        if run["status"] == "failed" and not run.get("error"):
            run["error"] = f"Migration tool exited with code {run['exit_code']}"
    except Exception as exc:  # pragma: no cover - defensive
        run["status"] = "failed"
        run["error"] = str(exc)
        run["lines"].append(traceback.format_exc())
    finally:
        run["finished_at"] = _now()
        run["current"] = None
        _save_meta(run)
        ACTIVE["id"] = None


def start_run(payload):
    cfg = resolve_config(payload.get("config"))
    apis = payload.get("apis") or []
    clean = []
    seen = set()
    for a in apis:
        api_id = str((a or {}).get("id", "")).strip()
        if not api_id or api_id in seen or len(api_id) > 200:
            continue
        seen.add(api_id)
        clean.append({
            "id": api_id,
            "name": str(a.get("name", ""))[:300],
            "version": str(a.get("version", ""))[:100],
            "state": str(a.get("state", ""))[:50],
            "lifecycle_state": str(a.get("lifecycle_state", ""))[:50],
        })
    if not clean:
        raise ValueError("Select at least one API to migrate.")
    auto_deploy = bool(payload.get("auto_deploy", True))
    # "Keep running": after migration + deploy, start again only the APIs that
    # were running before. Stopped APIs stay stopped, and nothing is started
    # without a deployment (the gateway would still run the V2 definition).
    keep_running = bool(payload.get("keep_running", True)) and auto_deploy
    auto_start = keep_running
    force = bool(payload.get("force", False))

    with RUNS_LOCK:
        if ACTIVE["id"]:
            raise RuntimeError("A migration is already running. Wait for it to finish.")
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        ACTIVE["id"] = run_id

    run_dir = _run_dir(run_id)
    run_dir.mkdir(parents=True)
    with open(run_dir / "input.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=INPUT_FIELDS)
        writer.writeheader()
        for a in clean:
            writer.writerow({
                "api_id": a["id"], "api_name": a["name"], "api_version": a["version"],
                "definition_version": "V2", "state": a["state"],
                "lifecycle_state": a["lifecycle_state"], "migrate_to_v4": "TRUE",
                "auto_deploy": "TRUE" if auto_deploy else "FALSE",
                "auto_start": "TRUE" if keep_running and a["state"].upper() == "STARTED" else "FALSE",
                "force_migration": "TRUE" if force else "FALSE",
            })

    run = {
        "id": run_id, "status": "running", "created_at": _now(), "finished_at": None,
        "exit_code": None, "total": len(clean), "progress": 0, "current": None,
        "config": {"base_url": cfg["base_url"], "org_id": cfg["org_id"], "env_id": cfg["env_id"]},
        "auto_deploy": auto_deploy, "auto_start": auto_start, "keep_running": keep_running,
        "force": force,
        "apis": [{"id": a["id"], "name": a["name"], "state": a["state"].upper()} for a in clean],
        "live": {}, "results": None, "summary": None, "error": None, "lines": [],
    }
    with RUNS_LOCK:
        RUNS[run_id] = run
    _save_meta(run)
    threading.Thread(target=_execute, args=(run, cfg), daemon=True).start()
    return run


AUDIT_FIELDS = [
    "run_id", "run_started_at", "run_finished_at", "run_status",
    "base_url", "org_id", "env_id", "force", "deploy", "keep_running",
    "api_id", "api_name", "migration_status", "migration_error",
    "deploy_status", "deploy_error", "start_status", "start_error", "timestamp",
]


def audit_csv():
    """One row per API per run, across the whole history."""
    import io
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=AUDIT_FIELDS, extrasaction="ignore")
    writer.writeheader()
    with RUNS_LOCK:
        runs = sorted(RUNS.values(), key=lambda r: r["id"])
        for run in runs:
            c = run.get("config") or {}
            base = {
                "run_id": run["id"], "run_started_at": run.get("created_at") or "",
                "run_finished_at": run.get("finished_at") or "", "run_status": run.get("status", ""),
                "base_url": c.get("base_url", ""), "org_id": c.get("org_id", ""), "env_id": c.get("env_id", ""),
                "force": "TRUE" if run.get("force") else "FALSE",
                "deploy": "TRUE" if run.get("auto_deploy") else "FALSE",
                "keep_running": "TRUE" if run.get("keep_running") else "FALSE",
            }
            by_id = {r.get("api_id"): r for r in (run.get("results") or [])}
            for api in run.get("apis", []):
                res = by_id.get(api["id"])
                if res is None:
                    live = (run.get("live") or {}).get(api["id"]) or {}
                    res = {"api_id": api["id"], "api_name": api.get("name", "")}
                    for step in ("migration", "deploy", "start"):
                        res[f"{step}_status"] = (live.get(step) or {}).get("status", "NOT_RUN")
                        res[f"{step}_error"] = (live.get(step) or {}).get("error", "")
                writer.writerow({**base, **res})
    return buf.getvalue()


def audit_logs():
    """All run logs concatenated, oldest first."""
    parts = []
    with RUNS_LOCK:
        runs = sorted(RUNS.values(), key=lambda r: r["id"])
    for run in runs:
        c = run.get("config") or {}
        names = ", ".join(a.get("name") or a["id"] for a in run.get("apis", []))
        parts.append("=" * 78)
        parts.append(f"Run {run['id']}  |  status: {run.get('status')}  |  started: {run.get('created_at')}"
                     f"  |  finished: {run.get('finished_at') or '-'}")
        parts.append(f"Target: {c.get('base_url')}  org {c.get('org_id')}  env {c.get('env_id')}")
        parts.append(f"APIs ({len(run.get('apis', []))}): {names}")
        parts.append("=" * 78)
        run_dir = _run_dir(run["id"])
        path = run_dir / "migration.log"
        if not path.exists():
            path = run_dir / "console.log"
        parts.append(path.read_text(encoding="utf-8", errors="replace").rstrip() if path.exists() else "(no log file)")
        parts.append("")
    return "\n".join(parts) + "\n"


def load_previous_runs():
    for d in sorted(RUNS_DIR.iterdir()):
        meta = d / "meta.json"
        if not meta.is_file():
            continue
        try:
            run = json.loads(meta.read_text())
        except Exception:
            continue
        if run.get("status") == "running":
            run["status"] = "interrupted"
            run["error"] = "The web server stopped while this run was in progress."
        run["lines"] = None  # read lazily from console.log
        RUNS[run["id"]] = run


def run_lines(run):
    if run.get("lines") is None:
        path = _run_dir(run["id"]) / "console.log"
        run["lines"] = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return run["lines"]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "GraviteeMigrationUI/1.0"

    def log_message(self, fmt, *args):
        if os.environ.get("WEB_ACCESS_LOG") == "1":
            super().log_message(fmt, *args)

    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode(), "application/json; charset=utf-8")

    def _payload(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 5_000_000:
            raise ValueError("Request too large")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

    def do_GET(self):
        path, _, query = self.path.partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        try:
            if path in ("/", "/index.html"):
                return self._send(200, INDEX_HTML.read_bytes(), "text/html; charset=utf-8")
            if path == "/healthz":
                return self._json(200, {"ok": True})
            if path == "/api/config":
                return self._json(200, {"defaults": public_defaults(), "active_run": ACTIVE["id"]})
            if path == "/api/runs":
                with RUNS_LOCK:
                    runs = sorted(RUNS.values(), key=lambda r: r["id"], reverse=True)
                    return self._json(200, {"runs": [_public_run(r, False) for r in runs],
                                            "active_run": ACTIVE["id"]})
            if path in ("/api/audit/export.csv", "/api/audit/logs.txt"):
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                if path.endswith(".csv"):
                    body, ctype, name = audit_csv(), "text/csv", f"migration-audit-{stamp}.csv"
                else:
                    body, ctype, name = audit_logs(), "text/plain", f"migration-logs-{stamp}.txt"
                return self._send(200, body.encode("utf-8"), ctype + "; charset=utf-8", {
                    "Content-Disposition": f'attachment; filename="{name}"'})
            parts = path.strip("/").split("/")
            if len(parts) >= 3 and parts[:2] == ["api", "runs"] and RUN_ID_RE.match(parts[2]):
                run = RUNS.get(parts[2])
                if not run:
                    return self._json(404, {"error": "Run not found"})
                if len(parts) == 3:
                    since = int(params.get("since", "0") or 0)
                    with RUNS_LOCK:
                        lines = run_lines(run)
                        out = _public_run(run)
                        out["lines"] = lines[since:]
                        out["next"] = len(lines)
                    return self._json(200, out)
                if len(parts) == 5 and parts[3] == "files":
                    files = {"results": "results.csv", "input": "input.csv",
                             "log": "migration.log", "console": "console.log"}
                    name = files.get(parts[4])
                    fpath = _run_dir(run["id"]) / name if name else None
                    if not fpath or not fpath.exists():
                        return self._json(404, {"error": "File not found"})
                    ctype = "text/csv" if name.endswith(".csv") else "text/plain"
                    return self._send(200, fpath.read_bytes(), ctype + "; charset=utf-8", {
                        "Content-Disposition": f'attachment; filename="{run["id"]}-{name}"'})
            return self._json(404, {"error": "Not found"})
        except Exception as exc:
            return self._json(500, {"error": str(exc)})

    def do_POST(self):
        path = self.path.partition("?")[0]
        try:
            payload = self._payload()
            if path == "/api/apis":
                cfg = resolve_config(payload)
                return self._json(200, list_v2_apis(cfg))
            if path == "/api/runs":
                run = start_run(payload)
                return self._json(201, _public_run(run))
            return self._json(404, {"error": "Not found"})
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        except RuntimeError as exc:
            msg = str(exc)
            return self._json(409 if "already running" in msg else 502, {"error": msg})
        except Exception as exc:
            return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})


def main():
    load_previous_runs()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Gravitee V2 -> V4 migration UI listening on http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
