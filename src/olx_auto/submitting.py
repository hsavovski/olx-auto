"""Listing → OLX advert (spec §4.5–4.6): photos to S3, `POST /adverts`, S3 objects deleted, result in
listing.json. Also keeps a posted advert's OLX status fresh for the card, and removes test adverts.

Submit runs inside the request: OLX answers within seconds, and the seller sees the result at once.
"""
import secrets
import time
from datetime import UTC, datetime, timedelta

from . import adverts, listings, profiles, s3, validation
from .config import Settings
from .olx import Olx, OlxError
from .users import User

# OLX advert status → what the card shows (spike-notes.md, spec §4.5)
OLX_STATES = {
    "disabled": "pending moderation", "new": "pending moderation", "active": "live",
    "limited": "needs payment on OLX", "moderated": "rejected by OLX", "blocked": "rejected by OLX",
    "removed_by_moderator": "rejected by OLX", "removed_by_user": "removed", "outdated": "expired",
    "deleted": "deleted",
}
FINAL_STATES = ("removed_by_moderator", "removed_by_user", "outdated", "deleted")
STATUS_CHECK_EVERY = timedelta(minutes=1)
# A submit is a few seconds; one still "submitting" after this was cut off (e.g. a restart)
STALE_SUBMIT = timedelta(minutes=5)
DEACTIVATE_WAIT = 15  # seconds `remove` waits for OLX to finish deactivating


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _older_than(timestamp: str | None, age: timedelta) -> bool:
    return not timestamp or datetime.fromisoformat(timestamp) < datetime.now(UTC) - age


def interrupted(listing: dict) -> bool:
    return listing["status"] == "submitting" and _older_than(listing.get("updated_at"), STALE_SUBMIT)


def can_submit(listing: dict) -> bool:
    return listing["status"] in ("draft", "failed") or interrupted(listing)


def frozen(listing: dict) -> bool:
    """Posted or being submitted: the card can't change it any more."""
    return listing["status"] in ("submitting", "posted") and not interrupted(listing)


def _card_field(olx_field: str) -> str:
    """An OLX error path (spec §4.6) → the card field it belongs to."""
    if olx_field.startswith(("params.price", "price")):
        return "price"
    if olx_field.startswith("params."):
        return "attributes." + olx_field.removeprefix("params.")
    if olx_field.startswith(("contact.", "location.", "advertiser_type")):
        return f"profile ({olx_field})"
    return olx_field


def _olx_error(e: OlxError) -> dict:
    error = e.body.get("error") if isinstance(e.body, dict) else None
    error = error if isinstance(error, dict) else {}
    fields = [{"field": _card_field(v.get("field") or ""), "olx_field": v.get("field"),
               "title": v.get("title"), "detail": v.get("detail")}
              for v in error.get("validation") or []]
    message = error.get("title") or error.get("detail") or str(e)[:300]
    return {"message": f"OLX refused the advert: {message}", "fields": fields}


def submit(settings: Settings, user: User, listing_id: str, *, olx: Olx | None = None,
           store: s3.PhotoStore | None = None) -> dict:
    """Post the listing. Ends `posted` (with the advert in `olx`) or `failed` (with `error`).
    Raises ValueError if the listing can't be submitted now (status, or `submit_problems`)."""
    listing = listings.load(user, listing_id)
    profile = profiles.load(user)
    if not can_submit(listing):
        raise ValueError(f"Can't submit a listing that is {listing['status']}")
    if validation.submit_problems(listing, profile):
        raise ValueError("Fix the listing's problems first")
    listing.update(status="submitting", error=None)
    listings.save(user, listing)

    olx = olx or user.olx(settings)
    keys, sent, advert, error = [], False, None, None
    try:
        store = store or s3.PhotoStore(settings)
        urls = []
        for name in listing["photos"]:
            key = f"{user.name}/{listing_id}/{secrets.token_hex(8)}-{name}"
            urls.append(store.put(key, listings.photo_path(user, listing_id, name)))
            keys.append(key)
        sent = True
        advert = olx.create_advert(**adverts.payload(listing, profile, urls))
    except OlxError as e:
        error = _olx_error(e)
    except OSError as e:
        if sent:  # a timeout doesn't tell whether OLX created the advert
            error = {"message": f"No answer from OLX ({e}). The advert may still have been created: "
                                "check your adverts on OLX before submitting again.", "fields": []}
        else:
            error = {"message": f"Photo upload failed: {e}", "fields": []}
    except (*s3.S3Error, RuntimeError) as e:
        error = {"message": str(e), "fields": []}
    finally:
        for key in keys:  # OLX has its own copies by now; on failure they aren't needed either
            try:
                store.delete(key)
            except s3.S3Error:
                pass  # the bucket's 30-day lifecycle rule removes it

    if error:
        listing.update(status="failed", error=error)
    else:
        listing.update(status="posted", olx={
            "id": advert["id"], "url": advert.get("url"), "status": advert.get("status"),
            "images": [i["url"] for i in advert.get("images") or []],
            "posted_at": _now(), "checked_at": _now()})
    listings.save(user, listing)
    return listing


def refresh_status(settings: Settings, user: User, listing: dict, *, olx: Olx | None = None) -> None:
    """Update a posted advert's OLX status, at most once per STATUS_CHECK_EVERY. Failures keep the old one."""
    advert = listing.get("olx")
    if listing["status"] != "posted" or not advert or advert.get("status") in FINAL_STATES:
        return
    if not _older_than(advert.get("checked_at"), STATUS_CHECK_EVERY):
        return
    olx = olx or user.olx(settings)
    try:
        status = olx.advert(advert["id"])["status"]
    except OlxError as e:
        if e.status != 404:
            return
        status = "deleted"
    except (OSError, RuntimeError):
        return
    advert.update(status=status, checked_at=_now())
    listings.save(user, listing)


def remove(settings: Settings, user: User, listing_id: str, *, olx: Olx | None = None) -> dict:
    """Take a listing's advert off OLX (for test adverts): deactivate if active, since OLX refuses to
    delete an active one, then delete. The listing goes back to `draft` so it can be posted again."""
    listing = listings.load(user, listing_id)
    if not listing.get("olx"):
        raise ValueError("This listing has no OLX advert")
    olx = olx or user.olx(settings)
    advert_id = listing["olx"]["id"]
    try:
        if olx.advert(advert_id)["status"] == "active":
            olx.advert_command(advert_id, "deactivate")
            # Deactivation finishes a moment later; deleting before that is refused ("Невалидно състояние")
            deadline = time.monotonic() + DEACTIVATE_WAIT
            while olx.advert(advert_id)["status"] == "active" and time.monotonic() < deadline:
                time.sleep(1)
        olx.delete_advert(advert_id)
    except OlxError as e:
        if e.status != 404:  # already gone
            raise
    listing["olx"].update(status="deleted", checked_at=_now())
    listing["status"] = "draft"
    listings.save(user, listing)
    return listing
