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

DEFAULT_CONFIG = {
    "angles": {},
    "re_trigger": {},
    "invert": {},
    "servo_meta": {},
    "servo_sides": {},
    "servo_ids": None,
    "overlay_rects": {},
    "cameras": {"left": 0, "right": 1},
    "capture_delay_ms": 1000,
    "camera_resolution": None,
    "prompt_template": "",
    "models_config": [
        {"model": "gemini-3.1-flash-lite", "priority": True},
        {"model": "gemini-3.5-flash", "priority": True},
    ],
}
VALID_SIDES = {"left", "right"}
VALID_SERVOS: set[str] = set()
MAX_SERVOS = 16


def default_side(sid) -> str:
    """Initial camera side for a servo that has none saved: 1-4 left, 5+ right."""
    return "left" if int(sid) <= 4 else "right"


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
    for sid in servo_ids:
        config_dict["angles"].setdefault(sid, 180)
        config_dict["re_trigger"].setdefault(sid, False)
        config_dict.setdefault("invert", {}).setdefault(sid, False)
        config_dict["servo_meta"].setdefault(sid, {"name": "", "aliases": []})
        config_dict.setdefault("servo_sides", {}).setdefault(sid, default_side(sid))
    config_dict.setdefault("overlay_rects", {})


def normalize_overlay_rect(raw_rect):
    if not isinstance(raw_rect, dict):
        return None
    try:
        x = float(raw_rect.get("x", 0))
        y = float(raw_rect.get("y", 0))
        width = float(raw_rect.get("width", 0))
        height = float(raw_rect.get("height", 0))
    except (TypeError, ValueError):
        return None

    if not (0 <= x <= 100 and 0 <= y <= 100 and 1 <= width <= 100 and 1 <= height <= 100):
        return None

    return {"x": round(x, 2), "y": round(y, 2), "width": round(width, 2), "height": round(height, 2)}


def build_overlay_rects(config_dict, servo_ids) -> dict[str, dict]:
    lookup: dict[str, dict] = {}
    overlay_rects = config_dict.get("overlay_rects", {}) or {}
    servo_meta = config_dict.get("servo_meta", {}) or {}
    for sid in servo_ids:
        servo_key = str(sid)
        rect = normalize_overlay_rect(overlay_rects.get(servo_key))
        if rect is None:
            rect = {"x": 0, "y": 0, "width": 100, "height": 100}
        meta = servo_meta.get(servo_key, {}) or {}
        name = str(meta.get("name", "") or "").strip()
        aliases = [str(a).strip() for a in meta.get("aliases", []) if str(a).strip()]
        labels = []
        if name:
            labels.append(name)
        labels.extend(aliases)
        labels.append(f"servo{sid}")
        for label in labels:
            lookup[re.sub(r"[^a-z0-9]", "", label.lower())] = rect
    return lookup


app_config = load_config()


# ── fetch servo count from ESP32 ───────────────────────────────────────────────


def fetch_servo_count() -> int | None:
    try:
        with httpx.Client() as client:
            resp = client.get(f"http://{ESP32_IP}/servo_count", timeout=5.0)
            return int(resp.json()["count"])
    except Exception as exc:
        logging.warning("Could not fetch servo count from ESP32 (%s)", exc)
        return None


def apply_servo_ids(ids):
    """Set the active servo ids (they need not be contiguous: deleting servo 2
    leaves 1, 3, 4... so every other servo keeps its ESP32 channel)."""
    global VALID_SERVOS
    ids = sorted({str(i) for i in ids}, key=int)
    VALID_SERVOS = set(ids)
    ensure_servo_slots(app_config, ids)


def active_servo_ids() -> list[str]:
    return sorted(VALID_SERVOS, key=int)


# Ids saved from the UI (Add/Delete OTP) are authoritative. Configs from the
# previous version only have a servo_count, so those become ids 1..count.
# With nothing saved yet, use what the ESP32 reports (else 5).
_saved_ids = app_config.get("servo_ids")
if _saved_ids:
    _start_ids = [str(i) for i in _saved_ids]
else:
    _start_count = int(app_config.get("servo_count") or 0) or (fetch_servo_count() or 0)
    if _start_count == 0:
        logging.warning("No servo count from ESP32 or config; defaulting to 5")
        _start_count = 5
    _start_ids = _servo_ids(_start_count)
apply_servo_ids(_start_ids)
save_config(app_config)


# ── camera manager ─────────────────────────────────────────────────────────────


