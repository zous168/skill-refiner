from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time


class RefinerError(Exception):
    pass


class Cancelled(RefinerError):
    pass


IGNORED = {".git", ".venv", "node_modules", "__pycache__", ".tools", ".refiner-runs"}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def relative_path(root, value):
    root = Path(root).resolve()
    p = Path(value)
    if p.is_absolute() or not p.parts or ".." in p.parts or "\\" in str(value):
        raise RefinerError(f"Unsafe relative path: {value}")
    target = root / p
    if not target.resolve().is_relative_to(root):
        raise RefinerError(f"Path escapes workspace: {value}")
    return target


def files(root):
    root = Path(root)
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if any(part in IGNORED for part in rel.parts):
            continue
        if p.is_symlink():
            raise RefinerError(f"Symlink requires explicit materialization first: {p}")
        if p.is_file():
            yield p


def manifest(root):
    root = Path(root)
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files(root)}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def snapshot(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists() or destination.is_relative_to(source):
        raise RefinerError("Snapshot must be a new directory outside the target Skill")
    destination.mkdir(parents=True)
    for path in files(source):
        dest = destination / path.relative_to(source)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)


def inventory(root, max_bytes=250_000, priority_paths=()):
    root = Path(root)
    records, used = [], 0
    priority = {Path(item).as_posix() for item in priority_paths}
    ordered = sorted(files(root), key=lambda item: (str(item.relative_to(root)) not in priority,
                                                    str(item.relative_to(root))))
    for path in ordered:
        size = path.stat().st_size
        raw = path.read_bytes()
        record = {"path": str(path.relative_to(root)), "bytes": size,
                  "sha256": hashlib.sha256(raw).hexdigest()}
        try:
            text = raw.decode("utf-8")
        except (UnicodeError, OSError):
            record["omitted"] = "binary"
        else:
            if used + size > max_bytes:
                record["omitted"] = "context limit; unresolved dependency if required"
            else:
                record["content"] = text
                used += size
        records.append(record)
    return records


def kill_process_tree(pid):
    """Nested adapters may create their own process groups; stop them too."""
    if os.name == "nt":
        try:
            subprocess.run(["taskkill.exe", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, text=True, timeout=5, check=False,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        return
    descendants = {pid}
    try:
        listing = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, timeout=5, check=True)
        pairs = [tuple(map(int, line.split())) for line in listing.stdout.splitlines() if len(line.split()) == 2]
        while True:
            expanded = descendants | {child for child, parent in pairs if parent in descendants}
            if expanded == descendants:
                break
            descendants = expanded
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    for child in descendants - {pid}:
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_process(argv, *, cwd=None, input_text=None, timeout=300, log=None, env=None,
                on_progress=None, progress_interval=30):
    """Kill the entire process group on timeout, including nested agent processes."""
    started = time.monotonic()
    proc = subprocess.Popen([str(x) for x in argv], cwd=cwd, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True, env=env)
    deadline = started + timeout
    pending_input = input_text
    try:
        while True:
            remaining = max(0, deadline - time.monotonic())
            wait_for = min(remaining, max(1, progress_interval)) if on_progress else remaining
            try:
                stdout, stderr = proc.communicate(pending_input, timeout=wait_for)
                break
            except subprocess.TimeoutExpired:
                pending_input = None
                elapsed = time.monotonic() - started
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                if on_progress:
                    try:
                        on_progress(round(elapsed, 1), round(remaining, 1))
                    except Exception:
                        # Progress reporting must never affect or leak the child process.
                        pass
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        kill_process_tree(proc.pid)
        try:
            # A descendant can keep the inherited stdout/stderr pipes open even
            # after the parent was terminated. Never let timeout cleanup wait on
            # those pipes indefinitely.
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired as cleanup_exc:
            stdout = cleanup_exc.output or getattr(exc, "output", None) or ""
            stderr = cleanup_exc.stderr or getattr(exc, "stderr", None) or ""
            try:
                proc.kill()
            except (OSError, ProcessLookupError):
                pass
            try:
                proc.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        if log:
            Path(log).parent.mkdir(parents=True, exist_ok=True)
            Path(log).write_text(stdout + "\n" + stderr, encoding="utf-8")
        if isinstance(exc, KeyboardInterrupt):
            raise Cancelled(f"Process interrupted: {argv[0]}") from exc
        raise RefinerError(f"Process timed out after {timeout}s: {argv[0]}") from exc
    if log:
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        Path(log).write_text(stdout + "\nSTDERR\n" + stderr, encoding="utf-8")
    return proc.returncode, stdout, stderr, round(time.monotonic() - started, 3)

