"""
=============================================================
  RFID Smart Home Security — Python AI Backend
=============================================================
  What this does:
  1. Subscribes to MQTT broker (HiveMQ Cloud)
  2. Saves every scan to Supabase database
  3. Trains an Isolation Forest model on past scan history
  4. Scores every new scan for anomaly (0.0 = normal, 1.0 = suspicious)
  5. Calls Claude AI to explain WHY it's suspicious
  6. Sends Telegram alert if anomaly score is high

  Install dependencies:
    pip install paho-mqtt supabase scikit-learn anthropic python-telegram-bot python-dotenv

  Create a .env file in the same folder:
    SUPABASE_URL=https://xxxx.supabase.co
    SUPABASE_KEY=your_anon_key
    MQTT_BROKER=your_broker.s2.eu.hivemq.cloud
    MQTT_USER=your_mqtt_user
    MQTT_PASSWORD=your_mqtt_password
    ANTHROPIC_API_KEY=your_claude_api_key
    TELEGRAM_BOT_TOKEN=your_telegram_bot_token
    TELEGRAM_CHAT_ID=your_telegram_chat_id
=============================================================
"""

import json
import ssl
import time
import os
from datetime import datetime

import numpy as np
import paho.mqtt.client as mqtt
from supabase import create_client
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler
import anthropic
from dotenv import load_dotenv

# ── Load environment variables ────────────────────────────────
load_dotenv()

SUPABASE_URL      = os.getenv("SUPABASE_URL")
SUPABASE_KEY      = os.getenv("SUPABASE_KEY")
MQTT_BROKER       = os.getenv("MQTT_BROKER")
MQTT_PORT         = 8883
MQTT_USER         = os.getenv("MQTT_USER")
MQTT_PASSWORD     = os.getenv("MQTT_PASSWORD")
ANTHROPIC_KEY     = os.getenv("ANTHROPIC_API_KEY")
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID")

TOPIC_SCAN        = "home/rfid/scan"
ANOMALY_THRESHOLD = 0.6   # scores above this trigger an alert (0.0–1.0)
MIN_TRAIN_SAMPLES = 20    # minimum scans needed before model trains

# ── Initialize clients ────────────────────────────────────────
supabase  = create_client(SUPABASE_URL, SUPABASE_KEY)
claude    = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# ── Global model state ────────────────────────────────────────
model   = None    # Isolation Forest model (trained on history)
scaler  = None    # StandardScaler for feature normalization
is_model_trained = False

# =============================================================
#  SUPABASE HELPERS
# =============================================================

def save_scan_to_db(scan_data: dict, anomaly_score: float, is_anomaly: bool):
    """Save a scan event to Supabase."""
    now = datetime.utcnow()

    row = {
        "card_uid":     scan_data["card_uid"],
        "authorized":   scan_data["authorized"],
        "location":     scan_data.get("location", "unknown"),
        "scanned_at":   now.isoformat(),
        "hour_of_day":  now.hour,
        "day_of_week":  now.weekday(),   # 0=Monday, 6=Sunday
        "anomaly_score": round(anomaly_score, 4),
        "is_anomaly":   is_anomaly,
    }

    result = supabase.table("rfid_scans").insert(row).execute()
    print(f"[DB] Saved scan: {scan_data['card_uid']} | anomaly={is_anomaly} | score={anomaly_score:.3f}")
    return result


def fetch_scan_history(card_uid: str = None, limit: int = 500) -> list:
    """
    Fetch past scans from Supabase for model training.
    If card_uid is given, fetch only that card's history.
    """
    query = supabase.table("rfid_scans").select(
        "hour_of_day, day_of_week, authorized, anomaly_score"
    ).order("scanned_at", desc=True).limit(limit)

    if card_uid:
        query = query.eq("card_uid", card_uid)

    result = query.execute()
    return result.data


def get_recent_scans(card_uid: str, hours: int = 24) -> list:
    """Get how many times a card was scanned in the past N hours."""
    from datetime import timedelta
    since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    result = supabase.table("rfid_scans") \
        .select("id") \
        .eq("card_uid", card_uid) \
        .gte("scanned_at", since) \
        .execute()
    return result.data

def is_card_authorized(card_uid: str) -> bool:
    result = supabase.table("authorized_cards") \
        .select("card_uid") \
        .eq("card_uid", card_uid) \
        .eq("is_active", True) \
        .execute()
    return len(result.data) > 0

# =============================================================
#  FEATURE ENGINEERING
#  (Convert a scan into numbers the model can understand)
# =============================================================

