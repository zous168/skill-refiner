from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import itertools

from .common import RefinerError, relative_path


def command_forms(command, root=None, literal=False):
    """Regexes describe the command, not its platform-added `sh -lc '...'` quoting."""
    forms = [command]
    try:
        words = shlex.split(command)
    except ValueError:
        return forms
    if len(words) >= 3 and Path(words[0]).name in {"sh", "bash", "zsh", "dash", "ksh", "fish"}:
        if words[1].startswith("-") and "c" in words[1]:
            forms.append(words[2])
    if literal:
        return forms
    # A shell list separator can immediately follow an argument (`file.json;`).
    # Match the observed simple command independently of that separator, while
    # retaining quoted semicolons inside arguments. Never execute the text.
    for form in list(forms):
        try:
            lexer = shlex.shlex(form, posix=True, punctuation_chars=";&|\n")
            lexer.whitespace = ' \t\r'
            lexer.whitespace_split = True
            lexer.commenters = ""
            group, groups = [], []
            for token in lexer:
                if token and set(token) <= set(';&|\n'):
                    if group:
                        groups.append(group)
                        group = []
                else:
                    group.append(token)
            if group:
                groups.append(group)
            for group in groups:
                forms.append(shlex.join(group))
                if root is None:
                    continue
                options = []
                base = Path(root).resolve()
                for token in group:
                    aliases = [token]
                    path = Path(token)
                    try:
                        resolved = (path if path.is_absolute() else base / path).resolve()
                        if resolved.is_relative_to(base) and resolved.is_file():
                            relative = resolved.relative_to(base).as_posix()
                            aliases += [str(resolved), relative, "./" + relative]
                    except (OSError, ValueError):
                        pass
                    options.append(list(dict.fromkeys(aliases)))
                # Bound expansion for long shell commands. Every alias points
                # to an existing file inside this actual case workspace.
                for variant in itertools.islice(itertools.product(*options), 64):
                    forms.append(shlex.join(variant))
        except ValueError:
            pass
    return forms


def parse_json_output(text):
    text = text.strip()
    if text.startswith("```"):
        text = "\n".join(text.splitlines()[1:-1])
    return json.loads(text)


def parse_json_sequence(text):
    decoder, values, offset = json.JSONDecoder(), [], 0
    while text[offset:].strip():
        offset += len(text[offset:]) - len(text[offset:].lstrip())
        value, offset = decoder.raw_decode(text, offset)
        values.append(value)
    return values


