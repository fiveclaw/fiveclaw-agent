"""
FiveClaw Agent — local MCP server.

Users run this on their machine and add it to their Claude Desktop config:
  { "fiveclaw": { "url": "http://localhost:5200/mcp" } }

Local tools (file I/O, SSH, MySQL, txAdmin/custom panel) run on the user's machine.
Intelligence tools (analysis, AI review, docs, patterns) relay to the FiveClaw VPS.
The VPS logic is never distributed — only results come back.
"""

import json as _json
import os
from typing import Optional
from fastmcp import FastMCP

from .config import Config
from .local import RepoMapTool, FileTool, MySQLTool, TxAdminTool, CustomPanelTool, DeployTool, ContextTool, collect_resource_files
from .remote import RemoteClient

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

server = FastMCP("fiveclaw-agent")

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _resource_files(resource: Optional[str] = None) -> dict:
    """Collect local source files to send to the VPS for analysis."""
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

def _unwrap(raw: str) -> dict:
    """
    Parse the raw VPS response and return the inner result dict.

    remote.call returns the JSON-RPC response directly:
      {"jsonrpc":"2.0","id":N,"result":{"content":[{"type":"text","text":"<actual-json>"}],...}}
    """
    try:
        parsed = _json.loads(raw)
        # JSON-RPC envelope
        if isinstance(parsed, dict) and "result" in parsed:
            rpc_result = parsed["result"]
            if isinstance(rpc_result, dict):
                content = rpc_result.get("content", [])
                if content and isinstance(content[0], dict):
                    text = content[0].get("text", "")
                    if text:
                        return _json.loads(text)
        return {}
    except Exception:
        return {}

def _per_resource_call(tool: str, base_params: dict, list_key: str, count_key: str) -> str:
    """
    Call a tool once per resource and merge the list results.
    Avoids HTTP write timeouts caused by large all-resource payloads.
    """
    resources = _list_resources()
    if not resources:
        return _json.dumps({"error": "No resources found in configured directory."})

    merged: list = []
    total = 0
    for r in resources:
        result = _unwrap(remote.call(tool, {**base_params, "resource": r}, files=_resource_files(r)))
        merged.extend(result.get(list_key, []))
        total += result.get(count_key, 0)

    return _json.dumps({count_key: total, list_key: merged, "resources_scanned": len(resources)})

# =============================================================================
# LOCAL TOOLS — run on the user's machine
# =============================================================================

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
    """Check Lua syntax using luac. Requires luac installed locally."""
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
async def tool_mysql_query(query: str, db_name: str = "default") -> str:
    """Execute a SQL query against a configured MySQL database.

    db_name: 'default' or a named database from MYSQL_EXTRA_DBS (e.g. 'qbcore', 'trucking', 'hz').
    """
    return await mysql.query(query, db_name)

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
    """Send a raw console command via txAdmin or custom panel."""
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
# REMOTE TOOLS — relay to FiveClaw VPS (your logic stays server-side)
# =============================================================================

@server.tool()
async def mcp_guide(topic: Optional[str] = None) -> str:
    """Get a guide on how to use FiveClaw MCP tools. Call with no args for the
    getting-started overview, or pass a topic: 'testing', 'security', 'patterns',
    'health', 'dependencies', 'flow', 'nui', 'contracts'."""
    return remote.call("mcp_guide", {"topic": topic})

@server.tool()
async def tool_validate_resource(resource_name: str) -> str:
    """Validate a FiveM resource — checks manifest, syntax, structure, and best practices."""
    return remote.call("tool_validate_resource", {"resource_name": resource_name},
                       files=_resource_files(resource_name))

@server.tool()
async def resource_health_check(resource_name: str) -> str:
    """Comprehensive health check: manifest, syntax, NUI build, TODOs, dependencies."""
    return remote.call("resource_health_check", {"resource_name": resource_name},
                       files=_resource_files(resource_name))

@server.tool()
async def detect_anti_patterns(resource: Optional[str] = None) -> str:
    """Detect common FiveM anti-patterns: busy waits, missing locals, performance issues."""
    if resource is None:
        return _per_resource_call("detect_anti_patterns", {}, "findings", "total_issues")
    return remote.call("detect_anti_patterns", {"resource": resource},
                       files=_resource_files(resource))

