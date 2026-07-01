import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
import ssl
from dotenv import load_dotenv
import os

load_dotenv()

BROKER   = os.getenv("MQTT_BROKER")
USER     = os.getenv("MQTT_USER")
PASSWORD = os.getenv("MQTT_PASSWORD")

print(f"Connecting to: {BROKER}")
print(f"User: {USER}")

def on_connect(client, userdata, flags, reason_code, properties):
    print(f"Connected: {reason_code}")
    client.subscribe("#")
    print("Subscribed to ALL topics — waiting for messages...")

def on_message(client, userdata, msg):
    print(f"✅ MESSAGE RECEIVED!")
    print(f"   Topic:   {msg.topic}")
    print(f"   Payload: {msg.payload.decode()}")

client = mqtt.Client(
    callback_api_version=CallbackAPIVersion.VERSION2,
    client_id="test-receiver-001"
)
client.username_pw_set(USER, PASSWORD)
client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS)
client.tls_insecure_set(False)
client.on_connect = on_connect
client.on_message = on_message

client.connect(BROKER, 8883, keepalive=60)
client.loop_forever()