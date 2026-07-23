#!/usr/bin/env python3
"""Tool implementations for the FiveClaw agent"""

import asyncio
import json
import subprocess
import os
import re
import shutil
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Dict, Any, List
from .config import Config
from .patterns_data import PATTERNS, apply_template_variables


def _normalize_cfxlua(code: str) -> str:
    """Rewrite CfxLua-only syntax into parser-valid vanilla Lua so a vanilla parser
    (luac / lupa's LuaJIT / luaparser) doesn't FALSE-flag it. FiveM's CfxLua adds
    extensions the game accepts but stock parsers reject — most commonly safe
    navigation (`a?.b`, `a?[k]`) and compound assignment (`x += y`, `-=`, `*=`, `/=`,
    `%=`, `^=`, `..=`). These are the '18 syntax errors in qbx_core' class of false
    positive.

    The rewrites only REMOVE/REPLACE characters without ever adding or removing a
    string/comment delimiter, so applying them across the whole file (even inside a
    string or comment) leaves the file's *syntactic* validity unchanged — which is all
    a syntax check cares about. That's why this needs no full Lua lexer.

    Semantics are intentionally NOT preserved (`x += y` becomes `x = y`); we only ever
    ask 'does it parse?', never run the result."""
    # safe navigation → normal indexing. Vanilla Lua has no '?' operator, so any '?.'
    # or '?[' is either CfxLua safe-nav (rewrite it) or inside a string/comment (a
    # harmless char swap that keeps the literal valid).
    code = code.replace("?.", ".").replace("?[", "[")
    # compound assignment → plain assignment. Operator set excludes ==, ~=, <=, >=, so
    # those comparisons are untouched. Guard ..= so it never eats the last two dots of
    # a '...=' (which is invalid Lua anyway).
    code = re.sub(r"(?<!\.)\.\.=", "=", code)               # ..=  (concat-assign)
    code = re.sub(r"([-+*/%^])=(?!=)", "=", code)           # += -= *= /= %= ^=
    return code


def _lua_syntax_check(file_path) -> tuple:
    """Check one Lua file's syntax, cross-platform + CfxLua-aware. Normalizes CfxLua
    extensions first (see _normalize_cfxlua), then prefers the `luac` binary (best
    error positions) when present; otherwise falls back to `lupa` (embedded LuaJIT — a
    bundled dependency, so this works on Windows/macOS with no luac installed) and then
    `luaparser`. Returns (valid: bool, error: Optional[str])."""
    p = Path(file_path)
    try:
        raw = p.read_text(errors="ignore")
    except Exception as e:
        return False, str(e)[:200]
    code = _normalize_cfxlua(raw)
    had_cfxlua = code != raw
    # 1. luac binary — exact positions. Only usable on the ORIGINAL file, and only when
    #    there's no CfxLua to normalize (luac reads the path directly and would reject
    #    CfxLua). With CfxLua present, skip to the in-process checkers, which run on the
    #    normalized string. Newlines are never changed, so reported line numbers stay
    #    accurate either way.
    if not had_cfxlua:
        try:
            r = subprocess.run(["luac", "-p", str(p)], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return True, None
            return False, (r.stderr.strip() or "syntax error")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass  # not installed, or hung — use an in-process checker instead
    # 2. lupa (embedded LuaJIT) — cross-platform, bundled dependency
    try:
        import lupa
        rt = lupa.LuaRuntime(unpack_returned_tuples=True)
        checker = rt.eval(
            'function(code, name) '
            '  local f, err = load(code, "@"..name, "t") '
            '  if f then return true, nil else return false, err end '
            'end'
        )
        ok, err = checker(code, p.name)
        return (True, None) if ok else (False, str(err) if err else "syntax error")
    except ImportError:
        pass
    except Exception:
        pass  # lupa runtime hiccup — fall through to the pure-Python parser
    # 3. luaparser (pure Python) — always available
    try:
        from luaparser import ast as _lua_ast
        try:
            _lua_ast.parse(code)
            return True, None
        except Exception as e:
            return False, str(e)[:200]
    except ImportError:
        pass
    return True, None  # no checker at all (shouldn't happen — lupa+luaparser are deps); don't block


def _declared_file_missing(resource_dir: Path, rel: str) -> bool:
    """Is a manifest-declared script/file path actually missing? FiveM manifests allow
    GLOB patterns (e.g. `modules/*.lua`, `**/*.lua`), which FXServer expands at load —
    so expand them here too instead of stat-ing the literal pattern (which always
    'misses'). A non-glob path is missing iff it doesn't exist."""
    if any(c in rel for c in "*?["):
        try:
            return not any(resource_dir.glob(rel))
        except (ValueError, OSError):
            return False  # malformed/odd pattern → don't false-flag it as missing
    return not (resource_dir / rel).exists()


@contextmanager
def _files_ctx(config: "Config", files: Optional[dict]):
    """Context manager: if files provided, write them to a temp dir and yield its path
    as a replacement for config.resources_dir.  Otherwise yield config.resources_dir.

    Files dict keys are expected to look like:
        resources/[local]/fc-core/fxmanifest.lua   (categorised)
        resources/fc-core/fxmanifest.lua            (flat)
    Both layouts are normalised so that tmp_root / "fc-core" / "..." is always valid.
    """
    if not files:
        yield config.resources_dir
        return

    tmpdir = tempfile.mkdtemp(prefix="fiveclaw_")
    try:
        tmp_root = Path(tmpdir)
        for rel_path, content in files.items():
            parts = Path(rel_path).parts
            # Strip leading "resources" segment
            try:
                res_idx = next(i for i, p in enumerate(parts) if p == "resources")
                remaining = parts[res_idx + 1:]
            except StopIteration:
                remaining = parts
            # Strip category dir if it starts with "["
            if remaining and remaining[0].startswith("["):
                remaining = remaining[1:]
            if not remaining:
                continue
            dest = tmp_root / Path(*remaining)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, errors="ignore")
        yield tmp_root
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _resolve_resource_dir(resources_dir: Path, resource_name: str) -> Optional[Path]:
    """Locate a resource's directory under resources_dir, tolerating FiveM's
    [category] bracket folders. Handles a flat layout ('fc-mdt'), a bracketed
    layout where the resource lives one level under a [category] dir, and a
    resource_name passed as a category-relative path ('[local]/fc-mdt').
    Returns None if it can't be found."""
    # 1. Direct hit — flat layout, or a temp tree where _files_ctx already
    #    stripped the [category] segment.
    direct = resources_dir / resource_name
    if direct.exists():
        return direct
    # 2. resource_name given as a category-relative path, e.g. "[local]/fc-mdt".
    parts = Path(resource_name).parts
    if len(parts) > 1:
        nested = resources_dir / Path(*parts)
        if nested.exists():
            return nested
        resource_name = parts[-1]  # retry searches below with the bare leaf name
        direct = resources_dir / resource_name
        if direct.exists():
            return direct
    # 3. Bare name nested one level under a [category] dir.
    if resources_dir.exists():
        for cat in resources_dir.iterdir():
            if cat.is_dir() and cat.name.startswith("["):
                candidate = cat / resource_name
                if candidate.exists():
                    return candidate
    return None

# Tool-call logging — OFF by default. It's opt-in via FIVECLAW_AGENT_LOG_DIR so the
# agent never writes users' resource params/results to disk unless they ask, and so
# there's no hardcoded /var/log or /tmp path (both absent/unwritable on Windows).
_LOG_DIR_ENV = os.getenv("FIVECLAW_AGENT_LOG_DIR", "").strip()
LOG_DIR = Path(_LOG_DIR_ENV) if _LOG_DIR_ENV else None
LOG_FILE = (LOG_DIR / "tool-calls.log") if LOG_DIR else None
DETAIL_LOG_FILE = (LOG_DIR / "tool-details.log") if LOG_DIR else None

def ensure_log_dir():
    """Create the opt-in log directory if logging is enabled."""
    if LOG_DIR is None:
        return
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

def get_client_info():
    """Identify the calling client by parent process (best-effort; Linux-only via
    /proc, returns 'unknown' elsewhere)."""
    try:
        ppid = os.getppid()
        with open(f"/proc/{ppid}/cmdline", "r") as f:
            parent_cmd = f.read().replace('\x00', ' ')[:100]
        return f"PPID:{ppid} {parent_cmd}"
    except Exception:
        return "unknown"

def log_tool_call(tool_name: str, params: dict, duration_ms: float, error: str = None, result: str = None):
    """Log a tool call with full details — no-op unless FIVECLAW_AGENT_LOG_DIR is set."""
    if LOG_DIR is None:
        return
    ensure_log_dir()

    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    client = get_client_info()
    
    # Summary log (one line per call)
    param_summary = json.dumps(params)
    if len(param_summary) > 200:
        param_summary = param_summary[:197] + "..."
    
    status = "ERROR" if error else "OK"
    result_size = len(result) if result else 0
    error_str = f" ERR:{error[:100]}" if error else ""
    
    log_line = f"[{timestamp}] [{status}] {tool_name}({param_summary}) - {duration_ms:.2f}ms - {result_size}b - {client}{error_str}\n"
    
    try:
        with open(LOG_FILE, "a") as f:
            f.write(log_line)
    except Exception:
        pass
    
    # Detailed log (full params and result preview)
    if result:
        detail_entry = {
            "timestamp": timestamp,
            "tool": tool_name,
            "params": params,
            "duration_ms": round(duration_ms, 2),
            "result_preview": result[:500] if len(result) > 500 else result,
            "result_size": result_size,
            "status": status,
            "client": client,
            "error": error
        }
        try:
            with open(DETAIL_LOG_FILE, "a") as f:
                f.write(json.dumps(detail_entry) + "\n")
        except:
            pass



# Tool call logging decorator
def log_method(func):
    """Decorator to log method calls with full details"""
    async def wrapper(*args, **kwargs):
        start = time.time()
        result = None
        error = None
        try:
            result = await func(*args, **kwargs)
            duration = (time.time() - start) * 1000
            log_tool_call(func.__name__, kwargs, duration, None, result)
            return result
        except Exception as e:
            duration = (time.time() - start) * 1000
            error = str(e)
            log_tool_call(func.__name__, kwargs, duration, error, result)
            raise
    return wrapper

class RepoMapTool:
    """Tool for codebase mapping and querying."""
    
    def __init__(self, config: Config):
        self.config = config
        self.map_file = config.cache_dir / "repomap.json"
    
    @log_method
    async def generate(self) -> str:
        """Generate the repository map by scanning resources directory"""
        try:
            resources = {}
            
            if not self.config.resources_dir.exists():
                return json.dumps({"error": f"Resources directory not found: {self.config.resources_dir}"}, indent=2)
            
            for entry in self.config.resources_dir.iterdir():
                if not entry.is_dir():
                    continue
                if (entry / "fxmanifest.lua").exists():
                    resources[entry.name] = self._scan_resource(entry)
                else:
                    # [category] folder (e.g. [local], [qbx]) — scan each child resource
                    for sub in entry.iterdir():
                        if sub.is_dir() and (sub / "fxmanifest.lua").exists():
                            resources[sub.name] = self._scan_resource(sub)
            
            result = {
                "resources": resources,
                "metadata": {
                    "generated": "auto",
                    "count": len(resources),
                    "path": str(self.config.resources_dir)
                }
            }
            
            # Save to cache
            self.config.cache_dir.mkdir(parents=True, exist_ok=True)
            with open(self.map_file, 'w') as f:
                json.dump(result, f, indent=2)
            
            return json.dumps({"success": True, "resources_count": len(resources), "map_file": str(self.map_file)}, indent=2)
            
        except Exception as e:
            return json.dumps({"error": str(e)}, indent=2)
    
    def _scan_resource(self, resource_dir: Path) -> Dict:
        """Scan a single resource directory"""
        info = {"name": resource_dir.name}
        
        # Detect type
        has_web = (resource_dir / "web").exists() or (resource_dir / "nui").exists()
        info["type"] = "nui" if has_web else "lua"
        
        # List files
        files = []
        for pattern in ["*.lua", "*.js", "*.jsx", "*.ts", "*.tsx", "*.html", "*.css"]:
            files.extend([str(f.relative_to(resource_dir)) for f in resource_dir.rglob(pattern) if "node_modules" not in str(f)])
        info["files"] = files[:100]  # Limit
        
        # Find exports
        exports = []
        fxmanifest = resource_dir / "fxmanifest.lua"
        if fxmanifest.exists():
            try:
                content = fxmanifest.read_text(errors='ignore')
                for line in content.split('\n'):
                    if 'exports(' in line:
                        match = re.search(r'exports\(["\']([^"\']+)["\']', line)
                        if match:
                            exports.append(match.group(1))
            except:
                pass
        info["exports"] = exports
        
        # Find database tables
        tables = []
        # Match any schema-qualified table reference: `schema.table`
        for lua_file in resource_dir.rglob("*.lua"):
            try:
                content = lua_file.read_text(errors='ignore')
                for match in re.finditer(r'`?([a-zA-Z_][a-zA-Z0-9_]*)`?\.`?([a-zA-Z_][a-zA-Z0-9_]*)`?', content):
                    entry = f"{match.group(1)}.{match.group(2)}"
                    if entry not in tables:
                        tables.append(entry)
            except:
                pass
        info["database_tables"] = tables
        
        # Find dependencies
        deps = []
        if fxmanifest.exists():
            try:
                content = fxmanifest.read_text(errors='ignore')
                for dep in ['oxmysql', 'qb-core', 'esx', 'ox_lib', 'qb-target']:
                    if dep in content:
                        deps.append(dep)
            except:
                pass
        info["dependencies"] = deps
        
        return info
    
    @log_method
    async def query(self, query_type: str, filter: Optional[str] = None) -> str:
        """Query the repository map"""
        try:
            if not self.map_file.exists():
                await self.generate()
            
            with open(self.map_file) as f:
                data = json.load(f)
            
            resources = data.get("resources", {})
            
            if query_type == "resources":
                return json.dumps({"resources": list(resources.keys())}, indent=2)
            
            elif query_type == "resource" and filter:
                return json.dumps(resources.get(filter, {}), indent=2)
            
            elif query_type == "exports" and filter:
                res = resources.get(filter, {})
                return json.dumps({"exports": res.get("exports", [])}, indent=2)
            
            elif query_type == "tables":
                result = []
                for name, info in resources.items():
                    tables = info.get("database_tables", [])
                    if tables:
                        result.append({"resource": name, "tables": tables})
                return json.dumps(result, indent=2)
            
            elif query_type == "uses-mysql":
                result = []
                for name, info in resources.items():
                    if info.get("database_tables"):
                        result.append(name)
                return json.dumps({"resources": result}, indent=2)
            
            elif query_type == "type" and filter:
                result = [name for name, info in resources.items() if info.get("type") == filter]
                return json.dumps({"resources": result}, indent=2)
            
            elif query_type == "dependencies":
                result = [{name: info.get("dependencies", [])} for name, info in resources.items() if info.get("dependencies")]
                return json.dumps(result, indent=2)
            
            else:
                return json.dumps(data, indent=2)
                
        except Exception as e:
            return json.dumps({"error": str(e)}, indent=2)
    
    @log_method
    async def show(self) -> str:
        """Show repository map summary"""
        try:
            if not self.map_file.exists():
                await self.generate()
            
            with open(self.map_file) as f:
                data = json.load(f)
            
            resources = data.get("resources", {})
            nui_count = sum(1 for r in resources.values() if r.get("type") == "nui")
            mysql_count = sum(1 for r in resources.values() if r.get("database_tables"))
            
            lines = [
                "Repository Map Summary",
                "======================",
                f"Total resources: {len(resources)}",
                f"NUI resources: {nui_count}",
                f"Lua-only resources: {len(resources) - nui_count}",
                f"Using MySQL: {mysql_count}",
                "",
                "Resources (first 30):",
            ]
            
            for name in sorted(resources.keys())[:30]:
                r = resources[name]
                rtype = r.get("type", "unknown")
                tables = len(r.get("database_tables", []))
                tables_str = f" [{tables} tables]" if tables else ""
                lines.append(f"  - {name} ({rtype}){tables_str}")
            
            if len(resources) > 30:
                lines.append(f"  ... and {len(resources) - 30} more")
            
            return "\n".join(lines)
                
        except Exception as e:
            return f"Error: {str(e)}"
    
    @log_method
    async def trace(self, from_resource: str, to_resource: str) -> str:
        """Trace connections between resources"""
        try:
            if not self.map_file.exists():
                await self.generate()
            
            with open(self.map_file) as f:
                data = json.load(f)
            
            resources = data.get("resources", {})
            from_info = resources.get(from_resource, {})
            to_info = resources.get(to_resource, {})
            
            connections = []
            
            # Check shared database tables
            from_tables = set(from_info.get("database_tables", []))
            to_tables = set(to_info.get("database_tables", []))
            shared_tables = from_tables & to_tables
            
            if shared_tables:
                connections.append({
                    "type": "database",
                    "tables": list(shared_tables)
                })
            
            return json.dumps({
                "from": from_resource,
                "to": to_resource,
                "connections": connections
            }, indent=2)
            
        except Exception as e:
            return json.dumps({"error": str(e)}, indent=2)


