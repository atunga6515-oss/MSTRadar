import requests

def get_robinhood_big_price(ticker="MU"):
    url = f"https://api.robinhood.com/quotes/{ticker}/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }
    
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        data = response.json()
        
        # Piyasa sonrası/öncesi fiyat (Varsa bu gösterilir)
        extended_price = data.get("last_extended_hours_trade_price")
        
        # Normal piyasa fiyatı
        regular_price = data.get("last_trade_price")
        
        # Eğer uzatılmış saatlerde işlem gördüyse (değer None değilse) o fiyatı al, 
        # yoksa normal fiyatı al. Robinhood web sitesi ana rakamı tam olarak böyle seçer.
        if extended_price:
            current_main_price = extended_price
            print(f"{ticker} Güncel Fiyat (After-Hours): ${current_main_price}")
        else:
            current_main_price = regular_price
            print(f"{ticker} Güncel Fiyat (Market): ${current_main_price}")
            
    else:
        print("API'ye ulaşılamadı. Hata:", response.status_code)

if __name__ == "__main__":
    get_robinhood_big_price()
