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
from openai import AsyncOpenAI

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


# ─── OpenAI Initialization ──────────────────────────────────────────────────
client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ─── Pydantic Models ─────────────────────────────────────────────────────────
class AIChatRequest(BaseModel):
    message: str
    userId: Optional[str] = None
    language: Optional[str] = 'en'

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


class TokenRegistrationRequest(BaseModel):
    userId: str
    token: str


class ChatMessageRequest(BaseModel):
    receiverId: str
    senderId: str
    senderName: str
    message: str
    chatId: Optional[str] = None


# ─── Helper: Fetch all Notification Tokens ─────────────────────────────────
async def get_all_notification_tokens() -> dict[str, list[str]]:
    """Reads all users and returns ONLY valid FCM tokens (not Expo tokens)."""
    loop = asyncio.get_event_loop()
    
    def _fetch():
        users_ref = db.collection('users')
        docs = users_ref.stream()
        fcm_tokens = []
        skipped = 0
        for doc in docs:
            data = doc.to_dict()
            # Prefer fcmToken; fall back to expoPushToken only if it is NOT an Expo format
            fcm = data.get('fcmToken', '')
            if fcm and isinstance(fcm, str) and len(fcm) > 10 and not fcm.startswith('ExponentPushToken'):
                fcm_tokens.append(fcm)
            else:
                skipped += 1
        unique = list(set(fcm_tokens))
        logger.info(f"[FCM] Found {len(unique)} valid FCM tokens ({skipped} users had no/invalid FCM token)")
        return {"fcm": unique, "expo": []}
    
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
            data = doc.to_dict()
            # Check both 'phone' and 'phoneNumber' fields
            p = data.get('phone') or data.get('phoneNumber') or ''
            if p and isinstance(p, str):
                # Remove non-numeric characters (+91, spaces, dashes)
                clean_p = "".join(filter(str.isdigit, p))
                if len(clean_p) >= 10:
                    # Take last 10 digits for Indian standard
                    final_p = clean_p[-10:]
                    phone_numbers.append(final_p)
                else:
                    logger.warning(f"[SMS] Skipping invalid number: {p}")
        unique = list(set(phone_numbers))
        logger.info(f"[SMS] Found {len(unique)} valid phone numbers")
        return unique
    
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
                            notification_priority='PRIORITY_MAX'
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


# ─── Helper: Send Expo Push Notifications ─────────────────────────────────────
async def send_expo_broadcast(tokens: list[str], title: str, body: str, data: dict) -> dict:
    """Sends push notifications via Expo's Push API."""
    if not tokens:
        return {"success_count": 0, "failure_count": 0}
    
    expo_url = "https://exp.host/--/api/v2/push/send"
    messages = []
    for token in tokens:
        messages.append({
            "to": token,
            "title": title,
            "body": body,
            "data": data,
            "sound": "default",
            "priority": "high",
            "channelId": "emergency-alerts"
        })
    
    success_count = 0
    failure_count = 0
    try:
        async with httpx.AsyncClient() as client:
            # Chunk Expo messages (Expo recommends 100 max per request)
            for i in range(0, len(messages), 100):
                batch = messages[i:i+100]
                response = await client.post(expo_url, json=batch, timeout=30.0)
                if response.status_code == 200:
                    res_data = response.json()
                    for item in res_data.get("data", []):
                        if item.get("status") == "ok":
                            success_count += 1
                        else:
                            failure_count += 1
                else:
                    failure_count += len(batch)
            
            logger.info(f"[Expo] Delivered {success_count} notifications")
    except Exception as e:
        logger.error(f"[Expo] Error: {e}")
        failure_count = len(tokens) - success_count
        
    return {"success_count": success_count, "failure_count": failure_count}


