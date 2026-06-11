# cowork

`cowork` is a terminal command that assembles a team of CLI-driven roles, spins
up the controller CLI you pick for each role (`claude` or `codex`), and bridges
that CLI's conversation straight to you.

This release implements the **foundation** and the **first two phases**:

- the entry flow (choose your team, configure each role, give context),
- the **scouting phase** — the **scout** (a context gatherer that explores the
  work and confirms a solid starting point) paired with the **scout-reviewer**
  (a critical reviewer that checks, before anything reaches you for approval,
  that the scout's questions, assumptions, and discoveries are actually aligned
  with the goal), and
- the **planning phase** — the **planner** (turns the approved intel into an
  implementation plan, delivered as a machine-readable plan JSON plus a
  human-first plan markdown) paired with the **planning-advisor** (a critical
  reviewer of the plan with the same verdict semantics as the scout-reviewer).

Phases form a **loop**, not a one-way chain: approving the scout's intel chains
straight into planning in the same run, and mid-planning the planner can hand
the work back to the scout through a confirmation gate (see
[Phases and the hand-back](#phases-and-the-hand-back)). The remaining roles
(revisor, builder) are named and reserved but not yet implemented.

## How it works

`cowork` is a standalone executable that owns your terminal. When you run it:

1. **Choose your team.** A checkbox menu of roles (`scout`, `scout-reviewer`,
   `revisor`, `planner`, `planning-advisor`, `builder`), all checked by default.
   Space toggles, Enter confirms.
2. **Configure each role.** Accept the defaults in one keystroke, or pick which
   roles to customize and choose a controller (`claude`/`codex`), a yolo
   (permission-bypass) toggle, and a mode (`plan`/`implement`) for each.
3. **Give context.** Type/paste the files/code/intent the work needs.

The interactive UI uses [rich](https://github.com/Textualize/rich) (streaming
markdown + panels), [prompt_toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit)
(multiline input), and [questionary](https://github.com/tmbo/questionary) (menus +
confirm). For tests and automation there is also a non-interactive **args path**
(`--team`/`--config`/`--context`) that skips the menus entirely (and needs none of
those packages) — see [Usage](#usage).

`cowork` then runs a **preflight** check and spins up the first role (`scout`)
using the controller you chose, bridging its live conversation to your terminal.

### The bridge

The two controllers are driven differently because their non-interactive modes
differ:

- **claude** runs as a single persistent duplex process
  (`claude -p --input-format stream-json --output-format stream-json`). Your
  typed lines are framed as stream-json user messages on stdin; the assistant's
  output streams back on stdout. A blank line ends the session.
- **codex** runs turn-based: the first turn is `codex exec --json`, from which
  `cowork` captures the session's `thread_id`; each follow-up turn is
  `codex exec resume <thread_id>`. (codex `exec` has no persistent stdin, so
  every turn is a fresh process resumed by id.)

### Controllers and modes

The flags `cowork` emits per (controller, mode, yolo), verified against
**Claude Code 2.1.x** and **codex-cli 0.133.x**:

| Setting | claude | codex |
| --- | --- | --- |
| plan mode | `--permission-mode plan` | `--sandbox read-only` |
| implement, yolo off | `--permission-mode acceptEdits` | `--sandbox workspace-write` |
| implement, yolo on | `--dangerously-skip-permissions` | `--dangerously-bypass-approvals-and-sandbox` |

Notes:

- `codex exec` is already non-interactive (it never prompts), so approval policy
  is set entirely by the sandbox — there is no `--ask-for-approval` flag on
  `exec`. `cowork` also passes `--skip-git-repo-check` so it runs outside a git
  repo, and `codex exec resume` inherits the original session's sandbox (it
  rejects `--sandbox`).
- The `scout` role spec is preloaded into claude via `--append-system-prompt-file`
  and into codex by prepending it to the prompt — `cowork` never writes an
  `AGENTS.md` into your repo.
- **yolo off has no interactive approval relay** in this release: a tool the
  permission/sandbox level does not auto-allow is denied and surfaced to you as
  an error (the run does not hang). `scout`'s defaults are plan + yolo, where
  this never triggers.

### Safety

With yolo on, claude runs with `--dangerously-skip-permissions` and codex with
`--dangerously-bypass-approvals-and-sandbox` — both bypass approval/sandbox
guards. Run `cowork` in a trusted/isolated workspace.

## Requirements

- Python 3.9 or newer.
- The interactive UX uses three pip packages — **rich** (streaming markdown +
  panels), **prompt_toolkit** (multiline input), **questionary** (menus + confirm).
  Install them into the **same interpreter** `./cowork` runs under (its shebang is
  `#!/usr/bin/env python3`):

  ```bash
  python3 -m pip install -r requirements.txt
  ```

  Use `python3 -m pip`, not a bare `pip` (often absent) or a `pip3` from a
  different Python — installing into the wrong interpreter leaves `./cowork`
  reporting the packages as missing. Only the interactive flow needs them; the
  non-interactive args path uses a plain readline/print fallback and needs none.
- The controller CLIs you intend to use, on your `PATH`:
  - **Claude Code** — `npm install -g @anthropic-ai/claude-code`
  - **Codex CLI** — `npm install -g @openai/codex` (Node 18+) or
    `brew install --cask codex`

`cowork`'s preflight reports exactly which of these is missing before doing
anything (the pip packages are checked only for the interactive flow).

## Install

Clone into your local skills directory, install the deps, and run the executable:

```bash
git clone https://github.com/garusis/co-plan.git ~/.claude/skills/co-plan
cd ~/.claude/skills/co-plan
python3 -m pip install -r requirements.txt   # rich + prompt_toolkit + questionary
./cowork --check                             # verify Python + packages + controller CLIs
```

Optionally symlink it onto your `PATH`:

```bash
ln -s ~/.claude/skills/co-plan/cowork ~/.local/bin/cowork
```

## Usage

### Interactive

```bash
./cowork            # run the full flow: team -> config -> context -> scout
./cowork --check    # run the preflight dependency check only
```

- **Team step:** a questionary checkbox menu (all roles preselected). Space
  toggles, Enter confirms.
- **Config step:** the per-role defaults are printed as a table first, then you
  pick "use these defaults" to continue instantly — or "customize", choose which
  roles, and select controller/permissions/mode for each.
- **Context step:** a multiline prompt_toolkit editor (Enter sends; Ctrl+J /
  Alt+Enter insert a newline).

### Non-interactive (args path)

Skip the menus entirely — useful for tests and automation. Providing any of
`--team`, `--config`, or `--context`/`--context-file` switches off the
interactive UI (and none of the pip packages are required):

```bash
# scout only, codex controller, no yolo, implement mode, context inline
./cowork --team scout --config "scout=codex,no-yolo,implement" --context "Refactor the auth module"

# context from a file (or '-' to read stdin)
./cowork --team scout --context-file ./brief.md
echo "the brief" | ./cowork --team scout --context-file -
```

- `--team` — comma-separated roles (default: all). Unknown roles error out.
- `--config ROLE=opt,opt` — repeatable; tokens are any of
  `claude|codex`, `yolo|no-yolo`, `plan|implement`.
- `--context TEXT` / `--context-file PATH` — initial context (`-` = stdin).
- `--session-file PATH` — use a specific session store (default
  `./.cowork/session.json`).
- `--no-session` — do not read or write the session store.

Defaults per role:

| Role | Controller | yolo | Mode |
| --- | --- | --- | --- |
| scout | claude | on | implement |
| scout-reviewer | codex | on | implement |
| revisor | codex | on | implement |
| planner | claude | on | implement |
| planning-advisor | codex | on | implement |
| builder | claude | on | implement |

Roles default to **implement** mode (write-enabled). They are kept in their lane
by **role-spec guardrails**, not by plan mode — e.g. the scout may write only its
intel file, the scout-reviewer only its review file, the planner only its two
plan files, and the planning-advisor only its review file (see below). This is
instruction-level confinement, not an OS sandbox.

The scouting and planning phases run in this release. A **fresh** team without
`scout` exits with a note: planning starts from approved scout intel, so the
planner needs the scout ahead of it (a session already in the planning phase
resumes without re-running the scout). Selecting only reserved roles exits with
a not-yet-available note.

## Sessions

`cowork` persists each session in a project-local **`.cowork/session.json`** in
the directory you run it from (add `.cowork/` to your `.gitignore`). It stores:

- a **cowork session UUID** (`session_uuid`) — minted once per session, distinct
  from any claude/codex session id. It names this session's assets: the scout
  intel file `.cowork/scout.intel.<session_uuid>.json`, the review file
  `.cowork/scout-review.<session_uuid>.json`, the planner's plan files
  `.cowork/planner.plan.<session_uuid>.json` / `.md`, the planning-advisor's
  review file `.cowork/planner-review.<session_uuid>.json`, and the private
  orchestration trace `.cowork/trace.<session_uuid>.jsonl`;
- the **team** and **per-role config** — so the next run in the same directory
  does not re-ask them (you'll see `using saved session config`);
- the **current phase** (`scouting`/`planning`) — so a killed run resumes into
  the phase it was in (see [Phases and the hand-back](#phases-and-the-hand-back));
- each role's **CLI session id** (claude `session_id` / codex `thread_id`) —
  scout, scout-reviewer, planner, and planning-advisor — so a run that is
  killed can be **resumed where it left off**, with the reviewers keeping their
  accumulated review context too; and
- the **current session context**, versioned (see below).

On the next run, if a saved session exists, `cowork` reuses the config and
**auto-resumes** the saved CLI sessions (`claude --resume <id>` /
`codex exec resume <thread_id>`). The claude session id is pinned up front
(`--session-id <uuid>`) and saved immediately, so even an instant kill is
resumable.

On a resume, `cowork` **skips the goal prompt and continues automatically** —
it sends "Continue the session." so the current phase's role picks up where it
left off with its prior context. To **redirect** the resumed session to a new
task, pass `--context "…"`; to **start fresh**, use `--no-session` (or delete
`.cowork/session.json`). `--session-file` points at a different store. Changing
the saved config is out of scope for now — delete
`.cowork/session.json` to start fresh.

### Orchestration trace

Each persisted session run appends private structured events to
`.cowork/trace.<session_uuid>.jsonl` (`--no-session` stays ephemeral and does not
write a trace). This trace does **not** duplicate Claude or Codex transcripts;
those controller CLIs already keep their own local logs. Instead, cowork records
the missing orchestration layer: when a controller was invoked, whether it was
fresh or resumed, which non-content params were used, which artifact
status/verdict was read, which gate was shown, and why stale state was
invalidated.

Prompt-like content is recorded only as `*_sha256` + `*_bytes`; argv entries that
would contain prompt bodies are replaced with `<prompt>`. The trace is intended
for local debugging with the `cowork-debug` skill, not for terminal output or a
shareable transcript.

### Context revisions

Explicit context (`--context`/the goal prompt) is a **session-wide event**, not a
one-off prompt to the scout. It is persisted as the current session context with
a monotonically increasing **revision** (`{text, hash, revision, source}`), and
every role records the last revision it acknowledged
(`last_context_revision_seen`). The invariant:

> Any role invoked after context is provided must receive the current context,
> unless it has already acknowledged that revision.

Fresh role sessions get it in their prompt naturally. **Resumed** sessions that
have not acknowledged the current revision are woken with an explicit
context-update block — "new user context was provided … treat this as the
current task context, keep prior session knowledge only where it remains
compatible" — so redirecting a resumed session keeps continuity without any role
quietly operating on stale assumptions. A role acknowledges a revision only after
it actually ran against it; a crash before that re-delivers the block on the next
resume.

## The scout role

`scout` doesn't gather blindly — it runs a short, consensus-building dialogue to
find the right thing to build, the way a good product conversation goes:

1. **Recon** — reads/searches the repo to ground itself.
2. **Clarify** — asks you the scope-defining questions (objective, definition of
   done, intended behavior). It asks blocking questions rather than guessing.
3. **Propose options** — when there are tradeoffs, it lays out concrete options
   *with a recommendation* instead of just asking open questions.
4. **Iterate** — refines with you until you reach product consensus.
5. **Hand off** — writes its intel and marks it ready for review.

Its **only write target** is its intel file
`.cowork/scout.intel.<session_uuid>.json`; it must not touch any other file
(reading/searching the whole repo is encouraged). Full spec:
[roles/scout.md](roles/scout.md).

### Intel file

A JSON object with a fixed top level; `result` is the scout's free-form
deliverable:

```json
{ "session": "<uuid>", "role": "scout",
  "status": "needs_input | ready_for_review",
  "result": { "objective": "…", "clarifications": [{"q":"…","a":"…"}],
              "relevant_code": "…", "open_unknowns": "…",
              "recommended_starting_point": "…", "plan?": "…" } }
```

cowork reads only `status`. The asked questions and your answers are recorded in
`result.clarifications`. If no `planner` role is on the team, the scout also
includes a lightweight plan in `result`.

## The scout-reviewer role

With `scout-reviewer` on the team, every time the scout marks its intel
`ready_for_review`, cowork **deterministically** runs the reviewer **before**
showing you the approve gate — orchestrator control flow, not a model deciding
when to review. The reviewer starts from the **same context the scout was given**
(the shared context + the team framing + the scout's current intel; never the
scout's own write-target brief) and critically checks objective alignment,
whether blocking product questions were buried as assumptions, whether cited
discoveries hold up, and completeness — it is instructed to find gaps, not to
rubber-stamp.

It writes a verdict to its own file, `.cowork/scout-review.<session_uuid>.json`
(its **only** write target, cleared before each pass so a stale verdict is never
read back):

- **`approve`** — the intel proceeds to your normal approve/revise gate.
- **`revise`** — the findings are handed back to the scout as its next turn; the
  scout fixes the intel and re-proposes. Bounded to **2 rounds** per
  `ready_for_review`; if the reviewer still hasn't approved, the gate is shown to
  you anyway **with the reviewer's unresolved notes attached** (it never
  hard-blocks). A missing or malformed verdict counts as `revise` — the safe
  non-approving default.
- **`needs_user`** — the reviewer found an unresolved **product** question only
  you can answer. The scout relays it to you **in its own voice** (it may
  rephrase, but must not change the meaning or drop context) and waits for your
  answer.

**Single voice:** the scout is the only role that talks to you. The reviewer is
not a secret — you'll see a small `reviewed: ...` status marker each time it runs
— but its raw output never interleaves into the conversation; its questions
reach you only through the scout's faithful relay. Full spec:
[roles/scout-reviewer.md](roles/scout-reviewer.md).

The reviewer is a **persistent session** like the scout: its CLI session id is
saved and resumed on every pass and across cowork resumes, and it participates in
[context revisions](#context-revisions) — a resumed reviewer that hasn't seen the
latest `--context` gets it as an explicit update block on its next pass.

## The planner role

When you approve the scout's intel and `planner` is on the team, cowork chains
straight into the planning phase **in the same run**: the planner is seeded with
the approved intel JSON plus the current shared context, and becomes the single
voice you talk to. Like the scout, it runs a dialogue — it asks the decisions
only you can make (scope, behavior, tradeoffs) as they appear, and marks the
plan ready when it is decision-complete.

The planner produces **two artifacts** (its only write targets):

- `.cowork/planner.plan.<session_uuid>.json` — the **machine deliverable** and
  source of truth for downstream roles, carrying the dense engineering detail:
  goal-coverage mapping, decisions with rationale, file/symbol-cited evidence,
  per-file change lists, and the test inventory. Its top level mirrors the
  scout intel (`{session, role, status, handoff?, result}`) and doubles as the
  planner's status channel
  (`needs_input | ready_for_review | handoff_back`).
- `.cowork/planner.plan.<session_uuid>.md` — the **human-first plan** you review
  at the plan gate: TL;DR; What we're building; Key decisions; How it will
  work; What changes; How we'll know it works; Out of scope; Risks &
  assumptions. Sections stay small and scannable; when you want deeper detail,
  ask the planner — it answers conversationally from the JSON instead of
  inflating the markdown.

At the plan gate you get the same approve/decline flow as the scout's: decline
with feedback and the planner keeps revising; approve and the session ends
(**plan approval is terminal** — there is no builder yet; rerunning `cowork`
resumes the planner conversation like any other resume). Full spec:
[roles/planner.md](roles/planner.md).

## The planning-advisor role

The planning-advisor pairs with the planner exactly as the scout-reviewer pairs
with the scout: each time the planner marks the plan `ready_for_review`, cowork
deterministically runs the advisor against **both** plan artifacts before
showing you the gate. Same verdict semantics — `approve` proceeds to your gate,
`revise` findings go back to the planner (bounded to 2 rounds, then the gate is
shown with the advisor's unresolved notes attached; never a hard block),
`needs_user` questions reach you only through the planner's faithful relay, and
a missing/malformed verdict counts as `revise`. Its only write target is
`.cowork/planner-review.<session_uuid>.json`, cleared before each pass. Full
spec: [roles/planning-advisor.md](roles/planning-advisor.md).

## Phases and the hand-back

The session phase (`scouting`/`planning`) is persisted in
`.cowork/session.json`, and the flow is a **loop**:

```text
scouting ──(you approve the intel; planner on team)──▶ planning ──(you approve the plan)──▶ done (run ends)
   ▲                                                      │
   └────────(you confirm the planner's hand-back)─────────┘
```

Mid-planning, the planner can **hand the work back to the scout** — say you
realize the implementation is too complex and want to reduce scope or redirect
the research. The planner writes a handoff note (what changed, what to
re-investigate, what to keep) and signals `handoff_back`; cowork shows you an
explicit confirmation gate. On yes, the **same scout session resumes**, woken
with the handoff note, and runs its full cycle again — investigation,
clarifications, scout-reviewer check, your approval gate. When you approve the
updated intel, the **same planner session resumes**, woken with the updated
intel to digest, and planning continues. On no, the planner is told and keeps
planning. A `handoff_back` without a note degrades to the normal needs-input
prompt — never an implicit hand-back.

The signal contract is role-generic (any role → its pre-processor); only
planner → scout is wired this iteration. A killed run resumes into the
persisted phase: a session mid-planning re-enters the planner conversation
directly, without re-running the scout.

### Interacting with scout — the three states

Each turn, cowork streams the reply, then reads the intel `status`:

- **working** — a `scout working…` spinner fills the gap before the first token,
  then the reply renders **live as markdown** (Rich `Live`) under `scout ›` —
  length-independent, so replies taller than the screen still render. Off a
  terminal (piped/scripted), tokens stream raw with no rendering.
- **`needs_input`** — scout asked you something (visible in its reply). cowork
  shows a `scout needs your input` panel and waits for your answer.
- **`ready_for_review`** — scout finished the intel and posts a **summary in the
  chat**. If the scout-reviewer is on the team it runs first (you'll see a
  `reviewed: approved`, `reviewed: changes requested`, or
  `reviewed: needs user input` marker; see
  [The scout-reviewer role](#the-scout-reviewer-role)), then cowork shows an
  explicit approve/revise gate. On a terminal this is
  a questionary confirm (**Approve & finish?**): confirm ends the session; decline
  opens an editor for revision feedback, which sends another turn so you keep
  refining.

**Input.** On a terminal each turn is a prompt_toolkit multiline editor: real line
editing (arrow keys, word-jump, paste, history) and multiline answers. A dim hint
sits right above the input line — **Enter to send · Ctrl+J or Alt+Enter for a new
line**. A **blank line re-prompts**; to stop scout before it's ready, use **Ctrl-C**
or type **`/quit`**.

About **Shift+Enter**: terminals send the same byte for Enter and Shift+Enter
unless the Kitty keyboard protocol is active, and prompt_toolkit has no Shift+Enter
key, so the portable newline keys are **Ctrl+J** and **Alt+Enter**. You can map
Shift+Enter to send Alt+Enter (ESC+Enter) in your terminal's keymap (VS Code,
iTerm2, …) to get a newline on Shift+Enter — the same approach as Claude Code's
`/terminal-setup`.

Turns are color-labeled throughout — your input as `you ›` (cyan), the role's
replies as `scout ›` (green). All of this uses rich/prompt_toolkit/questionary;
piped/scripted runs fall back to plain text and `readline`.

## Repository layout

```text
.
|-- cowork                      # executable entry point
|-- roles
|   |-- scout.md                # scout role spec (preloaded into the controller)
|   |-- scout-reviewer.md       # scout-reviewer role spec (critical review + verdict schema)
|   |-- planner.md              # planner role spec (dual plan artifacts + hand-back contract)
|   `-- planning-advisor.md     # planning-advisor role spec (plan critique + verdict schema)
`-- scripts
    |-- cowork.py               # entry flow (questionary menus + args path) + phase loop + role orchestration
    |-- cowork_bridge.py        # flag assembly, stream-json framing, codex resume, probe
    |-- cowork_ui.py            # shared UX layer: prompt_toolkit input, Rich markdown/panels, color
    |-- cowork_preflight.py     # Python-version + pip-package + controller PATH checks
    |-- cowork_trace.py         # private JSONL orchestration trace writer
    |-- cowork_state.py         # .cowork/session.json store (config, phase, session ids, context revisions, verdicts)
    `-- test_cowork.py          # unit + live integration tests
```

## Development

Run the fast unit suite (fakes only — no CLIs spawned, no API calls):

```bash
python3 -m unittest scripts/test_cowork.py
```

The unit tests cover flag assembly, preflight (including the pip-package check),
the menus (via injected ask-callables — no questionary prompt or TTY needed), the
non-interactive args path, the claude stream-json probe, event parsing, denial
handling, the plan-only fallthrough, the phase loop (scout→planner chaining, the
hand-back round trip, resume-into-planning, the scout-less refusal), the planner
loop and planning-advisor gate (via injected fakes), and that `cowork` stays
self-contained. Tests that exercise the real rich/prompt_toolkit libraries skip
when the packages aren't installed (like the `COWORK_LIVE` tests); install
`requirements.txt` to run them. The real terminal experience (live markdown, the
editor, panels) is a manual check — as is one live end-to-end phase loop:
scout → approve → planner → hand back → scout → approve → planner → approve.

### Live integration tests

To verify the real contracts against the installed CLIs (catching flag/version
drift), set `COWORK_LIVE=1`. These spawn real `claude`/`codex` processes, make
real API calls, and are slow:

```bash
COWORK_LIVE=1 python3 -m unittest scripts/test_cowork.py
```

They are skipped automatically when `COWORK_LIVE` is unset or the CLI is not on
`PATH`. Tune the per-call timeout with `COWORK_LIVE_TIMEOUT` (seconds, default
240). The live tests assert that:

- claude accepts `cowork`'s stream-json stdin message shape and returns
  `assistant` + `result` events (and the probe passes);
- codex `exec --json` emits a `thread.started` `thread_id` and an agent message;
- `codex exec resume <thread_id>` resumes the same session by explicit id.
