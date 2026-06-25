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
    "For the camera view showing THREE OTP devices:\n"
    "- '{servo1}': The LEFT BOTTOM display.\n"
    "- '{servo2}': The LEFT TOP display.\n"
    "- '{servo3}': The RIGHTMOST display (purple).\n"
    "For the camera view showing TWO OTP displays:\n"
    "- '{servo4}': The TOP display (labeled 'kakaobank').\n"
    "- '{servo5}': The BOTTOM display (labeled '{servo5}').\n\n"
    "Rules:\n"
    "- Only extract the 6-digit number visible on the device screen for '{target_key}'.\n"
    "- If the '{target_key}' device is completely missing, or its screen is blank/off, return null.\n"
    "- Ignore all other devices in the image.\n"
    "- {servo1} and {servo2} are identical, so follow the guide labelled reference image attached.\n"
    "- Output MUST strictly use this exact key: '{target_key}'."
)


def render_prompt(template: str, servo_names: dict, target_key: str) -> str:
    """Replace {servo1}..{servoN} and {target_key} in template with actual names."""
    result = template
    for sid, name in servo_names.items():
        result = result.replace(f"{{servo{sid}}}", name)
    result = result.replace("{target_key}", target_key)
    return result


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

    ref_image = Image.open("captures/reference_image.jpg")
    ref_image.thumbnail((768, 768))

    template = prompt_template if prompt_template else DEFAULT_PROMPT_TEMPLATE
    names = servo_names if servo_names else {}
    instructions = render_prompt(template, names, target_key)

    # Use dynamic model ordering and fallback list from app config if provided
    if models_config and isinstance(models_config, list) and len(models_config) > 0:
        models_to_run = [m["model"] for m in models_config if m.get("model")]
        model_priority_map = {
            m["model"]: m.get("priority", False)
            for m in models_config
            if m.get("model")
        }
    else:
        models_to_run = ["gemini-3.1-flash-lite", "gemini-3.5-flash"]
        model_priority_map = {"gemini-3.1-flash-lite": True, "gemini-3.5-flash": True}

    last_exception = None

    for idx, model in enumerate(models_to_run):
        is_priority = model_priority_map.get(model, False)
        config = types.GenerateContentConfig(
            system_instruction=instructions,
            temperature=0.0,
            response_mime_type="application/json",
            service_tier="priority" if is_priority else None,
            response_schema=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    target_key: types.Schema(type=types.Type.STRING, nullable=True),
                },
                required=[target_key],
            ),
        )
        try:
            response = client.models.generate_content(
                model=model,
                contents=[image, ref_image],
                config=config,
            )
            return json.loads(response.text)
        except Exception as e:
            print(f"Model {model} failed with error: {e}")
            next_model_msg = (
                models_to_run[idx + 1]
                if idx + 1 < len(models_to_run)
                else "no more models"
            )
            print(f"Falling back to {next_model_msg}")
            last_exception = e

    raise last_exception
