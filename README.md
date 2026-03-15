# Raksha Alert Backend Setup Guide

## What Changed
The backend now uses **Firebase Admin SDK** with **FCM HTTP v1 API** to send push notifications to ALL registered users — no more distance filtering.

## Quick Start

### 1. Get Firebase Service Account Key
1. Go to [Firebase Console](https://console.firebase.google.com)
2. Select your project `raksha-alert-48df4`
3. Go to **Project Settings** → **Service accounts**
4. Click **"Generate new private key"** → Download JSON file
5. Rename it to `serviceAccount.json` and place it in the `backend/` folder

### 2. Install Dependencies
```bash
cd backend
pip install -r requirements.txt
```

### 3. Start the Server
```bash
uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

## Firestore Security Rules
**Paste these into Firebase Console → Firestore → Rules → Publish:**

```
rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {

    function isAuthenticated() {
      return request.auth != null;
    }

    function isAdmin() {
      return isAuthenticated() &&
        get(/databases/$(database)/documents/users/$(request.auth.uid)).data.role == 'admin';
    }

    match /users/{userId} {
      allow read: if isAuthenticated();
      allow create, update: if isAuthenticated() && request.auth.uid == userId;
      allow delete: if isAdmin();
    }

    match /alerts/{alertId} {
      allow read: if isAuthenticated();
      allow create, update: if isAuthenticated();
      allow delete: if isAdmin();
    }

    match /sighting_reports/{reportId} {
      allow read: if isAuthenticated();
      allow create: if isAuthenticated();
      allow update, delete: if isAdmin();
    }

    match /incident_reports/{reportId} {
      allow read: if isAuthenticated();
      allow create: if isAuthenticated();
      allow update, delete: if isAdmin();
    }

    match /notifications_log/{logId} {
      allow read: if isAdmin();
      allow create: if isAuthenticated();
      allow update, delete: if isAdmin();
    }
  }
}
```

## API Endpoints
- `GET /api/` — Health check
- `POST /api/notifications/send-alert` — Broadcast FCM to ALL users
- `POST /api/notifications/chat-message` — Send FCM to specific user
- `GET /api/stats` — Get system statistics
