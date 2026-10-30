import asyncio, base64, logging, os, sqlite3, urllib.parse
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile, BusinessMessagesDeleted, Message

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-2.5-sunburst")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")

logging.basicConfig(level=logging.INFO)
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# ---------- Baza ----------
db = sqlite3.connect("messages.db")
db.execute("CREATE TABLE IF NOT EXISTS msgs (chat_id, message_id, sender, content, PRIMARY KEY (chat_id, message_id))")


def text_of(m: Message) -> str:
    return m.text or f"[{m.content_type}] {m.caption or ''}".strip()


def save(m: Message):
    who = m.from_user.full_name if m.from_user else "Noma'lum"
    db.execute("INSERT OR REPLACE INTO msgs VALUES (?,?,?,?)", (m.chat.id, m.message_id, who, text_of(m)))
    db.commit()


def load(chat_id, message_id):
    return db.execute("SELECT sender, content FROM msgs WHERE chat_id=? AND message_id=?", (chat_id, message_id)).fetchone()


# ---------- Rasm ----------
async def better_prompt(idea: str) -> str:
    if not ANTHROPIC_KEY:
        return idea
    try:
        from anthropic import AsyncAnthropic
        r = await AsyncAnthropic(api_key=ANTHROPIC_KEY).messages.create(
            model="claude-sonnet-4-6", max_tokens=300,
            system="Translate and expand the idea into one detailed English image prompt "
                   "(style, lighting, composition, quality). Keep the subject, gender, age "
                   "and number of people EXACTLY as stated; never assume a gender. Reply with ONLY the prompt.",
            messages=[{"role": "user", "content": idea}])
        return r.content[0].text.strip()
    except Exception as e:
        logging.warning("Claude xatosi: %s", e)
        return idea


async def make_image(prompt: str) -> bytes:
    if OPENAI_KEY:
        try:
            from openai import AsyncOpenAI
            r = await AsyncOpenAI(api_key=OPENAI_KEY).images.generate(
                model=OPENAI_IMAGE_MODEL, prompt=prompt, size="1024x1024")
            return base64.b64decode(r.data[0].b64_json)
        except Exception as e:
            logging.warning("OpenAI xatosi, bepul variantga o'tildi: %s", e)
    url = "https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt) + "?width=1024&height=1024&nologo=true"
    async with aiohttp.ClientSession() as s, s.get(url, timeout=aiohttp.ClientTimeout(total=120)) as r:
        r.raise_for_status()
        return await r.read()


@dp.message(CommandStart())
async def start(m: Message):
    if m.from_user.id == OWNER_ID:
        await m.answer(f"Salom! Qanday rasm kerak, yozing.\nSizning ID: {m.from_user.id}")


@dp.message(F.text)
async def generate(m: Message):
    if m.from_user.id != OWNER_ID:
        return
    wait = await m.answer("Rasm tayyorlanmoqda...")
    try:
        prompt = await better_prompt(m.text)
        img = await make_image(prompt)
        await m.answer_photo(BufferedInputFile(img, "image.png"), caption=prompt[:900])
    except Exception as e:
        await m.answer(f"Xatolik: {e}")
    finally:
        await wait.delete()


# ---------- Business: tahrirlangan / o'chirilgan ----------
@dp.business_message()
async def on_msg(m: Message):
    save(m)


@dp.edited_business_message()
async def on_edit(m: Message):
    old = load(m.chat.id, m.message_id)
    if old and old[1] != text_of(m):
        await bot.send_message(OWNER_ID, f"✏️ Tahrirlandi ({old[0]})\n\nOldin: {old[1]}\n\nHozir: {text_of(m)}")
    save(m)


@dp.deleted_business_messages()
async def on_delete(e: BusinessMessagesDeleted):
    for mid in e.message_ids:
        row = load(e.chat.id, mid)
        await bot.send_message(OWNER_ID, f"🗑 O'chirildi ({row[0]})\n\n{row[1]}" if row else "🗑 Xabar o'chirildi (bazada yo'q edi).")


# ---------- Render uchun web server + o'ziga ping ----------
async def keep_alive():
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000"))).start()
    url = os.getenv("RENDER_EXTERNAL_URL")
    while url:
        await asyncio.sleep(600)
        try:
            async with aiohttp.ClientSession() as s:
                await s.get(url, timeout=aiohttp.ClientTimeout(total=30))
        except Exception:
            pass


async def main():
    asyncio.create_task(keep_alive())
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
