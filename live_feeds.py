"""Best-effort in-process WebSocket feeds. Read-only; no order endpoints."""
import json, os, threading, time
from collections import deque
from datetime import datetime, timezone
import requests
import websocket

class Feeds:
    def __init__(self):
        self.lock=threading.RLock()
        self.btc=None; self.btc_at=0; self.kalshi={}; self.kalshi_at={}
        self.trades=deque(maxlen=1800); self.status={'coinbase':'connecting','kalshi':'not configured'}
        self.started=False; self.subscribed=set(); self.kalshi_ws=None
    def start(self):
        with self.lock:
            if self.started:return
            self.started=True
        threading.Thread(target=self._coinbase_loop,daemon=True).start()
        if os.getenv('KALSHI_API_KEY_ID') and os.getenv('KALSHI_PRIVATE_KEY'):
            threading.Thread(target=self._kalshi_loop,daemon=True).start()
    def subscribe(self,ticker):
        with self.lock:
            if ticker in self.subscribed:return
            self.subscribed.add(ticker)
            ws=self.kalshi_ws
        if ws:
            try:ws.send(json.dumps({'id':int(time.time()),'cmd':'subscribe','params':{'channels':['ticker'],'market_tickers':[ticker]}}))
            except Exception:pass
    def market_snapshot(self,ticker):
        with self.lock:
            age=time.time()-self.kalshi_at[ticker] if ticker in self.kalshi_at else None
            return dict(self.kalshi.get(ticker,{})),age
    def _coinbase_loop(self):
        while True:
            def on_open(ws):
                ws.send(json.dumps({'type':'subscribe','product_ids':['BTC-USD'],'channels':['ticker']}))
                with self.lock:self.status['coinbase']='connected'
            def on_message(ws,raw):
                try:
                    m=json.loads(raw)
                    if m.get('type')!='ticker':return
                    price=float(m['price']); now=time.time()
                    with self.lock:
                        self.btc=price;self.btc_at=now;self.trades.append((now,price))
                except (ValueError,KeyError,TypeError):pass
            def on_error(ws,e):
                with self.lock:self.status['coinbase']='error: '+str(e)[:90]
            def on_close(ws,*args):
                with self.lock:self.status['coinbase']='disconnected'
            try:
                websocket.WebSocketApp('wss://ws-feed.exchange.coinbase.com',on_open=on_open,on_message=on_message,on_error=on_error,on_close=on_close).run_forever(ping_interval=25,ping_timeout=10)
            except Exception as e:
                with self.lock:self.status['coinbase']='error: '+str(e)[:90]
            time.sleep(3)
    def _kalshi_loop(self):
        # Authentication uses Kalshi's RSA-PSS signing scheme. Credentials from server environment only.
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        key_id=os.environ['KALSHI_API_KEY_ID']
        private=serialization.load_pem_private_key(os.environ['KALSHI_PRIVATE_KEY'].replace('\\n','\n').encode(),password=None)
        path='/trade-api/ws/v2'
        while True:
            timestamp=str(int(time.time()*1000))
            signature=private.sign((timestamp+'GET'+path).encode(),padding.PSS(mgf=padding.MGF1(hashes.SHA256()),salt_length=padding.PSS.DIGEST_LENGTH),hashes.SHA256())
            import base64
            headers=[f'KALSHI-ACCESS-KEY: {key_id}',f'KALSHI-ACCESS-TIMESTAMP: {timestamp}',f'KALSHI-ACCESS-SIGNATURE: {base64.b64encode(signature).decode()}']
            def on_open(ws):
                with self.lock:
                    self.status['kalshi']='connected';self.kalshi_ws=ws
                # Subscribe to series ticker updates. API may reject series subscriptions; REST remains authoritative fallback.
                ws.send(json.dumps({'id':1,'cmd':'subscribe','params':{'channels':['ticker'],'market_tickers':list(self.subscribed)}})) if self.subscribed else None
            def on_message(ws,raw):
                try:
                    m=json.loads(raw);msg=m.get('msg') or {}
                    ticker=msg.get('market_ticker') or msg.get('ticker')
                    if ticker and m.get('type')=='ticker':
                        with self.lock:self.kalshi[ticker]=msg;self.kalshi_at[ticker]=time.time()
                    if m.get('type')=='error':
                        with self.lock:self.status['kalshi']='subscription error: '+str(msg)[:100]
                except (ValueError,TypeError):pass
            def on_error(ws,e):
                with self.lock:self.status['kalshi']='error: '+str(e)[:90]
            def on_close(ws,*args):
                with self.lock:
                    self.status['kalshi']='disconnected';self.kalshi_ws=None
            try:
                websocket.WebSocketApp('wss://external-api.kalshi.com'+path,header=headers,on_open=on_open,on_message=on_message,on_error=on_error,on_close=on_close).run_forever(ping_interval=25,ping_timeout=10)
            except Exception as e:
                with self.lock:self.status['kalshi']='error: '+str(e)[:90]
            time.sleep(5)
    def snapshot(self):
        with self.lock:return {'btc':self.btc,'btc_age':time.time()-self.btc_at if self.btc_at else None,'status':dict(self.status),'trades':list(self.trades)}

_FEED=Feeds()
def feeds():
    _FEED.start()
    return _FEED
