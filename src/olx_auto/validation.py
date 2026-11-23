"""Draft checks (spec §8): OLX content rules, lengths, attributes.

The agent's `submit_draft` tool runs `check_draft` and hands problems back to the model. The
submit-time checks (location, contact, photo count) belong here too once submit exists.
"""
import re

TITLE_MIN, TITLE_MAX = 16, 150  # not enforced by OLX (spike-notes.md); our own limit
DESCRIPTION_MIN, DESCRIPTION_MAX = 40, 9000

PHONE_RE = re.compile(r"\+?\d(?:[\s\-/.()]*\d){7,}")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
URL_RE = re.compile(r"https?://|www\.|\b[\w-]+\.(?:com|bg|net|org|eu|info)\b", re.IGNORECASE)
REPEATED_PUNCT_RE = re.compile(r"([!?.,:;*\-_=~])\1{2,}")


def content_problems(name: str, text: str) -> list[str]:
    problems = []
    if PHONE_RE.search(text):
        problems.append(f"{name}: contains something that looks like a phone number")
    if EMAIL_RE.search(text):
        problems.append(f"{name}: contains an email address")
    if URL_RE.search(text):
        problems.append(f"{name}: contains a link or domain")
    letters = [c for c in text if c.isalpha()]
    if len(letters) >= 10 and sum(c.isupper() for c in letters) > len(letters) / 2:
        problems.append(f"{name}: more than half of the letters are capitals")
    if m := REPEATED_PUNCT_RE.search(text):
        problems.append(f"{name}: punctuation repeated 3+ times ({m.group(0)!r})")
    return problems


def attribute_problems(values: dict, attributes: list[dict]) -> list[str]:
    """`values` is {code: value or [values]}; `attributes` is the OLX /categories/{id}/attributes data."""
    problems = []
    known = {a["code"]: a for a in attributes}
    for code in values:
        if code not in known:
            problems.append(f"attributes.{code}: not an attribute of this category")
    for code, attr in known.items():
        rules = attr.get("validation") or {}
        value = values.get(code)
        given = [str(v) for v in (value if isinstance(value, list) else [value])
                 if v not in (None, "")]
        if not given:
            if rules.get("required"):
                problems.append(f"attributes.{code}: required ({attr.get('label', code)})")
            continue
        if len(given) > 1 and not rules.get("allow_multiple_values"):
            problems.append(f"attributes.{code}: takes a single value")
        allowed = {str(v["code"]) for v in attr.get("values") or []}
        for v in given:
            if allowed and v not in allowed:
                problems.append(f"attributes.{code}: {v!r} is not an allowed value code")
            elif rules.get("numeric"):
                try:
                    n = float(v)
                except ValueError:
                    problems.append(f"attributes.{code}: must be a number")
                    continue
                lo, hi = rules.get("min"), rules.get("max")
                if (lo is not None and n < lo) or (hi is not None and n > hi):
                    problems.append(f"attributes.{code}: must be between {lo} and {hi}")
    return problems


def check_draft(draft: dict, attributes: list[dict] | None) -> list[str]:
    problems = []
    title = draft.get("title") or ""
    description = draft.get("description") or ""
    if not TITLE_MIN <= len(title) <= TITLE_MAX:
        problems.append(f"title: {len(title)} characters, must be {TITLE_MIN}-{TITLE_MAX}")
    if not DESCRIPTION_MIN <= len(description) <= DESCRIPTION_MAX:
        problems.append(f"description: {len(description)} characters, "
                        f"must be {DESCRIPTION_MIN}-{DESCRIPTION_MAX}")
    problems += content_problems("title", title)
    problems += content_problems("description", description)
    price = draft.get("price")
    if not isinstance(price, int | float) or price <= 0:
        problems.append("price: must be a positive number of EUR")
    if not isinstance(draft.get("category_id"), int):
        problems.append("category_id: missing")
    elif attributes is not None:
        problems += attribute_problems(draft.get("attributes") or {}, attributes)
    return problems
