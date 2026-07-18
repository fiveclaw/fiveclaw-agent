"""
FiveClaw Agent — local MCP server for FiveM development.

Runs on the user's machine and connects to their AI client over MCP. All tools —
file I/O, SSH, MySQL, txAdmin/custom panel, and the analysis suite (validation,
security, event/dependency tracing) — run locally. Only native/framework doc
lookups reach FiveClaw, and those require a (free) API key.
"""

import json as _json
import os
from typing import Optional
from fastmcp import FastMCP

try:
    from importlib.metadata import version as _pkg_version
    _AGENT_VERSION = _pkg_version("fiveclaw-agent")
except Exception:
    _AGENT_VERSION = "unknown"

from .config import Config
from .local import RepoMapTool, FileTool, MySQLTool, TxAdminTool, CustomPanelTool, DeployTool, ContextTool, SSHTool, ClientLogTool, collect_resource_files
from .remote import RemoteClient

# ─── Local analysis engines ───────────────────────────────────────────────────
from .engines.config import Config as _EngineConfig
from .engines.tools import (
    PatternTool as _PatternToolCls,
    ValidationTool as _ValidationToolCls,
    ContractTool as _ContractToolCls,
    FlowTool as _FlowToolCls,
    SecurityTool as _SecurityToolCls,
    DependencyTool as _DependencyToolCls,
)

# ─── Boot ─────────────────────────────────────────────────────────────────────

try:
    config = Config()
except RuntimeError as e:
    import sys
    print(f"[FiveClaw Agent] Configuration error:\n{e}", file=sys.stderr)
    sys.exit(1)

remote  = RemoteClient(config.api_key, config.api_url)
repomap = RepoMapTool(config)
files   = FileTool(config)
mysql   = MySQLTool(config)
txadmin = CustomPanelTool(config) if config.admin_panel_type == "custom" else TxAdminTool(config)
deploy  = DeployTool(config)
context = ContextTool(config)
ssh     = SSHTool(config)
clientlog = ClientLogTool(config, txadmin)

# Build the analysis-engine config without any network lookup.
_saved_fiveclaw_api_key = os.environ.pop("FIVECLAW_API_KEY", None)
try:
    _engine_config = _EngineConfig()
finally:
    if _saved_fiveclaw_api_key is not None:
        os.environ["FIVECLAW_API_KEY"] = _saved_fiveclaw_api_key

_validation = _ValidationToolCls(_engine_config)
_security   = _SecurityToolCls(_engine_config)
_contract   = _ContractToolCls(_engine_config)
_flow       = _FlowToolCls(_engine_config)
_dependency = _DependencyToolCls(_engine_config)
_pattern    = _PatternToolCls(_engine_config)

_OS_HINTS = {
    "windows": (
        "The user is on Windows. "
        "Use Windows-style paths (e.g. C:\\\\fivem-server\\\\resources\\\\my-script). "
        "Shell commands run in cmd/PowerShell — use `dir`, not `ls`. "
        "MySQL queries and Lua syntax checking need no extra setup — the agent "
        "ships a built-in MySQL client (PyMySQL) and Lua checker (lupa), so no "
        "MariaDB/luac install or MYSQL_BIN_DIR/LUAC_PATH is required."
    ),
    "linux": (
        "The user is on Linux. "
        "Use Unix paths (e.g. /home/user/fivem/resources/my-script). "
        "Shell commands run in bash."
    ),
    "macos": (
        "The user is on macOS. "
        "Use Unix paths. Shell commands run in zsh/bash. "
        "MySQL may be at /usr/local/bin/mysql (Homebrew)."
    ),
}

_os_hint = _OS_HINTS.get(config.os.lower(), f"User OS: {config.os}") if config.os else (
    "OS not set. Ask the user to add OS=windows or OS=linux to their MCP env config."
)

_INSTRUCTIONS = f"""You are the FiveClaw Agent — a local MCP server for FiveM server development.

## Environment
- Project root: {config.project_root}
- Resources dir: {config.resources_dir}
- Logs dir: {config.logs_dir}
- SSH: {"configured" if config.has_ssh() else "not configured"}
- MySQL: {"configured" if config.has_mysql() else "not configured"}
- Admin panel: {config.admin_panel_type} at {config.txadmin_url}

## OS
{_os_hint}

## Tool guidance
- Everything runs locally on the user's machine: repomap, file/SSH/MySQL/logs, txAdmin,
  and all analysis (security scan, anti-patterns, validation, event/dependency tracing).
- Only the FiveM docs/native lookups reach FiveClaw, and they need a free API key.
- Use `repomap_generate` before any analysis tool if the resource map is stale.
"""

