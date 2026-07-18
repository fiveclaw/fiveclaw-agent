"""
Local tool implementations — file I/O, SSH, MySQL, txAdmin.
These run entirely on the user's machine. No source logic is sent to FiveClaw.
"""

import asyncio
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

    _SKIP_DIRS = {"node_modules", ".git", "dist", "build", ".next", "__pycache__", "venv", ".venv"}
    for rdir in dirs:
        if not rdir.exists():
            continue
        for ext in ("*.lua", "*.js", "*.ts", "*.html", "*.css"):
            for f in rdir.rglob(ext):
                if any(part in _SKIP_DIRS for part in f.parts):
                    continue
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

        def _scan(rdir: Path):
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

        for d in self.config.resources_dir.iterdir():
            if not d.is_dir():
                continue
            if (d / "fxmanifest.lua").exists():
                _scan(d)
            else:
                # Category folder (e.g. [local], [system]) — index each child resource
                for sub in d.iterdir():
                    if sub.is_dir() and (sub / "fxmanifest.lua").exists():
                        _scan(sub)

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

    def _resolve_file(self, file_path: str) -> Optional[Path]:
        """Resolve a file path tolerant of the caller's base convention:
        absolute / cwd-relative first, then project_root-relative and
        resources_dir-relative, and finally the FiveM [category] layout
        (e.g. 'qbx_core/client/main.lua' living under resources/[qbx]/).
        Returns None if it can't be found. Both file_info and syntax_check
        share this so they no longer disagree on the path base."""
        p = Path(file_path)
        if p.exists():
            return p
        for base in (self.config.resources_dir, self.config.project_root):
            if base:
                cand = base / file_path
                if cand.exists():
                    return cand
        rdir = self.config.resources_dir
        if rdir and rdir.exists():
            for cat in rdir.iterdir():
                if cat.is_dir() and cat.name.startswith("["):
                    cand = cat / file_path
                    if cand.exists():
                        return cand
        return None

    async def file_info(self, file_path: str) -> str:
        p = self._resolve_file(file_path)
        if p is None:
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
        p = self._resolve_file(file_path)
        if p is None:
            return json.dumps({"error": f"File not found: {file_path}"})

        code = p.read_text(errors="ignore")

        # 1. luac binary — explicit LUAC_PATH or found in PATH
        luac_bin = self.config.luac_path or shutil.which("luac")
        if luac_bin:
            result = subprocess.run([luac_bin, "-p", str(p)], capture_output=True, text=True)
            if result.returncode == 0:
                return json.dumps({"valid": True, "file": str(p), "checker": "luac"})
            return json.dumps({"valid": False, "error": result.stderr.strip(), "checker": "luac"})

        # 2. lupa (embedded LuaJIT) — native cross-platform compiler check, no binary needed
        try:
            import lupa  # type: ignore
            _lua = lupa.LuaRuntime(unpack_returned_tuples=True)
            _checker = _lua.eval(
                'function(code) '
                '  local f, err = load(code, "@' + p.name + '", "t") '
                '  if f then return true, nil else return false, err end '
                'end'
            )
            ok, err = _checker(code)
            if ok:
                return json.dumps({"valid": True, "file": str(p), "checker": "lupa"})
            return json.dumps({"valid": False, "error": err or "Syntax error", "checker": "lupa"})
        except ImportError:
            pass

        # 3. luaparser — pure Python fallback
        try:
            from luaparser import ast as _lua_ast
            try:
                _lua_ast.parse(code)
                return json.dumps({"valid": True, "file": str(p), "checker": "luaparser"})
            except Exception as e:
                line = getattr(e, "line", None) or getattr(e, "lineno", None)
                err = {"valid": False, "error": str(e), "checker": "luaparser"}
                if line:
                    err["line"] = line
                return json.dumps(err)
        except ImportError:
            pass

        return json.dumps({
            "error": "No Lua checker available. Run: pip install lupa",
        })

    async def read_logs(self, lines: int = 100, pattern: Optional[str] = None) -> str:
        if not self.config.logs_dir.exists():
            return json.dumps({
                "error": f"Logs directory not found: {self.config.logs_dir}",
                "hint": "Set FIVEM_PROJECT_ROOT to your FiveM server directory.",
            })

        candidates = sorted(self.config.logs_dir.glob("*.log"), key=lambda f: f.stat().st_mtime, reverse=True)
        if not candidates:
            return json.dumps({"error": f"No log files found in {self.config.logs_dir}"})

        # Prefer fxserver.log (FiveM's main server log) — it's far more detailed than server.log
        preferred = self.config.logs_dir / "fxserver.log"
        latest = preferred if preferred.exists() else candidates[0]
        ansi = re.compile(r'\x1b(\[[0-9;]*[mGKJHF]|\][^\x07]*\x07)')
        all_lines = [ansi.sub("", l) for l in latest.read_text(errors="ignore").splitlines()[-lines:]]
        if pattern:
            all_lines = [l for l in all_lines if re.search(pattern, l, re.IGNORECASE)]

        return json.dumps({"file": str(latest), "lines": all_lines, "count": len(all_lines)})


