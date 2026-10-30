import asyncio, base64, html, logging, os, re, sqlite3, tempfile, time, urllib.parse
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile, BusinessMessagesDeleted, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
HORDE_KEY = os.getenv("HORDE_API_KEY", "0000000000")  # anonim kalit
HORDE_MODEL = os.getenv("HORDE_MODEL", "Deliberate")  # bo'sh qoldirsangiz, Horde o'zi tanlaydi
GEMINI_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")

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
async def gemini_image(prompt: str) -> bytes:
    """Google Gemini (Nano Banana), o'zbekchani o'zi tushunadi."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    body = {"contents": [{"parts": [{"text": f"Generate an image: {prompt}"}]}]}
    async with aiohttp.ClientSession() as s, s.post(
        url, json=body, headers={"x-goog-api-key": GEMINI_KEY},
        timeout=aiohttp.ClientTimeout(total=120),
    ) as r:
        data = await r.json()
        if r.status != 200:
            raise RuntimeError(f"{r.status}: {data.get('error', {}).get('message', data)}")
    for part in data["candidates"][0]["content"]["parts"]:
        blob = part.get("inlineData") or part.get("inline_data")
        if blob:
            return base64.b64decode(blob["data"])
    raise RuntimeError("Gemini rasm qaytarmadi")


FILLER = re.compile(r"\b(qilib ber|yasab ber|chizib ber|chiqarib ber|iltimos|bo['‘ʻ’`]?lsin)\b", re.I)
QUALITY = ", highly detailed, sharp focus, professional photography, realistic, high resolution, 8k"
NEGATIVE = "blurry, low quality, low resolution, jpeg artifacts, deformed, distorted, text, watermark"


async def to_english(text: str) -> str:
    """O'zbekcha (yoki istalgan tildagi) gapni inglizchaga o'giradi (kalitsiz)."""
    text = FILLER.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip() or text
    try:
        async with aiohttp.ClientSession() as s, s.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": "en", "dt": "t", "q": text},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as r:
            data = await r.json(content_type=None)
        return "".join(seg[0] for seg in data[0] if seg[0]).strip() or text
    except Exception as e:
        logging.warning("Tarjima xatosi: %s", e)
        return text


async def pollinations_image(prompt: str) -> bytes:
    url = "https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt + QUALITY) + "?width=1024&height=1024&nologo=true&model=flux"
    for attempt in range(2):
        try:
            async with aiohttp.ClientSession() as s, s.get(url, timeout=aiohttp.ClientTimeout(total=120)) as r:
                if r.status == 200:
                    return await r.read()
                logging.warning("Pollinations %s (urinish %s)", r.status, attempt + 1)
        except Exception as e:
            logging.warning("Pollinations xatosi: %s", e)
        await asyncio.sleep(3)
    raise RuntimeError("Pollinations band")


HORDE = "https://aihorde.net/api/v2"


async def horde_image(prompt: str) -> bytes:
    """AI Horde (anonim, kalitsiz). Navbat bo'lsa, bir necha daqiqa kutadi."""
    headers = {"apikey": HORDE_KEY, "Client-Agent": "yordamchi-bot:1.0:telegram"}
    body = {
        "prompt": f"{prompt}{QUALITY} ### {NEGATIVE}",
        "params": {"width": 512, "height": 512, "steps": 30, "cfg_scale": 7.5,
                   "sampler_name": "k_euler_a", "n": 1},
        "nsfw": False,
    }
    if HORDE_MODEL:
        body["models"] = [HORDE_MODEL]
    async with aiohttp.ClientSession(headers=headers) as s:
        async with s.post(f"{HORDE}/generate/async", json=body) as r:
            data = await r.json()
            if r.status not in (200, 202):
                raise RuntimeError(f"Horde {r.status}: {data.get('message', data)}")
            job = data["id"]
        for _ in range(72):  # 72 * 5 soniya = 6 daqiqagacha
            await asyncio.sleep(5)
            async with s.get(f"{HORDE}/generate/check/{job}") as r:
                st = await r.json()
            if st.get("faulted"):
                raise RuntimeError("Horde: xatolik yuz berdi")
            if st.get("done"):
                break
        else:
            raise RuntimeError("Horde: navbat juda uzun")
        async with s.get(f"{HORDE}/generate/status/{job}") as r:
            st = await r.json()
    img = st["generations"][0]["img"]
    if img.startswith("http"):
        async with aiohttp.ClientSession() as s2, s2.get(img, timeout=aiohttp.ClientTimeout(total=60)) as r:
            return await r.read()
    return base64.b64decode(img)