def _configure_capture(cap):
    """Apply the saved resolution (None = leave the camera at its default).
    MJPG is requested too: many USB webcams only offer 720p/1080p in MJPG."""
    res = app_config.get("camera_resolution")
    if not res:
        return
    try:
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(res["width"]))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(res["height"]))
    except Exception:
        logging.exception("Could not apply camera resolution %s", res)


class CameraManager:
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
                _configure_capture(cap)
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
                    _configure_capture(cap)
                    self.cameras[cam_id] = cap
        return self.latest_frames.get(cam_id)

    def reconfigure_all(self):
        """Reopen every open camera so a new resolution takes effect."""
        with self.lock:
            cam_ids = list(self.cameras.keys())
        for cam_id in cam_ids:
            self.release_camera(cam_id)
            self.open_camera(cam_id)

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
    payload = dict(app_config)
    payload["servo_ids"] = [int(s) for s in active_servo_ids()]
    payload["servo_count"] = len(VALID_SERVOS)
    return jsonify(payload)


@app.route("/servo_count")
def servo_count_route():
    return jsonify({"count": len(VALID_SERVOS)})


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


@app.route("/set_invert", methods=["POST"])
def set_invert():
    data = request.get_json()
    servo = str(data.get("servo"))
    invert = bool(data.get("invert", False))

    if servo not in VALID_SERVOS:
        return jsonify({"status": "error", "message": "Invalid servo ID"}), 400

    app_config.setdefault("invert", {})[servo] = invert
    save_config(app_config)
    return jsonify({"status": "ok"})


@app.route("/capture_servo", methods=["POST"])
def capture_servo():
    data = request.get_json(silent=True) or {}
    servo = str(data.get("servo"))
    if servo not in VALID_SERVOS:
        return jsonify({"status": "error", "message": "Invalid servo ID"}), 400

    token = next(
        (tok for tok in _build_token_lookup().values() if tok["servo"] == int(servo)),
        None,
    )
    image_path = _capture_processed_frame(token) if token else None
    if image_path is None:
        return jsonify({"status": "error", "message": "No camera frame available (is the camera open?)"}), 503
    return jsonify({"status": "ok"})


@app.route("/set_servo_meta", methods=["POST"])
def set_servo_meta():
    data = request.get_json()
    servo = str(data.get("servo"))

    if servo not in VALID_SERVOS:
        return jsonify({"status": "error", "message": "Invalid servo ID"}), 400

    meta = app_config["servo_meta"].setdefault(servo, {"name": "", "aliases": []})

    if "name" in data:
        meta["name"] = str(data["name"]).strip()

    if "aliases" in data:
        raw = data["aliases"]
        if isinstance(raw, list):
            aliases = [a.strip() for a in raw if str(a).strip()]
        else:
            aliases = [a.strip() for a in str(raw).split(",") if a.strip()]
        meta["aliases"] = aliases

    save_config(app_config)
    return jsonify({"status": "ok"})


@app.route("/set_servo_side", methods=["POST"])
def set_servo_side():
    data = request.get_json() or {}
    servo = str(data.get("servo"))
    side = str(data.get("side"))

    if servo not in VALID_SERVOS:
        return jsonify({"status": "error", "message": "Invalid servo ID"}), 400
    if side not in VALID_SIDES:
        return jsonify({"status": "error", "message": "Side must be left or right"}), 400

    app_config.setdefault("servo_sides", {})[servo] = side
    save_config(app_config)
    return jsonify({"status": "ok"})


@app.route("/add_servo", methods=["POST"])
def add_servo():
    data = request.get_json(silent=True) or {}
    side = str(data.get("side", "right"))
    if side not in VALID_SIDES:
        return jsonify({"status": "error", "message": "Side must be left or right"}), 400

    # take the lowest free number, so a deleted servo can be brought back
    used = {int(s) for s in VALID_SERVOS}
    new_id = next((i for i in range(1, MAX_SERVOS + 1) if i not in used), None)
    if new_id is None:
        return jsonify({"status": "error", "message": f"Maximum of {MAX_SERVOS} OTPs reached"}), 400

    ids = active_servo_ids() + [str(new_id)]
    apply_servo_ids(ids)
    app_config["servo_sides"][str(new_id)] = side
    app_config["servo_ids"] = sorted(VALID_SERVOS, key=int)
    save_config(app_config)
    return jsonify({"status": "ok", "servo": new_id, "side": side})


