"""
Local tool implementations — file I/O, SSH, MySQL, txAdmin.
These run entirely on the user's machine. No source logic is sent to the VPS.
"""

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from .config import Config


# ─── File collection helpers ──────────────────────────────────────────────────

def collect_resource_files(resources_dir: Path, resource_name: Optional[str] = None) -> dict[str, str]:
    """
    Collect Lua (and JS/TS) source files from one or all resources.
    Returns {relative_path: content} — content capped at 50 KB per file.
    """
    MAX_FILE = 50_000
    files: dict[str, str] = {}

    if resource_name:
        # Check direct path first, then search one level of category subdirs (e.g. [local], [system])
        direct = resources_dir / resource_name
        if direct.exists():
            dirs = [direct]
        else:
            found = None
            for cat in resources_dir.iterdir():
                if cat.is_dir():
                    candidate = cat / resource_name
                    if candidate.exists():
                        found = candidate
                        break
            dirs = [found] if found else [direct]  # fall back so the loop below skips gracefully
    else:
        # Flat layout (resources/fc-core) and categorised ([local]/fc-core, [system]/baseevents)
        all_dirs = []
        for d in resources_dir.iterdir():
            if not d.is_dir():
                continue
            if (d / "fxmanifest.lua").exists():
                all_dirs.append(d)
            else:
                # Treat as a category folder — add its children
                for sub in d.iterdir():
                    if sub.is_dir():
                        all_dirs.append(sub)
        dirs = all_dirs

    for rdir in dirs:
        if not rdir.exists():
            continue
        for ext in ("*.lua", "*.js", "*.ts", "*.html", "*.css"):
            for f in rdir.rglob(ext):
                try:
                    content = f.read_text(errors="ignore")
                    if len(content) > MAX_FILE:
                        content = content[:MAX_FILE] + "\n-- [truncated]"
                    rel = str(f.relative_to(resources_dir.parent))
                    files[rel] = content
                except Exception:
                    pass

    return files


# ─── RepoMap ──────────────────────────────────────────────────────────────────

class RepoMapTool:
    def __init__(self, config: Config):
        self.config = config
        self._cache_file = config.context_dir / "repomap.json"

    async def generate(self) -> str:
        if not self.config.resources_dir.exists():
            return json.dumps({"error": f"Resources directory not found: {self.config.resources_dir}"})

        resources = {}
        for rdir in self.config.resources_dir.iterdir():
            if not rdir.is_dir():
                continue
            info: dict = {"name": rdir.name, "files": [], "exports": [], "events": []}
            for f in rdir.rglob("*.lua"):
                info["files"].append(str(f.relative_to(rdir)))
                try:
                    text = f.read_text(errors="ignore")
                    info["exports"] += re.findall(r'exports\[[\'"](.*?)[\'"]\]', text)
                    info["events"]  += re.findall(r'RegisterNetEvent\([\'"]([^\'"]+)[\'"]', text)
                except Exception:
                    pass
            resources[rdir.name] = info

        result = {"resources": resources, "count": len(resources)}
        self._cache_file.parent.mkdir(parents=True, exist_ok=True)
        self._cache_file.write_text(json.dumps(result, indent=2))
        return json.dumps({"success": True, "resources_count": len(resources)})

    async def query(self, query_type: str, filter: Optional[str] = None) -> str:
        if not self._cache_file.exists():
            return json.dumps({"error": "Run repomap_generate first."})
        data = json.loads(self._cache_file.read_text())
        resources = data.get("resources", {})

        if filter:
            resources = {k: v for k, v in resources.items() if filter.lower() in k.lower()}

        if query_type == "exports":
            out = {k: v.get("exports", []) for k, v in resources.items()}
        elif query_type == "events":
            out = {k: v.get("events", []) for k, v in resources.items()}
        elif query_type == "files":
            out = {k: v.get("files", []) for k, v in resources.items()}
        else:
            out = resources

        return json.dumps(out, indent=2)

    async def show(self) -> str:
        if not self._cache_file.exists():
            return json.dumps({"error": "Run repomap_generate first."})
        return self._cache_file.read_text()


# ─── Search / File info ───────────────────────────────────────────────────────

