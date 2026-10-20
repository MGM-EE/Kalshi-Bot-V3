from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from .core import Settings, Store, start_feeds, stop_feeds
from .paper import PaperConfig
from .paper_runtime import PaperControl, PaperJournal, SettingsConflict, SettingsUnavailable, run_paper
from .research import DEFAULT_MAX_BYTES, ResearchRecorder

STORE = web.AppKey("store", Store)
STATIC = Path(__file__).resolve().parent.parent / "static"
MAX_CLIENTS = 8
JOURNAL = web.AppKey("paper_journal", PaperJournal)
RESEARCH = web.AppKey("research_recorder", ResearchRecorder)


async def paper_settings(request: web.Request):
    control = request.app[STORE].paper_control
    if request.method == "GET":
        document = control.document() if control else PaperControl(PaperConfig()).document()
        return web.json_response(document, headers={"Cache-Control": "no-store"})
    if control is None:
        return web.json_response({"error": "Paper trading is disabled"}, status=503)
    origin = request.headers.get("Origin")
    if origin:
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return web.json_response({"error": "Invalid settings request origin"}, status=403)
        forwarded_secure_browser = (parsed.scheme == "https" and request.scheme == "http"
                                    and request.headers.get("Sec-Fetch-Site") == "same-origin"
                                    and request.headers.get("X-Forwarded-Proto") == "https")
        if (parsed.scheme not in ("http", "https") or parsed.netloc.lower() != request.host.lower()
                or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment
                or (parsed.scheme != request.scheme and not forwarded_secure_browser)):
            return web.json_response({"error": "Settings must be saved from this app's origin"}, status=403)
    if request.content_type != "application/json":
        return web.json_response({"error": "Settings must be sent as application/json"}, status=415)
    try:
        payload = await request.json()
        if (not isinstance(payload, dict) or not {"settings", "revision"} <= set(payload)
                or set(payload) - {"settings", "revision", "apply_exit_controls_to_position"}):
            raise ValueError("Send settings and revision; reload settings to get the current revision")
        if "apply_exit_controls_to_position" in payload and payload["apply_exit_controls_to_position"] is None:
            raise ValueError("Identify the open trade by its ticker and opened_at time; omit the field for future entries only")
        document = await control.change(payload["settings"], payload["revision"],
                                        apply_exit_controls_to_position=payload.get("apply_exit_controls_to_position"))
        return web.json_response(document, headers={"Cache-Control": "no-store"})
    except SettingsConflict as error:
        return web.json_response({"error": str(error)}, status=409)
    except SettingsUnavailable as error:
        return web.json_response({"error": str(error)}, status=503)
    except (ValueError, TypeError) as error:
        return web.json_response({"error": str(error)}, status=400)


async def paper_events(request: web.Request):
    if JOURNAL not in request.app:
        return web.json_response({"mode": "paper", "events": []})
    try:
        limit = int(request.query.get("limit", "50"))
        if not 1 <= limit <= 200:
            raise ValueError
    except ValueError:
        raise web.HTTPBadRequest(text="limit must be an integer from 1 to 200")
    events = await asyncio.to_thread(request.app[JOURNAL].recent_events, limit)
    return web.json_response({"mode": "paper", "events": events}, headers={"Cache-Control": "no-store"})


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


def create_app(settings: Settings | None = None, *, run_feeds: bool = True,
               paper_path: Path | None = None, paper_config: PaperConfig | None = None,
               research_path: Path | None = None, research_max_bytes: int = DEFAULT_MAX_BYTES,
               research_decision_traces: bool = True) -> web.Application:
    app = web.Application(client_max_size=4096)
    app[STORE] = Store(settings or Settings())
    if research_path is not None:
        recorder = ResearchRecorder(app[STORE], research_path, paper_path=paper_path,
                                    max_bytes=research_max_bytes, decision_traces=research_decision_traces)
        app[RESEARCH] = recorder
        app[STORE].research = recorder

        async def research_lifecycle(application):
            await application[RESEARCH].start()
            yield
            await application[RESEARCH].close()

        # Flush research after feeds and paper tasks stop, including final fills.
        app.cleanup_ctx.append(research_lifecycle)
    if paper_path is not None:
        app[JOURNAL] = PaperJournal(paper_path)
        app[STORE].paper_control = PaperControl(paper_config or PaperConfig())

        async def paper_lifecycle(application):
            task = asyncio.create_task(run_paper(application[STORE], application[JOURNAL],
                                                  paper_config or PaperConfig()), name="paper-trading")
            yield
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        app.cleanup_ctx.append(paper_lifecycle)
    if run_feeds:
        app.cleanup_ctx.append(feeds_lifecycle)
    app.router.add_get("/", index)
    app.router.add_get("/api/snapshot", snapshot)
    app.router.add_get("/api/events", events)
    app.router.add_get("/api/paper/trades", paper_events)
    app.router.add_get("/api/paper/settings", paper_settings)
    app.router.add_put("/api/paper/settings", paper_settings)
    app.router.add_get("/healthz", health)
    app.router.add_get("/readyz", health)
    app.router.add_static("/static", STATIC, show_index=False)
    return app
