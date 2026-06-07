import os
from dotenv import load_dotenv
from google import genai
from google.genai import types
from PIL import Image

load_dotenv()
client = genai.Client(api_key=os.getenv("API_KEY"))
image = Image.open('image.png')
image.thumbnail((768, 768))

response = client.models.generate_content(
    model="gemini-3.5-flash",
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
    ),
)

print(response.text)