def extract_features(scan_data: dict) -> np.ndarray:
    """
    Convert a raw scan event into a feature vector for the model.

    Features:
      [0] hour_of_day   — 0–23  (late night scans are unusual)
      [1] day_of_week   — 0–6   (weekend patterns differ)
      [2] is_authorized — 0 or 1 (unauthorized cards score higher)
      [3] recent_count  — how many times this card scanned in last 24h
      [4] is_weekend    — 0 or 1 (weekend flag)
    """
    now = datetime.utcnow()
    hour       = now.hour
    day        = now.weekday()
    authorized = 1 if scan_data.get("authorized", False) else 0
    is_weekend = 1 if day >= 5 else 0

    # Count recent scans for this card (high frequency = suspicious)
    recent = get_recent_scans(scan_data["card_uid"], hours=24)
    recent_count = len(recent)

    features = np.array([[hour, day, authorized, recent_count, is_weekend]])
    return features


# =============================================================
#  ISOLATION FOREST MODEL
# =============================================================

def train_model():
    """
    Train the Isolation Forest model on historical scan data.

    Isolation Forest works by:
    1. Building many random decision trees
    2. Measuring how quickly a data point gets isolated (split off)
    3. Points that are isolated quickly = ANOMALIES (they're different)
    4. Points that take many splits = NORMAL (they blend in with others)

    Think of it like this:
    If you ask "Is this scan at night?" and most scans are during the day,
    a night scan gets isolated very quickly → it's an anomaly.
    """
    global model, scaler, is_model_trained

    print("[MODEL] Fetching scan history from Supabase...")
    history = fetch_scan_history(limit=500)

    if len(history) < MIN_TRAIN_SAMPLES:
        print(f"[MODEL] Not enough data to train ({len(history)} scans, need {MIN_TRAIN_SAMPLES})")
        return False

    # Build feature matrix from history
    X = []
    for row in history:
        features = [
            row["hour_of_day"],
            row["day_of_week"],
            1 if row["authorized"] else 0,
            0,       # recent_count not stored historically, use 0
            1 if row["day_of_week"] >= 5 else 0,
        ]
        X.append(features)

    X = np.array(X)

    # Normalize features so all are on the same scale
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # Train Isolation Forest
    # contamination = expected % of anomalies in training data (5% here)
    model = IsolationForest(
        n_estimators=100,     # number of trees (more = more accurate)
        contamination=0.05,   # assume 5% of past data were anomalies
        random_state=42,
        max_samples="auto",
    )
    model.fit(X_scaled)
    is_model_trained = True

    print(f"[MODEL] Trained on {len(X)} scans ✓")
    return True


def score_scan(scan_data: dict) -> float:
    """
    Score a new scan.
    Returns a value between 0.0 (totally normal) and 1.0 (very suspicious).

    How scoring works:
    - Isolation Forest returns a raw score where:
        -1 = anomaly, +1 = normal
    - We convert this to a 0.0–1.0 scale for readability
    """
    if not is_model_trained:
        print("[MODEL] Model not trained yet — returning neutral score 0.3")
        return 0.3

    features = extract_features(scan_data)
    features_scaled = scaler.transform(features)

    # Raw score: -1 (anomaly) to +1 (normal)
    raw_score = model.score_samples(features_scaled)[0]

    # Convert to 0.0–1.0 where higher = more suspicious
    # Typical range is about -0.5 to +0.5, so we normalize
    anomaly_score = max(0.0, min(1.0, (0.5 - raw_score)))

    return round(anomaly_score, 4)

# =============================================================
#  CLAUDE AI — EXPLAIN THE ANOMALY
# =============================================================

def ask_claude_about_anomaly(scan_data: dict, anomaly_score: float) -> str:
    """
    Ask Claude AI to explain WHY this scan is suspicious in plain English.
    This makes the alert notification much more useful.

    Example output:
    "Card A1B2C3D4 scanned at 3:47 AM on a Monday, which is highly unusual
    based on past patterns. This card has not been used in 30 days and the
    timing suggests unauthorized access. Recommend checking cameras."
    """
    now = datetime.utcnow()

    prompt = f"""You are an AI security assistant for a smart home RFID system.

An RFID card scan was flagged as potentially suspicious. Analyze it and explain 
the concern briefly in 2-3 sentences, as if notifying the homeowner.

Scan details:
- Card UID: {scan_data['card_uid']}
- Authorized card: {scan_data['authorized']}
- Location: {scan_data.get('location', 'front door')}
- Time of scan: {now.strftime('%I:%M %p')} on {now.strftime('%A, %B %d')}
- Hour of day: {now.hour} (0=midnight, 12=noon, 23=11pm)
- Anomaly score: {anomaly_score:.2f} out of 1.0 (higher = more suspicious)

Respond in 2-3 short sentences. Be direct and practical. 
Do not use technical terms like "anomaly score" — speak plainly.
End with a simple recommended action (check cameras, verify with family, etc.)."""

    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text

