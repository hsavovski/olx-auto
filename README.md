# olx-auto

Take a few photos on your phone → an AI agent drafts a complete [olx.bg](https://www.olx.bg) listing → you review, tweak and submit.

It's built for **olx.bg only**: listings are written in Bulgarian, prices are in EUR, and it posts through the OLX Bulgaria partner API. It's a small self-hosted app for a handful of people (one app user per OLX account), not a multi-tenant service.

> **Status:** works end to end: phone photos → AI draft (vision, web research, agent) → review on the listing card → live OLX advert, with style learning and on-demand price comparables from olx.bg. Not built yet: a warning when the OLX connection has expired. See the [work order](docs/SPEC.md#12-work-order).

- [docs/GUIDE.md](docs/GUIDE.md): how the app works, in plain language with diagrams
- [docs/SPEC.md](docs/SPEC.md): what we're building and why
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): how the code is organised
- [docs/spike-notes.md](docs/spike-notes.md): what we learned about the OLX API

## What you need

- A computer to run it on, with [uv](https://docs.astral.sh/uv/getting-started/installation/). uv installs Python and all dependencies for you. Or [Docker](#run-it-with-docker) instead.
- An **OLX partner API application** from [developer.olx.bg](https://developer.olx.bg), for its `client_id` and `client_secret`.
- An **[OpenRouter](https://openrouter.ai/keys) API key** with some credit. It pays for the AI models and their web search. A listing usually costs a few US cents; each agent run is capped by `AGENT_MAX_COST` ($0.50 by default).
- To submit adverts: an **AWS S3 bucket**. OLX only accepts photos by URL, so they go to a private bucket for a few seconds during Submit. [`infra/s3-setup.sh`](infra/s3-setup.sh) creates the bucket and a bucket-only access key with the AWS CLI. Drafting works without it.
- Optional: [Tailscale](https://tailscale.com), to use the app from your phone.

## First-time setup

```sh
git clone https://github.com/hsavovski/olx-auto.git
cd olx-auto
uv sync                                  # installs everything into .venv
cp .env.example .env                     # then fill in client_id, client_secret, OPENROUTER_API_KEY
infra/s3-setup.sh                        # optional, needed for Submit: adds the S3 settings to .env
```

`.env.example` lists every setting with a comment, including the model choices.

### Create a user

Each app user is linked to one OLX account. For example, `maria` for your real account and `maria-test` for a test account to try posting with.

```sh
uv run olx-auto add-user maria-test
```

You'll be asked for a password. This is the password for **this app's login page**, not your OLX password, and it never leaves your machine. Forgot it? Run `uv run olx-auto set-password maria-test`.

Each user gets a folder at `data/users/<name>/`. Edit two files in it:

- `profile.json`: your contact name, phone and email, default location, and delivery preferences. The location is set with OLX ids: Sofia center is `"city_id": 8771, "district_id": 1585`.
- `style_guide.md`: how your listings should read, in Bulgarian. It starts from a sensible default.

## Run it

```sh
uv run olx-auto serve
```

Open http://127.0.0.1:8000 and log in with the user name and password you just created.

Stop it with `Ctrl+C`. Use `--port 9000` to pick another port.

### Connect the OLX account

In the app, open **OLX** (top right) → **Connect OLX**.

1. **First, log in on olx.bg as the account this user should post to.** OLX connects whichever account is logged in. For a test account, use a private/incognito window.
2. Approve access on OLX.
3. OLX then redirects you somewhere:
   - If it lands back on the app, you're done.
   - Otherwise, copy the full URL from the address bar (the page doesn't need to load) and paste it into **"Or paste the redirect URL / code"**.

The OLX page then shows **"Connected as …"**, so check that it's the right account. From the terminal, `uv run olx-auto login <user>` does the same thing, and `uv run olx-auto check <user>` tests the connection.

The connection lasts as long as you use the app at least about once a month. If it was never connected, the home page shows a banner. If it has expired, OLX calls (such as Submit) fail with an error and the OLX page says the connection is broken. Just connect again.

### Run it with Docker

To keep the app running in the background and have it start again whenever Docker starts. Do the [first-time setup](#first-time-setup) first, so `.env` and `data/` exist before the first start (the `uv sync` step isn't needed).

```sh
docker compose up -d --build             # build and start; again after every git pull
docker compose logs -f                   # watch it
docker compose down                      # stop it
```

It's at `http://127.0.0.1:8000`, the same as `uv run olx-auto serve`, and uses the same `data/` folder and `.env` (restart it after editing `.env`: `docker compose restart`). Run only one of the two at a time. While the container runs, use commands through it, e.g. `docker compose exec -it app olx-auto add-user <name>`: OLX refresh tokens rotate, so the container and a separate `uv run` process refreshing the same token can lock the account out.

The app in the container runs as user id 1000. On Linux, `data/` must be writable by that id (`sudo chown -R 1000 data`); Docker Desktop on macOS and Windows handles this for you.

### Use it from your phone (Tailscale)

With Tailscale on the computer running the app and on your phone, and the app running:

```sh
tailscale serve --bg 8000
```

The app is now at `https://<machine-name>.<your-tailnet>.ts.net` on every device in your tailnet, but not on the public internet. Add it to your phone's home screen. To stop sharing: `tailscale serve reset`.

To reach it from any device without Tailscale, use `tailscale funnel --bg 8000` instead (Funnel must be allowed for the machine in the tailnet policy). The same URL is then public, and the login is all that protects it: use strong passwords. `tailscale funnel reset` takes it off the internet.

To have OLX redirect straight back to the app after Connect OLX, register `https://<machine-name>.<your-tailnet>.ts.net/olx/callback` as the redirect URI in your OLX app settings and put the same URL in `.env` as `redirect_uri=`.

## Commands

| Command | What it does |
|---|---|
| `uv run olx-auto serve` | Run the web app |
| `uv run olx-auto add-user NAME` | Create an app user |
| `uv run olx-auto set-password NAME` | Change a user's app password |
| `uv run olx-auto login NAME` | Connect an OLX account from the terminal |
| `uv run olx-auto check NAME` | Test the OLX connection (read-only) |
| `uv run olx-auto draft NAME LISTING_ID [-m TEXT]` | Rerun the AI on a listing, optionally with a message |
| `uv run olx-auto remove-advert NAME LISTING_ID` | Take a listing's advert off OLX (for test posts) |

## Your data

Everything the app stores is in `data/`: users, OLX connections, listings, photos. It never goes to git. Photos are stripped of location (GPS) data on upload. To back up, copy `data/`. To delete a user, delete their folder.

## Development

```sh
uv run pytest            # tests (no network: OLX is mocked)
uv run ruff check .      # lint
```

CI runs both plus a secret scan on every push. OLX has **no sandbox**, so creating an advert through the API publishes it. Do real test posts only with the test account, and delete them afterwards.