def check_one(check, response, root, transcript=None):
    kind, expected = check["type"], check["value"]
    actual = response
    try:
        if kind in {"tool_command", "tool_output_matches"}:
            if transcript is None:
                transcript_path = os.environ.get("EVAL_TRANSCRIPT_PATH")
                if not transcript_path or not Path(transcript_path).is_file():
                    return False, "Execution transcript unavailable; cannot prove command execution"
                transcript = json.loads(Path(transcript_path).read_text())
            commands, matched_ids, matched_order, outputs = [], set(), [], {}

            def visit(value):
                if isinstance(value, list):
                    for entry in value:
                        visit(entry)
                elif isinstance(value, dict):
                    if value.get("role") == "tool_call":
                        call = value.get("tool_call", {})
                        args = call.get("arguments", {})
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except ValueError:
                                args = {}
                        # skill-up's Codex rollout parser wraps native function-call JSON
                        # in arguments.arguments, while its stdout parser emits command directly.
                        if isinstance(args, dict) and isinstance(args.get("arguments"), str):
                            try:
                                args = json.loads(args["arguments"])
                            except ValueError:
                                args = {}
                        command = args.get("command") if isinstance(args, dict) else None
                        if command is None and call.get("name", "").split(".")[-1] == "exec_command" and isinstance(args, dict):
                            command = args.get("cmd")
                        if isinstance(command, str):
                            commands.append(command)
                            if any(re.search(str(expected), form) for form in command_forms(
                                    command, root, check.get("literal_command", False))):
                                call_id = call.get("id")
                                matched_ids.add(call_id)
                                if call_id is not None and call_id not in matched_order:
                                    matched_order.append(call_id)
                    if value.get("role") == "tool_result":
                        result = value.get("tool_result", {})
                        if result.get("status") == "success":
                            outputs[result.get("call_id")] = result.get("content", "")
                    for key, entry in value.items():
                        if key not in {"content", "arguments"}:
                            visit(entry)
            visit(transcript)
            if kind == "tool_command":
                return bool(matched_ids), f"Observed tool commands: {commands!r}"
            matching_outputs = [outputs[i] for i in matched_order if i in outputs]
            for output in matching_outputs:
                # Match parsed JSON values for structured replies, otherwise exact stripped text.
                try:
                    actual_json = parse_json_output(response)
                    try:
                        output_json = parse_json_output(output)
                    except ValueError:
                        candidates = []
                        for line in output.splitlines():
                            try:
                                candidates.append(json.loads(line))
                            except ValueError:
                                pass
                        if not candidates:
                            continue
                        # Shell tools may include a preceding input-file dump. The
                        # final structured output must still be the forwarded value.
                        output_json = candidates[-1]
                    if actual_json == output_json:
                        return True, "Final JSON equals successful matching tool output"
                except ValueError:
                    if response.strip() == output.strip():
                        return True, "Final text equals successful matching tool output"
            # JSON streams have the same insignificant formatting as one JSON
            # value. Preserve all recorded values and their order/count; never
            # require an invented newline between complete business outputs.
            for output in matching_outputs:
                try:
                    if parse_json_sequence(response) and parse_json_sequence(response) == parse_json_sequence(output):
                        return True, 'Final JSON stream equals successful matching tool output'
                except ValueError:
                    pass
            if len(matching_outputs) == len(matched_order) and matching_outputs:
                try:
                    actual_values = [value for output in matching_outputs for value in parse_json_sequence(output)]
                    if parse_json_sequence(response) and parse_json_sequence(response) == actual_values:
                        return True, 'Final JSON stream equals all ordered complete matching outputs'
                except ValueError:
                    pass
            # A task can request several separate helper invocations and forward
            # each complete stdout in order. Require every matching invocation to
            # have a successful paired result; never fill missing/failed outputs
            # from the assistant's answer or assemble partial JSON fragments.
            if (len(matching_outputs) > 1 and len(matching_outputs) == len(matched_order)
                    and all(isinstance(output, str) for output in matching_outputs)):
                combined = "\n".join(output.rstrip("\r\n") for output in matching_outputs)
                if response.strip() == combined.strip():
                    return True, "Final text equals ordered complete outputs of all matching successful calls"
            return False, f"Final answer differs from matching tool output: {matching_outputs!r}"
        if kind.startswith("file_"):
            p = relative_path(root, check["path"])
            if kind == "file_exists":
                return p.is_file(), f"file exists: {check['path']}"
            actual = p.read_bytes().decode("utf-8")
        if kind in {"contains", "file_contains"}:
            passed = str(expected) in actual
        elif kind == "not_contains":
            passed = str(expected) not in actual
        elif kind == "regex":
            passed = re.search(str(expected), actual) is not None
        elif kind == "file_text_equal":
            passed = actual == str(expected)
        elif kind == "text_equal":
            passed = actual.strip() == str(expected).strip()
        elif kind == 'json_sequence_equal':
            values = parse_json_sequence(actual)
            passed = isinstance(expected, list) and values == expected
            actual = values
        elif kind in {"json_equal", "file_json_equal", "json_path_equal", "file_json_path_equal"}:
            actual = parse_json_output(actual)
            if kind in {"json_path_equal", "file_json_path_equal"}:
                for token in check.get("pointer", "").strip("/").split("/"):
                    if not token:
                        continue
                    token = token.replace("~1", "/").replace("~0", "~")
                    actual = actual[int(token)] if isinstance(actual, list) else actual[token]
            passed = actual == expected
        else:
            raise RefinerError(f"Unknown checker: {kind}")
        return passed, f"{kind}: expected={expected!r}; actual={str(actual)[:1500]!r}"
    except (ValueError, KeyError, IndexError, OSError, TypeError, re.error, RefinerError) as exc:
        return False, f"{kind}: {exc}"


def grade(case, response, root):
    assertions = []
    for check in case["checks"]:
        passed, evidence = check_one(check, response, root)
        assertions.append({"check": check, "passed": passed, "evidence": evidence})
    return {"case_id": case["id"], "passed": all(a["passed"] for a in assertions), "assertions": assertions}