# ─── Helper: Send SMS via Fast2SMS ────────────────────────────────────────────
async def send_sms_broadcast(numbers: list[str], message_text: str) -> dict:
    """Sends SMS to all numbers in a single bulk request. Respects SMS_ENABLED toggle."""
    sms_enabled = os.getenv("SMS_ENABLED", "false").lower() == "true"
    
    if not sms_enabled:
        logger.info(f"[SMS] SMS_ENABLED is false. Skipping actual send for {len(numbers)} numbers.")
        return {"success": True, "count": len(numbers), "mode": "simulated"}

    api_key = os.getenv("FAST2SMS_API_KEY", "").strip()
    if not api_key:
        return {"success": False, "error": "API Key missing"}

    if not numbers:
        return {"success": True, "count": 0}

    # Ensure message fits character limits for 'q' route (160 characters)
    final_message = message_text
    if len(final_message) > 160:
        final_message = final_message[:157] + "..."

    sms_url = "https://www.fast2sms.com/dev/bulkV2"
    payload = {
        "route": "q",
        "message": final_message,
        "language": "english",
        "numbers": ",".join(numbers)
    }
    logger.info(f"==> [SMS DEBUG] Sending to numbers: {payload['numbers']}")
    
    
    headers = {
        "authorization": api_key,
        "Content-Type": "application/x-www-form-urlencoded"
    }

    try:
        def _call_api():
            return sync_requests.post(sms_url, data=payload, headers=headers, timeout=20)
        
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, _call_api)
        data = response.json()
        
        logger.info(f"==> [SMS DEBUG] Fast2SMS Status: {response.status_code}")
        logger.info(f"==> [SMS DEBUG] Fast2SMS Response JSON: {data}")
        
        if response.status_code == 200 and data.get("return"):
            return {"success": True, "count": len(numbers)}
        return {"success": False, "error": data.get("message", "Fast2SMS Error")}
    except Exception as e:
        logger.error(f"[SMS] Bulk request failed: {e}")
        return {"success": False, "error": str(e)}



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


@api_router.post("/ai/chat")
async def ai_chat(req: AIChatRequest):
    """
    Intelligent AI Assistant for Raksha Alert.
    Provides safety guidance, reporting help, and app navigation.
    """
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(status_code=500, detail="AI Service configuration missing.")

    system_prompt = (
        "You are the Raksha Alert AI Assistant, an intelligent safety companion for the 'Raksha Alert' emergency system.\n\n"
        "BEHAVIOR RULES:\n"
        "- Be clear, short, and helpful.\n"
        "- Use simple language.\n"
        "- Always prioritize user safety.\n"
        "- If the user says they are in danger/emergency (e.g., 'help me', 'danger'), IMMEDIATELY respond: "
        "'Please press the SOS button immediately or contact local emergency services.'\n"
        "- For reporting: Go to Report -> Fill details -> Submit.\n"
        "- For sightings: Go to Report Sighting -> Enter details -> Submit.\n"
        "- Never give harmful advice.\n\n"
        "APP FEATURES:\n"
        "- Report Incident, Report Sighting, Real-time Alerts, Chat with Admin, SOS Button, Map View, Admin Dashboard.\n\n"
        f"The user's preferred language is {req.language}. Please respond in that language."
    )

    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": req.message},
            ],
            temperature=0.7,
            max_tokens=500
        )
        reply = response.choices[0].message.content
        return {"reply": reply}
    except Exception as e:
        logger.error(f"[AI] Chat Error: {e}")
        raise HTTPException(status_code=500, detail="AI Assistant is currently unavailable.")


