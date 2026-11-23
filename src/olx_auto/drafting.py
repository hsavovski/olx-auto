"""Listing → draft: vision stage (once per listing), then an agent run. Used by the web app's
background task and by `olx-auto draft`.

Progress, the draft, questions and costs are written to listing.json as the run goes, so the
card can show them and the phone can be closed. Every run is appended to trace.json.
"""
import json
import os
from datetime import UTC, datetime

from . import agent, listings, vision
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


def _add_cost(user: User, listing_id: str, vision_cost: float = 0.0, agent_cost: float = 0.0) -> None:
    listing = listings.load(user, listing_id)
    listing["cost"]["vision"] += vision_cost
    listing["cost"]["agent"] += agent_cost
    listings.save(user, listing)


def _append_trace(user: User, listing_id: str, run: dict) -> None:
    path = listings.trace_path(user, listing_id)
    trace = json.loads(path.read_text()) if path.exists() else {"runs": []}
    trace["runs"].append(run)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(trace, ensure_ascii=False, indent=1))
    os.replace(tmp, path)


def _profile(user: User) -> dict:
    try:
        profile = json.loads(user.profile_file.read_text())
    except (OSError, ValueError):
        return {}
    profile.pop("contact", None)  # mapped into the payload by code; the model doesn't need it
    return profile


def generate(settings: Settings, user: User, listing_id: str, *, instruction: str = "",
             llm: LLM | None = None, catalog: Catalog | None = None) -> dict:
    """Draft (or refine) a listing. Errors end up in listing["ai_error"], never raised."""
    listing = _update(user, listing_id, progress="Starting…", ai_error=None)
    try:
        llm = llm or LLM(settings.openrouter_api_key)
        catalog = catalog or user.olx(settings)
        photo_paths = [listings.photo_path(user, listing_id, p) for p in listing["photos"]]

        observations = listing.get("observations")
        if not observations:
            _update(user, listing_id, progress="Reading photos…")
            observations, reply = vision.observe(llm, settings.vision_model, photo_paths, listing["note"])
            _add_cost(user, listing_id, vision_cost=reply.cost)
            _update(user, listing_id, observations=observations)
            _append_trace(user, listing_id, {
                "stage": "vision", "model": reply.model, "at": _now(), "usage": reply.usage,
                "observations": observations})

        answered = [q for q in listing["questions"] if q.get("answer")]
        inp = agent.AgentInput(
            observations=observations, note=listing["note"], profile=_profile(user),
            style_guide=user.style_guide_file.read_text() if user.style_guide_file.exists() else "",
            draft=listing["draft"], locked=listing["locked"], answered=answered,
            instruction=instruction)
        result = agent.run_agent(
            llm, settings.agent_model, inp, catalog=catalog, photo_paths=photo_paths,
            vision_model=settings.vision_model, max_iterations=settings.agent_max_iterations,
            max_cost=settings.agent_max_cost,
            on_progress=lambda step: _update(user, listing_id, progress=step))
        _add_cost(user, listing_id, result.cost_vision, result.cost_agent)
        _append_trace(user, listing_id, {**result.trace, "at": _now(), "instruction": instruction})

        # Answered questions stay; unanswered ones are replaced by this run's questions
        next_id = max((q["id"] for q in listing["questions"]), default=0) + 1
        questions = answered + [{"id": next_id + i, "question": q, "answer": None}
                                for i, q in enumerate(result.questions)]
        fields = {"questions": questions, "progress": None, "problems": result.problems,
                  "agent_stopped": result.stopped}
        if result.draft:
            fields["draft"] = result.draft
        return _update(user, listing_id, **fields)
    except (LLMError, OlxError, OSError, RuntimeError, KeyError) as e:
        return _update(user, listing_id, progress=None, ai_error=str(e))
