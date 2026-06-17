---
name: cowork-debug
description: >-
  Debug cowork sessions end to end. Use when the user asks why cowork showed a
  wrong gate/status, whether scout/reviewer/planner actually ran, how to inspect
  cowork session history, how to correlate .cowork artifacts with Claude/Codex
  logs, or to diagnose stale intel/review/session/resume behavior.
---

# Cowork Debug

Reconstruct what happened in a cowork run by joining four evidence sources:

1. `.cowork/session.json` for cowork session UUID, team/config, role controller
   IDs, and context revisions.
2. `.cowork/trace.<session_uuid>.jsonl` for cowork orchestration decisions.
3. `.cowork/scout.intel.<session_uuid>.json` and
   `.cowork/scout-review.<session_uuid>.json` for current/final artifacts.
4. Claude/Codex local logs for role conversation and tool history.

Do not rely on terminal transcripts. Do not mutate session artifacts while
debugging. Terminal transcripts are useful as symptom reports only; verify them
against trace events, artifacts, and controller logs.

## Quick Workflow

1. Resolve the cowork session:
   - Start in the repo cwd and read `.cowork/session.json`.
   - If the user gives a session UUID, verify it matches `session_uuid`.
   - Record `sessions.<role>.controller`, `sessions.<role>.id`, team/config,
     context revision, and `last_context_revision_seen`.
2. Read current artifacts:
   - Intel: `.cowork/scout.intel.<uuid>.json`
   - Review: `.cowork/scout-review.<uuid>.json`
   - Trace: `.cowork/trace.<uuid>.jsonl`
3. Locate controller logs:
   - Claude session id: `~/.claude/projects/**/<session_id>.jsonl`
   - Codex thread id: `~/.codex/sessions/**/rollout-*<thread_id>.jsonl`
4. Build a timeline:
   - First use trace events for cowork decisions: status reads, gates, review
     rounds, invalidations, session saves, context acks, controller invocations.
   - For display glitches, also inspect UI trace events:
     `ui.markdown.start`, `ui.markdown.commit`, and `ui.markdown.end`.
   - Then use controller logs for role content: user messages, assistant replies,
     tool calls, artifact edits.
   - Finally compare final artifact state with the trace and controller writes.
5. Report findings with labels:
   - `evidence`: directly shown by trace, artifact, or controller log.
   - `inference`: likely conclusion from multiple evidence points.
   - `missing evidence`: needed fact is absent from all available logs.

## Trace Semantics

The cowork trace complements controller logs; it does not duplicate role
conversation. It records metadata only:

- Controller invocation metadata: controller, role, fresh/resume, mode/yolo, cwd,
  prompt file, session/thread id, redacted argv.
- Prompt-like content as `prompt_sha256` and `prompt_bytes`, never raw text.
- Orchestration decisions: `status.read`, `status.invalidated`,
  `review.verdict`, `gate.show`, `user.action`, `context.*`, `run.*`.
- UI render diagnostics: `ui.markdown.start`, `ui.markdown.commit`, and
  `ui.markdown.end`. These are metadata only: renderer mode, TTY flag, terminal
  dimensions, label byte length, chunk/line/char counts, committed/tail sizes,
  and status-row counters. They must not contain prompt text, assistant output,
  user answers, artifact contents, or raw terminal transcript text.

If a trace file is missing, say so and fall back to artifacts + controller logs.
Older cowork runs may not have trace data.

## Debugging Display Replays

When the user reports repeated terminal lines, partial duplicated sentences, or
weird redraw artifacts:

1. Treat the transcript as a symptom, not proof that a role repeated itself.
2. Check trace for repeated controller turns or repeated `gate.show` events.
3. Check the relevant artifact for duplicated persisted content.
4. Check the controller log for the final assistant message or streamed events.
5. Check UI events:
   - `ui.markdown.start`: renderer (`rich_live` vs `raw`), TTY flag, terminal
     width/height, `term_present`, and `term_dumb`.
   - `ui.markdown.commit`: how often finalized paragraphs were committed out of
     the live region, plus chunk/tail sizes.
   - `ui.markdown.end`: total chunks/chars/lines, committed chars, final tail
     chars, status set/clear counts.

Diagnosis rule of thumb:

- Trace has one controller turn, artifact content is clean, controller log has
  one clean assistant message, but terminal transcript shows repeated partial
  lines -> likely Rich Live redraw/scrollback behavior.
- Trace shows repeated controller turns or gates -> investigate orchestration or
  role-loop behavior first.

Privacy rule: never ask for or add raw terminal output to trace. If more detail
is needed, add metadata counters or booleans that explain renderer behavior
without storing content.

## Reading Claude Logs

Claude logs live under `~/.claude/projects`. Given a session id from
`.cowork/session.json`, search for `<session_id>.jsonl`.

Useful records:

- `user`: user messages, tool results, and replayed cowork prompts.
- `assistant`: assistant text and tool calls.
- `toolUseResult`: file edits and command results, often with exact file paths.
- `last-prompt`: latest prompt summary pointer, useful for navigation.
- `summary` or compaction records, when present: lossy navigation aids only.

Claude logs can prove what the role saw or wrote. They cannot by themselves
prove why cowork chose a gate; use trace for that.

## Reading Codex Logs

Codex logs live under `~/.codex/sessions`. Given a thread id from
`.cowork/session.json`, search for `rollout-*<thread_id>.jsonl`.

Useful records:

- `session_meta`: thread id, cwd, CLI version, model/provider.
- `turn_context`: cwd, sandbox/approval policy, workspace roots.
- `event_msg.user_message`: user prompt sent to Codex.
- `response_item.message`: assistant message content.
- `response_item.function_call` and `function_call_output`: tool calls/results.
- `event_msg.task_complete`: turn completion.

Codex may include reasoning or summaries. Treat summaries as lossy unless raw
messages/tool events are unavailable.

## Common Diagnoses

- Wrong `Approve & finish?` gate: check latest `status.read`; if it read
  `ready_for_review`, inspect whether trace later shows `status.invalidated`.
  If trace is missing, compare the last controller write to the intel status.
- Reviewer seemed absent: check `review.round.start`, `review.run.start`,
  controller invocation events for `scout-reviewer`, and the Codex/Claude
  reviewer log by saved id.
- Review file has only one verdict: expected. Review files are latest-only; use
  trace + controller logs for history.
- Intel not updated until resume: compare controller artifact-edit records,
  final intel mtime/content, trace `status.read`, and any `context.gap` /
  `run.resume` events.
- Stale resume/context: compare `context.current`, `context.gap`,
  `context.ack`, and each role's `last_context_revision_seen`.
- Repeated terminal lines: compare UI render diagnostics with controller turns,
  artifacts, and controller logs. If only the transcript repeats, suspect Rich
  Live rendering/terminal redraw instead of role duplication.

## Output Shape

Keep the user-facing report short:

- Timeline: key timestamped events with source labels.
- Finding: what failed or behaved correctly.
- Evidence: paths and line/event references.
- Gaps: anything not logged that prevents certainty.
- Suggested fix/test when relevant.
