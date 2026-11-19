import asyncio

from pyrogram import Client


async def main():
    api_id = int(input("API_ID: ").strip())
    api_hash = input("API_HASH: ").strip()
    async with Client("gen", api_id=api_id, api_hash=api_hash, in_memory=True) as app:
        print("\n===== SESSION_STRING (poori copy karo) =====\n")
        print(await app.export_session_string())
        print("\n============================================")

asyncio.run(main())
