from telethon import TelegramClient, events

client = TelegramClient("login", , '').start()


@client.on(events.NewMessage)
async def handler(event):
    chat_id = event.chat_id
    print(f"{chat_id}")


client.start()
client.run_until_disconnected()
