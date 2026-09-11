"""Regression tests for skills/orca-pipeline/scripts/discover_models.py.

Run from the repository root (Python 3.11+, standard library only):

    python -m unittest discover -s tests -p "test_*.py" -v

Every test runs inside `Isolated`, which never touches the real machine:

- CODEX_HOME, APPDATA, USERPROFILE and HOME point into a fresh temp root, and
  pathlib.Path.home() returns a home inside that root.
- subprocess.run is replaced by a fake that records its argv; `claude` is
  never executed.
- Path.exists / is_file / is_dir / stat / read_text / open / glob / iterdir are
  observed, and each test asserts that no path outside the temp root was read.

These tests prove the script's parsing and failure handling only. They do not
prove that an account may use a model or that a real CLI prints this help text.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT = (pathlib.Path(__file__).resolve().parents[1]
          / "skills" / "orca-pipeline" / "scripts" / "discover_models.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("discover_models_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dm = _load_module()


HELP_OK = """\
Usage: claude [options] [command] [prompt]

Options:
  --effort <level>   Effort level for the current session (tiny, huge)
  --model <model>    Model for the current session. Provide an alias for the latest model (e.g. 'alpha1' or 'beta2') or a model's full name.
"""

# Same content, wrapped the way a narrow terminal wraps it.
HELP_WRAPPED = """\
Usage: claude [options]
  --effort <level>   Effort level for the
                     current session (tiny,
                     huge)
  --model <model>    Model for the current session. Provide an alias for the
                     latest
                     model (e.g. 'alpha1'
                     or 'beta2') or a model's full name.
