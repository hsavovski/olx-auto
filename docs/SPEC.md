# olx-auto — Project Spec

Status: agreed (design session 2026-11-23), updated with the OLX API spike results the same day ([spike-notes.md](spike-notes.md)). This document is the source of truth for the v1 scope; GitHub issues are derived from it. Progress is tracked in §12.

## 1. Goal

Create good olx.bg listings with minimal effort: open a web page on the phone, snap a few photos, optionally type a short note, and get back a complete, ready-to-post listing (title, description, category, attributes, price, location, contact). Review it, tweak if needed, tap **Submit**, and it is live on OLX.

Secondary goal: learn how to build AI tools. The agent is deliberately hand-written (no framework) and fully observable.

## 2. Non-goals (v1)

- Managing listings after posting (edit, deactivate, mark sold, stats). The OLX app does this.
- Replying to OLX messages. **Next milestone (v2)**; keep seams for it (`olx.py` already has thread/message methods).
- Self-service signup, public multi-tenant hosting.
- Agent auto-fixing OLX submit errors. A rejected payload means the agent's context or validation is wrong, and that's fixed in code/prompts.
- Using an agent harness/framework (OpenCode, Agent SDK, LangGraph). Can be revisited if the hand-written loop is outgrown.

## 3. Users & access

- **Small, fixed set of app users**, one per OLX account (e.g. `ico` and `ico-test`). Created via CLI (`uv run olx-auto add-user`); no signup page.
- Each user has their own password, profile, style guide, OLX connection, listings and cost totals.
- Login page with password → long-lived session cookie. All requests are scoped to the logged-in user's folder.
- Hosting: runs on the Mac, exposed on the tailnet with `tailscale serve` (HTTPS). The login exists in case it is ever exposed more widely.

### OLX accounts

- One OLX *application* (`client_id`/`client_secret`, global). Any OLX account can be connected to it via the `authorization_code` flow, and each gets its own token.
- **Development uses a separate test OLX account** (app user `ico-test`; verified: it connects to the same OLX app). This protects the real account's free-ad quotas and seller profile. Test posts still use up the test account's per-category free quota, even when they're deleted before going live, so spread them across categories.
- **Settings → Connect OLX**: in-browser OAuth flow redirecting back to the app's tailnet URL (which must be registered in the OLX app settings). A banner is shown when the token is dead. The refresh token lasts about a month and rotates on use. `olx-auto login <user>` is the CLI fallback.

## 4. User flow

1. **Home**: big **New listing** button; list of this user's listings with status (`draft` / `submitting` / `posted` + OLX link + OLX state / `failed` + error), cost per listing, and a total cost.
2. **New listing**: pick photos (camera *or* gallery), optional short note (any language) → upload.
3. **Draft is generated in the background** (≈20–60 s). The page shows live progress ("Reading photos… Choosing category… Checking comparables…"). State is on disk, so the phone can be closed and the user can come back later.
4. **Listing card** shows a best-guess draft immediately, rendered as it will look:
   - photos can be deleted (at least one stays), e.g. to get under the category's photo limit;
   - editable fields: title, description, attributes (incl. delivery), price. Category is changed by telling the agent ("wrong category, it's …"); contact and location come from the profile and are shown read-only;
   - **price panel**: AI estimate vs. OLX comparables (median, n, links) → suggested price (editable);
   - **questions** from the agent (in Bulgarian) for things it could not tell (condition, accessories, required attributes). Answering one refines the draft;
   - an **additional information** box for facts and change requests ("120 x 45 cm, no box; make it cheaper"). Every message is kept on the listing and given to every later run of it, so information is never lost between refinements;
   - fields edited by hand are **locked** (lock icon, tap to unlock), and the agent treats them as fixed;
   - **cost** of this listing so far (vision + agent + style learning, including all refinements);
   - optional collapsible **trace panel** ("how the agent got here"), shown when `SHOW_TRACE=true`.
5. **Submit**, enabled only when validation passes (§8) → images uploaded to S3 → advert created on OLX → S3 objects deleted → status `posted` with link. The card *is* the approval step: there is no staging on OLX.
   - A created advert isn't live yet. Its OLX status is shown next to `posted` and refreshed from the API: `disabled`/`new` → **pending moderation**; `limited` → **needs payment** (the category's free quota is used up; the user pays in the OLX app); `active` → **live**; `moderated`/`blocked` → **rejected by OLX**.
6. On OLX rejection → status `failed`, error shown and mapped to the relevant field when possible; the user fixes it and **resubmits**. OLX returns `{field, title (BG), detail}` per error. Field paths are `title`, `description`, `category_id`, `contact.*`, `location.*`, `params.price[0]` (price) and `params.<attribute code>`.

## 5. AI architecture

Hand-written **tool-use loop** ("level 2"), using the `openai` Python SDK pointed at **OpenRouter**. Two stages, each with a configurable model:

