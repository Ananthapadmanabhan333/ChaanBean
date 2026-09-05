"""Runtime configuration.

A handful of settings decide whether this process talks to real vendors or to
local stand-ins: `tts_backend`, `storage_backend`, `telephony_backend`, and the
three messaging backends. Everything else is identical in both modes. Going live
is flipping those values and supplying credentials — that is the entire point of
the adapter seams, and it is why no vendor account blocks the build.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    env: Literal["local", "staging", "production"] = "local"
    debug: bool = True

    # --- data -------------------------------------------------------------
    # Three roles, deliberately. RLS is bypassed unconditionally by superusers
    # and by BYPASSRLS roles *even with FORCE*, so the application must not
    # connect as the owner or as a superuser — otherwise every policy is
    # decoration and the isolation tests pass against nothing.
    #
    #   crp_app     application. RLS enforced. Owns nothing.
    #   crp_worker  cross-tenant workers. BYPASSRLS, used deliberately and rarely.
    #   crp         schema owner / superuser. DDL and grants only.
    database_url: str = "postgresql+psycopg://crp_app:crp_app@localhost:5432/crp"
    database_url_worker: str | None = (
        "postgresql+psycopg://crp_worker:crp_worker@localhost:5432/crp"
    )
    database_url_admin: str = "postgresql+psycopg://crp:crp@localhost:5432/crp"
    app_db_role: str = "crp_app"
    app_db_password: str = "crp_app"
    worker_db_role: str = "crp_worker"
    worker_db_password: str = "crp_worker"
    redis_url: str = "redis://localhost:6379/0"

    # --- adapter selection ------------------------------------------------
    # "local"/"fake" need no vendor account.
    tts_backend: Literal["local", "polly"] = "local"
    storage_backend: Literal["local", "s3"] = "local"
    telephony_backend: Literal["fake", "asterisk"] = "fake"
    sms_backend: Literal["fake", "gupshup"] = "fake"
    whatsapp_backend: Literal["fake", "meta"] = "fake"
    email_backend: Literal["fake", "ses"] = "fake"

    # --- contact safety (rule 4) ------------------------------------------
    # Outside production, refuse to contact anyone not on this list. Twenty lines
    # that stop a dev environment from messaging the production debtor list.
    # `NoDecode` matters here. Without it pydantic-settings tries to JSON-decode
    # any list-typed field coming from a .env file, so the perfectly ordinary
    # `CONTACT_ALLOWLIST=` (or a comma-separated list) raises a JSONDecodeError
    # before the validator below ever runs — and the app fails to start.
    contact_allowlist: Annotated[list[str], NoDecode] = Field(default_factory=list)
    contact_allowlist_enforced: bool = True

    # --- audio
    audio_dir: str = "./var/audio"
    audio_sample_rate: int = 8000  # Asterisk sln8. Do not change casually.
    default_voice_id: str = "Kajal"  # Polly Indian-English neural voice
    asterisk_sounds_dir: str = "./var/asterisk-sounds"

    # --- AWS (unset until an account exists)
    aws_region: str = "ap-south-1"
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    s3_bucket: str | None = None

    # --- Asterisk ARI
    ari_base_url: str = "http://localhost:8088/ari"
    ari_username: str = "crp"
    ari_password: str = "crp"
    ari_app_name: str = "crp-outbound"
    # Endpoint template for origination. With a carrier trunk this becomes:
    #   PJSIP/{number}@vobiz-trunk
    ari_endpoint_template: str = "PJSIP/{number}@local-test"
    ari_originate_timeout_sec: int = 30

    # --- policy
    dnd_max_age_days: int = 7
    call_stuck_after_sec: int = 180  # orphan reconciler threshold
    scheduler_tick_seconds: int = 10
    scheduler_batch_size: int = 50

    # --- auth ---------------------------------------------------------------
    # Supabase is an *identity provider*, not the database. It answers "who is
    # this person"; this system still answers "which tenant are they in and what
    # may they do", because those live in our own tables behind our own RLS.
    #
    #   local     — bcrypt + JWTs this backend issues. No account needed.
    #   supabase  — accept Supabase-issued JWTs, mapped to our users by `sub`.
    auth_backend: Literal["local", "supabase"] = "local"

    supabase_url: str | None = None          # https://<ref>.supabase.co
    supabase_anon_key: str | None = None     # public; the browser uses it to log in
    # Legacy projects sign with a shared HS256 secret. Newer ones use asymmetric
    # keys published at /auth/v1/.well-known/jwks.json — both are supported, and
    # JWKS is preferred because the secret never leaves Supabase.
    supabase_jwt_secret: str | None = None
    supabase_jwks_ttl_seconds: int = 600

    jwt_secret: str = Field(default="dev-only-change-me")
    jwt_ttl_minutes: int = 720
    refresh_ttl_days: int = 30
    otp_ttl_seconds: int = 300
    otp_max_per_hour: int = 5

    @property
    def supabase_jwks_url(self) -> str | None:
        if not self.supabase_url:
            return None
        return f"{self.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"

    @property
    def supabase_ready(self) -> bool:
        """Configured enough to verify a token, by one route or the other."""
        return bool(self.supabase_url and (self.supabase_jwt_secret or self.supabase_url))

    @field_validator("contact_allowlist", mode="before")
    @classmethod
    def _split_allowlist(cls, v):
        """Accept a comma-separated string from the environment."""
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def has_live_delivery_backend(self) -> bool:
        """Could a message or call actually reach a person from this process?"""
        return (
            self.telephony_backend != "fake"
            or self.sms_backend != "fake"
            or self.whatsapp_backend != "fake"
            or self.email_backend != "fake"
        )

    @property
    def enforce_allowlist(self) -> bool:
        """Production contacts real debtors; everywhere else must not.

        Scoped to when contact is genuinely possible. With every delivery
        backend set to `fake` nothing leaves the building, so enforcing there
        would only mean a local stack refuses to demonstrate itself — and a
        guard that blocks the safe case teaches people to switch it off, which
        is how it comes to be off on the day it matters.

        The instant any real backend is configured — even in local — this turns
        back on, which is exactly the case rule 4 exists for: a development
        environment pointed at a live provider and the production debtor list.
        """
        if self.is_production:
            return False
        if not self.contact_allowlist_enforced:
            return False
        return self.env != "local" or self.has_live_delivery_backend

    @property
    def uses_real_vendors(self) -> bool:
        return self.tts_backend == "polly" or self.telephony_backend == "asterisk"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
