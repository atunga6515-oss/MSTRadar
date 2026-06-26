"""
dashboard.py — Yerel izleme + yonetim paneli (Streamlit).
Worker ile ayni SQLite uzerinden konusur: okur (grafik/sinyal/performans) ve
config/commands tablolarina yazar (yonetim). Bot OTOMATIK ISLEM YAPMAZ.

Calistirma (SADECE YEREL):
    streamlit run dashboard.py
    # veya sadece localhost'a baglamak icin:
    streamlit run dashboard.py --server.address 127.0.0.1
"""
import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from core import (Database, Settings, IndicatorCalc, us_market_status, csv_list,
                  alpaca_latest_prices, fetch_klines, normalize_ohlc, mtf_score,
                  forecast_cone, empirical_up_probability, regime_from)

try:
    import yfinance as yf
    HAS_YF = True
except Exception:
    HAS_YF = False

st.set_page_config(page_title="BTC/MSTR ETF Bot", layout="wide", page_icon="📈")
db = Database()
settings = Settings(db)


@st.cache_data(ttl=60)
def load_candles(limit=1500):
    df = db.get_all_candles(limit=limit)
    if not df.empty:
        df = IndicatorCalc.enrich(df)
    return df


@st.cache_data(ttl=300)
def load_yf(symbols, period="5d", interval="15m"):
    out = {}
    if not HAS_YF:
        return out
    for s in symbols:
        try:
            d = yf.Ticker(s).history(period=period, interval=interval)
            if not d.empty:
                out[s] = d
        except Exception:
            pass
    return out


@st.cache_data(ttl=30)
def live_prices(symbols):
    """En guncel fiyat: once Alpaca (~gercek zamanli), eksikleri yfinance son kapanis."""
    px = dict(alpaca_latest_prices(list(symbols)))
    missing = [s for s in symbols if s not in px]
    if missing:
        d = load_yf(missing, period="1d", interval="5m")
        for s in missing:
            if s in d and not d[s].empty:
                px[s] = float(d[s]['Close'].iloc[-1])
    return px


# --- Coklu zaman dilimi (MTF) veri toplayicilari ---
MTF_LABELS = ["Saatlik", "12 Saat", "Gunluk", "Haftalik"]
MTF_WEIGHTS = [1.5, 1.3, 1.0, 0.7]  # 6 saatlik ufuk: yakin dilimler daha agirlikli


@st.cache_data(ttl=300)
def mtf_btc():
    """BTC icin 4 zaman dilimi (Binance, anahtar gerekmez)."""
    intervals = {"Saatlik": "1h", "12 Saat": "12h", "Gunluk": "1d", "Haftalik": "1w"}
    out = {}
    for label, iv in intervals.items():
        df = fetch_klines("BTCUSDT", iv, 300)
        if not df.empty:
            out[label] = mtf_score(df)
    return out