server = FastMCP("fiveclaw-agent", instructions=_INSTRUCTIONS)

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _resource_files(resource: Optional[str] = None) -> dict:
    """Collect local source files for analysis.

    resource can be:
      - a bare resource name ('character-select') — found in flat or category layout
      - a category-relative path ('[local]/character-select')
      - an absolute directory path
    """
    if resource:
        from pathlib import Path as _P
        p = _P(resource)
        if p.is_absolute() and p.is_dir():
            return collect_resource_files(p.parent, p.name)
        # Relative path with separators (e.g. "[local]/character-select")
        if p.parts and len(p.parts) > 1:
            candidate = config.resources_dir / p
            if candidate.is_dir():
                return collect_resource_files(candidate.parent, candidate.name)
    return collect_resource_files(config.resources_dir, resource)

def _manifest_files() -> dict:
    """Collect only fxmanifest.lua files + server.cfg — for tools that only need structure."""
    files: dict = {}
    resources_dir = config.resources_dir
    if resources_dir.exists():
        for f in resources_dir.rglob("fxmanifest.lua"):
            try:
                rel = str(f.relative_to(resources_dir.parent))
                files[rel] = f.read_text(errors="ignore")
            except Exception:
                pass
    # Include server.cfg if present
    for candidate in [config.project_root / "server.cfg", config.project_root.parent / "server.cfg"]:
        if candidate.exists():
            try:
                files["server.cfg"] = candidate.read_text(errors="ignore")
            except Exception:
                pass
            break
    return files

def _list_resources() -> list[str]:
    """Return resource names from the resources directory (handles category subdirs)."""
    resources_dir = config.resources_dir
    names: list[str] = []
    if not resources_dir.exists():
        return names
    for d in resources_dir.iterdir():
        if not d.is_dir():
            continue
        if (d / "fxmanifest.lua").exists():
            names.append(d.name)
        else:
            for sub in d.iterdir():
                if sub.is_dir() and (sub / "fxmanifest.lua").exists():
                    names.append(sub.name)
    return names

# =============================================================================
# LOCAL TOOLS — run on the user's machine
# =============================================================================

@server.tool()
async def tool_platform_info() -> str:
    """Return the configured OS and environment paths for this machine."""
    return _json.dumps({
        "os":               config.os or "not set — add OS=windows or OS=linux to MCP env",
        "project_root":     str(config.project_root),
        "resources_dir":    str(config.resources_dir),
        "logs_dir":         str(config.logs_dir),
        "ssh_configured":   config.has_ssh(),
        "mysql_configured": config.has_mysql(),
        "mysql_client":     "pymysql (built-in — no external client needed)",
        "luac_path":        config.luac_path or "not set (built-in lua checker used)",
        "txadmin_url":      config.txadmin_url,
        "admin_panel_type": config.admin_panel_type,
    })

@server.tool()
async def mcp_health() -> str:
    """Check FiveClaw Agent health and verify your connection is working.
    Returns environment status, tool count, and API key validity."""
    import time
    tool_names = [t.name for t in await server.list_tools()]
    categories = {
        "local":           [t for t in tool_names if t in (
            "tool_platform_info", "repomap_generate", "repomap_query", "repomap_show",
            "tool_search", "tool_file_info", "tool_syntax_check", "read_latest_logs",
            "get_client_logs", "tool_mysql_query", "mysql_list_databases", "tool_server_status",
            "tool_resource_control", "tool_server_console", "tool_server_control",
            "deploy_resource", "backup_resource",
        )],
        "ssh":             [t for t in tool_names if t.startswith("tool_ssh_")],
        "context_memory":  [t for t in tool_names if t.startswith("context_")],
        "security":        [t for t in tool_names if "security" in t or "scan" in t],
        "patterns":        [t for t in tool_names if t.startswith("pattern_")],
        "fivem_docs":      [t for t in tool_names if t.startswith("fivem_")],
        "code_intelligence": [t for t in tool_names if t in (
            "detect_anti_patterns", "detect_duplicate_code", "find_exports",
            "find_event_handlers", "find_triggers", "trace_event_flow",
            "analyze_export_usage", "validate_export_contracts", "analyze_data_structure",
        )],
    }
    return _json.dumps({
        "status":          "healthy",
        "timestamp":       time.strftime("%Y-%m-%d %H:%M:%S"),
        "agent_version":   _AGENT_VERSION,
        "environment": {
            "project_root":    str(config.project_root),
            "resources_dir":   str(config.resources_dir),
            "os":              config.os or "not set",
            "ssh_configured":  config.has_ssh(),
            "mysql_configured": config.has_mysql(),
            "admin_panel":     config.admin_panel_type,
        },
        "tools": {
            "total":      len(tool_names),
            "categories": {k: len(v) for k, v in categories.items()},
        },
    }, indent=2)

