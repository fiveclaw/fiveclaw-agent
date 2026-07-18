#!/usr/bin/env python3
"""Configuration management for the FiveClaw agent's local engines"""

import os
import json
import urllib.request
from pathlib import Path


def load_env_file():
    """Load environment variables from .env file if it exists"""
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    if key not in os.environ:
                        os.environ[key] = value


def _fetch_remote_config(api_key: str, api_url: str) -> dict:
    """Fetch server config from the FiveClaw dashboard API."""
    try:
        req = urllib.request.Request(
            f"{api_url.rstrip('/')}/api/user/server-config",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return {}


class Config:
    """Configuration for the MCP server.

    Priority order for each setting:
      1. Environment variable (set in shell or MCP server env)
      2. FiveClaw dashboard (fetched once at startup via FIVECLAW_API_KEY)
      3. Auto-detection (project root only)
      4. Sensible defaults
    """

    def __init__(self):
        load_env_file()

        # ------------------------------------------------------------------
        # FiveClaw SaaS integration
        # ------------------------------------------------------------------
        self.fiveclaw_api_key = os.getenv("FIVECLAW_API_KEY", "")
        self.fiveclaw_api_url = os.getenv("FIVECLAW_API_URL", "https://fiveclaw.xyz")

        # Fetch remote config once at startup (only when API key is set)
        remote: dict = {}
        if self.fiveclaw_api_key:
            remote = _fetch_remote_config(self.fiveclaw_api_key, self.fiveclaw_api_url)

        def env_or_remote(env_key: str, remote_key: str, default=""):
            return os.getenv(env_key) or remote.get(remote_key) or default

        # ------------------------------------------------------------------
        # Project root
        # ------------------------------------------------------------------
        env_root = env_or_remote("FIVEM_PROJECT_ROOT", "projectRoot")
        if env_root and Path(env_root).exists():
            self.project_root = Path(env_root)
        else:
            # Auto-detect: walk up from cwd looking for resources/ + fxmanifest.lua
            cwd = Path.cwd()
            self.project_root = None
            check = cwd
            for _ in range(10):
                res = check / "resources"
                if res.is_dir() and (
                    list(res.glob("*/fxmanifest.lua")) or        # flat layout
                    list(res.glob("*/*/fxmanifest.lua"))         # categorised: [local]/fc-core/
                ):
                    self.project_root = check
                    break
                parent = check.parent
                if parent == check:
                    break
                check = parent
            if self.project_root is None:
                import sys as _sys
                print(
                    f"WARNING: FIVEM_PROJECT_ROOT unset/invalid and no resources/ found "
                    f"walking up from {cwd}; falling back to cwd. Server-disk tools may read "
                    f"the wrong directory — set FIVEM_PROJECT_ROOT.",
                    file=_sys.stderr,
                )
                self.project_root = cwd

        # ------------------------------------------------------------------
        # Paths (relative to project root unless overridden)
        # ------------------------------------------------------------------
        resources_override = env_or_remote("FIVEM_RESOURCES_DIR", "resourcesDir")
        self.resources_dir = (
            Path(resources_override)
            if resources_override
            else self.project_root / "resources"
        )

        self.logs_dir = self.project_root / "logs"
        self.cache_dir = self.project_root / ".fiveclaw" / "cache"
        self.context_dir = self.project_root / ".fiveclaw" / "context"
        self.context_dir.mkdir(parents=True, exist_ok=True)

        # ------------------------------------------------------------------
        # txAdmin panel
        # ------------------------------------------------------------------
        # Default port 40120 is the txAdmin web UI (not the game port 30120)
        self.admin_url = env_or_remote("TXADMIN_URL", "txAdminUrl", "http://localhost:40120")
        self.admin_token = env_or_remote("TXADMIN_TOKEN", "txAdminToken", "")

        # ------------------------------------------------------------------
        # MySQL
        # Single primary database.  For multiple databases use MYSQL_EXTRA_DBS.
        # ------------------------------------------------------------------
        self.mysql = {
            "host":     env_or_remote("MYSQL_HOST",     "mysqlHost",     "127.0.0.1"),
            "port":     int(env_or_remote("MYSQL_PORT", "mysqlPort",     "3306")),
            "user":     env_or_remote("MYSQL_USER",     "mysqlUser",     ""),
            "password": env_or_remote("MYSQL_PASSWORD", "mysqlPassword", ""),
            "database": env_or_remote("MYSQL_DATABASE", "mysqlDatabase", ""),
        }

        # Optional named extra databases as JSON:
        # MYSQL_EXTRA_DBS='{"qbcore":{"host":"...","user":"...","password":"...","database":"qbcore"}}'
        extra_raw = os.getenv("MYSQL_EXTRA_DBS", "")
        try:
            self.extra_databases: dict = json.loads(extra_raw) if extra_raw else {}
        except json.JSONDecodeError:
            self.extra_databases = {}

        # ------------------------------------------------------------------
        # SSH / remote deployment
        # ------------------------------------------------------------------
        self.ssh = {
            "host":     env_or_remote("FIVEM_SSH_HOST", "host",       ""),
            "port":     int(env_or_remote("FIVEM_SSH_PORT", "port",   "22")),
            "user":     env_or_remote("FIVEM_SSH_USER",  "sshUser",   ""),
            "key_path": env_or_remote("FIVEM_SSH_KEY",   "sshKeyPath",""),
        }

        # Remote path to the resources directory on the FiveM server
        self.remote_resources_dir = env_or_remote(
            "FIVEM_REMOTE_RESOURCES_DIR", "resourcesDir", ""
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def has_mysql(self, name: str = "default") -> bool:
        """Return True if the named database is fully configured."""
        db = self.get_db(name)
        return bool(db.get("user") and db.get("password") and db.get("database"))

    def get_db(self, name: str = "default") -> dict:
        """Return connection dict for the named database (or primary if not found)."""
        if name != "default" and name in self.extra_databases:
            return self.extra_databases[name]
        return self.mysql

    def has_ssh(self) -> bool:
        """Return True if SSH deployment is configured."""
        return bool(self.ssh.get("host") and self.ssh.get("user"))

    # Backwards-compat aliases used by un-migrated code
    def has_mysql_config(self, db_name: str = "default") -> bool:
        return self.has_mysql(db_name)

    def get_db_config(self, db_name: str = "default") -> dict:
        return self.get_db(db_name)
