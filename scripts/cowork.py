#!/usr/bin/env python3
"""cowork: multi-role CLI orchestration entry flow + the scout (context
gatherer) role.

This is the foundation only: the 3-step entry flow (team checklist, per-role
tool config, initial context), the preflight dependency check, and running the
first role (`scout`) by spawning the selected CLI and bridging it to the user.
Later roles (revisor/planner/advisor/builder) are out of scope here.

Selection uses `gum` for real interactive checkbox/choice menus. A
non-interactive args path (--team/--config/--context) skips gum entirely so the
flow is testable and scriptable.

Additive to the co-plan skill: new file, stdlib only, Python 3.9+, does not
import or modify co_plan_file.py.
"""

import argparse
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cowork_bridge as bridge  # noqa: E402
import cowork_preflight as preflight  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_ui as ui  # noqa: E402

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCOUT_PROMPT_PATH = os.path.join(SKILL_ROOT, "roles", "scout.md")
SCOUT_REVIEWER_PROMPT_PATH = os.path.join(SKILL_ROOT, "roles", "scout-reviewer.md")

# Max reviewer<->scout review rounds per `ready_for_review` (D5). After this many
# reviewer passes without approval, cowork falls through to the user review gate
# with the reviewer's last dissent attached. Never hard-blocks.
REVIEW_ROUND_CAP = 2

# Role order matches the user's vision: context-gather, scout-reviewer,
# plan-revisor, planner, advisor, implementer. `scout` (and its paired
# `scout-reviewer`) are implemented now.
#
# `scout-reviewer` is a critical reviewer paired with the scout DURING the scout
# session (deterministically invoked when the scout sets `ready_for_review`); it
# is distinct from `revisor`, the planned SEQUENTIAL plan-revisor that would run
# after the scout.
SCOUT_REVIEWER = "scout-reviewer"
ROLES = ["scout", SCOUT_REVIEWER, "revisor", "planner", "advisor", "builder"]

# Per-role defaults (controller, yolo, mode), all roles checked by default.
# Roles default to implement mode (write-enabled) and are kept in their lane by
# role-spec guardrails, not by plan mode.
DEFAULTS = {
    "scout": {"controller": "claude", "yolo": True, "mode": "implement"},
    SCOUT_REVIEWER: {"controller": "codex", "yolo": True, "mode": "implement"},
    "revisor": {"controller": "codex", "yolo": True, "mode": "implement"},
    "planner": {"controller": "claude", "yolo": True, "mode": "implement"},
    "advisor": {"controller": "codex", "yolo": True, "mode": "implement"},
    "builder": {"controller": "claude", "yolo": True, "mode": "implement"},
}


# --------------------------------------------------------------------------- #
# Menu seam (questionary): the interactive menus take injectable ask-callables  #
# so they are unit-testable without a TTY or a real questionary prompt. The     #
# defaults below are the only place questionary is imported.                    #
# --------------------------------------------------------------------------- #


def _q_checkbox(message, options, checked=None):
    """questionary multi-select. Returns the picked list (or None on Ctrl-C)."""
    import questionary
    from questionary import Choice
    checked = set(checked or [])
    return questionary.checkbox(
        message, choices=[Choice(o, checked=(o in checked)) for o in options]
    ).ask()


def _q_select(options, default=None, message=""):
    """questionary single-select. Returns the picked item, falling back to
    `default` on cancel so callers never get None."""
    import questionary
    picked = questionary.select(
        message or "", choices=list(options), default=default).ask()
    return picked if picked is not None else default


# --------------------------------------------------------------------------- #
# Step 1: team checklist (interactive).                                       #
# --------------------------------------------------------------------------- #


def select_team_interactive(checkbox_fn=None):
    """Checkbox menu, all roles preselected. Returns ordered roles ([] on cancel)."""
    checkbox_fn = checkbox_fn or _q_checkbox
    picks = checkbox_fn("Choose your team (space toggles, enter confirms)",
                        ROLES, checked=ROLES)
    if not picks:  # None (cancelled) or empty selection
        return []
    return [r for r in ROLES if r in picks]


# --------------------------------------------------------------------------- #
# Step 2: per-role tool config.                                               #
# --------------------------------------------------------------------------- #


def default_config(selected):
    return {role: dict(DEFAULTS[role]) for role in selected}


def apply_config_override(config, role, tokens):
    """Apply tokens (controller/yolo/no-yolo/plan/implement) to one role.
    Returns (ok, error_or_None). Mutates config."""
    if role not in config:
        return False, "unknown or unselected role: %r" % role
    cfg = config[role]
    for token in tokens:
        if token in ("claude", "codex"):
            cfg["controller"] = token
        elif token == "yolo":
            cfg["yolo"] = True
        elif token == "no-yolo":
            cfg["yolo"] = False
        elif token in ("plan", "implement"):
            cfg["mode"] = token
        else:
            return False, "unknown option: %r" % token
    return True, None


