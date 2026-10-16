import json
import time
import random
import paho.mqtt.client as mqtt

BROKER = "localhost"
PORT = 1883
DEVICE_ID = "bms-node-01"
TOPIC = f"libre-solar/bms/{DEVICE_ID}/telemetry"

# Use callback API version to suppress modern paho-mqtt warnings
client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.connect(BROKER, PORT, 60)

voltage = 52.4  # Volts
soc = 98.5      # State of Charge (%)
soh = 95.0      # State of Health (%)
cycles = 142    # Cycle count estimate
sequence = 1    # Message sequence counter

print("Starting High-Detail Libre Solar BMS Simulator... Press Ctrl+C to stop.")

try:
    while True:
        voltage -= 0.02
        soc -= 0.05
        if soc < 20:
            soc, voltage = 99.0, 52.8

        payload = {
            "device_id": DEVICE_ID,
            "sequence": sequence,
            "timestamp": int(time.time()),
            "telemetry": {
                "voltage_V": round(voltage, 4),
                "current_A": round(random.uniform(-4.5, -1.2), 2),
                "temperature_C": round(random.uniform(25.0, 29.5), 1),
                "soc_percent": round(soc, 2),
                "soh_percent": round(soh, 2),
                "cycle_count": cycles,
                "cell_imbalance_mV": random.randint(5, 18),
                "max_temperature_C": 31.2
            },
            "quality": "Measured",
            "fault_flags": [],
            "signature": "simulated_ed25519_secure_hash_v1"
        }

        client.publish(TOPIC, json.dumps(payload))
        print(f"[{sequence}] Published to {TOPIC}: {payload['telemetry']}")
        sequence += 1
        time.sleep(5)

except KeyboardInterrupt:
    print("\nSimulator stopped.")