class PatternTool:
    """Tool for applying code patterns."""

    def __init__(self, config: Config):
        self.config = config

    def _merged_patterns(self, team_id: Optional[str] = None) -> Dict[str, Any]:
        """The built-in pattern library."""
        return dict(PATTERNS)

    @log_method
    async def list_patterns(self, team_id: Optional[str] = None) -> str:
        """List available patterns"""
        all_patterns = self._merged_patterns(team_id)
        lines = ["Available Patterns:", "=================="]
        for name, info in all_patterns.items():
            lines.append(f"  - {name}: {info['description']}")
            vars_display = ", ".join(info['variables'].keys()) if info.get('variables') else "none"
            lines.append(f"    Variables: {vars_display}")
        return "\n".join(lines)

    @log_method
    async def show_pattern(self, pattern_name: str, team_id: Optional[str] = None) -> str:
        """Show pattern details"""
        all_patterns = self._merged_patterns(team_id)
        pattern = all_patterns.get(pattern_name)
        if not pattern:
            return f"Pattern not found: {pattern_name}"

        lines = [
            f"Pattern: {pattern_name}",
            f"Description: {pattern['description']}",
            "",
            "Variables:"
        ]

        for var_name, var_info in (pattern.get('variables') or {}).items():
            if isinstance(var_info, dict):
                required = "required" if var_info.get('required') else f"default: {var_info.get('default', 'none')}"
                desc = var_info.get('description', '')
                lines.append(f"  - {var_name}: {desc} ({required})")
            else:
                lines.append(f"  - {var_name}: {var_info}")

        lines.extend([
            "",
            "Files to create:",
        ])
        for filename in (pattern.get('files') or {}).keys():
            lines.append(f"  - {filename}")

        return "\n".join(lines)

    @log_method
    async def apply_pattern(self, pattern_name: str, variables: Dict, output_dir: Optional[str] = None, dry_run: bool = False, team_id: Optional[str] = None) -> str:
        """Apply a pattern"""
        all_patterns = self._merged_patterns(team_id)
        pattern = all_patterns.get(pattern_name)
        if not pattern:
            return json.dumps({"error": f"Pattern not found: {pattern_name}"}, indent=2)
        
        # Validate required variables (var_info may be a dict or a plain string default)
        for var_name, var_info in (pattern.get('variables') or {}).items():
            if isinstance(var_info, dict):
                if var_info.get('required') and not variables.get(var_name):
                    return json.dumps({"error": f"Missing required variable: {var_name}"}, indent=2)
                if not variables.get(var_name) and var_info.get('default'):
                    variables[var_name] = var_info['default']
            elif not variables.get(var_name):
                # Plain string = default value for custom patterns
                variables[var_name] = str(var_info)
        
        # Determine output directory / mode.
        #   - explicit output_dir  -> persist to disk (must stay inside project root)
        #   - no output_dir        -> generate into a throwaway temp dir and RETURN
        #                             the file contents (server disk is shared, so we
        #                             never persist generated files there)
        persist_to_disk = bool(output_dir)
        name = variables.get("name", variables.get("resource_name", "unnamed"))

        if persist_to_disk:
            out_dir = Path(output_dir).resolve()
            base_guard = out_dir  # every file must stay under the chosen output_dir
            # Reject paths that escape the project root
            try:
                out_dir.relative_to(self.config.project_root.resolve())
            except ValueError:
                return json.dumps({
                    "error": "output_dir must be inside the project root",
                    "project_root": str(self.config.project_root),
                }, indent=2)
        else:
            tmp_root = Path(tempfile.mkdtemp(prefix="fiveclaw_pattern_"))
            out_dir = (tmp_root / name)
            base_guard = tmp_root  # files must stay under the temp root

        if dry_run:
            if not persist_to_disk:
                shutil.rmtree(tmp_root, ignore_errors=True)
            return json.dumps({
                "dry_run": True,
                "would_create": str(out_dir) if persist_to_disk else f"<generated:{name}>",
                "files": list(pattern["files"].keys()),
                "variables": variables
            }, indent=2)

        try:
            created = []
            generated_files: Dict[str, str] = {}
            resolved_base = base_guard.resolve()

            for filename, template in pattern["files"].items():
                # Apply template variables
                content = apply_template_variables(template, variables)

                # Handle filenames with variables (user-controlled -> untrusted)
                actual_filename = apply_template_variables(filename, variables)

                file_path = (out_dir / actual_filename)
                # ── Path-traversal guard ─────────────────────────────────────
                # The resolved destination MUST stay within base_guard. Rejects
                # "../", absolute paths, and symlink-style escapes from templated
                # names/filenames sourced from user `variables`.
                try:
                    file_path.resolve().relative_to(resolved_base)
                except ValueError:
                    if not persist_to_disk:
                        shutil.rmtree(tmp_root, ignore_errors=True)
                    return json.dumps({
                        "error": "Refusing to write outside the output directory",
                        "offending_path": actual_filename,
                    }, indent=2)

                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(content)

                if persist_to_disk:
                    try:
                        created.append(str(file_path.relative_to(self.config.project_root)))
                    except ValueError:
                        created.append(str(file_path))
                else:
                    # Return the generated content keyed by its resource-relative path.
                    try:
                        rel_key = str(file_path.relative_to(tmp_root))
                    except ValueError:
                        rel_key = actual_filename
                    generated_files[rel_key] = content
                    created.append(rel_key)

            if persist_to_disk:
                return json.dumps({
                    "success": True,
                    "created": created,
                    "output_dir": str(out_dir)
                }, indent=2)

            # Non-persist mode: hand the caller the generated files to write
            # locally; nothing is left on the shared server disk.
            result = json.dumps({
                "success": True,
                "generated": True,
                "note": "Files were generated in-memory and returned below; nothing was "
                        "written to the shared server disk. Write these into your own "
                        "resources dir, or pass output_dir to persist server-side.",
                "resource_name": name,
                "created": created,
                "files": generated_files,
            }, indent=2)
            shutil.rmtree(tmp_root, ignore_errors=True)
            return result

        except Exception as e:
            if not persist_to_disk:
                shutil.rmtree(tmp_root, ignore_errors=True)
            return json.dumps({"error": str(e)}, indent=2)


