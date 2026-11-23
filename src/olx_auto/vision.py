"""Stage 1 (spec §5): photos + note → structured observations, plus follow-up questions about the photos.

Only this stage sees images. The agent works from the observations text, so images are not resent
on every turn of the tool loop.
"""
import base64
from pathlib import Path

from .llm import LLM, Reply, parse_json

OBSERVE_PROMPT = """\
You look at photos of a second-hand item that someone wants to sell on olx.bg and report what you see.
The seller may add a short note in any language; trust it for facts the photos can't show.

Answer with one JSON object, nothing else:
{
  "item_type": "what the item is, in English",
  "brand": "brand or null",
  "model": "model name/number or null",
  "text_on_item": ["labels, model numbers, sizes, specs read verbatim from the photos"],
  "specs": ["other facts you can tell: colour, material, size, capacity, ..."],
  "condition": "new | like new | used | worn | damaged, with a short reason",
  "defects": ["each visible scratch, stain, crack, missing part, with photo number"],
  "accessories": ["what else is in the photos: box, charger, cables, manuals, ..."],
  "uncertainties": ["things a buyer would ask that the photos don't settle"]
}
Report only what is visible or stated in the note. Don't guess a model from looks alone: put it in
uncertainties instead. Photos are numbered in the order given, starting at 1."""

ASK_PROMPT = """\
You look at photos of a second-hand item for sale on olx.bg and answer one question about them.
Be short and concrete, cite photo numbers, and say plainly when the photos don't show the answer.
Photos are numbered in the order given, starting at 1."""


def image_parts(photo_paths: list[Path]) -> list[dict]:
    parts = []
    for i, path in enumerate(photo_paths, 1):
        data = base64.b64encode(path.read_bytes()).decode()
        parts.append({"type": "text", "text": f"Photo {i}:"})
        parts.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}})
    return parts


def observe(llm: LLM, model: str, photo_paths: list[Path], note: str) -> tuple[dict, Reply]:
    text = f"Seller's note: {note}" if note else "The seller added no note."
    messages = [
        {"role": "system", "content": OBSERVE_PROMPT},
        {"role": "user", "content": [*image_parts(photo_paths), {"type": "text", "text": text}]},
    ]
    reply = llm.chat(model, messages, response_format={"type": "json_object"})
    return parse_json(reply.text), reply


def ask(llm: LLM, model: str, photo_paths: list[Path], note: str, question: str) -> tuple[str, Reply]:
    text = (f"Seller's note: {note}\n\n" if note else "") + f"Question: {question}"
    messages = [
        {"role": "system", "content": ASK_PROMPT},
        {"role": "user", "content": [*image_parts(photo_paths), {"type": "text", "text": text}]},
    ]
    reply = llm.chat(model, messages)
    return reply.text, reply
