"""Deterministic exchange fixture; no HTTP or user credentials."""
from decimal import Decimal as D
import hashlib
from bot.binance import ExchangeError


class FakeBinance:
    def __init__(self,key='k'*32,secret='s'*32,mode='demo'):
        self.mode=mode
        self.fingerprint=hashlib.sha256(key.encode()).hexdigest()
        self._offset=0
        self.millis=1800000000000
        self._clock=lambda:self.millis/1000
        self.wallet=D(5000)
        self.market={'BTCUSDT':D(100),'ETHUSDT':D(50)}
        self.position={}
        self.regular={}
        self.conditional={}
        self.trades=[]
        self.funding=[]
        self.writes=[]
        self.hedge=False
        self.multi=False
        self.can_trade=True
        self.fee_burn=False
        self.next_id=1
        self.timeout_entry=False
        self.unknown_entry=False
        self.partial_entry=False
        self.fail_tp=False
        self.unknown_tp=False
        self.stale=False
        self.closes=[D(100)]*16
        self.fees_delay=False

    def sync_time(self):return self.millis
    def account(self):
        return {'assets':[{'asset':'USDT','walletBalance':str(self.wallet),'availableBalance':str(self.wallet-50*len(self.position))}]}
    def positions(self):
        return [{'symbol':s,'positionSide':'BOTH','positionAmt':str(p['qty']),'entryPrice':str(p['entry']),
                 'markPrice':str(self.market[s]),'isolatedWallet':str(abs(p['qty'])*p['entry']/5),
                 'unRealizedProfit':str(p['qty']*(self.market[s]-p['entry']))} for s,p in self.position.items() if p['qty']]
    def orders(self):return [o.copy() for o in self.regular.values() if o['status'] in {'NEW','PARTIALLY_FILLED'}]
    def algos(self):return [o.copy() for o in self.conditional.values() if o['algoStatus']=='NEW']
    def exchange_info(self):
        return {'symbols':[{'symbol':s,'status':'TRADING','quoteAsset':'USDT','contractType':'PERPETUAL','filters':[
            {'filterType':'LOT_SIZE','stepSize':'0.001','minQty':'0.001','maxQty':'1000'},
            {'filterType':'MARKET_LOT_SIZE','stepSize':'0.001','minQty':'0.001','maxQty':'1000'},
            {'filterType':'PRICE_FILTER','tickSize':'0.1','minPrice':'0.1','maxPrice':'1000000'},
            {'filterType':'MIN_NOTIONAL','notional':'5'}]} for s in self.market]}
    def mark(self,s):return self.market[s]
    def candles(self,s):
        end=self.millis-1000-(300000 if self.stale else 0)
        return [[end-(15-i)*60000-59999,'0','0','0',str(p),'1',end-(15-i)*60000] for i,p in enumerate(self.closes)]
    def query(self,symbol,client_id,algo=False):
        source=self.conditional if algo else self.regular
        if client_id not in source:raise ExchangeError('Order missing',code=-2013)
        return source[client_id].copy()
    def get(self,path,**params):
        if path.endswith('/accountConfig'):return {'canTrade':self.can_trade,'dualSidePosition':self.hedge,'multiAssetsMargin':self.multi}
        if path.endswith('/feeBurn'):return {'feeBurn':self.fee_burn}
        if path.endswith('/positionSide/dual'):return {'dualSidePosition':self.hedge}
        if path.endswith('/multiAssetsMargin'):return {'multiAssetsMargin':self.multi}
        if path.endswith('/commissionRate'):return {'takerCommissionRate':'0.0005'}
        if path.endswith('/symbolConfig'):return [{'symbol':params['symbol'],'marginType':'ISOLATED','leverage':5,'isAutoAddMargin':'false'}]
        if path.endswith('/userTrades'):
            if self.fees_delay:return []
            return [t.copy() for t in self.trades if t['symbol']==params['symbol'] and t['id']>=params.get('fromId',0) and t['time']>=params.get('startTime',0)]
        if path.endswith('/income'):return [f.copy() for f in self.funding if f['time']>=params['startTime']]
        if path.endswith('/order'):
            return next(o.copy() for o in self.regular.values() if o['orderId']==params['orderId'])
        raise AssertionError((path,params))
    def _market(self,params,partial=False):
        symbol=params['symbol']; price=self.market[symbol]
        qty=D(params['quantity'])/(2 if partial else 1)
        direction=1 if params['side']=='BUY' else -1
        existing=self.position.get(symbol,{'qty':D(0),'entry':price})
        realized=D(0)
        if params.get('reduceOnly')=='true':
            assert existing['qty']*direction<0, 'reduce-only must oppose existing position'
            qty=min(qty,abs(existing['qty']))
            realized=(1 if existing['qty']>0 else -1)*qty*(price-existing['entry'])
        else:assert not existing['qty'], 'one entry at a time'
        existing['qty']+=direction*qty
        self.position[symbol]=existing
        fee=qty*price*D('.0005'); self.wallet+=realized-fee
        order={'orderId':self.next_id,'clientOrderId':params['newClientOrderId'],'symbol':symbol,'side':params['side'],
               'executedQty':str(qty),'avgPrice':str(price),'status':'PARTIALLY_FILLED' if partial else 'FILLED'}
        self.next_id+=1
        self.regular[order['clientOrderId']]=order
        self.trades.append({'id':len(self.trades)+1,'orderId':order['orderId'],'symbol':symbol,'side':params['side'],
                            'qty':str(qty),'price':str(price),'commission':str(fee),'commissionAsset':'USDT',
                            'realizedPnl':str(realized),'time':self.millis})
        return order.copy()
    def post(self,path,**params):
        self.writes.append((path,params.copy()))
        if path.endswith('/marginType'):return {'code':200}
        if path.endswith('/leverage'):return {'leverage':params['leverage']}
        if path.endswith('/order'):
            entry=params.get('reduceOnly')!='true'
            if entry and self.unknown_entry:raise ExchangeError('Timeout',uncertain=True)
            result=self._market(params,partial=entry and self.partial_entry)
            if entry and self.timeout_entry:raise ExchangeError('Timeout after fill',uncertain=True)
            # The official POST /order response does not guarantee avgPrice.
            # Query-by-ID retains it, matching the production recovery path.
            result.pop('avgPrice',None)
            return result
        if path.endswith('/algoOrder'):
            if params['type']=='TAKE_PROFIT_MARKET':
                if self.fail_tp:raise ExchangeError('Rejected TP',code=-2021)
                if self.unknown_tp:raise ExchangeError('TP timeout',uncertain=True)
            result={**params,'algoId':self.next_id,'algoStatus':'NEW','actualOrderId':0}
            self.next_id+=1
            self.conditional[params['clientAlgoId']]=result
            return result.copy()
        raise AssertionError(path)
    def delete(self,path,**params):
        self.writes.append((path,params.copy()))
        source=self.conditional if path.endswith('/algoOrder') else self.regular
        key=params.get('clientAlgoId',params.get('origClientOrderId'))
        if key not in source:raise ExchangeError('Missing',code=-2011)
        source[key]['algoStatus' if path.endswith('/algoOrder') else 'status']='CANCELED'
        return source[key].copy()
    def trigger(self,kind):
        algo=next(a for a in self.conditional.values() if a['type']==kind and a['algoStatus']=='NEW')
        result=self._market({'symbol':algo['symbol'],'side':algo['side'],'quantity':algo['quantity'],
                             'reduceOnly':'true','newClientOrderId':f"native-{algo['algoId']}"})
        algo.update(algoStatus='FINISHED',actualOrderId=result['orderId'])
