"""Local test server wired to a fake exchange. Never part of application startup."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
import uvicorn
from fake_binance import FakeBinance
from bot.web import create_app
from bot.futures import FuturesConfig,LIVE_CONFIRMATION

exchange=FakeBinance()
live_exchange=FakeBinance(mode='live')
if __name__=='__main__':
    app=create_app(run_worker=False,demo_client_factory=lambda *args:exchange,
                   live_client_factory=lambda *args:live_exchange)

    @app.post('/test/closed-incident')
    def closed_incident():
        # Test fixture only; production create_app never registers this route.
        live_exchange.millis+=60000
        live=app.state.live
        live.start(FuturesConfig(),LIVE_CONFIRMATION)
        live._send('entry','BTCUSDT',{'side':'BUY','type':'MARKET','quantity':'2.5','newOrderRespType':'RESULT'})
        live.pause('Lỗi hệ thống trong dữ liệu kiểm thử.')
        live_exchange.post('/fapi/v1/order',symbol='BTCUSDT',side='SELL',type='MARKET',quantity='2.5',
                           reduceOnly='true',newClientOrderId='manual-exit')
        return {'ok':True}

    uvicorn.run(app,host='127.0.0.1',port=8003,log_level='warning')
