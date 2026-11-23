# olx-auto

Take a few photos on your phone → an AI agent drafts a complete olx.bg listing → review, tweak, submit.

Spec and design decisions: [docs/SPEC.md](docs/SPEC.md).

## Setup

Requires [uv](https://docs.astral.sh/uv/) and an OLX partner API application.

```sh
uv sync
cp .env.example .env            # fill in OLX client_id / client_secret
uv run olx-auto add-user ico    # prompts for a password
uv run olx-auto serve           # http://127.0.0.1:8000
```

Then log in and open **OLX → Connect OLX** to link the user's OLX account (or `uv run olx-auto login ico`).
Edit `data/users/<name>/profile.json` and `style_guide.md` to set contact details, location and listing style.

### Phone access via Tailscale

```sh
tailscale serve --bg 8000       # https://<machine>.<tailnet>.ts.net
```

## Development

```sh
uv run pytest
uv run ruff check .
```

All runtime data (users, tokens, listings, photos) lives in `data/` and is gitignored.