"""

HELP_UNPARSEABLE = "Usage: claude [options]\n  --verbose   Be loud\n"

REMEMBERED_NAMES = {"opus", "sonnet", "low", "medium", "high"}


def _cache(*models, **extra):
    body = {"fetched_at": "2026-09-01T00:00:00Z", "client_version": "9.9.9",
            "models": list(models)}
    body.update(extra)
    return body


def _model(slug, priority=None, efforts=("low", "high"), **extra):
    record = {
        "slug": slug,
        "display_name": slug.upper(),
        "description": f"{slug} description",
        "default_reasoning_level": efforts[0] if efforts else None,
        "supported_reasoning_levels": [{"effort": e} for e in efforts],
        "visibility": "list",
    }
    if priority is not None:
        record["priority"] = priority
    record.update(extra)
    return record


class Isolated:
    """A throwaway machine: temp homes, fake `claude`, observed file access."""

    def __init__(self, help_text=HELP_OK, help_mode="ok", codex_home_in_env=True):
        self.help_text = help_text
        self.help_mode = help_mode
        self.codex_home_in_env = codex_home_in_env
        self.subprocess_calls = []
        self.accessed = []
        self._patches = []

    # -- fixture writers ---------------------------------------------------
    def write_cache(self, body, home=None):
        home = home or self.codex_home
        home.mkdir(parents=True, exist_ok=True)
        text = body if isinstance(body, str) else json.dumps(body)
        (home / "models_cache.json").write_text(text, encoding="utf-8")

    def write_config(self, text):
        self.codex_home.mkdir(parents=True, exist_ok=True)
        (self.codex_home / "config.toml").write_text(text, encoding="utf-8")

    def write_settings(self, body):
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        text = body if isinstance(body, str) else json.dumps(body)
        settings.write_text(text, encoding="utf-8")

    def add_account_home(self, name, body):
        home = self.appdata / "orca" / "codex-accounts" / name / "home"
        self.write_cache(body, home=home)
        return home

    # -- fakes -------------------------------------------------------------
    def _fake_run(self, argv, *args, **kwargs):
        self.subprocess_calls.append(list(argv))
        if self.help_mode == "missing":
            raise FileNotFoundError(argv[0])
        if self.help_mode == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
        code = 7 if self.help_mode == "nonzero" else 0
        return subprocess.CompletedProcess(argv, code, stdout=self.help_text, stderr="")

    def _observe(self, name):
        original = getattr(pathlib.Path, name)
        accessed = self.accessed

        def wrapper(path, *args, **kwargs):
            accessed.append((name, str(path)))
            return original(path, *args, **kwargs)

        return mock.patch.object(pathlib.Path, name, wrapper)

    # -- context -----------------------------------------------------------
    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name).resolve()
        self.home = self.root / "home"
        self.appdata = self.root / "appdata"
        self.codex_home = self.root / "codex-home"
        self.home.mkdir()
        self.appdata.mkdir()
        env = {"APPDATA": str(self.appdata), "USERPROFILE": str(self.home),
               "HOME": str(self.home)}
        if self.codex_home_in_env:
            env["CODEX_HOME"] = str(self.codex_home)
        else:
            self.codex_home = self.home / ".codex"
        env_patch = mock.patch.dict(os.environ, env)
        env_patch.start()
        self._patches.append(env_patch)
        if not self.codex_home_in_env:
            os.environ.pop("CODEX_HOME", None)
        for p in (
            mock.patch("pathlib.Path.home", return_value=self.home),
            mock.patch("subprocess.run", side_effect=self._fake_run),
            *(self._observe(n) for n in ("exists", "is_file", "is_dir", "stat",
                                          "read_text", "open", "glob", "iterdir")),
        ):
            p.start()
            self._patches.append(p)
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()
        return False

    def outside_root(self):
        bad = []
        for op, raw in self.accessed:
            try:
                pathlib.Path(raw).resolve().relative_to(self.root)
            except ValueError:
                bad.append((op, raw))
        return bad


class DiscoveryTestCase(unittest.TestCase):
    def run_main(self, env):
        buf = io.BytesIO()
        # A cp949 text layer: main() must bypass it and write UTF-8 bytes.
        fake_stdout = io.TextIOWrapper(buf, encoding="cp949")
        with mock.patch.object(sys, "stdout", fake_stdout):
            code = dm.main()
        raw = buf.getvalue()
        fake_stdout.detach()
        self.assertEqual(code, 0)
        text = raw.decode("utf-8")  # strict: must be valid UTF-8
        payload = json.loads(text)  # exactly one JSON document
        self.assert_isolated(env)
        return payload, raw

    def assert_isolated(self, env):
        self.assertEqual(env.outside_root(), [], "read a path outside the temp root")
        for argv in env.subprocess_calls:
            self.assertEqual(argv[0], "claude")

    def assert_warning(self, warnings, *needles):
        joined = "\n".join(warnings)
        for needle in needles:
            self.assertIn(needle, joined)

    def assert_no_remembered(self, claude):
        self.assertFalse(REMEMBERED_NAMES & set(claude["aliases"]), claude["aliases"])
        self.assertFalse(REMEMBERED_NAMES & set(claude["efforts"]), claude["efforts"])


class IsolationHarnessTest(DiscoveryTestCase):
    def test_harness_observes_access_and_fakes_claude(self):
        with Isolated() as env:
            env.write_cache(_cache(_model("m1", 1)))
            payload, _ = self.run_main(env)
            self.assertTrue(env.accessed, "file access was not observed")
            self.assertEqual(env.subprocess_calls, [["claude", "--help"]])
            self.assertEqual(payload["agents"]["codex"]["home"], str(env.codex_home))


class MalformedInputTest(DiscoveryTestCase):
    """The six inputs from the plan's diagnosis table, plus the _emit crashes."""

    def test_cache_root_is_a_list(self):
        with Isolated() as env:
            env.write_cache([1])
            env.write_config('model = "cfg-model"\n')
            payload, _ = self.run_main(env)
            codex, claude = payload["agents"]["codex"], payload["agents"]["claude"]
            self.assertEqual(codex["models"], [])
            self.assertEqual(codex["default_model"], "cfg-model")
            self.assertEqual(claude["aliases"], ["alpha1", "beta2"])
            self.assert_warning(payload["warnings"], "models_cache.json", "JSON object")

    def test_cache_models_is_not_a_list(self):
        with Isolated() as env:
            env.write_cache(_cache() | {"models": True})
            payload, _ = self.run_main(env)
            codex = payload["agents"]["codex"]
            self.assertEqual(codex["models"], [])
            self.assertEqual(codex["cache_client_version"], "9.9.9")
            self.assert_warning(payload["warnings"], "`models`")

    def test_reasoning_levels_is_not_a_list(self):
        with Isolated() as env:
            env.write_cache(_cache(
                _model("good", 1, efforts=("low", "max")),
                _model("bad", 2) | {"supported_reasoning_levels": 7},
            ))
            payload, _ = self.run_main(env)
            models = {m["slug"]: m for m in payload["agents"]["codex"]["models"]}
            self.assertEqual(models["good"]["efforts"], ["low", "max"])
            self.assertEqual(models["bad"]["efforts"], [])
            self.assert_warning(payload["warnings"], "bad", "supported_reasoning_levels")

    def test_mixed_priority_types(self):
        with Isolated() as env:
            env.write_cache(_cache(_model("second", "2"), _model("first", 1)))
            payload, _ = self.run_main(env)
            models = payload["agents"]["codex"]["models"]
            self.assertEqual([m["slug"] for m in models], ["first", "second"])
            self.assertIsNone(models[1]["priority"])
            self.assert_warning(payload["warnings"], "second", "priority")

    def test_profiles_is_not_a_table(self):
        with Isolated() as env:
            env.write_cache(_cache(_model("m1", 1)))
            env.write_config('model = "cfg-model"\nprofiles = ["bad"]\n')
            payload, _ = self.run_main(env)
            codex = payload["agents"]["codex"]
            self.assertEqual(codex["profiles"], {})
            self.assertEqual(codex["default_model"], "cfg-model")
            self.assertEqual([m["slug"] for m in codex["models"]], ["m1"])
            self.assert_warning(payload["warnings"], "profiles")

    def test_claude_settings_root_is_a_list(self):
        with Isolated() as env:
            env.write_cache(_cache(_model("m1", 1)))
            env.write_settings(["bad"])
            payload, _ = self.run_main(env)
            claude = payload["agents"]["claude"]
            self.assertIsNone(claude["default_model"])
            self.assertEqual(claude["aliases"], ["alpha1", "beta2"])
            self.assertEqual([m["slug"] for m in payload["agents"]["codex"]["models"]], ["m1"])
            self.assert_warning(payload["warnings"], "settings.json", "JSON object")

    def test_toml_datetime_default_model(self):
        with Isolated() as env:
            env.write_cache(_cache(_model("m1", 1)))
            env.write_config('model = 1979-05-27T07:32:00Z\nmodel_reasoning_effort = "high"\n')
            payload, _ = self.run_main(env)
            codex = payload["agents"]["codex"]
            self.assertIsNone(codex["default_model"])
            self.assertEqual(codex["default_effort"], "high")
            self.assert_warning(payload["warnings"], "config.toml", "`model`")

    def test_toml_date_in_profile(self):
        with Isolated() as env:
            env.write_config('[profiles.p]\nmodel = 1979-05-27\n'
                             'model_reasoning_effort = "xhigh"\n')
            payload, _ = self.run_main(env)
            self.assertEqual(payload["agents"]["codex"]["profiles"],
                             {"p": {"model": None, "effort": "xhigh"}})
            self.assert_warning(payload["warnings"], "profiles.p", "`model`")

    def test_non_string_settings_model(self):
        with Isolated() as env:
            env.write_settings({"model": ["opus"]})
            payload, _ = self.run_main(env)
            self.assertIsNone(payload["agents"]["claude"]["default_model"])
            self.assert_warning(payload["warnings"], "settings.json", "`model`")

    def test_lone_surrogate_in_description(self):
        with Isolated() as env:
            env.write_cache('{"models": [{"slug": "m1", "description": "bad \\ud800 end"}]}')
            payload, raw = self.run_main(env)
            description = payload["agents"]["codex"]["models"][0]["description"]
            self.assertTrue(description.startswith("bad ") and description.endswith(" end"))
            self.assertNotIn("\ud800", description)

    def test_invalid_slugs_and_efforts_are_dropped(self):
        with Isolated() as env:
            env.write_cache(_cache(
                _model("ok", 1) | {"supported_reasoning_levels": [
                    {"effort": "low"}, {"effort": 3}, "high", {"effort": ""}]},
                {"slug": ""}, {"slug": 5}, "not-a-record",
                _model("flag", True),
            ))
            payload, _ = self.run_main(env)
            models = payload["agents"]["codex"]["models"]
            self.assertEqual([m["slug"] for m in models], ["ok", "flag"])
            self.assertEqual(models[0]["efforts"], ["low"])
            self.assertIsNone(models[1]["priority"])  # bool is not a priority