@st.cache_data(ttl=600)
def mtf_equity(symbol):
    """MSTR/ETF icin 4 zaman dilimi (yfinance). Intraday seans disi bosluklu olabilir."""
    out = {}
    if not HAS_YF:
        return out
    try:
        h1 = normalize_ohlc(yf.Ticker(symbol).history(period="60d", interval="1h"))
        if not h1.empty:
            out["Saatlik"] = mtf_score(h1)
            h12 = h1.set_index("timestamp_dt").resample("12h").agg(
                {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
            ).dropna().reset_index()
            if len(h12) >= 55:
                out["12 Saat"] = mtf_score(h12)
        d1 = normalize_ohlc(yf.Ticker(symbol).history(period="2y", interval="1d"))
        if not d1.empty:
            out["Gunluk"] = mtf_score(d1)
        w1 = normalize_ohlc(yf.Ticker(symbol).history(period="5y", interval="1wk"))
        if not w1.empty:
            out["Haftalik"] = mtf_score(w1)
    except Exception:
        pass
    return out


def confluence(scores: dict):
    """Zaman dilimi skorlarini agirlikli birlestir -> (genel skor, etiket)."""
    num = den = 0.0
    for lbl, w in zip(MTF_LABELS, MTF_WEIGHTS):
        if lbl in scores and scores[lbl][1] != "VERI YOK":
            num += scores[lbl][0] * w
            den += w
    if den == 0:
        return None, "VERI YOK"
    s = num / den
    lab = ("GUCLU YUKARI" if s >= 35 else "YUKARI" if s >= 12 else
           "GUCLU ASAGI" if s <= -35 else "ASAGI" if s <= -12 else "NOTR / KARARSIZ")
    return round(s, 1), lab


def dir_emoji(label: str) -> str:
    if "YUKARI" in label:
        return "🟢 ↑"
    if "ASAGI" in label:
        return "🔴 ↓"
    if label == "VERI YOK":
        return "⚪ –"
    return "🟡 →"


def fmt_ts(ms):
    if not ms or pd.isna(ms):
        return "-"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


# ===========================================================================
# Header
# ===========================================================================
st.title("📈 BTC / MSTR ETF Sinyal Botu")
top = st.columns([1, 1, 1, 1, 2])
df = load_candles()
last_price = float(df.iloc[-1]['close']) if not df.empty else None
market = us_market_status()
paused = settings.b("PAUSED")

top[0].metric("BTC", f"${last_price:,.0f}" if last_price else "-")
top[1].metric("ABD Piyasasi", {"OPEN": "🟢 ACIK", "PRE": "🟡 PRE-MKT",
                               "AFTER": "🟡 AFTER", "CLOSED": "🔴 KAPALI"}[market])
top[2].metric("Bot Durumu", "⏸️ DURDU" if paused else "▶️ AKTIF")
open_pos = db.get_open_position()
top[3].metric("Acik Pozisyon", open_pos["direction"] if open_pos else "Yok")
if top[4].button("🔄 Yenile"):
    st.cache_data.clear()
    st.rerun()

# Kompakt rejim + MSTR teyit satiri (Sinyal v2)
if not df.empty and len(df) > 30:
    ci_now = IndicatorCalc.choppiness(df, 14).iloc[-1]
    adx_now = df['adx'].iloc[-1] if 'adx' in df else None
    reg = regime_from(float(adx_now) if pd.notna(adx_now) else None,
                      float(ci_now) if pd.notna(ci_now) else None,
                      settings.f("ADX_MIN"), settings.f("CHOP_CI_MAX") or 61.8)
    reg_txt = {"TREND": "🟢 TREND (yönlü)", "CHOP": "🔴 CHOP (yatay — yeni pozisyon riskli)",
               "ZAYIF": "🟡 ZAYIF (belirsiz)"}.get(reg, reg)
    und = settings.s("UNDERLYING") or "MSTR"
    _, mlabel = confluence(mtf_equity(und))
    st.caption(f"🧭 **BTC Rejim:** {reg_txt}  ·  **{und} eğilim:** {dir_emoji(mlabel)} {mlabel}  "
               f"·  Tam sinyal için chop dışı + {und} teyidi gerekir.")

tabs = st.tabs(["📊 Grafikler", "🔮 Tahmin / MTF", "🔔 Sinyaller & Pozisyonlar",
                "📈 Performans", "⚙️ Yonetim"])

# ===========================================================================
# TAB 1 — Grafikler
# ===========================================================================
with tabs[0]:
    if df.empty:
        st.warning("Henuz veri yok. Once `python bot.py` ile veri biriktir.")
    else:
        view = df.tail(600)
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.7, 0.3],
                            vertical_spacing=0.03, subplot_titles=("BTC/USDT (5dk)", "RSI / ADX"))
        fig.add_trace(go.Candlestick(x=view['timestamp_dt'], open=view['open'], high=view['high'],
                                     low=view['low'], close=view['close'], name="BTC"), row=1, col=1)
        fig.add_trace(go.Scatter(x=view['timestamp_dt'], y=view['vwap'], name="VWAP",
                                 line=dict(color="orange", width=1)), row=1, col=1)
        fig.add_trace(go.Scatter(x=view['timestamp_dt'], y=view['bb_upper'], name="BB Ust",
                                 line=dict(color="gray", width=1, dash="dot")), row=1, col=1)
        fig.add_trace(go.Scatter(x=view['timestamp_dt'], y=view['bb_lower'], name="BB Alt",
                                 line=dict(color="gray", width=1, dash="dot"), fill='tonexty',
                                 fillcolor="rgba(128,128,128,0.08)"), row=1, col=1)
        fig.add_trace(go.Scatter(x=view['timestamp_dt'], y=view['rsi'], name="RSI",
                                 line=dict(color="purple", width=1)), row=2, col=1)
        fig.add_trace(go.Scatter(x=view['timestamp_dt'], y=view['adx'], name="ADX",
                                 line=dict(color="teal", width=1)), row=2, col=1)
        fig.add_hline(y=70, line=dict(color="red", width=0.5), row=2, col=1)
        fig.add_hline(y=30, line=dict(color="green", width=0.5), row=2, col=1)
        # Sinyal isaretleri
        sig = db.get_signals_df(200)
        if not sig.empty:
            sig = sig[sig['asset_target'].isin(settings.list("ASSETS_BULL"))]
            sig['dt'] = pd.to_datetime(sig['timestamp'], unit='ms', utc=True)
            buys = sig[sig['signal_type'] == 'BUY']
            sells = sig[sig['signal_type'] == 'SELL']
            fig.add_trace(go.Scatter(x=buys['dt'], y=buys['btc_price'], mode='markers', name='AL',
                                     marker=dict(symbol='triangle-up', color='green', size=11)), row=1, col=1)
            fig.add_trace(go.Scatter(x=sells['dt'], y=sells['btc_price'], mode='markers', name='SAT',
                                     marker=dict(symbol='triangle-down', color='red', size=11)), row=1, col=1)
        fig.update_layout(height=620, xaxis_rangeslider_visible=False, margin=dict(t=40, b=10))
        st.plotly_chart(fig, use_container_width=True)

    # MSTR + ETF grafikleri (yfinance)
    st.subheader("MSTR & ETF'ler (baz riski takibi)")
    if not HAS_YF:
        st.info("yfinance kurulu degil: `pip install yfinance`")
    else:
        underlying = settings.s("UNDERLYING") or "MSTR"
        syms = [underlying] + settings.list("ASSETS_BULL") + settings.list("ASSETS_BEAR")
        data = load_yf(syms)
        cols = st.columns(min(len(syms), 4) or 1)
        for i, s in enumerate(syms):
            with cols[i % len(cols)]:
                if s in data:
                    d = data[s]
                    chg = (d['Close'].iloc[-1] / d['Close'].iloc[0] - 1) * 100
                    st.metric(s, f"${d['Close'].iloc[-1]:.2f}", f"{chg:+.1f}% (5g)")
                    st.line_chart(d['Close'], height=140)
                else:
                    st.metric(s, "veri yok")
        # BTC-MSTR korelasyonu (baz riski göstergesi)
        if underlying in data and not df.empty:
            try:
                m = data[underlying]['Close'].pct_change().dropna()
                b = df.set_index('timestamp_dt')['close'].resample('15min').last().pct_change().dropna()
                j = pd.concat([m.tz_convert('UTC'), b], axis=1).dropna()
                if len(j) > 10:
                    corr = j.iloc[:, 0].corr(j.iloc[:, 1])
                    st.caption(f"BTC ↔ {underlying} 15dk getiri korelasyonu (5g): **{corr:.2f}** "
                               f"— 1'e yakin = ETF sinyali BTC ile uyumlu; dusukse baz riski yuksek.")
            except Exception:
                pass

