"""Submit readiness (validation.submit_problems) and the OLX advert payload (adverts.payload)."""
import copy

import pytest

from olx_auto import adverts, profiles, validation
from tests.test_ai import GOOD_DRAFT, STROLLER_ATTRS
from tests.test_card import client, drafted  # noqa: F401 (fixtures)

LISTING = {"draft": GOOD_DRAFT, "photos": ["01.jpg", "02.jpg"],
           "category": {"id": 185, "name": "Детски колички", "photos_limit": 12,
                        "attributes": STROLLER_ATTRS}}
PROFILE = {"advertiser_type": "private",
           "contact": {"name": "Тест Тестов", "phone": "+359 888 000 000", "email": "test@example.com"},
           "location": {"city_id": 8771, "city_name": "София", "district_id": 1585,
                        "district_name": "Център"},
           "delivery": {"courier": True, "note": ""}}


def changed(base: dict, path: str, value) -> dict:
    out = copy.deepcopy(base)
    *parents, key = path.split(".")
    target = out
    for p in parents:
        target = target[p]
    target[key] = value
    return out


def test_ready_listing_has_no_problems():
    assert validation.submit_problems(LISTING, PROFILE) == []


@pytest.mark.parametrize("listing, profile, problem", [
    (changed(LISTING, "photos", []), PROFILE, "photos: add at least one"),
    (changed(LISTING, "photos", [f"{i:02d}.jpg" for i in range(1, 14)]), PROFILE,
     "photos: 13, this category takes at most 12"),
    (changed(LISTING, "category", None), PROFILE, "category: its attributes aren't loaded"),
    (changed(LISTING, "draft.price", None), PROFILE, "price: must be a positive number"),
    (LISTING, changed(PROFILE, "location", {}), "location: not set"),
    (LISTING, changed(PROFILE, "contact.name", " "), "contact: name missing"),
])
def test_submit_problems(listing, profile, problem):
    problems = validation.submit_problems(listing, profile)
    assert len(problems) == 1 and problems[0].startswith(problem)


def test_payload_maps_listing_and_profile():
    advert = adverts.payload(LISTING, PROFILE, ["https://s3/1.jpg", "https://s3/2.jpg"])
    assert advert == {
        "title": GOOD_DRAFT["title"], "description": GOOD_DRAFT["description"], "category_id": 185,
        "advertiser_type": "private",
        "contact": {"name": "Тест Тестов", "phone": "+359 888 000 000"},
        "location": {"city_id": 8771, "district_id": 1585},
        "price": {"value": 60, "currency": "EUR", "negotiable": True},
        "attributes": [{"code": "state", "value": "used"},
                       {"code": "delivery_paid_by", "values": ["207", "209"]}],
        "courier": True,
        "images": [{"url": "https://s3/1.jpg"}, {"url": "https://s3/2.jpg"}],
    }


def test_payload_leaves_out_empty_optional_fields():
    listing = changed(LISTING, "draft.attributes", {"state": "used", "model": "", "delivery_paid_by": []})
    profile = changed(changed(PROFILE, "contact.phone", ""), "location.district_id", None)
    advert = adverts.payload(listing, profile, [])
    assert advert["contact"] == {"name": "Тест Тестов"}
    assert advert["location"] == {"city_id": 8771}
    assert advert["attributes"] == [{"code": "state", "value": "used"}]


def test_card_lists_profile_problems(client, user, drafted):  # noqa: F811
    page = client.get(f"/listings/{drafted['id']}").text
    assert "location: not set, choose it in your profile" in page

    profile = profiles.load(user)
    profile["location"] = dict(PROFILE["location"])
    profiles.save(user, profile)
    assert "To fix before submitting" not in client.get(f"/listings/{drafted['id']}").text
