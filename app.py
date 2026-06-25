import asyncio
import logging
import os
import json
import re
import threading
import time
import cv2
import httpx
from datetime import datetime
from flask import (
    Flask,
    Response,
    render_template,
    request,
    jsonify,
    send_from_directory,
)
from dotenv import load_dotenv
from gemini import get_extracted_otps, DEFAULT_PROMPT_TEMPLATE, render_prompt
from telethon import TelegramClient, events

load_dotenv()

# ── silence noisy Werkzeug routes ─────────────────────────────────────────────


class SilenceRoutes(logging.Filter):
    SILENT = ("/captures/latest.jpg", "/video_feed/")

    def filter(self, record):
        msg = record.getMessage()
        return not any(route in msg for route in self.SILENT)


logging.getLogger("werkzeug").addFilter(SilenceRoutes())

# ── app setup ──────────────────────────────────────────────────────────────────

app = Flask(__name__)
ESP32_IP = os.getenv("ESP32_IP")
CONFIG_FILE = "config.json"

# servo_meta shape: { "1": { "name": "Shinhan", "aliases": ["sh", "shinshan"] }, ... }
DEFAULT_CONFIG = {
    "angles": {},
    "re_trigger": {},
    "servo_meta": {},
    "cameras": {"left": 0, "right": 1},
    "capture_delay_ms": 1000,
    "prompt_template": "",  # empty = use DEFAULT_PROMPT_TEMPLATE in gemini.py
    "models_config": [
        {"model": "gemini-3.1-flash-lite", "priority": True},
        {"model": "gemini-3.5-flash", "priority": True},
    ],
}
VALID_SIDES = {"left", "right"}

# populated at startup once we know the servo count
VALID_SERVOS: set[str] = set()


def _servo_ids(count: int) -> list[str]:
    return [str(i) for i in range(1, count + 1)]


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
            for key, val in DEFAULT_CONFIG.items():
                if key not in data:
                    data[key] = val
            return data
        except Exception:
            pass
    with open(CONFIG_FILE, "w") as f:
        json.dump(DEFAULT_CONFIG, f, indent=4)
    return DEFAULT_CONFIG.copy()


def save_config(config_dict):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(config_dict, f, indent=4)
    except Exception:
        pass


def ensure_servo_slots(config_dict, servo_ids: list[str]):
    """Guarantee that angles / re_trigger / servo_meta have an entry for every servo."""
    for sid in servo_ids:
        config_dict["angles"].setdefault(sid, 180)
        config_dict["re_trigger"].setdefault(sid, False)
        config_dict["servo_meta"].setdefault(sid, {"name": "", "aliases": []})


app_config = load_config()


# ── fetch servo count from ESP32 ───────────────────────────────────────────────


def fetch_servo_count() -> int:
    """Ask the ESP32 how many servos it has.  Falls back to 5 on any error."""
    try:
        with httpx.Client() as client:
            resp = client.get(f"http://{ESP32_IP}/servo_count", timeout=5.0)
            return int(resp.json()["count"])
    except Exception as exc:
        logging.warning(
            "Could not fetch servo count from ESP32 (%s); defaulting to 5", exc
        )
        return 5


_servo_count = fetch_servo_count()
VALID_SERVOS = set(_servo_ids(_servo_count))
ensure_servo_slots(app_config, _servo_ids(_servo_count))
save_config(app_config)


# ── camera manager ─────────────────────────────────────────────────────────────


