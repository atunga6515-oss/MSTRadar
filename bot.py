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
        self._watch_last_sent: Dict[str, int] = {}  # bellek-ici watch cooldown {direction: ts_ms}

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

        snapshot = {
            "price": current_price, "score": score, "confidence": round(confidence, 1),
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

        # ETF fiyatlarini gerekiyorsa bir kez cek (acik pozisyon ya da yeni sinyal varsa)
        need_etf = self.db.get_open_position() is not None or action is not None
        etf_prices = self.fetcher.fetch_etf_prices() if need_etf else {}

        # 1) ONCE acik pozisyonun cikis kosullari (paper kapanisi icin etf_prices gerekli)
        self._manage_open_position(ts, current_price, score, adx_val, gap_msg, etf_prices)

        # 2) Yeni sinyal
        if action:
            self._handle_signal(action, is_watch, ts, current_price, macro_trend, score,
                                confidence, atr_val, breakdown, snapshot, gap_msg, etf_prices)
        else:
            logger.info(f"Sinyal yok. Skor:{score:+.0f} ADX:{adx_val:.0f} Trend:{macro_trend} "
                        f"Fiyat:{current_price:.0f}")
            if force_alert:
                self._send_status_report(current_price, score, adx_val, macro_trend,
                                         last_candle, sentiment, thr)

    # ---- paper trading yardimcilari ----
    def _paper_open(self, pos_id, assets, etf_prices, ts):
        if not self.settings.b("PAPER_TRADING"):
            return
        equity = self.db.paper_equity(self.settings.f("PAPER_START_EQUITY"))
        alloc = equity * (self.settings.f("PAPER_ALLOC_PCT") / 100.0)
        self.db.open_paper_trades(pos_id, assets, etf_prices, ts, alloc, self.settings.f("PAPER_FEE_PCT"))
        logger.info(f"Paper trade opened pos={pos_id} alloc=${alloc:.0f}")

    def _paper_close(self, pos_id, etf_prices, ts):
        if not self.settings.b("PAPER_TRADING"):
            return
        self.db.close_paper_trades(pos_id, etf_prices or {}, ts, self.settings.f("PAPER_FEE_PCT"))

    # ---- pozisyon yasam dongusu ----
    def _manage_open_position(self, ts: int, price: float, score: float, adx_val: float,
                              gap_msg: str, etf_prices: Dict[str, float]):
        pos = self.db.get_open_position()
        if not pos:
            return
        reason = None
        # a) Stop seviyesi
        if pos["direction"] == "BULL" and pos["stop_btc"] and price <= pos["stop_btc"]:
            reason = f"STOP (BTC {price:.0f} <= {pos['stop_btc']:.0f})"
        elif pos["direction"] == "BEAR" and pos["stop_btc"] and price >= pos["stop_btc"]:
            reason = f"STOP (BTC {price:.0f} >= {pos['stop_btc']:.0f})"
        # b) Ters tam sinyal (momentum dondu)
        thr = self.settings.f("SIGNAL_SCORE_THRESHOLD")
        if reason is None:
            if pos["direction"] == "BULL" and score <= -thr:
                reason = "TERS SINYAL (skor dususe dondu)"
            elif pos["direction"] == "BEAR" and score >= thr:
                reason = "TERS SINYAL (skor yukselise dondu)"
        # c) Maksimum tutma suresi
        if reason is None:
            max_hold = self.settings.f("MAX_HOLD_HOURS")
            held_h = (ts - pos["entry_ts"]) / (1000 * 3600)
            if max_hold > 0 and held_h >= max_hold:
                reason = f"ZAMAN ASIMI ({held_h:.0f}s >= {max_hold:.0f}s)"

        if reason:
            self._paper_close(pos["id"], etf_prices, ts)  # once kagit pozisyonu gercek ETF fiyatindan kapat
            self.db.close_position(pos["id"], ts, price, reason)
            pnl = (price - pos["entry_btc"]) / pos["entry_btc"] * 100
            pnl = pnl if pos["direction"] == "BULL" else -pnl
            self.notifier.send_message(
                f"🔚 *POZISYON KAPAT: {pos['assets']}*\n"
                f"Sebep: {reason}\n"
                f"Giris BTC: ${pos['entry_btc']:,.0f}  →  Cikis: ${price:,.0f}\n"
                f"BTC bazli yaklasik getiri: {pnl:+.2f}% "
                f"(ETF kaldiracli ~{abs(pnl)*2:.0f}%+ olabilir)\n"
                f"_Not: Emri sen kapatacaksin, bot otomatik islem yapmaz._")
            logger.info(f"Position {pos['id']} closed: {reason}")

    def _handle_signal(self, action, is_watch, ts, price, macro_trend, score, confidence,
                       atr_val, breakdown, snapshot, gap_msg, etf_prices):
        settings = self.settings
        bull = settings.list("ASSETS_BULL")
        bear = settings.list("ASSETS_BEAR")
        if action == "BULLISH_SETUP":
            targets_buy, targets_sell, direction, desired_type = bull, bear, "BULL", "BUY"
        else:
            targets_buy, targets_sell, direction, desired_type = bear, bull, "BEAR", "SELL"

        market = us_market_status()
        # Piyasa saati filtresi: Midas'ta islem yapilamayan saatte tam sinyali watch'a dusur
        if settings.b("MARKET_HOURS_ONLY") and not is_tradeable(market) and not is_watch:
            is_watch = True

        if is_watch:
            # bellek-ici watch cooldown (her 5dk tekrar etmesin)
            last = self._watch_last_sent.get(direction, 0)
            cooldown_ms = settings.f("ALERT_COOLDOWN_HOURS") * 3600 * 1000
            if ts - last < cooldown_ms:
                logger.info(f"Watch suppressed ({direction}).")
                return
            self._watch_last_sent[direction] = ts
        else:
            # DB tabanli anti-spam (ayni yon + cooldown icinde tekrar etme)
            last_sig = self.db.get_last_signal(targets_buy[0])
            if last_sig:
                last_time, last_type = last_sig
                hrs = (ts - last_time) / (1000 * 3600)
                if last_type == desired_type and hrs < settings.f("ALERT_COOLDOWN_HOURS"):
                    logger.info("Full signal suppressed by anti-spam.")
                    return
            # Sinyali kaydet + pozisyon ac (zaten ayni yonde acik degilse)
            for a in targets_buy:
                self.db.insert_signal(ts, a, desired_type, price, snapshot)
            for a in targets_sell:
                self.db.insert_signal(ts, a, "SELL" if desired_type == "BUY" else "BUY", price, snapshot)
            self._open_position_if_needed(direction, targets_buy, ts, price, score, atr_val, etf_prices)

        self._send_alert(action, is_watch, price, macro_trend, score, confidence,
                         atr_val, breakdown, targets_buy, targets_sell, etf_prices, market, gap_msg)

    def _open_position_if_needed(self, direction, targets_buy, ts, price, score, atr_val, etf_prices):
        pos = self.db.get_open_position()
        if pos and pos["direction"] == direction:
            return  # zaten ayni yonde acik
        if pos:  # ters yon -> once eskiyi (ve kagit pozisyonu) kapat
            self._paper_close(pos["id"], etf_prices, ts)
            self.db.close_position(pos["id"], ts, price, "TERS YENI SINYAL")
        mult = self.settings.f("ATR_STOP_MULT") or 2.0
        if direction == "BULL":
            stop = price - mult * atr_val
            target = price + 2 * mult * atr_val
        else:
            stop = price + mult * atr_val
            target = price - 2 * mult * atr_val
        pos_id = self.db.open_position(direction, ",".join(targets_buy), ts, price, score, stop, target)
        self._paper_open(pos_id, targets_buy, etf_prices, ts)  # gercek ETF fiyatindan kagit pozisyon ac
        logger.info(f"Position opened: {direction} @ {price:.0f} stop {stop:.0f}")

    def _send_alert(self, action, is_watch, price, macro_trend, score, confidence,
                    atr_val, breakdown, targets_buy, targets_sell, etf_prices, market, gap_msg):
        etf_str = "\n".join(f"• {k}: ${v:.2f}" for k, v in etf_prices.items()) if etf_prices else "Veri cekilemedi"
        gap_section = f"\n{gap_msg}\n" if gap_msg else ""
        if is_watch:
            head = "👀 *ERKEN UYARI (Momentum kuruluyor)*"
        else:
            head = "🚨 *YUKSELIS (BULLISH) SINYALI* 🚨" if action == "BULLISH_SETUP" \
                   else "🚨 *DUSUS (BEARISH) SINYALI* 🚨"
        macro_tr = "YUKSELIS" if macro_trend == "BULLISH" else ("DUSUS" if macro_trend == "BEARISH" else "NOTR")
        market_tr = {
            "OPEN": "ACIK (normal seans)",
            "PRE": "PRE-MARKET (Midas'ta islem var, spread genis)",
            "AFTER": "AFTER-HOURS (Midas'ta islem var, spread genis)",
            "CLOSED": "KAPALI (islem yok)",
        }[market]
        mult = self.settings.f("ATR_STOP_MULT") or 2.0
        stop = price - mult * atr_val if action == "BULLISH_SETUP" else price + mult * atr_val
        desc = " | ".join(f"{k}:{v:+.1f}" for k, v in breakdown.items())
        msg = (f"{head}\n{gap_section}"
               f"🟢 *AL (LONG):* {' & '.join(targets_buy)}\n"
               f"🔴 *SAT / ELDEN CIKAR:* {' & '.join(targets_sell)}\n\n"
               f"📈 *Skor:* {score:+.0f}/100  |  *Guven:* %{confidence:.0f}\n"
               f"🛡️ *Onerilen Stop (BTC):* ${stop:,.0f}  (ATR={atr_val:.0f})\n"
               f"🏛️ *ABD Piyasasi:* {market_tr}\n"
               f"📊 BTC: ${price:,.2f}  |  Ana Trend (4S): {macro_tr}\n"
               f"• Tetikleyiciler: {desc}\n\n"
               f"💰 *ETF Fiyatlari:*\n{etf_str}\n\n"
               f"_Islemi sen yapacaksin; bot otomatik emir gondermez._")
        self.notifier.send_message(msg)

    def _send_status_report(self, price, score, adx_val, macro_trend, last_candle, sentiment, thr):
        fr = sentiment.get("funding_rate")
        self.notifier.send_message(
            f"📊 *DURUM RAPORU* 📊\n\n"
            f"BTC: ${price:,.2f}\n"
            f"Skor: {score:+.0f}/100 (esik ±{thr:.0f})\n"
            f"ADX: {adx_val:.0f}  |  Makro (4S): {macro_trend}\n"
            f"RSI (5dk): {last_candle['rsi']:.1f}\n"
            f"ABD Piyasasi: {us_market_status()}\n"
            f"Funding: {('%.4f%%' % (fr*100)) if fr is not None else 'N/A'}\n\n"
            f"Sistem arka planda calisiyor.")

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
                elif name == "close":
                    pos = self.db.get_open_position()
                    if pos:
                        df = self.db.get_all_candles(limit=1)
                        price = float(df.iloc[-1]['close']) if not df.empty else pos["entry_btc"]
                        now_ms = int(time.time() * 1000)
                        self._paper_close(pos["id"], self.fetcher.fetch_etf_prices(), now_ms)
                        self.db.close_position(pos["id"], now_ms, price, "MANUEL (dashboard)")
                        self.notifier.send_message(f"🔚 Pozisyon manuel kapatildi: {pos['assets']}")
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
