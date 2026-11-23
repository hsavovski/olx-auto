"""The OLX `POST /adverts` payload, built from a listing and the user's profile (spike-notes.md).

Call it only once `validation.submit_problems` is empty: it maps fields and doesn't check them.
"""
CURRENCY = "EUR"


def _attributes(values: dict) -> list[dict]:
    out = []
    for code, value in values.items():
        if isinstance(value, list):
            given = [str(v) for v in value if v not in (None, "")]
            if given:
                # OLX takes several values as `values` (confirmed on a real post, spike-notes.md)
                out.append({"code": code, "values": given})
        elif value not in (None, ""):
            out.append({"code": code, "value": str(value)})
    return out


def payload(listing: dict, profile: dict, image_urls: list[str]) -> dict:
    draft, contact, location = listing["draft"], profile["contact"], profile["location"]
    advert = {
        "title": draft["title"],
        "description": draft["description"],
        "category_id": draft["category_id"],
        "advertiser_type": profile["advertiser_type"],
        "contact": {"name": contact["name"].strip()},
        "location": {"city_id": location["city_id"]},
        "price": {"value": draft["price"], "currency": CURRENCY,
                  "negotiable": bool(draft.get("negotiable"))},
        "attributes": _attributes(draft.get("attributes") or {}),
        "courier": bool(profile["delivery"].get("courier")),
        "images": [{"url": url} for url in image_urls],
    }
    if contact.get("phone"):
        advert["contact"]["phone"] = contact["phone"]
    if location.get("district_id"):
        advert["location"]["district_id"] = location["district_id"]
    return advert
