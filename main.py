"""Paas-friendly entrypoint for LLM Platform.

Most sandbox / PaaS builders (PandaStack, Zeabur, Sealos, Koyeb, ...) detect a
Python project and then blindly run::

    uvicorn main:app --host 0.0.0.0 --port $PORT

The original project only ships ``gateway.py`` (a Flask / WSGI app) plus
``wsgi.py`` for gunicorn, so ``main:app`` does not exist and uvicorn cannot
serve a WSGI callable.  This module closes that gap:

* sets a writable ``LLM_PLATFORM_HOME`` before ``gateway`` is imported
  (``gateway.py`` calls ``init_db()`` at import time, so an unwritable
  ``/opt/llm-platform`` makes the process die immediately),
* keeps ``FLASK_SECRET_KEY`` stable across restarts,
* exposes an ASGI ``app`` (uvicorn) *and* plain WSGI callables
  (``wsgi_app`` / ``application`` for gunicorn).

Bridge selection order: a2wsgi -> asgiref -> bundled stdlib thread bridge.
Force one with ``ASGI_BRIDGE=a2wsgi|asgiref|builtin``.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _ensure_runtime_home():
    """Point LLM_PLATFORM_HOME at a directory we can actually write to."""
    candidates = []
    configured = (os.environ.get("LLM_PLATFORM_HOME") or "").strip()
    if configured:
        candidates.append(configured)
    candidates.append(os.path.join(_HERE, "runtime"))
    candidates.append(os.path.join(os.getcwd(), "runtime"))
    candidates.append("/tmp/llm-platform")

    for path in candidates:
        try:
            os.makedirs(os.path.join(path, "data"), exist_ok=True)
            os.makedirs(os.path.join(path, "logs"), exist_ok=True)
            probe = os.path.join(path, "data", ".write-test")
            with open(probe, "w") as fh:
                fh.write("ok")
            os.remove(probe)
            os.environ["LLM_PLATFORM_HOME"] = path
            return path
        except Exception:
            continue
    # Last resort: let gateway.py create its own default directory.
    return os.environ.get("LLM_PLATFORM_HOME", "/opt/llm-platform")


def _ensure_secret_key():
    """Generate FLASK_SECRET_KEY once and persist it, so sessions survive restarts."""
    if (os.environ.get("FLASK_SECRET_KEY") or "").strip():
        return
    home = os.environ.get("LLM_PLATFORM_HOME", _HERE)
    key_file = os.path.join(home, "secret.key")
    try:
        if os.path.exists(key_file):
            with open(key_file, "r") as fh:
                key = fh.read().strip()
            if key:
                os.environ["FLASK_SECRET_KEY"] = key
                return
        import secrets as _secrets

        key = _secrets.token_hex(32)
        with open(key_file, "w") as fh:
            fh.write(key)
        try:
            os.chmod(key_file, 0o600)
        except Exception:
            pass
        os.environ["FLASK_SECRET_KEY"] = key
    except Exception:
        # Non fatal: gateway.py falls back to a random per-process key.
        pass


RUNTIME_HOME = _ensure_runtime_home()
_ensure_secret_key()

from gateway import app as flask_app, init_db  # noqa: E402  (env must be set first)

init_db()

# ---------------------------------------------------------------- ASGI bridge
class _ThreadedWSGIBridge:
    """Minimal stdlib-only WSGI -> ASGI bridge used when no helper package exists.

    The WSGI app runs in a worker thread (Flask is blocking), and the response
    iterable is streamed back through a bounded queue so SSE / chunked answers
    keep flowing instead of being buffered.
    """

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    async def __call__(self, scope, receive, send):
        scope_type = scope.get("type")
        if scope_type == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
            return
        if scope_type != "http":
            return

        import asyncio
        import io
        import queue
        import threading

        body = b""
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] == "http.request":
                body += message.get("body", b"") or b""
                if not message.get("more_body", False):
                    break

        environ = self._build_environ(scope, body, io)
        captured = {}
        out_queue = queue.Queue(maxsize=32)

        def start_response(status, headers, exc_info=None):
            captured["status"] = status
            captured["headers"] = headers
            return lambda chunk: None

        def run_wsgi():
            iterator = None
            try:
                iterator = self.wsgi_app(environ, start_response)
                for chunk in iterator:
                    if chunk:
                        out_queue.put(("body", chunk))
                out_queue.put(("end", None))
            except Exception as exc:  # surfaced to uvicorn as a 500
                out_queue.put(("error", exc))
            finally:
                closer = getattr(iterator, "close", None)
                if callable(closer):
                    try:
                        closer()
                    except Exception:
                        pass

        threading.Thread(target=run_wsgi, daemon=True).start()
        loop = asyncio.get_event_loop()
        get_next = lambda: loop.run_in_executor(None, out_queue.get)  # noqa: E731

        kind, payload = await get_next()
        if kind == "error":
            raise payload

        status_line = captured.get("status") or "200 OK"
        try:
            status_code = int(str(status_line).split(" ", 1)[0])
        except Exception:
            status_code = 200
        raw_headers = captured.get("headers") or []
        headers = []
        for name, value in raw_headers:
            lname = str(name).lower()
            # Hop-by-hop headers must not be forwarded over ASGI.
            if lname in ("connection", "keep-alive", "transfer-encoding", "upgrade"):
                continue
            headers.append((lname.encode("latin-1"), str(value).encode("latin-1")))

        await send({"type": "http.response.start", "status": status_code, "headers": headers})
        if kind == "body":
            await send({"type": "http.response.body", "body": payload, "more_body": True})
        while True:
            kind, payload = await get_next()
            if kind == "end":
                break
            if kind == "error":
                raise payload
            await send({"type": "http.response.body", "body": payload, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    @staticmethod
    def _build_environ(scope, body, io):
        server = scope.get("server") or ("0.0.0.0", 80)
        client = scope.get("client") or ("127.0.0.1", 0)
        environ = {
            "REQUEST_METHOD": scope.get("method", "GET"),
            "SCRIPT_NAME": scope.get("root_path", ""),
            "PATH_INFO": scope.get("path", "/"),
            "QUERY_STRING": (scope.get("query_string") or b"").decode("latin-1"),
            "SERVER_NAME": str(server[0]),
            "SERVER_PORT": str(server[1]),
            "SERVER_PROTOCOL": "HTTP/" + str(scope.get("http_version", "1.1")),
            "REMOTE_ADDR": str(client[0]),
            "wsgi.version": (1, 0),
            "wsgi.url_scheme": scope.get("scheme", "http"),
            "wsgi.input": io.BytesIO(body),
            "wsgi.errors": sys.stderr,
            "wsgi.multithread": True,
            "wsgi.multiprocess": False,
            "wsgi.run_once": False,
            "CONTENT_LENGTH": str(len(body)),
        }
        for raw_name, raw_value in scope.get("headers", []):
            name = raw_name.decode("latin-1").lower()
            value = raw_value.decode("latin-1")
            if name in ("content-type", "content-length"):
                environ["CONTENT_" + name.split("-", 1)[1].upper()] = value
            else:
                environ["HTTP_" + name.upper().replace("-", "_")] = value
        return environ


def _build_asgi_app():
    forced = (os.environ.get("ASGI_BRIDGE") or "").strip().lower()
    if forced in ("builtin", "stdlib"):
        print("[main] ASGI bridge: bundled stdlib bridge (forced)", flush=True)
        return _ThreadedWSGIBridge(flask_app)
    if forced not in ("", "a2wsgi", "asgiref"):
        print(f"[main] unknown ASGI_BRIDGE={forced!r}, auto-detecting", flush=True)
    try:
        from a2wsgi import WSGIMiddleware

        print("[main] ASGI bridge: a2wsgi", flush=True)
        return WSGIMiddleware(flask_app)
    except Exception as exc:
        print(f"[main] a2wsgi unavailable ({exc}); trying asgiref", flush=True)
    try:
        from asgiref.wsgi import WsgiToAsgi

        print("[main] ASGI bridge: asgiref", flush=True)
        return WsgiToAsgi(flask_app)
    except Exception as exc:
        print(f"[main] asgiref unavailable ({exc}); using bundled stdlib bridge", flush=True)
    return _ThreadedWSGIBridge(flask_app)


# uvicorn main:app          -> ASGI wrapper
app = _build_asgi_app()
# gunicorn main:wsgi_app    -> plain WSGI
wsgi_app = flask_app
application = flask_app


def _resolve_port():
    for name in ("PORT", "SERVER_PORT", "APP_PORT"):
        raw = (os.environ.get(name) or "").strip()
        if raw.isdigit():
            return int(raw)
    return 3000


def main():
    port = _resolve_port()
    print(
        f"[main] starting on 0.0.0.0:{port} "
        f"(LLM_PLATFORM_HOME={os.environ.get('LLM_PLATFORM_HOME')})",
        flush=True,
    )
    try:
        import uvicorn

        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info", proxy_headers=True)
        return
    except ImportError:
        print("[main] uvicorn not installed, falling back to the Flask dev server", flush=True)
    flask_app.run(host="0.0.0.0", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