class ValidationTool:
    """Tool for code validation."""
    
    def __init__(self, config: Config):
        self.config = config

    @log_method
    async def syntax_check(self, file_path: str) -> str:
        """Check Lua syntax — luac when available, else lupa/luaparser (cross-platform)."""
        try:
            if not file_path.startswith("/"):
                file_path = str(self.config.project_root / file_path)

            path = Path(file_path)
            if not path.exists():
                return json.dumps({"error": f"File not found: {file_path}"}, indent=2)

            valid, error = _lua_syntax_check(path)
            return json.dumps({
                "valid": valid,
                "file": file_path,
                "error": error,
            }, indent=2)

        except Exception as e:
            return json.dumps({"error": str(e)}, indent=2)
    
    @log_method
    async def validate_resource(self, resource_name: str, files: Optional[dict] = None) -> str:
        """Validate a resource

        Args:
            resource_name: Name of the resource to validate
            files: Optional dict of {relative_path: content} from fiveclaw-agent
        """
        try:
            with _files_ctx(self.config, files) as resources_dir:
                resource_dir = _resolve_resource_dir(resources_dir, resource_name)
                if resource_dir is None:
                    return json.dumps({"error": f"Resource not found: {resource_name}"}, indent=2)
                return self._validate_resource_inner(resource_name, resource_dir)
        except Exception as e:
            return json.dumps({"error": str(e)}, indent=2)

    def _validate_resource_inner(self, resource_name: str, resource_dir: Path) -> str:
        checks = []
        all_valid = True

        # Check fxmanifest
        has_manifest = (resource_dir / "fxmanifest.lua").exists()
        checks.append({"name": "fxmanifest.lua exists", "valid": has_manifest})
        if not has_manifest:
            all_valid = False

        # Check Lua files — cross-platform (luac if present, else the bundled
        # lupa/luaparser), so syntax is validated even on hosts without luac.
        lua_files = list(resource_dir.rglob("*.lua"))
        for lua_file in lua_files[:20]:
            valid, _err = _lua_syntax_check(lua_file)
            if not valid:
                all_valid = False
            checks.append({
                "file": str(lua_file.relative_to(resource_dir)),
                "valid": valid
            })

        # Check for MySQL usage (informational only — no hardcoded prefix assumed)
        db_name = self.config.mysql.get("database", "")
        for lua_file in lua_files:
            try:
                content = lua_file.read_text(errors='ignore')
                if 'MySQL' in content or 'mysql' in content:
                    if db_name and db_name + '.' in content:
                        checks.append({
                            "file": str(lua_file.relative_to(resource_dir)),
                            "valid": True,
                            "note": f"MySQL queries use '{db_name}' prefix"
                        })
                    break
            except:
                pass

        return json.dumps({
            "resource": resource_name,
            "valid": all_valid,
            "checks": checks
        }, indent=2)

    @log_method
    async def mysql_query(self, query: str, database: str = "default") -> str:
        """Execute MySQL query using configured credentials

        Args:
            query: SQL query to execute
            database: Database alias - 'default' for the primary DB, or a named
                      key from MYSQL_EXTRA_DBS (e.g. 'qbcore').
        """
        try:
            # Check if MySQL is configured
            if not self.config.has_mysql(database):
                return json.dumps({
                    "error": f"MySQL not configured for '{database}'.",
                    "setup": "Set MYSQL_HOST, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE in your .env or on the FiveClaw dashboard.",
                    "available": ["default"] + list(self.config.extra_databases.keys()),
                }, indent=2)

            # Check if mysql client exists
            result = subprocess.run(["which", "mysql"], capture_output=True)
            if result.returncode != 0:
                return json.dumps({"error": "MySQL client not installed"}, indent=2)

            # Get database config
            db_config = self.config.get_db(database)

            # Build command — password passed via env to avoid process-list exposure
            cmd = [
                "mysql",
                "-h", db_config["host"],
                "-P", str(db_config["port"]),
                "-u", db_config["user"],
                "--password=" + db_config["password"],   # still visible in /proc on Linux;
                # safer: use a ~/.my.cnf or MYSQL_PWD env var below
                "-N", "-e", query, db_config["database"]
            ]

            env = os.environ.copy()
            env["MYSQL_PWD"] = db_config["password"]
            cmd = [
                "mysql",
                "-h", db_config["host"],
                "-P", str(db_config["port"]),
                "-u", db_config["user"],
                "-N", "-e", query, db_config["database"]
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=env,
            )
            
            if result.returncode != 0:
                return json.dumps({"error": result.stderr}, indent=2)
            
            # Parse results
            lines = result.stdout.strip().split("\n") if result.stdout else []
            rows = []
            for line in lines:
                if line.strip():
                    rows.append(line.split("\t"))
            
            return json.dumps({
                "success": True,
                "rows": rows,
                "count": len(rows),
                "database": db_config["database"]
            }, indent=2)
            
        except Exception as e:
            return json.dumps({"error": str(e)}, indent=2)
    
    @log_method
    async def server_status(self) -> str:
        """Check server status via admin panel API"""
        try:
            import urllib.request
            import urllib.error
            
            # Use admin panel API endpoint (not info.json which returns HTML)
            url = f"{self.config.admin_url}/api/server/status"
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    data = json.loads(response.read().decode('utf-8'))
                    return json.dumps({
                        "running": data.get("online", False),
                        "status": data.get("statusText", "unknown"),
                        "players": data.get("players", {}).get("current", 0),
                        "max_players": data.get("players", {}).get("max", 0),
                        "raw": data
                    }, indent=2)
            except urllib.error.URLError as e:
                return json.dumps({
                    "running": False,
                    "error": f"Cannot connect to admin panel: {str(e)}"
                }, indent=2)
                
        except Exception as e:
            return json.dumps({"error": str(e)}, indent=2)
    
    @log_method
    async def search(self, pattern: str, path: Optional[str] = None) -> str:
        """Search code using grep with extended regex support"""
        try:
            search_dir = self.config.project_root
            if path:
                search_dir = self.config.project_root / path
            
            if not search_dir.exists():
                return json.dumps({"error": f"Path not found: {search_dir}"}, indent=2)
            
            # Build grep command with excludes for common large directories
            cmd = [
                "grep", "-r", "-E", "-n", "-i", "-I",
                "--include=*.lua", "--include=*.js", "--include=*.jsx", 
                "--include=*.ts", "--include=*.tsx",
                "--exclude-dir=node_modules",
                "--exclude-dir=.git",
                "--exclude-dir=dist",
                "--exclude-dir=build",
                "--exclude-dir=.vscode",
                "--exclude-dir=__pycache__",
                pattern, str(search_dir)
            ]
            
            # Run with timeout to prevent hanging on large searches
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10  # 10 second timeout
            )
            
            lines = result.stdout.strip().split("\n") if result.stdout else []
            matches = []
            for line in lines[:50]:  # Limit to 50 matches
                if ":" in line:
                    parts = line.split(":", 2)
                    if len(parts) >= 2:
                        file_path = parts[0]
                        line_num = parts[1]
                        content = parts[2] if len(parts) > 2 else ""
                        # Make path relative to project root
                        try:
                            rel_path = str(Path(file_path).relative_to(self.config.project_root))
                        except:
                            rel_path = file_path
                        matches.append({
                            "file": rel_path,
                            "line": line_num,
                            "content": content[:200]  # Limit content length
                        })
            
            return json.dumps({"count": len(matches), "matches": matches}, indent=2)
            
        except subprocess.TimeoutExpired:
            return json.dumps({"error": "Search timeout - pattern too broad or too many files. Try a more specific path or pattern."}, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)}, indent=2)
    
    @log_method
    async def file_info(self, file_path: str) -> str:
        """Get file information"""
        try:
            if not file_path.startswith("/"):
                file_path = str(self.config.project_root / file_path)
            
            path = Path(file_path)
            if not path.exists():
                return json.dumps({"error": f"File not found: {file_path}"}, indent=2)
            
            # Count lines
            try:
                with open(path) as f:
                    lines = len(f.readlines())
            except:
                lines = 0
            
            stat = path.stat()
            
            return json.dumps({
                "file": str(path.relative_to(self.config.project_root)),
                "lines": lines,
                "size_bytes": stat.st_size,
                "size_kb": round(stat.st_size / 1024, 2),
                "modified": stat.st_mtime
            }, indent=2)
            
        except Exception as e:
            return json.dumps({"error": str(e)}, indent=2)
    
    @log_method
    async def resource_control(self, action: str, resource_name: str) -> str:
        """Control a FiveM resource (restart, start, stop) via admin panel API
        
        Args:
            action: 'restart', 'start', 'stop', or 'refresh'
            resource_name: Name of the resource to control
        """
        try:
            # Map actions to FiveM console commands
            action_map = {
                "restart": "restart",
                "start": "ensure",
                "stop": "stop",
                "refresh": "refresh"
            }
            
            if action not in action_map:
                return json.dumps({
                    "error": f"Invalid action: {action}. Use: restart, start, stop, refresh"
                }, indent=2)
            
            command = f"{action_map[action]} {resource_name}"
            
            # Use admin panel API endpoint
            import urllib.request
            import urllib.error
            
            url = f"{self.config.admin_url}/api/server-control/command"
            data = json.dumps({"command": command}).encode('utf-8')
            
            req = urllib.request.Request(
                url,
                data=data,
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            
            try:
                with urllib.request.urlopen(req, timeout=10) as response:
                    result = json.loads(response.read().decode('utf-8'))
                    return json.dumps({
                        "success": True,
                        "action": action,
                        "command": command,
                        "resource": resource_name,
                        "api_response": result
                    }, indent=2)
            except urllib.error.HTTPError as e:
                return json.dumps({
                    "error": f"Admin API error: {e.code} - {e.reason}",
                    "url": url
                }, indent=2)
            except urllib.error.URLError as e:
                return json.dumps({
                    "error": f"Cannot connect to admin panel: {e.reason}",
                    "url": url,
                    "hint": "Is the admin panel running on localhost:30121?"
                }, indent=2)
            
        except Exception as e:
            return json.dumps({"error": str(e)}, indent=2)
    
    @log_method
    async def server_console(self, command: str) -> str:
        """Send a raw command to the FiveM server console via admin panel API
        
        Args:
            command: Raw command to send (e.g., 'sv_maxclients 32')
        """
        try:
            # Use admin panel API endpoint
            import urllib.request
            import urllib.error
            
            url = f"{self.config.admin_url}/api/server-control/command"
            data = json.dumps({"command": command}).encode('utf-8')
            
            req = urllib.request.Request(
                url,
                data=data,
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            
            try:
                with urllib.request.urlopen(req, timeout=10) as response:
                    result = json.loads(response.read().decode('utf-8'))
                    return json.dumps({
                        "success": True,
                        "command": command,
                        "api_response": result
                    }, indent=2)
            except urllib.error.HTTPError as e:
                return json.dumps({
                    "error": f"Admin API error: {e.code} - {e.reason}",
                    "url": url
                }, indent=2)
            except urllib.error.URLError as e:
                return json.dumps({
                    "error": f"Cannot connect to admin panel: {e.reason}",
                    "url": url,
                    "hint": "Is the admin panel running on localhost:30121?"
                }, indent=2)
            
        except Exception as e:
            return json.dumps({"error": str(e)}, indent=2)
    
    # =============================================================================
    # BATCH OPERATIONS
    # =============================================================================
    
    @log_method
    async def batch_resource_control(self, resources: List[str], action: str) -> str:
        """Control multiple resources at once (batch restart/stop/start)
        
        Args:
            resources: List of resource names to control
            action: 'restart', 'start', 'stop', or 'refresh'
        """
        try:
            results = []
            for resource in resources:
                result = await self.resource_control(action, resource)
                results.append({
                    "resource": resource,
                    "result": json.loads(result)
                })
            
            success_count = sum(1 for r in results if r["result"].get("success"))
            
            return json.dumps({
                "action": action,
                "total": len(resources),
                "successful": success_count,
                "failed": len(resources) - success_count,
                "results": results
            }, indent=2)
            
        except Exception as e:
            return json.dumps({"error": str(e)}, indent=2)
    
    # =============================================================================
    # CODE INTELLIGENCE
    # =============================================================================
    
    @log_method
    async def find_exports(self, export_name: Optional[str] = None, resource: Optional[str] = None, files: Optional[dict] = None) -> str:
        """Find exports across resources - where functions are defined"""
        with _files_ctx(self.config, files) as resources_dir:
            if resource:
                search_dir = _resolve_resource_dir(resources_dir, resource)
                if search_dir is None:
                    return json.dumps({"error": f"Resource not found: {resource}"}, indent=2)
            else:
                search_dir = resources_dir
            if not search_dir.exists():
                return json.dumps({"error": f"Directory not found: {search_dir}"}, indent=2)
            try:
                exports_found = []
                for lua_file in search_dir.rglob("*.lua"):
                    try:
                        content = lua_file.read_text(errors='ignore')
                        lines = content.split('\n')
                        for line_num, line in enumerate(lines, 1):
                            match = re.search(r"exports\(['\"]([^'\"]+)['\"]", line)
                            if match:
                                found_export = match.group(1)
                                if not export_name or export_name.lower() in found_export.lower():
                                    exports_found.append({
                                        "export": found_export,
                                        "file": str(lua_file.relative_to(resources_dir)) if lua_file.is_relative_to(resources_dir) else str(lua_file),
                                        "line": line_num,
                                        "context": line.strip()[:100]
                                    })
                    except:
                        continue
                for fxmanifest in search_dir.rglob("fxmanifest.lua"):
                    try:
                        content = fxmanifest.read_text(errors='ignore')
                        for match in re.finditer(r'exports\s*{([^}]+)}', content, re.DOTALL):
                            for export_match in re.finditer(r'["\']([^"\']+)["\']', match.group(1)):
                                found_export = export_match.group(1)
                                if not export_name or export_name.lower() in found_export.lower():
                                    exports_found.append({
                                        "export": found_export,
                                        "file": str(fxmanifest.relative_to(resources_dir)) if fxmanifest.is_relative_to(resources_dir) else str(fxmanifest),
                                        "line": 1,
                                        "context": f"exports {{ '{found_export}' }}"
                                    })
                    except:
                        continue
                return json.dumps({"export_searched": export_name or "*", "found_count": len(exports_found), "exports": exports_found}, indent=2)
            except Exception as e:
                return json.dumps({"error": str(e)}, indent=2)
    
    @log_method
    async def find_event_handlers(self, event_name: Optional[str] = None, resource: Optional[str] = None, files: Optional[dict] = None) -> str:
        """Find event handlers (RegisterNetEvent, AddEventHandler)"""
        with _files_ctx(self.config, files) as resources_dir:
            if resource:
                search_dir = _resolve_resource_dir(resources_dir, resource)
                if search_dir is None:
                    return json.dumps({"error": f"Resource not found: {resource}"}, indent=2)
            else:
                search_dir = resources_dir
            try:
                handlers_found = []
                for lua_file in search_dir.rglob("*.lua"):
                    try:
                        content = lua_file.read_text(errors='ignore')
                        lines = content.split('\n')
                        for line_num, line in enumerate(lines, 1):
                            match = re.search(r"(RegisterNetEvent|AddEventHandler)\(['\"]([^'\"]+)['\"]", line)
                            if match:
                                found_event = match.group(2)
                                if not event_name or event_name.lower() in found_event.lower():
                                    handlers_found.append({
                                        "event": found_event,
                                        "handler_type": match.group(1),
                                        "file": str(lua_file.relative_to(resources_dir)) if lua_file.is_relative_to(resources_dir) else str(lua_file),
                                        "line": line_num,
                                        "context": line.strip()[:100]
                                    })
                    except:
                        continue
                return json.dumps({"event_searched": event_name or "*", "found_count": len(handlers_found), "handlers": handlers_found}, indent=2)
            except Exception as e:
                return json.dumps({"error": str(e)}, indent=2)
    
    @log_method
    async def find_triggers(self, pattern: Optional[str] = None, resource: Optional[str] = None, files: Optional[dict] = None) -> str:
        """Find event triggers (TriggerServerEvent, TriggerClientEvent, TriggerEvent)"""
        with _files_ctx(self.config, files) as resources_dir:
            if resource:
                search_dir = _resolve_resource_dir(resources_dir, resource)
                if search_dir is None:
                    return json.dumps({"error": f"Resource not found: {resource}"}, indent=2)
            else:
                search_dir = resources_dir
            try:
                triggers_found = []
                for lua_file in search_dir.rglob("*.lua"):
                    try:
                        content = lua_file.read_text(errors='ignore')
                        lines = content.split('\n')
                        for line_num, line in enumerate(lines, 1):
                            match = re.search(r"(TriggerServerEvent|TriggerClientEvent|TriggerEvent)\(['\"]([^'\"]+)['\"]", line)
                            if match:
                                found_event = match.group(2)
                                if not pattern or pattern.lower() in found_event.lower():
                                    triggers_found.append({
                                        "event": found_event,
                                        "trigger_type": match.group(1),
                                        "file": str(lua_file.relative_to(resources_dir)) if lua_file.is_relative_to(resources_dir) else str(lua_file),
                                        "line": line_num,
                                        "context": line.strip()[:100]
                                    })
                    except:
                        continue
                return json.dumps({"pattern_searched": pattern or "*", "found_count": len(triggers_found), "triggers": triggers_found}, indent=2)
            except Exception as e:
                return json.dumps({"error": str(e)}, indent=2)
    
    # =============================================================================
    # RESOURCE HEALTH DASHBOARD
    # =============================================================================
    
    @log_method
    async def resource_health_check(self, resource_name: str, files: Optional[dict] = None) -> str:
        """Comprehensive health check for a resource

        Args:
            resource_name: Name of the resource to check
            files: Optional dict of {relative_path: content} from fiveclaw-agent
        """
        with _files_ctx(self.config, files) as resources_dir:
            resource_dir = _resolve_resource_dir(resources_dir, resource_name)
            if resource_dir is None:
                return json.dumps({"error": f"Resource not found: {resource_name}"}, indent=2)
            return await self._resource_health_check_inner(resource_name, resource_dir)

    async def _resource_health_check_inner(self, resource_name: str, resource_dir: Path) -> str:
        try:
            health = {
                "resource": resource_name,
                "checks": {},
                "issues": [],
                "stats": {}
            }
            
            # Check 1: fxmanifest exists
            fxmanifest = resource_dir / "fxmanifest.lua"
            health["checks"]["has_manifest"] = fxmanifest.exists()
            if not fxmanifest.exists():
                health["issues"].append("Missing fxmanifest.lua")
            
            # Check 2: Lua syntax — cross-platform (luac if present, else the
            # bundled lupa/luaparser), so it validates on any OS instead of skipping.
            lua_files = list(resource_dir.rglob("*.lua"))
            syntax_errors = []
            for lua_file in lua_files:
                valid, err = _lua_syntax_check(lua_file)
                if not valid:
                    syntax_errors.append({
                        "file": str(lua_file.relative_to(resource_dir)),
                        "error": (err or "syntax error")[:200]
                    })
            health["checks"]["syntax_valid"] = len(syntax_errors) == 0
            health["stats"]["lua_files"] = len(lua_files)
            health["stats"]["syntax_errors"] = len(syntax_errors)
            if syntax_errors:
                health["issues"].append(f"{len(syntax_errors)} Lua syntax errors")
                health["syntax_errors"] = syntax_errors[:5]
            
            # Check 3: NUI build status
            web_dir = resource_dir / "web"
            if not web_dir.exists():
                # Also check html/ layout
                web_dir = resource_dir / "html"
            if web_dir.exists():
                dist_dir = web_dir / "dist"
                src_dir  = web_dir / "src"
                node_modules = web_dir / "node_modules"
                vanilla_html = (web_dir / "index.html").exists()
                health["checks"]["has_nui"] = True
                if src_dir.exists() or node_modules.exists():
                    # React/Vite NUI — needs a build step
                    health["checks"]["nui_built"] = dist_dir.exists()
                    health["checks"]["node_modules"] = node_modules.exists()
                    if not dist_dir.exists():
                        health["issues"].append("NUI not built (npm run build needed)")
                else:
                    # Vanilla HTML/JS — no build step needed
                    health["checks"]["nui_built"] = vanilla_html
                    if not vanilla_html:
                        health["issues"].append("NUI web dir exists but index.html missing")
            else:
                health["checks"]["has_nui"] = False
            
            # Check 4: Find TODO/FIXME/ERROR patterns
            todos = []
            for lua_file in lua_files[:50]:  # Limit scan
                try:
                    content = lua_file.read_text(errors='ignore')
                    for match in re.finditer(r'(TODO|FIXME|XXX|HACK|BUG).*$', content, re.MULTILINE | re.IGNORECASE):
                        lines_before = content[:match.start()].count('\n') + 1
                        todos.append({
                            "file": str(lua_file.relative_to(resource_dir)),
                            "line": lines_before,
                            "type": match.group(1).upper(),
                            "text": match.group(0)[:80]
                        })
                except:
                    pass
            health["stats"]["todos_found"] = len(todos)
            if todos:
                health["todos"] = todos[:10]
            
            # Check 5: MySQL usage detection (informational)
            db_name = self.config.mysql.get("database", "")
            for lua_file in lua_files:
                try:
                    content = lua_file.read_text(errors='ignore')
                    if 'MySQL' in content or 'mysql' in content:
                        uses_db = bool(db_name and db_name + '.' in content)
                        health["checks"]["mysql_usage"] = True
                        health["checks"]["mysql_db_prefix"] = uses_db
                        break
                except:
                    pass
            
            # Check 6: Dependencies
            if fxmanifest.exists():
                try:
                    content = fxmanifest.read_text(errors='ignore')
                    deps = []
                    for dep in ['oxmysql', 'qb-core', 'esx', 'ox_lib', 'qb-target', 'ox_target', 'fc-core']:
                        if dep in content:
                            deps.append(dep)
                    health["dependencies"] = deps
                except:
                    pass

            # Check 7: Deep fxmanifest validation
            if fxmanifest.exists():
                try:
                    content = fxmanifest.read_text(errors='ignore')
                    manifest_issues = []

                    # 7a: lua54 flag
                    if "lua54" not in content:
                        manifest_issues.append("lua54 'yes' missing — resource runs in Lua 5.3 mode")
                    health["checks"]["manifest_lua54"] = "lua54" in content

                    # 7b: fx_version present
                    if "fx_version" not in content:
                        manifest_issues.append("fx_version declaration missing")

                    # 7c: ui_page file existence
                    ui_match = re.search(r"ui_page\s+['\"]([^'\"]+)['\"]", content)
                    if ui_match:
                        ui_file = resource_dir / ui_match.group(1)
                        health["checks"]["ui_page_exists"] = ui_file.exists()
                        if not ui_file.exists():
                            manifest_issues.append(f"ui_page '{ui_match.group(1)}' does not exist")

                    # 7d: listed script files actually exist
                    missing_scripts = []
                    for block_match in re.finditer(
                        r"(?:client_scripts|server_scripts|shared_scripts|files)\s*\{([^}]+)\}",
                        content, re.DOTALL
                    ):
                        for file_match in re.finditer(r"['\"]([^'\"@][^'\"]*\.(?:lua|js|html|css))['\"]", block_match.group(1)):
                            rel = file_match.group(1)
                            if not rel.startswith("@") and _declared_file_missing(resource_dir, rel):
                                missing_scripts.append(rel)

                    # Also check single-string forms: client_script 'file.lua'
                    for single_match in re.finditer(
                        r"(?:client_script|server_script|shared_script)\s+['\"]([^'\"@][^'\"]*\.lua)['\"]",
                        content
                    ):
                        rel = single_match.group(1)
                        if _declared_file_missing(resource_dir, rel):
                            missing_scripts.append(rel)

                    health["checks"]["all_scripts_exist"] = len(missing_scripts) == 0
                    if missing_scripts:
                        manifest_issues.append(f"Listed scripts not found on disk: {missing_scripts[:5]}")
                        health["missing_scripts"] = missing_scripts[:10]

                    if manifest_issues:
                        health["issues"].extend(manifest_issues)
                        health["manifest_issues"] = manifest_issues
                except Exception as e:
                    health["manifest_check_error"] = str(e)

            # Overall health
            health["healthy"] = len(health["issues"]) == 0

            return json.dumps(health, indent=2)

        except Exception as e:
            return json.dumps({"error": str(e)}, indent=2)

    # =============================================================================
    # DATABASE SCHEMA TOOLS
    # =============================================================================
    
    @log_method
    async def mysql_visualize_schema(self, database: str = "default") -> str:
        """Visualize database schema with ASCII diagram
        
        Args:
            database: Database name ('qbcore', 'trucking', or 'hz')
        """
        try:
            # Get tables
            tables_result = await self.mysql_query(
                "SHOW TABLES",
                database
            )
            tables_data = json.loads(tables_result)
            
            if "error" in tables_data:
                return tables_result
            
            tables = []
            for row in tables_data.get("rows", []):
                table_name = row[0] if row else None
                if table_name:
                    # Get columns
                    cols_result = await self.mysql_query(
                        f"DESCRIBE {table_name}",
                        database
                    )
                    cols_data = json.loads(cols_result)
                    columns = []
                    if "rows" in cols_data:
                        for col_row in cols_data["rows"]:
                            columns.append({
                                "name": col_row[0],
                                "type": col_row[1],
                                "null": col_row[2],
                                "key": col_row[3],
                                "default": col_row[4]
                            })
                    
                    # Get foreign keys
                    fk_result = await self.mysql_query(
                        f"SELECT COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
                        f"FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE "
                        f"WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = '{table_name}' "
                        f"AND REFERENCED_TABLE_NAME IS NOT NULL",
                        database
                    )
                    fk_data = json.loads(fk_result)
                    foreign_keys = []
                    if "rows" in fk_data:
                        for fk_row in fk_data["rows"]:
                            foreign_keys.append({
                                "column": fk_row[0],
                                "references_table": fk_row[1],
                                "references_column": fk_row[2]
                            })
                    
                    tables.append({
                        "name": table_name,
                        "columns": columns,
                        "foreign_keys": foreign_keys
                    })
            
            # Build ASCII diagram
            lines = [
                f"Database: {database}",
                "=" * 60,
                ""
            ]
            
            for table in tables:
                lines.append(f"┌─ {table['name']}")
                for col in table['columns']:
                    key_marker = "🔑 " if col['key'] == 'PRI' else "   "
                    null_marker = "NULL" if col['null'] == 'YES' else "NOT NULL"
                    lines.append(f"│  {key_marker}{col['name']}: {col['type']} {null_marker}")
                
                if table['foreign_keys']:
                    lines.append("│")
                    lines.append("│  Foreign Keys:")
                    for fk in table['foreign_keys']:
                        lines.append(f"│    {fk['column']} → {fk['references_table']}.{fk['references_column']}")
                
                lines.append("└─" + "─" * 40)
                lines.append("")
            
            return json.dumps({
                "database": database,
                "table_count": len(tables),
                "ascii_diagram": "\n".join(lines),
                "tables": tables
            }, indent=2)
            
        except Exception as e:
            return json.dumps({"error": str(e)}, indent=2)
    
    @log_method
    async def mysql_find_orphans(self, parent_table: str, child_table: str, 
                                  parent_column: str = "id", child_column: str = "parent_id",
                                  database: str = "default") -> str:
        """Find orphaned records (child records without parent)
        
        Args:
            parent_table: Parent table name
            child_table: Child table name
            parent_column: Parent table key column (default: id)
            child_column: Child table foreign key column (default: parent_id)
            database: Database name
        """
        try:
            query = f"""
                SELECT c.* FROM {child_table} c
                LEFT JOIN {parent_table} p ON c.{child_column} = p.{parent_column}
                WHERE p.{parent_column} IS NULL
                LIMIT 100
            """
            
            result = await self.mysql_query(query, database)
            result_data = json.loads(result)
            
            if "error" in result_data:
                return result
            
            # Also get count
            count_query = f"""
                SELECT COUNT(*) FROM {child_table} c
                LEFT JOIN {parent_table} p ON c.{child_column} = p.{parent_column}
                WHERE p.{parent_column} IS NULL
            """
            count_result = await self.mysql_query(count_query, database)
            count_data = json.loads(count_result)
            total_orphans = count_data.get("rows", [[0]])[0][0] if "rows" in count_data else 0
            
            return json.dumps({
                "parent_table": parent_table,
                "child_table": child_table,
                "total_orphans": total_orphans,
                "orphans_found": len(result_data.get("rows", [])),
                "orphans": result_data.get("rows", [])[:20],
                "query": query
            }, indent=2)
            
        except Exception as e:
            return json.dumps({"error": str(e)}, indent=2)
    
    # =============================================================================
    # NUI DEV TOOLS
    # =============================================================================
    
    @log_method
    async def nui_build_check(self, resource_name: str, files: Optional[dict] = None) -> str:
        """Check if NUI needs rebuild by comparing source vs build timestamps

        Args:
            resource_name: Name of the resource with NUI
            files: Optional dict of {relative_path: content} from fiveclaw-agent
        """
        with _files_ctx(self.config, files) as resources_dir:
            resource_dir = _resolve_resource_dir(resources_dir, resource_name)
            if resource_dir is None:
                return json.dumps({"error": f"Resource not found: {resource_name}"}, indent=2)
            return await self._nui_build_check_inner(resource_name, resource_dir, resources_dir)

    async def _nui_build_check_inner(self, resource_name: str, resource_dir: Path, resources_dir: Path) -> str:
        # Base for rendering readable relative paths without throwing on temp trees.
        rel_base = resources_dir
        def _rel(p: Path) -> str:
            try:
                return str(p.relative_to(rel_base))
            except ValueError:
                return str(p)
        try:
            web_dir = resource_dir / "web"
            dist_dir = web_dir / "dist"
            src_dir = web_dir / "src"
            
            if not web_dir.exists():
                return json.dumps({
                    "resource": resource_name,
                    "has_nui": False,
                    "message": "No web/ directory found"
                }, indent=2)
            
            result = {
                "resource": resource_name,
                "has_nui": True,
                "web_dir": _rel(web_dir),
                "needs_rebuild": False,
                "details": {}
            }
            
            # Check if dist exists
            if not dist_dir.exists():
                result["needs_rebuild"] = True
                result["reason"] = "dist/ folder missing - never built"
                return json.dumps(result, indent=2)
            
            # Find newest source file
            src_files = list(web_dir.rglob("src/**/*")) if src_dir.exists() else list(web_dir.rglob("*"))
            src_files = [f for f in src_files if f.is_file() and 'node_modules' not in str(f) and 'dist' not in str(f)]
            
            newest_src = None
            newest_src_time = 0
            for f in src_files:
                try:
                    mtime = f.stat().st_mtime
                    if mtime > newest_src_time:
                        newest_src_time = mtime
                        newest_src = f
                except:
                    pass
            
            # Find oldest dist file
            dist_files = list(dist_dir.rglob("*"))
            dist_files = [f for f in dist_files if f.is_file()]
            
            oldest_dist = None
            oldest_dist_time = float('inf')
            for f in dist_files:
                try:
                    mtime = f.stat().st_mtime
                    if mtime < oldest_dist_time:
                        oldest_dist_time = mtime
                        oldest_dist = f
                except:
                    pass
            
            if newest_src and oldest_dist:
                if newest_src_time > oldest_dist_time:
                    result["needs_rebuild"] = True
                    result["reason"] = "Source files newer than build"
                    result["details"]["newest_source"] = {
                        "file": _rel(newest_src),
                        "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(newest_src_time))
                    }
                    result["details"]["oldest_dist"] = {
                        "file": _rel(oldest_dist),
                        "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(oldest_dist_time))
                    }
                else:
                    result["reason"] = "Build is up to date"
            else:
                result["needs_rebuild"] = True
                result["reason"] = "Could not compare timestamps"
            
            result["details"]["src_file_count"] = len(src_files)
            result["details"]["dist_file_count"] = len(dist_files)
            
            return json.dumps(result, indent=2)
            
        except Exception as e:
            return json.dumps({"error": str(e)}, indent=2)
    
    # =============================================================================
    # NUI COMPLETENESS CHECKER
    # =============================================================================

    @log_method
    async def nui_completeness_check(self, resource_name: str, files: Optional[dict] = None) -> str:
        """Check NUI integration completeness: SendNUIMessage ↔ JS listeners, RegisterNUICallback ↔ fetchNui, SetNuiFocus pairs.

        Args:
            resource_name: Name of the resource to check
            files: Optional dict of {relative_path: content} from fiveclaw-agent
        """
        with _files_ctx(self.config, files) as resources_dir:
            resource_dir = _resolve_resource_dir(resources_dir, resource_name)
            if resource_dir is None:
                return json.dumps({"error": f"Resource not found: {resource_name}"}, indent=2)
            return await self._nui_completeness_check_inner(resource_name, resource_dir)

    async def _nui_completeness_check_inner(self, resource_name: str, resource_dir: Path) -> str:
        try:
            web_dir = resource_dir / "web"
            if not web_dir.exists():
                return json.dumps({"resource": resource_name, "has_nui": False, "message": "No web/ directory"}, indent=2)

            issues = []
            result: Dict[str, Any] = {"resource": resource_name, "has_nui": True, "checks": {}}

            # ── 1. Gather Lua side ──────────────────────────────────────────────
            lua_send_actions: set = set()       # actions in SendNUIMessage { type/action = '...' }
            lua_callbacks: set = set()          # RegisterNUICallback names
            focus_true_count = 0
            focus_false_count = 0

            for lua_file in resource_dir.rglob("*.lua"):
                try:
                    content = lua_file.read_text(errors="ignore")
                    # SendNUIMessage({ type = 'action' }) or { action = 'action' }
                    for m in re.finditer(r"SendNUIMessage\s*\(\s*\{[^}]*(?:type|action)\s*=\s*['\"]([^'\"]+)['\"]", content):
                        lua_send_actions.add(m.group(1))
                    # RegisterNUICallback('name', ...)
                    for m in re.finditer(r"RegisterNUICallback\s*\(\s*['\"]([^'\"]+)['\"]", content):
                        lua_callbacks.add(m.group(1))
                    # SetNuiFocus
                    focus_true_count += len(re.findall(r"SetNuiFocus\s*\(\s*true", content))
                    focus_false_count += len(re.findall(r"SetNuiFocus\s*\(\s*false", content))
                except Exception:
                    pass

            # ── 2. Gather JS/TS side ────────────────────────────────────────────
            js_listeners: set = set()           # window.addEventListener('message', ...) action handlers
            js_fetchnui_calls: set = set()      # fetchNui('name') calls

            js_files = list(web_dir.rglob("*.js")) + list(web_dir.rglob("*.ts")) + \
                       list(web_dir.rglob("*.jsx")) + list(web_dir.rglob("*.tsx"))
            js_files = [f for f in js_files if "node_modules" not in str(f) and "dist" not in str(f)]

            for js_file in js_files:
                try:
                    content = js_file.read_text(errors="ignore")
                    # Listen for NUI messages: action/type field matching
                    for m in re.finditer(r"(?:data|event\.data|msg|message)\s*\.\s*(?:type|action)\s*===?\s*['\"]([^'\"]+)['\"]", content):
                        js_listeners.add(m.group(1))
                    # useNuiEvent hook pattern: useNuiEvent('action', ...)
                    for m in re.finditer(r"useNuiEvent\s*\(\s*['\"]([^'\"]+)['\"]", content):
                        js_listeners.add(m.group(1))
                    # fetchNui('callbackName', ...)
                    for m in re.finditer(r"fetchNui\s*\(\s*['\"]([^'\"]+)['\"]", content):
                        js_fetchnui_calls.add(m.group(1))
                except Exception:
                    pass

            # ── 3. Compare ──────────────────────────────────────────────────────
            # 3a: SendNUIMessage actions not handled in JS
            unhandled_sends = lua_send_actions - js_listeners
            if unhandled_sends:
                issues.append(f"SendNUIMessage actions with no JS handler: {sorted(unhandled_sends)}")

            # 3b: JS fetchNui calls with no RegisterNUICallback
            unhandled_callbacks = js_fetchnui_calls - lua_callbacks
            if unhandled_callbacks:
                issues.append(f"fetchNui() calls with no RegisterNUICallback: {sorted(unhandled_callbacks)}")

            # 3c: SetNuiFocus(true) / SetNuiFocus(false) balance
            if focus_true_count != focus_false_count:
                issues.append(
                    f"SetNuiFocus mismatch: {focus_true_count} × true vs {focus_false_count} × false — cursor may get stuck"
                )

            result["checks"] = {
                "lua_send_actions": sorted(lua_send_actions),
                "js_listeners": sorted(js_listeners),
                "lua_callbacks": sorted(lua_callbacks),
                "js_fetchnui_calls": sorted(js_fetchnui_calls),
                "focus_open_count": focus_true_count,
                "focus_close_count": focus_false_count,
            }
            result["issues"] = issues
            result["healthy"] = len(issues) == 0

            return json.dumps(result, indent=2)

        except Exception as e:
            import traceback
            return json.dumps({"error": str(e), "traceback": traceback.format_exc()}, indent=2)

    # =============================================================================
    # PATTERN DETECTION
    # =============================================================================

    @log_method
    async def detect_anti_patterns(self, resource: Optional[str] = None, files: Optional[dict] = None) -> str:
        """Detect common anti-patterns and code smells in Lua code

        Args:
            resource: Specific resource to check (optional, checks all if omitted)
            files: Optional dict of {relative_path: content} from fiveclaw-agent
        """
        with _files_ctx(self.config, files) as resources_dir:
            search_dir = resources_dir
            if resource:
                search_dir = _resolve_resource_dir(resources_dir, resource)
                if search_dir is None:
                    return json.dumps({"error": f"Resource not found: {resource}"}, indent=2)
            return await self._detect_anti_patterns_inner(search_dir, resources_dir)

    async def _detect_anti_patterns_inner(self, search_dir: Path, resources_dir: Path) -> str:
        try:
            patterns = {
                "busy_wait": {
                    "pattern": r"while\s+true\s+do\s*\n?\s*Wait\(0\)",
                    "description": "Busy-wait loop without sleep delay (causes high CPU)",
                    "severity": "high"
                },
                "missing_local": {
                    "pattern": r"^\s*(?!--)(?!.*\blocal\b)(?!.*\bfunction\b)([a-z][a-zA-Z0-9_]*)\s*=\s*(?!=)",
                    "description": "Lowercase global variable without 'local' (PascalCase framework globals like Config/FCCore are fine)",
                    "severity": "medium",
                    "flags": 0  # case-sensitive: [a-z] must not match uppercase PascalCase globals
                },
                "print_debug": {
                    "pattern": r"print\s*\(",
                    "description": "Debug print statements left in code",
                    "severity": "low"
                },
                "hardcoded_config": {
                    "pattern": r"(price|cost|amount)\s*=\s*\d{4,}",
                    "description": "Hardcoded values that should be in config",
                    "severity": "low"
                },
                "long_wait": {
                    "pattern": r"Wait\s*\(\s*([5-9]\d{3}|\d{5,})\s*\)",
                    "description": "Very long Wait() - may cause unresponsive UI",
                    "severity": "medium"
                },
                "Citizen_wait_deprecated": {
                    "pattern": r"Citizen\.Wait",
                    "description": "Citizen.Wait is deprecated, use Wait instead",
                    "severity": "low"
                },
                "thread_in_event": {
                    "pattern": r"AddEventHandler\s*\([^)]+\)\s*,?\s*function[^\n]*\n(?:[^\n]*\n){0,10}[^\n]*CreateThread",
                    "description": "CreateThread inside event handler — leaks a thread every trigger, move to module scope",
                    "severity": "high"
                },
                "mysql_in_loop": {
                    "pattern": r"while\s+true\s+do(?:(?!end).)*MySQL\.",
                    "description": "MySQL query inside while-true loop — causes DB spam, cache results instead",
                    "severity": "high"
                },
                "expensive_perframe": {
                    "pattern": r"Wait\s*\(\s*0\s*\)(?:(?!Wait).)*(?:GetPlayerVehicle|GetEntitySpeed|GetNearbyPeds|GetNearbyVehicles|GetAllPeds|GetAllVehicles)",
                    "description": "Expensive native called every frame (Wait(0)) — add a higher-frequency Wait or cache",
                    "severity": "medium"
                }
            }
            
            findings = []
            lua_files = list(search_dir.rglob("*.lua"))
            
            for lua_file in lua_files:
                try:
                    content = lua_file.read_text(errors='ignore')
                    lines = content.split('\n')
                    
                    for pattern_name, pattern_info in patterns.items():
                        flags = pattern_info.get("flags", re.IGNORECASE)
                        for match in re.finditer(pattern_info["pattern"], content, flags):
                            line_num = content[:match.start()].count('\n') + 1
                            findings.append({
                                "pattern": pattern_name,
                                "description": pattern_info["description"],
                                "severity": pattern_info["severity"],
                                "file": str(lua_file.relative_to(resources_dir)) if lua_file.is_relative_to(resources_dir) else str(lua_file.relative_to(self.config.project_root)) if lua_file.is_relative_to(self.config.project_root) else str(lua_file),
                                "line": line_num,
                                "context": lines[line_num-1].strip()[:80] if line_num <= len(lines) else ""
                            })
                except:
                    continue
            
            # Group by severity
            by_severity = {"high": [], "medium": [], "low": []}
            for f in findings:
                by_severity[f["severity"]].append(f)
            
            return json.dumps({
                "resource_scanned": search_dir.name if search_dir != resources_dir else "all",
                "total_files": len(lua_files),
                "total_issues": len(findings),
                "by_severity": {
                    "high": len(by_severity["high"]),
                    "medium": len(by_severity["medium"]),
                    "low": len(by_severity["low"])
                },
                "findings": findings[:50]  # Limit output
            }, indent=2)

        except Exception as e:
            return json.dumps({"error": str(e)}, indent=2)

    @log_method
    async def detect_duplicate_code(self, min_lines: int = 10, resource: Optional[str] = None, files: Optional[dict] = None) -> str:
        """Detect potential duplicate/copy-pasted code blocks"""
        with _files_ctx(self.config, files) as resources_dir:
            if resource:
                search_dir = _resolve_resource_dir(resources_dir, resource)
                if search_dir is None:
                    return json.dumps({"error": f"Resource not found: {resource}"}, indent=2)
            else:
                search_dir = resources_dir
            return await self._detect_duplicate_inner(min_lines, resource, search_dir, resources_dir)

    async def _detect_duplicate_inner(self, min_lines: int, resource: Optional[str], search_dir: Path, resources_dir: Path) -> str:
        try:
            
            # Collect code chunks from all files
            chunks = []  # List of (file, start_line, content_hash, content)
            
            lua_files = list(search_dir.rglob("*.lua"))
            
            for lua_file in lua_files:
                try:
                    content = lua_file.read_text(errors='ignore')
                    lines = content.split('\n')
                    
                    # Create chunks of N lines
                    for i in range(len(lines) - min_lines + 1):
                        chunk_lines = lines[i:i + min_lines]
                        # Normalize: remove whitespace and comments
                        normalized = '\n'.join(
                            re.sub(r'--.*$', '', line).strip() 
                            for line in chunk_lines
                        )
                        # Skip if mostly empty or just braces
                        if len(normalized.strip()) < min_lines * 5:
                            continue
                        
                        import hashlib
                        content_hash = hashlib.md5(normalized.encode()).hexdigest()[:16]
                        
                        chunks.append({
                            "file": str(lua_file.relative_to(resources_dir)) if lua_file.is_relative_to(resources_dir) else str(lua_file),
                            "start_line": i + 1,
                            "hash": content_hash,
                            "content": '\n'.join(chunk_lines)[:200]
                        })
                except:
                    continue
            
            # Find duplicates
            from collections import defaultdict
            hash_groups = defaultdict(list)
            for chunk in chunks:
                hash_groups[chunk["hash"]].append(chunk)
            
            duplicates = []
            for h, group in hash_groups.items():
                if len(group) > 1:
                    # Check if they're from different files or far apart in same file
                    locations = []
                    for g in group:
                        locations.append(f"{g['file']}:{g['start_line']}")
                    
                    duplicates.append({
                        "hash": h,
                        "occurrences": len(group),
                        "locations": locations[:10],
                        "sample": group[0]["content"]
                    })
            
            # Sort by occurrence count
            duplicates.sort(key=lambda x: x["occurrences"], reverse=True)
            
            return json.dumps({
                "resource_scanned": search_dir.name if search_dir != resources_dir else "all",
                "min_lines": min_lines,
                "total_chunks": len(chunks),
                "duplicates_found": len(duplicates),
                "duplicates": duplicates[:20],
                "duplicates_truncated": len(duplicates) > 20,
            }, indent=2)
            
        except Exception as e:
            return json.dumps({"error": str(e)}, indent=2)
    
    # =============================================================================
    # DEPLOYMENT TOOLS
    # =============================================================================
    
    @log_method
    async def deploy_resource(self, resource_name: str, target: str = "production") -> str:
        """Deploy a resource to the target environment
        
        Args:
            resource_name: Resource to deploy
            target: Target environment ('production', 'txdata', or path)
        """
        try:
            source = _resolve_resource_dir(self.config.resources_dir, resource_name)
            if source is None:
                return json.dumps({"error": f"Resource not found: {resource_name}"}, indent=2)

            # SSH remote deploy — used when FIVEM_SSH_HOST is configured
            if self.config.has_ssh():
                return await self._deploy_ssh(resource_name, source, target)

            # Local deploy
            # Resolve target path: use FIVEM_REMOTE_RESOURCES_DIR if "production",
            # otherwise treat as an absolute or relative path.
            remote_res = self.config.remote_resources_dir or str(self.config.resources_dir)
            if target in ("production", "txdata"):
                target_path = Path(remote_res) / resource_name
            elif target.startswith("/"):
                target_path = Path(target) / resource_name
            else:
                target_path = self.config.project_root / target / resource_name

            # Create backup first
            import shutil
            backup_dir = self.config.project_root / "backups" / "deploy"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / f"{resource_name}_{time.strftime('%Y%m%d_%H%M%S')}"

            if target_path.exists():
                shutil.copytree(target_path, backup_path)
                backup_created = str(backup_path)
            else:
                backup_created = None

            if target_path.exists():
                shutil.rmtree(target_path)
            shutil.copytree(source, target_path)

            return json.dumps({
                "success": True,
                "resource": resource_name,
                "source": str(source),
                "target": str(target_path),
                "backup": backup_created,
                "message": f"Deployed {resource_name} to {target_path}",
                "method": "local",
            }, indent=2)

        except Exception as e:
            return json.dumps({"error": str(e)}, indent=2)

    async def _deploy_ssh(self, resource_name: str, source: "Path", target: str) -> str:
        """Deploy a resource to a remote FiveM server via SSH/SFTP."""
        try:
            import paramiko
        except ImportError:
            return json.dumps({
                "error": "paramiko not installed. Run: pip install paramiko",
            }, indent=2)

        ssh_cfg = self.config.ssh
        remote_res = self.config.remote_resources_dir
        if not remote_res:
            return json.dumps({
                "error": "FIVEM_REMOTE_RESOURCES_DIR not set. Configure it in your .env or FiveClaw dashboard.",
            }, indent=2)

        remote_target = f"{remote_res.rstrip('/')}/{resource_name}"

        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            connect_kwargs: dict = {
                "hostname": ssh_cfg["host"],
                "port":     ssh_cfg["port"],
                "username": ssh_cfg["user"],
            }
            if ssh_cfg.get("key_path"):
                connect_kwargs["key_filename"] = ssh_cfg["key_path"]

            client.connect(**connect_kwargs, timeout=15)
            sftp = client.open_sftp()

            def _upload_dir(local_dir: "Path", remote_dir: str):
                try:
                    sftp.mkdir(remote_dir)
                except OSError:
                    pass
                for item in local_dir.iterdir():
                    r = f"{remote_dir}/{item.name}"
                    if item.is_dir():
                        _upload_dir(item, r)
                    else:
                        sftp.put(str(item), r)

            # Remove existing remote dir if present
            stdin, stdout, stderr = client.exec_command(f"rm -rf '{remote_target}'")
            stdout.channel.recv_exit_status()

            _upload_dir(source, remote_target)
            sftp.close()
            client.close()

            return json.dumps({
                "success": True,
                "resource": resource_name,
                "target": remote_target,
                "host": ssh_cfg["host"],
                "method": "ssh",
                "message": f"Deployed {resource_name} to {ssh_cfg['host']}:{remote_target}",
            }, indent=2)

        except Exception as e:
            return json.dumps({"error": f"SSH deploy failed: {str(e)}"}, indent=2)
    
    @log_method
    async def backup_resource(self, resource_name: str) -> str:
        """Create a backup of a resource
        
        Args:
            resource_name: Resource to backup
        """
        try:
            source = _resolve_resource_dir(self.config.resources_dir, resource_name)
            if source is None:
                return json.dumps({"error": f"Resource not found: {resource_name}"}, indent=2)
            
            backup_dir = self.config.project_root / "backups" / "resources"
            backup_dir.mkdir(parents=True, exist_ok=True)
            
            timestamp = time.strftime('%Y%m%d_%H%M%S')
            backup_path = backup_dir / f"{resource_name}_{timestamp}"
            
            import shutil
            shutil.copytree(source, backup_path)
            
            return json.dumps({
                "success": True,
                "resource": resource_name,
                "backup_path": str(backup_path.relative_to(self.config.project_root)),
                "timestamp": timestamp
            }, indent=2)
            
        except Exception as e:
            return json.dumps({"error": str(e)}, indent=2)


class ContextTool:
    """Tool for context memory, backed by a local JSON file."""
    
    def __init__(self, config: Config):
        self.config = config
        self.context_file = config.context_dir / "knowledge.json"
        self._ensure_file()
    
    def _ensure_file(self):
        """Ensure context file exists"""
        self.config.context_dir.mkdir(parents=True, exist_ok=True)
        if not self.context_file.exists():
            with open(self.context_file, 'w') as f:
                json.dump({"facts": {}, "history": []}, f, indent=2)
    
    def _load(self) -> Dict:
        """Load context data"""
        try:
            with open(self.context_file) as f:
                data = json.load(f)
                # Migrate old 'conversations' to 'history' if needed
                if "conversations" in data and "history" not in data:
                    data["history"] = data.pop("conversations")
                # Ensure history key exists
                if "history" not in data:
                    data["history"] = []
                return data
        except:
            return {"facts": {}, "history": []}
    
    def _save(self, data: Dict):
        """Save context data"""
        with open(self.context_file, 'w') as f:
            json.dump(data, f, indent=2)
    
    @log_method
    async def remember(self, key: str, value: str, category: str = "general") -> str:
        """Store a fact"""
        try:
            data = self._load()
            import time
            data["facts"][key] = {
                "value": value,
                "category": category,
                "updated": time.time()
            }
            self._save(data)
            return json.dumps({"success": True, "key": key, "category": category}, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)}, indent=2)
    
    @log_method
    async def recall(self, key: Optional[str] = None) -> str:
        """Recall a fact"""
        try:
            data = self._load()
            if key:
                fact = data["facts"].get(key)
                if fact:
                    return json.dumps(fact, indent=2)
                return json.dumps({"error": f"Key not found: {key}"}, indent=2)
            return json.dumps(data["facts"], indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)}, indent=2)
    
    @log_method
    async def search(self, query: str) -> str:
        """Search context"""
        try:
            data = self._load()
            results = {}
            query_lower = query.lower()
            for k, v in data["facts"].items():
                if query_lower in k.lower() or query_lower in str(v).lower():
                    results[k] = v
            return json.dumps(results, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)}, indent=2)
    
    @log_method
    async def forget(self, key: str) -> str:
        """Forget a fact"""
        try:
            data = self._load()
            if key in data["facts"]:
                del data["facts"][key]
                self._save(data)
                return json.dumps({"success": True, "forgotten": key}, indent=2)
            return json.dumps({"error": f"Key not found: {key}"}, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)}, indent=2)
    
    @log_method
    async def record(self, summary: str, tags: str) -> str:
        """Record conversation"""
        try:
            data = self._load()
            import time
            data["history"].append({
                "summary": summary,
                "tags": [t.strip() for t in tags.split(",")],
                "timestamp": time.time()
            })
            self._save(data)
            return json.dumps({"success": True, "recorded": True}, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)}, indent=2)
    
    @log_method
    async def history(self, limit: int = 10) -> str:
        """Get history"""
        try:
            data = self._load()
            history = data.get("history", [])[-limit:]
            return json.dumps(history, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)}, indent=2)
    
    @log_method
    async def show(self) -> str:
        """Show all context"""
        try:
            data = self._load()
            facts = data.get("facts", {})
            history = data.get("history", [])
            
            # Group by category
            categories = {}
            for k, v in facts.items():
                cat = v.get("category", "general")
                if cat not in categories:
                    categories[cat] = []
                categories[cat].append(k)
            
            lines = [
                "Context Memory",
                "==============",
                f"Total facts: {len(facts)}",
                f"History entries: {len(history)}",
                "",
                "By Category:"
            ]
            
            for cat, keys in sorted(categories.items()):
                lines.append(f"  {cat}: {len(keys)} facts")
            
            lines.extend([
                "",
                "Recent History (last 5):"
            ])
            
            for entry in history[-5:]:
                lines.append(f"  - {entry.get('summary', 'N/A')}")
            
            return "\n".join(lines)
        except Exception as e:
            return f"Error: {str(e)}"


