from fastapi import FastAPI, APIRouter, UploadFile, File, HTTPException
from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import os
import shutil
import uuid
import logging
import asyncio
from pathlib import Path
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone
import httpx
import requests as sync_requests

# Firebase Admin SDK
import firebase_admin
from firebase_admin import credentials, firestore, messaging, auth

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Firebase Admin Initialization ───────────────────────────────────────────
SERVICE_ACCOUNT_PATH = ROOT_DIR / 'serviceAccount.json'
if not firebase_admin._apps:
    if SERVICE_ACCOUNT_PATH.exists():
        cred = credentials.Certificate(str(SERVICE_ACCOUNT_PATH))
        firebase_admin.initialize_app(cred)
        logger.info("✅ Firebase Admin SDK initialized from serviceAccount.json")
    else:
        # Fallback: use env var GOOGLE_APPLICATION_CREDENTIALS
        firebase_admin.initialize_app()
        logger.info("✅ Firebase Admin SDK initialized from environment credentials")

db = firestore.client()

# ─── FastAPI App ──────────────────────────────────────────────────────────────
app = FastAPI(title="Raksha Alert API v2 – FCM Edition")

os.makedirs("uploads", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

api_router = APIRouter(prefix="/api")


# ─── Pydantic Models ─────────────────────────────────────────────────────────
class SendAlertRequest(BaseModel):
    alertId: str
    title: str
    description: str
    location: str
    alertType: str
    dateTime: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    # radius field kept for backward compat but IGNORED – all users notified
    radius: Optional[float] = None
    # Professional Template Fields (Optional)
    personName: Optional[str] = None
    age: Optional[str] = None
    gender: Optional[str] = None
    contactNumber: Optional[str] = None
    imageUrl: Optional[str] = None


class ChatMessageRequest(BaseModel):
    receiverId: str
    senderName: str
    message: str


# ─── Helper: Fetch all FCM tokens from Firestore ─────────────────────────────
async def get_all_fcm_tokens() -> list[str]:
    """Reads all users from Firestore and returns their stored FCM tokens."""
    loop = asyncio.get_event_loop()
    
    def _fetch():
        users_ref = db.collection('users')
        docs = users_ref.stream()
        tokens = []
        for doc in docs:
            data = doc.to_dict()
            token = data.get('fcmToken')
            if token and isinstance(token, str) and len(token) > 10:
                tokens.append(token)
        return tokens
    
    return await loop.run_in_executor(None, _fetch)



# ─── Helper: Fetch all Phone numbers from Firestore ──────────────────────────
async def get_all_phone_numbers() -> list[str]:
    """Reads all users and returns valid 10-digit phone numbers."""
    loop = asyncio.get_event_loop()
    
    def _fetch():
        users_ref = db.collection('users')
        docs = users_ref.stream()
        phone_numbers = []
        for doc in docs:
            p = doc.to_dict().get('phone')
            if p and isinstance(p, str):
                # Remove non-numeric characters
                clean_p = "".join(filter(str.isdigit, p))
                if len(clean_p) >= 10:
                    # Take last 10 digits for Indian standard
                    final_p = clean_p[-10:]
                    phone_numbers.append(final_p)
                else:
                    logger.warning(f"[SMS] Skipping invalid number: {p}")
        return list(set(phone_numbers)) # Unique only
    
    return await loop.run_in_executor(None, _fetch)


# ─── Helper: Send FCM via Firebase Admin SDK ──────────────────────────────────
async def send_fcm_broadcast(tokens: list[str], title: str, body: str, data: dict) -> dict:
    """
    Sends FCM messages to all provided tokens in batches of 500.
    Uses Firebase Admin SDK (HTTP v1 API under the hood).
    """
    if not tokens:
        return {"success_count": 0, "failure_count": 0, "errors": ["No tokens available"]}
    
    loop = asyncio.get_event_loop()
    
    def _send():
        success_count = 0
        failure_count = 0
        errors = []
        
        # FCM supports up to 500 tokens per multicast message
        BATCH_SIZE = 500
        for i in range(0, len(tokens), BATCH_SIZE):
            batch_tokens = tokens[i:i + BATCH_SIZE]
            try:
                message = messaging.MulticastMessage(
                    tokens=batch_tokens,
                    notification=messaging.Notification(
                        title=title,
                        body=body,
                    ),
                    android=messaging.AndroidConfig(
                        priority='high',
                        notification=messaging.AndroidNotification(
                            channel_id='emergency-alerts',
                            sound='default',
                            default_vibrate_timings=True,
                        ),
                    ),
                    apns=messaging.APNSConfig(
                        payload=messaging.APNSPayload(
                            aps=messaging.Aps(
                                alert=messaging.ApsAlert(title=title, body=body),
                                sound='default',
                                badge=1,
                                content_available=True,
                            )
                        )
                    ),
                    data={str(k): str(v) for k, v in data.items()},
                )
                
                response = messaging.send_each_for_multicast(message)
                success_count += response.success_count
                failure_count += response.failure_count
                
                # Log individual failures
                for idx, resp in enumerate(response.responses):
                    if not resp.success and resp.exception:
                        errors.append(f"Token[{i+idx}]: {resp.exception}")
                        
            except Exception as e:
                failure_count += len(batch_tokens)
                errors.append(f"Batch error: {str(e)}")
                logger.error(f"[FCM] Batch {i//BATCH_SIZE} failed: {e}")
        
        return {"success_count": success_count, "failure_count": failure_count, "errors": errors}
    
    return await loop.run_in_executor(None, _send)


# ─── Helper: Send SMS via Fast2SMS ────────────────────────────────────────────
async def send_sms_broadcast(numbers: list[str], message_text: str) -> dict:
    """Sends SMS to all numbers in a single bulk request. Respects SMS_ENABLED toggle."""
    sms_enabled = os.getenv("SMS_ENABLED", "false").lower() == "true"
    
    if not sms_enabled:
        logger.info(f"[SMS] SMS_ENABLED is false. Skipping actual send for {len(numbers)} numbers.")
        # We still write to debug log so the user can see what WOULD have been sent
        with open(ROOT_DIR / "sms_debug.log", "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now()}] [TEST-MODE] To: {','.join(numbers)}\n")
            f.write(f"Message: {message_text}\n")
            f.write("-" * 50 + "\n")
        return {"success": True, "count": len(numbers), "mode": "simulated"}

    api_key = os.getenv("FAST2SMS_API_KEY", "").strip()
    if not api_key:
        logger.error("[SMS] FAST2SMS_API_KEY not found in .env")
        return {"success": False, "error": "API Key missing"}

    if not numbers:
        return {"success": True, "count": 0}

    url = "https://www.fast2sms.com/dev/bulkV2"
    
    # TRANSACTIONAL/QUICK ROUTES (q) ARE VERY STRICT
    # We must use plain text, NO emojis, and keep it under 160 characters
    # Note: message_text is the professional template, but 'q' route usually rejects long texts.
    # We will use a concise format for the 'q' route but include the deep link if possible.
    concise_message = f"RAKSHA ALERT: Emergency! Details: raksha://alert/ID. Check app now."
    # If the alert id is known, we should ideally swap ID
    # But for bulk 'q' route, Fast2SMS usually requires the SAME message for all.
    # We'll use the provided message_text if it's within limits, else fallback.
    
    final_message = message_text if len(message_text) <= 160 else concise_message

    payload = {
        "route": "q",
        "message": final_message,
        "language": "english",
        "numbers": ",".join(numbers)
    }
    
    headers = {
        "authorization": api_key,
        "Content-Type": "application/x-www-form-urlencoded"
    }

    try:
        def _call_api():
            return sync_requests.post(url, data=payload, headers=headers, timeout=20)
        
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, _call_api)
        data = response.json()
        
        # Log to file for verification
        with open(ROOT_DIR / "sms_debug.log", "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now()}] To: {','.join(numbers)}\n")
            f.write(f"Status: {response.status_code} | Result: {data.get('return')}\n")
            f.write(f"JSON: {data}\n")
            f.write("-" * 50 + "\n")
        
        if response.status_code == 200 and data.get("return"):
            logger.info(f"[SMS] Fast2SMS Bulk Success: {data.get('request_id')}")
            return {"success": True, "count": len(numbers)}
        else:
            msg = data.get("message", "Unknown error")
            if isinstance(msg, list): msg = " ".join(msg)
            logger.error(f"[SMS] Fast2SMS Bulk Rejected: {msg}")
            return {"success": False, "error": msg}

    except Exception as e:
        logger.error(f"[SMS] Bulk request failed: {e}")
        return {"success": False, "error": str(e)}


