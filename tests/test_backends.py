import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from refiner.agent import Agent
from refiner.checks import check_one
from refiner.common import RefinerError, manifest, snapshot, write_json, run_process
from refiner.matrix import Matrix
from refiner.pipeline import compare_candidate
from refiner.profiles import load_config
from refiner.runtime import native_rollouts, parse_codex, parse_cursor, run_api
from refiner.skillup import SkillUp
from refiner.execute import verify_skill

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures"


class ProfilesAndEvidence(unittest.TestCase):
    def test_agent_refuses_to_silently_select_a_vendor_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = Agent(Path(tmp) / "run")
            with self.assertRaisesRegex(RefinerError, "No agent backend configured"):
                agent.ask("plan", "return an object", {}, {"type": "object"})

    def test_cli_requires_explicit_adapter_and_generic_doctor_does_not_assume_codex(self):
        doctor = subprocess.run([sys.executable, "-m", "refiner.cli", "doctor"], cwd=ROOT,
                                text=True, capture_output=True)
        report = json.loads(doctor.stdout)
        self.assertTrue(report["backend_config_required"])
        self.assertNotIn("codex", report)
        run = subprocess.run([sys.executable, "-m", "refiner.cli", "run", "/missing-skill"], cwd=ROOT,
                             text=True, capture_output=True)
        self.assertEqual(run.returncode, 2)
        self.assertIn("Select a controller backend", run.stderr)

    def test_command_paths_are_equivalent_only_inside_the_actual_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            (root / "scripts/total.py").write_text("# installed script")
            (root / "input.json").write_text("{}")
            check = {"type": "tool_output_matches", "value": r"python3\s+\S*/scripts/total\.py\s+input\.json(?:\s|$)"}
            def trace(command):
                return [{"role": "tool_call", "tool_call": {"id": "c", "name": "shell", "arguments": {"command": command}}},
                        {"role": "tool_result", "tool_result": {"call_id": "c", "status": "success", "content": "42"}}]
            absolute = f"python3 {root}/scripts/total.py {root}/input.json"
            self.assertTrue(check_one(check, "42", root, trace(absolute))[0])
            self.assertTrue(check_one(check, "42", root, trace("python3 scripts/total.py input.json"))[0])
            self.assertFalse(check_one({**check, "literal_command": True}, "42", root, trace(absolute))[0])
            self.assertFalse(check_one(check, "42", root, trace(f"python3 {root}/scripts/total.py /unrelated/input.json"))[0])

    def test_timeout_kills_nested_adapter_process_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = "import time,pathlib;time.sleep(1);pathlib.Path('escaped.txt').write_text('still alive')"
            parent = "import subprocess,sys,time;subprocess.Popen([sys.executable,'-c'," + repr(child) + "],start_new_session=True);time.sleep(10)"
            with self.assertRaisesRegex(RefinerError, "timed out"):
                run_process([sys.executable, "-c", parent], cwd=root, timeout=0.3)
            time.sleep(1.1)
            self.assertFalse((root / "escaped.txt").exists())

    def test_executor_cannot_repair_the_skill_during_evaluation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skill"
            snapshot(ROOT / "examples/decimal-total", root)
            expected = manifest(root)
            (root / "task-output.json").write_text('{"allowed":"task artifact"}')
            verify_skill(root, expected)
            (root / "scripts/total.py").write_text('print("manually repaired")')
            with self.assertRaisesRegex(RefinerError, "source changed"):
                verify_skill(root, expected)

    def test_source_guard_error_preserves_completed_backend_evidence(self):
        import refiner.execute as adapter
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skill"
            snapshot(ROOT / "examples/decimal-total", root)
            expected = manifest(root)
            profile, request, output = [root / name for name in ("profile.json", "request.json", "result.json")]
            write_json(profile, {"backend": "codex", "_skill_manifest": expected})
            write_json(request, {"messages": [{"role": "user", "content": "Guard probe"}]})
            trace = [{"role": "tool_result", "tool_result": {"call_id": "observed", "status": "success", "content": "done"}}]
            def completed(*args):
                (root / "scripts/total.py").write_text('print("changed")')
                return {"exit_code": 0, "final_message": "done", "transcript": trace,
                        "artifacts": {"files": [{"name": "adapter-events.jsonl", "content": "completed backend events"}]},
                        "metadata": {"backend": "codex", "model": "recorded-model"}}
            with patch.object(adapter.Path, "cwd", return_value=root), patch.object(adapter, "execute", side_effect=completed), \
                    patch.object(adapter.sys, "argv", ["execute.py", str(profile), str(request), str(output)]):
                self.assertEqual(adapter.main(), 0)
            result = json.loads(output.read_text())
            self.assertEqual(result["exit_code"], 1)
            self.assertEqual(result["final_message"], "")
            self.assertEqual(result["transcript"], trace)
            metadata = json.loads(next(f["content"] for f in result["artifacts"]["files"] if f["name"] == "adapter-metadata.json"))
            self.assertEqual(metadata["model"], "recorded-model")
            self.assertEqual(metadata["failure_stage"], "post_execution_source_check")
            self.assertIn("source changed", metadata["error"])
            self.assertTrue(any(f["name"] == "adapter-events.jsonl" for f in result["artifacts"]["files"]))

    def test_shell_separator_does_not_hide_real_script_execution(self):
        check = {"type": "tool_output_matches", "value": r"\bpython3\s+\S*scripts/total\.py\s+empty\.json(?=\s|$)"}
        trace = [{"role": "tool_call", "tool_call": {"id": "x", "name": "shell", "arguments": {
                 "command": "python3 scripts/total.py empty.json; printf '[exit=0]'"}}},
                 {"role": "tool_result", "tool_result": {"call_id": "x", "status": "success", "content": '{"total":"0.00"}\n[exit=0]'}}]
        self.assertTrue(check_one(check, '{"total":"0.00"}', ROOT, trace)[0])
        trace[0]["tool_call"]["arguments"]["command"] = "python3 scripts/total.py 'empty.json;something'"
        self.assertFalse(check_one(check, '{"total":"0.00"}', ROOT, trace)[0])

    def test_config_rejects_duplicates_secrets_and_message_override(self):
        data = {"schema_version": 1, "executors": [{"id": "a", "backend": "codex"}]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            for bad in [dict(data, executors=data["executors"] * 2),
                        dict(data, agents={"repair": {"backend": "codex", "api_key": "secret"}}),
                        dict(data, agents={"repair": {"backend": "codex", "max_request_bytes": 100}}),
                        dict(data, executors=[{"id": "x", "backend": "openai_compatible", "model": "m",
                                             "base_url": "https://example.com/v1", "request_options": {"messages": []}}])]:
                write_json(path, bad)
                with self.assertRaises(RefinerError):
                    load_config(path)

    def test_oversized_agent_handoff_is_saved_and_rejected_before_backend_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "backend-was-run"
            script = root / "agent.py"
            script.write_text('from pathlib import Path; Path(' + repr(str(marker)) + ').write_text("yes")')
            config = {"agents": {"default": {"backend": "command", "command": [sys.executable, str(script)],
                                               "max_request_bytes": 16_384}}}
            agent = Agent(root / "run", config=config)
            with self.assertRaisesRegex(RefinerError, "exceeding the configured"):
                agent.ask("plan", "return a JSON object", {"source": "x" * 20_000}, {"type": "object"})
            call = root / "run/agent-calls/001-plan"
            request = json.loads((call / "request.json").read_text())
            manifest = json.loads((call / "request-manifest.json").read_text())
            transmission = (call / "transmission.txt").read_bytes()
            self.assertEqual(request["schema_version"], "skill-refiner.agent-request/v1")
            self.assertEqual(manifest["status"], "rejected_context_budget")
            self.assertGreater(manifest["transmitted_bytes"], manifest["max_request_bytes"])
            self.assertEqual(manifest["transmitted_bytes"], len(transmission))
            self.assertEqual(manifest["transmitted_sha256"], __import__("hashlib").sha256(transmission).hexdigest())
            self.assertFalse(marker.exists())

    def test_planning_repair_and_review_route_to_separate_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = {"agents": {}}
            for role in ("planning", "repair", "review"):
                script = tmp / (role + ".py")
                script.write_text('import json,sys\nr=json.load(sys.stdin)\nprint(json.dumps({"backend":' + repr(role) + ',"role":r["role"]}))\n')
                config["agents"][role] = {"backend": "command", "command": [sys.executable, str(script)]}
            agent = Agent(tmp / "run", config=config)
            for role, expected in [("plan", "planning"), ("acceptance-plan", "planning"),
                                   ("repair", "repair"), ("plan-review", "review"), ("standards-review", "review")]:
                result = agent.ask(role, "return JSON", {}, {"type": "object"})
                self.assertEqual(result, {"backend": expected, "role": role})
            for call in (tmp / "run/agent-calls").iterdir():
                request = json.loads((call / "request.json").read_text())
                transmitted = (call / "transmission.txt").read_bytes()
                saved = json.loads((call / "request-manifest.json").read_text())
                self.assertEqual(json.loads(transmitted), request)
                self.assertEqual(saved["transmitted_bytes"], len(transmitted))
                self.assertEqual(saved["transmitted_sha256"], __import__("hashlib").sha256(transmitted).hexdigest())

    def test_cursor_requires_observed_completed_tool_result(self):
        events = [{"type": "tool_call", "subtype": "started", "call_id": "x", "tool_call": {
            "shellToolCall": {"args": {"command": "python3 scripts/total.py input.json"}}}},
            {"type": "result", "subtype": "success", "result": '{"total":"1.00"}'}]
        check = {"type": "tool_output_matches", "value": "python.*total.py"}
        final, trace = parse_cursor(events)
        self.assertFalse(check_one(check, final, ROOT, trace)[0])
        events.insert(1, {"type": "tool_call", "subtype": "completed", "call_id": "x", "tool_call": {
            "shellToolCall": {"args": {"command": "python3 scripts/total.py input.json"},
                              "result": {"success": {"exitCode": 0, "stdout": final}}}}})
        final, trace = parse_cursor(events)
        self.assertTrue(check_one(check, final, ROOT, trace)[0])
        events[1]["tool_call"]["shellToolCall"]["result"]["success"]["exitCode"] = 1
        final, trace = parse_cursor(events)
        self.assertFalse(check_one(check, final, ROOT, trace)[0])

    def test_native_requires_spawn_completed_child_and_real_command_record(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"CODEX_HOME": ""}):
            os.environ["CODEX_HOME"] = tmp
            root = Path(tmp) / "sessions/2026/10/17"
            root.mkdir(parents=True)
            def save(path, records):
                path.write_text("\n".join(json.dumps(x) for x in records) + "\n")
            save(root / "root.jsonl", [{"type": "session_meta", "payload": {"id": "root"}},
                 {"type": "response_item", "payload": {"type": "function_call", "name": "spawn_agent"}}])
            child = [{"type": "session_meta", "payload": {"id": "child", "parent_thread_id": "root"}},
                     {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                         "type": "CommandExecution", "id": "cmd", "command": ["python3", "scripts/total.py"],
                         "exit_code": 0, "aggregated_output": '{"total":"1.00"}'}}}]
            save(root / "child.jsonl", child)
            with self.assertRaisesRegex(RefinerError, "did not complete"):
                native_rollouts("root", 0)
            child.append({"type": "event_msg", "payload": {"type": "task_complete"}})
            save(root / "child.jsonl", child)
            trace, evidence, _ = native_rollouts("root", 0)
            self.assertTrue(evidence[0]["completed"])
            self.assertTrue(check_one({"type": "tool_output_matches", "value": "python.*total.py"}, '{"total":"1.00"}', ROOT, trace)[0])

    def test_regression_is_protected_per_engine_case(self):
        best = {"cases": [{"id": "codex::a", "passed": True, "status": "PASS"},
                          {"id": "cursor::a", "passed": False, "status": "FAIL"},
                          {"id": "cursor::b", "passed": False, "status": "FAIL"}]}
        current = copy.deepcopy(best)
        for row in current["cases"]:
            row.update(passed=not row["passed"], status="FAIL" if row["passed"] else "PASS")
        accepted, regressions = compare_candidate(set(), best, [], current, [])
        self.assertFalse(accepted)
        self.assertEqual(regressions, ["codex::a"])

    def test_misrouted_judge_is_error_even_if_upstream_says_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = SkillUp(str(ROOT / ".tools/skill-up"), engine_config={"name": "test"})
            def process(argv, **kwargs):
                write_json(root / "upstream/iteration-1/result.json", {"case_results": [
                    {"case_id": "empty", "status": "PASS", "grading": {"assertion_results": [
                        {"evidence": json.dumps({"case_id": "cents", "passed": True})}]}}]})
                return 0, "", "", 0.01
            with patch("refiner.skillup.run_process", side_effect=process):
                result = backend._execute(root / "config.yaml", root, 1)
            self.assertFalse(result["passed"])
            self.assertEqual(result["cases"][0]["status"], "ERROR")
            self.assertEqual(result["effective_parallelism"], 1)