class ContractTool:
    """Tool for cross-resource contract validation."""

    def __init__(self, config: Config):
        self.config = config
        self.resources_dir = config.resources_dir
        # Base used to build human-readable relative file paths. Normally the
        # project root; when a bundled `files` payload is active it is swapped
        # to the temp resources_dir so relative_to() does not throw on paths
        # that live outside the project tree.
        self._rel_base = config.project_root

    @contextmanager
    def _use_files(self, files: Optional[dict]):
        """Point self.resources_dir / self._rel_base at a temp tree built from
        the caller's bundled `files`, restoring them afterwards. Relies on
        serialized (non-reentrant) calls."""
        with _files_ctx(self.config, files) as resources_dir:
            old_res, old_base = self.resources_dir, self._rel_base
            self.resources_dir = resources_dir
            # If a bundle was supplied resources_dir is a temp root; use it as
            # the relative base so paths render as "<resource>/<file>".
            self._rel_base = resources_dir if files else self.config.project_root
            try:
                yield resources_dir
            finally:
                self.resources_dir = old_res
                self._rel_base = old_base

    def _find_export_calls(self, caller_resource: str, target_resource: str, export_name: str = None) -> List[Dict]:
        """Find all places where caller_resource calls exports from target_resource"""
        calls = []
        resource_dir = _resolve_resource_dir(self.resources_dir, caller_resource)

        if resource_dir is None:
            return calls
        
        # Pattern: exports["target"]:"exportName" or exports['target']:exportName
        export_pattern = re.compile(
            r'exports\s*\[\s*["\']([^"\']+)["\']\s*\]\s*:\s*(["\'])?([a-zA-Z_][a-zA-Z0-9_]*)\2?'
        )
        
        # Pattern: exports.exportName (for same-resource calls)
        local_export_pattern = re.compile(
            r'exports\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\('
        )
        
        for lua_file in resource_dir.rglob("*.lua"):
            try:
                content = lua_file.read_text(errors='ignore')
                lines = content.split('\n')
                
                for line_num, line in enumerate(lines, 1):
                    # Check for cross-resource exports
                    for match in export_pattern.finditer(line):
                        res_name = match.group(1)
                        func_name = match.group(3)
                        
                        if res_name == target_resource:
                            if export_name and func_name != export_name:
                                continue
                            
                            # Extract parameters from the same line (if call is on same line)
                            params = self._extract_call_params(line, func_name)
                            
                            calls.append({
                                "file": self._rel_path(lua_file),
                                "line": line_num,
                                "code": line.strip(),
                                "target_resource": res_name,
                                "export_name": func_name,
                                "parameters_sent": params
                            })
                    
                    # Check for local exports (exports.funcName)
                    for match in local_export_pattern.finditer(line):
                        func_name = match.group(1)
                        
                        if caller_resource == target_resource and export_name == func_name:
                            params = self._extract_call_params(line, func_name)
                            calls.append({
                                "file": self._rel_path(lua_file),
                                "line": line_num,
                                "code": line.strip(),
                                "target_resource": caller_resource,
                                "export_name": func_name,
                                "parameters_sent": params
                            })
                            
            except Exception:
                continue
        
        return calls
    
    def _extract_call_params(self, line: str, func_name: str) -> List[str]:
        """Extract parameter names from an export call"""
        params = []
        
        # Find function call and extract the arguments
        # Pattern: exportName(arg1, arg2, ...) or exportName{key=value, ...}
        call_pattern = re.compile(
            rf'{re.escape(func_name)}\s*(?:\(|\{{)\s*(.*?)\s*(?:\)|\}})',
            re.DOTALL
        )
        
        match = call_pattern.search(line)
        if match:
            args_str = match.group(1)
            
            # Check if it's a table {key=value}
            if '{' in line[line.find(func_name):line.find(func_name)+len(func_name)+5]:
                # Table-style parameters
                for param_match in re.finditer(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*=', args_str):
                    params.append(param_match.group(1))
            else:
                # Regular function arguments - look for variable names
                # Split by comma and clean up
                args = [a.strip() for a in args_str.split(',') if a.strip()]
                for arg in args:
                    # Try to extract just the variable name (not values)
                    var_match = re.match(r'([a-zA-Z_][a-zA-Z0-9_]*)', arg)
                    if var_match:
                        params.append(var_match.group(1))
                    else:
                        params.append(arg[:30])  # Truncate long expressions
        
        return params

    def _rel_path(self, path: Path) -> str:
        """Render a file path relative to the current base, tolerating paths
        that live outside it (e.g. temp bundle trees)."""
        try:
            return str(path.relative_to(self._rel_base))
        except ValueError:
            try:
                return str(path.relative_to(self.resources_dir))
            except ValueError:
                return str(path)

    def _find_export_definition(self, resource: str, export_name: str) -> Dict:
        """Find where an export is defined and extract its expected parameters"""
        resource_dir = _resolve_resource_dir(self.resources_dir, resource)

        if resource_dir is None:
            return None
        
        # Patterns for export definitions:
        # exports('name', function(...)
        # exports.name = function(...)
        # exports("name", function(...)
        
        patterns = [
            # exports('name', function(args)
            re.compile(
                rf"exports\s*\(\s*['\"]\s*{re.escape(export_name)}\s*['\"]\s*,\s*function\s*\(([^)]*)\)",
                re.IGNORECASE
            ),
            # exports.name = function(args)
            re.compile(
                rf"exports\.{re.escape(export_name)}\s*=\s*function\s*\(([^)]*)\)",
                re.IGNORECASE
            ),
            # local function export_name (common pattern)
            re.compile(
                rf"local\s+function\s+{re.escape(export_name)}\s*\(([^)]*)\)",
                re.IGNORECASE
            ),
            # function export_name (global)
            re.compile(
                rf"^\s*function\s+{re.escape(export_name)}\s*\(([^)]*)\)",
                re.IGNORECASE | re.MULTILINE
            ),
        ]
        
        for lua_file in resource_dir.rglob("*.lua"):
            try:
                content = lua_file.read_text(errors='ignore')
                lines = content.split('\n')
                
                for line_num, line in enumerate(lines, 1):
                    for pattern in patterns:
                        match = pattern.search(line)
                        if match:
                            params_str = match.group(1) if match.groups() else ""
                            params = [p.strip() for p in params_str.split(',') if p.strip()]
                            
                            # Find parameter documentation in comments above
                            doc_params = self._extract_param_docs(lines, line_num)
                            
                            return {
                                "file": self._rel_path(lua_file),
                                "line": line_num,
                                "code": line.strip(),
                                "parameters_expected": params,
                                "documentation": doc_params,
                                "resource": resource
                            }
                            
            except Exception:
                continue
        
        return None
    
    def _extract_param_docs(self, lines: List[str], definition_line: int) -> Dict:
        """Extract parameter documentation from comments above the function"""
        docs = {}
        
        # Look at up to 10 lines before the definition
        start = max(0, definition_line - 10)
        
        for i in range(start, definition_line):
            line = lines[i]
            # Pattern: --- @param name type description
            # Pattern: -- @param name type description
            match = re.search(r'--\s*-?\s*@param\s+(\w+)\s+(\w+)\s*(.*)', line)
            if match:
                param_name = match.group(1)
                param_type = match.group(2)
                description = match.group(3)
                docs[param_name] = {
                    "type": param_type,
                    "description": description
                }
        
        return docs
    
    def _compare_parameters(self, sent: List[str], expected: List[str]) -> Dict:
        """Compare sent parameters with expected parameters"""
        mismatches = []
        
        # Simple name matching (fuzzy)
        for i, sent_param in enumerate(sent):
            if i < len(expected):
                expected_param = expected[i]
                # Check for obvious mismatches
                # e.g., pickup vs pickupName, trailerPlate vs trailerNetId
                
                # Normalize: remove common suffixes
                sent_norm = re.sub(r'(name|id|plate|coords?)$', '', sent_param.lower())
                exp_norm = re.sub(r'(name|id|plate|coords?)$', '', expected_param.lower())
                
                if sent_norm != exp_norm and sent_param.lower() != expected_param.lower():
                    # Check if they share a common root
                    if len(sent_norm) > 3 and len(exp_norm) > 3:
                        if sent_norm[:4] == exp_norm[:4] or sent_norm[-4:] == exp_norm[-4:]:
                            mismatches.append({
                                "sent": sent_param,
                                "expected": expected_param,
                                "type": "similar_name"
                            })
                        else:
                            mismatches.append({
                                "sent": sent_param,
                                "expected": expected_param,
                                "type": "different_name"
                            })
        
        # Check for missing required params
        missing = []
        if len(expected) > len(sent):
            for i in range(len(sent), len(expected)):
                missing.append(expected[i])
        
        return {
            "mismatches": mismatches,
            "missing_required": missing,
            "sent_count": len(sent),
            "expected_count": len(expected)
        }
    
    @log_method
    async def analyze_export_usage(self, caller: str, target: str, export_name: str = None,
                                   files: Optional[dict] = None) -> str:
        """Analyze cross-resource export usage and detect contract mismatches

        Args:
            caller: Resource that calls the export (e.g., "jetsam-building-react")
            target: Resource that defines the export (e.g., "hz-trailer-documents")
            export_name: Specific export to analyze (optional, analyzes all if omitted)
            files: Optional dict of {relative_path: content} from fiveclaw-agent
        """
        with self._use_files(files):
            return await self._analyze_export_usage_inner(caller, target, export_name)

    async def _analyze_export_usage_inner(self, caller: str, target: str, export_name: str = None) -> str:
        try:
            # Find all export calls from caller to target
            calls = self._find_export_calls(caller, target, export_name)
            
            if not calls:
                return json.dumps({
                    "caller": caller,
                    "target": target,
                    "export_name": export_name,
                    "calls_found": 0,
                    "message": f"No export calls found from {caller} to {target}"
                }, indent=2)
            
            # Group by export name
            exports_analyzed = {}
            
            for call in calls:
                func_name = call["export_name"]
                
                if func_name not in exports_analyzed:
                    # Find the definition in target resource
                    definition = self._find_export_definition(target, func_name)
                    exports_analyzed[func_name] = {
                        "calls": [],
                        "definition": definition
                    }
                
                exports_analyzed[func_name]["calls"].append(call)
            
            # Analyze each export for mismatches
            results = []
            issues_found = 0
            
            for func_name, data in exports_analyzed.items():
                definition = data["definition"]
                calls = data["calls"]
                
                export_result = {
                    "export_name": func_name,
                    "calls_count": len(calls),
                    "calls": calls
                }
                
                if definition:
                    export_result["definition"] = {
                        "file": definition["file"],
                        "line": definition["line"],
                        "parameters_expected": definition["parameters_expected"]
                    }
                    
                    # Compare each call with definition
                    for call in calls:
                        comparison = self._compare_parameters(
                            call["parameters_sent"],
                            definition["parameters_expected"]
                        )
                        
                        call["comparison"] = comparison
                        
                        if comparison["mismatches"] or comparison["missing_required"]:
                            issues_found += 1
                            call["has_issue"] = True
                        else:
                            call["has_issue"] = False
                else:
                    export_result["definition"] = None
                    export_result["warning"] = f"Export '{func_name}' not found in {target}"
                    issues_found += 1
                
                results.append(export_result)
            
            return json.dumps({
                "caller": caller,
                "target": target,
                "export_name_filter": export_name,
                "exports_analyzed": len(results),
                "issues_found": issues_found,
                "exports": results,
                "recommendation": "Fix parameter name mismatches before deploying"
            }, indent=2)
            
        except Exception as e:
            import traceback
            return json.dumps({"error": str(e), "traceback": traceback.format_exc()}, indent=2)
    
    @log_method
    async def validate_export_contracts(self, resource: str, files: Optional[dict] = None) -> str:
        """Validate all export contracts for a resource

        Finds all exports called by this resource and checks for contract mismatches

        Args:
            resource: Resource to validate (e.g., "jetsam-building-react")
            files: Optional dict of {relative_path: content} from fiveclaw-agent
        """
        with self._use_files(files):
            return await self._validate_export_contracts_inner(resource)

    async def _validate_export_contracts_inner(self, resource: str) -> str:
        try:
            # Find all unique target resources this resource calls
            targets = set()
            resource_dir = _resolve_resource_dir(self.resources_dir, resource)

            if resource_dir is None:
                return json.dumps({"error": f"Resource not found: {resource}"}, indent=2)
            
            export_pattern = re.compile(
                r'exports\s*\[\s*["\']([^"\']+)["\']\s*\]\s*:\s*(["\'])?([a-zA-Z_][a-zA-Z0-9_]*)\2?'
            )
            
            for lua_file in resource_dir.rglob("*.lua"):
                try:
                    content = lua_file.read_text(errors='ignore')
                    for match in export_pattern.finditer(content):
                        target_res = match.group(1)
                        if target_res != resource:  # Exclude self-calls for now
                            targets.add(target_res)
                except Exception:
                    continue
            
            # Analyze each target
            all_results = []
            total_issues = 0
            
            for target in sorted(targets):
                # Use the inner variant so we don't re-enter _use_files (which
                # would reset resources_dir mid-validation).
                result_json = await self._analyze_export_usage_inner(resource, target)
                result = json.loads(result_json)
                
                if "exports" in result and result["exports"]:
                    all_results.append({
                        "target": target,
                        "exports_count": result["exports_analyzed"],
                        "issues": result["issues_found"],
                        "exports": result["exports"]
                    })
                    total_issues += result["issues_found"]
            
            return json.dumps({
                "resource": resource,
                "targets_validated": len(all_results),
                "total_issues": total_issues,
                "validations": all_results,
                "status": "PASS" if total_issues == 0 else "FAIL",
                "recommendation": "Fix all contract mismatches before resource restart" if total_issues > 0 else "All contracts valid"
            }, indent=2)
            
        except Exception as e:
            import traceback
            return json.dumps({"error": str(e), "traceback": traceback.format_exc()}, indent=2)
    
    def _find_variable_assignment(self, resource: str, file_path: str, line_num: int, var_name: str) -> Dict:
        """Find where a variable was assigned before being used"""
        try:
            full_path = self._rel_base / file_path
            if not full_path.exists():
                return None
            
            content = full_path.read_text(errors='ignore')
            lines = content.split('\n')
            
            # Search backwards from the usage line
            for i in range(line_num - 2, -1, -1):
                if i >= len(lines):
                    continue
                line = lines[i]
                
                # Pattern: local var_name = { or var_name = {
                assignment_pattern = re.compile(
                    rf'^\s*local\s+{re.escape(var_name)}\s*=\s*\{{?'
                )
                
                if assignment_pattern.search(line):
                    # Found assignment, extract table fields
                    fields = self._extract_table_fields(lines, i, var_name)
                    return {
                        "file": file_path,
                        "line": i + 1,
                        "code": line.strip(),
                        "fields": fields
                    }
            
            return None
            
        except Exception:
            return None
    
    def _extract_table_fields(self, lines: List[str], start_line: int, var_name: str) -> List[str]:
        """Extract field names from a table assignment"""
        fields = []
        
        # Collect lines until table closes
        table_content = ""
        brace_count = 0
        started = False
        
        for i in range(start_line, min(start_line + 50, len(lines))):
            line = lines[i]
            table_content += line + "\n"
            
            for char in line:
                if char == '{':
                    brace_count += 1
                    started = True
                elif char == '}':
                    brace_count -= 1
                    if started and brace_count == 0:
                        break
            
            if started and brace_count == 0:
                break
        
        # Extract field names: key = value or ["key"] = value
        for match in re.finditer(r'(?:^|\s|,)([a-zA-Z_][a-zA-Z0-9_]*)\s*=', table_content):
            fields.append(match.group(1))
        for match in re.finditer(r'\[\s*["\']([^"\']+)["\']\s*\]\s*=', table_content):
            fields.append(match.group(1))
        
        return fields
    
    def _resolve_caller_file(self, caller_file: str) -> str:
        """Return a path (relative to self._rel_base) to the caller file,
        searching each resource in the current base. Falls back to caller_file
        unchanged if nothing matches."""
        # 1. Already resolvable relative to the base.
        if (self._rel_base / caller_file).exists():
            return caller_file
        # 2. Try under resources_dir directly (bundle temp tree).
        try:
            base = self.resources_dir
            direct = base / caller_file
            if direct.exists():
                return str(direct.relative_to(self._rel_base)) if self._is_under(direct) else str(direct)
            # 3. Search one level down: <resource>/<caller_file>. A [category]
            #    dir has no fxmanifest itself, so descend one further level to
            #    reach the actual resources it contains.
            if base.exists():
                for res in base.iterdir():
                    if not res.is_dir():
                        continue
                    if res.name.startswith("["):
                        for sub in res.iterdir():
                            if sub.is_dir():
                                cand = sub / caller_file
                                if cand.exists():
                                    return str(cand.relative_to(self._rel_base)) if self._is_under(cand) else str(cand)
                        continue
                    cand = res / caller_file
                    if cand.exists():
                        return str(cand.relative_to(self._rel_base)) if self._is_under(cand) else str(cand)
        except Exception:
            pass
        return caller_file

    def _is_under(self, path: Path) -> bool:
        try:
            path.relative_to(self._rel_base)
            return True
        except ValueError:
            return False

    def _analyze_table_structure(self, file_path: str, line_num: int, var_name: str) -> Dict:
        """Analyze the structure of a table variable"""
        assignment = self._find_variable_assignment(
            "", file_path, line_num, var_name
        )
        
        if not assignment:
            return {
                "variable": var_name,
                "fields_found": [],
                "note": "Could not find table definition - may be built dynamically"
            }
        
        return {
            "variable": var_name,
            "defined_at": f"{assignment['file']}:{assignment['line']}",
            "fields_found": assignment["fields"],
            "field_count": len(assignment["fields"]),
            "code_preview": assignment["code"][:100]
        }
    
    @log_method
    async def analyze_data_structure(self, caller_file: str, caller_line: int,
                                     variable_name: str, target_resource: str,
                                     target_export: str, param_index: int = 2,
                                     files: Optional[dict] = None) -> str:
        """Analyze table structure passed to an export vs what's expected

        Deep field validation for table parameters - catches bugs like:
        - trailerData.pickup vs loadData.pickupName
        - Missing required fields (payment, trailerNetId)

        Args:
            caller_file: File where variable is used (e.g., "server/jobs.lua")
            caller_line: Line number where export is called
            variable_name: Name of table variable (e.g., "trailerData")
            target_resource: Target resource (e.g., "hz-trailer-documents")
            target_export: Export name (e.g., "SetActiveLoad")
            param_index: Which parameter is the table (default: 2)
            files: Optional dict of {relative_path: content} from fiveclaw-agent
        """
        with self._use_files(files):
            return await self._analyze_data_structure_inner(
                caller_file, caller_line, variable_name,
                target_resource, target_export, param_index
            )

    async def _analyze_data_structure_inner(self, caller_file: str, caller_line: int,
                                            variable_name: str, target_resource: str,
                                            target_export: str, param_index: int = 2) -> str:
        try:
            # Resolve the caller file within the current base. The path may be
            # given resource-relative ("server/jobs.lua"), resource-prefixed
            # ("my-res/server/jobs.lua"), or already project-relative.
            full_caller_path = self._resolve_caller_file(caller_file)
            caller_structure = self._analyze_table_structure(
                full_caller_path,
                caller_line,
                variable_name
            )
            
            # Find the target export definition to see expected fields
            definition = self._find_export_definition(target_resource, target_export)
            
            if not definition:
                return json.dumps({
                    "error": f"Export {target_export} not found in {target_resource}",
                    "caller_structure": caller_structure
                }, indent=2)
            
            # Extract expected fields from export parameter docs or function body
            expected_fields = self._infer_expected_fields(definition)
            
            # Compare structures
            sent_fields = set(caller_structure.get("fields_found", []))
            expected = set(expected_fields)
            
            missing = expected - sent_fields
            extra = sent_fields - expected
            
            # Check for name mismatches (pickup vs pickupName)
            field_mismatches = []
            for sent in sent_fields:
                for exp in expected:
                    # Check similarity
                    sent_norm = re.sub(r'(name|id|plate)$', '', sent.lower())
                    exp_norm = re.sub(r'(name|id|plate)$', '', exp.lower())
                    
                    if sent_norm == exp_norm and sent != exp:
                        field_mismatches.append({
                            "sent": sent,
                            "expected": exp,
                            "issue": "field_name_mismatch"
                        })
            
            return json.dumps({
                "analysis_type": "deep_structure_validation",
                "variable": variable_name,
                "caller": {
                    "file": caller_file,
                    "line": caller_line,
                    "structure": caller_structure
                },
                "target": {
                    "resource": target_resource,
                    "export": target_export,
                    "definition_file": definition["file"],
                    "definition_line": definition["line"],
                    "expected_fields": expected_fields
                },
                "comparison": {
                    "fields_sent": list(sent_fields),
                    "fields_expected": list(expected),
                    "missing_fields": list(missing),
                    "extra_fields": list(extra),
                    "field_name_mismatches": field_mismatches
                },
                "has_issues": len(missing) > 0 or len(field_mismatches) > 0,
                "recommendation": "Fix missing fields and field name mismatches"
            }, indent=2)
            
        except Exception as e:
            import traceback
            return json.dumps({"error": str(e), "traceback": traceback.format_exc()}, indent=2)
    
    def _extract_sql_columns(self, content: str, table_param: str) -> List[str]:
        """Extract column names from SQL INSERT/UPDATE queries and table parameter usage
        
        This method extracts:
        1. Column names from INSERT INTO table (col1, col2) clauses
        2. Column names from UPDATE table SET col1 = ? clauses  
        3. Field accesses from VALUES ({table_param}.field) patterns
        """
        columns = []
        escaped_param = re.escape(table_param)
        
        # Pattern 1: INSERT INTO table (col1, col2, col3)
        insert_pattern = re.compile(
            r'INSERT\s+(?:INTO\s+)?\w+\s*\(([\w\s,]+)\)',
            re.IGNORECASE
        )
        
        # Pattern 2: UPDATE table SET col1 = ?, col2 = ?
        update_pattern = re.compile(
            r'UPDATE\s+\w+\s+SET\s+([\w\s,=?!]+)',
            re.IGNORECASE
        )
        
        # Pattern 3: Look for {table_param}.field in the content (commonly used in VALUES)
        # This catches patterns like: VALUES (loadData.jobId, loadData.payment)
        field_access_pattern = re.compile(
            rf'{escaped_param}\.([a-zA-Z_][a-zA-Z0-9_]*)',
            re.IGNORECASE
        )
        
        # Extract from INSERT column lists
        for match in insert_pattern.finditer(content):
            cols_str = match.group(1)
            for col in cols_str.split(','):
                col = col.strip()
                if col and col not in columns:
                    columns.append(col)
        
        # Extract from UPDATE SET clauses
        for match in update_pattern.finditer(content):
            set_str = match.group(1)
            # Extract column names before =
            for col_match in re.finditer(r'(\w+)\s*=', set_str):
                col = col_match.group(1).strip()
                if col and col not in columns:
                    columns.append(col)
        
        # Extract from field accesses (e.g., loadData.jobId)
        # This is especially important when the SQL uses table fields in VALUES
        for match in field_access_pattern.finditer(content):
            field = match.group(1)
            if field and field not in columns:
                columns.append(field)
        
        return columns
    
    def _extract_field_accesses(self, content: str, table_param: str) -> List[str]:
        """Extract field accesses like loadData.fieldName from function body"""
        fields = []
        
        # Pattern: paramName.fieldName or paramName['fieldName'] or paramName["fieldName"]
        # Also handle cases like param.field or param:field (Lua allows both)
        # And handle bracket notation with variables
        escaped_param = re.escape(table_param)
        patterns = [
            # Standard dot notation: loadData.fieldName
            rf'{escaped_param}\.([a-zA-Z_][a-zA-Z0-9_]*)',
            # Bracket with string: loadData['fieldName'] or loadData["fieldName"]
            rf'{escaped_param}\[["\']([a-zA-Z_][a-zA-Z0-9_]*)["\']\]',
        ]
        
        for pattern in patterns:
            for match in re.finditer(pattern, content):
                field = match.group(1)
                if field and field not in fields:
                    fields.append(field)
        
        return fields
    
    def _infer_expected_fields(self, definition: Dict) -> List[str]:
        """Infer expected table fields from export definition
        
        Searches across the entire resource directory to find all field accesses
        of the table parameter, not just in the export definition file.
        """
        expected = []
        
        try:
            # Get the function parameter name for the table
            params = definition.get("parameters_expected", [])
            if len(params) < 2:
                return expected
                
            table_param = params[1]  # Usually 2nd param is the table
            resource_name = definition.get("resource", "")
            
            # First, check the export definition file for @param docs
            try:
                file_path = self._rel_base / definition["file"]
                content = file_path.read_text(errors='ignore')
                lines = content.split('\n')
                
                # Search for @param documentation above the export
                for i in range(definition["line"] - 10, definition["line"]):
                    if i < 0 or i >= len(lines):
                        continue
                    line = lines[i]
                    
                    # Look for @param tableParam.field type
                    match = re.search(
                        rf'@param\s+{re.escape(table_param)}\.([a-zA-Z_][a-zA-Z0-9_]*)',
                        line
                    )
                    if match:
                        field = match.group(1)
                        if field not in expected:
                            expected.append(field)
            except Exception:
                pass
            
            # Now search across the ENTIRE resource for field accesses
            # This handles cases where exports.lua is just a wrapper
            if resource_name:
                resource_dir = _resolve_resource_dir(self.resources_dir, resource_name)
                if resource_dir is not None:
                    for lua_file in resource_dir.rglob("*.lua"):
                        try:
                            content = lua_file.read_text(errors='ignore')
                            
                            # Extract from SQL queries
                            sql_fields = self._extract_sql_columns(content, table_param)
                            for field in sql_fields:
                                if field not in expected:
                                    expected.append(field)
                            
                            # Extract from field accesses like loadData.fieldName
                            access_fields = self._extract_field_accesses(content, table_param)
                            for field in access_fields:
                                if field not in expected:
                                    expected.append(field)
                                    
                        except Exception:
                            continue
            else:
                # Fallback: just search the definition file
                try:
                    content = file_path.read_text(errors='ignore')
                    
                    sql_fields = self._extract_sql_columns(content, table_param)
                    for field in sql_fields:
                        if field not in expected:
                            expected.append(field)
                    
                    access_fields = self._extract_field_accesses(content, table_param)
                    for field in access_fields:
                        if field not in expected:
                            expected.append(field)
                except Exception:
                    pass
                        
        except Exception:
            pass
        
        return expected

class FlowTool:
    """Tool for tracing event and data flows across resources."""

    def __init__(self, config: Config):
        self.config = config
        self.resources_dir = config.resources_dir
        # See ContractTool for the rationale — base for relative paths, swapped
        # to the temp bundle dir when `files` are supplied.
        self._rel_base = config.project_root

    @contextmanager
    def _use_files(self, files: Optional[dict]):
        """Point self.resources_dir / self._rel_base at a temp tree built from
        the caller's bundled `files`. Relies on serialized calls."""
        with _files_ctx(self.config, files) as resources_dir:
            old_res, old_base = self.resources_dir, self._rel_base
            self.resources_dir = resources_dir
            self._rel_base = resources_dir if files else self.config.project_root
            try:
                yield resources_dir
            finally:
                self.resources_dir = old_res
                self._rel_base = old_base

    def _rel_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(self._rel_base))
        except ValueError:
            try:
                return str(path.relative_to(self.resources_dir))
            except ValueError:
                return str(path)


    def _find_event_triggers(self, event_name: str, source_resource: str = None) -> List[Dict]:
        """Find where an event is triggered"""
        triggers = []
        
        search_dirs = [self.resources_dir]
        if source_resource:
            resolved = _resolve_resource_dir(self.resources_dir, source_resource)
            search_dirs = [resolved] if resolved is not None else []

        for search_dir in search_dirs:
            if not search_dir.exists():
                continue
                
            for lua_file in search_dir.rglob("*.lua"):
                try:
                    content = lua_file.read_text(errors='ignore')
                    lines = content.split('\n')
                    
                    for line_num, line in enumerate(lines, 1):
                        # Pattern: TriggerServerEvent('name'), TriggerClientEvent('name'), TriggerEvent('name')
                        for pattern in [r"TriggerServerEvent\s*\(\s*['\"]([^'\"]+)['\"]",
                                       r"TriggerClientEvent\s*\(\s*['\"]([^'\"]+)['\"]",
                                       r"TriggerEvent\s*\(\s*['\"]([^'\"]+)['\"]"]:
                            match = re.search(pattern, line)
                            if match:
                                found_event = match.group(1)
                                if found_event == event_name or event_name in found_event:
                                    # Determine trigger type
                                    trigger_type = "server"
                                    if "TriggerClientEvent" in line:
                                        trigger_type = "client"
                                    elif "TriggerEvent" in line and "TriggerServerEvent" not in line and "TriggerClientEvent" not in line:
                                        trigger_type = "local"
                                    
                                    triggers.append({
                                        "file": self._rel_path(lua_file),
                                        "line": line_num,
                                        "code": line.strip()[:100],
                                        "event": found_event,
                                        "trigger_type": trigger_type
                                    })
                except Exception:
                    continue
        
        return triggers
    
    def _find_event_handlers(self, event_name: str, target_resource: str = None) -> List[Dict]:
        """Find where an event is handled"""
        handlers = []
        
        search_dirs = [self.resources_dir]
        if target_resource:
            resolved = _resolve_resource_dir(self.resources_dir, target_resource)
            search_dirs = [resolved] if resolved is not None else []

        for search_dir in search_dirs:
            if not search_dir.exists():
                continue
                
            for lua_file in search_dir.rglob("*.lua"):
                try:
                    content = lua_file.read_text(errors='ignore')
                    lines = content.split('\n')
                    
                    for line_num, line in enumerate(lines, 1):
                        # Pattern: RegisterNetEvent('name') or RegisterServerEvent('name')
                        for pattern in [r"RegisterNetEvent\s*\(\s*['\"]([^'\"]+)['\"]",
                                       r"RegisterServerEvent\s*\(\s*['\"]([^'\"]+)['\"]"]:
                            match = re.search(pattern, line)
                            if match:
                                found_event = match.group(1)
                                if found_event == event_name or event_name in found_event:
                                    handler_type = "server" if "RegisterServerEvent" in line else "shared"
                                    
                                    handlers.append({
                                        "file": self._rel_path(lua_file),
                                        "line": line_num,
                                        "code": line.strip()[:100],
                                        "event": found_event,
                                        "handler_type": handler_type
                                    })
                except Exception:
                    continue
        
        return handlers
    
    def _find_export_calls_in_file(self, file_path: Path) -> List[Dict]:
        """Find all export calls in a specific file"""
        calls = []
        
        try:
            content = file_path.read_text(errors='ignore')
            lines = content.split('\n')
            
            for line_num, line in enumerate(lines, 1):
                # Pattern: exports["resource"]:"exportName" or exports["resource"]:exportName
                pattern = r'exports\s*\[\s*["\']([^"\']+)["\']\s*\]\s*:\s*(["\'])?([a-zA-Z_][a-zA-Z0-9_]*)\2?'
                for match in re.finditer(pattern, line):
                    target_resource = match.group(1)
                    export_name = match.group(3)
                    
                    calls.append({
                        "file": self._rel_path(file_path),
                        "line": line_num,
                        "code": line.strip()[:100],
                        "target_resource": target_resource,
                        "export_name": export_name
                    })
        except Exception:
            pass
        
        return calls
    
    def _trace_event_flow(self, event_name: str, depth: int = 0, max_depth: int = 2, visited: set = None) -> Dict:
        """Trace event flow through resources (non-recursive for performance)"""
        if visited is None:
            visited = set()
        
        if event_name in visited:
            return None
        
        visited.add(event_name)
        
        # Find triggers (limited to first 10 for performance)
        triggers = self._find_event_triggers(event_name)[:10]
        
        # Find handlers (limited to first 10 for performance)
        handlers = self._find_event_handlers(event_name)[:10]
        
        if not triggers and not handlers:
            return None
        
        # For each handler, find what exports it calls (limited)
        handler_flows = []
        for handler in handlers[:5]:  # Limit handlers processed
            handler_file = self._rel_base / handler["file"]
            if handler_file.exists():
                export_calls = self._find_export_calls_in_file(handler_file)[:5]  # Limit exports
                
                handler_flows.append({
                    "handler": handler,
                    "export_calls": export_calls,
                    "next_flows": []  # Skip recursive tracing for performance
                })
        
        return {
            "event": event_name,
            "triggers": triggers,
            "handlers": handler_flows,
            "depth": depth
        }
    
    @log_method
    async def trace_event_flow(self, event_name: str, max_depth: int = 3,
                               files: Optional[dict] = None) -> str:
        """Trace the complete flow of an event through the system

        Shows: Event triggered by → Handled by → Exports called → Next events

        Args:
            event_name: Event to trace (e.g., "jetsam:StartJob")
            max_depth: Maximum recursion depth (default: 3)
            files: Optional dict of {relative_path: content} from fiveclaw-agent
        """
        with self._use_files(files):
            return await self._trace_event_flow_outer(event_name, max_depth)

    async def _trace_event_flow_outer(self, event_name: str, max_depth: int = 3) -> str:
        try:
            flow = self._trace_event_flow(event_name, max_depth=max_depth)
            
            if not flow:
                return json.dumps({
                    "event": event_name,
                    "error": "Event not found in codebase",
                    "suggestion": "Check event name spelling or search with find_event_handlers"
                }, indent=2)
            
            return json.dumps({
                "event": event_name,
                "max_depth": max_depth,
                "flow": flow,
                "summary": self._generate_flow_summary(flow)
            }, indent=2)
            
        except Exception as e:
            import traceback
            return json.dumps({"error": str(e), "traceback": traceback.format_exc()}, indent=2)
    
    def _generate_flow_summary(self, flow: Dict) -> List[str]:
        """Generate human-readable summary of the flow"""
        summary = []

        event = flow.get("event", "Unknown")
        triggers = flow.get("triggers", [])
        handlers = flow.get("handlers", [])

        # Trigger summary
        if triggers:
            for trigger in triggers:
                summary.append(f"📤 {event} triggered by {trigger['file']}:{trigger['line']}")

        # Handler summary
        for handler_data in handlers:
            handler = handler_data["handler"]
            summary.append(f"📥 {event} handled by {handler['file']}:{handler['line']}")

            # Export calls
            for call in handler_data.get("export_calls", []):
                summary.append(f"   └─▶ calls {call['target_resource']}:{call['export_name']}()")

            # Next flows
            for next_flow in handler_data.get("next_flows", []):
                next_summary = self._generate_flow_summary(next_flow)
                for line in next_summary:
                    summary.append(f"      {line}")

        return summary