# =============================================================
#  TELEGRAM ALERT
# =============================================================

def send_telegram_alert(scan_data: dict, anomaly_score: float, ai_explanation: str):
    """Send an alert to Telegram with the AI explanation."""
    import urllib.request

    severity = "🔴 HIGH" if anomaly_score > 0.75 else "🟡 MEDIUM"
    now = datetime.utcnow().strftime("%I:%M %p, %b %d")

    message = (
        f"⚠️ *Security Alert — {severity}*\n\n"
        f"🪪 Card: `{scan_data['card_uid']}`\n"
        f"📍 Location: {scan_data.get('location', 'front door')}\n"
        f"🕐 Time: {now}\n"
        f"📊 Score: {anomaly_score:.2f}/1.0\n\n"
        f"🤖 *AI Analysis:*\n{ai_explanation}"
    )

    url = (
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        f"?chat_id={TELEGRAM_CHAT_ID}"
        f"&text={urllib.parse.quote(message)}"
        f"&parse_mode=Markdown"
    )

    try:
        import urllib.parse
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data=json.dumps({
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown"
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        urllib.request.urlopen(req)
        print("[TELEGRAM] Alert sent ✓")
    except Exception as e:
        print(f"[TELEGRAM] Failed to send alert: {e}")


# =============================================================
#  MQTT CALLBACKS
# =============================================================

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"[MQTT] Connected to broker ✓")
        client.subscribe(TOPIC_SCAN)
        print(f"[MQTT] Subscribed to {TOPIC_SCAN}")
    else:
        print(f"[MQTT] Connection failed, rc={rc}")


def on_message(client, userdata, msg):
    """Called every time the ESP32 publishes a card scan."""
    try:
        payload = json.loads(msg.payload.decode())
        card_uid = payload.get("card_uid", "UNKNOWN")
        # Override whatever the ESP32 sent — check DB instead
        payload["authorized"] = is_card_authorized(payload["card_uid"])

        print(f"\n[SCAN] New scan received: {card_uid}")
        # 1. Score the scan for anomaly
        anomaly_score = score_scan(payload)
        is_anomaly    = anomaly_score >= ANOMALY_THRESHOLD

        print(f"[SCORE] {card_uid} → score={anomaly_score:.3f} | anomaly={is_anomaly}")

        # 2. Save to Supabase
        save_scan_to_db(payload, anomaly_score, is_anomaly)

        # 3. If anomaly detected, ask Claude and send alert
        if is_anomaly:
            print(f"[ALERT] Anomaly detected! Asking Claude for explanation...")
            explanation = ask_claude_about_anomaly(payload, anomaly_score)
            print(f"[CLAUDE] {explanation}")
            send_telegram_alert(payload, anomaly_score, explanation)

        # 4. Retrain model every 50 scans to improve over time
        retrain_counter.append(1)
        if len(retrain_counter) >= 50:
            retrain_counter.clear()
            print("[MODEL] Retraining model with new data...")
            train_model()

    except Exception as e:
        print(f"[ERROR] Failed to process message: {e}")


# =============================================================
#  MAIN
# =============================================================

retrain_counter = []

def main():
    print("=" * 55)
    print("  RFID Smart Home Security — AI Backend")
    print("=" * 55)

    # Train initial model from existing Supabase data
    print("[STARTUP] Training initial model from Supabase history...")
    trained = train_model()
    if not trained:
        print("[STARTUP] Will train once enough scan data is collected (need 20 scans)")

    # Connect to MQTT
    client = mqtt.Client(client_id="python-rfid-backend")
    client.username_pw_set(MQTT_USER, MQTT_PASSWORD)

    # TLS for HiveMQ Cloud
    client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS)
    client.tls_insecure_set(False)

    client.on_connect = on_connect
    client.on_message = on_message

    print(f"[MQTT] Connecting to {MQTT_BROKER}:{MQTT_PORT}...")
    client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)

    # Keep running forever, processing scan events
    print("[READY] Listening for RFID scan events...\n")
    client.loop_forever()


if __name__ == "__main__":
    main()
