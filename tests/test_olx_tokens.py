"""Token file handling: refresh tokens rotate, so every write must keep the newest one."""
import json
import time

from olx_auto.olx import Olx

EXPIRED = {"access_token": "old", "refresh_token": "r1", "expires_at": 0}


def _client(path):
    return Olx(path, "id", "secret")


def test_client_token_save_keeps_a_user_token_rotated_by_another_instance(tmp_path, monkeypatch):
    path = tmp_path / "olx_token.json"
    path.write_text(json.dumps({"user": EXPIRED}))
    stale, fresh = _client(path), _client(path)

    def fake_token_request(self, **fields):
        if fields["grant_type"] == "refresh_token":
            return {"access_token": "u2", "refresh_token": "r2", "expires_in": 3600}
        return {"access_token": "c1", "expires_in": 3600}

    monkeypatch.setattr(Olx, "_token_request", fake_token_request)
    assert fresh.user_token() == "u2"
    assert stale.client_token() == "c1"  # `stale` still holds r1 in memory
    saved = json.loads(path.read_text())
    assert saved["user"]["refresh_token"] == "r2"
    assert saved["client"]["access_token"] == "c1"


def test_user_token_uses_a_refresh_done_by_another_instance(tmp_path, monkeypatch):
    path = tmp_path / "olx_token.json"
    path.write_text(json.dumps({"user": EXPIRED}))
    stale, fresh = _client(path), _client(path)
    used = []

    def fake_token_request(self, **fields):
        used.append(fields["refresh_token"])
        return {"access_token": "u2", "refresh_token": "r2", "expires_in": 3600}

    monkeypatch.setattr(Olx, "_token_request", fake_token_request)
    assert fresh.user_token() == "u2"
    assert stale.user_token() == "u2"  # reads r2 from disk instead of refreshing with the dead r1
    assert used == ["r1"]


def test_token_file_is_replaced_atomically_and_private(tmp_path, monkeypatch):
    path = tmp_path / "olx_token.json"
    monkeypatch.setattr(Olx, "_token_request",
                        lambda self, **f: {"access_token": "c1", "expires_in": 3600})
    _client(path).client_token()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert [p.name for p in tmp_path.iterdir()] == ["olx_token.json"]
    assert json.loads(path.read_text())["client"]["expires_at"] > time.time()
