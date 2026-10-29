"""Local test server wired to a fake exchange. Never part of application startup."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
import uvicorn
from fake_binance import FakeBinance
from bot.web import create_app

exchange=FakeBinance()
live_exchange=FakeBinance(mode='live')
if __name__=='__main__':
    uvicorn.run(create_app(run_worker=False,demo_client_factory=lambda *args:exchange,
                          live_client_factory=lambda *args:live_exchange),host='127.0.0.1',port=8003,log_level='warning')
