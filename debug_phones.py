
import firebase_admin
from firebase_admin import credentials, firestore
from pathlib import Path

ROOT_DIR = Path(__file__).parent
SERVICE_ACCOUNT_PATH = ROOT_DIR / 'serviceAccount.json'

if not firebase_admin._apps:
    cred = credentials.Certificate(str(SERVICE_ACCOUNT_PATH))
    firebase_admin.initialize_app(cred)

db = firestore.client()

def check_phone_numbers():
    users_ref = db.collection('users')
    docs = users_ref.stream()
    print("--- Database Phone Check ---")
    for doc in docs:
        data = doc.to_dict()
        p = data.get('phone')
        name = data.get('name', 'Unknown')
        
        if p and isinstance(p, str):
            clean_p = "".join(filter(str.isdigit, p))
            if len(clean_p) >= 10:
                final_p = clean_p[-10:]
                print(f"User: {name} | Raw: {p} | Clean: {clean_p} | Final (last 10): {final_p} | VALID")
            else:
                print(f"User: {name} | Raw: {p} | Clean: {clean_p} | INVALID (too short)")
        else:
            print(f"User: {name} | Raw: {p} | INVALID (empty/wrong type)")

if __name__ == "__main__":
    check_phone_numbers()
