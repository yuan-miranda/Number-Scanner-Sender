import asyncio
import os
import random
from dotenv import load_dotenv
from telethon import TelegramClient, events

load_dotenv()
api_id = os.getenv("API_ID")
api_hash = os.getenv("API_HASH")

client = TelegramClient("login", api_id, api_hash)

async def get_otp_code():
    return str(random.randint(100000, 999999))

@client.on(events.NewMessage(chats="otp_22"))
async def handle_otp_requests(event):
    if event.out:
        return

    text = event.raw_text.strip().lower()
    reply_text = ""

    # sh/shinhan, woori, kfcc, nh, kakao
    if text == "sh" or text == "shinhan":
        reply_text = await get_otp_code()

    elif text == "woori":
        reply_text = await get_otp_code()

    elif text == "kfcc":
        reply_text = await get_otp_code()

    elif text == "nh":
        reply_text = await get_otp_code()

    elif text == "kakao":
        reply_text = await get_otp_code()

    if reply_text:
        await asyncio.sleep(random.uniform(1, 3))
        await client.send_message(event.chat_id, reply_text)


client.start()
client.run_until_disconnected()
