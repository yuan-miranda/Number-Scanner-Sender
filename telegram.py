import asyncio
import os
import httpx
import cv2
from dotenv import load_dotenv
from telethon import TelegramClient, events
from gemini import get_extracted_otps

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
        "sh": "shinhan", "shinhan": "shinhan",
        "woori": "woori", "kfcc": "kfcc",
        "nh": "nh", "kakao": "kakao"
    }
    
    servo_mapping = {
        "nh": 1, "kakao": 2, "kfcc": 3,
        "shinhan": 4, "sh": 4, "woori": 5
    }

    if text in key_mapping:
        async with processing_lock:
            print(f"Request: '{text}'")
            
            target_key = key_mapping[text]
            target_servo = servo_mapping[text]
            
            await trigger_servo(target_servo)
            await asyncio.sleep(2.0)
            
            cam = cv2.VideoCapture(0)
            ret, frame = cam.read()
            if ret:
                cv2.imwrite("1.jpg", frame)
            cam.release()

            otp_data = get_extracted_otps()
            
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