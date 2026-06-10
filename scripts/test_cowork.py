#!/usr/bin/env python3
"""Tests for the cowork foundation + scout role.

Pure functions (flag assembly, framing, parsing, probe, flow) are tested with
fakes; no real claude/codex CLI is spawned. Run:

    python3 -m unittest scripts/test_cowork.py
"""

import io
import json
import os
import shutil
import subprocess
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import cowork  # noqa: E402
import cowork_bridge as bridge  # noqa: E402
import cowork_preflight as preflight  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_ui as ui  # noqa: E402

# The rich UX stack is optional at import time (lazy-imported in cowork_ui). Tests
# that exercise the real libraries skip when they are absent — same pattern as the
# COWORK_LIVE integration tests below.
try:
    import rich  # noqa: F401
    import prompt_toolkit  # noqa: F401
    import questionary  # noqa: F401
    HAS_UI_DEPS = True
except ImportError:
    HAS_UI_DEPS = False


class FlagAssemblyTest(unittest.TestCase):
    def test_claude_mode_flags(self):
        self.assertEqual(bridge.claude_mode_flags("plan", True),
                         ["--permission-mode", "plan"])
        self.assertEqual(bridge.claude_mode_flags("plan", False),
                         ["--permission-mode", "plan"])
        self.assertEqual(bridge.claude_mode_flags("implement", True),
                         ["--dangerously-skip-permissions"])
        self.assertEqual(bridge.claude_mode_flags("implement", False),
                         ["--permission-mode", "acceptEdits"])

    def test_codex_mode_flags(self):
        # codex exec has no --ask-for-approval; approval is governed by sandbox.
        self.assertEqual(bridge.codex_mode_flags("plan", True),
                         ["--sandbox", "read-only"])
        self.assertEqual(bridge.codex_mode_flags("implement", True),
                         ["--dangerously-bypass-approvals-and-sandbox"])
        self.assertEqual(bridge.codex_mode_flags("implement", False),
                         ["--sandbox", "workspace-write"])

    def test_build_claude_command_is_duplex(self):
        cmd = bridge.build_claude_command("roles/scout.md", "plan", True)
        for flag in ("-p", "--input-format", "stream-json", "--output-format",
                     "--verbose", "--replay-user-messages",
                     "--append-system-prompt-file"):
            self.assertIn(flag, cmd)
        self.assertIn("roles/scout.md", cmd)
        self.assertEqual(cmd[0], "claude")
        # interactive question tool is blocked (auto-"skipped" in headless -p)
        self.assertIn("--disallowedTools", cmd)
        self.assertIn("AskUserQuestion", cmd)

    def test_build_codex_command(self):
        cmd = bridge.build_codex_command("PROMPT", "plan", True)
        self.assertEqual(cmd[:4], ["codex", "exec", "--json", "--skip-git-repo-check"])
        self.assertIn("--sandbox", cmd)
        self.assertEqual(cmd[-1], "PROMPT")

    def test_codex_resume_uses_explicit_id_never_last(self):
        cmd = bridge.build_codex_resume_command("thread-abc", "next")
        self.assertEqual(
            cmd,
            ["codex", "exec", "resume", "--json", "--skip-git-repo-check",
             "thread-abc", "next"],
        )
        self.assertNotIn("--last", cmd)
        # resume rejects --sandbox (policy inherited from the original session).
        self.assertNotIn("--sandbox", cmd)


class PreflightTest(unittest.TestCase):
    def test_python_floor(self):
        ok, alert = preflight.check_python((3, 9, 6))
        self.assertTrue(ok)
        self.assertIsNone(alert)
        ok, alert = preflight.check_python((3, 8, 18))
        self.assertFalse(ok)
        self.assertIn("3.8.18", alert)

    def test_required_controllers_dedup(self):
        cfg = {
            "scout": {"controller": "claude"},
            "planner": {"controller": "claude"},
            "advisor": {"controller": "codex"},
        }
        self.assertEqual(preflight.required_controllers(cfg), ["claude", "codex"])

    def test_check_controllers_missing_and_present(self):
        ok, alerts = preflight.check_controllers(["claude"], which=lambda c: None)
        self.assertFalse(ok)
        self.assertIn("@anthropic-ai/claude-code", alerts[0])

        ok, alerts = preflight.check_controllers(["codex"], which=lambda c: None)
        self.assertIn("@openai/codex", alerts[0])

        ok, alerts = preflight.check_controllers(
            ["claude"], which=lambda c: "/usr/bin/" + c
        )
        self.assertTrue(ok)
        self.assertEqual(alerts, [])

    def test_preflight_aggregates_non_interactive(self):
        cfg = {"scout": {"controller": "codex"}}
        ok, alerts = preflight.preflight(
            cfg, version_info=(3, 8, 0), which=lambda c: None, interactive=False
        )
        self.assertFalse(ok)
        # python alert + codex alert (no gum required when non-interactive)
        self.assertEqual(len(alerts), 2)

    def test_preflight_requires_packages_only_when_interactive(self):
        cfg = {"scout": {"controller": "claude"}}
        present = lambda c: "/bin/" + c if c == "claude" else None
        have = lambda name: object()   # all packages importable
        missing = lambda name: None    # none importable
        # Non-interactive: pip packages are not required.
        ok, _ = preflight.preflight(cfg, which=present, interactive=False,
                                    find_spec=missing)
        self.assertTrue(ok)
        # Interactive + packages missing -> fail with a package alert.
        ok, alerts = preflight.preflight(cfg, which=present, interactive=True,
                                         find_spec=missing)
        self.assertFalse(ok)
        self.assertTrue(any("prompt_toolkit" in a or "rich" in a
                            or "questionary" in a for a in alerts))
        # Interactive + packages present -> ok.
        ok, _ = preflight.preflight(cfg, which=present, interactive=True,
                                    find_spec=have)
        self.assertTrue(ok)

    def test_check_python_packages(self):
        ok, alerts = preflight.check_python_packages(
            ["rich", "questionary"], find_spec=lambda n: None)
        self.assertFalse(ok)
        self.assertEqual(len(alerts), 2)
        self.assertIn("pip install", alerts[0])
        ok, alerts = preflight.check_python_packages(
            ["rich"], find_spec=lambda n: object())
        self.assertTrue(ok)
        self.assertEqual(alerts, [])


class MenuTest(unittest.TestCase):
    """Interactive menus driven by injected ask-callables; questionary never runs."""

    def test_select_team_interactive(self):
        self.assertEqual(
            cowork.select_team_interactive(
                checkbox_fn=lambda msg, opts, checked=None: ["planner", "scout"]),
            ["scout", "planner"])  # re-ordered by canonical ROLES

    def test_select_team_cancel_returns_empty(self):
        self.assertEqual(
            cowork.select_team_interactive(checkbox_fn=lambda *a, **k: None), [])
        self.assertEqual(
            cowork.select_team_interactive(checkbox_fn=lambda *a, **k: []), [])

    def test_configure_roles_accepts_defaults(self):
        cfg = cowork.configure_roles_interactive(
            ["scout", "advisor"],
            select_fn=lambda opts, default=None, message="": "use these defaults",
            checkbox_fn=lambda *a, **k: [])
        self.assertEqual(cfg["scout"], cowork.DEFAULTS["scout"])

    def test_configure_roles_customizes(self):
        def select_fn(opts, default=None, message=""):
            if "use these defaults" in opts:        # the defaults-vs-customize gate
                return "customize"
            if message.endswith("controller"):
                return "codex"
            if message.endswith("permissions"):
                return "no-yolo"
            if message.endswith("mode"):
                return "implement"
            return default
        cfg = cowork.configure_roles_interactive(
            ["scout"], select_fn=select_fn,
            checkbox_fn=lambda msg, opts, checked=None: ["scout"])
        self.assertEqual(cfg["scout"]["controller"], "codex")
        self.assertFalse(cfg["scout"]["yolo"])
        self.assertEqual(cfg["scout"]["mode"], "implement")

    def test_gather_context_eof_is_empty(self):
        self.assertEqual(
            cowork.gather_context_interactive(prompt_fn=lambda: ui.EOF), "")
        self.assertEqual(
            cowork.gather_context_interactive(prompt_fn=lambda: "the brief"),
            "the brief")

    def test_format_config_summary_aligned(self):
        cfg = cowork.default_config(["scout", "advisor", "builder"])
        text = cowork.format_config_summary(cfg)
        self.assertIn("scout", text)
        for label in ("role", "controller", "permissions", "mode"):
            self.assertIn(label, text)
        self.assertIn("no-yolo", cowork.format_config_summary(
            {"scout": {"controller": "claude", "yolo": False, "mode": "plan"}}))


class ConfigTest(unittest.TestCase):
    def test_default_config_matches_defaults(self):
        cfg = cowork.default_config(cowork.ROLES)
        # Roles default to implement mode (guardrailed by role spec, not plan).
        self.assertEqual(cfg["scout"],
                         {"controller": "claude", "yolo": True, "mode": "implement"})
        for role in cowork.ROLES:
            self.assertEqual(cfg[role]["mode"], "implement")

    def test_apply_config_override(self):
        cfg = cowork.default_config(["scout"])
        ok, err = cowork.apply_config_override(
            cfg, "scout", ["codex", "no-yolo", "implement"])
        self.assertTrue(ok)
        self.assertEqual(
            cfg["scout"], {"controller": "codex", "yolo": False, "mode": "implement"})
        ok, _ = cowork.apply_config_override(cfg, "ghost", ["claude"])
        self.assertFalse(ok)
        ok, _ = cowork.apply_config_override(cfg, "scout", ["bogus"])
        self.assertFalse(ok)


