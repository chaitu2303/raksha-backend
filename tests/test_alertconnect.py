"""
AlertConnect Backend API Tests
Tests: admin/exists, users/register, notifications/send-alert, stats
"""
import pytest
import requests
import os

BASE_URL = os.environ.get('EXPO_PUBLIC_BACKEND_URL', os.environ.get('EXPO_BACKEND_URL', '')).rstrip('/')

TEST_UID = "TEST_uid_playwright_123"
TEST_USER = {
    "uid": TEST_UID,
    "name": "TEST_User Playwright",
    "email": "test_playwright@example.com",
    "phone": "9876543210",
    "role": "user",
    "expoPushToken": ""
}


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    yield s
    # Cleanup: we can't delete via API, but test data is prefixed with TEST_


class TestHealth:
    """Health check"""
    def test_root(self, session):
        resp = session.get(f"{BASE_URL}/api/")
        assert resp.status_code == 200
        data = resp.json()
        assert "message" in data
        print(f"Root: {data}")


class TestAdminExists:
    """Admin existence check"""
    def test_admin_exists_returns_bool(self, session):
        resp = session.get(f"{BASE_URL}/api/admin/exists")
        assert resp.status_code == 200
        data = resp.json()
        assert "exists" in data
        assert isinstance(data["exists"], bool)
        print(f"Admin exists: {data['exists']}")


class TestUserRegister:
    """User registration endpoint"""
    def test_register_user(self, session):
        resp = session.post(f"{BASE_URL}/api/users/register", json=TEST_USER)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("success") is True
        print(f"Register response: {data}")

    def test_register_user_update(self, session):
        # Re-register same user should update (not duplicate)
        updated = {**TEST_USER, "name": "TEST_User Updated"}
        resp = session.post(f"{BASE_URL}/api/users/register", json=updated)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("success") is True
        print(f"Update response: {data}")

    def test_register_admin_role(self, session):
        admin_user = {
            "uid": "TEST_admin_uid_999",
            "name": "TEST_Admin User",
            "email": "test_admin_x@example.com",
            "phone": "9999999999",
            "role": "admin",
            "expoPushToken": ""
        }
        resp = session.post(f"{BASE_URL}/api/users/register", json=admin_user)
        assert resp.status_code == 200
        assert resp.json().get("success") is True


class TestUpdatePushToken:
    """Update push token"""
    def test_update_push_token(self, session):
        resp = session.put(f"{BASE_URL}/api/users/push-token", json={
            "uid": TEST_UID,
            "expoPushToken": "ExponentPushToken[test123]"
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("success") is True
        print(f"Push token update: {data}")


class TestStats:
    """Stats endpoint"""
    def test_stats(self, session):
        resp = session.get(f"{BASE_URL}/api/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "totalUsers" in data
        assert "totalAdmins" in data
        assert isinstance(data["totalUsers"], int)
        print(f"Stats: {data}")


class TestSendAlert:
    """Notification send-alert endpoint"""
    def test_send_alert(self, session):
        payload = {
            "alertId": "TEST_alert_001",
            "title": "TEST Alert - Playwright Test",
            "description": "This is a test alert",
            "location": "Test Location",
            "alertType": "GENERAL_ALERT"
        }
        resp = session.post(f"{BASE_URL}/api/notifications/send-alert", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("success") is True
        assert "results" in data
        assert "totalUsers" in data
        print(f"Send alert response: {data}")
