import os, math, sqlite3, time
from datetime import datetime, timezone
import requests
import streamlit as st
import pandas as pd

API='https://external-api.kalshi.com/trade-api/v2'
DB=os.getenv('DB_PATH','signals.db')
SERIES=['KXBTC15M','KXBTC5M']
REFRESH=30

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
    candles=sorted(candles,key=lambda x:x[0])[-45:]
    closes=[float(c[4]) for c in candles]
    return spot,closes

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

st.set_page_config(page_title='Kalshi BTC Paper Signals',page_icon='₿',layout='wide')
st.title('₿ Kalshi BTC — Paper Signal Dashboard')
st.caption('READ ONLY · NO REAL ORDERS · BTC spot is Coinbase proxy, not the Kalshi settlement index · illustrative model, not validated')
edge=st.sidebar.slider('Minimum estimated edge after fees',0.01,0.25,0.10,0.01)
st.sidebar.write('Refresh: every 30 seconds while this dashboard is open. One paper entry per market. Default 1 contract.')
st.sidebar.warning('For 24/7 recording, use an always-on paid host and an independent scheduler/worker. This dashboard alone does not record while asleep.')

@st.fragment(run_every='30s')
def dashboard():
    con=connect()
    settle(con)
    try:spot,closes=spot_data()
    except Exception as e:
        st.error(f'BTC feed unavailable: {e}. No signal generated.');con.close();return
    st.metric('Coinbase BTC/USD (proxy)',f'${spot:,.2f}')
    st.line_chart(pd.DataFrame({'BTC/USD':closes}))
    for series in SERIES:
        st.subheader(series+' — '+('15-minute' if series.endswith('15M') else '5-minute'))
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
            st.caption('Illustrative model estimate — not a validated forecast')
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
    st.caption('Last refresh: '+datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC'))
    con.close()

dashboard()