async def make_image(text: str) -> bytes:
    # 1) Gemini kaliti bo'lsa: o'zbekchani o'zi tushunadi
    if GEMINI_KEY:
        try:
            return await gemini_image(text)
        except Exception as e:
            logging.warning("Gemini xatosi: %s", e)
    # 2) Inglizchaga o'girib, bepul xizmatlar: avval Pollinations, keyin AI Horde
    prompt = await to_english(text)
    logging.info("Inglizcha prompt: %s", prompt)
    try:
        return await pollinations_image(prompt)
    except Exception as e:
        logging.warning("Pollinations ishlamadi, AI Horde sinaladi: %s", e)
    return await horde_image(prompt)


# ---------- Foydalanuvchilar (ro'yxatdan o'tish) ----------
db.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, lang TEXT, phone TEXT, name TEXT, username TEXT, joined TEXT)")
db.commit()

STATE: dict = {}    # uid -> {"mode": "img" | "voice_pick" | "tts", "voice": "boy"}
PENDING: dict = {}  # uid -> tanlangan til (kontakt kutilmoqda)
LAST: dict = {}     # uid -> oxirgi so'rov vaqti
COOLDOWN = 15       # soniya (egasi uchun cheklov yo'q)
MAX_LEN = 500


def get_user(uid):
    return db.execute("SELECT lang, phone FROM users WHERE id=?", (uid,)).fetchone()


def lang_of(uid) -> str:
    row = get_user(uid)
    return row[0] if row else "uz"


def cooldown_left(uid) -> int:
    if uid == OWNER_ID:
        return 0
    left = COOLDOWN - (time.monotonic() - LAST.get(uid, 0))
    return int(left) + 1 if left > 0 else 0


T = {
    "uz": dict(
        contact="Davom etish uchun kontaktingizni yuboring 👇", btn_contact="📱 Kontaktni yuborish",
        bad_contact="Iltimos, faqat o'zingizning kontaktingizni pastdagi tugma orqali yuboring.",
        registered="✅ Rahmat! Ro'yxatdan o'tdingiz.", menu="Tanlang:",
        b1="1️⃣ Rasm yasash", b2="2️⃣ Matnni ovozga aylantirish",
        ask_img="🖼 Qanday rasm kerak? Yozing.", ask_voice="🎙 Ovozni tanlang:",
        v_boy="👦 O'g'il bola", v_girl="👧 Qiz bola", v_robot="🤖 Robot", v_hacker="💻 Haker",
        ask_text="✍️ Ovozli qilinadigan matnni yozing:",
        wait_img="Rasm tayyorlanmoqda...", wait_tts="Ovoz tayyorlanmoqda...",
        too_long="Matn juda uzun (eng ko'pi 500 belgi).",
        cooldown="Biroz kuting ({n} soniya) va qayta urinib ko'ring.", err="Xatolik: {e}",
    ),
    "ru": dict(
        contact="Для продолжения отправьте свой контакт 👇", btn_contact="📱 Отправить контакт",
        bad_contact="Пожалуйста, отправьте только свой контакт кнопкой ниже.",
        registered="✅ Спасибо! Вы зарегистрированы.", menu="Выберите:",
        b1="1️⃣ Создать картинку", b2="2️⃣ Озвучить текст",
        ask_img="🖼 Какую картинку создать? Напишите.", ask_voice="🎙 Выберите голос:",
        v_boy="👦 Мальчик", v_girl="👧 Девочка", v_robot="🤖 Робот", v_hacker="💻 Хакер",
        ask_text="✍️ Напишите текст для озвучки:",
        wait_img="Создаю картинку...", wait_tts="Создаю голос...",
        too_long="Текст слишком длинный (максимум 500 символов).",
        cooldown="Подождите ({n} сек.) и попробуйте снова.", err="Ошибка: {e}",
    ),
    "en": dict(
        contact="Please share your contact to continue 👇", btn_contact="📱 Share contact",
        bad_contact="Please send only your own contact using the button below.",
        registered="✅ Thanks! You are registered.", menu="Choose:",
        b1="1️⃣ Create image", b2="2️⃣ Text to voice",
        ask_img="🖼 What image do you want? Write it.", ask_voice="🎙 Choose a voice:",
        v_boy="👦 Boy", v_girl="👧 Girl", v_robot="🤖 Robot", v_hacker="💻 Hacker",
        ask_text="✍️ Write the text to be voiced:",
        wait_img="Creating the image...", wait_tts="Creating the voice...",
        too_long="Text is too long (max 500 characters).",
        cooldown="Please wait ({n} sec.) and try again.", err="Error: {e}",
    ),
}


