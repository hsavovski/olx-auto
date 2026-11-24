"""Stage 2 (spec §5): hand-written tool-use loop that turns observations into a listing draft.

One `run_agent` call = one run: build the context, call the model until it calls `submit_draft`
with a draft that passes `validation.check_draft`, or a guardrail (iterations, cost) stops it. The
run never blocks on the user: questions go onto the card and the draft is a best guess.
"""
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import validation, vision
from .comparables import ComparablesError
from .llm import LLM, Reply
from .olx import OlxError

# Fields of a draft the user can lock (spec §4.4); locked values are restored after every run
DRAFT_FIELDS = ("title", "description", "category_id", "attributes", "price", "negotiable")


class Catalog(Protocol):
    """OLX category data. `Olx` satisfies it; a disk cache can stand in (spike-notes.md)."""

    def suggest_category(self, q: str) -> list[dict]: ...
    def category(self, category_id: int) -> dict: ...
    def category_attributes(self, category_id: int) -> list[dict]: ...


class Comparables(Protocol):
    """Prices of similar adverts on olx.bg. `comparables.Search` satisfies it."""

    def search(self, query: str, category_id: int | None = None, filters: dict | None = None) -> dict: ...


MAX_SEARCHES = 3  # comparables searches per run: each one is a page load on olx.bg


SYSTEM_PROMPT = """\
You write second-hand listings for olx.bg on behalf of a private seller in Bulgaria.

You get observations from a vision model that looked at the seller's photos, the seller's note,
their profile and their style guide. Work through the tools:
1. suggest_category with a short query (English or Bulgarian, e.g. "iPhone 13", "детска количка").
   Pick the best leaf category.
2. get_category_attributes for it. Fill every required attribute, and optional ones you can tell.
   Use value *codes*, never labels.
3. If something important isn't settled by the observations, use look_at_photos with a specific
   question before guessing.
4. ask_user for what only the seller knows (age, working condition, reason for selling, missing
   accessories). It does not wait for answers: keep going with your best guess. Don't ask again
   what the seller already answered or told you.
5. submit_draft. If it returns problems, fix them and submit again.

The seller's answers and messages are facts and wishes that override the observations; when two
messages disagree, the later one wins.

The listing, your questions to the seller and `price_reasoning` are all in Bulgarian. Follow the
style guide. Title: what the item is, brand, model and the
key spec; no "ПРОДАВАМ", no exclamation marks. Description: honest, mention every defect.
OLX rejects phone numbers, emails and links in the text, more than 50% capital letters, and
punctuation repeated 3+ times. Contact, location and delivery are added by the app, not by you,
except delivery attributes such as `delivery_paid_by`, which follow the profile.

Price in EUR: estimate what this item sells for second-hand in Bulgaria, given its condition, and
suggest a price that sells within a couple of weeks. These are asking prices, so things
usually sell for somewhat less. Explain it briefly in `price_reasoning`; when there are
comparable adverts from olx.bg, say what they're listed at.

Fields listed as locked are fixed by the seller: keep them exactly as given."""

# Added to the prompt only when the seller pressed "Compare prices on OLX"
COMPARE_PROMPT = """

The seller asked to compare prices this run. Before submit_draft, use search_olx_comparables for
what similar items are listed at on olx.bg right now: a short query (brand and model, the way
sellers write it) in the chosen category, filtered by condition when you know it. At most 3
searches. Judge each result by its title: leave out other models, parts, accessories, bundles and
"търся" adverts, then price from the ones that really compare. If the search fails or finds
nothing useful, price from your own knowledge."""

