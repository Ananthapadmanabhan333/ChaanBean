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

from app.main import app as _app  # noqa: E402  (path must be set first)

_PREFIX = "/api/index"


async def app(scope, receive, send):
    """Restore the original request path before handing off to FastAPI.

    `vercel.json` rewrites every route to `/api/index` so that one function
    serves the whole surface. The rewrite is what the function actually
    receives, so FastAPI sees `/api/index` rather than `/health` or
    `/api/portal/state` and 404s on everything — the app boots perfectly and
    answers nothing, which is a confusing way to fail.

    Stripping the prefix here keeps the routing fix at the edge instead of
    reshaping every route to suit one host. It is a no-op on any platform that
    already passes the original path through, so nothing breaks locally.
    """
    if scope["type"] in ("http", "websocket"):
        path = scope.get("path", "")
        # Loop rather than strip once: a rewrite that matches its own output
        # would nest the prefix, and one leftover `/api/index` is the
        # difference between the portal and a 404.
        while path == _PREFIX or path.startswith(_PREFIX + "/"):
            path = path[len(_PREFIX):] or "/"
        if path != scope.get("path", ""):
            scope = dict(scope, path=path, raw_path=path.encode("utf-8"))
    await _app(scope, receive, send)


__all__ = ["app"]
