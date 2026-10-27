from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import itertools
import tempfile

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


def json_pointer_tokens(pointer):
    if pointer == "":
        return []
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("JSON pointer must be empty or start with '/'")
    return [token.replace("~1", "/").replace("~0", "~") for token in pointer[1:].split("/")]


def json_pointer_get(value, pointer):
    for token in json_pointer_tokens(pointer):
        value = value[int(token)] if isinstance(value, list) else value[token]
    return value


def json_pointer_set(value, pointer, replacement):
    tokens = json_pointer_tokens(pointer)
    if not tokens:
        return replacement
    current = value
    for index, token in enumerate(tokens[:-1]):
        next_token = tokens[index + 1]
        if isinstance(current, list):
            slot = int(token)
            while len(current) <= slot:
                current.append(None)
            if current[slot] is None:
                current[slot] = [] if next_token.isdigit() else {}
            current = current[slot]
        else:
            current = current.setdefault(token, [] if next_token.isdigit() else {})
    final = tokens[-1]
    if isinstance(current, list):
        slot = int(final)
        while len(current) <= slot:
            current.append(None)
        current[slot] = replacement
    else:
        current[final] = replacement
    return value


def json_path_matches_file_pointer(check):
    value = check.get("value")
    if not isinstance(value, dict):
        raise ValueError("value must identify the evidence-file JSON pointer")
    file_pointer = value.get("file_pointer", value.get("pointer"))
    if "file_pointer" in value and "pointer" in value and value["file_pointer"] != value["pointer"]:
        raise ValueError("conflicting value.file_pointer and value.pointer")
    if not isinstance(file_pointer, str):
        raise ValueError("value.file_pointer (or value.pointer) is required")
    return file_pointer


