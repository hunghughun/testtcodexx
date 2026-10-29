from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
import threading

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .models import SessionConfig
from .service import Conflict, Service

STATIC = Path(__file__).resolve().parent / "static"


def create_app(db_path=None, run_worker=True):
    service = Service(db_path or os.environ.get("BOT_DB_PATH", ".runtime/bot.sqlite3"))

    @asynccontextmanager
    async def lifespan(app):
        shutdown = threading.Event()

        def work():
            while not shutdown.wait(2):
                try:
                    service.tick()
                except Exception:
                    logging.exception("Paper trading worker paused")
                    service.fail("Không thể cập nhật phiên; xem nhật ký máy chủ.")

        worker = threading.Thread(target=work, daemon=True, name="paper-trading")
        if run_worker:
            worker.start()
        yield
        shutdown.set()
        if run_worker:
            worker.join(timeout=5)

    app = FastAPI(title="TradePilot — Futures simulator", lifespan=lifespan)
    app.state.service = service

    @app.middleware("http")
    async def local_access(request: Request, call_next):
        # Local operator UI only. Reject browser cross-origin writes and DNS rebinding.
        from urllib.parse import urlsplit
        if request.url.hostname not in {"127.0.0.1", "localhost", "testserver"}:
            return JSONResponse({"detail": "Ứng dụng này chỉ phục vụ trên máy cục bộ."}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "Không cho phép thao tác từ trang khác."}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Không cho phép thao tác từ trang khác."}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "mode": "paper", "live_trading": False}

    @app.get("/api/state")
    def state():
        return service.snapshot()

    @app.post("/api/config")
    def configure(config: SessionConfig):
        return service.configure(config)

    @app.post("/api/start")
    def start():
        return service.start()

    @app.post("/api/stop")
    def stop():
        return service.stop()

    @app.get("/api/export")
    def export():
        return JSONResponse(service.archive(), headers={"Content-Disposition": 'attachment; filename="paper-sessions.json"'})

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