# ===========================================================================
# TAB 1b — Tahmin / Coklu Zaman Dilimi (MTF)
# ===========================================================================
with tabs[1]:
    st.subheader("🔮 Çoklu Zaman Dilimi Eğilim Analizi")
    st.caption("Her varlık için Saatlik / 12 Saat / Günlük / Haftalık dilimlerde yön + güç. "
               "Bu bir kesin tahmin DEĞİL; mevcut kurulumun eğilimini özetler.")

    underlying = settings.s("UNDERLYING") or "MSTR"
    assets = ["BTC", underlying] + settings.list("ASSETS_BULL") + settings.list("ASSETS_BEAR")

    rows = []
    btc_conf = None
    for a in assets:
        scores = mtf_btc() if a == "BTC" else mtf_equity(a)
        cs, clabel = confluence(scores)
        if a == "BTC":
            btc_conf = cs
        row = {"Varlık": a}
        for lbl in MTF_LABELS:
            if lbl in scores:
                sc, slabel, _ = scores[lbl]
                row[lbl] = f"{dir_emoji(slabel)} {sc:+.0f}"
            else:
                row[lbl] = "⚪ –"
        row["GENEL"] = f"{dir_emoji(clabel)} {clabel}" + (f" ({cs:+.0f})" if cs is not None else "")
        rows.append(row)
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    st.caption("🟢↑ yukarı · 🔴↓ aşağı · 🟡→ nötr/kararsız · ⚪– veri yok. "
               "Sayı = o dilimin yön skoru (−100…+100). GENEL = ağırlıklı birleşim "
               "(6 saatlik ufuk için yakın dilimler daha ağırlıklı).")

    st.divider()
    st.subheader("📈 BTC — 6 Saatlik Olasılık Konisi")
    df5 = load_candles(2000)
    if df5.empty or len(df5) < 300:
        st.info("Koni için yeterli 5dk veri yok (worker biraz daha çalışsın).")
    else:
        drift = (btc_conf / 100.0) if btc_conf is not None else 0.0
        cone = forecast_cone(df5, horizon_hours=6, bar_minutes=5, drift_bias=drift)
        prob, nsample = empirical_up_probability(df5, horizon_bars=72)

        mc = st.columns(3)
        mc[0].metric("Genel eğilim (BTC)", f"{btc_conf:+.0f}" if btc_conf is not None else "–")
        mc[1].metric("6s yukarı olasılığı*", f"%{prob:.0f}" if prob is not None else "yetersiz veri",
                     help="Geçmişte benzer trend+momentum rejiminde fiyatın 6 saatte yukarı kapama oranı.")
        mc[2].metric("Beklenen oynaklık (±1σ, 6s)", f"%{cone.get('sigma_6h_pct', 0):.1f}" if cone else "–")

        if cone:
            hist = df5.tail(72)  # son 6 saat
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=hist['timestamp_dt'], y=hist['close'], name="BTC (gerçek)",
                                     line=dict(color="#2962FF", width=2)))
            t = cone['times']
            fig.add_trace(go.Scatter(x=t, y=cone['upper2'], line=dict(width=0), showlegend=False,
                                     hoverinfo='skip'))
            fig.add_trace(go.Scatter(x=t, y=cone['lower2'], fill='tonexty', name="±2σ aralık",
                                     fillcolor="rgba(41,98,255,0.08)", line=dict(width=0), hoverinfo='skip'))
            fig.add_trace(go.Scatter(x=t, y=cone['upper1'], line=dict(width=0), showlegend=False,
                                     hoverinfo='skip'))
            fig.add_trace(go.Scatter(x=t, y=cone['lower1'], fill='tonexty', name="±1σ aralık",
                                     fillcolor="rgba(41,98,255,0.18)", line=dict(width=0), hoverinfo='skip'))
            fig.add_trace(go.Scatter(x=t, y=cone['median'], name="Eğilim (medyan)",
                                     line=dict(color="orange", width=2, dash="dash")))
            fig.update_layout(height=420, margin=dict(t=30, b=10),
                              title="Son 6 saat + önümüzdeki 6 saat olası aralık")
            st.plotly_chart(fig, use_container_width=True)
        st.caption(f"*Olasılık örnek sayısı: {nsample}. Koni bir TAHMİN değil; mevcut oynaklığa göre "
                   "olası fiyat ARALIĞIDIR. Turuncu çizgi mevcut momentumun hafif eğilimidir, hedef değildir. "
                   "Kaldıraçlı ETF'ler BTC değil MSTR'ı takip eder — baz riskini unutma.")