def lang_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🇺🇿 O'zbekcha", callback_data="lang:uz"),
        InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang:ru"),
        InlineKeyboardButton(text="🇬🇧 English", callback_data="lang:en"),
    ]])


def menu_kb(l):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=T[l]["b1"], callback_data="m:img")],
        [InlineKeyboardButton(text=T[l]["b2"], callback_data="m:tts")],
    ])


def voice_kb(l):
    b = lambda k: InlineKeyboardButton(text=T[l]["v_" + k], callback_data="v:" + k)
    return InlineKeyboardMarkup(inline_keyboard=[[b("boy"), b("girl")], [b("robot"), b("hacker")]])


def contact_kb(l):
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=T[l]["btn_contact"], request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True)


async def ask_language(target: Message):
    await target.answer("Tilni tanlang / Выберите язык / Choose language:", reply_markup=lang_kb())


# ---------- Ovoz (matnni ovozga aylantirish) ----------
VOICES = {
    "uz": {"boy": "uz-UZ-SardorNeural", "girl": "uz-UZ-MadinaNeural"},
    "ru": {"boy": "ru-RU-DmitryNeural", "girl": "ru-RU-SvetlanaNeural"},
    "en": {"boy": "en-US-GuyNeural", "girl": "en-US-JennyNeural"},
}
PITCH = {"boy": "+20Hz", "girl": "+5Hz", "robot": "+0Hz", "hacker": "-35Hz"}
EFFECTS = {
    "robot": "afftfilt=real='hypot(re,im)*sin(0)':imag='hypot(re,im)*cos(0)':win_size=512:overlap=0.75",
    "hacker": "acrusher=level_in=3:level_out=5:bits=6:mode=lin:aa=0,aecho=0.8:0.8:25:0.6,highpass=f=120",
}


async def synth(text: str, lang: str, kind: str) -> bytes:
    import edge_tts
    import imageio_ffmpeg

    base = VOICES[lang]["girl" if kind == "girl" else "boy"]
    with tempfile.TemporaryDirectory() as d:
        mp3, ogg = os.path.join(d, "a.mp3"), os.path.join(d, "a.ogg")
        await edge_tts.Communicate(text, base, pitch=PITCH[kind]).save(mp3)
        cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-i", mp3]
        if kind in EFFECTS:
            cmd += ["-af", EFFECTS[kind]]
        cmd += ["-c:a", "libopus", "-b:a", "48k", "-ar", "48000", "-ac", "1", ogg]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(err.decode(errors="ignore")[-200:])
        with open(ogg, "rb") as f:
            return f.read()


# ---------- Buyruqlar va tugmalar ----------
@dp.message(CommandStart())
@dp.message(Command("menu"))
async def start(m: Message):
    uid = m.from_user.id
    if uid == OWNER_ID and not get_user(uid):
        db.execute("INSERT OR REPLACE INTO users VALUES (?,?,?,?,?,?)",
                   (uid, "uz", "-", m.from_user.full_name, m.from_user.username or "", time.strftime("%Y-%m-%d %H:%M")))
        db.commit()
    if get_user(uid):
        STATE.pop(uid, None)
        await m.answer(T[lang_of(uid)]["menu"], reply_markup=menu_kb(lang_of(uid)))
    else:
        await ask_language(m)


@dp.message(Command("lang"))
async def change_lang(m: Message):
    await ask_language(m)


@dp.callback_query(F.data.startswith("lang:"))
async def pick_lang(cb: CallbackQuery):
    l, uid = cb.data.split(":")[1], cb.from_user.id
    await cb.answer()
    if get_user(uid):
        db.execute("UPDATE users SET lang=? WHERE id=?", (l, uid))
        db.commit()
        await cb.message.answer(T[l]["menu"], reply_markup=menu_kb(l))
    else:
        PENDING[uid] = l
        await cb.message.answer(T[l]["contact"], reply_markup=contact_kb(l))


