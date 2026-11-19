"""
Locust load-testing plan for the Rule-Based AI Service Booking Engine.

Measures:
  - Requests per second (RPS)
  - P50 / P95 / P99 response times
  - Error rate
  - Multi-turn conversational search & booking contention

Usage:
  1. Start main_backend on :8000 and ai_booking_service on :8001
  2. Run:
       .\.venv\Scripts\locust.exe -f load_tests\locustfile.py --host http://127.0.0.1:8000
  3. Open http://localhost:8089 to configure virtual users & spawn rate, or run headless:
       .\.venv\Scripts\locust.exe -f load_tests\locustfile.py --host http://127.0.0.1:8000 --headless -u 50 -r 10 -t 60s
"""

import random
import uuid

from locust import HttpUser, between, task

SAMPLE_QUERIES = [
    "Find the cheapest car wash near Aluva tomorrow at 2 PM",
    "Find a plumber near Kochi tomorrow at 10 AM",
    "I need AC repair near Aluva tomorrow at 11 AM",
    "Book an electrician near Kakkanad tomorrow at 4 PM",
    "Find available home cleaning near Edappally tomorrow at 9 AM",
]


class BookingCustomerUser(HttpUser):
    wait_time = between(0.5, 2.0)

    def on_start(self):
        email = f"loaduser_{uuid.uuid4().hex[:10]}@example.com"
        resp = self.client.post(
            "/api/v1/auth/register",
            json={
                "email": email,
                "full_name": "Load Test Customer",
                "password": "Password123!",
            },
            name="/api/v1/auth/register",
        )
        if resp.status_code in (200, 201):
            self.token = resp.json()["access_token"]
            self.headers = {"Authorization": f"Bearer {self.token}"}
        else:
            self.token = None
            self.headers = {}

    @task(4)
    def search_and_rank_conversation(self):
        if not self.token:
            return
        query = random.choice(SAMPLE_QUERIES)
        self.client.post(
            "/api/v1/chat/message",
            headers=self.headers,
            json={"message": query},
            name="/api/v1/chat/message [search]",
        )

    @task(2)
    def multi_turn_search_select_and_confirm_contention(self):
        """Tests booking contention when many users target the same popular slots."""
        if not self.token:
            return
        r1 = self.client.post(
            "/api/v1/chat/message",
            headers=self.headers,
            json={"message": "Find the cheapest car wash near Aluva tomorrow at 2 PM"},
            name="/api/v1/chat/message [turn1]",
        )
        if r1.status_code != 200:
            return
        body1 = r1.json()
        conv_id = body1.get("conversation_id")
        if not conv_id or body1.get("status") != "OPTIONS_FOUND":
            return

        r2 = self.client.post(
            "/api/v1/chat/message",
            headers=self.headers,
            json={"message": "Book that one", "conversation_id": conv_id},
            name="/api/v1/chat/message [select]",
        )
        if r2.status_code != 200 or not r2.json().get("requires_confirmation"):
            return

        with self.client.post(
            "/api/v1/chat/confirm",
            headers=self.headers,
            json={"conversation_id": conv_id, "confirm": True},
            name="/api/v1/chat/confirm",
            catch_response=True,
        ) as r3:
            # Under contention, 200 with SLOT_UNAVAILABLE is an expected safe conflict, not a server crash
            if r3.status_code == 200:
                r3.success()
            else:
                r3.failure(f"Unexpected status {r3.status_code}")

    @task(1)
    def check_booking_status(self):
        if not self.token:
            return
        self.client.post(
            "/api/v1/chat/message",
            headers=self.headers,
            json={"message": "Show my booking status"},
            name="/api/v1/chat/message [status]",
        )
