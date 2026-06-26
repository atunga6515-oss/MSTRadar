# Linux'ta 7/24 Çalıştırma (systemd)

Bu rehber botu Linux makinende `systemd` ile kurar: makine açıldığında otomatik
başlar, çökerse kendini yeniden başlatır, logları `journalctl`'de tutulur.

## 1. Dosyaları kopyala

macOS'taki `venv/` klasörünü **kopyalama** (platforma özeldir, Linux'ta çalışmaz).
Geri kalan her şeyi kopyala — özellikle `.env` (Telegram + Alpaca anahtarların) ve
istersen `trading_bot.db` (geçmiş veri + sanal portföy kayıtların korunur).

```bash
# Linux makinede, örn. home dizinine
mkdir -p ~/btc_etf_bot
# macOS'tan kopyalama (scp ornegi; venv haric):
# scp -r ./* ~/btc_etf_bot/   ya da rsync:
rsync -av --exclude venv --exclude __pycache__ ./ kullanici@linux-ip:~/btc_etf_bot/
```

## 2. Sanal ortam + bağımlılıklar (Linux'ta yeniden)

```bash
cd ~/btc_etf_bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 3. `.env` kontrolü

`.env` içinde `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `ALPACA_API_KEY`,
`ALPACA_API_SECRET` dolu olmalı. (Kopyaladıysan zaten hazır.)

Tek seferlik elle test:
```bash
source venv/bin/activate && python bot.py
# Telegram'a "Bot Aktif" geldiyse Ctrl+C ile durdur, servise geç.
```

## 4. systemd servislerini kur

`deploy/` içindeki iki dosyada **User** ve **yolları kendine göre düzelt**
(varsayılan `alper` / `/home/alper/btc_etf_bot`). Sonra:

```bash
sudo cp deploy/btc-bot.service /etc/systemd/system/
sudo cp deploy/btc-dashboard.service /etc/systemd/system/   # dashboard istersen
sudo systemctl daemon-reload

# Acilista otomatik baslat + simdi calistir
sudo systemctl enable --now btc-bot.service
sudo systemctl enable --now btc-dashboard.service           # opsiyonel
```

## 5. Durum ve loglar

```bash
systemctl status btc-bot.service           # calisiyor mu
journalctl -u btc-bot.service -f           # canli log (Telegram + analiz dongusu)
journalctl -u btc-bot.service --since today
```

Ayrıca uygulama logu `~/btc_etf_bot/bot.log` (5 MB × 3 = döner), DB yedekleri
`~/btc_etf_bot/backups/` (her gece, son 7 gün).

## 6. Güncelleme / yeniden başlatma

```bash
# kod degistirdiysen:
sudo systemctl restart btc-bot.service
# durdur / baslat:
sudo systemctl stop btc-bot.service
sudo systemctl start btc-bot.service
```

## Dashboard'a erişim

- Aynı makinede tarayıcı: `http://127.0.0.1:8501`
- Uzaktan: **interneti açma.** VPN/Tailscale kur, servis adresini tailscale IP'ne
  çevir (örn. `--server.address 100.x.y.z`) ve oradan bağlan.

## Notlar

- Bot 7/24 BTC verisini Binance'tan çeker; ETF fiyatlarını Alpaca'dan (yoksa yfinance).
- Makine saatinin doğru olması önemli (analiz 5dk sınırlarına hizalı). `timedatectl`
  ile NTP'nin açık olduğundan emin ol.
- WAL modu açık olduğu için worker yazarken dashboard güvenle okuyabilir.
