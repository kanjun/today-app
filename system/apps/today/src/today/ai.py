"""Headless ``claude -p`` completions for Today (stdlib only).

A trimmed copy of the workspace's keyless claude_p helper: no tools, an isolated
throwaway cwd (so repo CLAUDE.md/hooks never bleed in), no session persistence,
and MAIN_CLAUDE_SESSION_ID dropped from the child environment.
"""

import json
import os
import subprocess
import tempfile

DEFAULT_MODEL = "sonnet"


class ClaudeError(RuntimeError):
    pass


def _default_account_dir() -> str:
    root = os.path.join(os.path.expanduser("~"), ".minds", "accounts")
    try:
        with open(os.path.join(root, "index.json"), encoding="utf-8") as f:
            index = json.load(f)
        rows = [a for a in index.get("accounts", []) if a.get("lane") == "anthropic"]
        if not rows:
            return ""
        chosen = next((a for a in rows if a.get("id") == index.get("mru")), rows[0])
        path = os.path.join(root, str(chosen["id"]))
        return path if os.path.isdir(path) else ""
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    for var in (
        "MAIN_CLAUDE_SESSION_ID",
        "MNGR_AGENT_STATE_DIR",
        "MNGR_AGENT_NAME",
        "MNGR_HOST_DIR",
    ):
        env.pop(var, None)
    if not env.get("CLAUDE_CONFIG_DIR"):
        account_dir = _default_account_dir()
        if account_dir:
            env["CLAUDE_CONFIG_DIR"] = account_dir
    return env


def complete(
    prompt: str, *, system: str, model: str = DEFAULT_MODEL, timeout: int = 300
) -> str:
    argv = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--model",
        model,
        "--system-prompt",
        system,
        "--tools",
        "",
        "--no-session-persistence",
    ]
    with tempfile.TemporaryDirectory(prefix="today_claude_") as cwd:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            env=_child_env(),
            cwd=cwd,
            timeout=timeout,
            check=False,
        )
    if proc.returncode != 0:
        raise ClaudeError(
            f"claude -p exited {proc.returncode}: {(proc.stderr or proc.stdout)[:400]}"
        )
    try:
        data = json.loads(proc.stdout)
    except ValueError as e:
        raise ClaudeError(f"claude -p output not JSON: {e}") from e
    if (
        not isinstance(data, dict)
        or data.get("is_error")
        or data.get("subtype") != "success"
        or data.get("result") is None
    ):
        raise ClaudeError(f"claude -p error result: {str(data)[:400]}")
    return str(data["result"])


def complete_json(
    prompt: str, *, system: str, model: str = DEFAULT_MODEL, timeout: int = 300
) -> dict:
    """A completion whose answer is one JSON object (fenced or bare)."""
    text = complete(prompt, system=system, model=model, timeout=timeout)
    start = text.find("{")
    if start < 0:
        raise ClaudeError(f"no JSON object in answer: {text[:300]}")
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError as e:
        raise ClaudeError(f"answer JSON invalid: {e}: {text[:300]}") from e
    if not isinstance(value, dict):
        raise ClaudeError("answer JSON is not an object")
    return value
