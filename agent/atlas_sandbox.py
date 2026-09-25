"""Atlas's Linux-only native-backtest broker and filesystem boundary.

The public application never runs as root. The broker accepts one fixed native
operation from the application UID, then executes it as a different UID with a
Landlock allowlist. No model text, interpreter path, or shell command is accepted.
The module is deliberately stdlib-only so its privileged portion imports no
application plugins. This does not isolate outbound networking.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time

AGENT = Path(__file__).resolve().parent
SOCKET = Path("/run/atlas-sandbox/backtest.sock")
DATA = Path("/data")
RUN_ROOTS = tuple(DATA / p for p in ("runs", "swarm-runs", "home/runs", "home/shadow_runs", "home/swarm/runs"))
IMPORT_ROOTS = tuple(DATA / p for p in ("uploads", "home/uploads", "home/imports", "home/data-bridge"))
MAX_REQUEST = 65536
MAX_OUTPUT = 2 * 1024 * 1024
MAX_RESPONSE = 12 * MAX_OUTPUT + 65536  # JSON may escape each output byte.
_JOB_LOCK = threading.Lock()

# Only loader credentials/configuration, never provider/API/broker credentials.
# Interpreter, HOME, PYTHONPATH and dynamic-linker settings are broker-owned.
DATA_ENV = frozenset({
    "TUSHARE_TOKEN", "FINNHUB_API_KEY", "ALPHAVANTAGE_API_KEY", "TIINGO_API_KEY",
    "FMP_API_KEY", "FRED_API_KEY", "VIBE_TRADING_IWENCAI_KEY", "VIBE_TRADING_SEC_UA",
    "VIBE_TRADING_DATA_CACHE", "CCXT_EXCHANGE", "CCXT_TIMEOUT_MS", "CCXT_FETCH_BUDGET_S",
    "OKX_TIMEOUT_S", "OKX_FETCH_BUDGET_S", "RSSHUB_BASE_URL", "RSSHUB_TIMEOUT_S",
    "RSSHUB_FETCH_BUDGET_S", "FUTU_HOST", "FUTU_PORT", "HTTP_PROXY", "HTTPS_PROXY",
    "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "VIBE_TRADING_EASTMONEY_MIN_INTERVAL", "VIBE_TRADING_SINA_MIN_INTERVAL",
    "VIBE_TRADING_STOOQ_MIN_INTERVAL", "VIBE_TRADING_YAHOO_MIN_INTERVAL",
    "VIBE_TRADING_SEC_MIN_INTERVAL", "VIBE_TRADING_FINNHUB_MIN_INTERVAL",
    "VIBE_TRADING_ALPHAVANTAGE_MIN_INTERVAL", "VIBE_TRADING_TIINGO_MIN_INTERVAL",
    "VIBE_TRADING_FMP_MIN_INTERVAL", "VIBE_TRADING_FRED_MIN_INTERVAL",
    "VIBE_TRADING_IWENCAI_MIN_INTERVAL", "VIBE_TRADING_THS_MIN_INTERVAL",
})


def _accounts():
    import pwd
    return pwd.getpwnam("vibe"), pwd.getpwnam("vibe-sandbox")


def validate_run(raw: object, roots: tuple[Path, ...] = RUN_ROOTS) -> Path:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ValueError("A native run directory is required")
    path = Path(raw).resolve(strict=True)
    if not path.is_dir() or not any(root.resolve() == root and path != root and path.is_relative_to(root) for root in roots):
        raise ValueError("Run directory must be inside an approved run root")
    # Resolve once, then reject symlinks in the canonical tree and hard-linked
    # files. Passing an app's known top-level volume symlink is still supported.
    count = 0
    for directory, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            info = (Path(directory) / name).lstat()
            count += 1
            if count > 20000:
                raise ValueError("Run has too many entries")
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise ValueError("Run directory may contain only regular files and directories")
            if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                raise ValueError("Hard-linked files are not permitted in a run")
    return path


def validate_request(value: object) -> tuple[Path, int, dict[str, str]]:
    if not isinstance(value, dict) or set(value) != {"operation", "run_dir", "timeout", "env"}:
        raise ValueError("Invalid native-backtest request")
    if value["operation"] != "backtest":
        raise ValueError("Only the native backtest operation is supported")
    timeout = value["timeout"]
    if type(timeout) is not int or not 1 <= timeout <= 900:
        raise ValueError("Backtest timeout must be between 1 and 900 seconds")
    supplied = value["env"]
    if not isinstance(supplied, dict) or any(not isinstance(k, str) or not isinstance(v, str) or "\x00" in v for k, v in supplied.items()):
        raise ValueError("Invalid loader environment")
    # Ignore all unexpected values; never pass client-controlled runtime knobs.
    env = {k: v for k, v in supplied.items() if k in DATA_ENV}
    return validate_run(value["run_dir"]), timeout, env


def _prepare_permissions(run: Path, owner_uid: int, shared_gid: int) -> None:
    # Traverse the private home only along the current run's path. Landlock
    # denies every sibling and private file even when its Unix mode is broad.
    current = run.parent
    while current != DATA and current.is_relative_to(DATA):
        os.chmod(current, stat.S_IMODE(current.stat().st_mode) | 0o010)
        current = current.parent
    # fwalk descriptors and no-follow metadata operations prevent chown/chmod
    # from following model-created links outside the run tree.
    for directory, dirs, files, fd in os.fwalk(run, follow_symlinks=False):
        os.fchown(fd, owner_uid, shared_gid)
        os.fchmod(fd, 0o2770)
        for name in files:
            filefd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                info = os.fstat(filefd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("Run changed during validation")
                os.fchown(filefd, owner_uid, shared_gid)
                os.fchmod(filefd, 0o660)
            finally:
                os.close(filefd)


def _prepare_import_permissions() -> list[Path]:
    # Imported datasets are explicit read-only inputs. Landlock denies writes
    # even if the application's group has write access through Unix modes.
    available = []
    for root in IMPORT_ROOTS:
        if not root.is_dir() or root.resolve() != root:
            continue
        # Determine existence while privileged, rather than asking the child
        # to inspect missing paths beneath a private 0700 application home.
        # Existing approved inputs need traversal only along their ancestors;
        # do not grant directory listing or read access to the enclosing home.
        current = root.parent
        while current != DATA and current.is_relative_to(DATA):
            os.chmod(current, stat.S_IMODE(current.stat().st_mode) | 0o010)
            current = current.parent
        for directory, dirs, files, fd in os.fwalk(root, follow_symlinks=False):
            os.fchmod(fd, stat.S_IMODE(os.fstat(fd).st_mode) | 0o050)
            for name in files:
                try:
                    filefd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                except OSError:
                    continue
                try:
                    info = os.fstat(filefd)
                    if stat.S_ISREG(info.st_mode):
                        os.fchmod(filefd, stat.S_IMODE(info.st_mode) | 0o040)
                finally:
                    os.close(filefd)
        available.append(root)
    return available


def _copy_loader_config(home: Path) -> None:
    target = home / ".vibe-trading"
    target.mkdir(mode=0o770)
    # These are upstream's explicitly re-exposed loader settings; do not copy
    # the enclosing home, agent.json, .env, sessions, or broker configuration.
    for relative in ("data-bridge/config.yaml", "qveris.json"):
        source = DATA / "home" / relative
        if not source.is_file() or source.resolve() != source or source.stat().st_size > 1024 * 1024:
            continue
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    # mootdx expects its config file to exist even when first-run server
    # discovery fails. This is market-data configuration, never credentials.
    mootdx = home / ".mootdx"
    mootdx.mkdir()
    (mootdx / "config.json").write_text("{}")


def _child_env(run: Path, home: Path, loader_env: dict[str, str]) -> dict[str, str]:
    return {
        **{k: v for k, v in loader_env.items() if k in DATA_ENV},
        "PATH": "/opt/venv/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home), "USERPROFILE": str(home), "USER": "vibe-sandbox",
        "LANG": "C.UTF-8", "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
        "TMPDIR": str(home / "tmp"), "XDG_CACHE_HOME": str(home / "cache"),
        "XDG_CONFIG_HOME": str(home / "config"), "XDG_DATA_HOME": str(home / "local"),
        "MPLCONFIGDIR": str(home / "config" / "matplotlib"),
        "VIBE_TRADING_ALLOWED_RUN_ROOTS": str(run),
        "VIBE_TRADING_HOME": str(home / ".vibe-trading"),
        "ATLAS_SANDBOX_CHILD": "1", "PYTHON_DOTENV_DISABLED": "1",
        "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1",
    }


def _libc():
    return ctypes.CDLL(None, use_errno=True)


def _checked(value: int, action: str) -> int:
    if value < 0:
        raise OSError(ctypes.get_errno(), action)
    return value


def restrict_filesystem(readonly: list[Path], writable: list[Path]) -> int:
    """Apply Landlock >= ABI 3, including truncation denial, or fail closed."""
    if sys.platform != "linux":
        raise RuntimeError("Atlas isolated execution requires Linux Landlock")
    libc = _libc()
    abi = _checked(libc.syscall(444, 0, 0, 1), "Landlock is unavailable")
    if abi < 3:
        raise RuntimeError("Atlas isolated execution requires Landlock ABI 3 or later")
    # Linux's generic syscall numbers are shared by x86_64 and aarch64.
    handled = (1 << 15) - 1
    class Ruleset(ctypes.Structure):
        _fields_ = [("handled_access_fs", ctypes.c_uint64)]
    class PathBeneath(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]
    rules = Ruleset(handled)
    rules_fd = _checked(libc.syscall(444, ctypes.byref(rules), ctypes.sizeof(rules), 0), "Create filesystem boundary")
    try:
        for paths, rights in ((readonly, 1 | 4 | 8), (writable, handled)):
            for path in paths:
                if not path.exists():
                    continue
                fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
                try:
                    allowed = rights if path.is_dir() else rights & (1 | 2 | 4 | (1 << 14))
                    rule = PathBeneath(allowed, fd)
                    _checked(libc.syscall(445, rules_fd, 1, ctypes.byref(rule), 0), "Add filesystem boundary")
                finally:
                    os.close(fd)
        _checked(libc.prctl(38, 1, 0, 0, 0), "Disable privilege gains")
        _checked(libc.syscall(446, rules_fd, 0), "Enforce filesystem boundary")
    finally:
        os.close(rules_fd)
    return abi


def _apply_child_boundary(run: Path, home: Path, approved_imports: list[Path]) -> int:
    import resource
    os.umask(0o007)
    for key, limit in ((resource.RLIMIT_AS, 4096 * 1024 * 1024), (resource.RLIMIT_NOFILE, 512),
                       (resource.RLIMIT_NPROC, 64), (resource.RLIMIT_FSIZE, 64 * 1024 * 1024),
                       (resource.RLIMIT_CORE, 0)):
        resource.setrlimit(key, (limit, limit))
    readonly = [Path(p) for p in ("/usr", "/opt/venv", "/lib", "/lib64", "/etc", "/app",
                                  "/dev/urandom", "/dev/random", "/sys/devices/system/cpu")]
    if len(approved_imports) > len(IMPORT_ROOTS) or any(path not in IMPORT_ROOTS for path in approved_imports):
        raise RuntimeError("Unapproved isolated input directory")
    readonly.extend(approved_imports)
    return restrict_filesystem(readonly, [run, home, Path("/dev/null")])


def _sandbox_pids(uid: int) -> list[int]:
    result = []
    for item in Path("/proc").iterdir():
        if item.name.isdigit():
            try:
                if item.stat().st_uid == uid:
                    result.append(int(item.name))
            except FileNotFoundError:
                pass
    return result


def _kill_sandbox_processes(uid: int) -> None:
    # One job at a time, with an account reserved exclusively for this broker.
    # Kill by UID as well as process group so a detached grandchild cannot
    # outlive a run. The broker is a subreaper and reaps adopted descendants.
    for pid in _sandbox_pids(uid):
        try:
            os.kill(pid, signal.SIGKILL)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            pass


def _wait_timeout_probe_ready(process, pid_file: Path, timeout: float = 10) -> bool:
    """Bound startup separately so the timeout exercises a real descendant."""
    deadline = time.monotonic() + timeout
    while process.poll() is None:
        try:
            if int(pid_file.read_text()) > 1:
                return True
        except (FileNotFoundError, ValueError):
            pass  # File creation and its first write need not be atomic.
        if time.monotonic() >= deadline:
            break
        time.sleep(0.02)
    return False


def _timeout_probe_child(run: Path) -> None:
    stage = "fork"
    try:
        if os.fork() == 0:
            stage = "detach"
            os.setsid()
            stage = "pid_file"
            (run / "detached-probe.pid").write_text(str(os.getpid()))
        while True:
            time.sleep(1)
    except Exception as exc:
        # Only this fixed synthetic probe uses this record. Never print the
        # exception message, paths, environment, or any generated-code output.
        print("ATLAS_TIMEOUT_PROBE " + json.dumps({"stage": stage,
            "error_type": type(exc).__name__, "errno": getattr(exc, "errno", None)}),
            file=sys.stderr, flush=True)
        raise SystemExit(1) from None


def _timeout_probe_diagnostics(timed: dict, detached: Path) -> dict:
    diagnostics = {"timed_out": bool(timed["timed_out"]), "returncode": timed["returncode"],
        "probe_ready": bool(timed.get("probe_ready")), "detached_pid_file_exists": detached.is_file(),
        "descendant_pid_valid": False, "descendant_alive": False,
        "stderr_present": bool(timed["stderr"])}
    if diagnostics["detached_pid_file_exists"]:
        try:
            pid = int(detached.read_text())
            diagnostics["descendant_pid_valid"] = pid > 1
            if pid > 1:
                try:
                    os.kill(pid, 0)
                    diagnostics["descendant_alive"] = True
                except ProcessLookupError:
                    pass
                except PermissionError:
                    diagnostics["descendant_alive"] = True
        except (OSError, ValueError):
            pass
    for line in timed["stderr"][-2048:].splitlines():
        if line.startswith("ATLAS_TIMEOUT_PROBE "):
            try:
                record = json.loads(line.partition(" ")[2])
                if (record.get("stage") in {"fork", "detach", "pid_file"}
                    and isinstance(record.get("error_type"), str)
                    and record["error_type"].isidentifier() and len(record["error_type"]) <= 64
                    and (record.get("errno") is None or type(record["errno"]) is int)):
                    diagnostics["child_error"] = {key: record.get(key) for key in ("stage", "error_type", "errno")}
            except (ValueError, AttributeError):
                pass
    if timed["stderr"].strip():
        error_type = timed["stderr"].strip().splitlines()[-1].partition(":")[0]
        if error_type.isidentifier() and len(error_type) <= 64:
            diagnostics["stderr_error_type"] = error_type
    return diagnostics


def run_native(run: Path, timeout: int, loader_env: dict[str, str], *, probe: bool = False,
               timeout_probe: bool = False, fixture_bridge: dict | None = None) -> dict:
    owner, sandbox = _accounts()
    _prepare_permissions(run, owner.pw_uid, owner.pw_gid)
    approved_imports = _prepare_import_permissions()
    home = Path(tempfile.mkdtemp(prefix="atlas-vibe-job-"))
    timed_out = False
    probe_ready = False
    try:
        _copy_loader_config(home)
        if fixture_bridge is not None:
            # Startup-only synthetic data; this argument is not in the socket
            # protocol and cannot be supplied by the application or a model.
            fixture = home / ".vibe-trading" / "data-bridge" / "config.yaml"
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_text(json.dumps(fixture_bridge))
        for rel in ("tmp", "cache", "config", "local"):
            (home / rel).mkdir()
        for root, dirs, files in os.walk(home):
            os.chown(root, sandbox.pw_uid, owner.pw_gid)
            os.chmod(root, 0o770)
            for name in files:
                os.chown(Path(root) / name, sandbox.pw_uid, owner.pw_gid)
                os.chmod(Path(root) / name, 0o660)
        mode = "--timeout-child" if timeout_probe else "--probe-child" if probe else "--child"
        cmd = [sys.executable, "-I", str(AGENT / "atlas_sandbox.py"), mode, str(run), str(home),
               json.dumps([str(path) for path in approved_imports])]
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(cmd, cwd=AGENT, env=_child_env(run, home, loader_env),
                user=sandbox.pw_uid, group=owner.pw_gid, extra_groups=[],
                stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True)
            try:
                if timeout_probe:
                    probe_ready = _wait_timeout_probe_ready(process, run / "detached-probe.pid")
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                _kill_sandbox_processes(sandbox.pw_uid)
                process.wait(timeout=10)
                # Adoption and SIGKILL delivery are asynchronous. Bound the
                # wait but do not mistake an unreaped zombie for clean exit.
                cleanup_deadline = time.monotonic() + 3
                while True:
                    while True:
                        try:
                            if os.waitpid(-1, os.WNOHANG)[0] == 0:
                                break
                        except ChildProcessError:
                            break
                    if not _sandbox_pids(sandbox.pw_uid):
                        break
                    if time.monotonic() >= cleanup_deadline:
                        raise RuntimeError("Isolated process cleanup did not finish")
                    _kill_sandbox_processes(sandbox.pw_uid)
                    time.sleep(0.02)
            stdout.seek(0)
            stderr.seek(0)
            return {"returncode": process.returncode, "stdout": stdout.read(MAX_OUTPUT).decode("utf-8", "replace"),
                    "stderr": stderr.read(MAX_OUTPUT).decode("utf-8", "replace"), "timed_out": timed_out,
                    "probe_ready": probe_ready}
    finally:
        shutil.rmtree(home)


def _probe_child(run: Path, home: Path, abi: int) -> None:
    owner, sandbox = _accounts()
    checks = {"uid": os.getuid() == sandbox.pw_uid, "groups": os.getgroups() == [], "landlock": abi >= 3}
    for label, path in (("secret_denied", DATA / (run.name + "-sentinel")),
                        ("settings_denied", DATA / "settings.env"),
                        ("sibling_denied", DATA / (run.name + "-sibling")),
                        ("proc_denied", Path("/proc/1/environ"))):
        try:
            path.read_bytes()
            checks[label] = False
        except PermissionError:
            checks[label] = True
        except FileNotFoundError:
            checks[label] = label in {"settings_denied", "proc_denied"}
    try:
        with (AGENT / "atlas_sandbox.py").open("a"):
            pass
        checks["source_readonly"] = False
    except PermissionError:
        checks["source_readonly"] = True
    for label, path in (("output_write", run / "isolation-output.txt"), ("cache_write", home / "cache" / "probe.txt")):
        path.write_text("synthetic isolation probe\n")
        checks[label] = path.read_text().startswith("synthetic")
    checks["env_filtered"] = all(k not in os.environ for k in ("OPENAI_API_KEY", "API_AUTH_KEY", "ATLAS_VIBE_API_KEY"))
    print(json.dumps(checks, sort_keys=True))
    if not all(checks.values()):
        raise SystemExit(1)


def startup_probe() -> dict:
    # Dummy, world-readable files demonstrate the kernel boundary rather than
    # succeeding solely because Unix owner bits happen to hide a secret.
    run_root = DATA / "runs"
    run_root.mkdir(exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="atlas-isolation-check-", dir=run_root))
    files = [DATA / (run.name + "-sentinel"), DATA / (run.name + "-sibling")]
    try:
        for path in files:
            with path.open("x") as handle:
                handle.write("synthetic credential; not an actual secret\n")
            path.chmod(0o644)
        result = run_native(run, 30, {"OPENAI_API_KEY": "dummy"}, probe=True)
        if result["returncode"] != 0 or result["timed_out"]:
            raise RuntimeError("Atlas isolation startup probe failed: " + result["stderr"][-1200:])
        checks = json.loads(result["stdout"])
        if not all(checks.values()):
            raise RuntimeError("Atlas isolation startup checks did not all pass")
        for label, kind in (("symlink_rejected", "symlink"), ("hardlink_rejected", "hardlink")):
            escape = run / "escape-probe"
            if kind == "symlink":
                escape.symlink_to(files[0])
            else:
                os.link(files[0], escape)
            try:
                validate_run(str(run))
                checks[label] = False
            except ValueError:
                checks[label] = True
            finally:
                escape.unlink()
        if not checks["symlink_rejected"] or not checks["hardlink_rejected"]:
            raise RuntimeError("Isolated run path checks failed")
        # Positive coverage runs the actual native loader/SignalEngine/engine
        # pipeline on a tiny synthetic local dataset, without any AI/network.
        (run / "code").mkdir()
        (run / "code" / "signal_engine.py").write_text(
            "import pandas as pd\nclass SignalEngine:\n"
            "    def generate(self, data_map):\n"
            "        return {code: pd.Series(0.5, index=bars.index) for code, bars in data_map.items()}\n"
        )
        bars = run / "synthetic-bars.csv"
        bars.write_text("date,open,high,low,close,volume\n" + "".join(
            f"2025-01-{day:02d},{100 + day},{102 + day},{99 + day},{101 + day},10000\n" for day in range(2, 16)))
        (run / "config.json").write_text(json.dumps({"source": "local", "codes": ["SPY"],
            "start_date": "2025-01-02", "end_date": "2025-01-15", "interval": "1D", "initial_cash": 10000}))
        native = run_native(run, 90, {}, fixture_bridge={"sources": [{"symbol": "SPY", "type": "csv", "path": str(bars)}]})
        checks["native_backtest"] = native["returncode"] == 0 and not native["timed_out"]
        checks["native_outputs"] = all((run / "artifacts" / name).is_file() for name in ("equity.csv", "metrics.csv", "trades.csv"))
        if not checks["native_backtest"] or not checks["native_outputs"]:
            raise RuntimeError("Isolated native backtest startup fixture failed: " + native["stderr"][-1800:])
        timed = run_native(run, 1, {}, timeout_probe=True)
        detached = run / "detached-probe.pid"
        diagnostics = _timeout_probe_diagnostics(timed, detached)
        print("ATLAS_TIMEOUT_CLEANUP " + json.dumps(diagnostics, sort_keys=True), flush=True)
        checks["timeout_cleanup"] = (diagnostics["timed_out"] and diagnostics["probe_ready"]
            and diagnostics["descendant_pid_valid"] and not diagnostics["descendant_alive"])
        if not checks["timeout_cleanup"]:
            raise RuntimeError("Isolated timeout cleanup probe failed")
        return checks
    finally:
        shutil.rmtree(run)
        for path in files:
            path.unlink(missing_ok=True)


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        owner, _ = _accounts()
        _, uid, _ = struct.unpack("3i", self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != owner.pw_uid:
            return
        self.request.settimeout(10)
        raw = self.rfile.readline(MAX_REQUEST + 1)
        if len(raw) > MAX_REQUEST or not raw.endswith(b"\n"):
            return
        if not _JOB_LOCK.acquire(blocking=False):
            response = {"error": "A native backtest is already running; retry when it finishes"}
        else:
            try:
                run, timeout, env = validate_request(json.loads(raw))
                response = run_native(run, timeout, env)
            except Exception as exc:
                response = {"error": "Isolated native backtest refused: " + str(exc)}
            finally:
                _JOB_LOCK.release()
        self.wfile.write(json.dumps(response).encode() + b"\n")


class Server(getattr(socketserver, "ThreadingUnixStreamServer", socketserver.ThreadingTCPServer)):
    daemon_threads = True


def serve() -> None:
    if os.getuid() != 0:
        raise RuntimeError("Only the startup supervisor may run the isolated broker")
    _checked(_libc().prctl(36, 1, 0, 0, 0), "Set broker child subreaper")
    owner, sandbox = _accounts()
    _kill_sandbox_processes(sandbox.pw_uid)
    SOCKET.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chown(SOCKET.parent, 0, owner.pw_gid)
    os.chmod(SOCKET.parent, 0o750)
    SOCKET.unlink(missing_ok=True)
    checks = startup_probe()
    print("ATLAS_ISOLATION " + json.dumps({"verified": True, "checks": checks, "network_isolated": False}), flush=True)
    with Server(str(SOCKET), Handler) as server:
        os.chown(SOCKET, 0, owner.pw_gid)
        os.chmod(SOCKET, 0o660)
        server.serve_forever()


def _disable_child_dotenv() -> None:
    """Keep dependency settings on the broker-filtered environment only.

    Pydantic settings calls dotenv_values directly, which ignores
    PYTHON_DOTENV_DISABLED. Some native dependencies create settings during
    import, so disable that file source before importing the native runner.
    This is child-process compatibility policy, not the security boundary:
    Landlock still denies private files even if generated code undoes it.
    The privileged broker and trusted web server never install this policy.
    """
    if os.environ.get("ATLAS_SANDBOX_CHILD") != "1":
        return
    from pydantic_settings import DotEnvSettingsSource

    if not callable(getattr(DotEnvSettingsSource, "_read_env_files", None)):
        raise RuntimeError("Unsupported isolated settings file source")

    def no_env_files(self):
        return {}

    # Covers model_config env_file, constructor _env_file overrides, and
    # dependencies constructing the source directly, without touching the
    # separate EnvSettingsSource or constructor/default settings sources.
    DotEnvSettingsSource._read_env_files = no_env_files


def request_backtest(run: Path, timeout: int, env: dict[str, str]) -> subprocess.CompletedProcess:
    payload = {"operation": "backtest", "run_dir": str(run), "timeout": timeout,
               "env": {k: v for k, v in env.items() if k in DATA_ENV}}
    raw = json.dumps(payload).encode() + b"\n"
    if len(raw) > MAX_REQUEST:
        raise RuntimeError("Native-backtest request is too large")
    if not hasattr(socket, "AF_UNIX"):
        raise RuntimeError("Isolated backtest broker is unavailable; execution was refused")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout + 30)
            client.connect(str(SOCKET))
            client.sendall(raw)
            with client.makefile("rb") as stream:
                response = json.loads(stream.readline(MAX_RESPONSE))
    except (OSError, ValueError) as exc:
        raise RuntimeError("Isolated backtest broker is unavailable; execution was refused") from exc
    if "error" in response:
        raise RuntimeError(response["error"])
    if response.get("timed_out"):
        raise subprocess.TimeoutExpired("isolated native backtest", timeout, response["stdout"], response["stderr"])
    return subprocess.CompletedProcess("isolated native backtest", response["returncode"], response["stdout"], response["stderr"])


if __name__ == "__main__":
    if sys.argv[1:] == ["--broker"]:
        serve()
    elif len(sys.argv) == 5 and sys.argv[1] in {"--child", "--probe-child", "--timeout-child"}:
        run, home = Path(sys.argv[2]), Path(sys.argv[3])
        raw_imports = json.loads(sys.argv[4])
        if not isinstance(raw_imports, list) or any(not isinstance(path, str) for path in raw_imports):
            raise SystemExit("Invalid isolated input directories")
        abi = _apply_child_boundary(run, home, [Path(path) for path in raw_imports])
        if sys.argv[1] == "--timeout-child":
            _timeout_probe_child(run)
        elif sys.argv[1] == "--probe-child":
            _probe_child(run, home, abi)
        else:
            # The only production entry point, identical to BacktestTool's
            # native invocation. Import resolution cannot use client paths.
            _disable_child_dotenv()
            import runpy
            sys.path.insert(0, str(AGENT))
            sys.argv = [str(AGENT / "backtest" / "runner.py"), str(run)]
            runpy.run_path(sys.argv[0], run_name="__main__")
    else:
        raise SystemExit("Unsupported isolated execution mode")
