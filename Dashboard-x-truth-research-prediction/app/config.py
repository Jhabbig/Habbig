import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

settings = {
    "TWITTER_BEARER_TOKEN": os.environ.get("TWITTER_BEARER_TOKEN", ""),
    "TWITTER_MONTHLY_QUOTA": int(os.environ.get("TWITTER_MONTHLY_QUOTA", "10000")),
    "TRUTHSOCIAL_USERNAME": os.environ.get("TRUTHSOCIAL_USERNAME", ""),
    "TRUTHSOCIAL_PASSWORD": os.environ.get("TRUTHSOCIAL_PASSWORD", ""),
    "TRUTHSOCIAL_ACCESS_TOKEN": os.environ.get("TRUTHSOCIAL_ACCESS_TOKEN", ""),
    "TRUTHSOCIAL_API_BASE_URL": os.environ.get("TRUTHSOCIAL_API_BASE_URL", "https://truthsocial.com"),
    "DASHBOARD_USER": os.environ.get("DASHBOARD_USER", ""),
    "DASHBOARD_PASSWORD": os.environ.get("DASHBOARD_PASSWORD", ""),
    "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
    "LLM_EXTRACTOR_MODEL": os.environ.get("LLM_EXTRACTOR_MODEL", "claude-opus-4-7"),
    "LLM_EXTRACTION_ENABLED": os.environ.get("LLM_EXTRACTION_ENABLED", "1") not in ("0", "false", "False", ""),
    "DATABASE_URL": os.environ.get("DATABASE_URL", f"sqlite+aiosqlite:///{BASE_DIR / 'predictions.db'}"),
    "LOG_LEVEL": os.environ.get("LOG_LEVEL", "INFO"),
}

_config_path = BASE_DIR / "config.yaml"
try:
    with open(_config_path) as f:
        yaml_config: dict = yaml.safe_load(f) or {}
except FileNotFoundError:
    yaml_config: dict = {}