class NormalCacheTest(DiscoveryTestCase):
    def test_hidden_priority_per_model_efforts_and_other_homes(self):
        with Isolated() as env:
            env.write_cache(_cache(
                _model("later", 5, efforts=("low", "medium")),
                _model("internal", 0, visibility="hide"),
                _model("sooner", 1, efforts=("low", "xhigh", "ultra"),
                       description="한국어 설명 — 비ASCII"),
                _model("unranked", efforts=()),
            ))
            env.write_config('model = "sooner"\nmodel_reasoning_effort = "xhigh"\n'
                             '[profiles.deep]\nmodel = "sooner"\n'
                             'model_reasoning_effort = "ultra"\n')
            other = env.add_account_home("acct1", _cache(_model("elsewhere", 0)))
            payload, _ = self.run_main(env)
            codex = payload["agents"]["codex"]
            self.assertEqual([m["slug"] for m in codex["models"]],
                             ["sooner", "later", "unranked"])
            efforts = {m["slug"]: m["efforts"] for m in codex["models"]}
            self.assertEqual(efforts, {"sooner": ["low", "xhigh", "ultra"],
                                       "later": ["low", "medium"], "unranked": []})
            self.assertEqual(codex["models"][0]["description"], "한국어 설명 — 비ASCII")
            self.assertEqual(codex["other_homes"], [str(other)])
            self.assertNotIn("elsewhere", {m["slug"] for m in codex["models"]})
            self.assertEqual(codex["default_model"], "sooner")
            self.assertEqual(codex["profiles"], {"deep": {"model": "sooner", "effort": "ultra"}})
            self.assertTrue(codex["home_from_env"])
            self.assertEqual(payload["warnings"], [])

    def test_default_home_without_codex_home_env(self):
        with Isolated(codex_home_in_env=False) as env:
            env.write_cache(_cache(_model("m1", 1)))
            payload, _ = self.run_main(env)
            codex = payload["agents"]["codex"]
            self.assertEqual(codex["home"], str(env.home / ".codex"))
            self.assertFalse(codex["home_from_env"])
            self.assertEqual([m["slug"] for m in codex["models"]], ["m1"])