@dp.message(F.contact)
async def got_contact(m: Message):
    uid = m.from_user.id
    if uid not in PENDING:
        if get_user(uid):
            await m.answer(T[lang_of(uid)]["menu"], reply_markup=menu_kb(lang_of(uid)))
        else:
            await ask_language(m)
        return
    l = PENDING[uid]
    if m.contact.user_id != uid:  # boshqaning kontaktini qabul qilmaymiz
        await m.answer(T[l]["bad_contact"], reply_markup=contact_kb(l))
        return
    PENDING.pop(uid)
    db.execute("INSERT OR REPLACE INTO users VALUES (?,?,?,?,?,?)",
               (uid, l, m.contact.phone_number, m.from_user.full_name, m.from_user.username or "", time.strftime("%Y-%m-%d %H:%M")))
    db.commit()
    await m.answer(T[l]["registered"], reply_markup=ReplyKeyboardRemove())
    await m.answer(T[l]["menu"], reply_markup=menu_kb(l))
    if OWNER_ID:  # egasiga xabar
        try:
            await bot.send_message(
                OWNER_ID,
                "🆕 <b>Yangi foydalanuvchi</b>\n"
                f"Ism: {html.escape(m.from_user.full_name)}\n"
                f"Username: {'@' + html.escape(m.from_user.username) if m.from_user.username else 'yo`q'}\n"
                f"ID: <code>{uid}</code>\n"
                f"Telefon: <code>{html.escape(m.contact.phone_number)}</code>\n"
                f"Til: {l}\n"
                f'<a href="tg://user?id={uid}">Profilni ochish</a>',
                parse_mode="HTML")
        except Exception as e:
            logging.warning("Egasiga xabar yuborilmadi: %s", e)


async def choose_image(target: Message, uid: int):
    STATE[uid] = {"mode": "img"}
    await target.answer(T[lang_of(uid)]["ask_img"])


async def choose_voice(target: Message, uid: int):
    STATE[uid] = {"mode": "voice_pick"}
    await target.answer(T[lang_of(uid)]["ask_voice"], reply_markup=voice_kb(lang_of(uid)))


@dp.callback_query(F.data.startswith("m:"))
async def menu_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    await cb.answer()
    if not get_user(uid):
        await ask_language(cb.message)
    elif cb.data == "m:img":
        await choose_image(cb.message, uid)
    else:
        await choose_voice(cb.message, uid)


@dp.callback_query(F.data.startswith("v:"))
async def voice_cb(cb: CallbackQuery):
    uid, kind = cb.from_user.id, cb.data.split(":")[1]
    await cb.answer()
    if not get_user(uid) or kind not in PITCH:
        return
    l = lang_of(uid)
    STATE[uid] = {"mode": "tts", "voice": kind}
    await cb.message.answer(f"{T[l]['v_' + kind]}\n{T[l]['ask_text']}")


# ---------- Matn kelganda ----------
async def do_image(m: Message, l: str):
    uid = m.from_user.id
    if len(m.text) > MAX_LEN:
        return await m.answer(T[l]["too_long"])
    if n := cooldown_left(uid):
        return await m.answer(T[l]["cooldown"].format(n=n))
    LAST[uid] = time.monotonic()
    wait = await m.answer(T[l]["wait_img"])
    try:
        img = await make_image(m.text)
        try:
            await m.answer_photo(BufferedInputFile(img, "image.png"), caption=m.text[:900], reply_markup=menu_kb(l))
        except Exception:
            await m.answer_document(BufferedInputFile(img, "image.webp"), caption=m.text[:900], reply_markup=menu_kb(l))
    except Exception as e:
        await m.answer(T[l]["err"].format(e=str(e)[:200]))
    finally:
        await wait.delete()


async def do_tts(m: Message, l: str, kind: str):
    uid = m.from_user.id
    if len(m.text) > MAX_LEN:
        return await m.answer(T[l]["too_long"])
    if n := cooldown_left(uid):
        return await m.answer(T[l]["cooldown"].format(n=n))
    LAST[uid] = time.monotonic()
    wait = await m.answer(T[l]["wait_tts"])
    try:
        audio = await synth(m.text, l, kind)
        await m.answer_voice(BufferedInputFile(audio, "voice.ogg"), reply_markup=menu_kb(l))
    except Exception as e:
        logging.warning("TTS xatosi: %s", e)
        await m.answer(T[l]["err"].format(e=str(e)[:200]))
    finally:
        await wait.delete()


@dp.message(F.text, F.chat.type == "private")
async def on_text(m: Message):
    uid = m.from_user.id
    if not get_user(uid):
        if uid in PENDING:
            await m.answer(T[PENDING[uid]]["contact"], reply_markup=contact_kb(PENDING[uid]))
        else:
            await ask_language(m)
        return
    l, t = lang_of(uid), m.text.strip()
    if t == "1":
        return await choose_image(m, uid)
    if t == "2":
        return await choose_voice(m, uid)
    st = STATE.get(uid)
    if not st:
        await m.answer(T[l]["menu"], reply_markup=menu_kb(l))
    elif st["mode"] == "img":
        await do_image(m, l)
    elif st["mode"] == "tts":
        await do_tts(m, l, st["voice"])
    else:
        await m.answer(T[l]["ask_voice"], reply_markup=voice_kb(l))


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
        
