import copy
import json
import math
from pathlib import Path
import sqlite3
import threading

from .engine import Engine, new_state
from .models import SessionConfig


class Conflict(Exception):
    pass


class Service:
    """Single-process coordinator; every command commits an atomic SQLite snapshot."""

    def __init__(self, db_path):
        self.db_path = str(db_path)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.error = None
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY, active INTEGER NOT NULL, state TEXT NOT NULL)")
            row = db.execute("SELECT id, state FROM sessions WHERE active = 1 ORDER BY id DESC LIMIT 1").fetchone()
            if row:
                self.session_id, raw = row
                self.state = json.loads(raw)
            else:
                self.state = new_state(SessionConfig())
                cursor = db.execute("INSERT INTO sessions(active, state) VALUES(1, ?)", (json.dumps(self.state),))
                self.session_id = cursor.lastrowid
            # Never silently resume trading after a process restart.
            if self.state["status"] == "running":
                self.state["status"] = "paused"
                self.state["reason"] = "Ứng dụng đã khởi động lại; cần tiếp tục phiên thủ công"
                Engine(self.state).event(self.state["reason"])
                db.execute("UPDATE sessions SET state = ? WHERE id = ?", (json.dumps(self.state), self.session_id))

    def connect(self):
        return sqlite3.connect(self.db_path, timeout=10)

    def commit(self, state):
        with self.connect() as db:
            db.execute("UPDATE sessions SET state = ? WHERE id = ?", (json.dumps(state), self.session_id))
        self.state = state

    def snapshot(self):
        with self.lock:
            result = Engine(copy.deepcopy(self.state)).snapshot()
            result["session_id"] = self.session_id
            result["error"] = self.error
            return result

    def configure(self, config):
        with self.lock:
            if self.state["status"] in {"running", "paused"} or self.state["positions"]:
                raise Conflict("Hãy dừng và đóng toàn bộ vị thế trước khi tạo phiên mới.")
            state = new_state(config)
            with self.connect() as db:
                db.execute("UPDATE sessions SET active = 0 WHERE active = 1")
                cursor = db.execute("INSERT INTO sessions(active, state) VALUES(1, ?)", (json.dumps(state),))
                session_id = cursor.lastrowid
            self.session_id, self.state, self.error = session_id, state, None
            return self.snapshot()

    def start(self):
        with self.lock:
            if self.state["status"] == "running":
                return self.snapshot()
            if self.error:
                raise Conflict("Có lỗi lưu trạng thái; cần khởi động lại ứng dụng và kiểm tra dữ liệu.")
            if self.state["status"] not in {"idle", "paused"}:
                raise Conflict("Phiên đã kết thúc. Tạo phiên mới để tiếp tục.")
            state = copy.deepcopy(self.state)
            state["status"], state["reason"] = "running", "Đang chạy mô phỏng"
            Engine(state).event("Bắt đầu / tiếp tục mô phỏng Futures. Không gửi lệnh đến Binance.")
            self.commit(state)
            return self.snapshot()

    def stop(self):
        with self.lock:
            if self.state["status"] in {"running", "paused"}:
                state = copy.deepcopy(self.state)
                Engine(state).stop(reason="Dừng khẩn cấp — đã đóng các vị thế mô phỏng")
                self.commit(state)
            return self.snapshot()

    def tick(self):
        with self.lock:
            if self.state["status"] != "running" or self.error:
                return
            state = copy.deepcopy(self.state)
            tick = state["tick"] + 1
            # Deterministic, fictional series. Never labelled as real market data.
            bases = {"BTCUSDT": 60000, "ETHUSDT": 3000, "BNBUSDT": 500, "SOLUSDT": 150}
            prices = {s: str(round(bases.get(s, 100) * (1 + 0.035 * math.sin(tick / 9 + i) + 0.012 * math.sin(tick / 23)), 8))
                      for i, s in enumerate(state["config"]["symbols"])}
            Engine(state).tick(prices)
            self.commit(state)

    def fail(self, message):
        with self.lock:
            self.error = message
            self.state["status"] = "paused"
            self.state["reason"] = "Tạm dừng do lỗi hệ thống; kiểm tra nhật ký máy chủ"

    def archive(self):
        with self.lock, self.connect() as db:
            return [{"session_id": row[0], "state": json.loads(row[1])}
                    for row in db.execute("SELECT id, state FROM sessions ORDER BY id")]
