"""The seller's style guide: storage with backups, and learning from feedback on listings.

Feedback (messages on the card, hand edits of the text) is turned into general rules in
style_guide.md, so the same correction isn't needed on every listing. Every change backs up the
previous guide to style_guide_history/ and is logged in style_guide_log.json, with the reason.
"""
import json
import os
import re
from datetime import UTC, datetime

from .llm import LLM, LLMError, Reply, parse_json
from .users import User

BACKUP_RE = re.compile(r"^\d{8}-\d{6}(-\d+)?\.md$")
MIN_GUIDE_CHARS = 100  # a learned guide shorter than this is a model error, not an edit

LEARN_PROMPT = """\
You maintain a seller's style guide for olx.bg listings. The guide is Markdown in Bulgarian and is
given to an AI that writes all of this seller's listings.

You get the current guide, one complete listing as it stands now, and the seller's new feedback on
it: messages they wrote on the listing, and edits they made by hand (before → after).

Sort the feedback into:
- general preferences that should apply to future listings: tone, structure, wording, what to always
  mention or never write, how to describe condition, pricing habits;
- facts about this item only (measurements, age, accessories, a wrong category): ignore these.
Hand edits are feedback too: infer the rule behind an edit when it's clear (e.g. a deleted sentence
about unknown details means "don't write about what isn't known"), and skip edits that only fix facts.

If there's a general preference the guide doesn't already state, update the guide:
- merge into the existing sections and rephrase rules instead of appending near-duplicates;
- remove or change rules the seller contradicted;
- keep it short, concrete and in Bulgarian, with a short example where it helps;
- never add anything specific to this item.

Answer with one JSON object, nothing else:
{"changed": false}
or
{"changed": true, "guide": "<the full updated guide>",
 "summary": "<one sentence in Bulgarian: what changed and why>"}"""


def read_guide(user: User) -> str:
    return user.style_guide_file.read_text() if user.style_guide_file.exists() else ""


def log(user: User) -> list[dict]:
    """Newest first."""
    if not user.style_log_file.exists():
        return []
    return list(reversed(json.loads(user.style_log_file.read_text())))


def _write_atomic(path, text: str) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def write_guide(user: User, text: str, *, summary: str, source: str) -> None:
    """source: a listing id (learned), "manual" or "revert"."""
    text = text.strip() + "\n"
    old = read_guide(user)
    if text == old:
        return
    user.style_history_dir.mkdir(exist_ok=True)
    name = f"{datetime.now(UTC):%Y%m%d-%H%M%S}.md"
    n = 1
    while (user.style_history_dir / name).exists():
        n += 1
        name = f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{n}.md"
    (user.style_history_dir / name).write_text(old)
    _write_atomic(user.style_guide_file, text)
    entries = list(reversed(log(user)))
    entries.append({"at": datetime.now(UTC).isoformat(timespec="seconds"), "summary": summary,
                    "source": source, "backup": name})
    _write_atomic(user.style_log_file, json.dumps(entries, ensure_ascii=False, indent=1))


def revert(user: User, backup: str) -> None:
    """Restore the guide as it was before the change that saved `backup`."""
    if not BACKUP_RE.match(backup) or not (user.style_history_dir / backup).exists():
        raise KeyError(backup)
    text = (user.style_history_dir / backup).read_text()
    write_guide(user, text, summary=f"Върнат вариантът отпреди промяна {backup[:-3]}", source="revert")


def learn(llm: LLM, model: str, guide: str, draft: dict, messages: list[str],
          edits: list[dict]) -> tuple[dict, Reply]:
    """Returns ({"changed": bool, "guide"?, "summary"?}, reply)."""
    listing = {k: draft.get(k) for k in ("title", "description", "price", "negotiable")}
    feedback = {"messages": messages,
                "edits": [{"field": e["field"], "before": e["before"], "after": e["after"]} for e in edits]}
    user_msg = (f"## Current guide\n{guide or '(empty)'}\n\n"
                f"## The listing now\n{json.dumps(listing, ensure_ascii=False, indent=1)}\n\n"
                f"## New feedback\n{json.dumps(feedback, ensure_ascii=False, indent=1)}")
    reply = llm.chat(model, [{"role": "system", "content": LEARN_PROMPT},
                             {"role": "user", "content": user_msg}],
                     response_format={"type": "json_object"})
    result = parse_json(reply.text)
    if result.get("changed"):
        if len((result.get("guide") or "").strip()) < MIN_GUIDE_CHARS:
            raise LLMError("Style learner returned an empty or truncated guide")
        result["summary"] = (result.get("summary") or "").strip() or "Обновено от обратна връзка"
    return result, reply
