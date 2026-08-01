"""
eval_loop_config.py — optional loop.config.json loader.

If loop.config.json exists at the project root, it overrides defaults.
Missing file → pure defaults, no error, no file created.
Malformed JSON or invalid values → ValueError with a clear message.

Type contract for allowed_tools:
    Post-load invariant: every ``allowed_tools`` value in the returned
    config is a comma-separated *string*. Lists of strings are accepted
    as input sugar only and are joined with commas at load time; a list
    containing non-string elements raises ValueError.

Evaluator isolation (by design):
    The evaluator always runs isolated with no tools. Any non-empty
    ``allowed_tools.evaluator`` value is ignored with a stderr warning
    and forced to "" in the returned config, so the config surface and
    runtime behavior can never diverge.

Comment keys:
    Keys starting with "_" (e.g. "_comment") are permitted at the top
    level and inside models/allowed_tools; they are never merged and
    never warned about, enabling self-documenting config files.
"""

import json
import sys
from pathlib import Path

def _warn(msg: str) -> None:
    print(f"[config] warning: {msg}", file=sys.stderr)

DEFAULTS = {
    "max_iterations": 10,
    "models": {
        "planner": "",
        "generator": "",
        "evaluator": "",
    },
    "allowed_tools": {
        "planner": "Read,Grep,Glob",
        "generator": "Read,Write,Edit,Bash(python3 -m pytest*),Bash(python3 -m py_compile*),Bash(python3 -c*),Bash(python -m pytest*),Bash(python -m py_compile*),Bash(ls*),Bash(cat*),Bash(rm *),Bash(find *),Bash(bash*),Bash(chmod*)",
        "evaluator": "",
    },
}


def load_config(path) -> dict:
    """Load loop.config.json and deep-merge over DEFAULTS. Returns a full config dict.

    Post-load invariant: every value in the returned ``allowed_tools`` dict
    is a comma-separated string — lists in the input file are accepted as
    sugar only and joined with commas here. ``allowed_tools.evaluator`` is
    always "" in the returned config: the evaluator runs isolated by design,
    and any non-empty value is stripped with a stderr warning.
    """
    import copy
    cfg = copy.deepcopy(DEFAULTS)
    p = Path(path)
    if not p.exists():
        return cfg
    try:
        user = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"loop.config.json: malformed JSON — {e}") from e
    validate(user)
    # Deep merge. Keys starting with "_" (comments) are never merged.
    if "max_iterations" in user:
        cfg["max_iterations"] = user["max_iterations"]
    if "models" in user:
        for role, val in user["models"].items():
            if role in {"planner", "generator", "evaluator"}:
                cfg["models"][role] = val
    if "allowed_tools" in user:
        for k, v in user["allowed_tools"].items():
            if k not in {"planner", "generator", "evaluator"}:
                continue
            if isinstance(v, list):
                if not all(isinstance(x, str) for x in v):
                    raise ValueError(f"loop.config.json: allowed_tools.{k} list elements must all be strings")
                cfg["allowed_tools"][k] = ",".join(v)
            else:
                cfg["allowed_tools"][k] = v
    # Evaluator isolation: the evaluator never receives tools. A non-empty
    # value (already normalized to a string above) is stripped with a warning.
    if cfg["allowed_tools"]["evaluator"]:
        _warn(
            "allowed_tools.evaluator is ignored by design — the evaluator "
            "runs isolated with no tools; forcing empty"
        )
        cfg["allowed_tools"]["evaluator"] = ""
    return cfg


def validate(cfg: dict) -> None:
    """Raise ValueError with a precise message on any invalid value."""
    if not isinstance(cfg, dict):
        raise ValueError("loop.config.json: top level must be a JSON object")
    known = {"max_iterations", "models", "allowed_tools"}
    unknown = {k for k in cfg if k not in known and not k.startswith("_")}
    if unknown:
        _warn(f"unknown keys ignored: {sorted(unknown)}")
    if "max_iterations" in cfg:
        v = cfg["max_iterations"]
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise ValueError(f"loop.config.json: max_iterations must be int >= 1, got {v!r}")
    if "models" in cfg:
        if not isinstance(cfg["models"], dict):
            raise ValueError("loop.config.json: models must be an object")
        for role, name in cfg["models"].items():
            if role.startswith("_"):
                continue  # comment key, ignored silently
            if role not in {"planner", "generator", "evaluator"}:
                _warn(f"unknown model role ignored: {role!r}")
            elif not isinstance(name, str):
                raise ValueError(f"loop.config.json: models.{role} must be a string, got {name!r}")
    if "allowed_tools" in cfg:
        if not isinstance(cfg["allowed_tools"], dict):
            raise ValueError("loop.config.json: allowed_tools must be an object")
        for role, tools in cfg["allowed_tools"].items():
            if role.startswith("_"):
                continue  # comment key, ignored silently
            if role not in {"planner", "generator", "evaluator"}:
                _warn(f"unknown allowed_tools role ignored: {role!r}")
                continue
            if not isinstance(tools, (str, list)):
                raise ValueError(f"loop.config.json: allowed_tools.{role} must be a string or list")
