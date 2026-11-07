import os, math, sqlite3, time
from datetime import datetime, timezone
import requests
import streamlit as st
import pandas as pd

API='https://external-api.kalshi.com/trade-api/v2'
DB=os.getenv('DB_PATH','signals.db')
SERIES=['KXBTC15M']
REFRESH=1

def get(url,params=None):
    r=requests.get(url,params=params,timeout=12,headers={'User-Agent':'BTC-Paper-Dashboard/1.0'})
    r.raise_for_status()
    return r.json()

def number(v):
    try: return float(v)
    except (TypeError,ValueError): return None

def quote(m,side):
    # Prefer dollar-quoted ask; otherwise derive executable ask from opposite best bid.
    val=number(m.get(side+'_ask_dollars'))
    if val is not None: return val
    val=number(m.get(side+'_ask'))
    if val is not None: return val/100
    other='no' if side=='yes' else 'yes'
    val=number(m.get(other+'_bid_dollars'))
    if val is not None: return 1-val
    val=number(m.get(other+'_bid'))
    if val is not None: return 1-val/100
    return None

def fee(price,contracts=1):
    # General taker fee, rounded up to the cent for an order.
    return math.ceil((0.07*contracts*price*(1-price)-1e-12)*100)/100

def connect():
    con=sqlite3.connect(DB,timeout=15)
    con.execute('CREATE TABLE IF NOT EXISTS paper (ticker TEXT PRIMARY KEY, series TEXT, side TEXT, entry REAL, fee REAL, strike REAL, spot REAL, created INTEGER, close_ts INTEGER, status TEXT DEFAULT "OPEN", result TEXT, pnl REAL)')
    con.execute('''CREATE TABLE IF NOT EXISTS forecasts (ticker TEXT PRIMARY KEY, predicted_at INTEGER NOT NULL, close_ts INTEGER NOT NULL, seconds_left INTEGER NOT NULL, p_up REAL NOT NULL, direction TEXT NOT NULL, strike REAL NOT NULL, proxy_spot REAL NOT NULL, status TEXT NOT NULL DEFAULT 'OPEN', result TEXT, correct INTEGER, brier REAL)''')
    cols={row[1] for row in con.execute('PRAGMA table_info(forecasts)')}
    for name in ('experimental_p_up','momentum_1m','momentum_5m','rsi_14','volatility_1m','volume_ratio','target_distance_pct','experimental_correct','experimental_brier'):
        if name not in cols:
            con.execute(f'ALTER TABLE forecasts ADD COLUMN {name} REAL')
    con.commit();return con

def market_data(series):
    data=get(API+'/markets',{'series_ticker':series,'status':'open','limit':30})
    now=time.time()
    markets=[]
    for m in data.get('markets',[]):
        close=m.get('close_time') or m.get('expiration_time')
        try: ts=datetime.fromisoformat(close.replace('Z','+00:00')).timestamp()
        except (TypeError,ValueError,AttributeError):continue
        if ts>now and m.get('status') in ('active','open',None):
            markets.append((ts,m))
    markets.sort(key=lambda x:x[0])
    return markets[0] if markets else None

def spot_data():
    # Coinbase USD spot is a PROXY, not Kalshi's CF Benchmarks settlement index.
    p=get('https://api.coinbase.com/v2/prices/BTC-USD/spot')
    spot=float(p['data']['amount'])
    candles=get('https://api.exchange.coinbase.com/products/BTC-USD/candles',{'granularity':60})
    candles=sorted((c for c in candles if float(c[0])+60 <= time.time()),key=lambda x:x[0])[-45:]
    closes=[float(c[4]) for c in candles]
    return spot,closes,candles

