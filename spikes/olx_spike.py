"""Spike: read-only probe of the OLX.bg partner API (spec §12, step 1). Findings: docs/spike-notes.md.

Never creates an advert: validation probes always omit location.city_id, which is checked in the
first validation stage. Raw responses go to spikes/out/ (gitignored: contains account data).

    uv run python spikes/olx_spike.py [token_file]     # default: .token.test.json
"""
import collections
import json
import sys
from pathlib import Path

from olx_auto.config import Settings
from olx_auto.olx import Olx, OlxError

OUT = Path(__file__).parent / "out"
SAMPLE_LEAVES = [454, 185, 1679, 414]  # iPhone, baby strollers, tyres, tools
SUGGEST_QUERIES = ["iphone 13", "детска количка", "дрелка bosch", "зимни гуми"]
DESC = "Описание на продукта, достатъчно дълго за валидация на OLX."


def dump(name, fn):
    try:
        r = fn()
    except OlxError as e:
        r = {"error": e.status, "body": e.body}
    (OUT / f"{name}.json").write_text(json.dumps(r, ensure_ascii=False, indent=1))
    return r


def probe(olx, name, **advert):
    assert "location" not in advert, "probes must stay invalid"
    try:
        olx.create_advert(**advert)
    except OlxError as e:
        (OUT / f"validate_{name}.json").write_text(json.dumps(e.body, ensure_ascii=False, indent=1))
        return [(v["field"], v["detail"]) for v in e.body["error"]["validation"]]
    raise RuntimeError(f"probe {name} created an advert: delete it on the test account")


def main():
    token_file = Path(sys.argv[1] if len(sys.argv) > 1 else ".token.test.json")
    s = Settings.load()
    olx = Olx(token_file, s.olx_client_id, s.olx_client_secret)
    OUT.mkdir(exist_ok=True)

    print("currencies:", dump("currencies", olx.currencies))

    cats = {c["id"]: c for c in dump("categories", olx.categories)}
    leaves = [c for c in cats.values() if c["is_leaf"]]
    print(f"categories: {len(cats)} total, {len(leaves)} leaves")
    print("photos_limit:", collections.Counter(c["photos_limit"] for c in leaves))

    for q in SUGGEST_QUERIES:
        r = dump("suggest_" + q.replace(" ", "_"), lambda q=q: olx.suggest_category(q))
        print(f"suggest {q!r}:", [(x["id"], x["name"], cats[int(x["id"])]["is_leaf"]) for x in r])

    for cid in SAMPLE_LEAVES:
        attrs = dump(f"attrs_{cid}", lambda cid=cid: olx.category_attributes(cid))
        print(f"attrs {cid} {cats[cid]['name']}:",
              [(a["code"], "req" if a["validation"]["required"] else "opt") for a in attrs])

    dump("regions", olx.regions)
    dump("locations_sofia", lambda: olx.get("/locations", user=False,
                                            params={"latitude": 42.6977, "longitude": 23.3219})["data"])
    for path in ["/users/me", "/users/me/account-balance", "/users/me/packets",
                 "/users/me/payment-methods", "/paid-features", "/adverts"]:
        dump("ep" + path.replace("/", "_"), lambda path=path: olx.get(path)["data"])

    print("validate minimal:", probe(olx, "minimal", title="abc", description="short"))
    print("validate long:", probe(olx, "long", title="Тест " * 40, description="Описание " * 1200))
    print("validate no city:", probe(olx, "no_city", title="а", description=DESC, category_id=454,
                                     advertiser_type="private", contact={"name": "Тест"}))
    print("adverts after probes:", olx.adverts())


if __name__ == "__main__":
    main()
