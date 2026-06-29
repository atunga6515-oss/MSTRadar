import requests

def get_realtime_btc():
    # Robinhood'un kendi iç kripto veri adresi ve Bitcoin'in sistem ID'si
    btc_id = "3d961844-d360-45fc-989b-f6fca761d511"
    url = f"https://api.robinhood.com/marketdata/forex/quotes/{btc_id}/"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }
    
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        data = response.json()
        
        # Kriptolarda 7/24 piyasa açık olduğu için ana fiyat "mark_price" ile gösterilir
        current_price = data.get("mark_price")
        print(f"Bitcoin (BTC) Anlık Fiyat: ${current_price}")
        
        # Ekstra veriler (İsteğe bağlı)
        print(f"Alış (Bid): ${data.get('bid_price')}")
        print(f"Satış (Ask): ${data.get('ask_price')}")
        
    else:
        print("API'ye ulaşılamadı. Hata:", response.status_code)

if __name__ == "__main__":
    get_realtime_btc()
