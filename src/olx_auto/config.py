"""Global settings: read from .env (repo root), overridable by environment variables."""
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_env(path: Path) -> dict[str, str]:
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
    return env


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    session_secret: str
    olx_client_id: str
    olx_client_secret: str
    olx_redirect_uri: str | None = None

    @classmethod
    def load(cls, env_file: Path = ROOT / ".env") -> "Settings":
        env = {**load_env(env_file), **os.environ}
        data_dir = Path(env.get("DATA_DIR", ROOT / "data"))
        return cls(
            data_dir=data_dir,
            session_secret=env.get("SESSION_SECRET") or _persistent_secret(data_dir),
            olx_client_id=env.get("client_id", ""),
            olx_client_secret=env.get("client_secret", ""),
            olx_redirect_uri=env.get("redirect_uri") or None,
        )


def _persistent_secret(data_dir: Path) -> str:
    """Generated once and kept in data/, so sessions survive restarts without manual setup."""
    path = data_dir / ".session_secret"
    if not path.exists():
        data_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_urlsafe(48))
        os.chmod(path, 0o600)
    return path.read_text().strip()
