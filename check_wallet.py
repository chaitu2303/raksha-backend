
import requests
import os
from dotenv import load_dotenv

load_dotenv()

def check_wallet():
    api_key = os.getenv("FAST2SMS_API_KEY", "").strip()
    url = f"https://www.fast2sms.com/dev/wallet?authorization={api_key}"
    
    print(f"Checking Wallet for API Key: {api_key[:10]}...")
    try:
        response = requests.get(url, timeout=15)
        print(f"Status: {response.status_code}")
        print(f"Response: {response.text}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check_wallet()
