"""
backtest.py — Stratejinin gecmis veride isabetini olcer.
Ayni indikator + skorlama mantigini (core) kullanir, tek pozisyonlu basit bir
simulasyon yapar: skor esigi gecince giris, stop / ters sinyal / zaman asimi ile cikis.

Kullanim:
    python backtest.py                       # varsayilan ayarlarla
    python backtest.py --threshold 35 --atr-mult 2.5 --leverage 2
    python backtest.py --grid                # esik/ADX kombinasyonlarini tara
"""
import argparse
from typing import List, Dict

import numpy as np
import pandas as pd

from core import Database, IndicatorCalc, SignalScorer


def run_backtest(df: pd.DataFrame, threshold: float, adx_min: float,
                 atr_mult: float, max_hold_h: float, leverage: float) -> Dict:
    """Tek pozisyonlu simulasyon. BTC bazli getiri * leverage = ETF yaklasik getirisi."""
    df = IndicatorCalc.enrich(df)
    df_4h = IndicatorCalc.resample_4h(df)
    # her 5dk satira o ana ait son kapanan 4S degerlerini esle
    df = df.copy()
    df['ts'] = df['timestamp_dt']
    sma50 = df_4h['sma_50'].reindex(df['ts'], method='ffill').values
    sma200 = df_4h['sma_200'].reindex(df['ts'], method='ffill').values
    df['sma_50_4h'] = sma50
    df['sma_200_4h'] = sma200

    trades: List[Dict] = []
    pos = None  # {'dir','entry_i','entry_price','stop'}

    start = 9600  # yeterli warmup (SMA200-4S)
    for i in range(start, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i - 1]
        price = row['close']
        if pd.isna(row['sma_50_4h']) or pd.isna(row['adx']) or pd.isna(row['atr']):
            continue
        last_4h = pd.Series({'sma_50': row['sma_50_4h'], 'sma_200': row['sma_200_4h']})
        score, _ = SignalScorer.score(row, prev, last_4h, price, None)
        adx_val = row['adx']
        atr_val = row['atr']

        # --- cikis ---
        if pos:
            exit_reason = None
            if pos['dir'] == 'BULL' and price <= pos['stop']:
                exit_reason = 'STOP'
            elif pos['dir'] == 'BEAR' and price >= pos['stop']:
                exit_reason = 'STOP'
            elif pos['dir'] == 'BULL' and score <= -threshold and adx_val >= adx_min:
                exit_reason = 'REVERSE'
            elif pos['dir'] == 'BEAR' and score >= threshold and adx_val >= adx_min:
                exit_reason = 'REVERSE'
            else:
                held_h = (i - pos['entry_i']) * 5 / 60
                if max_hold_h > 0 and held_h >= max_hold_h:
                    exit_reason = 'TIME'
            if exit_reason:
                move = (price - pos['entry_price']) / pos['entry_price'] * 100
                pnl = move if pos['dir'] == 'BULL' else -move
                trades.append({
                    'dir': pos['dir'], 'entry': pos['entry_price'], 'exit': price,
                    'btc_pct': pnl, 'etf_pct': pnl * leverage, 'reason': exit_reason,
                    'bars': i - pos['entry_i'],
                    'entry_time': df.iloc[pos['entry_i']]['timestamp_dt'],
                    'exit_time': row['timestamp_dt'],
                })
                pos = None

        # --- giris ---
        if not pos and adx_val >= adx_min:
            if score >= threshold:
                pos = {'dir': 'BULL', 'entry_i': i, 'entry_price': price, 'stop': price - atr_mult * atr_val}
            elif score <= -threshold:
                pos = {'dir': 'BEAR', 'entry_i': i, 'entry_price': price, 'stop': price + atr_mult * atr_val}

    tdf = pd.DataFrame(trades)
    if tdf.empty:
        return {'trades': 0, 'win_rate': 0, 'avg_etf_pct': 0, 'total_etf_pct': 0,
                'max_win': 0, 'max_loss': 0, 'df': tdf}
    wins = (tdf['etf_pct'] > 0).sum()
    return {
        'trades': len(tdf),
        'win_rate': round(wins / len(tdf) * 100, 1),
        'avg_etf_pct': round(tdf['etf_pct'].mean(), 2),
        'total_etf_pct': round(tdf['etf_pct'].sum(), 1),
        'avg_btc_pct': round(tdf['btc_pct'].mean(), 2),
        'max_win': round(tdf['etf_pct'].max(), 1),
        'max_loss': round(tdf['etf_pct'].min(), 1),
        'avg_bars': round(tdf['bars'].mean(), 1),
        'df': tdf,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--threshold', type=float, default=40)
    ap.add_argument('--adx-min', type=float, default=20)
    ap.add_argument('--atr-mult', type=float, default=2.0)
    ap.add_argument('--max-hold', type=float, default=48)
    ap.add_argument('--leverage', type=float, default=2.0, help='ETF kaldiraci (MSTU/MSTX ~2x)')
    ap.add_argument('--grid', action='store_true', help='parametre taramasi')
    ap.add_argument('--save', type=str, default='', help='islemleri CSV olarak kaydet')
    args = ap.parse_args()

    db = Database()
    df = db.get_all_candles()
    print(f"Mum sayisi: {len(df)}  ({df['timestamp_dt'].min()} -> {df['timestamp_dt'].max()})\n")
    if len(df) < 9600:
        print("UYARI: yeterli mum yok (>=9600 gerekli). Once botu calistirip veri biriktir.")
        return

    if args.grid:
        print("=== PARAMETRE TARAMASI (kaldirac=%.0fx) ===" % args.leverage)
        print(f"{'thr':>5} {'adx':>5} {'atr':>5} | {'islem':>6} {'kazanc%':>8} {'topETF%':>9} {'ortETF%':>8}")
        for thr in (30, 35, 40, 45):
            for adx in (15, 20, 25):
                for am in (1.5, 2.0, 2.5):
                    r = run_backtest(df, thr, adx, am, args.max_hold, args.leverage)
                    print(f"{thr:>5} {adx:>5} {am:>5} | {r['trades']:>6} {r['win_rate']:>7}% "
                          f"{r['total_etf_pct']:>8}% {r['avg_etf_pct']:>7}%")
        return

    r = run_backtest(df, args.threshold, args.adx_min, args.atr_mult, args.max_hold, args.leverage)
    print(f"=== BACKTEST (thr={args.threshold}, adx>={args.adx_min}, "
          f"atr={args.atr_mult}, kaldirac={args.leverage}x) ===")
    print(f"Islem sayisi    : {r['trades']}")
    print(f"Kazanc orani    : {r['win_rate']}%")
    print(f"Toplam ETF getr : {r['total_etf_pct']}%  (komisyon/decay HARIC, yaklasik)")
    print(f"Ortalama / islem: {r['avg_etf_pct']}%  (BTC bazli {r.get('avg_btc_pct')}%)")
    print(f"En iyi / en kotu: +{r['max_win']}% / {r['max_loss']}%")
    print(f"Ort. tutma      : {r.get('avg_bars')} mum (~{r.get('avg_bars',0)*5/60:.1f} saat)")
    print("\nNOT: Bu BTC hareketine kaldirac uygulanmis YAKLASIK bir tahmindir. "
          "ETF'ler aslinda MSTR'i takip eder; gercek sonuc komisyon, spread ve "
          "gunluk kaldirac asinmasi (decay) nedeniyle farkli olur.")
    if args.save and not r['df'].empty:
        r['df'].to_csv(args.save, index=False)
        print(f"\nIslemler kaydedildi: {args.save}")


if __name__ == "__main__":
    main()
