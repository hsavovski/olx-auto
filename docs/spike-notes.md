# OLX API spike — findings

Spec §12 step 1. Run on 2026-11-23 against the **test** OLX account (`.token.test.json`), which connected to the app without problems.
Reproduce with `uv run python spikes/olx_spike.py`. Raw responses are written to `spikes/out/` (gitignored, because they contain account data).

Status: **done** (2026-11-23). Read-only probes, a real post without images, and a post with S3 images (`spikes/olx_image_spike.py`). The leftover questions at the end are non-blocking.

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
- **Title length is not enforced by the API**: a 1-character and a 303-character title were both accepted. Our own validation must enforce sensible limits. The web form (edit page, checked 2026-11-23) sets `minlength=16` on the title but no `maxlength`, so the upper bound, if any, is JS-only and still unverified; the spec's 150 stays our own rule.
- Web form required fields: `title`, `description`, `parameters.price.price`, `person` (contact name). `email` and `phone` are optional. `city_id` isn't marked `required` in the HTML but is required by the API. The description has no HTML length attributes (its 40–9000 comes from the API). Category attributes are custom dropdowns, not `<select>`s, so their codes come only from `/categories/{id}/attributes`.

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
- **`limited` = free quota used up in that category.** The third post in 185 Strollers on the same day was created as `limited` straight away. `activate` → 400 "Достигна лимита на активни обяви в тази категория. За да активираш повече обяви, купи един от нашите пакети." The two earlier posts counted toward the limit **even though they had been deleted** (they never reached `active`). The app must show `limited` as "posted, needs payment on OLX" with the advert link, not as a failure. Test posts burn the quota, so spread them across categories.
- **Cleanup:** `deactivate` only works on `active` adverts (400 "Обявата трябва да бъде активна"). For `new` adverts, `DELETE /adverts/{id}` works directly. It returns immediately, the advert disappears from the list within seconds, and `GET` then returns 404. The dev cleanup command should be: deactivate if `active`, then delete.

### Images (S3 → OLX)
Ran `spikes/olx_image_spike.py`: two JPEGs uploaded to S3, posted with 7-day presigned URLs in `images[].url`.
- **OLX copies images at create time, synchronously.** The create response already lists `https://frankfurt.apollo.olxcdn.com:443/v1/files/<id>-BG/image;s=800x600`. After deleting the S3 objects (presigned URLs → 404), both OLX CDN URLs still returned 200.
- So S3 only has to serve the images for the few seconds of the create call. Retention can be **minimal**: delete the objects right after a successful create. Keep the 30-day lifecycle only as a safety net for failed submits, and presigned URLs can be short-lived (e.g. 1 hour instead of 7 days).
- The CDN URLs still returned 200 15 s after the advert was deleted (probably cached; not our concern).
- **Presigning gotcha:** a default `boto3.client("s3")` presigns against the global endpoint, and S3 rejects that with 400 `AuthorizationQueryParametersError ... expecting 'us-east-1'` for the `eu-central-1` bucket. Use `Config(signature_version="s3v4", s3={"addressing_style": "virtual"})` plus `region_name`, which gives `https://<bucket>.s3.eu-central-1.amazonaws.com/...`.

### S3 setup
`infra/s3-setup.sh` (idempotent) creates bucket `olx-auto-<account-id>` in `eu-central-1` with all public access blocked and a 30-day expiry, plus IAM user `olx-auto` with one inline policy: Put/Get/DeleteObject on the bucket's objects and ListBucket on the bucket. No groups, no managed policies, no console login. Verified with the app's key: bucket object operations work; ListBuckets, DeleteBucket, PutBucketPolicy, lifecycle changes, IAM and EC2 are denied, and a direct public object URL returns 403.

## Still open (non-blocking)

1. Moderation time from `new` to `active`, and what a rejection looks like. It didn't resolve within 3+ min, and the image post went straight to `limited`. This will show up on the first real post.
2. Free-ad quota per category (how many, over what period). Only matters for volume.
3. Title limits in the OLX web form (the API doesn't enforce any).

## Implications for the design

- Cache the category tree and per-leaf attributes on disk. They're static enough, and the agent's `get_category_attributes` tool can read from the cache.
- Code-level validation (§8) must cover the title length itself (the API doesn't), and should catch required attributes and price before submit so that users don't run into OLX's staged errors one at a time.
- Map error fields: `title`, `description`, `category_id`, `contact.*`, `location.*` directly; `params.price[0]` → price; `params.<code>` → attribute `<code>`.
- Listing status after submit: `posted (pending moderation)` for `disabled`/`new`; `posted, needs payment` for `limited`; `posted` + link for `active`.
- Submit flow: upload photos to S3 → presign (short expiry) → create advert → on success delete the S3 objects. Store OLX's CDN image URLs in `listing.json`.
- Exclude `photos_limit == 0` categories.