# ─── MySQL ────────────────────────────────────────────────────────────────────

class MySQLTool:
    def __init__(self, config: Config):
        self.config = config

    def _run_sql(self, db: dict, sql: str) -> tuple[Optional[list], Optional[str]]:
        """Run a single SQL statement via PyMySQL — a pure-Python driver, so no
        `mysql` client binary is needed on any OS (removes the old Windows PATH /
        Program Files hunting). Returns (rows, error): rows is a list of row-lists
        with every cell stringified and NULL rendered as "NULL", matching the tab
        output the old `mysql -N -e` CLI produced, so downstream parsing is unchanged.
        autocommit mirrors the CLI's batch-mode behaviour (writes persist)."""
        try:
            import pymysql
        except ImportError:
            return None, "pymysql is not installed — add it to the agent dependencies (pip install pymysql)."
        conn = None
        try:
            conn = pymysql.connect(
                host=db.get("host", "127.0.0.1"),
                port=int(db.get("port", 3306)),
                user=db["user"],
                password=db.get("password", ""),
                database=db["database"],
                connect_timeout=10,
                read_timeout=30,
                charset="utf8mb4",
                autocommit=True,
            )
            with conn.cursor() as cur:
                cur.execute(sql)
                fetched = cur.fetchall() or ()
            rows = [["NULL" if v is None else str(v) for v in row] for row in fetched]
            return rows, None
        except Exception as e:
            return None, str(e)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    async def query(self, query: str, db_name: str = "default") -> str:
        if not self.config.has_mysql(db_name):
            available = ["default"] + list(self.config.extra_databases.keys())
            return json.dumps({
                "error": f"MySQL database '{db_name}' not configured.",
                "available": available,
                "setup": "Set MYSQL_USER/MYSQL_PASSWORD/MYSQL_DATABASE for default, or MYSQL_EXTRA_DBS for named databases.",
            })

        db = self.config.get_db(db_name)
        rows, err = self._run_sql(db, query)
        if err is not None:
            return json.dumps({"error": err})

        return json.dumps({
            "success": True,
            "rows": rows,
            "count": len(rows),
            "db_name": db_name,
            "database": db["database"],
        })

    async def list_databases(self) -> str:
        """List every configured MySQL connection (default + extra databases) with
        its alias, real database, host:port, and table list (via SHOW TABLES)."""
        # Build the alias → connection map: 'default' plus every extra database.
        connections = [("default", self.config.mysql)]
        for alias, db in self.config.extra_databases.items():
            connections.append((alias, db))

        databases = []
        for alias, db in connections:
            entry = {
                "alias":    alias,
                "database": db.get("database", ""),
                "host":     f"{db.get('host', '127.0.0.1')}:{db.get('port', 3306)}",
            }
            if not (db.get("user") and db.get("database")):
                entry["error"] = "not configured (missing user or database)"
                databases.append(entry)
                continue
            try:
                rows, err = self._run_sql(db, "SHOW TABLES")
                if err is not None:
                    entry["error"] = err
                else:
                    entry["tables"] = [row[0] for row in rows if row and row[0]]
                    entry["table_count"] = len(entry["tables"])
            except Exception as e:
                entry["error"] = str(e)
            databases.append(entry)

        return json.dumps({
            "success": True,
            "count": len(databases),
            "databases": databases,
        }, indent=2)

    async def visualize_schema(self, db_name: str = "default") -> str:
        """Render an ASCII schema diagram (tables, columns, foreign keys) for a
        configured database. Runs locally against your MySQL — the schema and data
        never leave the machine. db_name accepts an alias or a real database name."""
        if not self.config.has_mysql(db_name):
            available = ["default"] + list(self.config.extra_databases.keys())
            return json.dumps({
                "error": f"MySQL database '{db_name}' not configured.",
                "available": available,
            })

        db = self.config.get_db(db_name)

        table_rows, err = self._run_sql(db, "SHOW TABLES")
        if err is not None:
            return json.dumps({"error": err})
        table_names = [row[0] for row in table_rows if row and row[0]]

        tables = []
        for name in table_names:
            col_rows, cols_err = self._run_sql(db, f"DESCRIBE `{name}`")
            columns = []
            if cols_err is None:
                for c in col_rows:
                    if len(c) >= 4:
                        columns.append({"name": c[0], "type": c[1], "null": c[2], "key": c[3]})
            fk_rows, fks_err = self._run_sql(
                db,
                "SELECT COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
                "FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE "
                f"WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = '{name}' "
                "AND REFERENCED_TABLE_NAME IS NOT NULL",
            )
            foreign_keys = []
            if fks_err is None:
                for f in fk_rows:
                    if len(f) >= 3:
                        foreign_keys.append({"column": f[0], "references_table": f[1], "references_column": f[2]})
            tables.append({"name": name, "columns": columns, "foreign_keys": foreign_keys})

        lines = [f"Database: {db['database']}", "=" * 60, ""]
        for t in tables:
            lines.append(f"┌─ {t['name']}")
            for col in t["columns"]:
                key_marker = "🔑 " if col["key"] == "PRI" else "   "
                null_marker = "NULL" if col["null"] == "YES" else "NOT NULL"
                lines.append(f"│  {key_marker}{col['name']}: {col['type']} {null_marker}")
            if t["foreign_keys"]:
                lines.append("│")
                lines.append("│  Foreign Keys:")
                for fk in t["foreign_keys"]:
                    lines.append(f"│    {fk['column']} → {fk['references_table']}.{fk['references_column']}")
            lines.append("└─" + "─" * 40)
            lines.append("")

        return json.dumps({
            "success": True,
            "db_name": db_name,
            "database": db["database"],
            "table_count": len(tables),
            "ascii_diagram": "\n".join(lines),
            "tables": tables,
        }, indent=2)


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
        if verb == "refresh" and not arg:
            return self._post("/fxserver/commands", {"action": "refresh_res", "parameter": ""})

        # Arbitrary command → txAdmin's live console over socket.io. This is the ONLY
        # channel txAdmin v8 accepts raw commands on, and it's the product-correct path:
        # it reuses the admin session the customer already configured, needs no server-side
        # config, and adds no new attack surface. There is NO RCON fallback — for non-txAdmin
        # setups the custom panel path (CustomPanelTool) has its own command endpoint.
        return self._txadmin_socket_command(command)

    def _txadmin_socket_command(self, command: str, _retry: bool = True) -> str:
        """Run an arbitrary console command via txAdmin's live-console socket.io room —
        the only channel txAdmin v8 accepts raw commands on. Reuses the cached admin session
        cookie (re-auths only when it's missing or has expired, so we don't hammer txAdmin's
        rate-limited /auth/password on every call). Joins the `liveconsole` room, emits
        `consoleCommand`, and returns the output streamed back over `consoleData`. Always
        returns a JSON string (success or a helpful error)."""
        try:
            import socketio
        except ImportError:
            return json.dumps({
                "error": "socketio_missing",
                "hint": "python-socketio[client] is required for txAdmin console commands — reinstall fiveclaw-agent.",
                "command": command,
            })
        if not self._cookie:
            self._authenticate()
        if not self._cookie:
            return json.dumps({
                "error": "txadmin_not_configured",
                "hint": ("Set TXADMIN_URL / TXADMIN_USER / TXADMIN_PASS to run console commands over "
                         "txAdmin, or use a custom panel (ADMIN_PANEL_TYPE=custom with a command endpoint)."),
                "command": command,
            })
        import re
        sio = socketio.Client(reconnection=False)
        out = []

        @sio.on("consoleData")
        def _on(data):
            out.append(data if isinstance(data, str) else str(data))

        try:
            # let engine.io negotiate transports (websocket-only forced = connect error);
            # request the liveconsole room via query; auth via the cached session cookie.
            sio.connect(self.config.txadmin_url + "?rooms=liveconsole",
                        headers={"Cookie": self._cookie}, wait_timeout=10)
        except Exception as e:
            try:
                sio.disconnect()
            except Exception:
                pass
            # The cached session may have expired — re-auth once and retry with a fresh cookie.
            if _retry:
                self._cookie = ""
                self._authenticate()
                if self._cookie:
                    return self._txadmin_socket_command(command, _retry=False)
            return json.dumps({
                "error": "txadmin_console_unreachable",
                "hint": (f"Couldn't open the txAdmin live console at {self.config.txadmin_url} ({e}). "
                         "Is txAdmin running and the admin account valid?"),
                "command": command,
            })
        try:
            sio.emit("consoleCommand", command)
            sio.sleep(1.5)  # collect the console output this command produces
        except Exception as e:
            return json.dumps({"error": f"txadmin_console_emit_failed: {e}", "command": command})
        finally:
            try:
                sio.disconnect()
            except Exception:
                pass
        clean = re.sub(r"\x1b\[[0-9;]*m", "", "".join(out))  # strip ANSI colour
        tail = "\n".join(l.rstrip() for l in clean.splitlines() if l.strip())[-2000:]
        # An expired/invalid session still CONNECTS at the transport layer, but the
        # liveconsole room silently rejects it → zero consoleData. A valid session always
        # echoes at least the command back, so treat empty output as a dead session and
        # re-auth + retry once. (Connect-time exceptions are handled above; this catches
        # the silent-reject case they miss.)
        if not tail and _retry:
            self._cookie = ""
            self._authenticate()
            if self._cookie:
                return self._txadmin_socket_command(command, _retry=False)
        return json.dumps({"ok": bool(tail), "command": command, "channel": "txadmin-socketio", "output": tail})

    async def server_control(self, action: str) -> str:
        """POST /fxserver/controls — restart/start/stop the whole server."""
        return self._post("/fxserver/controls", {"action": action})


