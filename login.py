from telethon import TelegramClient

client = TelegramClient('login', , '').start()
client.run_until_disconnected()