def format_config_summary(config, header="Tool config:"):
    """Aligned per-role summary with a column header row."""
    labels = ("role", "controller", "permissions", "mode")
    rows = [
        (role, config[role]["controller"],
         "yolo" if config[role]["yolo"] else "no-yolo", config[role]["mode"])
        for role in ROLES if role in config
    ]
    if not rows:
        return header
    cols = list(zip(labels, *rows))
    widths = [max(len(str(v)) for v in col) for col in cols]

    def fmt(cells):
        return "  " + "   ".join(
            str(cell).ljust(widths[i]) for i, cell in enumerate(cells))

    lines = [header, fmt(labels), fmt("-" * w for w in widths)]
    for row in rows:
        lines.append(fmt(row))
    return "\n".join(lines)


def configure_roles_interactive(selected, select_fn=None, checkbox_fn=None):
    """Step 2 via questionary. A fast path accepts the defaults (shown as the
    summary); otherwise pick roles to customize and choose controller/yolo/mode."""
    select_fn = select_fn or _q_select
    checkbox_fn = checkbox_fn or _q_checkbox
    config = default_config(selected)
    summary = format_config_summary(config, header="Default tool config:")
    choice = select_fn(["use these defaults", "customize"],
                       default="use these defaults", message=summary)
    if choice != "customize":
        return config
    to_customize = checkbox_fn("Customize which roles?", selected) or []
    for role in selected:
        if role not in to_customize:
            continue
        cfg = config[role]
        cfg["controller"] = select_fn(["claude", "codex"],
                                      default=cfg["controller"],
                                      message=role + " controller")
        yolo = select_fn(["yolo", "no-yolo"],
                         default="yolo" if cfg["yolo"] else "no-yolo",
                         message=role + " permissions")
        cfg["yolo"] = (yolo == "yolo")
        cfg["mode"] = select_fn(["plan", "implement"], default=cfg["mode"],
                                message=role + " mode")
    return config


# --------------------------------------------------------------------------- #
# Step 3: initial context.                                                    #
# --------------------------------------------------------------------------- #


def gather_context_interactive(prompt_fn=None):
    """One multiline editor for the initial context. EOF/cancel => no context."""
    prompt_fn = prompt_fn or (lambda: ui.prompt_user(
        sys.stdin, sys.stdout,
        header="What do you want to build or change? Describe the goal — "
               "paste any files, code, or context that matter."))
    val = prompt_fn()
    if val is ui.EOF or val is ui.CANCEL:
        return ""
    return val


def resolve_context(args, resuming=False):
    """Context from --context, --context-file (or '-' for stdin), or the editor.

    When `resuming` a saved session, skip the interactive goal prompt and return
    "" — run_scout turns that into "Continue the session." so the resumed scout
    picks up where it left off automatically. An explicit --context/--context-file
    still wins (lets you redirect a resumed session)."""
    if args.context is not None:
        return args.context
    if args.context_file is not None:
        if args.context_file == "-":
            return sys.stdin.read()
        with open(args.context_file, "r") as fh:
            return fh.read()
    if _is_non_interactive(args):
        return ""
    if resuming:
        return ""  # auto-continue; no goal prompt on resume
    return gather_context_interactive()


# --------------------------------------------------------------------------- #
# Argument parsing / non-interactive path.                                    #
# --------------------------------------------------------------------------- #


def build_parser():
    p = argparse.ArgumentParser(prog="cowork", add_help=True)
    p.add_argument("--check", action="store_true",
                   help="run the preflight dependency check only")
    p.add_argument("--team",
                   help="comma-separated roles, e.g. scout,advisor "
                        "(non-interactive)")
    p.add_argument("--config", action="append", default=[],
                   metavar="ROLE=opt,opt",
                   help="per-role override, e.g. scout=codex,no-yolo,implement "
                        "(repeatable)")
    p.add_argument("--context", help="initial context text (non-interactive)")
    p.add_argument("--context-file",
                   help="read initial context from a file, or '-' for stdin")
    p.add_argument("--session-file",
                   help="path to the session store (default: ./.cowork/session.json)")
    p.add_argument("--no-session", action="store_true",
                   help="do not read or write the session store")
    return p


def _is_non_interactive(args):
    return bool(args.team or args.config or args.context is not None
                or args.context_file)


def parse_team(team_arg):
    """Validate a --team value. Returns (selected, error_or_None)."""
    requested = [r.strip() for r in team_arg.split(",") if r.strip()]
    unknown = [r for r in requested if r not in ROLES]
    if unknown:
        return None, "unknown role(s): %s" % ", ".join(unknown)
    return [r for r in ROLES if r in requested], None