@app.route("/remove_servo", methods=["POST"])
def remove_servo():
    data = request.get_json(silent=True) or {}
    servo = str(data.get("servo"))

    if servo not in VALID_SERVOS:
        return jsonify({"status": "error", "message": "Invalid servo ID"}), 400
    if len(VALID_SERVOS) <= 1:
        return jsonify({"status": "error", "message": "At least one OTP is required"}), 400

    # Renumber so the list stays 1..N: everything after the deleted servo moves
    # up one (its name, aliases, angle, side, crop rect and re-trigger go with it).
    remaining = [s for s in active_servo_ids() if s != servo]
    mapping = {old: str(i + 1) for i, old in enumerate(remaining)}
    for key in ("angles", "re_trigger", "invert", "servo_meta", "servo_sides", "overlay_rects"):
        section = app_config.setdefault(key, {})
        shifted = {mapping[old]: section[old] for old in remaining if old in section}
        section.clear()
        section.update(shifted)

    new_ids = sorted(mapping.values(), key=int)
    apply_servo_ids(new_ids)
    app_config["servo_ids"] = new_ids
    save_config(app_config)
    return jsonify({"status": "ok", "servo_ids": [int(s) for s in new_ids]})


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


@app.route("/set_overlay_rect", methods=["POST"])
def set_overlay_rect():
    data = request.get_json()
    servo = str(data.get("servo"))

    if servo not in VALID_SERVOS:
        return jsonify({"status": "error", "message": "Invalid servo ID"}), 400

    raw_rect = data.get("rect")
    if raw_rect in (None, ""):
        app_config.setdefault("overlay_rects", {}).pop(servo, None)
        save_config(app_config)
        return jsonify({"status": "ok"})

    rect = normalize_overlay_rect(raw_rect)
    if rect is None:
        return jsonify({"status": "error", "message": "Use x%, y%, w%, h% values"}), 400

    app_config.setdefault("overlay_rects", {})[servo] = rect
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


RESOLUTIONS = {
    "default": None,
    "640x480": (640, 480),
    "1280x720": (1280, 720),
    "1920x1080": (1920, 1080),
}


@app.route("/set_resolution", methods=["POST"])
def set_resolution():
    data = request.get_json(silent=True) or {}
    key = str(data.get("resolution", "default"))
    if key not in RESOLUTIONS:
        return jsonify({"status": "error", "message": "Unknown resolution"}), 400

    size = RESOLUTIONS[key]
    app_config["camera_resolution"] = (
        {"width": size[0], "height": size[1]} if size else None
    )
    save_config(app_config)
    cam_manager.reconfigure_all()
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
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Invalid angle value"}), 400

    if servo not in VALID_SERVOS or not (1 <= angle <= 180):
        return (
            jsonify(
                {"status": "error", "message": "Invalid servo or angle parameters"}
            ),
            400,
        )

    url = f"http://{ESP32_IP}/activate?servo={servo}&angle={angle}"
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

def _build_token_lookup():
    lookup: dict[str, dict] = {}
    for sid in sorted(VALID_SERVOS, key=int):
        meta = app_config.get("servo_meta", {}).get(sid, {})
        name = meta.get("name", "").strip()
        aliases = [a.strip().lower() for a in meta.get("aliases", []) if a.strip()]
        key = re.sub(r"[^a-z0-9]", "", name.lower()) if name else f"servo{sid}"
        camera_side = app_config.get("servo_sides", {}).get(sid) or default_side(sid)
        token = {"key": key, "servo": int(sid), "camera": camera_side}
        for alias in [key] + aliases:
            clean = re.sub(r"[^a-z0-9]", "", alias)
            if clean:
                lookup[clean] = token
    return lookup


def match_token(message: str):
    lookup = _build_token_lookup()
    clean = re.sub(r"[^a-z0-9]", "", message.strip().lower())
    return lookup.get(clean)


def _build_servo_names() -> dict[str, str]:
    return {
        sid: (
            app_config.get("servo_meta", {}).get(sid, {}).get("name", "").strip()
            or f"servo{sid}"
        )
        for sid in sorted(VALID_SERVOS, key=int)
    }


def _needs_visibility_retry(otp_data, target_key: str) -> bool:
    if not isinstance(otp_data, dict):
        return True

    visibility_key = f"{target_key}_isVisible"
    if visibility_key not in otp_data or not otp_data[visibility_key]:
        return True
    return False


