"""Preserve native Vibe state across Railway deployments."""
import os
from pathlib import Path
import pwd
import secrets


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
        link = Path(source)
        if not link.is_symlink():
            # Only remove the image's empty placeholder; never erase state.
            if link.exists():
                link.rmdir()
            link.symlink_to(target, target_is_directory=True)
    envfile = data / "settings.env"
    envfile.touch(mode=0o600, exist_ok=True)
    os.chown(envfile, uid, gid)
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
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    os.execvp("vibe-trading", ["vibe-trading", "serve", "--host", "0.0.0.0", "--port", os.environ.get("PORT", "8899")])


if __name__ == "__main__":
    main()