class CameraManager:
    """Runs cameras in a background thread so web and telegram can share them."""

    def __init__(self):
        self.cameras = {}
        self.latest_frames = {}
        self.lock = threading.Lock()
        self.running = True
        self.thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.thread.start()

    def open_camera(self, cam_id):
        cam_id = int(cam_id)
        with self.lock:
            if cam_id in self.cameras:
                if self.cameras[cam_id].isOpened():
                    return True
                self.cameras[cam_id].release()
                del self.cameras[cam_id]
            cap = cv2.VideoCapture(cam_id)
            if cap.isOpened():
                self.cameras[cam_id] = cap
                return True
            cap.release()
            return False

    def release_camera(self, cam_id):
        cam_id = int(cam_id)
        with self.lock:
            if cam_id in self.cameras:
                self.cameras[cam_id].release()
                del self.cameras[cam_id]
            self.latest_frames.pop(cam_id, None)

    def get_frame(self, cam_id):
        cam_id = int(cam_id)
        with self.lock:
            if cam_id not in self.cameras:
                cap = cv2.VideoCapture(cam_id)
                if cap.isOpened():
                    self.cameras[cam_id] = cap
        return self.latest_frames.get(cam_id)

    def is_open(self, cam_id):
        cam_id = int(cam_id)
        with self.lock:
            return cam_id in self.cameras and self.cameras[cam_id].isOpened()

    def _capture_loop(self):
        while self.running:
            with self.lock:
                cam_ids = list(self.cameras.keys())
            for cam_id in cam_ids:
                with self.lock:
                    cap = self.cameras.get(cam_id)
                if cap and cap.isOpened():
                    ret, frame = cap.read()
                    if ret:
                        self.latest_frames[cam_id] = frame
            time.sleep(0.03)


cam_manager = CameraManager()

for _side in VALID_SIDES:
    _cam_id = app_config["cameras"].get(_side, 0)
    cam_manager.open_camera(_cam_id)


# ── flask routes ───────────────────────────────────────────────────────────────


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/captures/<filename>")
def get_capture(filename):
    return send_from_directory("captures", filename)


@app.route("/get_config")
def get_config():
    # Always include the live servo count so the frontend is in sync
    payload = dict(app_config)
    payload["servo_count"] = _servo_count
    return jsonify(payload)


@app.route("/servo_count")
def servo_count_route():
    return jsonify({"count": _servo_count})


@app.route("/set_angle", methods=["POST"])
def set_angle():
    data = request.get_json()
    servo = str(data.get("servo"))
    try:
        angle = int(data.get("angle"))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Invalid angle value"}), 400

    if servo not in VALID_SERVOS or not (1 <= angle <= 180):
        return (
            jsonify({"status": "error", "message": "Angle must be between 1 and 180"}),
            400,
        )

    app_config["angles"][servo] = angle
    save_config(app_config)
    return jsonify({"status": "ok"})


@app.route("/set_re_trigger", methods=["POST"])
def set_re_trigger():
    data = request.get_json()
    servo = str(data.get("servo"))
    re_trigger = bool(data.get("re_trigger", False))

    if servo not in VALID_SERVOS:
        return jsonify({"status": "error", "message": "Invalid servo ID"}), 400

    app_config["re_trigger"][servo] = re_trigger
    save_config(app_config)
    return jsonify({"status": "ok"})


@app.route("/set_servo_meta", methods=["POST"])
def set_servo_meta():
    """Update the display name and/or aliases for a single servo."""
    data = request.get_json()
    servo = str(data.get("servo"))

    if servo not in VALID_SERVOS:
        return jsonify({"status": "error", "message": "Invalid servo ID"}), 400

    meta = app_config["servo_meta"].setdefault(servo, {"name": "", "aliases": []})

    if "name" in data:
        meta["name"] = str(data["name"]).strip()

    if "aliases" in data:
        # Accept either a list or a comma-separated string
        raw = data["aliases"]
        if isinstance(raw, list):
            aliases = [a.strip() for a in raw if str(a).strip()]
        else:
            aliases = [a.strip() for a in str(raw).split(",") if a.strip()]
        meta["aliases"] = aliases

    save_config(app_config)
    return jsonify({"status": "ok"})