@api_router.post("/ai/transcribe")
async def ai_transcribe(file: UploadFile = File(...)):
    """
    Transcribe audio using OpenAI Whisper.
    """
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(status_code=500, detail="AI Service configuration missing.")

    try:
        # Save temporary file
        temp_path = f"uploads/temp_{uuid.uuid4()}.m4a"
        with open(temp_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        # Transcribe
        with open(temp_path, "rb") as audio_file:
            transcript = await client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file
            )
        
        # Cleanup
        os.remove(temp_path)
        
        return {"text": transcript.text}
    except Exception as e:
        logger.error(f"[AI] Transcription Error: {e}")
        raise HTTPException(status_code=500, detail="Voice transcription failed.")


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
    
    # 1. Fetch ALL tokens (Segregated)
    tokens_seg = await get_all_notification_tokens()
    fcm_tokens = tokens_seg["fcm"]
    expo_tokens = tokens_seg["expo"]
    
    logger.info(f"[Notify] tokens: FCM({len(fcm_tokens)}) | Expo({len(expo_tokens)})")
    
    # 2. Build notification payload
    alert_type_label = alert.alertType.replace('_', ' ').title()
    push_title = f"🚨 {alert_type_label}: {alert.title}"
    push_body = f"{alert.description[:100]}... Tap to view."
    
    data_payload = {
        "alertId": alert.alertId,
        "alert_id": alert.alertId, # Backward compatibility & frontend preference
        "alertType": alert.alertType,
        "location": alert.location,
        "screen": "alert_detail",
    }
    
    # 3. Send FCM broadcast (FCM ONLY — Expo Push removed per user request)
    fcm_result = {}
    if fcm_tokens:
        fcm_result = await send_fcm_broadcast(fcm_tokens, push_title, push_body, data_payload)
    else:
        logger.warning("[Notify] No FCM tokens found. No push notifications sent.")
    
    # 4. Send Quick SMS (Fast2SMS 'q' route)
    sms_numbers = await get_all_phone_numbers()
    sms_text = "RAKSHA ALERT: Emergency reported nearby. Please check the Raksha app for details."
    sms_result = await send_sms_broadcast(sms_numbers, sms_text)
    
    total_success = fcm_result.get("success_count", 0)
    total_failure = fcm_result.get("failure_count", 0)
    
    combined_result = {
        "success_count": total_success,
        "failure_count": total_failure,
        "fcm": fcm_result,
    }
    
    # 5. Log to Firestore
    await log_notification(alert.alertId, push_title, push_body, len(fcm_tokens), combined_result)
    
    logger.info(f"[Notify] Broadcast done. FCM={total_success}/{len(fcm_tokens)} sent. SMS={sms_result.get('count', 0)} sent.")
    
    return {
        "success": True,
        "targetedCount": len(fcm_tokens),
        "results": {"fcm": fcm_result},
        "sms": sms_result,
        "diagnostics": {
            "alertId": alert.alertId,
            "fcm_success": total_success,
            "sms_success": sms_result.get("success", False)
        },
        "message": "FCM + SMS notifications broadcasted successfully."
    }


@api_router.post("/notifications/chat-message")
async def send_chat_notification(data: ChatMessageRequest):
    """Send FCM notification for a chat message to a specific user."""
    loop = asyncio.get_event_loop()
    
    def _get_token():
        docs = db.collection('users').where('uid', '==', data.receiverId).limit(1).stream()
        for doc in docs:
            d = doc.to_dict()
            return d.get('fcmToken') or d.get('expoPushToken')
        return None
    
    token = await loop.run_in_executor(None, _get_token)
    
    if not token:
        return {"success": False, "error": "Receiver FCM token not found"}
    
    try:
        result = await send_fcm_broadcast(
            [token],
            title=f"💬 New message from {data.senderName}",
            body=data.message[:200],
            data={
                "screen": "chat", 
                "chatId": data.chatId or "",
                "senderId": data.senderId
            }
        )
        return {"success": True, "delivery": {"push": result["success_count"] > 0}}

    except Exception as e:
        return {"success": False, "error": str(e)}


