"""Configuration for the FiveClaw Agent."""

import os
import json
from pathlib import Path


def load_env_file():
    for candidate in [Path.cwd() / ".env", Path(__file__).parent.parent / ".env"]:
        if candidate.exists():
            with open(candidate) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, value = line.split("=", 1)
                        if key not in os.environ:
                            os.environ[key] = value
            break


class Config:
    def __init__(self):
        load_env_file()

        self.api_key = os.getenv("FIVECLAW_API_KEY", "")
        self.api_url = os.getenv("FIVECLAW_API_URL", "https://fiveclaw.xyz").rstrip("/")

        if not self.api_key:
            raise RuntimeError(
                "FIVECLAW_API_KEY is not set.\n"
                "Add it to your .env file: FIVECLAW_API_KEY=fc_live_...\n"
                "Get your key at https://fiveclaw.xyz/dashboard/keys"
            )

        # Fetch dashboard config once — env vars always take priority over remote
        remote = self._fetch_remote_config()

        # Local project root: env var > remote config > auto-detect
        env_root = os.getenv("FIVEM_PROJECT_ROOT", "")
        remote_root = remote.get("projectRoot", "")
        if env_root and Path(env_root).exists():
            self.project_root = Path(env_root)
        elif remote_root and Path(remote_root).exists():
            self.project_root = Path(remote_root)
        else:
            self.project_root = self._detect_project_root()

        # Resources dir: env var > remote config > project_root/resources
        resources_override = os.getenv("FIVEM_RESOURCES_DIR", "")
        remote_resources = remote.get("resourcesDir", "")
        if resources_override:
            self.resources_dir = Path(resources_override)
        elif remote_resources and Path(remote_resources).exists():
            self.resources_dir = Path(remote_resources)
        else:
            self.resources_dir = self.project_root / "resources"

        logs_override = os.getenv("FIVEM_LOGS_DIR", remote.get("logsDir", ""))
        self.logs_dir = Path(logs_override) if logs_override else self.project_root / "logs"
        self.logs_dir_explicit = bool(logs_override)
        self.context_dir = self.project_root / ".fiveclaw" / "context"
        self.context_dir.mkdir(parents=True, exist_ok=True)

        # SSH
        self.ssh = {
            "host":     os.getenv("FIVEM_SSH_HOST", remote.get("host",       "")),
            "port":     int(os.getenv("FIVEM_SSH_PORT", str(remote.get("port", 22)))),
            "user":     os.getenv("FIVEM_SSH_USER", remote.get("sshUser",    "")),
            "key_path": os.getenv("FIVEM_SSH_KEY",  remote.get("sshKeyPath", "")),
        }

        # MySQL
        self.mysql = {
            "host":     os.getenv("MYSQL_HOST",     remote.get("mysqlHost",     "127.0.0.1")),
            "port":     int(os.getenv("MYSQL_PORT", str(remote.get("mysqlPort", 3306)))),
            "user":     os.getenv("MYSQL_USER",     remote.get("mysqlUser",     "")),
            "password": os.getenv("MYSQL_PASSWORD", remote.get("mysqlPassword", "")),
            "database": os.getenv("MYSQL_DATABASE", remote.get("mysqlDatabase", "")),
        }

        # txAdmin
        self.txadmin_url  = os.getenv("TXADMIN_URL",  remote.get("txAdminUrl",  "") or "http://localhost:40120")
        self.txadmin_user = os.getenv("TXADMIN_USER", remote.get("txAdminUser", "") or "")
        self.txadmin_pass = os.getenv("TXADMIN_PASS", remote.get("txAdminPass", "") or "")

        # Admin panel (txadmin | custom)
        self.admin_panel_type = os.getenv("ADMIN_PANEL_TYPE", remote.get("adminPanelType", "txadmin"))
        self.custom_panel_url = os.getenv("ADMIN_PANEL_URL",  remote.get("adminPanelUrl",  ""))

        # Custom panel endpoints — must be set in MCP env config
        self.custom_panel_status_endpoint  = os.getenv("ADMIN_PANEL_STATUS_ENDPOINT",  "")
        self.custom_panel_start_endpoint   = os.getenv("ADMIN_PANEL_START_ENDPOINT",   "")
        self.custom_panel_stop_endpoint    = os.getenv("ADMIN_PANEL_STOP_ENDPOINT",    "")
        self.custom_panel_command_endpoint = os.getenv("ADMIN_PANEL_COMMAND_ENDPOINT", "")

        # Extra named databases via flat env vars: MYSQL_<NAME>_HOST, _USER, _PASSWORD, _DATABASE, _PORT
        # e.g. MYSQL_TRUCKING_HOST=127.0.0.1, MYSQL_TRUCKING_USER=root, ...
        import re as _re
        self.extra_databases: dict = {}
        for _key in os.environ:
            _m = _re.match(r'^MYSQL_([A-Z0-9]+(?:_[A-Z0-9]+)*)_HOST$', _key)
            if _m:
                _name = _m.group(1).lower()
                _pfx  = f"MYSQL_{_m.group(1)}"
                self.extra_databases[_name] = {
                    "host":     os.environ[_key],
                    "port":     int(os.getenv(f"{_pfx}_PORT", "3306")),
                    "user":     os.getenv(f"{_pfx}_USER",     ""),
                    "password": os.getenv(f"{_pfx}_PASSWORD", ""),
                    "database": os.getenv(f"{_pfx}_DATABASE", ""),
                }

    def has_ssh(self) -> bool:
        return bool(self.ssh["host"] and self.ssh["user"])

    def has_mysql(self, name: str = "default") -> bool:
        if name != "default" and name not in self.extra_databases:
            return False
        db = self.get_db(name)
        return bool(db.get("user") and db.get("database"))

    def get_db(self, name: str = "default") -> dict:
        if name != "default" and name in self.extra_databases:
            return self.extra_databases[name]
        return self.mysql

    def _fetch_remote_config(self) -> dict:
        """Fetch server config from the FiveClaw dashboard API. Returns {} on failure."""
        import ssl, urllib.request, urllib.error
        if not self.api_url.startswith("https://"):
            raise ValueError(
                f"FIVECLAW_API_URL must use HTTPS (got: {self.api_url!r}). "
                "All API calls must be encrypted."
            )
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            req = urllib.request.Request(
                f"{self.api_url}/api/user/server-config",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "User-Agent":    "FiveClaw-Agent/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=5, context=ctx) as r:
                return json.loads(r.read().decode()) or {}
        except Exception:
            return {}

    def _detect_project_root(self) -> Path:
        check = Path.cwd()
        for _ in range(10):
            res = check / "resources"
            if res.is_dir() and (
                list(res.glob("*/fxmanifest.lua")) or          # flat: resources/fc-core/
                list(res.glob("*/*/fxmanifest.lua"))           # categorised: resources/[local]/fc-core/
            ):
                return check
            parent = check.parent
            if parent == check:
                break
            check = parent
        return Path.cwd()
