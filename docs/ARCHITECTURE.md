# Architecture

How the code is organised today. It covers the skeleton, the vision stage and the agent loop (spec §12 steps 2–4). For *what* is being built and why, see [SPEC.md](SPEC.md); this file describes *how* the code is put together. Update it when a module, a data layout or a flow changes.

## Layout

```
src/olx_auto/
  config.py      Settings: .env + environment variables → frozen dataclass
  olx.py         OLX partner API client (stdlib only), one token file per instance
  users.py       App users: folder per user, scrypt passwords, User path helpers
  listings.py    Listings: folder per listing, listing.json CRUD, photo paths
  photos.py      Upload processing: decode/HEIC → upright, resized, metadata-free JPEG
  llm.py         OpenRouter chat via the openai SDK; Reply carries tokens + cost (USD)
  vision.py      Stage 1: photos + note → observations JSON; follow-up questions on the photos
  agent.py       Stage 2: hand-written tool loop → draft, questions; guardrails, locks
  validation.py  Draft checks (spec §8): content rules, lengths, attributes
  drafting.py    Listing-level orchestration: vision once, agent run, progress/cost/trace on disk
  cli.py         `olx-auto` command (serve, add-user, set-password, login, check, draft)
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
      trace.json             {"runs": [vision run, agent run, ...]}, appended per run
```

`listing.json` shape (`olx`/`error` are placeholders for submit):

```json
{"id", "created_at", "updated_at", "status": "draft|submitting|posted|failed",
 "note", "photos": ["01.jpg"],
 "observations": {...} | null,        vision output, computed once per listing
 "draft": {"title", "description", "category_id", "attributes": {"<code>": "<value code>" | [...]},
           "price", "negotiable", "price_estimate": {"low", "high"}, "price_reasoning"},
 "locked": ["title", ...],            draft fields the agent must not change
 "questions": [{"id", "question", "answer": null | "..."}],
 "problems": [],                      check_draft output for the last draft
 "progress": null | "Choosing category…",  set while a run is going
 "ai_error": null | "...", "agent_stopped": "submitted|max_iterations|max_cost",
 "cost": {"vision": 0.0, "agent": 0.0}, "olx": null, "error": null}
```

Draft attributes are a `{code: value}` dict because that's easier for the model; the submit payload
builder converts them to OLX's `attributes: [{code, value}]`. Contact and location are not in the
draft: they come from `profile.json` at submit time (spec §6).

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

### Drafting (vision + agent)
After a successful upload, `POST /listings` adds `drafting.generate` as a FastAPI background task (only when `OPENROUTER_API_KEY` is set). `olx-auto draft <user> <id> [--instruction …] [--fresh]` runs the same thing in the foreground. `generate`:
1. **Vision** (skipped if `observations` already exist): `vision.observe` sends every photo as a base64 data URL plus the note, with `response_format: json_object`, and stores the observations.
2. **Agent**: `agent.run_agent` gets the observations, note, profile (without `contact`), style guide, current draft, locked fields, answered questions and an optional refinement instruction. Tools: `look_at_photos` (a `vision.ask` call, costed as vision), `suggest_category`, `get_category_attributes` (rejects non-leaf and `photos_limit == 0` categories), `ask_user`, `submit_draft`.
3. `submit_draft` runs `validation.check_draft` against the category's attributes. Problems go back to the model as the tool result; a clean draft ends the run.
4. Guardrails: at `AGENT_MAX_COST` the run stops; on the last of `AGENT_MAX_ITERATIONS` turns, `tool_choice` forces `submit_draft`. The last submitted draft is kept even if it still has problems; with none, the previous draft stays.
5. Locked fields are put back by code after the run, whatever the model wrote.
6. Answered questions are kept; unanswered ones are replaced by this run's questions.

Each step updates `progress` in `listing.json` (reloading the file before each write); the listing page auto-refreshes while it's set. Costs come from OpenRouter's `usage.cost` and are added per stage. Errors (`LLMError`, `OlxError`, …) end up in `ai_error`. Every run is appended to `trace.json`: the vision observations and usage, plus the agent's full message list, per-call usage and stop reason. Images never go into the trace.

`agent.Catalog` is the category-data interface the tools use. `Olx` satisfies it with the client token; a disk cache of the category tree (spike-notes.md) can replace it.

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
| `OPENROUTER_API_KEY` | OpenRouter key; drafting is off without it |
| `VISION_MODEL`, `AGENT_MODEL` | OpenRouter model ids (defaults in `config.py`) |
| `AGENT_MAX_ITERATIONS`, `AGENT_MAX_COST` | Guardrails per agent run (15, $0.50) |
| `SHOW_TRACE` | Show the trace panel on the card (UI not built yet) |

Tests construct `Settings(...)` directly with a `tmp_path` data dir and never read `.env`.

## Testing & CI

- `tests/conftest.py` provides `settings` (tmp data dir), `user`, `client` (FastAPI `TestClient`), `logged_in`, and `jpeg_bytes()`.
- OLX calls are mocked by monkeypatching `Olx` methods (`_token_request`, `me`, `exchange_code`). No test touches the network.
- LLM calls are scripted: `tests/test_ai.py` passes a `FakeLLM` (a queue of `Reply` objects) and a `FakeCatalog` into `run_agent`/`generate`. `LLM.chat` itself is tested against a recorded `ChatCompletion`.
- CI (`.github/workflows/ci.yml`): `uv sync --locked`, `ruff check`, `pytest`, plus a separate gitleaks job over the full history.

## Decisions made while building the skeleton

- **Passwords: stdlib `hashlib.scrypt`** rather than a bcrypt/argon2 dependency. That's sufficient for a handful of users.
- **Users are created only via CLI**, with no signup route (spec §3).
- **Root `login.py` / `test_api.py` became `olx-auto login` / `olx-auto check`.** `add-user --import-token` copies an existing token file in.
- **Costs are shown in USD** (Jinja filter `usd`), because OpenRouter bills in dollars. Listing *prices* are EUR.
- **No HTMX yet.** Nothing in the skeleton needs it. It arrives with the progress/card UI (spec §12 steps 4–5).
- Verified against the real API with the test account: a second OLX account connects to the same app, and `/currencies` defaults to EUR. More in [spike-notes.md](spike-notes.md).

## Decisions made while building the AI stages

- **No `search_olx_comparables` tool yet.** It arrives with the scraper (spec §12 step 7). Until then the agent prices from observations and general knowledge (`price_estimate`, `price_reasoning`).
- **The model gets validation problems as tool results** instead of the app fixing drafts, so prompt gaps show up in traces.
- **Contact details are never sent to the model.** They aren't needed for the text, and the code fills them in.

## Not built yet

Listing card editing/locks/answering questions/refinement UI, trace panel, the rest of validation (location, contact, photo count), submit to OLX + S3, comparables, autostart. Order and parallelism are in spec §12.
