"""
Sweepstakes Agent Configuration

Loads settings from .env files with proper precedence:
  sweepstakes/.env  (user's personal config — highest priority)
  <repo-root>/.env  (fallback)

All fields read from env vars at instantiation time via __post_init__,
not at import time, so re-reading .env always works.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


def _load_env():
    """Load .env from the sweepstakes directory, then fall back to repo root."""
    sweeps_env = Path(__file__).resolve().parent / ".env"
    root_env = Path(__file__).resolve().parent.parent / ".env"
    if sweeps_env.exists():
        load_dotenv(sweeps_env, override=True)
    if root_env.exists():
        load_dotenv(root_env, override=False)


_load_env()


def _env(key: str, default: str = "") -> str:
    """Read an env var at call time (not import time)."""
    return os.getenv(key, default)


# ─── Entrant profile ──────────────────────────────────────────


@dataclass
class EntrantProfile:
    """Personal information for sweepstakes entries.

    Values default to empty strings so the UI can show blanks.
    __post_init__ fills missing values from env vars.
    """

    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    date_of_birth: str = ""   # MM/DD/YYYY
    age: str = ""
    street_address: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    country: str = "US"
    instagram_handle: str = ""
    twitter_handle: str = ""
    facebook_name: str = ""

    def __post_init__(self):
        _map = {
            "first_name": "SWEEPS_FIRST_NAME",
            "last_name": "SWEEPS_LAST_NAME",
            "email": "SWEEPS_EMAIL",
            "phone": "SWEEPS_PHONE",
            "date_of_birth": "SWEEPS_DOB",
            "age": "SWEEPS_AGE",
            "street_address": "SWEEPS_ADDRESS",
            "city": "SWEEPS_CITY",
            "state": "SWEEPS_STATE",
            "zip_code": "SWEEPS_ZIP",
            "instagram_handle": "SWEEPS_INSTAGRAM",
            "twitter_handle": "SWEEPS_TWITTER",
            "facebook_name": "SWEEPS_FACEBOOK",
        }
        for attr, env_key in _map.items():
            if not getattr(self, attr):
                val = _env(env_key)
                if val:
                    setattr(self, attr, val)
        # Country — only override if still default "US" and env says otherwise
        if self.country == "US":
            env_country = _env("SWEEPS_COUNTRY", "US")
            if env_country:
                self.country = env_country

    def validate(self) -> list[str]:
        """Return list of missing *required* fields."""
        missing = []
        if not self.first_name:
            missing.append("first_name (SWEEPS_FIRST_NAME)")
        if not self.last_name:
            missing.append("last_name (SWEEPS_LAST_NAME)")
        if not self.email:
            missing.append("email (SWEEPS_EMAIL)")
        return missing

    def to_dict(self) -> dict:
        return {
            "first_name": self.first_name,
            "last_name": self.last_name,
            "email": self.email,
            "phone": self.phone,
            "date_of_birth": self.date_of_birth,
            "age": self.age,
            "street_address": self.street_address,
            "city": self.city,
            "state": self.state,
            "zip_code": self.zip_code,
            "country": self.country,
            "instagram_handle": self.instagram_handle,
            "twitter_handle": self.twitter_handle,
            "facebook_name": self.facebook_name,
        }

    def summary(self) -> str:
        d = self.to_dict()
        filled = {k: v for k, v in d.items() if v}
        return "\n".join(f"  {k}: {v}" for k, v in filled.items())

    @property
    def filled_count(self) -> int:
        return sum(1 for v in self.to_dict().values() if v)

    @property
    def total_fields(self) -> int:
        return len(self.to_dict())


# ─── Agent config ─────────────────────────────────────────────


@dataclass
class SweepstakesConfig:
    """Global configuration for the sweepstakes agent."""

    max_entries_per_run: int = 5
    min_age_requirement: int = 18
    eligible_country: str = "US"
    categories_of_interest: list[str] = field(default_factory=list)

    aggregator_sites: list[str] = field(default_factory=lambda: [
        "https://www.sweetiessweeps.com",
        "https://www.sweepstakesadvantage.com",
        "https://www.online-sweepstakes.com",
        "https://www.sweepstakesbible.com",
        "https://www.sweepstakesmag.com",
        "https://www.contestgirl.com",
        "https://www.sweepstakesfanatics.com",
        "https://www.ilovegiveaways.com",
    ])

    scam_red_flags: list[str] = field(default_factory=lambda: [
        "credit card", "payment required", "buy now", "purchase necessary",
        "processing fee", "shipping fee", "pay to enter", "subscription required",
        "wire transfer", "social security", "SSN", "bank account", "routing number",
        "you've already won", "claim your prize now", "act immediately",
        "send money", "advance fee", "foreign lottery", "guaranteed winner",
        "you are selected", "tax payment upfront",
    ])

    trusted_sponsors: list[str] = field(default_factory=lambda: [
        "coca-cola", "pepsi", "amazon", "walmart", "target", "costco",
        "nike", "apple", "samsung", "microsoft", "google", "hgtv",
        "food network", "nbc", "abc", "cbs", "hershey", "nestle",
        "procter & gamble", "unilever", "kraft", "general mills",
        "kellogg", "disney", "sony", "lg", "hp", "dell",
        "starbucks", "mcdonald", "subway", "chipotle",
    ])

    entry_log_path: str = "sweepstakes_entries.json"
    llm_model: str = "claude-sonnet-4-6"
    demo_mode: bool = True
    headless: bool = False
    min_safety_score: int = 40

    def __post_init__(self):
        _map = {
            "SWEEPS_MAX_ENTRIES": ("max_entries_per_run", int),
            "SWEEPS_COUNTRY": ("eligible_country", str),
            "SWEEPS_LOG_PATH": ("entry_log_path", str),
            "SWEEPS_LLM_MODEL": ("llm_model", str),
        }
        for env_key, (attr, typ) in _map.items():
            val = _env(env_key)
            if val:
                setattr(self, attr, typ(val))

        env_demo = _env("SWEEPS_DEMO_MODE")
        if env_demo:
            self.demo_mode = env_demo.lower() == "true"
        env_headless = _env("SWEEPS_HEADLESS")
        if env_headless:
            self.headless = env_headless.lower() == "true"
