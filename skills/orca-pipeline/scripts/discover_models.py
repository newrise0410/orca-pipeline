#!/usr/bin/env python3
"""Discover what models each agent CLI can actually reach, right now.

Model names and effort levels rot fast. Hardcoding either is wrong within weeks:
`gpt-5.5` became `gpt-5.6-luna`; `claude-opus-4-7` became `claude-opus-5`; Claude
gained a `fable` alias; `gpt-5.6-sol` accepts an `ultra` effort that older models
do not. This script reads the CLIs' own state instead and prints JSON.

Read-only. Failure contract: for a missing, unreadable, malformed or
wrongly-typed source (including OSError on lookup/read, JSON/TOML parse errors,
non-string values such as TOML dates, and lone surrogates in strings), the
affected field becomes null/[]/{} with an entry in `warnings`, valid neighbouring
records are kept, and one agent's failure never erases the other agent's result.
`main()` still prints exactly one UTF-8 JSON document. This is not a guarantee
against arbitrary system failure (e.g. stdout itself being closed).

A caller must offer discovered values to the user, never assume the list is
complete, and never invent a model id. An empty `aliases`/`efforts` means
"not discovered", not "use remembered names".

Sources
-------
codex   <codex_home>/models_cache.json
          models[].slug / display_name / description
          models[].default_reasoning_level
          models[].supported_reasoning_levels[].effort   <- EFFORT IS PER MODEL
          models[].visibility == "hide"  -> internal, excluded
          models[].priority              -> lower sorts first
        <codex_home>/config.toml   (needs Python 3.11+ tomllib; skipped with a
                                    warning otherwise)
          model, model_reasoning_effort, [profiles.<name>]

        codex_home is $CODEX_HOME when set, else ~/.codex. Orca redirects
        CODEX_HOME per account, so the same machine holds several caches with
        DIFFERENT model lists — an account may not see every model. Other homes
        are reported under `other_homes` for visibility, never merged in.

claude  `claude --help`   (must exit 0; failed help output is never parsed)
          --model description -> aliases (e.g. fable, opus, sonnet)
          --effort description -> effort levels
        ~/.claude/settings.json -> model

        Claude has no models cache. Aliases resolve to "latest of that tier", so
        an alias tracks releases on its own; prefer one over a pinned id.

Usage: python discover_models.py
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

HELP_TIMEOUT_S = 20


def _exists(path: Path) -> tuple[bool, str | None]:
    try:
        return path.exists(), None
    except OSError as exc:
        return False, f"{path} not checkable: {type(exc).__name__}"


def _read_json(path: Path) -> tuple[dict, str | None]:
    """Return the file's top-level JSON object, or {} plus a warning."""
    present, warn = _exists(path)
    if not present:
        return {}, warn  # absent is normal, not a warning
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # JSONDecodeError/UnicodeDecodeError are ValueError
        return {}, f"{path} unreadable: {type(exc).__name__}"
    if not isinstance(data, dict):
        return {}, f"{path} is not a JSON object (got {type(data).__name__}); ignored"
    return data, None


def _read_toml(path: Path) -> tuple[dict, str | None]:
    present, warn = _exists(path)
    if not present:
        return {}, warn
    try:
        import tomllib
    except ImportError:
        return {}, "tomllib unavailable (needs Python 3.11+); codex config skipped"
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh), None
    except (OSError, ValueError) as exc:  # TOMLDecodeError is a ValueError
        return {}, f"{path} unreadable: {type(exc).__name__}"


def _text(value, where: str, field: str, warnings: list[str]) -> str | None:
    """A present string field, or None. Non-strings (TOML dates, lists, numbers)
    are dropped with a warning because they are neither meaningful nor JSON-safe.
    Lone surrogates are replaced so the output always encodes as UTF-8."""
    if value is None:
        return None
    if not isinstance(value, str):
        warnings.append(
            f"{where}: `{field}` is {type(value).__name__}, not a string; reported as null"
        )
        return None
    return value.encode("utf-8", "replace").decode("utf-8")


# ---------------------------------------------------------------------------
# codex
# ---------------------------------------------------------------------------

def _codex_homes(warnings: list[str]) -> tuple[Path | None, list[Path]]:
    """Return (active_home, other_homes_that_exist). active_home is None only
    when neither CODEX_HOME nor a user home can be determined."""
    env_home = os.environ.get("CODEX_HOME")
    try:
        user_codex = Path.home() / ".codex"
    except (RuntimeError, OSError, KeyError) as exc:
        warnings.append(f"user home not determinable: {type(exc).__name__}")
        user_codex = None
    active = Path(env_home) if env_home else user_codex
    if active is None:
        return None, []

    candidates = [user_codex] if user_codex else []
    appdata = os.environ.get("APPDATA")
    if appdata:
        orca = Path(appdata) / "orca"
        try:
            candidates += sorted((orca / "codex-accounts").glob("*/home"))
        except OSError as exc:
            warnings.append(f"{orca / 'codex-accounts'} not listable: {type(exc).__name__}")
        candidates.append(orca / "codex-runtime-home" / "home")

    others = []
    for c in candidates:
        try:
            same = c.resolve() == active.resolve()
        except OSError:
            same = c == active
        present, _ = _exists(c / "models_cache.json")
        if not same and present:
            others.append(c)
    return active, others


