#!/usr/bin/env python3
"""cowork session store.

Persists a cowork session in a project-local `.cowork/session.json` so the team
+ per-role config is not re-asked on the next run in the same directory, and so
the scout's claude/codex session can be resumed if a run is killed.

Schema (version 1):

    {
      "version": 1,
      "team": ["scout", "advisor", ...],
      "config": {"scout": {"controller": "claude", "yolo": true, "mode": "plan"}, ...},
      "context": {                 # current shared session context (versioned)
        "text": "...",
        "hash": "<sha256>",
        "revision": 3,
        "source": "--context"
      },
      "sessions": {
        "scout": {"controller": "claude", "id": "<uuid>",   # claude session_id
                  "last_context_revision_seen": 3}
        # or:    {"controller": "codex",  "id": "<thread_id>", ...}
      }
    }

The context invariant: explicit context (`--context`/prompted goal) is persisted
as the CURRENT session context, with a monotonically increasing revision. Any
role invoked afterward must receive that current context unless it has already
acknowledged that revision (`last_context_revision_seen`); a resumed CLI session
that has not seen the latest revision gets it as an explicit wake block instead
of being discarded.

Python 3.9+, stdlib only. Does not import co_plan_file.py.
"""

import hashlib
import json
import os

VERSION = 1
DIR_NAME = ".cowork"
FILE_NAME = "session.json"


def session_dir(cwd=None):
    return os.path.join(cwd or os.getcwd(), DIR_NAME)


def session_path(cwd=None):
    return os.path.join(session_dir(cwd), FILE_NAME)