class MissingAndBrokenSourceTest(DiscoveryTestCase):
    def test_nothing_exists(self):
        with Isolated() as env:
            payload, _ = self.run_main(env)
            codex = payload["agents"]["codex"]
            self.assertEqual(codex["models"], [])
            self.assertEqual(codex["profiles"], {})
            self.assertIsNone(codex["default_model"])
            self.assertIsNone(payload["agents"]["claude"]["default_model"])
            self.assertEqual(payload["agents"]["claude"]["efforts"], ["tiny", "huge"])
            self.assert_warning(payload["warnings"], "no usable models_cache.json")

    def test_broken_json_and_toml(self):
        with Isolated() as env:
            env.write_cache("{not json")
            env.write_config("model = \n")
            env.write_settings("{")
            payload, _ = self.run_main(env)
            self.assertEqual(payload["agents"]["codex"]["models"], [])
            self.assertEqual(payload["agents"]["claude"]["aliases"], ["alpha1", "beta2"])
            self.assert_warning(payload["warnings"], "models_cache.json unreadable",
                                "config.toml unreadable", "settings.json unreadable")

    def test_tomllib_unavailable_skips_config_only(self):
        with Isolated() as env:
            env.write_cache(_cache(_model("m1", 1)))
            env.write_config('model = "cfg-model"\n')
            with mock.patch.dict(sys.modules, {"tomllib": None}):
                payload, _ = self.run_main(env)
            codex = payload["agents"]["codex"]
            self.assertIsNone(codex["default_model"])
            self.assertEqual([m["slug"] for m in codex["models"]], ["m1"])
            self.assert_warning(payload["warnings"], "tomllib unavailable")

    def test_permission_error_on_cache(self):
        with Isolated() as env:
            env.write_cache(_cache(_model("m1", 1)))
            original = pathlib.Path.read_text

            def deny(path, *args, **kwargs):
                if path.name == "models_cache.json":
                    raise PermissionError("denied")
                return original(path, *args, **kwargs)

            with mock.patch.object(pathlib.Path, "read_text", deny):
                payload, _ = self.run_main(env)
            self.assertEqual(payload["agents"]["codex"]["models"], [])
            self.assertEqual(payload["agents"]["claude"]["aliases"], ["alpha1", "beta2"])
            self.assert_warning(payload["warnings"], "PermissionError")

    def test_home_lookup_failure_keeps_other_provider(self):
        with Isolated(codex_home_in_env=False) as env:
            with mock.patch("pathlib.Path.home", side_effect=RuntimeError("no home")):
                payload, _ = self.run_main(env)
            self.assertEqual(payload["agents"]["claude"]["aliases"], ["alpha1", "beta2"])
            self.assert_warning(payload["warnings"], "home")

    def test_unexpected_provider_crash_keeps_other_provider(self):
        with Isolated() as env:
            with mock.patch.object(dm, "_parse_cache", side_effect=RuntimeError("boom")):
                payload, _ = self.run_main(env)
            self.assertEqual(payload["agents"]["codex"]["models"], [])
            self.assertEqual(payload["agents"]["claude"]["efforts"], ["tiny", "huge"])
            self.assert_warning(payload["warnings"], "codex discovery failed", "RuntimeError")


