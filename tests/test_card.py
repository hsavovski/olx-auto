"""Listing card (edit, lock, refine) and style guide pages. Drafting itself is replaced by recorders."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from olx_auto import drafting, listings, style, users
from olx_auto.web.app import create_app
from tests.conftest import PASSWORD, jpeg_bytes
from tests.test_ai import GOOD_DRAFT, STROLLER_ATTRS


@pytest.fixture
def started(monkeypatch):
    """Background tasks that would call the LLM, recorded instead of run."""
    calls = []
    monkeypatch.setattr(drafting, "generate", lambda s, u, i, compare=False:
                        calls.append(("compare" if compare else "generate", i)))
    monkeypatch.setattr(drafting, "try_learn_style", lambda s, u, i: calls.append(("learn", i)))
    return calls


@pytest.fixture
def client(settings, user):
    c = TestClient(create_app(replace(settings, openrouter_api_key="test-key", show_trace=True)))
    c.post("/login", data={"username": "ico", "password": PASSWORD})
    return c


@pytest.fixture
def drafted(user):
    listing = listings.create(user, "Chicco", [jpeg_bytes()])
    listing.update(draft=dict(GOOD_DRAFT), ai_draft=dict(GOOD_DRAFT),
                   category={"id": 185, "name": "Детски колички", "photos_limit": 12,
                             "attributes": STROLLER_ATTRS},
                   questions=[{"id": 1, "question": "На колко години е?", "answer": None}])
    listings.save(user, listing)
    return listing


def test_card_shows_editable_draft(client, drafted):
    page = client.get(f"/listings/{drafted['id']}").text
    assert f'value="{GOOD_DRAFT["title"]}"' in page
    assert GOOD_DRAFT["description"] in page
    assert "Детски колички" in page
    assert '<option value="used" selected>Използвано</option>' in page
    assert 'name="attr_delivery_paid_by" value="207"\n          checked' in page
    assert 'value="60"' in page and GOOD_DRAFT["price_reasoning"] in page
    assert "На колко години е?" in page and "Add information or ask for changes" in page
    assert "🔒" not in page


def test_edit_locks_fields_and_learns_from_text(client, user, drafted, started):
    form = {"title": "Количка Chicco, леко използвана", "description": GOOD_DRAFT["description"],
            "price": "55", "negotiable": "on", "attr_state": "used",
            "attr_delivery_paid_by": ["207", "209"]}
    r = client.post(f"/listings/{drafted['id']}/edit", data=form, follow_redirects=False)
    assert r.status_code == 303

    listing = listings.load(user, drafted["id"])
    assert listing["draft"]["title"] == "Количка Chicco, леко използвана"
    assert listing["draft"]["price"] == 55
    assert listing["locked"] == ["title", "price"]
    assert [e["field"] for e in listing["edits"]] == ["title", "price"]
    assert listing["progress"] == "Starting…"
    assert started == [("learn", drafted["id"])]
    assert "🔒 locked" in client.get(f"/listings/{drafted['id']}").text


def test_attribute_edit_locks_without_learning(client, user, drafted, started):
    form = {"title": GOOD_DRAFT["title"], "description": GOOD_DRAFT["description"], "price": "60",
            "negotiable": "on", "attr_state": "new"}
    client.post(f"/listings/{drafted['id']}/edit", data=form)
    listing = listings.load(user, drafted["id"])
    assert listing["draft"]["attributes"] == {"state": "new"}
    assert listing["locked"] == ["attributes"]
    assert started == []


def test_unlock(client, user, drafted):
    drafted["locked"] = ["title", "price"]
    listings.save(user, drafted)
    client.post(f"/listings/{drafted['id']}/unlock/price")
    assert listings.load(user, drafted["id"])["locked"] == ["title"]


def test_refine_saves_answers_and_message_then_redrafts(client, user, drafted, started):
    client.post(f"/listings/{drafted['id']}/refine",
                data={"answer_1": "2 години", "message": "Има и дъждобран. Направи го по-кратко."})
    listing = listings.load(user, drafted["id"])
    assert listing["questions"][0]["answer"] == "2 години"
    assert [m["text"] for m in listing["messages"]] == ["Има и дъждобран. Направи го по-кратко."]
    assert started == [("generate", drafted["id"])]
    page = client.get(f"/listings/{drafted['id']}").text
    assert "Има и дъждобран" in page and 'http-equiv="refresh"' in page


def test_delete_photo_keeps_the_last_one(client, user):
    listing = listings.create(user, "", [jpeg_bytes(), jpeg_bytes()])
    first = listings.photo_path(user, listing["id"], "01.jpg")
    assert client.get(f"/listings/{listing['id']}").text.count("/delete") == 2

    r = client.post(f"/listings/{listing['id']}/photos/01.jpg/delete", follow_redirects=False)
    assert r.status_code == 303
    assert listings.load(user, listing["id"])["photos"] == ["02.jpg"] and not first.exists()
    assert "/delete" not in client.get(f"/listings/{listing['id']}").text

    client.post(f"/listings/{listing['id']}/photos/02.jpg/delete")
    assert listings.load(user, listing["id"])["photos"] == ["02.jpg"]
    for name in ("01.jpg", "..%2Flisting.json"):
        assert client.post(f"/listings/{listing['id']}/photos/{name}/delete").status_code == 404


def test_running_listing_cannot_be_changed(client, user, drafted, started):
    drafted["progress"] = "Choosing category…"
    listings.save(user, drafted)
    client.post(f"/listings/{drafted['id']}/refine", data={"message": "x"})
    client.post(f"/listings/{drafted['id']}/edit", data={"title": "Друго заглавие за количка"})
    client.post(f"/listings/{drafted['id']}/photos/01.jpg/delete")
    listing = listings.load(user, drafted["id"])
    assert listing["messages"] == [] and listing["draft"]["title"] == GOOD_DRAFT["title"]
    assert listing["photos"] == ["01.jpg"]
    assert started == []


def test_other_users_listing_is_404(client, settings, started):
    other = users.create_user(settings.data_dir, "other", PASSWORD)
    theirs = listings.create(other, "", [jpeg_bytes(), jpeg_bytes()])
    for path in ("edit", "refine", "compare", "unlock/title", "photos/01.jpg/delete"):
        assert client.post(f"/listings/{theirs['id']}/{path}", data={"message": "x"}).status_code == 404
    listing = listings.load(other, theirs["id"])
    assert listing["messages"] == [] and listing["photos"] == ["01.jpg", "02.jpg"]


def test_trace_panel(client, user, drafted):
    listings.trace_path(user, drafted["id"]).write_text(
        '{"runs": [{"stage": "agent", "model": "m", "at": "t", "stopped": "submitted", "messages": ['
        '{"role": "system", "content": "SYSTEM"}, {"role": "user", "content": "context"},'
        '{"role": "assistant", "content": "", "tool_calls": [{"function": '
        '{"name": "suggest_category", "arguments": "{}"}}]}]}]}')
    page = client.get(f"/listings/{drafted['id']}").text
    assert "How the agent got here" in page and "suggest_category" in page and "SYSTEM" not in page


def test_style_page_edit_and_undo(client, user):
    original = style.read_guide(user)
    client.post("/settings/style", data={"guide": "# Моят стил\n- Кратко."})
    page = client.get("/settings/style").text
    assert "Моят стил" in page and "edited by hand" in page
    backup = style.log(user)[0]["backup"]
    client.post(f"/settings/style/revert/{backup}")
    assert style.read_guide(user) == original
    assert client.post("/settings/style/revert/..%2Fuser.json").status_code == 404
