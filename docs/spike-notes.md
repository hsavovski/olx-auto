# OLX API spike — findings

Spec §12 step 1. Run on 2026-11-23 against the **test** OLX account (`.token.test.json`), which connected to the app without problems.
Reproduce with `uv run python spikes/olx_spike.py`. Raw responses are written to `spikes/out/` (gitignored, because they contain account data).

Status: **read-only part done**. The posting part (real advert + S3 images) is still open; see the end of this file.

## Answered

### Currency
`/currencies` returns `EUR` (default, `€`) and `BGN` (`лв.`). Use **EUR**, as the spec assumed.

### Categories
- `/categories` with no `parent_id` returns the **whole tree in one call**: 1242 categories, 1105 of them leaves. Fields: `id, name, parent_id, photos_limit, is_leaf`. It is cheap to cache locally.
- `photos_limit` on leaves is 12 (most), 15 (auto parts and others), 24 (real estate) or **0 (all 13 `Работа` job leaves)**. Treat 0 as "not for us" and exclude it from the agent's categories.
- Nothing in the API marks a category as closed to the partner API. Whether a category is open can only be confirmed by posting.
- `/categories/suggestion?q=` works well with short queries in English or Bulgarian, and every result in the samples was a **leaf**. It can return several candidates (e.g. `зимни гуми` → Гуми, or "За автомобили" in wheel sets). The response includes the `path` (ancestors), which the agent can show.

### Attributes
`/categories/{id}/attributes` returns `code, label, unit, validation{required, numeric, min, max, allow_multiple_values}, values[{code,label}]`.

| Leaf | Required | Notes |
|---|---|---|
| 454 iPhone | `state` (new/used) | `model` has 41 values, `internal_memory` is optional |
| 185 Strollers | `state` | |
| 414 Tools | `state`, `equipment_type` (1=professional, 2=non-professional) | Codes can be opaque numbers: the agent must use `code`, not `label` |
| 1679 Tyres | none | 17 optional attributes; the condition attribute is `condition` (not `state`) and has 4 values |

- **Delivery is an attribute**: `delivery_paid_by` (multi-value: `207` buyer, `208` seller, `209` in-person handover). Map it from `profile.json`'s delivery preferences.
- The attribute for condition differs by category (`state` vs `condition`), so it can't be hardcoded.

### Locations
- `/locations?latitude=&longitude=` returns city + district candidates (Sofia center → city 8771, district 1585 "Център"). That's an easy way to set the profile's default location.
- `/regions` and `/cities/{id}/districts` work with the client token.

### Validation (`POST /adverts`)
- Errors are structured: `error.validation[] = {field, title (BG), detail (EN)}`, where `field` is a dotted path (`contact.name`, `location.city_id`). This maps straight onto card fields for the failed/resubmit flow (§4.6).
- **Description: 40–9000 characters** (confirmed).
- Always required: `title`, `description`, `category_id`, `advertiser_type`, `contact.name`, `location.city_id`.
- **Validation is staged.** Title length, required attributes (e.g. missing `state`) and invalid attribute values were **not reported** while a basic field (city) was missing. Titles of 1 and 300 characters both passed the first stage. So **title limits and attribute and price rules are still unconfirmed** and need a fully valid payload.

### Account
- `/users/me/account-balance` is all zeros, `/users/me/packets` is empty, and payment methods are `["account"]`. Whether a free ad on the test account can be posted with no packet is unknown until we post.
- `/paid-features` lists promotion bundles. Not needed for v1.

## Still open (needs a real post on the test account)

1. Title limits (spec guesses 16–150) and the price field shape/requirement (`price: {value, currency, negotiable?}`).
2. Whether attribute validation (required + allowed values) is enforced server-side.
3. The initial advert status after create (moderation? `new` → `active`?).
4. Free-ad quota/packet behaviour on an account with a zero balance.
5. **Images:** whether OLX copies `images[].url` to its CDN. Post with presigned S3 URLs, delete the S3 objects, then check whether the images still load. This needs the S3 bucket first.
6. Deactivate → delete flow (`advert_command(..., "deactivate")` then `delete_advert`).

Suggested approach: one post in 414 Tools or 185 Strollers (simple, with required attributes), `advertiser_type: private`, city 8771, a deliberately short title first to read the stage-2 errors, then the real post.

## Implications for the design

- Cache the category tree and per-leaf attributes on disk. They're static enough, and the agent's `get_category_attributes` tool can read from the cache.
- Code-level validation (§8) must cover the stage-2 rules itself, because OLX only reports them once the stage-1 fields pass. Otherwise a user fixes one error and immediately hits the next.
- Exclude `photos_limit == 0` categories.
- Use the field paths from validation errors to highlight the matching card field.
