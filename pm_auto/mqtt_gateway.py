import threading
import glob
import json
import logging
import os
from evdev import InputDevice, ecodes
import paho.mqtt.client as mqtt

def get_mqtt_config():
    """Extracts MQTT configurations mapped by Home Assistant Supervisor."""
    options_file = "/data/options.json"
    
    if os.path.exists(options_file):
        try:
            with open(options_file, "r") as f:
                options = json.load(f)
                broker = options.get("mqtt_broker", "core-mosquitto")
                user = options.get("mqtt_username", "")
                password = options.get("mqtt_password", "")
                return broker, user, password
        except Exception as e:
            logging.error(f"Failed to parse HA options.json: {e}")
            
    # Fallback if running outside of HA environment
    return "core-mosquitto", "", ""

MQTT_BROKER, MQTT_USER, MQTT_PASS = get_mqtt_config()


class PironmanMQTTBridge:
    def __init__(self, pm_mcu=None, pm_ws2812=None):
        self.mcu = pm_mcu          # Reference to fan/system hooks if needed
        self.ws2812 = pm_ws2812    # Reference to LED strip object
        self.client = mqtt.Client()
        
    def start(self):
        self.client.username_pw_set(MQTT_USER, MQTT_PASS)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        
        try:
            self.client.connect(MQTT_BROKER, 1883, 60)
            self.client.loop_start()
            
            # Spawn dedicated thread for the IR hardware loop
            ir_thread = threading.Thread(target=self.ir_receiver_loop, daemon=True)
            ir_thread.start()
            logging.info("MQTT Gateway and IR thread initialized successfully.")
        except Exception as e:
            logging.error(f"Failed to initialize MQTT Gateway: {e}")

    def on_connect(self, client, userdata, flags, rc):
        logging.info(f"Connected to MQTT Broker (rc: {rc})")
        # Subscribe to light commands
        client.subscribe("pironman/rgb/set")
        # Subscribe to custom OLED commands
        client.subscribe("pironman/oled/set")

    def on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            
            # --- RGB Control Logic ---
            if msg.topic == "pironman/rgb/set":
                if payload.get("state") == "ON":
                    if "color" in payload:
                        r = payload["color"].get("r", 255)
                        g = payload["color"].get("g", 255)
                        b = payload["color"].get("b", 255)
                        if self.ws2812:
                            self.ws2812.update_rgb_style('solid')
                            self.ws2812.update_rgb_color([r, g, b])
                elif payload.get("state") == "OFF":
                    if self.ws2812:
                        self.ws2812.update_rgb_enable(False)
                        
        except Exception as e:
            logging.error(f"Error parsing MQTT message: {e}")

    def ir_receiver_loop(self):
        """Scans system input events to capture hardware IR signals dynamically"""
        ir_device = None
        # Locate the gpio-ir overlay device in the container
        for path in glob.glob('/dev/input/event*'):
            try:
                dev = InputDevice(path)
                if "ir" in dev.name.lower() or "gpio" in dev.name.lower():
                    ir_device = dev
                    break
            except Exception:
                continue

        if not ir_device:
            logging.error("IR Receiver hardware device not detected in /dev/input/")
            return

        logging.info(f"Bound hardware IR engine to input stream: {ir_device.path}")
        
        # Read stream events sequentially
        for event in ir_device.read_loop():
            if event.type == ecodes.EV_KEY and event.value == 1: # Key Down Event
                payload = {"code": event.code, "hex": hex(event.code)}
                self.client.publish("pironman/ir/receiver", json.dumps(payload), qos=1)

    def publish_fan_status(self, speed):
        """Helper call to send telemetry back to HA"""
        if self.client.is_connected():
            self.client.publish("pironman/fan/speed", json.dumps({"speed": speed}), retain=True)
