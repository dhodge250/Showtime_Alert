"""Entry point for the IMAX Alert application."""
import logging
import os

from app import create_app
from app.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = create_app(os.environ.get("FLASK_ENV", "development"))


if __name__ == "__main__":
    try:
        start_scheduler(app)
        app.run(
            host=os.environ.get("HOST", "0.0.0.0"),
            port=int(os.environ.get("PORT", 5000)),
            debug=app.config.get("DEBUG", True),
            use_reloader=False,  # Avoid double-starting the scheduler
        )
    finally:
        stop_scheduler()