# ─── Helper: Professional Template Generator ─────────────────────────────────
def generate_professional_template(alert: SendAlertRequest) -> str:
    """Formats an emergency alert using the official Raksha template."""
    now = datetime.now()
    timestamp_str = now.strftime("%d %B %Y | %I:%M %p")
    
    incident_label = alert.alertType.replace('_', ' ').title()
    
    # Coordinates link
    map_link = f"https://maps.google.com/?q={alert.latitude},{alert.longitude}" if alert.latitude and alert.longitude else "Location data unavailable"

    # Deep Link for App
    deep_link = f"raksha://alert/{alert.alertId}"

    # Template Construction
    message = f"RAKSHA ALERT\n\n"
    message += f"Type: {alert.alertType.replace('_', ' ').title()}\n\n"
    message += f"Location:\nhttps://maps.google.com/?q={alert.latitude},{alert.longitude}\n\n"
    message += f"Time:\n{alert.dateTime}\n\n"
    message += f"Description:\n{alert.description}\n\n"
    if alert.imageUrl:
        message += f"📷 Photo:\n{alert.imageUrl}\n\n"
        
    message += f"View alert:\n{deep_link}\n\n"
    message += f"Raksha Safety System"
    
    return message


# ─── Helper: Log notification to Firestore ────────────────────────────────────
async def log_notification(alert_id: str, title: str, body: str, user_count: int, result: dict):
    loop = asyncio.get_event_loop()
    
    def _log():
        db.collection('notifications_log').add({
            'alertId': alert_id,
            'title': title,
            'body': body,
            'sentUsersCount': user_count,
            'successCount': result.get('success_count', 0),
            'failureCount': result.get('failure_count', 0),
            'timestamp': firestore.SERVER_TIMESTAMP,
        })
    
    await loop.run_in_executor(None, _log)


