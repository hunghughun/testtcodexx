"""Isolated USDT perpetual futures simulation, never an exchange order client.

Synthetic minute bars; simplified 0.5% maintenance margin; fixed funding rate
per 480 bars. Explicit modelling assumptions, not Binance liquidation rules.
"""
from datetime import datetime, timezone
from decimal import Decimal

from .models import SessionConfig

D = Decimal
MAINTENANCE_RATE = D('0.005')


def now():
    return datetime.now(timezone.utc).isoformat()


def new_state(config: SessionConfig):
    return {
        'config': config.model_dump(mode='json'),
        'status': 'idle', 'reason': 'Chưa bắt đầu', 'cash': str(config.capital),
        'positions': {}, 'prices': {}, 'histories': {}, 'cooldowns': {},
        'trades': [], 'equity_history': [], 'events': [], 'tick': 0,
        'created_at': now(), 'updated_at': now(), 'realized_pnl': '0', 'funding_paid': '0',
    }


class Engine:
    def __init__(self, state):
        self.s = state
        self.c = SessionConfig(**state['config'])

    @property
    def fee(self):
        return self.c.fee_bps / 10000

    @property
    def slippage(self):
        return self.c.slippage_bps / 10000

    def event(self, message):
        self.s['events'].append({'at': now(), 'message': message})
        self.s['events'] = self.s['events'][-100:]

    def valuation(self, symbol):
        p = self.s['positions'][symbol]
        direction = D(1) if p['side'] == 'LONG' else D(-1)
        mark, quantity = D(self.s['prices'][symbol]), D(p['quantity'])
        exit_price = mark * (1 - direction * self.slippage)
        exit_fee = quantity * exit_price * self.fee
        unrealized = direction * quantity * (mark - D(p['entry']))
        liquidated = D(p['margin']) + unrealized <= quantity * mark * MAINTENANCE_RATE + exit_fee
        pnl = direction * quantity * (exit_price - D(p['entry']))
        # Conservative liquidation model: forfeit the remaining isolated margin.
        proceeds = D(0) if liquidated else max(D(0), D(p['margin']) + pnl - exit_fee)
        return {'proceeds': proceeds, 'exit_price': exit_price, 'exit_fee': exit_fee,
                'unrealized': unrealized, 'liquidated': liquidated}

    def equity(self):
        """Estimated cash after closing all positions, including exit costs."""
        return D(self.s['cash']) + sum((self.valuation(s)['proceeds'] for s in self.s['positions']), D(0))

    def open_position(self, symbol, side):
        if self.s['status'] != 'running' or symbol not in self.c.symbols or side not in {'LONG', 'SHORT'}:
            return False
        if symbol in self.s['positions'] or symbol not in self.s['prices']:
            return False
        margin, notional = self.c.order_budget, self.c.order_budget * self.c.leverage
        entry_fee = notional * self.fee
        if D(self.s['cash']) < margin + entry_fee:
            return False
        mark = D(self.s['prices'][symbol])
        if not mark.is_finite() or mark <= 0:
            raise ValueError('Giá phải là số dương hữu hạn.')
        direction = D(1) if side == 'LONG' else D(-1)
        fill = mark * (1 + direction * self.slippage)
        quantity = notional / fill
        self.s['cash'] = str(D(self.s['cash']) - margin - entry_fee)
        self.s['positions'][symbol] = {
            'side': side, 'quantity': str(quantity), 'entry': str(fill),
            'margin': str(margin), 'entry_fee': str(entry_fee), 'funding_paid': '0',
            'stop': str(fill * (1 - direction * self.c.stop_loss_pct / 100)),
            'take': str(fill * (1 + direction * self.c.take_profit_pct / 100)),
            'opened_at': now(),
        }
        self.trade(symbol, 'OPEN', side, quantity, fill, entry_fee, None, 'Giao cắt SMA 5/15')
        self.event(f'Mở {side} {symbol} mô phỏng, isolated {self.c.leverage}x; đã đặt ngưỡng SL/TP.')
        return True

    def trade(self, symbol, action, side, quantity, fill, fee, pnl, reason):
        self.s['trades'].append({
            'id': len(self.s['trades']) + 1, 'at': now(), 'symbol': symbol,
            'action': action, 'side': side, 'quantity': str(quantity), 'price': str(fill),
            'fee': str(fee), 'pnl': str(pnl) if pnl is not None else None, 'reason': reason,
        })

    def close_position(self, symbol, reason):
        value = self.valuation(symbol)
        position = self.s['positions'].pop(symbol)
        if value['liquidated']:
            reason = 'Thanh lý mô phỏng'
        pnl = value['proceeds'] - D(position['margin']) - D(position['entry_fee'])
        self.s['cash'] = str(D(self.s['cash']) + value['proceeds'])
        self.s['realized_pnl'] = str(D(self.s['realized_pnl']) + pnl)
        self.s['cooldowns'][symbol] = self.s['tick'] + 5
        self.trade(symbol, 'CLOSE', position['side'], D(position['quantity']), value['exit_price'],
                   D(0) if value['liquidated'] else value['exit_fee'], pnl - D(position['funding_paid']), reason)
        self.event(f"Đóng {position['side']} {symbol}: {reason}.")

    def stop(self, status='stopped', reason='Dừng theo yêu cầu'):
        for symbol in list(self.s['positions']):
            self.close_position(symbol, reason)
        self.s['status'], self.s['reason'], self.s['updated_at'] = status, reason, now()
        self.event(reason)

    def check_limits(self):
        equity = self.equity()
        if equity >= self.c.target:
            self.stop('target_reached', 'Đã đạt mục tiêu sau phí và trượt giá')
            return True
        if equity <= self.c.capital - self.c.max_loss:
            self.stop('loss_limit', 'Đã chạm mức lỗ tối đa')
            return True
        return False

    def funding(self):
        if self.s['tick'] % 480:
            return
        for symbol, p in self.s['positions'].items():
            direction = D(1) if p['side'] == 'LONG' else D(-1)
            payment = direction * D(p['quantity']) * D(self.s['prices'][symbol]) * self.c.funding_bps / 10000
            # Funding draws from isolated margin; never transfer extra collateral.
            payment = min(payment, D(p['margin'])) if payment > 0 else payment
            p['margin'] = str(D(p['margin']) - payment)
            p['funding_paid'] = str(D(p['funding_paid']) + payment)
            self.s['funding_paid'] = str(D(self.s['funding_paid']) + payment)
            self.s['realized_pnl'] = str(D(self.s['realized_pnl']) - payment)
            self.event(f'Funding mô phỏng {symbol}: {payment:.4f} USDT (dương = trả).')

    def tick(self, prices):
        if self.s['status'] != 'running':
            return
        if set(prices) != set(self.c.symbols):
            raise ValueError('Thiếu giá hoặc có mã nằm ngoài danh sách được phép.')
        normalized = {s: D(str(p)) for s, p in prices.items()}
        if any(not p.is_finite() or p <= 0 for p in normalized.values()):
            raise ValueError('Giá không hợp lệ; đã ngừng xử lý phiên.')
        self.s['tick'] += 1
        self.s['prices'] = {s: str(p) for s, p in normalized.items()}
        for symbol, price in normalized.items():
            history = self.s['histories'].setdefault(symbol, [])
            history.append(str(price))
            self.s['histories'][symbol] = history[-16:]
        self.funding()
        if not self.check_limits():
            for symbol, p in list(self.s['positions'].items()):
                price, direction = normalized[symbol], (1 if p['side'] == 'LONG' else -1)
                if self.valuation(symbol)['liquidated']:
                    self.close_position(symbol, 'Thanh lý mô phỏng')
                elif direction * (price - D(p['stop'])) <= 0:
                    self.close_position(symbol, 'Stop loss')
                elif direction * (price - D(p['take'])) >= 0:
                    self.close_position(symbol, 'Take profit')
            if not self.check_limits():
                for symbol in self.c.symbols:
                    history = [D(p) for p in self.s['histories'][symbol]]
                    if len(history) < 16 or self.s['tick'] < self.s['cooldowns'].get(symbol, 0):
                        continue
                    old_delta = sum(history[-6:-1]) / 5 - sum(history[-16:-1]) / 15
                    delta = sum(history[-5:]) / 5 - sum(history[-15:]) / 15
                    side = 'LONG' if old_delta <= 0 < delta else 'SHORT' if old_delta >= 0 > delta else None
                    if side:
                        existing = self.s['positions'].get(symbol)
                        if existing and existing['side'] != side:
                            self.close_position(symbol, 'Tín hiệu đảo chiều')
                        if self.check_limits():
                            break
                        self.open_position(symbol, side)
                        if self.check_limits():
                            break
        self.s['updated_at'] = now()
        self.s['equity_history'].append({'tick': self.s['tick'], 'equity': str(self.equity())})
        self.s['equity_history'] = self.s['equity_history'][-240:]

    def snapshot(self):
        equity = self.equity()
        positions = {s: {**p, 'unrealized_pnl': str(self.valuation(s)['unrealized'])} for s, p in self.s['positions'].items()}
        return {**self.s, 'positions': positions, 'equity': str(equity), 'pnl': str(equity - self.c.capital),
                'progress': str(max(D(0), min(D(100), (equity - self.c.capital) / (self.c.target - self.c.capital) * 100))),
                'mode': 'paper', 'market': 'futures', 'price_source': 'synthetic', 'margin_mode': 'isolated'}