### Stage 1: Vision (`VISION_MODEL`, cheap vision model)

One call: photos + user note → structured **observations** JSON: item type, brand/model, readable text/labels, condition, visible defects, included accessories, uncertainties.

Reason for the split: in a tool loop the whole history is resent every turn, so including images there multiplies their cost. The agent works from text instead.

### Stage 2: Agent (`AGENT_MODEL`, stronger reasoning model)

Input: observations, user note, the user's profile and style guide (Bulgarian), OLX content rules, any locked fields and answered questions.

Tools:

| Tool | Purpose |
|---|---|
| `look_at_photos(question)` | Targeted follow-up question to the vision model (fixes "can't look again") |
| `suggest_category(query)` | OLX category suggestion |
| `get_category_attributes(category_id)` | Required/optional attributes, allowed values, `photos_limit` |
| `search_olx_comparables(query, ...)` | Prices of similar items (scraped, §7) |
| `ask_user(questions)` | Add questions to the card (non-blocking) |
| `submit_draft(listing)` | Final structured draft, which ends the loop |

Rules:
- **Non-blocking questions**: always produce a best-guess draft; questions refine it later.
- **Guardrails**: max iterations (default 15) and max cost per run (default $0.50), both configurable. On hitting a cap, the run stops and keeps the best draft so far.
- **Refinement** (answers, additional information) reruns the agent stage with the current draft + locked fields + all messages. Vision is not rerun.
- Listing content (title, description, attributes) and everything the agent writes for the seller (questions, price reasoning, style guide change notes) is **Bulgarian**. The UI is English.

### Style learning

So the seller doesn't repeat the same feedback on every listing, the style guide improves itself:
- **Feedback** = new additional-information messages, and hand edits of title, description or price (attribute edits are facts, not style).
- After each agent run, and after hand edits of text, a learner call (`AGENT_MODEL`) gets the current guide, the **complete listing** as it now stands and the new feedback. It keeps only general preferences (tone, structure, wording, what to always/never write, pricing habits), ignores facts about the item, and merges them into the guide.
- Every change is applied immediately, backed up and logged with a one-sentence reason (Bulgarian) and the listing it came from. The **Style** page shows the guide (editable by hand) and the change log with **Undo**.
- Feedback is learned once: the listing records how much of it has been used.

### Traces & cost

- Every run writes `trace.json` next to the listing: each message, tool call/result, model, tokens, cost per stage. Always saved; the UI panel is controlled by `SHOW_TRACE`.
- Cost is accumulated per listing (vision / agent split) and per user. This is used to compare models on price vs. quality.

## 6. Context (per user)

- `profile.json`: structured, mapped into the payload by code: contact name, phone, email, default `city_id`/`district_id`, `advertiser_type` (`private`), delivery/courier preferences. Edited on the **Profile** page (with display name and password); the location is picked from the nearest OLX cities/districts to the phone's position.
- `style_guide.md`: Bulgarian prose for the LLM: tone, structure, what to always mention, what to avoid. Edited by hand on the Style page and updated by style learning (§5).
- A new user starts from committed `context/*.example` files.

## 7. Pricing

Show **both** an AI estimate (from observations and general knowledge) and **OLX comparables** side by side, plus a suggested price. The partner API cannot search other users' adverts, so comparables come from **scraping the public olx.bg search** (first N results: title, price, URL). It lives in an isolated module because it is fragile. Both numbers are recorded in traces, and the comparables module is removed if the AI estimate proves good enough.

Currency: **EUR** (Bulgaria adopted the euro on 2026-01-01; confirmed: `/currencies` has EUR as the default, BGN is still accepted).

## 8. Validation (before Submit is enabled)

Code-level checks, mirrored in the agent's instructions. OLX validates in stages (category-specific rules are only reported once contact, location etc. pass) and doesn't check title length at all, so these checks must be complete on our side:
- category is a leaf with `photos_limit > 0` (this excludes `Работа` jobs); all required attributes filled with allowed **codes** (the attribute name for condition varies by category, e.g. `state` vs `condition`);
- title 16–150 characters (our own rule: the API accepted both 1 and 303 characters); description 40–9000 characters (enforced by OLX);
- OLX content rules: no phone numbers, emails or URLs in text; ≤50% capitals; no punctuation repeated 3+ times;
- photo count ≤ category `photos_limit`; at least one photo;
- price set (OLX requires it); location (`city_id`, `district_id` if the city has districts); contact name (phone is optional for OLX).

## 9. Photos & images

