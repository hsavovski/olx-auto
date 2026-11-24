import json

import pytest

from olx_auto import listings, users
from olx_auto.olx import Olx
from tests.conftest import PASSWORD, jpeg_bytes


@pytest.mark.parametrize("path", ["/", "/listings/new", "/settings/olx"])
def test_pages_require_login(client, path):
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_wrong_password(client, user, monkeypatch):
    monkeypatch.setattr("olx_auto.web.app.time.sleep", lambda s: None)
    r = client.post("/login", data={"username": "ico", "password": "nope"})
    assert r.status_code == 401
    assert "Wrong user name or password" in r.text


def test_login_locks_a_name_after_repeated_wrong_passwords(client, user, monkeypatch):
    monkeypatch.setattr("olx_auto.web.app.time.sleep", lambda s: None)
    for _ in range(10):
        assert client.post("/login", data={"username": "ico", "password": "nope"}).status_code == 401
    r = client.post("/login", data={"username": "ico", "password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 429 and "Too many wrong passwords" in r.text


def test_login_throttle_forgets_old_failures_and_resets_on_success():
    now = [0.0]
    throttle = users.LoginThrottle(max_failures=2, window=60, clock=lambda: now[0])
    throttle.failed("ico")
    throttle.failed("ico")
    assert throttle.locked("ico") and not throttle.locked("other")
    now[0] = 61
    assert not throttle.locked("ico")
    throttle.failed("ico")
    throttle.succeeded("ico")
    throttle.failed("ico")
    assert not throttle.locked("ico")


def test_home_shows_not_connected_banner(logged_in):
    r = logged_in.get("/")
    assert r.status_code == 200
    assert "OLX account not connected" in r.text


def test_logout(logged_in):
    logged_in.post("/logout")
    assert logged_in.get("/", follow_redirects=False).status_code == 303


def test_upload_creates_listing(logged_in, user):
    files = [("photos", ("a.jpg", jpeg_bytes(), "image/jpeg")),
             ("photos", ("b.jpg", jpeg_bytes(), "image/jpeg"))]
    r = logged_in.post("/listings", files=files, data={"note": "Nikon"}, follow_redirects=False)
    assert r.status_code == 303
    listing_id = r.headers["location"].rsplit("/", 1)[1]
    assert listings.load(user, listing_id)["photos"] == ["01.jpg", "02.jpg"]

    page = logged_in.get(f"/listings/{listing_id}")
    assert "Nikon" in page.text
    photo = logged_in.get(f"/listings/{listing_id}/photos/01.jpg")
    assert photo.headers["content-type"] == "image/jpeg"


def test_upload_rejects_non_image(logged_in):
    files = [("photos", ("a.txt", b"hello", "text/plain"))]
    r = logged_in.post("/listings", files=files)
    assert r.status_code == 400
    assert "Not a supported image" in r.text


def test_users_cannot_see_each_others_listings(logged_in, settings, user):
    other = users.create_user(settings.data_dir, "other", PASSWORD)
    theirs = listings.create(other, "secret", [jpeg_bytes()])
    assert logged_in.get(f"/listings/{theirs['id']}").status_code == 404
    assert logged_in.get(f"/listings/{theirs['id']}/photos/01.jpg").status_code == 404


def test_olx_connect_redirects_to_authorize_with_state(logged_in):
    r = logged_in.post("/settings/olx/connect", follow_redirects=False)
    assert r.headers["location"].startswith("https://www.olx.bg/oauth/authorize?")
    assert "state=" in r.headers["location"]


def test_olx_callback_rejects_bad_state(logged_in, monkeypatch):
    called = []
    monkeypatch.setattr(Olx, "exchange_code", lambda self, code: called.append(code))
    logged_in.post("/settings/olx/connect", follow_redirects=False)
    r = logged_in.get("/olx/callback?code=abc&state=forged", follow_redirects=False)
    assert "Invalid" in r.headers["location"]
    assert called == []


def test_olx_paste_code_saves_token_per_user(logged_in, user, monkeypatch):
    def fake_token_request(self, **fields):
        assert fields == {"grant_type": "authorization_code", "code": "abc", "scope": "v2 read write"}
        return {"access_token": "at", "refresh_token": "rt", "expires_in": 86400}

    monkeypatch.setattr(Olx, "_token_request", fake_token_request)
    monkeypatch.setattr(Olx, "me", lambda self: {"id": 7, "name": "Test Account"})
    r = logged_in.post("/settings/olx/code", data={"code": "https://example.com/cb?code=abc&state=x"})
    assert "Connected as" in r.text and "Test Account" in r.text
    assert json.loads(user.token_file.read_text())["user"]["refresh_token"] == "rt"
    assert oct(user.token_file.stat().st_mode & 0o777) == "0o600"
