"""WSGI entry for Docker / gunicorn.

Keeps the original single-file gateway.py layout while providing a
stable import path for container and systemd deployments.
"""

from gateway import app  # gateway 导入时已经执行 init_db()，这里不要再调一次

__all__ = ["app"]