- Upload accepts JPEG/PNG/**HEIC** (converted to JPEG). On upload: **strip EXIF (GPS!)**, auto-rotate, resize to ~2000 px long edge, compress.
- Before submit: photos live on local disk in the listing folder. Models receive them as base64; no S3 is involved.
- At submit: upload to a **private S3 bucket** (`eu-central-1`) and pass **presigned GET URLs** (1-hour expiry) in the OLX `images[].url` field. Presign with SigV4 against the regional endpoint; the boto3 default produces URLs that S3 rejects for this region.
- **OLX copies the images to its own CDN synchronously during the create call** (verified in the spike: the response already has `apollo.olxcdn.com` URLs, and they keep working after the S3 objects are deleted). So the S3 objects are **deleted right after a successful create**, and the OLX image URLs are stored in `listing.json`.
- The bucket's 30-day lifecycle rule is only a safety net for failed submits. The IAM user has access to this bucket only. Both are created by `infra/s3-setup.sh`.

## 10. Storage

Plain files, no database:

```
data/users/<user>/
  user.json          # display name, password hash
  profile.json
  style_guide.md
  style_guide_history/<timestamp>.md   # the guide before each change
  style_guide_log.json                 # what changed, why, from which listing
  olx_token.json     # chmod 600
  listings/<id>/
    listing.json     # draft, status, locked fields, questions, messages, edits, costs, OLX advert id/url
    photos/          # processed JPEGs
    trace.json
```

Global config in `.env`: OLX `client_id`/`client_secret`, OpenRouter key, `VISION_MODEL`, `AGENT_MODEL`, cost/iteration caps, `SHOW_TRACE`, S3 bucket + credentials, session secret.

## 11. Tech stack

- Python 3.13+, `uv`, FastAPI, server-rendered templates with plain forms (mobile-first, no SPA/build step), progress by page refresh. HTMX/SSE only if that stops being enough (ARCHITECTURE.md).
- Existing `olx.py` (stdlib OLX client) moves into the package and gains per-user token storage.
- `openai` SDK → OpenRouter; `boto3` → S3; Pillow + pillow-heif for images.
- Testing: `pytest`, **OLX and LLM APIs mocked** in automated tests (recorded responses). Real posting happens only manually, on the test account, followed by cleanup (dev command): deactivate if `active`, then delete. `deactivate` is rejected for adverts that aren't active, and `delete` works on them directly.
- CI: GitHub Actions running tests, `ruff`, and **gitleaks** secret scanning.
- Public GitHub repo. Secrets and all of `data/` are gitignored.

## 12. Work order

The spike comes first, because its answers can change the design. After the skeleton (storage layout) exists, vision, comparables and UI work can proceed in parallel.

Progress (2026-11-23): **steps 1–5 are done**; step 6 is next. One real drafting run on photos (test account) worked end to end.

| Step | Status | Where |
|---|---|---|
| 1. Spike | ✅ done | `docs/spike-notes.md`, `spikes/`, `infra/s3-setup.sh` |
| 2. Skeleton | ✅ done | `src/olx_auto/`, `docs/ARCHITECTURE.md` |
| 3. Vision stage | ✅ done | `vision.py`, `llm.py` |
| 4. Agent loop + tools | ✅ done (no comparables tool until step 7) | `agent.py`, `drafting.py`, `olx-auto draft` |
| 5. Listing card UI | ✅ done, plus style learning (§5) | `web/templates/listing.html`, `style.py`, Style page |
| 6. Validation + submit | validation done (`validation.submit_problems`), payload builder done (`adverts.py`); S3 upload, submit route, resubmit and status refresh not started (S3 bucket and IAM user exist) | |
| 7. Comparables scraper | not started | |
| 8. Polish | not started | |

1. **Spike: OLX API.** Connect a second (test) OLX account to the existing app; post one real advert with S3 images; check the initial status, currency format, title/description limits, and which categories are open to the API on .bg; delete the S3 objects and check whether the images survive; deactivate → delete.
   *Done:* test account connected; real posts made (no images, then S3 images); EUR confirmed; validation rules and error format mapped; status lifecycle and the free-quota `limited` state found; OLX copies images to its CDN, so S3 retention is minimal; S3 bucket + bucket-only IAM user created and permission-tested. Still open, but not blocking: moderation time to `active`, the exact free quota per category.
2. **Skeleton.** Repo layout, users + login, CLI `add-user`, per-user storage, photo upload/processing, Connect OLX page.
   *Done:* FastAPI app with login (scrypt, signed session cookie), per-user folders and scoping, CLI (`serve`, `add-user`, `set-password`, `login`, `check`), photo upload (HEIC, EXIF/GPS stripped, resized), listing folders, in-browser Connect OLX + paste-code fallback, CI (ruff, pytest, gitleaks). 30 tests pass. Users `ico` and `ico-test` exist, both connected to OLX.
3. **Vision stage.**
4. **Agent loop + tools**, traces, cost tracking, guardrails.
5. **Listing card UI**: progress, editing, locks, questions, refinement, trace panel.
6. **Validation + submit** (S3 upload, OLX create, failed/resubmit).
7. **Comparables scraper.**
8. **Polish**: launchd autostart, cost overview, model comparison.

## 13. Future

- v2: OLX messages: inbox, agent-drafted replies.
- Agent harness if the hand-written loop is outgrown.