TOOLS = [
    {"type": "function", "function": {
        "name": "look_at_photos",
        "description": "Ask the vision model a specific question about the seller's photos.",
        "parameters": {"type": "object", "properties": {"question": {"type": "string"}},
                       "required": ["question"]}}},
    {"type": "function", "function": {
        "name": "suggest_category",
        "description": "OLX category suggestions (leaf categories with their path) for a short query.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                       "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "get_category_attributes",
        "description": "Attributes of a leaf category: code, label, required, allowed value codes, "
                       "plus the category's photo limit.",
        "parameters": {"type": "object", "properties": {"category_id": {"type": "integer"}},
                       "required": ["category_id"]}}},
    {"type": "function", "function": {
        "name": "search_olx_comparables",
        "description": "Asking prices (EUR) of similar adverts on olx.bg now: one results page with "
                       "title, price, condition, date, plus the median and middle half of all prices.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "A few words, e.g. \"chicco liteway\""},
            "category_id": {"type": "integer", "description": "Leaf category to search in"},
            "filters": {"type": "object", "description": "{attribute code: value code}, e.g. "
                                                         "{\"state\": \"used\"}"},
        }, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "ask_user",
        "description": "Add questions for the seller to the listing card. Non-blocking: answers arrive "
                       "in a later run, so keep going with best guesses. Ask in Bulgarian, short.",
        "parameters": {"type": "object", "properties": {
            "questions": {"type": "array", "items": {"type": "string"}}},
            "required": ["questions"]}}},
    {"type": "function", "function": {
        "name": "submit_draft",
        "description": "Submit the finished listing. Returns problems to fix, or ends the run.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "Bulgarian, 16-150 characters"},
            "description": {"type": "string", "description": "Bulgarian, 40-9000 characters"},
            "category_id": {"type": "integer"},
            "attributes": {"type": "object", "description": "{attribute code: value code, or a list "
                                                            "of value codes for multi-value attributes}"},
            "price": {"type": "number", "description": "Suggested price in EUR"},
            "negotiable": {"type": "boolean"},
            "price_estimate": {"type": "object", "description": "Second-hand value range in EUR",
                               "properties": {"low": {"type": "number"}, "high": {"type": "number"}}},
            "price_reasoning": {"type": "string", "description": "One or two sentences, Bulgarian"},
        }, "required": ["title", "description", "category_id", "attributes", "price"]}}},
]

PROGRESS = {
    "look_at_photos": "Looking at the photos again…",
    "suggest_category": "Choosing category…",
    "get_category_attributes": "Reading category attributes…",
    "search_olx_comparables": "Checking comparables…",
    "ask_user": "Noting questions…",
    "submit_draft": "Writing the listing…",
}


@dataclass
class AgentInput:
    observations: dict
    note: str = ""
    profile: dict = field(default_factory=dict)
    style_guide: str = ""
    draft: dict = field(default_factory=dict)  # current draft, when refining
    locked: list[str] = field(default_factory=list)
    answered: list[dict] = field(default_factory=list)  # [{"question", "answer"}]
    messages: list[str] = field(default_factory=list)  # seller's additional info / requests, oldest first
    comparables: dict | None = None  # the listing's last comparables search, from an earlier run


@dataclass
class AgentResult:
    draft: dict | None  # None when the run ended without any submitted draft
    category: dict | None  # {id, name, photos_limit, attributes} of the draft's category, if looked up
    questions: list[str]
    problems: list[str]  # what check_draft still reports about `draft`
    comparables: dict | None  # the last successful comparables search, for the card
    stopped: str  # submitted | max_iterations | max_cost
    cost_vision: float
    cost_agent: float
    trace: dict


