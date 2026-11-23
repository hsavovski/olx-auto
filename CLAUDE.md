# olx-auto

Phone photos → AI-drafted olx.bg listing → review → submit. Single-owner app with a few users (one per OLX account), hosted on a Mac via Tailscale.

## Docs: read before acting

- `docs/SPEC.md`: **scope and behaviour**. Read before building a feature, and check that a change fits a decision recorded there. When a decision changes, update the spec in the same change.
- `docs/ARCHITECTURE.md`: **code layout, data on disk, request flows**. Read before adding a module, a route or a field in `listing.json`. Update it when any of those change.
- `docs/GUIDE.md`: **plain-language overview with diagrams** for a reader new to Python. Update it in the same change when a module, a flow or a listing state changes.
- `docs/spike-notes.md`: **OLX API facts** (categories, attributes, validation errors, open questions). Read before writing anything that builds or validates an advert payload.

## Done means

`uv run ruff check . && uv run pytest -q` is green, and new behaviour has a test.

## OLX: production only

OLX has no sandbox. A `POST /adverts` with a connected token publishes a real advert.
- Automated tests mock OLX by monkeypatching `Olx` methods. Tests and code paths stay offline.
- Real posts happen only on the **`ico-test`** user, and only when the user asks for one. Each test advert is then cleaned up: deactivate if `active` (it's rejected otherwise), then delete. Every post, even a deleted one, uses up that category's free quota (the advert turns `limited`), so vary the category.
- Refresh tokens **rotate**: each refresh invalidates the previous one. Every token file has exactly one owner, so copying one and using both copies kills the older copy.

## Public repo

The repo is public. Real data stays in gitignored places: `.env`, `data/`, `spikes/out/`, `*token*.json`. Fixtures and examples use fake contact details. A gitleaks CI job scans the history.

## Conventions

- **Per-user scoping:** handlers get a `User` from the `CurrentUser` dependency and reach files only through it. Request input becomes a path only after a regex check (`NAME_RE`, `ID_RE`, `PHOTO_RE`).
- **Storage:** plain JSON files, written atomically with `listings.save` (temporary file + `os.replace`).
- **Language:** listing content and user context (style guide, prompts about the listing) are Bulgarian. The UI and code are English.
- **AI:** hand-written tool-use loop on OpenRouter through the `openai` SDK (spec §5). Agent frameworks are out of scope.
- **Money:** advert prices are EUR. LLM costs are USD.
- `olx.py` stays stdlib-only.
