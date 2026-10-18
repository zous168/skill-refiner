#!/usr/bin/env python3
"""Install skill-refiner and its runtime for an Agent Skills-compatible host."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_SOURCE = PROJECT_ROOT / "skill-refiner"


def run(argv: list[str], *, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(str(part) for part in argv), flush=True)
    subprocess.run(argv, check=True, env=env)


def install() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-dir", required=True,
                        help="Destination skill directory for your agent, ending in skill-refiner")
    parser.add_argument("--config", required=True,
                        help="JSON backend configuration for planning, repair, review and evaluation")
    parser.add_argument("--runtime-dir", default=str(Path.home() / ".skill-refiner-runtime"),
                        help="Isolated runtime location (default: ~/.skill-refiner-runtime)")
    args = parser.parse_args()

    if sys.version_info < (3, 10):
        raise SystemExit("Python 3.10 or newer is required")
    if platform.system() not in {"Darwin", "Linux"}:
        raise SystemExit("The runtime installer currently supports macOS and Linux")
    if not (PROJECT_ROOT / "pyproject.toml").is_file() or not (SKILL_SOURCE / "SKILL.md").is_file():
        raise SystemExit("Run this script from a complete skill-refiner repository checkout")

    skill_dest = Path(args.skill_dir).expanduser().resolve()
    runtime = Path(args.runtime_dir).expanduser().resolve()
    config_source = Path(args.config).expanduser().resolve()
    if skill_dest.name != "skill-refiner":
        raise SystemExit("--skill-dir must name the final directory skill-refiner")
    if skill_dest == runtime or skill_dest in runtime.parents or runtime in skill_dest.parents:
        raise SystemExit("--skill-dir and --runtime-dir must be separate directories")
    if skill_dest.exists():
        raise SystemExit(f"Skill already exists: {skill_dest}\nMove it aside before reinstalling.")
    if runtime.exists():
        raise SystemExit(f"Runtime already exists: {runtime}\nMove it aside before reinstalling.")
    if not config_source.is_file():
        raise SystemExit(f"Backend config not found: {config_source}")

    try:
        backend_config = json.loads(config_source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Cannot read backend config JSON: {exc}") from exc

    # Do not silently inherit Codex as a fallback: all controller roles must be
    # deliberately selected by the user for a host- and provider-neutral install.
    required_roles = {"planning", "creation", "repair", "review"}
    agents = backend_config.get("agents") if isinstance(backend_config, dict) else None
    if not isinstance(agents, dict) or not required_roles.issubset(agents):
        raise SystemExit("Backend config must explicitly define agents.planning, creation, repair and review")

    runtime.mkdir(parents=True)
    completed = False
    try:
        venv_dir = runtime / "venv"
        tools_dir = runtime / "tools"
        config_path = runtime / "backends.json"
        python = venv_dir / "bin" / "python3"
        run([sys.executable, "-m", "venv", str(venv_dir)])
        if not python.exists():
            python = venv_dir / "bin" / "python"
        run([str(python), "-m", "pip", "install", "--disable-pip-version-check", str(PROJECT_ROOT)])
        run([str(python), "-m", "refiner.cli", "bootstrap", "--directory", str(tools_dir)])

        run([str(python), "-c", "from refiner.profiles import load_config; load_config(__import__('sys').argv[1])",
             str(config_source)])
        config_path.write_text(json.dumps(backend_config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        paths = [part for part in os.environ.get("PATH", "").split(os.pathsep) if part and Path(part).is_dir()]
        for profile in [*backend_config.get("agents", {}).values(), *backend_config.get("executors", [])]:
            executable = profile.get("executable") if isinstance(profile, dict) else None
            if executable:
                path = Path(executable).expanduser()
                if path.is_file():
                    paths.append(str(path.parent))
        path_entries = list(dict.fromkeys(paths))
        local_runtime = {
            "schema_version": 1,
            "python": str(python),
            "skill_up": str(tools_dir / "skill-up"),
            "config": str(config_path),
            "path_entries": path_entries,
            "limits": {"max_rounds": 3, "max_agent_calls": 24, "max_evaluations": 100,
                       "max_seconds": 3600, "timeout": 300},
        }
        skill_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(SKILL_SOURCE, skill_dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (skill_dest / "references" / "local-runtime.json").write_text(
            json.dumps(local_runtime, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        # Verify the profile schema and report readiness without making model calls.
        env = os.environ.copy()
        env["PATH"] = os.pathsep.join([*path_entries, env.get("PATH", "")])
        doctor = subprocess.run([str(python), "-m", "refiner.cli", "doctor", "--config", str(config_path)],
                                check=False, capture_output=True, text=True, env=env)
        if doctor.stdout:
            print("\nBackend readiness (availability check only):")
            print(doctor.stdout.strip())
        if doctor.stderr:
            print(doctor.stderr.strip(), file=sys.stderr)
        completed = True
    finally:
        if not completed:
            shutil.rmtree(runtime, ignore_errors=True)
            if skill_dest.exists():
                shutil.rmtree(skill_dest, ignore_errors=True)

    print(f"\nInstalled Skill: {skill_dest}")
    print(f"Installed runtime: {runtime}")
    if doctor.returncode:
        print("One or more configured backends are unavailable. Install/authenticate them or update backends.json, then run:")
        print(f"  {python} -m refiner.cli doctor --config {config_path}")
    print("The Skill is available in your agent's next turn. Describe the Skill to create or improve in natural language.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(install())
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode)
