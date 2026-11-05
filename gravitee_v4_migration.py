#!/usr/bin/env python3
import argparse, base64, csv, getpass, json, logging, os, ssl, sys, time
import urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone

# ===========================================================================
# CONFIG - connection settings are read from environment variables (.env).
# Credentials are never read from .env or the environment: the web interface
# sends them on stdin (--credentials-stdin), and on the command line the script
# asks for them (see ask_credentials).
# ===========================================================================
CONFIG = {
    "base_url":       os.environ.get("GRAVITEE_BASE_URL",       "http://localhost:8083"),
    "org_id":         os.environ.get("GRAVITEE_ORG_ID",         "DEFAULT"),
    "env_id":         os.environ.get("GRAVITEE_ENV_ID",         "DEFAULT"),
    "token":          "",
    "username":       "",
    "password":       "",
    "verify_ssl":     os.environ.get("GRAVITEE_VERIFY_SSL",     "true").lower() == "true",
    "retry_attempts": int(os.environ.get("GRAVITEE_RETRY_ATTEMPTS", "3")),
    "retry_delay_s":  int(os.environ.get("GRAVITEE_RETRY_DELAY_S",  "2")),
    "page_size":      int(os.environ.get("GRAVITEE_PAGE_SIZE",      "100")),
}
INPUT_CSV  = os.environ.get("INPUT_CSV",  "/data/migration_input.csv")
OUTPUT_CSV = os.environ.get("OUTPUT_CSV", "/data/migration_results.csv")
LOG_FILE   = os.environ.get("LOG_FILE",   "/data/gravitee_migration.log")
# ===========================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


