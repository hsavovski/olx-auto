import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from olx_auto import users
from olx_auto.config import Settings
from olx_auto.web.app import create_app

PASSWORD = "correct horse"


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, session_secret="test-secret",
                    olx_client_id="cid", olx_client_secret="csecret")


@pytest.fixture
def user(settings):
    return users.create_user(settings.data_dir, "ico", PASSWORD)


@pytest.fixture
def client(settings):
    return TestClient(create_app(settings))


@pytest.fixture
def logged_in(client, user):
    r = client.post("/login", data={"username": "ico", "password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 303
    return client


def jpeg_bytes(size=(64, 48), color="red", **save_kwargs) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "JPEG", **save_kwargs)
    return out.getvalue()