# =============================================================================
# SECURITY SCANNER
# =============================================================================

class SecurityTool:
    """Scan FiveM resources for common server-side security vulnerabilities"""

    # Expensive / game-breaking natives that are safe on server but should
    # never be trusted when values originate from a client event payload.
    _DANGEROUS_CLIENT_VALUES = [
        "coords", "x", "y", "z", "health", "armour", "money", "cash", "bank",
        "ped", "vehicle", "netid", "networkid",
    ]

    def __init__(self, config: Config):
        self.config = config

    @log_method
    async def scan_resource(self, resource_name: str, files: Optional[dict] = None) -> str:
        """Scan a resource for security vulnerabilities.

        Checks:
        - Unvalidated `source` usage in RegisterNetEvent handlers
        - Client-supplied coordinates passed directly to server natives
        - MySQL queries built with unvalidated player data
        - Missing entity ownership checks before SetEntity* calls
        - TriggerClientEvent broadcasting to -1 (all clients) with sensitive data

        Args:
            resource_name: Name of the resource to scan
            files: Optional dict of {relative_path: content} from fiveclaw-agent
        """
        with _files_ctx(self.config, files) as resources_dir:
            resource_dir = _resolve_resource_dir(resources_dir, resource_name)
            if resource_dir is None:
                return json.dumps({"error": f"Resource not found: {resource_name}"}, indent=2)
            return await self._scan_resource_inner(resource_name, resource_dir)

    async def _scan_resource_inner(self, resource_name: str, resource_dir: Path) -> str:
        try:
            findings: List[Dict] = []

            server_files = list((resource_dir / "server").rglob("*.lua")) if (resource_dir / "server").exists() else []
            # Also pick up server-side files at root level
            server_files += [f for f in resource_dir.glob("*.lua") if "server" in f.name.lower()]

            all_lua = list(resource_dir.rglob("*.lua"))

            for lua_file in all_lua:
                try:
                    content = lua_file.read_text(errors="ignore")
                    lines = content.split("\n")
                    try:
                        rel = str(lua_file.relative_to(resource_dir.parent))
                    except ValueError:
                        rel = str(lua_file)

                    # ── Check 1: source used without GetPlayerIdentifier / GetPlayerName validation ──
                    in_net_event = False
                    net_event_name = ""
                    brace_depth = 0
                    for i, line in enumerate(lines, 1):
                        net_m = re.search(r"RegisterNetEvent\s*\(\s*['\"]([^'\"]+)['\"]", line)
                        if net_m:
                            in_net_event = True
                            net_event_name = net_m.group(1)
                            brace_depth = 0
                        if in_net_event:
                            brace_depth += line.count("{") + line.count("(") - line.count("}") - line.count(")")
                            # Look for raw `source` used in MySQL / SetEntity without validation
                            if re.search(r"\bsource\b", line) and not re.search(r"GetPlayer|IsPlayerAceAllowed|IsPlayerDead|tonumber\s*\(\s*source", line):
                                if re.search(r"MySQL\.|SetEntity|NetworkGetEntityOwner", line):
                                    findings.append({
                                        "type": "unvalidated_source",
                                        "severity": "high",
                                        "file": rel,
                                        "line": i,
                                        "code": line.strip()[:120],
                                        "description": f"'source' used in {net_event_name} without player validation before MySQL/entity call",
                                    })

                    # ── Check 2: Client-trusted coords passed to SetEntityCoords ──
                    for i, line in enumerate(lines, 1):
                        if re.search(r"SetEntityCoords|SetPedCoordsKeepVehicle", line):
                            # Check if the previous ~5 lines contain a client event parameter named coords/x/y/z
                            context = "\n".join(lines[max(0, i-6):i])
                            if re.search(r"function\s*\([^)]*\bcoords\b", context):
                                findings.append({
                                    "type": "client_trusted_coords",
                                    "severity": "critical",
                                    "file": rel,
                                    "line": i,
                                    "code": line.strip()[:120],
                                    "description": "Server teleports player to client-supplied coordinates — easy exploit for teleport hacks",
                                })

                    # ── Check 3: MySQL string interpolation (SQL injection risk) ──
                    for i, line in enumerate(lines, 1):
                        if re.search(r"MySQL\.\w+\.await\s*\(", line) or re.search(r"MySQL\.\w+\s*\(", line):
                            if re.search(r"\.\.\s*(?:source|name|identifier|citizenid|charname)", line):
                                findings.append({
                                    "type": "sql_injection_risk",
                                    "severity": "critical",
                                    "file": rel,
                                    "line": i,
                                    "code": line.strip()[:120],
                                    "description": "MySQL query built via string concatenation with player data — use parameterised queries",
                                })

                    # ── Check 4: TriggerClientEvent(-1, ...) broadcasting sensitive keys ──
                    for i, line in enumerate(lines, 1):
                        if re.search(r"TriggerClientEvent\s*\([^,]+,\s*-1\s*,", line):
                            for key in ["password", "token", "secret", "apikey", "api_key", "license"]:
                                if key in line.lower():
                                    findings.append({
                                        "type": "sensitive_broadcast",
                                        "severity": "high",
                                        "file": rel,
                                        "line": i,
                                        "code": line.strip()[:120],
                                        "description": f"Sensitive data ('{key}') broadcast to ALL clients via TriggerClientEvent(-1, ...)",
                                    })

                    # ── Check 5: Missing entity ownership check ──
                    for i, line in enumerate(lines, 1):
                        if re.search(r"SetEntityHealth|DeleteEntity|SetEntityModel", line):
                            context = "\n".join(lines[max(0, i-8):i])
                            if not re.search(r"NetworkGetEntityOwner|DoesEntityExist|NetworkIsEntityNetworkReady", context):
                                # Only flag if an entity variable came from client payload
                                if re.search(r"function\s*\([^)]*\b(?:ped|entity|vehicle|netId)\b", context):
                                    findings.append({
                                        "type": "missing_ownership_check",
                                        "severity": "medium",
                                        "file": rel,
                                        "line": i,
                                        "code": line.strip()[:120],
                                        "description": "Entity mutation without NetworkGetEntityOwner check — can be exploited to modify other players' entities",
                                    })

                except Exception:
                    continue

            by_severity = {"critical": [], "high": [], "medium": [], "low": []}
            for f in findings:
                sev = f.get("severity", "medium")
                by_severity.setdefault(sev, []).append(f)

            return json.dumps({
                "resource": resource_name,
                "total_issues": len(findings),
                "by_severity": {k: len(v) for k, v in by_severity.items()},
                "findings": findings[:60],
                "findings_truncated": len(findings) > 60,
            }, indent=2)

        except Exception as e:
            import traceback
            return json.dumps({"error": str(e), "traceback": traceback.format_exc()}, indent=2)

    @log_method
    async def scan_all(self, files: Optional[dict] = None) -> str:
        """Scan all resources for security vulnerabilities.

        Returns a summary grouped by resource and severity.
        """
        with _files_ctx(self.config, files) as resources_dir:
            return await self._scan_all_inner(resources_dir)

    async def _scan_all_inner(self, resources_dir: Path) -> str:
        try:
            if not resources_dir.exists():
                return json.dumps({"error": f"Resources directory not found: {resources_dir}"}, indent=2)

            summary: List[Dict] = []

            for item in sorted(resources_dir.iterdir()):
                # Handle categorised layouts like [local]/fc-core
                if item.is_dir() and item.name.startswith("["):
                    for sub in sorted(item.iterdir()):
                        if sub.is_dir() and (sub / "fxmanifest.lua").exists():
                            result_json = await self._scan_resource_inner(sub.name, sub)
                            result = json.loads(result_json)
                            if result.get("total_issues", 0) > 0 or "error" not in result:
                                summary.append({
                                    "resource": sub.name,
                                    "total_issues": result.get("total_issues", 0),
                                    "by_severity": result.get("by_severity", {}),
                                })
                elif item.is_dir() and (item / "fxmanifest.lua").exists():
                    result_json = await self._scan_resource_inner(item.name, item)
                    result = json.loads(result_json)
                    if result.get("total_issues", 0) > 0 or "error" not in result:
                        summary.append({
                            "resource": item.name,
                            "total_issues": result.get("total_issues", 0),
                            "by_severity": result.get("by_severity", {}),
                        })

            total_critical = sum(r["by_severity"].get("critical", 0) for r in summary)
            total_high = sum(r["by_severity"].get("high", 0) for r in summary)

            return json.dumps({
                "resources_scanned": len(summary),
                "total_critical": total_critical,
                "total_high": total_high,
                "resources": sorted(summary, key=lambda x: x["total_issues"], reverse=True),
            }, indent=2)

        except Exception as e:
            import traceback
            return json.dumps({"error": str(e), "traceback": traceback.format_exc()}, indent=2)