# ─── Custom Panel ─────────────────────────────────────────────────────────────

class CustomPanelTool:
    """Controls a custom REST admin panel (e.g. trucking admin-panel).

    No auth required.  Configure via MCP env:
      ADMIN_PANEL_TYPE=custom
      ADMIN_PANEL_URL=http://your-panel-host:port

    REST API expected:
      GET  /api/server/status
      POST /api/server-control/command  { "command": "<cmd>" }
    """

    def __init__(self, config: Config):
        self._url     = config.custom_panel_url.rstrip("/") if config.custom_panel_url else ""
        self._ep_status  = config.custom_panel_status_endpoint
        self._ep_start   = config.custom_panel_start_endpoint
        self._ep_stop    = config.custom_panel_stop_endpoint
        self._ep_command = config.custom_panel_command_endpoint

    def _not_configured(self, missing: str) -> str:
        return json.dumps({
            "error": "custom_panel_not_configured",
            "hint":  f"Set {missing} in your MCP env config.",
        })

    def _get(self, path: str, env_var: str = "ADMIN_PANEL_STATUS_ENDPOINT") -> str:
        import urllib.request
        if not self._url:
            return self._not_configured("ADMIN_PANEL_URL")
        if not path:
            return self._not_configured(env_var)
        try:
            with urllib.request.urlopen(f"{self._url}{path}", timeout=10) as r:
                return r.read().decode()
        except Exception as e:
            return json.dumps({"error": str(e), "hint": f"Is the panel running at {self._url}?"})

    def _post_cmd(self, command: str) -> str:
        import urllib.request
        if not self._url:
            return self._not_configured("ADMIN_PANEL_URL")
        if not self._ep_command:
            return self._not_configured("ADMIN_PANEL_COMMAND_ENDPOINT")
        data = json.dumps({"command": command}).encode()
        req = urllib.request.Request(
            f"{self._url}{self._ep_command}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.read().decode()
        except Exception as e:
            return json.dumps({"error": str(e), "hint": f"Is the panel running at {self._url}?"})

    async def server_status(self) -> str:
        return self._get(self._ep_status, "ADMIN_PANEL_STATUS_ENDPOINT")

    async def resource_control(self, action: str, resource_name: str) -> str:
        action_map = {
            "start":   "ensure",
            "ensure":  "ensure",
            "stop":    "stop",
            "restart": "restart",
        }
        cmd = f"{action_map.get(action.lower(), action)} {resource_name}"
        return self._post_cmd(cmd)

    async def server_console(self, command: str) -> str:
        return self._post_cmd(command)

    async def server_control(self, action: str) -> str:
        """start/stop via dedicated endpoints; restart = stop+start; refresh = console command."""
        action = action.lower().strip()
        if action == "restart":
            self._post_endpoint(self._ep_stop, {})
            import asyncio; await asyncio.sleep(3)
            return self._post_endpoint(self._ep_start, {})
        if action == "start":
            return self._post_endpoint(self._ep_start, {})
        if action == "stop":
            return self._post_endpoint(self._ep_stop, {})
        if action == "refresh":
            return self._post_cmd("refresh")
        return self._post_cmd(action)

    def _post_endpoint(self, path: str, payload: dict) -> str:
        import urllib.request
        if not self._url:
            return self._not_configured("ADMIN_PANEL_URL")
        if not path:
            return self._not_configured("ADMIN_PANEL_START_ENDPOINT or ADMIN_PANEL_STOP_ENDPOINT")
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self._url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.read().decode()
        except Exception as e:
            return json.dumps({"error": str(e), "hint": f"Is the panel running at {self._url}?"})


# ─── Client Logs (fc-clientlog) ───────────────────────────────────────────────

_LEVEL_SEVERITY = {"info": 1, "warn": 2, "error": 3}


class ClientLogTool:
    """Fetches client-side logs captured by the companion `fc-clientlog` resource.

    Triggers a fresh dump over the same console channel `tool_server_console` uses
    (`fc_clientlog_get <player_id>`), then reads the JSON file fc-clientlog writes to
    disk — a direct local file read, no network round trip.
    """

    _POLL_TIMEOUT = 3.0
    _POLL_INTERVAL = 0.1
    _HARD_CAP = 200

    def __init__(self, config: Config, console):
        self.config = config
        self.console = console  # TxAdminTool or CustomPanelTool — both expose server_console()

    def _find_resource_dir(self) -> Optional[Path]:
        """Locate the fc-clientlog resource dir — direct path first, then one level
        of category subdirs (e.g. [local]/fc-clientlog). Mirrors the resource lookup
        used by collect_resource_files/DeployTool.deploy."""
        resources_dir = self.config.resources_dir
        if not resources_dir.exists():
            return None
        direct = resources_dir / "fc-clientlog"
        if direct.exists():
            return direct
        for cat in resources_dir.iterdir():
            if cat.is_dir():
                candidate = cat / "fc-clientlog"
                if candidate.exists():
                    return candidate
        return None

    @staticmethod
    def _severity(level: str) -> int:
        return _LEVEL_SEVERITY.get((level or "").lower(), 1)

    @staticmethod
    def _relative_time(ts, now: float) -> str:
        if not ts:
            return "unknown"
        delta = max(0, int(now - ts))
        if delta < 60:
            return f"{delta}s ago"
        if delta < 3600:
            return f"{delta // 60}m ago"
        if delta < 86400:
            return f"{delta // 3600}h ago"
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))

    async def get_client_logs(self, player_id: int, level: str = "warn",
                               limit: int = 50, since: Optional[int] = None) -> str:
        level = (level or "warn").lower()
        if level not in _LEVEL_SEVERITY:
            return json.dumps({"error": f"invalid level '{level}' — must be one of: error, warn, info"})

        limit = max(1, min(int(limit), self._HARD_CAP))
        min_severity = _LEVEL_SEVERITY[level]

        resource_dir = self._find_resource_dir()
        if resource_dir is None:
            return json.dumps({
                "error": "fc-clientlog resource not found in resources dir",
                "hint": "Install and `ensure` the fc-clientlog resource so client logs can be captured.",
            })

        dump_path = resource_dir / "dumps" / f"{player_id}.json"

        # Trigger a fresh dump over the same console channel tool_server_console uses.
        send_time = time.time()
        await self.console.server_console(f"fc_clientlog_get {player_id}")

        # Poll for the dump file to be (re)written after we sent the command.
        deadline = send_time + self._POLL_TIMEOUT
        fresh = False
        while time.time() < deadline:
            if dump_path.exists() and dump_path.stat().st_mtime >= send_time - 0.05:
                fresh = True
                break
            await asyncio.sleep(self._POLL_INTERVAL)

        if not fresh:
            return json.dumps({
                "error": f"no response from client {player_id} (offline, fc-clientlog not running, or capture disabled)",
            })

        try:
            data = json.loads(dump_path.read_text(errors="ignore"))
        except Exception as e:
            return json.dumps({"error": f"malformed dump file for player {player_id}: {e}"})

        if isinstance(data, dict):
            if data.get("error"):
                return json.dumps({"error": f"fc-clientlog: {data['error']}", "player_id": player_id})
            entries = data.get("entries", []) or []
            player_name = data.get("player_name") or data.get("name") or f"player {player_id}"
            total_captured = data.get("total_captured", len(entries))
        elif isinstance(data, list):
            entries = data
            player_name = f"player {player_id}"
            total_captured = len(entries)
        else:
            return json.dumps({"error": f"unexpected dump format for player {player_id}"})

        filtered = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            if self._severity(e.get("level", "info")) < min_severity:
                continue
            ts = e.get("ts", 0) or 0
            if since is not None and ts < since:
                continue
            filtered.append(e)

        filtered.sort(key=lambda e: e.get("ts", 0) or 0, reverse=True)
        dropped = len(entries) - len(filtered)
        shown = filtered[:limit]
        truncated = len(filtered) > limit

        now = time.time()
        lines = []
        for e in shown:
            lvl = (e.get("level") or "info").upper()
            tag = e.get("tag", "?")
            msg = e.get("message", "")
            count = e.get("count", 1) or 1
            count_str = f" (×{count})" if count > 1 else ""
            when = self._relative_time(e.get("ts"), now)
            lines.append(f"[{lvl}] {tag}: {msg}{count_str} — {when}")

        header = (
            f"fc-clientlog: {player_name} (id {player_id}) — "
            f"{total_captured} captured, {len(shown)} shown, {dropped} dropped (level={level}+)"
        )
        if truncated:
            header += f" [truncated to {limit} of {len(filtered)} matching]"

        return json.dumps({
            "success": True,
            "player_id": player_id,
            "player_name": player_name,
            "total_captured": total_captured,
            "shown": len(shown),
            "dropped": dropped,
            "truncated": truncated,
            "log": "\n".join([header] + lines),
        })


