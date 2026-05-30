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
            
            # Start hardware listeners
            threading.Thread(target=self.ir_receiver_loop, daemon=True).start()
            threading.Thread(target=self.fan_telemetry_loop, daemon=True).start()
        except Exception as e:
            log_msg(f"Critical failure connecting to MQTT Broker: {e}")

    def on_connect(self, client, userdata, flags, rc):
        log_msg(f"Successfully connected to MQTT Broker! (Result Code: {rc})")
        client.subscribe("pironman/rgb/set")

        # 1. RGB Light Auto-Discovery
        light_payload = {
            "name": "Pironman Case Lights",
            "schema": "json",
            "command_topic": "pironman/rgb/set",
            "state_topic": "pironman/rgb/state",
            "brightness": True,
            "color_mode": True,
            "supported_color_modes": ["rgb"],
            "unique_id": "pironman5_rgb_strip",
            "device": {"identifiers": ["pironman5_case"], "name": "Pironman 5", "manufacturer": "SunFounder"}
        }
        client.publish("homeassistant/light/pironman5_rgb/config", json.dumps(light_payload), retain=True)

        # 2. Fan Speed Auto-Discovery
        fan_payload = {
            "name": "Pironman Fan Speed",
            "state_topic": "pironman/fan/speed",
            "unit_of_measurement": "RPM",
            "value_template": "{{ value_json.speed }}",
            "icon": "mdi:fan",
            "unique_id": "pironman5_fan_speed",
            "device": {"identifiers": ["pironman5_case"], "name": "Pironman 5", "manufacturer": "SunFounder"}
        }
        client.publish("homeassistant/sensor/pironman5_fan/config", json.dumps(fan_payload), retain=True)

    def on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            log_msg(f"Received HA Command: {payload}")
            
            if msg.topic == "pironman/rgb/set":
                if payload.get("state") == "ON":
                    config_update = {"rgb_enable": True, "rgb_style": "solid"}
                    
                    # Map HA Brightness (0-255) to WS2812 (0-100)
                    if "brightness" in payload:
                        ha_bright = payload.get("brightness", 255)
                        config_update["rgb_brightness"] = int((ha_bright / 255.0) * 100)
                    
                    # Map HA Color (R,G,B) to WS2812 Hex
                    if "color" in payload:
                        r = payload["color"].get("r", 255)
                        g = payload["color"].get("g", 255)
                        b = payload["color"].get("b", 255)
                        config_update["rgb_color"] = f"#{r:02x}{g:02x}{b:02x}"
                        
                    if self.ws2812:
                        self.ws2812.update_config(config_update)
                        
                    self.client.publish("pironman/rgb/state", json.dumps({"state": "ON"}), retain=True)
                    
                elif payload.get("state") == "OFF":
                    if self.ws2812:
                        self.ws2812.update_config({"rgb_enable": False})
                    self.client.publish("pironman/rgb/state", json.dumps({"state": "OFF"}), retain=True)
                        
        except Exception as e:
            log_msg(f"Error parsing MQTT message: {e}")

    def fan_telemetry_loop(self):
        """Reads hardware tree to bypass SunFounder class tracking"""
        hwmon_dir = '/sys/devices/platform/cooling_fan/hwmon/'
        while True:
            time.sleep(5) # Update every 5 seconds
            try:
                if os.path.exists(hwmon_dir):
                    subdirs = os.listdir(hwmon_dir)
                    if subdirs:
                        path = os.path.join(hwmon_dir, subdirs[0], 'fan1_input')
                        if os.path.exists(path):
                            with open(path, 'r') as f:
                                speed = int(f.read().strip())
                            self.client.publish("pironman/fan/speed", json.dumps({"speed": speed}), retain=True)
            except Exception:
                pass

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
        
        for event in ir_device.read_loop():
            # Skip synchronization bursts to prevent log spam
            if event.type != ecodes.EV_SYN:
                log_msg(f"Raw Input Event -> Type: {event.type}, Code: {event.code}, Value: {event.value}")
                
                # If a button is pressed down, forward it to Home Assistant
                if event.value == 1: 
                    payload = {"code": event.code, "type": event.type}
                    self.client.publish("pironman/ir/receiver", json.dumps(payload), qos=1)