# =============================================================================
# DEPENDENCY GRAPH / LOAD ORDER VALIDATOR
# =============================================================================

class DependencyTool:
    """Analyse resource dependencies, validate load order and detect circular deps"""

    def __init__(self, config: Config):
        self.config = config

    def _parse_manifest_deps(self, resource_dir: Path) -> List[str]:
        """Parse dependency declarations from fxmanifest.lua"""
        deps: List[str] = []
        manifest = resource_dir / "fxmanifest.lua"
        if not manifest.exists():
            return deps
        try:
            content = manifest.read_text(errors="ignore")
            # dependency 'name' or dependencies { 'a', 'b' }
            for m in re.finditer(r"dependency\s+['\"]([^'\"]+)['\"]", content):
                deps.append(m.group(1))
            for block in re.finditer(r"dependencies\s*\{([^}]+)\}", content, re.DOTALL):
                for m in re.finditer(r"['\"]([^'\"]+)['\"]", block.group(1)):
                    deps.append(m.group(1))
        except Exception:
            pass
        return list(dict.fromkeys(deps))  # deduplicate, preserve order

    def _parse_export_deps(self, resource_dir: Path) -> List[str]:
        """Find resources depended on via exports['resource']:fn() calls"""
        deps: List[str] = []
        for lua_file in resource_dir.rglob("*.lua"):
            try:
                content = lua_file.read_text(errors="ignore")
                for m in re.finditer(r"exports\s*\[\s*['\"]([^'\"]+)['\"]\s*\]", content):
                    dep = m.group(1)
                    if dep not in deps:
                        deps.append(dep)
            except Exception:
                pass
        return deps

    def _resources_root(self) -> Path:
        """Return the root resources/ directory regardless of whether resources_dir
        points to resources/ itself or a category like resources/[local]."""
        rd = self.config.resources_dir
        # If resources_dir is a category dir like [local], step up one level
        if rd.name.startswith("[") and rd.name.endswith("]"):
            return rd.parent
        return rd

    def _discover_resources(self, resources_dir: Optional[Path] = None) -> Dict[str, Path]:
        """Return {resource_name: (path, category)} for ALL resources found across
        every [category] directory under the resources root, plus FiveM's built-in
        citizen/system_resources if present."""
        found: Dict[str, Path] = {}

        def _scan_dir(root: Path) -> None:
            if not root.exists():
                return
            for item in root.iterdir():
                if not item.is_dir():
                    continue
                if item.name.startswith("["):
                    # Category folder — recurse one level
                    for sub in item.iterdir():
                        if sub.is_dir() and (sub / "fxmanifest.lua").exists():
                            found.setdefault(sub.name, sub)
                elif (item / "fxmanifest.lua").exists():
                    found.setdefault(item.name, item)

        # 1. All categories under the resources root
        _scan_dir(resources_dir if resources_dir is not None else self._resources_root())

        # 2. FiveM built-in citizen/system_resources (txAdmin-style layout)
        for candidate in [
            self.config.project_root / "alpine" / "opt" / "cfx-server" / "citizen" / "system_resources",
            self.config.project_root / "citizen" / "system_resources",
            Path("/opt/cfx-server/citizen/system_resources"),
        ]:
            if candidate.exists():
                for item in candidate.iterdir():
                    if item.is_dir() and (item / "fxmanifest.lua").exists():
                        found.setdefault(item.name, item)
                break

        return found

    def _parse_ensure_order(self, files: Optional[dict] = None) -> List[str]:
        """Parse ensure order from server.cfg"""
        order: List[str] = []
        # If files were sent, check for server.cfg in the dict first
        if files and "server.cfg" in files:
            try:
                content = files["server.cfg"]
                for m in re.finditer(r"^\s*ensure\s+(\S+)", content, re.MULTILINE):
                    order.append(m.group(1))
                return order
            except Exception:
                pass
        # Fall back to disk
        for search in [self.config.project_root, self.config.project_root.parent]:
            cfg = search / "server.cfg"
            if cfg.exists():
                try:
                    content = cfg.read_text(errors="ignore")
                    for m in re.finditer(r"^\s*ensure\s+(\S+)", content, re.MULTILINE):
                        order.append(m.group(1))
                except Exception:
                    pass
                break
        return order

    def _detect_cycles(self, graph: Dict[str, List[str]]) -> List[List[str]]:
        """Detect cycles in dependency graph via DFS"""
        visited: set = set()
        path: List[str] = []
        path_set: set = set()
        cycles: List[List[str]] = []

        def dfs(node: str):
            if node in path_set:
                idx = path.index(node)
                cycle = path[idx:] + [node]
                cycles.append(cycle)
                return
            if node in visited:
                return
            visited.add(node)
            path.append(node)
            path_set.add(node)
            for dep in graph.get(node, []):
                dfs(dep)
            path.pop()
            path_set.discard(node)

        for node in graph:
            dfs(node)
        return cycles

    @log_method
    async def validate_load_order(self, files: Optional[dict] = None) -> str:
        """Validate server.cfg ensure order against resource dependencies."""
        with _files_ctx(self.config, files) as resources_dir:
            return await self._validate_load_order_inner(resources_dir, files)

    async def _validate_load_order_inner(self, resources_dir: Path, files: Optional[dict] = None) -> str:
        try:
            resources = self._discover_resources(resources_dir)
            ensure_order = self._parse_ensure_order(files)

            # Build full dependency graph
            graph: Dict[str, List[str]] = {}
            for name, path in resources.items():
                manifest_deps = self._parse_manifest_deps(path)
                export_deps = self._parse_export_deps(path)
                all_deps = list(dict.fromkeys(manifest_deps + export_deps))
                # Filter to only local resources
                graph[name] = [d for d in all_deps if d in resources]

            # Detect cycles
            cycles = self._detect_cycles(graph)

            # Check ensure order
            order_issues: List[Dict] = []
            ensure_set = set(ensure_order)
            # Bracket-folder ensures: `ensure [cat]` loads EVERY resource inside
            # resources/[cat]/ (FXServer expands it). Expand them so (a) an existing
            # [cat] isn't reported "missing from disk" and (b) a dependency satisfied by
            # a bracket-folder ensure isn't falsely flagged as absent from the ensure list.
            bracket_ensures = {e for e in ensure_order if e.startswith("[") and e.endswith("]")}
            covered_by_bracket: set = set()
            for cat in bracket_ensures:
                cat_dir = resources_dir / cat
                if cat_dir.is_dir():
                    cat_res = cat_dir.resolve()
                    for rname, rpath in resources.items():
                        try:
                            Path(rpath).resolve().relative_to(cat_res)
                            covered_by_bracket.add(rname)
                        except (ValueError, OSError):
                            pass
            effective_ensure = ensure_set | covered_by_bracket

            for i, resource in enumerate(ensure_order):
                deps = graph.get(resource, [])
                for dep in deps:
                    if dep in effective_ensure:
                        dep_idx = ensure_order.index(dep) if dep in ensure_order else -1
                        if dep_idx > i:
                            order_issues.append({
                                "resource": resource,
                                "depends_on": dep,
                                "issue": f"'{resource}' (position {i+1}) loaded before dependency '{dep}' (position {dep_idx+1})",
                            })
                    else:
                        order_issues.append({
                            "resource": resource,
                            "depends_on": dep,
                            "issue": f"'{resource}' depends on '{dep}' but '{dep}' is not in server.cfg ensure list",
                        })

            # Classify resources in server.cfg that weren't found on disk
            # Known FiveM built-ins that ship with the server binary (not on disk as resources)
            FIVEM_BUILTINS = {
                "spawnmanager", "mapmanager", "sessionmanager", "baseevents",
                "hardcap", "chat", "monitor", "playernames", "scoreboard",
                "yarn", "webpack", "basic-gamemode",
            }
            not_on_disk = [
                r for r in ensure_order
                if r not in resources and not r.startswith("@")
                # a bracket-folder ensure that exists on disk isn't "missing" — FXServer
                # loads the whole [cat]/ folder, whose resources ARE discovered above.
                and not (r in bracket_ensures and (resources_dir / r).is_dir())
            ]
            # Split into truly missing vs. known external/builtin
            truly_missing = [r for r in not_on_disk if r not in FIVEM_BUILTINS]
            known_external = [r for r in not_on_disk if r in FIVEM_BUILTINS]

            return json.dumps({
                "resources_found": len(resources),
                "ensure_order_count": len(ensure_order),
                "circular_dependencies": [" → ".join(c) for c in cycles],
                "order_issues": order_issues[:30],
                "order_issues_truncated": len(order_issues) > 30,
                "missing_from_disk": truly_missing,
                "known_external_resources": known_external,
                "dependency_graph": {k: v for k, v in graph.items() if v},
            }, indent=2)

        except Exception as e:
            import traceback
            return json.dumps({"error": str(e), "traceback": traceback.format_exc()}, indent=2)

    @log_method
    async def show_dependency_graph(self, resource_name: Optional[str] = None, files: Optional[dict] = None) -> str:
        """Show the dependency graph for a resource or all resources."""
        with _files_ctx(self.config, files) as resources_dir:
            return await self._show_dependency_graph_inner(resource_name, resources_dir)

    async def _show_dependency_graph_inner(self, resource_name: Optional[str], resources_dir: Path) -> str:
        try:
            resources = self._discover_resources(resources_dir)

            if resource_name:
                if resource_name not in resources:
                    return json.dumps({"error": f"Resource not found: {resource_name}"}, indent=2)
                scope = {resource_name: resources[resource_name]}
            else:
                scope = resources

            graph: Dict[str, Any] = {}
            for name, path in scope.items():
                manifest_deps = self._parse_manifest_deps(path)
                export_deps = self._parse_export_deps(path)
                graph[name] = {
                    "manifest_dependencies": manifest_deps,
                    "export_dependencies": [d for d in export_deps if d != name],
                    "all_dependencies": list(dict.fromkeys(manifest_deps + export_deps)),
                }

            # ASCII tree for single resource
            if resource_name:
                info = graph[resource_name]
                lines = [f"Dependency graph: {resource_name}", "=" * 50]
                all_deps = info["all_dependencies"]
                if all_deps:
                    for dep in all_deps:
                        source = "manifest" if dep in info["manifest_dependencies"] else "exports"
                        in_local = dep in resources
                        lines.append(f"  ├─ {dep} [{source}]{'  ✓ local' if in_local else '  ⚠ not found locally'}")
                else:
                    lines.append("  (no dependencies)")
                return "\n".join(lines)

            return json.dumps(graph, indent=2)

        except Exception as e:
            import traceback
            return json.dumps({"error": str(e), "traceback": traceback.format_exc()}, indent=2)