@dataclass
class _Run:
    llm: LLM
    catalog: Catalog
    comparables: Comparables | None
    photo_paths: list[Path]
    note: str
    vision_model: str
    cost_vision: float = 0.0
    cost_agent: float = 0.0
    calls: list[dict] = field(default_factory=list)
    categories: dict[int, dict] = field(default_factory=dict)  # looked-up leaves, with attributes
    questions: list[str] = field(default_factory=list)
    searches: list[dict] = field(default_factory=list)  # successful comparables searches
    search_count: int = 0
    draft: dict | None = None
    problems: list[str] = field(default_factory=list)
    accepted: bool = False

    @property
    def cost(self) -> float:
        return self.cost_vision + self.cost_agent

    def record(self, stage: str, reply: Reply) -> None:
        self.calls.append({"stage": stage, "model": reply.model, **reply.usage})
        if stage == "vision":
            self.cost_vision += reply.cost
        else:
            self.cost_agent += reply.cost

    # ---- tools ----------------------------------------------------------
    def look_at_photos(self, question: str):
        answer, reply = vision.ask(self.llm, self.vision_model, self.photo_paths, self.note, question)
        self.record("vision", reply)
        return {"answer": answer}

    def suggest_category(self, query: str):
        return {"categories": self.catalog.suggest_category(query)[:8]}

    def get_category_attributes(self, category_id: int):
        category = self.catalog.category(category_id)
        if not category.get("is_leaf", True):
            return {"error": f"{category_id} is not a leaf category, pick a more specific one"}
        if category.get("photos_limit") == 0:
            return {"error": f"{category_id} ({category.get('name')}) does not accept adverts from us"}
        attributes = self.catalog.category_attributes(category_id)
        self.categories[category_id] = {"id": category_id, "name": category.get("name"),
                                        "photos_limit": category.get("photos_limit"),
                                        "attributes": attributes}
        return {"category": {"id": category_id, "name": category.get("name"),
                             "photos_limit": category.get("photos_limit")},
                "attributes": [_compact_attribute(a) for a in attributes]}

    def search_olx_comparables(self, query: str, category_id: int | None = None,
                               filters: dict | None = None):
        if not self.comparables:
            return {"error": "comparables are not available, price from your own knowledge"}
        if self.search_count >= MAX_SEARCHES:
            return {"error": f"at most {MAX_SEARCHES} searches per run, price from what you have"}
        self.search_count += 1
        result = self.comparables.search(query, category_id, filters if isinstance(filters, dict) else None)
        self.searches.append(result)
        return {"search_url": result["url"], "total_matches": result["total"], **_for_model(result)}

    def ask_user(self, questions: list[str]):
        self.questions += [q.strip() for q in questions if q.strip()]
        return {"ok": True, "note": "Questions added to the card. Continue with your best guesses."}

    def submit_draft(self, **draft):
        category_id = draft.get("category_id")
        if isinstance(category_id, int) and category_id not in self.categories:
            result = self.get_category_attributes(category_id)
            if "error" in result:
                self.draft, self.problems = draft, [result["error"]]
                return {"accepted": False, "problems": self.problems}
        self.draft = draft
        self.problems = validation.check_draft(draft, self.categories.get(category_id, {}).get("attributes"))
        self.accepted = not self.problems
        if self.accepted:
            return {"accepted": True}
        return {"accepted": False, "problems": self.problems}

    def dispatch(self, call: dict) -> dict:
        name = call["function"]["name"]
        try:
            args = json.loads(call["function"]["arguments"] or "{}")
        except ValueError:
            return {"error": "arguments are not valid JSON"}
        if name not in PROGRESS:
            return {"error": f"unknown tool {name}"}
        try:
            return getattr(self, name)(**args)
        except TypeError as e:
            return {"error": f"bad arguments: {e}"}
        except (OlxError, OSError, ComparablesError) as e:
            return {"error": str(e)}


def _for_model(search: dict) -> dict:
    """A comparables search as the model sees it: no links, no seller details."""
    return {**search["summary"], "adverts": [
        {k: i[k] for k in ("title", "price", "condition", "negotiable", "created")} for i in search["items"]]}


def _compact_attribute(a: dict) -> dict:
    rules = a.get("validation") or {}
    out = {"code": a["code"], "label": a.get("label"), "required": bool(rules.get("required"))}
    if a.get("unit"):
        out["unit"] = a["unit"]
    if rules.get("numeric"):
        out.update(numeric=True, min=rules.get("min"), max=rules.get("max"))
    if rules.get("allow_multiple_values"):
        out["multiple"] = True
    if a.get("values"):
        out["values"] = {str(v["code"]): v.get("label") for v in a["values"]}
    return out


