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

app = create_app(os.environ.get("FLASK_ENV", "production"))

# Refuse to start in production with the insecure default key.
# Set SECRET_KEY as an environment variable before running.
if (app.config.get("SECRET_KEY") == "dev-secret-key-change-in-production"
        and os.environ.get("FLASK_ENV", "production") == "production"):
    print("ERROR: SECRET_KEY is not set. Generate one with:")
    print('  python -c "import secrets; print(secrets.token_hex(32))"')
    print("Then set it as an environment variable (TrueNAS UI, docker-compose, etc.).")
    sys.exit(1)

start_scheduler(app)