class GraviteeClient:
    def __init__(self, cfg):
        self.base      = cfg["base_url"].rstrip("/")
        self.org       = cfg["org_id"]
        self.env       = cfg["env_id"]
        self.retries   = cfg["retry_attempts"]
        self.delay     = cfg["retry_delay_s"]
        self.page_size = cfg["page_size"]
        self.last_details = []
        if cfg.get("token"):
            self.auth_header = f"Bearer {cfg['token']}"
        else:
            creds = base64.b64encode(
                f"{cfg['username']}:{cfg['password']}".encode()
            ).decode()
            self.auth_header = f"Basic {creds}"
        if cfg["verify_ssl"]:
            self.ssl_ctx = ssl.create_default_context()
        else:
            self.ssl_ctx = ssl._create_unverified_context()

    def _url_v1(self, path):
        return (f"{self.base}/management/organizations/{self.org}"
                f"/environments/{self.env}/{path.lstrip('/')}")

    def _url_v2(self, path):
        return (f"{self.base}/management/v2/organizations/{self.org}"
                f"/environments/{self.env}/{path.lstrip('/')}")

    def _request(self, method, url, params=None, body=None):
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("Authorization", self.auth_header)
        last_err = (0, "")
        for attempt in range(1, self.retries + 1):
            try:
                with urllib.request.urlopen(req, context=self.ssl_ctx, timeout=30) as resp:
                    return resp.status, resp.read().decode("utf-8")
            except urllib.error.HTTPError as e:
                status = e.code
                body_text = e.read().decode("utf-8") if e.fp else ""
                if status < 500:
                    return status, body_text
                log.warning("Server error %s attempt %d/%d", status, attempt, self.retries)
                last_err = (status, body_text)
            except Exception as exc:
                log.warning("Exception attempt %d/%d: %s", attempt, self.retries, exc)
                last_err = (0, str(exc))
            if attempt < self.retries:
                time.sleep(self.delay)
        return last_err

    @staticmethod
    def _parse_error(status, body):
        try:
            obj = json.loads(body)
            return obj.get("message") or obj.get("error") or body[:300]
        except Exception:
            return body[:300] if body else f"HTTP {status}"

    def list_all_apis(self):
        apis, page = [], 1
        while True:
            url = self._url_v1("apis")
            status, body = self._request("GET", url, params={"page": page, "size": self.page_size})
            if status != 200:
                raise RuntimeError(f"Failed to list APIs (HTTP {status}): {self._parse_error(status, body)}")
            data = json.loads(body)
            if isinstance(data, list):
                apis.extend(data)
                break
            batch = data.get("data", data.get("apis", []))
            apis.extend(batch)
            total_pages = data.get("pageCount", data.get("total_pages", 1))
            if not batch or page >= total_pages:
                break
            page += 1
        return apis

    def migrate_to_v4(self, api_id, force=False):
        """Migrate an API and only report success once Gravitee confirms it.

        Gravitee answers HTTP 200 even when it refuses to migrate: the body then
        carries a state (CAN_BE_FORCED / IMPOSSIBLE) and the list of issues.
        Only state MIGRATED - or the API now reading back as V4 - is a success.

        On failure, self.last_details holds extra lines (Gravitee's state, each
        issue, a hint) that explain why, for the log.
        """
        self.last_details = []
        url = self._url_v2(f"apis/{api_id}/_migrate")
        status, body = self._request("POST", url, params={"mode": "FORCE"} if force else None)
        if status not in (200, 201, 202):
            msg = self._parse_error(status, body)
            self.last_details = [f"Gravitee response : HTTP {status or 'no response'}"]
            if body and body.strip() and body.strip()[:300] != msg:
                self.last_details.append(f"Response body     : {body.strip()[:500]}")
            return False, f"HTTP {status}: {msg}" if status else msg
        try:
            data = json.loads(body) if body else {}
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        state = str(data.get("state", "")).upper()
        if state == "MIGRATED":
            return True, "Migration succeeded"

        # No explicit confirmation: check what the API looks like now.
        s2, b2 = self._request("GET", self._url_v2(f"apis/{api_id}"))
        if s2 == 200:
            try:
                if get_definition_version(json.loads(b2)) == "V4":
                    return True, "Migration succeeded"
            except ValueError:
                pass

        issues = [i for i in (data.get("issues") or []) if isinstance(i, dict) and i.get("message")]
        messages = "; ".join(str(i["message"]).strip() for i in issues)
        if state == "CAN_BE_FORCED" and not force:
            reason = "Not migrated (can be forced)"
            hint = ("Fix the issue(s) above in APIM Console (e.g. deploy the API) and retry, "
                    "or retry with Force migration.")
        elif state == "IMPOSSIBLE":
            reason = "Not migrated (impossible)"
            hint = "Gravitee cannot migrate this API automatically. Fix the issue(s) above first; Force does not apply."
        elif state:
            reason = f"Not migrated ({state})"
            hint = "Check the issue(s) above in APIM Console and retry."
        else:
            reason = "Not migrated - Gravitee did not confirm the migration, API is still V2"
            hint = "Gravitee returned no migration state. Check the Management API logs."

        self.last_details.append(
            f"Gravitee response : HTTP {status}, state {state or '(none)'}{' (force mode)' if force else ''}")
        for i in issues:
            self.last_details.append(f"Issue [{i.get('state', '?')}] : {str(i['message']).strip()}")
        if not issues and body and body.strip() not in ("", "{}"):
            self.last_details.append(f"Response body     : {body.strip()[:500]}")
        self.last_details.append(f"How to fix        : {hint}")
        return False, f"{reason}: {messages}" if messages else reason

    def deploy_api(self, api_id):
        url = self._url_v2(f"apis/{api_id}/deployments")
        status, body = self._request("POST", url)
        if status in (200, 202):
            return True, "Deployment succeeded"
        return False, f"HTTP {status}: {self._parse_error(status, body)}"

    def start_api(self, api_id):
        url = self._url_v2(f"apis/{api_id}/_start")
        status, body = self._request("POST", url)
        if status in (200, 202, 204):
            return True, "Start succeeded"
        err = self._parse_error(status, body)
        if "already started" in err.lower():
            return True, "Already started (no action needed)"
        return False, f"HTTP {status}: {err}"


def get_definition_version(api):
    raw = (api.get("definitionVersion")
           or api.get("definition_version")
           or api.get("gravitee", "")
           or "")
    raw = str(raw).strip().upper()
    if raw in ("2.0.0", "V2", "2"):
        return "V2"
    if raw in ("4.0.0", "V4", "4"):
        return "V4"
    if raw in ("1.0.0", "V1", "1"):
        return "V1"
    return raw or "UNKNOWN"


