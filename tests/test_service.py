from decimal import Decimal as D
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from bot.models import SessionConfig
from bot.service import Service, Conflict
from bot.web import create_app


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'test.sqlite3'
        self.service=Service(self.path)

    def test_restart_requires_manual_resume_and_preserves_positions(self):
        s=self.service
        s.start()
        for _ in range(25): s.tick()
        old=s.snapshot()
        self.assertTrue(old['trades'])
        loaded=Service(self.path).snapshot()
        self.assertEqual(loaded['status'],'paused')
        self.assertEqual(loaded['positions'],old['positions'])
        self.assertEqual(loaded['cash'],old['cash'])
        self.assertEqual(loaded['trades'],old['trades'])

    def test_cannot_configure_running_and_archives_finished_sessions(self):
        s=self.service
        s.start()
        with self.assertRaises(Conflict): s.configure(SessionConfig())
        s.stop()
        s.configure(SessionConfig(capital=600))
        history=s.archive()
        self.assertEqual(len(history),2)
        self.assertEqual(history[0]['state']['status'],'stopped')
        self.assertEqual(D(history[1]['state']['cash']),600)

    def test_duplicate_start_stop_are_idempotent(self):
        s=self.service
        threads=[threading.Thread(target=s.start) for _ in range(10)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(len(s.snapshot()['events']),1)
        for _ in range(25):s.tick()
        s.stop()
        first=s.snapshot()
        s.stop()
        self.assertEqual(first,s.snapshot())

    def test_failed_commit_does_not_advance_memory(self):
        s=self.service
        s.start()
        original=s.snapshot()
        with patch.object(s,'connect',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):s.tick()
        self.assertEqual(s.snapshot(),original)

    def test_extended_simulation_has_trades_and_reconciles(self):
        s=self.service
        s.start()
        for _ in range(1000):s.tick()
        s.stop()
        state=s.snapshot()
        self.assertGreater(len(state['trades']),10)
        self.assertEqual(state['positions'],{})
        self.assertAlmostEqual(D(state['cash'])-D(state['config']['capital']),D(state['realized_pnl']),places=18)
        closed_pnl=sum((D(t['pnl']) for t in state['trades'] if t['action']=='CLOSE'),D(0))
        self.assertAlmostEqual(closed_pnl,D(state['realized_pnl']),places=18)
        self.assertTrue({'LONG','SHORT'}.issubset({t['side'] for t in state['trades']}))

    def test_api_end_to_end_and_validation(self):
        app=create_app(self.path,run_worker=False)
        with TestClient(app) as client:
            self.assertEqual(client.get('/').status_code,200)
            self.assertFalse(client.get('/api/health').json()['live_trading'])
            configured=client.post('/api/config',json=SessionConfig().model_dump(mode='json'))
            self.assertEqual(configured.status_code,200)
            self.assertEqual(client.post('/api/start').json()['status'],'running')
            self.assertEqual(client.post('/api/config',json={}).status_code,409)
            self.assertEqual(client.post('/api/stop').json()['status'],'stopped')
            self.assertEqual(client.post('/api/start').status_code,409)
            self.assertEqual(client.post('/api/config',json={'leverage':30}).status_code,422)
            self.assertGreaterEqual(len(client.get('/api/export').json()),2)

    def test_cross_origin_writes_and_unknown_hosts_rejected(self):
        with TestClient(create_app(self.path,run_worker=False)) as client:
            self.assertEqual(client.post('/api/start',headers={'Origin':'https://bad.example'}).status_code,403)
            self.assertEqual(client.post('/api/start',headers={'Sec-Fetch-Site':'cross-site'}).status_code,403)
            self.assertEqual(client.get('/api/state',headers={'Host':'bad.example'}).status_code,403)
            self.assertEqual(client.post('/api/start',headers={'Origin':'http://testserver'}).status_code,200)
