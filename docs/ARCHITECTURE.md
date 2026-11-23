# Architecture

How the code is organised today. It covers the skeleton, the vision stage, the agent loop, the listing card and style learning (spec §12 steps 2–5). For *what* is being built and why, see [SPEC.md](SPEC.md); this file describes *how* the code is put together. Update it when a module, a data layout or a flow changes.

## Layout

```
src/olx_auto/
  config.py      Settings: .env + environment variables → frozen dataclass
  olx.py         OLX partner API client (stdlib only), one token file per instance
  users.py       App users: folder per user, scrypt passwords, User path helpers
  listings.py    Listings: folder per listing, listing.json CRUD, photo paths, hand edits
  profiles.py    profile.json: load/save, form validation, OLX /locations → city/district choices
  photos.py      Upload processing: decode/HEIC → upright, resized, metadata-free JPEG
  llm.py         OpenRouter chat via the openai SDK; Reply carries tokens + cost (USD)
  vision.py      Stage 1: photos + note → observations JSON; follow-up questions on the photos
  agent.py       Stage 2: hand-written tool loop → draft, questions; guardrails, locks
  validation.py  Draft checks (spec §8): content rules, lengths, attributes; submit readiness
  adverts.py     Listing + profile → OLX `POST /adverts` payload
  drafting.py    Listing-level orchestration: vision once, agent run, style learning, progress/cost/trace
  style.py       Style guide: read/write with backups + change log, revert, the learner LLM call
  cli.py         `olx-auto` command (serve, add-user, set-password, login, check, draft)
  web/app.py     FastAPI app factory: auth, listings + card (edit/lock/refine), profile, style guide, Connect OLX
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
    profile.json             {advertiser_type, contact: {name, phone, email},
                              location: {city_id, city_name, district_id, district_name},
                              delivery: {courier, note}} (copied from context/, edited on /settings/profile)
    style_guide.md           Bulgarian listing style guide (copied from context/, then learned)
    style_guide_history/     <YYYYMMDD-HHMMSS>.md: the guide before each change
    style_guide_log.json     [{at, summary, source: listing id|manual|revert, backup}]
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
 "category": {"id", "name", "photos_limit", "attributes": [OLX attribute defs]} | null,
 "draft": {"title", "description", "category_id", "attributes": {"<code>": "<value code>" | [...]},
           "price", "negotiable", "price_estimate": {"low", "high"}, "price_reasoning"},
 "ai_draft": {...},                   the draft as the agent last wrote it
 "locked": ["title", ...],            draft fields the agent must not change
 "questions": [{"id", "question", "answer": null | "..."}],
 "messages": [{"at", "text"}],        additional information / change requests, given to every run
 "edits": [{"at", "field", "before", "after"}],  hand edits from the card
 "learned": {"messages": n, "edits": n},          feedback already used by style learning
 "problems": [],                      check_draft output for the current draft
 "progress": null | "Choosing category…",  set while a run is going
 "ai_error": null | "...", "agent_stopped": "submitted|max_iterations|max_cost",
 "cost": {"vision": 0.0, "agent": 0.0, "style": 0.0}, "olx": null, "error": null}
```

Draft attributes are a `{code: value}` dict because that's easier for the model; the submit payload
builder converts them to OLX's `attributes: [{code, value}]`. Contact and location are not in the
draft: they come from `profile.json` at submit time (spec §6).

`listings.save` writes atomically, using a temporary file and `os.replace`. `listings.load` fills in fields
added since a listing was written, so older listings keep working.

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
After a successful upload, `POST /listings` adds `drafting.generate` as a FastAPI background task (only when `OPENROUTER_API_KEY` is set). `olx-auto draft <user> <id> [-m "message"] [--fresh]` runs the same thing in the foreground. `generate`:
1. **Vision** (skipped if `observations` already exist): `vision.observe` sends every photo as a base64 data URL plus the note, with `response_format: json_object`, and stores the observations.
2. **Agent**: `agent.run_agent` gets the observations, note, profile (without `contact`), style guide, current draft, locked fields, answered questions and all of the listing's messages. Tools: `look_at_photos` (a `vision.ask` call, costed as vision), `suggest_category`, `get_category_attributes` (rejects non-leaf and `photos_limit == 0` categories), `ask_user`, `submit_draft`.
3. `submit_draft` runs `validation.check_draft` against the category's attributes. Problems go back to the model as the tool result; a clean draft ends the run.
4. Guardrails: at `AGENT_MAX_COST` the run stops; on the last of `AGENT_MAX_ITERATIONS` turns, `tool_choice` forces `submit_draft`. The last submitted draft is kept even if it still has problems; with none, the previous draft stays.
5. Locked fields are put back by code after the run, whatever the model wrote.
6. Answered questions are kept; unanswered ones are replaced by this run's questions. `draft`, `ai_draft` and `category` (with attribute definitions, for the card) are saved.
7. Style learning runs on any new feedback (below). Its failure is reported in `ai_error`; the draft stays.

