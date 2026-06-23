import asyncio
import os
import json
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

app = Flask(__name__)
ESP32_IP = os.getenv("ESP32_IP")
CONFIG_FILE = "config.json"
DEFAULT_CONFIG = {
    "angles": {"1": 180, "2": 180, "3": 180, "4": 180, "5": 180},
    "cameras": {"left": 0, "right": 1},
}


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
                if "angles" not in data:
                    return DEFAULT_CONFIG.copy()
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


class CameraManager:
    """Runs cameras in a background thread so web and telegram can share them without crashing."""

    def __init__(self):
        self.cameras = {}
        self.latest_frames = {}
        self.lock = threading.Lock()
        self.running = True
        self.thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.thread.start()

    def validate_camera(self, cam_id):
        """Checks if a camera hardware exists and can be opened."""
        cam_id = int(cam_id)
        with self.lock:
            if cam_id in self.cameras:
                return self.cameras[cam_id].isOpened()

            cap = cv2.VideoCapture(cam_id)
            if cap.isOpened():
                self.cameras[cam_id] = cap
                return True

            cap.release()
            return False

    def get_frame(self, cam_id):
        cam_id = int(cam_id)
        with self.lock:
            if cam_id not in self.cameras:
                cap = cv2.VideoCapture(cam_id)
                if cap.isOpened():
                    self.cameras[cam_id] = cap
        return self.latest_frames.get(cam_id)

    def _capture_loop(self):
        while self.running:
            with self.lock:
                cam_ids = list(self.cameras.keys())

            for cam_id in cam_ids:
                cap = self.cameras[cam_id]
                if cap.isOpened():
                    ret, frame = cap.read()
                    if ret:
                        self.latest_frames[cam_id] = frame
            time.sleep(0.03)


cam_manager = CameraManager()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/captures/<filename>")
def get_capture(filename):
    return send_from_directory("captures", filename)


@app.route("/set_angle", methods=["POST"])
def set_angle():
    data = request.get_json()
    servo = str(data.get("servo"))
    angle = int(data.get("angle"))
    if servo in ["1", "2", "3", "4", "5"] and 1 <= angle <= 180:
        app_config["angles"][servo] = angle
        save_config(app_config)
        return jsonify({"status": "ok"})
    return jsonify({"status": "error"}), 400


@app.route("/set_camera", methods=["POST"])
def set_camera():
    data = request.get_json()
    side = str(data.get("side"))
    cam_id = int(data.get("cam_id"))

    if side in ["left", "right"] and cam_id >= 0:
        if not cam_manager.validate_camera(cam_id):
            return (
                jsonify(
                    {"status": "error", "message": f"Camera ID {cam_id} not found."}
                ),
                400,
            )

        app_config["cameras"][side] = cam_id
        save_config(app_config)
        return jsonify({"status": "ok"})
    return jsonify({"status": "error", "message": "Invalid input"}), 400


@app.route("/get_config")
def get_config():
    return jsonify(app_config)


@app.route("/fire_servo")
def fire_servo():
    servo = request.args.get("servo")
    angle = request.args.get("angle")
    reset_angle = request.args.get("reset_angle", "0")
    url = f"http://{ESP32_IP}/activate?servo={servo}&angle={angle}&reset_angle={reset_angle}"
    try:
        with httpx.Client() as client:
            resp = client.get(url, timeout=5.0)
            return jsonify({"status": "ok", "esp32_status": resp.status_code})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


def gen_frames(camera_id):
    while True:
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
    if not cam_manager.validate_camera(camera_id):
        return "Camera offline", 404

    return Response(
        gen_frames(camera_id), mimetype="multipart/x-mixed-replace; boundary=frame"
    )


key_mapping = {
    "sh": "shinhan",
    "shinhan": "shinhan",
    "woori": "woori",
    "wr": "woori",
    "kfcc": "kfcc",
    "nh": "nh",
    "kakao": "kakao",
}

servo_mapping = {
    "shinhan": 1,
    "sh": 1,
    "kfcc": 2,
    "kakao": 3,
    "woori": 4,
    "wr": 4,
    "nh": 5,
}

processing_lock = None
tg_client = TelegramClient("login", os.getenv("API_ID"), os.getenv("API_HASH"))


async def trigger_servo(servo_number):
    angle = app_config["angles"].get(str(servo_number), 180)
    url = f"http://localhost:5000/fire_servo?servo={servo_number}&angle={angle}&reset_angle=0"
    async with httpx.AsyncClient() as http_client:
        try:
            response = await http_client.get(url, timeout=5.0)
            return response.status_code == 200
        except Exception:
            pass
    return False


@tg_client.on(events.NewMessage(chats="otp_22"))
async def handle_otp_requests(event):
    if event.out:
        return

    text = event.raw_text.strip().lower()
    if text not in key_mapping:
        return

    async with processing_lock:
        target_key = key_mapping[text]
        target_servo = servo_mapping[text]

        await trigger_servo(target_servo)
        await asyncio.sleep(1.0)

        camera_index = (
            int(app_config["cameras"]["left"])
            if target_servo in [3, 5]
            else int(app_config["cameras"]["right"])
        )

        frame = cam_manager.get_frame(camera_index)

        os.makedirs("captures", exist_ok=True)
        image_path = "captures/latest.jpg"
        if frame is not None:
            cv2.imwrite(image_path, frame)
        else:
            image_path = None

        otp_data = get_extracted_otps(image_path) if image_path else None
        reply_text = "null"
        if otp_data and target_key in otp_data and otp_data[target_key]:
            reply_text = str(otp_data[target_key]).strip()

        print(
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} replying '{reply_text}' from {target_key}"
        )
        await tg_client.send_message(event.chat_id, reply_text)


async def run_telegram():
    global processing_lock
    processing_lock = asyncio.Lock()
    await tg_client.start()
    await tg_client.run_until_disconnected()


def start_telegram_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_telegram())


if __name__ == "__main__":
    os.makedirs("captures", exist_ok=True)
    t = threading.Thread(target=start_telegram_thread, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
