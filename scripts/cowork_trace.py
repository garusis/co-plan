#!/usr/bin/env python3
"""Private cowork orchestration trace.

The trace complements Claude/Codex controller logs. It records cowork's own
decisions and controller invocation metadata, but never raw prompts, replies, or
terminal transcript text.
"""

import datetime
import hashlib
import json
import os
import uuid


def trace_path_for(cowork_dir, session_uuid):
    return os.path.join(cowork_dir, "trace.%s.jsonl" % session_uuid)


def new_run_id():
    return str(uuid.uuid4())


def prompt_meta(text, prefix="prompt"):
    text = text or ""
    raw = text.encode("utf-8")
    return {
        "%s_sha256" % prefix: hashlib.sha256(raw).hexdigest(),
        "%s_bytes" % prefix: len(raw),
    }


def redacted_argv(argv, prompt_text=None):
    """Return argv with any prompt body replaced by '<prompt>'."""
    if argv is None:
        return None
    out = []
    for arg in argv:
        if prompt_text is not None and arg == prompt_text:
            out.append("<prompt>")
        else:
            out.append(arg)
    return out


def command_meta(argv, prompt_text=None):
    data = {"argv": redacted_argv(argv, prompt_text=prompt_text)}
    if prompt_text is not None:
        data.update(prompt_meta(prompt_text))
    return data


class Trace:
    def __init__(self, path, session_uuid=None, run_id=None, enabled=True):
        self.path = path
        self.session_uuid = session_uuid
        self.run_id = run_id or new_run_id()
        self.enabled = bool(enabled and path)

    def event(self, name, **fields):
        if not self.enabled:
            return
        obj = {
            "ts": _now(),
            "event": name,
            "run_id": self.run_id,
        }
        if self.session_uuid:
            obj["session_uuid"] = self.session_uuid
        obj.update({k: v for k, v in fields.items() if v is not None})
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a") as fh:
                json.dump(_json_safe(obj), fh, sort_keys=True)
                fh.write("\n")
        except OSError:
            # Debug tracing must never break cowork.
            return


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace(
        "+00:00", "Z")


def _json_safe(value):
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
