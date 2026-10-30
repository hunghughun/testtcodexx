"""Fixed-environment Futures REST adapters; no automatic write retries."""
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import hashlib
import hmac
import json
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

DEMO_URL = 'https://demo-fapi.binance.com'
LIVE_URL = 'https://fapi.binance.com'
D = Decimal


class ExchangeError(Exception):
    def __init__(self, message, code=None, uncertain=False):
        super().__init__(message)
        self.code, self.uncertain = code, uncertain


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def number(value):
    result = D(str(value))
    if not result.is_finite():
        raise ExchangeError('Sàn trả về số liệu không hợp lệ.')
    return result


def decimal_text(value):
    return format(value, 'f')


def rounded(value, step, up=False):
    return (value / step).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN) * step


class BinanceFutures:
    base_url = DEMO_URL
    mode = 'demo'
    label = 'Binance Demo'

    def __init__(self, api_key, api_secret, *, opener=None, clock=time.time):
        self._key, self._secret = api_key, api_secret
        self._clock = clock
        self._offset = 0
        self._synced = 0
        self._blocked_until = 0
        self._opener = opener or build_opener(NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))

    @property
    def fingerprint(self):
        return hashlib.sha256(self._key.encode()).hexdigest()

    def request(self, method, path, params=None, signed=False):
        if not path.startswith('/fapi/') or '?' in path or '#' in path or '..' in path:
            raise ExchangeError('Đường dẫn Binance không được phép.')
        if self._clock() < self._blocked_until:
            raise ExchangeError('Đang chờ Binance hết giới hạn truy cập. Hãy thử lại sau.')
        params = dict(params or {})
        headers = {'User-Agent': 'TradePilot-Futures/0.3'}
        if signed:
            if self._clock() - self._synced > 60:
                self.sync_time()
            params.update(timestamp=int(self._clock()*1000 + self._offset), recvWindow=5000)
            headers['X-MBX-APIKEY'] = self._key
        query = urlencode(params)
        if signed:
            signature = hmac.new(self._secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            query += '&signature=' + signature
        url = self.base_url + path
        data = None
        if method in {'POST', 'PUT'}:
            data = query.encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        elif query:
            url += '?' + query
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=10) as response:
                return json.loads(response.read())
        except HTTPError as exc:
            # Never expose request URLs, signatures, keys or raw exchange messages.
            code = None
            try:
                payload = json.loads(exc.read(8192))
                code = payload.get('code') if isinstance(payload, dict) else None
                if not isinstance(code, int): code = None
            except (ValueError, OSError):
                pass
            if exc.code in {418, 429}:
                try: delay = max(60, int(exc.headers.get('Retry-After', '60')))
                except (TypeError, ValueError): delay = 60
                self._blocked_until = self._clock() + delay
            descriptions = {-2015:f'Khóa, quyền Futures hoặc IP API không hợp lệ trên {self.label}.', -2014:'Sai định dạng API Key.',
                            -1022:f'Sai API Secret hoặc loại khóa; cần khóa HMAC của {self.label}.',
                            -1021:'Đồng hồ lệch với Binance; hãy kiểm tra giờ máy tính và kết nối lại.',
                            -2019:'Không đủ ký quỹ Futures.', -2013:'Chưa tìm thấy lệnh trên sàn.',
                            -4046:'Ký quỹ đã ở chế độ yêu cầu.', -2021:'Giá kích hoạt SL/TP đã bị vượt qua.',
                            -2022:'Sàn từ chối lệnh giảm vị thế.'}
            message = descriptions.get(code, f'{self.label} từ chối yêu cầu (HTTP {exc.code}, mã {code}).')
            if exc.code == 451:
                message = f'{self.label} không khả dụng từ vị trí mạng hiện tại.'
            uncertain = method != 'GET' and (code in {-1001,-1006,-1007} or exc.code >= 500 or exc.code in {408, 418, 429} or 300 <= exc.code < 400)
            raise ExchangeError(message, code=code, uncertain=uncertain) from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise ExchangeError(f'Không nhận được phản hồi hợp lệ từ {self.label}. Kiểm tra mạng/proxy; không tự gửi lại lệnh.',
                                uncertain=method != 'GET') from None

    def sync_time(self):
        before = self._clock()*1000
        result = self.request('GET', '/fapi/v1/time')
        after = self._clock()*1000
        self._offset = int(result['serverTime']) - (before+after)/2
        self._synced = self._clock()
        return int(result['serverTime'])

    def get(self, path, **params):
        return self.request('GET', path, params, signed=True)

    def post(self, path, **params):
        return self.request('POST', path, params, signed=True)

    def delete(self, path, **params):
        return self.request('DELETE', path, params, signed=True)

    def account(self): return self.get('/fapi/v3/account')
    def positions(self): return self.get('/fapi/v3/positionRisk')
    def orders(self): return self.get('/fapi/v1/openOrders')
    def algos(self): return self.get('/fapi/v1/openAlgoOrders')
    def exchange_info(self): return self.request('GET', '/fapi/v1/exchangeInfo')
    def mark(self, symbol):
        result = self.request('GET', '/fapi/v1/premiumIndex', {'symbol':symbol})
        if abs(int(self._clock()*1000 + self._offset)-int(result['time'])) > 30000:
            raise ExchangeError('Giá mark đã cũ; không mở lệnh mới.')
        price = number(result['markPrice'])
        if price <= 0: raise ExchangeError('Giá mark không hợp lệ.')
        return price
    def candles(self, symbol):
        return self.request('GET', '/fapi/v1/klines', {'symbol':symbol, 'interval':'1m', 'limit':20})
    def query(self, symbol, client_id, algo=False):
        if algo: return self.get('/fapi/v1/algoOrder', clientAlgoId=client_id)
        return self.get('/fapi/v1/order', symbol=symbol, origClientOrderId=client_id)