class FileTool:
    def __init__(self, config: Config):
        self.config = config

    async def search(self, pattern: str, path: Optional[str] = None) -> str:
        search_path = Path(path) if path else self.config.resources_dir
        if not search_path.exists():
            return json.dumps({"error": f"Path not found: {search_path}"})

        matches = []
        for ext in ("*.lua", "*.js", "*.ts", "*.json"):
            for f in search_path.rglob(ext):
                try:
                    for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                        if re.search(pattern, line, re.IGNORECASE):
                            matches.append({
                                "file": str(f.relative_to(search_path)),
                                "line": i,
                                "content": line.strip()[:200],
                            })
                            if len(matches) >= 100:
                                return json.dumps({"matches": matches, "truncated": True})
                except Exception:
                    pass

        return json.dumps({"matches": matches, "count": len(matches)})

    async def file_info(self, file_path: str) -> str:
        p = Path(file_path)
        if not p.exists():
            p = self.config.project_root / file_path
        if not p.exists():
            return json.dumps({"error": f"File not found: {file_path}"})

        stat = p.stat()
        try:
            lines = len(p.read_text(errors="ignore").splitlines())
        except Exception:
            lines = None

        return json.dumps({
            "path": str(p),
            "size_bytes": stat.st_size,
            "lines": lines,
            "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
        })

    async def syntax_check(self, file_path: str) -> str:
        p = Path(file_path)
        if not p.exists():
            p = self.config.resources_dir / file_path
        if not p.exists():
            return json.dumps({"error": f"File not found: {file_path}"})

        result = subprocess.run(["luac", "-p", str(p)], capture_output=True, text=True)
        if result.returncode == 0:
            return json.dumps({"valid": True, "file": str(p)})
        return json.dumps({"valid": False, "error": result.stderr.strip()})

    async def read_logs(self, lines: int = 100, pattern: Optional[str] = None) -> str:
        if not self.config.logs_dir.exists():
            return json.dumps({"error": f"Logs directory not found: {self.config.logs_dir}"})

        log_files = sorted(self.config.logs_dir.glob("*.log"), key=lambda f: f.stat().st_mtime, reverse=True)
        if not log_files:
            return json.dumps({"error": "No log files found."})

        latest = log_files[0]
        all_lines = latest.read_text(errors="ignore").splitlines()[-lines:]
        if pattern:
            all_lines = [l for l in all_lines if re.search(pattern, l, re.IGNORECASE)]

        return json.dumps({"file": latest.name, "lines": all_lines, "count": len(all_lines)})


# ─── MySQL ────────────────────────────────────────────────────────────────────