@server.tool()
async def detect_duplicate_code(min_lines: int = 10, resource: Optional[str] = None) -> str:
    """Find duplicate or copy-pasted code blocks across your resources."""
    if resource is None:
        return _per_resource_call("detect_duplicate_code", {"min_lines": min_lines}, "groups", "total_groups")
    return remote.call("detect_duplicate_code", {"min_lines": min_lines, "resource": resource},
                       files=_resource_files(resource))

@server.tool()
async def find_exports(export_name: Optional[str] = None, resource: Optional[str] = None) -> str:
    """Find where exports are defined and called across your resources."""
    if resource is None:
        return _per_resource_call("find_exports", {"export_name": export_name}, "exports", "found_count")
    return remote.call("find_exports", {"export_name": export_name, "resource": resource},
                       files=_resource_files(resource))

@server.tool()
async def find_event_handlers(event_name: Optional[str] = None, resource: Optional[str] = None) -> str:
    """Find RegisterNetEvent and AddEventHandler calls."""
    if resource is None:
        return _per_resource_call("find_event_handlers", {"event_name": event_name}, "handlers", "found_count")
    return remote.call("find_event_handlers", {"event_name": event_name, "resource": resource},
                       files=_resource_files(resource))

@server.tool()
async def find_triggers(pattern: Optional[str] = None, resource: Optional[str] = None) -> str:
    """Find TriggerServerEvent and TriggerClientEvent calls."""
    if resource is None:
        return _per_resource_call("find_triggers", {"pattern": pattern}, "triggers", "found_count")
    return remote.call("find_triggers", {"pattern": pattern, "resource": resource},
                       files=_resource_files(resource))

@server.tool()
async def nui_build_check(resource_name: str) -> str:
    """Check if a NUI resource needs to be rebuilt."""
    return remote.call("nui_build_check", {"resource_name": resource_name},
                       files=_resource_files(resource_name))

@server.tool()
async def test_resource(resource_name: str, mode: str = "auto") -> str:
    """Test a FiveM resource using the FiveClaw test engine. mode: 'auto' or 'analyze'."""
    return remote.call("test_resource", {"resource_name": resource_name, "mode": mode},
                       files=_resource_files(resource_name))

@server.tool()
async def test_generate(resource_name: str) -> str:
    """Generate test cases for a resource without running them."""
    return remote.call("test_generate", {"resource_name": resource_name},
                       files=_resource_files(resource_name))

@server.tool()
async def test_function(resource_name: str, function_name: str, args: Optional[list] = None) -> str:
    """Test a specific exported function in a resource using the FiveClaw test engine."""
    return remote.call("test_function",
                       {"resource_name": resource_name, "function_name": function_name, "args": args or []},
                       files=_resource_files(resource_name))

@server.tool()
async def test_event(event_name: str, payload: Optional[dict] = None, source: int = 1) -> str:
    """Test an event handler by triggering it against the FiveClaw test engine."""
    return remote.call("test_event",
                       {"event_name": event_name, "payload": payload or {}, "source": source},
                       files=_resource_files())

@server.tool()
async def test_coverage(resource_name: str) -> str:
    """Analyze test coverage for a resource — shows which functions, exports, and events are tested."""
    return remote.call("test_coverage", {"resource_name": resource_name},
                       files=_resource_files(resource_name))

@server.tool()
async def test_database(resource_name: str) -> str:
    """Test resource database operations using transaction rollback — no test data persists."""
    return remote.call("test_database", {"resource_name": resource_name},
                       files=_resource_files(resource_name))

@server.tool()
async def trace_event_flow(event_name: str, max_depth: int = 3) -> str:
    """Trace how an event flows through your resources."""
    # Send only the two resources most likely involved: the one registering the event
    # and all others (kept small by collecting all resources individually)
    all_files: dict = {}
    for r in _list_resources():
        all_files.update(_resource_files(r))
    return remote.call("trace_event_flow", {"event_name": event_name, "max_depth": max_depth},
                       files=all_files)

@server.tool()
async def analyze_export_usage(caller: str, target: str, export_name: Optional[str] = None) -> str:
    """Analyze how a resource uses another resource's exports."""
    merged = {**_resource_files(caller), **_resource_files(target)}
    return remote.call("analyze_export_usage",
                       {"caller": caller, "target": target, "export_name": export_name},
                       files=merged)

@server.tool()
async def mysql_visualize_schema() -> str:
    """Visualize your database schema as an ASCII diagram."""
    return remote.call("mysql_visualize_schema", {})