# ─── Routes ──────────────────────────────────────────────────────────────────
@api_router.get("/")
async def root():
    return {"message": "Raksha Alert API v2 – FCM Edition", "status": "running"}


@api_router.get("/health")
async def health():
    return {"status": "Raksha backend running"}


@api_router.post("/notifications/send-alert")
async def send_alert_notifications(alert: SendAlertRequest):
    """
    Broadcasts an FCM push notification to ALL registered users.
    ⚠️ The 'radius' field is accepted but IGNORED — all users receive the alert.
    """
    logger.info(f"[Notify] Broadcast triggered for alert: {alert.alertId} | Type: {alert.alertType}")
    
    # 1. Fetch ALL FCM tokens (no radius/location filter)
    tokens = await get_all_fcm_tokens()
    total_tokens = len(tokens)
    logger.info(f"[Notify] Found {total_tokens} FCM tokens to broadcast to")
    
    if total_tokens == 0:
        logger.warning("[Notify] No FCM tokens found. Users may not have registered their device yet.")
        return {
            "success": True,
            "message": "No registered device tokens found. Ensure users have opened the app at least once.",
            "targetedCount": 0,
            "results": {"success_count": 0, "failure_count": 0, "errors": []},
        }
    
    # 2. Build notification payload
    alert_type_label = alert.alertType.replace('_', ' ').title()
    
    # Professional Template for SMS
    structured_sms_text = generate_professional_template(alert)
    
    # Concise Template for Push (Standard Notification Tray behavior)
    push_title = f"🚨 {alert_type_label}: {alert.title}"
    push_body = f"{alert.description[:60]}... Tap to view map & photo."
    
    data_payload = {
        "alertId": alert.alertId,
        "alertType": alert.alertType,
        "location": alert.location,
        "screen": "alert_detail",  # used by app to navigate
    }
    
    # 3. Send FCM multicast
    result = await send_fcm_broadcast(tokens, push_title, push_body, data_payload)
    logger.info(f"[Notify] FCM result: ✅ {result['success_count']} sent, ❌ {result['failure_count']} failed")
    
    # 3b. Send SMS Fallback
    sms_numbers = await get_all_phone_numbers()
    sms_result = await send_sms_broadcast(sms_numbers, structured_sms_text)
    
    # 4. Log to Firestore
    await log_notification(alert.alertId, push_title, structured_sms_text, total_tokens, result)
    
    return {
        "success": True,
        "targetedCount": total_tokens,
        "results": result,
        "sms": sms_result,
        "message": f"Broadcast sent to {result['success_count']}/{total_tokens} devices via FCM and SMS fallback triggered for {len(sms_numbers)} users."
    }