@server.tool()
async def repomap_generate() -> str:
    """Scan your FiveM resources directory and build a map of all resources,
    their files, exports, and event handlers. Run this first."""
    return await repomap.generate()

@server.tool()
async def repomap_query(query_type: str, filter: Optional[str] = None) -> str:
    """Query the resource map. query_type: 'exports', 'events', 'files', or 'all'."""
    return await repomap.query(query_type, filter)

@server.tool()
async def repomap_show() -> str:
    """Show the full resource map JSON."""
    return await repomap.show()

@server.tool()
async def tool_search(pattern: str, path: Optional[str] = None) -> str:
    """Search for a regex pattern across all Lua/JS files in your resources."""
    return await files.search(pattern, path)

@server.tool()
async def tool_file_info(file_path: str) -> str:
    """Get file size, line count, and last modified time."""
    return await files.file_info(file_path)

@server.tool()
async def tool_syntax_check(file_path: str) -> str:
    """Check Lua syntax (built-in checker — no external binary needed)."""
    return await files.syntax_check(file_path)

@server.tool()
async def read_latest_logs(lines: int = 100, pattern: Optional[str] = None) -> str:
    """Read the latest FiveM server log file."""
    if config.admin_panel_type == "custom" and not config.logs_dir_explicit:
        import json as _j
        return _j.dumps({
            "error": "FIVEM_LOGS_DIR not set",
            "hint": "Custom panel mode requires FIVEM_LOGS_DIR to be set in your MCP env config.",
        })
    return await files.read_logs(lines, pattern)

@server.tool()
async def get_client_logs(player_id: int, level: str = "warn", limit: int = 50,
                           since: Optional[int] = None) -> str:
    """Fetch client-side logs captured by the fc-clientlog resource for a connected player.

    Requires the fc-clientlog resource to be installed and running on the server with
    capture enabled. Triggers a fresh dump via the server console (fc_clientlog_get
    <player_id>, same channel as tool_server_console), then reads and filters the JSON
    dump fc-clientlog writes to disk. Returns an error if the player is offline, the
    resource isn't running, or capture is disabled — never dumps raw log files.

    level: minimum severity to return — 'error' (errors only), 'warn' (warn+error,
    default), or 'info' (everything captured).
    limit: max entries returned, default 50, hard-capped at 200 to keep output bounded.
    since: optional unix timestamp — drop entries older than this.
    """
    return await clientlog.get_client_logs(player_id, level, limit, since)

@server.tool()
async def tool_mysql_query(query: str, db_name: str = "default") -> str:
    """Execute a SQL query against a configured MySQL database.

    db_name: 'default', a configured alias, or a real database name. Call mysql_list_databases to see all configured databases.
    """
    return await mysql.query(query, db_name)

@server.tool()
async def mysql_list_databases() -> str:
    """List every configured MySQL connection — alias, real database, host:port, and table list.

    Gives a one-shot topology view of all databases this agent can reach (default + extras).
    """
    return await mysql.list_databases()

@server.tool()
async def tool_server_status() -> str:
    """Check FiveM server status via txAdmin or custom control panel."""
    return await txadmin.server_status()

@server.tool()
async def tool_resource_control(action: str, resource_name: str) -> str:
    """Start, stop, restart, or ensure a resource via txAdmin or custom panel. action: 'start'|'stop'|'restart'|'ensure'."""
    return await txadmin.resource_control(action, resource_name)

@server.tool()
async def tool_server_console(command: str) -> str:
    """Send a server console command.

    txAdmin mode supports: start/stop/restart/ensure <resource>, refresh [resource], restart_server, stop_server.
    Custom panel mode (ADMIN_PANEL_TYPE=custom) passes the command through as-is.
    """
    return await txadmin.server_console(command)

@server.tool()
async def tool_server_control(action: str) -> str:
    """Control the entire FiveM server via txAdmin or custom panel. action: 'restart'|'start'|'stop'."""
    return await txadmin.server_control(action)

@server.tool()
async def deploy_resource(resource_name: str, target: str = "production") -> str:
    """Deploy a resource to your FiveM server.
    Uses SSH if FIVEM_SSH_HOST is configured, otherwise local file copy.
    target: 'production' or an absolute path."""
    return await deploy.deploy(resource_name, target)

@server.tool()
async def backup_resource(resource_name: str) -> str:
    """Create a timestamped backup of a resource in your project's backups/ folder."""
    return await deploy.backup(resource_name)

@server.tool()
async def tool_ssh_run(command: str, timeout: int = 30) -> str:
    """Run a shell command on the remote server via SSH. Returns stdout, stderr, and exit code."""
    return await ssh.run_command(command, timeout)

