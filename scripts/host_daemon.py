#!/usr/bin/env python3
"""KH Host Daemon — lightweight HTTP server for managing processes on the host machine.

Run on the host (not in the container). The KH container calls this daemon
via HTTP to start/stop/observe programs that need host-level resources (GUI, hardware, etc).

Usage:
    python3 scripts/host_daemon.py [--port 8770] [--allowed-dirs ~/.kh/workspaces,./data/workspaces]
"""

import argparse
import asyncio
import fcntl
import logging
import os
import pty
import signal
import struct
import termios
import time
import uuid
from collections import deque
from pathlib import Path

from aiohttp import web

VERSION = "0.14"
DEFAULT_PORT = 8770
DEFAULT_ALLOWED_DIRS = "~/.kh/workspaces"
BUFFER_MAX_LINES = 1000
STOP_TIMEOUT = 5

logger = logging.getLogger("kh.host_daemon")

# --- Process Registry ---

_processes: dict[str, dict] = {}


def _gen_id() -> str:
    return f"run_{uuid.uuid4().hex[:8]}"


def _resolve_allowed_dirs(raw: str) -> list[str]:
    dirs = []
    for d in raw.split(","):
        d = d.strip()
        if d:
            dirs.append(str(Path(d).expanduser().resolve()))
    return dirs


def _is_path_allowed(workspace: str, allowed_dirs: list[str]) -> bool:
    resolved = str(Path(workspace).resolve())
    return any(resolved == d or resolved.startswith(d + os.sep) for d in allowed_dirs)


# --- Process Management ---

async def _read_output(run_id: str, proc: asyncio.subprocess.Process):
    buf = _processes[run_id]["output"]
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace").rstrip("\n")
            buf.append(text)
    except Exception:
        pass


async def _wait_proc(run_id: str, proc: asyncio.subprocess.Process):
    await proc.wait()
    if run_id in _processes:
        _processes[run_id]["running"] = False
        _processes[run_id]["returncode"] = proc.returncode
        logger.info("Process %s exited with code %d", run_id, proc.returncode)


async def _kill_process_group(proc: asyncio.subprocess.Process):
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        proc.terminate()

    try:
        await asyncio.wait_for(proc.wait(), timeout=STOP_TIMEOUT)
    except asyncio.TimeoutError:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            proc.kill()
        await proc.wait()


def _fix_ownership(workspace: str):
    """If workspace files are owned by root (created by container), chown to current user."""
    uid = os.getuid()
    if uid == 0:
        return
    st = os.stat(workspace)
    if st.st_uid != uid:
        import subprocess
        subprocess.run(["sudo", "chown", "-R", f"{uid}:{os.getgid()}", workspace],
                       capture_output=True, timeout=30)
        logger.info("Fixed ownership of %s to uid=%d", workspace, uid)


# --- HTTP Handlers ---

async def handle_start(request: web.Request) -> web.Response:
    data = await request.json()
    workspace = data.get("workspace", "")
    cmd = data.get("cmd", "")

    if not workspace or not cmd:
        return web.json_response({"error": "workspace and cmd required"}, status=400)

    allowed_dirs = request.app["allowed_dirs"]
    if not _is_path_allowed(workspace, allowed_dirs):
        logger.warning("Rejected start: workspace %s not in allowed dirs", workspace)
        return web.json_response({"error": "workspace not allowed"}, status=403)

    if not os.path.isdir(workspace):
        return web.json_response({"error": "workspace directory not found"}, status=404)

    run_id = _gen_id()

    proc = await asyncio.create_subprocess_shell(
        cmd,
        cwd=workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.PIPE,
        preexec_fn=os.setsid,
    )

    _processes[run_id] = {
        "proc": proc,
        "pid": proc.pid,
        "workspace": workspace,
        "cmd": cmd,
        "output": deque(maxlen=BUFFER_MAX_LINES),
        "running": True,
        "returncode": None,
        "started_at": time.time(),
    }

    asyncio.create_task(_read_output(run_id, proc))
    asyncio.create_task(_wait_proc(run_id, proc))

    logger.info("Started %s: pid=%d cmd=%s cwd=%s", run_id, proc.pid, cmd, workspace)

    return web.json_response({"id": run_id, "pid": proc.pid})


async def handle_stop(request: web.Request) -> web.Response:
    data = await request.json()
    run_id = data.get("id", "")

    if run_id not in _processes:
        return web.json_response({"error": "unknown process id"}, status=404)

    entry = _processes[run_id]
    proc = entry["proc"]

    if not entry["running"]:
        return web.json_response({"ok": True, "already_exited": True, "returncode": entry["returncode"]})

    await _kill_process_group(proc)

    entry["running"] = False
    entry["returncode"] = proc.returncode
    logger.info("Stopped %s (pid=%d)", run_id, entry["pid"])

    return web.json_response({"ok": True, "returncode": proc.returncode})


