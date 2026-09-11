#!/usr/bin/env python3
"""Discover what each agent CLI is actually configured to use, right now.

Model names rot. `gpt-5.5` became `gpt-5.6-luna`; `claude-opus-4-7` became
`claude-opus-5`. A skill that hardcodes them is stale the week it ships, so this
script reads the CLIs' own configuration instead and prints JSON.

Read, never write. Every source is optional: a missing or malformed file yields
`null`/`[]` for that field and a note in `warnings`, never an exception. The
caller's job is to offer the discovered values to the user — not to assume they
are complete.

Sources
-------
codex   ~/.codex/config.toml
          model                          -> current default
          model_reasoning_effort         -> current default effort
          [tui.model_availability_nux]   -> models the TUI advertised as available
          [profiles.<name>]              -> named model/effort presets
claude  ~/.claude/settings.json          -> model (alias or pinned id)
        ~/.claude.json                   -> model ids seen in session history

Claude aliases (`opus`, `sonnet`, `haiku`) are deliberately preferred over pinned
ids: `claude --help` documents --model as taking "an alias for the latest model",
so an alias tracks updates on its own and never needs this script to be right.

Usage: python discover_models.py [--json]
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Claude aliases that resolve to "latest of that tier" and therefore survive
# model releases without edits here.
CLAUDE_ALIASES = ["opus", "sonnet", "haiku"]

# Effort levels each CLI accepts. Orca forwards --effort only for Claude, Codex,
# and Cursor, and only when --model is also passed.
CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
CODEX_EFFORTS = ["low", "medium", "high", "xhigh"]


def _load_toml(path: Path) -> tuple[dict, str | None]:
    """Parse TOML with stdlib tomllib. Returns ({}, reason) on any failure."""
    if not path.exists():
        return {}, f"{path} not found"
    try:
        import tomllib
    except ImportError:  # Python < 3.11
        return {}, "tomllib unavailable (needs Python 3.11+)"
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh), None
    except Exception as exc:
        return {}, f"{path} unreadable: {type(exc).__name__}"


def _load_json(path: Path) -> tuple[dict, str | None]:
    if not path.exists():
        return {}, f"{path} not found"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except Exception as exc:
        return {}, f"{path} unreadable: {type(exc).__name__}"


def discover_codex(home: Path) -> tuple[dict, list[str]]:
    cfg, warn = _load_toml(home / ".codex" / "config.toml")
    warnings = [warn] if warn else []

    # Models the TUI has advertised. Keys are model ids; values are view counts
    # we do not care about.
    nux = cfg.get("tui", {}).get("model_availability_nux", {})
    available = sorted(nux.keys()) if isinstance(nux, dict) else []

    profiles = {}
    raw_profiles = cfg.get("profiles", {})
    if isinstance(raw_profiles, dict):
        for name, body in raw_profiles.items():
            if not isinstance(body, dict):
                continue
            entry = {
                "model": body.get("model"),
                "effort": body.get("model_reasoning_effort"),
            }
            if entry["model"] or entry["effort"]:
                profiles[name] = entry

    return {
        "agent": "codex",
        "default_model": cfg.get("model"),
        "default_effort": cfg.get("model_reasoning_effort"),
        "advertised_models": available,
        "profiles": profiles,
        "efforts": CODEX_EFFORTS,
        "model_flag": "--model",
        "config_path": str(home / ".codex" / "config.toml"),
    }, warnings


def discover_claude(home: Path) -> tuple[dict, list[str]]:
    settings, warn_s = _load_json(home / ".claude" / "settings.json")
    warnings = [warn_s] if warn_s else []

    # Session history mentions concrete model ids. Useful as evidence of what the
    # account can reach, but aliases remain the better choice for the flag.
    seen: list[str] = []
    hist_path = home / ".claude.json"
    if hist_path.exists():
        try:
            raw = hist_path.read_text(encoding="utf-8", errors="replace")
            for m in re.findall(r'"model"\s*:\s*"([^"]+)"', raw):
                if m not in seen:
                    seen.append(m)
        except Exception as exc:
            warnings.append(f"{hist_path} unreadable: {type(exc).__name__}")

    return {
        "agent": "claude",
        "default_model": settings.get("model"),
        "default_effort": settings.get("effort"),
        "aliases": CLAUDE_ALIASES,
        "models_seen": seen,
        "efforts": CLAUDE_EFFORTS,
        "model_flag": "--model",
        "config_path": str(home / ".claude" / "settings.json"),
        "note": (
            "--model accepts an alias for the latest model of a tier; prefer "
            "opus/sonnet/haiku over a pinned id so updates need no edits."
        ),
    }, warnings


def main() -> int:
    home = Path.home()
    codex, w1 = discover_codex(home)
    claude, w2 = discover_claude(home)

    out = {
        "agents": {"codex": codex, "claude": claude},
        # Omitting --model/--effort entirely is the only truly update-proof
        # choice: the agent CLI then applies its own current default.
        "recommended": "omit --model/--effort to inherit each CLI's own default",
        "warnings": [w for w in (*w1, *w2) if w],
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