def apply_config_args(config, config_args):
    """Apply --config ROLE=opt,opt entries. Returns (ok, error_or_None)."""
    for item in config_args:
        if "=" not in item:
            return False, "bad --config %r (expected ROLE=opt,opt)" % item
        role, _, rest = item.partition("=")
        tokens = [t.strip() for t in rest.split(",") if t.strip()]
        ok, err = apply_config_override(config, role.strip(), tokens)
        if not ok:
            return False, err
    return True, None


# --------------------------------------------------------------------------- #
# Scout run.                                                                  #
# --------------------------------------------------------------------------- #


def scout_intel_path(intel_dir, session_uuid):
    return os.path.join(intel_dir, "scout.intel.%s.json" % session_uuid)


def assemble_scout_brief(selected, intel_path):
    """Dynamic first-message brief for the scout: where to write, the JSON +
    domain guardrail, and the plan-only fallthrough for this team."""
    if "planner" in selected:
        plan_note = (
            "A dedicated `planner` role is on the team: stop at the intel file "
            "and hand off; do NOT produce a plan."
        )
    else:
        plan_note = (
            "NO `planner` role is on the team: in the same intel JSON, also "
            "include a lightweight plan/handoff."
        )
    return (
        "Write your findings as a single JSON object to exactly this file:\n"
        "  %s\n"
        "That intel file is your ONLY write target. Do not create, edit, or "
        "delete any other file (reading/searching the repo is fine).\n"
        "%s" % (intel_path, plan_note)
    )


def read_scout_prompt(path=SCOUT_PROMPT_PATH):
    with open(path, "r") as fh:
        return fh.read()


def assemble_codex_prompt(role_text, team_note, context):
    return "\n\n".join([role_text.strip(), team_note.strip(), context.strip()]).strip()


# --------------------------------------------------------------------------- #
# scout-reviewer: a critical reviewer paired with the scout. Invoked            #
# deterministically when the scout sets `ready_for_review`. It shares the       #
# scout's initial context (the user `context`, NOT the scout's write-target     #
# brief), reads the scout intel, and writes a verdict to its own review file.   #
# --------------------------------------------------------------------------- #


def _read_text(path):
    try:
        with open(path, "r") as fh:
            return fh.read()
    except OSError:
        return ""


def read_scout_reviewer_prompt(path=SCOUT_REVIEWER_PROMPT_PATH):
    with open(path, "r") as fh:
        return fh.read()


def assemble_reviewer_brief(review_path):
    """The reviewer's write-target instruction — its analogue of the scout brief.
    It points at the review file only (never the scout intel)."""
    return (
        "Write your verdict as a single JSON object to exactly this file:\n"
        "  %s\n"
        "That review file is your ONLY write target. Do NOT edit the scout intel "
        "file or any other file (reading/searching the repo is fine). Use the "
        "verdict schema from your role (verdict: approve|revise|needs_user, "
        "findings, and user_question when needs_user)." % review_path
    )


def assemble_reviewer_context(context, selected, intel_path):
    """The reviewer's situational context: the SAME initial `context` the scout
    received, the team framing, and the scout's current intel JSON to review.

    Deliberately excludes the scout's write-target `brief` / `first` payload —
    that carries the scout's own guardrail and would mis-instruct the reviewer."""
    intel_text = _read_text(intel_path)
    team = ", ".join(selected) if selected else "(unspecified)"
    return (
        "Shared initial context — this is the SAME context the scout was given:\n"
        "%s\n\n"
        "Team on this session: %s\n\n"
        "The scout's current intel (review it critically against the context "
        "above):\n%s" % (context.strip(), team, intel_text.strip())
    )


def assemble_reviewer_handoff(verdict, review):
    """Build the scout-facing handoff string from a reviewer verdict dict.

    Pure string templating — NO second model call. This is the scout half of the
    faithful-relay guardrail: for `needs_user` it carries the reviewer's FULL
    `user_question` plus an instruction to relay it without changing its meaning
    or dropping context. Returns "" for `approve` (no handoff; fall through to the
    user gate)."""
    review = review or {}
    findings = review.get("findings") or []
    if verdict == "needs_user":
        question = (review.get("user_question") or "").strip()
        return (
            "[reviewer handoff] Before this can go to the user for approval, a "
            "blocking product question is unresolved. Put this question to the "
            "user in your own next reply. You MAY rephrase it into your own voice, "
            "but you must NOT change its meaning or omit any part of its context. "
            "Then set status back to needs_input.\n\n"
            "Question: %s" % question
        )
    if verdict == "revise":
        bullet = "\n".join("- " + str(f) for f in findings) if findings else \
            "- (no specific findings provided)"
        return (
            "[reviewer handoff] A reviewer checked your intel and it is not ready "
            "to hand off yet. Address the following, update your intel, and set "
            "status back to ready_for_review when done. Do not mention the "
            "reviewer to the user.\n%s" % bullet
        )
    return ""


