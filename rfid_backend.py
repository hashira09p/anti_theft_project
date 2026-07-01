"""
=============================================================
  RFID Smart Home Security — Python AI Backend (Fixed)
=============================================================
  Install dependencies:
    pip install "paho-mqtt>=2.0" supabase scikit-learn groq python-dotenv

  .env file:
    SUPABASE_URL=https://xxxx.supabase.co
    SUPABASE_KEY=your_anon_key
    MQTT_BROKER=edb6b3ee9e994bd7bde9e049bfe11e30.s1.eu.hivemq.cloud
    MQTT_USER=your_mqtt_username
    MQTT_PASSWORD=your_mqtt_password
    GROQ_API_KEY=your_groq_api_key
    TELEGRAM_BOT_TOKEN=your_telegram_bot_token
    TELEGRAM_CHAT_ID=your_telegram_chat_id
=============================================================
"""

import json
import ssl
import os
import urllib.request
import urllib.parse
from datetime import datetime, timedelta

import numpy as np
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
from supabase import create_client
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler
from groq import Groq
from dotenv import load_dotenv

# ── Load environment variables ────────────────────────────────
load_dotenv()

SUPABASE_URL     = os.getenv("SUPABASE_URL")
SUPABASE_KEY     = os.getenv("SUPABASE_KEY")
MQTT_BROKER      = os.getenv("MQTT_BROKER")
MQTT_PORT        = 8883
MQTT_USER        = os.getenv("MQTT_USER")
MQTT_PASSWORD    = os.getenv("MQTT_PASSWORD")
GROQ_API_KEY     = os.getenv("GROQ_API_KEY")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TOPIC_SCAN        = "home/rfid/scan"
ANOMALY_THRESHOLD = 0.6
MIN_TRAIN_SAMPLES = 20

# ── Initialize clients ────────────────────────────────────────
supabase    = create_client(SUPABASE_URL, SUPABASE_KEY)
groq_client = Groq(api_key=GROQ_API_KEY)

# ── Global model state ────────────────────────────────────────
model            = None
scaler           = None
is_model_trained = False
retrain_counter  = []

# =============================================================
#  SUPABASE
# =============================================================

def save_scan_to_db(scan_data: dict, anomaly_score: float, is_anomaly: bool):
    now = datetime.utcnow()
    row = {
        "card_uid":      scan_data["card_uid"],
        "authorized":    scan_data["authorized"],
        "location":      scan_data.get("location", "unknown"),
        "scanned_at":    now.isoformat(),
        "hour_of_day":   now.hour,
        "day_of_week":   now.weekday(),
        "anomaly_score": round(anomaly_score, 4),
        "is_anomaly":    is_anomaly,
    }
    try:
        result = supabase.table("ci_train").insert(row).execute()
        print(f"[DB] Saved: {scan_data['card_uid']} | score={anomaly_score:.3f} | anomaly={is_anomaly}")
        return result
    except Exception as e:
        print(f"[DB ERROR] Failed to save: {e}")


def fetch_scan_history(limit: int = 500) -> list:
    try:
        result = supabase.table("ci_train").select(
            "hour_of_day, day_of_week, authorized, anomaly_score"
        ).order("scanned_at", desc=True).limit(limit).execute()
        return result.data
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch history: {e}")
        return []


def get_recent_scans(card_uid: str, hours: int = 24) -> list:
    try:
        since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        result = supabase.table("ci_train") \
            .select("id") \
            .eq("card_uid", card_uid) \
            .gte("scanned_at", since) \
            .execute()
        return result.data
    except Exception as e:
        print(f"[DB ERROR] Failed to get recent scans: {e}")
        return []

#  ISOLATION FOREST MODEL

def train_model():
    global model, scaler, is_model_trained

    print("[MODEL] Fetching scan history from Supabase...")
    history = fetch_scan_history(limit=500)

    if len(history) < MIN_TRAIN_SAMPLES:
        print(f"[MODEL] Not enough data ({len(history)} scans, need {MIN_TRAIN_SAMPLES})")
        return False

    X = []
    for row in history:
        X.append([
            row["hour_of_day"],
            row["day_of_week"],
            1 if row["authorized"] else 0,
            0,
            1 if row["day_of_week"] >= 5 else 0,
        ])

    X = np.array(X)
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    model = IsolationForest(n_estimators=100, contamination=0.05, random_state=42)
    model.fit(X_scaled)
    is_model_trained = True
    print(f"[MODEL] Trained on {len(X)} scans ✓")
    return True


def score_scan(scan_data: dict) -> float:
    if not is_model_trained:
        return 0.3

    now = datetime.utcnow()
    recent = get_recent_scans(scan_data["card_uid"], hours=24)
    features = np.array([[
        now.hour,
        now.weekday(),
        1 if scan_data.get("authorized", False) else 0,
        len(recent),
        1 if now.weekday() >= 5 else 0,
    ]])
    features_scaled = scaler.transform(features)
    raw_score = model.score_samples(features_scaled)[0]
    return round(max(0.0, min(1.0, 0.5 - raw_score)), 4)

