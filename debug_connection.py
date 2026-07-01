import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
import ssl, time
from dotenv import load_dotenv
import os

load_dotenv()

BROKER   = os.getenv("MQTT_BROKER")
USER     = os.getenv("MQTT_USER")  
PASSWORD = os.getenv("MQTT_PASSWORD")

print(f"=== CONNECTION DEBUG ===")
print(f"Broker:   [{BROKER}]")
print(f"User:     [{USER}]")
print(f"Password: [{PASSWORD}]")
print(f"=======================")

def on_connect(client, userdata, flags, reason_code, properties):
    print(f"\n>>> on_connect fired! reason_code = {reason_code}")
    client.subscribe("#")
    print(f">>> Subscribed to #")

def on_message(client, userdata, msg):
    print(f"\n>>> MESSAGE! topic={msg.topic} payload={msg.payload}")

def on_subscribe(client, userdata, mid, reason_codes, properties):
    print(f">>> on_subscribe fired! mid={mid} reason={reason_codes}")

def on_log(client, userdata, level, buf):
    print(f"[LOG] {buf}")  # prints every internal MQTT event

client = mqtt.Client(
    callback_api_version=CallbackAPIVersion.VERSION2,
    client_id="debug-001"
)
client.username_pw_set(USER, PASSWORD)
client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS)
client.tls_insecure_set(False)
client.on_connect   = on_connect
client.on_message   = on_message
client.on_subscribe = on_subscribe
client.on_log       = on_log  # ← logs everything

print(f"\nConnecting to {BROKER}:8883 ...")
client.connect(BROKER, 8883, keepalive=60)
client.loop_start()
time.sleep(10)  # wait 10 seconds
client.loop_stop()
print("\nDone - did you see any messages?")