"""Run the API server: ``python -m teamboard``."""

import uvicorn

from teamboard.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "teamboard.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        # Logging is configured by the application (JSON); access logs come from
        # RequestContextMiddleware, which knows the tenant.
        log_config=None,
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
    )


if __name__ == "__main__":
    main()
