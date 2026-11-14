"""Worker entrypoint.

    python -m teamboard.worker            run the worker
    python -m teamboard.worker --check    exit 0 if a worker heartbeat is present

The ``--check`` mode is the container health check: a running worker refreshes a
heartbeat key in Redis every few seconds, and the key expires shortly after the
worker stops (or loses Redis).
"""

import asyncio
import sys

from arq.worker import async_check_health

from teamboard.config import get_settings
from teamboard.infra.redis import redis_settings
from teamboard.logging_config import configure_logging
from teamboard.worker.jobs import create_worker


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    settings = get_settings()
    if "--check" in arguments:
        return asyncio.run(async_check_health(redis_settings(settings)))

    configure_logging(settings.log_level)
    create_worker(settings).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
