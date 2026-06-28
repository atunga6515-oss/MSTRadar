"""
bot.py — Worker (scheduler). 7/24 calisir, BTC verisini ceker, indikatorleri
hesaplar, sinyal/pozisyon yonetir ve Telegram'a haber verir.
Ayarlari core.Settings uzerinden DB'den canli okur; dashboard komutlarini isler.
"""
import time
import logging
import logging.handlers
import traceback
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import requests
import schedule
import yfinance as yf
import pandas as pd

from core import (
    Database, Settings, IndicatorCalc, SignalScorer,
    BINANCE_API_URL, BINANCE_FAPI, MIN_CANDLES, NY_TZ, TR_TZ,
    f_or_none, us_market_status, is_tradeable, alpaca_latest_prices,
    normalize_ohlc, mtf_score, regime_from, holding_advice,
    weekend_state, weekend_override,
)

# ---------------------------------------------------------------------------
# Logging (rotation ile - dosya sinirsiz buyumesin)
# ---------------------------------------------------------------------------
handler = logging.handlers.RotatingFileHandler("bot.log", maxBytes=5_000_000, backupCount=3)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[handler, logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

import os
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID_HERE")


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------
class DataFetcher:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.session = requests.Session()

    def _get_json(self, url: str, attempts: int = 3):
        for attempt in range(attempts):
            try:
                resp = self.session.get(url, timeout=10)
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                logger.error(f"Attempt {attempt + 1}: GET failed {url.split('?')[0]}: {e}")
                time.sleep(2 ** attempt)
        return None

    def fetch_historical_klines(self, symbol="BTCUSDT", interval="5m", start_time=None, limit=1000):
        url = f"{BINANCE_API_URL}?symbol={symbol}&interval={interval}&limit={limit}"
        if start_time:
            url += f"&startTime={start_time}"
        data = self._get_json(url)
        if not data:
            return []
        return [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in data]

    def seed_data(self):
        latest_ts = self.db.get_latest_candle_timestamp()
        if not latest_ts:
            logger.info("Database empty, seeding last 45 days of data...")
            start_time = int((datetime.now(timezone.utc) - timedelta(days=45)).timestamp() * 1000)
        else:
            start_time = latest_ts + 1
        now_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
        all_candles = []
        while start_time < now_ts:
            logger.info(f"Fetching candles from {datetime.fromtimestamp(start_time/1000, tz=timezone.utc)}")
            candles = self.fetch_historical_klines(start_time=start_time)
            if not candles:
                break
            all_candles.extend(candles)
            new_start = candles[-1][0] + 1
            if new_start <= start_time:
                break
            start_time = new_start
            time.sleep(0.5)
        if all_candles:
            self.db.insert_candles(all_candles)
            logger.info(f"Inserted {len(all_candles)} candles into db.")

    def update_latest_candles(self):
        latest_ts = self.db.get_latest_candle_timestamp()
        if not latest_ts:
            self.seed_data()
            return
        total = 0
        while True:
            candles = self.fetch_historical_klines(start_time=latest_ts + 1)
            if not candles:
                break
            self.db.insert_candles(candles)
            total += len(candles)
            latest_ts = candles[-1][0]
            if len(candles) < 1000:
                break
            time.sleep(0.3)
        if total:
            logger.info(f"Updated {total} new candles.")

    def fetch_etf_prices(self) -> Dict[str, float]:
        assets = self.settings.list("ASSETS_BULL") + self.settings.list("ASSETS_BEAR")
        # 1) Once Alpaca (varsa ~gercek zamanli IEX); 2) eksikleri yfinance (~15dk gecikmeli)
        prices: Dict[str, float] = alpaca_latest_prices(assets)
        if prices:
            logger.info(f"ETF fiyatlari Alpaca'dan alindi: {list(prices.keys())}")
        missing = [a for a in assets if a not in prices]
        for asset in missing:
            for attempt in range(3):
                try:
                    data = yf.Ticker(asset).history(period="1d")
                    if not data.empty:
                        prices[asset] = float(data['Close'].iloc[-1])
                    break
                except Exception as e:
                    logger.error(f"Attempt {attempt + 1}: Error fetching {asset}: {e}")
                    time.sleep(2 ** attempt)
        return prices

    def fetch_underlying_score(self) -> Optional[Dict]:
        """MSTR (UNDERLYING) icin kisa vadeli yon skoru — tam sinyal teyidi icin.
        Once 15dk intraday (seans), yoksa gunluk. Veri yoksa None (teyit atlanir)."""
        sym = self.settings.s("UNDERLYING") or "MSTR"
        try:
            df = normalize_ohlc(yf.Ticker(sym).history(period="5d", interval="15m"))
            if df.empty or len(df) < 55:
                df = normalize_ohlc(yf.Ticker(sym).history(period="6mo", interval="1d"))
            if df.empty or len(df) < 55:
                return None
            score, label, _ = mtf_score(df)
            return {"score": score, "label": label}
        except Exception as e:
            logger.error(f"MSTR teyit verisi alinamadi: {e}")
            return None

    def fetch_futures_sentiment(self) -> Dict[str, Optional[float]]:
        out: Dict[str, Optional[float]] = {"funding_rate": None, "open_interest": None}
        if not self.settings.b("USE_FUTURES_SENTIMENT"):
            return out
        prem = self._get_json(f"{BINANCE_FAPI}/fapi/v1/premiumIndex?symbol=BTCUSDT", attempts=2)
        if prem and "lastFundingRate" in prem:
            try:
                out["funding_rate"] = float(prem["lastFundingRate"])
            except (TypeError, ValueError):
                pass
        oi = self._get_json(f"{BINANCE_FAPI}/fapi/v1/openInterest?symbol=BTCUSDT", attempts=2)
        if oi and "openInterest" in oi:
            try:
                out["open_interest"] = float(oi["openInterest"])
            except (TypeError, ValueError):
                pass
        return out


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
class TelegramNotifier:
    def __init__(self):
        self.bot_token = TELEGRAM_BOT_TOKEN
        self.chat_id = TELEGRAM_CHAT_ID
        self.session = requests.Session()

    def _configured(self) -> bool:
        return self.bot_token not in ("", "YOUR_BOT_TOKEN_HERE") and \
               self.chat_id not in ("", "YOUR_CHAT_ID_HERE")

    def send_message(self, message: str):
        if not self._configured():
            logger.warning("Telegram ayarli degil (.env). Mesaj gonderilmedi.")
            logger.info(f"Would have sent:\n{message}")
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": message, "parse_mode": "Markdown"}
        for attempt in range(3):
            try:
                resp = self.session.post(url, json=payload, timeout=10)
                resp.raise_for_status()
                logger.info("Telegram message sent.")
                return
            except Exception as e:
                logger.error(f"Attempt {attempt + 1}: Telegram send failed: {e}")
                time.sleep(2 ** attempt)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class TradingEngine:
    def __init__(self, db: Database, settings: Settings, fetcher: DataFetcher, notifier: TelegramNotifier):
        self.db = db
        self.settings = settings
        self.fetcher = fetcher
        self.notifier = notifier
        self._last_advice_sig = None  # anti-spam: son tavsiye imzasi

    # ---- sentiment trend yardimcilari ----
    def _sentiment_trends(self, current: Dict[str, Optional[float]]):
        hist = self.db.get_recent_sentiment(6)
        funding_trend = oi_trend = None
        if not hist.empty:
            fr = hist['funding_rate'].dropna()
            if len(fr) >= 2 and current.get("funding_rate") is not None:
                funding_trend = current["funding_rate"] - float(fr.iloc[0])
            oi = hist['open_interest'].dropna()
            if len(oi) >= 2 and current.get("open_interest") and float(oi.iloc[0]):
                oi_trend = (current["open_interest"] - float(oi.iloc[0])) / float(oi.iloc[0])
        return funding_trend, oi_trend

    def compute_indicators_and_signal(self, force_alert: bool = False):
        df = self.db.get_all_candles()
        if df.empty or len(df) < MIN_CANDLES:
            logger.warning(f"Yeterli veri yok (gerekli {MIN_CANDLES}, mevcut {len(df)})...")
            return

        df_4h = IndicatorCalc.resample_4h(df)
        if df_4h.empty or pd.isna(df_4h.iloc[-1]['sma_200']):
            logger.info("4S SMA200 icin yeterli veri yok.")
            return
        last_4h = df_4h.iloc[-1]

        df = IndicatorCalc.enrich(df)
        last_candle = df.iloc[-1]
        prev_candle = df.iloc[-2]
        current_price = float(last_candle['close'])
        ts = int(last_candle['timestamp'])

        macro_trend = "BULLISH" if current_price > last_4h['sma_50'] else \
                      ("BEARISH" if current_price < last_4h['sma_50'] else "NEUTRAL")

        # Sentiment + trendler
        sentiment = self.fetcher.fetch_futures_sentiment()
        self.db.insert_sentiment(ts, sentiment.get("funding_rate"), sentiment.get("open_interest"))
        funding_trend, oi_trend = self._sentiment_trends(sentiment)

        score, breakdown = SignalScorer.score(
            last_candle, prev_candle, last_4h, current_price,
            sentiment.get("funding_rate"), funding_trend, oi_trend)
        adx_val = float(last_candle['adx']) if pd.notna(last_candle['adx']) else 0.0
        atr_val = float(last_candle['atr']) if pd.notna(last_candle['atr']) else 0.0
        confidence = min(100.0, abs(score) * (0.5 + min(adx_val, 50) / 100))

        # Rejim (CHOP/TREND/ZAYIF) — chop filtresi icin
        ci_series = IndicatorCalc.choppiness(df, 14)
        ci_val = float(ci_series.iloc[-1]) if pd.notna(ci_series.iloc[-1]) else None
        regime = regime_from(adx_val, ci_val, self.settings.f("ADX_MIN"),
                             self.settings.f("CHOP_CI_MAX") or 61.8)

        snapshot = {
            "price": current_price, "score": score, "confidence": round(confidence, 1),
            "regime": regime, "choppiness": round(ci_val, 1) if ci_val is not None else None,
            "rsi": f_or_none(last_candle['rsi']), "macd": f_or_none(last_candle['macd']),
            "macd_signal": f_or_none(last_candle['macd_signal']), "vwap": f_or_none(last_candle['vwap']),
            "atr": atr_val, "adx": adx_val, "stochrsi_k": f_or_none(last_candle['stochrsi_k']),
            "bb_pctb": f_or_none(last_candle['bb_pctb']), "obv": f_or_none(last_candle['obv']),
            "funding_rate": sentiment.get("funding_rate"), "open_interest": sentiment.get("open_interest"),
            "funding_trend": funding_trend, "oi_trend": oi_trend,
            "sma_50_4h": f_or_none(last_4h['sma_50']), "sma_200_4h": f_or_none(last_4h['sma_200']),
            "market": us_market_status(), "breakdown": breakdown,
        }

        gap_msg = self._gap_play(df, current_price)

        # Karar: giris/cikis icin once action belirle
        thr = self.settings.f("SIGNAL_SCORE_THRESHOLD")
        watch_thr = self.settings.f("WATCH_SCORE_THRESHOLD")
        adx_min = self.settings.f("ADX_MIN")

        action, is_watch = None, False
        if adx_val >= adx_min and score >= thr:
            action = "BULLISH_SETUP"
        elif adx_val >= adx_min and score <= -thr:
            action = "BEARISH_SETUP"
        elif score >= watch_thr:
            action, is_watch = "BULLISH_SETUP", True
        elif score <= -watch_thr:
            action, is_watch = "BEARISH_SETUP", True

        # Kullanici portfoyu + ETF fiyatlari + MSTR yonu (teyit) — gerekiyorsa cek
        holdings = self.db.get_open_holdings()
        need_data = bool(holdings) or action is not None
        etf_prices = self.fetcher.fetch_etf_prices() if need_data else {}
        mstr = self.fetcher.fetch_underlying_score() if (need_data and self.settings.b("USE_MSTR_CONFIRM")) else None
        snapshot["mstr_confirm"] = mstr

        # --- SINYAL v2 kapilari: yeni AL onerisini chop/MSTR/piyasa-saati ile suz ---
        gate_reason = None
        if action and not is_watch:
            if self.settings.b("CHOP_FILTER") and regime == "CHOP":
                is_watch = True
                gate_reason = f"CHOP/yatay piyasa (CI={ci_val:.0f})"
            if mstr is not None:
                if (action == "BULLISH_SETUP") != (mstr["score"] > 0):
                    is_watch = True
                    gate_reason = ((gate_reason + " + ") if gate_reason else "") + \
                                  f"MSTR teyit yok ({mstr['label']} {mstr['score']:+.0f})"
            if self.settings.b("MARKET_HOURS_ONLY") and not is_tradeable(us_market_status()):
                is_watch = True
                gate_reason = ((gate_reason + " + ") if gate_reason else "") + "piyasa kapali"
        snapshot["gate_reason"] = gate_reason

        # Kullanicinin ELDEKI pozisyonuna gore kademeli tavsiye + kisisel bildirim
        rsi_val = float(last_candle['rsi']) if pd.notna(last_candle['rsi']) else None
        self._advise(ts, current_price, action, is_watch, score, regime, ci_val, gate_reason,
                     mstr, etf_prices, holdings, rsi_val, macro_trend, force_alert, snapshot, gap_msg)

    # ---- kullanici portfoyune gore kademeli tavsiye ----
    def _advise(self, ts, btc_price, action, is_watch, score, regime, ci_val, gate_reason,
                mstr, etf_prices, holdings, rsi, macro_trend, force_alert, snapshot=None, gap_msg=""):
        settings = self.settings
        bull = settings.list("ASSETS_BULL")
        bear = settings.list("ASSETS_BEAR")

        # Hafta sonu koruması durumu
        wstate = None
        if settings.b("WEEKEND_FLAT"):
            wstate = weekend_state(datetime.now(timezone.utc),
                                   settings.f("WEEKEND_FLAT_MIN") or 20,
                                   settings.f("WEEKEND_NOENTRY_MIN") or 90)
        wk_override = weekend_override(wstate)

        lines, sig_parts, records = [], [], []
        for h in holdings:
            asset = h["asset"]
            kind = h["kind"] or ("BULL" if asset in bull else "BEAR")
            live = etf_prices.get(asset)
            mstr_ok = None
            if mstr is not None:
                mstr_ok = ((mstr["score"] > 0) == (kind == "BULL"))
            if wk_override:  # hafta sonu koruması her seyin onunde
                advice, reason, frac = wk_override
            else:
                advice, reason, frac = holding_advice(kind, score, regime, mstr_ok, rsi,
                                                      live, h["entry_price"], settings)
            records.append((asset, advice))
            pl = f"  (P/L {(live - h['entry_price']) / h['entry_price'] * 100:+.1f}%)" \
                 if (live and h["entry_price"]) else ""
            emoji = {"EKLE": "🟢 EKLE", "TUT": "🟡 TUT",
                     "PARCALI_SAT": "🟠 PARÇALI SAT", "SAT": "🔴 SAT"}[advice]
            extra = ""
            if advice == "PARCALI_SAT" and frac:
                extra = f" (~%{frac * 100:.0f} ≈ {h['qty'] * frac:,.0f} adet)"
            elif advice == "SAT":
                extra = f" (tümü ≈ {h['qty']:,.0f} adet)"
            lines.append(f"• {h['qty']:,.0f} {asset}{pl} → {emoji}{extra}\n    {reason}")
            sig_parts.append(f"{asset}:{advice}")

        # Elde bir sey yoksa: net & teyitli sinyal varsa AL oner, yoksa BEKLE
        # (hafta sonu durumunda yeni giris onerilmez)
        flat_reco = None
        if not holdings:
            if action and not is_watch and wstate is None:
                buy = bull if action == "BULLISH_SETUP" else bear
                flat_reco = ("AL", buy)
                sig_parts.append("FLAT:AL:" + ",".join(buy))
                records = [(x, "AL") for x in buy]
            else:
                sig_parts.append("FLAT:BEKLE")
                if wstate and not gate_reason:
                    gate_reason = {"FLATTEN": "hafta sonu yaklaşıyor, yeni giriş yok",
                                   "NO_ENTRY": "Cuma kapanışı yakın, yeni giriş yok",
                                   "WEEKEND": "hafta sonu, piyasa kapalı"}.get(wstate)

        # Anti-spam: tavsiye durumu degismediyse ve eylem yoksa gonderme
        sig = "|".join(sorted(sig_parts))
        actionable = any(x in sig for x in ("EKLE", "PARCALI_SAT", "SAT", "AL"))
        if sig == getattr(self, "_last_advice_sig", None) and not force_alert:
            logger.info(f"Tavsiye degismedi, bildirim atlandi ({sig}).")
            return
        self._last_advice_sig = sig
        if not actionable and not force_alert:
            logger.info(f"Sadece TUT/BEKLE — bildirim atlandi ({sig}).")
            return

        # Tavsiye gecmisini kaydet (Sinyaller sekmesi + grafik oklari icin)
        if snapshot is not None:
            for asset, adv in records:
                if adv in ("SAT", "PARCALI_SAT", "EKLE", "AL"):
                    self.db.insert_signal(ts, asset, adv, btc_price, snapshot)

        self._send_advice(lines, flat_reco, holdings, btc_price, score, regime,
                          gate_reason, mstr, macro_trend, gap_msg)

    def _send_advice(self, lines, flat_reco, holdings, btc_price, score, regime,
                     gate_reason, mstr, macro_trend, gap_msg=""):
        regime_tr = {"TREND": "TREND (yonlu)", "CHOP": "CHOP (yatay-riskli)",
                     "ZAYIF": "ZAYIF (belirsiz)"}.get(regime, regime)
        market_tr = {"OPEN": "ACIK", "PRE": "PRE-MARKET", "AFTER": "AFTER-HOURS",
                     "CLOSED": "KAPALI"}[us_market_status()]
        mstr_tr = f"{mstr['label']} ({mstr['score']:+.0f})" if mstr is not None else "veri yok"
        macro_tr = "YUKSELIS" if macro_trend == "BULLISH" else ("DUSUS" if macro_trend == "BEARISH" else "NOTR")

        if holdings:
            body = "*📋 PORTFÖY TALİMATI*\n\n*Elindekiler:*\n" + "\n".join(lines)
        elif flat_reco:
            _, buy = flat_reco
            body = ("*📋 PORTFÖY TALİMATI*\n\n"
                    f"Elin boş. 🟢 *AL:* {' & '.join(buy)}\n"
                    f"({macro_tr} yönünde teyitli, chop dışı sinyal)")
        else:
            body = ("*📋 PORTFÖY TALİMATI*\n\n"
                    f"Elin boş. 🟡 *BEKLE* — net/teyitli sinyal yok"
                    + (f"\n   ({gate_reason})" if gate_reason else "."))

        gap_line = f"\n{gap_msg}" if gap_msg else ""
        msg = (f"{body}{gap_line}\n\n"
               f"📈 Skor: {score:+.0f}/100  |  🧭 Rejim: {regime_tr}\n"
               f"🏛️ MSTR yön: {mstr_tr}  |  ABD: {market_tr}\n"
               f"📊 BTC: ${btc_price:,.0f}  |  Trend(4S): {macro_tr}\n"
               f"_İşlemi sen yaparsın; bot otomatik emir göndermez._")
        self.notifier.send_message(msg)

    def _gap_play(self, df: pd.DataFrame, current_price: float) -> str:
        now_tr = datetime.now(TR_TZ)
        if not (now_tr.hour == 16 and 15 <= now_tr.minute < 30):
            return ""
        now_ny = datetime.now(NY_TZ)
        yclose_ny = (now_ny - timedelta(days=1)).replace(hour=16, minute=0, second=0, microsecond=0)
        yclose_utc = pd.Timestamp(yclose_ny).tz_convert("UTC")
        idx = (df['timestamp_dt'] - yclose_utc).abs().idxmin()
        last_close = df.loc[idx]['close']
        net = (current_price - last_close) / last_close * 100
        msg = f"⚠️ *Acilis Gap Beklentisi:* Dunku ABD kapanisindan bu yana BTC: {net:+.2f}%"
        logger.info(msg)
        return msg

    # ---- dashboard komutlari ----
    def process_commands(self):
        for cmd in self.db.pop_pending_commands():
            try:
                name = cmd["cmd"]
                if name == "trigger":
                    logger.info("Command: manual trigger")
                    self.run_cycle(force_alert=True)
                elif name == "pause":
                    self.db.set_config("PAUSED", "true")
                elif name == "resume":
                    self.db.set_config("PAUSED", "false")
                self.db.mark_command_done(cmd["id"])
            except Exception as e:
                logger.error(f"Command {cmd['cmd']} failed: {e}")
                self.db.mark_command_done(cmd["id"], f"error: {e}")

    def run_cycle(self, force_alert: bool = False):
        try:
            self.settings.refresh()
            if self.settings.b("PAUSED"):
                logger.info("PAUSED — cycle skipped.")
                return
            logger.info("--- Starting Analysis Cycle ---")
            self.fetcher.update_latest_candles()
            self.compute_indicators_and_signal(force_alert=force_alert)
        except Exception as e:
            logger.error(f"Error in analysis cycle: {e}\n{traceback.format_exc()}")
            try:
                self.notifier.send_message(f"⚠️ Bot hata aldi: `{e}`")
            except Exception:
                pass


def _seconds_until_next_run(now: datetime) -> float:
    target = (now.replace(second=2, microsecond=0) + timedelta(minutes=(5 - now.minute % 5)))
    while target <= now:
        target += timedelta(minutes=5)
    return (target - now).total_seconds()


def run_bot():
    db = Database()
    settings = Settings(db)
    fetcher = DataFetcher(db, settings)
    notifier = TelegramNotifier()
    engine = TradingEngine(db, settings, fetcher, notifier)

    logger.info("Initializing bot and seeding data...")
    notifier.send_message("✅ *Trading Bot Aktif!*\nSistem 7/24 piyasayi izlemeye basladi...")
    fetcher.seed_data()
    engine.run_cycle(force_alert=True)

    schedule.every().day.at("00:00").do(db.purge_old_data)
    schedule.every().day.at("00:05").do(lambda: logger.info(f"DB backup: {db.backup_db()}"))

    logger.info("Scheduler started. 5dk sinirlarina hizalanmis calisiyor (XX:05:02, ...)")
    while True:
        schedule.run_pending()
        engine.process_commands()  # dashboard komutlarini sik kontrol et
        sleep_s = min(_seconds_until_next_run(datetime.now()), 15)
        time.sleep(max(sleep_s, 0.5))
        now = datetime.now()
        if now.minute % 5 == 0 and now.second >= 2:
            engine.run_cycle()
            time.sleep(5)


if __name__ == "__main__":
    run_bot()
