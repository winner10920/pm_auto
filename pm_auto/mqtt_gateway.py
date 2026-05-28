import threading
import glob
import json
import os
import time
from evdev import InputDevice, ecodes
import paho.mqtt.client as mqtt

# Force logs directly to the Supervisor UI
def log_msg(msg):
    print(f"[MQTT Gateway] {msg}", flush=True)

def get_mqtt_config():
    options_file = "/data/options.json"
    if os.path.exists(options_file):
        try:
            with open(options_file, "r") as f:
                options = json.load(f)
                return options.get("mqtt_broker", "core-mosquitto"), options.get("mqtt_username", ""), options.get("mqtt_password", "")
        except Exception as e:
            log_msg(f"Failed to parse HA options.json: {e}")
    return "core-mosquitto", "", ""

MQTT_BROKER, MQTT_USER, MQTT_PASS = get_mqtt_config()

class PironmanMQTTBridge:
    def __init__(self, pm_mcu=None, pm_ws2812=None):
        self.mcu = pm_mcu
        self.ws2812 = pm_ws2812
        self.client = mqtt.Client()
        
    def start(self):
        log_msg(f"Initializing connection to broker: {MQTT_BROKER}")
        self.client.username_pw_set(MQTT_USER, MQTT_PASS)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        
        try:
            self.client.connect(MQTT_BROKER, 1883, 60)
            self.client.loop_start()
            
            ir_thread = threading.Thread(target=self.ir_receiver_loop, daemon=True)
            ir_thread.start()
        except Exception as e:
            log_msg(f"Critical failure connecting to MQTT Broker: {e}")

    def on_connect(self, client, userdata, flags, rc):
        log_msg(f"Successfully connected to MQTT Broker! (Result Code: {rc})")
        client.subscribe("pironman/rgb/set")

    def on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            log_msg(f"Received Command: {payload}")
            
            if msg.topic == "pironman/rgb/set":
                # Interrogate the LED object to see what SunFounder named their functions
                if self.ws2812:
                    methods = [m for m in dir(self.ws2812) if callable(getattr(self.ws2812, m)) and not m.startswith('_')]
                    log_msg(f"DIAGNOSTIC - WS2812 Available Methods: {methods}")
                
                # Keep the Home Assistant UI toggle in sync while we test
                if payload.get("state") == "ON":
                    self.client.publish("pironman/rgb/state", json.dumps({"state": "ON"}), retain=True)
                elif payload.get("state") == "OFF":
                    self.client.publish("pironman/rgb/state", json.dumps({"state": "OFF"}), retain=True)
                        
        except Exception as e:
            log_msg(f"Error parsing MQTT message: {e}")

    def ir_receiver_loop(self):
        """Scans system input events to capture hardware IR signals dynamically"""
        time.sleep(5) 
        ir_device = None
        
        for path in glob.glob('/dev/input/event*'):
            try:
                dev = InputDevice(path)
                if "ir" in dev.name.lower() or "gpio" in dev.name.lower():
                    ir_device = dev
                    break
            except Exception:
                continue

        if not ir_device:
            log_msg("FATAL: IR Receiver hardware device not detected.")
            return

        log_msg(f"Bound hardware IR engine to input stream: {ir_device.path}")
        
        # Listen to the raw hardware stream without filtering
        for event in ir_device.read_loop():
            # Filter out synchronization bursts to avoid log spam, print everything else
            if event.type != ecodes.EV_SYN:
                log_msg(f"Raw Input Event -> Type: {event.type}, Code: {event.code}, Value: {event.value}")
