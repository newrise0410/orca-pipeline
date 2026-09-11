#!/usr/bin/env python3
"""Discover what models each agent CLI can actually reach, right now.

Model names and effort levels rot fast. Hardcoding either is wrong within weeks:
`gpt-5.5` became `gpt-5.6-luna`; `claude-opus-4-7` became `claude-opus-5`; Claude
gained a `fable` alias; `gpt-5.6-sol` accepts an `ultra` effort that older models
do not. This script reads the CLIs' own state instead and prints JSON.

Read-only. Never raises: a missing or malformed source yields null/[] for that
field plus an entry in `warnings`. A caller must offer discovered values to the
user, never assume the list is complete, and never invent a model id.

Sources
-------
codex   <codex_home>/models_cache.json
          models[].slug / display_name / description
          models[].default_reasoning_level
          models[].supported_reasoning_levels[].effort   <- EFFORT IS PER MODEL
          models[].visibility == "hide"  -> internal, excluded
          models[].priority              -> lower sorts first
        <codex_home>/config.toml
          model, model_reasoning_effort, [profiles.<name>]

        codex_home is $CODEX_HOME when set, else ~/.codex. Orca redirects
        CODEX_HOME per account, so the same machine holds several caches with
        DIFFERENT model lists — an account may not see every model. Other homes
        are reported under `other_homes` for visibility, never merged in.

claude  `claude --help`
          --model description -> aliases (e.g. fable, opus, sonnet)
          --effort description -> effort levels
        ~/.claude/settings.json -> model

        Claude has no models cache. Aliases resolve to "latest of that tier", so
        an alias tracks releases on its own; prefer one over a pinned id.

Usage: python discover_models.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

HELP_TIMEOUT_S = 20

# Used only when `claude --help` cannot be parsed. Flagged in warnings when hit,
# so a stale fallback is visible rather than silently authoritative.
CLAUDE_ALIAS_FALLBACK = ["opus", "sonnet"]
CLAUDE_EFFORT_FALLBACK = ["low", "medium", "high"]


def _read_json(path: Path) -> tuple[dict, str | None]:
    if not path.exists():
        return {}, None  # absent is normal, not a warning
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except Exception as exc:
        return {}, f"{path} unreadable: {type(exc).__name__}"


def _read_toml(path: Path) -> tuple[dict, str | None]:
    if not path.exists():
        return {}, None
    try:
        import tomllib
    except ImportError:
        return {}, "tomllib unavailable (needs Python 3.11+); codex config skipped"
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh), None
    except Exception as exc:
        return {}, f"{path} unreadable: {type(exc).__name__}"


# ---------------------------------------------------------------------------
# codex
# ---------------------------------------------------------------------------

def _codex_homes() -> tuple[Path, list[Path]]:
    """Return (active_home, other_homes_that_exist)."""
    env_home = os.environ.get("CODEX_HOME")
    active = Path(env_home) if env_home else Path.home() / ".codex"

    candidates = [Path.home() / ".codex"]
    appdata = os.environ.get("APPDATA")
    if appdata:
        orca = Path(appdata) / "orca"
        candidates += sorted((orca / "codex-accounts").glob("*/home"))
        candidates.append(orca / "codex-runtime-home" / "home")

    others = []
    for c in candidates:
        try:
            same = c.resolve() == active.resolve()
        except OSError:
            same = c == active
        if not same and (c / "models_cache.json").exists():
            others.append(c)
    return active, others


def _parse_cache(home: Path) -> tuple[dict, list[str]]:
    data, warn = _read_json(home / "models_cache.json")
    warnings = [warn] if warn else []
    if not data:
        return {"models": [], "fetched_at": None, "client_version": None}, warnings

    models = []
    for m in data.get("models") or []:
        if not isinstance(m, dict) or not m.get("slug"):
            continue
        # "hide" marks internal models (gpt-reserve, codex-auto-review). Offering
        # them to a user would be offering something they cannot meaningfully pick.
        if m.get("visibility") == "hide":
            continue
        levels = m.get("supported_reasoning_levels") or []
        models.append({
            "slug": m["slug"],
            "display_name": m.get("display_name"),
            "description": m.get("description"),
            "default_effort": m.get("default_reasoning_level"),
            # Per-model, never a shared list: only some models accept max/ultra.
            "efforts": [lv["effort"] for lv in levels
                        if isinstance(lv, dict) and lv.get("effort")],
            "priority": m.get("priority"),
        })
    models.sort(key=lambda x: (x["priority"] is None, x["priority"] or 0, x["slug"]))
    return {
        "models": models,
        "fetched_at": data.get("fetched_at"),
        "client_version": data.get("client_version"),
    }, warnings


def discover_codex() -> tuple[dict, list[str]]:
    active, others = _codex_homes()
    cache, warnings = _parse_cache(active)

    cfg, warn = _read_toml(active / "config.toml")
    if warn:
        warnings.append(warn)

    profiles = {}
    for name, body in (cfg.get("profiles") or {}).items():
        if isinstance(body, dict) and (body.get("model") or body.get("model_reasoning_effort")):
            profiles[name] = {
                "model": body.get("model"),
                "effort": body.get("model_reasoning_effort"),
            }

    if not cache["models"]:
        warnings.append(
            f"no usable models_cache.json under {active}; run codex once to populate it"
        )

    return {
        "agent": "codex",
        "home": str(active),
        "home_from_env": "CODEX_HOME" in os.environ,
        "default_model": cfg.get("model"),
        "default_effort": cfg.get("model_reasoning_effort"),
        "models": cache["models"],
        "profiles": profiles,
        "cache_fetched_at": cache["fetched_at"],
        "cache_client_version": cache["client_version"],
        # Reported, deliberately not merged: each home is a separate account with
        # its own entitlements, so another home's list is not reachable from here.
        "other_homes": [str(o) for o in others],
        "note": (
            "Effort levels are per model — read each model's own `efforts`. "
            "Orca sets CODEX_HOME per account, so this list is account-specific."
        ),
    }, warnings


# ---------------------------------------------------------------------------
# claude
# ---------------------------------------------------------------------------

def _claude_help() -> tuple[str, str | None]:
    try:
        # Decode as UTF-8 explicitly. `text=True` would use the locale codec,
        # which raises UnicodeDecodeError on a cp949/cp932 console the moment the
        # help text contains a non-ASCII character (it does).
        proc = subprocess.run(
            ["claude", "--help"],
            capture_output=True, timeout=HELP_TIMEOUT_S,
            encoding="utf-8", errors="replace",
        )
        return (proc.stdout or "") + (proc.stderr or ""), None
    except FileNotFoundError:
        return "", "claude not on PATH; alias/effort discovery skipped"
    except subprocess.TimeoutExpired:
        return "", f"claude --help timed out after {HELP_TIMEOUT_S}s"
    except Exception as exc:
        return "", f"claude --help failed: {type(exc).__name__}"


def discover_claude() -> tuple[dict, list[str]]:
    help_text, warn = _claude_help()
    warnings = [warn] if warn else []

    # Help collapses onto wrapped lines, so flatten whitespace before matching.
    flat = re.sub(r"\s+", " ", help_text)

    # "Provide an alias for the latest model (e.g. 'fable', 'opus', or 'sonnet')"
    aliases: list[str] = []
    m = re.search(r"alias for the latest model \(e\.g\.(.*?)\)", flat)
    if m:
        aliases = re.findall(r"'([A-Za-z0-9._-]+)'", m.group(1))
    if not aliases:
        aliases = list(CLAUDE_ALIAS_FALLBACK)
        if not warn:
            warnings.append(
                "could not parse --model aliases from claude --help; using a "
                "possibly stale fallback"
            )

    # "--effort <level> Effort level for the current session (low, medium, high, xhigh, max)"
    efforts: list[str] = []
    m = re.search(r"--effort <level>.*?\(([^)]*)\)", flat)
    if m:
        efforts = [t.strip() for t in m.group(1).split(",") if t.strip()]
    if not efforts:
        efforts = list(CLAUDE_EFFORT_FALLBACK)
        if not warn:
            warnings.append(
                "could not parse --effort levels from claude --help; using a "
                "possibly stale fallback"
            )

    settings, warn_s = _read_json(Path.home() / ".claude" / "settings.json")
    if warn_s:
        warnings.append(warn_s)

    return {
        "agent": "claude",
        "default_model": settings.get("model"),
        "aliases": aliases,
        "efforts": efforts,
        "note": (
            "No models cache exists for Claude. Aliases resolve to the latest "
            "model of a tier, so prefer an alias over a pinned id."
        ),
    }, warnings


def _emit(payload: dict) -> None:
    """Write UTF-8 bytes straight to stdout.

    print() would encode with the console codec, and model descriptions carry
    arbitrary Unicode — enough to raise UnicodeEncodeError on a cp949/cp932
    console. Bypassing the text layer keeps output identical on every locale.
    """
    blob = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    buf = getattr(sys.stdout, "buffer", None)
    if buf is None:  # stdout already replaced by something text-only
        sys.stdout.write(blob)
    else:
        buf.write(blob.encode("utf-8"))
        buf.flush()


def main() -> int:
    codex, w1 = discover_codex()
    claude, w2 = discover_claude()
    _emit({
        "agents": {"codex": codex, "claude": claude},
        # Omitting both flags is the only choice that cannot go stale: the agent
        # CLI then applies its own current default.
        "recommended": "omit --model/--effort to inherit each CLI's own default",
        "warnings": [w for w in (*w1, *w2) if w],
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
