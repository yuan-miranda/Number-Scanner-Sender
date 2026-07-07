import json
import os
from dotenv import load_dotenv
from google import genai
from google.genai import types
from PIL import Image
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

load_dotenv()


DEFAULT_PROMPT_TEMPLATE = (
    "Extract the 6-digit OTP code strictly for the '{target_key}' token into a JSON object.\n"
    "Always include an 'isVisible' boolean for the target device.\n"
    "For the camera view showing THREE OTP devices:\n"
    "- '{servo1}': The BOTTOM display (labeled '{servo1}').\n"
    "- '{servo2}': The CENTER display (also labelled as '{servo1}' but on the center of them three).\n"
    "- '{servo3}': The TOP display (labeled 'kakaobank').\n"
    "For the camera view showing TWO OTP displays:\n"
    "- '{servo4}': The BOTTOM display (purple with cartoon).\n"
    "- '{servo5}': The TOP display (labelled '{servo1}' and has also label of 'choi su hyun woori').\n\n"
    "Rules:\n"
    "- Only extract the 6-digit number visible on the device screen for '{target_key}'.\n"
    "- If the '{target_key}' device is completely missing, or its screen is blank/off, return null.\n"
    "- Output MUST strictly use this exact key: '{target_key}'."
)


def render_prompt(template: str, servo_names: dict, target_key: str) -> str:
    result = template
    for sid, name in servo_names.items():
        result = result.replace(f"{{servo{sid}}}", name)
    return result.replace("{target_key}", target_key)


def _build_config(
    model: str, is_priority: bool, target_key: str, instructions: str
) -> types.GenerateContentConfig:
    return types.GenerateContentConfig(
        system_instruction=instructions,
        temperature=0.0,
        response_mime_type="application/json",
        service_tier="priority" if is_priority else None,
        response_schema=types.Schema(
            type=types.Type.OBJECT,
            properties={
                target_key: types.Schema(type=types.Type.STRING, nullable=True),
                "isVisible": types.Schema(type=types.Type.BOOLEAN),
            },
            required=[target_key],
        ),
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def get_extracted_otps(
    image_path, target_key, prompt_template=None, servo_names=None, models_config=None
):
    client = genai.Client(api_key=os.getenv("API_KEY"))

    image = Image.open(image_path)
    image.thumbnail((768, 768))

    instructions = render_prompt(
        prompt_template or DEFAULT_PROMPT_TEMPLATE,
        servo_names or {},
        target_key,
    )

    if models_config and isinstance(models_config, list) and len(models_config) > 0:
        models_to_run = [
            (m["model"], m.get("priority", False))
            for m in models_config
            if m.get("model")
        ]
    else:
        models_to_run = [("gemini-3.1-flash-lite", True), ("gemini-3.5-flash", True)]

    last_exception = None
    for idx, (model, is_priority) in enumerate(models_to_run):
        config = _build_config(model, is_priority, target_key, instructions)
        try:
            response = client.models.generate_content(
                model=model,
                contents=[image],
                config=config,
            )
            return json.loads(response.text)
        except Exception as e:
            next_model = (
                models_to_run[idx + 1][0] if idx + 1 < len(models_to_run) else "none"
            )
            print(f"[gemini] {model} failed: {e}. Falling back to {next_model}")
            last_exception = e

    raise last_exception
