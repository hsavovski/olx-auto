"""OLX comparables: the search-page scraper (pages are synthetic, nothing is fetched) and the agent tool."""
import json
import urllib.error

import pytest

from olx_auto import comparables, drafting, listings
from olx_auto.comparables import ComparablesError, Search
from tests.conftest import jpeg_bytes
from tests.test_ai import (
    GOOD_DRAFT,
    OBSERVATIONS,
    FakeCatalog,
    FakeLLM,
    run,
    text_reply,
    tool_reply,
    tool_results,
)
from tests.test_card import client, drafted, started  # noqa: F401 (fixtures)

CATEGORIES = {"185": {"id": 185, "path": "za-bebeto-i-deteto/bebeshki-i-detski-kolichki"},
              "139": {"id": 139, "path": "za-bebeto-i-deteto"}}


def ad(id, title, price, *, currency="EUR", state="used", promoted=False, url=None):
    return {"id": id, "title": title, "isPromoted": promoted, "isBusiness": False,
            "url": url or f"https://www.olx.bg/d/ad/test-ID{id}.html",
            "createdTime": "2026-11-01T10:00:00+02:00", "location": {"cityName": "София"},
            "price": {"displayValue": f"{price} €", "regularPrice": {
                "value": price, "currencyCode": currency, "negotiable": True}} if price else {},
            "params": [{"key": "state", "normalizedValue": state}],
            "user": {"name": "Иван"}}


ADS = [ad(1, "Количка Chicco Liteway", 45), ad(2, "Chicco Trio", 120, promoted=True),
       ad(3, "Чанта за количка Chicco", 10), ad(4, "Chicco Bravo", 80),
       ad(5, "Chicco в лева", 150, currency="BGN"), ad(6, "Chicco за подарък", None),
       ad(7, "Chicco", 60, url="javascript:alert(1)")]


def page(ads=ADS, categories=CATEGORIES, total=None):
    state = {"listing": {"listing": {"totalElements": total or len(ads), "ads": ads}},
             "categories": {"list": categories}}
    return f"<html><script>window.__PRERENDERED_STATE__ = {json.dumps(json.dumps(state))};</script></html>"


class FakeGet:
    def __init__(self, *pages):
        self.pages, self.urls = list(pages), []

    def __call__(self, url):
        self.urls.append(url)
        return self.pages.pop(0)


def test_search_url():
    assert comparables.search_url("Chicco  Liteway 3!") == "https://www.olx.bg/ads/q-chicco-liteway-3/"
    url = comparables.search_url("ски обувки", "sport/zimni",
                                 {"state": "used", "delivery_paid_by": ["207", "209"]})
    assert url.startswith("https://www.olx.bg/sport/zimni/q-%D1%81%D0%BA%D0%B8-%D0%BE%D0%B1%D1%83%D0%B2%D0%BA%D0%B8/?")
    assert "search%5Bfilter_enum_state%5D%5B0%5D=used" in url
    assert "search%5Bfilter_enum_delivery_paid_by%5D%5B1%5D=209" in url
    with pytest.raises(ComparablesError, match="empty"):
        comparables.search_url(" !! ")
    with pytest.raises(ComparablesError, match="bad filter"):
        comparables.search_url("chicco", filters={"state][0]=x&a": "used"})


def test_search_parses_prices_and_caches_category_paths(tmp_path):
    get = FakeGet(page(ads=[]), page(total=51), page())  # /ads/ for the category paths, then searches
    search = Search(tmp_path / "paths.json", get)
    result = search.search("chicco количка", 185, {"state": "used"})

    assert get.urls[0] == "https://www.olx.bg/ads/"
    assert get.urls[1].startswith("https://www.olx.bg/za-bebeto-i-deteto/bebeshki-i-detski-kolichki/q-chicco-")
    assert result["total"] == 51 and len(result["items"]) == 7
    first, promoted = result["items"][:2]
    assert first == {"title": "Количка Chicco Liteway", "price": 45, "negotiable": True, "condition": "used",
                     "url": "https://www.olx.bg/d/ad/test-ID1.html", "promoted": False, "business": False,
                     "city": "София", "created": "2026-11-01"}
    assert promoted["promoted"] is True
    assert result["items"][4]["price"] is None  # BGN isn't mixed in
    assert result["items"][6]["url"] is None  # only olx.bg links
    assert "Иван" not in json.dumps(result, ensure_ascii=False)  # sellers' details aren't kept
    assert result["summary"] == {"n": 5, "median": 60, "low": 45, "high": 80, "min": 10, "max": 120}

    search.search("chicco", 185)  # paths come from the cache now
    assert len(get.urls) == 3


def test_unknown_category_and_changed_page(tmp_path):
    search = Search(tmp_path / "paths.json", FakeGet(page(ads=[]), page(ads=[])))
    with pytest.raises(ComparablesError, match="no search page for category 1"):
        search.search("x", 1)
    with pytest.raises(ComparablesError, match="no embedded results"):
        Search(tmp_path / "p.json", FakeGet("<html>captcha</html>")).search("chicco")
    with pytest.raises(ComparablesError, match="no adverts"):
        Search(tmp_path / "p.json", FakeGet(page().replace("listing", "lst"))).search("chicco")


