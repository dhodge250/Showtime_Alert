"""Gunicorn entry point for production deployment."""
import logging
import os
import sys

from app import create_app
from app.scheduler import start_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

try:
    app = create_app(os.environ.get("FLASK_ENV", "production"))
except RuntimeError as exc:
    # create_app() refuses to boot in production with the default SECRET_KEY.
    print(f"ERROR: {exc}")
    print("Generate one with:")
    print('  python -c "import secrets; print(secrets.token_hex(32))"')
    print("Then set it as an environment variable (TrueNAS UI, docker-compose, etc.).")
    sys.exit(1)

start_scheduler(app)