def validate_plan(plan, root, max_cases=8):
    """Validate real coverage, oracle paths and discriminating output assertions."""
    from .schemas import PLAN
    import jsonschema
    jsonschema.validate(plan, PLAN)
    if len(plan["cases"]) > max_cases:
        raise RefinerError(f"Plan exceeds {max_cases} cases")
    requirements = {r["id"]: r for r in plan["requirements"]}
    if len(requirements) != len(plan["requirements"]):
        raise RefinerError("Duplicate requirement IDs")
    covered, ids = set(), set()
    for case in plan["cases"]:
        if case["id"] in ids:
            raise RefinerError(f"Duplicate case ID: {case['id']}")
        ids.add(case["id"])
        if not set(case["requirement_ids"]) <= requirements.keys():
            raise RefinerError(f"Unknown requirement reference: {case['id']}")
        covered.update(case["requirement_ids"])
        for name in case["files"]:
            if name == 'refiner-python-probe.py':
                raise RefinerError('Fixture cannot replace the frozen Python observer')
            relative_path(root, name)
            if (name.split("/")[0] in {".codex", ".agents", ".refiner", "evals", ".cursor", ".claude"}
                    or Path(name).name in {"AGENTS.md", "CLAUDE.md", ".cursorrules", ".mcp.json"}
                    or name.endswith("SKILL.md")):
                raise RefinerError(f"Fixture cannot inject Skill instructions or oracles: {name}")
        for check in case["checks"]:
            if check["type"].startswith("file_"):
                relative_path(root, check.get("path", ""))
            if check["type"] in {"regex", "tool_command", "tool_output_matches"}:
                try:
                    pattern = re.compile(check["value"])
                except (re.error, TypeError) as exc:
                    raise RefinerError(f"Invalid checker regex: {case['id']}: {exc}") from exc
                if pattern.search(""):
                    raise RefinerError(f"Checker regex matches empty evidence: {case['id']}")
            if check["type"] in {"contains", "not_contains", "file_contains"} and not str(check["value"]):
                raise RefinerError(f"Empty content check: {case['id']}")
        substantive = [c for c in case["checks"] if c["type"] not in {"file_exists", "tool_command", "tool_output_matches"}]
        if not substantive:
            raise RefinerError(f"Case {case['id']} only tests file/command existence, not task results")
        response_checks = [c for c in substantive if not c["type"].startswith("file_")]
        if response_checks:
            if not all(check_one(c, case["good_response"], root)[0] for c in response_checks):
                raise RefinerError(f"Good-response control rejected by {case['id']}")
            if all(check_one(c, case["bad_response"], root)[0] for c in response_checks):
                raise RefinerError(f"Bad-response control passes {case['id']}")
        for check in case["checks"]:
            if check["type"] != "tool_output_matches":
                continue
            command = check.get("control_command", "")
            if not command or not re.search(str(check["value"]), command):
                raise RefinerError(f"Missing/nonmatching tool control command: {case['id']}")
            def trace(output):
                return [{"role": "tool_call", "tool_call": {"id": "control", "name": "command_execution",
                         "arguments": {"command": command}}},
                        {"role": "tool_result", "tool_result": {"call_id": "control", "status": "success", "content": output}}]
            if not check_one(check, case["good_response"], root, trace(case["good_response"]))[0]:
                raise RefinerError(f"Good execution trace rejected: {case['id']}")
            if check_one(check, case["good_response"], root, trace(case["bad_response"]))[0]:
                raise RefinerError(f"Corrected/hallucinated answer passes tool-output check: {case['id']}")
        import tempfile
        for check in substantive:
            if not check["type"].startswith("file_"):
                continue
            with tempfile.TemporaryDirectory() as control_root:
                p = relative_path(control_root, check["path"])
                p.parent.mkdir(parents=True, exist_ok=True)
                value = check['value']
                if check['type'] == 'file_json_path_equal':
                    tokens = [t.replace('~1','/').replace('~0','~') for t in check.get('pointer','').strip('/').split('/') if t]
                    for token in reversed(tokens):
                        if token.isdigit():
                            index = int(token)
                            if index > 1000:
                                raise RefinerError('Control JSON pointer index too large')
                            value = [None]*index+[value]
                        else:
                            value = {token:value}
                content = json.dumps(value) if check['type'] in {'file_json_equal','file_json_path_equal'} else str(value)
                p.write_bytes(content.encode('utf-8'))
                if not check_one(check, "", control_root)[0]:
                    raise RefinerError(f"Good file control rejected: {case['id']}")
                p.unlink()
                if check_one(check, "", control_root)[0]:
                    raise RefinerError(f"Missing file passes content check: {case['id']}")
    uncovered = [r["id"] for r in plan["requirements"] if r["required"] and r["id"] not in covered]
    if uncovered:
        raise RefinerError(f"Uncovered required behaviors: {uncovered}")
    kinds = {c["kind"] for c in plan["cases"]}
    if "normal" not in kinds or not kinds & {"boundary", "error", "negative"}:
        raise RefinerError("Plan needs a normal case and at least one boundary/error/negative case")


def grader_main(case_path):
    from .common import read_json
    case = read_json(case_path)
    result = grade(case, os.environ.get("EVAL_FINAL_MESSAGE", ""), Path.cwd())
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["passed"] else 1