def test_http_errors_become_comparables_errors(monkeypatch):
    def blocked(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)
    monkeypatch.setattr(comparables.urllib.request, "urlopen", blocked)
    with pytest.raises(ComparablesError, match="HTTP 403"):
        comparables.fetch("https://www.olx.bg/ads/")


def test_summary_edges():
    assert comparables.summary([{"price": None}]) == {"n": 0}
    assert comparables.summary([{"price": 30}]) == {"n": 1, "median": 30, "low": 30, "high": 30,
                                                     "min": 30, "max": 30}


# ---- agent tool ------------------------------------------------------------

class FakeSearch:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def search(self, query, category_id=None, filters=None):
        self.calls.append((query, category_id, filters))
        if self.fail:
            raise ComparablesError("olx.bg search answered HTTP 403")
        return {"query": query, "category_id": category_id, "filters": filters or {},
                "url": "https://www.olx.bg/ads/q-chicco/", "total": 7,
                "summary": comparables.summary([comparables._item(a) for a in ADS]),
                "items": [comparables._item(a) for a in ADS], "at": "2026-11-23T10:00:00+00:00"}


def test_agent_searches_comparables_at_most_three_times():
    search = FakeSearch()
    q = ("search_olx_comparables", {"query": "chicco", "category_id": 185, "filters": {"state": "used"}})
    llm = FakeLLM(tool_reply(q, q, q, q), tool_reply(("submit_draft", GOOD_DRAFT)))
    result = run(llm, comparables=search)

    assert len(search.calls) == 3 and search.calls[0] == ("chicco", 185, {"state": "used"})
    first, *_, fourth = tool_results(llm.requests[1])
    assert first["median"] == 60 and first["total_matches"] == 7
    assert first["adverts"][0] == {"title": "Количка Chicco Liteway", "price": 45, "condition": "used",
                                   "negotiable": True, "created": "2026-11-01"}
    assert "url" not in first["adverts"][0]  # links are for the card, not the model
    assert "at most 3" in fourth["error"]
    assert result.comparables["query"] == "chicco"


def test_agent_prices_without_comparables_when_search_fails():
    llm = FakeLLM(tool_reply(("search_olx_comparables", {"query": "chicco"})),
                  tool_reply(("submit_draft", GOOD_DRAFT)))
    result = run(llm, comparables=FakeSearch(fail=True))
    assert tool_results(llm.requests[1]) == [{"error": "olx.bg search answered HTTP 403"}]
    assert result.stopped == "submitted" and result.comparables is None


def test_generate_keeps_comparables_on_the_listing(settings, user, client, drafted):  # noqa: F811
    from dataclasses import replace
    ai_settings = replace(settings, openrouter_api_key="test-key")
    listing = listings.create(user, "Chicco", [jpeg_bytes()])
    llm = FakeLLM(text_reply(json.dumps(OBSERVATIONS)),
                  tool_reply(("search_olx_comparables", {"query": "chicco", "category_id": 185}),
                             ("submit_draft", GOOD_DRAFT)))
    drafting.generate(ai_settings, user, listing["id"], llm=llm, catalog=FakeCatalog(), compare=True,
                      search=FakeSearch())
    saved = listings.load(user, listing["id"])
    assert saved["comparables"]["summary"]["median"] == 60

    page_html = client.get(f"/listings/{listing['id']}").text
    assert "median <strong>€60</strong>" in page_html and "https://www.olx.bg/d/ad/test-ID1.html" in page_html
    assert "javascript:" not in page_html

    search = FakeSearch()
    llm = FakeLLM(tool_reply(("submit_draft", GOOD_DRAFT)))  # an update without Compare
    drafting.generate(ai_settings, user, listing["id"], llm=llm, catalog=FakeCatalog(), search=search)
    assert listings.load(user, listing["id"])["comparables"]["summary"]["median"] == 60
    request = llm.requests[0]
    assert "search_olx_comparables" not in [t["function"]["name"] for t in request["tools"]]
    assert search.calls == []
    context = request["messages"][1]["content"]
    assert 'Comparable adverts on olx.bg (search "chicco"' in context and '"median": 60' in context
    assert "test-ID1" not in context  # no links for the model


def test_compare_is_only_offered_on_demand():
    llm = FakeLLM(tool_reply(("submit_draft", GOOD_DRAFT)))
    run(llm)
    assert "search_olx_comparables" not in [t["function"]["name"] for t in llm.requests[0]["tools"]]
    assert "search_olx_comparables" not in llm.requests[0]["messages"][0]["content"]

    llm = FakeLLM(tool_reply(("submit_draft", GOOD_DRAFT)))
    run(llm, comparables=FakeSearch())
    assert "search_olx_comparables" in [t["function"]["name"] for t in llm.requests[0]["tools"]]
    assert "asked to compare prices" in llm.requests[0]["messages"][0]["content"]


def test_compare_button_starts_a_comparing_run(client, drafted, started):  # noqa: F811
    assert "Compare prices on OLX" in client.get(f"/listings/{drafted['id']}").text
    r = client.post(f"/listings/{drafted['id']}/compare", follow_redirects=False)
    assert r.status_code == 303 and started == [("compare", drafted["id"])]