@server.tool()
async def tool_ssh_ls(path: str = ".") -> str:
    """List files and directories at a path on the remote server."""
    return await ssh.list_dir(path)

@server.tool()
async def tool_ssh_read(path: str) -> str:
    """Read a file from the remote server (up to 100 KB)."""
    return await ssh.read_file(path)

@server.tool()
async def tool_ssh_write(path: str, content: str) -> str:
    """Write content to a file on the remote server via SFTP."""
    return await ssh.write_file(path, content)

@server.tool()
async def tool_ssh_stat(path: str) -> str:
    """Get size, type, and last-modified time for a remote file or directory."""
    return await ssh.stat(path)

# Context memory
@server.tool()
async def context_remember(key: str, value: str, category: str = "general") -> str:
    """Save a fact to persistent local memory."""
    return await context.remember(key, value, category)

@server.tool()
async def context_recall(key: Optional[str] = None, category: Optional[str] = None) -> str:
    """Recall a saved fact by key, filter all facts by category, or list everything."""
    return await context.recall(key, category)

@server.tool()
async def context_search(query: str) -> str:
    """Full-text search across all saved facts (key, value, and category)."""
    return await context.search(query)

@server.tool()
async def context_forget(key: str) -> str:
    """Delete a saved fact."""
    return await context.forget(key)

@server.tool()
async def context_record(summary: str, tags: str) -> str:
    """Record a session note (comma-separated tags)."""
    return await context.record(summary, tags)

@server.tool()
async def context_history(limit: int = 10, tag: Optional[str] = None) -> str:
    """Show recent session history. Optionally filter by tag."""
    return await context.history(limit, tag)

# =============================================================================
# FIVECLAW DOCS — native / framework reference lookups (require an API key)
# =============================================================================

