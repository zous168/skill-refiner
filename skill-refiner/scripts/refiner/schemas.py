"""Internal contracts, deliberately separate from skill-up's native YAML schema."""

CHECK = {
    "type": "object", "additionalProperties": False,
    "required": ["id", "type", "value"],
    "properties": {
        "id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$"},
        "type": {"enum": ["contains", "contains_any", "not_contains", "not_contains_any", "regex", "text_equal", "tool_command", "tool_command_absent", "tool_call_absent", "tool_output_matches", "json_equal", "json_sequence_equal", "json_path_equal", "json_path_regex",
                           "json_path_matches_file", "file_exists", "file_contains", "file_text_equal", "file_json_equal", "file_json_path_equal"]},
        "value": {}, "path": {"type": "string"}, "pointer": {"type": "string"},
        "control_command": {"type": "string"}, "control_tool_name": {"type": "string"},
        "expected_exit_code": {"type": "integer", "minimum": 0},
        "literal_command": {"type": "boolean"},
    },
    "allOf": [{
        "if": {"properties": {"type": {"enum": ["contains_any", "not_contains_any"]}}},
        "then": {"properties": {"value": {"type": "array", "minItems": 2,
                                                "uniqueItems": True,
                                                "items": {"type": "string", "minLength": 1}}}},
    }, {
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
ASSET_AUDIT = {
    "type": "object", "additionalProperties": False,
    "required": ["status", "required_paths", "available_paths", "source_refs", "basis"],
    "properties": {
        "status": {"enum": ["ready", "not_required", "missing"]},
        "required_paths": {"type": "array", "uniqueItems": True, "items": {"type": "string"}},
        "available_paths": {"type": "array", "uniqueItems": True, "items": {"type": "string"}},
        "source_refs": {"type": "array", "uniqueItems": True, "items": {"type": "string"}},
        "basis": {"type": "string", "minLength": 12},
    },
}
POSITIVE_CONTROL = {
    "type": "object", "additionalProperties": False,
    "required": ["id", "response"],
    "properties": {
        "id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$"},
        "response": {"type": "string"},
    },
}
NEGATIVE_CONTROL = {
    "type": "object", "additionalProperties": False,
    "required": ["id", "assertion_id", "response", "rationale"],
    "properties": {
        "id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$"},
        "assertion_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$"},
        "response": {"type": "string"},
        "rationale": {"type": "string", "minLength": 12},
    },
}
CASE = {
    "type": "object", "additionalProperties": False,
    "required": ["id", "requirement_ids", "kind", "scenario", "expected_outcome", "prompt", "files", "checks",
                 "helpers", "asset_audit", "positive_controls", "negative_controls",
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
        "asset_audit": ASSET_AUDIT,
        "positive_controls": {"type": "array", "minItems": 1, "items": POSITIVE_CONTROL},
        "negative_controls": {"type": "array", "minItems": 1, "items": NEGATIVE_CONTROL},
        "checks": {"type": "array", "minItems": 1, "items": CHECK},
        "oracle_reason": {"type": "string", "minLength": 12},
        "good_response": {"type": "string"},
        "good_responses": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
        "bad_response": {"type": "string"},
    },
}
PLAN = {
    "type": "object", "additionalProperties": False,
    "required": ["schema_version", "requirements", "cases", "coverage_gaps", "assumptions"],
    "properties": {
        "schema_version": {"const": "skill-refiner.plan/v2"},
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
                         "expected_outcome_consistent", "machine_checks_consistent",
                         "matrix_complete", "asset_audit_consistent",
                         "positive_controls_validated", "negative_controls_targeted", "basis"],
            "properties": {"case_id": {"type": "string"},
                           "installed_source_unchanged": {"type": "boolean"},
                           "fixture_contract_consistent": {"type": "boolean"},
                           "response_contract_consistent": {"type": "boolean"},
                           "trace_checks_observable": {"type": "boolean"},
                           "scenario_consistent": {"type": "boolean"},
                           "requirement_trace_consistent": {"type": "boolean"},
                           "expected_outcome_consistent": {"type": "boolean"},
                           "machine_checks_consistent": {"type": "boolean"},
                           "matrix_complete": {"type": "boolean"},
                           "asset_audit_consistent": {"type": "boolean"},
                           "positive_controls_validated": {"type": "boolean"},
                           "negative_controls_targeted": {"type": "boolean"},
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

