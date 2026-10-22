"""One-shot Home Assistant setup for the demo stack.

Finishes the onboarding wizard (creating the admin user) and adds the MQTT
integration pointing at the broker, so BMS entities appear without any clicks.
Safe to run again: steps already done are skipped.

Only for local demos. Against a Home Assistant you already use, add the MQTT
integration by hand instead (Settings → Devices & services → Add → MQTT).
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HA_URL = os.getenv("HA_URL", "http://homeassistant:8123").rstrip("/")
CLIENT_ID = os.getenv("HA_CLIENT_ID", "http://localhost:8123/")
USERNAME = os.getenv("HA_USERNAME", "admin")
PASSWORD = os.getenv("HA_PASSWORD", "admin")
MQTT_HOST = os.getenv("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))


class HAError(Exception):
    def __init__(self, method: str, path: str, exc: urllib.error.HTTPError):
        self.code = exc.code
        super().__init__(f"{method} {path} failed: HTTP {exc.code} {exc.read().decode()[:300]}")


def request(method: str, path: str, data=None, token: str | None = None, form: bool = False):
    headers = {}
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(HA_URL + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode()
            return json.loads(text) if text else None
    except urllib.error.HTTPError as exc:
        raise HAError(method, path, exc) from None


def form_data(schema: list[dict], values: dict) -> dict:
    """Fill a config-flow form: our values, else the form's own defaults.

    Newer Home Assistant versions add required fields (some grouped in
    expandable sections), so the form is read instead of hard-coded.
    """
    data = {}
    for field in schema:
        name = field["name"]
        if name in values:
            data[name] = values[name]
        elif field.get("type") == "expandable":
            data[name] = form_data(field["schema"], values)
        elif "default" in field:
            data[name] = field["default"]
        elif field.get("required"):
            # Required without a default: booleans off, selects on their first option (e.g. TLS "off").
            selector = field.get("selector", {})
            if "boolean" in selector:
                data[name] = False
            elif "select" in selector:
                option = selector["select"]["options"][0]
                data[name] = option["value"] if isinstance(option, dict) else option
    return data


def wait_for_ha(timeout_s: float = 600) -> dict[str, bool]:
    """Onboarding steps and whether each is done. Home Assistant removes the
    onboarding API (404) once everything is done."""
    deadline = time.time() + timeout_s
    while True:
        try:
            return {s["step"]: s["done"] for s in request("GET", "/api/onboarding")}
        except HAError as exc:
            if exc.code == 404:
                return {}
            raise
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            if time.time() > deadline:
                sys.exit(f"Home Assistant not reachable at {HA_URL}: {exc}")
            print("Waiting for Home Assistant...", flush=True)
            time.sleep(5)


def login(attempts: int = 12) -> str:
    """Auth code for an existing user via the login flow (retried while HA starts)."""
    for _ in range(attempts):
        flow = request("POST", "/auth/login_flow", {
            "client_id": CLIENT_ID, "handler": ["homeassistant", None], "redirect_uri": CLIENT_ID,
        })
        result = request("POST", f"/auth/login_flow/{flow['flow_id']}", {
            "username": USERNAME, "password": PASSWORD, "client_id": CLIENT_ID,
        })
        if result.get("type") == "create_entry":
            return result["result"]
        time.sleep(5)
    sys.exit(f"Login as {USERNAME} failed: {result.get('errors')}. Was HA_PASSWORD changed?")


def main() -> None:
    steps = wait_for_ha()

    if steps and not steps.get("user"):
        code = request("POST", "/api/onboarding/users", {
            "client_id": CLIENT_ID, "name": "Administrator", "username": USERNAME,
            "password": PASSWORD, "language": "en",
        })["auth_code"]
        print(f"Created Home Assistant user '{USERNAME}'")
    else:
        code = login()
    token = request("POST", "/auth/token", {
        "grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID,
    }, form=True)["access_token"]

    for step, data in (("core_config", {}), ("analytics", {}),
                       ("integration", {"client_id": CLIENT_ID, "redirect_uri": CLIENT_ID})):
        if steps and not steps.get(step):
            request("POST", f"/api/onboarding/{step}", data, token=token)
    print("Onboarding complete")

    entries = request("GET", "/api/config/config_entries/entry?domain=mqtt", token=token)
    if entries:
        print("MQTT integration already configured")
        return
    flow = request("POST", "/api/config/config_entries/flow", {"handler": "mqtt"}, token=token)
    for _ in range(5):
        if flow["type"] == "create_entry":
            print(f"MQTT integration added (broker {MQTT_HOST}:{MQTT_PORT})")
            return
        if flow["type"] == "menu":
            data = {"next_step_id": "broker"}
        elif flow["type"] == "form" and flow["step_id"] == "broker":
            data = form_data(flow["data_schema"], {"broker": MQTT_HOST, "port": MQTT_PORT})
        else:
            sys.exit(f"Unexpected MQTT setup step: {flow}")
        flow = request("POST", f"/api/config/config_entries/flow/{flow['flow_id']}", data, token=token)
    sys.exit(f"MQTT setup did not finish: {flow}")


if __name__ == "__main__":
    try:
        main()
    except HAError as exc:
        sys.exit(str(exc))
