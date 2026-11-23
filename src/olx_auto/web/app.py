"""FastAPI app: login, listings (upload + view), Connect OLX settings."""
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .. import listings, users
from ..config import Settings
from ..olx import OlxError
from ..photos import InvalidPhoto

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
TEMPLATES.env.filters["usd"] = lambda v: f"${v:.2f}"
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
        return render(request, "home.html", user=user, listings=items,
                      total_cost=sum(listings.total_cost(i) for i in items),
                      olx_connected=user.olx(settings).connected, cost_of=listings.total_cost)

    @app.get("/listings/new")
    def new_listing_page(request: Request, user: CurrentUser):
        return render(request, "new_listing.html", user=user)

    @app.post("/listings")
    def create_listing(request: Request, user: CurrentUser,
                       photos: Annotated[list[UploadFile], File()],
                       note: Annotated[str, Form()] = ""):
        raw = [p.file.read() for p in photos if p.filename]
        try:
            listing = listings.create(user, note, raw)
        except (ValueError, InvalidPhoto) as e:
            return render(request, "new_listing.html", 400, user=user, error=str(e), note=note)
        return redirect(f"/listings/{listing['id']}")

    @app.get("/listings/{listing_id}")
    def listing_page(request: Request, user: CurrentUser, listing_id: str):
        try:
            listing = listings.load(user, listing_id)
        except KeyError:
            return render(request, "not_found.html", 404, user=user)
        return render(request, "listing.html", user=user, listing=listing,
                      cost=listings.total_cost(listing))

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
