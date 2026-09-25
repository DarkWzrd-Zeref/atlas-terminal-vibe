"""Preserve native Vibe state across Railway deployments."""
import os
from pathlib import Path
import pwd
import secrets
import signal
import subprocess
import sys
import time


def main():
    data = Path("/data")
    if os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") != str(data):
        raise SystemExit("Vibe requires a persistent Railway volume mounted at /data.")
    account = pwd.getpwnam("vibe")
    uid, gid = account.pw_uid, account.pw_gid
    directories = {
        "/app/agent/runs": "runs",
        "/app/agent/sessions": "sessions",
        "/app/agent/uploads": "uploads",
        "/app/agent/.swarm/runs": "swarm-runs",
        "/home/vibe/.vibe-trading": "home",
    }
    for source, name in directories.items():
        target = data / name
        target.mkdir(exist_ok=True)
        os.chown(target, uid, gid)
        target.chmod(0o700)
        link = Path(source)
        if not link.is_symlink():
            # Only remove the image's empty placeholder; never erase state.
            if link.exists():
                link.rmdir()
            link.symlink_to(target, target_is_directory=True)
    envfile = data / "settings.env"
    envfile.touch(mode=0o600, exist_ok=True)
    os.chown(envfile, uid, gid)
    envfile.chmod(0o600)
    link = Path("/app/agent/.env")
    if not link.is_symlink():
        link.symlink_to(envfile)
    authfile = data / "api-auth-key"
    if not authfile.exists():
        with authfile.open("x") as handle:
            handle.write(secrets.token_urlsafe(48))
        authfile.chmod(0o600)
    os.chown(authfile, uid, gid)
    os.environ["API_AUTH_KEY"] = os.environ.get("ATLAS_VIBE_API_KEY") or authfile.read_text().strip()
    os.environ["HOME"] = "/home/vibe"
    os.environ["VIBE_TRADING_HOME"] = "/home/vibe/.vibe-trading"
    os.environ["ATLAS_REQUIRE_ISOLATION"] = "1"
    # Shell/background tools bypass the native Runner. Keep the networked
    # server's upstream default disabled even if an inherited variable differs.
    os.environ["VIBE_TRADING_ENABLE_SHELL_TOOLS"] = "false"
    from atlas_sandbox import SOCKET
    SOCKET.unlink(missing_ok=True)
    broker = subprocess.Popen(
        [sys.executable, "-I", "/app/agent/atlas_sandbox.py", "--broker"],
        env={"PATH": os.environ["PATH"], "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1"},
        start_new_session=True,
    )
    server = None
    stopping = False

    def stop(_signal=None, _frame=None):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        deadline = time.monotonic() + 180
        while not SOCKET.exists():
            if broker.poll() is not None or time.monotonic() > deadline or stopping:
                raise SystemExit("Vibe isolation broker failed startup verification; server was not started.")
            time.sleep(0.1)
        server = subprocess.Popen(
            ["vibe-trading", "serve", "--host", "0.0.0.0", "--port", os.environ.get("PORT", "8899")],
            user=uid, group=gid, extra_groups=[], start_new_session=True,
        )
        while not stopping:
            if broker.poll() is not None:
                raise SystemExit("Vibe isolation broker exited; stopping the server.")
            if server.poll() is not None:
                raise SystemExit(server.returncode)
            time.sleep(0.5)
    finally:
        for child in (server, broker):
            if child is not None and child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
        for child in (server, broker):
            if child is not None:
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=5)
        from atlas_sandbox import _kill_sandbox_processes
        _kill_sandbox_processes(pwd.getpwnam("vibe-sandbox").pw_uid)


if __name__ == "__main__":
    main()