def market_features(spot, strike, closes, candles):
    """Features from completed 1-minute Coinbase candles, not Kalshi settlement data."""
    if len(closes)<22 or len(candles)<22 or not strike or strike<=0:
        return None
    returns=[math.log(b/a) for a,b in zip(closes,closes[1:]) if a>0 and b>0]
    if len(returns)<20: return None
    last=returns[-20:]
    avg=sum(last)/len(last)
    volatility=math.sqrt(sum((r-avg)**2 for r in last)/(len(last)-1))
    gains=[max(0, r) for r in returns[-14:]]
    losses=[max(0,-r) for r in returns[-14:]]
    gain=sum(gains)/14; loss=sum(losses)/14
    rsi=100 if loss==0 and gain>0 else (50 if loss==0 else 100-100/(1+gain/loss))
    vols=[float(c[5]) for c in candles]
    base=sum(vols[-21:-1])/20
    return {'momentum_1m':returns[-1], 'momentum_5m':math.log(closes[-1]/closes[-6]),
            'rsi_14':rsi,'volatility_1m':volatility,
            'volume_ratio':(vols[-1]/base if base>0 else 1.0),
            'target_distance_pct':(spot/strike-1)*100}

def experimental_probability(base_p, features, seconds):
    """EXPERIMENTAL heuristic. Not trained/calibrated; compare only, do not trade from it."""
    if base_p is None or features is None: return None
    # Keep modifications modest; features are correlated and coefficients are provisional.
    logit=math.log(max(.001,min(.999,base_p))/(1-max(.001,min(.999,base_p))))
    momentum=(.65*features['momentum_5m']+.35*features['momentum_1m'])
    vol=max(features['volatility_1m'],.00005)
    signal=max(-2,min(2,momentum/(vol*math.sqrt(5))))
    volume_weight=min(1.5,max(.5,features['volume_ratio']))
    rsi_tilt=max(-1,min(1,(features['rsi_14']-50)/25))
    adjustment=(.22*signal*volume_weight+.08*rsi_tilt)*min(1,seconds/300)
    z=max(-8,min(8,logit+adjustment))
    return 1/(1+math.exp(-z))

def probability(spot,strike,seconds,closes):
    # Illustrative zero-drift lognormal model; not a validated forecast.
    returns=[math.log(b/a) for a,b in zip(closes,closes[1:]) if a>0 and b>0]
    if len(returns)<15:return None
    mean=sum(returns)/len(returns)
    sd=math.sqrt(sum((x-mean)**2 for x in returns)/max(1,len(returns)-1))
    sigma=max(sd,0.00005)*math.sqrt(max(1,seconds)/60)
    z=math.log(spot/strike)/sigma
    return max(.001,min(.999,.5*(1+math.erf(z/math.sqrt(2)))))

def evaluate(m,close_ts,spot,closes,edge_threshold):
    strike=number(m.get('floor_strike'))
    if strike is None: return {'signal':'SKIP','reason':'Official strike not yet published'}
    left=max(0,int(close_ts-time.time()))
    if left<45:return {'signal':'SKIP','reason':'Less than 45 seconds remain','strike':strike,'left':left}
    p=probability(spot,strike,left,closes)
    if p is None:return {'signal':'SKIP','reason':'Not enough BTC price history','strike':strike,'left':left}
    asks={'UP':quote(m,'yes'),'DOWN':quote(m,'no')}
    if any(v is None or not .01<=v<=.99 for v in asks.values()):
        return {'signal':'SKIP','reason':'Missing or invalid executable quotes','strike':strike,'left':left}
    values={side:((p if side=='UP' else 1-p)-ask-fee(ask)) for side,ask in asks.items()}
    best=max(values,key=values.get)
    sig=best if values[best]>=edge_threshold else 'SKIP'
    return {'signal':sig,'reason':('Illustrative estimated edge clears threshold' if sig!='SKIP' else 'Insufficient estimated edge after fees'), 'strike':strike,'left':left,'p':p,'asks':asks,'edges':values,'best':best}

