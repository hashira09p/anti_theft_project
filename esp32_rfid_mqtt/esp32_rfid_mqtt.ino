/*
 * ============================================================
 *  ESP32 + RC522 RFID — Smart Home Security with MQTT
 * ============================================================
 *  Libraries required (install via Arduino Library Manager):
 *    - MFRC522  by GithubCommunity  (RFID reader)
 *    - PubSubClient by Nick O'Leary (MQTT client)
 *    - ArduinoJson by Benoit Blanchon (JSON payload)
 *
 *  Wiring (RC522 → ESP32):
 *    SDA  → GPIO 5
 *    SCK  → GPIO 18
 *    MOSI → GPIO 23
 *    MISO → GPIO 19
 *    RST  → GPIO 22
 *    3.3V → 3.3V
 *    GND  → GND
 * ============================================================
 */

#include <SPI.h>
#include <MFRC522.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>

// ── Pin definitions ──────────────────────────────────────────
#define SS_PIN   5    // SDA
#define RST_PIN  22
#define LED_GREEN_PIN  2   // Green LED = access granted
#define LED_RED_PIN    4   // Red LED   = access denied
#define BUZZER_PIN     15  // Buzzer for audio feedback

// ── WiFi credentials ─────────────────────────────────────────
const char* WIFI_SSID     = "ESP32";
const char* WIFI_PASSWORD = "77179562";

// ── MQTT broker settings ─────────────────────────────────────
// Using HiveMQ Cloud (free tier) — replace with your broker
const char* MQTT_BROKER   = "edb6b3ee9e994bd7bde9e049bfe11e30.s1.eu.hivemq.cloud";
const int   MQTT_PORT     = 8883;           // TLS port
const char* MQTT_USER     = "testing";
const char* MQTT_PASSWORD = "Testing123";
const char* MQTT_CLIENT_ID = "esp32-rfid-home";

// ── MQTT topics ───────────────────────────────────────────────
const char* TOPIC_SCAN    = "home/rfid/scan";       // publish scans here
const char* TOPIC_ACCESS  = "home/rfid/access";     // subscribe for access decisions
const char* TOPIC_STATUS  = "home/rfid/status";     // device heartbeat

// ── Authorized card UIDs ──────────────────────────────────────
// Add your registered card UIDs here (format: "AA BB CC DD")
const char* AUTHORIZED_CARDS[] = {
  "39 34 49 12",   // Owner card
  "E5 F6 07 18",   // Family member
};
const int AUTHORIZED_COUNT = 2;

// ── Location identifier ───────────────────────────────────────
const char* LOCATION = "front_door";   // change per device

// ── Globals ───────────────────────────────────────────────────
MFRC522 rfid(SS_PIN, RST_PIN);
WiFiClientSecure espClient;
PubSubClient mqtt(espClient);

String lastCardUID  = "";
unsigned long lastScanTime = 0;
const unsigned long SCAN_DEBOUNCE_MS = 2000;  // ignore same card for 2 sec

// ═══════════════════════════════════════════════════════════════
//  SETUP
// ═══════════════════════════════════════════════════════════════
void setup() {
  Serial.begin(115200);

  // Pins
  pinMode(LED_GREEN_PIN, OUTPUT);
  pinMode(LED_RED_PIN,   OUTPUT);
  pinMode(BUZZER_PIN,    OUTPUT);

  // SPI + RFID
  SPI.begin();
  rfid.PCD_Init();
  Serial.println("[RFID] RC522 initialized");
  rfid.PCD_DumpVersionToSerial();

  // WiFi
  connectWiFi();

  // MQTT
  espClient.setInsecure(); 
  mqtt.setServer(MQTT_BROKER, MQTT_PORT);
  mqtt.setCallback(onMqttMessage);
  mqtt.setKeepAlive(60);
  connectMQTT();

  // Startup feedback
  blinkLED(LED_GREEN_PIN, 3, 100);
  Serial.println("[READY] Waiting for RFID card...");
}

// ═══════════════════════════════════════════════════════════════
//  MAIN LOOP
// ═══════════════════════════════════════════════════════════════
void loop() {
  // Keep MQTT alive
  if (!mqtt.connected()) {
    connectMQTT();
  }
  mqtt.loop();

  // Send heartbeat every 30 seconds
  sendHeartbeat();

  // Check for new RFID card
  if (!rfid.PICC_IsNewCardPresent() || !rfid.PICC_ReadCardSerial()) {
    return;
  }

  // Get card UID as string
  String uid = getCardUID();
  Serial.println(uid);

  // Debounce: ignore same card scanned within 2 seconds
  if (uid == lastCardUID && (millis() - lastScanTime < SCAN_DEBOUNCE_MS)) {
    rfid.PICC_HaltA();
    return;
  }

  lastCardUID  = uid;
  lastScanTime = millis();

  Serial.print("[SCAN] Card detected: ");
  Serial.println(uid);

  // Check if authorized
  bool isAuthorized = checkAuthorized(uid);

  // Local feedback (LED + buzzer)
  if (isAuthorized) {
    grantAccess();
  } else {
    denyAccess();
  }

  // Publish scan event to MQTT broker
  publishScanEvent(uid, isAuthorized);

  rfid.PICC_HaltA();
  rfid.PCD_StopCrypto1();
}

// ═══════════════════════════════════════════════════════════════
//  RFID HELPERS
// ═══════════════════════════════════════════════════════════════