# ─── mcp_guide static content ───
_MCP_GUIDE_TOPICS = {
    "overview": """
# FiveClaw MCP — Getting Started

FiveClaw gives you a suite of server-side tools for analysing, testing, and
scaffolding FiveM resources. Here is the recommended workflow for a new project:

## Step 1 — Understand your codebase
  resource_health_check("fc-core")     — manifest, syntax, lua54, TODOs
  find_exports(resource="fc-core")     — what does this resource expose?
  find_event_handlers(resource="...")  — what events does it handle?
  find_triggers(resource="...")        — what events does it fire?
  detect_anti_patterns()               — scan all resources for common issues

## Step 2 — Validate structure and dependencies
  tool_validate_resource("fc-core")    — deep manifest + file check
  validate_load_order()                — is server.cfg order correct?
  show_dependency_graph()              — who depends on what?

## Step 3 — Security scan
  scan_security_all()                  — critical/high issues across all resources
  scan_security("fc-core")            — detailed findings for one resource

## Step 4 — Scaffold new resources
  pattern_list()                       — see all available templates
  pattern_show("fc-resource")          — preview a template
  pattern_apply("fc-resource", '{"name":"fc-garage","author":"Me"}')

## Tips
- Run mcp_guide("security") for what each security check covers
- Run mcp_guide("patterns") to see all pattern variables
- tool_validate_resource is the quickest first check on any new resource
- detect_anti_patterns() with no arguments scans every resource at once
""",
    "security": """
# Security Scan Guide

  scan_security("my-resource")   — single resource, full findings
  scan_security_all()             — all resources, severity summary

## What is checked

CRITICAL
  - SQL injection: string concatenation in MySQL queries
  - Unvalidated source: using `source` in a server event without checking
    the player is allowed (missing ownership/permission check)

HIGH
  - Client-trusted coordinates: using coords sent from client directly
    without server-side validation
  - Missing ownership check: modifying player data without verifying
    the requesting player owns that data

MEDIUM
  - Sensitive broadcast: TriggerClientEvent to -1 (all clients) with
    data that should be per-player (money, inventory, etc.)

LOW
  - Debug prints left in production code
  - Hardcoded admin source IDs

## Interpreting results
  findings[].file + findings[].line  — exact location
  findings[].context                 — the offending line
  findings[].recommendation          — what to fix
""",
    "patterns": """
# Pattern Library Guide

  pattern_list()                  — show all patterns and their variables
  pattern_show("name")            — preview files that will be created
  pattern_apply("name", json)     — scaffold the files

## Available patterns
  fivem-resource   — bare FiveM resource (manifest, client, server, config)
  fc-resource      — FC Framework resource (fc-core exports, lua54, events)
  fc-shop          — FC shop with NUI, server-side price validation
  fc-job           — FC job (on-duty toggle, grades, blips, duty command)
  mysql-storage    — MySQL table + Lua CRUD module
  state-bag        — State bag entity sync pattern
  nui-component    — React NUI component with FiveM bridge

## Example
  pattern_apply("fc-resource", '{"name":"fc-garage","author":"YourName","description":"Garage system"}')
  — Creates fxmanifest.lua, config.lua, client/main.lua, server/main.lua

## Variables
  All patterns have required and optional variables.
  pattern_show("name") lists every variable, its default, and whether required.
""",
    "health": """
# Health Check Guide

  resource_health_check("my-resource")   — comprehensive check
  tool_validate_resource("my-resource")  — manifest + syntax check

## resource_health_check covers
  - fxmanifest.lua exists and is valid Lua
  - lua54 'yes' present (Lua 5.4 mode — important for performance)
  - All scripts listed in manifest exist on disk
  - ui_page HTML file exists (for NUI resources)
  - oxmysql / MySQL usage detected
  - Syntax errors in every Lua file
  - TODO/FIXME comments found
  - Dependency declarations

## tool_validate_resource covers
  - fxmanifest.lua present
  - Lua syntax valid for every listed file
  - No missing script files

## Common issues and fixes
  lua54 missing       → add `lua54 'yes'` to fxmanifest.lua
  script not found    → file listed in manifest but not on disk
  syntax error        → check the file + line reported
""",
    "dependencies": """
# Dependency Tools Guide

  validate_load_order()               — check server.cfg ensure order
  show_dependency_graph()             — full dependency map
  show_dependency_graph("fc-core")    — single resource tree

## validate_load_order checks
  - Resources loaded before their dependencies (order issue)
  - Resources that depend on something not in server.cfg at all
  - Circular dependency chains (A → B → A)
  - missing_from_disk: resources in server.cfg not found in any [category] folder
  - known_external_resources: FiveM built-ins (mapmanager, sessionmanager…)

## Dependency detection
  manifest deps   — `dependency` / `dependencies {}` in fxmanifest.lua
  export deps     — `exports['resource']:fn()` calls in Lua files

## show_dependency_graph columns
  manifest_dependencies  — declared in manifest
  export_dependencies    — inferred from code
  all_dependencies       — union of both
""",
    "flow": """
# Event Flow Guide

  trace_event_flow("fc-core:client:playerLoaded")
  trace_event_flow("my-event", max_depth=5)

## What it returns
  triggers   — every file/line that fires this event
  handlers   — every file/line that listens for this event (across ALL resources)
  next_flows — events triggered inside each handler (recursive up to max_depth)
  summary    — human-readable list of trigger → handler chains

## Use cases
  - Understand what fires when a player loads
  - Find missing handlers (event triggered but nothing listening)
  - Trace cross-resource event chains
  - Debug why an event isn't reaching its handler

## Also useful
  find_triggers(pattern="playerLoaded")      — find all triggers matching a pattern
  find_event_handlers(event_name="fc-core")  — find handlers by event name prefix
""",
    "nui": """
# NUI Tools Guide

  nui_build_check("my-resource")          — check if NUI needs rebuilding
  nui_completeness_check("my-resource")   — cross-reference Lua ↔ JS NUI calls

## nui_build_check
  Checks if the resource has a web/ directory, and if so whether
  source files are newer than the built output (dist/).
  Returns: needs_rebuild, reason, source_files, built_files

## nui_completeness_check covers
  SendNUIMessage actions in Lua  ↔  window.addEventListener handlers in JS
  RegisterNUICallback names      ↔  fetchNui() calls in JS
  SetNuiFocus(true) calls        ↔  SetNuiFocus(false) calls (balance check)

## Common issues
  Unmatched SendNUIMessage   — Lua sends an action JS never handles → silent failure
  Unmatched fetchNui         — JS calls a callback Lua never registered → timeout
  Focus imbalance            — SetNuiFocus(true) without matching false → input locked
""",
    "contracts": """
# Export Contract Tools Guide

  validate_export_contracts("fc-core")        — validate a provider's contracts
  analyze_export_usage("fc-multicharacter", "fc-core")  — check a caller's usage

## validate_export_contracts
  Checks that every export declared in fxmanifest.lua is actually
  implemented (has a matching exports('name', fn) call).
  Catches: declared but not implemented, implemented but not declared.

## analyze_export_usage
  Compares what a caller resource passes to an export against what
  the provider expects.
  Reports: parameter count mismatches, missing required args, extra args.
""",
}

@server.tool()
async def mcp_guide(topic: Optional[str] = None) -> str:
    """Get a guide on how to use FiveClaw MCP tools. Call with no args for the
    getting-started overview, or pass a topic: 'security', 'patterns',
    'health', 'dependencies', 'flow', 'nui', 'contracts'.
    """
    key = (topic or "overview").lower().strip()
    content = _MCP_GUIDE_TOPICS.get(key)
    if content is None:
        available = ", ".join(f'"{k}"' for k in _MCP_GUIDE_TOPICS if k != "overview")
        return (
            f'Unknown topic "{topic}". '
            f"Available topics: {available}. "
            f'Call mcp_guide() with no arguments for the overview.'
        )
    return content.strip()

