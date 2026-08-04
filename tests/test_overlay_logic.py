import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

fake_gemini = types.ModuleType("gemini")
fake_gemini.get_extracted_otps = lambda *args, **kwargs: None
fake_gemini.DEFAULT_PROMPT_TEMPLATE = ""
fake_gemini.render_prompt = lambda *args, **kwargs: ""
sys.modules["gemini"] = fake_gemini

fake_dotenv = types.ModuleType("dotenv")
fake_dotenv.load_dotenv = lambda *args, **kwargs: None
sys.modules["dotenv"] = fake_dotenv

fake_telethon = types.ModuleType("telethon")
fake_events = types.SimpleNamespace(NewMessage=object)
fake_telethon.events = fake_events

class DummyTelegramClient:
    def __init__(self, *args, **kwargs):
        pass

    def on(self, *args, **kwargs):
        def decorator(func):
            return func

        return decorator

    async def start(self):
        return None

    async def run_until_disconnected(self):
        return None


fake_telethon.TelegramClient = DummyTelegramClient
sys.modules["telethon"] = fake_telethon

spec = importlib.util.spec_from_file_location("app_module", ROOT / "app.py")
app_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_module)


def test_overlay_rect_matches_named_device():
    config = {
        "servo_meta": {
            "1": {"name": "Woori", "aliases": ["wr"]},
            "2": {"name": "", "aliases": []},
        },
        "overlay_rects": {
            "1": {"x": 10, "y": 10, "width": 20, "height": 30},
        },
    }
    overlay = app_module.build_overlay_rects(config, 2)

    assert overlay["woori"] == {"x": 10, "y": 10, "width": 20, "height": 30}
    assert overlay["wr"] == {"x": 10, "y": 10, "width": 20, "height": 30}
    assert overlay["servo2"] == {"x": 0, "y": 0, "width": 100, "height": 100}