class MySQLTool:
    def __init__(self, config: Config):
        self.config = config

    async def query(self, query: str) -> str:
        if not self.config.has_mysql():
            return json.dumps({
                "error": "MySQL not configured.",
                "setup": "Set MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE in your .env",
            })

        if shutil.which("mysql") is None:
            return json.dumps({"error": "mysql client not installed. Download from https://dev.mysql.com/downloads/"})

        db = self.config.mysql
        cmd = [
            "mysql",
            "-h", db["host"],
            "-P", str(db["port"]),
            "-u", db["user"],
            f"-p{db['password']}",
            "-N", "-e", query, db["database"],
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return json.dumps({"error": result.stderr.strip()})

        rows = [line.split("\t") for line in result.stdout.strip().splitlines() if line]
        return json.dumps({"success": True, "rows": rows, "count": len(rows), "database": db["database"]})


# ─── txAdmin ──────────────────────────────────────────────────────────────────

class TxAdminTool:
    """Talks to txAdmin v8 via password session auth."""

    def __init__(self, config: Config):
        self.config = config
        self._cookie: str = ""
        self._csrf: str = ""

    # ── auth ──────────────────────────────────────────────────────────────────

    def _authenticate(self) -> bool:
        """POST /auth/password → cache session cookie + CSRF token."""
        import urllib.request, urllib.error
        if not (self.config.txadmin_user and self.config.txadmin_pass):
            return False
        payload = json.dumps({
            "username": self.config.txadmin_user,
            "password": self.config.txadmin_pass,
        }).encode()
        req = urllib.request.Request(
            f"{self.config.txadmin_url}/auth/password",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                cookie_header = r.getheader("Set-Cookie", "")
                self._cookie = cookie_header.split(";")[0].strip() if cookie_header else ""
                body = json.loads(r.read().decode())
                self._csrf = body.get("csrfToken", "")
                return bool(self._cookie and self._csrf)
        except Exception:
            return False

    def _headers(self) -> dict:
        """Return headers with session cookie + CSRF token, re-auth if missing."""
        if not self._cookie or not self._csrf:
            self._authenticate()
        return {
            "Content-Type": "application/json",
            "Cookie": self._cookie,
            "x-txadmin-csrftoken": self._csrf,
        }

    def _post(self, path: str, payload: dict) -> str:
        """POST with auto-retry on session expiry."""
        import urllib.request, urllib.error
        for attempt in range(2):
            data = json.dumps(payload).encode()
            req = urllib.request.Request(
                f"{self.config.txadmin_url}{path}",
                data=data,
                headers=self._headers(),
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    body = r.read().decode()
                    parsed = json.loads(body)
                    # txAdmin returns {logout:true} when session expired
                    if isinstance(parsed, dict) and parsed.get("logout") and attempt == 0:
                        self._cookie = ""
                        self._authenticate()
                        continue
                    return body
            except urllib.error.HTTPError as e:
                if e.code == 403 and attempt == 0:
                    self._cookie = ""
                    self._authenticate()
                    continue
                return json.dumps({"error": str(e)})
            except Exception as e:
                return json.dumps({"error": str(e)})
        return json.dumps({"error": "auth failed after retry"})

    def _get(self, path: str) -> str:
        import urllib.request, urllib.error
        for attempt in range(2):
            req = urllib.request.Request(
                f"{self.config.txadmin_url}{path}",
                headers=self._headers(),
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    body = r.read().decode()
                    parsed = json.loads(body)
                    if isinstance(parsed, dict) and parsed.get("logout") and attempt == 0:
                        self._cookie = ""
                        self._authenticate()
                        continue
                    return body
            except Exception as e:
                if attempt == 0:
                    self._cookie = ""
                    self._authenticate()
                    continue
                return json.dumps({"error": str(e), "hint": f"Is txAdmin running at {self.config.txadmin_url}?"})
        return json.dumps({"error": "request failed"})

    # ── public API ────────────────────────────────────────────────────────────

    async def server_status(self) -> str:
        """GET /auth/self — returns admin info confirming txAdmin is alive + log tail."""
        self_resp = self._get("/auth/self")
        log_resp  = self._get("/serverLog/partial")
        try:
            self_data = json.loads(self_resp)
            log_data  = json.loads(log_resp)
            recent = [e for e in (log_data.get("log") or [])[-10:]]
            return json.dumps({"txadmin": self_data, "recent_log": recent})
        except Exception:
            return self_resp

    async def resource_control(self, action: str, resource_name: str) -> str:
        """POST /fxserver/commands — start/stop/restart/ensure a resource."""
        action_map = {
            "start":   "start_res",
            "stop":    "stop_res",
            "restart": "restart_res",
            "ensure":  "ensure_res",
            "refresh": "refresh_res",
        }
        tx_action = action_map.get(action.lower(), action)
        return self._post("/fxserver/commands", {"action": tx_action, "parameter": resource_name})

    async def server_console(self, command: str) -> str:
        """Route console commands to the correct txAdmin v8 HTTP endpoint.

        Supports: restart/start/stop/ensure <resource>, restart_server, stop_server.
        Raw arbitrary commands are not supported via HTTP in txAdmin v8.
        """
        parts = command.strip().split(None, 1)
        verb  = parts[0].lower() if parts else ""
        arg   = parts[1].strip() if len(parts) > 1 else ""

        # Resource-scoped commands
        if verb in ("restart", "start", "stop", "ensure", "refresh") and arg:
            return await self.resource_control(verb, arg)

        # Whole-server commands
        if verb in ("restart_server", "quit") or (verb == "restart" and not arg):
            return self._post("/fxserver/controls", {"action": "restart"})
        if verb == "stop_server" or (verb == "stop" and not arg):
            return self._post("/fxserver/controls", {"action": "stop"})

        return json.dumps({
            "error": "unsupported_command",
            "hint":  "txAdmin v8 HTTP API supports: restart/start/stop/ensure <resource>, restart_server, stop_server",
            "command": command,
        })

    async def server_control(self, action: str) -> str:
        """POST /fxserver/controls — restart/start/stop the whole server."""
        return self._post("/fxserver/controls", {"action": action})


# ─── Deploy ───────────────────────────────────────────────────────────────────

class DeployTool:
    def __init__(self, config: Config):
        self.config = config

    async def deploy(self, resource_name: str, target: str = "production") -> str:
        source = self.config.resources_dir / resource_name
        if not source.exists():
            return json.dumps({"error": f"Resource not found: {resource_name}"})

        if self.config.has_ssh():
            return await self._deploy_ssh(resource_name, source)

        # Local copy
        remote_res = os.getenv("FIVEM_REMOTE_RESOURCES_DIR", str(self.config.resources_dir))
        target_path = (
            Path(remote_res) / resource_name
            if target in ("production", "txdata")
            else Path(target) / resource_name
        )

        import shutil
        backup_dir = self.config.project_root / "backups" / "deploy"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"{resource_name}_{time.strftime('%Y%m%d_%H%M%S')}"

        if target_path.exists():
            shutil.copytree(target_path, backup_path)
            shutil.rmtree(target_path)

        shutil.copytree(source, target_path)
        return json.dumps({
            "success": True, "resource": resource_name,
            "target": str(target_path), "method": "local",
        })

    async def _deploy_ssh(self, resource_name: str, source: Path) -> str:
        try:
            import paramiko
        except ImportError:
            return json.dumps({"error": "paramiko not installed. Run: pip install fiveclaw-agent[ssh]"})

        remote_res = os.getenv("FIVEM_REMOTE_RESOURCES_DIR", "")
        if not remote_res:
            return json.dumps({"error": "FIVEM_REMOTE_RESOURCES_DIR not set."})

        ssh_cfg = self.config.ssh
        remote_target = f"{remote_res.rstrip('/')}/{resource_name}"

        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            kw: dict = {"hostname": ssh_cfg["host"], "port": ssh_cfg["port"], "username": ssh_cfg["user"]}
            if ssh_cfg.get("key_path"):
                kw["key_filename"] = ssh_cfg["key_path"]
            client.connect(**kw, timeout=15)
            sftp = client.open_sftp()

            def _upload(local: Path, remote: str):
                try: sftp.mkdir(remote)
                except OSError: pass
                for item in local.iterdir():
                    r = f"{remote}/{item.name}"
                    if item.is_dir(): _upload(item, r)
                    else: sftp.put(str(item), r)

            client.exec_command(f"rm -rf '{remote_target}'")[1].channel.recv_exit_status()
            _upload(source, remote_target)
            sftp.close(); client.close()
            return json.dumps({"success": True, "resource": resource_name, "target": remote_target,
                               "host": ssh_cfg["host"], "method": "ssh"})
        except Exception as e:
            return json.dumps({"error": f"SSH deploy failed: {e}"})

    async def backup(self, resource_name: str) -> str:
        source = self.config.resources_dir / resource_name
        if not source.exists():
            return json.dumps({"error": f"Resource not found: {resource_name}"})

        import shutil
        backup_dir = self.config.project_root / "backups" / "resources"
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        dest = backup_dir / f"{resource_name}_{ts}"
        shutil.copytree(source, dest)
        return json.dumps({"success": True, "backup": str(dest), "timestamp": ts})


# ─── Context memory ───────────────────────────────────────────────────────────

class ContextTool:
    """
    Persistent local memory backed by SQLite + FTS5.

    Storage layout (context_dir/knowledge.db):
      facts        — key/value store with category + timestamp
      facts_fts    — FTS5 virtual table over facts (auto-synced via triggers)
      history      — append-only session log
      history_fts  — FTS5 virtual table over history summaries/tags

    Migration: if a legacy knowledge.json exists it is imported on first open
    and renamed to knowledge.json.bak so data is never lost.
    """

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS facts (
        key       TEXT PRIMARY KEY,
        value     TEXT NOT NULL,
        category  TEXT NOT NULL DEFAULT 'general',
        ts        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
    );

    CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
        key, value, category,
        content='facts',
        content_rowid='rowid',
        tokenize='unicode61'
    );

    CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
        INSERT INTO facts_fts(rowid, key, value, category)
        VALUES (new.rowid, new.key, new.value, new.category);
    END;
    CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
        INSERT INTO facts_fts(facts_fts, rowid, key, value, category)
        VALUES ('delete', old.rowid, old.key, old.value, old.category);
    END;
    CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
        INSERT INTO facts_fts(facts_fts, rowid, key, value, category)
        VALUES ('delete', old.rowid, old.key, old.value, old.category);
        INSERT INTO facts_fts(rowid, key, value, category)
        VALUES (new.rowid, new.key, new.value, new.category);
    END;

    CREATE TABLE IF NOT EXISTS history (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        summary  TEXT NOT NULL,
        tags     TEXT NOT NULL DEFAULT '[]',
        ts       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
    );

    CREATE VIRTUAL TABLE IF NOT EXISTS history_fts USING fts5(
        summary, tags,
        content='history',
        content_rowid='id',
        tokenize='unicode61'
    );

    CREATE TRIGGER IF NOT EXISTS history_ai AFTER INSERT ON history BEGIN
        INSERT INTO history_fts(rowid, summary, tags)
        VALUES (new.id, new.summary, new.tags);
    END;
    """

    def __init__(self, config: Config):
        import sqlite3
        self._db_path = config.context_dir / "knowledge.db"
        self._legacy  = config.context_dir / "knowledge.json"
        config.context_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(self._SCHEMA)
        self._conn.commit()
        self._migrate_legacy()

    def _migrate_legacy(self):
        """Import knowledge.json into SQLite if it exists, then rename it."""
        if not self._legacy.exists():
            return
        try:
            data = json.loads(self._legacy.read_text())
            facts   = data.get("facts", {})
            history = data.get("history", [])
            cur = self._conn.cursor()
            for key, v in facts.items():
                cur.execute(
                    "INSERT OR IGNORE INTO facts(key, value, category, ts) VALUES (?,?,?,?)",
                    (key, v.get("value", ""), v.get("category", "general"), v.get("ts", ""))
                )
            for entry in history:
                tags = entry.get("tags", [])
                cur.execute(
                    "INSERT INTO history(summary, tags, ts) VALUES (?,?,?)",
                    (entry.get("summary", ""), json.dumps(tags), entry.get("ts", ""))
                )
            self._conn.commit()
            self._legacy.rename(self._legacy.with_suffix(".json.bak"))
        except Exception as e:
            pass  # don't crash on bad legacy data

    # ── facts ──────────────────────────────────────────────────────────────────

    async def remember(self, key: str, value: str, category: str = "general") -> str:
        self._conn.execute(
            "INSERT INTO facts(key, value, category) VALUES (?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            " category=excluded.category,"
            " ts=strftime('%Y-%m-%d %H:%M:%S','now')",
            (key, value, category)
        )
        self._conn.commit()
        return json.dumps({"saved": key})

    async def recall(self, key: Optional[str] = None, category: Optional[str] = None) -> str:
        if key:
            row = self._conn.execute(
                "SELECT key, value, category, ts FROM facts WHERE key=?", (key,)
            ).fetchone()
            if row:
                return json.dumps(dict(row), indent=2)
            return json.dumps({"error": f"Not found: {key}"})

        if category:
            rows = self._conn.execute(
                "SELECT key, value, category, ts FROM facts WHERE category=? ORDER BY ts DESC",
                (category,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT key, value, category, ts FROM facts ORDER BY ts DESC"
            ).fetchall()

        return json.dumps([dict(r) for r in rows], indent=2)

    async def search(self, query: str) -> str:
        # FTS5 match across key + value + category, ranked by relevance
        rows = self._conn.execute(
            """
            SELECT f.key, f.value, f.category, f.ts
            FROM facts f
            JOIN facts_fts fts ON f.rowid = fts.rowid
            WHERE facts_fts MATCH ?
            ORDER BY rank
            LIMIT 50
            """,
            (query,)
        ).fetchall()

        if not rows:
            # Fallback: LIKE scan for short/symbol queries FTS5 won't tokenise
            like = f"%{query}%"
            rows = self._conn.execute(
                "SELECT key, value, category, ts FROM facts"
                " WHERE key LIKE ? OR value LIKE ? OR category LIKE ?"
                " ORDER BY ts DESC LIMIT 50",
                (like, like, like)
            ).fetchall()

        return json.dumps([dict(r) for r in rows], indent=2)

    async def forget(self, key: str) -> str:
        cur = self._conn.execute("DELETE FROM facts WHERE key=?", (key,))
        self._conn.commit()
        if cur.rowcount:
            return json.dumps({"deleted": key})
        return json.dumps({"error": f"Key not found: {key}"})

    # ── history ────────────────────────────────────────────────────────────────

    async def record(self, summary: str, tags: str) -> str:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        self._conn.execute(
            "INSERT INTO history(summary, tags) VALUES (?,?)",
            (summary, json.dumps(tag_list))
        )
        self._conn.commit()
        return json.dumps({"recorded": True})

    async def history(self, limit: int = 10, tag: Optional[str] = None) -> str:
        if tag:
            rows = self._conn.execute(
                "SELECT h.id, h.summary, h.tags, h.ts"
                " FROM history h"
                " JOIN history_fts hf ON h.id = hf.rowid"
                " WHERE history_fts MATCH ?"
                " ORDER BY h.id DESC LIMIT ?",
                (tag, limit)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, summary, tags, ts FROM history ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()

        result = []
        for r in reversed(rows):  # chronological order
            d = dict(r)
            d["tags"] = json.loads(d["tags"])
            result.append(d)
        return json.dumps(result, indent=2)
