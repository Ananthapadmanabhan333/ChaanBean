"""Scheduler entry point.

    python -m app.worker

Separate process from the API on purpose: they scale differently, and a deploy
of one should not sever the other halfway through a dispatch.
"""

from __future__ import annotations

import logging

from app.comms.base import build_providers
from app.config import settings
from app.db import SessionLocal
from app.scheduler.tick import run_forever
from app.storage import build_storage
from app.telephony.ari import AriClient
from app.telephony.staging import LocalStager
from app.tts import build_tts


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s %(message)s"
    )

    kwargs = {
        "stager": LocalStager(settings.asterisk_sounds_dir),
        "storage": build_storage(),
        "tts": build_tts(),
        "providers": build_providers(),
    }
    if settings.telephony_backend == "asterisk":
        kwargs["ari_client"] = AriClient()

    if settings.redis_url:
        import redis as redis_lib

        from app.scheduler.throttle import Throttle

        client = redis_lib.from_url(settings.redis_url)
        kwargs["redis_client"] = client
        # Only throttle when there is a real carrier to protect. Against the stub
        # it would just slow the demo down for no benefit.
        if settings.telephony_backend == "asterisk":
            kwargs["throttle"] = Throttle(client)

    run_forever(SessionLocal, **kwargs)


if __name__ == "__main__":
    main()
