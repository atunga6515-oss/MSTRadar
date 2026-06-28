# 📡 MSTRadar

**BTC'yi tarayıp kaldıraçlı MSTR ETF'leri için sinyal üreten radar.**

MSTRadar, Binance'tan gerçek zamanlı BTC verisini izler; bir dizi teknik indikatörü
ağırlıklı bir skora dönüştürür ve MSTR'a dayalı kaldıraçlı ETF'ler (long: MSTU/MSTX,
short: MSTZ) için al/sat/erken-uyarı sinyalleri üretir. Sinyaller Telegram'a düşer,
yerel bir panelden izlenir/yönetilir ve bir sanal portföyle gerçek başarısı ölçülür.

> ⚠️ **MSTRadar otomatik işlem yapmaz.** Yalnızca sinyal/uyarı üretir; işlemleri
> kullanıcı kendi aracı kurumunda manuel yapar. Bu bir yatırım tavsiyesi değildir.
> Kaldıraçlı ETF'ler yüksek risklidir ve yatay piyasada "decay" ile değer kaybeder.

---

## Özellikler

- **Gerçek zamanlı BTC sinyali** — Binance 5dk mumları, 4S makro trend (SMA50/200).
- **Ağırlıklı skorlama (−100…+100)** — RSI, MACD, VWAP, Bollinger, Stochastic RSI,
  ADX, OBV ve Binance Futures funding rate + open interest tek bir skorda birleşir.
- **Pozisyon yaşam döngüsü** — giriş + ATR tabanlı stop, ters sinyal ve zaman aşımı
  ile **otomatik çıkış uyarısı**.
- **Piyasa saati farkındalığı** — ABD seansı PRE / OPEN / AFTER / CLOSED (Midas
  uzatılmış saatleri dahil, DST otomatik).
- **Hafta sonu koruması** — Cuma kapanışına yakın elindeki kaldıraçlı ETF'ler için
  **SAT** uyarısı + Cuma öğleden sonra yeni giriş önermeme (hafta sonu BTC gap riski).
- **Kişisel portföy danışmanı** — ETF alımlarını panele **sen girersin**; bot
  elindekine göre kademeli yönlendirir: **AL / EKLE / TUT / PARÇALI SAT / SAT**
  (sinyal gücü, rejim, MSTR teyidi ve girişe göre stop ile). Bot otomatik işlem
  yapmaz; kararı sen verirsin.
- **Yerel yönetim paneli (Streamlit)** — BTC + MSTR + ETF grafikleri, BTC↔MSTR
  korelasyonu (baz riski), çoklu zaman dilimi tahmini, portföyüm ekranı ve canlı
  ayar düzenleme.
- **Esnek veri kaynağı** — ETF fiyatı için önce Alpaca (~gerçek zamanlı IEF), yoksa
  otomatik yfinance.
- **Backtest + parametre taraması** — stratejiyi kendi verinde doğrula.
- **Dayanıklılık** — SQLite WAL modu, günlük otomatik DB yedeği, log rotation.

## Mimari

İki bağımsız process, ortak SQLite (`trading_bot.db`) üzerinden konuşur:

| Dosya | Görev |
|-------|-------|
| `core.py` | Ortak çekirdek: DB, indikatörler, skorlama, ayarlar, veri kaynakları |
| `bot.py` | Worker / scheduler — 7/24 çalışır, sinyal + pozisyon yönetir, Telegram |
| `dashboard.py` | Streamlit panel — grafik, sinyal, performans, yönetim (yerel) |
| `backtest.py` | Stratejinin geçmiş veride isabetini ölçer |
| `deploy/` | systemd servis dosyaları (Linux 7/24) |

## Kurulum

```bash
git clone https://github.com/<kullanici>/MSTRadar.git
cd MSTRadar
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # .env içine anahtarlarını yaz (aşağıya bak)
```

### `.env` (gizli — repoya gitmez)

| Değişken | Zorunlu | Açıklama |
|----------|---------|----------|
| `TELEGRAM_BOT_TOKEN` | ✅ | BotFather'dan |
| `TELEGRAM_CHAT_ID` | ✅ | Mesajların gideceği chat |
| `ALPACA_API_KEY` / `ALPACA_API_SECRET` | ❌ | ETF için ~gerçek zamanlı veri; boşsa yfinance |

Diğer eşikler ve davranış ayarları `.env.example` içinde belgelenmiştir ve
panelden canlı değiştirilebilir.

## Çalıştırma

```bash
python bot.py                                           # worker (7/24)
streamlit run dashboard.py --server.address 127.0.0.1   # panel (SADECE yerel)
python backtest.py                                      # tek koşu
python backtest.py --grid                               # parametre taraması
```

> 🔒 **Güvenlik:** paneli internete açma. Yalnızca `127.0.0.1` veya VPN/Tailscale
> üzerinden eriş. `0.0.0.0` kullanma.

## 7/24 dağıtım (Linux)

`systemd` ile kurulum, otomatik başlatma ve loglar için → **[DEPLOY.md](DEPLOY.md)**.

## Strateji hakkında dürüst not

`backtest.py` mevcut veride çalıştırıldığında strateji çoğu parametrede **başa baş /
hafif zararlı** çıkabiliyor. Kısa süreli yüksek getiriler büyük olasılıkla o döneme
özgü güçlü bir trendin kaldıraçla büyümesidir — kalıcı bir avantaj kanıtı değil.
Ayrıca ETF'ler aslında **MSTR'ı** takip eder (BTC'yi değil), bu da bir baz riski
taşır. Gerçek parayla güvenmeden önce mutlaka farklı dönemlerde backtest yap.

## Lisans

MIT — bkz. [LICENSE](LICENSE).
