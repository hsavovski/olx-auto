# OLX API spike — findings

Spec §12 step 1. Run on 2026-11-23 against the **test** OLX account (`.token.test.json`), which connected to the app without problems.
Reproduce with `uv run python spikes/olx_spike.py`. Raw responses are written to `spikes/out/` (gitignored, because they contain account data).

Status: **read-only probes and a real post without images done** (2026-11-23). Only the image question is still open; see the end of this file.

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
- **Validation is staged.** Category-specific rules (required attributes, price) are only reported once the basic fields pass.
- Stage 2 (on 185 Strollers with valid basics) reported `params.price[0]` "Въведи цена!" and `params.state` "Невалидна стойност". So **attribute values are enforced server-side** and **price is required**. Note the error paths: `params.<attr_code>` and `params.price[0]`, not `attributes`/`price`.
- **Title length is not enforced by the API**: a 1-character and a 303-character title were both accepted. Our own validation must enforce sensible limits. The web form's limits are unverified; the spec's 16–150 is a reasonable start, though shorter titles probably read better in search results.

### Account
- `/users/me/account-balance` is all zeros, `/users/me/packets` is empty, and payment methods are `["account"]`. A free ad was still accepted on the test account (category 185 Strollers).
- `/paid-features` lists promotion bundles. Not needed for v1.

### Real post (test account, no images)
Payload that works:

```json
{"title": "...", "description": "...", "category_id": 185, "advertiser_type": "private",
 "contact": {"name": "Тест"}, "location": {"city_id": 8771, "district_id": 1585},
 "price": {"value": 50, "currency": "EUR", "negotiable": false},
 "attributes": [{"code": "state", "value": "used"}]}
```

- Contact phone is optional. The response echoes `price` as `{value: "50" (string), currency, negotiable, budget, trade}`, plus `courier: false`, `valid_to` (+30 days) and the advert `url`.
- **Status lifecycle:** `disabled` straight after create → `new` after ~1 min (presumably awaiting moderation). It stayed `new` for 3+ minutes, then we deleted it. The public URL showed no advert while it was `disabled`. The app should treat `disabled`/`new` as "posted, pending moderation" and poll for `active` (or show `moderated`/`blocked` if it's rejected).
- `activate` on a `disabled` advert → 400 "Невалидно състояние".
- **Cleanup:** `deactivate` only works on `active` adverts (400 "Обявата трябва да бъде активна"). For `new` adverts, `DELETE /adverts/{id}` works directly. It returns immediately, the advert disappears from the list within seconds, and `GET` then returns 404. The dev cleanup command should be: deactivate if `active`, then delete.

## Still open

1. **Images:** whether OLX copies `images[].url` to its CDN. Strong hint that it does: the real account's existing advert (posted through the web) lists images as `https://frankfurt.apollo.olxcdn.com/v1/files/<id>-BG/image;s=800x600`. Confirm by posting with presigned S3 URLs, deleting the S3 objects, and checking whether the advert's images still load. This needs the S3 bucket.
2. Moderation time to reach `active`, and what a rejection looks like. This will show up naturally in the image post if we let it reach `active` before deleting.
3. Free-ad quota per category: unknown, and it only matters once posting volume grows.

## Implications for the design

- Cache the category tree and per-leaf attributes on disk. They're static enough, and the agent's `get_category_attributes` tool can read from the cache.
- Code-level validation (§8) must cover the title length itself (the API doesn't), and should catch required attributes and price before submit so that users don't run into OLX's staged errors one at a time.
- Map error fields: `title`, `description`, `category_id`, `contact.*`, `location.*` directly; `params.price[0]` → price; `params.<code>` → attribute `<code>`.
- Listing status after submit: `posted (pending moderation)` until OLX reports `active`.
- Exclude `photos_limit == 0` categories.