def scout_reviewed_text():
    """Content-free marker shown to the user so they can see a review happened
    (D7). Carries no reviewer content; the substring 'reviewed' is asserted by
    tests."""
    return "reviewed"


class _QuietSink:
    """A write sink that discards everything — used as the reviewer session's
    `io_out` so its raw stream is never interleaved into the user conversation
    (single-voice invariant, D7). Reports not-a-tty so sessions take plain
    paths."""

    def write(self, _s):
        return None

    def flush(self):
        return None

    def isatty(self):
        return False


def context_update_block(text):
    """Wake block for any role resuming a CLI session that has not acknowledged
    the current session context revision. Role-agnostic: scout, scout-reviewer,
    and future roles all receive the same framing."""
    return (
        "New user context was provided for this resumed cowork session.\n\n"
        "Treat this as the current task context. Keep prior session knowledge "
        "only where it remains compatible.\n\n"
        "<context>\n%s\n</context>" % text.strip()
    )


def assemble_reviewer_resume_context(intel_path, context_update=None):
    """Lighter context for a RESUMED reviewer session: its thread already holds
    the role + the prior context, so only the updated intel is sent — plus a
    context-update wake block when the session context changed since the
    reviewer last acknowledged it."""
    body = (
        "The scout has updated its intel since your last review. Re-review the "
        "current intel below against the current task context, and write your "
        "verdict to the review file again:\n%s" % _read_text(intel_path).strip()
    )
    if context_update:
        return context_update_block(context_update) + "\n\n" + body
    return body


def run_reviewer_once(config, context, selected, intel_path, review_path,
                      session_factory=None, claude_spawn=None,
                      resume_id=None, on_session=None, context_update=None):
    """Spawn (or resume) the scout-reviewer for one pass and return its verdict.

    The reviewer is a PERSISTENT session: its id is captured via `on_session`
    (so cowork can store it) and `resume_id` resumes it on later rounds and on a
    cowork resume, preserving its accumulated context across invocations. A fresh
    session gets the full context (brief + shared context + intel); a resumed one
    gets only the updated intel — prefixed with a context-update wake block
    (`context_update`) when the session context changed since the reviewer last
    acknowledged it, so a resumed reviewer never operates on stale context.

    The reviewer writes its verdict to `review_path`; we read it back via
    `state_store.read_review` (the review file is the handoff channel because the
    session bridges stream to io_out and return no value). Its raw stream goes to
    a quiet sink so nothing reaches the user. On any failure or missing/malformed
    file, read_review yields a safe non-approving `revise` (or None, which the
    caller treats as revise)."""
    cfg = config.get(SCOUT_REVIEWER) or DEFAULTS[SCOUT_REVIEWER]
    quiet = _QuietSink()
    brief = assemble_reviewer_brief(review_path)
    # The review file is per-pass output, not durable state: clear any previous
    # verdict BEFORE the pass so a reviewer that fails (or never writes) yields
    # None -> safe revise, instead of a stale `approve` from an earlier round
    # being read back as this pass's verdict.
    try:
        os.remove(review_path)
    except OSError:
        pass
    if resume_id:
        ctx_block = assemble_reviewer_resume_context(
            intel_path, context_update=context_update)
    else:
        ctx_block = assemble_reviewer_context(context, selected, intel_path)

    if cfg["controller"] == "claude":
        cb = (lambda i: on_session("claude", i)) if on_session else None
        if session_factory:
            session = session_factory("claude", quiet)
        elif resume_id:
            session = bridge.ClaudeSession(
                SCOUT_REVIEWER_PROMPT_PATH, cfg["mode"], cfg["yolo"],
                io_out=quiet, speaker="scout-reviewer",
                resume_id=resume_id, on_session_id=cb)
        else:
            spawn = claude_spawn or bridge._real_claude_spawn
            ok, _alert = bridge.probe_claude_stream_json(
                spawn, mode=cfg["mode"], yolo=cfg["yolo"],
                role_prompt_file=SCOUT_REVIEWER_PROMPT_PATH)
            if not ok:
                return state_store.read_review(review_path)
            # Pin a known id up front so it is resumable even if killed early.
            sid = str(uuid.uuid4())
            if on_session:
                on_session("claude", sid)
            session = bridge.ClaudeSession(
                SCOUT_REVIEWER_PROMPT_PATH, cfg["mode"], cfg["yolo"],
                io_out=quiet, speaker="scout-reviewer",
                session_id=sid, on_session_id=cb)
        first = (brief + "\n\n" + ctx_block).strip()
        try:
            session.send(first)
        finally:
            session.close()
        return state_store.read_review(review_path)

    # codex (default)
    cb = (lambda i: on_session("codex", i)) if on_session else None
    if resume_id:
        prompt = (brief + "\n\n" + ctx_block).strip()  # thread already has role
    else:
        prompt = assemble_codex_prompt(read_scout_reviewer_prompt(), brief, ctx_block)
    if session_factory:
        session = session_factory("codex", quiet)
    else:
        session = bridge.CodexSession(
            cfg["mode"], cfg["yolo"], io_out=quiet, speaker="scout-reviewer",
            resume_thread_id=resume_id, on_thread_id=cb)
    try:
        session.send(prompt)
    finally:
        session.close()
    return state_store.read_review(review_path)


