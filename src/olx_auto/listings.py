"""Listings: one folder per listing under data/users/<user>/listings/<id>/ (see docs/SPEC.md §10)."""
import json
import os
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path

from . import photos
from .users import User

ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
PHOTO_RE = re.compile(r"^\d{2}\.jpg$")
MAX_PHOTOS = 20  # upload cap; the per-category OLX limit is enforced at validation

STATUSES = ("draft", "submitting", "posted", "failed")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _dir(user: User, listing_id: str) -> Path:
    if not ID_RE.match(listing_id):
        raise KeyError(listing_id)
    return user.listings_dir / listing_id


def _new_id() -> str:
    return f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


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

    listing = {
        "id": listing_id,
        "created_at": _now(),
        "status": "draft",
        "note": note.strip(),
        "photos": names,
        "observations": None,
        "draft": {},
        "locked": [],
        "questions": [],
        "problems": [],
        "progress": None,
        "ai_error": None,
        "cost": {"vision": 0.0, "agent": 0.0},
        "olx": None,
        "error": None,
    }
    save(user, listing)
    return listing


def load(user: User, listing_id: str) -> dict:
    try:
        return json.loads((_dir(user, listing_id) / "listing.json").read_text())
    except FileNotFoundError:
        raise KeyError(listing_id) from None


def list_all(user: User) -> list[dict]:
    if not user.listings_dir.exists():
        return []
    out = []
    for p in user.listings_dir.iterdir():
        if ID_RE.match(p.name) and (p / "listing.json").exists():
            out.append(json.loads((p / "listing.json").read_text()))
    return sorted(out, key=lambda item: item["created_at"], reverse=True)


def photo_path(user: User, listing_id: str, name: str) -> Path:
    if not PHOTO_RE.match(name):
        raise KeyError(name)
    path = _dir(user, listing_id) / "photos" / name
    if not path.exists():
        raise KeyError(name)
    return path


def trace_path(user: User, listing_id: str) -> Path:
    return _dir(user, listing_id) / "trace.json"


def total_cost(listing: dict) -> float:
    return sum(listing["cost"].values())
