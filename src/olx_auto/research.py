"""Stage 1b (spec §5): observations → product facts from the web, for a detailed, accurate listing.

One call to a small model with OpenRouter's web plugin, which searches and puts the results into
the prompt. Runs on every listing, after vision and before the agent, so the agent gets the facts
as text instead of searching inside its tool loop (where results would be resent every turn).
"""
import json

from .llm import LLM, Reply, parse_json

WEB_PLUGIN = {"id": "web", "max_results": 5}

PROMPT = """\
You research a second-hand item that someone wants to sell on olx.bg. You get what a vision model
read from the seller's photos, and what the seller said. Use the web search results to identify the
exact product and report its published facts, so a good listing can be written.

Answer with one JSON object, nothing else:
{
  "identified_as": "exact product name and variant, or null when you can't tell",
  "confidence": "high | medium | low",
  "specs": ["key specs of this product: dimensions, weight, capacity, power, materials, sizes, ..."],
  "features": ["selling points a buyer cares about"],
  "variants_note": "how variants differ, when the item could be more than one of them, else null",
  "new_price": "approximate price new, with currency, or null",
  "release_year": "year or null"
}
Only report facts that apply to this exact product. When the model is ambiguous or sources
disagree, say so in variants_note and set confidence to low. Never invent a fact: leave a field
empty instead. If brand and model are unknown, give useful general facts about this kind of item
(typical sizes, what to check) with confidence low. At most 15 specs and 8 features, each short.
The seller's statements are facts about their item and win over the photos."""


def research(llm: LLM, model: str, observations: dict, note: str,
             answered: list[dict] = (), messages: list[str] = ()) -> tuple[dict, Reply]:
    """Returns the facts with `sources` ([{url, title}] of the pages the search found) added."""
    parts = ["## Observations (from the photos)\n" + json.dumps(observations, ensure_ascii=False, indent=1),
             "## Seller's note\n" + (note or "(none)")]
    if answered:
        parts.append("## Seller's answers\n" + "\n".join(
            f"- Q: {a['question']}\n  A: {a['answer']}" for a in answered))
    if messages:
        parts.append("## Seller's messages (oldest first)\n" + "\n".join(f"- {m}" for m in messages))
    reply = llm.chat(model, [{"role": "system", "content": PROMPT},
                             {"role": "user", "content": "\n\n".join(parts)}],
                     response_format={"type": "json_object"}, extra_body={"plugins": [WEB_PLUGIN]})
    facts = parse_json(reply.text)
    # web links only (they become hrefs on the card), each once
    facts["sources"] = list({c["url"]: c for c in reply.citations
                             if c["url"].startswith(("https://", "http://"))}.values())
    return facts, reply
