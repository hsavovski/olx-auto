"""Listings: one folder per listing under data/users/<user>/listings/<id>/ (see docs/SPEC.md §10)."""
import json
import os
import re
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import photos
from .users import User

ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
PHOTO_RE = re.compile(r"^\d{2}\.jpg$")
MAX_PHOTOS = 20  # upload cap; the per-category OLX limit is enforced at validation

STATUSES = ("draft", "submitting", "posted", "failed")
# Draft fields the card can edit; editing one locks it (spec §4.4)
EDITABLE = ("title", "description", "price", "negotiable", "attributes")
# A run that hasn't written progress for this long died (e.g. the server restarted)
STALE_RUN = timedelta(minutes=10)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _dir(user: User, listing_id: str) -> Path:
    if not ID_RE.match(listing_id):
        raise KeyError(listing_id)
    return user.listings_dir / listing_id


def _new_id() -> str:
    return f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def _defaults() -> dict:
    return {
        "status": "draft",
        "note": "",
        "photos": [],
        "observations": None,
        "category": None,  # {id, name, photos_limit, attributes} of the draft's category
        "draft": {},
        "ai_draft": {},  # the draft as the agent last wrote it, before hand edits
        "locked": [],
        "questions": [],
        "messages": [],  # seller's additional information / change requests: [{at, text}]
        "edits": [],  # hand edits: [{at, field, before, after}]
        "learned": {"messages": 0, "edits": 0},  # feedback already used for the style guide
        "problems": [],
        "progress": None,
        "ai_error": None,
        "cost": {"vision": 0.0, "agent": 0.0, "style": 0.0},
        "olx": None,
        "error": None,
    }


def save(user: User, listing: dict) -> None:
    listing["updated_at"] = _now()
    path = _dir(user, listing["id"]) / "listing.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(listing, ensure_ascii=False, indent=2))
    os.replace(tmp, path)


def create(user: User, note: str, raw_photos: list[bytes]) -> dict:
    if not raw_photos:
        raise ValueError("Add at least one photo")
    if len(raw_photos) > MAX_PHOTOS:
        raise ValueError(f"At most {MAX_PHOTOS} photos")
    processed = [photos.process(p) for p in raw_photos]  # validate all before touching disk

    listing_id = _new_id()
    photo_dir = _dir(user, listing_id) / "photos"
    photo_dir.mkdir(parents=True)
    names = []
    for i, data in enumerate(processed, 1):
        name = f"{i:02d}.jpg"
        (photo_dir / name).write_bytes(data)
        names.append(name)

    listing = {**_defaults(), "id": listing_id, "created_at": _now(), "note": note.strip(),
               "photos": names}
    save(user, listing)
    return listing


def _read(path: Path) -> dict:
    """Listings written by older code get the fields added since."""
    listing = {**_defaults(), **json.loads(path.read_text())}
    listing["cost"] = {**_defaults()["cost"], **listing["cost"]}
    return listing


def load(user: User, listing_id: str) -> dict:
    try:
        return _read(_dir(user, listing_id) / "listing.json")
    except FileNotFoundError:
        raise KeyError(listing_id) from None


def list_all(user: User) -> list[dict]:
    if not user.listings_dir.exists():
        return []
    out = []
    for p in user.listings_dir.iterdir():
        if ID_RE.match(p.name) and (p / "listing.json").exists():
            out.append(_read(p / "listing.json"))
    return sorted(out, key=lambda item: item["created_at"], reverse=True)


def photo_path(user: User, listing_id: str, name: str) -> Path:
    if not PHOTO_RE.match(name):
        raise KeyError(name)
    path = _dir(user, listing_id) / "photos" / name
    if not path.exists():
        raise KeyError(name)
    return path


def delete_photo(user: User, listing: dict, name: str) -> None:
    """Remove a photo from the listing and from disk (e.g. more than the category's `photos_limit`).
    The last photo stays: a listing always has one. The other photos keep their names."""
    if name not in listing["photos"]:
        raise KeyError(name)
    if len(listing["photos"]) == 1:
        raise ValueError("A listing needs at least one photo")
    listing["photos"].remove(name)
    save(user, listing)
    photo_path(user, listing["id"], name).unlink()


def trace_path(user: User, listing_id: str) -> Path:
    return _dir(user, listing_id) / "trace.json"


def total_cost(listing: dict) -> float:
    return sum(listing["cost"].values())


def is_running(listing: dict) -> bool:
    if not listing.get("progress"):
        return False
    return datetime.fromisoformat(listing["updated_at"]) > datetime.now(UTC) - STALE_RUN


def add_message(listing: dict, text: str) -> None:
    """Additional information or a change request from the seller; every later run sees it."""
    if text.strip():
        listing["messages"].append({"at": _now(), "text": text.strip()})


def _canonical(field: str, value):
    if field == "attributes":
        return {k: sorted(map(str, v)) if isinstance(v, list) else str(v) for k, v in (value or {}).items()}
    return value


def edit_draft(listing: dict, changes: dict) -> list[str]:
    """Apply hand edits from the card: changed fields are locked and recorded for style learning."""
    changed = []
    for field, value in changes.items():
        if field not in EDITABLE:
            raise ValueError(f"Not editable: {field}")
        before = listing["draft"].get(field)
        if _canonical(field, before) == _canonical(field, value):
            continue
        changed.append(field)
        listing["edits"].append({"at": _now(), "field": field, "before": before, "after": value})
        listing["draft"][field] = value
        if field not in listing["locked"]:
            listing["locked"].append(field)
    return changed
