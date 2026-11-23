"""Submit to OLX (submitting.py, POST /listings/{id}/submit). S3 and OLX are faked: all offline."""
from datetime import UTC, datetime, timedelta

import pytest

from olx_auto import listings, profiles, s3, submitting
from olx_auto.olx import Olx, OlxError
from tests.conftest import jpeg_bytes
from tests.test_card import client, drafted  # noqa: F401 (fixtures)
from tests.test_submit import PROFILE

CREATED = {"id": 900001, "status": "disabled", "url": "https://www.olx.bg/d/ad/test-ID900001.html",
           "images": [{"url": "https://frankfurt.apollo.olxcdn.com/v1/files/abc-BG/image"}]}


class FakeStore:
    def __init__(self):
        self.objects, self.deleted = {}, []

    def put(self, key, path):
        self.objects[key] = path.read_bytes()
        return f"https://bucket.s3.example/{key}?X-Amz-Signature=x"

    def delete(self, key):
        self.deleted.append(key)


@pytest.fixture
def store(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(s3, "PhotoStore", lambda settings: fake)
    return fake


@pytest.fixture
def ready(user, drafted):  # noqa: F811
    """A drafted listing and a complete profile: nothing left to fix."""
    profile = profiles.load(user)
    profile.update(PROFILE)
    profiles.save(user, profile)
    return drafted


@pytest.fixture
def olx(monkeypatch):
    calls = []

    def create(self, **advert):
        calls.append(("create", advert))
        return dict(CREATED)
    monkeypatch.setattr(Olx, "create_advert", create)
    return calls


def test_submit_posts_and_cleans_up_s3(client, user, ready, store, olx):  # noqa: F811
    r = client.post(f"/listings/{ready['id']}/submit", follow_redirects=False)
    assert r.status_code == 303

    (_, advert), = olx
    assert advert["title"] == ready["draft"]["title"] and advert["contact"]["name"] == "Тест Тестов"
    key, = store.objects
    assert key.startswith(f"{user.name}/{ready['id']}/") and key.endswith("-01.jpg")
    assert advert["images"] == [{"url": f"https://bucket.s3.example/{key}?X-Amz-Signature=x"}]
    assert store.deleted == [key]

    listing = listings.load(user, ready["id"])
    assert listing["status"] == "posted" and listing["error"] is None
    assert listing["olx"]["id"] == 900001 and listing["olx"]["status"] == "disabled"
    assert listing["olx"]["images"] == [CREATED["images"][0]["url"]]

    page = client.get(f"/listings/{ready['id']}").text
    assert "On OLX: pending moderation" in page and CREATED["url"] in page
    assert "Submit to OLX</button>" not in page
    assert "pending moderation" in client.get("/").text


def test_posted_listing_is_frozen(client, user, ready, store, olx):  # noqa: F811
    client.post(f"/listings/{ready['id']}/submit")
    client.post(f"/listings/{ready['id']}/submit")
    client.post(f"/listings/{ready['id']}/edit", data={"title": "Друго заглавие за количка"})
    assert len(olx) == 1
    assert listings.load(user, ready["id"])["draft"]["title"] == ready["draft"]["title"]


def test_olx_refusal_is_mapped_to_card_fields(client, user, ready, store, monkeypatch):  # noqa: F811
    def refuse(self, **advert):
        raise OlxError(400, {"error": {"status": 400, "title": "Невалидни данни", "validation": [
            {"field": "params.price[0]", "title": "Въведи цена!", "detail": "price"},
            {"field": "params.state", "title": "Невалидна стойност", "detail": "state"},
            {"field": "contact.name", "title": "Въведи име", "detail": "name"}]}})
    monkeypatch.setattr(Olx, "create_advert", refuse)
    client.post(f"/listings/{ready['id']}/submit")

    listing = listings.load(user, ready["id"])
    assert listing["status"] == "failed"
    assert listing["error"]["message"] == "OLX refused the advert: Невалидни данни"
    assert [f["field"] for f in listing["error"]["fields"]] == \
        ["price", "attributes.state", "profile (contact.name)"]
    assert store.deleted == list(store.objects)

    page = client.get(f"/listings/{ready['id']}").text
    assert "price: Въведи цена!" in page and "Submit to OLX</button>" in page


def test_timeout_after_sending_warns_about_duplicates(client, user, ready, store, monkeypatch):  # noqa: F811
    def timeout(self, **advert):
        raise TimeoutError("timed out")
    monkeypatch.setattr(Olx, "create_advert", timeout)
    client.post(f"/listings/{ready['id']}/submit")
    listing = listings.load(user, ready["id"])
    assert listing["status"] == "failed" and "check your adverts on OLX" in listing["error"]["message"]


def test_missing_s3_config_fails_before_olx(client, user, ready, olx):  # noqa: F811
    client.post(f"/listings/{ready['id']}/submit")
    listing = listings.load(user, ready["id"])
    assert listing["status"] == "failed" and "S3 isn't configured" in listing["error"]["message"]
    assert olx == []


def test_listing_with_problems_is_not_submitted(client, user, drafted, store, olx):  # noqa: F811
    client.post(f"/listings/{drafted['id']}/submit")  # the example profile has no location
    assert listings.load(user, drafted["id"])["status"] == "draft"
    assert olx == [] and store.objects == {}
    assert 'type="submit" disabled>Submit to OLX' in client.get(f"/listings/{drafted['id']}").text


def test_cut_off_submit_can_be_retried():
    old = (datetime.now(UTC) - timedelta(minutes=6)).isoformat()
    assert submitting.can_submit({"status": "submitting", "updated_at": old})
    assert not submitting.frozen({"status": "submitting", "updated_at": old})
    assert not submitting.can_submit({"status": "submitting", "updated_at": datetime.now(UTC).isoformat()})
    assert not submitting.can_submit({"status": "posted", "updated_at": old})


def posted(user, listing, checked_at):
    listing.update(status="posted", olx={"id": 900001, "url": CREATED["url"], "status": "new",
                                         "images": [], "checked_at": checked_at})
    listings.save(user, listing)
    return listing


def test_status_refresh_is_throttled(settings, user, drafted, monkeypatch):  # noqa: F811
    seen = []
    monkeypatch.setattr(Olx, "advert", lambda self, i: seen.append(i) or {"id": i, "status": "limited"})
    listing = posted(user, drafted, datetime.now(UTC).isoformat())
    submitting.refresh_status(settings, user, listing)
    assert seen == []

    listing = posted(user, drafted, (datetime.now(UTC) - timedelta(minutes=2)).isoformat())
    submitting.refresh_status(settings, user, listing)
    assert seen == [900001] and listings.load(user, drafted["id"])["olx"]["status"] == "limited"


def test_home_page_refreshes_posted_adverts(client, user, drafted, monkeypatch):  # noqa: F811
    seen = []
    monkeypatch.setattr(Olx, "advert", lambda self, i: seen.append(i) or {"id": i, "status": "active"})
    listings.create(user, "", [jpeg_bytes()])  # a draft: not checked
    posted(user, drafted, None)

    assert "live" in client.get("/").text
    assert seen == [900001] and listings.load(user, drafted["id"])["olx"]["status"] == "active"
    client.get("/")
    assert seen == [900001]  # checked a moment ago


def test_status_refresh_marks_deleted_adverts(settings, user, drafted, monkeypatch):  # noqa: F811
    def gone(self, advert_id):
        raise OlxError(404, {"error": {"title": "Not found"}})
    monkeypatch.setattr(Olx, "advert", gone)
    listing = posted(user, drafted, None)
    submitting.refresh_status(settings, user, listing)
    assert listings.load(user, drafted["id"])["olx"]["status"] == "deleted"


@pytest.mark.parametrize("statuses, commands", [
    (["active", "active", "removed_by_user"], ["deactivate", "sleep", "delete"]),  # waits for OLX
    (["new"], ["delete"]),
])
def test_remove_deactivates_active_then_deletes(settings, user, drafted, monkeypatch,  # noqa: F811
                                                statuses, commands):
    calls, statuses = [], iter(statuses)
    monkeypatch.setattr(Olx, "advert", lambda self, i: {"id": i, "status": next(statuses)})
    monkeypatch.setattr(Olx, "advert_command", lambda self, i, c: calls.append(c))
    monkeypatch.setattr(Olx, "delete_advert", lambda self, i: calls.append("delete"))
    monkeypatch.setattr(submitting.time, "sleep", lambda s: calls.append("sleep"))
    posted(user, drafted, None)

    listing = submitting.remove(settings, user, drafted["id"])
    assert calls == commands
    assert listing["status"] == "draft" and listing["olx"]["status"] == "deleted"


def test_remove_accepts_an_advert_already_gone(settings, user, drafted, monkeypatch):  # noqa: F811
    def gone(self, advert_id):
        raise OlxError(404, {"error": {"title": "Not found"}})
    monkeypatch.setattr(Olx, "advert", gone)
    posted(user, drafted, None)
    assert submitting.remove(settings, user, drafted["id"])["olx"]["status"] == "deleted"