def check_one(check, response, root, transcript=None):
    kind, expected = check["type"], check["value"]
    actual = response
    try:
        if kind in {"tool_command", "tool_command_absent", "tool_call_absent", "tool_output_matches"}:
            if transcript is None:
                transcript_path = os.environ.get("EVAL_TRANSCRIPT_PATH")
                if not transcript_path or not Path(transcript_path).is_file():
                    return False, "Execution transcript unavailable; cannot prove command execution"
                transcript = json.loads(Path(transcript_path).read_text())
            commands, tool_names, matched_ids, matched_order, results = [], [], set(), [], {}

            def visit(value):
                if isinstance(value, list):
                    for entry in value:
                        visit(entry)
                elif isinstance(value, dict):
                    if value.get("role") == "tool_call":
                        call = value.get("tool_call", {})
                        tool_name = call.get("name", "")
                        if isinstance(tool_name, str):
                            tool_names.append(tool_name)
                            if kind == "tool_call_absent" and re.search(str(expected), tool_name):
                                call_id = call.get("id")
                                matched_ids.add(call_id if call_id is not None else tool_name)
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
                            if kind != "tool_call_absent" and any(re.search(str(expected), form) for form in command_forms(
                                    command, root, check.get("literal_command", False))):
                                call_id = call.get("id")
                                matched_ids.add(call_id)
                                if call_id is not None and call_id not in matched_order:
                                    matched_order.append(call_id)
                    if value.get("role") == "tool_result":
                        result = value.get("tool_result", {})
                        results[result.get("call_id")] = result
                    for key, entry in value.items():
                        if key not in {"content", "arguments"}:
                            visit(entry)
            visit(transcript)
            if kind == "tool_command":
                return bool(matched_ids), f"Observed tool commands: {commands!r}"
            if kind == "tool_command_absent":
                return not matched_ids, ("No observed tool command matched the forbidden pattern"
                                         if not matched_ids else
                                         f"Forbidden tool commands were observed: {commands!r}")
            if kind == "tool_call_absent":
                return not matched_ids, ("No observed tool call name matched the forbidden pattern"
                                         if not matched_ids else
                                         f"Forbidden tool calls were observed: {tool_names!r}")
            expected_exit_code = check.get("expected_exit_code")
            matching_results = []
            for call_id in matched_order:
                result = results.get(call_id)
                if not isinstance(result, dict):
                    continue
                if expected_exit_code is None:
                    if result.get("status") == "success":
                        matching_results.append(result)
                elif (result.get("exit_code") == expected_exit_code
                      and result.get("status") == ("success" if expected_exit_code == 0 else "error")):
                    matching_results.append(result)
            matching_outputs = [item.get("content", "") for item in matching_results]
            if expected_exit_code is not None and len(matching_results) != len(matched_order):
                return False, (f"Expected exit code {expected_exit_code} from every matching command; observed "
                               f"{[results.get(i, {}).get('exit_code') for i in matched_order]!r}")
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
                        return True, (f"Final JSON equals matching tool output with exit code {expected_exit_code}"
                                      if expected_exit_code is not None else
                                      "Final JSON equals successful matching tool output")
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
            exit_context = f" at exit code {expected_exit_code}" if expected_exit_code is not None else ""
            return False, f"Final answer differs from matching tool output{exit_context}: {matching_outputs!r}"
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
        elif kind == "json_path_regex":
            actual = json_pointer_get(parse_json_output(actual), check["pointer"])
            if not isinstance(actual, str):
                return False, f"json_path_regex: pointer {check['pointer']!r} is not a string"
            passed = re.search(str(expected), actual) is not None
        elif kind == "file_text_equal":
            passed = actual == str(expected)
        elif kind == "text_equal":
            passed = actual.strip() == str(expected).strip()
        elif kind == 'json_sequence_equal':
            values = parse_json_sequence(actual)
            passed = isinstance(expected, list) and values == expected
            actual = values
        elif kind == "json_path_matches_file":
            response_value = json_pointer_get(parse_json_output(actual), check["pointer"])
            observed = json.loads(relative_path(root, check["path"]).read_text(encoding="utf-8"))
            source_pointer = json_path_matches_file_pointer(check)
            file_value = json_pointer_get(observed, source_pointer)
            passed = response_value == file_value
            actual = {"response": response_value, "file": file_value}
        elif kind in {"json_equal", "file_json_equal", "json_path_equal", "file_json_path_equal"}:
            actual = parse_json_output(actual)
            if kind in {"json_path_equal", "file_json_path_equal"}:
                actual = json_pointer_get(actual, check.get("pointer", ""))
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
    # Exact status/state labels are especially easy to invent when an agent
    # fabricates a JSON response contract for a conversational Skill. A
    # generated good_response is only a checker control, not evidence that
    # the target actually uses that label. Require the enum in the task prompt
    # or a documented source contract; implementation literals alone may
    # describe a CLI result rather than the evaluated agent's response.
    contract_text = []
    original_root = Path(root) / "original"
    if original_root.is_dir():
        for source_path in original_root.rglob("*"):
            if source_path.is_file() and source_path.suffix.lower() in {".md", ".yaml", ".yml", ".json"}:
                try:
                    contract_text.append(source_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError):
                    continue
    documented_contract = "\n".join(contract_text)

    def accepted_bad_response_checks(checks, response, check_root):
        """Name response oracles that still accept a deliberately wrong control."""
        matched = []
        for index, check in enumerate(checks, 1):
            passed, _ = check_one(check, response, check_root)
            if not passed:
                continue
            item = {"index": index, "type": check["type"]}
            if "pointer" in check:
                item["pointer"] = check["pointer"]
            if "value" in check:
                item["value"] = str(check["value"])[:240]
            matched.append(item)
        return matched

    requirements = {r["id"]: r for r in plan["requirements"]}
    if len(requirements) != len(plan["requirements"]):
        raise RefinerError("Duplicate requirement IDs")
    covered, ids = set(), set()
    for case in plan["cases"]:
        if case["id"] in ids:
            raise RefinerError(f"Duplicate case ID: {case['id']}")
        ids.add(case["id"])
        for field in ("scenario", "expected_outcome"):
            value = case.get(field, "")
            if not isinstance(value, str) or not value.strip():
                raise RefinerError(f"Case {case['id']} missing required {field}")
            if value.strip() == case["prompt"].strip():
                raise RefinerError(f"Case {case['id']} {field} must be separate from the executor prompt")
        if not set(case["requirement_ids"]) <= requirements.keys():
            raise RefinerError(f"Unknown requirement reference: {case['id']}")
        covered.update(case["requirement_ids"])
        linked_requirements = [requirements[item]["behavior"] for item in case["requirement_ids"]]
        boundary_contract = "\n".join([case["prompt"], *linked_requirements]).lower()
        if re.search(r"(?:invalid|malformed|bad|non.?utf|encoding|encoded).{0,35}(?:utf.?8|byte)|(?:utf.?8|byte).{0,35}(?:invalid|malformed|decode|encoding)", boundary_contract):
            override = re.compile(
                r"(?is)(?:\b(?:PYTHONIOENCODING|PYTHONUTF8|LC_ALL|LC_CTYPE|PYTHONCOERCECLOCALE)\b"
                r"\s*=\s*['\"][^'\"]+['\"]|['\"](?:PYTHONIOENCODING|PYTHONUTF8|LC_ALL|LC_CTYPE|"
                r"PYTHONCOERCECLOCALE)['\"]\s*:\s*['\"][^'\"]+['\"])")
            for filename, content in case["files"].items():
                if override.search(content):
                    raise RefinerError(
                        f"Boundary fixture {case['id']} overrides the runtime decoding environment in {filename}; "
                        "test invalid bytes under ordinary runtime defaults"
                    )
        for name in case["files"]:
            if name == 'refiner-python-probe.py':
                raise RefinerError('Fixture cannot replace the frozen Python observer')
            relative_path(root, name)
            if (name.split("/")[0] in {".codex", ".agents", ".refiner", "evals", ".cursor", ".claude"}
                    or Path(name).name in {"AGENTS.md", "CLAUDE.md", ".cursorrules", ".mcp.json"}
                    or name.endswith("SKILL.md")):
                raise RefinerError(f"Fixture cannot inject Skill instructions or oracles: {name}")
        for check in case["checks"]:
            if (check["type"] == "json_path_equal"
                    and check.get("pointer", "").rsplit("/", 1)[-1].lower() in {"status", "state"}
                    and isinstance(check.get("value"), str)
                    and check["value"] not in case["prompt"]
                    and not re.search(
                        rf"(?i)\bstatus\b[^\n]{{0,120}}\b{re.escape(check['value'])}\b",
                        documented_contract)):
                raise RefinerError(
                    f"Ungrounded exact status/state value in {case['id']}: {check['value']!r}; "
                    "state the enum in the prompt or ground it in the documented Skill contract"
                )
            if "expected_exit_code" in check and check["type"] != "tool_output_matches":
                raise RefinerError(f"expected_exit_code is only valid for tool_output_matches: {case['id']}")
            if "expected_exit_code" in check and (type(check["expected_exit_code"]) is not int
                                                    or check["expected_exit_code"] < 0):
                raise RefinerError(f"Invalid expected_exit_code: {case['id']}")
            if check.get("expected_exit_code") == 0:
                raise RefinerError(
                    f"Redundant expected_exit_code=0 in {case['id']}; successful tool-result status proves success, "
                    "and some adapters omit numeric exit codes"
                )
            if check["type"].startswith("file_"):
                relative_path(root, check.get("path", ""))
            elif check["type"] == "json_path_matches_file":
                relative_path(root, check.get("path", ""))
                try:
                    json_pointer_tokens(check.get("pointer", ""))
                    file_pointer = json_path_matches_file_pointer(check)
                    json_pointer_tokens(file_pointer)
                except (AttributeError, TypeError, ValueError) as exc:
                    raise RefinerError(f"Invalid JSON pointer in {case['id']}: {exc}") from exc
            elif check["type"] == "json_path_regex":
                try:
                    json_pointer_tokens(check.get("pointer", ""))
                except (AttributeError, TypeError, ValueError) as exc:
                    raise RefinerError(f"Invalid JSON pointer in {case['id']}: {exc}") from exc
            if check["type"] in {"regex", "json_path_regex", "tool_command", "tool_command_absent", "tool_call_absent", "tool_output_matches"}:
                try:
                    pattern = re.compile(check["value"])
                except (re.error, TypeError) as exc:
                    raise RefinerError(f"Invalid checker regex: {case['id']}: {exc}") from exc
                if pattern.search(""):
                    raise RefinerError(f"Checker regex matches empty evidence: {case['id']}")
            if check["type"] in {"contains", "not_contains", "file_contains"} and not str(check["value"]):
                raise RefinerError(f"Empty content check: {case['id']}")
        substantive = [c for c in case["checks"] if c["type"] not in {
            "file_exists", "tool_command", "tool_command_absent", "tool_call_absent", "tool_output_matches"}]
        if not substantive:
            raise RefinerError(f"Case {case['id']} only tests file/command existence, not task results")
        response_checks = [c for c in substantive if not c["type"].startswith("file_")]
        if response_checks:
            positive_responses = [case["good_response"], *case.get("good_responses", [])]
            text_only_oracle = (
                any(c["type"] in {"contains", "not_contains", "regex", "text_equal"}
                    for c in response_checks)
                and not any(c["type"].startswith("json_") for c in response_checks)
            )
            if text_only_oracle and len(case.get("good_responses", [])) < 2:
                raise RefinerError(
                    f"Natural-language case {case['id']} needs at least two additional good-response paraphrase controls"
                )
            dynamic = [c for c in response_checks if c["type"] == "json_path_matches_file"]
            if dynamic:
                for response_index, response in enumerate(positive_responses, 1):
                    try:
                        good = parse_json_output(response)
                        with tempfile.TemporaryDirectory() as control_root:
                            observed_by_path = {}
                            for check in dynamic:
                                value = json_pointer_get(good, check["pointer"])
                                relative = check["path"]
                                observed = observed_by_path.setdefault(relative, {})
                                observed_by_path[relative] = json_pointer_set(
                                    observed, json_path_matches_file_pointer(check), value)
                            for relative, observed in observed_by_path.items():
                                path = relative_path(control_root, relative)
                                path.parent.mkdir(parents=True, exist_ok=True)
                                path.write_text(json.dumps(observed), encoding="utf-8")
                            if not all(check_one(c, response, control_root)[0]
                                       for c in response_checks):
                                raise RefinerError(
                                    f"Good-response control {response_index} rejected by {case['id']}"
                                )
                    except (KeyError, TypeError, ValueError) as exc:
                        raise RefinerError(f"Invalid dynamic file control {response_index} in {case['id']}: {exc}") from exc
                try:
                    good = parse_json_output(positive_responses[0])
                    with tempfile.TemporaryDirectory() as control_root:
                        observed_by_path = {}
                        for check in dynamic:
                            value = json_pointer_get(good, check["pointer"])
                            relative = check["path"]
                            observed = observed_by_path.setdefault(relative, {})
                            observed_by_path[relative] = json_pointer_set(
                                observed, json_path_matches_file_pointer(check), value)
                        for relative, observed in observed_by_path.items():
                            path = relative_path(control_root, relative)
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_text(json.dumps(observed), encoding="utf-8")
                        if all(check_one(c, case["bad_response"], control_root)[0]
                               for c in response_checks):
                            matched = accepted_bad_response_checks(response_checks, case["bad_response"],
                                                                  control_root)
                            raise RefinerError(
                                f"Bad-response control passes {case['id']}; response checks that still match: "
                                + json.dumps(matched, ensure_ascii=False)
                            )
                except (KeyError, TypeError, ValueError) as exc:
                    raise RefinerError(f"Invalid dynamic file control in {case['id']}: {exc}") from exc
            else:
                for response_index, response in enumerate(positive_responses, 1):
                    if not all(check_one(c, response, root)[0] for c in response_checks):
                        raise RefinerError(f"Good-response control {response_index} rejected by {case['id']}")
                if all(check_one(c, case["bad_response"], root)[0] for c in response_checks):
                    matched = accepted_bad_response_checks(response_checks, case["bad_response"], root)
                    raise RefinerError(
                        f"Bad-response control passes {case['id']}; response checks that still match: "
                        + json.dumps(matched, ensure_ascii=False)
                    )
        for check in case["checks"]:
            if check["type"] == "tool_command_absent":
                command = check.get("control_command", "")
                if not command or not re.search(str(check["value"]), command):
                    raise RefinerError(f"Missing/nonmatching forbidden-command control: {case['id']}")
                if not check_one(check, case["good_response"], root, [] )[0]:
                    raise RefinerError(f"Good no-command trace rejected: {case['id']}")
                trace = [{"role": "tool_call", "tool_call": {"id": "forbidden", "name": "command_execution",
                          "arguments": {"command": command}}}]
                if check_one(check, case["good_response"], root, trace)[0]:
                    raise RefinerError(f"Forbidden-command control not detected: {case['id']}")
            if check["type"] == "tool_call_absent":
                tool_name = check.get("control_tool_name", "")
                if not tool_name or not re.search(str(check["value"]), tool_name):
                    raise RefinerError(f"Missing/nonmatching forbidden-tool-name control: {case['id']}")
                if not check_one(check, case["good_response"], root, [])[0]:
                    raise RefinerError(f"Good no-tool-call trace rejected: {case['id']}")
                trace = [{"role": "tool_call", "tool_call": {"id": "forbidden", "name": tool_name,
                          "arguments": {}}}]
                if check_one(check, case["good_response"], root, trace)[0]:
                    raise RefinerError(f"Forbidden tool-name control not detected: {case['id']}")
            if check["type"] != "tool_output_matches":
                continue
            command = check.get("control_command", "")
            if not command or not re.search(str(check["value"]), command):
                raise RefinerError(f"Missing/nonmatching tool control command: {case['id']}")
            def trace(output):
                exit_code = check.get("expected_exit_code", 0)
                return [{"role": "tool_call", "tool_call": {"id": "control", "name": "command_execution",
                         "arguments": {"command": command}}},
                        {"role": "tool_result", "tool_result": {"call_id": "control",
                         "status": "success" if exit_code == 0 else "error", "exit_code": exit_code,
                         "content": output}}]
            if not check_one(check, case["good_response"], root, trace(case["good_response"]))[0]:
                raise RefinerError(f"Good execution trace rejected: {case['id']}")
            if check_one(check, case["good_response"], root, trace(case["bad_response"]))[0]:
                raise RefinerError(f"Corrected/hallucinated answer passes tool-output check: {case['id']}")
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

