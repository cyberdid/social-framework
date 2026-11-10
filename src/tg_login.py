"""One-time interactive Telegram login. Run it yourself in a terminal:

    cd ~/Projects/Social && .venv/bin/python src/tg_login.py

It asks for phone, login code and (if set) 2FA password. Nothing is printed
or logged except the account name. The session file is stored outside the
project (~/.config/social/, mode 600) and grants full account access: never
commit or share it.
"""
import asyncio
import getpass
import os
from pathlib import Path

from telethon import TelegramClient, errors, functions, types, utils

ENV = Path.home() / ".config/social/telegram.env"


def load_env(path: Path) -> dict:
    out = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


async def main() -> None:
    env = load_env(ENV)
    os.umask(0o077)
    client = TelegramClient(env["TG_SESSION"], int(env["TG_API_ID"]), env["TG_API_HASH"],
                            request_retries=0, connection_retries=1, flood_sleep_threshold=0)
    try:
        print("З'єднуюся з Telegram…", flush=True)
        await asyncio.wait_for(client.connect(), 20)
        if await asyncio.wait_for(client.is_user_authorized(), 15):
            print("OK: сесія вже авторизована.", flush=True)
            return
        phone = utils.parse_phone(input("Номер телефону з кодом країни (+380…): "))
        if not phone:
            print("Некоректний номер телефону.", flush=True)
            return
        print("Надсилаю один запит коду; чекаю відповіді сервера…", flush=True)
        # Use one request, without implicit retries or resends.
        sent = await asyncio.wait_for(client(functions.auth.SendCodeRequest(
            phone, int(env["TG_API_ID"]), env["TG_API_HASH"], types.CodeSettings())), 30)
        if isinstance(sent, types.auth.SentCodeSuccess):
            print("Telegram підтвердив вхід без коду.", flush=True)
            return
        delivery = getattr(sent, "type", None)
        kind = type(delivery).__name__
        print(f"Відповідь Telegram: {type(sent).__name__}; доставка: {kind}.", flush=True)
        if isinstance(delivery, types.auth.SentCodeTypeApp):
            print("Код надіслано у службовий чат Telegram на вже авторизованих пристроях. "
                  "Перевір чат Telegram, зокрема архів. Це не SMS.", flush=True)
        elif isinstance(delivery, types.auth.SentCodeTypeSms):
            print("Сервер повідомив про відправлення SMS на введений номер.", flush=True)
        elif isinstance(delivery, types.auth.SentCodeTypeEmailCode):
            print(f"Код надіслано на пошту: {delivery.email_pattern}", flush=True)
        else:
            print("Цей спосіб доставки не підтримується цим скриптом. "
                  "Запусти .venv/bin/python src/tg_login_qr.py для входу через QR.", flush=True)
            return
        print(f"Наступний спосіб: {type(sent.next_type).__name__}; "
              f"таймер сервера: {sent.timeout} с. Автоматичних повторних запитів немає.", flush=True)
        code = getpass.getpass("Код входу (або Enter, якщо не надійшов): ").strip()
        if not code:
            print("Код не отримано. Для входу без коду: .venv/bin/python src/tg_login_qr.py", flush=True)
            return
        try:
            await asyncio.wait_for(client.sign_in(phone=phone, code=code,
                                                  phone_code_hash=sent.phone_code_hash), 30)
        except errors.SessionPasswordNeededError:
            await asyncio.wait_for(client.sign_in(password=getpass.getpass("Пароль 2FA: ")), 30)
        print("OK: вхід підтверджено, сесію збережено.", flush=True)
    except errors.FloodWaitError as exc:
        print(f"Telegram обмежив спроби входу: зачекай {exc.seconds} с. Не повторюй запит раніше.", flush=True)
    except asyncio.TimeoutError:
        print("Telegram не відповів у межах тайм-ауту. Доставка коду не підтверджена; "
              "повторний запит автоматично не надсилався.", flush=True)
    except errors.RPCError as exc:
        print(f"Помилка Telegram: {type(exc).__name__} ({exc.message}).", flush=True)
    finally:
        await client.disconnect()
        session_path = Path(client.session.filename)
        if session_path.exists():
            session_path.chmod(0o600)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, EOFError):
        print("\nВхід скасовано.")