@api_router.post("/notifications/chat-message")
async def send_chat_notification(data: ChatMessageRequest):
    """Send FCM notification for a chat message to a specific user."""
    loop = asyncio.get_event_loop()
    
    def _get_token():
        docs = db.collection('users').where('uid', '==', data.receiverId).limit(1).stream()
        for doc in docs:
            return doc.to_dict().get('fcmToken')
        return None
    
    token = await loop.run_in_executor(None, _get_token)
    
    if not token:
        return {"success": False, "error": "Receiver FCM token not found"}
    
    try:
        result = await send_fcm_broadcast(
            [token],
            title=f"💬 New message from {data.senderName}",
            body=data.message[:200],
            data={"screen": "chat", "senderId": data.receiverId}
        )
        return {"success": True, "delivery": {"push": result["success_count"] > 0}}
    except Exception as e:
        return {"success": False, "error": str(e)}


@api_router.get("/stats")
async def get_stats():
    """Get system stats from Firestore."""
    loop = asyncio.get_event_loop()
    
    def _fetch():
        users = db.collection('users').stream()
        alerts = db.collection('alerts').stream()
        
        user_list = list(users)
        alert_list = list(alerts)
        sos_count = sum(1 for a in alert_list if a.to_dict().get('alertType') == 'SOS_EMERGENCY')
        
        return {
            "totalUsers": len(user_list),
            "totalAlerts": len(alert_list),
            "sosAlerts": sos_count,
        }
    
    return await loop.run_in_executor(None, _fetch)


@api_router.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    ext = file.filename.split('.')[-1]
    filename = f"{uuid.uuid4()}.{ext}"
    file_path = f"uploads/{filename}"
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    return {"url": f"/uploads/{filename}"}


@api_router.delete("/admin/delete-user/{uid}")
async def delete_user(uid: str, admin_uid: str):
    """
    Administrative endpoint to delete a user from both Auth and Firestore.
    Requires admin_uid for rudimentary verification.
    """
    loop = asyncio.get_event_loop()
    
    def _delete():
        # 1. Verify caller is admin
        admin_doc = db.collection('users').document(admin_uid).get()
        if not admin_doc.exists or admin_doc.to_dict().get('role') != 'admin':
            raise Exception("Unauthorized: Admin privilege required")
            
        # 2. Delete from Auth
        try:
            auth.delete_user(uid)
        except Exception as e:
            logger.warning(f"[Admin] User {uid} not found in Auth or already deleted: {e}")
            
        # 3. Delete from Firestore
        db.collection('users').document(uid).delete()
        
        # 4. Cleanup related data (Alerts/Reports)
        # We don't delete alerts as they are public data, but we could scrub metadata
        logger.info(f"[Admin] User {uid} successfully removed by Admin {admin_uid}")
        return True

    try:
        await loop.run_in_executor(None, _delete)
        return {"success": True, "message": "User deleted successfully"}
    except Exception as e:
        logger.error(f"[Admin] Deletion failed: {e}")
        return {"success": False, "error": str(e)}


# ─── App Setup ────────────────────────────────────────────────────────────────
app.include_router(api_router)
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
