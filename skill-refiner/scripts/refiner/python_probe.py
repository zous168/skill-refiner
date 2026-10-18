#!/usr/bin/env python3
"""Observe an actual Python script; no business answers or grading decisions."""
import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import sys
import traceback


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', required=True)
    parser.add_argument('--track', action='append', default=[])
    parser.add_argument('--protect-inputs', action='store_true')
    parser.add_argument('--deny-network', action='store_true')
    parser.add_argument('--deny-subprocess', action='store_true')
    parser.add_argument('script')
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    script = Path(args.script).resolve()
    evidence = Path(args.evidence).resolve()
    tracked = [Path(p).resolve() for p in args.track]
    if evidence in tracked or evidence == script:
        raise ValueError('Evidence must not replace the script or tracked inputs')
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    before = {str(path): sha(path) for path in tracked}
    source_hash = sha(script)
    writes, network, subprocesses = [], [], []
    active = False

    def audit(event, values):
        if not active:
            return
        if event == 'open':
            path, mode, flags = values
            writing = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if writing:
                resolved = str(Path(os.fsdecode(path)).resolve()) if isinstance(path, (str, bytes, os.PathLike)) else repr(path)
                writes.append({'event':event,'path':resolved})
                if args.protect_inputs and resolved in before:
                    raise PermissionError('Attempt to write a protected input')
        elif event in {'os.remove','os.rename','os.rmdir','os.truncate','os.chmod'}:
            paths = [str(Path(p).resolve()) for p in values[:2] if isinstance(p, (str, os.PathLike))]
            writes.append({'event':event,'paths':paths})
            if args.protect_inputs and any(p in before for p in paths):
                raise PermissionError('Attempt to modify a protected input')
        elif event.startswith('socket.'):
            network.append({'event':event,'arguments':repr(values)[:500]})
            if args.deny_network:
                raise PermissionError('Network use is disallowed for this observed task')
        elif event in {'subprocess.Popen','os.system','os.exec','os.posix_spawn','os.fork'}:
            subprocesses.append({'event':event,'arguments':repr(values)[:500]})
            if args.deny_subprocess:
                raise PermissionError('Subprocess would escape Python-level observation')

    sys.addaudithook(audit)
    stdout, stderr, exit_code = io.StringIO(), io.StringIO(), 0
    old_argv = sys.argv
    old_path = sys.path[:]
    try:
        sys.argv = [str(script), *args.arguments]
        sys.path.insert(0, str(script.parent))
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            active = True
            try:
                runpy.run_path(str(script), run_name='__main__')
            except SystemExit as exc:
                exit_code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
            except BaseException:
                exit_code = 1
                traceback.print_exc()
            finally:
                active = False
    finally:
        sys.argv = old_argv
        sys.path[:] = old_path
    after = {str(path):sha(path) for path in tracked}
    try:
        parsed = json.loads(stdout.getvalue())
    except ValueError:
        parsed = None
    result = {'script':str(script),'script_sha256':source_hash,'argv':[str(script),*args.arguments],
              'exit_code':exit_code,'stdout':stdout.getvalue(),'stdout_json':parsed,'stderr':stderr.getvalue(),
              'input_sha256_before':before,'input_sha256_after':after,'inputs_unchanged':before == after,
              'write_events':writes,'network_events':network,'subprocess_events':subprocesses,
              'observation_scope':'Python audit events during runpy execution; not OS isolation'}
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    # The observer transport succeeds even when business code fails. The grader
    # must assert evidence.exit_code; never mistake the observer's code for it.
    sys.stdout.write(stdout.getvalue())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