@server.tool()
async def tool_validate_resource(resource_name: str) -> str:
    """Validate a FiveM resource — checks manifest, syntax, structure, and best practices."""
    return await _validation.validate_resource(resource_name, files=None)

@server.tool()
async def resource_health_check(resource_name: str) -> str:
    """Comprehensive health check: manifest, syntax, NUI build, TODOs, dependencies."""
    return await _validation.resource_health_check(resource_name, files=None)

@server.tool()
async def detect_anti_patterns(resource: Optional[str] = None) -> str:
    """Detect common FiveM anti-patterns: busy waits, missing locals, performance issues.

    When resource is omitted, scans every resource at once.
    """
    return await _validation.detect_anti_patterns(resource, files=None)

@server.tool()
async def detect_duplicate_code(min_lines: int = 10, resource: Optional[str] = None) -> str:
    """Find duplicate or copy-pasted code blocks across your resources."""
    return await _validation.detect_duplicate_code(min_lines, resource, files=None)

@server.tool()
async def find_exports(export_name: Optional[str] = None, resource: Optional[str] = None) -> str:
    """Find where exports are defined and called across your resources."""
    return await _validation.find_exports(export_name, resource, files=None)

@server.tool()
async def find_event_handlers(event_name: Optional[str] = None, resource: Optional[str] = None) -> str:
    """Find RegisterNetEvent and AddEventHandler calls."""
    return await _validation.find_event_handlers(event_name, resource, files=None)

@server.tool()
async def find_triggers(pattern: Optional[str] = None, resource: Optional[str] = None) -> str:
    """Find TriggerServerEvent and TriggerClientEvent calls."""
    return await _validation.find_triggers(pattern, resource, files=None)

@server.tool()
async def nui_build_check(resource_name: str) -> str:
    """Check if a NUI resource needs to be rebuilt."""
    return await _validation.nui_build_check(resource_name, files=None)

# A test-generation/execution suite (test_resource / test_generate / test_function /
# test_event / test_coverage / test_database) is planned for a later release and is
# not wired up yet.

@server.tool()
async def trace_event_flow(event_name: str, max_depth: int = 3) -> str:
    """Trace how an event flows through your resources."""
    return await _flow.trace_event_flow(event_name, max_depth, files=None)

@server.tool()
async def analyze_export_usage(caller: str, target: str, export_name: Optional[str] = None) -> str:
    """Analyze how a resource uses another resource's exports."""
    return await _contract.analyze_export_usage(caller, target, export_name, files=None)

@server.tool()
async def mysql_visualize_schema(db_name: str = "default") -> str:
    """Visualize a database's schema as an ASCII diagram (tables, columns, foreign keys).

    Runs locally against your MySQL — the schema and data never leave your machine.
    db_name: 'default', a configured alias, or a real database name (see mysql_list_databases).
    """
    return await mysql.visualize_schema(db_name)

@server.tool()
async def pattern_list() -> str:
    """List all available FiveM code patterns in the FiveClaw pattern library."""
    return await _pattern.list_patterns(team_id=None)

@server.tool()
async def pattern_show(pattern_name: str) -> str:
    """Show the full code for a pattern from the FiveClaw library."""
    return await _pattern.show_pattern(pattern_name, team_id=None)

@server.tool()
async def pattern_apply(pattern_name: str, variables_json: str,
                        output_dir: Optional[str] = None, dry_run: bool = False) -> str:
    """Apply a FiveClaw code pattern to scaffold a new resource or feature."""
    try:
        variables = _json.loads(variables_json)
    except _json.JSONDecodeError as e:
        return _json.dumps({"error": f"Invalid JSON in variables: {str(e)}"})
    return await _pattern.apply_pattern(pattern_name, variables, output_dir, dry_run, team_id=None)

@server.tool()
async def nui_completeness_check(resource_name: str) -> str:
    """Check NUI integration completeness: SendNUIMessage ↔ JS handlers, RegisterNUICallback ↔ fetchNui, SetNuiFocus balance."""
    return await _validation.nui_completeness_check(resource_name, files=None)

@server.tool()
async def scan_security(resource_name: str) -> str:
    """Scan a resource for server-side security vulnerabilities: unvalidated source, SQL injection, sensitive broadcasts."""
    return await _security.scan_resource(resource_name, files=None)