// Convert UID bytes to readable hex string "AA BB CC DD"
String getCardUID() {
  String uid = "";
  for (byte i = 0; i < rfid.uid.size; i++) {
    if (rfid.uid.uidByte[i] < 0x10) uid += "0";
    uid += String(rfid.uid.uidByte[i], HEX);
    if (i < rfid.uid.size - 1) uid += " ";
  }
  uid.toUpperCase();
  Serial.println(uid);
  return uid;
}

// Check if UID is in the authorized list
bool checkAuthorized(String uid) {
  for (int i = 0; i < AUTHORIZED_COUNT; i++) {
    if (uid == String(AUTHORIZED_CARDS[i])) {
      return true;
    }
  }
  return false;
}

// ═══════════════════════════════════════════════════════════════
//  MQTT HELPERS
// ═══════════════════════════════════════════════════════════════

// Publish a scan event as JSON
void publishScanEvent(String uid, bool authorized) {
  StaticJsonDocument<256> doc;

  doc["card_uid"]    = uid;
  doc["authorized"]  = authorized;
  doc["location"]    = LOCATION;
  doc["timestamp"]   = millis();   // use NTP time in production
  doc["device_id"]   = MQTT_CLIENT_ID;

  char payload[256];
  serializeJson(doc, payload);

  if (mqtt.publish(TOPIC_SCAN, payload, true)) {
    Serial.print("[MQTT] Published scan: ");
    Serial.println(payload);
  } else {
    Serial.println("[MQTT] Publish failed!");
  }
}

// Receive messages from the broker (e.g. AI access decisions)
void onMqttMessage(char* topic, byte* message, unsigned int length) {
  String msg = "";
  for (unsigned int i = 0; i < length; i++) {
    msg += (char)message[i];
  }

  Serial.print("[MQTT] Message on topic ");
  Serial.print(topic);
  Serial.print(": ");
  Serial.println(msg);

  // Parse JSON response from AI backend
  if (String(topic) == TOPIC_ACCESS) {
    StaticJsonDocument<256> doc;
    DeserializationError err = deserializeJson(doc, msg);
    if (!err) {
      const char* decision  = doc["decision"];   // "grant" or "deny"
      const char* reason    = doc["reason"];      // AI explanation
      int         severity  = doc["severity"];    // 0–100

      Serial.print("[AI DECISION] ");
      Serial.print(decision);
      Serial.print(" — ");
      Serial.println(reason);

      // Override local decision based on AI (for anomaly override)
      if (severity > 70) {
        Serial.println("[ALERT] High severity anomaly — locking down!");
        denyAccess();
        triggerAlarm();
      }
    }
  }
}

// Publish device heartbeat every 30 seconds
unsigned long lastHeartbeat = 0;
void sendHeartbeat() {
  if (millis() - lastHeartbeat < 30000) return;
  lastHeartbeat = millis();

  StaticJsonDocument<128> doc;
  doc["device_id"] = MQTT_CLIENT_ID;
  doc["location"]  = LOCATION;
  doc["uptime_ms"] = millis();
  doc["wifi_rssi"] = WiFi.RSSI();

  char payload[128];
  serializeJson(doc, payload);
  mqtt.publish(TOPIC_STATUS, payload);
  Serial.println("[HEARTBEAT] Sent");
}

// ═══════════════════════════════════════════════════════════════
//  WiFi + MQTT CONNECTION
// ═══════════════════════════════════════════════════════════════
void connectWiFi() {
  Serial.print("[WiFi] Connecting to ");
  Serial.print(WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();
  Serial.print("[WiFi] Connected — IP: ");
  Serial.println(WiFi.localIP());
}

void connectMQTT() {
  while (!mqtt.connected()) {
    Serial.print("[MQTT] Connecting to broker...");
    if (mqtt.connect(MQTT_CLIENT_ID, MQTT_USER, MQTT_PASSWORD)) {
      Serial.println(" connected!");
      // Subscribe to access decision topic
      mqtt.subscribe(TOPIC_ACCESS);
      Serial.print("[MQTT] Subscribed to: ");
      Serial.println(TOPIC_ACCESS);
    } else {
      Serial.print(" failed, rc=");
      Serial.print(mqtt.state());
      Serial.println(" — retry in 5s");
      delay(5000);
    }
  }
}

// ═══════════════════════════════════════════════════════════════
//  FEEDBACK: LED + BUZZER
// ═══════════════════════════════════════════════════════════════
void grantAccess() {
  Serial.println("[ACCESS] GRANTED ✓");
  digitalWrite(LED_GREEN_PIN, HIGH);
  tone(BUZZER_PIN, 1000, 200);
  delay(200);
  tone(BUZZER_PIN, 1500, 200);
  delay(500);
  digitalWrite(LED_GREEN_PIN, LOW);
}

void denyAccess() {
  Serial.println("[ACCESS] DENIED ✗");
  for (int i = 0; i < 3; i++) {
    digitalWrite(LED_RED_PIN, HIGH);
    tone(BUZZER_PIN, 300, 150);
    delay(150);
    digitalWrite(LED_RED_PIN, LOW);
    delay(100);
  }
}

void triggerAlarm() {
  Serial.println("[ALARM] Anomaly alarm triggered!");
  for (int i = 0; i < 10; i++) {
    digitalWrite(LED_RED_PIN, HIGH);
    tone(BUZZER_PIN, 2000, 100);
    delay(100);
    digitalWrite(LED_RED_PIN, LOW);
    delay(100);
  }
}

void blinkLED(int pin, int times, int delayMs) {
  for (int i = 0; i < times; i++) {
    digitalWrite(pin, HIGH);
    delay(delayMs);
    digitalWrite(pin, LOW);
    delay(delayMs);
  }
}
