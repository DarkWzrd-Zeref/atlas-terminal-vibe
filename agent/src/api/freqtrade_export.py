"""Export an explicit native Freqtrade artifact; never translate or run code."""
import base64
import hashlib
import json
import keyword
import os
from pathlib import Path
import re
import stat

MAX_FILE = 4 * 1024 * 1024
MAX_TOTAL = 16 * 1024 * 1024


class ExportError(ValueError):
    pass


def _read(path: Path, limit: int) -> bytes:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or path.is_symlink() or before.st_nlink != 1:
        raise ExportError("Only regular files inside this run may be exported.")
    if before.st_size > limit:
        raise ExportError("Export exceeds its size limit.")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ExportError("Export changed while opening it.")
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ExportError("Export exceeds its size limit.")
    return data


def export_freqtrade(runs_root: Path, run_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
        raise ExportError("Invalid run identifier.")
    # The configured runs root may intentionally be a persistent-volume symlink.
    # Every component below that trusted root must be an actual directory.
    root = runs_root.resolve(strict=True)
    for component in (run_id, "artifacts", "freqtrade"):
        root = root / component
        if root.is_symlink() or not stat.S_ISDIR(root.lstat().st_mode):
            raise ExportError("Export directories cannot be links or special files.")
    metadata = json.loads(_read(root / "strategy.json", 16384))
    if not isinstance(metadata, dict) or set(metadata) != {"strategy_class", "timeframe", "entrypoint", "semantic_notes", "dependencies"}:
        raise ExportError("strategy.json needs strategy_class, timeframe, entrypoint, semantic_notes and dependencies.")
    name, timeframe, entrypoint = (metadata[k] for k in ("strategy_class", "timeframe", "entrypoint"))
    if not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name) or len(name) > 128:
        raise ExportError("Invalid Freqtrade class name.")
    if not isinstance(timeframe, str) or not re.fullmatch(r"[1-9][0-9]{0,5}[smhdwMy]", timeframe):
        raise ExportError("Invalid candle timeframe.")
    if not isinstance(entrypoint, str) or not entrypoint.endswith(".py"):
        raise ExportError("An explicit Python entrypoint is required.")
    notes, deps = metadata["semantic_notes"], metadata["dependencies"]
    if not isinstance(notes, str) or not 1 <= len(notes) <= 2048:
        raise ExportError("Explain differences from the original research in semantic_notes.")
    if not isinstance(deps, list) or len(deps) > 32 or any(not isinstance(x, str) or not 1 <= len(x) <= 128 for x in deps):
        raise ExportError("Dependencies must be a bounded list of package names; they are not installed automatically.")
    records, payloads, pending, total, seen = [], [], [root], 0, set()
    entries = 0
    while pending:
        directory = pending.pop()
        for file in sorted(directory.iterdir()):
            entries += 1
            if entries > 512:
                raise ExportError("Too many export entries.")
            relative = file.relative_to(root).as_posix()
            if len(file.relative_to(root).parts) > 16 or len(relative.encode()) > 512:
                raise ExportError("Export path is too long.")
            info = file.lstat()
            if file.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ExportError("Export links are forbidden.")
            if stat.S_ISDIR(info.st_mode):
                pending.append(file)
                continue
            if relative == "strategy.json":
                continue
            if relative.casefold() in seen or len(records) >= 128:
                raise ExportError("Duplicate names or too many export files.")
            seen.add(relative.casefold())
            content = _read(file, min(MAX_FILE, MAX_TOTAL - total))
            total += len(content)
            records.append({"path": relative, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()})
            payloads.append({"path": relative, "content_base64": base64.b64encode(content).decode("ascii")})
    if entrypoint not in {x["path"] for x in records}:
        raise ExportError("The declared Python strategy is missing.")
    manifest = {"version": 1, "strategy_class": name, "timeframe": timeframe, "entrypoint": entrypoint,
                "provenance": {"kind": "vibe-export", "source": f"Vibe run {run_id}", "run_id": run_id,
                               "semantic_notes": notes, "dependencies": ", ".join(deps) or "standard Freqtrade image"},
                "files": sorted(records, key=lambda x: x["path"])}
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return {"manifest": manifest, "files": sorted(payloads, key=lambda x: x["path"]),
            "sha256": hashlib.sha256(canonical).hexdigest(), "executed": False}