@server.tool()
async def scan_security_all() -> str:
    """Scan ALL resources for security vulnerabilities and return a severity summary."""
    return await _security.scan_all(files=None)

@server.tool()
async def validate_load_order() -> str:
    """Validate server.cfg ensure order against resource dependencies."""
    return await _dependency.validate_load_order(files=None)

@server.tool()
async def show_dependency_graph(resource_name: Optional[str] = None) -> str:
    """Show the dependency graph for a resource or all resources."""
    return await _dependency.show_dependency_graph(resource_name, files=None)

# ── FiveM docs: search ────────────────────────────────────────────────────

@server.tool()
async def fivem_docs(query: str) -> str:
    """Search FiveM docs, natives, frameworks, and scripting guides all at once."""
    return remote.call("fivem_docs", {"query": query})

@server.tool()
async def fivem_search_by_category(category: str, query: str) -> str:
    """Search within a specific FiveM doc category (e.g. 'native', 'esx', 'qbcore', 'oxlib')."""
    return remote.call("fivem_search_by_category", {"category": category, "query": query})

@server.tool()
async def fivem_get_search_categories() -> str:
    """List all available search categories for fivem_search_by_category."""
    return remote.call("fivem_get_search_categories", {})

# ── FiveM docs: natives ───────────────────────────────────────────────────

@server.tool()
async def fivem_native(native_name: str) -> str:
    """Get full details for a FiveM native function — signature, params, return value, examples."""
    return remote.call("fivem_native", {"native_name": native_name})

@server.tool()
async def fivem_search_live_natives(query: str) -> str:
    """Search the live CFX native database (requires network). More up-to-date than the cached index."""
    return remote.call("fivem_search_live_natives", {"query": query})

@server.tool()
async def fivem_get_live_native_details(native_name: str) -> str:
    """Fetch full details for a native by name from the live CFX API."""
    return remote.call("fivem_get_live_native_details", {"native_name": native_name})

@server.tool()
async def fivem_fetch_live_natives(force_refresh: bool = False) -> str:
    """Pull the latest native list from CFX. Set force_refresh=True to bypass cache."""
    return remote.call("fivem_fetch_live_natives", {"force_refresh": force_refresh})

@server.tool()
async def fivem_get_live_docs_status() -> str:
    """Check the live CFX docs API availability and last-updated timestamp."""
    return remote.call("fivem_get_live_docs_status", {})

# ── FiveM docs: frameworks ────────────────────────────────────────────────

@server.tool()
async def fivem_get_framework_docs(framework: str, topic: Optional[str] = None) -> str:
    """Get documentation for a specific framework. framework: 'esx' | 'qbcore' | 'ox_lib' | 'oxmysql'."""
    return remote.call("fivem_get_framework_docs", {"framework": framework, "topic": topic})

@server.tool()
async def fivem_get_framework_pattern(framework: str, pattern: Optional[str] = None) -> str:
    """Get idiomatic code patterns for a framework (player loading, callbacks, inventory, etc.)."""
    return remote.call("fivem_get_framework_pattern", {"framework": framework, "pattern": pattern})

@server.tool()
async def fivem_get_framework_comparison() -> str:
    """Compare ESX vs QBCore vs ox_lib — architecture, API style, migration notes."""
    return remote.call("fivem_get_framework_comparison", {})

# ── FiveM docs: knowledge ─────────────────────────────────────────────────

@server.tool()
async def fivem_explain_concept(concept: str) -> str:
    """Explain a FiveM concept in plain language (e.g. 'state bags', 'net events', 'deferred loading')."""
    return remote.call("fivem_explain_concept", {"concept": concept})

@server.tool()
async def fivem_get_best_practice(topic: Optional[str] = None) -> str:
    """Get best practice guides for FiveM development (performance, security, sync, etc.)."""
    return remote.call("fivem_get_best_practice", {"topic": topic})

@server.tool()
async def fivem_get_anti_pattern(topic: Optional[str] = None) -> str:
    """Get common FiveM anti-patterns to avoid, with explanations and fixes."""
    return remote.call("fivem_get_anti_pattern", {"topic": topic})

@server.tool()
async def fivem_get_error_solution(error_type: Optional[str] = None, search: Optional[str] = None) -> str:
    """Look up a known FiveM error message and get the cause + fix."""
    return remote.call("fivem_get_error_solution", {"error_type": error_type, "search": search})

@server.tool()
async def fivem_get_troubleshooting_guide(issue: str) -> str:
    """Get a troubleshooting guide for a FiveM issue (e.g. 'sync', 'nui', 'database', 'resources')."""
    return remote.call("fivem_get_troubleshooting_guide", {"issue": issue})

@server.tool()
async def fivem_get_lua_reference(topic: str) -> str:
    """Get Lua language reference relevant to FiveM (closures, coroutines, metatables, etc.)."""
    return remote.call("fivem_get_lua_reference", {"topic": topic})

