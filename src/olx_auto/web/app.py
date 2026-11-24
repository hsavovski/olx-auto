"""FastAPI app: login, listings (upload, card: edit/lock/refine), style guide, Connect OLX settings."""
import json
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .. import drafting, listings, profiles, style, submitting, users, validation
from ..config import Settings
from ..olx import OlxError
from ..photos import InvalidPhoto

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
TEMPLATES.env.filters["usd"] = lambda v: f"${v:.2f}"
TEMPLATES.env.globals["olx_states"] = submitting.OLX_STATES
SESSION_MAX_AGE = 90 * 24 * 3600


class LoginRequired(Exception):
    pass


def settings_of(request: Request) -> Settings:
    return request.app.state.settings


def current_user(request: Request) -> users.User:
    name = request.session.get("user")
    user = users.get_user(settings_of(request).data_dir, name) if name else None
    if not user:
        raise LoginRequired
    return user


CurrentUser = Annotated[users.User, Depends(current_user)]


def render(request: Request, template: str, status_code: int = 200, **ctx) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, template, ctx, status_code=status_code)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret,
                       max_age=SESSION_MAX_AGE, same_site="lax")

    @app.exception_handler(LoginRequired)
    def _login_required(request: Request, exc: LoginRequired):
        return redirect("/login")

    # ---- auth ---------------------------------------------------------
    @app.get("/login")
    def login_page(request: Request):
        return render(request, "login.html")

    @app.post("/login")
    def login(request: Request, username: Annotated[str, Form()], password: Annotated[str, Form()]):
        user = users.authenticate(settings.data_dir, username.strip().lower(), password)
        if not user:
            time.sleep(1)  # slow down guessing
            return render(request, "login.html", 401, error="Wrong user name or password")
        request.session.clear()
        request.session["user"] = user.name
        return redirect("/")

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return redirect("/login")

    # ---- listings -----------------------------------------------------
    @app.get("/")
    def home(request: Request, user: CurrentUser):
        items = listings.list_all(user)
        olx = user.olx(settings)
        for item in items:  # posted adverts' OLX status; throttled, skips final states
            submitting.refresh_status(settings, user, item, olx=olx)
        return render(request, "home.html", user=user, listings=items,
                      total_cost=sum(listings.total_cost(i) for i in items),
                      olx_connected=olx.connected, cost_of=listings.total_cost)

    @app.get("/listings/new")
    def new_listing_page(request: Request, user: CurrentUser):
        return render(request, "new_listing.html", user=user)

    @app.post("/listings")
    def create_listing(request: Request, user: CurrentUser, background: BackgroundTasks,
                       photos: Annotated[list[UploadFile], File()],
                       note: Annotated[str, Form()] = ""):
        raw = [p.file.read() for p in photos if p.filename]
        try:
            listing = listings.create(user, note, raw)
        except (ValueError, InvalidPhoto) as e:
            return render(request, "new_listing.html", 400, user=user, error=str(e), note=note)
        if settings.openrouter_api_key:
            _start(background, drafting.generate, user, listing["id"])
        return redirect(f"/listings/{listing['id']}")

    def _start(background: BackgroundTasks, task, user: users.User, listing_id: str, **kwargs) -> None:
        """Mark the run as started now, so the page the user lands on already shows progress."""
        drafting.mark_started(user, listing_id)
        background.add_task(task, settings, user, listing_id, **kwargs)

    def _editable(request: Request, user: users.User, listing_id: str):
        """The listing, or a response: 404 for a foreign/unknown id, back to the card while a run is
        going or once it's posted."""
        try:
            listing = listings.load(user, listing_id)
        except KeyError:
            return None, render(request, "not_found.html", 404, user=user)
        if listings.is_running(listing) or submitting.frozen(listing):
            return None, redirect(f"/listings/{listing_id}")
        return listing, None

    @app.get("/listings/{listing_id}")
    def listing_page(request: Request, user: CurrentUser, listing_id: str):
        try:
            listing = listings.load(user, listing_id)
        except KeyError:
            return render(request, "not_found.html", 404, user=user)
        submitting.refresh_status(settings, user, listing)
        trace = None
        path = listings.trace_path(user, listing_id)
        if settings.show_trace and path.exists():
            trace = json.loads(path.read_text())["runs"]
        category = listing["category"] or {}
        profile = profiles.load(user)
        running = listings.is_running(listing)
        return render(request, "listing.html", user=user, listing=listing, draft=listing["draft"],
                      running=running, frozen=running or submitting.frozen(listing),
                      can_submit=submitting.can_submit(listing),
                      interrupted=submitting.interrupted(listing), category=category,
                      attributes=category.get("attributes") or [], profile=profile,
                      problems=validation.submit_problems(listing, profile),
                      cost=listings.total_cost(listing), trace=trace,
                      ai_enabled=bool(settings.openrouter_api_key))

    @app.get("/listings/{listing_id}/progress")
    def listing_progress(user: CurrentUser, listing_id: str):
        """Polled by the card while a run is going, so it can update in place instead of reloading."""
        try:
            listing = listings.load(user, listing_id)
        except KeyError:
            return JSONResponse({"error": "not found"}, 404)
        running = listings.is_running(listing)
        return {"running": running, "progress": listing["progress"] if running else None}

    @app.post("/listings/{listing_id}/edit")
    async def edit_listing(request: Request, user: CurrentUser, listing_id: str,
                           background: BackgroundTasks):
        listing, response = _editable(request, user, listing_id)
        if response:
            return response
        form = await request.form()
        changes = {"title": str(form.get("title", "")).strip(),
                   "description": str(form.get("description", "")).strip(),
                   "negotiable": form.get("negotiable") == "on"}
        try:
            changes["price"] = float(str(form.get("price", "")).replace(",", "."))
        except ValueError:
            pass  # an unparsable price keeps the old one
        else:
            changes["price"] = int(changes["price"]) if changes["price"].is_integer() else changes["price"]
        definitions = (listing["category"] or {}).get("attributes") or []
        if definitions:
            attrs = {}
            for a in definitions:
                values = [v for v in form.getlist(f"attr_{a['code']}") if v]
                if values:
                    multiple = (a.get("validation") or {}).get("allow_multiple_values")
                    attrs[a["code"]] = values if multiple else values[0]
            changes["attributes"] = attrs
        changed = listings.edit_draft(listing, changes)
        listing["problems"] = validation.check_draft(listing["draft"], definitions or None)
        listings.save(user, listing)
        if settings.openrouter_api_key and set(changed) & set(drafting.STYLE_FIELDS):
            _start(background, drafting.try_learn_style, user, listing_id)
        return redirect(f"/listings/{listing_id}")

    @app.post("/listings/{listing_id}/unlock/{field}")
    def unlock_field(request: Request, user: CurrentUser, listing_id: str, field: str):
        listing, response = _editable(request, user, listing_id)
        if response:
            return response
        if field in listing["locked"]:
            listing["locked"].remove(field)
            listings.save(user, listing)
        return redirect(f"/listings/{listing_id}")

    @app.post("/listings/{listing_id}/photos/{name}/delete")
    def delete_photo(request: Request, user: CurrentUser, listing_id: str, name: str):
        listing, response = _editable(request, user, listing_id)
        if response:
            return response
        try:
            listings.delete_photo(user, listing, name)
        except KeyError:
            return render(request, "not_found.html", 404, user=user)
        except ValueError:
            pass  # the last photo; the card hides its button
        return redirect(f"/listings/{listing_id}")

    @app.post("/listings/{listing_id}/submit")
    def submit_listing(request: Request, user: CurrentUser, listing_id: str):
        listing, response = _editable(request, user, listing_id)
        if response:
            return response
        try:
            submitting.submit(settings, user, listing_id)
        except ValueError:
            pass  # problems or status; the card shows why
        return redirect(f"/listings/{listing_id}")

    @app.post("/listings/{listing_id}/refine")
    async def refine_listing(request: Request, user: CurrentUser, listing_id: str,
                             background: BackgroundTasks):
        listing, response = _editable(request, user, listing_id)
        if response:
            return response
        form = await request.form()
        for q in listing["questions"]:
            answer = str(form.get(f"answer_{q['id']}", "")).strip()
            if answer:
                q["answer"] = answer
        listings.add_message(listing, str(form.get("message", "")))
        listings.save(user, listing)
        if settings.openrouter_api_key:
            _start(background, drafting.generate, user, listing_id)
        return redirect(f"/listings/{listing_id}")

    @app.post("/listings/{listing_id}/compare")
    def compare_prices(request: Request, user: CurrentUser, listing_id: str, background: BackgroundTasks):
        listing, response = _editable(request, user, listing_id)
        if response:
            return response
        if settings.openrouter_api_key and listing["draft"]:
            _start(background, drafting.generate, user, listing_id, compare=True)
        return redirect(f"/listings/{listing_id}")

    # ---- profile ----------------------------------------------------------
    def _profile_page(request: Request, user: users.User, status_code: int = 200, **ctx):
        return render(request, "profile.html", status_code, user=user, profile=profiles.load(user),
                      message=request.query_params.get("message"), **ctx)

    @app.get("/settings/profile")
    def profile_page(request: Request, user: CurrentUser, lat: float | None = None,
                     lng: float | None = None):
        if lat is None or lng is None:
            return _profile_page(request, user)
        try:
            choices = profiles.location_choices(user.olx(settings).locations(lat, lng))
        except (OlxError, OSError) as e:
            return _profile_page(request, user, location_error=f"OLX location lookup failed: {e}")
        return _profile_page(request, user, choices=choices, lat=lat, lng=lng)

    @app.post("/settings/profile")
    def profile_save(request: Request, user: CurrentUser,
                     display_name: Annotated[str, Form()] = "", name: Annotated[str, Form()] = "",
                     phone: Annotated[str, Form()] = "", email: Annotated[str, Form()] = "",
                     advertiser_type: Annotated[str, Form()] = "private",
                     courier: Annotated[bool, Form()] = False,
                     delivery_note: Annotated[str, Form()] = ""):
        profile = profiles.load(user)
        errors = profiles.update_details(profile, name=name, phone=phone, email=email,
                                         advertiser_type=advertiser_type, courier=courier,
                                         delivery_note=delivery_note)
        if errors:
            return _profile_page(request, user, 400, errors=errors)
        profiles.save(user, profile)
        users.set_display_name(user, display_name)
        return redirect("/settings/profile?message=Saved")

    @app.post("/settings/profile/location")
    def profile_location(request: Request, user: CurrentUser, lat: Annotated[float, Form()],
                         lng: Annotated[float, Form()], choice: Annotated[int, Form()]):
        # Look the point up again rather than trusting ids and names sent back by the browser
        olx = user.olx(settings)
        needs_district = False
        try:
            choices = profiles.location_choices(olx.locations(lat, lng))
            if 0 <= choice < len(choices) and choices[choice]["district_id"] is None:
                needs_district = bool(olx.districts(choices[choice]["city_id"]))
        except (OlxError, OSError) as e:
            return _profile_page(request, user, 502, location_error=f"OLX location lookup failed: {e}")
        if not 0 <= choice < len(choices):
            return _profile_page(request, user, 400, location_error="Pick one of the places")
        if needs_district:
            return _profile_page(request, user, 400, location_error="This city has districts "
                                 "(OLX needs one): pick a place with a district")
        profile = profiles.load(user)
        profiles.set_location(profile, **choices[choice])
        profiles.save(user, profile)
        return redirect("/settings/profile?message=Location+saved")

    @app.post("/settings/password")
    def password_change(request: Request, user: CurrentUser, current: Annotated[str, Form()],
                        new: Annotated[str, Form()], repeat: Annotated[str, Form()]):
        error = None
        if not users.authenticate(settings.data_dir, user.name, current):
            time.sleep(1)  # slow down guessing, as on /login
            error = "Current password is wrong"
        elif len(new) < 8:
            error = "New password must be at least 8 characters"
        elif new != repeat:
            error = "New passwords don't match"
        if error:
            return _profile_page(request, user, 400, password_error=error)
        users.set_password(user, new)
        return redirect("/settings/profile?message=Password+changed")

    # ---- style guide ------------------------------------------------------
    @app.get("/settings/style")
    def style_page(request: Request, user: CurrentUser):
        return render(request, "style.html", user=user, guide=style.read_guide(user),
                      log=style.log(user), message=request.query_params.get("message"))

    @app.post("/settings/style")
    def style_save(user: CurrentUser, guide: Annotated[str, Form()]):
        style.write_guide(user, guide, summary="Ръчна редакция", source="manual")
        return redirect("/settings/style?message=Saved")

    @app.post("/settings/style/revert/{backup}")
    def style_revert(request: Request, user: CurrentUser, backup: str):
        try:
            style.revert(user, backup)
        except KeyError:
            return render(request, "not_found.html", 404, user=user)
        return redirect("/settings/style?message=Reverted")

    @app.get("/listings/{listing_id}/photos/{name}")
    def listing_photo(user: CurrentUser, listing_id: str, name: str):
        try:
            return FileResponse(listings.photo_path(user, listing_id, name), media_type="image/jpeg")
        except KeyError:
            return HTMLResponse("Not found", 404)

    # ---- OLX connection -------------------------------------------------
    @app.get("/settings/olx")
    def olx_settings(request: Request, user: CurrentUser):
        olx = user.olx(settings)
        account, error = None, None
        if olx.connected:
            try:
                account = olx.me()
            except (OlxError, OSError, RuntimeError) as e:
                error = f"Connection is broken, reconnect: {e}"
        return render(request, "olx_settings.html", user=user, connected=olx.connected,
                      account=account, error=error, message=request.query_params.get("message"))

    @app.post("/settings/olx/connect")
    def olx_connect(request: Request, user: CurrentUser):
        state = secrets.token_urlsafe(16)
        request.session["olx_state"] = state
        return redirect(user.olx(settings).authorize_url(state=state))

    def _exchange(user: users.User, code: str) -> RedirectResponse:
        try:
            user.olx(settings).exchange_code(code)
        except (OlxError, OSError) as e:
            return redirect("/settings/olx?" + urllib.parse.urlencode({"message": f"Failed: {e}"}))
        return redirect("/settings/olx?message=Connected")

    @app.get("/olx/callback")
    def olx_callback(request: Request, user: CurrentUser, code: str = "", state: str = ""):
        expected = request.session.pop("olx_state", None)
        if not code or not expected or not secrets.compare_digest(state, expected):
            return redirect("/settings/olx?message=Invalid+OAuth+callback,+try+again")
        return _exchange(user, code)

    @app.post("/settings/olx/code")
    def olx_paste_code(user: CurrentUser, code: Annotated[str, Form()]):
        raw = code.strip()
        code = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query).get("code", [raw])[0]
        return _exchange(user, code)

    return app
