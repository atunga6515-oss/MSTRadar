# Linux'ta 7/24 Çalıştırma (systemd)

Bu rehber botu Linux makinende `systemd` ile kurar: makine açıldığında otomatik
başlar, çökerse kendini yeniden başlatır, logları `journalctl`'de tutulur.

## 1. Projeyi indir (git clone)

GitHub'dan klonla — klasör adı repo adıyla **`MSTRadar`** olur:

```bash
cd ~
git clone https://github.com/atunga6515-oss/MSTRadar.git
cd MSTRadar
```

> Servis dosyaları `~/MSTRadar` yolunu varsayar. Başka bir yere klonladıysan
> `deploy/*.service` içindeki `MSTRadar` kısımlarını güncelle.

## 2. Sanal ortam + bağımlılıklar

```bash
cd ~/MSTRadar
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 3. `.env` oluştur (repoda YOK — gizli)

`.env` git'e gitmez, o yüzden klonlama sonrası elle oluşturman gerekir:

```bash
cp .env.example .env
nano .env   # TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ALPACA_API_KEY, ALPACA_API_SECRET doldur
```

Tek seferlik elle test:
```bash
source venv/bin/activate && python bot.py
# Telegram'a "Bot Aktif" geldiyse Ctrl+C ile durdur, servise geç.
```

## 4. systemd KULLANICI servislerini kur (sudo/şifre gerektirmez)

Servisler **kullanıcı servisi** olarak kurulur: yönetimi `sudo` istemez, şifre
sormaz. Servis dosyaları `%h` (home dizini) kullandığı için, proje `~/MSTRadar`
altındaysa **dosyalarda hiçbir şey düzeltmen gerekmez.**

```bash
mkdir -p ~/.config/systemd/user
cp deploy/btc-bot.service ~/.config/systemd/user/
cp deploy/btc-dashboard.service ~/.config/systemd/user/   # dashboard istersen
systemctl --user daemon-reload

# Şimdi başlat + boot'ta otomatik başlasın
systemctl --user enable --now btc-bot.service
systemctl --user enable --now btc-dashboard.service        # opsiyonel
```

**Reboot'ta login olmadan da çalışsın diye (TEK SEFERLİK, sudo gerekir):**

```bash
sudo loginctl enable-linger $USER
```

> `enable-linger` olmazsa servisler sadece sen login olunca başlar. Bu tek komut
> sayesinde makine açılır açılmaz (sen giriş yapmadan) çalışırlar.

## 5. Durum ve loglar (sudo yok)

```bash
systemctl --user status btc-bot.service        # calisiyor mu
journalctl --user -u btc-bot.service -f        # canli log (Telegram + analiz dongusu)
journalctl --user -u btc-bot.service --since today
```

Ayrıca uygulama logu `~/MSTRadar/bot.log` (5 MB × 3 = döner), DB yedekleri
`~/MSTRadar/backups/` (her gece, son 7 gün).

## 6. Güncelleme / yeniden başlatma (sudo yok)

```bash
# kod degistirdiysen:
systemctl --user restart btc-bot.service
# durdur / baslat:
systemctl --user stop btc-bot.service
systemctl --user start btc-bot.service
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
