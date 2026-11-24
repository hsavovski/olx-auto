"""Listing → draft: vision stage (once per listing), web research, then an agent run, then style
learning from the seller's feedback. Used by the web app's background tasks and by `olx-auto draft`.

Progress, the draft, questions and costs are written to listing.json as the run goes, so the
card can show them and the phone can be closed. Every run is appended to trace.json.
"""
import json
import os
import threading
from datetime import UTC, datetime

from . import agent, comparables, listings, profiles, research, style, vision
from .agent import Catalog
from .config import Settings
from .llm import LLM, LLMError
from .olx import OlxError
from .users import User


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _update(user: User, listing_id: str, **fields) -> dict:
    """Reload before writing, so fields changed elsewhere meanwhile aren't overwritten."""
    listing = listings.load(user, listing_id)
    listing.update(fields)
    listings.save(user, listing)
    return listing


def mark_started(user: User, listing_id: str) -> None:
    _update(user, listing_id, progress="Starting…", ai_error=None)


def _add_cost(user: User, listing_id: str, **costs: float) -> None:
    """costs: vision=, agent=, style= (USD)."""
    listing = listings.load(user, listing_id)
    for stage, cost in costs.items():
        listing["cost"][stage] += cost
    listings.save(user, listing)


def _append_trace(user: User, listing_id: str, run: dict) -> None:
    path = listings.trace_path(user, listing_id)
    trace = json.loads(path.read_text()) if path.exists() else {"runs": []}
    trace["runs"].append(run)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(trace, ensure_ascii=False, indent=1))
    os.replace(tmp, path)


def _profile(user: User) -> dict:
    profile = profiles.load(user)
    profile.pop("contact", None)  # mapped into the payload by code; the model doesn't need it
    return profile


def generate(settings: Settings, user: User, listing_id: str, *,
             llm: LLM | None = None, catalog: Catalog | None = None,
             compare: bool = False, search: agent.Comparables | None = None) -> dict:
    """Draft (or refine) a listing, then learn from any new feedback. `compare` (the card's
    "Compare prices on OLX") lets the agent search olx.bg; other runs don't go online for prices.
    Errors end up in listing["ai_error"], never raised."""
    listing = _update(user, listing_id, progress="Starting…", ai_error=None)
    try:
        llm = llm or LLM(settings.openrouter_api_key)
        catalog = catalog or user.olx(settings)
        if compare:
            search = search or comparables.Search(settings.data_dir / "olx_category_paths.json")
        else:
            search = None
        photo_paths = [listings.photo_path(user, listing_id, p) for p in listing["photos"]]

        observations = listing.get("observations")
        if not observations:
            _update(user, listing_id, progress="Reading photos…")
            observations, reply = vision.observe(llm, settings.vision_model, photo_paths, listing["note"])
            _add_cost(user, listing_id, vision=reply.cost)
            _update(user, listing_id, observations=observations)
            _append_trace(user, listing_id, {
                "stage": "vision", "model": reply.model, "at": _now(), "usage": reply.usage,
                "observations": observations})

        answered = [q for q in listing["questions"] if q.get("answer")]
        messages = [m["text"] for m in listing["messages"]]
        facts = _research(settings, user, listing_id, llm, observations, answered, messages)
        inp = agent.AgentInput(
            observations=observations, research=facts, note=listing["note"], profile=_profile(user),
            style_guide=style.read_guide(user), draft=listing["draft"], locked=listing["locked"],
            answered=answered, messages=messages, comparables=listing.get("comparables"))
        result = agent.run_agent(
            llm, settings.agent_model, inp, catalog=catalog, comparables=search, photo_paths=photo_paths,
            vision_model=settings.vision_model, max_iterations=settings.agent_max_iterations,
            max_cost=settings.agent_max_cost,
            on_progress=lambda step: _update(user, listing_id, progress=step))
        _add_cost(user, listing_id, vision=result.cost_vision, agent=result.cost_agent)
        _append_trace(user, listing_id, {**result.trace, "at": _now()})

        # Answered questions stay; unanswered ones are replaced by this run's questions
        next_id = max((q["id"] for q in listing["questions"]), default=0) + 1
        questions = answered + [{"id": next_id + i, "question": q, "answer": None}
                                for i, q in enumerate(result.questions)]
        fields = {"questions": questions, "problems": result.problems, "agent_stopped": result.stopped}
        if result.draft:
            fields.update(draft=result.draft, ai_draft=result.draft)
        if result.category:
            fields["category"] = result.category
        if result.comparables:  # a run that found nothing keeps the earlier result
            fields["comparables"] = result.comparables
        _update(user, listing_id, **fields)
    except (LLMError, OlxError, OSError, RuntimeError, KeyError) as e:
        return _update(user, listing_id, progress=None, ai_error=str(e))
    try_learn_style(settings, user, listing_id, llm=llm)
    return listings.load(user, listing_id)