class BinanceDemo(BinanceFutures):
    """Demo keys are only ever sent to the Demo endpoint."""


class BinanceLive(BinanceFutures):
    base_url = LIVE_URL
    mode = 'live'
    label = 'Binance tiền thật'


class SymbolRules:
    def __init__(self, info):
        if info.get('status') != 'TRADING' or info.get('quoteAsset') != 'USDT' or info.get('contractType') != 'PERPETUAL':
            raise ExchangeError('Chỉ hỗ trợ hợp đồng USDT PERPETUAL đang giao dịch.')
        filters = {f['filterType']:f for f in info['filters']}
        self.symbol = info['symbol']
        self.base_asset = info.get('baseAsset', self.symbol.removesuffix('USDT'))
        self.lot = filters['LOT_SIZE']
        self.market = filters.get('MARKET_LOT_SIZE', self.lot)
        self.price = filters['PRICE_FILTER']
        self.tick = number(self.price['tickSize'])
        self.step = number(self.market['stepSize']) or number(self.lot['stepSize'])
        if self.step <= 0 or self.tick <= 0: raise ExchangeError('Bước giá/số lượng không hợp lệ.')
        notional = filters.get('MIN_NOTIONAL', filters.get('NOTIONAL', {}))
        self.min_notional = number(notional.get('notional', notional.get('minNotional', 0)))

    def quantity_error(self, reason, budget, leverage, mark, raw_qty, qty):
        # These are the exchangeInfo filters loaded for this session, never
        # hard-coded BTC limits. Show sizing inputs without any account secrets.
        details = []
        for label, rule in [('LOT_SIZE', self.lot), ('MARKET_LOT_SIZE', self.market)]:
            details.append(f"{label}: tối thiểu {rule['minQty']}, tối đa {rule['maxQty']}, bước {rule['stepSize']} {self.base_asset}")
        return ExchangeError(
            f'{self.symbol}: {reason}. '
            f'Ký quỹ {decimal_text(budget)} USDT × {leverage}x / giá mark {decimal_text(mark)} USDT '
            f'= {decimal_text(raw_qty)} {self.base_asset}; sau làm tròn xuống còn {decimal_text(qty)} {self.base_asset}. '
            + '; '.join(details) + '. '
            f'Giá trị lệnh sau làm tròn: {decimal_text(qty*mark)} USDT; '
            f'giá trị lệnh tối thiểu: {decimal_text(self.min_notional)} USDT. '
            'Đây là giới hạn số lượng/giá trị vị thế, không phải số dư ví. Chưa gửi lệnh; bot không tự tăng ký quỹ.'
        )

    def quantity(self, budget, leverage, mark):
        raw_qty = budget * leverage / mark
        qty = rounded(raw_qty, self.step)
        for rule in [self.lot, self.market]:
            step = number(rule['stepSize'])
            if qty < number(rule['minQty']):
                raise self.quantity_error('Số lượng sau làm tròn nhỏ hơn tối thiểu', budget, leverage, mark, raw_qty, qty)
            if qty > number(rule['maxQty']):
                raise self.quantity_error('Số lượng sau làm tròn vượt tối đa', budget, leverage, mark, raw_qty, qty)
            if step and qty % step:
                raise self.quantity_error('Số lượng không khớp bước Binance cho phép', budget, leverage, mark, raw_qty, qty)
        if qty <= 0 or qty*mark < self.min_notional:
            raise self.quantity_error('Giá trị lệnh nhỏ hơn mức tối thiểu', budget, leverage, mark, raw_qty, qty)
        return qty

    def triggers(self, entry, side, stop_pct, take_pct):
        direction = 1 if side == 'LONG' else -1
        stop = rounded(entry*(1-direction*stop_pct/100), self.tick, up=side=='LONG')
        take = rounded(entry*(1+direction*take_pct/100), self.tick, up=side=='SHORT')
        for price in [stop,take]:
            if price <= 0 or price < number(self.price['minPrice']) or price > number(self.price['maxPrice']):
                raise ExchangeError('Giá SL/TP vượt giới hạn Binance.')
        if direction*(entry-stop) <= 0 or direction*(take-entry) <= 0:
            raise ExchangeError('Khoảng SL/TP nhỏ hơn bước giá cho phép.')
        return stop, take