class ArgsPathTest(unittest.TestCase):
    def _args(self, argv):
        return cowork.build_parser().parse_args(argv)

    def test_parse_team(self):
        selected, err = cowork.parse_team("advisor,scout")
        self.assertIsNone(err)
        self.assertEqual(selected, ["scout", "advisor"])  # canonical order
        selected, err = cowork.parse_team("scout,ghost")
        self.assertIsNotNone(err)

    def test_apply_config_args(self):
        cfg = cowork.default_config(["scout", "advisor"])
        ok, err = cowork.apply_config_args(cfg, ["scout=codex,no-yolo,implement"])
        self.assertTrue(ok)
        self.assertEqual(
            cfg["scout"], {"controller": "codex", "yolo": False, "mode": "implement"})
        ok, err = cowork.apply_config_args(cfg, ["scoutcodex"])  # no '='
        self.assertFalse(ok)

    def test_resolve_context_text_and_file(self):
        args = self._args(["--context", "hello"])
        self.assertEqual(cowork.resolve_context(args), "hello")
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("from file")
            path = fh.name
        try:
            args = self._args(["--context-file", path])
            self.assertEqual(cowork.resolve_context(args), "from file")
        finally:
            os.unlink(path)

    def test_resolve_context_resuming_skips_prompt(self):
        # Resuming + interactive => skip the goal prompt, return "" so run_scout
        # auto-continues ("Continue the session.") without ever prompting.
        import unittest.mock as mock
        with mock.patch.object(cowork, "gather_context_interactive",
                               side_effect=AssertionError("prompted on resume")):
            self.assertEqual(cowork.resolve_context(self._args([]), resuming=True), "")
        # An explicit --context still wins on resume (redirect a resumed session).
        self.assertEqual(
            cowork.resolve_context(self._args(["--context", "new goal"]),
                                   resuming=True),
            "new goal")

    def test_run_flow_non_interactive_reaches_scout(self):
        captured = {}

        def fake_run_scout(config, context, selected, io_in=None, io_out=None,
                           resume_id=None, on_session=None, intel_path=None,
                           review_path=None, **kwargs):
            captured["config"] = config
            captured["context"] = context
            captured["selected"] = selected
            captured["intel_path"] = intel_path
            captured["review_path"] = review_path
            return 0

        args = self._args(
            ["--team", "scout,advisor",
             "--config", "scout=codex,no-yolo,implement",
             "--context", "do the thing", "--no-session"])
        out = io.StringIO()
        rc = cowork.run_flow(
            args, io_out=out,
            which=lambda c: "/bin/" + c,  # everything present
            run_scout_fn=fake_run_scout,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(captured["selected"], ["scout", "advisor"])
        self.assertEqual(captured["context"], "do the thing")
        self.assertEqual(captured["config"]["scout"]["controller"], "codex")

    def test_run_flow_non_interactive_skips_gum_in_preflight(self):
        # claude present, gum absent: non-interactive must still pass preflight.
        args = self._args(["--team", "advisor", "--context", "x", "--no-session"])
        out = io.StringIO()
        rc = cowork.run_flow(
            args, io_out=out,
            which=lambda c: None if c == "gum" else "/bin/" + c,
        )
        # advisor (no scout) -> "not selected" note, rc 0, gum never required
        self.assertEqual(rc, 0)
        self.assertIn("scout not selected", out.getvalue())


class StateStoreTest(unittest.TestCase):
    def _tmp(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return os.path.join(d, ".cowork", "session.json")

    def test_save_load_roundtrip_and_has_config(self):
        path = self._tmp()
        self.assertIsNone(state_store.load(path))
        cfg = cowork.default_config(["scout", "advisor"])
        state_store.save_config(path, ["scout", "advisor"], cfg)
        loaded = state_store.load(path)
        self.assertTrue(state_store.has_config(loaded))
        self.assertEqual(loaded["team"], ["scout", "advisor"])
        self.assertEqual(loaded["config"]["scout"]["controller"], "claude")

    def test_role_session_roundtrip_and_controller_match(self):
        path = self._tmp()
        state_store.save_config(path, ["scout"], cowork.default_config(["scout"]))
        state_store.save_role_session(path, "scout", "claude", "uuid-123")
        loaded = state_store.load(path)
        self.assertEqual(
            state_store.get_role_session(loaded, "scout", "claude"), "uuid-123")
        # controller mismatch -> no resume id
        self.assertIsNone(
            state_store.get_role_session(loaded, "scout", "codex"))

    def test_ensure_session_mints_and_persists_uuid_once(self):
        path = self._tmp()
        s1 = state_store.ensure_session(path, None, "fixed-uuid")
        self.assertEqual(state_store.get_session_uuid(s1), "fixed-uuid")
        self.assertEqual(
            state_store.get_session_uuid(state_store.load(path)), "fixed-uuid")
        # a second call with a different candidate must not overwrite it
        s2 = state_store.ensure_session(path, state_store.load(path), "other")
        self.assertEqual(state_store.get_session_uuid(s2), "fixed-uuid")

    def test_read_status(self):
        path = self._tmp()
        self.assertIsNone(state_store.read_status(path))  # missing
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as fh:
            fh.write('{"session":"x","role":"scout","status":"needs_input","result":{}}')
        self.assertEqual(state_store.read_status(path), "needs_input")
        with open(path, "w") as fh:
            fh.write("not json")
        self.assertIsNone(state_store.read_status(path))

    def test_load_rejects_incompatible_version(self):
        path = self._tmp()
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as fh:
            fh.write('{"version": 999, "team": ["scout"], "config": {}}')
        self.assertIsNone(state_store.load(path))


class SessionFlowTest(unittest.TestCase):
    def _tmp_session(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return os.path.join(d, ".cowork", "session.json")

    def _args(self, argv):
        return cowork.build_parser().parse_args(argv)

    def test_config_saved_then_reused_and_session_resumed(self):
        spath = self._tmp_session()

        def fake_scout(config, context, selected, io_in=None, io_out=None,
                      resume_id=None, on_session=None, intel_path=None,
                      review_path=None, **kwargs):
            fake_scout.last_resume = resume_id
            fake_scout.last_intel = intel_path
            if on_session and resume_id is None:
                on_session("claude", "sess-abc")  # simulate id capture
            return 0
        fake_scout.last_resume = "unset"
        fake_scout.last_intel = None

        # Run 1: choose config via args, scout saves its session id.
        rc = cowork.run_flow(
            self._args(["--team", "scout", "--config", "scout=claude,yolo,plan",
                        "--context", "first", "--session-file", spath]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c,
            run_scout_fn=fake_scout)
        self.assertEqual(rc, 0)
        self.assertIsNone(fake_scout.last_resume)
        saved = state_store.load(spath)
        self.assertTrue(state_store.has_config(saved))
        self.assertEqual(state_store.get_role_session(saved, "scout", "claude"),
                         "sess-abc")
        # a cowork session uuid is minted, persisted, and names the intel file
        suid = state_store.get_session_uuid(saved)
        self.assertTrue(suid)
        self.assertIn("scout.intel.%s.json" % suid, fake_scout.last_intel)

        # Run 2: only context + session file -> config reused, session resumed.
        out = io.StringIO()
        rc = cowork.run_flow(
            self._args(["--context", "second", "--session-file", spath]),
            io_out=out, which=lambda c: "/bin/" + c, run_scout_fn=fake_scout)
        self.assertEqual(rc, 0)
        self.assertIn("using saved session config", out.getvalue())
        self.assertEqual(fake_scout.last_resume, "sess-abc")
        # session uuid is stable across runs
        self.assertEqual(state_store.get_session_uuid(state_store.load(spath)), suid)
        self.assertIn(suid, fake_scout.last_intel)

    def test_no_session_writes_nothing(self):
        spath = self._tmp_session()
        cowork.run_flow(
            self._args(["--team", "scout", "--context", "x",
                        "--session-file", spath, "--no-session"]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c,
            run_scout_fn=lambda *a, **k: 0)
        self.assertFalse(os.path.exists(spath))


class ProbeTest(unittest.TestCase):
    def test_probe_accepts_assistant(self):
        def spawn(cmd, stdin):
            return [{"type": "assistant", "message": {"content": [
                {"type": "text", "text": "pong"}]}}]
        ok, alert = bridge.probe_claude_stream_json(spawn)
        self.assertTrue(ok)
        self.assertIsNone(alert)

    def test_probe_rejects_unsupported(self):
        def spawn(cmd, stdin):
            return [{"type": "other"}]
        ok, alert = bridge.probe_claude_stream_json(spawn)
        self.assertFalse(ok)
        self.assertIn("stream-json", alert)

    def test_probe_spawn_failure(self):
        def spawn(cmd, stdin):
            raise OSError("claude: not found")
        ok, alert = bridge.probe_claude_stream_json(spawn)
        self.assertFalse(ok)
        self.assertIn("not found", alert)


class FramingTest(unittest.TestCase):
    def test_encode_user_message(self):
        import json
        line = bridge.encode_user_message("hello")
        self.assertTrue(line.endswith("\n"))
        obj = json.loads(line)
        self.assertEqual(obj["type"], "user")
        self.assertEqual(obj["message"]["role"], "user")
        self.assertEqual(obj["message"]["content"][0]["text"], "hello")

    def test_parse_claude_assistant_and_result(self):
        a = bridge.parse_claude_event(
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "hi"}]}})
        self.assertEqual(a, {"kind": "assistant", "text": "hi"})
        r = bridge.parse_claude_event(
            {"type": "result", "subtype": "success", "result": "done"})
        self.assertEqual(r["kind"], "result")
        self.assertFalse(r["is_error"])

    def test_parse_claude_partial_text_delta(self):
        ev = {"type": "stream_event",
              "event": {"delta": {"type": "text_delta", "text": "hel"}}}
        self.assertEqual(bridge.parse_claude_event(ev),
                         {"kind": "partial", "text": "hel"})
        # non-text deltas are partials with no text
        ev2 = {"type": "stream_event", "event": {"delta": {"type": "input_json"}}}
        self.assertEqual(bridge.parse_claude_event(ev2)["kind"], "partial")

    def test_speaker_label(self):
        self.assertEqual(bridge.speaker_label("scout"), "scout › ")
        self.assertEqual(bridge.USER_LABEL, "you › ")

    def test_claude_command_streams_partials(self):
        cmd = bridge.build_claude_command("roles/scout.md", "implement", True)
        self.assertIn("--include-partial-messages", cmd)
        self.assertIn("--dangerously-skip-permissions", cmd)  # implement+yolo

    def test_parse_codex_events(self):
        ts = bridge.parse_codex_event({"type": "thread.started", "thread_id": "T1"})
        self.assertEqual(ts, {"kind": "thread_started", "thread_id": "T1"})
        msg = bridge.parse_codex_event(
            {"type": "item.completed", "item": {"type": "agent_message",
                                                "text": "context map"}})
        self.assertEqual(msg, {"kind": "message", "text": "context map"})

    def test_capture_thread_id(self):
        events = [
            {"type": "turn.started"},
            {"type": "thread.started", "thread_id": "T-42"},
            {"type": "turn.completed"},
        ]
        self.assertEqual(bridge.capture_thread_id(events), "T-42")
        self.assertIsNone(bridge.capture_thread_id([{"type": "turn.started"}]))


class DenialTest(unittest.TestCase):
    def test_claude_permission_denied(self):
        ev = {"type": "assistant", "message": {"content": [
            {"type": "tool_result", "is_error": True,
             "content": [{"type": "text", "text": "Permission denied for Bash"}]}]}}
        self.assertEqual(bridge.parse_claude_event(ev)["kind"], "denied")

    def test_codex_rejected_item(self):
        ev = {"type": "item.completed",
              "item": {"type": "command_execution", "status": "rejected",
                       "text": "rm -rf"}}
        self.assertEqual(bridge.parse_codex_event(ev)["kind"], "denied")

    def test_codex_error(self):
        ev = {"type": "error", "message": "sandbox violation"}
        parsed = bridge.parse_codex_event(ev)
        self.assertEqual(parsed["kind"], "error")
        self.assertIn("sandbox", parsed["text"])


class FallthroughTest(unittest.TestCase):
    def test_brief_with_planner(self):
        brief = cowork.assemble_scout_brief(
            ["scout", "planner"], ".cowork/scout.intel.S.json")
        self.assertIn("do NOT produce a plan", brief)
        self.assertIn(".cowork/scout.intel.S.json", brief)
        self.assertIn("ONLY write target", brief)

    def test_brief_without_planner(self):
        brief = cowork.assemble_scout_brief(
            ["scout", "advisor"], ".cowork/scout.intel.S.json")
        self.assertIn("lightweight plan", brief)

    def test_brief_requires_json(self):
        brief = cowork.assemble_scout_brief(["scout"], "/tmp/x.json")
        self.assertIn("JSON", brief)

    def test_scout_intel_path(self):
        self.assertEqual(
            cowork.scout_intel_path(".cowork", "abc-123"),
            ".cowork/scout.intel.abc-123.json")

    def test_codex_prompt_includes_all_parts(self):
        prompt = cowork.assemble_codex_prompt("ROLE", "TEAM", "CTX")
        self.assertIn("ROLE", prompt)
        self.assertIn("TEAM", prompt)
        self.assertIn("CTX", prompt)


class RunScoutTest(unittest.TestCase):
    def test_run_scout_claude_probe_fail_aborts(self):
        config = {"scout": {"controller": "claude", "yolo": True, "mode": "plan"}}
        out = io.StringIO()

        def bad_spawn(cmd, stdin):
            return [{"type": "other"}]

        rc = cowork.run_scout(config, "ctx", ["scout", "planner"],
                              io_in=io.StringIO(""), io_out=out,
                              claude_spawn=bad_spawn)
        self.assertEqual(rc, 1)
        self.assertIn("cowork:", out.getvalue())


class InterruptTest(unittest.TestCase):
    def _main_with(self, exc):
        import contextlib
        orig = cowork.run_flow
        cowork.run_flow = lambda *a, **k: (_ for _ in ()).throw(exc)
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = cowork.main(["--team", "scout"])
            return rc, err.getvalue()
        finally:
            cowork.run_flow = orig

    def test_keyboard_interrupt_exits_130(self):
        rc, err = self._main_with(KeyboardInterrupt())
        self.assertEqual(rc, 130)
        self.assertIn("interrupted", err)

    def test_eof_exits_130(self):
        rc, err = self._main_with(EOFError())
        self.assertEqual(rc, 130)

    def test_terminate_kills_live_proc(self):
        class FakeProc:
            def __init__(self):
                self.state = "running"
                self.terminated = False
            def poll(self):
                return None if self.state == "running" else 0
            def terminate(self):
                self.terminated = True
                self.state = "done"
            def wait(self, timeout=None):
                return 0
            def kill(self):
                self.state = "done"
        p = FakeProc()
        bridge._terminate(p)
        self.assertTrue(p.terminated)

    def test_terminate_noop_when_already_exited(self):
        class Dead:
            def poll(self):
                return 0
            def terminate(self):
                raise AssertionError("should not terminate an exited process")
        bridge._terminate(Dead())  # must not raise


class ScoutLoopTest(unittest.TestCase):
    """Drive _scout_loop with a fake session that writes intel statuses."""

    def _intel(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return os.path.join(d, ".cowork", "scout.intel.X.json")

    def _session(self, intel_path, statuses):
        test = self

        class FakeSession:
            def __init__(self):
                self.sent = []
                self.closed = False

            def send(self, text):
                self.sent.append(text)
                st = statuses.pop(0) if statuses else "ready_for_review"
                os.makedirs(os.path.dirname(intel_path), exist_ok=True)
                with open(intel_path, "w") as fh:
                    json.dump({"session": "X", "role": "scout",
                               "status": st, "result": {}}, fh)

            def close(self):
                self.closed = True
        return FakeSession()

    def test_needs_input_then_review_then_approve(self):
        intel = self._intel()
        sess = self._session(intel, ["needs_input", "ready_for_review"])
        out = io.StringIO()
        rc = cowork._scout_loop(
            sess, "seed", intel, context="ctx",
            io_in=io.StringIO("answer 1\n\n"), io_out=out)
        self.assertEqual(rc, 0)
        self.assertEqual(sess.sent, ["seed", "answer 1"])
        self.assertTrue(sess.closed)
        text = out.getvalue()
        self.assertIn("scout needs your input", text)
        self.assertIn("ready for review", text)
        self.assertIn("scout finished", text)

    def test_review_revise_then_approve(self):
        intel = self._intel()
        sess = self._session(intel, ["ready_for_review", "ready_for_review"])
        out = io.StringIO()
        rc = cowork._scout_loop(
            sess, "seed", intel, context="",
            io_in=io.StringIO("more feedback\n\n"), io_out=out)
        self.assertEqual(rc, 0)
        self.assertEqual(sess.sent, ["seed", "more feedback"])
        self.assertTrue(sess.closed)

    def test_blank_reprompts_then_eof_ends(self):
        # A blank line no longer aborts (#10): it re-prompts. Here the blank is
        # followed by EOF, which legitimately ends the loop — so still only the
        # seed was sent. The re-prompt means send() was NOT called again.
        intel = self._intel()
        sess = self._session(intel, ["needs_input"])
        out = io.StringIO()
        rc = cowork._scout_loop(
            sess, "seed", intel, context="", io_in=io.StringIO("\n"), io_out=out)
        self.assertEqual(rc, 0)
        self.assertEqual(sess.sent, ["seed"])  # blank re-prompted, EOF then ended
        self.assertTrue(sess.closed)

    def test_blank_reprompts_then_answers(self):
        # Prove a blank line re-prompts rather than ending: a blank followed by a
        # real answer must still deliver that answer as the next turn.
        intel = self._intel()
        sess = self._session(intel, ["needs_input", "ready_for_review"])
        out = io.StringIO()
        rc = cowork._scout_loop(
            sess, "seed", intel, context="",
            io_in=io.StringIO("\nreal answer\n\n"), io_out=out)
        self.assertEqual(rc, 0)
        self.assertEqual(sess.sent, ["seed", "real answer"])
        self.assertTrue(sess.closed)

    def test_slash_quit_ends_loop(self):
        intel = self._intel()
        sess = self._session(intel, ["needs_input"])
        out = io.StringIO()
        rc = cowork._scout_loop(
            sess, "seed", intel, context="",
            io_in=io.StringIO("/quit\n"), io_out=out)
        self.assertEqual(rc, 0)
        self.assertEqual(sess.sent, ["seed"])  # /quit ended before another send
        self.assertTrue(sess.closed)


class SessionClassTest(unittest.TestCase):
    def test_claude_session_streams_and_reports_session_id(self):
        import unittest.mock as mock

        class FakeStdin:
            def __init__(self):
                self.data = []

            def write(self, s):
                self.data.append(s)

            def flush(self):
                pass

            def close(self):
                pass

        class FakeProc:
            def __init__(self, lines):
                self.stdout = iter(lines)
                self.stdin = FakeStdin()

            def poll(self):
                return 0

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        lines = [
            json.dumps({"type": "stream_event",
                        "event": {"delta": {"type": "text_delta", "text": "hi"}}}),
            json.dumps({"type": "result", "subtype": "success",
                        "result": "hi", "session_id": "S1"}),
        ]
        got = {}
        with mock.patch.object(bridge.subprocess, "Popen",
                               return_value=FakeProc(lines)):
            out = io.StringIO()
            s = bridge.ClaudeSession(
                "roles/scout.md", "implement", True, io_out=out,
                on_session_id=lambda i: got.setdefault("id", i))
            s.send("hello")
        self.assertIn("scout › hi", out.getvalue())
        self.assertEqual(got.get("id"), "S1")
        self.assertEqual(s.proc.stdin.data[0],
                         bridge.encode_user_message("hello"))

    def test_claude_session_separates_text_blocks(self):
        import unittest.mock as mock

        class FakeStdin:
            def write(self, s):
                pass

            def flush(self):
                pass

            def close(self):
                pass

        class FakeProc:
            def __init__(self, lines):
                self.stdout = iter(lines)
                self.stdin = FakeStdin()

            def poll(self):
                return 0

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        lines = [
            json.dumps({"type": "stream_event", "event": {
                "type": "content_block_start", "content_block": {"type": "text"}}}),
            json.dumps({"type": "stream_event", "event": {
                "delta": {"type": "text_delta", "text": "first."}}}),
            # tool use happens, then a new text block resumes narration
            json.dumps({"type": "stream_event", "event": {
                "type": "content_block_start", "content_block": {"type": "text"}}}),
            json.dumps({"type": "stream_event", "event": {
                "delta": {"type": "text_delta", "text": "Second."}}}),
            json.dumps({"type": "result", "subtype": "success", "result": ""}),
        ]
        with mock.patch.object(bridge.subprocess, "Popen",
                               return_value=FakeProc(lines)):
            out = io.StringIO()
            s = bridge.ClaudeSession("roles/scout.md", "implement", True, io_out=out)
            s.send("go")
        # blocks separated, not "first.Second."
        self.assertNotIn("first.Second.", out.getvalue())
        self.assertIn("first.", out.getvalue())
        self.assertIn("Second.", out.getvalue())

    def test_codex_session_first_then_resume(self):
        recorded = {"cmds": [], "tid": None}

        class FakeCodex(bridge.CodexSession):
            def _run(self, command):
                recorded["cmds"].append(command)
                return [{"type": "thread.started", "thread_id": "T1"}]

        s = FakeCodex("implement", True, io_out=io.StringIO(),
                      on_thread_id=lambda i: recorded.__setitem__("tid", i))
        s.send("first")
        s.send("second")
        self.assertEqual(recorded["cmds"][0][:4],
                         ["codex", "exec", "--json", "--skip-git-repo-check"])
        self.assertEqual(recorded["cmds"][0][-1], "first")
        self.assertEqual(
            recorded["cmds"][1],
            ["codex", "exec", "resume", "--json", "--skip-git-repo-check",
             "T1", "second"])
        self.assertEqual(recorded["tid"], "T1")


class AdditiveTest(unittest.TestCase):
    """cowork must stay additive: it must not import or reference the existing
    co-plan helper, and the existing files must still be present."""

    def test_cowork_does_not_import_co_plan_file(self):
        import ast
        for name in ("cowork.py", "cowork_bridge.py", "cowork_preflight.py",
                     "cowork_state.py", "cowork_ui.py"):
            with open(os.path.join(_HERE, name)) as fh:
                tree = ast.parse(fh.read(), filename=name)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn("co_plan_file", alias.name,
                                         "%s must not import co_plan_file" % name)
                elif isinstance(node, ast.ImportFrom):
                    self.assertNotIn("co_plan_file", node.module or "",
                                     "%s must not import co_plan_file" % name)

    def test_existing_skill_files_present(self):
        root = os.path.dirname(_HERE)
        for rel in ("SKILL.md", "scripts/co_plan_file.py"):
            self.assertTrue(os.path.exists(os.path.join(root, rel)))


class ScoutReviewerRegistrationTest(unittest.TestCase):
    def test_role_registered_with_codex_yolo_implement(self):
        self.assertIn("scout-reviewer", cowork.ROLES)
        # placed right after scout (paired reviewer), before the sequential revisor
        self.assertEqual(cowork.ROLES.index("scout-reviewer"), 1)
        self.assertIn("revisor", cowork.ROLES)  # reserved slot preserved
        self.assertEqual(
            cowork.DEFAULTS["scout-reviewer"],
            {"controller": "codex", "yolo": True, "mode": "implement"})

    def test_role_prompt_file_exists(self):
        self.assertTrue(os.path.exists(cowork.SCOUT_REVIEWER_PROMPT_PATH))


class ReadReviewTest(unittest.TestCase):
    def _tmp(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return os.path.join(d, ".cowork", "scout-review.X.json")

    def _write(self, path, obj):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(obj, fh)

    def test_missing_returns_none(self):
        self.assertIsNone(state_store.read_review(self._tmp()))
        self.assertIsNone(state_store.read_review(""))

    def test_valid_verdicts_preserved(self):
        path = self._tmp()
        self._write(path, {"verdict": "approve", "findings": []})
        self.assertEqual(state_store.read_review(path)["verdict"], "approve")
        self._write(path, {"verdict": "needs_user",
                           "user_question": "per-device or per-account?"})
        got = state_store.read_review(path)
        self.assertEqual(got["verdict"], "needs_user")
        self.assertEqual(got["user_question"], "per-device or per-account?")

    def test_malformed_degrades_to_safe_revise(self):
        path = self._tmp()
        # present but no/invalid verdict -> safe non-approving default
        self._write(path, {"role": "scout-reviewer", "findings": ["x"]})
        got = state_store.read_review(path)
        self.assertEqual(got["verdict"], "revise")
        self.assertTrue(got["malformed"])
        self._write(path, {"verdict": "maybe"})
        self.assertEqual(state_store.read_review(path)["verdict"], "revise")

    def test_needs_user_without_question_degrades_to_revise(self):
        path = self._tmp()
        self._write(path, {"verdict": "needs_user"})            # no user_question
        got = state_store.read_review(path)
        self.assertEqual(got["verdict"], "revise")
        self.assertTrue(got["malformed"])
        self._write(path, {"verdict": "needs_user", "user_question": "   "})
        self.assertEqual(state_store.read_review(path)["verdict"], "revise")

    def test_non_json_returns_none(self):
        path = self._tmp()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("not json")
        self.assertIsNone(state_store.read_review(path))

    def test_review_path_for(self):
        self.assertEqual(
            state_store.review_path_for(".cowork", "abc-123"),
            ".cowork/scout-review.abc-123.json")


class ReviewerContextTest(unittest.TestCase):
    """B1 guard: the reviewer shares the user context + intel, NOT the scout's
    write-target brief / first payload."""

    def _intel(self, obj):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        path = os.path.join(d, ".cowork", "scout.intel.X.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(obj, fh)
        return path

    def test_context_has_user_context_and_intel_not_scout_brief(self):
        intel = self._intel({"status": "ready_for_review",
                             "result": {"objective": "dark mode toggle"}})
        selected = ["scout", "scout-reviewer"]
        ctx = cowork.assemble_reviewer_context(
            "add a dark-mode toggle", selected, intel)
        self.assertIn("add a dark-mode toggle", ctx)      # shared user context
        self.assertIn("dark mode toggle", ctx)            # intel JSON embedded
        # B1: the scout's write-target brief must NOT leak into the reviewer.
        scout_brief = cowork.assemble_scout_brief(selected, intel)
        self.assertNotIn(scout_brief, ctx)
        self.assertNotIn("do NOT produce a plan", ctx)

    def test_reviewer_brief_targets_review_file_only(self):
        brief = cowork.assemble_reviewer_brief(".cowork/scout-review.X.json")
        self.assertIn(".cowork/scout-review.X.json", brief)
        self.assertIn("ONLY write target", brief)
        self.assertIn("Do NOT edit the scout intel", brief)


class ReviewerHandoffTest(unittest.TestCase):
    """Faithful-relay handoff template (pure string templating, no model call)."""

    def test_needs_user_carries_full_question_and_relay_instruction(self):
        out = cowork.assemble_reviewer_handoff(
            "needs_user",
            {"user_question": "Persist per-device, or per-account when logged in?"})
        self.assertIn("[reviewer handoff]", out)
        self.assertIn("Persist per-device, or per-account when logged in?", out)
        self.assertIn("NOT change its meaning", out)
        self.assertIn("needs_input", out)

    def test_revise_lists_findings(self):
        out = cowork.assemble_reviewer_handoff(
            "revise", {"findings": ["cited file is wrong", "tighten assumption Y"]})
        self.assertIn("[reviewer handoff]", out)
        self.assertIn("cited file is wrong", out)
        self.assertIn("tighten assumption Y", out)
        self.assertIn("ready_for_review", out)

    def test_approve_is_empty(self):
        self.assertEqual(cowork.assemble_reviewer_handoff("approve", {}), "")


class ScoutLoopReviewTest(unittest.TestCase):
    """Drive _scout_loop with an injected review_fn (topology D)."""

    def _intel(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return os.path.join(d, ".cowork", "scout.intel.X.json")

    def _session(self, intel_path, statuses):
        class FakeSession:
            def __init__(self):
                self.sent = []
                self.closed = False

            def send(self, text):
                self.sent.append(text)
                st = statuses.pop(0) if statuses else "ready_for_review"
                os.makedirs(os.path.dirname(intel_path), exist_ok=True)
                with open(intel_path, "w") as fh:
                    json.dump({"status": st}, fh)

            def close(self):
                self.closed = True
        return FakeSession()

    def _review_fn(self, verdicts):
        calls = {"n": 0}

        def review_fn(intel_path, round_index):
            calls["n"] += 1
            return verdicts.pop(0) if verdicts else {"verdict": "approve"}
        review_fn.calls = calls
        return review_fn

    def test_revise_injected_then_approve_runs_user_gate(self):
        intel = self._intel()
        sess = self._session(intel, ["ready_for_review", "ready_for_review"])
        rfn = self._review_fn([
            {"verdict": "revise", "findings": ["fix the cited path"]},
            {"verdict": "approve"},
        ])
        out = io.StringIO()
        rc = cowork._scout_loop(sess, "seed", intel, context="",
                                io_in=io.StringIO(""),  # "" at the gate => approve
                                io_out=out, review_fn=rfn)
        self.assertEqual(rc, 0)
        # the reviewer's revise was injected as the scout's next turn...
        self.assertEqual(len(sess.sent), 2)
        self.assertEqual(sess.sent[0], "seed")
        self.assertIn("[reviewer handoff]", sess.sent[1])
        self.assertIn("fix the cited path", sess.sent[1])
        # ...and the reviewer ran twice; the user only saw the 'reviewed' marker.
        self.assertEqual(rfn.calls["n"], 2)
        self.assertIn("reviewed", out.getvalue())
        # single-voice: reviewer finding text never reached the user channel.
        self.assertNotIn("fix the cited path", out.getvalue())

    def test_round_cap_falls_through_to_user_with_dissent(self):
        intel = self._intel()
        sess = self._session(intel, ["ready_for_review", "ready_for_review",
                                     "ready_for_review"])
        rfn = self._review_fn([
            {"verdict": "revise", "findings": ["first concern"]},
            {"verdict": "revise", "findings": ["still not aligned"]},
            {"verdict": "revise", "findings": ["should not be reached"]},
        ])
        out = io.StringIO()
        rc = cowork._scout_loop(sess, "seed", intel, context="",
                                io_in=io.StringIO(""), io_out=out, review_fn=rfn)
        self.assertEqual(rc, 0)
        # cap=2 -> reviewer called at most twice, then user gate with dissent
        self.assertEqual(rfn.calls["n"], cowork.REVIEW_ROUND_CAP)
        self.assertIn("reviewer's unresolved notes", out.getvalue())
        self.assertIn("still not aligned", out.getvalue())

    def test_needs_user_drives_scout_back_to_user_question(self):
        intel = self._intel()
        sess = self._session(intel, ["ready_for_review", "needs_input"])
        rfn = self._review_fn([
            {"verdict": "needs_user",
             "user_question": "per-device or per-account?"},
        ])
        out = io.StringIO()
        rc = cowork._scout_loop(sess, "seed", intel, context="",
                                io_in=io.StringIO(""),  # EOF at the needs_input turn
                                io_out=out, review_fn=rfn)
        self.assertEqual(rc, 0)
        self.assertEqual(len(sess.sent), 2)
        self.assertIn("[reviewer handoff]", sess.sent[1])
        self.assertIn("per-device or per-account?", sess.sent[1])
        self.assertIn("needs_input", sess.sent[1])

    def test_missing_review_is_safe_revise_not_approve(self):
        # review_fn returns None (missing/unreadable review file) -> must be
        # treated as revise, never a silent fall-through to approval.
        intel = self._intel()
        sess = self._session(intel, ["ready_for_review", "ready_for_review"])
        calls = {"n": 0}

        def review_fn(intel_path, round_index):
            calls["n"] += 1
            return None

        out = io.StringIO()
        rc = cowork._scout_loop(sess, "seed", intel, context="",
                                io_in=io.StringIO(""), io_out=out,
                                review_fn=review_fn)
        self.assertEqual(rc, 0)
        # round 1 None -> revise handoff injected (not the user gate)
        self.assertIn("[reviewer handoff]", sess.sent[1])
        # cap reached -> user gate with a generic non-approval dissent
        self.assertEqual(calls["n"], cowork.REVIEW_ROUND_CAP)
        self.assertIn("reviewer did not approve", out.getvalue())

    def test_unknown_verdict_is_safe_revise(self):
        intel = self._intel()
        sess = self._session(intel, ["ready_for_review", "ready_for_review"])
        rfn = self._review_fn([{"verdict": "lgtm"}, {"verdict": "approve"}])
        out = io.StringIO()
        rc = cowork._scout_loop(sess, "seed", intel, context="",
                                io_in=io.StringIO(""), io_out=out, review_fn=rfn)
        self.assertEqual(rc, 0)
        # unknown verdict did NOT approve on round 1; it injected a revise handoff
        self.assertIn("[reviewer handoff]", sess.sent[1])

    def test_needs_user_without_question_does_not_relay_empty(self):
        intel = self._intel()
        sess = self._session(intel, ["ready_for_review", "ready_for_review"])
        # needs_user but empty question -> safe revise, never an empty relay
        rfn = self._review_fn([{"verdict": "needs_user", "user_question": ""},
                               {"verdict": "approve"}])
        out = io.StringIO()
        rc = cowork._scout_loop(sess, "seed", intel, context="",
                                io_in=io.StringIO(""), io_out=out, review_fn=rfn)
        self.assertEqual(rc, 0)
        self.assertIn("[reviewer handoff]", sess.sent[1])
        self.assertNotIn("Question:", sess.sent[1])   # not a needs_user relay

    def test_no_review_fn_keeps_legacy_user_gate(self):
        intel = self._intel()
        sess = self._session(intel, ["ready_for_review"])
        out = io.StringIO()
        rc = cowork._scout_loop(sess, "seed", intel, context="",
                                io_in=io.StringIO(""), io_out=out)  # no review_fn
        self.assertEqual(rc, 0)
        self.assertEqual(sess.sent, ["seed"])
        self.assertNotIn("reviewed", out.getvalue())


class MakeReviewFnTest(unittest.TestCase):
    def test_none_when_reviewer_not_selected(self):
        self.assertIsNone(cowork.make_review_fn(
            cowork.default_config(["scout"]), "ctx", ["scout"],
            ".cowork/scout-review.X.json"))

    def test_none_without_review_path(self):
        self.assertIsNone(cowork.make_review_fn(
            cowork.default_config(["scout", "scout-reviewer"]), "ctx",
            ["scout", "scout-reviewer"], None))

    def test_builds_callable_that_invokes_runner(self):
        seen = {}

        def runner(config, context, selected, intel_path, review_path,
                   resume_id=None, on_session=None, context_update=None):
            seen["intel"] = intel_path
            seen["review"] = review_path
            return {"verdict": "approve"}

        fn = cowork.make_review_fn(
            cowork.default_config(["scout", "scout-reviewer"]), "ctx",
            ["scout", "scout-reviewer"], ".cowork/scout-review.X.json",
            reviewer_runner=runner)
        self.assertIsNotNone(fn)
        verdict = fn(".cowork/scout.intel.X.json", 1)
        self.assertEqual(verdict["verdict"], "approve")
        self.assertEqual(seen["intel"], ".cowork/scout.intel.X.json")
        self.assertEqual(seen["review"], ".cowork/scout-review.X.json")

    def test_persistent_reviewer_id_reused_and_persisted(self):
        # First pass creates the reviewer session (id captured + persisted);
        # the second pass resumes it (gets the first pass's id).
        seen = []
        persisted = []

        def runner(config, context, selected, intel_path, review_path,
                   resume_id=None, on_session=None, context_update=None):
            seen.append(resume_id)
            if resume_id is None and on_session:
                on_session("codex", "rev-thread-1")  # capture a fresh id
            return {"verdict": "revise", "findings": ["x"]}

        fn = cowork.make_review_fn(
            cowork.default_config(["scout", "scout-reviewer"]), "ctx",
            ["scout", "scout-reviewer"], ".cowork/scout-review.X.json",
            reviewer_runner=runner,
            on_reviewer_session=lambda c, i: persisted.append((c, i)))
        fn(".cowork/scout.intel.X.json", 1)
        fn(".cowork/scout.intel.X.json", 2)
        self.assertEqual(seen, [None, "rev-thread-1"])      # 2nd pass resumes
        self.assertEqual(persisted, [("codex", "rev-thread-1")])

    def test_seeded_resume_id_used_on_first_pass(self):
        # On a cowork resume the stored reviewer id seeds make_review_fn and is
        # used from the very first pass.
        seen = []

        def runner(config, context, selected, intel_path, review_path,
                   resume_id=None, on_session=None, context_update=None):
            seen.append(resume_id)
            return {"verdict": "approve"}

        fn = cowork.make_review_fn(
            cowork.default_config(["scout", "scout-reviewer"]), "ctx",
            ["scout", "scout-reviewer"], ".cowork/scout-review.X.json",
            reviewer_runner=runner, reviewer_resume_id="stored-rev-id")
        fn(".cowork/scout.intel.X.json", 1)
        self.assertEqual(seen, ["stored-rev-id"])


class ContextRevisionStoreTest(unittest.TestCase):
    """The versioned shared session context + per-role acknowledgment."""

    def _tmp(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return os.path.join(d, ".cowork", "session.json")

    def test_save_context_bumps_revision_only_on_change(self):
        path = self._tmp()
        s = state_store.save_context(path, "goal v1")
        self.assertEqual(state_store.get_context(s), "goal v1")
        self.assertEqual(state_store.get_context_revision(s), 1)
        self.assertTrue(s["context"]["hash"])
        self.assertEqual(s["context"]["source"], "--context")
        # identical text -> no-op, same revision
        s = state_store.save_context(path, "goal v1", prior=s)
        self.assertEqual(state_store.get_context_revision(s), 1)
        # changed text -> revision bump
        s = state_store.save_context(path, "goal v2", prior=s)
        self.assertEqual(state_store.get_context_revision(s), 2)
        self.assertEqual(state_store.get_context(s), "goal v2")

    def test_legacy_plain_string_context_tolerated(self):
        state = {"context": "old plain context"}
        self.assertEqual(state_store.get_context(state), "old plain context")
        self.assertEqual(state_store.get_context_revision(state), 1)

    def test_seen_revision_and_gap(self):
        path = self._tmp()
        s = state_store.save_context(path, "goal")
        self.assertEqual(state_store.get_seen_revision(s, "scout-reviewer"), 0)
        self.assertEqual(state_store.role_context_gap(s, "scout-reviewer"), "goal")
        s = state_store.mark_context_seen(path, "scout-reviewer", 1, prior=s)
        self.assertEqual(state_store.get_seen_revision(s, "scout-reviewer"), 1)
        self.assertIsNone(state_store.role_context_gap(s, "scout-reviewer"))
        # a new revision reopens the gap
        s = state_store.save_context(path, "redirected goal", prior=s)
        self.assertEqual(state_store.role_context_gap(s, "scout-reviewer"),
                         "redirected goal")

    def test_save_role_session_preserves_seen_revision(self):
        path = self._tmp()
        s = state_store.save_context(path, "goal")
        s = state_store.mark_context_seen(path, "scout", 1, prior=s)
        # refreshing the session id must not clobber the acknowledgment
        s = state_store.save_role_session(path, "scout", "claude", "id-2", prior=s)
        self.assertEqual(state_store.get_seen_revision(s, "scout"), 1)
        self.assertEqual(state_store.get_role_session(s, "scout", "claude"), "id-2")


class ContextUpdateBlockTest(unittest.TestCase):
    def test_block_framing(self):
        block = cowork.context_update_block("the new goal")
        self.assertIn("New user context was provided", block)
        self.assertIn("<context>\nthe new goal\n</context>", block)
        self.assertIn("Keep prior session knowledge", block)

    def test_resumed_reviewer_prompt_includes_update_block(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        intel = os.path.join(d, "scout.intel.X.json")
        with open(intel, "w") as fh:
            json.dump({"status": "ready_for_review"}, fh)
        ctx = cowork.assemble_reviewer_resume_context(
            intel, context_update="redirected goal")
        self.assertIn("<context>\nredirected goal\n</context>", ctx)
        self.assertIn("ready_for_review", ctx)   # intel still included
        # without an update there is no block
        self.assertNotIn("<context>",
                         cowork.assemble_reviewer_resume_context(intel))


class ReviewFnContextAckTest(unittest.TestCase):
    """make_review_fn delivers the update block once and acks once."""

    def test_update_delivered_once_then_acked(self):
        seen_updates = []
        acks = []

        def runner(config, context, selected, intel_path, review_path,
                   resume_id=None, on_session=None, context_update=None):
            seen_updates.append(context_update)
            return {"verdict": "revise", "findings": ["x"]}

        fn = cowork.make_review_fn(
            cowork.default_config(["scout", "scout-reviewer"]), "current goal",
            ["scout", "scout-reviewer"], ".cowork/scout-review.X.json",
            reviewer_runner=runner, reviewer_resume_id="rev-1",
            context_update="current goal",
            on_context_ack=lambda: acks.append(True))
        fn(".cowork/scout.intel.X.json", 1)
        fn(".cowork/scout.intel.X.json", 2)
        # block on the first pass only; ack exactly once
        self.assertEqual(seen_updates, ["current goal", None])
        self.assertEqual(acks, [True])

    def test_failed_pass_does_not_ack(self):
        acks = []

        def runner(config, context, selected, intel_path, review_path,
                   resume_id=None, on_session=None, context_update=None):
            return None  # reviewer never produced a verdict

        fn = cowork.make_review_fn(
            cowork.default_config(["scout", "scout-reviewer"]), "goal",
            ["scout", "scout-reviewer"], ".cowork/scout-review.X.json",
            reviewer_runner=runner, reviewer_resume_id="rev-1",
            context_update="goal", on_context_ack=lambda: acks.append(True))
        fn(".cowork/scout.intel.X.json", 1)
        self.assertEqual(acks, [])  # no verdict -> revision not acknowledged


class ReviewerSessionFlowTest(unittest.TestCase):
    """run_flow persists the reviewer session id + original context, and on a
    resume hands the reviewer its stored id and the original context."""

    def _tmp_session(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return os.path.join(d, ".cowork", "session.json")

    def _args(self, argv):
        return cowork.build_parser().parse_args(argv)

    def test_reviewer_id_and_context_persist_across_resume(self):
        spath = self._tmp_session()
        rec = []

        def fake(config, context, selected, io_in=None, io_out=None,
                 resume_id=None, on_session=None, intel_path=None,
                 review_path=None, reviewer_resume_id=None,
                 on_reviewer_session=None, reviewer_context=None,
                 reviewer_context_update=None, on_reviewer_context_ack=None,
                 **kw):
            rec.append({"resume_id": resume_id,
                        "reviewer_resume_id": reviewer_resume_id,
                        "reviewer_context": reviewer_context,
                        "reviewer_context_update": reviewer_context_update,
                        "context": context})
            if on_session and resume_id is None:
                on_session(config["scout"]["controller"], "scout-1")
            if on_reviewer_session and reviewer_resume_id is None:
                on_reviewer_session("codex", "rev-1")
            if on_reviewer_context_ack:
                on_reviewer_context_ack()  # reviewer ran successfully
            return 0

        # Run 1: fresh, with a real goal. Persists scout + reviewer ids + context.
        rc = cowork.run_flow(
            self._args(["--team", "scout,scout-reviewer",
                        "--context", "the original goal",
                        "--session-file", spath]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c, run_scout_fn=fake)
        self.assertEqual(rc, 0)
        self.assertIsNone(rec[0]["reviewer_resume_id"])
        self.assertEqual(rec[0]["reviewer_context"], "the original goal")
        saved = state_store.load(spath)
        self.assertEqual(state_store.get_context(saved), "the original goal")
        self.assertEqual(
            state_store.get_role_session(saved, "scout-reviewer", "codex"),
            "rev-1")

        # Run 2: resume (team set so it's non-interactive; no --context => empty).
        rc = cowork.run_flow(
            self._args(["--team", "scout,scout-reviewer",
                        "--session-file", spath]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c, run_scout_fn=fake)
        self.assertEqual(rc, 0)
        self.assertEqual(rec[1]["context"], "")                  # scout auto-continues
        self.assertEqual(rec[1]["reviewer_resume_id"], "rev-1")  # stored id reused
        self.assertEqual(rec[1]["reviewer_context"], "the original goal")  # from store
        # both roles already acknowledged revision 1 -> no wake block
        self.assertIsNone(rec[1]["reviewer_context_update"])

        # Run 3: resume WITH a new --context (a redirect). Revision bumps; the
        # resumed reviewer must get the new context as a wake block; the scout
        # gets it naturally as its prompt.
        rc = cowork.run_flow(
            self._args(["--team", "scout,scout-reviewer",
                        "--context", "redirected goal",
                        "--session-file", spath]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c, run_scout_fn=fake)
        self.assertEqual(rc, 0)
        # scout gets the redirect wrapped in the wake block (same semantics the
        # reviewer gets: current context, keep prior memory only if compatible)
        self.assertIn("New user context was provided", rec[2]["context"])
        self.assertIn("<context>\nredirected goal\n</context>", rec[2]["context"])
        self.assertEqual(rec[2]["reviewer_context"], "redirected goal")
        self.assertEqual(rec[2]["reviewer_context_update"], "redirected goal")
        saved = state_store.load(spath)
        self.assertEqual(state_store.get_context_revision(saved), 2)
        # both roles acknowledged revision 2 (fake ran the ack + rc==0)
        self.assertEqual(state_store.get_seen_revision(saved, "scout"), 2)
        self.assertEqual(state_store.get_seen_revision(saved, "scout-reviewer"), 2)

    def test_resumed_scout_gets_wake_block_when_unacknowledged(self):
        # A crash before the scout acked revision 1: the next resume must deliver
        # the stored context as an explicit wake block, not "Continue the session.".
        spath = self._tmp_session()
        state = state_store.save_config(
            spath, ["scout", "scout-reviewer"],
            cowork.default_config(["scout", "scout-reviewer"]))
        state = state_store.save_context(spath, "unseen goal", prior=state)
        state = state_store.save_role_session(
            spath, "scout", "claude", "scout-1", prior=state)  # resumable, no ack
        rec = []

        def fake(config, context, selected, **kw):
            rec.append(context)
            return 0

        rc = cowork.run_flow(
            self._args(["--team", "scout,scout-reviewer",
                        "--session-file", spath]),
            io_out=io.StringIO(), which=lambda c: "/bin/" + c, run_scout_fn=fake)
        self.assertEqual(rc, 0)
        self.assertIn("New user context was provided", rec[0])
        self.assertIn("<context>\nunseen goal\n</context>", rec[0])
        # delivered + successful run -> acknowledged now
        self.assertEqual(state_store.get_seen_revision(
            state_store.load(spath), "scout"), 1)


class RunReviewerOnceTest(unittest.TestCase):
    """The reviewer spawn path: quiet sink (single-voice) + review-file readback."""

    def _paths(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        intel = os.path.join(d, ".cowork", "scout.intel.X.json")
        review = os.path.join(d, ".cowork", "scout-review.X.json")
        os.makedirs(os.path.dirname(intel), exist_ok=True)
        with open(intel, "w") as fh:
            json.dump({"status": "ready_for_review", "result": {}}, fh)
        return intel, review

    def test_codex_reviewer_writes_and_is_read_back_via_quiet_sink(self):
        intel, review = self._paths()
        seen = {}

        def factory(controller, io_out):
            seen["controller"] = controller
            seen["io_out"] = io_out

            class FakeRevSession:
                def send(self, text):
                    seen["prompt"] = text
                    with open(review, "w") as fh:
                        json.dump({"verdict": "needs_user",
                                   "user_question": "scope?"}, fh)

                def close(self):
                    seen["closed"] = True
            return FakeRevSession()

        cfg = cowork.default_config(["scout", "scout-reviewer"])
        verdict = cowork.run_reviewer_once(
            cfg, "the goal", ["scout", "scout-reviewer"], intel, review,
            session_factory=factory)
        self.assertEqual(verdict["verdict"], "needs_user")
        self.assertEqual(verdict["user_question"], "scope?")
        self.assertEqual(seen["controller"], "codex")     # default controller
        self.assertTrue(seen["closed"])
        # single-voice: the reviewer's io_out is a quiet sink, not a real terminal.
        self.assertFalse(ui.is_tty(seen["io_out"]))
        self.assertIsInstance(seen["io_out"], cowork._QuietSink)
        # reviewer prompt carries the shared context + intel, not the scout brief.
        self.assertIn("the goal", seen["prompt"])
        self.assertNotIn("do NOT produce a plan", seen["prompt"])

    def test_stale_verdict_cleared_before_each_pass(self):
        # An old `approve` on disk must not be read back as THIS pass's verdict
        # when the reviewer fails to write a new one — that would both falsely
        # approve and falsely ack a context revision.
        intel, review = self._paths()
        with open(review, "w") as fh:
            json.dump({"verdict": "approve"}, fh)   # stale prior-round verdict

        def factory(controller, io_out):
            class DeadRevSession:
                def send(self, text):
                    pass                            # never writes the review file

                def close(self):
                    pass
            return DeadRevSession()

        cfg = cowork.default_config(["scout", "scout-reviewer"])
        verdict = cowork.run_reviewer_once(
            cfg, "goal", ["scout", "scout-reviewer"], intel, review,
            session_factory=factory)
        self.assertIsNone(verdict)                  # caller treats as safe revise
        self.assertFalse(os.path.exists(review))    # stale file was cleared


# --------------------------------------------------------------------------- #
# UX layer (cowork_ui). Pure/fallback paths run anywhere; the rich-library paths #
# use a FakeTTY stream + injected seams, and skip when the deps are absent.      #
# --------------------------------------------------------------------------- #


class FakeTTY(io.StringIO):
    """A StringIO that claims to be a terminal, so is_tty() returns True."""

    def isatty(self):
        return True


class UiBasicsTest(unittest.TestCase):
    def test_is_tty(self):
        self.assertTrue(ui.is_tty(FakeTTY()))
        self.assertFalse(ui.is_tty(io.StringIO()))

    def test_colorize_gated(self):
        self.assertEqual(ui.colorize("x", ui.RED, False), "x")
        self.assertEqual(ui.colorize("x", ui.RED, True),
                         ui.RED + "x" + ui.RESET)

    def test_label_plain_and_colored(self):
        # Plain forms must match the historical labels exactly.
        self.assertEqual(ui.label("you", False), "you › ")
        self.assertEqual(ui.label("scout", False), "scout › ")
        self.assertEqual(ui.label("you", True), ui.CYAN + "you › " + ui.RESET)
        self.assertEqual(ui.label("scout", True), ui.GREEN + "scout › " + ui.RESET)

    def test_shorten_path(self):
        cwd = "/tmp/work"
        self.assertEqual(
            ui.shorten_path("/tmp/work/.cowork/scout.intel.X.json", cwd=cwd),
            ".cowork/scout.intel.X.json")
        self.assertEqual(
            ui.shorten_path("/var/data/scout.intel.Y.json", cwd=cwd),
            "…/scout.intel.Y.json")

    def test_turn_separator_tty_only(self):
        out = io.StringIO()
        ui.turn_separator(out)              # non-TTY -> nothing
        self.assertEqual(out.getvalue(), "")
        tout = FakeTTY()
        ui.turn_separator(tout)             # TTY -> a dim rule
        self.assertIn("─", tout.getvalue())

    def test_spinner_noop_off_tty(self):
        s = ui.Spinner(io.StringIO())
        s.start()
        self.assertIsNone(s._thread)        # never spawns a thread off a TTY
        s.stop()


class PromptUserFallbackTest(unittest.TestCase):
    """The non-TTY readline fallback — unchanged from before, runs without deps."""

    def test_readline_returns_line(self):
        self.assertEqual(
            ui.prompt_user(io.StringIO("hello\n"), io.StringIO()), "hello")

    def test_readline_eof_returns_sentinel(self):
        self.assertIs(ui.prompt_user(io.StringIO(""), io.StringIO()), ui.EOF)

    def test_readline_blank_line_is_empty_not_eof(self):
        # A blank line ("\n") is distinct from EOF: it yields "" (re-prompt).
        self.assertEqual(ui.prompt_user(io.StringIO("\n"), io.StringIO()), "")


@unittest.skipUnless(HAS_UI_DEPS, "prompt_toolkit not installed")
class PromptUserTtyTest(unittest.TestCase):
    """The TTY editor path, driven through an injected prompt_toolkit session.
    (Still needs prompt_toolkit: prompt_user builds real key bindings.)"""

    def _session_factory(self, behaviour):
        class FakeSession:
            def prompt(self, message, **kw):
                self.message = message
                self.kw = kw
                return behaviour()
        self._sess = FakeSession()
        return lambda: self._sess

    def test_returns_stripped_text(self):
        got = ui.prompt_user(FakeTTY(), FakeTTY(), header="your answer",
                             session_factory=self._session_factory(
                                 lambda: "multi\nline\n"))
        self.assertEqual(got, "multi\nline")
        # multiline enabled + the header and inline submit hint are in the prompt.
        self.assertTrue(self._sess.kw.get("multiline"))
        msg = getattr(self._sess.message, "value", self._sess.message)
        self.assertIn("your answer", msg)
        self.assertIn("Enter to send", msg)        # submit key is discoverable

    def test_eof_returns_sentinel(self):
        def boom():
            raise EOFError
        self.assertIs(
            ui.prompt_user(FakeTTY(), FakeTTY(),
                           session_factory=self._session_factory(boom)),
            ui.EOF)

    def test_keyboard_interrupt_propagates(self):
        def boom():
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            ui.prompt_user(FakeTTY(), FakeTTY(),
                           session_factory=self._session_factory(boom))


@unittest.skipUnless(HAS_UI_DEPS, "prompt_toolkit not installed")
class KeyBindingsTest(unittest.TestCase):
    """Drive a real PromptSession headlessly: Enter submits, Ctrl+J newlines."""

    def test_enter_submits_ctrl_j_newlines(self):
        from prompt_toolkit import PromptSession
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        with create_pipe_input() as inp:
            # "ab", Ctrl+J (\n -> newline), "cd", Enter (\r -> submit).
            inp.send_text("ab\ncd\r")
            session = PromptSession(
                input=inp, output=DummyOutput(), multiline=True,
                key_bindings=ui.build_key_bindings())
            result = session.prompt("> ")
        self.assertEqual(result, "ab\ncd")


class RenderMarkdownTest(unittest.TestCase):
    def test_non_tty_writes_raw(self):
        out = io.StringIO()
        ui.render_markdown(out, "# hi\nbody", enabled=False)
        self.assertEqual(out.getvalue(), "# hi\nbody\n")

    @unittest.skipUnless(HAS_UI_DEPS, "rich not installed")
    def test_tty_renders_via_rich(self):
        out = FakeTTY()
        ui.render_markdown(out, "**bold**", enabled=True)
        text = out.getvalue()
        self.assertNotIn("**bold**", text)   # markers rendered away
        self.assertIn("bold", text)


class StreamingMarkdownTest(unittest.TestCase):
    def test_non_tty_streams_raw_with_label(self):
        out = io.StringIO()
        region = ui.StreamingMarkdown(out, "scout › ")
        region.__enter__()
        region.feed("hello ")
        region.feed("world")
        region.__exit__(None, None, None)
        self.assertEqual(out.getvalue(), "\nscout › hello world\n")

    @unittest.skipUnless(HAS_UI_DEPS, "rich not installed")
    def test_tty_renders_buffer(self):
        out = FakeTTY()
        with ui.StreamingMarkdown(out, "scout › ") as region:
            region.feed("**bold**")
        text = out.getvalue()
        self.assertIn("scout › ", text)
        self.assertIn("bold", text)


class BannerTest(unittest.TestCase):
    def test_non_tty_plain_keeps_substrings(self):
        for text in ("scout needs your input",
                     "scout intel ready for review — x",
                     "scout finished — intel → x"):
            out = io.StringIO()
            ui.banner(out, text, "info")
            self.assertIn(text, out.getvalue())

    @unittest.skipUnless(HAS_UI_DEPS, "rich not installed")
    def test_tty_renders_panel(self):
        out = FakeTTY()
        ui.banner(out, "ready for review", "review")
        text = out.getvalue()
        self.assertIn("ready for review", text)
        # Rich Panel draws a box border.
        self.assertTrue(any(ch in text for ch in "─│╭╰╮╯┌└"))


class ConfirmTest(unittest.TestCase):
    def test_injected_ask_fn(self):
        self.assertTrue(ui.confirm("ok?", ask_fn=lambda: True))
        self.assertFalse(ui.confirm("ok?", ask_fn=lambda: False))
        self.assertFalse(ui.confirm("ok?", ask_fn=lambda: None))  # cancel -> False


class ScoutLoopTtyTest(unittest.TestCase):
    """The review gate uses an explicit confirm on a TTY (#8). ui.confirm /
    ui.prompt_user / ui.banner are patched so no real prompt/library is needed."""

    def _intel(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return os.path.join(d, ".cowork", "scout.intel.X.json")

    def _session(self, intel_path, statuses):
        class FakeSession:
            def __init__(self):
                self.sent = []
                self.closed = False

            def send(self, text):
                self.sent.append(text)
                st = statuses.pop(0) if statuses else "ready_for_review"
                os.makedirs(os.path.dirname(intel_path), exist_ok=True)
                with open(intel_path, "w") as fh:
                    json.dump({"status": st}, fh)

            def close(self):
                self.closed = True
        return FakeSession()

    def test_review_confirm_approve(self):
        import unittest.mock as mock
        intel = self._intel()
        sess = self._session(intel, ["ready_for_review"])
        with mock.patch.object(cowork.ui, "banner"), \
                mock.patch.object(cowork.ui, "confirm", return_value=True) as conf:
            rc = cowork._scout_loop(sess, "seed", intel, context="",
                                    io_in=FakeTTY(), io_out=FakeTTY())
        self.assertEqual(rc, 0)
        self.assertEqual(sess.sent, ["seed"])
        conf.assert_called_once()

    def test_review_confirm_revise_then_approve(self):
        import unittest.mock as mock
        intel = self._intel()
        sess = self._session(intel, ["ready_for_review", "ready_for_review"])
        with mock.patch.object(cowork.ui, "banner"), \
                mock.patch.object(cowork.ui, "confirm",
                                  side_effect=[False, True]), \
                mock.patch.object(cowork.ui, "prompt_user",
                                  return_value="please tweak X"):
            rc = cowork._scout_loop(sess, "seed", intel, context="",
                                    io_in=FakeTTY(), io_out=FakeTTY())
        self.assertEqual(rc, 0)
        self.assertEqual(sess.sent, ["seed", "please tweak X"])


class ClaudeSessionTtyTest(unittest.TestCase):
    """On a TTY the claude reply streams into a render region (#5). The region is
    injected so this test needs no real terminal or Rich."""

    class _Stdin:
        def write(self, s):
            pass

        def flush(self):
            pass

        def close(self):
            pass

    class _Proc:
        def __init__(self, lines):
            self.stdout = iter(lines)
            self.stdin = ClaudeSessionTtyTest._Stdin()

        def poll(self):
            return 0

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    def test_tokens_stream_into_region(self):
        import unittest.mock as mock

        class FakeRegion:
            log = []

            def __init__(self, io_out, label):
                self.io_out = io_out
                self.label = label
                self.buf = []
                self.entered = self.exited = False

            def __enter__(self):
                self.entered = True
                FakeRegion.log.append(self)
                return self

            def feed(self, chunk):
                self.buf.append(chunk)

            def __exit__(self, *exc):
                self.exited = True

        lines = [
            json.dumps({"type": "stream_event", "event": {
                "delta": {"type": "text_delta", "text": "**hi**"}}}),
            json.dumps({"type": "stream_event", "event": {
                "delta": {"type": "text_delta", "text": " there"}}}),
            json.dumps({"type": "result", "subtype": "success",
                        "result": "x", "session_id": "S1"}),
        ]
        got = {}
        with mock.patch.object(bridge.subprocess, "Popen",
                               return_value=self._Proc(lines)):
            s = bridge.ClaudeSession(
                "roles/scout.md", "implement", True, io_out=io.StringIO(),
                region_factory=FakeRegion,
                on_session_id=lambda i: got.setdefault("id", i))
            s.send("hello")
        region = FakeRegion.log[0]
        self.assertTrue(region.entered and region.exited)
        self.assertEqual("".join(region.buf), "**hi** there")  # streamed in order
        self.assertEqual(region.label, "scout › ")             # plain label
        self.assertEqual(got.get("id"), "S1")


# --------------------------------------------------------------------------- #
# Live integration tests against the real claude / codex CLIs.                #
#                                                                             #
# These exercise the actual stdin/stdout contracts (not fakes) so we catch    #
# CLI-version drift in the flags and event shapes. They cost real API calls   #
# and are slow, so they only run when COWORK_LIVE=1 is set AND the CLI is on  #
# PATH. Run them with:  COWORK_LIVE=1 python3 -m unittest scripts/test_cowork #
# --------------------------------------------------------------------------- #

LIVE = os.environ.get("COWORK_LIVE") == "1"
HAS_CLAUDE = shutil.which("claude") is not None
HAS_CODEX = shutil.which("codex") is not None
LIVE_TIMEOUT = int(os.environ.get("COWORK_LIVE_TIMEOUT", "240"))


def _run_cli(cmd, stdin_text=None, timeout=LIVE_TIMEOUT):
    """Run a real CLI command and return (returncode, [parsed json objs], stderr)."""
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        out, err = proc.communicate(stdin_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        raise AssertionError("CLI timed out after %ss: %s" % (timeout, cmd[:3]))
    objs = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                objs.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return proc.returncode, objs, err


@unittest.skipUnless(LIVE and HAS_CLAUDE, "set COWORK_LIVE=1 with claude on PATH")
class LiveClaudeTest(unittest.TestCase):
    """Verify the real claude stream-json contract our bridge depends on."""

    def test_stdin_schema_accepted_and_assistant_result(self):
        cmd = bridge.build_claude_command(cowork.SCOUT_PROMPT_PATH, "plan", True)
        rc, objs, err = _run_cli(cmd, bridge.encode_user_message(
            "Reply with exactly the word: pong"))
        self.assertEqual(rc, 0, err[:300])
        parsed = [bridge.parse_claude_event(o) for o in objs]
        kinds = [p["kind"] for p in parsed]
        self.assertIn("assistant", kinds, "no assistant event: %s" % kinds)
        self.assertIn("result", kinds, "no result event: %s" % kinds)
        texts = " ".join(p.get("text", "") for p in parsed if p["kind"] == "assistant")
        self.assertIn("pong", texts.lower())
        result = [p for p in parsed if p["kind"] == "result"][0]
        self.assertFalse(result["is_error"])

    def test_probe_passes_against_real_claude(self):
        ok, alert = bridge.probe_claude_stream_json(
            lambda c, s: _run_cli(c, s)[1], mode="plan", yolo=True,
            role_prompt_file=cowork.SCOUT_PROMPT_PATH)
        self.assertTrue(ok, alert)


@unittest.skipUnless(LIVE and HAS_CODEX, "set COWORK_LIVE=1 with codex on PATH")
class LiveCodexTest(unittest.TestCase):
    """Verify the real codex exec --json + resume contract."""

    def test_exec_emits_thread_id_and_message(self):
        cmd = bridge.build_codex_command(
            "Reply with exactly the word: pong", "plan", True)
        rc, objs, err = _run_cli(cmd)
        self.assertEqual(rc, 0, err[:300])
        tid = bridge.capture_thread_id(objs)
        self.assertIsNotNone(tid, "no thread.started/thread_id: %s" %
                             [o.get("type") for o in objs])
        msgs = [bridge.parse_codex_event(o) for o in objs]
        texts = " ".join(m.get("text", "") for m in msgs if m["kind"] == "message")
        self.assertIn("pong", texts.lower())

    def test_resume_by_explicit_id_carries_session(self):
        rc, objs, err = _run_cli(bridge.build_codex_command(
            "Remember the number 7. Reply ok.", "plan", True))
        self.assertEqual(rc, 0, err[:300])
        tid = bridge.capture_thread_id(objs)
        self.assertIsNotNone(tid)
        rc2, objs2, err2 = _run_cli(bridge.build_codex_resume_command(
            tid, "What number did I ask you to remember? Reply with just the number."))
        self.assertEqual(rc2, 0, err2[:300])
        texts = " ".join(
            bridge.parse_codex_event(o).get("text", "")
            for o in objs2 if bridge.parse_codex_event(o)["kind"] == "message")
        self.assertIn("7", texts)


if __name__ == "__main__":
    unittest.main()
