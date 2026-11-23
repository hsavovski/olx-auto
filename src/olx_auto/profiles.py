"""A user's profile.json (spec §6): contact, location, advertiser type, delivery preferences.

Code maps it into the advert payload at submit; the agent sees it without `contact`. Keys this
module doesn't know about are kept as they are.
"""
import json
import os
import re

from .users import User

ADVERTISER_TYPES = ("private", "business")
PHONE_RE = re.compile(r"^\+?[\d\s\-/()]{6,20}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def load(user: User) -> dict:
    try:
        profile = json.loads(user.profile_file.read_text())
    except (OSError, ValueError):
        profile = {}
    profile.setdefault("advertiser_type", "private")
    for key in ("contact", "location", "delivery"):
        if not isinstance(profile.get(key), dict):
            profile[key] = {}
    return profile


def save(user: User, profile: dict) -> None:
    tmp = user.profile_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(profile, ensure_ascii=False, indent=2))
    os.replace(tmp, user.profile_file)


def update_details(profile: dict, *, name: str, phone: str, email: str, advertiser_type: str,
                   courier: bool, delivery_note: str) -> list[str]:
    """Apply the profile form. Returns errors; the profile is changed only when there are none."""
    name, phone, email = name.strip(), phone.strip(), email.strip()
    errors = []
    if not name:
        errors.append("Contact name is required (OLX needs it)")
    if phone and not PHONE_RE.match(phone):
        errors.append("Phone should be digits, optionally starting with +")
    if email and not EMAIL_RE.match(email):
        errors.append("Email doesn't look right")
    if advertiser_type not in ADVERTISER_TYPES:
        errors.append("Advertiser type must be private or business")
    if errors:
        return errors
    profile["contact"].update(name=name, phone=phone, email=email)
    profile["advertiser_type"] = advertiser_type
    profile["delivery"].update(courier=courier, note=delivery_note.strip())
    return []


def set_location(profile: dict, *, city_id: int, city_name: str, district_id: int | None,
                 district_name: str | None) -> None:
    profile["location"].update(city_id=city_id, city_name=city_name, district_id=district_id,
                               district_name=district_name)
    profile["location"].pop("note", None)  # the example's "how to find ids" hint


def location_choices(candidates: list[dict]) -> list[dict]:
    """OLX /locations results → unique {city_id, city_name, district_id, district_name}, nearest first."""
    out, seen = [], set()
    for c in candidates:
        city, district = c.get("city") or {}, c.get("district") or {}
        if not city.get("id"):
            continue
        choice = {"city_id": city["id"], "city_name": city.get("name", ""),
                  "district_id": district.get("id"), "district_name": district.get("name")}
        key = (choice["city_id"], choice["district_id"])
        if key not in seen:
            seen.add(key)
            out.append(choice)
    return out
