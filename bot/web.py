from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
import threading
from typing import Literal
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict

from .models import SessionConfig
from .service import Conflict, Service
from .futures import DemoService, LiveService, FuturesConfig, FuturesCredentials
from .binance import ExchangeError

STATIC = Path(__file__).resolve().parent / "static"


class DemoStart(BaseModel):
    model_config = ConfigDict(extra='forbid')
    config: FuturesConfig
    confirm_demo: Literal[True]


class FuturesOrder(BaseModel):
    model_config = ConfigDict(extra='forbid')
    symbol: str
    side: Literal['LONG','SHORT']
    request_id: UUID


class LiveConfirmation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirm_live: Literal['GIAO DICH TIEN THAT']


class LiveStart(LiveConfirmation):
    config: FuturesConfig


def create_app(db_path=None, run_worker=True, demo_client_factory=None, live_client_factory=None):
    service = Service(db_path or os.environ.get("BOT_DB_PATH", ".runtime/bot.sqlite3"))
    demo_path = Path(service.db_path).with_name(Path(service.db_path).stem+'-demo.sqlite3')
    demo = DemoService(demo_path, **({'client_factory':demo_client_factory} if demo_client_factory else {}))
    live_path = Path(service.db_path).with_name(Path(service.db_path).stem+'-live.sqlite3')
    live = LiveService(live_path, client_factory=live_client_factory)

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

        def exchange_work(exchange):
            while not shutdown.wait(10):
                try:
                    exchange.tick()
                except Exception:
                    logging.error('Futures worker paused due to an unexpected error; no request details logged.')
                    try: exchange.pause('Lỗi hệ thống. Đã tạm dừng; kiểm tra lệnh trên sàn rồi đối soát.')
                    except ExchangeError: pass

        demo_worker = threading.Thread(target=exchange_work, args=(demo,), daemon=True, name='binance-demo')
        live_worker = threading.Thread(target=exchange_work, args=(live,), daemon=True, name='binance-live')
        if run_worker:
            worker.start()
            demo_worker.start()
            live_worker.start()
        yield
        shutdown.set()
        if run_worker:
            worker.join(timeout=5)
            demo_worker.join(timeout=5)
            live_worker.join(timeout=5)
        if not live_worker.is_alive():
            live.close()

    app = FastAPI(title="TradePilot — Binance Futures", lifespan=lifespan)
    app.state.service = service
    app.state.demo = demo
    app.state.live = live

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

    @app.exception_handler(ExchangeError)
    async def exchange_error(request, exc):
        return JSONResponse({'detail':str(exc)}, status_code=409)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Pydantic normally echoes invalid input; never echo submitted API secrets.
        details=[{'loc':e['loc'],'msg':e['msg'],'type':e['type']} for e in exc.errors()]
        return JSONResponse({'detail':details},status_code=422)

    @app.get('/api/demo/state')
    def demo_state(): return demo.snapshot()

    @app.post('/api/demo/connect')
    def demo_connect(credentials: FuturesCredentials): return demo.connect(credentials)

    @app.post('/api/demo/start')
    def demo_start(body: DemoStart): return demo.start(body.config)

    @app.post('/api/demo/order')
    def demo_order(body: FuturesOrder): return demo.manual_order(body.symbol,body.side,str(body.request_id))

    @app.post('/api/demo/refresh')
    def demo_refresh(): return demo.refresh()

    @app.post('/api/demo/resume')
    def demo_resume(): return demo.resume()

    @app.post('/api/demo/stop')
    def demo_stop(): return demo.stop()

    @app.get('/demo')
    def demo_page(): return FileResponse(STATIC / 'demo.html')

    @app.get('/live')
    def live_page(): return FileResponse(STATIC / 'live.html')

    @app.get('/api/live/state')
    def live_state(): return live.snapshot()

    @app.post('/api/live/connect')
    def live_connect(credentials: FuturesCredentials): return live.connect(credentials)

    @app.post('/api/live/start')
    def live_start(body: LiveStart): return live.start(body.config, body.confirm_live)

    @app.post('/api/live/order')
    def live_order(body: FuturesOrder): return live.manual_order(body.symbol,body.side,str(body.request_id))

    @app.post('/api/live/refresh')
    def live_refresh(): return live.refresh()

    @app.post('/api/live/resume')
    def live_resume(body: LiveConfirmation): return live.resume(body.confirm_live)

    @app.post('/api/live/stop')
    def live_stop(): return live.stop()

    @app.get("/api/health")
    def health():
        return {"status": "ok", "app": "tradepilot", "version": "0.3",
                "live_supported": True, "live_trading": live.s['status']=='running'}

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