@server.tool()
async def fivem_get_export_contract_guide() -> str:
    """Get guides on writing and consuming FiveM resource exports correctly."""
    return remote.call("fivem_get_export_contract_guide", {})

# ── FiveM docs: templates & snippets ──────────────────────────────────────

@server.tool()
async def fivem_generate_resource_template(resource_type: str, framework: Optional[str] = None) -> str:
    """Generate a FiveM resource template. resource_type: 'client' | 'server' | 'shared'."""
    return remote.call("fivem_generate_resource_template", {"resource_type": resource_type, "framework": framework})

@server.tool()
async def fivem_get_sql_snippet(category: Optional[str] = None, name: Optional[str] = None) -> str:
    """Get SQL snippet examples for FiveM (oxmysql patterns, migrations, indexing, etc.)."""
    return remote.call("fivem_get_sql_snippet", {"category": category, "name": name})

@server.tool()
async def fivem_get_vehicle_spawn(search: Optional[str] = None, category: Optional[str] = None) -> str:
    """Look up vehicle spawn names/hashes. Use search for a model name, category for a vehicle class."""
    return remote.call("fivem_get_vehicle_spawn", {"search": search, "category": category})

@server.tool()
async def fivem_fetch_fivem_docs(page: str = "scripting-manual") -> str:
    """Fetch a page from the live CFX/FiveM documentation (e.g. 'scripting-manual', 'natives')."""
    return remote.call("fivem_fetch_fivem_docs", {"page": page})

@server.tool()
async def fivem_fetch_github_repo(repo_path: str = "", path: str = "", branch: str = "main",
                                   search_query: str = "") -> str:
    """Fetch files from a GitHub repo or search GitHub for FiveM resources.

    Search mode: leave repo_path empty, set search_query (e.g. "esx inventory", "qbcore jobs language:lua").
    Fetch mode: set repo_path ('owner/repo'), optionally path to a file or directory.
    """
    return remote.call("fivem_fetch_github_repo",
                       {"repo_path": repo_path, "path": path, "branch": branch,
                        "search_query": search_query})

# ── FiveM docs: guide ─────────────────────────────────────────────────────

@server.tool()
async def fivem_guide(topic: Optional[str] = None) -> str:
    """Get a guide on how to use the FiveM docs/natives/frameworks tools. Call
    with no args for the getting-started overview, or pass a topic: 'natives',
    'docs', 'frameworks', 'knowledge', 'templates', 'workflows'."""
    return remote.call("fivem_guide", {"topic": topic})

# ── contracts ──────────────────────────────────────────────────────────────

@server.tool()
async def validate_export_contracts(resource: str) -> str:
    """Validate that all exports defined in a resource match how they are consumed by callers."""
    return await _contract.validate_export_contracts(resource, files=None)

@server.tool()
async def analyze_data_structure(
    caller_file: str,
    caller_line: int,
    variable_name: str,
    target_resource: str,
    target_export: str,
    param_index: int = 2,
) -> str:
    """Analyze a table/object at a specific call site and compare it against the
    expected parameter structure of the target export.
    """
    return await _contract.analyze_data_structure(
        caller_file, caller_line, variable_name,
        target_resource, target_export, param_index,
        files=None,
    )


# =============================================================================
# Entry point
# =============================================================================

def main():
    import signal, sys

    def _stop(sig, frame):
        print("\n[FiveClaw Agent] Shutting down.", file=sys.stderr)
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    try:
        signal.signal(signal.SIGTERM, _stop)  # not available on Windows
    except AttributeError:
        pass

    transport = os.getenv("TRANSPORT", "stdio")
    host = os.getenv("HOST", "127.0.0.1")   # localhost only by default
    port = int(os.getenv("PORT", "5200"))

    if transport in ("streamable-http", "sse"):
        path = "/sse" if transport == "sse" else "/mcp"
        print(f"[FiveClaw Agent] Starting on http://{host}:{port}{path} (transport={transport})", file=sys.stderr)
        print(f"[FiveClaw Agent] Project root: {config.project_root}", file=sys.stderr)
        if transport == "streamable-http":
            server.run(transport="streamable-http", host=host, port=port)
        else:
            server.run(transport="sse", host=host, port=port)
    else:
        # Force unbuffered stdout for stdio transport — critical on Windows where
        # Python switches to fully-buffered mode when stdout is connected to a pipe.
        # The MCP client (Claude Code, Cursor, etc.) never receives responses otherwise.
        import io as _io
        sys.stdout = _io.TextIOWrapper(
            sys.stdout.buffer, line_buffering=True, write_through=True
        )
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