# Banner text producers. The text is rendered through ui.banner (a gum-styled box
# on a TTY, plain text otherwise). The full intel path is shown once, in the start
# banner; later banners use the shortened form (#11/#12). Keyword substrings
# ("needs your input", "ready for review", "scout finished") are preserved so the
# non-TTY/test path can assert them.


def scout_start_text(intel_path, resuming=False):
    if resuming:
        head = (
            "scout — resuming our previous session\n"
            "Picking up where we left off with the earlier context (no goal prompt "
            "needed). To start fresh instead, run with --no-session; to redirect, "
            "pass --context. Ctrl-C aborts."
        )
    else:
        head = (
            "scout — gathering context\n"
            "I'll investigate, ask what I need, and propose options. I finish on my\n"
            "own once we agree. You drive — answer my questions. Ctrl-C aborts."
        )
    return head + "\nintel → %s" % intel_path


def scout_needs_input_text():
    return "scout needs your input"


def scout_review_text(intel_path):
    return "scout intel ready for review — %s" % ui.shorten_path(intel_path)


def scout_done_text(intel_path):
    return "scout finished — intel → %s" % ui.shorten_path(intel_path)


# Returned by the turn readers to mean "end the conversation" (EOF / Ctrl-D /
# explicit /quit), distinct from a blank line (which re-prompts).
_END = object()


def _read_turn(io_in, io_out):
    """Read one working-turn reply. Blank input and a cancelled editor re-prompt;
    only EOF or an explicit /quit (or /stop) ends the loop (#4/#10)."""
    while True:
        ui.turn_separator(io_out)
        reply = ui.prompt_user(io_in, io_out, header="your answer")
        if reply is ui.EOF:        # input exhausted / Ctrl-D — end
            return _END
        if reply is ui.CANCEL:     # editor dismissed — discard draft, re-prompt
            continue
        if reply.strip() == "":    # blank line — re-prompt, never abort (#10)
            continue
        if reply.strip() in ("/quit", "/stop"):
            return _END
        return reply


def _read_review(io_in, io_out):
    """At ready_for_review, decide approve-vs-revise. On a TTY this is an explicit
    questionary confirm (#8); off a TTY it keeps the historical blank=finish /
    text=revise contract so the scripted/test path is unchanged. Returns _END to
    approve & finish, or the revision feedback text."""
    if ui.is_tty(io_in) and ui.is_tty(io_out):
        if ui.confirm("Approve & finish?"):
            return _END
        while True:
            fb = ui.prompt_user(io_in, io_out, header="Revise — your feedback")
            if fb is ui.CANCEL or fb is ui.EOF or fb.strip() == "":
                # Nothing to revise with: treat as approve so the user is never
                # trapped at the gate.
                return _END
            return fb
    line = io_in.readline()
    if line == "" or line.strip() == "":
        return _END
    return line.rstrip("\n")


def _dissent_suffix(verdict):
    """A short, user-visible note attached to the review gate when the reviewer's
    concerns were not resolved within the round cap."""
    findings = (verdict or {}).get("findings") or []
    if not findings:
        # No specific findings (e.g. a missing/unreadable review): still tell the
        # user the reviewer did not sign off, rather than implying a clean pass.
        return "\nreviewer's unresolved notes:\n  - reviewer did not approve " \
               "within the review round cap."
    return "\nreviewer's unresolved notes:\n" + "\n".join(
        "  - " + str(f) for f in findings)