def _priority(value, slug: str, warnings: list[str]) -> int | float | None:
    if value is None:
        return None
    # bool is an int subclass; NaN/inf break ordering and are not valid JSON.
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        warnings.append(
            f"models_cache.json: model {slug!r} has non-numeric priority "
            f"({type(value).__name__}); treated as unset"
        )
        return None
    return value


def _parse_cache(home: Path) -> tuple[dict, list[str]]:
    path = home / "models_cache.json"
    data, warn = _read_json(path)
    warnings = [warn] if warn else []
    empty = {"models": [], "fetched_at": None, "client_version": None}
    if not data:
        return empty, warnings

    where = str(path)
    raw_models = data.get("models")
    if raw_models is None:
        raw_models = []
    elif not isinstance(raw_models, list):
        warnings.append(f"{where}: `models` is {type(raw_models).__name__}, not a list; ignored")
        raw_models = []

    models = []
    for m in raw_models:
        if not isinstance(m, dict):
            continue
        slug = m.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            continue
        slug = _text(slug, where, "slug", warnings)
        # "hide" marks internal models (gpt-reserve, codex-auto-review). Offering
        # them to a user would be offering something they cannot meaningfully pick.
        if m.get("visibility") == "hide":
            continue
        record_where = f"{where}: model {slug!r}"
        levels = m.get("supported_reasoning_levels")
        if levels is None:
            levels = []
        elif not isinstance(levels, list):
            warnings.append(
                f"{record_where}: `supported_reasoning_levels` is "
                f"{type(levels).__name__}, not a list; efforts left empty"
            )
            levels = []
        models.append({
            "slug": slug,
            "display_name": _text(m.get("display_name"), record_where, "display_name", warnings),
            "description": _text(m.get("description"), record_where, "description", warnings),
            "default_effort": _text(m.get("default_reasoning_level"), record_where,
                                    "default_reasoning_level", warnings),
            # Per-model, never a shared list: only some models accept max/ultra.
            "efforts": [_text(lv["effort"], record_where, "effort", warnings)
                        for lv in levels
                        if isinstance(lv, dict) and isinstance(lv.get("effort"), str)
                        and lv["effort"].strip()],
            "priority": _priority(m.get("priority"), slug, warnings),
        })
    models.sort(key=lambda x: (x["priority"] is None, x["priority"] or 0, x["slug"]))
    return {
        "models": models,
        "fetched_at": _text(data.get("fetched_at"), where, "fetched_at", warnings),
        "client_version": _text(data.get("client_version"), where, "client_version", warnings),
    }, warnings


def discover_codex() -> tuple[dict, list[str]]:
    warnings: list[str] = []
    active, others = _codex_homes(warnings)
    result = {
        "agent": "codex",
        "home": str(active) if active else None,
        "home_from_env": "CODEX_HOME" in os.environ,
        "default_model": None,
        "default_effort": None,
        "models": [],
        "profiles": {},
        "cache_fetched_at": None,
        "cache_client_version": None,
        # Reported, deliberately not merged: each home is a separate account with
        # its own entitlements, so another home's list is not reachable from here.
        "other_homes": [str(o) for o in others],
        "note": (
            "Effort levels are per model — read each model's own `efforts`. "
            "Orca sets CODEX_HOME per account, so this list is account-specific."
        ),
    }
    if active is None:
        warnings.append("no codex home (CODEX_HOME unset and no user home); codex skipped")
        return result, warnings

    cache, cache_warnings = _parse_cache(active)
    warnings += cache_warnings

    cfg_path = active / "config.toml"
    cfg, warn = _read_toml(cfg_path)
    if warn:
        warnings.append(warn)
    where = str(cfg_path)

    profiles = {}
    raw_profiles = cfg.get("profiles")
    if raw_profiles is not None and not isinstance(raw_profiles, dict):
        warnings.append(f"{where}: `profiles` is {type(raw_profiles).__name__}, not a table; ignored")
        raw_profiles = None
    for name, body in (raw_profiles or {}).items():
        if not isinstance(body, dict):
            continue
        if body.get("model") is None and body.get("model_reasoning_effort") is None:
            continue
        pwhere = f"{where}: profiles.{name}"
        model = _text(body.get("model"), pwhere, "model", warnings)
        effort = _text(body.get("model_reasoning_effort"), pwhere,
                       "model_reasoning_effort", warnings)
        if model or effort:
            profiles[name] = {"model": model, "effort": effort}

    if not cache["models"]:
        warnings.append(
            f"no usable models_cache.json under {active}; run codex once to populate it"
        )

    result.update({
        "default_model": _text(cfg.get("model"), where, "model", warnings),
        "default_effort": _text(cfg.get("model_reasoning_effort"), where,
                                "model_reasoning_effort", warnings),
        "models": cache["models"],
        "profiles": profiles,
        "cache_fetched_at": cache["fetched_at"],
        "cache_client_version": cache["client_version"],
    })
    return result, warnings


