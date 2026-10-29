"""Production-mode control-flow tests. All exchange I/O is fake; no live orders."""
from decimal import Decimal as D
import hashlib
import hmac
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock
from urllib.error import HTTPError
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from bot.binance import BinanceLive, BinanceDemo, ExchangeError
from bot.futures import DemoService, LiveService, FuturesConfig, FuturesCredentials, LIVE_CONFIRMATION
from bot.web import create_app
from fake_binance import FakeBinance

CREDS=FuturesCredentials(api_key='k'*32,api_secret='s'*32)


class LiveAdapterTests(unittest.TestCase):
    def test_exchange_timeout_code_is_unknown_even_with_http_400(self):
        opener=MagicMock()
        opener.open.side_effect=HTTPError('private-url',400,'private',{},io.BytesIO(b'{"code":-1007}'))
        client=BinanceLive('k'*32,'s'*32,opener=opener,clock=lambda:1800000000)
        client._synced=1800000000
        with self.assertRaises(ExchangeError) as caught:
            client.post('/fapi/v1/order',symbol='BTCUSDT')
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(opener.open.call_count,1)

    def test_read_only_connect_uses_official_v3_balances_and_account_config_shapes(self):
        # In particular, v3/account DOES NOT return canTrade. Keep this independent
        # of FakeBinance so an invented fixture field cannot mask an API mismatch.
        payloads={
            '/fapi/v1/time':{'serverTime':1800000000000},
            '/fapi/v3/account':{'assets':[{'asset':'USDT','walletBalance':'500','availableBalance':'500'}]},
            '/fapi/v1/accountConfig':{'canTrade':True,'dualSidePosition':False,'multiAssetsMargin':False},
            '/fapi/v1/feeBurn':{'feeBurn':False},
            '/fapi/v3/positionRisk':[],
        }
        opener=MagicMock()
        def response(request,**kwargs):
            self.assertEqual(request.method,'GET')
            self.assertEqual(urlsplit(request.full_url).hostname,'fapi.binance.com')
            return io.BytesIO(json.dumps(payloads[urlsplit(request.full_url).path]).encode())
        opener.open.side_effect=response
        adapter=BinanceLive('k'*32,'s'*32,opener=opener,clock=lambda:1800000000)
        with tempfile.TemporaryDirectory() as temp:
            service=LiveService(Path(temp)/'contract.sqlite3',client_factory=lambda *args:adapter)
            try:
                state=service.connect(CREDS)
                self.assertEqual(state['account']['available'],'500')
                self.assertEqual(state['status'],'idle')
                self.assertEqual(opener.open.call_count,5)
            finally:service.close()

    def test_live_signed_orders_use_only_production_and_no_fallback(self):
        opener=MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value=b'{"orderId":1}'
        client=BinanceLive('k'*32,'s'*32,opener=opener,clock=lambda:1800000000)
        client._synced=1800000000
        client.post('/fapi/v1/order',symbol='BTCUSDT',quantity='0.01',side='BUY',type='MARKET')
        request=opener.open.call_args.args[0]
        self.assertEqual(request.full_url,'https://fapi.binance.com/fapi/v1/order')
        body,signature=request.data.decode().rsplit('&signature=',1)
        self.assertEqual(signature,hmac.new(b's'*32,body.encode(),hashlib.sha256).hexdigest())
        self.assertEqual(request.get_header('X-mbx-apikey'),'k'*32)
        opener.open.side_effect=HTTPError('secret-url',401,'secret',{},io.BytesIO(b'{"code":-2015}'))
        with self.assertRaises(ExchangeError) as caught:
            client.get('/fapi/v3/account')
        self.assertIn('tiền thật',str(caught.exception))
        self.assertNotIn('secret',str(caught.exception))
        self.assertEqual(opener.open.call_count,2)
        self.assertNotEqual(BinanceLive.base_url,BinanceDemo.base_url)
        with self.assertRaises(ExchangeError):
            client.request('GET','https://demo-fapi.binance.com/fapi/v3/account')


class LiveServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'live.sqlite3'
        self.exchange=FakeBinance(mode='live')
        self.service=LiveService(self.path,client_factory=lambda *args:self.exchange)
        self.addCleanup(self.service.close)

    def start(self,**config):
        self.service.connect(CREDS)
        return self.service.start(FuturesConfig(**config),LIVE_CONFIRMATION)

    def open(self,side='LONG',symbol='BTCUSDT',request='request-1'):
        return self.service.manual_order(symbol,side,request)

    def test_read_only_connection_and_explicit_session_confirmation(self):
        self.service.connect(CREDS)
        self.service.tick()
        for confirmation in [None,True,'','GIAO DICH DEMO']:
            with self.assertRaises(ExchangeError):
                self.service.start(FuturesConfig(),confirmation)
        with self.assertRaises(ExchangeError):self.open()
        self.assertEqual(self.exchange.writes,[])
        state=self.service.snapshot()
        self.assertEqual(state['status'],'idle')
        self.assertEqual(state['mode'],'binance_live')
        self.assertEqual(state['endpoint'],'fapi.binance.com')
        self.assertTrue(state['live_trading'])

    def test_bnb_fees_account_disabled_and_hedge_block_before_writes(self):
        for setting in ['fee_burn','hedge','multi','can_trade']:
            with self.subTest(setting=setting):
                original=getattr(self.exchange,setting)
                setattr(self.exchange,setting,not original)
                with self.assertRaises(ExchangeError):self.service.connect(CREDS)
                self.assertEqual(self.exchange.writes,[])
                setattr(self.exchange,setting,original)

    def test_live_long_short_native_protection_and_stop(self):
        self.start()
        self.open('LONG')
        self.open('SHORT','ETHUSDT','request-2')
        self.assertEqual(len(self.exchange.algos()),4)
        for algo in self.exchange.algos():
            self.assertEqual(algo['reduceOnly'],'true')
            self.assertEqual(algo['workingType'],'MARK_PRICE')
            self.assertTrue(algo['clientAlgoId'].startswith('tpl_'))
        state=self.service.stop()  # no confirmation needed to reduce existing risk
        self.assertEqual(state['status'],'stopped')
        self.assertFalse(self.exchange.positions());self.assertFalse(self.exchange.algos())
        self.assertLess(D(state['equity']),D(500))  # fees really deducted
        self.assertNotIn(b'k'*32,self.path.read_bytes())
        self.assertNotIn(b's'*32,self.path.read_bytes())

    def test_timeout_after_execution_and_unknown_write_no_duplicate(self):
        self.start();self.exchange.timeout_entry=True
        self.open()
        self.assertEqual(sum(p.endswith('/order') for p,_ in self.exchange.writes),1)
        self.assertEqual(len(self.exchange.algos()),2)
        self.exchange.unknown_entry=True
        with self.assertRaises(ExchangeError):self.open('SHORT','ETHUSDT','unknown')
        count=len(self.exchange.writes)
        with self.assertRaises(ExchangeError):self.service.resume(LIVE_CONFIRMATION)
        self.assertEqual(len(self.exchange.writes),count)
        self.assertEqual(self.service.snapshot()['status'],'paused')

    def test_rejected_protection_attempts_reduce_only_exit(self):
        self.start();self.exchange.fail_tp=True
        with self.assertRaises(ExchangeError):self.open()
        self.assertFalse(self.exchange.positions())
        closes=[p for path,p in self.exchange.writes if path.endswith('/order') and p.get('reduceOnly')=='true']
        self.assertEqual(len(closes),1)
        self.assertFalse(self.exchange.algos())

    def test_loss_exit_not_blocked_by_delayed_fill_history(self):
        self.start(max_loss=5)
        self.exchange.fees_delay=True
        self.open()
        self.exchange.market['BTCUSDT']=D(97)
        self.service.tick()
        self.assertFalse(self.exchange.positions())
        self.assertEqual(self.service.snapshot()['status'],'paused')  # waiting for final fees
        self.exchange.fees_delay=False
        self.service.stop()
        self.assertEqual(self.service.snapshot()['status'],'loss_limit')

    def test_target_closes_before_terminal_and_does_not_restart(self):
        self.start(target=509)
        self.open()
        self.exchange.market['BTCUSDT']=D(104)
        self.service.tick()
        self.assertEqual(self.service.snapshot()['status'],'target_reached')
        self.assertFalse(self.exchange.positions());self.assertFalse(self.exchange.algos())
        writes=len(self.exchange.writes)
        self.service.tick()
        self.assertEqual(len(self.exchange.writes),writes)

    def test_auto_strategy_can_open_both_symbols_and_stop(self):
        self.start(strategy='sma')
        self.service.tick()
        self.exchange.closes=[D(100)]*15+[D(101)];self.exchange.millis+=60000
        self.service.tick()
        self.assertEqual(set(self.service.snapshot()['positions']),{'BTCUSDT','ETHUSDT'})
        self.service.stop()
        self.assertFalse(self.exchange.positions())

    def test_restart_is_paused_and_resume_requires_confirmation_and_same_key(self):
        self.start();self.open()
        self.service.close()
        restored=LiveService(self.path,client_factory=lambda *args:self.exchange)
        self.addCleanup(restored.close)
        self.assertFalse(restored.snapshot()['connected'])
        self.assertEqual(restored.snapshot()['status'],'paused')
        wrong=FakeBinance(key='x'*32,mode='live')
        restored.client_factory=lambda *args:wrong
        with self.assertRaises(ExchangeError):restored.connect(CREDS)
        restored.client_factory=lambda *args:self.exchange
        restored.connect(CREDS)
        before=len(self.exchange.writes)
        with self.assertRaises(ExchangeError):restored.resume()
        restored.tick()
        self.assertEqual(len(self.exchange.writes),before)
        restored.resume(LIVE_CONFIRMATION)
        self.assertEqual(restored.snapshot()['status'],'running')
        self.assertEqual(len(self.exchange.writes),before)

    def test_wrong_environment_adapter_rejected_before_any_exchange_io(self):
        wrong=MagicMock(mode='demo')
        self.service.client_factory=lambda *args:wrong
        with self.assertRaises(ExchangeError):self.service.connect(CREDS)
        wrong.sync_time.assert_not_called()

    def test_demo_database_cannot_be_loaded_by_live(self):
        path=Path(self.temp.name)/'old-demo.sqlite3'
        demo=DemoService(path)
        demo.s.pop('environment')  # supports old Demo DBs without a mode marker
        demo.save()
        before=path.read_bytes()
        with self.assertRaises(ExchangeError):LiveService(path)
        self.assertEqual(path.read_bytes(),before)

    def test_single_process_lock_blocks_second_process_without_changing_state(self):
        self.start();self.open()
        script="from bot.futures import LiveService; from bot.binance import ExchangeError; import sys\ntry: LiveService(sys.argv[1])\nexcept ExchangeError: sys.exit(0)\nsys.exit(1)"
        result=subprocess.run([sys.executable,'-c',script,str(self.path)],capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.service.snapshot()['status'],'running')
        with self.assertRaises(ExchangeError):LiveService(self.path)

    def test_live_api_confirmation_secrets_origin_and_demo_isolation(self):
        demo=FakeBinance()
        app=create_app(Path(self.temp.name)/'api.sqlite3',run_worker=False,
                       demo_client_factory=lambda *args:demo,live_client_factory=lambda *args:self.exchange)
        with TestClient(app) as client:
            bad=client.post('/api/live/connect',json={'api_key':'bad-secret','api_secret':'secret value'})
            self.assertEqual(bad.status_code,422)
            self.assertNotIn('secret value',bad.text)
            creds={'api_key':'k'*32,'api_secret':'s'*32}
            self.assertEqual(client.post('/api/live/connect',json=creds).status_code,200)
            self.assertFalse(client.get('/api/demo/state').json()['connected'])
            for body in [{'config':{}},{'config':{},'confirm_live':True},{'config':{},'confirm_demo':True}]:
                self.assertEqual(client.post('/api/live/start',json=body).status_code,422)
            self.assertEqual(self.exchange.writes,[])
            started=client.post('/api/live/start',json={'config':{},'confirm_live':LIVE_CONFIRMATION})
            self.assertEqual(started.status_code,200,started.text)
            self.assertTrue(client.get('/api/health').json()['live_trading'])
            order={'symbol':'BTCUSDT','side':'LONG','request_id':'00000000-0000-4000-8000-000000000001'}
            result=client.post('/api/live/order',json=order)
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(client.get('/api/demo/state').json()['status'],'idle')
            self.assertEqual(demo.writes,[])
            self.assertEqual(client.post('/api/live/stop',headers={'Origin':'https://bad.example'}).status_code,403)
            self.assertTrue(self.exchange.positions())
            app.state.live.pause('Test interruption')
            self.assertEqual(client.post('/api/live/resume',json={}).status_code,422)
            self.assertEqual(client.post('/api/live/stop').json()['status'],'stopped')
            self.assertFalse(client.get('/api/health').json()['live_trading'])
        with self.assertRaises(ExchangeError):app.state.live.connect(CREDS)
