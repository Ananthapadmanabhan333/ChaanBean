"""Vercel serverless entrypoint.

Vercel's Python runtime looks for an ASGI `app` in a module under `api/`. The
real application lives in `backend/`, so that directory goes on the path here
rather than the package being restructured to suit one host.

**This entrypoint serves the API and the portal only.** Two parts of ChaanBean
cannot run on a serverless platform, and pretending otherwise would produce a
deployment that looks healthy while doing nothing:

* `app/worker.py` is a tick loop that claims due targets with
  `SELECT … FOR UPDATE SKIP LOCKED`. Serverless functions are request-scoped, so
  nothing drives the escalation ladder — no campaign advances and no call is
  placed.
* `app/telephony/events.py` holds a persistent websocket to Asterisk. Call
  events are the source of truth for call state, so without it `Call` rows are
  never projected.

Both need a host that runs a process. See docs/deployment.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

# backend/ holds the `app` package.
_BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.main import app  # noqa: E402  (path must be set first)

__all__ = ["app"]
