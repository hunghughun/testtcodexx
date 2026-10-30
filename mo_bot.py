"""Open the local Futures UI. This launcher never starts a trading session."""
import argparse
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener
import venv
import webbrowser

ROOT = Path(__file__).resolve().parent


def is_bot_running(url):
    # Only probe loopback; a corporate web proxy is not needed for this request.
    try:
        with build_opener(ProxyHandler({})).open(url + "/api/health", timeout=1) as response:
            result = json.loads(response.read(4096))
            return result.get('status')=='ok' and result.get('app')=='tradepilot' and result.get('version')=='0.3.1'
    except (OSError, URLError, ValueError):
        return False


def prepare_python():
    virtualenv = ROOT / ".venv"
    python = virtualenv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not python.is_file():
        print("Đang chuẩn bị chương trình cho lần chạy đầu tiên…", flush=True)
        venv.EnvBuilder(with_pip=True).create(virtualenv)
    requirements = ROOT / "requirements.txt"
    digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
    marker = ROOT / ".runtime" / "desktop-dependencies.sha256"
    installed = marker.is_file() and marker.read_text().strip() == digest
    if installed:
        installed = subprocess.run(
            [str(python), "-c", "import fastapi, uvicorn"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0
    if not installed:
        print("Đang cài các thành phần cần thiết. Lần đầu cần Internet; hãy chờ đến khi trình duyệt mở.", flush=True)
        subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                        "-r", str(requirements)], cwd=ROOT, check=True)
        marker.parent.mkdir(exist_ok=True)
        marker.write_text(digest, encoding="utf-8")
    return python


def show_browser(url, disabled):
    print(f"Mở ứng dụng trên máy này tại: {url}", flush=True)
    if not disabled:
        try:
            if not webbrowser.open(url):
                print("Trình duyệt chưa tự mở. Nhập địa chỉ phía trên vào Chrome hoặc Edge.", flush=True)
        except webbrowser.Error:
            print("Hãy nhập địa chỉ phía trên vào Chrome hoặc Edge.", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Mở bot Binance Futures trên máy cá nhân.")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Cổng phải nằm trong khoảng 1024–65535.")
    if sys.version_info < (3, 12):
        print("Cần Python 3.12 trở lên. Hãy cài Python từ python.org rồi mở lại tệp này.")
        return 1
    url = f"http://127.0.0.1:{args.port}"
    if is_bot_running(url):
        print("Bot đã được mở sẵn. Đang mở lại giao diện; không tạo tiến trình trùng.", flush=True)
        show_browser(url, args.no_browser)
        return 0
    try:
        with socket.create_connection(("127.0.0.1", args.port), timeout=1):
            print(f"Cổng {args.port} đang được chương trình khác sử dụng. Chưa khởi động bot.")
            return 1
    except OSError:
        pass
    python = prepare_python()
    print("Đang mở TradePilot. Chỉ giao dịch tiền thật khi bạn kết nối và xác nhận bắt đầu trong ứng dụng.", flush=True)
    process = subprocess.Popen([str(python), "-m", "uvicorn", "bot.web:create_app", "--factory",
                                "--host", "127.0.0.1", "--port", str(args.port)], cwd=ROOT)
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Ứng dụng chưa khởi động được. Hãy đọc thông báo lỗi phía trên.")
            if is_bot_running(url):
                break
            time.sleep(.2)
        else:
            raise RuntimeError("Ứng dụng chưa phản hồi sau 30 giây. Hãy kiểm tra cửa sổ thông báo.")
        show_browser(url, args.no_browser)
        print("Giữ cửa sổ này mở khi sử dụng. Muốn tắt: bấm Dừng & đóng vị thế trên trang trước, rồi nhấn Ctrl+C tại đây.", flush=True)
        return process.wait()
    except KeyboardInterrupt:
        print("\nĐang đóng chương trình. Lịch sử đã lưu trong thư mục .runtime.", flush=True)
        return 0
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"\nChưa mở được bot: {exc}")
        print("Giữ lại thông báo này để được hỗ trợ. Không gửi API key hoặc mật khẩu.")
        sys.exit(1)
