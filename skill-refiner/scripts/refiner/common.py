from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import time
from queue import Empty, Queue


class RefinerError(Exception):
    pass


class PlanGateError(RefinerError):
    """The generated plan or its controls/review failed before Skill evaluation."""


class PlanValidationError(RefinerError):
    """A generated plan failed self-check, optionally with completed control evidence."""

    def __init__(self, message, evidence):
        super().__init__(message)
        self.evidence = evidence


class PlanControlError(PlanValidationError):
    """A complete plan-control pass ran but one or more controls failed."""


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


def _run_streaming_process(argv, *, cwd=None, input_text=None, timeout=None, log=None, env=None,
                           on_progress=None, on_output=None, progress_interval=30):
    started = time.monotonic()
    proc = subprocess.Popen([str(x) for x in argv], cwd=cwd, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace",
                            bufsize=1, start_new_session=True, env=env)
    output_queue = Queue()
    streams_done = set()
    stdout_parts, stderr_parts = [], []

    def pump(stream_name, stream):
        try:
            for line in iter(stream.readline, ""):
                output_queue.put((stream_name, line))
        finally:
            try:
                stream.close()
            except OSError:
                pass
            output_queue.put((stream_name, None))

    def feed_stdin():
        try:
            if input_text is not None:
                proc.stdin.write(input_text)
                proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    readers = [threading.Thread(target=pump, args=("stdout", proc.stdout), daemon=True),
               threading.Thread(target=pump, args=("stderr", proc.stderr), daemon=True)]
    writer = threading.Thread(target=feed_stdin, daemon=True)
    for thread in readers:
        thread.start()
    writer.start()

    deadline = None if timeout is None else started + timeout
    next_progress = started + max(1, progress_interval)
    timed_out = False
    interrupted = False
    try:
        while True:
            now = time.monotonic()
            remaining = None if deadline is None else max(0.0, deadline - now)
            wait_for = max(0.05, next_progress - now)
            wait_for = min(wait_for, 0.2)  # Poll process exit even when no output is emitted.
            if remaining is not None:
                wait_for = min(wait_for, remaining)
            try:
                stream_name, chunk = output_queue.get(timeout=wait_for)
                if chunk is None:
                    streams_done.add(stream_name)
                else:
                    (stdout_parts if stream_name == "stdout" else stderr_parts).append(chunk)
                    if on_output:
                        try:
                            on_output(stream_name, chunk)
                        except Exception:
                            pass
            except Empty:
                pass

            now = time.monotonic()
            if now >= next_progress:
                if on_progress:
                    try:
                        on_progress(round(now - started, 1),
                                    None if deadline is None else round(max(0.0, deadline - now), 1))
                    except Exception:
                        pass
                next_progress = now + max(1, progress_interval)
            if deadline is not None and now >= deadline:
                timed_out = True
                break
            if proc.poll() is not None and len(streams_done) == 2 and output_queue.empty():
                break
    except KeyboardInterrupt:
        interrupted = True

    if timed_out or interrupted:
        kill_process_tree(proc.pid)
        try:
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass
        for thread in readers + [writer]:
            thread.join(timeout=1)
        while True:
            try:
                stream_name, chunk = output_queue.get_nowait()
            except Empty:
                break
            if chunk is not None:
                (stdout_parts if stream_name == "stdout" else stderr_parts).append(chunk)
    else:
        proc.wait()
        writer.join(timeout=1)
        for thread in readers:
            thread.join(timeout=1)

    stdout, stderr = "".join(stdout_parts), "".join(stderr_parts)
    if log:
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        Path(log).write_text(stdout + "\nSTDERR\n" + stderr, encoding="utf-8")
    if interrupted:
        raise Cancelled(f"Process interrupted: {argv[0]}")
    if timed_out:
        raise RefinerError(f"Process timed out after {timeout}s: {argv[0]}")
    return proc.returncode, stdout, stderr, round(time.monotonic() - started, 3)


def run_process(argv, *, cwd=None, input_text=None, timeout=300, log=None, env=None,
                on_progress=None, on_output=None, progress_interval=30):
    """Run a process; stream output when requested and only kill it for an explicit timeout."""
    if timeout is None or on_output is not None:
        return _run_streaming_process(argv, cwd=cwd, input_text=input_text, timeout=timeout,
                                      log=log, env=env, on_progress=on_progress,
                                      on_output=on_output, progress_interval=progress_interval)
    started = time.monotonic()
    proc = subprocess.Popen([str(x) for x in argv], cwd=cwd, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace",
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

