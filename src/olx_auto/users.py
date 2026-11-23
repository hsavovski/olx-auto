"""App users: one folder per user under data/users/<name>/ (see docs/SPEC.md §10)."""
import hashlib
import hmac
import json
import re
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import ROOT, Settings
from .olx import Olx

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
CONTEXT_EXAMPLES = ROOT / "context"


@dataclass(frozen=True)
class User:
    name: str
    dir: Path

    @property
    def meta_file(self) -> Path:
        return self.dir / "user.json"

    @property
    def profile_file(self) -> Path:
        return self.dir / "profile.json"

    @property
    def style_guide_file(self) -> Path:
        return self.dir / "style_guide.md"

    @property
    def token_file(self) -> Path:
        return self.dir / "olx_token.json"

    @property
    def listings_dir(self) -> Path:
        return self.dir / "listings"

    @property
    def display_name(self) -> str:
        return json.loads(self.meta_file.read_text()).get("display_name") or self.name

    def olx(self, settings: Settings) -> Olx:
        return Olx(self.token_file, settings.olx_client_id, settings.olx_client_secret,
                   settings.olx_redirect_uri)


def _users_dir(data_dir: Path) -> Path:
    return data_dir / "users"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def _check_hash(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
    return hmac.compare_digest(digest.hex(), digest_hex)


def get_user(data_dir: Path, name: str) -> User | None:
    if not NAME_RE.match(name or ""):
        return None
    user = User(name, _users_dir(data_dir) / name)
    return user if user.meta_file.exists() else None


def list_users(data_dir: Path) -> list[User]:
    root = _users_dir(data_dir)
    if not root.exists():
        return []
    return [u for p in sorted(root.iterdir()) if (u := get_user(data_dir, p.name))]


def create_user(data_dir: Path, name: str, password: str, display_name: str | None = None) -> User:
    if not NAME_RE.match(name):
        raise ValueError("User name must be lowercase letters, digits, '-' or '_' (max 32 chars)")
    if get_user(data_dir, name):
        raise ValueError(f"User {name!r} already exists")
    user = User(name, _users_dir(data_dir) / name)
    user.listings_dir.mkdir(parents=True)
    user.dir.chmod(0o700)
    user.meta_file.write_text(json.dumps(
        {"display_name": display_name or name, "password": hash_password(password)}, indent=2))
    shutil.copy(CONTEXT_EXAMPLES / "profile.json.example", user.profile_file)
    shutil.copy(CONTEXT_EXAMPLES / "style_guide.md.example", user.style_guide_file)
    return user


def set_password(user: User, password: str) -> None:
    meta = json.loads(user.meta_file.read_text())
    meta["password"] = hash_password(password)
    user.meta_file.write_text(json.dumps(meta, indent=2))


def authenticate(data_dir: Path, name: str, password: str) -> User | None:
    user = get_user(data_dir, name)
    if not user:
        _check_hash(password, hash_password("timing-equaliser"))
        return None
    stored = json.loads(user.meta_file.read_text()).get("password", "")
    return user if _check_hash(password, stored) else None