def record(con,ticker,series,res,spot,close_ts):
    side=res['signal']
    if side=='SKIP':return False
    entry=res['asks'][side]
    con.execute('INSERT OR IGNORE INTO paper(ticker,series,side,entry,fee,strike,spot,created,close_ts,status) VALUES (?,?,?,?,?,?,?,?,?,?)',(ticker,series,side,entry,fee(entry),res['strike'],spot,int(time.time()),int(close_ts),'OPEN'))
    changed=con.execute('SELECT changes()').fetchone()[0]>0
    con.commit();return changed

def settle(con):
    # Only official Kalshi result counts. Never settle using Coinbase spot.
    rows=con.execute('SELECT ticker,side,entry,fee FROM paper WHERE status="OPEN" AND close_ts<?',(int(time.time())-30,)).fetchall()
    for ticker,side,entry,cost in rows:
        try:
            data=get(API+'/markets/'+ticker).get('market',{})
            result=str(data.get('result') or '').lower()
            if result not in ('yes','no'):
                data=get(API+'/historical/markets/'+ticker).get('market',{})
                result=str(data.get('result') or '').lower()
            if result not in ('yes','no'):continue
            won=(result=='yes')==(side=='UP')
            pnl=(1 if won else 0)-entry-cost
            con.execute('UPDATE paper SET status="SETTLED",result=?,pnl=? WHERE ticker=?',(result,round(pnl,4),ticker))
            con.commit()
        except (requests.RequestException,ValueError):pass

