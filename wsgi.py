"""WSGI entry for Docker / gunicorn.

Keeps the original single-file gateway.py layout while providing a
stable import path for container and systemd deployments.
"""

from gateway import app, init_db

init_db()

__all__ = ["app"]