# ===========================================================================
# TAB 2 — Sinyaller & Pozisyonlar
# ===========================================================================
with tabs[2]:
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Son Sinyaller")
        sig = db.get_signals_df(50)
        if sig.empty:
            st.info("Henuz sinyal yok.")
        else:
            sig['zaman'] = sig['timestamp'].apply(fmt_ts)
            sig['skor'] = sig['indicator_snapshots'].apply(
                lambda s: json.loads(s).get('score') if s else None)
            st.dataframe(sig[['zaman', 'asset_target', 'signal_type', 'btc_price', 'skor']],
                         use_container_width=True, height=400)
    with c2:
        st.subheader("Pozisyonlar")
        pos = db.get_positions_df(50)
        if pos.empty:
            st.info("Henuz pozisyon yok.")
        else:
            pos['giris'] = pos['entry_ts'].apply(fmt_ts)
            pos['cikis'] = pos['exit_ts'].apply(fmt_ts)
            show = pos[['giris', 'direction', 'assets', 'entry_btc', 'stop_btc',
                        'status', 'cikis', 'exit_btc', 'pnl_btc_pct', 'exit_reason']]
            st.dataframe(show, use_container_width=True, height=400)

# ===========================================================================
# TAB 3 — Performans (Sanal Portfoy)
# ===========================================================================
with tabs[3]:
    st.subheader("💼 Sanal Portfoy (gercek ETF fiyatlariyla kagit islem)")
    st.caption("Bot sinyalleri gercek ETF fiyatindan kagit uzerinde al/sat edilir; "
               "botun gercek basarisini zamanla olcer. Gercek para degildir.")

    start_eq = settings.f("PAPER_START_EQUITY") or 10000
    pt = db.get_paper_trades_df(1000)
    closed_pt = pt[pt['status'] == 'CLOSED'].copy() if not pt.empty else pd.DataFrame()
    open_pt = db.get_open_paper_trades()

    realized = closed_pt['pnl_abs'].sum() if not closed_pt.empty else 0.0
    equity = start_eq + realized

    # Acik kagit pozisyonlarin guncel mark-to-market (once Alpaca, sonra yfinance)
    mtm = 0.0
    if not open_pt.empty:
        live = live_prices(list(open_pt['asset'].unique()))
        for _, r in open_pt.iterrows():
            cur = live.get(r['asset'])
            if cur:
                mtm += r['qty'] * (cur - r['entry_price'])

    m = st.columns(4)
    m[0].metric("Sermaye (baslangic)", f"${start_eq:,.0f}")
    m[1].metric("Guncel Oz Sermaye", f"${equity + mtm:,.0f}",
                f"{(equity + mtm - start_eq)/start_eq*100:+.1f}%")
    m[2].metric("Gerceklesen K/Z", f"${realized:+,.0f}")
    m[3].metric("Acik pozisyon (MTM)", f"${mtm:+,.0f}" if not open_pt.empty else "Yok")

    if closed_pt.empty:
        st.info("Henuz kapanmis kagit islem yok. Bot sinyal urettikce burada birikir.")
    else:
        wins = (closed_pt['pnl_abs'] > 0).sum()
        n = len(closed_pt)
        closed_pt = closed_pt.sort_values('entry_ts')
        closed_pt['kumulatif'] = start_eq + closed_pt['pnl_abs'].cumsum()
        run_max = closed_pt['kumulatif'].cummax()
        max_dd = ((closed_pt['kumulatif'] - run_max) / run_max).min() * 100

        s = st.columns(4)
        s[0].metric("Kapanan islem", n)
        s[1].metric("Kazanc orani", f"%{wins/n*100:.0f}")
        s[2].metric("Ort. K/Z / islem", f"${closed_pt['pnl_abs'].mean():+,.1f}")
        s[3].metric("Max Drawdown", f"%{max_dd:.1f}")

        st.markdown("**Oz sermaye egrisi**")
        st.line_chart(closed_pt.set_index(closed_pt['exit_ts'].apply(fmt_ts))['kumulatif'], height=260)

        closed_pt['giris'] = closed_pt['entry_ts'].apply(fmt_ts)
        closed_pt['cikis'] = closed_pt['exit_ts'].apply(fmt_ts)
        st.dataframe(closed_pt[['giris', 'cikis', 'asset', 'qty', 'entry_price',
                               'exit_price', 'fee', 'pnl_abs']],
                     use_container_width=True, height=300)
    st.caption("Daha genis donem testi icin: `python backtest.py --grid`")

