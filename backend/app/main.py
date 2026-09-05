"""The application.

One deployable: API, and the scheduler run as a separate process against the
same code. The voice worker is the one thing that would be extracted first, when
real-time media needs a different scaling curve from CRUD.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from sqlalchemy import text

from app.api.routes import router
from app.config import settings
from app.db import engine

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
)

app = FastAPI(
    title="ChaanBean",
    version="0.1.0",
    description=(
        "Outbound credit recovery for the Indian market. One-way, "
        "template-driven contact with a deterministic escalation ladder."
    ),
)

app.add_middleware(
    CORSMiddleware,
    # The portal is served from this same origin. Widen deliberately, per
    # environment, rather than shipping a wildcard that nobody revisits.
    allow_origins=["http://localhost:8000", "http://127.0.0.1:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/health")
def health():
    """Liveness plus the two facts an operator actually wants."""
    checks = {"database": False, "redis": False}
    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
        checks["database"] = True
    except Exception:
        pass
    try:
        import redis as redis_lib

        redis_lib.from_url(settings.redis_url).ping()
        checks["redis"] = True
    except Exception:
        pass

    return {
        "status": "ok" if all(checks.values()) else "degraded",
        "env": settings.env,
        "checks": checks,
        "backends": {
            "tts": settings.tts_backend,
            "storage": settings.storage_backend,
            "telephony": settings.telephony_backend,
            "sms": settings.sms_backend,
        },
        "allowlist_enforced": settings.enforce_allowlist,
    }


@app.get("/", response_class=HTMLResponse)
def portal():
    page = Path(__file__).parent / "static" / "portal.html"
    if not page.exists():
        return HTMLResponse("<h1>Portal not built</h1>", status_code=404)
    return HTMLResponse(page.read_text(encoding="utf-8"))