Each step updates `progress` in `listing.json` (reloading the file before each write); the listing page auto-refreshes while it's set. Costs come from OpenRouter's `usage.cost` and are added per stage. Errors (`LLMError`, `OlxError`, …) end up in `ai_error`. Every run is appended to `trace.json`: the vision observations and usage, plus the agent's full message list, per-call usage and stop reason. Images never go into the trace.

`agent.Catalog` is the category-data interface the tools use. `Olx` satisfies it with the client token; a disk cache of the category tree (spike-notes.md) can replace it.

### Listing card
`GET /listings/{id}` renders the draft as a form. While `listings.is_running` (progress set and written in the last 10 minutes, so a run killed by a restart doesn't block forever), the page refreshes every 3 s, the forms are disabled, and the POST routes below redirect back without changing anything.
- **To fix before submitting** is `validation.submit_problems(listing, profile)`, computed on every page view because the profile can change at any time: `check_draft` plus category loaded, photo count (≥ 1, ≤ `photos_limit`), location and contact name. The stored `problems` field stays `check_draft` only (what the agent last saw).
- `POST /listings/{id}/edit`: title, description, price, negotiable, `attr_<code>` (one select, or checkboxes for multi-value attributes). `listings.edit_draft` compares canonically (so a re-ordered multi-value isn't a change), records each changed field in `edits` and locks it. `problems` is recomputed. If title, description or price changed, `drafting.try_learn_style` runs in the background.
- `POST /listings/{id}/photos/{name}/delete` (name checked against `PHOTO_RE`) removes the photo from `photos` and from disk, so a listing can be brought under the category's `photos_limit`. The last photo can't be deleted, and the other photos keep their names. Observations that mention a photo by number aren't updated.
- `POST /listings/{id}/unlock/{field}` removes a lock. The lock buttons sit inside the edit form but submit their own empty forms (`form="unlock-<field>"`), because forms can't be nested.
- `POST /listings/{id}/refine`: `answer_<question id>` and `message` → answers saved, message appended to `messages` → `drafting.generate` in the background.
- Background work is started through `_start`, which sets `progress` *before* the redirect, so the page the user lands on is already refreshing.
- The trace panel (`SHOW_TRACE`) shows each run: vision observations, the agent's messages and tool calls (system prompt left out), and style learner results.

### Profile
`/settings/profile` edits the user's data. `profiles.load` fills in missing sections, and saving keeps keys it doesn't know about.
- `POST /settings/profile`: display name (`user.json`), contact, advertiser type, delivery. `profiles.update_details` validates first (name required, phone/email format) and changes nothing on error.
- **Location**: the "Use my location" button reads the browser's geolocation (needs HTTPS, i.e. the tailnet URL, or localhost) and reloads with `?lat&lng`. The page shows the nearest unique city/district pairs from OLX `/locations` (client token). `POST /settings/profile/location` sends back the point and the chosen index. The server looks the point up again instead of trusting ids and names from the form. A choice without a district is refused if OLX lists districts for that city (`/cities/{id}/districts`), because OLX then requires one. It stores ids **and names** (so pages can show them without another OLX call). The coordinates are not stored.
- `POST /settings/password`: current password checked with `users.authenticate` (1 s sleep on failure, as on `/login`), new one at least 8 characters and repeated.

### Style learning
`drafting.learn_style` takes the listing's messages and text/price edits that are newer than `learned`, and sends them with the current guide and the complete listing (title, description, price) to `style.learn` (`AGENT_MODEL`, JSON output). The model answers `{"changed": false}` or the full new guide plus a Bulgarian summary. A guide under 100 characters is treated as a model error. On a change, `style.write_guide` saves the old guide to `style_guide_history/`, writes the new one atomically and appends to the log. `learned` is advanced either way, so feedback is sent once. A process-wide lock serialises learners, so two can't overwrite each other's change. The cost goes to the listing's `cost.style`.

`/settings/style` shows the guide (saving it by hand is logged as `manual`) and the log; **Undo** (`POST /settings/style/revert/{backup}`, name checked against `style.BACKUP_RE`) restores the guide as it was before that change, itself as a logged change.

### Connect OLX
- `POST /settings/olx/connect` stores a random `olx_state` in the session and redirects to OLX `authorize_url(state=…)`.
- `GET /olx/callback?code&state` checks the state, then exchanges the code. This only works if the OLX app's registered redirect URI points at this app (`redirect_uri` in `.env`).
- `POST /settings/olx/code` is the fallback: paste the redirect URL or the bare code.
- The exchanged tokens go to the user's `olx_token.json`. `/settings/olx` calls `/users/me` to show *which* OLX account is connected.
- The home page shows a "not connected" banner when there's no user token.

### OLX client
`Olx(token_file, client_id, client_secret, redirect_uri)`. The client-credentials token (public data) and the user token are cached in the same file. `user_token()` refreshes automatically and writes the **rotated** refresh token back. `users.User.olx(settings)` builds a client for a user.

Every request builds its own `Olx`, so several clients can hold the same token file. Writes go through a per-file lock shared by all of them: `_save` re-reads the file and changes only its own entry (writing a stale copy back would kill a rotated refresh token), then replaces the file atomically with mode 600. `user_token()` re-reads the file under the same lock before refreshing, so a token another client already refreshed is used instead of refreshed again with a dead refresh token. The lock is per process; the CLI and the server refreshing at the same moment can still collide.

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
| `SHOW_TRACE` | Show the trace panel on the card |

Tests construct `Settings(...)` directly with a `tmp_path` data dir and never read `.env`.

## Testing & CI

- `tests/conftest.py` provides `settings` (tmp data dir), `user`, `client` (FastAPI `TestClient`), `logged_in`, and `jpeg_bytes()`.
- OLX calls are mocked by monkeypatching `Olx` methods (`_token_request`, `me`, `exchange_code`). No test touches the network.
- LLM calls are scripted: `tests/test_ai.py` passes a `FakeLLM` (a queue of `Reply` objects) and a `FakeCatalog` into `run_agent`/`generate`. `LLM.chat` itself is tested against a recorded `ChatCompletion`. `tests/test_card.py` covers the card and Style page with background tasks replaced by recorders.
- CI (`.github/workflows/ci.yml`): `uv sync --locked`, `ruff check`, `pytest`, plus a separate gitleaks job over the full history.

## Decisions made while building the skeleton

- **Passwords: stdlib `hashlib.scrypt`** rather than a bcrypt/argon2 dependency. That's sufficient for a handful of users.
- **Users are created only via CLI**, with no signup route (spec §3).
- **Root `login.py` / `test_api.py` became `olx-auto login` / `olx-auto check`.** `add-user --import-token` copies an existing token file in.
- **Costs are shown in USD** (Jinja filter `usd`), because OpenRouter bills in dollars. Listing *prices* are EUR.
- **No HTMX.** The card is plain forms with redirects, and progress is a meta refresh while a run is going. Nothing has needed more so far.
- Verified against the real API with the test account: a second OLX account connects to the same app, and `/currencies` defaults to EUR. More in [spike-notes.md](spike-notes.md).

## Decisions made while building the AI stages

- **No `search_olx_comparables` tool yet.** It arrives with the scraper (spec §12 step 7). Until then the agent prices from observations and general knowledge (`price_estimate`, `price_reasoning`).
- **The model gets validation problems as tool results** instead of the app fixing drafts, so prompt gaps show up in traces.
- **Contact details are never sent to the model.** They aren't needed for the text, and the code fills them in.

## Decisions made while building the card

- **One box for additional information and change requests**, kept as a message history rather than a one-off instruction, so facts given once are never lost on a later refinement.
- **Category isn't a form field.** Picking from 1105 leaves on a phone is worse than telling the agent; it re-runs `suggest_category`.
- **Contact and location are read-only on the card**, shown from `profile.json`. Per-listing overrides can come with submit if needed.
- **Style learning applies changes immediately** (with backup, log and Undo) instead of asking for approval, so improving the guide costs the seller nothing.

## Not built yet

Submit itself (S3 upload, the route calling `adverts.payload` + `create_advert`, failed/resubmit, OLX status refresh, test-advert cleanup command), comparables, autostart. Order and parallelism are in spec §12.
