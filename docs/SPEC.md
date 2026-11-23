# olx-auto — Project Spec

Status: agreed (design session 2026-11-23). This document is the source of truth for the v1 scope; GitHub issues are derived from it.

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

- **Small, fixed set of app users**, one per OLX account (e.g. `ivo` and `ivo-test`). Created via CLI (`uv run olx-auto add-user`); no signup page.
- Each user has their own password, profile, style guide, OLX connection, listings and cost totals.
- Login page with password → long-lived session cookie. All requests are scoped to the logged-in user's folder.
- Hosting: runs on the Mac, exposed on the tailnet with `tailscale serve` (HTTPS). The login exists in case it is ever exposed more widely.

### OLX accounts

- One OLX *application* (`client_id`/`client_secret`, global). Any OLX account can be connected to it via the `authorization_code` flow, and each gets its own token.
- **Development uses a separate test OLX account**. This protects the real account's free-ad quotas and seller profile. Fallback if a second account can't connect to the app: the real account, with every test post immediately deactivated and deleted.
- **Settings → Connect OLX**: in-browser OAuth flow redirecting back to the app's tailnet URL (which must be registered in the OLX app settings). A banner is shown when the token is dead. The refresh token lasts about a month and rotates on use. `login.py` remains as a CLI fallback.

## 4. User flow

1. **Home**: big **New listing** button; list of this user's listings with status (`draft` / `submitting` / `posted` + OLX link / `failed` + error), cost per listing, and a total cost.
2. **New listing**: pick photos (camera *or* gallery), optional short note (any language) → upload.
3. **Draft is generated in the background** (≈20–60 s). The page shows live progress ("Reading photos… Choosing category… Checking comparables…"). State is on disk, so the phone can be closed and the user can come back later.
4. **Listing card** shows a best-guess draft immediately, rendered as it will look:
   - editable fields: title, description, category, attributes, price, location, contact, delivery;
   - **price panel**: AI estimate vs. OLX comparables (median, n, links) → suggested price (editable);
   - **questions** from the agent for things it could not tell (condition, accessories, required attributes). Answering one refines the draft;
   - a free-text box for refinement ("make it cheaper, mention the original box");
   - fields edited by hand are **locked** (lock icon, tap to unlock), and the agent treats them as fixed;
   - **cost** of this listing so far (vision + agent, including all refinements);
   - optional collapsible **trace panel** ("how the agent got here"), shown when `SHOW_TRACE=true`.
5. **Submit**, enabled only when validation passes (§8) → images uploaded to S3 → advert created on OLX → status `posted` with link. The card *is* the approval step: there is no staging on OLX.
6. On OLX rejection → status `failed`, error shown and mapped to the relevant field when possible; the user fixes it and **resubmits**.

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
- **Refinement** (answers, free text) reruns the agent stage with the current draft + locked fields. Vision is not rerun.
- Listing content (title, description, attributes) is **Bulgarian**. The UI is English.

### Traces & cost

- Every run writes `trace.json` next to the listing: each message, tool call/result, model, tokens, cost per stage. Always saved; the UI panel is controlled by `SHOW_TRACE`.
- Cost is accumulated per listing (vision / agent split) and per user. This is used to compare models on price vs. quality.

## 6. Context (per user)

- `profile.json`: structured, mapped into the payload by code: contact name, phone, email, default `city_id`/`district_id`, `advertiser_type` (`private`), delivery/courier preferences.
- `style_guide.md`: Bulgarian prose for the LLM: tone, structure, what to always mention, what to avoid.
- A new user starts from committed `context/*.example` files.

## 7. Pricing

Show **both** an AI estimate (from observations and general knowledge) and **OLX comparables** side by side, plus a suggested price. The partner API cannot search other users' adverts, so comparables come from **scraping the public olx.bg search** (first N results: title, price, URL). It lives in an isolated module because it is fragile. Both numbers are recorded in traces, and the comparables module is removed if the AI estimate proves good enough.

Currency: **EUR** (Bulgaria adopted the euro on 2026-01-01; to be confirmed against `/currencies`).

## 8. Validation (before Submit is enabled)

Code-level checks, mirrored in the agent's instructions:
- category is a leaf; all required attributes filled with allowed values;
- title/description length within OLX limits (spec says ~16–150 / 80–9000; confirm in spike);
- OLX content rules: no phone numbers, emails or URLs in text; ≤50% capitals; no punctuation repeated 3+ times;
- photo count ≤ category `photos_limit`; at least one photo;
- price set; location (`city_id`, `district_id` if the city has districts); contact name/phone.

## 9. Photos & images

- Upload accepts JPEG/PNG/**HEIC** (converted to JPEG). On upload: **strip EXIF (GPS!)**, auto-rotate, resize to ~2000 px long edge, compress.
- Before submit: photos live on local disk in the listing folder. Models receive them as base64; no S3 is involved.
- At submit: upload to a **private S3 bucket** and pass **presigned GET URLs** (7-day expiry) in the OLX `images[].url` field. The bucket has a lifecycle rule deleting objects after 30 days. The IAM user has access to this bucket only.
- Open question for the spike: does OLX copy images to its CDN? If yes, retention can be shortened. If no, images must stay reachable permanently.

## 10. Storage

Plain files, no database:

```
data/users/<user>/
  user.json          # display name, password hash
  profile.json
  style_guide.md
  olx_token.json     # chmod 600
  listings/<id>/
    listing.json     # draft, status, locked fields, questions, costs, OLX advert id/url
    photos/          # processed JPEGs
    trace.json
```

Global config in `.env`: OLX `client_id`/`client_secret`, OpenRouter key, `VISION_MODEL`, `AGENT_MODEL`, cost/iteration caps, `SHOW_TRACE`, S3 bucket + credentials, session secret.

## 11. Tech stack

- Python 3.13+, `uv`, FastAPI, server-rendered templates + HTMX (mobile-first, no SPA/build step), SSE or polling for progress.
- Existing `olx.py` (stdlib OLX client) moves into the package and gains per-user token storage.
- `openai` SDK → OpenRouter; `boto3` → S3; Pillow + pillow-heif for images.
- Testing: `pytest`, **OLX and LLM APIs mocked** in automated tests (recorded responses). Real posting happens only manually, on the test account, followed by deactivate → delete (dev command).
- CI: GitHub Actions running tests, `ruff`, and **gitleaks** secret scanning.
- Public GitHub repo. Secrets and all of `data/` are gitignored.

## 12. Work order

The spike comes first, because its answers can change the design. After the skeleton (storage layout) exists, vision, comparables and UI work can proceed in parallel.

1. **Spike: OLX API.** Connect a second (test) OLX account to the existing app; post one real advert with S3 images; check the initial status, currency format, title/description limits, and which categories are open to the API on .bg; delete the S3 objects and check whether the images survive; deactivate → delete.
2. **Skeleton.** Repo layout, users + login, CLI `add-user`, per-user storage, photo upload/processing, Connect OLX page.
3. **Vision stage.**
4. **Agent loop + tools**, traces, cost tracking, guardrails.
5. **Listing card UI**: progress, editing, locks, questions, refinement, trace panel.
6. **Validation + submit** (S3 upload, OLX create, failed/resubmit).
7. **Comparables scraper.**
8. **Polish**: launchd autostart, cost overview, model comparison.

## 13. Future

- v2: OLX messages: inbox, agent-drafted replies.
- Agent harness if the hand-written loop is outgrown.