def _scout_loop(session, first, intel_path, context, io_in, io_out,
                review_fn=None):
    """Drive the per-turn loop: send → read intel status → prompt or finish.

    Ends when the user approves at `ready_for_review`, hits EOF/Ctrl-D, or types
    /quit. A blank line re-prompts; Ctrl-C aborts.

    When `review_fn` is provided (the scout-reviewer is on the team), each
    `ready_for_review` first runs the reviewer (topology D) BEFORE the user gate:
    `review_fn(intel_path, round_index)` returns a verdict dict
    {verdict, findings, user_question}. The reviewer is bounded by
    REVIEW_ROUND_CAP rounds, after which cowork falls through to the user with the
    reviewer's dissent attached. The reviewer never writes to the user channel;
    only the content-free `reviewed` marker and the scout's own replies appear."""
    pending = first
    review_rounds = 0
    try:
        if context.strip():
            io_out.write(ui.label("you", ui.is_tty(io_out)) + context.strip() + "\n")
            io_out.flush()
        while True:
            session.send(pending)
            status = state_store.read_status(intel_path)
            if status == "ready_for_review":
                dissent = ""
                # Reviewer gate (topology D): runs transparently before the user.
                if review_fn is not None and review_rounds < REVIEW_ROUND_CAP:
                    review_rounds += 1
                    verdict = review_fn(intel_path, review_rounds) or {}
                    ui.banner(io_out, scout_reviewed_text(), "info")
                    v = verdict.get("verdict")
                    has_question = bool(str(verdict.get("user_question") or "").strip())
                    if v == "approve":
                        # Only an explicit approve reaches the user gate.
                        review_rounds = 0
                    elif v == "needs_user" and has_question:
                        review_rounds = 0
                        pending = assemble_reviewer_handoff("needs_user", verdict)
                        continue
                    else:
                        # revise, an unknown/empty verdict (missing or unreadable
                        # review file), or needs_user without a question: the safe
                        # non-approving default — never silently approve.
                        if review_rounds < REVIEW_ROUND_CAP:
                            pending = assemble_reviewer_handoff("revise", verdict)
                            continue
                        # Cap reached without approval: fall through to the user
                        # with the reviewer's unresolved dissent attached (D5).
                        dissent = _dissent_suffix(verdict)
                        review_rounds = 0
                ui.banner(io_out, scout_review_text(intel_path) + dissent, "review")
                outcome = _read_review(io_in, io_out)
                if outcome is _END:
                    ui.banner(io_out, scout_done_text(intel_path), "done")
                    break
                pending = outcome  # revision feedback → another turn
                review_rounds = 0  # user re-engaged: fresh review budget
            else:
                if status == "needs_input":
                    review_rounds = 0  # scout re-opened work: fresh review budget
                    ui.banner(io_out, scout_needs_input_text(), "needs_input")
                outcome = _read_turn(io_in, io_out)
                if outcome is _END:
                    break
                pending = outcome
    except KeyboardInterrupt:
        pass
    finally:
        session.close()
    return 0


def make_review_fn(config, context, selected, review_path, reviewer_runner=None,
                   reviewer_resume_id=None, on_reviewer_session=None,
                   context_update=None, on_context_ack=None):
    """Build the `review_fn` passed to `_scout_loop` when the scout-reviewer is on
    the team, or None when it is not. The closure runs one reviewer pass and
    returns its verdict dict.

    The reviewer is a persistent session: the first pass creates it (id captured
    and persisted via `on_reviewer_session`); every later pass — within this run
    and after a cowork resume (seeded by `reviewer_resume_id`) — resumes it.

    Context invariant: `context` must be the CURRENT session context. A fresh
    session receives it in full; a resumed session that has not acknowledged the
    current revision receives it as a `context_update` wake block on its first
    pass. After the first successful pass, `on_context_ack()` records the
    acknowledgment (and the block is not repeated on later rounds).
    `reviewer_runner` is injectable for tests."""
    if SCOUT_REVIEWER not in selected or not review_path:
        return None
    runner = reviewer_runner or run_reviewer_once
    holder = {"resume_id": reviewer_resume_id,
              "context_update": context_update,
              "ack": on_context_ack}

    def review_fn(intel_path, _round_index):
        def capture(controller, sid):
            if sid:
                holder["resume_id"] = sid
            if on_reviewer_session:
                on_reviewer_session(controller, sid)

        verdict = runner(config, context, selected, intel_path, review_path,
                         resume_id=holder["resume_id"], on_session=capture,
                         context_update=holder["context_update"])
        if verdict is not None:
            # The reviewer ran against the current context: acknowledge the
            # revision once and stop repeating the wake block.
            holder["context_update"] = None
            if holder["ack"]:
                holder["ack"]()
                holder["ack"] = None
        return verdict

    return review_fn


