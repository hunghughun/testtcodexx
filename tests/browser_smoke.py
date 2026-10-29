"""Optional local browser smoke test. No Binance requests or real orders."""
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parent.parent
URL = 'http://127.0.0.1:8001'
with tempfile.TemporaryDirectory() as temp:
    process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'bot.web:create_app', '--factory',
                                '--host', '127.0.0.1', '--port', '8001'], cwd=ROOT,
                               env={**os.environ, 'BOT_DB_PATH':str(Path(temp)/'browser.sqlite3')},
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for _ in range(50):
            try:
                with urlopen(URL+'/api/health', timeout=1) as response:
                    assert json.load(response)['live_trading'] is False
                break
            except OSError:
                if process.poll() is not None:
                    raise RuntimeError(process.stderr.read().decode())
                time.sleep(.1)
        else:
            raise RuntimeError('Server did not become ready')
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=shutil.which('chromium'), headless=True, args=['--no-sandbox'])
            page = browser.new_page(viewport={'width':1440,'height':1100}, device_scale_factor=1)
            errors=[]
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(URL)
            expect(page.locator('#connection')).to_contain_text('Đã kết nối')
            with page.expect_popup() as popup:
                page.get_by_role('link', name='Hướng dẫn sử dụng').click()
            guide = popup.value
            expect(guide.get_by_role('heading', name='Hướng dẫn dùng bot trên Windows')).to_be_visible()
            guide.close()
            assert page.locator('#equity').inner_text()=='500.00'
            assert page.locator('input[name=leverage]').input_value()=='5'
            page.locator('input[name=target]').fill('1001')
            assert page.locator('#start').is_disabled()
            page.locator('input[name=target]').fill('1000')
            page.locator('#save').click()
            expect(page.locator('#start')).to_be_enabled()
            page.locator('#start').click()
            expect(page.locator('#status')).to_have_text('Đang mô phỏng')
            assert page.locator('#save').is_disabled()
            assert page.locator('input[name=capital]').is_disabled()
            expect(page.locator('#trade-count')).to_have_text(re.compile(r'^[1-9]'), timeout=65000)
            output=ROOT/'.runtime'/'screenshots'
            output.mkdir(parents=True,exist_ok=True)
            page.screenshot(path=str(output/'desktop.png'),full_page=True)
            page.locator('#stop').click()
            expect(page.locator('#status')).to_have_text('Đã dừng')
            assert page.locator('#position-count').inner_text()=='0'
            page.set_viewport_size({'width':390,'height':844})
            page.screenshot(path=str(output/'mobile.png'),full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            assert not errors, errors
            browser.close()
            print('Browser smoke passed: configure, dirty form guard, start, real worker trade, stop, desktop/mobile, no JS errors.')
    finally:
        process.terminate()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