# ---------------------------------------------------------------------------
# claude
# ---------------------------------------------------------------------------

def _claude_help() -> tuple[str | None, str | None]:
    """Return (help_text, None) on a clean exit, or (None, warning) otherwise.
    Output from a failed run is never returned: it is not evidence of options."""
    try:
        # Decode as UTF-8 explicitly. `text=True` would use the locale codec,
        # which raises UnicodeDecodeError on a cp949/cp932 console the moment the
        # help text contains a non-ASCII character (it does).
        proc = subprocess.run(
            ["claude", "--help"],
            capture_output=True, timeout=HELP_TIMEOUT_S,
            encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        return None, "claude not on PATH; alias/effort discovery skipped"
    except subprocess.TimeoutExpired:
        return None, f"claude --help timed out after {HELP_TIMEOUT_S}s; alias/effort discovery skipped"
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return None, f"claude --help failed: {type(exc).__name__}; alias/effort discovery skipped"
    if proc.returncode != 0:
        return None, (f"claude --help exited with code {proc.returncode}; "
                      "alias/effort discovery skipped")
    return (proc.stdout or "") + (proc.stderr or ""), None


def discover_claude() -> tuple[dict, list[str]]:
    help_text, warn = _claude_help()
    warnings = [warn] if warn else []

    aliases: list[str] = []
    efforts: list[str] = []
    if help_text is not None:
        # Help collapses onto wrapped lines, so flatten whitespace before matching.
        flat = re.sub(r"\s+", " ", help_text)

        # "Provide an alias for the latest model (e.g. 'fable', 'opus', or 'sonnet')"
        m = re.search(r"alias for the latest model \(e\.g\.(.*?)\)", flat)
        if m:
            aliases = re.findall(r"'([A-Za-z0-9._-]+)'", m.group(1))
        if not aliases:
            warnings.append("could not parse --model aliases from claude --help; "
                            "aliases left empty")

        # "--effort <level> Effort level for the current session (low, medium, high, xhigh, max)"
        m = re.search(r"--effort <level>.*?\(([^)]*)\)", flat)
        if m:
            efforts = [t.strip() for t in m.group(1).split(",") if t.strip()]
        if not efforts:
            warnings.append("could not parse --effort levels from claude --help; "
                            "efforts left empty")

    try:
        settings_path = Path.home() / ".claude" / "settings.json"
    except (RuntimeError, OSError, KeyError) as exc:
        warnings.append(f"user home not determinable: {type(exc).__name__}; "
                        "claude settings skipped")
        settings_path, settings = None, {}
    else:
        settings, warn_s = _read_json(settings_path)
        if warn_s:
            warnings.append(warn_s)

    return {
        "agent": "claude",
        "default_model": _text(settings.get("model"), str(settings_path), "model", warnings),
        "aliases": aliases,
        "efforts": efforts,
        "note": (
            "No models cache exists for Claude. Aliases resolve to the latest "
            "model of a tier, so prefer an alias over a pinned id. Empty aliases/"
            "efforts mean not discovered; offer the CLI default instead."
        ),
    }, warnings


def _guarded(name: str, discover, skeleton: dict) -> tuple[dict, list[str]]:
    """Last-resort boundary so one agent's unexpected bug cannot erase the other
    agent's result. Expected failures are handled field by field above."""
    try:
        return discover()
    except Exception as exc:  # noqa: BLE001 - boundary by design, reported below
        return dict(skeleton), [f"{name} discovery failed: {type(exc).__name__}: {exc}; "
                                f"{name} results left empty"]


def _emit(payload: dict) -> None:
    """Write UTF-8 bytes straight to stdout.

    print() would encode with the console codec, and model descriptions carry
    arbitrary Unicode — enough to raise UnicodeEncodeError on a cp949/cp932
    console. Bypassing the text layer keeps output identical on every locale.
    """
    blob = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    data = blob.encode("utf-8", "replace")
    buf = getattr(sys.stdout, "buffer", None)
    if buf is None:  # stdout already replaced by something text-only
        sys.stdout.write(data.decode("utf-8"))
    else:
        buf.write(data)
        buf.flush()


def main() -> int:
    codex, w1 = _guarded("codex", discover_codex, {
        "agent": "codex", "home": None, "home_from_env": "CODEX_HOME" in os.environ,
        "default_model": None, "default_effort": None, "models": [], "profiles": {},
        "cache_fetched_at": None, "cache_client_version": None, "other_homes": [],
        "note": "codex discovery failed; see warnings",
    })
    claude, w2 = _guarded("claude", discover_claude, {
        "agent": "claude", "default_model": None, "aliases": [], "efforts": [],
        "note": "claude discovery failed; see warnings",
    })
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
