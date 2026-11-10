"""Telegram login by QR code (no SMS/code needed). Run in your own terminal:

    cd ~/Projects/Social && .venv/bin/python src/tg_login_qr.py

A window with the QR image opens (also printed in the terminal). On the phone:
Telegram -> Settings -> Devices -> Link Desktop Device -> scan. The QR refreshes
automatically before it expires. If 2FA is on, type the cloud password here.
"""
import asyncio
import getpass
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import qrcode
from telethon import TelegramClient, errors
from tg_login import load_env, ENV

CFG = Path.home() / ".config/social"
PNG = CFG / "login_qr.png"
viewer = None


def show(url: str) -> None:
    global viewer
    img = qrcode.make(url, box_size=12, border=4)
    img.save(PNG)
    q = qrcode.QRCode(border=1)
    q.add_data(url)
    q.print_ascii(invert=True)
    print(f"\nQR image: {PNG}")
    print("Scan: Telegram -> Settings -> Devices -> Link Desktop Device\n", flush=True)
    if viewer is None:
        try:
            viewer = subprocess.Popen(["xdg-open", str(PNG)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass


async def main() -> None:
    os.umask(0o077)
    env = load_env(ENV)
    client = TelegramClient(env["TG_SESSION"], int(env["TG_API_ID"]), env["TG_API_HASH"],
                            request_retries=0, connection_retries=1, flood_sleep_threshold=0)
    try:
        print("З'єднуюся з Telegram для входу через QR…", flush=True)
        await asyncio.wait_for(client.connect(), 20)
        if await asyncio.wait_for(client.is_user_authorized(), 15):
            print("Already logged in.", flush=True)
            return
        qr = await asyncio.wait_for(client.qr_login(), 20)
        while True:
            show(qr.url)
            left = max(5, (qr.expires - datetime.now(timezone.utc)).total_seconds() - 2)
            try:
                await qr.wait(timeout=left)
                break
            except asyncio.TimeoutError:
                print("QR refreshed (old one expired). If an image viewer is open, reopen", PNG, flush=True)
                await asyncio.wait_for(qr.recreate(), 20)
            except errors.SessionPasswordNeededError:
                await client.sign_in(password=getpass.getpass("2FA cloud password: "))
                break
        me = await client.get_me()
        print(f"OK: logged in as {me.first_name}. Session saved.")
    finally:
        PNG.unlink(missing_ok=True)
        await client.disconnect()
        session_path = Path(client.session.filename)
        if session_path.exists():
            session_path.chmod(0o600)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except errors.FloodWaitError as exc:
        print(f"Telegram обмежив спроби: зачекай {exc.seconds} с.", flush=True)
    except asyncio.TimeoutError:
        print("Telegram не відповів у межах тайм-ауту.", flush=True)
    except errors.RPCError as exc:
        print(f"Помилка Telegram: {type(exc).__name__} ({exc.message}).", flush=True)
    except (KeyboardInterrupt, EOFError):
        print("\nВхід скасовано.")