def generate_csv(client, output_path):
    log.info("Fetching API list from Gravitee APIM ...")
    all_apis = client.list_all_apis()
    log.info("Total APIs retrieved: %d", len(all_apis))

    v2_apis = [a for a in all_apis if get_definition_version(a) == "V2"]
    skipped = len(all_apis) - len(v2_apis)
    log.info("V2 APIs: %d  |  Skipped (non-V2): %d", len(v2_apis), skipped)

    if not v2_apis:
        log.warning("No V2 APIs found - nothing to write.")
        return

    fieldnames = [
        "api_id", "api_name", "api_version", "definition_version",
        "state", "lifecycle_state", "migrate_to_v4", "auto_deploy", "auto_start",
        "force_migration",
    ]

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for api in v2_apis:
            writer.writerow({
                "api_id":             api.get("id", ""),
                "api_name":           api.get("name", ""),
                "api_version":        api.get("version", ""),
                "definition_version": "V2",
                "state":              api.get("state", ""),
                "lifecycle_state":    api.get("lifecycleState", api.get("lifecycle_state", "")),
                "migrate_to_v4":      "TRUE",
                "auto_deploy":        "TRUE",
                "auto_start":         "TRUE",
                "force_migration":    "FALSE",
            })

    print(f"\n✅  Input CSV created: {output_path}")
    print(f"    {len(v2_apis)} V2 APIs listed  |  {skipped} non-V2 APIs excluded")
    print(f"    Edit migrate_to_v4 / auto_deploy / auto_start as needed, then run:")
    print(f"    docker compose run --rm migrate\n")


def run_migration(client, input_path, output_path):
    if not os.path.exists(input_path):
        log.error("Input file not found: %s", input_path)
        sys.exit(1)
    with open(input_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    log.info("Loaded %d rows", len(rows))
    out_fields = [
        "api_id", "api_name", "migrate_to_v4", "auto_deploy", "auto_start",
        "migration_status", "migration_error",
        "deploy_status", "deploy_error",
        "start_status", "start_error", "timestamp", "force_migration",
    ]
    results = []
    for i, row in enumerate(rows, 1):
        api_id     = row.get("api_id", "").strip()
        api_name   = row.get("api_name", "").strip()
        do_migrate = row.get("migrate_to_v4", "FALSE").strip().upper() == "TRUE"
        do_deploy  = row.get("auto_deploy",   "FALSE").strip().upper() == "TRUE"
        do_start   = row.get("auto_start",    "FALSE").strip().upper() == "TRUE"
        do_force   = (row.get("force_migration") or "FALSE").strip().upper() == "TRUE"
        log.info("[%d/%d] %s (%s)", i, len(rows), api_name, api_id)
        result = {
            "api_id": api_id, "api_name": api_name,
            "migrate_to_v4": row.get("migrate_to_v4", ""),
            "auto_deploy":   row.get("auto_deploy", ""),
            "auto_start":    row.get("auto_start", ""),
            "migration_status": "SKIPPED", "migration_error": "",
            "deploy_status":    "NOT_RUN", "deploy_error": "",
            "start_status":     "NOT_RUN", "start_error": "",
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "force_migration": "TRUE" if do_force else "FALSE",
        }
        if not api_id:
            result["migration_error"] = "No api_id in row"
            results.append(result)
            continue

        migration_ok = False
        if do_migrate:
            ok, msg = client.migrate_to_v4(api_id, force=do_force)
            if ok:
                result["migration_status"] = "SUCCESS"
                migration_ok = True
                log.info("    ✅ Migration SUCCESS")
            else:
                result["migration_status"] = "FAILED"
                result["migration_error"]  = msg
                log.error("    ❌ Migration FAILED: %s", msg)
                for line in getattr(client, "last_details", []):
                    log.error("         %s", line)

        deploy_ok = False
        if do_deploy:
            if migration_ok:
                ok, msg = client.deploy_api(api_id)
                if ok:
                    result["deploy_status"] = "SUCCESS"
                    deploy_ok = True
                    log.info("    ✅ Deploy SUCCESS")
                else:
                    result["deploy_status"] = "FAILED"
                    result["deploy_error"]  = msg
                    log.error("    ❌ Deploy FAILED: %s", msg)
            elif not do_migrate:
                result["deploy_status"] = "NOT_RUN"
                result["deploy_error"]  = "Skipped - migration not requested"
            else:
                result["deploy_status"] = "NOT_RUN"
                result["deploy_error"]  = "Skipped - migration failed"
        else:
            result["deploy_status"] = "SKIPPED"

        if do_start:
            deploy_prerequisite_met = (not do_deploy) or deploy_ok
            if migration_ok and deploy_prerequisite_met:
                ok, msg = client.start_api(api_id)
                if ok:
                    result["start_status"] = "SUCCESS"
                    log.info("    ✅ Start SUCCESS")
                else:
                    result["start_status"] = "FAILED"
                    result["start_error"]  = msg
                    log.error("    ❌ Start FAILED: %s", msg)
            elif not migration_ok and not do_migrate:
                result["start_status"] = "NOT_RUN"
                result["start_error"]  = "Skipped - migration not requested"
            elif not migration_ok:
                result["start_status"] = "NOT_RUN"
                result["start_error"]  = "Skipped - migration failed"
            else:
                result["start_status"] = "NOT_RUN"
                result["start_error"]  = "Skipped - deploy failed"
                log.warning("    ⚠️  Start skipped - deploy did not succeed")
        else:
            result["start_status"] = "SKIPPED"

        results.append(result)

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_fields)
        writer.writeheader()
        writer.writerows(results)
    total = len(results)
    print(f"\n{'='*55}")
    print(f"  Done: {total} APIs processed")
    print(f"  Migration : {sum(1 for r in results if r['migration_status']=='SUCCESS')} ok / {sum(1 for r in results if r['migration_status']=='FAILED')} failed")
    print(f"  Deploy    : {sum(1 for r in results if r['deploy_status']=='SUCCESS')} ok / {sum(1 for r in results if r['deploy_status']=='FAILED')} failed")
    print(f"  Start     : {sum(1 for r in results if r['start_status']=='SUCCESS')} ok / {sum(1 for r in results if r['start_status']=='FAILED')} failed")
    print(f"  Results   : {output_path}")
    print(f"  Log       : {LOG_FILE}")
    print(f"{'='*55}\n")


