# olx-auto

Take a few photos on your phone → an AI agent drafts a complete [olx.bg](https://www.olx.bg) listing → you review, tweak and submit.

> **Status:** v1 works end to end: phone photos → AI draft (vision, web research, agent) → review on the listing card → live OLX advert, with style learning and on-demand price comparables from olx.bg. Left: autostart, model comparison, and a banner for an expired OLX connection. See the [work order](docs/SPEC.md#12-work-order).

- [docs/GUIDE.md](docs/GUIDE.md): how the app works, in plain language with diagrams
- [docs/SPEC.md](docs/SPEC.md): what we're building and why
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): how the code is organised
- [docs/spike-notes.md](docs/spike-notes.md): what we learned about the OLX API

## What you need

- A Mac (or any machine) with [uv](https://docs.astral.sh/uv/) (`brew install uv`). uv installs Python and all dependencies for you.
- An **OLX partner API application** from [developer.olx.bg](https://developer.olx.bg), for its `client_id` and `client_secret`.
- Optional: [Tailscale](https://tailscale.com) on the Mac and your phone, to use the app from the phone.

## First-time setup

```sh
git clone https://github.com/hsavovski/olx-auto.git
cd olx-auto
uv sync                                  # installs everything into .venv
cp .env.example .env                     # then put your OLX client_id / client_secret in it
```

### Create a user

Each app user is linked to one OLX account. For example, use `ico` for your real account and `ico-test` for a test account.

```sh
uv run olx-auto add-user ico-test
```

You'll be asked for a password. This is the password for **this app's login page**, not your OLX password, and it never leaves your machine. Forgot it? Run `uv run olx-auto set-password ico-test`.

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

### Use it from your phone (Tailscale)

With the app running:

```sh
tailscale serve --bg 8000
```

The app is now at `https://<your-mac-name>.<your-tailnet>.ts.net` on every device in your tailnet, but not on the public internet. Add it to your phone's home screen. To stop sharing: `tailscale serve reset`.

To reach it from any device without Tailscale, use `tailscale funnel --bg 8000` instead (Funnel must be allowed for the machine in the tailnet policy). The same URL is then public, and the login is all that protects it: use strong passwords. `tailscale funnel reset` takes it off the internet.

To have OLX redirect straight back to the app after Connect OLX, register `https://<your-mac-name>.<your-tailnet>.ts.net/olx/callback` as the redirect URI in your OLX app settings and put the same URL in `.env` as `redirect_uri=`.

## Commands

| Command | What it does |
|---|---|
| `uv run olx-auto serve` | Run the web app |
| `uv run olx-auto add-user NAME` | Create an app user |
| `uv run olx-auto set-password NAME` | Change a user's app password |
| `uv run olx-auto login NAME` | Connect an OLX account from the terminal |
| `uv run olx-auto check NAME` | Test the OLX connection (read-only) |

## Your data

Everything the app stores is in `data/`: users, OLX connections, listings, photos. It never goes to git. Photos are stripped of location (GPS) data on upload. To back up, copy `data/`. To delete a user, delete their folder.

## Development

```sh
uv run pytest            # tests (no network: OLX is mocked)
uv run ruff check .      # lint
```

CI runs both plus a secret scan on every push. OLX has **no sandbox**, so creating an advert through the API publishes it. Do real test posts only with the test account, and delete them afterwards.
