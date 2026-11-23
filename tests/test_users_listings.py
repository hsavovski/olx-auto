import json

import pytest

from olx_auto import listings, users
from tests.conftest import PASSWORD, jpeg_bytes


def test_create_user_layout(user):
    assert user.profile_file.exists()
    assert user.style_guide_file.exists()
    assert user.listings_dir.is_dir()
    assert PASSWORD not in user.meta_file.read_text()


def test_authenticate(settings, user):
    assert users.authenticate(settings.data_dir, "ico", PASSWORD) == user
    assert users.authenticate(settings.data_dir, "ico", "wrong") is None
    assert users.authenticate(settings.data_dir, "nobody", PASSWORD) is None


@pytest.mark.parametrize("name", ["", "Ico", "../etc", "a/b", "x" * 40])
def test_rejects_bad_user_names(settings, name):
    with pytest.raises(ValueError):
        users.create_user(settings.data_dir, name, PASSWORD)
    assert users.get_user(settings.data_dir, name) is None


def test_duplicate_user(settings, user):
    with pytest.raises(ValueError):
        users.create_user(settings.data_dir, "ico", PASSWORD)


def test_listing_roundtrip(user):
    listing = listings.create(user, "  stara kamera  ", [jpeg_bytes(), jpeg_bytes()])
    assert listing["note"] == "stara kamera"
    assert listing["photos"] == ["01.jpg", "02.jpg"]
    assert listings.load(user, listing["id"])["status"] == "draft"
    assert [item["id"] for item in listings.list_all(user)] == [listing["id"]]
    assert listings.photo_path(user, listing["id"], "01.jpg").exists()


def test_listing_rejects_bad_ids_and_names(user):
    listing = listings.create(user, "", [jpeg_bytes()])
    for bad_id in ["../ico", "x", "20261123-000000-zzzz"]:
        with pytest.raises(KeyError):
            listings.load(user, bad_id)
    for bad_name in ["../listing.json", "1.jpg", "99.jpg"]:
        with pytest.raises(KeyError):
            listings.photo_path(user, listing["id"], bad_name)


def test_listing_invalid_photo_leaves_no_folder(user):
    with pytest.raises(ValueError):
        listings.create(user, "", [jpeg_bytes(), b"junk"])
    assert listings.list_all(user) == []
    assert list(user.listings_dir.iterdir()) == []


def test_listing_needs_photos(user):
    with pytest.raises(ValueError):
        listings.create(user, "note", [])


def test_listing_json_is_utf8(user):
    listing = listings.create(user, "Фотоапарат", [jpeg_bytes()])
    raw = (user.listings_dir / listing["id"] / "listing.json").read_text()
    assert "Фотоапарат" in raw
    assert json.loads(raw)["cost"] == {"vision": 0.0, "agent": 0.0}
