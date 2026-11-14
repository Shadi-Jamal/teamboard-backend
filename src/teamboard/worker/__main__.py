from teamboard.config import get_settings
from teamboard.logging_config import configure_logging
from teamboard.worker.jobs import create_worker


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    create_worker(settings).run()


if __name__ == "__main__":
    main()