def ask_ai_about_anomaly(scan_data: dict, anomaly_score: float) -> str:
    now = datetime.utcnow()
    try:
        response = groq_client.chat.completions.create(
            model="llama3-8b-8192",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    f"You are a smart home security assistant. "
                    f"An RFID scan was flagged as suspicious. "
                    f"Card UID: {scan_data['card_uid']}, "
                    f"Authorized: {scan_data['authorized']}, "
                    f"Location: {scan_data.get('location', 'front door')}, "
                    f"Time: {now.strftime('%I:%M %p on %A')}, "
                    f"Suspicion score: {anomaly_score:.2f}/1.0. "
                    f"Explain in 2-3 plain sentences why this is suspicious "
                    f"and what the homeowner should do."
                )
            }]
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"[AI ERROR] {e}")
        return f"Suspicious scan at {now.strftime('%I:%M %p')}. Score: {anomaly_score:.2f}/1.0. Please verify."


def send_telegram_alert(scan_data: dict, anomaly_score: float, explanation: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TELEGRAM] Token or Chat ID not set — skipping")
        return

    severity = "🔴 HIGH" if anomaly_score > 0.75 else "🟡 MEDIUM"
    now = datetime.utcnow().strftime("%I:%M %p, %b %d")
    message = (
        f"⚠️ Security Alert — {severity}\n\n"
        f"Card: {scan_data['card_uid']}\n"
        f"Location: {scan_data.get('location', 'front door')}\n"
        f"Time: {now}\n"
        f"Score: {anomaly_score:.2f}/1.0\n\n"
        f"AI Analysis:\n{explanation}"
    )
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data=json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": message}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        urllib.request.urlopen(req)
        print("[TELEGRAM] Alert sent ✓")
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")

# =============================================================
#  MQTT CALLBACKS — paho-mqtt v2 (5 args)
# =============================================================

def check_card(payload: dict):
    result = supabase.table("ci_authorized_cards").select("*").eq("card_uid", payload["card_uid"]).execute()
    return result.data[0]['is_active'] if result.data else False

def on_connect(client, userdata, flags, reason_code, properties):
    print(f"[MQTT] Connect result: {reason_code}")
    if reason_code == 0:
        print("[MQTT] Connected to broker ✓")
        # Subscribe to everything to confirm messages arrive
        client.subscribe("#")
        print("[MQTT] Subscribed to ALL topics (#)")
    else:
        print(f"[MQTT] Connection failed: {reason_code}")


def on_disconnect(client, userdata, flags, reason_code, properties):
    print(f"[MQTT] Disconnected: {reason_code}")


def on_message(client, userdata, msg):
    # print(f"\n[DEBUG] Message on topic: {msg.topic}")
    # print(f"[DEBUG] Raw payload: {msg.payload}")
    try:
        payload      = json.loads(msg.payload.decode())
        card_uid     = payload.get("card_uid", "UNKNOWN")
        # print(f"[SCAN] Card: {card_uid}")
        
        is_authorized = check_card(payload)
        
        anomaly_score = score_scan(payload)
        is_anomaly    = anomaly_score >= ANOMALY_THRESHOLD
        # print(f"[SCORE] {card_uid} → score={anomaly_score:.3f} | anomaly={is_anomaly}")

        print('AUTHORIZED:', is_authorized)

        if is_authorized:
            print('pede pumasok')
            save_scan_to_db(payload, anomaly_score, is_anomaly)
        else:
            print('not authorized')

        if is_anomaly:
            print("[ALERT] Anomaly detected! Asking AI...")
            explanation = ask_ai_about_anomaly(payload, anomaly_score)
            print(f"[AI] {explanation}")
            send_telegram_alert(payload, anomaly_score, explanation)

        retrain_counter.append(1)
        if len(retrain_counter) >= 50:
            retrain_counter.clear()
            train_model()

    except Exception as e:
        print(f"[ERROR] Failed to process message: {e}")

# =============================================================
#  MAIN
# =============================================================

def main():
    print("=" * 55)
    print("  RFID Smart Home Security — AI Backend")
    print("=" * 55)

    train_model()

    client = mqtt.Client(
        callback_api_version=CallbackAPIVersion.VERSION2,
        client_id="python-rfid-backend-v2",  # ← changed ID
        clean_session=True
    )
    client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS)
    client.tls_insecure_set(False)

    client.on_connect    = on_connect
    client.on_disconnect = on_disconnect
    client.on_message    = on_message

    print(f"[MQTT] Connecting to {MQTT_BROKER}:{MQTT_PORT}...")
    client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)

    # Use loop_start instead of loop_forever
    client.loop_start()

    print("[READY] Listening for RFID scan events...\n")

    # Keep the script alive manually
    import time
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[STOP] Shutting down...")
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()