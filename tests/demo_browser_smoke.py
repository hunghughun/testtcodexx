import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen
from playwright.sync_api import sync_playwright,expect

root=Path(__file__).resolve().parent.parent
with tempfile.TemporaryDirectory() as temp:
    process=subprocess.Popen([sys.executable,'tests/demo_browser_server.py'],cwd=root,
        env={**os.environ,'BOT_DB_PATH':str(Path(temp)/'browser.sqlite3')},stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    try:
        for _ in range(50):
            try:
                with urlopen('http://127.0.0.1:8003/api/health',timeout=1) as r:assert r.status==200
                break
            except OSError:
                if process.poll() is not None:raise RuntimeError(process.stderr.read().decode())
                time.sleep(.1)
        else:raise RuntimeError('Server not ready')
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=shutil.which('chromium'),headless=True,args=['--no-sandbox'])
            page=browser.new_page(viewport={'width':1440,'height':1000})
            errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto('http://127.0.0.1:8003/demo')
            expect(page.locator('#demo-connect')).to_be_enabled()
            page.locator('input[name=api_key]').fill('k'*32)
            page.locator('input[name=api_secret]').fill('s'*32)
            page.locator('#demo-connect').click()
            expect(page.locator('#demo-connection')).to_have_text('Đã kết nối Binance Demo')
            assert page.locator('input[name=api_secret]').input_value()==''
            assert page.locator('input[name=api_key]').input_value()==''
            page.locator('#confirm-demo').check()
            page.locator('#demo-start').click()
            expect(page.locator('#demo-status')).to_have_text('Đang chạy Demo')
            page.locator('#demo-long').click()
            expect(page.locator('#demo-count')).to_have_text('1')
            expect(page.locator('#demo-positions')).to_contain_text(re.compile(r'SL: 98(?:\.0+)? \(NEW\)'))
            expect(page.locator('#demo-positions')).to_contain_text(re.compile(r'TP: 104(?:\.0+)? \(NEW\)'))
            out=root/'.runtime'/'screenshots';out.mkdir(parents=True,exist_ok=True)
            page.screenshot(path=str(out/'demo-desktop.png'),full_page=True)
            page.locator('#demo-stop').click()
            expect(page.locator('#demo-status')).to_have_text('Demo đã dừng')
            expect(page.locator('#demo-count')).to_have_text('0')
            page.set_viewport_size({'width':390,'height':844})
            page.screenshot(path=str(out/'demo-mobile.png'),full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            assert not errors,errors
            assert page.evaluate('Object.keys(localStorage).length')==0
            for path in Path(temp).glob('*.sqlite3'):
                assert b's'*32 not in path.read_bytes()
            browser.close()
            print('Demo browser passed: connect, clear secrets, start, native SL/TP, stop, responsive UI; fake exchange only.')
    finally:
        process.terminate()
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:process.kill();process.wait()
