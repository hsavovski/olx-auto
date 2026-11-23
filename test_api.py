"""Smoke-test read-only endpoints. Never prints secrets or tokens."""
import sys
from olx import Olx, OlxError

olx = Olx()

checks = [
    ("client token", lambda: bool(olx.client_token())),
    ("categories (top level)", lambda: [(c["id"], c["name"]) for c in olx.categories()][:5]),
    ("category suggestion 'iphone'", lambda: olx.suggest_category("iphone")),
    ("regions", lambda: len(olx.regions())),
    ("cities", lambda: [c.get("name") for c in olx.cities(limit=3)]),
    ("currencies", lambda: olx.currencies()),
    ("users/me (needs user token)", lambda: {k: v for k, v in olx.me().items() if k in ("id", "name", "status")}),
    ("adverts (needs user token)", lambda: len(olx.adverts())),
]

for name, fn in checks:
    try:
        print(f"OK   {name}: {str(fn())[:200]}")
    except OlxError as e:
        print(f"FAIL {name}: {e}")
    except Exception as e:
        print(f"FAIL {name}: {type(e).__name__}: {e}")
        if name == "client token":
            sys.exit(1)
