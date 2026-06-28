"""
core.py — Ortak cekirdek (worker, dashboard ve backtest tarafindan paylasilir).
Database + indikatorler + skorlama + DB tabanli ayarlar burada toplanir.
"""
import os
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Tuple

import pandas as pd
import numpy as np


# ---------------------------------------------------------------------------
# .env yukleme (python-dotenv varsa onu kullan, yoksa basit manuel parser)
# ---------------------------------------------------------------------------
def load_env(path: str = ".env"):
    try:
        from dotenv import load_dotenv
        load_dotenv(path)
        return
    except Exception:
        pass
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                val = val.split("#")[0].strip().strip('"').strip("'")
                os.environ.setdefault(key.strip(), val)


load_env()

DB_NAME = os.getenv("DB_NAME", "trading_bot.db")
BINANCE_API_URL = "https://api.binance.com/api/v3/klines"
BINANCE_FAPI = "https://fapi.binance.com"

# .env'den gelen varsayilanlar (config tablosu bunlari override eder)
ENV_DEFAULTS = {
    "ASSETS_BULL": os.getenv("ASSETS_BULL", "MSTU,MSTX"),
    "ASSETS_BEAR": os.getenv("ASSETS_BEAR", "MSTZ"),
    "UNDERLYING": os.getenv("UNDERLYING", "MSTR"),   # ETF'lerin takip ettigi hisse
    "SIGNAL_SCORE_THRESHOLD": os.getenv("SIGNAL_SCORE_THRESHOLD", "40"),
    "WATCH_SCORE_THRESHOLD": os.getenv("WATCH_SCORE_THRESHOLD", "22"),
    "ADX_MIN": os.getenv("ADX_MIN", "20"),
    "ALERT_COOLDOWN_HOURS": os.getenv("ALERT_COOLDOWN_HOURS", "4"),
    "USE_FUTURES_SENTIMENT": os.getenv("USE_FUTURES_SENTIMENT", "true"),
    "ATR_STOP_MULT": os.getenv("ATR_STOP_MULT", "2.0"),       # stop = giris -/+ N*ATR
    "MAX_HOLD_HOURS": os.getenv("MAX_HOLD_HOURS", "48"),      # zaman bazli cikis
    "MARKET_HOURS_ONLY": os.getenv("MARKET_HOURS_ONLY", "false"),  # sadece ABD seansinda tam sinyal
    "PAUSED": os.getenv("PAUSED", "false"),                  # dashboard'dan duraklatma
    # --- Sinyal v2: MSTR teyit + chop filtresi ---
    "USE_MSTR_CONFIRM": os.getenv("USE_MSTR_CONFIRM", "true"),  # tam sinyalde MSTR ayni yonu teyit etsin
    "CHOP_FILTER": os.getenv("CHOP_FILTER", "true"),           # yatay piyasada yeni pozisyon acma
    "CHOP_CI_MAX": os.getenv("CHOP_CI_MAX", "61.8"),           # Choppiness Index > bu -> CHOP (trendsiz)
    # --- Kullanici portfoyu (elle girilen) + kademeli tavsiye ---
    "HOLDING_STOP_PCT": os.getenv("HOLDING_STOP_PCT", "15"),   # ETF girise gore bu kadar dustuyse SAT
    "PARTIAL_SELL_PCT": os.getenv("PARTIAL_SELL_PCT", "33"),   # PARCALI SAT'ta onerilen oran (%)
    # --- Hafta sonu koruması (kaldiracli ETF gap riski) ---
    "WEEKEND_FLAT": os.getenv("WEEKEND_FLAT", "true"),        # Cuma kapanisa yakin pozisyonlari kapat
    "WEEKEND_FLAT_MIN": os.getenv("WEEKEND_FLAT_MIN", "20"),  # Cuma kapanistan kac dk once SAT uyarisi
    "WEEKEND_NOENTRY_MIN": os.getenv("WEEKEND_NOENTRY_MIN", "90"),  # Cuma kapanistan kac dk once yeni AL yok
    # --- Sanal portfoy (paper trading) — ARTIK KULLANILMIYOR (kullanici portfoyune gecildi) ---
    "PAPER_TRADING": os.getenv("PAPER_TRADING", "false"),   # otomatik kagit islem (kapali)
    "PAPER_START_EQUITY": os.getenv("PAPER_START_EQUITY", "10000"),  # baslangic sermayesi ($)
    "PAPER_ALLOC_PCT": os.getenv("PAPER_ALLOC_PCT", "100"), # her sinyalde sermayenin %'si
    "PAPER_FEE_PCT": os.getenv("PAPER_FEE_PCT", "0.1"),     # islem basina komisyon+spread (%)
}

# 4S SMA200 icin gereken 5dk mum sayisi: 200 mum * 48 (5dk -> 4S) = 9600
MIN_CANDLES = 200 * 48

try:
    from zoneinfo import ZoneInfo
    NY_TZ = ZoneInfo("America/New_York")
    TR_TZ = ZoneInfo("Europe/Istanbul")
except Exception:  # pragma: no cover
    NY_TZ = TR_TZ = timezone.utc


def csv_list(value: str) -> List[str]:
    return [x.strip() for x in (value or "").split(",") if x.strip()]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
