"""Global settings: read from .env (repo root), overridable by environment variables."""
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# OpenRouter model ids (spec §5); override with VISION_MODEL / AGENT_MODEL to compare models
DEFAULT_VISION_MODEL = "google/gemini-3.5-flash-lite"
DEFAULT_AGENT_MODEL = "anthropic/claude-sonnet-5.5"


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
    openrouter_api_key: str = ""
    vision_model: str = DEFAULT_VISION_MODEL
    agent_model: str = DEFAULT_AGENT_MODEL
    agent_max_iterations: int = 15
    agent_max_cost: float = 0.50  # USD per agent run
    show_trace: bool = False

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
            openrouter_api_key=env.get("OPENROUTER_API_KEY", ""),
            vision_model=env.get("VISION_MODEL") or DEFAULT_VISION_MODEL,
            agent_model=env.get("AGENT_MODEL") or DEFAULT_AGENT_MODEL,
            agent_max_iterations=int(env.get("AGENT_MAX_ITERATIONS") or 15),
            agent_max_cost=float(env.get("AGENT_MAX_COST") or 0.50),
            show_trace=env.get("SHOW_TRACE", "").lower() in ("1", "true", "yes"),
        )


def _persistent_secret(data_dir: Path) -> str:
    """Generated once and kept in data/, so sessions survive restarts without manual setup."""
    path = data_dir / ".session_secret"
    if not path.exists():
        data_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_urlsafe(48))
        os.chmod(path, 0o600)
    return path.read_text().strip()
