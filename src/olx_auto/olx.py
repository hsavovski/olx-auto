"""Minimal OLX.bg partner API client (stdlib only).

Tokens are cached in a per-account JSON file (chmod 600). Two contexts:
  - client: client_credentials, read-only public data (categories, cities, ...)
  - user:   authorization_code, needed to create/manage adverts and read /users/me
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://www.olx.bg/api/partner"
TOKEN_URL = "https://www.olx.bg/api/open/oauth/token"
AUTH_URL = "https://www.olx.bg/oauth/authorize"
SCOPE = "v2 read write"


class OlxError(Exception):
    def __init__(self, status, body):
        self.status, self.body = status, body
        super().__init__(f"HTTP {status}: {json.dumps(body, ensure_ascii=False)[:800]}")


def _http(method, url, *, headers=None, data=None, form=None):
    body = None
    headers = dict(headers or {})
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = {"raw": raw[:500].decode("utf-8", "replace")}
    if status >= 400:
        raise OlxError(status, parsed)
    return parsed


class Olx:
    def __init__(self, token_file: Path, client_id, client_secret, redirect_uri=None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.token_file = Path(token_file)
        self._tokens = self._read_tokens()

    @property
    def connected(self):
        return "user" in self._tokens

    # ---- tokens -------------------------------------------------------
    def _read_tokens(self):
        try:
            return json.loads(self.token_file.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, ctx, tok):
        tok["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
        self._tokens[ctx] = tok
        self.token_file.write_text(json.dumps(self._tokens))
        os.chmod(self.token_file, 0o600)

    def _token_request(self, **fields):
        form = {"client_id": self.client_id, "client_secret": self.client_secret, **fields}
        return _http("POST", TOKEN_URL, form=form)

    def client_token(self):
        t = self._tokens.get("client")
        if not t or t["expires_at"] < time.time():
            t = self._token_request(grant_type="client_credentials", scope=SCOPE)
            self._save("client", t)
            t = self._tokens["client"]
        return t["access_token"]

    def authorize_url(self, state="olx-auto"):
        q = {"client_id": self.client_id, "response_type": "code", "scope": SCOPE, "state": state}
        if self.redirect_uri:
            q["redirect_uri"] = self.redirect_uri
        return f"{AUTH_URL}?{urllib.parse.urlencode(q)}"

    def exchange_code(self, code):
        f = {"grant_type": "authorization_code", "code": code, "scope": SCOPE}
        if self.redirect_uri:
            f["redirect_uri"] = self.redirect_uri
        self._save("user", self._token_request(**f))

    def user_token(self):
        t = self._tokens.get("user")
        if not t:
            raise RuntimeError("OLX not connected: use Settings → Connect OLX or `olx-auto login <user>`")
        if t["expires_at"] < time.time():
            new = self._token_request(grant_type="refresh_token", refresh_token=t["refresh_token"])
            self._save("user", new)
            t = self._tokens["user"]
        return t["access_token"]

    # ---- requests -----------------------------------------------------
    def request(self, method, path, *, user=True, params=None, data=None):
        token = self.user_token() if user else self.client_token()
        url = BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Authorization": f"Bearer {token}", "Version": "2.0"}
        return _http(method, url, headers=headers, data=data)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    # ---- public data (client token is enough) -------------------------
    def categories(self, parent_id=None):
        params = {"parent_id": parent_id} if parent_id else None
        return self.get("/categories", user=False, params=params)["data"]

    def category(self, category_id):
        return self.get(f"/categories/{category_id}", user=False)["data"]

    def category_attributes(self, category_id):
        return self.get(f"/categories/{category_id}/attributes", user=False)["data"]

    def suggest_category(self, q):
        return self.get("/categories/suggestion", user=False, params={"q": q})["data"]

    def regions(self):
        return self.get("/regions", user=False)["data"]

    def cities(self, offset=0, limit=100):
        return self.get("/cities", user=False, params={"offset": offset, "limit": limit})["data"]

    def districts(self, city_id):
        return self.get(f"/cities/{city_id}/districts", user=False)["data"]

    def currencies(self):
        return self.get("/currencies", user=False)["data"]

    # ---- user context -------------------------------------------------
    def me(self):
        return self.get("/users/me")["data"]

    def balance(self):
        return self.get("/users/me/account-balance")["data"]

    def adverts(self, **params):
        return self.get("/adverts", params=params or None)["data"]

    def advert(self, advert_id):
        return self.get(f"/adverts/{advert_id}")["data"]

    def create_advert(self, **advert):
        return self.request("POST", "/adverts", data=advert)["data"]

    def update_advert(self, advert_id, **advert):
        return self.request("PUT", f"/adverts/{advert_id}", data=advert)["data"]

    def advert_command(self, advert_id, command):
        """command: activate | deactivate | finish | extend"""
        return self.request("POST", f"/adverts/{advert_id}/commands",
                            data={"command": command, "is_success": True})

    def delete_advert(self, advert_id):
        return self.request("DELETE", f"/adverts/{advert_id}")

    def statistics(self, advert_id):
        return self.get(f"/adverts/{advert_id}/statistics")["data"]

    def threads(self):
        return self.get("/threads")["data"]

    def messages(self, thread_id):
        return self.get(f"/threads/{thread_id}/messages")["data"]

    def send_message(self, thread_id, text):
        return self.request("POST", f"/threads/{thread_id}/messages", data={"text": text})