# ─── SSH ──────────────────────────────────────────────────────────────────────

class SSHTool:
    """General-purpose SSH access — run commands, browse dirs, read/write files."""

    def __init__(self, config: Config):
        self.config = config

    def _not_configured(self) -> str:
        return json.dumps({
            "error": "SSH not configured",
            "hint": "Set FIVEM_SSH_HOST and FIVEM_SSH_USER in your MCP env config (optionally FIVEM_SSH_KEY, FIVEM_SSH_PORT).",
        })

    def _connect(self):
        import paramiko
        cfg = self.config.ssh
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kw: dict = {"hostname": cfg["host"], "port": cfg["port"], "username": cfg["user"]}
        if cfg.get("key_path"):
            kw["key_filename"] = cfg["key_path"]
        if cfg.get("passphrase"):
            kw["passphrase"] = cfg["passphrase"]
        client.connect(**kw, timeout=15)
        return client

    async def run_command(self, command: str, timeout: int = 30) -> str:
        """Execute a shell command on the remote server."""
        if not self.config.has_ssh():
            return self._not_configured()
        try:
            client = self._connect()
            _, stdout, stderr = client.exec_command(command, timeout=timeout)
            out  = stdout.read().decode(errors="replace")
            err  = stderr.read().decode(errors="replace")
            code = stdout.channel.recv_exit_status()
            client.close()
            return json.dumps({"stdout": out, "stderr": err, "exit_code": code, "command": command})
        except Exception as e:
            return json.dumps({"error": str(e), "command": command})

    async def list_dir(self, path: str = ".") -> str:
        """List files and directories at a remote path."""
        if not self.config.has_ssh():
            return self._not_configured()
        try:
            import stat as _stat
            client = self._connect()
            sftp = client.open_sftp()
            entries = sftp.listdir_attr(path)
            items = []
            for e in sorted(entries, key=lambda x: x.filename):
                is_dir = _stat.S_ISDIR(e.st_mode or 0)
                items.append({
                    "name":     e.filename,
                    "type":     "dir" if is_dir else "file",
                    "size":     e.st_size,
                    "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.st_mtime or 0)),
                })
            sftp.close(); client.close()
            return json.dumps({"path": path, "entries": items, "count": len(items)})
        except Exception as e:
            return json.dumps({"error": str(e), "path": path})

    async def read_file(self, path: str) -> str:
        """Read a file from the remote server (capped at 100 KB)."""
        if not self.config.has_ssh():
            return self._not_configured()
        MAX = 100_000
        try:
            client = self._connect()
            sftp = client.open_sftp()
            size = sftp.stat(path).st_size or 0
            with sftp.open(path, "r") as f:
                content = f.read(MAX).decode(errors="replace")
            sftp.close(); client.close()
            return json.dumps({"path": path, "content": content,
                               "truncated": size > MAX, "size": size})
        except Exception as e:
            return json.dumps({"error": str(e), "path": path})

    async def write_file(self, path: str, content: str) -> str:
        """Write content to a file on the remote server."""
        if not self.config.has_ssh():
            return self._not_configured()
        try:
            client = self._connect()
            sftp = client.open_sftp()
            with sftp.open(path, "w") as f:
                f.write(content)
            sftp.close(); client.close()
            return json.dumps({"success": True, "path": path, "bytes": len(content.encode())})
        except Exception as e:
            return json.dumps({"error": str(e), "path": path})

    async def stat(self, path: str) -> str:
        """Get metadata (size, type, modified time) for a remote file or directory."""
        if not self.config.has_ssh():
            return self._not_configured()
        try:
            import stat as _stat
            client = self._connect()
            sftp = client.open_sftp()
            st = sftp.stat(path)
            sftp.close(); client.close()
            return json.dumps({
                "path":     path,
                "type":     "dir" if _stat.S_ISDIR(st.st_mode or 0) else "file",
                "size":     st.st_size,
                "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime or 0)),
            })
        except Exception as e:
            return json.dumps({"error": str(e), "path": path})