@app.route("/set_capture_delay", methods=["POST"])
def set_capture_delay():
    data = request.get_json()
    try:
        delay = int(data.get("delay_ms"))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Invalid delay value"}), 400

    if not (1000 <= delay <= 5000):
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Delay must be between 1000ms and 5000ms",
                }
            ),
            400,
        )

    app_config["capture_delay_ms"] = delay
    save_config(app_config)
    return jsonify({"status": "ok"})


@app.route("/get_prompt")
def get_prompt():
    template = app_config.get("prompt_template", "") or ""
    default = DEFAULT_PROMPT_TEMPLATE
    return jsonify({"prompt_template": template, "default_template": default})


@app.route("/set_prompt", methods=["POST"])
def set_prompt():
    data = request.get_json()
    template = data.get("prompt_template", "")
    if not isinstance(template, str):
        return jsonify({"status": "error", "message": "Invalid prompt value"}), 400
    # empty string = revert to default
    app_config["prompt_template"] = template.strip()
    save_config(app_config)
    return jsonify({"status": "ok"})


@app.route("/set_models_config", methods=["POST"])
def set_models_config():
    data = request.get_json()
    models_config = data.get("models_config")
    if not isinstance(models_config, list):
        return (
            jsonify(
                {"status": "error", "message": "Invalid models configuration format"}
            ),
            400,
        )

    for item in models_config:
        if not isinstance(item, dict) or "model" not in item or "priority" not in item:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Each model item must contain model and priority",
                    }
                ),
                400,
            )

    app_config["models_config"] = models_config
    save_config(app_config)
    return jsonify({"status": "ok"})


@app.route("/set_camera", methods=["POST"])
def set_camera():
    data = request.get_json()
    side = str(data.get("side"))
    try:
        cam_id = int(data.get("cam_id"))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Invalid camera ID value"}), 400

    if side not in VALID_SIDES or not (0 <= cam_id <= 3):
        return (
            jsonify(
                {"status": "error", "message": "Camera ID must be between 0 and 3"}
            ),
            400,
        )

    app_config["cameras"][side] = cam_id
    save_config(app_config)
    cam_manager.open_camera(cam_id)
    return jsonify({"status": "ok"})


@app.route("/release_camera", methods=["POST"])
def release_camera():
    data = request.get_json()
    try:
        cam_id = int(data.get("cam_id"))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Invalid camera ID value"}), 400

    cam_manager.release_camera(cam_id)
    return jsonify({"status": "ok"})


@app.route("/fire_servo")
def fire_servo():
    servo = request.args.get("servo")
    try:
        angle = int(request.args.get("angle"))
        reset_angle = int(request.args.get("reset_angle", 0))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Invalid angle value"}), 400

    if (
        servo not in VALID_SERVOS
        or not (1 <= angle <= 180)
        or not (0 <= reset_angle <= 180)
    ):
        return (
            jsonify(
                {"status": "error", "message": "Invalid servo or angle parameters"}
            ),
            400,
        )

    url = f"http://{ESP32_IP}/activate?servo={servo}&angle={angle}&reset_angle={reset_angle}"
    try:
        with httpx.Client() as client:
            resp = client.get(url, timeout=10.0)
            return jsonify({"status": "ok", "esp32_status": resp.status_code})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


def gen_frames(camera_id):
    while True:
        if not cam_manager.is_open(camera_id):
            break
        frame = cam_manager.get_frame(camera_id)
        if frame is not None:
            ret, buffer = cv2.imencode(".jpg", frame)
            if ret:
                yield (
                    b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                    + buffer.tobytes()
                    + b"\r\n"
                )
        time.sleep(0.05)


@app.route("/video_feed/<camera_id>")
def video_feed(camera_id):
    if not cam_manager.is_open(camera_id):
        if not cam_manager.open_camera(camera_id):
            return "Camera offline", 404
    return Response(
        gen_frames(int(camera_id)), mimetype="multipart/x-mixed-replace; boundary=frame"
    )


# ── token matching ─────────────────────────────────────────────────────────────

