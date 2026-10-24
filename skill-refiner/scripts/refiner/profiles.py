"""Validated, independently selectable controller and evaluation backends."""
from __future__ import annotations

import copy
import re
from pathlib import Path

from .common import RefinerError, read_json, write_json

BACKENDS = {"codex", "cursor", "codex_subagent", "claude_code", "openai_compatible", "command", "skillup"}
ROLES = {"analyze": "planning", "analysis-review": "review", "draft": "creation",
         "plan": "planning", "acceptance-plan": "planning", "repair": "repair",
         "plan-review": "review", "standards-review": "review"}
FIELDS = {"id", "backend", "model", "executable", "command", "base_url", "api_key_env",
          "max_turns", "timeout", "max_request_bytes", "reasoning_effort", "child_model", "engine", "request_options"}


def validate_profile(profile, *, executor=False):
    if not isinstance(profile, dict) or set(profile) - FIELDS:
        raise RefinerError("Unknown backend profile fields; credentials must use api_key_env, never literal keys")
    p = copy.deepcopy(profile)
    if not isinstance(p.get("backend"), str) or p["backend"] not in BACKENDS:
        raise RefinerError(f"Unknown backend: {p.get('backend')}")
    if executor and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}", p.get("id", "")):
        raise RefinerError("Every executor needs a unique safe id (letters, digits, underscore, hyphen)")
    for key in ("max_turns", "timeout"):
        if key in p and (type(p[key]) is not int or p[key] < 1):
            raise RefinerError(f"Profile {key} must be a positive integer")
    if "max_request_bytes" in p and (type(p["max_request_bytes"]) is not int
                                      or not 16_384 <= p["max_request_bytes"] <= 10_000_000):
        raise RefinerError("Profile max_request_bytes must be an integer from 16384 to 10000000")
    for key in ("model", "executable", "base_url", "api_key_env", "reasoning_effort", "child_model"):
        if key in p and (not isinstance(p[key], str) or not p[key].strip()):
            raise RefinerError(f"Profile {key} must be a nonempty string")
    if p["backend"] == "openai_compatible":
        from urllib.parse import urlsplit
        url = urlsplit(p.get("base_url", ""))
        if url.scheme not in {"http", "https"} or not url.netloc or url.username or url.password or url.query or url.fragment:
            raise RefinerError("openai_compatible requires base_url without embedded credentials/query")
        if not p.get("model"):
            raise RefinerError("openai_compatible requires an explicit model")
        if "request_options" in p:
            options = p["request_options"]
            allowed = {"temperature", "max_tokens", "max_completion_tokens", "reasoning_effort", "seed", "top_p"}
            if not isinstance(options, dict) or set(options) - allowed:
                raise RefinerError("Unsupported request_options (model/messages/tools cannot be overridden)")
    if p["backend"] == "command":
        if not isinstance(p.get("command"), list) or not p["command"] or not all(isinstance(x, str) for x in p["command"]):
            raise RefinerError("command backend requires an argv list, not a shell string")
    if p["backend"] == "skillup" and (not executor or not isinstance(p.get("engine"), dict)):
        raise RefinerError("skillup backend is executor-only and requires an engine object")
    return p


def load_config(path):
    if not path:
        return None
    data = read_json(path)
    if not isinstance(data, dict) or set(data) - {"schema_version", "agents", "executors"} or data.get("schema_version") != 1:
        raise RefinerError("Backend config requires schema_version=1, agents and executors")
    agents = data.get("agents", {})
    if not isinstance(agents, dict) or set(agents) - {"default", "planning", "creation", "repair", "review"}:
        raise RefinerError("agents supports default, planning, creation, repair and review")
    data["agents"] = {role: validate_profile(p) for role, p in agents.items()}
    profiles = data.get("executors")
    if not isinstance(profiles, list) or not profiles:
        raise RefinerError("At least one executor is required")
    data["executors"] = [validate_profile(p, executor=True) for p in profiles]
    ids = [p["id"] for p in data["executors"]]
    if len(set(ids)) != len(ids):
        raise RefinerError("Executor ids must be unique")
    return data


def role_profile(config, role):
    agents = (config or {}).get("agents", {})
    fallback = agents.get("repair", agents.get("default")) if role == "draft" else agents.get("default")
    profile = agents.get(ROLES[role], fallback)
    if profile is None:
        raise RefinerError(f"No backend configured for role {role!r}; set its profile or agents.default")
    return profile


def skillup_engine(profile, directory):
    """All managed adapters retain upstream installation, judges and result reporting."""
    if profile["backend"] == "skillup":
        return profile["engine"]
    import sys
    path = Path(directory) / (profile["id"] + ".json")
    write_json(path, profile)
    runner = Path(__file__).with_name("execute.py").resolve()
    return {"name": "refiner-" + profile["id"],
            "custom": {"transport": "local", "response_format": "session_result",
                       "local": {"command": sys.executable,
                                 "args": [str(runner), str(path.resolve()), "${input_file}", "${output_file}"],
                                 "input_file": "${input_file}", "output_file": "${output_file}"}}}