def build_context(inp: AgentInput) -> str:
    parts = [
        "## Observations (from the photos)\n" + json.dumps(inp.observations, ensure_ascii=False, indent=1),
        "## Seller's note\n" + (inp.note or "(none)"),
        "## Profile\n" + json.dumps(inp.profile, ensure_ascii=False, indent=1),
        "## Style guide\n" + (inp.style_guide or "(none)"),
    ]
    if inp.draft:
        parts.append("## Current draft\n" + json.dumps(inp.draft, ensure_ascii=False, indent=1))
    if inp.locked:
        locked = {f: inp.draft.get(f) for f in inp.locked}
        parts.append("## Locked fields (keep exactly)\n" + json.dumps(locked, ensure_ascii=False, indent=1))
    if inp.answered:
        parts.append("## Seller's answers\n" + "\n".join(
            f"- Q: {a['question']}\n  A: {a['answer']}" for a in inp.answered))
    if inp.messages:
        parts.append("## Seller's additional information and requests (oldest first)\n"
                     + "\n".join(f"- {m}" for m in inp.messages))
    if inp.comparables:
        c = inp.comparables
        parts.append(f"## Comparable adverts on olx.bg (search \"{c['query']}\" on {c['at'][:10]})\n"
                     + json.dumps(_for_model(c), ensure_ascii=False, indent=1))
    return "\n\n".join(parts)


def run_agent(llm: LLM, model: str, inp: AgentInput, *, catalog: Catalog, photo_paths: list[Path],
              vision_model: str, comparables: Comparables | None = None, max_iterations: int = 15,
              max_cost: float = 0.50, on_progress: Callable[[str], None] = lambda step: None) -> AgentResult:
    run = _Run(llm, catalog, comparables, photo_paths, inp.note, vision_model)
    # The comparables tool is offered only when the seller asked for a comparison
    system = SYSTEM_PROMPT + (COMPARE_PROMPT if comparables else "")
    tools = TOOLS if comparables else [t for t in TOOLS if t["function"]["name"] != "search_olx_comparables"]
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": build_context(inp)}]
    stopped = "max_iterations"
    for i in range(max_iterations):
        if run.cost >= max_cost:
            stopped = "max_cost"
            break
        # Last turn: force a draft out of the model rather than ending with nothing
        last = i == max_iterations - 1
        tool_choice = {"type": "function", "function": {"name": "submit_draft"}} if last else "auto"
        on_progress("Thinking…" if i else "Planning the listing…")
        reply = llm.chat(model, messages, tools=tools, tool_choice=tool_choice)
        run.record("agent", reply)
        messages.append(reply.message)
        if not reply.tool_calls:
            messages.append({"role": "user", "content": "Use the tools. Finish by calling submit_draft."})
            continue
        for call in reply.tool_calls:
            on_progress(PROGRESS.get(call["function"]["name"], "Working…"))
            result = run.dispatch(call)
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": json.dumps(result, ensure_ascii=False)})
        if run.accepted:
            stopped = "submitted"
            break

    draft = _apply_locks(run.draft, inp) if run.draft else None
    category = run.categories.get(draft.get("category_id")) if draft else None
    trace = {"stage": "agent", "model": model, "stopped": stopped, "calls": run.calls,
             "messages": messages,
             "cost": {"vision": run.cost_vision, "agent": run.cost_agent}}
    return AgentResult(draft, category, run.questions, run.problems,
                       run.searches[-1] if run.searches else None, stopped,
                       run.cost_vision, run.cost_agent, trace)


def _apply_locks(draft: dict, inp: AgentInput) -> dict:
    """Locks are enforced in code: whatever the model wrote, locked fields keep the seller's values."""
    draft = {k: v for k, v in draft.items()}
    for f in inp.locked:
        if f in inp.draft:
            draft[f] = inp.draft[f]
    return draft
