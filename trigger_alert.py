
import requests
import json

url = "http://localhost:8000/api/notifications/send-alert"
payload = {
    "alertId": "debug-trigger",
    "title": "Debug SMS Alert",
    "description": "This is a system test to verify SMS delivery logs.",
    "location": "System Lab",
    "alertType": "GENERAL_ALERT",
    "latitude": 17.7,
    "longitude": 83.3
}

print(f"Triggering {url}...")
try:
    response = requests.post(url, json=payload, timeout=25)
    print(f"Status: {response.status_code}")
    print(f"Response: {response.text}")
except Exception as e:
    print(f"Error: {e}")
