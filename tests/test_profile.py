"""Profile page: contact/delivery details, location via OLX /locations (mocked), display name, password."""
import json

import pytest

from olx_auto import profiles, users
from olx_auto.olx import Olx, OlxError
from tests.conftest import PASSWORD

SOFIA = [
    {"city": {"id": 8771, "name": "София"}, "district": {"id": 1585, "name": "Център"}},
    {"city": {"id": 8771, "name": "София"}, "district": {"id": 1585, "name": "Център"}},  # duplicate
    {"city": {"id": 8771, "name": "София"}, "district": {"id": 1387, "name": "Зона Б-5-3"}},
    {"city": {"id": 9000, "name": "Банкя"}, "district": None},
]
DISTRICTS = {8771: [{"id": 1585, "name": "Център"}, {"id": 1387, "name": "Зона Б-5-3"}]}
DETAILS = {"display_name": "Ицо", "name": "Тест Тестов", "phone": "+359 888 000 000",
           "email": "test@example.com", "advertiser_type": "private", "courier": "true",
           "delivery_note": "лично или OLX доставка"}


@pytest.fixture
def located(monkeypatch):
    calls = []

    def fake_locations(self, latitude, longitude):
        calls.append((latitude, longitude))
        return SOFIA
    monkeypatch.setattr(Olx, "locations", fake_locations)
    monkeypatch.setattr(Olx, "districts", lambda self, city_id: DISTRICTS.get(city_id, []))
    return calls


def test_profile_page_shows_example_profile(logged_in):
    page = logged_in.get("/settings/profile").text
    assert 'value="Иван"' in page  # from context/profile.json.example
    assert "Not set. Adverts can't be submitted without a location." in page


def test_save_details_keeps_unknown_keys(logged_in, user):
    profile = profiles.load(user)
    profile["custom"] = {"keep": True}
    profiles.save(user, profile)

    r = logged_in.post("/settings/profile", data=DETAILS, follow_redirects=False)
    assert r.headers["location"] == "/settings/profile?message=Saved"
    saved = json.loads(user.profile_file.read_text())
    assert saved["contact"] == {"name": "Тест Тестов", "phone": "+359 888 000 000",
                                "email": "test@example.com"}
    assert saved["delivery"] == {"courier": True, "note": "лично или OLX доставка"}
    assert saved["custom"] == {"keep": True}
    assert user.display_name == "Ицо"
    assert ">Ицо</a>" in logged_in.get("/").text


def test_unchecked_courier_is_false(logged_in, user):
    logged_in.post("/settings/profile", data={k: v for k, v in DETAILS.items() if k != "courier"})
    assert profiles.load(user)["delivery"]["courier"] is False


@pytest.mark.parametrize("field, value, error", [
    ("name", " ", "Contact name is required"),
    ("phone", "call me", "Phone should be digits"),
    ("email", "nope", "look right"),
    ("advertiser_type", "agency", "Advertiser type"),
])
def test_invalid_details_change_nothing(logged_in, user, field, value, error):
    before = user.profile_file.read_text()
    r = logged_in.post("/settings/profile", data={**DETAILS, field: value})
    assert r.status_code == 400 and error in r.text
    assert user.profile_file.read_text() == before


def test_location_lookup_and_choice(logged_in, user, located):
    page = logged_in.get("/settings/profile?lat=42.6977&lng=23.3219").text
    assert located == [(42.6977, 23.3219)]
    assert page.count('name="choice"') == 3  # duplicate removed
    assert "София, Зона Б-5-3" in page and "Банкя" in page

    r = logged_in.post("/settings/profile/location", data={"lat": 42.6977, "lng": 23.3219, "choice": 1},
                       follow_redirects=False)
    assert r.status_code == 303
    location = profiles.load(user)["location"]
    assert location == {"city_id": 8771, "city_name": "София", "district_id": 1387,
                        "district_name": "Зона Б-5-3"}  # the example's hint "note" is dropped
    assert "София, Зона Б-5-3" in logged_in.get("/settings/profile").text


def test_location_without_district_and_bad_choice(logged_in, user, located):
    logged_in.post("/settings/profile/location", data={"lat": 1, "lng": 2, "choice": 2})
    assert profiles.load(user)["location"]["district_id"] is None
    r = logged_in.post("/settings/profile/location", data={"lat": 1, "lng": 2, "choice": 7})
    assert r.status_code == 400 and "Pick one of the places" in r.text


def test_city_with_districts_needs_one(logged_in, user, monkeypatch, located):
    monkeypatch.setattr(Olx, "locations",
                        lambda self, latitude, longitude: [{"city": {"id": 8771, "name": "София"}}])
    before = user.profile_file.read_text()
    r = logged_in.post("/settings/profile/location", data={"lat": 1, "lng": 2, "choice": 0})
    assert r.status_code == 400 and "This city has districts" in r.text
    assert user.profile_file.read_text() == before


def test_location_lookup_failure_is_shown(logged_in, monkeypatch):
    def broken(self, latitude, longitude):
        raise OlxError(500, {"error": "boom"})
    monkeypatch.setattr(Olx, "locations", broken)
    page = logged_in.get("/settings/profile?lat=1&lng=2").text
    assert "OLX location lookup failed" in page


def test_change_password(logged_in, settings, user, monkeypatch):
    monkeypatch.setattr("olx_auto.web.app.time.sleep", lambda s: None)
    form = {"current": "wrong", "new": "new password 1", "repeat": "new password 1"}
    assert "Current password is wrong" in logged_in.post("/settings/password", data=form).text
    form["current"] = PASSWORD
    assert "at least 8" in logged_in.post("/settings/password", data={**form, "new": "short",
                                                                       "repeat": "short"}).text
    mismatch = logged_in.post("/settings/password", data={**form, "repeat": "other"})
    assert "passwords don&#39;t match" in mismatch.text
    assert users.authenticate(settings.data_dir, "ico", PASSWORD)

    r = logged_in.post("/settings/password", data=form, follow_redirects=False)
    assert r.headers["location"] == "/settings/profile?message=Password+changed"
    assert users.authenticate(settings.data_dir, "ico", "new password 1")
    assert not users.authenticate(settings.data_dir, "ico", PASSWORD)
    assert logged_in.get("/settings/profile").status_code == 200  # this session stays


def test_password_change_ends_other_sessions(logged_in, user):
    users.set_password(user, "changed elsewhere")  # e.g. `olx-auto set-password` or another device
    r = logged_in.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_profile_requires_login(client):
    for method, path in [("get", "/settings/profile"), ("post", "/settings/profile"),
                         ("post", "/settings/password")]:
        r = getattr(client, method)(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
