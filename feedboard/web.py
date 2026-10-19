from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from aiohttp import web

from .core import Settings, Store, start_feeds, stop_feeds

STORE = web.AppKey("store", Store)
STATIC = Path(__file__).resolve().parent.parent / "static"
MAX_CLIENTS = 8


async def feeds_lifecycle(app: web.Application):
    session, tasks = await start_feeds(app[STORE])
    yield
    await stop_feeds(session, tasks)


async def index(request: web.Request):
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


async def snapshot(request: web.Request):
    return web.json_response(request.app[STORE].snapshot(), headers={"Cache-Control": "no-store"})


async def health(request: web.Request):
    store = request.app[STORE]
    ready = store.ready()
    status = 503 if request.path == "/readyz" and not ready else 200
    return web.json_response({"running": True, "ready": ready,
                              "feeds": {k: v["status"] for k, v in store.snapshot()["feeds"].items()}},
                             status=status, headers={"Cache-Control": "no-store"})


async def events(request: web.Request):
    store = request.app[STORE]
    if store.clients >= MAX_CLIENTS:
        raise web.HTTPServiceUnavailable(text="Too many dashboard clients; close an unused tab.")
    response = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                           "Cache-Control": "no-cache, no-transform",
                                           "X-Accel-Buffering": "no"})
    # Reserve a slot before the first await so concurrent connections cannot exceed the cap.
    store.clients += 1
    previous_revision = -1
    last_sent = 0.0
    try:
        await response.prepare(request)
        while True:
            # Coalesce tick bursts to the latest state. Each client has no pending event queue.
            if store.revision != previous_revision or time.monotonic() - last_sent >= 1:
                revision = store.revision
                data = json.dumps(store.snapshot(), separators=(",", ":"), allow_nan=False)
                await asyncio.wait_for(response.write(f"event: snapshot\ndata: {data}\n\n".encode()), 2)
                previous_revision = revision
                last_sent = time.monotonic()
            await asyncio.sleep(0.1)
    except (ConnectionError, TimeoutError):
        pass
    finally:
        store.clients -= 1
    return response


def create_app(settings: Settings | None = None, *, run_feeds: bool = True) -> web.Application:
    app = web.Application(client_max_size=1024)
    app[STORE] = Store(settings or Settings())
    if run_feeds:
        app.cleanup_ctx.append(feeds_lifecycle)
    app.router.add_get("/", index)
    app.router.add_get("/api/snapshot", snapshot)
    app.router.add_get("/api/events", events)
    app.router.add_get("/healthz", health)
    app.router.add_get("/readyz", health)
    app.router.add_static("/static", STATIC, show_index=False)
    return app