def _apply_overlay_rect(frame, rect):
    if frame is None or not isinstance(rect, dict):
        return frame

    height, width = frame.shape[:2]
    try:
        x = int(round(width * (rect.get("x", 0) / 100.0)))
        y = int(round(height * (rect.get("y", 0) / 100.0)))
        rect_width = int(round(width * (rect.get("width", 0) / 100.0)))
        rect_height = int(round(height * (rect.get("height", 0) / 100.0)))
    except (TypeError, ValueError):
        return frame

    x = max(0, min(width, x))
    y = max(0, min(height, y))
    rect_width = max(1, min(width - x, rect_width))
    rect_height = max(1, min(height - y, rect_height))
    return frame[y : y + rect_height, x : x + rect_width]


def _capture_processed_frame(token):
    """Grab the current camera frame, apply this OTP's crop + invert, and save it
    to captures/latest.jpg. Returns the path, or None if there is no frame."""
    camera_index = int(app_config["cameras"][token["camera"]])
    frame = cam_manager.get_frame(camera_index)

    overlay_lookup = build_overlay_rects(app_config, active_servo_ids())
    rect = None
    if token.get("key"):
        rect = overlay_lookup.get(token["key"])
    if rect is None:
        rect = normalize_overlay_rect(
            app_config.get("overlay_rects", {}).get(str(token["servo"]))
        )
    if rect is not None and frame is not None:
        frame = _apply_overlay_rect(frame, rect)

    if frame is not None and app_config.get("invert", {}).get(str(token["servo"]), False):
        frame = cv2.bitwise_not(frame)

    os.makedirs("captures", exist_ok=True)
    image_path = "captures/latest.jpg"
    if frame is not None:
        cv2.imwrite(image_path, frame)
    else:
        image_path = None
    return image_path


async def _capture_and_extract_otp(token):
    await trigger_servo(token["servo"])

    capture_delay_s = app_config.get("capture_delay_ms", 1000) / 1000.0
    await asyncio.sleep(capture_delay_s)

    image_path = _capture_processed_frame(token)

    otp_data = None
    if image_path:
        template = app_config.get("prompt_template") or None
        otp_data = get_extracted_otps(
            image_path,
            token["key"],
            template,
            _build_servo_names(),
            models_config=app_config.get("models_config", None),
        )

    return image_path, otp_data


async def _send_capture_prompt(event, token, image_path):
    caption = (
        f"OTP extraction failed. There may be an issue with {token['key']}.\n"
        f" If you get this message, just request the OTP again. It means the camera didn't capture the OTP device properly."
    )
    try:
        await tg_client.send_message(event.chat_id, caption, reply_to=event.id)
    except Exception:
        logging.exception(
            "Failed to send Telegram capture prompt for token %s", token["key"]
        )


# ── telegram ───────────────────────────────────────────────────────────────────

processing_lock = None
tg_client = TelegramClient("login", os.getenv("API_ID"), os.getenv("API_HASH"))
group_chat_id = int(os.getenv("GROUP_CHAT_ID"))


async def trigger_servo(servo_number):
    angle = app_config["angles"].get(str(servo_number), 180)
    url = f"http://localhost:5000/fire_servo?servo={servo_number}&angle={angle}"
    async with httpx.AsyncClient() as http_client:
        try:
            response = await http_client.get(url, timeout=10.0)
            return response.status_code == 200
        except Exception:
            logging.exception("Failed to trigger servo %s", servo_number)
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

    reply_text = None
    image_path = None
    async with processing_lock:
        try:
            for attempt in range(3):
                image_path, otp_data = await _capture_and_extract_otp(token)
                if otp_data and token["key"] in otp_data and otp_data[token["key"]]:
                    reply_text = str(otp_data[token["key"]]).strip()
                    break
                if _needs_visibility_retry(otp_data, token["key"]):
                    logging.info(
                        "Retrying OTP extraction for token %s (attempt %d/3) because it was not visible/readable.",
                        token["key"],
                        attempt + 1,
                    )
                    continue
                break
        except Exception:
            logging.exception("OTP processing failed for token %s", token["key"])

        if reply_text is not None:
            print(
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} replying '{reply_text}' from {token['key']}"
            )
            try:
                await tg_client.send_message(event.chat_id, reply_text)
            except Exception:
                logging.exception(
                    "Failed to send Telegram reply for token %s", token["key"]
                )
        else:
            print(
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} sending capture prompt for {token['key']}"
            )
            await _send_capture_prompt(event, token, image_path)

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