
import asyncio
import httpx
import os
from dotenv import load_dotenv

load_dotenv()

async def test_sms():
    api_key = os.getenv("FAST2SMS_API_KEY")
    url = "https://www.fast2sms.com/dev/bulkV2"
    
    # Test number (Chaitanya)
    numbers = "8309869017" 
    message = "RAKSHA ALERT: Emergency triggered! Please check the app immediately."
    
    payload = {
        "route": "q",
        "message": message,
        "language": "english",
        "numbers": numbers
    }
    
    headers = {
        "authorization": api_key,
        "Content-Type": "application/x-www-form-urlencoded"
    }
    
    print(f"Testing Fast2SMS with API Key: {api_key[:10]}...")
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, headers=headers, data=payload, timeout=20.0)
            print(f"Status: {response.status_code}")
            print(f"Response: {response.text}")
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(test_sms())
