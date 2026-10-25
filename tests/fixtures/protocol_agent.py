"""Deterministic test double for the planner protocol. Never used as a live LLM claim."""
import json
import os
import sys


def case(identifier, values, expected, kind):
    return {"id": identifier, "requirement_ids": ["R-total"] if kind == "normal" else ["R-empty"],
            "kind": kind, "prompt": "Use the installed decimal-total script on input.json and return its JSON verbatim.",
            "files": {"input.json": json.dumps({"amounts": values})},
            "checks": [{"type": "json_equal", "value": {"total": expected}},
                       {"type": "tool_output_matches", "value": r"python.*scripts/total\.py",
                        "control_command": "python3 scripts/total.py input.json"}],
            "oracle_reason": "External monetary requirements demand exact base-ten sum, including an empty sum of zero.",
            "good_response": json.dumps({"total": expected}), "bad_response": '{"total":"999.99"}'}


request = json.load(sys.stdin)
role, payload = request["role"], request["input"]
if role in {"plan", "acceptance-plan"}:
    suffix = "accept" if role == "acceptance-plan" else "dev"
    response = {"requirements": [
        {"id": "R-total", "behavior": "Exact signed decimal total", "source": "references/requirements.md", "required": True},
        {"id": "R-empty", "behavior": "Empty total is zero", "source": "references/requirements.md", "required": True}],
        "cases": [case(suffix+"-cents", ["0.11", "0.22"] if suffix == "dev" else ["1.25", "-0.10"],
                       "0.33" if suffix == "dev" else "1.15", "normal"),
                  case(suffix+"-empty", [], "0.00", "boundary")],
        "coverage_gaps": [], "assumptions": []}
    if role == "acceptance-plan":
        response["requirements"] = payload["contract"]
        if os.environ.get("REFINER_TEST_ACCEPT_FAIL") or os.environ.get("REFINER_TEST_ACCEPT_FIX"):
            expected = "999.99" if os.environ.get("REFINER_TEST_ACCEPT_FAIL") else "0.11"
            response["cases"][0] = case("accept-large", ["100000000000000000000000000000.11", "-100000000000000000000000000000"], expected, "normal")
            response["cases"][0]["bad_response"] = '{"total":"0.00"}'
elif role in {"plan-review", "standards-review"}:
    response = {"approved": True, "issues": []}
    if role == "plan-review":
        response["case_contracts"] = [{"case_id": c["id"], "installed_source_unchanged": True,
            "fixture_contract_consistent": True,
            "response_contract_consistent": True, "trace_checks_observable": True,
            "basis": "Fixture supplies only ledger data; the installed script reads it without changing its source."}
            for c in payload["plan"]["cases"]]
elif role == "repair":
    if any(str(item).startswith("acceptance::") for item in payload.get("allowed_evidence_ids", [])):
        if os.environ.get("REFINER_TEST_ACCEPT_FAIL"):
            response = {"diagnosis": "The frozen acceptance expectation cannot be justified by the target contract.",
                        "changes": [], "unresolved": ["Acceptance evidence conflicts with its frozen oracle."]}
        else:
            script = next(f["content"] for f in payload["files"] if f["path"] == "scripts/total.py")
            script = script.replace("import json", "import json\nfrom decimal import Decimal, localcontext")
            script = script.replace("total = sum(float(value) for value in values)",
                                    "with localcontext() as context:\n"
                                    "        context.prec = max(80, sum(len(value) for value in values) + 4)\n"
                                    "        total = sum((Decimal(value) for value in values), Decimal('0'))")
            case_id = next(x for x in payload["allowed_evidence_ids"] if x.startswith("acceptance::"))
            response = {"diagnosis": "The acceptance execution reproduced float precision loss on large decimal inputs.",
                        "changes": [{"path": "scripts/total.py", "content": script,
                                     "reason": "Use exact decimal arithmetic for the reproduced monetary total.",
                                     "evidence_ids": [case_id]}], "unresolved": []}
        print(json.dumps(response))
        raise SystemExit(0)
    script = next(f["content"] for f in payload["files"] if f["path"] == "scripts/total.py")
    # Two separate fixes ensure the iteration controller really performs multiple rounds.
    partial = "sum(int(float(value))" in script
    if partial:
        script = script.replace("import json", "import json\nfrom decimal import Decimal")
        script = script.replace("sum(int(float(value)) for value in values)", "sum((Decimal(value) for value in values), Decimal('0'))")
        case_id = "dev-cents"
    else:
        script = script.replace('if not values:', 'if False and not values:')
        case_id = "dev-empty"
    # Matrix evidence IDs retain the engine identity; cite a real failing pair.
    case_id = next((x for x in payload.get("allowed_evidence_ids", [])
                    if x == case_id or x.endswith("::" + case_id)), case_id)
    response = {"diagnosis": "Repair one observed defect per round", "changes": [
        {"path": "scripts/total.py", "content": script, "reason": "Correct a reproduced violation of the monetary requirements", "evidence_ids": [case_id]}],
        "unresolved": ["Empty input still violates the zero-total contract"] if partial else []}
else:
    raise SystemExit("Unknown role: " + role)
print(json.dumps(response))