class CompatibleAPI(unittest.TestCase):
    """Local provider double; actual shell/file tools run, no live-model claim."""
    def test_tool_loop_runs_real_command_and_auth_stays_out_of_trace(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append((body, self.headers.get("Authorization")))
                if len(seen) == 1:
                    message = {"role": "assistant", "content": None, "tool_calls": [{"id": "shell-1", "type": "function",
                        "function": {"name": "shell", "arguments": json.dumps({"command": "python3 -c 'print(17+25)'"})}}]}
                else:
                    observed = json.loads(body["messages"][-1]["content"])["output"].strip()
                    message = {"role": "assistant", "content": observed}
                payload = json.dumps({"choices": [{"message": message}], "usage": {"total_tokens": 10}}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(payload)
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"REFINER_TEST_KEY": "do-not-record"}):
                result = run_api({"backend": "openai_compatible", "model": "provider-double", "api_key_env": "REFINER_TEST_KEY",
                                  "base_url": f"http://127.0.0.1:{server.server_port}/v1"}, "Calculate using the shell", tmp, 10)
                self.assertEqual(result["final_message"], "42")
                self.assertEqual(seen[0][1], "Bearer do-not-record")
                self.assertEqual(seen[0][0]["model"], "provider-double")
                self.assertTrue(check_one({"type": "tool_output_matches", "value": "python3"}, "42", tmp, result["transcript"])[0])
                self.assertNotIn("do-not-record", json.dumps(result))
        finally:
            server.shutdown(); server.server_close(); thread.join()


