"""Read-only chart and probability history add-on for the existing Kalshi bot."""
import sqlite3
import time
from datetime import datetime, timezone

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
import streamlit as st

CANDLE_URL = 'https://api.exchange.coinbase.com/products/BTC-USD/candles'


def init_history(con):
    con.execute('''CREATE TABLE IF NOT EXISTS signal_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        observed INTEGER NOT NULL, ticker TEXT NOT NULL, series TEXT NOT NULL,
        signal TEXT NOT NULL, spot REAL, strike REAL, up_prob REAL,
        up_ask REAL, down_ask REAL, up_edge REAL, down_edge REAL,
        reason TEXT)''')
    con.execute('CREATE INDEX IF NOT EXISTS idx_signal_history ON signal_history(series, observed)')
    con.commit()


def log_signal(con, ticker, series, res, spot):
    """Save snapshots, including SKIP. Does not place any orders."""
    init_history(con)
    asks = res.get('asks') or {}
    edges = res.get('edges') or {}
    con.execute('''INSERT INTO signal_history
        (observed,ticker,series,signal,spot,strike,up_prob,up_ask,down_ask,up_edge,down_edge,reason)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
        (int(time.time()), ticker, series, res.get('signal', 'SKIP'), spot,
         res.get('strike'), res.get('p'), asks.get('UP'), asks.get('DOWN'),
         edges.get('UP'), edges.get('DOWN'), res.get('reason')))
    con.execute('DELETE FROM signal_history WHERE observed < ?', (int(time.time())-14*86400,))
    con.commit()


def candles_for(interval):
    seconds = {'1m': 60, '5m': 300, '15m': 900}[interval]
    r = requests.get(CANDLE_URL, params={'granularity': seconds}, timeout=12,
                     headers={'User-Agent': 'BTC-Paper-Dashboard/1.1'})
    r.raise_for_status()
    raw = r.json()
    if not isinstance(raw, list) or not raw:
        raise ValueError('No candle data returned')
    df = pd.DataFrame(raw, columns=['time', 'low', 'high', 'open', 'close', 'volume'])
    for c in ['low', 'high', 'open', 'close', 'volume']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)
    df = df.dropna().drop_duplicates('time').sort_values('time').tail(300).copy()
    if len(df) < 35:
        raise ValueError('Not enough candles for indicators')
    df['ema9'] = df.close.ewm(span=9, adjust=False).mean()
    df['ema21'] = df.close.ewm(span=21, adjust=False).mean()
    delta = df.close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    rs = gain / loss.replace(0, float('nan'))
    df['rsi'] = (100 - 100 / (1 + rs)).fillna(50)
    df['macd'] = df.close.ewm(span=12, adjust=False).mean() - df.close.ewm(span=26, adjust=False).mean()
    df['macd_signal'] = df.macd.ewm(span=9, adjust=False).mean()
    df['macd_hist'] = df.macd - df.macd_signal
    return df


def show_charts(con):
    st.header('Live BTC charts and signal tracking')
    st.caption('Coinbase BTC/USD proxy, not Kalshi settlement index. Refreshes with the existing 30-second dashboard. Read-only.')
    interval = st.selectbox('Candle timeframe', ['1m', '5m', '15m'], index=1, key='chart_candle_interval')
    try:
        df = candles_for(interval)
    except (requests.RequestException, ValueError, KeyError) as exc:
        st.warning(f'Candlestick data unavailable: {exc}')
        df = None
    if df is not None:
        fig = make_subplots(rows=4, cols=1, shared_xaxes=True,
                            row_heights=[0.55, 0.14, 0.15, 0.16], vertical_spacing=0.035)
        fig.add_trace(go.Candlestick(x=df.time, open=df.open, high=df.high,
                                    low=df.low, close=df.close, name='BTC'), row=1, col=1)
        for name, color in [('ema9', '#f4c542'), ('ema21', '#5aa9ff')]:
            fig.add_trace(go.Scatter(x=df.time, y=df[name], name=name.upper(),
                                     line=dict(color=color, width=1.5)), row=1, col=1)
        fig.add_trace(go.Bar(x=df.time, y=df.volume, name='Volume', marker_color='#8296a9'), row=2, col=1)
        fig.add_trace(go.Scatter(x=df.time, y=df.rsi, name='RSI 14', line=dict(color='#ba8aff')), row=3, col=1)
        for level in [30, 70]:
            fig.add_hline(y=level, row=3, col=1, line_dash='dot', line_color='#777')
        fig.add_trace(go.Bar(x=df.time, y=df.macd_hist, name='MACD histogram', marker_color='#9eabb7'), row=4, col=1)
        fig.add_trace(go.Scatter(x=df.time, y=df.macd, name='MACD', line=dict(color='#52d1b1')), row=4, col=1)
        fig.add_trace(go.Scatter(x=df.time, y=df.macd_signal, name='MACD signal', line=dict(color='#ffae62')), row=4, col=1)
        init_history(con)
        markers = pd.read_sql_query('SELECT observed,series,signal,spot,ticker FROM signal_history WHERE observed >= ? ORDER BY observed',
                                    con, params=(int(df.time.iloc[0].timestamp()),))
        if not markers.empty:
            markers['time'] = pd.to_datetime(markers.observed, unit='s', utc=True)
            for signal, color, symbol in [('UP', '#20c997', 'triangle-up'), ('DOWN', '#ff6b6b', 'triangle-down'), ('SKIP', '#b8b8b8', 'circle-open')]:
                sub = markers[markers.signal == signal]
                if not sub.empty:
                    fig.add_trace(go.Scatter(x=sub.time, y=sub.spot, mode='markers', name=signal,
                        marker=dict(color=color, symbol=symbol, size=9),
                        text=sub.series + ' | ' + sub.ticker, hovertemplate='%{text}<br>%{x}<br>$%{y:,.2f}<extra></extra>'), row=1, col=1)
        fig.update_layout(template='plotly_dark', height=850, margin=dict(l=5,r=5,t=25,b=5),
                          xaxis_rangeslider_visible=False, legend=dict(orientation='h', y=1.03))
        st.plotly_chart(fig, use_container_width=True, key='btc_candles_chart')
        st.caption('Signal markers are plotted at observed Coinbase spot prices, not at Kalshi settlement prices. A signal may repeat at each refresh.')
    st.subheader('Model probability and signal history')
    init_history(con)
    series = st.selectbox('Kalshi series', ['KXBTC5M', 'KXBTC15M'], key='history_series')
    history = pd.read_sql_query('''SELECT observed,ticker,signal,spot,strike,up_prob,up_ask,down_ask,up_edge,down_edge,reason
                                   FROM signal_history WHERE series=? ORDER BY observed DESC LIMIT 500''', con, params=(series,))
    if history.empty:
        st.info('No signal snapshots recorded yet. Leave this dashboard open for a few refreshes.')
        return
    history = history.sort_values('observed')
    history['time'] = pd.to_datetime(history.observed, unit='s', utc=True)
    pfig = go.Figure()
    for col, label, color in [('up_prob','Model UP probability','#55c6ff'), ('up_ask','UP ask','#ffbf69'), ('down_ask','DOWN ask','#cc91ff')]:
        pfig.add_trace(go.Scatter(x=history.time, y=history[col], mode='lines+markers',
                                  name=label, line=dict(color=color), connectgaps=False))
    pfig.update_layout(template='plotly_dark', height=350, yaxis=dict(tickformat='.0%', range=[0,1]),
                       margin=dict(l=5,r=5,t=25,b=5))
    st.plotly_chart(pfig, use_container_width=True, key='probability_history_chart')
    st.caption('Model probabilities are illustrative, not calibrated or validated. Kalshi asks are market prices, not forecasts.')
    display = history[['time','ticker','signal','spot','strike','up_prob','up_ask','down_ask','up_edge','down_edge','reason']].sort_values('time', ascending=False)
    st.dataframe(display, use_container_width=True, hide_index=True)
    st.download_button('Export signal snapshots CSV', display.to_csv(index=False), 'signal_history.csv', 'text/csv', key='export_signal_history')
    st.warning('On Streamlit Community Cloud, local SQLite files may be lost when the app restarts or redeploys. For durable 24/7 history, use an external database and always-on worker.')