# ===========================================================================
# TAB 4 — Yonetim
# ===========================================================================
with tabs[4]:
    st.subheader("Hizli Kontroller")
    b = st.columns(4)
    if b[0].button("▶️ Devam et" if paused else "⏸️ Durdur"):
        db.push_command("resume" if paused else "pause")
        st.success("Komut gonderildi (worker birkac sn icinde uygular).")
    if b[1].button("⚡ Simdi analiz et"):
        db.push_command("trigger")
        st.success("Manuel analiz komutu gonderildi.")
    if b[2].button("🔚 Acik pozisyonu kapat", disabled=open_pos is None):
        db.push_command("close")
        st.success("Pozisyon kapatma komutu gonderildi.")

    st.divider()
    st.subheader("Ayarlar (config)")
    st.caption("Degisiklikler worker'in bir sonraki dongusunde otomatik uygulanir.")
    cfg = db.get_config()
    editable = ["SIGNAL_SCORE_THRESHOLD", "WATCH_SCORE_THRESHOLD", "ADX_MIN",
                "ALERT_COOLDOWN_HOURS", "ATR_STOP_MULT", "MAX_HOLD_HOURS",
                "MARKET_HOURS_ONLY", "USE_FUTURES_SENTIMENT",
                "USE_MSTR_CONFIRM", "CHOP_FILTER", "CHOP_CI_MAX",
                "ASSETS_BULL", "ASSETS_BEAR", "UNDERLYING",
                "PAPER_TRADING", "PAPER_START_EQUITY", "PAPER_ALLOC_PCT", "PAPER_FEE_PCT"]
    with st.form("config_form"):
        new_vals = {}
        cc = st.columns(2)
        for i, key in enumerate(editable):
            with cc[i % 2]:
                cur = cfg.get(key, "")
                if cur.lower() in ("true", "false"):
                    new_vals[key] = "true" if st.checkbox(key, value=(cur.lower() == "true")) else "false"
                else:
                    new_vals[key] = st.text_input(key, value=cur)
        if st.form_submit_button("💾 Kaydet"):
            for k, v in new_vals.items():
                if str(v) != str(cfg.get(k, "")):
                    db.set_config(k, v)
            st.success("Ayarlar kaydedildi.")
            st.cache_data.clear()