def run_scout(config, context, selected, io_in=None, io_out=None,
              claude_spawn=None, resume_id=None, on_session=None,
              intel_path=None, session_factory=None, review_path=None,
              reviewer_runner=None, reviewer_resume_id=None,
              on_reviewer_session=None, reviewer_context=None,
              reviewer_context_update=None, on_reviewer_context_ack=None):
    """Spin up the scout's CLI and drive the review loop.

    `resume_id` continues a saved CLI session; `on_session(controller, id)` is
    called so the session id can be persisted for a future resume.
    `intel_path` is the scout's only write target (`.cowork/scout.intel.*.json`).
    `session_factory(controller, **kw)` overrides session creation (for tests).
    `review_path` + the scout-reviewer being on the team enable the reviewer gate;
    `reviewer_runner` overrides the reviewer pass (for tests).
    `reviewer_resume_id` resumes a stored reviewer session; `on_reviewer_session`
    persists a new one. `reviewer_context` is the CURRENT session context for the
    reviewer (defaults to `context`); `reviewer_context_update` is set when a
    resumed reviewer has not acknowledged the current context revision (it is
    delivered as a wake block) and `on_reviewer_context_ack` records the
    acknowledgment after the first successful pass.
    """
    io_in = io_in or sys.stdin
    io_out = io_out or sys.stdout
    cfg = config["scout"]
    brief = assemble_scout_brief(selected, intel_path or "")
    review_fn = make_review_fn(
        config,
        reviewer_context if reviewer_context is not None else context,
        selected, review_path, reviewer_runner=reviewer_runner,
        reviewer_resume_id=reviewer_resume_id,
        on_reviewer_session=on_reviewer_session,
        context_update=reviewer_context_update,
        on_context_ack=on_reviewer_context_ack)
    if resume_id and not context.strip():
        context = "Continue the session."
    ui.banner(io_out, scout_start_text(intel_path or "", resuming=bool(resume_id)),
              "start")
    io_out.flush()

    if cfg["controller"] == "claude":
        spawn = claude_spawn or bridge._real_claude_spawn
        ok, alert = bridge.probe_claude_stream_json(
            spawn, mode=cfg["mode"], yolo=cfg["yolo"],
            role_prompt_file=SCOUT_PROMPT_PATH,
        )
        if not ok:
            io_out.write("cowork: " + alert + "\n")
            io_out.flush()
            return 1
        if resume_id:
            session_id, rid = None, resume_id
            io_out.write("cowork: resuming claude session %s\n" % resume_id)
        else:
            # Pin a known UUID up front so the session is resumable even if the
            # run is killed immediately.
            session_id, rid = str(uuid.uuid4()), None
            if on_session:
                on_session("claude", session_id)
        cb = (lambda i: on_session("claude", i)) if on_session else None
        if session_factory:
            session = session_factory("claude", session_id=session_id,
                                      resume_id=rid, on_session_id=cb)
        else:
            session = bridge.ClaudeSession(
                SCOUT_PROMPT_PATH, cfg["mode"], cfg["yolo"], io_out=io_out,
                speaker="scout", session_id=session_id, resume_id=rid,
                on_session_id=cb)
        first = (brief + "\n\n" + context).strip()
        return _scout_loop(session, first, intel_path, context, io_in, io_out,
                           review_fn=review_fn)

    role_text = read_scout_prompt()
    prompt = assemble_codex_prompt(role_text, brief, context)
    if resume_id:
        io_out.write("cowork: resuming codex session %s\n" % resume_id)
    cb = (lambda i: on_session("codex", i)) if on_session else None
    if session_factory:
        session = session_factory("codex", resume_thread_id=resume_id,
                                  on_thread_id=cb)
    else:
        session = bridge.CodexSession(
            cfg["mode"], cfg["yolo"], io_out=io_out, speaker="scout",
            resume_thread_id=resume_id, on_thread_id=cb)
    return _scout_loop(session, prompt, intel_path, context, io_in, io_out,
                       review_fn=review_fn)


# --------------------------------------------------------------------------- #
# Entry point.                                                                #
# --------------------------------------------------------------------------- #


