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


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def get_extracted_otps(image_path, target_key):
    client = genai.Client(api_key=os.getenv("API_KEY"))

    image = Image.open(image_path)
    image.thumbnail((768, 768))

    ref_image = Image.open("captures/reference_image.jpg")
    ref_image.thumbnail((768, 768))

    instructions = (
        f"Extract the 6-digit OTP code strictly for the '{target_key}' token into a JSON object.\n"
        "For the camera view showing THREE OTP devices:\n"
        "- 'shinhan': The LEFT BOTTOM display.\n"
        "- 'kfcc': The LEFT TOP display.\n"
        "- 'woori': The RIGHTMOST display (purple).\n"
        "For the camera view showing TWO OTP displays:\n"
        "- 'kakao': The TOP display (labeled 'kakaobank').\n"
        "- 'nh': The BOTTOM display (labeled 'NH').\n\n"
        f"Rules:\n"
        "- Only extract the 6-digit number visible on the device screen for '{target_key}'.\n"
        "- If the '{target_key}' device is completely missing, or its screen is blank/off, return null.\n"
        "- Ignore all other devices in the image.\n"
        "- Shinhan and kfcc are identical, so follow the guide labelled reference image attached.\n"
        f"- Output MUST strictly use this exact key: '{target_key}'."
    )

    config = types.GenerateContentConfig(
        system_instruction=instructions,
        temperature=0.0,
        response_mime_type="application/json",
        service_tier="priority",
        response_schema=types.Schema(
            type=types.Type.OBJECT,
            properties={
                target_key: types.Schema(type=types.Type.STRING, nullable=True),
            },
            required=[target_key],
        ),
    )

    models = ["gemini-3.1-flash-lite", "gemini-3.5-flash"]
    last_exception = None

    for model in models:
        try:
            response = client.models.generate_content(
                model=model,
                contents=[image, ref_image],
                config=config,
            )
            return json.loads(response.text)
        except Exception as e:
            print(f"Model {model} failed with error: {e}")
            print(
                f"Falling back to {models[models.index(model) + 1] if models.index(model) + 1 < len(models) else 'no more models'}"
            )
            last_exception = e

    raise last_exception
