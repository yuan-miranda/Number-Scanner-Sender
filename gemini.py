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
def get_extracted_otps(image_path):
    try:
        client = genai.Client(api_key=os.getenv("API_KEY"))
        image = Image.open(image_path)
        image.thumbnail((768, 768))
        ref_image = Image.open("captures/reference_image.jpg")
        ref_image.thumbnail((768, 768))

        response = client.models.generate_content(
            # model="gemini-3.5-flash",
            model="gemini-3.1-flash-lite",
            contents=[image, ref_image],
            config=types.GenerateContentConfig(
                system_instruction=(
                    "Extract 6-digit OTP codes from the tokens in the image into a JSON object.\n\n"
                    "Identify tokens based on their visual appearance and their horizontal order from LEFT to RIGHT in the frame:\n\n"
                    "For the camera view showing THREE tokens:\n"
                    "- 'shinhan': The LEFT BOTTOM oval token. (located at the bottom of kfcc and left side of woori).\n"
                    "- 'kfcc': The LEFT TOP oval token.\n"
                    "- 'woori': The RIGHTMOST token (purple credit-card-shaped token with cartoon characters).\n\n"
                    "For the camera view showing TWO tokens:\n"
                    "- 'kakao': The TOP token (white rectangular box with lowercase 'kakaobank').\n"
                    "- 'nh': The BOTTOM token (white rectangular box with blue/green 'NH' logo).\n\n"
                    "Rules:\n"
                    "- For the shinhan and kfcc, they are identical, so follow the guide reference image attached.\n"
                    "- Only extract the 6-digit number visible on the device screen.\n"
                    "- If a device/slot is completely missing from the current camera view, or its screen is blank/off, return null for that key.\n"
                    "- Output MUST strictly use these exact keys in this required order: 'shinhan', 'kfcc', 'woori', 'kakao', 'nh'."
                ),
                temperature=0.0,
                response_mime_type="application/json",
                response_schema=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "shinhan": types.Schema(type=types.Type.STRING, nullable=True),
                        "kfcc": types.Schema(type=types.Type.STRING, nullable=True),
                        "woori": types.Schema(type=types.Type.STRING, nullable=True),
                        "kakao": types.Schema(type=types.Type.STRING, nullable=True),
                        "nh": types.Schema(type=types.Type.STRING, nullable=True),
                    },
                    required=["shinhan", "kfcc", "woori", "kakao", "nh"],
                ),
            ),
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"Gemini processing error: {e}")
        return None