def run_flow(args, io_in=None, io_out=None, which=None, run_scout_fn=None):
    io_in = io_in or sys.stdin
    io_out = io_out or sys.stdout
    run_scout_fn = run_scout_fn or run_scout
    interactive = not _is_non_interactive(args)

    # Session store: project-local .cowork/session.json unless disabled.
    session_enabled = not args.no_session
    spath = args.session_file or state_store.session_path()
    saved = state_store.load(spath) if session_enabled else None
    # cowork session UUID (distinct from any claude/codex session id): names this
    # session's assets, e.g. the scout intel file.
    if session_enabled:
        saved = state_store.ensure_session(spath, saved, str(uuid.uuid4()))
        session_uuid = state_store.get_session_uuid(saved)
    else:
        session_uuid = str(uuid.uuid4())
    reuse_config = (session_enabled and state_store.has_config(saved)
                    and not args.team and not args.config)

    # Step 1: team.
    if args.team:
        selected, err = parse_team(args.team)
        if err:
            io_out.write("cowork: " + err + "\n")
            return 2
    elif reuse_config:
        selected = [r for r in ROLES if r in saved["team"]]
    elif interactive:
        selected = select_team_interactive()
    else:
        selected = list(ROLES)
    if not selected:
        io_out.write("cowork: no roles selected; nothing to do.\n")
        return 0

    # Step 2: config.
    config = default_config(selected)
    if args.config:
        ok, err = apply_config_args(config, args.config)
        if not ok:
            io_out.write("cowork: " + err + "\n")
            return 2
    elif reuse_config:
        config = {r: dict(saved["config"][r]) for r in selected
                  if r in saved["config"]}
        io_out.write("cowork: using saved session config (%s)\n" % spath)
    elif interactive:
        config = configure_roles_interactive(selected)

    # Persist team + config the first time (or whenever freshly chosen).
    if session_enabled and not reuse_config:
        saved = state_store.save_config(spath, selected, config, prior=saved or {})

    # Preflight (rich/prompt_toolkit/questionary required only for interactive use).
    kwargs = {"interactive": interactive}
    if which is not None:
        kwargs["which"] = which
    ok, alerts = preflight.preflight(config, **kwargs)
    if not ok:
        io_out.write("cowork preflight failed:\n")
        for alert in alerts:
            io_out.write("  - " + alert + "\n")
        io_out.flush()
        return 1

    if "scout" not in selected:
        io_out.write(
            "cowork: scout not selected. Only the scout role is implemented in "
            "this version; later roles are not yet available.\n"
        )
        return 0

    # Resume saved CLI sessions if they match the current controllers.
    # Resolved BEFORE the context step so we can skip the goal prompt on a resume.
    resume_id = None
    on_session = None
    reviewer_resume_id = None
    on_reviewer_session = None
    if session_enabled:
        resume_id = state_store.get_role_session(
            saved, "scout", config["scout"]["controller"])
        holder = {"state": saved}

        def on_session(controller, sid):
            holder["state"] = state_store.save_role_session(
                spath, "scout", controller, sid, prior=holder["state"])

        if SCOUT_REVIEWER in selected:
            reviewer_resume_id = state_store.get_role_session(
                saved, SCOUT_REVIEWER, config[SCOUT_REVIEWER]["controller"])

            def on_reviewer_session(controller, sid):
                if not sid:
                    return
                holder["state"] = state_store.save_role_session(
                    spath, SCOUT_REVIEWER, controller, sid, prior=holder["state"])

    # Step 3: context. On a resume, skip the goal prompt and auto-continue.
    context = resolve_context(args, resuming=bool(resume_id))

    # Context invariant: explicit context is a session-wide event. Persist it as
    # the CURRENT session context (bumping the revision when it changed), and
    # make sure every role invoked from here on receives the current revision —
    # fresh sessions get it in their prompt; resumed sessions that have not
    # acknowledged it get an explicit context-update wake block.
    reviewer_context = context
    reviewer_context_update = None
    on_reviewer_context_ack = None
    current_rev = 0
    if session_enabled:
        if context.strip():
            holder["state"] = state_store.save_context(
                spath, context, prior=holder.get("state"))
        state = holder["state"]
        current_text = state_store.get_context(state) or ""
        current_rev = state_store.get_context_revision(state)
        reviewer_context = current_text or context
        # Scout: a non-empty `context` is delivered in its prompt this run. On a
        # resume, wrap it in the wake block so a redirect carries the same
        # "this is the current context, keep prior memory only if compatible"
        # semantics the reviewer gets; with no new context, deliver any
        # unacknowledged revision the same way instead of a bare
        # "Continue the session.".
        if resume_id and context.strip():
            context = context_update_block(context)
        elif resume_id and state_store.role_context_gap(state, "scout"):
            context = context_update_block(current_text)
        if SCOUT_REVIEWER in selected:
            if reviewer_resume_id:
                reviewer_context_update = state_store.role_context_gap(
                    state, SCOUT_REVIEWER)

            def on_reviewer_context_ack():
                holder["state"] = state_store.mark_context_seen(
                    spath, SCOUT_REVIEWER, current_rev, prior=holder["state"])

    intel_dir = os.path.dirname(spath) if session_enabled else state_store.session_dir()
    intel_path = scout_intel_path(intel_dir, session_uuid)
    review_path = state_store.review_path_for(intel_dir, session_uuid)
    rc = run_scout_fn(config, context, selected, io_in=io_in, io_out=io_out,
                      resume_id=resume_id, on_session=on_session,
                      intel_path=intel_path, review_path=review_path,
                      reviewer_resume_id=reviewer_resume_id,
                      on_reviewer_session=on_reviewer_session,
                      reviewer_context=reviewer_context,
                      reviewer_context_update=reviewer_context_update,
                      on_reviewer_context_ack=on_reviewer_context_ack)
    # The scout received the current context in its prompt this run; record the
    # acknowledgment after a successful run (a crash leaves it unacknowledged, so
    # the next resume re-delivers the wake block — safe direction).
    if session_enabled and rc == 0 and current_rev:
        holder["state"] = state_store.mark_context_seen(
            spath, "scout", current_rev, prior=holder["state"])
    return rc


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
        if args.check:
            return preflight.main()
        return run_flow(args)
    except KeyboardInterrupt:
        # Clean exit on Ctrl-C instead of dumping a traceback. 130 = 128 + SIGINT.
        sys.stderr.write("\ncowork: interrupted.\n")
        return 130
    except EOFError:
        # Ctrl-D at a prompt / closed stdin.
        sys.stderr.write("\ncowork: input closed.\n")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