async def handle_logs(request: web.Request) -> web.Response:
    run_id = request.match_info["id"]
    since = int(request.query.get("since", "0"))

    if run_id not in _processes:
        return web.json_response({"error": "unknown process id"}, status=404)

    entry = _processes[run_id]
    buf = entry["output"]
    all_lines = list(buf)
    total = len(all_lines)

    return web.json_response({
        "lines": all_lines[since:],
        "total": total,
        "running": entry["running"],
        "returncode": entry["returncode"],
    })


async def handle_screenshot(request: web.Request) -> web.Response:
    run_id = request.match_info["id"]

    if run_id not in _processes:
        return web.json_response({"error": "unknown process id"}, status=404)

    entry = _processes[run_id]
    if not entry["running"]:
        return web.json_response({"error": "process not running"}, status=400)

    pid = entry["pid"]

    try:
        find_proc = await asyncio.create_subprocess_exec(
            "xdotool", "search", "--pid", str(pid),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(find_proc.communicate(), timeout=5)
        window_ids = stdout.decode().strip().split("\n")
        window_ids = [w for w in window_ids if w.strip()]
    except (asyncio.TimeoutError, FileNotFoundError):
        return web.json_response({"error": "xdotool not available or timed out"}, status=500)

    if not window_ids:
        return web.json_response({"error": "no_window"}, status=404)

    wid = window_ids[0]

    try:
        import_proc = await asyncio.create_subprocess_exec(
            "import", "-window", wid, "png:-",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        png_data, stderr = await asyncio.wait_for(import_proc.communicate(), timeout=10)
    except (asyncio.TimeoutError, FileNotFoundError):
        return web.json_response({"error": "import (ImageMagick) not available or timed out"}, status=500)

    if import_proc.returncode != 0:
        return web.json_response({"error": f"screenshot failed: {stderr.decode()}"}, status=500)

    return web.Response(body=png_data, content_type="image/png")


async def handle_input(request: web.Request) -> web.Response:
    run_id = request.match_info["id"]
    data = await request.json()

    if run_id not in _processes:
        return web.json_response({"error": "unknown process id"}, status=404)

    entry = _processes[run_id]
    if not entry["running"]:
        return web.json_response({"error": "process not running"}, status=400)

    text = data.get("text")
    keys = data.get("keys")

    if text is not None:
        proc = entry["proc"]
        if proc.stdin is None:
            return web.json_response({"error": "stdin not available"}, status=400)
        proc.stdin.write((text + "\n").encode())
        await proc.stdin.drain()
        return web.json_response({"ok": True, "mode": "stdin"})

    if keys is not None:
        pid = entry["pid"]
        try:
            find_proc = await asyncio.create_subprocess_exec(
                "xdotool", "search", "--pid", str(pid),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(find_proc.communicate(), timeout=5)
            window_ids = [w for w in stdout.decode().strip().split("\n") if w.strip()]
        except (asyncio.TimeoutError, FileNotFoundError):
            return web.json_response({"error": "xdotool not available"}, status=500)

        if not window_ids:
            return web.json_response({"error": "no_window"}, status=404)

        wid = window_ids[0]
        key_proc = await asyncio.create_subprocess_exec(
            "xdotool", "key", "--window", wid, keys,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(key_proc.communicate(), timeout=5)
        return web.json_response({"ok": True, "mode": "xdotool", "window": wid})

    return web.json_response({"error": "text or keys required"}, status=400)


async def handle_health(request: web.Request) -> web.Response:
    active_pids = [e["pid"] for e in _processes.values() if e["running"]]
    return web.json_response({
        "status": "ok",
        "version": VERSION,
        "pids": active_pids,
        "total_managed": len(_processes),
    })


# --- PTY Terminal ---

_terminals: dict[str, dict] = {}


async def handle_terminal_create(request: web.Request) -> web.Response:
    data = await request.json()
    workspace = data.get("workspace", "")

    if not workspace:
        return web.json_response({"error": "workspace required"}, status=400)

    allowed_dirs = request.app["allowed_dirs"]
    if not _is_path_allowed(workspace, allowed_dirs):
        return web.json_response({"error": "workspace not allowed"}, status=403)

    if not os.path.isdir(workspace):
        return web.json_response({"error": "workspace directory not found"}, status=404)

    term_id = f"term_{uuid.uuid4().hex[:8]}"

    master_fd, slave_fd = pty.openpty()

    pid = os.fork()
    if pid == 0:
        # Child process
        os.close(master_fd)
        os.setsid()
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        os.close(slave_fd)
        os.chdir(workspace)
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["PS1"] = r"\w $ "
        os.execvpe("/bin/bash", ["/bin/bash", "--norc", "-i"], env)

    # Parent process
    os.close(slave_fd)
    # Set master_fd non-blocking
    flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    _terminals[term_id] = {
        "master_fd": master_fd,
        "pid": pid,
        "workspace": workspace,
        "created_at": time.time(),
    }

    # Auto-run start.sh if it exists
    start_sh = os.path.join(workspace, "start.sh")
    if os.path.isfile(start_sh):
        cmd = b"bash start.sh && exec bash\n"
        os.write(master_fd, cmd)

    logger.info("Terminal %s created: pid=%d cwd=%s", term_id, pid, workspace)
    return web.json_response({"id": term_id, "pid": pid})


async def handle_terminal_ws(request: web.Request) -> web.WebSocketResponse:
    term_id = request.match_info["id"]

    if term_id not in _terminals:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"error": "unknown terminal id"})
        await ws.close()
        return ws

    entry = _terminals[term_id]
    master_fd = entry["master_fd"]

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    loop = asyncio.get_event_loop()

    # Read from PTY → send to WebSocket
    async def pty_reader():
        try:
            while not ws.closed:
                try:
                    data = await loop.run_in_executor(None, _pty_read, master_fd)
                    if data:
                        await ws.send_bytes(data)
                except OSError:
                    break
                await asyncio.sleep(0.01)
        except Exception:
            pass

    reader_task = asyncio.create_task(pty_reader())

    # Read from WebSocket → write to PTY
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.BINARY:
                os.write(master_fd, msg.data)
            elif msg.type == web.WSMsgType.TEXT:
                # Handle resize messages
                if msg.data.startswith('{"resize":'):
                    import json
                    info = json.loads(msg.data)
                    cols = info["resize"].get("cols", 80)
                    rows = info["resize"].get("rows", 24)
                    winsize = struct.pack("HHHH", rows, cols, 0, 0)
                    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
                else:
                    os.write(master_fd, msg.data.encode())
            elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                break
    except Exception:
        pass
    finally:
        reader_task.cancel()

    return ws


def _pty_read(master_fd: int) -> bytes:
    try:
        return os.read(master_fd, 4096)
    except (OSError, BlockingIOError):
        import time as _t
        _t.sleep(0.05)
        return b""


async def handle_terminal_stop(request: web.Request) -> web.Response:
    data = await request.json()
    term_id = data.get("id", "")

    if term_id not in _terminals:
        return web.json_response({"error": "unknown terminal id"}, status=404)

    entry = _terminals[term_id]
    pid = entry["pid"]
    master_fd = entry["master_fd"]

    try:
        os.kill(pid, signal.SIGTERM)
        await asyncio.sleep(0.5)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(pid, os.WNOHANG)
    except (ProcessLookupError, ChildProcessError):
        pass

    try:
        os.close(master_fd)
    except OSError:
        pass

    del _terminals[term_id]
    logger.info("Terminal %s stopped", term_id)
    return web.json_response({"ok": True})


# --- App Setup ---

def create_app(allowed_dirs: list[str]) -> web.Application:
    app = web.Application()
    app["allowed_dirs"] = allowed_dirs

    app.router.add_post("/start", handle_start)
    app.router.add_post("/stop", handle_stop)
    app.router.add_get("/logs/{id}", handle_logs)
    app.router.add_get("/screenshot/{id}", handle_screenshot)
    app.router.add_post("/input/{id}", handle_input)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/terminal", handle_terminal_create)
    app.router.add_get("/ws/terminal/{id}", handle_terminal_ws)
    app.router.add_post("/terminal/stop", handle_terminal_stop)

    return app


def main():
    parser = argparse.ArgumentParser(description="KH Host Daemon")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Listen port (default: {DEFAULT_PORT})")
    parser.add_argument("--allowed-dirs", type=str, default=DEFAULT_ALLOWED_DIRS,
                        help="Comma-separated list of allowed workspace directories")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(Path("~/.kh/daemon.log").expanduser(), mode="a"),
        ],
    )

    os.makedirs(Path("~/.kh").expanduser(), exist_ok=True)

    allowed_dirs = _resolve_allowed_dirs(args.allowed_dirs)
    logger.info("KH Host Daemon v%s starting on 127.0.0.1:%d", VERSION, args.port)
    logger.info("Allowed dirs: %s", allowed_dirs)

    app = create_app(allowed_dirs)
    web.run_app(app, host="127.0.0.1", port=args.port, print=lambda *a: None)


if __name__ == "__main__":
    main()
