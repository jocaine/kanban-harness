"""Project Runner — execute start.sh and manage project processes.

Coach-Dev is required to maintain a start.sh in every project.
This module only reads start.sh — no heuristic fallback.

When a host daemon is available (KH_DAEMON_URL), delegates execution to the host.
Falls back to in-container execution when daemon is unreachable.
"""

import asyncio
import logging
import os
import re
import time

logger = logging.getLogger("kh.core.project_runner")

WORKSPACE_BASE = os.getenv("KH_WORKSPACE", os.path.expanduser("~/.kh/workspaces"))
HOST_WORKSPACE_BASE = os.getenv("KH_HOST_WORKSPACE", WORKSPACE_BASE)
DAEMON_URL = os.getenv("KH_DAEMON_URL", "http://127.0.0.1:8770")

_running: dict[int, dict] = {}
_output: dict[int, list[str]] = {}
_OUTPUT_MAX_LINES = 500

# --- Daemon Client ---

_daemon_cache: dict[str, float | bool] = {"available": False, "checked_at": 0.0}
_DAEMON_CACHE_TTL = 30.0


async def _daemon_available() -> bool:
    now = time.time()
    if now - _daemon_cache["checked_at"] < _DAEMON_CACHE_TTL:
        return _daemon_cache["available"]

    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{DAEMON_URL}/health", timeout=aiohttp.ClientTimeout(total=2)) as resp:
                _daemon_cache["available"] = resp.status == 200
    except Exception:
        _daemon_cache["available"] = False

    _daemon_cache["checked_at"] = now
    return _daemon_cache["available"]


async def _daemon_request(method: str, path: str, json_data: dict | None = None) -> dict | None:
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            kwargs = {"timeout": aiohttp.ClientTimeout(total=10)}
            if json_data:
                kwargs["json"] = json_data
            async with session.request(method, f"{DAEMON_URL}{path}", **kwargs) as resp:
                if resp.status == 200:
                    return await resp.json()
                return {"_error": resp.status, "_body": await resp.text()}
    except Exception as e:
        logger.warning("Daemon request failed: %s %s — %s", method, path, e)
        return None


async def start_terminal(project_id: int) -> dict | None:
    """Start a PTY terminal session for a CLI project via daemon."""
    if not await _daemon_available():
        return None

    host_path = os.path.join(HOST_WORKSPACE_BASE, f"project_{project_id}")
    result = await _daemon_request("POST", "/terminal", {"workspace": host_path})
    if result and "_error" not in result:
        return {"term_id": result["id"], "pid": result.get("pid", 0), "daemon_url": DAEMON_URL}
    return None


async def _daemon_get_bytes(path: str) -> bytes | None:
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{DAEMON_URL}{path}", timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    return await resp.read()
                return None
    except Exception:
        return None


def parse_start_script(project_path: str) -> dict | None:
    """Parse start.sh header comments for PORT and PATH metadata.

    Expected format:
        #!/bin/bash
        # PORT=3000
        # PATH=/minesweeper.html
        npm install && npm start

    Returns None if start.sh doesn't exist.
    """
    script_path = os.path.join(project_path, "start.sh")
    if not os.path.isfile(script_path):
        return None

    try:
        with open(script_path) as f:
            lines = f.readlines()
    except OSError:
        return None

    port = None
    path = ""
    proj_type = "web"

    for line in lines[:10]:
        line = line.strip()
        m = re.match(r"#\s*PORT\s*=\s*(\d+)", line)
        if m:
            port = int(m.group(1))
        m = re.match(r"#\s*PATH\s*=\s*(.+)", line)
        if m:
            path = m.group(1).strip()
        m = re.match(r"#\s*TYPE\s*=\s*(\w+)", line)
        if m:
            proj_type = m.group(1)

    if proj_type == "cli":
        port = None

    return {
        "command": "bash start.sh",
        "port": port,
        "path": path,
        "type": proj_type,
    }


async def _read_output(project_id: int, proc: asyncio.subprocess.Process):
    """Background task to read stdout lines into the output buffer."""
    buf = _output.setdefault(project_id, [])
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        text = line.decode(errors="replace").rstrip("\n")
        buf.append(text)
        if len(buf) > _OUTPUT_MAX_LINES:
            del buf[0]


