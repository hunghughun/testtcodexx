from decimal import Decimal as D
import hashlib
import hmac
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock,patch
from urllib.error import HTTPError,URLError
from urllib.parse import parse_qs,urlsplit

from fastapi.testclient import TestClient
from bot.binance import BinanceDemo,ExchangeError,SymbolRules,NoRedirect
from bot.demo import DemoService,DemoConfig,DemoCredentials
from bot.web import create_app
from fake_binance import FakeBinance

CREDS=DemoCredentials(api_key='k'*32,api_secret='s'*32)


class AdapterTests(unittest.TestCase):
    def client(self):
        opener=MagicMock()
        client=BinanceDemo('k'*32,'s'*32,opener=opener,clock=lambda:1800000000)
        client._synced=1800000000
        return client,opener

    def test_hmac_signed_body_and_fixed_demo_endpoint(self):
        client,opener=self.client()
        opener.open.return_value.__enter__.return_value.read.return_value=b'{"orderId":1}'
        client.post('/fapi/v1/order',symbol='BTCUSDT',side='BUY',type='MARKET',quantity='0.01')
        request=opener.open.call_args.args[0]
        self.assertEqual(request.full_url,'https://demo-fapi.binance.com/fapi/v1/order')
        query,signature=request.data.decode().rsplit('&signature=',1)
        self.assertEqual(signature,hmac.new(('s'*32).encode(),query.encode(),hashlib.sha256).hexdigest())
        self.assertEqual(request.get_header('X-mbx-apikey'),'k'*32)
        self.assertIn('recvWindow=5000',query)
        with self.assertRaises(ExchangeError):client.request('GET','https://fapi.binance.com/fapi/v1/order')
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,'',{},'https://other.example'))

    def test_redacted_errors_no_automatic_write_retry(self):
        client,opener=self.client()
        opener.open.side_effect=HTTPError('signed-url-secret',503,'secret message',{},io.BytesIO(json.dumps({'code':-1000,'msg':'s'*32}).encode()))
        with self.assertRaises(ExchangeError) as caught:client.post('/fapi/v1/order',symbol='BTCUSDT')
        self.assertTrue(caught.exception.uncertain)
        self.assertNotIn('s'*32,str(caught.exception))
        self.assertNotIn('signed-url',str(caught.exception))
        self.assertEqual(opener.open.call_count,1)

    def test_rate_limit_blocks_immediate_retry(self):
        client,opener=self.client()
        opener.open.side_effect=HTTPError('safe',429,'limit',{'Retry-After':'120'},io.BytesIO(b'{}'))
        with self.assertRaises(ExchangeError):client.get('/fapi/v3/account')
        with self.assertRaises(ExchangeError):client.get('/fapi/v3/account')
        self.assertEqual(opener.open.call_count,1)

    def test_server_clock_sync_and_stale_mark(self):
        client,opener=self.client()
        opener.open.return_value.__enter__.return_value.read.return_value=b'{"serverTime":1800000005000}'
        client.sync_time()
        self.assertEqual(client._offset,5000)
        opener.open.return_value.__enter__.return_value.read.return_value=b'{"markPrice":"100","time":1700000000000}'
        with self.assertRaises(ExchangeError):client.mark('BTCUSDT')

    def test_symbol_filters_and_trigger_rounding(self):
        rules=SymbolRules(FakeBinance().exchange_info()['symbols'][0])
        self.assertEqual(rules.quantity(D('50'),5,D('101')),D('2.475'))
        sl,tp=rules.triggers(D('101.01'),'LONG',D(2),D(4))
        self.assertEqual((sl,tp),(D('99.0'),D('105.0')))
        with self.assertRaises(ExchangeError):rules.quantity(D('.01'),1,D(100))


class DemoServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'demo.sqlite3'
        self.exchange=FakeBinance()
        self.service=DemoService(self.path,client_factory=lambda *args:self.exchange)
        self.service.connect(CREDS)

    def start(self,**changes):
        return self.service.start(DemoConfig(**changes))
    def open(self,side='LONG',symbol='BTCUSDT',rid='request-1'):
        return self.service.manual_order(symbol,side,rid)

    def test_connect_read_only_credentials_not_persisted(self):
        self.assertEqual(self.exchange.writes,[])
        self.assertNotIn('k'*32,json.dumps(self.service.snapshot()))
        self.assertNotIn(b's'*32,self.path.read_bytes())
        self.assertNotIn(b'k'*32,self.path.read_bytes())

    def test_reject_hedge_mode_unlisted_symbol_and_existing_positions(self):
        self.exchange.hedge=True
        with self.assertRaises(ExchangeError):self.start()
        self.exchange.hedge=False
        with self.assertRaises(ExchangeError):self.start(symbols=['FAKEUSDT'])
        self.exchange.position['BTCUSDT']={'qty':D(1),'entry':D(100)}
        with self.assertRaises(ExchangeError):self.start()
        self.assertEqual(self.exchange.writes,[])

    def test_market_long_short_and_native_protection(self):
        for side in ['LONG','SHORT']:
            with self.subTest(side=side):
                self.start();self.open(side=side,rid=side)
                self.assertEqual(len(self.exchange.algos()),2)
                for algo in self.exchange.algos():
                    self.assertEqual(algo['workingType'],'MARK_PRICE')
                    self.assertEqual(algo['reduceOnly'],'true')
                    self.assertNotIn('closePosition',algo)
                self.assertEqual(self.service.snapshot()['positions']['BTCUSDT']['side'],side)
                self.service.stop()
                self.assertFalse(self.exchange.algos())
                self.assertFalse(self.exchange.positions())
                self.exchange.trades=[]  # exchange time advances between sessions
                self.exchange.millis+=1000

    def test_idempotent_manual_request(self):
        self.start();self.open()
        count=len(self.exchange.writes)
        self.open()
        self.assertEqual(len(self.exchange.writes),count)

    def test_budget_cap_and_duplicate_position_do_not_pause_protected_session(self):
        self.start(capital=60,target=100,max_loss=30)
        self.open()
        count=len(self.exchange.writes)
        with self.assertRaises(ExchangeError):self.open(symbol='ETHUSDT',rid='over-budget')
        with self.assertRaises(ExchangeError):self.open(rid='duplicate-position')
        self.assertEqual(len(self.exchange.writes),count)
        self.assertEqual(self.service.snapshot()['status'],'running')

    def test_fee_loss_limit_checked_immediately_after_entry(self):
        self.start(max_loss='0.1')
        self.open()
        self.assertEqual(self.service.snapshot()['status'],'loss_limit')
        self.assertFalse(self.exchange.positions())

    def test_unrealized_profit_does_not_spend_outside_allocated_collateral(self):
        self.start(order_budget=300,target=2000,take_profit_pct=50)
        self.open()
        self.exchange.market['BTCUSDT']=D(120)
        with self.assertRaises(ExchangeError):self.open(symbol='ETHUSDT',rid='borrow-unrealized')
        self.assertNotIn('ETHUSDT',self.service.snapshot()['positions'])
        self.assertEqual(self.service.snapshot()['status'],'running')

    def test_changed_leverage_blocks_new_order(self):
        self.start()
        original=self.exchange.get
        def changed(path,**params):
            if path.endswith('/symbolConfig'):
                return [{'symbol':params['symbol'],'marginType':'ISOLATED','leverage':20,'isAutoAddMargin':'false'}]
            return original(path,**params)
        self.exchange.get=changed
        with self.assertRaises(ExchangeError):self.open()
        self.assertFalse(self.exchange.positions())

    def test_timeout_after_fill_reconciles_without_duplicate(self):
        self.start();self.exchange.timeout_entry=True;self.open()
        self.assertEqual(sum(path.endswith('/order') for path,_ in self.exchange.writes),1)
        self.assertEqual(len(self.exchange.algos()),2)

    def test_unknown_timeout_stays_paused_no_retry_on_restart(self):
        self.start();self.exchange.unknown_entry=True
        with self.assertRaises(ExchangeError):self.open()
        before=len(self.exchange.writes)
        restored=DemoService(self.path,client_factory=lambda *args:self.exchange)
        restored.connect(CREDS)
        with self.assertRaises(ExchangeError):restored.resume()
        self.assertEqual(len(self.exchange.writes),before)
        self.assertEqual(restored.snapshot()['status'],'paused')

    def test_partial_fill_protects_only_filled_quantity(self):
        self.start();self.exchange.partial_entry=True;self.open()
        self.assertEqual(self.service.snapshot()['positions']['BTCUSDT']['quantity'],'1.25')
        self.assertTrue(all(a['quantity']=='1.25' for a in self.exchange.algos()))

    def test_protection_rejected_flattens_position(self):
        self.start();self.exchange.fail_tp=True
        with self.assertRaises(ExchangeError):self.open()
        self.assertFalse(self.exchange.positions())
        self.assertFalse(self.exchange.algos())
        self.assertEqual(self.service.snapshot()['status'],'paused')

    def test_unknown_protection_does_not_prevent_emergency_reduction(self):
        self.start();self.exchange.unknown_tp=True
        with self.assertRaises(ExchangeError):self.open()
        self.assertFalse(self.exchange.positions())
        self.assertEqual(self.service.snapshot()['status'],'paused')

    def test_native_take_profit_cancels_sibling_and_books_real_fees(self):
        self.start();self.open()
        self.exchange.market['BTCUSDT']=D(104)
        self.exchange.trigger('TAKE_PROFIT_MARKET')
        self.service.tick()
        state=self.service.snapshot()
        self.assertFalse(state['positions'])
        self.assertFalse(self.exchange.algos())
        self.assertEqual(D(state['net']),D('9.745'))
        self.assertEqual(D(state['equity']),D('509.745'))

    def test_target_and_loss_exit_verify_flat(self):
        for target,max_loss,price,status in [(509,300,104,'target_reached'),(1000,5,97,'loss_limit')]:
            with self.subTest(status=status):
                self.start(target=target,max_loss=max_loss)
                self.open(rid=status)
                self.exchange.market['BTCUSDT']=D(price)
                self.service.tick()
                self.assertEqual(self.service.snapshot()['status'],status)
                self.assertFalse(self.exchange.positions());self.assertFalse(self.exchange.algos())
                self.exchange.market['BTCUSDT']=D(100)
                self.exchange.trades=[];self.exchange.millis+=1000

    def test_account_deposit_not_counted_as_bot_profit(self):
        self.start();self.exchange.wallet+=D(100000)
        self.service.tick()
        self.assertEqual(self.service.snapshot()['equity'],'500')
        self.assertEqual(self.service.snapshot()['status'],'running')

    def test_funding_applied_once(self):
        self.start();self.open()
        self.exchange.funding=[{'tranId':1,'symbol':'BTCUSDT','income':'-0.25','asset':'USDT','time':self.exchange.millis}]
        self.service.tick();first=self.service.snapshot()['equity']
        self.service.tick();self.assertEqual(self.service.snapshot()['equity'],first)
        self.assertEqual(len(self.service.snapshot()['funding']),1)

    def test_missing_fill_history_prevents_new_entries_and_false_target(self):
        self.start();self.exchange.fees_delay=True;self.open()
        self.assertTrue(self.service.snapshot()['waiting'])
        with self.assertRaises(ExchangeError):self.open(symbol='ETHUSDT',rid='second')
        self.assertNotIn('ETHUSDT',self.service.snapshot()['positions'])

    def test_foreign_position_and_missing_protection_stop_new_orders(self):
        self.start();self.open()
        next(iter(self.exchange.conditional.values()))['algoStatus']='CANCELED'
        self.service.tick()
        self.assertFalse(self.exchange.positions())
        self.assertNotEqual(self.service.snapshot()['status'],'running')

    def test_restart_requires_keys_and_manual_resume(self):
        self.start();self.open()
        count=len(self.exchange.writes)
        restored=DemoService(self.path,client_factory=lambda *args:self.exchange)
        self.assertFalse(restored.snapshot()['connected'])
        self.assertEqual(restored.snapshot()['status'],'paused')
        restored.connect(CREDS)
        self.assertEqual(len(self.exchange.writes),count)
        restored.resume()
        self.assertEqual(restored.snapshot()['status'],'running')
        self.assertEqual(len(self.exchange.writes),count)

    def test_write_ahead_failure_does_not_send_order(self):
        self.start()
        before=len(self.exchange.writes)
        with patch.object(self.service,'save',side_effect=ExchangeError('disk error')):
            with self.assertRaises(ExchangeError):self.service._send('entry','BTCUSDT',{'side':'BUY','type':'MARKET','quantity':'1'})
        self.assertEqual(len(self.exchange.writes),before)

    def test_sma_closed_candles_once_per_candle_and_stale_pause(self):
        self.start(strategy='sma')
        self.service.tick()
        self.exchange.closes=[D(100)]*15+[D(101)];self.exchange.millis+=60000
        self.service.tick()
        self.assertTrue(self.service.snapshot()['positions'])
        count=len(self.exchange.writes)
        self.service.tick();self.assertEqual(len(self.exchange.writes),count)
        self.exchange.stale=True;self.exchange.millis+=60000
        self.service.tick();self.assertEqual(self.service.snapshot()['status'],'paused')

    def test_api_connect_start_and_secret_validation_redaction(self):
        app=create_app(Path(self.temp.name)/'api.sqlite3',run_worker=False,demo_client_factory=lambda *args:self.exchange)
        with TestClient(app) as client:
            bad=client.post('/api/demo/connect',json={'api_key':'secret-invalid','api_secret':'sensitive value'})
            self.assertEqual(bad.status_code,422)
            self.assertNotIn('sensitive value',bad.text)
            self.assertNotIn('secret-invalid',bad.text)
            response=client.post('/api/demo/connect',json={'api_key':'k'*32,'api_secret':'s'*32})
            self.assertEqual(response.status_code,200)
            self.assertNotIn('k'*32,response.text)
            self.assertEqual(client.post('/api/demo/start',json={'config':{},'confirm_demo':False}).status_code,422)
            self.assertEqual(client.post('/api/demo/start',json={'config':{},'confirm_demo':True}).status_code,200)
            self.assertEqual(client.post('/api/demo/order',json={'symbol':'BTCUSDT','side':'LONG','request_id':'00000000-0000-4000-8000-000000000001'}).status_code,200)
            self.assertEqual(client.post('/api/demo/stop').json()['status'],'stopped')
            self.assertEqual(client.post('/api/demo/connect',json={'api_key':'k'*32,'api_secret':'s'*32},headers={'Origin':'https://bad.example'}).status_code,403)