@unittest.skipUnless((ROOT / ".tools/skill-up").exists(), "Run bootstrap first")
class MultiEngineIntegration(unittest.TestCase):
    def test_real_skillup_matrix_two_round_repair_and_compare(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            command = [sys.executable, str(FIXTURE / "script_engine.py"), "{input_file}", "{output_file}"]
            config = {"schema_version": 1, "agents": {"default": {"backend": "command", "command":
                      [sys.executable, str(FIXTURE / "protocol_agent.py")]}}, "executors": [
                      {"id": "a", "backend": "command", "command": command},
                      {"id": "b", "backend": "command", "command": command}]}
            write_json(tmp / "config.json", config)
            target = ROOT / "examples/decimal-total"
            before = manifest(target)
            proc = subprocess.run([sys.executable, "-m", "refiner.cli", "run", str(target), "--config", str(tmp / "config.json"),
                "--output", str(tmp / "run"), "--timeout", "30", "--max-evaluations", "80"], capture_output=True, text=True, timeout=240)
            report = (tmp / "run/report.md").read_text()
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr + report)
            evidence = json.loads((tmp / "run/final/evidence.json").read_text())
            self.assertEqual(len(evidence["regression"]["engines"]), 2)
            self.assertTrue(evidence["acceptance"]["passed"])
            self.assertEqual(len(json.loads((tmp / "run/state.json").read_text())["rounds"]), 2)
            self.assertEqual(manifest(target), before)
            self.assertEqual(len({e["suite_sha256"] for e in evidence["regression"]["engines"]}), 1)
            # An unavailable engine must remain ERROR while the next engine still runs.
            config["executors"].insert(0, {"id": "missing", "backend": "codex", "executable": str(tmp / "not-installed")})
            write_json(tmp / "config.json", config)
            proc = subprocess.run([sys.executable, "-m", "refiner.cli", "compare", str(tmp / "run/final/decimal-total"),
                "--suite", str(tmp / "run"), "--config", str(tmp / "config.json"), "--output", str(tmp / "compare"), "--timeout", "20"],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            result = json.loads((tmp / "compare/regression/summary.json").read_text())
            self.assertTrue(all(r["status"] == "ERROR" for r in result["cases"] if r["executor"] == "missing"))
            self.assertTrue(all(r["passed"] for r in result["cases"] if r["executor"] in {"a", "b"}))


if __name__ == "__main__":
    unittest.main()
