import os
import json
from dotenv import load_dotenv
from google import genai
from google.genai import types
from PIL import Image

load_dotenv()

def get_extracted_otps():
    try:
        client = genai.Client(api_key=os.getenv("API_KEY"))
        image = Image.open('2.jpg')
        image.thumbnail((768, 768))

        response = client.models.generate_content(
            model="gemini-3.1-flash-lite",
            contents=image,
            config=types.GenerateContentConfig(
                system_instruction=(
                    "Extract 6-digit OTPs from the image into a JSON object. "
                    "Identify tokens by visual markings, ignoring table position:\n"
                    "'nh': White rectangular box with blue/green 'NH' logo.\n"
                    "'kakao': White rectangular box with lowercase 'kakaobank'.\n"
                    "'kfcc': Oval token, dark border, silver button [PLACEHOLDER].\n"
                    "'shinhan': Other oval token, dark border, silver button [PLACEHOLDER].\n"
                    "'woori': Purple credit-card-shaped token with cartoon characters.\n\n"
                    "Rules:\n"
                    "- If a screen is blank/off, return null for that key.\n"
                    "- Output must only use these exact keys in this order: 'nh', 'kakao', 'kfcc', 'shinhan', 'woori'."
                ),
                temperature=0.0,
                response_mime_type="application/json",
                response_schema=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "nh": types.Schema(type=types.Type.STRING, nullable=True),
                        "kakao": types.Schema(type=types.Type.STRING, nullable=True),
                        "kfcc": types.Schema(type=types.Type.STRING, nullable=True),
                        "shinhan": types.Schema(type=types.Type.STRING, nullable=True),
                        "woori": types.Schema(type=types.Type.STRING, nullable=True),
                    },
                    required=["nh", "kakao", "kfcc", "shinhan", "woori"],
                ),
            ),
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"Gemini processing error: {e}")
        return None