class Database:
    def __init__(self, db_name: str = DB_NAME):
        self.db_name = db_name
        self.init_db()

    def get_connection(self):
        conn = sqlite3.connect(self.db_name, timeout=30)
        conn.row_factory = sqlite3.Row
        # Es zamanli erisim + kilitlenmeye karsi
        conn.execute("PRAGMA busy_timeout=30000")    # kilitliyse 30sn bekle, hata firlatma
        conn.execute("PRAGMA synchronous=NORMAL")    # WAL ile guvenli + hizli
        return conn

    def init_db(self):
        with self.get_connection() as conn:
            # WAL modu: worker yazarken dashboard ayni anda okuyabilir (kilit cakismasi azalir,
            # ani kapanmada bozulma riski duser). Kalici ayardir, bir kez yeter.
            conn.execute("PRAGMA journal_mode=WAL")
            c = conn.cursor()
            c.execute('''CREATE TABLE IF NOT EXISTS btc_candles (
                timestamp INTEGER PRIMARY KEY,
                open REAL, high REAL, low REAL, close REAL, volume REAL)''')
            c.execute('''CREATE TABLE IF NOT EXISTS signals_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER, asset_target TEXT, signal_type TEXT,
                btc_price REAL, indicator_snapshots TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY, value TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                direction TEXT, assets TEXT,
                entry_ts INTEGER, entry_btc REAL, entry_score REAL,
                stop_btc REAL, target_btc REAL,
                status TEXT DEFAULT 'OPEN',
                exit_ts INTEGER, exit_btc REAL, exit_reason TEXT,
                pnl_btc_pct REAL, note TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cmd TEXT, payload TEXT, status TEXT DEFAULT 'PENDING',
                created_ts INTEGER, done_ts INTEGER, result TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS sentiment_history (
                timestamp INTEGER PRIMARY KEY,
                funding_rate REAL, open_interest REAL)''')
            c.execute('''CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id INTEGER, asset TEXT, qty REAL,
                entry_ts INTEGER, entry_price REAL,
                exit_ts INTEGER, exit_price REAL,
                fee REAL DEFAULT 0, pnl_abs REAL, status TEXT DEFAULT 'OPEN')''')
            c.execute('''CREATE TABLE IF NOT EXISTS holdings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset TEXT, kind TEXT, qty REAL,
                entry_price REAL, entry_ts INTEGER,
                status TEXT DEFAULT 'OPEN',
                exit_price REAL, exit_ts INTEGER, pnl_abs REAL, note TEXT)''')
            c.execute('CREATE INDEX IF NOT EXISTS idx_signals_asset_ts ON signals_history(asset_target, timestamp)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_paper_status ON paper_trades(status)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_holdings_status ON holdings(status)')
            conn.commit()
        self._seed_config()

    # --- config ---
    def _seed_config(self):
        with self.get_connection() as conn:
            existing = {r["key"] for r in conn.execute("SELECT key FROM config")}
            for k, v in ENV_DEFAULTS.items():
                if k not in existing:
                    conn.execute("INSERT INTO config(key, value) VALUES(?, ?)", (k, str(v)))
            conn.commit()

    def get_config(self) -> Dict[str, str]:
        with self.get_connection() as conn:
            return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM config")}

    def set_config(self, key: str, value: str):
        with self.get_connection() as conn:
            conn.execute("INSERT INTO config(key,value) VALUES(?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
            conn.commit()

    # --- candles ---
    def insert_candles(self, candles: List[Tuple]):
        with self.get_connection() as conn:
            conn.executemany('''INSERT OR IGNORE INTO btc_candles
                (timestamp, open, high, low, close, volume) VALUES (?,?,?,?,?,?)''', candles)
            conn.commit()

    def get_latest_candle_timestamp(self) -> Optional[int]:
        with self.get_connection() as conn:
            res = conn.execute('SELECT MAX(timestamp) AS m FROM btc_candles').fetchone()
            return res["m"] if res and res["m"] is not None else None

    def get_all_candles(self, limit: Optional[int] = None) -> pd.DataFrame:
        q = 'SELECT * FROM btc_candles ORDER BY timestamp ASC'
        if limit:
            q = f'SELECT * FROM (SELECT * FROM btc_candles ORDER BY timestamp DESC LIMIT {int(limit)}) ORDER BY timestamp ASC'
        with self.get_connection() as conn:
            df = pd.read_sql_query(q, conn)
        if not df.empty:
            df['timestamp_dt'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
        return df

    # --- signals ---
    def insert_signal(self, timestamp: int, asset_target: str, signal_type: str,
                      btc_price: float, snapshot: Dict):
        with self.get_connection() as conn:
            conn.execute('''INSERT INTO signals_history
                (timestamp, asset_target, signal_type, btc_price, indicator_snapshots)
                VALUES (?,?,?,?,?)''',
                         (timestamp, asset_target, signal_type, btc_price, json.dumps(snapshot)))
            conn.commit()

    def get_last_signal(self, asset_target: str) -> Optional[Tuple]:
        with self.get_connection() as conn:
            r = conn.execute('''SELECT timestamp, signal_type FROM signals_history
                WHERE asset_target=? ORDER BY timestamp DESC LIMIT 1''', (asset_target,)).fetchone()
            return (r["timestamp"], r["signal_type"]) if r else None

    def get_signals_df(self, limit: int = 500) -> pd.DataFrame:
        with self.get_connection() as conn:
            return pd.read_sql_query(
                f'SELECT * FROM signals_history ORDER BY timestamp DESC LIMIT {int(limit)}', conn)

    # --- positions ---
    def open_position(self, direction: str, assets: str, entry_ts: int, entry_btc: float,
                      entry_score: float, stop_btc: float, target_btc: Optional[float]) -> int:
        with self.get_connection() as conn:
            cur = conn.execute('''INSERT INTO positions
                (direction, assets, entry_ts, entry_btc, entry_score, stop_btc, target_btc, status)
                VALUES (?,?,?,?,?,?,?, 'OPEN')''',
                               (direction, assets, entry_ts, entry_btc, entry_score, stop_btc, target_btc))
            conn.commit()
            return cur.lastrowid

    def get_open_position(self) -> Optional[sqlite3.Row]:
        with self.get_connection() as conn:
            return conn.execute("SELECT * FROM positions WHERE status='OPEN' "
                                "ORDER BY entry_ts DESC LIMIT 1").fetchone()

    def close_position(self, pos_id: int, exit_ts: int, exit_btc: float, reason: str):
        with self.get_connection() as conn:
            row = conn.execute("SELECT direction, entry_btc FROM positions WHERE id=?", (pos_id,)).fetchone()
            pnl = None
            if row and row["entry_btc"]:
                move = (exit_btc - row["entry_btc"]) / row["entry_btc"] * 100
                pnl = move if row["direction"] == "BULL" else -move  # BTC bazli yaklasik
            conn.execute('''UPDATE positions SET status='CLOSED', exit_ts=?, exit_btc=?,
                exit_reason=?, pnl_btc_pct=? WHERE id=?''',
                         (exit_ts, exit_btc, reason, pnl, pos_id))
            conn.commit()

    def get_positions_df(self, limit: int = 200) -> pd.DataFrame:
        with self.get_connection() as conn:
            return pd.read_sql_query(
                f'SELECT * FROM positions ORDER BY entry_ts DESC LIMIT {int(limit)}', conn)

    # --- commands (dashboard -> worker) ---
    def push_command(self, cmd: str, payload: str = ""):
        with self.get_connection() as conn:
            conn.execute("INSERT INTO commands(cmd, payload, created_ts) VALUES(?,?,?)",
                         (cmd, payload, int(time.time() * 1000)))
            conn.commit()

    def pop_pending_commands(self) -> List[sqlite3.Row]:
        with self.get_connection() as conn:
            return conn.execute("SELECT * FROM commands WHERE status='PENDING' ORDER BY id ASC").fetchall()

    def mark_command_done(self, cmd_id: int, result: str = "ok"):
        with self.get_connection() as conn:
            conn.execute("UPDATE commands SET status='DONE', done_ts=?, result=? WHERE id=?",
                         (int(time.time() * 1000), result, cmd_id))
            conn.commit()

    # --- sentiment ---
    def insert_sentiment(self, timestamp: int, funding_rate: Optional[float], open_interest: Optional[float]):
        with self.get_connection() as conn:
            conn.execute("INSERT OR REPLACE INTO sentiment_history(timestamp, funding_rate, open_interest) "
                         "VALUES(?,?,?)", (timestamp, funding_rate, open_interest))
            conn.commit()

    def get_recent_sentiment(self, n: int = 10) -> pd.DataFrame:
        with self.get_connection() as conn:
            return pd.read_sql_query(
                f"SELECT * FROM (SELECT * FROM sentiment_history ORDER BY timestamp DESC LIMIT {int(n)}) "
                "ORDER BY timestamp ASC", conn)

    # --- paper trading (sanal portfoy) ---
    def paper_realized_pnl(self) -> float:
        with self.get_connection() as conn:
            r = conn.execute("SELECT COALESCE(SUM(pnl_abs),0) AS s FROM paper_trades WHERE status='CLOSED'").fetchone()
            return float(r["s"] or 0.0)

    def paper_equity(self, start_equity: float) -> float:
        return start_equity + self.paper_realized_pnl()

    def open_paper_trades(self, position_id: int, assets, prices: Dict[str, float],
                          entry_ts: int, alloc_usd: float, fee_pct: float):
        valid = [a for a in assets if prices.get(a)]
        if not valid:
            return
        per = alloc_usd / len(valid)
        with self.get_connection() as conn:
            for a in valid:
                price = prices[a]
                qty = per / price
                fee = per * fee_pct / 100.0
                conn.execute('''INSERT INTO paper_trades
                    (position_id, asset, qty, entry_ts, entry_price, fee, status)
                    VALUES (?,?,?,?,?,?, 'OPEN')''', (position_id, a, qty, entry_ts, price, fee))
            conn.commit()

    def close_paper_trades(self, position_id: int, prices: Dict[str, float],
                           exit_ts: int, fee_pct: float):
        with self.get_connection() as conn:
            rows = conn.execute("SELECT * FROM paper_trades WHERE position_id=? AND status='OPEN'",
                                (position_id,)).fetchall()
            for r in rows:
                exit_price = prices.get(r["asset"], r["entry_price"])  # fiyat yoksa basa bas
                exit_fee = (r["qty"] * exit_price) * fee_pct / 100.0
                pnl = r["qty"] * (exit_price - r["entry_price"]) - (r["fee"] or 0) - exit_fee
                conn.execute('''UPDATE paper_trades SET exit_ts=?, exit_price=?, fee=?,
                    pnl_abs=?, status='CLOSED' WHERE id=?''',
                             (exit_ts, exit_price, (r["fee"] or 0) + exit_fee, pnl, r["id"]))
            conn.commit()

    def get_open_paper_trades(self) -> pd.DataFrame:
        with self.get_connection() as conn:
            return pd.read_sql_query("SELECT * FROM paper_trades WHERE status='OPEN'", conn)

    def get_paper_trades_df(self, limit: int = 500) -> pd.DataFrame:
        with self.get_connection() as conn:
            return pd.read_sql_query(
                f"SELECT * FROM paper_trades ORDER BY entry_ts DESC LIMIT {int(limit)}", conn)

    # --- holdings (kullanici elle girer; bot buna gore yonlendirir) ---
    def add_holding(self, asset: str, kind: str, qty: float, entry_price: float,
                    entry_ts: int, note: str = "") -> int:
        with self.get_connection() as conn:
            cur = conn.execute('''INSERT INTO holdings
                (asset, kind, qty, entry_price, entry_ts, status, note)
                VALUES (?,?,?,?,?, 'OPEN', ?)''', (asset, kind, qty, entry_price, entry_ts, note))
            conn.commit()
            return cur.lastrowid

    def get_open_holdings(self):
        with self.get_connection() as conn:
            return conn.execute("SELECT * FROM holdings WHERE status='OPEN' ORDER BY entry_ts").fetchall()

    def get_holdings_df(self, limit: int = 200) -> pd.DataFrame:
        with self.get_connection() as conn:
            return pd.read_sql_query(
                f"SELECT * FROM holdings ORDER BY entry_ts DESC LIMIT {int(limit)}", conn)

    def close_holding(self, hid: int, exit_price: float, exit_ts: int):
        with self.get_connection() as conn:
            r = conn.execute("SELECT qty, entry_price FROM holdings WHERE id=?", (hid,)).fetchone()
            pnl = (r["qty"] * (exit_price - r["entry_price"])) if r else None
            conn.execute('''UPDATE holdings SET status='CLOSED', exit_price=?, exit_ts=?, pnl_abs=?
                WHERE id=?''', (exit_price, exit_ts, pnl, hid))
            conn.commit()

    def partial_sell(self, hid: int, sell_qty: float, exit_price: float, exit_ts: int):
        """Parcali sat: satilan kismi CLOSED kayit olarak ayir, kalani OPEN birak."""
        with self.get_connection() as conn:
            r = conn.execute("SELECT * FROM holdings WHERE id=?", (hid,)).fetchone()
            if not r:
                return
            if sell_qty >= r["qty"]:
                self.close_holding(hid, exit_price, exit_ts)
                return
            pnl = sell_qty * (exit_price - r["entry_price"])
            conn.execute('''INSERT INTO holdings
                (asset, kind, qty, entry_price, entry_ts, status, exit_price, exit_ts, pnl_abs, note)
                VALUES (?,?,?,?,?, 'CLOSED', ?,?,?, ?)''',
                         (r["asset"], r["kind"], sell_qty, r["entry_price"], r["entry_ts"],
                          exit_price, exit_ts, pnl, "parcali sat"))
            conn.execute("UPDATE holdings SET qty=qty-? WHERE id=?", (sell_qty, hid))
            conn.commit()

    def holdings_realized_pnl(self) -> float:
        with self.get_connection() as conn:
            r = conn.execute("SELECT COALESCE(SUM(pnl_abs),0) AS s FROM holdings WHERE status='CLOSED'").fetchone()
            return float(r["s"] or 0.0)

    def purge_old_data(self, days: int = 45):
        cutoff = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
        with self.get_connection() as conn:
            cur = conn.execute('DELETE FROM btc_candles WHERE timestamp < ?', (cutoff,))
            conn.execute('DELETE FROM sentiment_history WHERE timestamp < ?', (cutoff,))
            conn.commit()
            return cur.rowcount

    def backup_db(self, backup_dir: str = "backups", keep: int = 7) -> Optional[str]:
        """Tutarli (canli) yedek - SQLite backup API. Eski yedekleri (keep'ten fazlasini) siler."""
        import glob
        try:
            os.makedirs(backup_dir, exist_ok=True)
            dest = os.path.join(backup_dir, f"trading_bot_{datetime.now().strftime('%Y%m%d_%H%M')}.db")
            with self.get_connection() as src, sqlite3.connect(dest) as dst:
                src.backup(dst)
            files = sorted(glob.glob(os.path.join(backup_dir, "trading_bot_*.db")))
            for old in files[:-keep]:
                try:
                    os.remove(old)
                except OSError:
                    pass
            return dest
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Settings — config tablosunu tip guvenli okur (worker her dongude tazeler)
# ---------------------------------------------------------------------------
class Settings:
    def __init__(self, db: Database):
        self.db = db
        self.refresh()

    def refresh(self):
        self._c = self.db.get_config()

    def s(self, key: str) -> str:
        return self._c.get(key, ENV_DEFAULTS.get(key, ""))

    def f(self, key: str) -> float:
        try:
            return float(self.s(key))
        except (TypeError, ValueError):
            return 0.0

    def b(self, key: str) -> bool:
        return self.s(key).strip().lower() in ("1", "true", "yes", "on")

    def list(self, key: str) -> List[str]:
        return csv_list(self.s(key))


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------
class IndicatorCalc:
    @staticmethod
    def sma(series: pd.Series, window: int) -> pd.Series:
        return series.rolling(window=window).mean()

    @staticmethod
    def rsi(series: pd.Series, window: int = 14) -> pd.Series:
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1/window, min_periods=window).mean()
        avg_loss = loss.ewm(alpha=1/window, min_periods=window).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        return (100 - (100 / (1 + rs))).fillna(100)

    @staticmethod
    def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
        ema_fast = series.ewm(span=fast, adjust=False).mean()
        ema_slow = series.ewm(span=slow, adjust=False).mean()
        macd = ema_fast - ema_slow
        signal_line = macd.ewm(span=signal, adjust=False).mean()
        return macd, signal_line

    @staticmethod
    def vwap(df: pd.DataFrame) -> pd.Series:
        tp = (df['high'] + df['low'] + df['close']) / 3
        tp_v = tp * df['volume']
        date = df['timestamp_dt'].dt.date
        return tp_v.groupby(date).cumsum() / df['volume'].groupby(date).cumsum()

    @staticmethod
    def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
        high, low, close = df['high'], df['low'], df['close']
        prev_close = close.shift(1)
        tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
        return tr.ewm(alpha=1/window, min_periods=window).mean()

    @staticmethod
    def bollinger(series: pd.Series, window: int = 20, num_std: float = 2.0):
        mid = series.rolling(window).mean()
        std = series.rolling(window).std()
        upper = mid + num_std * std
        lower = mid - num_std * std
        width = (upper - lower) / mid
        pct_b = (series - lower) / (upper - lower)
        return mid, upper, lower, width, pct_b

    @staticmethod
    def stoch_rsi(series: pd.Series, rsi_window: int = 14, stoch_window: int = 14, k: int = 3, d: int = 3):
        rsi = IndicatorCalc.rsi(series, rsi_window)
        lo = rsi.rolling(stoch_window).min()
        hi = rsi.rolling(stoch_window).max()
        stoch = (rsi - lo) / (hi - lo).replace(0, np.nan)
        k_line = (stoch * 100).rolling(k).mean()
        d_line = k_line.rolling(d).mean()
        return k_line, d_line

    @staticmethod
    def adx(df: pd.DataFrame, window: int = 14):
        high, low = df['high'], df['low']
        up = high.diff()
        down = -low.diff()
        plus_dm = np.where((up > down) & (up > 0), up, 0.0)
        minus_dm = np.where((down > up) & (down > 0), down, 0.0)
        atr = IndicatorCalc.atr(df, window).replace(0, np.nan)
        plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1/window, min_periods=window).mean() / atr
        minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1/window, min_periods=window).mean() / atr
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        adx = dx.ewm(alpha=1/window, min_periods=window).mean()
        return adx, plus_di, minus_di

    @staticmethod
    def obv(df: pd.DataFrame) -> pd.Series:
        direction = np.sign(df['close'].diff()).fillna(0)
        return (direction * df['volume']).cumsum()

    @staticmethod
    def choppiness(df: pd.DataFrame, window: int = 14) -> pd.Series:
        """Choppiness Index (0-100). >61.8 = yatay/trendsiz (CHOP), <38.2 = guclu trend."""
        high, low, close = df['high'], df['low'], df['close']
        prev = close.shift(1)
        tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
        atr_sum = tr.rolling(window).sum()
        rng = high.rolling(window).max() - low.rolling(window).min()
        return 100 * np.log10(atr_sum / rng.replace(0, np.nan)) / np.log10(window)

    @staticmethod
    def enrich(df: pd.DataFrame) -> pd.DataFrame:
        """5dk df'e tum indikatorleri ekler (worker + backtest + dashboard ortak kullanir)."""
        df = df.reset_index(drop=True).copy()
        df['rsi'] = IndicatorCalc.rsi(df['close'], 14)
        df['macd'], df['macd_signal'] = IndicatorCalc.macd(df['close'])
        df['vwap'] = IndicatorCalc.vwap(df)
        df['atr'] = IndicatorCalc.atr(df, 14)
        _, df['bb_upper'], df['bb_lower'], df['bb_width'], df['bb_pctb'] = IndicatorCalc.bollinger(df['close'])
        df['stochrsi_k'], df['stochrsi_d'] = IndicatorCalc.stoch_rsi(df['close'])
        df['adx'], df['plus_di'], df['minus_di'] = IndicatorCalc.adx(df)
        df['obv'] = IndicatorCalc.obv(df)
        return df

    @staticmethod
    def resample_4h(df: pd.DataFrame) -> pd.DataFrame:
        df_4h = df.resample('4h', on='timestamp_dt').agg(
            {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'}).dropna()
        df_4h['sma_50'] = IndicatorCalc.sma(df_4h['close'], 50)
        df_4h['sma_200'] = IndicatorCalc.sma(df_4h['close'], 200)
        return df_4h


# ---------------------------------------------------------------------------
# Signal scoring engine  (-100 .. +100)
# ---------------------------------------------------------------------------
class SignalScorer:
    @staticmethod
    def score(row: pd.Series, prev: pd.Series, last_4h: pd.Series, current_price: float,
              funding_rate: Optional[float], funding_trend: Optional[float] = None,
              oi_trend: Optional[float] = None) -> Tuple[float, Dict]:
        votes: List[Tuple[float, float, str]] = []

        def clamp(x):
            return max(-1.0, min(1.0, x))

        if pd.notna(last_4h['sma_50']):
            votes.append((clamp((current_price - last_4h['sma_50']) / last_4h['sma_50'] * 50), 2.5, "4S SMA50 trend"))
        if pd.notna(last_4h['sma_200']):
            votes.append((clamp((current_price - last_4h['sma_200']) / last_4h['sma_200'] * 25), 1.5, "4S SMA200 trend"))

        macd_hist = row['macd'] - row['macd_signal']
        prev_hist = prev['macd'] - prev['macd_signal']
        votes.append((1.0 if macd_hist > 0 else -1.0, 1.5, "MACD yon"))
        votes.append((clamp((macd_hist - prev_hist) * 100), 1.0, "MACD ivme"))

        rsi = row['rsi']
        votes.append((clamp((rsi - 50) / 25), 1.0, "RSI seviye"))
        votes.append((1.0 if rsi > prev['rsi'] else -1.0, 0.8, "RSI egim"))

        k, d = row['stochrsi_k'], row['stochrsi_d']
        if pd.notna(k) and pd.notna(d):
            sr = 0.0
            if k < 20:
                sr += 0.6
            elif k > 80:
                sr -= 0.6
            sr += 0.4 if k > d else -0.4
            votes.append((clamp(sr), 1.0, "StochRSI"))

        votes.append((1.0 if current_price > row['vwap'] else -1.0, 1.0, "VWAP"))

        pb = row['bb_pctb']
        if pd.notna(pb):
            votes.append((clamp((pb - 0.5) * 2), 0.7, "Bollinger %B"))

        votes.append((1.0 if row['obv'] > prev['obv'] else -1.0, 0.8, "OBV (hacim)"))

        # Funding rate (KONTRARYAN seviye)
        if funding_rate is not None:
            fr_bps = funding_rate * 10000
            if abs(fr_bps) >= 8:
                votes.append((clamp(-fr_bps / 10), 1.0, "Funding (asiri/kontraryan)"))
            else:
                votes.append((clamp(fr_bps / 10), 0.4, "Funding (mild)"))

        # Funding trendi: dususte ise yukaris lehine (kalabalik long azaliyor)
        if funding_trend is not None:
            votes.append((clamp(-funding_trend * 5000), 0.5, "Funding trend"))

        # Open interest trendi + fiyat yonu teyidi
        if oi_trend is not None:
            price_dir = 1.0 if row['close'] > prev['close'] else -1.0
            # OI artisi mevcut yonu guclendirir; OI dususu zayiflatir
            votes.append((clamp(price_dir * np.sign(oi_trend) * min(abs(oi_trend) * 20, 1.0)), 0.6, "OI teyit"))

        total_w = sum(w for _, w, _ in votes)
        raw = sum(v * w for v, w, _ in votes)
        score = (raw / total_w) * 100 if total_w else 0.0
        return round(score, 1), {label: round(v, 2) for v, _, label in votes}


def alpaca_latest_prices(symbols) -> Dict[str, float]:
    """Alpaca'dan en guncel ETF/hisse fiyatlari (ucretsiz IEX feed).
    .env'de ALPACA_API_KEY / ALPACA_API_SECRET yoksa veya hata olursa bos doner
    (cagiran taraf yfinance'a duser). Ucretsiz katman ~gercek zamanli (IEX)."""
    key = os.getenv("ALPACA_API_KEY", "")
    secret = os.getenv("ALPACA_API_SECRET", "")
    if not key or not secret or not symbols:
        return {}
    feed = os.getenv("ALPACA_FEED", "iex")
    try:
        import requests
        url = "https://data.alpaca.markets/v2/stocks/snapshots"
        resp = requests.get(url, params={"symbols": ",".join(symbols), "feed": feed},
                            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
                            timeout=10)
        resp.raise_for_status()
        data = resp.json()
        out: Dict[str, float] = {}
        for sym, snap in data.items():
            if not isinstance(snap, dict):
                continue
            price = None
            trade = snap.get("latestTrade") or {}
            quote = snap.get("latestQuote") or {}
            if trade.get("p"):
                price = float(trade["p"])
            elif quote.get("ap") and quote.get("bp"):  # alis/satis ortasi
                price = (float(quote["ap"]) + float(quote["bp"])) / 2
            if price and price > 0:
                out[sym] = price
        return out
    except Exception:
        return {}


def f_or_none(x):
    """NaN-guvenli float (JSON icin)."""
    try:
        return float(x) if pd.notna(x) else None
    except (TypeError, ValueError):
        return None


def us_market_status(now_utc: Optional[datetime] = None) -> str:
    """ABD borsa seansi durumu (Midas uzatilmis saat dahil):
       OPEN   = normal seans      09:30-16:00 ET
       PRE    = acilis oncesi      04:00-09:30 ET
       AFTER  = kapanis sonrasi    16:00-20:00 ET
       CLOSED = islem yok / hafta sonu
    Midas pre-market ve after-hours'ta islem yapilabilir (likidite dusuk, spread genis)."""
    now_ny = (now_utc or datetime.now(timezone.utc)).astimezone(NY_TZ)
    if now_ny.weekday() >= 5:
        return "CLOSED"
    t = now_ny.hour * 60 + now_ny.minute
    if 9 * 60 + 30 <= t < 16 * 60:
        return "OPEN"
    if 4 * 60 <= t < 9 * 60 + 30:
        return "PRE"
    if 16 * 60 <= t < 20 * 60:
        return "AFTER"
    return "CLOSED"


def is_tradeable(status: Optional[str] = None) -> bool:
    """Midas'ta islem yapilabilir mi (normal + uzatilmis saatler)."""
    s = status or us_market_status()
    return s in ("OPEN", "PRE", "AFTER")


def regime_from(adx: Optional[float], ci: Optional[float],
                adx_min: float = 20, ci_chop: float = 61.8) -> str:
    """Piyasa rejimi: CHOP (yatay), TREND (yonlu), ZAYIF (belirsiz)."""
    if ci is not None and ci >= ci_chop:
        return "CHOP"
    if adx is not None and adx >= adx_min:
        return "TREND"
    return "ZAYIF"


def weekend_state(now_utc: Optional[datetime] = None,
                  flat_before: float = 20, noentry_before: float = 90) -> Optional[str]:
    """Hafta sonu gap riski durumu (ABD borsa kapanisi 16:00 ET baz alinir):
       FLATTEN  = Cuma kapanisa <flat_before> dk kala (ve sonrasi) -> pozisyonlari kapat
       NO_ENTRY = Cuma kapanisa <noentry_before> dk kala -> yeni AL onerme
       WEEKEND  = Cmt/Pazar -> piyasa kapali, hafta sonu
       None     = normal."""
    ny = (now_utc or datetime.now(timezone.utc)).astimezone(NY_TZ)
    wd = ny.weekday()  # Pzt=0 ... Cuma=4, Cmt=5, Pazar=6
    minutes = ny.hour * 60 + ny.minute
    close = 16 * 60
    if wd == 4:  # Cuma
        if minutes >= close - flat_before:
            return "FLATTEN"
        if minutes >= close - noentry_before:
            return "NO_ENTRY"
    if wd in (5, 6):
        return "WEEKEND"
    return None


def weekend_override(wstate: Optional[str]):
    """Hafta sonu durumuna gore holding tavsiyesini ezer. (advice, reason, frac) ya da None."""
    if wstate == "FLATTEN":
        return "SAT", "🛡️ Hafta sonu koruması: seans kapanışı yakın, kaldıraçlı ETF'i elden çıkar", 1.0
    if wstate == "WEEKEND":
        return "SAT", "🛡️ Hafta sonu: piyasa kapalı — açılışta/pre-market kapat", 1.0
    return None


def holding_advice(kind, score, regime, mstr_ok, rsi, live_price, entry_price, settings):
    """Kullanicinin ELDEKI pozisyonuna gore kademeli tavsiye.
    Donen: (advice, reason, fraction)
      advice  : EKLE / TUT / PARCALI_SAT / SAT / AL (AL bu fonksiyonda uretilmez)
      fraction: PARCALI_SAT/SAT icin satilacak oran (0-1), digerinde None
    kind: 'BULL' (MSTU/MSTX) ya da 'BEAR' (MSTZ). dir_score = sinyalin LEHE puani."""
    sig_thr = settings.f("SIGNAL_SCORE_THRESHOLD") or 40
    watch_thr = settings.f("WATCH_SCORE_THRESHOLD") or 22
    stop_pct = settings.f("HOLDING_STOP_PCT") or 15
    part = (settings.f("PARTIAL_SELL_PCT") or 33) / 100.0
    dir_score = score if kind == "BULL" else -score

    # 1) Sert ETF stopu (gercek enstrumandaki zarar)
    if live_price and entry_price:
        dd = (live_price - entry_price) / entry_price * 100
        if dd <= -stop_pct:
            return "SAT", f"STOP: girise gore {dd:+.0f}% (>= {stop_pct:.0f}% zarar)", 1.0

    # 2) Sert ters sinyal -> tam cikis
    if dir_score <= -sig_thr:
        return "SAT", f"Sinyal sert ters dondu (lehe skor {dir_score:+.0f})", 1.0

    # 3) Aleyhine zayifliyor -> parcali sat
    if dir_score < 0:
        return "PARCALI_SAT", f"Sinyal aleyhine donuyor (lehe skor {dir_score:+.0f})", part

    # 4) Yatay piyasada zayif lehte -> riski azalt (kaldiracli ETF erir)
    if regime == "CHOP" and dir_score < watch_thr:
        return "PARCALI_SAT", f"Yatay piyasa (chop), lehe skor zayif {dir_score:+.0f}", part

    # 5) Guclu + trendli + teyitli + asiri degil -> EKLE
    overbought = rsi is not None and ((kind == "BULL" and rsi >= 75) or (kind == "BEAR" and rsi <= 25))
    if dir_score >= sig_thr and regime == "TREND" and mstr_ok is not False and not overbought:
        return "EKLE", f"Guclu ve devam ediyor (lehe skor {dir_score:+.0f})", None

    # 6) Lehine ama temkinli -> TUT
    if overbought:
        return "TUT", f"Lehine ama asiri (RSI={rsi:.0f}); ekleme yapma", None
    return "TUT", f"Lehine, temkinli tut (lehe skor {dir_score:+.0f})", None


# ===========================================================================
# Coklu zaman dilimi (MTF) analizi + 6 saatlik koni + ampirik olasilik
# ===========================================================================
def fetch_klines(symbol: str = "BTCUSDT", interval: str = "1h", limit: int = 400) -> pd.DataFrame:
    """Binance'tan herhangi bir zaman diliminde OHLCV (anahtar gerekmez)."""
    try:
        import requests
        url = f"{BINANCE_API_URL}?symbol={symbol}&interval={interval}&limit={limit}"
        data = requests.get(url, timeout=10).json()
        if not isinstance(data, list) or not data:
            return pd.DataFrame()
        df = pd.DataFrame([{
            "timestamp": int(r[0]), "open": float(r[1]), "high": float(r[2]),
            "low": float(r[3]), "close": float(r[4]), "volume": float(r[5]),
        } for r in data])
        df["timestamp_dt"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df
    except Exception:
        return pd.DataFrame()


def normalize_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    """yfinance ciktisini (Open/High/.. + DatetimeIndex) ortak formata cevirir."""
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.rename(columns={c: c.lower() for c in df.columns}).copy()
    idx = pd.to_datetime(out.index, utc=True)
    out = out.reset_index(drop=True)
    out["timestamp_dt"] = idx
    keep = [c for c in ["open", "high", "low", "close", "volume", "timestamp_dt"] if c in out.columns]
    return out[keep].dropna()


def mtf_score(df: pd.DataFrame) -> Tuple[float, str, Dict]:
    """Tek bir zaman dilimi OHLCV df'inden yon skoru (-100..100) + etiket.
    Trend (EMA20/50), RSI (seviye+egim), MACD ve trend gucu (ADX) birlesir."""
    if df is None or len(df) < 55:
        return 0.0, "VERI YOK", {}
    close = df["close"]
    ema_f = close.ewm(span=20, adjust=False).mean()
    ema_s = close.ewm(span=50, adjust=False).mean()
    rsi = IndicatorCalc.rsi(close, 14)
    macd, sig = IndicatorCalc.macd(close)
    adx, _, _ = IndicatorCalc.adx(df)

    def clamp(x):
        return max(-1.0, min(1.0, x))

    votes = [
        (1.0 if close.iloc[-1] > ema_s.iloc[-1] else -1.0, 2.0),   # fiyat vs EMA50
        (1.0 if ema_f.iloc[-1] > ema_s.iloc[-1] else -1.0, 1.5),   # EMA20 vs EMA50
        (clamp((rsi.iloc[-1] - 50) / 25), 1.0),                    # RSI seviye
        (1.0 if rsi.iloc[-1] > rsi.iloc[-2] else -1.0, 0.6),       # RSI egim
        (1.0 if (macd.iloc[-1] - sig.iloc[-1]) > 0 else -1.0, 1.5),  # MACD
    ]
    total_w = sum(w for _, w in votes)
    score = sum(v * w for v, w in votes) / total_w * 100
    adx_last = float(adx.iloc[-1]) if pd.notna(adx.iloc[-1]) else 0.0

    if score >= 20 and adx_last >= 20:
        label = "YUKARI"
    elif score <= -20 and adx_last >= 20:
        label = "ASAGI"
    elif score >= 12:
        label = "ZAYIF YUKARI"
    elif score <= -12:
        label = "ZAYIF ASAGI"
    else:
        label = "NOTR"
    return round(score, 1), label, {"adx": round(adx_last, 1), "rsi": round(float(rsi.iloc[-1]), 1)}


def forecast_cone(df: pd.DataFrame, horizon_hours: float = 6, bar_minutes: int = 5,
                  drift_bias: float = 0.0) -> Dict:
    """Volatilite konisi: son fiyattan baslayip ATR/oynaklik ile olasi ARALIK cizer.
    Tahmin degil senaryo; drift_bias (-1..1) hafif yon egilimi verir (skordan)."""
    if df is None or len(df) < 50:
        return {}
    close = df["close"]
    logret = np.log(close / close.shift(1)).dropna()
    sigma_bar = float(logret.tail(288).std())  # son ~1 gunluk 5dk oynakligi
    if not np.isfinite(sigma_bar) or sigma_bar <= 0:
        return {}
    n = int(horizon_hours * 60 / bar_minutes)
    steps = np.arange(1, n + 1)
    last_price = float(close.iloc[-1])
    last_time = df["timestamp_dt"].iloc[-1]
    mu_bar = clamp_drift(drift_bias) * (sigma_bar * 0.3)  # mutevazi drift
    sigma_t = sigma_bar * np.sqrt(steps)
    times = [last_time + pd.Timedelta(minutes=bar_minutes * int(s)) for s in steps]
    return {
        "times": times,
        "median": last_price * np.exp(mu_bar * steps),
        "upper1": last_price * np.exp(mu_bar * steps + sigma_t),
        "lower1": last_price * np.exp(mu_bar * steps - sigma_t),
        "upper2": last_price * np.exp(mu_bar * steps + 2 * sigma_t),
        "lower2": last_price * np.exp(mu_bar * steps - 2 * sigma_t),
        "last_price": last_price,
        "last_time": last_time,
        "sigma_6h_pct": round(float(sigma_bar * np.sqrt(n)) * 100, 2),
    }


def clamp_drift(x: float) -> float:
    return max(-1.0, min(1.0, x))


def empirical_up_probability(df: pd.DataFrame, horizon_bars: int = 72) -> Tuple[Optional[float], int]:
    """Gecmiste, SU ANKI rejime (trend + momentum yonu) benzer durumlarda fiyatin
    sonraki `horizon_bars` icinde yukari kapama orani. Tahmin degil, baz oran."""
    if df is None or len(df) < horizon_bars + 200:
        return None, 0
    close = df["close"].reset_index(drop=True)
    ema50 = close.ewm(span=50, adjust=False).mean()
    macd, sig = IndicatorCalc.macd(close)
    trend_up = close > ema50
    mom_up = (macd - sig) > 0
    fut = close.shift(-horizon_bars)
    up = fut > close
    cu, cm = bool(trend_up.iloc[-1]), bool(mom_up.iloc[-1])
    mask = (trend_up == cu) & (mom_up == cm) & up.notna()
    sub = up[mask]
    if len(sub) < 30:
        return None, len(sub)
    return round(float(sub.mean()) * 100, 1), int(len(sub))