@server.tool()
async def pattern_list() -> str:
    """List all available FiveM code patterns in the FiveClaw pattern library."""
    return remote.call("pattern_list", {})

@server.tool()
async def pattern_show(pattern_name: str) -> str:
    """Show the full code for a pattern from the FiveClaw library."""
    return remote.call("pattern_show", {"pattern_name": pattern_name})

@server.tool()
async def pattern_apply(pattern_name: str, variables_json: str,
                        output_dir: Optional[str] = None, dry_run: bool = False) -> str:
    """Apply a FiveClaw code pattern to scaffold a new resource or feature."""
    return remote.call("pattern_apply",
                       {"pattern_name": pattern_name, "variables_json": variables_json,
                        "output_dir": output_dir, "dry_run": dry_run})

@server.tool()
async def nui_completeness_check(resource_name: str) -> str:
    """Check NUI integration completeness: SendNUIMessage ↔ JS handlers, RegisterNUICallback ↔ fetchNui, SetNuiFocus balance."""
    return remote.call("nui_completeness_check", {"resource_name": resource_name},
                       files=_resource_files(resource_name))

@server.tool()
async def scan_security(resource_name: str) -> str:
    """Scan a resource for server-side security vulnerabilities: unvalidated source, SQL injection, sensitive broadcasts."""
    return remote.call("scan_security", {"resource_name": resource_name},
                       files=_resource_files(resource_name))

@server.tool()
async def scan_security_all() -> str:
    """Scan ALL resources for security vulnerabilities and return a severity summary."""
    resources = _list_resources()
    if not resources:
        return _json.dumps({"error": "No resources found."})
    summary: dict = {}
    for r in resources:
        result = _unwrap(remote.call("scan_security", {"resource_name": r}, files=_resource_files(r)))
        summary[r] = {"total_issues": result.get("total_issues", 0), "findings": result.get("findings", [])}
    total = sum(v["total_issues"] for v in summary.values())
    return _json.dumps({"resources_scanned": len(resources), "total_issues": total, "by_resource": summary})

@server.tool()
async def validate_load_order() -> str:
    """Validate server.cfg ensure order against resource dependencies."""
    return remote.call("validate_load_order", {}, files=_manifest_files())

@server.tool()
async def show_dependency_graph(resource_name: Optional[str] = None) -> str:
    """Show the dependency graph for a resource or all resources."""
    return remote.call("show_dependency_graph", {"resource_name": resource_name},
                       files=_manifest_files())

# ── fivem-mcp: search ─────────────────────────────────────────────────────

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

# ── fivem-mcp: natives ────────────────────────────────────────────────────

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

# ── fivem-mcp: frameworks ─────────────────────────────────────────────────

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

# ── fivem-mcp: knowledge ──────────────────────────────────────────────────

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

# ── fivem-mcp: templates & snippets ──────────────────────────────────────

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
async def fivem_fetch_github_repo(repo_path: str, path: str = "", branch: str = "main") -> str:
    """Fetch file contents from a GitHub repo (e.g. repo_path='esx-framework/es_extended', path='README.md')."""
    return remote.call("fivem_fetch_github_repo", {"repo_path": repo_path, "path": path, "branch": branch})

# ── fivem-mcp: guide ─────────────────────────────────────────────────────

@server.tool()
async def fivem_guide(topic: Optional[str] = None) -> str:
    """Get a guide on how to use fivem-mcp tools. Call with no args for the
    getting-started overview, or pass a topic: 'natives', 'docs', 'frameworks',
    'knowledge', 'templates', 'workflows'."""
    return remote.call("fivem_guide", {"topic": topic})

# ── ai-fivem-dev-mcp: contracts ───────────────────────────────────────────

@server.tool()
async def validate_export_contracts(resource: str) -> str:
    """Validate that all exports defined in a resource match how they are consumed by callers."""
    return remote.call("validate_export_contracts", {"resource": resource},
                       files=_resource_files(resource))

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
    expected parameter structure of the target export."""
    return remote.call("analyze_data_structure", {
        "caller_file":     caller_file,
        "caller_line":     caller_line,
        "variable_name":   variable_name,
        "target_resource": target_resource,
        "target_export":   target_export,
        "param_index":     param_index,
    }, files=_resource_files(target_resource))


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
