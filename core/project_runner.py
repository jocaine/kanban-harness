"""Project Runner — execute start.sh and manage project processes.

Coach-Dev is required to maintain a start.sh in every project.
This module only reads start.sh — no heuristic fallback.
"""

import asyncio
import logging
import os
import re

logger = logging.getLogger("kh.core.project_runner")

WORKSPACE_BASE = os.getenv("KH_WORKSPACE", os.path.expanduser("~/.kh/workspaces"))

_running: dict[int, dict] = {}
_output: dict[int, list[str]] = {}
_OUTPUT_MAX_LINES = 500


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
    """Start a project by executing its start.sh."""
    if project_id in _running:
        proc = _running[project_id]["proc"]
        if proc.returncode is None:
            entry = _running[project_id]
            return {
                "status": "already_running",
                "pid": proc.pid,
                "command": entry["cmd"],
                "port": entry.get("port"),
                "path": entry.get("path", ""),
                "type": entry.get("type"),
            }
        else:
            del _running[project_id]

    project_path = os.path.join(WORKSPACE_BASE, f"project_{project_id}")
    if not os.path.isdir(project_path):
        return {"status": "error", "message": f"workspace not found: project_{project_id}"}

    info = parse_start_script(project_path)
    if not info:
        return {"status": "error", "message": "no_start_sh"}

    cmd = info["command"]
    port = info["port"]
    path = info["path"]
    proj_type = info["type"]

    logger.info("[RUNNER] start project_%d: %s (type=%s, port=%s)", project_id, cmd, proj_type, port)

    _output[project_id] = []

    proc = await asyncio.create_subprocess_shell(
        cmd,
        cwd=project_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        preexec_fn=os.setsid,
    )

    asyncio.create_task(_read_output(project_id, proc))

    _running[project_id] = {
        "proc": proc, "cmd": cmd, "pid": proc.pid,
        "port": port, "path": path, "type": proj_type,
    }
    return {
        "status": "running",
        "pid": proc.pid,
        "command": cmd,
        "port": port,
        "path": path,
        "type": proj_type,
    }


async def stop_project(project_id: int) -> dict:
    """Stop a running project process and its children."""
    if project_id not in _running:
        return {"status": "not_running"}

    proc = _running[project_id]["proc"]
    if proc.returncode is not None:
        del _running[project_id]
        return {"status": "already_exited"}

    # Kill the entire process group (bash + child processes like python3 http.server)
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


def get_project_output(project_id: int, since: int = 0) -> dict:
    """Return buffered output lines starting from index `since`."""
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