class ClaudeHelpTest(DiscoveryTestCase):
    def test_normal_help(self):
        with Isolated(help_text=HELP_OK) as env:
            payload, _ = self.run_main(env)
            claude = payload["agents"]["claude"]
            self.assertEqual(claude["aliases"], ["alpha1", "beta2"])
            self.assertEqual(claude["efforts"], ["tiny", "huge"])
            self.assertEqual(payload["warnings"],
                             ["no usable models_cache.json under %s; run codex once to "
                              "populate it" % env.codex_home])

    def test_wrapped_help(self):
        with Isolated(help_text=HELP_WRAPPED) as env:
            payload, _ = self.run_main(env)
            claude = payload["agents"]["claude"]
            self.assertEqual(claude["aliases"], ["alpha1", "beta2"])
            self.assertEqual(claude["efforts"], ["tiny", "huge"])

    def test_help_failures_return_empty_arrays(self):
        cases = {
            "missing": "not on PATH",
            "timeout": "timed out",
            "nonzero": "exited with code 7",
        }
        for mode, needle in cases.items():
            with self.subTest(mode=mode), Isolated(help_mode=mode) as env:
                payload, _ = self.run_main(env)
                claude = payload["agents"]["claude"]
                self.assertEqual(claude["aliases"], [])
                self.assertEqual(claude["efforts"], [])
                self.assert_no_remembered(claude)
                self.assert_warning(payload["warnings"], needle)

    def test_unparseable_help(self):
        with Isolated(help_text=HELP_UNPARSEABLE) as env:
            payload, _ = self.run_main(env)
            claude = payload["agents"]["claude"]
            self.assertEqual(claude["aliases"], [])
            self.assertEqual(claude["efforts"], [])
            self.assert_no_remembered(claude)
            self.assert_warning(payload["warnings"], "--model aliases", "--effort levels")

    def test_settings_model_is_reported(self):
        with Isolated() as env:
            env.write_settings({"model": "opus[1m]"})
            payload, _ = self.run_main(env)
            self.assertEqual(payload["agents"]["claude"]["default_model"], "opus[1m]")


class DefaultsAndProfilesTest(DiscoveryTestCase):
    def test_effort_without_model(self):
        with Isolated() as env:
            env.write_cache(_cache(_model("m1", 1)))
            env.write_config('model_reasoning_effort = "xhigh"\n')
            payload, _ = self.run_main(env)
            codex = payload["agents"]["codex"]
            self.assertIsNone(codex["default_model"])
            self.assertEqual(codex["default_effort"], "xhigh")
            self.assertEqual(payload["warnings"], [])

    def test_profile_without_model_or_effort_is_skipped(self):
        with Isolated() as env:
            env.write_config('[profiles.empty]\nsandbox = "x"\n'
                             '[profiles.effort_only]\nmodel_reasoning_effort = "low"\n')
            payload, _ = self.run_main(env)
            self.assertEqual(payload["agents"]["codex"]["profiles"],
                             {"effort_only": {"model": None, "effort": "low"}})

    def test_model_without_reasoning_levels(self):
        with Isolated() as env:
            env.write_cache(_cache({"slug": "bare", "priority": 1}))
            payload, _ = self.run_main(env)
            model = payload["agents"]["codex"]["models"][0]
            self.assertEqual(model["efforts"], [])
            self.assertIsNone(model["default_effort"])
            self.assertEqual(payload["warnings"], [])


class OutputShapeTest(DiscoveryTestCase):
    def test_top_level_shape(self):
        with Isolated(help_mode="missing") as env:
            env.write_cache(_cache(_model("m1", 1)))
            payload, raw = self.run_main(env)
            self.assertEqual(set(payload), {"agents", "recommended", "warnings"})
            self.assertEqual(set(payload["agents"]), {"codex", "claude"})
            self.assertEqual(set(payload["agents"]["codex"]),
                             {"agent", "home", "home_from_env", "default_model",
                              "default_effort", "models", "profiles", "cache_fetched_at",
                              "cache_client_version", "other_homes", "note"})
            self.assertEqual(set(payload["agents"]["claude"]),
                             {"agent", "default_model", "aliases", "efforts", "note"})
            self.assertTrue(all(isinstance(w, str) for w in payload["warnings"]))
            self.assertTrue(raw.endswith(b"\n"))


if __name__ == "__main__":
    unittest.main()