# ─── Deploy ───────────────────────────────────────────────────────────────────

class DeployTool:
    def __init__(self, config: Config):
        self.config = config

    async def deploy(self, resource_name: str, target: str = "production") -> str:
        source = self.config.resources_dir / resource_name
        if not source.exists():
            # Search category subdirs (e.g. [local]/character-select)
            for cat in self.config.resources_dir.iterdir():
                if cat.is_dir():
                    candidate = cat / resource_name
                    if candidate.exists():
                        source = candidate
                        break
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
        import paramiko

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
            if ssh_cfg.get("passphrase"):
                kw["passphrase"] = ssh_cfg["passphrase"]
            client.connect(**kw, timeout=15)
            sftp = client.open_sftp()

            def _sftp_rmtree(sftp: paramiko.SFTPClient, remote: str):
                """Recursively delete a remote directory via SFTP (works on Linux + Windows SSH)."""
                try:
                    entries = sftp.listdir_attr(remote)
                except FileNotFoundError:
                    return  # already gone
                import stat as _stat
                for entry in entries:
                    rpath = f"{remote}/{entry.filename}"
                    if _stat.S_ISDIR(entry.st_mode):
                        _sftp_rmtree(sftp, rpath)
                        sftp.rmdir(rpath)
                    else:
                        sftp.remove(rpath)
                sftp.rmdir(remote)

            def _upload(local: Path, remote: str):
                try: sftp.mkdir(remote)
                except OSError: pass
                for item in local.iterdir():
                    r = f"{remote}/{item.name}"
                    if item.is_dir(): _upload(item, r)
                    else: sftp.put(str(item), r)

            _sftp_rmtree(sftp, remote_target)
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
