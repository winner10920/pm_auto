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
    """Extracts MQTT configurations mapped by Home Assistant Supervisor."""
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
        
        # Inject Home Assistant Auto-Discovery for the RGB Light
        discovery_payload = {
            "name": "Pironman Case Lights",
            "schema": "json",
            "command_topic": "pironman/rgb/set",
            "state_topic": "pironman/rgb/state",
            "brightness": False,
            "color_mode": True,
            "supported_color_modes": ["rgb"],
            "unique_id": "pironman5_rgb_strip",
            "device": {
                "identifiers": ["pironman5_case"],
                "name": "Pironman 5",
                "manufacturer": "SunFounder"
            }
        }
        client.publish("homeassistant/light/pironman5_rgb/config", json.dumps(discovery_payload), retain=True)
        log_msg("Published Auto-Discovery payload. The light entity should now appear in HA!")

    def on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            log_msg(f"Received Command: {payload}")
            
            if msg.topic == "pironman/rgb/set":
                if payload.get("state") == "ON":
                    if "color" in payload:
                        r = payload["color"].get("r", 255)
                        g = payload["color"].get("g", 255)
                        b = payload["color"].get("b", 255)
                        if self.ws2812:
                            self.ws2812.update_rgb_style('solid')
                            self.ws2812.update_rgb_color([r, g, b])
                            
                    # Report back to HA that the light is actually ON
                    self.client.publish("pironman/rgb/state", json.dumps({"state": "ON"}), retain=True)
                    
                elif payload.get("state") == "OFF":
                    if self.ws2812:
                        self.ws2812.update_rgb_enable(False)
                    self.client.publish("pironman/rgb/state", json.dumps({"state": "OFF"}), retain=True)
                        
        except Exception as e:
            log_msg(f"Error parsing MQTT message: {e}")

    def ir_receiver_loop(self):
        """Scans system input events to capture hardware IR signals dynamically"""
        time.sleep(5) # Let the system boot before scanning for devices
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
            log_msg("FATAL: IR Receiver hardware device not detected in /dev/input/. Did you edit the host config.txt?")
            return

        log_msg(f"Bound hardware IR engine to input stream: {ir_device.path}")
        
        for event in ir_device.read_loop():
            if event.type == ecodes.EV_KEY and event.value == 1:
                payload = {"code": event.code, "hex": hex(event.code)}
                log_msg(f"IR Code Captured: {payload}")
                self.client.publish("pironman/ir/receiver", json.dumps(payload), qos=1)
