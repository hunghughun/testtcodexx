"""Persistent Binance Futures session coordinator, shared by Demo and live.

Credentials stay in memory. All exchange writes have a durable intent; uncertain
writes are queried by ID, never blindly retried. A dedicated account is required.
"""
import copy
from decimal import Decimal as D
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, SecretStr, field_validator
from .models import SessionConfig
from .engine import now
from .binance import BinanceDemo, BinanceLive, ExchangeError, SymbolRules, number, decimal_text
from .process_lock import ProcessLock

TERMINAL = {'FILLED','CANCELED','CANCELLED','EXPIRED','REJECTED','EXPIRED_IN_MATCH'}
ALGO_TERMINAL = {'CANCELED','CANCELLED','EXPIRED','REJECTED','FINISHED'}
LIVE_CONFIRMATION = 'GIAO DICH TIEN THAT'


class OrderBlocked(ExchangeError):
    """An expected order constraint; existing protected positions can keep running."""


class FuturesConfig(SessionConfig):
    strategy: Literal['manual','sma'] = 'manual'


class FuturesCredentials(BaseModel):
    model_config = ConfigDict(extra='forbid')
    api_key: SecretStr
    api_secret: SecretStr

    @field_validator('api_key','api_secret')
    @classmethod
    def validate_secret(cls, value):
        raw=value.get_secret_value().strip()
        if not 16 <= len(raw) <= 256 or not raw.isascii() or not raw.isalnum():
            raise ValueError('Cần khóa API HMAC đúng môi trường, gồm chữ/số và không có khoảng trắng.')
        return SecretStr(raw)


def initial(mode='demo'):
    return dict(environment=mode, status='idle', config=FuturesConfig().model_dump(mode='json'), run_id=None,
                fingerprint=None, started_ms=0, operations={}, positions={}, trades=[], funding=[],
                trade_cursors={}, funding_cursor=0, last_candle={}, manual_requests=[], seq=0,
                equity='500', net='0', error=None, events=[], updated_at=now(), waiting=False,
                finish_status=None, fee_rates={}, brackets={})


