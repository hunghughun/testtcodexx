import copy
from decimal import Decimal as D
import unittest

from pydantic import ValidationError
from bot.engine import Engine, new_state
from bot.models import SessionConfig


class EngineTests(unittest.TestCase):
    def engine(self, **kwargs):
        config = SessionConfig(**{'symbols': ['BTCUSDT'], 'fee_bps': 0, 'slippage_bps': 0, **kwargs})
        state = new_state(config)
        state['status'] = 'running'
        state['prices'] = {'BTCUSDT': '100'}
        return Engine(state)

    def test_user_defaults(self):
        c = SessionConfig()
        self.assertEqual((c.capital, c.target, c.capital-c.max_loss, c.leverage), (500, 1000, 200, 5))
        self.assertEqual(c.symbols, ['BTCUSDT', 'ETHUSDT'])

    def test_invalid_configs_rejected(self):
        for change in [{'target':500}, {'max_loss':500}, {'capital':0}, {'leverage':21},
                       {'leverage':True}, {'symbols':['BTCUSDT','BTCUSDT']}, {'symbols':['BTC/USD']},
                       {'capital':'NaN'}, {'fee_bps':-1}, {'order_budget':501}, {'live':True}]:
            with self.subTest(change=change), self.assertRaises(ValidationError):
                SessionConfig(**change)

    def test_long_and_short_profit_accounting(self):
        for side, exit_price in [('LONG', 104), ('SHORT', 96)]:
            with self.subTest(side=side):
                e = self.engine()
                self.assertTrue(e.open_position('BTCUSDT', side))
                self.assertEqual(D(e.s['cash']), 450)
                self.assertEqual(D(e.s['positions']['BTCUSDT']['quantity']), D('2.5'))
                e.s['prices']['BTCUSDT'] = str(exit_price)
                e.close_position('BTCUSDT', 'test')
                self.assertEqual(D(e.s['cash']), 510)
                self.assertEqual(D(e.s['realized_pnl']), 10)

    def test_fees_and_slippage_both_sides(self):
        e = self.engine(fee_bps=5, slippage_bps=5)
        e.open_position('BTCUSDT', 'LONG')
        position = e.s['positions']['BTCUSDT']
        qty = D(250) / D('100.05')
        expected = D(500) - D('.125') + qty * (D('99.95')-D('100.05')) - qty*D('99.95')*D('.0005')
        self.assertAlmostEqual(e.equity(), expected, places=20)
        e.close_position('BTCUSDT', 'test')
        self.assertAlmostEqual(D(e.s['cash']), expected, places=20)

    def test_allowed_symbols_and_duplicate_guard(self):
        e = self.engine()
        self.assertFalse(e.open_position('SOLUSDT', 'LONG'))
        self.assertTrue(e.open_position('BTCUSDT', 'LONG'))
        self.assertFalse(e.open_position('BTCUSDT', 'SHORT'))
        self.assertEqual(len(e.s['trades']), 1)

    def test_cannot_open_with_insufficient_collateral(self):
        e = self.engine()
        e.s['cash'] = '49'
        self.assertFalse(e.open_position('BTCUSDT', 'LONG'))
        self.assertEqual(e.s['positions'], {})

    def test_stop_loss_and_take_profit_for_each_direction(self):
        for side, price, reason, expected in [('LONG',98,'Stop loss',495), ('LONG',104,'Take profit',510),
                                               ('SHORT',102,'Stop loss',495), ('SHORT',96,'Take profit',510)]:
            with self.subTest(side=side,reason=reason):
                e = self.engine()
                e.open_position('BTCUSDT', side)
                e.tick({'BTCUSDT':price})
                self.assertEqual(e.s['positions'], {})
                self.assertEqual(e.s['trades'][-1]['reason'], reason)
                self.assertEqual(e.equity(), expected)

    def test_target_uses_net_liquidation_equity(self):
        e = self.engine(target=510, fee_bps=10)
        e.open_position('BTCUSDT','LONG')
        e.s['prices']['BTCUSDT']='104'
        self.assertFalse(e.check_limits())  # Gross profit is 10, net profit is lower.
        e.s['prices']['BTCUSDT']='105'
        self.assertTrue(e.check_limits())
        self.assertEqual(e.s['status'],'target_reached')
        self.assertFalse(e.s['positions'])
        self.assertGreaterEqual(D(e.s['cash']),510)
        before=copy.deepcopy(e.s)
        e.tick({'BTCUSDT':90})
        self.assertEqual(e.s,before)

    def test_global_loss_limit_closes_every_position(self):
        e = self.engine(symbols=['BTCUSDT','ETHUSDT'],max_loss=20)
        e.s['prices']['ETHUSDT']='100'
        e.open_position('BTCUSDT','LONG')
        e.open_position('ETHUSDT','SHORT')
        e.tick({'BTCUSDT':90,'ETHUSDT':110})
        self.assertEqual(e.s['status'],'loss_limit')
        self.assertEqual(e.s['positions'],{})
        self.assertEqual(D(e.s['cash']),450)

    def test_isolated_liquidation_never_uses_extra_cash(self):
        for side, mark in [('LONG',1),('SHORT',1000)]:
            with self.subTest(side=side):
                e = self.engine()
                e.open_position('BTCUSDT',side)
                e.tick({'BTCUSDT':mark})
                self.assertEqual(D(e.s['cash']),450)
                self.assertEqual(e.s['trades'][-1]['reason'],'Thanh lý mô phỏng')

    def test_funding_debits_long_credits_short_and_reconciles(self):
        for side, cost in [('LONG',D('.025')),('SHORT',D('-.025'))]:
            with self.subTest(side=side):
                e = self.engine()
                e.open_position('BTCUSDT',side)
                e.s['tick']=479
                e.tick({'BTCUSDT':100})
                self.assertEqual(D(e.s['funding_paid']),cost)
                self.assertEqual(e.equity(),D(500)-cost)
                e.close_position('BTCUSDT','test')
                self.assertEqual(D(e.s['cash'])-500,D(e.s['realized_pnl']))
                self.assertEqual(D(e.s['trades'][-1]['pnl']),-cost)

    def test_funding_can_liquidate_an_isolated_position(self):
        e = self.engine(funding_bps=100)
        e.open_position('BTCUSDT','LONG')
        e.s['positions']['BTCUSDT']['margin']='1'
        e.s['tick']=479
        e.tick({'BTCUSDT':100})
        self.assertEqual(e.s['positions'],{})
        self.assertEqual(D(e.s['cash']),450)

    def test_quotes_validated_atomically(self):
        e = self.engine()
        for prices in [{'BTCUSDT':0},{'BTCUSDT':-1},{'BTCUSDT':'NaN'}, {'BTCUSDT':'Infinity'}, {}, {'SOLUSDT':100}]:
            before=copy.deepcopy(e.s)
            with self.subTest(prices=prices), self.assertRaises(ValueError):
                e.tick(prices)
            self.assertEqual(e.s,before)

    def test_crossover_generates_long_and_short_without_lookahead(self):
        for history, last, side in [([100]*15,101,'LONG'),([100]*15,99,'SHORT')]:
            with self.subTest(side=side):
                e=self.engine()
                for price in history:
                    e.tick({'BTCUSDT':price})
                    self.assertEqual(e.s['trades'],[])
                e.tick({'BTCUSDT':last})
                self.assertEqual(e.s['positions']['BTCUSDT']['side'],side)

    def test_manual_stop_flattens_and_blocks_reentry(self):
        e=self.engine()
        e.open_position('BTCUSDT','SHORT')
        e.stop()
        self.assertFalse(e.s['positions'])
        self.assertFalse(e.open_position('BTCUSDT','LONG'))
