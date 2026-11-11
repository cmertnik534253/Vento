"""Один раз войти в свой аккаунт Telegram, чтобы юзербот мог читать входящие.

Запусти:  python login.py
Telegram пришлёт код в приложение; если включён облачный пароль (2FA) — введи и его.
Сессия сохранится в data/userbot.session. Этот файл = доступ к аккаунту, никому не отдавай.
"""
import asyncio

from telethon import TelegramClient

from settings import load_settings


async def main():
    cfg = load_settings(need_bot=False)
    client = TelegramClient(
        str(cfg.data_dir / "userbot"), cfg.api_id, cfg.api_hash,
        device_model="Message Notifier", app_version="1.0",
    )
    await client.start()  # спросит телефон, код и (если есть) пароль
    me = await client.get_me()
    name = " ".join(p for p in (me.first_name, me.last_name) if p)
    (cfg.data_dir / "login.ok").write_text(str(me.id))  # метка для bot.py и install.sh
    print(f"\nГотово! Вошёл как {name} (id {me.id}).")
    print("Теперь можно запускать бота: python bot.py")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
