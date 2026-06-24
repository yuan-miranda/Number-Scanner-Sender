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
from gemini import get_extracted_otps
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
DEFAULT_CONFIG = {
    "angles": {"1": 180, "2": 180, "3": 180, "4": 180, "5": 180},
    "re_trigger": {"1": False, "2": False, "3": False, "4": False, "5": False},
    "cameras": {"left": 0, "right": 1},
    "capture_delay_ms": 1000,
}
VALID_SERVOS = {"1", "2", "3", "4", "5"}
VALID_SIDES = {"left", "right"}


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
            if "angles" not in data:
                return DEFAULT_CONFIG.copy()
            for key, val in DEFAULT_CONFIG.items():
                if key not in data:
                    data[key] = val
            for servo_id in VALID_SERVOS:
                data["re_trigger"].setdefault(servo_id, False)
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


app_config = load_config()


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
    return jsonify(app_config)


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

TOKENS = [
    {"key": "shinhan", "servo": 1, "camera": "right", "aliases": ["sh", "shinshan"]},
    {"key": "kfcc", "servo": 2, "camera": "right", "aliases": ["kfcc", "kfc"]},
    {"key": "kakao", "servo": 3, "camera": "left", "aliases": ["kakao", "cacao"]},
    {
        "key": "woori",
        "servo": 4,
        "camera": "right",
        "aliases": ["wr", "woori", "wori", "wuri"],
    },
    {"key": "nh", "servo": 5, "camera": "left", "aliases": ["nh"]},
]

# Build a flat lookup dict for O(1) matching: alias/key → token
_TOKEN_LOOKUP = {
    alias: token
    for token in TOKENS
    for alias in ([token["key"]] + token.get("aliases", []))
}


def match_token(message):
    clean = re.sub(r"[^a-z]", "", message.strip().lower())
    return _TOKEN_LOOKUP.get(clean)


# ── telegram ───────────────────────────────────────────────────────────────────

processing_lock = None
tg_client = TelegramClient("login", os.getenv("API_ID"), os.getenv("API_HASH"))


async def trigger_servo(servo_number):
    angle = app_config["angles"].get(str(servo_number), 180)
    url = f"http://localhost:5000/fire_servo?servo={servo_number}&angle={angle}&reset_angle=0"
    async with httpx.AsyncClient() as http_client:
        try:
            response = await http_client.get(url, timeout=10.0)
            return response.status_code == 200
        except Exception:
            return False


@tg_client.on(events.NewMessage(chats="otp_22"))
async def handle_otp_requests(event):
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

        otp_data = get_extracted_otps(image_path) if image_path else None
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
