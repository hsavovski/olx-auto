"""One-time user login (authorization_code flow).

1. Prints the authorize URL; open it in your browser and approve.
2. OLX redirects to your callback URI with ?code=... in the address bar
   (the page itself doesn't need to load). Paste the code or full URL here.
"""
import urllib.parse
from olx import Olx

olx = Olx()
print("Open this URL and approve access:\n")
print(olx.authorize_url())
raw = input("\nPaste the code (or the full redirect URL): ").strip()
code = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query).get("code", [raw])[0]
olx.exchange_code(code)
print("Saved user token to .token.json")
