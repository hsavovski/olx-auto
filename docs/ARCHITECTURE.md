# Architecture

How the code is organised today. It covers the skeleton (spec §12 step 2). For *what* is being built and why, see [SPEC.md](SPEC.md); this file describes *how* the code is put together. Update it when a module, a data layout or a flow changes.

## Layout

```
src/olx_auto/
  config.py      Settings: .env + environment variables → frozen dataclass
  olx.py         OLX partner API client (stdlib only), one token file per instance
  users.py       App users: folder per user, scrypt passwords, User path helpers
  listings.py    Listings: folder per listing, listing.json CRUD, photo paths
  photos.py      Upload processing: decode/HEIC → upright, resized, metadata-free JPEG
  cli.py         `olx-auto` command (serve, add-user, set-password, login, check)
  web/app.py     FastAPI app factory: auth, listings, Connect OLX
  web/templates/ Jinja templates, mobile-first, one inline stylesheet in base.html
context/         *.example files copied into each new user's folder
spikes/          One-off API exploration scripts (output in spikes/out/, gitignored)
tests/           pytest, all external APIs mocked
```

## Data on disk

Everything at runtime lives under `DATA_DIR` (default `./data`, gitignored). There is no database:

```
data/
  .session_secret            generated on first run when SESSION_SECRET is unset
  users/<name>/              chmod 700
    user.json                {display_name, password: "scrypt$<salt>$<hash>"}
    profile.json             contact, location, delivery (copied from context/)
    style_guide.md           Bulgarian listing style guide (copied from context/)
    olx_token.json           {"client": {...}, "user": {...}}, chmod 600
    listings/<id>/           id = YYYYMMDD-HHMMSS-xxxx (UTC + 4 hex)
      listing.json
      photos/01.jpg, 02.jpg, ...
```

`listing.json` shape (fields beyond `photos`/`note` are placeholders for later stages):

```json
{"id", "created_at", "updated_at", "status": "draft|submitting|posted|failed",
 "note", "photos": ["01.jpg"], "draft": {}, "locked": [], "questions": [],
 "cost": {"vision": 0.0, "agent": 0.0}, "olx": null, "error": null}
```

`listings.save` writes atomically, using a temporary file and `os.replace`.

## Key flows

### Login
`POST /login` → `users.authenticate` (constant-time compare; an unknown user still costs one scrypt) → `session["user"] = name`. The session is a signed cookie (Starlette `SessionMiddleware`) lasting 90 days with `SameSite=Lax`. A failed login sleeps 1 s. The `CurrentUser` dependency resolves the session to a `User` or raises `LoginRequired`, which redirects to `/login`. Every page except `/login` depends on it.

### Per-user scoping
Route handlers reach data only through the `User` they receive from `CurrentUser`. Before a path is built from request input, it is validated against a regex: `users.NAME_RE`, `listings.ID_RE` and `listings.PHOTO_RE`. Anything else is a 404. This prevents both cross-user access and path traversal (both are tested).

### New listing
`POST /listings` (multipart: `photos[]`, `note`) → `listings.create`:
1. Reject 0 or more than 20 photos (`MAX_PHOTOS` is an upload cap; the per-category OLX `photos_limit` belongs to validation, spec §8).
2. `photos.process` **every** photo first, so a bad file leaves nothing on disk.
3. Create the folder, write `01.jpg`…, then write `listing.json` with status `draft`.

`photos.process`: Pillow (and pillow-heif for HEIC) opens the image, `exif_transpose` makes it upright, it's converted to RGB, its long edge is limited to 2000 px, and it's saved as JPEG q85. Saving without `exif=` drops all metadata, including GPS.

The handlers are plain `def` functions, so FastAPI runs them in its threadpool and image processing doesn't block the event loop.

### Connect OLX
- `POST /settings/olx/connect` stores a random `olx_state` in the session and redirects to OLX `authorize_url(state=…)`.
- `GET /olx/callback?code&state` checks the state, then exchanges the code. This only works if the OLX app's registered redirect URI points at this app (`redirect_uri` in `.env`).
- `POST /settings/olx/code` is the fallback: paste the redirect URL or the bare code.
- The exchanged tokens go to the user's `olx_token.json`. `/settings/olx` calls `/users/me` to show *which* OLX account is connected.
- The home page shows a "not connected" banner when there's no user token.

### OLX client
`Olx(token_file, client_id, client_secret, redirect_uri)`. The client-credentials token (public data) and the user token are cached in the same file. `user_token()` refreshes automatically and writes the **rotated** refresh token back. `users.User.olx(settings)` builds a client for a user.

## Config

Read by `Settings.load()` from `.env` at the repo root, with environment variables taking precedence:

| Key | Purpose |
|---|---|
| `client_id`, `client_secret` | OLX app credentials (lowercase, as OLX names them) |
| `redirect_uri` | Optional OAuth redirect registered in the OLX app |
| `DATA_DIR` | Runtime data root, default `./data` |
| `SESSION_SECRET` | Optional; otherwise generated into `data/.session_secret` |

Tests construct `Settings(...)` directly with a `tmp_path` data dir and never read `.env`.

## Testing & CI

- `tests/conftest.py` provides `settings` (tmp data dir), `user`, `client` (FastAPI `TestClient`), `logged_in`, and `jpeg_bytes()`.
- OLX calls are mocked by monkeypatching `Olx` methods (`_token_request`, `me`, `exchange_code`). No test touches the network.
- CI (`.github/workflows/ci.yml`): `uv sync --locked`, `ruff check`, `pytest`, plus a separate gitleaks job over the full history.

## Decisions made while building the skeleton

- **Passwords: stdlib `hashlib.scrypt`** rather than a bcrypt/argon2 dependency. That's sufficient for a handful of users.
- **Users are created only via CLI**, with no signup route (spec §3).
- **Root `login.py` / `test_api.py` became `olx-auto login` / `olx-auto check`.** `add-user --import-token` copies an existing token file in.
- **Costs are shown in USD** (Jinja filter `usd`), because OpenRouter bills in dollars. Listing *prices* are EUR.
- **No HTMX yet.** Nothing in the skeleton needs it. It arrives with the progress/card UI (spec §12 steps 4–5).
- Verified against the real API with the test account: a second OLX account connects to the same app, and `/currencies` defaults to EUR. More in [spike-notes.md](spike-notes.md).

## Not built yet

Vision stage, agent loop and tools, traces, listing card editing/locks/questions, validation, submit to OLX + S3, comparables, autostart. Order and parallelism are in spec §12.