# Hardcoded camera side mapping (servo id → which camera to use for capture).
# Edit this if your physical setup changes.
SERVO_CAMERA_SIDE = {
    "1": "right",
    "2": "right",
    "3": "left",
    "4": "right",
    "5": "left",
}


def _build_token_lookup():
    """Build O(1) alias → servo dict from live config."""
    lookup: dict[str, dict] = {}
    for sid in sorted(VALID_SERVOS, key=int):
        meta = app_config.get("servo_meta", {}).get(sid, {})
        name = meta.get("name", "").strip()
        aliases = [a.strip().lower() for a in meta.get("aliases", []) if a.strip()]
        key = re.sub(r"[^a-z0-9]", "", name.lower()) if name else f"servo{sid}"
        camera_side = SERVO_CAMERA_SIDE.get(sid, "left")
        token = {"key": key, "servo": int(sid), "camera": camera_side}
        for alias in [key] + aliases:
            clean = re.sub(r"[^a-z]", "", alias)
            if clean:
                lookup[clean] = token
    return lookup


def match_token(message: str):
    lookup = _build_token_lookup()
    clean = re.sub(r"[^a-z]", "", message.strip().lower())
    return lookup.get(clean)


# ── telegram ───────────────────────────────────────────────────────────────────

processing_lock = None
tg_client = TelegramClient("login", os.getenv("API_ID"), os.getenv("API_HASH"))
group_chat_id = int(os.getenv("GROUP_CHAT_ID"))


async def trigger_servo(servo_number):
    angle = app_config["angles"].get(str(servo_number), 180)
    url = f"http://localhost:5000/fire_servo?servo={servo_number}&angle={angle}&reset_angle=0"
    async with httpx.AsyncClient() as http_client:
        try:
            response = await http_client.get(url, timeout=10.0)
            return response.status_code == 200
        except Exception:
            return False


@tg_client.on(events.NewMessage)
async def handle_otp_requests(event):
    if event.chat_id != group_chat_id:
        return
    if event.out:
        return

    token = match_token(event.raw_text)
    if token is None:
        return

    async with processing_lock:
        await trigger_servo(token["servo"])

        capture_delay_s = app_config.get("capture_delay_ms", 1000) / 1000.0
        await asyncio.sleep(capture_delay_s)

        camera_index = int(app_config["cameras"][token["camera"]])
        frame = cam_manager.get_frame(camera_index)

        os.makedirs("captures", exist_ok=True)
        image_path = "captures/latest.jpg"
        if frame is not None:
            cv2.imwrite(image_path, frame)
        else:
            image_path = None

        otp_data = None
        if image_path:
            template = app_config.get("prompt_template") or None
            servo_names = {
                sid: (
                    app_config.get("servo_meta", {})
                    .get(sid, {})
                    .get("name", "")
                    .strip()
                    or f"servo{sid}"
                )
                for sid in sorted(VALID_SERVOS, key=int)
            }
            models_config = app_config.get("models_config", None)
            otp_data = get_extracted_otps(
                image_path,
                token["key"],
                template,
                servo_names,
                models_config=models_config,
            )
        reply_text = "null"
        if otp_data and token["key"] in otp_data and otp_data[token["key"]]:
            reply_text = str(otp_data[token["key"]]).strip()

        print(
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} replying '{reply_text}' from {token['key']}"
        )
        await tg_client.send_message(event.chat_id, reply_text)

        if app_config.get("re_trigger", {}).get(str(token["servo"]), False):
            await asyncio.sleep(0.5)
            await trigger_servo(token["servo"])


async def run_telegram():
    global processing_lock
    processing_lock = asyncio.Lock()
    await tg_client.start()
    await tg_client.run_until_disconnected()


def start_telegram_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_telegram())


# ── entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    os.makedirs("captures", exist_ok=True)
    t = threading.Thread(target=start_telegram_thread, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
