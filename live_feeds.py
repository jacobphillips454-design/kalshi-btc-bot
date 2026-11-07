"""Read-only websocket price cache. No order endpoints are used."""
import base64
import json
import os
import threading
import time
from datetime import datetime, timezone

import requests
import websocket

COINBASE_WS = 'wss://ws-feed.exchange.coinbase.com'
KALSHI_WS = 'wss://api.elections.kalshi.com/trade-api/ws/v2'
KALSHI_API = 'https://external-api.kalshi.com/trade-api/v2'

class LiveFeed:
    def __init__(self, key_id='', private_key=''):
        self.lock = threading.RLock()
        self.price = None
        self.price_at = 0
        self.kalshi = {}
        self.kalshi_at = {}
        self.active_ticker = None
        self.status = {'coinbase': 'connecting', 'kalshi': 'not configured'}
        self.key_id, self.private_key = key_id, private_key
        threading.Thread(target=self._coinbase_loop, daemon=True).start()
        if key_id and private_key:
            threading.Thread(target=self._kalshi_loop, daemon=True).start()

    def snapshot(self):
        with self.lock:
            return dict(price=self.price, price_at=self.price_at,
                        kalshi={k: dict(v) for k,v in self.kalshi.items()},
                        kalshi_at=dict(self.kalshi_at), active_ticker=self.active_ticker,
                        status=dict(self.status))

    def _coinbase_loop(self):
        backoff = 1
        while True:
            try:
                def opened(ws):
                    ws.send(json.dumps({'type':'subscribe','product_ids':['BTC-USD'], 'channels':['ticker']}))
                    with self.lock: self.status['coinbase']='connected'
                def message(ws, raw):
                    msg=json.loads(raw)
                    if msg.get('type')=='ticker' and msg.get('product_id')=='BTC-USD':
                        try: price=float(msg['price'])
                        except (ValueError, TypeError, KeyError): return
                        with self.lock:
                            self.price=price
                            self.price_at=time.time()
                ws=websocket.WebSocketApp(COINBASE_WS, on_open=opened, on_message=message)
                ws.run_forever(ping_interval=25, ping_timeout=10)
            except Exception:
                pass
            with self.lock: self.status['coinbase']='reconnecting'
            time.sleep(backoff)
            backoff=min(backoff*2, 30)

    def _auth_headers(self):
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        key=serialization.load_pem_private_key(self.private_key.replace('\\n','\n').encode(), password=None)
        ts=str(int(time.time()*1000))
        payload=(ts+'GET'+'/trade-api/ws/v2').encode()
        signature=key.sign(payload, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),salt_length=padding.PSS.DIGEST_LENGTH),hashes.SHA256())
        return [f'KALSHI-ACCESS-KEY: {self.key_id}',f'KALSHI-ACCESS-TIMESTAMP: {ts}',f'KALSHI-ACCESS-SIGNATURE: {base64.b64encode(signature).decode()}']

    def _watch_markets(self, ws):
        def watcher():
            counter=1
            previous=None
            while ws.sock and ws.sock.connected:
                try:
                    response=requests.get(KALSHI_API+'/markets', params={
                        'series_ticker':'KXBTC15M','status':'open','limit':30},timeout=8)
                    response.raise_for_status()
                    now=time.time()
                    candidates=[]
                    for m in response.json().get('markets',[]):
                        close=m.get('close_time') or m.get('expiration_time')
                        if not close: continue
                        ts=datetime.fromisoformat(close.replace('Z','+00:00')).timestamp()
                        if ts>now: candidates.append((ts,m['ticker']))
                    if candidates:
                        ticker=min(candidates)[1]
                        if ticker!=previous:
                            counter+=1
                            ws.send(json.dumps({'id':counter,'cmd':'subscribe',
                                'params':{'channels':['ticker'],'market_tickers':[ticker]}}))
                            previous=ticker
                            with self.lock: self.active_ticker=ticker
                except Exception as exc:
                    with self.lock: self.status['kalshi']='market discovery retrying: '+str(exc)[:70]
                time.sleep(15)
        threading.Thread(target=watcher, daemon=True).start()

    def _kalshi_loop(self):
        backoff=1
        while True:
            try:
                headers=self._auth_headers()
                def opened(ws):
                    with self.lock: self.status['kalshi']='connected'
                    # Subscribe to the currently open 15-minute contract.
                    self._watch_markets(ws)
                def message(ws, raw):
                    msg=json.loads(raw)
                    if msg.get('type')=='error':
                        with self.lock: self.status['kalshi']='subscription error: '+str(msg.get('msg',''))[:120]
                        return
                    if msg.get('type')!='ticker': return
                    m=msg.get('msg') or {}
                    ticker=m.get('market_ticker') or m.get('ticker')
                    if not ticker or not ticker.startswith('KXBTC15M-'): return
                    with self.lock:
                        self.kalshi[ticker]=dict(m)
                        self.kalshi_at[ticker]=time.time()
                ws=websocket.WebSocketApp(KALSHI_WS,header=headers,on_open=opened,on_message=message)
                ws.run_forever(ping_interval=25,ping_timeout=10)
            except Exception as exc:
                with self.lock: self.status['kalshi']='reconnecting: '+str(exc)[:80]
            time.sleep(backoff)
            backoff=min(backoff*2,30)