def load(path):
    """Return the stored state dict, or None if absent/unreadable/incompatible."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or state.get("version") != VERSION:
        return None
    return state


def save(path, state):
    """Write state atomically, creating the .cowork dir if needed."""
    state = dict(state)
    state["version"] = VERSION
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def get_session_uuid(state):
    return (state or {}).get("session_uuid")


def read_status(intel_path):
    """Return the scout intel `status` (needs_input/ready_for_review), or None if
    the file is missing, unreadable, or not yet written. Tolerant by design so a
    missing/partial file never forces the cowork loop to end."""
    if not intel_path:
        return None
    try:
        with open(intel_path, "r") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if isinstance(data, dict):
        return data.get("status")
    return None


VALID_VERDICTS = ("approve", "revise", "needs_user")


def read_review(review_path):
    """Return the scout-reviewer verdict dict, or None if the file is missing,
    unreadable, or not yet written. Tolerant by design (mirrors read_status) so a
    missing/partial review never crashes the cowork loop.

    A file that is present but lacks a valid `verdict` is reported as a
    `{"verdict": "revise", ...}` so the caller never silently approves on a
    malformed review — the safe non-approving default."""
    if not review_path:
        return None
    try:
        with open(review_path, "r") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("verdict") not in VALID_VERDICTS:
        # Present but malformed: degrade to a safe, non-approving verdict so the
        # plan never reaches the user on an unparseable review.
        return _safe_revise(
            "Reviewer wrote an unparseable or missing verdict; treating as "
            "revise (safe default).", data.get("user_question"))
    if data.get("verdict") == "needs_user" and not str(
            data.get("user_question") or "").strip():
        # needs_user with no question can't be relayed faithfully -> safe revise.
        return _safe_revise(
            "Reviewer returned needs_user without a user_question; treating as "
            "revise (safe default).", None)
    return data


def _safe_revise(reason, user_question):
    return {
        "verdict": "revise",
        "findings": [reason],
        "user_question": user_question,
        "malformed": True,
    }


def review_path_for(intel_dir, session_uuid):
    """Path of the scout-reviewer's verdict file for a session (sibling of the
    scout intel file)."""
    return os.path.join(intel_dir, "scout-review.%s.json" % session_uuid)


def ensure_session(path, prior, new_uuid):
    """Guarantee the session has a cowork session UUID (distinct from any
    claude/codex session id) and that it is persisted. Returns the state.

    `new_uuid` is used only when none exists yet, so callers control id
    generation (real runs pass a fresh uuid4; tests can pass a fixed value)."""
    state = dict(prior or {})
    if not state.get("session_uuid"):
        state["session_uuid"] = new_uuid
        state.setdefault("team", [])
        state.setdefault("config", {})
        state.setdefault("sessions", {})
        save(path, state)
    return state


def has_config(state):
    return bool(state and state.get("team") and state.get("config"))


def save_config(path, team, config, prior=None):
    """Persist team + config, preserving any existing saved sessions."""
    state = dict(prior or {})
    state["team"] = list(team)
    state["config"] = {r: dict(c) for r, c in config.items()}
    state.setdefault("sessions", {})
    save(path, state)
    return state


def get_role_session(state, role, controller):
    """Return the saved session id for a role if it matches the controller."""
    if not state:
        return None
    sess = (state.get("sessions") or {}).get(role)
    if sess and sess.get("controller") == controller and sess.get("id"):
        return sess["id"]
    return None


def save_role_session(path, role, controller, session_id, prior=None):
    """Persist (or update) the resumable session id for a role. Merges into the
    role's existing entry so bookkeeping fields (e.g.
    `last_context_revision_seen`) survive an id refresh."""
    state = dict(prior or load(path) or {})
    state.setdefault("team", state.get("team") or [])
    state.setdefault("config", state.get("config") or {})
    sessions = dict(state.get("sessions") or {})
    entry = dict(sessions.get(role) or {})
    entry.update({"controller": controller, "id": session_id})
    sessions[role] = entry
    state["sessions"] = sessions
    save(path, state)
    return state


# --------------------------------------------------------------------------- #
# Shared session context (versioned).                                          #
#                                                                              #
# Explicit context is a session-wide event, not a one-off prompt to the        #
# user-facing role: it is persisted with a revision, and every role tracks the #
# last revision it acknowledged so a resumed CLI session can be woken with the #
# current context instead of silently operating on stale assumptions.         #
# --------------------------------------------------------------------------- #


def save_context(path, text, prior=None, source="--context"):
    """Persist `text` as the CURRENT session context. Bumps the revision only
    when the text actually changed; re-providing identical context is a no-op."""
    state = dict(prior or load(path) or {})
    state.setdefault("team", state.get("team") or [])
    state.setdefault("config", state.get("config") or {})
    state.setdefault("sessions", state.get("sessions") or {})
    if get_context(state) == text:
        return state
    state["context"] = {
        "text": text,
        "hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "revision": get_context_revision(state) + 1,
        "source": source,
    }
    save(path, state)
    return state


def get_context(state):
    """Return the current session context text, or None. Tolerates the legacy
    plain-string form."""
    ctx = (state or {}).get("context")
    if isinstance(ctx, dict):
        return ctx.get("text")
    return ctx


def get_context_revision(state):
    """Return the current context revision (0 when no context exists). A legacy
    plain-string context counts as revision 1."""
    ctx = (state or {}).get("context")
    if isinstance(ctx, dict):
        try:
            return int(ctx.get("revision") or 0)
        except (TypeError, ValueError):
            return 0
    return 1 if ctx else 0


def get_seen_revision(state, role):
    """Return the last context revision this role acknowledged (0 if never)."""
    sess = ((state or {}).get("sessions") or {}).get(role) or {}
    try:
        return int(sess.get("last_context_revision_seen") or 0)
    except (TypeError, ValueError):
        return 0


def mark_context_seen(path, role, revision, prior=None):
    """Record that `role` has received (acknowledged) context `revision`."""
    state = dict(prior or load(path) or {})
    state.setdefault("team", state.get("team") or [])
    state.setdefault("config", state.get("config") or {})
    sessions = dict(state.get("sessions") or {})
    entry = dict(sessions.get(role) or {})
    entry["last_context_revision_seen"] = revision
    sessions[role] = entry
    state["sessions"] = sessions
    save(path, state)
    return state


def role_context_gap(state, role):
    """Return the current context text when `role` has not yet acknowledged the
    current revision, else None."""
    if get_context_revision(state) > get_seen_revision(state, role):
        return get_context(state)
    return None
