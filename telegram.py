import asyncio
import os
from datetime import datetime
import cv2
import httpx
from dotenv import load_dotenv
from gemini import get_extracted_otps
from telethon import TelegramClient, events

load_dotenv()
api_id = os.getenv("API_ID")
api_hash = os.getenv("API_HASH")
esp32_ip = os.getenv("ESP32_IP")

client = TelegramClient("login", api_id, api_hash)

processing_lock = asyncio.Lock()


async def trigger_servo(servo_number):
    url = f"http://{esp32_ip}/activate?servo={servo_number}"
    async with httpx.AsyncClient() as http_client:
        try:
            response = await http_client.get(url, timeout=5.0)
            if response.status_code == 200:
                return True
        except Exception as e:
            print(f"Failed to connect to ESP32: {e}")
    return False


@client.on(events.NewMessage(chats="otp_22"))
async def handle_otp_requests(event):
    if event.out:
        return

    text = event.raw_text.strip().lower()

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

    if text in key_mapping:
        async with processing_lock:
            print(f"Request: '{text}'")

            target_key = key_mapping[text]
            target_servo = servo_mapping[text]

            await trigger_servo(target_servo)
            await asyncio.sleep(1.0)

            # cam1 = 5, 3
            # cam2 = 1, 2, 4
            if target_servo in [5, 3]:
                camera_index = 1
            else:
                camera_index = 2

            cam = cv2.VideoCapture(camera_index)
            await asyncio.sleep(1)
            ret, frame = cam.read()

            os.makedirs("captures", exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            image_path = f"captures/{timestamp}.jpg"

            if ret:
                cv2.imwrite(image_path, frame)
                print(f"Saved snapshot to {image_path}")
            else:
                print("Failed to capture image from camera.")
                image_path = None
            cam.release()

            otp_data = None
            if image_path and os.path.exists(image_path):
                otp_data = get_extracted_otps(image_path)

            reply_text = "null"
            if otp_data and target_key in otp_data:
                extracted_code = otp_data[target_key]
                if extracted_code:
                    reply_text = str(extracted_code).strip()

            print(f"Reply: '{reply_text}'")
            await client.send_message(event.chat_id, reply_text)


print("Telegram listener started...")
client.start()
client.run_until_disconnected()