async def start_project(project_id: int) -> dict:
    """Start a project by executing its start.sh. Delegates to host daemon if available."""
    if project_id in _running:
        entry = _running[project_id]
        proc = entry.get("proc")
        # Local process still running
        if proc and proc.returncode is None:
            return {
                "status": "already_running",
                "pid": entry.get("pid") or proc.pid,
                "command": entry["cmd"],
                "port": entry.get("port"),
                "path": entry.get("path", ""),
                "type": entry.get("type"),
            }
        # Daemon-managed process still running
        if entry.get("daemon_id") and entry.get("daemon_running", False):
            return {
                "status": "already_running",
                "pid": entry.get("pid", 0),
                "command": entry["cmd"],
                "port": entry.get("port"),
                "path": entry.get("path", ""),
                "type": entry.get("type"),
            }
        del _running[project_id]

    project_path = os.path.join(WORKSPACE_BASE, f"project_{project_id}")
    if not os.path.isdir(project_path):
        return {"status": "error", "message": f"workspace not found: project_{project_id}"}

    from core.stable_build import get_stable_dir
    stable_dir = await get_stable_dir(project_id)
    run_path = stable_dir or project_path
    is_stable = stable_dir is not None

    info = parse_start_script(run_path)
    if not info:
        if is_stable:
            info = parse_start_script(project_path)
        if not info:
            return {"status": "error", "message": "no_start_sh"}

    cmd = info["command"]
    port = info["port"]
    path = info["path"]
    proj_type = info["type"]

    logger.info("[RUNNER] start project_%d: %s (type=%s, port=%s, stable=%s)", project_id, cmd, proj_type, port, is_stable)

    # 统一宿主机执行，daemon 不在线则报错
    if not await _daemon_available():
        return {"status": "error", "message": "host daemon not running (start: python3 scripts/host_daemon.py)"}

    if stable_dir:
        host_path = os.path.join(HOST_WORKSPACE_BASE, f"project_{project_id}", "_stable")
    else:
        host_path = os.path.join(HOST_WORKSPACE_BASE, f"project_{project_id}")
    result = await _daemon_request("POST", "/start", {"workspace": host_path, "cmd": cmd})
    if result and "_error" not in result:
        run_id = result["id"]
        _running[project_id] = {
            "proc": None, "daemon_id": run_id, "daemon_running": True,
            "cmd": cmd, "pid": result.get("pid", 0),
            "port": port, "path": path, "type": proj_type,
        }
        logger.info("[RUNNER] delegated to daemon: %s", run_id)
        return {
            "status": "running",
            "pid": result.get("pid", 0),
            "command": cmd,
            "port": port,
            "path": path,
            "type": proj_type,
            "daemon": True,
        }
    return {"status": "error", "message": f"daemon rejected start: {result}"}


async def stop_project(project_id: int) -> dict:
    """Stop a running project process and its children."""
    if project_id not in _running:
        return {"status": "not_running"}

    entry = _running[project_id]

    # Daemon-managed process
    if entry.get("daemon_id"):
        if not entry.get("daemon_running", False):
            del _running[project_id]
            return {"status": "already_exited"}
        result = await _daemon_request("POST", "/stop", {"id": entry["daemon_id"]})
        entry["daemon_running"] = False
        del _running[project_id]
        if result and result.get("ok"):
            return {"status": "stopped", "returncode": result.get("returncode")}
        return {"status": "stopped"}

    # Local process
    proc = entry["proc"]
    if proc.returncode is not None:
        del _running[project_id]
        return {"status": "already_exited"}

    import signal
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        proc.terminate()

    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            proc.kill()
        await proc.wait()

    del _running[project_id]
    return {"status": "stopped"}


def get_project_status(project_id: int) -> dict:
    """Get running status of a project."""
    if project_id not in _running:
        return {"running": False}

    entry = _running[project_id]

    # Daemon-managed process
    if entry.get("daemon_id"):
        return {
            "running": entry.get("daemon_running", False),
            "pid": entry.get("pid", 0),
            "command": entry["cmd"],
            "port": entry.get("port"),
            "path": entry.get("path", ""),
            "type": entry.get("type"),
            "daemon": True,
        }

    # Local process
    proc = entry["proc"]
    if proc.returncode is not None:
        del _running[project_id]
        return {"running": False, "exited": True, "returncode": proc.returncode}

    return {
        "running": True,
        "pid": proc.pid,
        "command": entry["cmd"],
        "port": entry.get("port"),
        "path": entry.get("path", ""),
        "type": entry.get("type"),
    }


async def get_project_output(project_id: int, since: int = 0) -> dict:
    """Return buffered output lines starting from index `since`."""
    if project_id in _running and _running[project_id].get("daemon_id"):
        entry = _running[project_id]
        result = await _daemon_request("GET", f"/logs/{entry['daemon_id']}?since={since}")
        if result and "_error" not in result:
            return result
        return {"lines": [], "total": 0, "running": entry.get("daemon_running", False), "returncode": None}

    # Local process
    lines = _output.get(project_id, [])
    running = False
    returncode = None

    if project_id in _running:
        proc = _running[project_id]["proc"]
        if proc.returncode is None:
            running = True
        else:
            returncode = proc.returncode
    elif lines:
        returncode = 0

    return {
        "lines": lines[since:],
        "total": len(lines),
        "running": running,
        "returncode": returncode,
    }


async def get_project_screenshot(project_id: int) -> bytes | None:
    """Get a screenshot of the project's GUI window via daemon."""
    if project_id not in _running:
        return None

    entry = _running[project_id]
    if not entry.get("daemon_id"):
        return None

    return await _daemon_get_bytes(f"/screenshot/{entry['daemon_id']}")


async def send_project_input(project_id: int, text: str | None = None, keys: str | None = None) -> dict:
    """Send input to a running project via daemon."""
    if project_id not in _running:
        return {"error": "not_running"}

    entry = _running[project_id]
    if not entry.get("daemon_id"):
        return {"error": "input only supported via daemon"}

    payload = {}
    if text is not None:
        payload["text"] = text
    if keys is not None:
        payload["keys"] = keys

    if not payload:
        return {"error": "text or keys required"}

    result = await _daemon_request("POST", f"/input/{entry['daemon_id']}", payload)
    if result and "_error" not in result:
        return result
    return {"error": "daemon request failed"}