def record_forecast(con,ticker,res,spot,close_ts,features=None,experimental_p=None):
    # One immutable forecast per contract, captured when 5-10 minutes remain.
    # No later edits or cherry-picking of a better prediction.
    p=res.get('p')
    left=res.get('left')
    if p is None or left is None or not (300 <= left <= 600):
        return False
    direction='UP' if p >= .5 else 'DOWN'
    f=features or {}
    cur=con.execute('''INSERT OR IGNORE INTO forecasts
        (ticker,predicted_at,close_ts,seconds_left,p_up,direction,strike,proxy_spot,
         experimental_p_up,momentum_1m,momentum_5m,rsi_14,volatility_1m,volume_ratio,target_distance_pct)
         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (ticker,int(time.time()),int(close_ts),left,p,direction,res['strike'],spot,
         experimental_p,*(f.get(k) for k in ('momentum_1m','momentum_5m','rsi_14','volatility_1m','volume_ratio','target_distance_pct'))))
    con.commit()
    return cur.rowcount > 0

def settle_forecasts(con):
    rows=con.execute('SELECT ticker,p_up,direction,experimental_p_up FROM forecasts WHERE status="OPEN" AND close_ts<?',(int(time.time())-30,)).fetchall()
    for ticker,p_up,direction,experimental_p_up in rows:
        try:
            data=get(API+'/markets/'+ticker).get('market',{})
            result=str(data.get('result') or '').lower()
            if result not in ('yes','no'):
                data=get(API+'/historical/markets/'+ticker).get('market',{})
                result=str(data.get('result') or '').lower()
            if result not in ('yes','no'): continue
            actual=1 if result=='yes' else 0
            correct=int((direction=='UP')==bool(actual))
            brier=(p_up-actual)**2
            exp_correct=(int((experimental_p_up>=.5)==bool(actual)) if experimental_p_up is not None else None)
            exp_brier=((experimental_p_up-actual)**2 if experimental_p_up is not None else None)
            con.execute('''UPDATE forecasts SET status="SETTLED",result=?,correct=?,brier=?,
                        experimental_correct=?,experimental_brier=? WHERE ticker=?''',
                        (result,correct,brier,exp_correct,exp_brier,ticker))
            con.commit()
        except (requests.RequestException,ValueError,KeyError):
            pass

st.set_page_config(page_title='Kalshi BTC Paper Signals',page_icon='₿',layout='wide')
st.title('₿ Kalshi BTC 15-Minute — Paper Signals')
st.caption('READ ONLY · NO REAL ORDERS · BTC spot is Coinbase proxy, not the Kalshi settlement index · illustrative model, not validated')
edge=st.sidebar.slider('Minimum estimated edge after fees',0.01,0.25,0.10,0.01)
st.sidebar.write(f'Refresh target: every {REFRESH} second(s) while open; network requests may take longer. One paper entry per market.')
st.sidebar.info('Stage 2: baseline plus experimental indicators. Stable REST-only baseline. No WebSocket, API keys, or live execution. More advanced features require measured out-of-sample validation.')
st.sidebar.warning('For 24/7 recording, use an always-on paid host and an independent scheduler/worker. This dashboard alone does not record while asleep.')

@st.fragment(run_every=f'{REFRESH}s')
def dashboard():
    con=connect()
    settle(con)
    settle_forecasts(con)
    try:spot,closes,candles=spot_data()
    except Exception as e:
        st.error(f'BTC feed unavailable: {e}. No signal generated.');con.close();return
    st.metric('Coinbase BTC/USD (proxy)',f'${spot:,.2f}')
    st.line_chart(pd.DataFrame({'BTC/USD':closes}))
    for series in SERIES:
        st.subheader('BTC 15-minute contract')
        try:current=market_data(series)
        except Exception as e:
            st.warning(f'Kalshi feed unavailable: {e}');continue
        if not current:
            st.info('No currently open market found. This series may not be listed. No simulated 5-minute contract is substituted.')
            continue
        close_ts,m=current
        res=evaluate(m,close_ts,spot,closes,edge)
        
        # Directional forecast independent of paper-trade decision
        p_up = res.get('p')
        if p_up is None and 'strike' in res and res.get('left', 0) > 0:
            p_up = probability(spot, res['strike'], res['left'], closes)
        if p_up is not None:
            direction = 'UP' if p_up >= 0.5 else 'DOWN'
            confidence = p_up if direction == 'UP' else 1 - p_up
            st.markdown(f"## {'🟢' if direction == 'UP' else '🔴'} BTC PREDICTION: {direction}")
            st.metric('Estimated direction probability', f'{confidence:.1%}')
            st.caption(f'Model: UP {p_up:.1%} | DOWN {1-p_up:.1%}')
            st.caption('Illustrative model estimate — not a validated forecast. No accuracy claim until backtested.')
        else:
            st.warning('Direction unavailable: insufficient market data')
  
        st.write('Market:',m['ticker'],'|',res['signal'],'|',res['reason'])
        a,b,c,d=st.columns(4)
        a.metric('Target',f"${res['strike']:,.2f}" if 'strike' in res else 'Unavailable')
        b.metric('Seconds left',res.get('left','—'))
        c.metric('UP ask',f"{res['asks']['UP']:.0%}" if 'asks' in res else '—')
        d.metric('DOWN ask',f"{res['asks']['DOWN']:.0%}" if 'asks' in res else '—')
        if 'p' in res:
            st.write(f"Model UP probability: {res['p']:.1%} | UP net edge: {res['edges']['UP']:+.1%} | DOWN net edge: {res['edges']['DOWN']:+.1%}")
        features=market_features(spot,res.get('strike'),closes,candles) if res.get('strike') else None
        exp_p=experimental_probability(p_up,features,res.get('left',0))
        st.subheader('Market indicators — experimental')
        if features:
            a,b,c=st.columns(3)
            a.metric('5m momentum',f"{features['momentum_5m']*100:+.3f}%")
            b.metric('RSI (14)',f"{features['rsi_14']:.1f}")
            c.metric('Relative volume',f"{features['volume_ratio']:.2f}x")
            a,b,c=st.columns(3)
            a.metric('1m realized volatility',f"{features['volatility_1m']*100:.3f}%")
            b.metric('Spot vs target',f"{features['target_distance_pct']:+.3f}%")
            c.metric('1m momentum',f"{features['momentum_1m']*100:+.3f}%")
            if exp_p is not None:
                st.write(f"Baseline UP: **{p_up:.1%}** | Experimental UP: **{exp_p:.1%}**")
                st.caption('Experimental model is an unvalidated heuristic, not a calibrated trading signal. Existing paper decisions still use baseline.')
        else:
            st.info('Waiting for enough Coinbase candle history to calculate indicators.')
        if record_forecast(con,m['ticker'],res,spot,close_ts,features,exp_p):
            st.success('Accuracy forecast saved for this market (5–10 minutes remaining)')
        if res['signal']!='SKIP':
            if record(con,m['ticker'],series,res,spot,close_ts):st.success('New one-contract simulated paper entry recorded')
    df=pd.read_sql_query('SELECT ticker,series,side,entry,fee,status,result,pnl,datetime(created,"unixepoch") AS entered_utc FROM paper ORDER BY created DESC LIMIT 150',con)
    st.subheader('Paper trading log')
    if len(df):
        settled=df[df.status=='SETTLED']
        a,b,c=st.columns(3)
        a.metric('Settled paper trades',len(settled))
        b.metric('Paper P/L',f"${settled.pnl.sum():+.2f}")
        c.metric('Win rate',f"{((settled.pnl>0).mean()):.1%}" if len(settled) else '—')
        st.dataframe(df,use_container_width=True,hide_index=True)
        st.download_button('Export paper trades CSV',df.to_csv(index=False),'paper_trades.csv','text/csv')
    else:st.info('No simulated trades yet. SKIP is normal and preferred when data is insufficient.')
    st.subheader('Prediction accuracy — official settled markets')
    st.caption('One locked forecast per 15-minute contract, first observed with 5–10 minutes left. Recorded even when paper trade says SKIP. Results use official Kalshi YES/NO outcomes.')
    forecasts=pd.read_sql_query('SELECT ticker, direction, p_up, experimental_p_up, momentum_5m, rsi_14, volatility_1m, volume_ratio, target_distance_pct, seconds_left, status, result, correct, brier, experimental_correct, experimental_brier, datetime(predicted_at,"unixepoch") AS predicted_utc FROM forecasts ORDER BY predicted_at DESC LIMIT 500',con)
    done=forecasts[forecasts.status=='SETTLED']
    x,y,z=st.columns(3)
    x.metric('Settled forecasts',len(done))
    y.metric('Directional accuracy',f"{done.correct.mean():.1%}" if len(done) else '—')
    z.metric('Brier score (lower is better)',f"{done.brier.mean():.3f}" if len(done) else '—')
    tested=done[done.experimental_brier.notna()]
    if len(tested):
        a,b,c=st.columns(3)
        a.metric('Comparable settled markets',len(tested))
        b.metric('Experimental accuracy',f"{tested.experimental_correct.mean():.1%}")
        c.metric('Experimental Brier',f"{tested.experimental_brier.mean():.3f}")
        st.caption('Compare baseline and experimental on the SAME settled markets. Early samples are not evidence of a durable edge.')
    if len(done):
        st.caption('Baseline Brier score for always predicting 50%: 0.250. Accuracy alone does not measure profitability.')
    if len(forecasts):
        st.dataframe(forecasts,use_container_width=True,hide_index=True)
        st.download_button('Export forecast accuracy CSV',forecasts.to_csv(index=False),'btc_forecast_accuracy.csv','text/csv',key='export_forecasts')
    else:
        st.info('Waiting to observe a contract with 5–10 minutes remaining. Leave the dashboard open to capture forecasts.')
    st.warning('Streamlit Community Cloud local SQLite storage can reset on redeploy/restart. Export the CSV regularly; this is not durable 24/7 tracking.')
    st.caption('Last refresh: '+datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC'))
    con.close()

dashboard()

