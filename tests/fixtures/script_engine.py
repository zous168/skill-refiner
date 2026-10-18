"""skill-up custom engine for protocol integration: executes the real target script."""
import json
from pathlib import Path
import subprocess
import sys
import shlex

request = json.loads(Path(sys.argv[1]).read_text())
scripts = list(Path.cwd().rglob("scripts/total.py"))
if len(scripts) != 1:
    raise RuntimeError(f"Expected exactly one installed target script; got {scripts}")
argv = [sys.executable, str(scripts[0]), "input.json"]
if Path('refiner-python-probe.py').exists():
    argv = [sys.executable, 'refiner-python-probe.py', '--evidence', 'observed.json', '--track', 'input.json',
            '--protect-inputs', '--deny-network', '--deny-subprocess', str(scripts[0]), 'input.json']
result = subprocess.run(argv, capture_output=True, text=True)
payload = {"exit_code": result.returncode, "final_message": result.stdout.strip(),
           "stderr": result.stderr, "transcript": [
               {"role": "tool_call", "tool_call": {"id": "exec-1", "name": "command_execution", "arguments": {
                   "command": shlex.join(argv)}}},
               {"role": "tool_result", "tool_result": {"call_id": "exec-1", "status": "success", "content": result.stdout}},
               {"role": "assistant", "content": result.stdout.strip()}]}
output = Path(sys.argv[2])
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(payload))