@api_router.post("/register-token")
async def register_token(data: TokenRegistrationRequest):
    """Saves or updates an FCM token for a specific user in Firestore."""
    loop = asyncio.get_event_loop()
    
    def _save():
        user_ref = db.collection('users').document(data.userId)
        user_ref.set({
            'fcmToken': data.token,
            'lastTokenUpdate': datetime.now(timezone.utc).isoformat()
        }, merge=True)
        logger.info(f"[FCM] Registered token for user {data.userId}")
        return True
        
    try:
        await loop.run_in_executor(None, _save)
        return {"success": True, "message": "Token registered successfully"}
    except Exception as e:
        logger.error(f"[FCM] Token registration failed for {data.userId}: {e}")
        return {"success": False, "error": str(e)}


@api_router.get("/stats")
async def get_stats():
    """Get system stats from Firestore."""
    loop = asyncio.get_event_loop()
    
    def _fetch():
        # Using list() on stream() is simple for small to medium sets
        users_count = len(list(db.collection('users').stream()))
        alerts_count = len(list(db.collection('alerts').stream()))
        sos_count = len(list(db.collection('sos_alerts').stream()))
        sighting_count = len(list(db.collection('sighting_reports').stream()))
        incident_count = len(list(db.collection('incident_reports').stream()))
        
        return {
            "totalUsers": users_count,
            "totalAlerts": alerts_count,
            "sosAlerts": sos_count,
            "sightingReports": sighting_count,
            "incidentReports": incident_count,
        }
    
    return await loop.run_in_executor(None, _fetch)


@api_router.get("/alerts")
async def get_alerts():
    """Get recent alerts from Firestore."""
    loop = asyncio.get_event_loop()
    
    def _fetch():
        # Fetch last 50 alerts ordered by creation time
        alerts_ref = db.collection('alerts').order_by('createdAt', direction=firestore.Query.DESCENDING).limit(50)
        docs = alerts_ref.stream()
        result = []
        for doc in docs:
            data = doc.to_dict()
            data['id'] = doc.id
            # Convert timestamp to string for JSON serialization
            if 'createdAt' in data and data['createdAt']:
                try:
                    if hasattr(data['createdAt'], 'isoformat'):
                        data['createdAt'] = data['createdAt'].isoformat()
                    else:
                        data['createdAt'] = str(data['createdAt'])
                except: 
                    data['createdAt'] = str(data['createdAt'])
            result.append(data)
        return result
    
    return await loop.run_in_executor(None, _fetch)


@api_router.get("/users/locations")
async def get_user_locations():
    """Get all user locations and their current status for map markers."""
    loop = asyncio.get_event_loop()
    
    def _fetch():
        users_ref = db.collection('users')
        users_docs = users_ref.stream()
        
        # Get active alerts to determine SOS status
        active_alerts = db.collection('alerts').where('status', '!=', 'resolved').stream()
        active_alert_creators = {a.to_dict().get('createdBy') for a in active_alerts if a.to_dict().get('alertType') == 'SOS_EMERGENCY'}
        
        # Get incident reports to determine Incident status
        active_incidents = db.collection('incident_reports').where('status', 'not-in', ['resolved', 'rejected']).stream()
        incident_creators = {i.to_dict().get('uid') or i.to_dict().get('userId') for i in active_incidents}
        
        results = []
        for doc in users_docs:
            data = doc.to_dict()
            if 'latitude' in data and 'longitude' in data:
                uid = doc.id
                status = 'normal'
                if uid in active_alert_creators:
                    status = 'sos'
                elif uid in incident_creators:
                    status = 'incident'
                
                results.append({
                    "id": uid,
                    "name": data.get('name', 'Unknown'),
                    "latitude": data.get('latitude'),
                    "longitude": data.get('longitude'),
                    "status": status,
                    "lastActive": data.get('lastActive').isoformat() if data.get('lastActive') and hasattr(data.get('lastActive'), 'isoformat') else None
                })
        return results
    
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
@app.get("/")
def root():
    return {"message": "Raksha backend running"}

@app.get("/health")
def health():
    return {"status": "ok"}

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
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("server:app", host="0.0.0.0", port=port)
