"""Internal contracts, deliberately separate from skill-up's native YAML schema."""

CHECK = {
    "type": "object", "additionalProperties": False,
    "required": ["type", "value"],
    "properties": {
        "type": {"enum": ["contains", "not_contains", "regex", "text_equal", "tool_command", "tool_command_absent", "tool_call_absent", "tool_output_matches", "json_equal", "json_sequence_equal", "json_path_equal", "json_path_regex",
                           "json_path_matches_file", "file_exists", "file_contains", "file_text_equal", "file_json_equal", "file_json_path_equal"]},
        "value": {}, "path": {"type": "string"}, "pointer": {"type": "string"},
        "control_command": {"type": "string"}, "control_tool_name": {"type": "string"},
        "expected_exit_code": {"type": "integer", "minimum": 0},
        "literal_command": {"type": "boolean"},
    },
    "allOf": [{
        "if": {"properties": {"type": {"const": "json_path_matches_file"}}},
        "then": {
            "required": ["path", "pointer"],
            "properties": {"value": {
                "type": "object",
                "oneOf": [
                    {"required": ["file_pointer"], "properties": {"file_pointer": {"type": "string"}}},
                    {"required": ["pointer"], "properties": {"pointer": {"type": "string"}}},
                ],
            }},
        },
    }, {
        "if": {"properties": {"type": {"const": "json_path_regex"}}},
        "then": {"required": ["pointer"], "properties": {"value": {"type": "string"}}},
    }, {
        "if": {"properties": {"type": {"const": "tool_call_absent"}}},
        "then": {"required": ["control_tool_name"], "properties": {"value": {"type": "string"}}},
    }],
}
CASE = {
    "type": "object", "additionalProperties": False,
    "required": ["id", "requirement_ids", "kind", "scenario", "expected_outcome", "prompt", "files", "checks",
                 "oracle_reason", "good_response", "bad_response"],
    "properties": {
        "id": {"type": "string", "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$"},
        "requirement_ids": {"type": "array", "minItems": 1, "items": {"type": "string"}},
        "kind": {"enum": ["normal", "boundary", "error", "negative", "regression"]},
        "scenario": {"type": "string", "minLength": 1},
        "expected_outcome": {"type": "string", "minLength": 1},
        "prompt": {"type": "string", "minLength": 1},
        "files": {"type": "object", "additionalProperties": {"type": "string"}},
        "helpers": {"type": "array", "uniqueItems": True, "items": {"enum": ["python_probe"]}},
        "checks": {"type": "array", "minItems": 1, "items": CHECK},
        "oracle_reason": {"type": "string", "minLength": 12},
        "good_response": {"type": "string"},
        "good_responses": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
        "bad_response": {"type": "string"},
    },
}
PLAN = {
    "type": "object", "additionalProperties": False,
    "required": ["requirements", "cases", "coverage_gaps", "assumptions"],
    "properties": {
        "requirements": {"type": "array", "minItems": 1, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "behavior", "source", "required"],
            "properties": {"id": {"type": "string"}, "behavior": {"type": "string"},
                           "source": {"type": "string"}, "required": {"type": "boolean"}},
        }},
        "cases": {"type": "array", "minItems": 2, "items": CASE},
        "coverage_gaps": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
    },
}
REVIEW = {
    "type": "object", "additionalProperties": False,
    "required": ["approved", "issues"],
    "properties": {"approved": {"type": "boolean"}, "issues": {"type": "array", "items": {"type": "string"}}},
    "allOf": [{"if": {"properties": {"approved": {"const": False}}},
               "then": {"properties": {"issues": {"minItems": 1}}},
               "else": {"properties": {"issues": {"maxItems": 0}}}}],
}
PLAN_REVIEW = {
    **REVIEW,
    "required": ["approved", "issues", "case_contracts"],
    "properties": {**REVIEW["properties"], "case_contracts": {
        "type": "array", "minItems": 2, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["case_id", "installed_source_unchanged", "fixture_contract_consistent",
                         "response_contract_consistent", "trace_checks_observable",
                         "scenario_consistent", "requirement_trace_consistent",
                         "expected_outcome_consistent", "machine_checks_consistent", "basis"],
            "properties": {"case_id": {"type": "string"},
                           "installed_source_unchanged": {"type": "boolean"},
                           "fixture_contract_consistent": {"type": "boolean"},
                           "response_contract_consistent": {"type": "boolean"},
                           "trace_checks_observable": {"type": "boolean"},
                           "scenario_consistent": {"type": "boolean"},
                           "requirement_trace_consistent": {"type": "boolean"},
                           "expected_outcome_consistent": {"type": "boolean"},
                           "machine_checks_consistent": {"type": "boolean"},
                           "basis": {"type": "string", "minLength": 12}},
        },
    }},
}
REPAIR = {
    "type": "object", "additionalProperties": False,
    "required": ["diagnosis", "changes", "unresolved"],
    "properties": {
        "diagnosis": {"type": "string"}, "unresolved": {"type": "array", "items": {"type": "string"}},
        "changes": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["path", "content", "reason", "evidence_ids"],
            "properties": {"path": {"type": "string"}, "content": {"type": "string"},
                           "reason": {"type": "string", "minLength": 8},
                           "evidence_ids": {"type": "array", "minItems": 1, "items": {"type": "string"}}},
        }},
    },
}