class FuturesService:
    mode = 'demo'
    label = 'Binance Demo'
    adapter = BinanceDemo
    client_prefix = 'tpd'

    def __init__(self, db_path, client_factory=None):
        self.db_path=str(db_path)
        Path(db_path).parent.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock()
        self.client_factory=client_factory or self.adapter
        self.client=None
        self.account_summary=None
        self.rules={}
        self._storage_error=False
        self._closed=False
        self._process_lock=ProcessLock(self.db_path+'.lock') if self.mode=='live' else None
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS demo_runtime (id INTEGER PRIMARY KEY, state TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS demo_archives (id INTEGER PRIMARY KEY, state TEXT NOT NULL)')
            row=db.execute('SELECT state FROM demo_runtime WHERE id=1').fetchone()
            self.s=json.loads(row[0]) if row else initial(self.mode)
        if self.s.get('environment','demo')!=self.mode:
            self.close()
            raise ExchangeError('Dữ liệu thuộc môi trường khác; không dùng lịch sử Demo cho tiền thật.')
        if self.s['status'] in {'running','stopping'}:
            self.s['status']='paused'
            self.s['error']=f'Ứng dụng đã khởi động lại. Nhập lại cùng khóa {self.label} rồi tiếp tục/đóng phiên để đối soát lệnh.'
        self.save()

    def close(self):
        with self.lock:
            self.client=None
            self._closed=True
            if self._process_lock:
                self._process_lock.close()

    def __del__(self):
        # The operating system also releases this lock if the process is killed.
        process_lock=getattr(self,'_process_lock',None)
        if process_lock:
            process_lock.close()

    def db(self): return sqlite3.connect(self.db_path,timeout=10)

    def save(self):
        if self._storage_error:
            raise ExchangeError(f'Lỗi lưu dữ liệu. Khởi động lại ứng dụng và kiểm tra lệnh trên {self.label}.')
        try:
            self.s['updated_at']=now()
            with self.db() as db:
                db.execute('INSERT OR REPLACE INTO demo_runtime(id,state) VALUES(1,?)',(json.dumps(self.s),))
        except (OSError,sqlite3.Error):
            self._storage_error=True
            self.s['status']='paused'
            self.s['error']='Không lưu được ý định lệnh; đã ngừng gửi yêu cầu giao dịch.'
            raise ExchangeError(self.s['error']) from None

    def event(self,message):
        self.s['events'].append({'at':now(),'message':message})
        self.s['events']=self.s['events'][-100:]

    def pause(self,message):
        self.s['status']='paused'
        self.s['error']=message
        self.event(message)
        self.save()

    def system_error(self,exc,stage):
        # Do not log exception messages, request URLs, locals or raw responses.
        diagnostic={'id':uuid.uuid4().hex[:8],'at':now(),'stage':stage,
                    'type':type(exc).__name__,'location':None}
        tb=exc.__traceback__
        while tb:
            filename=Path(tb.tb_frame.f_code.co_filename)
            if filename.parent.resolve()==Path(__file__).resolve().parent:
                diagnostic['location']=f'{filename.name}:{tb.tb_lineno}:{tb.tb_frame.f_code.co_name}'
            tb=tb.tb_next
        self.s.setdefault('diagnostics',[]).append(diagnostic)
        self.s['diagnostics']=self.s['diagnostics'][-10:]
        logging.error('Futures diagnostic %s',json.dumps(diagnostic))
        return ExchangeError(f"Lỗi xử lý {stage} ({diagnostic['type']}, mã {diagnostic['id']}). Kiểm tra vị thế và SL/TP trên Binance; chưa tự chạy tiếp.")

    def handle_unexpected(self,exc,stage):
        with self.lock:
            error=self.system_error(exc,stage)
            self.pause(str(error))
            return error

    def snapshot(self):
        with self.lock:
            state=copy.deepcopy(self.s)
            state.pop('fingerprint',None)
            state.update(connected=self.client is not None, account=self.account_summary,
                         mode='binance_'+self.mode, live_trading=self.mode=='live',
                         endpoint=self.adapter.base_url.removeprefix('https://'))
            return state

    @staticmethod
    def usdt(account):
        asset=next((a for a in account['assets'] if a['asset']=='USDT'),None)
        if not asset: raise ExchangeError('Tài khoản Futures chưa có tài sản USDT.')
        return asset

    def ensure_modes(self,client):
        # v3/account only contains balances/positions; flags live in accountConfig.
        config=client.get('/fapi/v1/accountConfig')
        if config.get('canTrade') is not True:
            raise ExchangeError('Tài khoản chưa được phép giao dịch Futures.')
        if config.get('dualSidePosition') is not False:
            raise ExchangeError(f'Cần chuyển tài khoản {self.label} sang One-way Mode trên sàn trước.')
        if config.get('multiAssetsMargin') is not False:
            raise ExchangeError(f'Cần dùng Single-Asset Mode (USDT) trên {self.label}.')
        if client.get('/fapi/v1/feeBurn').get('feeBurn') is not False:
            raise ExchangeError('Tắt trả phí Futures bằng BNB trên Binance trước; bot đối soát phí bằng USDT.')

    def verify_symbol_settings(self,symbol,leverage):
        rows=self.required().get('/fapi/v1/symbolConfig',symbol=symbol)
        row=next((x for x in rows if x['symbol']==symbol),{})
        if row.get('marginType')!='ISOLATED' or int(row.get('leverage',0))!=leverage or str(row.get('isAutoAddMargin')).lower()!='false':
            raise ExchangeError(f'Cần isolated đúng đòn bẩy và tắt Auto-add Margin trên {self.label}.')

    def connect(self,credentials):
        with self.lock:
            if self._closed:
                raise ExchangeError('Chương trình đã đóng kết nối; hãy mở lại ứng dụng.')
            if self.s['status']=='running':
                raise ExchangeError('Phiên đang chạy; không thay kết nối giữa phiên.')
            client=self.client_factory(credentials.api_key.get_secret_value(),credentials.api_secret.get_secret_value())
            if client.mode!=self.mode:
                raise ExchangeError('Kết nối không đúng môi trường giao dịch; chưa gửi yêu cầu.')
            if self.s['status'] in {'running','paused','stopping'} and self.s['fingerprint'] and client.fingerprint!=self.s['fingerprint']:
                raise ExchangeError(f'Phiên chưa kết thúc: cần kết nối lại cùng API Key {self.label} đã mở phiên.')
            client.sync_time()
            account=client.account()
            self.ensure_modes(client)
            asset=self.usdt(account)
            positions=client.positions()
            if self.s['fingerprint'] and self.s['fingerprint']!=client.fingerprint:
                # A completed session may switch account without querying its old IDs
                # against the new account. Preserve that completed history locally.
                state=initial(self.mode)
                with self.db() as db:
                    db.execute('INSERT INTO demo_archives(state) VALUES(?)',(json.dumps(self.s),))
                    db.execute('INSERT OR REPLACE INTO demo_runtime(id,state) VALUES(1,?)',(json.dumps(state),))
                self.s=state
            self.client=client
            self.account_summary={'wallet':str(number(asset['walletBalance'])),
                                  'available':str(number(asset['availableBalance'])),
                                  'open_positions':sum(number(p['positionAmt'])!=0 for p in positions),'checked_at':now()}
            self.event(f'Đã kiểm tra kết nối {self.label} bằng yêu cầu chỉ đọc. Chưa bắt đầu giao dịch.')
            self.save()
            return self.snapshot()

    def required(self):
        if not self.client: raise ExchangeError(f'Nhập khóa {self.label} và kiểm tra kết nối trước.')
        if self._storage_error: raise ExchangeError('Dữ liệu chưa lưu được; cần khởi động lại và đối soát.')
        return self.client

    def load_rules(self,config):
        symbols={s['symbol']:s for s in self.required().exchange_info()['symbols']}
        rules={}
        for symbol in config.symbols:
            if symbol not in symbols: raise ExchangeError(f'{symbol} không tồn tại trên {self.label}.')
            rules[symbol]=SymbolRules(symbols[symbol])
        self.rules=rules

    def start(self,config):
        with self.lock:
            c=self.required()
            if self.s['status'] in {'running','stopping'}: raise ExchangeError('Phiên đang hoạt động.')
            if self.s['status']=='paused': raise ExchangeError('Tiếp tục hoặc đóng phiên đang tạm dừng trước khi tạo phiên mới.')
            self.ensure_modes(c)
            if any(number(p['positionAmt'])!=0 for p in c.positions()) or c.orders() or c.algos():
                raise ExchangeError(f'Cần tài khoản {self.label} riêng, không có vị thế hoặc lệnh chờ trước khi bắt đầu.')
            account=c.account()
            if number(self.usdt(account)['availableBalance']) < config.capital:
                raise ExchangeError('USDT khả dụng trên Futures nhỏ hơn vốn bạn cấp cho bot.')
            self.load_rules(config)
            fees={}
            for symbol in config.symbols:
                mark=c.mark(symbol)
                self.rules[symbol].quantity(config.order_budget,config.leverage,mark)
                for side in ['LONG','SHORT']:
                    self.rules[symbol].triggers(mark,side,config.stop_loss_pct,config.take_profit_pct)
                fees[symbol]=str(number(c.get('/fapi/v1/commissionRate',symbol=symbol)['takerCommissionRate']))
                if not D(0) <= number(fees[symbol]) <= D('.01'): raise ExchangeError('Mức phí tài khoản không hợp lệ.')
                try: c.post('/fapi/v1/marginType',symbol=symbol,marginType='ISOLATED')
                except ExchangeError as exc:
                    if exc.code!=-4046: raise
                result=c.post('/fapi/v1/leverage',symbol=symbol,leverage=config.leverage)
                if int(result['leverage'])!=config.leverage: raise ExchangeError('Sàn chưa xác nhận đúng đòn bẩy.')
                self.verify_symbol_settings(symbol,config.leverage)
            state=initial(self.mode)
            state.update(config=config.model_dump(mode='json'),run_id=uuid.uuid4().hex[:12],
                         fingerprint=c.fingerprint,started_ms=c.sync_time(),equity=str(config.capital),fee_rates=fees,status='running')
            with self.db() as db:
                if self.s['run_id']: db.execute('INSERT INTO demo_archives(state) VALUES(?)',(json.dumps(self.s),))
                db.execute('INSERT OR REPLACE INTO demo_runtime(id,state) VALUES(1,?)',(json.dumps(state),))
            self.s=state
            self.event(f'Phiên {self.label} bắt đầu. Tài khoản dành riêng cho bot.')
            self.save()
            return self.snapshot()

    def _update_op(self,op):
        if op.get('status') in (ALGO_TERMINAL if op['algo'] else TERMINAL): return
        result=self.required().query(op['symbol'],op['client_id'],op['algo'])
        op.update(response=result,status=result.get('algoStatus' if op['algo'] else 'status','UNKNOWN'))
        self.save()

    def _send(self,kind,symbol,params):
        self.s['seq']+=1
        client_id=f"{self.client_prefix}_{self.s['run_id']}_{self.s['seq']}"
        algo=kind in {'sl','tp'}
        params={**params,('clientAlgoId' if algo else 'newClientOrderId'):client_id,'symbol':symbol}
        op=dict(client_id=client_id,kind=kind,symbol=symbol,algo=algo,params=params,status='UNKNOWN',response={})
        self.s['operations'][client_id]=op
        self.save()  # If this fails, no order may be sent.
        try:
            result=self.required().post('/fapi/v1/algoOrder' if algo else '/fapi/v1/order',**params)
        except ExchangeError as exc:
            try:
                self._update_op(op)
                return op
            except ExchangeError:
                if not exc.uncertain:
                    op['status']='REJECTED'
                    self.save()
                raise ExchangeError(str(exc)+' Cần đối soát mã lệnh đã lưu; không gửi lại tự động.',uncertain=exc.uncertain) from None
        op.update(response=result,status=result.get('algoStatus' if algo else 'status','UNKNOWN'))
        if not result.get('algoId' if algo else 'orderId'):
            self.save()
            raise ExchangeError('Sàn chưa xác nhận mã lệnh; cần đối soát.',uncertain=True)
        self.save()
        return op

    def _cancel(self,op):
        terminal=ALGO_TERMINAL if op['algo'] else TERMINAL
        if op['status'] in terminal: return
        try:
            if op['algo']: self.required().delete('/fapi/v1/algoOrder',clientAlgoId=op['client_id'])
            else: self.required().delete('/fapi/v1/order',symbol=op['symbol'],origClientOrderId=op['client_id'])
        except ExchangeError as exc:
            if exc.code not in {-2011,-2013}: raise
        self._update_op(op)
        if op['status'] not in terminal:
            raise ExchangeError('Chưa xác nhận lệnh đã hủy/hoàn tất. Đã ngừng mở lệnh mới.')

    def _reconcile_operations(self, relaxed_algos=False):
        for op in list(self.s['operations'].values()):
            try:
                self._update_op(op)
            except ExchangeError:
                if not (relaxed_algos and op['algo'] and op['status']=='UNKNOWN'): raise
                continue
            actual=op['response'].get('actualOrderId') if op['algo'] else None
            if actual and str(actual)!='0':
                key=op['client_id']+'.fill'
                existing=self.s['operations'].get(key)
                if existing and existing['status'] in TERMINAL: continue
                result=self.required().get('/fapi/v1/order',symbol=op['symbol'],orderId=actual)
                self.s['operations'][key]=dict(client_id=result['clientOrderId'],kind='exit',symbol=op['symbol'],
                    algo=False,params={'side':op['params']['side']},status=result['status'],response=result)
                self.save()

    def _refresh(self, relaxed_algos=False):
        c=self.required()
        self._reconcile_operations(relaxed_algos)
        known_ids={str(op['response']['orderId']) for op in self.s['operations'].values() if not op['algo'] and op['response'].get('orderId')}
        expected={}
        for op in self.s['operations'].values():
            if not op['algo']:
                amount=number(op['response'].get('executedQty',0))*(1 if op['params'].get('side')=='BUY' else -1)
                expected[op['symbol']]=expected.get(op['symbol'],D(0))+amount
        positions={}
        for row in c.positions():
            amount=number(row['positionAmt'])
            if not amount: continue
            if row.get('positionSide')!='BOTH' or amount!=expected.get(row['symbol'],D(0)):
                raise ExchangeError('Vị thế trên sàn không khớp lệnh của bot. Kiểm tra lệnh tay/bot khác trước khi tiếp tục.')
            mark=c.mark(row['symbol'])
            positions[row['symbol']]={'side':'LONG' if amount>0 else 'SHORT','quantity':str(abs(amount)),
                'entry':str(number(row['entryPrice'])),'mark':str(mark),
                'margin':str(number(row['isolatedWallet'])),'unrealized':str(amount*(mark-number(row['entryPrice'])))}
        if any(q and symbol not in positions for symbol,q in expected.items()):
            raise ExchangeError('Số lượng vị thế chưa đồng bộ với lệnh. Hãy đối soát trước khi tiếp tục.')
        self.s['positions']=positions
        ours={op['client_id'] for op in self.s['operations'].values()}
        if any(o.get('clientOrderId') not in ours for o in c.orders()) or any(o.get('clientAlgoId') not in ours for o in c.algos()):
            raise ExchangeError('Phát hiện lệnh chờ ngoài phiên bot; cần tài khoản Futures riêng.')
        for symbol in self.s['config']['symbols']:
            cursor=self.s['trade_cursors'].get(symbol)
            params={'symbol':symbol,'limit':1000}
            params.update({'fromId':cursor+1} if cursor is not None else {'startTime':self.s['started_ms']})
            trades=c.get('/fapi/v1/userTrades',**params)
            if len(trades)>=1000: raise ExchangeError('Quá nhiều giao dịch chưa đối soát; cần kiểm tra lịch sử trên sàn.')
            for trade in sorted(trades,key=lambda x:int(x['id'])):
                if str(trade['orderId']) not in known_ids:
                    raise ExchangeError('Phát hiện giao dịch ngoài bot trên cặp đang quản lý; đã tạm dừng.')
                if trade['commissionAsset']!='USDT': raise ExchangeError('Chưa hỗ trợ trả phí bằng tài sản khác USDT.')
                clean={k:trade[k] for k in ['id','orderId','symbol','side','qty','price','commission','realizedPnl','time']}
                for k in ['qty','price','commission','realizedPnl']: clean[k]=str(number(clean[k]))
                self.s['trades'].append(clean)
                self.s['trade_cursors'][symbol]=int(trade['id'])
        funding=c.get('/fapi/v1/income',incomeType='FUNDING_FEE',startTime=self.s['funding_cursor'] or self.s['started_ms'],limit=1000)
        if len(funding)>=1000: raise ExchangeError('Quá nhiều kỳ funding chưa đối soát; cần kiểm tra lịch sử.')
        seen={(str(f['tranId']),f['symbol']) for f in self.s['funding']}
        for f in funding:
            if f['asset']!='USDT': raise ExchangeError('Funding không phải USDT chưa được hỗ trợ.')
            if f['symbol'] in self.s['config']['symbols'] and (str(f['tranId']),f['symbol']) not in seen:
                self.s['funding'].append({'tranId':str(f['tranId']),'symbol':f['symbol'],'income':str(number(f['income'])),'time':int(f['time'])})
                seen.add((str(f['tranId']),f['symbol']))
            self.s['funding_cursor']=max(self.s['funding_cursor'],int(f['time']))
        filled={}
        for t in self.s['trades']: filled[str(t['orderId'])]=filled.get(str(t['orderId']),D(0))+number(t['qty'])
        self.s['waiting']=any(number(op['response'].get('executedQty',0))>filled.get(str(op['response'].get('orderId')),D(0))
                              for op in self.s['operations'].values() if not op['algo'])
        net=sum((number(t['realizedPnl'])-number(t['commission']) for t in self.s['trades']),D(0))+sum((number(f['income']) for f in self.s['funding']),D(0))
        close_cost=sum((number(p['quantity'])*number(p['mark'])*(number(self.s['fee_rates'][sym])+number(self.s['config']['slippage_bps'])/10000)
                        for sym,p in positions.items()),D(0))
        self.s['net']=str(net)
        self.s['equity']=str(number(self.s['config']['capital'])+net+sum((number(p['unrealized']) for p in positions.values()),D(0))-close_cost)
        asset=self.usdt(c.account())
        self.account_summary={'wallet':str(number(asset['walletBalance'])),'available':str(number(asset['availableBalance'])),
                              'open_positions':len(positions),'checked_at':now()}
        self.save()

    def refresh(self):
        with self.lock:
            self.required()
            if self.s['run_id'] and not self.s.get('closed_externally'):
                try: self._refresh()
                except ExchangeError as exc:
                    self.pause(str(exc)); raise
            else:
                asset=self.usdt(self.client.account())
                self.account_summary={'wallet':str(number(asset['walletBalance'])),'available':str(number(asset['availableBalance'])),
                                      'open_positions':sum(number(p['positionAmt'])!=0 for p in self.client.positions()),'checked_at':now()}
            return self.snapshot()

    def _cleanup(self):
        for op in list(self.s['operations'].values()):
            if op['algo'] and op['symbol'] not in self.s['positions']:
                self._cancel(op)
        for symbol,bracket in list(self.s['brackets'].items()):
            if symbol not in self.s['positions']:
                del self.s['brackets'][symbol]
                self.save()

    def _entry_fill(self,op):
        def positive(value):
            try:
                value=number(value)
                return value if value>0 else None
            except ExchangeError:
                return None

        response=op['response']
        qty=positive(response.get('executedQty'))
        entry=positive(response.get('avgPrice'))
        if qty is not None and entry is not None:
            return qty,entry
        # POST /order can omit avgPrice even when FILLED. GET the same order;
        # never infer the execution price from mark/limit price or resend entry.
        self.event(f"Đang lấy giá khớp của {op['symbol']} theo mã lệnh đã lưu để đặt SL/TP.")
        self.save()
        result=self.required().query(op['symbol'],op['client_id'])
        if (not isinstance(result,dict) or result.get('symbol')!=op['symbol']
                or result.get('clientOrderId')!=op['client_id']
                or result.get('side')!=op['params']['side']
                or str(result.get('orderId'))!=str(response.get('orderId'))):
            raise ExchangeError('Kết quả tra cứu không khớp mã lệnh mở; chưa xác nhận được giá khớp.')
        op.update(response=result,status=result.get('status','UNKNOWN'))
        self.save()
        if op['status'] not in TERMINAL:
            raise ExchangeError('Lệnh mở chưa kết thúc sau khi tra cứu; chưa xác nhận đủ phần khớp.')
        qty=positive(result.get('executedQty'))
        entry=positive(result.get('avgPrice'))
        quote=positive(result.get('cumQuote'))
        if entry is None and qty is not None and quote is not None:
            entry=quote/qty
        if qty is None or entry is None:
            raise ExchangeError('Không lấy được giá/số lượng khớp hợp lệ sau khi tra cứu lệnh; không dùng giá mark để đoán SL/TP.')
        return qty,entry

    def _protect(self,symbol,entry_op):
        config=FuturesConfig(**self.s['config'])
        qty,entry=self._entry_fill(entry_op)
        side='LONG' if entry_op['params']['side']=='BUY' else 'SHORT'
        stop,take=self.rules[symbol].triggers(entry,side,config.stop_loss_pct,config.take_profit_pct)
        bracket=self.s['brackets'].setdefault(symbol,{'entry':entry_op['client_id']})
        for kind,order_type,price in [('sl','STOP_MARKET',stop),('tp','TAKE_PROFIT_MARKET',take)]:
            # Recover acknowledged protection by its persisted intent after a crash.
            if bracket.get(kind): continue
            op=self._send(kind,symbol,{'algoType':'CONDITIONAL','type':order_type,'side':'SELL' if side=='LONG' else 'BUY',
                     'quantity':decimal_text(qty),'triggerPrice':decimal_text(price),'reduceOnly':'true','workingType':'MARK_PRICE'})
            bracket[kind]=op['client_id']
            self.save()
            if op['status']!='NEW': raise ExchangeError('SL/TP chưa ở trạng thái chờ bảo vệ; cần đóng/đối soát vị thế.')
        self.event(f'Đã đặt SL/TP trên {self.label} cho {symbol}.')
        self.save()

    def _open(self,symbol,side):
        config=FuturesConfig(**self.s['config'])
        if symbol not in config.symbols or side not in {'LONG','SHORT'}: raise OrderBlocked('Cặp hoặc chiều lệnh không được phép.')
        if symbol in self.s['positions']: raise OrderBlocked('Cặp này đã có vị thế của bot.')
        if self.s['waiting']: raise OrderBlocked('Đang chờ lịch sử khớp lệnh đồng bộ; chưa mở lệnh mới.')
        self.ensure_modes(self.required())
        self.verify_symbol_settings(symbol,config.leverage)
        mark=self.required().mark(symbol)
        qty=self.rules[symbol].quantity(config.order_budget,config.leverage,mark)
        self.rules[symbol].triggers(mark,side,config.stop_loss_pct,config.take_profit_pct)
        # Retained entry margin must not shrink merely because market price fell.
        used=sum((max(number(p['margin']),number(p['quantity'])*number(p['mark'])/config.leverage)
                  for p in self.s['positions'].values()),D(0))
        reserve=qty*mark/config.leverage+qty*mark*number(self.s['fee_rates'][symbol])*2
        # Unrealized gains must not authorize collateral from the rest of the wallet.
        collateral=min(number(self.s['equity']),config.capital+number(self.s['net']))
        if collateral-used<reserve or number(self.account_summary['available'])<reserve:
            raise OrderBlocked('Vốn của phiên hoặc USDT khả dụng không đủ cho lệnh và phí.')
        op=self._send('entry',symbol,{'side':'BUY' if side=='LONG' else 'SELL','type':'MARKET',
                                     'quantity':decimal_text(qty),'newOrderRespType':'RESULT'})
        if op['status'] not in TERMINAL:
            self._cancel(op)
        try: filled_qty=number(op['response'].get('executedQty'))
        except ExchangeError: filled_qty=None
        if filled_qty is None or filled_qty>0 or op['status']=='FILLED':
            self.s['brackets'][symbol]={'entry':op['client_id']}
            self.save()
            try: self._protect(symbol,op)
            except Exception as exc:
                reason=exc if isinstance(exc,ExchangeError) else self.system_error(exc,'đặt SL/TP sau khi khớp')
                self.s['finish_status']='stopped'
                self.s['status']='stopping'
                self.save()
                try:
                    self._flatten()
                    outcome='Đã xác nhận đóng hết vị thế và lệnh của phiên.'
                except Exception as close_exc:
                    if not isinstance(close_exc,ExchangeError):
                        self.system_error(close_exc,'đóng khẩn cấp sau lỗi SL/TP')
                    outcome='CHƯA xác nhận đóng hết. Kiểm tra và xử lý vị thế ngay trên Binance.'
                raise ExchangeError(f'Không xác nhận được đủ SL/TP. {outcome} Nguyên nhân: {reason}') from None
        else: raise ExchangeError('Lệnh mở không khớp; không tạo vị thế mới.')
        self.event(f'Đã gửi {side} {symbol} trên {self.label}.')
        self._refresh()
        self._limits()

    def manual_order(self,symbol,side,request_id):
        with self.lock:
            self.required()
            if request_id in self.s['manual_requests']: return self.snapshot()
            if self.s['status']!='running' or self.s['config']['strategy']!='manual':
                raise ExchangeError('Cần bắt đầu phiên ở chế độ thủ công.')
            self.s['manual_requests'].append(request_id)
            self.save()
            try:
                self._refresh(); self._cleanup()
                if self._limits(): return self.snapshot()
                self._open(symbol,side)
            except OrderBlocked:
                raise
            except ExchangeError as exc:
                self.pause(str(exc)); raise
            except Exception as exc:
                raise self.handle_unexpected(exc,'đặt lệnh thủ công') from None
            return self.snapshot()

    def _flatten(self,only_symbol=None):
        # Cancel unfilled entry orders first; retain protection until flat is confirmed.
        for op in list(self.s['operations'].values()):
            if op['kind']=='entry' and op['status'] not in TERMINAL and (not only_symbol or op['symbol']==only_symbol): self._cancel(op)
        self._refresh(relaxed_algos=True)
        for symbol,p in list(self.s['positions'].items()):
            if only_symbol and symbol!=only_symbol: continue
            op=self._send('close',symbol,{'side':'SELL' if p['side']=='LONG' else 'BUY','type':'MARKET',
                                          'quantity':p['quantity'],'reduceOnly':'true','newOrderRespType':'RESULT'})
            if op['status'] not in TERMINAL: self._cancel(op)
        self._refresh(relaxed_algos=True)
        if any(not only_symbol or s==only_symbol for s in self.s['positions']):
            raise ExchangeError(f'Chưa xác nhận đóng hết vị thế. Kiểm tra trên {self.label} rồi đối soát.')
        self._cleanup()
        if only_symbol: return
        if self.s['waiting']: raise ExchangeError('Vị thế đã đóng nhưng đang chờ phí/lịch sử khớp đồng bộ; cần đối soát.')
        for op in self.s['operations'].values():
            if op['status'] not in (ALGO_TERMINAL if op['algo'] else TERMINAL):
                raise ExchangeError('Còn lệnh chưa được đối soát; chưa thể xác nhận phiên đã dừng.')
        status=self.s['finish_status'] or 'stopped'
        if status=='target_reached' and number(self.s['equity'])<number(self.s['config']['target']): status='stopped'
        self.s['status']=status
        self.s['finish_status']=None
        self.s['error']=None
        self.event(f'Đã xác nhận hết vị thế/lệnh của phiên trên {self.label}.')
        self.save()

    def stop(self):
        with self.lock:
            self.required()
            if self.s['status'] not in {'running','paused','stopping'}: return self.snapshot()
            self.s['status']='stopping'; self.s['finish_status']=self.s['finish_status'] or 'stopped'
            self.save()
            try: self._flatten()
            except ExchangeError as exc:
                self.pause(str(exc)); raise
            return self.snapshot()

    def finish_external_close(self,confirmed=False):
        """Read-only recovery after the operator closed directly on Binance.

        Keep the incident/history. Never guess final PnL or silently turn manual
        trades into bot fills. Only unlock a new session after verified flatness.
        """
        with self.lock:
            c=self.required()
            if confirmed is not True:
                raise ExchangeError('Cần xác nhận đã đóng vị thế và hủy lệnh còn lại trực tiếp trên Binance.')
            if self.s['status']!='paused' or not self.s['run_id']:
                raise ExchangeError('Chỉ dùng bước này cho phiên đã tạm dừng sau sự cố.')
            self._reconcile_operations()
            for op in self.s['operations'].values():
                if (op['status'] not in (ALGO_TERMINAL if op['algo'] else TERMINAL)
                        or (not op['response'].get('algoId' if op['algo'] else 'orderId') and op['status']!='REJECTED')):
                    raise ExchangeError('Còn mã lệnh chưa xác minh; không thể kết thúc phiên bằng bước xác nhận đóng ngoài bot.')
            if any(number(p['positionAmt'])!=0 for p in c.positions()) or c.orders() or c.algos():
                raise ExchangeError('Binance vẫn còn vị thế hoặc lệnh chờ. Chưa thể xác nhận tài khoản đã trống.')
            asset=self.usdt(c.account())
            self.account_summary={'wallet':str(number(asset['walletBalance'])),'available':str(number(asset['availableBalance'])),
                                  'open_positions':0,'checked_at':now()}
            self.s.update(status='stopped',closed_externally=True,positions={},brackets={},waiting=False,
                          finish_status=None,error=None,equity=None,net=None)
            self.event('Đã kiểm tra tài khoản không còn vị thế/lệnh sau khi xử lý trực tiếp trên Binance. Phiên kết thúc; lãi/lỗ cuối chưa đối soát, không đánh dấu đạt mục tiêu. Lịch sử sự cố được giữ lại.')
            self.save()
            return self.snapshot()

    def resume(self):
        with self.lock:
            self.required()
            if self.s['status']!='paused': raise ExchangeError('Phiên không ở trạng thái tạm dừng.')
            self.ensure_modes(self.required())
            self.load_rules(FuturesConfig(**self.s['config']))
            for symbol in self.s['config']['symbols']:
                self.verify_symbol_settings(symbol,self.s['config']['leverage'])
            try:
                self._refresh()
                if self.s['finish_status']:
                    self._flatten(); return self.snapshot()
                self._cleanup()
                for symbol in self.s['positions']:
                    bracket=self.s['brackets'].get(symbol)
                    if not bracket: raise ExchangeError('Vị thế chưa có bộ SL/TP đã lưu. Bấm Dừng & đóng để kết thúc an toàn.')
                    if not all(bracket.get(k) and self.s['operations'][bracket[k]]['status']=='NEW' for k in ['sl','tp']):
                        raise ExchangeError('SL/TP chưa đầy đủ hoặc đã thay đổi. Bấm Dừng & đóng và kiểm tra trên sàn.')
                if self.s['waiting']: raise ExchangeError('Lịch sử khớp/chi phí chưa đồng bộ. Thử đối soát lại sau.')
                self.s['status']='running'; self.s['error']=None
                self.save()
            except ExchangeError as exc:
                self.pause(str(exc)); raise
            return self.snapshot()

    def _limits(self):
        config=FuturesConfig(**self.s['config'])
        value=number(self.s['equity'])
        status='loss_limit' if value<=config.capital-config.max_loss else 'target_reached' if value>=config.target and not self.s['waiting'] else None
        if not status: return False
        self.s['status']='stopping'; self.s['finish_status']=status
        self.save(); self._flatten()
        return True

    def tick(self):
        with self.lock:
            if not self.client or self.s['status']!='running' or self._storage_error: return
            try:
                self._refresh(); self._cleanup()
                for symbol in self.s['positions']:
                    bracket=self.s['brackets'].get(symbol,{})
                    if not all(bracket.get(k) and self.s['operations'][bracket[k]]['status']=='NEW' for k in ['sl','tp']):
                        self.s['finish_status']='stopped'; self.save()
                        self._flatten()
                        return
                if self._limits() or self.s['waiting'] or self.s['config']['strategy']!='sma': return
                for symbol in self.s['config']['symbols']:
                    server_now=int(self.client._clock()*1000+self.client._offset)
                    closed=[r for r in self.client.candles(symbol) if int(r[6])<server_now]
                    if len(closed)<16: raise ExchangeError('Chưa đủ nến đã đóng để tính SMA.')
                    rows=closed[-16:]
                    if server_now-int(rows[-1][6])>120000 or any(int(b[0])-int(a[0])!=60000 for a,b in zip(rows,rows[1:])):
                        raise ExchangeError('Dữ liệu nến bị cũ hoặc thiếu; đã tạm dừng mở lệnh.')
                    stamp=int(rows[-1][6]); previous=self.s['last_candle'].get(symbol)
                    if previous==stamp: continue
                    self.s['last_candle'][symbol]=stamp; self.save()
                    if previous is None: continue
                    closes=[number(r[4]) for r in rows]
                    if any(p<=0 for p in closes): raise ExchangeError('Giá nến không hợp lệ.')
                    before=sum(closes[-6:-1])/5-sum(closes[-16:-1])/15
                    after=sum(closes[-5:])/5-sum(closes[-15:])/15
                    side='LONG' if before<=0<after else 'SHORT' if before>=0>after else None
                    if not side: continue
                    position=self.s['positions'].get(symbol)
                    if position and position['side']==side: continue
                    if position: self._flatten(only_symbol=symbol)
                    if self._limits() or self.s['waiting']: return
                    self._open(symbol,side)
                    if self.s['status']!='running': return
            except OrderBlocked as exc:
                self.event(str(exc)); self.save()
            except ExchangeError as exc:
                self.pause(str(exc))
            except Exception as exc:
                self.handle_unexpected(exc,'vòng theo dõi tự động')


class DemoService(FuturesService):
    pass


class LiveService(FuturesService):
    mode = 'live'
    label = 'Binance tiền thật'
    adapter = BinanceLive
    client_prefix = 'tpl'

    @staticmethod
    def confirm(confirmation):
        if confirmation!=LIVE_CONFIRMATION:
            raise ExchangeError('Nhập GIAO DICH TIEN THAT để xác nhận bắt đầu hoặc tiếp tục giao dịch tiền thật.')

    def start(self,config,confirmation=None):
        self.confirm(confirmation)
        return super().start(config)

    def resume(self,confirmation=None):
        self.confirm(confirmation)
        return super().resume()
