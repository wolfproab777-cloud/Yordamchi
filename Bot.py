import asyncio
import logging
import os
import sqlite3
import urllib.parse

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    BufferedInputFile,
    BusinessMessagesDeleted,
    Message,
)

# ====== SOZLAMALAR ======
BOT_TOKEN = os.getenv("BOT_TOKEN", "BU_YERGA_BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))  # sizning Telegram ID raqamingiz
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")  # ixtiyoriy
# ========================

logging.basicConfig(level=logging.INFO)
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# ---------- Baza ----------
db = sqlite3.connect("messages.db")
db.execute(
    """CREATE TABLE IF NOT EXISTS msgs (
        chat_id INTEGER,
        message_id INTEGER,
        sender TEXT,
        content TEXT,
        PRIMARY KEY (chat_id, message_id)
    )"""
)
db.commit()


def describe(m: Message) -> str:
    """Xabar mazmunini matn ko'rinishida qaytaradi."""
    if m.text:
        return m.text
    kind = m.content_type.value if hasattr(m.content_type, "value") else str(m.content_type)
    return f"[{kind}] {m.caption or ''}".strip()


def sender_name(m: Message) -> str:
    u = m.from_user
    if not u:
        return "Noma'lum"
    return u.full_name + (f" (@{u.username})" if u.username else "")


def save(m: Message):
    db.execute(
        "INSERT OR REPLACE INTO msgs VALUES (?,?,?,?)",
        (m.chat.id, m.message_id, sender_name(m), describe(m)),
    )
    db.commit()


def load(chat_id: int, message_id: int):
    return db.execute(
        "SELECT sender, content FROM msgs WHERE chat_id=? AND message_id=?",
        (chat_id, message_id),
    ).fetchone()


# ---------- 1) RASM YASASH ----------
async def improve_prompt(idea: str) -> str:
    """Claude yordamida oddiy gapni professional prompt'ga aylantiradi."""
    if not ANTHROPIC_API_KEY:
        return idea
    try:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        resp = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            system=(
                "You turn a user's short idea (any language) into one detailed, "
                "professional English prompt for an AI image generator: subject, "
                "style, lighting, composition, camera, quality. "
                "Reply with ONLY the prompt, nothing else."
            ),
            messages=[{"role": "user", "content": idea}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        logging.warning("Claude prompt xatosi: %s", e)
        return idea


async def make_image(prompt: str) -> bytes:
    """Pollinations.ai orqali bepul rasm (kalit shart emas)."""
    url = (
        "https://image.pollinations.ai/prompt/"
        + urllib.parse.quote(prompt)
        + "?width=1024&height=1024&nologo=true"
    )
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=120)) as r:
            r.raise_for_status()
            return await r.read()


@dp.message(CommandStart())
async def start(m: Message):
    if m.from_user.id != OWNER_ID:
        return
    await m.answer(
        "Salom! Qanday rasm kerakligini yozing, men professional qilib yasab beraman.\n\n"
        f"Sizning ID: {m.from_user.id}"
    )


@dp.message(F.text)
async def generate(m: Message):
    if m.from_user.id != OWNER_ID:
        return  # boshqalarga javob bermaydi
    wait = await m.answer("Rasm tayyorlanmoqda...")
    try:
        prompt = await improve_prompt(m.text)
        img = await make_image(prompt)
        await m.answer_photo(
            BufferedInputFile(img, "image.png"),
            caption=f"Prompt:\n{prompt[:900]}",
        )
    except Exception as e:
        await m.answer(f"Xatolik: {e}")
    finally:
        await wait.delete()


# ---------- 2) BUSINESS: o'chirilgan / tahrirlangan xabarlar ----------
@dp.business_message()
async def on_business_message(m: Message):
    save(m)


@dp.edited_business_message()
async def on_edited(m: Message):
    old = load(m.chat.id, m.message_id)
    new_text = describe(m)
    if old and old[1] != new_text:
        await bot.send_message(
            OWNER_ID,
            f"✏️ Xabar tahrirlandi\n"
            f"Kim: {old[0]}\n\n"
            f"Oldin: {old[1]}\n\n"
            f"Hozir: {new_text}",
        )
    save(m)


@dp.deleted_business_messages()
async def on_deleted(event: BusinessMessagesDeleted):
    for mid in event.message_ids:
        row = load(event.chat.id, mid)
        if row:
            await bot.send_message(
                OWNER_ID,
                f"🗑 Xabar o'chirildi\nKim: {row[0]}\n\nMatni: {row[1]}",
            )
        else:
            await bot.send_message(
                OWNER_ID, "🗑 Xabar o'chirildi (lekin bazada saqlanmagan edi)."
            )


# ---------- KEEP-ALIVE (Render bepul Web Service uchun) ----------
async def health(request):
    return web.Response(text="Bot ishlayapti")


async def start_web():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logging.info("Web server %s portda", port)


async def self_ping():
    """Har 10 daqiqada o'z manziliga so'rov yuboradi, Render uxlatmaydi."""
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        return
    while True:
        await asyncio.sleep(600)
        try:
            async with aiohttp.ClientSession() as s:
                await s.get(url, timeout=aiohttp.ClientTimeout(total=30))
        except Exception as e:
            logging.warning("Ping xatosi: %s", e)


async def main():
    await start_web()
    asyncio.create_task(self_ping())
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