def _research(settings: Settings, user: User, listing_id: str, llm: LLM, observations: dict,
              answered: list[dict], messages: list[str]) -> dict | None:
    """Look the product up once, and again when the seller has said more since (they may have
    corrected the model). A failure is traced and drafting goes on without it."""
    listing = listings.load(user, listing_id)
    seen = {"messages": len(messages), "answers": len(answered)}
    if listing["research_seen"] == seen:
        return listing["research"]
    _update(user, listing_id, progress="Looking up the product…")
    try:
        facts, reply = research.research(llm, settings.research_model, observations, listing["note"],
                                         answered, messages, settings.search_engine)
    except LLMError as e:
        _append_trace(user, listing_id, {"stage": "research", "model": settings.research_model,
                                         "engine": settings.search_engine, "at": _now(), "error": str(e)})
        return listing["research"]
    _add_cost(user, listing_id, research=reply.cost)
    _update(user, listing_id, research=facts, research_seen=seen)
    _append_trace(user, listing_id, {"stage": "research", "model": reply.model,
                                     "engine": settings.search_engine, "at": _now(),
                                     "usage": reply.usage, "research": facts})
    return facts


_learn_lock = threading.Lock()  # one learner at a time, so two updates can't overwrite each other

# Hand edits of these fields say something about style; attribute edits are facts
STYLE_FIELDS = ("title", "description", "price")


def learn_style(settings: Settings, user: User, listing_id: str, *, llm: LLM | None = None) -> dict | None:
    """Turn this listing's new feedback (messages, text edits) into style guide rules.
    Returns the log entry when the guide changed. Raises LLMError; callers decide where it goes."""
    with _learn_lock:
        listing = listings.load(user, listing_id)
        seen = listing["learned"]
        messages = [m["text"] for m in listing["messages"][seen["messages"]:]]
        edits = [e for e in listing["edits"][seen["edits"]:] if e["field"] in STYLE_FIELDS]
        learned = {"messages": len(listing["messages"]), "edits": len(listing["edits"])}
        if not messages and not edits:
            _update(user, listing_id, learned=learned)
            return None
        _update(user, listing_id, progress="Updating the style guide…")
        llm = llm or LLM(settings.openrouter_api_key)
        result, reply = style.learn(llm, settings.agent_model, style.read_guide(user), listing["draft"],
                                    messages, edits)
        _add_cost(user, listing_id, style=reply.cost)
        _append_trace(user, listing_id, {"stage": "style", "model": reply.model, "at": _now(),
                                         "usage": reply.usage, "feedback": {"messages": messages,
                                                                            "edits": edits},
                                         "result": result})
        entry = None
        if result.get("changed"):
            style.write_guide(user, result["guide"], summary=result["summary"], source=listing_id)
            entry = style.log(user)[0]
        _update(user, listing_id, learned=learned)
        return entry


def try_learn_style(settings: Settings, user: User, listing_id: str, *,
                              llm: LLM | None = None) -> None:
    """After a run or hand edits: errors go to ai_error, progress is cleared."""
    try:
        learn_style(settings, user, listing_id, llm=llm)
        _update(user, listing_id, progress=None)
    except (LLMError, OSError, KeyError) as e:
        _update(user, listing_id, progress=None, ai_error=f"Style guide update failed: {e}")