def read_credentials_stdin(cfg):
    """Credentials sent by the web interface as one JSON line on stdin."""
    data = json.loads(sys.stdin.readline() or "{}")
    cfg["token"] = str(data.get("token") or "")
    cfg["username"] = str(data.get("username") or "")
    cfg["password"] = str(data.get("password") or "")
    if not cfg["token"] and not (cfg["username"] and cfg["password"]):
        sys.exit("No credentials received.")


def ask_credentials(cfg):
    """Prompt for credentials (command-line use)."""
    if not sys.stdin.isatty():
        sys.exit("No credentials provided. Run interactively to be prompted, "
                 "or use the web interface.")
    print(f"Gravitee Management API: {cfg['base_url']}  (org {cfg['org_id']}, env {cfg['env_id']})")
    token = getpass.getpass("Personal access token (leave empty to use username/password): ").strip()
    if token:
        cfg["token"] = token
        return
    cfg["username"] = input("Username: ").strip()
    cfg["password"] = getpass.getpass("Password: ")
    if not cfg["username"] or not cfg["password"]:
        sys.exit("A token, or a username and password, is required.")


def main():
    parser = argparse.ArgumentParser(description="Gravitee APIM v4 Migration")
    sub = parser.add_subparsers(dest="command", required=True)
    gp = sub.add_parser("generate")
    gp.add_argument("--output", default=INPUT_CSV)
    gp.add_argument("--credentials-stdin", action="store_true", help=argparse.SUPPRESS)
    mp = sub.add_parser("migrate")
    mp.add_argument("--input",  default=INPUT_CSV)
    mp.add_argument("--output", default=OUTPUT_CSV)
    mp.add_argument("--credentials-stdin", action="store_true", help=argparse.SUPPRESS)
    args   = parser.parse_args()
    if args.credentials_stdin:
        read_credentials_stdin(CONFIG)
    else:
        ask_credentials(CONFIG)
    client = GraviteeClient(CONFIG)
    if args.command == "generate":
        generate_csv(client, args.output)
    elif args.command == "migrate":
        run_migration(client, args.input, args.output)

if __name__ == "__main__":
    main()
