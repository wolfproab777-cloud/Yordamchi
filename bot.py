import asyncio, base64, html, io, logging, os, re, sqlite3, tempfile, time, urllib.parse
import aiohttp
from aiohttp import web
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand, BotCommandScopeChat, BufferedInputFile, BusinessMessagesDeleted, CallbackQuery,
    InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

# ====== SOZLAMALAR (Render Environment orqali) ======
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")   # rasm
GEMINI_TEXT_MODEL = os.getenv("GEMINI_TEXT_MODEL", "gemini-flash-latest")  # ovoz -> matn
HORDE_KEY = os.getenv("HORDE_API_KEY", "0000000000")  # anonim kalit
HORDE_MODEL = os.getenv("HORDE_MODEL", "Deliberate")  # bo'sh qoldirsangiz, Horde o'zi tanlaydi
DAILY_IMAGES = int(os.getenv("DAILY_IMAGES", "5"))    # bir odamga kuniga rasm soni
DAILY_VOICES = int(os.getenv("DAILY_VOICES", "20"))   # kuniga ovoz (matn->ovoz va ovoz->matn)
COOLDOWN = 15   # so'rovlar orasidagi soniya (egasi uchun cheklov yo'q)
MAX_LEN = 500   # matn uzunligi
# =====================================================

logging.basicConfig(level=logging.INFO)
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# ---------- Baza ----------
db = sqlite3.connect("messages.db")
db.execute("CREATE TABLE IF NOT EXISTS msgs (chat_id, message_id, sender, content, PRIMARY KEY (chat_id, message_id))")
for _col in ("ftype", "fid"):  # eski bazada bo'lmasa qo'shiladi
    try:
        db.execute(f"ALTER TABLE msgs ADD COLUMN {_col} TEXT")
    except sqlite3.OperationalError:
        pass
db.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, lang TEXT, phone TEXT, name TEXT, username TEXT, joined TEXT)")
db.execute("CREATE TABLE IF NOT EXISTS usage (uid INTEGER, day TEXT, kind TEXT, n INTEGER, PRIMARY KEY (uid, day, kind))")
db.execute("CREATE TABLE IF NOT EXISTS banned (id INTEGER PRIMARY KEY)")
db.execute("CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT)")
db.commit()

BANNED = {r[0] for r in db.execute("SELECT id FROM banned")}


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


def today() -> str:
    return time.strftime("%Y-%m-%d")


def get_setting(k):
    r = db.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return r[0] if r else None


def set_setting(k, v):
    if v is None:
        db.execute("DELETE FROM settings WHERE k=?", (k,))
    else:
        db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (k, v))
    db.commit()


# ---------- Kunlik limit ----------
LIMITS = {"img": DAILY_IMAGES, "voice": DAILY_VOICES}


def used(uid, kind) -> int:
    r = db.execute("SELECT n FROM usage WHERE uid=? AND day=? AND kind=?", (uid, today(), kind)).fetchone()
    return r[0] if r else 0


def add_use(uid, kind):
    db.execute("INSERT INTO usage VALUES (?,?,?,1) ON CONFLICT(uid, day, kind) DO UPDATE SET n = n + 1",
               (uid, today(), kind))
    db.commit()


def limit_hit(uid, kind) -> bool:
    return uid != OWNER_ID and used(uid, kind) >= LIMITS[kind]


# ---------- Business: xabarlarni saqlash ----------
def text_of(m: Message) -> str:
    if m.text:
        return m.text
    ct = m.content_type
    return f"[{getattr(ct, 'value', ct)}] {m.caption or ''}".strip()


def media_of(m: Message):
    if m.photo:
        return "photo", m.photo[-1].file_id
    for kind in ("video", "voice", "video_note", "audio", "document", "animation", "sticker"):
        obj = getattr(m, kind, None)
        if obj:
            return kind, obj.file_id
    return None, None


def save(m: Message):
    who = m.from_user.full_name if m.from_user else "Noma'lum"
    ft, fid = media_of(m)
    db.execute("INSERT OR REPLACE INTO msgs (chat_id, message_id, sender, content, ftype, fid) VALUES (?,?,?,?,?,?)",
               (m.chat.id, m.message_id, who, text_of(m), ft, fid))
    db.commit()


def load(chat_id, message_id):
    return db.execute("SELECT sender, content, ftype, fid FROM msgs WHERE chat_id=? AND message_id=?",
                      (chat_id, message_id)).fetchone()


async def send_saved(ftype, fid, caption):
    fns = {"photo": bot.send_photo, "video": bot.send_video, "voice": bot.send_voice,
           "video_note": bot.send_video_note, "audio": bot.send_audio, "document": bot.send_document,
           "animation": bot.send_animation, "sticker": bot.send_sticker}
    kw = {} if ftype in ("video_note", "sticker") else {"caption": caption[:1000]}
    await fns[ftype](OWNER_ID, fid, **kw)


# ---------- Gemini ----------
async def gemini_call(model: str, body: dict) -> dict:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    async with aiohttp.ClientSession() as s, s.post(
        url, json=body, headers={"x-goog-api-key": GEMINI_KEY}, timeout=aiohttp.ClientTimeout(total=120)
    ) as r:
        data = await r.json()
        if r.status != 200:
            raise RuntimeError(f"{r.status}: {data.get('error', {}).get('message', data)}")
        return data


def gemini_parts(data: dict) -> list:
    return data["candidates"][0]["content"]["parts"]


async def gemini_image(prompt: str, ratio: str = "1:1") -> bytes:
    """Google Gemini (Nano Banana), o'zbekchani o'zi tushunadi."""
    body = {"contents": [{"parts": [{"text": f"Generate an image: {prompt}"}]}]}
    attempts = [body]
    if ratio != "1:1":
        with_cfg = {**body, "generationConfig": {"imageConfig": {"aspectRatio": ratio}}}
        no_cfg = {"contents": [{"parts": [{"text": f"Generate an image ({ratio} aspect ratio): {prompt}"}]}]}
        attempts = [with_cfg, no_cfg]
    last_err = None
    for b in attempts:
        try:
            data = await gemini_call(GEMINI_MODEL, b)
            for part in gemini_parts(data):
                blob = part.get("inlineData") or part.get("inline_data")
                if blob:
                    return base64.b64decode(blob["data"])
            last_err = RuntimeError("Gemini rasm qaytarmadi")
        except Exception as e:
            last_err = e
    raise last_err


async def gemini_stt(data: bytes, mime: str) -> str:
    body = {"contents": [{"parts": [
        {"text": "Transcribe this audio exactly as spoken, in its original language. "
                 "Output only the transcript, nothing else."},
        {"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}},
    ]}]}
    res = await gemini_call(GEMINI_TEXT_MODEL, body)
    return "".join(p.get("text", "") for p in gemini_parts(res)).strip()


# ---------- Tarjima (kalitsiz) ----------
FILLER = re.compile(r"\b(qilib ber|yasab ber|chizib ber|chiqarib ber|iltimos|bo['‘ʻ’`]?lsin)\b", re.I)


async def translate(text: str, tl: str = "en") -> str:
    try:
        async with aiohttp.ClientSession() as s, s.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": tl, "dt": "t", "q": text},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as r:
            data = await r.json(content_type=None)
        return "".join(seg[0] for seg in data[0] if seg[0]).strip() or text
    except Exception as e:
        logging.warning("Tarjima xatosi: %s", e)
        return text


# ---------- Rasm ----------
STYLES = {
    "none": "",
    "real": ", photorealistic, ultra detailed, natural lighting, DSLR photo",
    "cartoon": ", cartoon style, vibrant colors, clean outlines, 2D animation",
    "anime": ", anime style, detailed illustration, studio ghibli inspired",
    "3d": ", 3D render, octane render, smooth shading, highly detailed",
}
# kalit: (Gemini nisbati, Pollinations eni/bo'yi, Horde eni/bo'yi)
SIZES = {"sq": ("1:1", 1024, 1024, 512, 512), "v": ("9:16", 576, 1024, 384, 640), "h": ("16:9", 1024, 576, 640, 384)}
QUALITY = ", highly detailed, sharp focus, professional photography, realistic, high resolution, 8k"
QUALITY_ART = ", highly detailed, high quality, sharp, beautiful composition"
NEGATIVE = "blurry, low quality, low resolution, jpeg artifacts, deformed, distorted, text, watermark"


async def pollinations_image(prompt: str, w: int, h: int) -> bytes:
    url = (f"https://image.pollinations.ai/prompt/{urllib.parse.quote(prompt)}"
           f"?width={w}&height={h}&nologo=true&model=flux")
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


async def horde_image(prompt: str, w: int, h: int) -> bytes:
    """AI Horde (anonim, kalitsiz). Navbat bo'lsa, bir necha daqiqa kutadi."""
    headers = {"apikey": HORDE_KEY, "Client-Agent": "yordamchi-bot:1.0:telegram"}
    body = {
        "prompt": f"{prompt} ### {NEGATIVE}",
        "params": {"width": w, "height": h, "steps": 30, "cfg_scale": 7.5, "sampler_name": "k_euler_a", "n": 1},
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


async def make_image(text: str, style: str = "none", size: str = "sq") -> bytes:
    ratio, pw, ph, hw, hh = SIZES[size]
    suffix = STYLES[style]
    # 1) Gemini: o'zbekchani o'zi tushunadi
    if GEMINI_KEY:
        try:
            return await gemini_image(text + suffix, ratio)
        except Exception as e:
            logging.warning("Gemini xatosi: %s", e)
    # 2) Inglizchaga o'girib, bepul xizmatlar
    clean = re.sub(r"\s+", " ", FILLER.sub(" ", text)).strip() or text
    prompt = await translate(clean, "en") + suffix + (QUALITY if style in ("none", "real") else QUALITY_ART)
    logging.info("Inglizcha prompt: %s", prompt)
    try:
        return await pollinations_image(prompt, pw, ph)
    except Exception as e:
        logging.warning("Pollinations ishlamadi, AI Horde sinaladi: %s", e)
    return await horde_image(prompt, hw, hh)


# ---------- Ovoz (matn -> ovoz) ----------
VOICES = {
    "uz": {"male": "uz-UZ-SardorNeural", "female": "uz-UZ-MadinaNeural"},
    "ru": {"male": "ru-RU-DmitryNeural", "female": "ru-RU-SvetlanaNeural"},
    "en": {"male": "en-US-GuyNeural", "female": "en-US-JennyNeural"},
}
FEMALE = {"girl", "fem"}
PITCH = {"boy": "+20Hz", "girl": "+5Hz", "robot": "+0Hz", "hacker": "-35Hz", "old": "-25Hz", "fem": "+0Hz"}
RATE = {"slow": "-25%", "norm": "+0%", "fast": "+25%"}
ROBOT = "afftfilt=real='hypot(re,im)*sin(0)':imag='hypot(re,im)*cos(0)':win_size=512:overlap=0.75"
EFFECTS = {
    "robot": ROBOT,
    "fem": ROBOT,
    "hacker": "acrusher=level_in=3:level_out=5:bits=6:mode=lin:aa=0,aecho=0.8:0.8:25:0.6,highpass=f=120",
}


async def synth(text: str, lang: str, kind: str, speed: str = "norm") -> bytes:
    import edge_tts
    import imageio_ffmpeg

    base = VOICES[lang]["female" if kind in FEMALE else "male"]
    with tempfile.TemporaryDirectory() as d:
        mp3, ogg = os.path.join(d, "a.mp3"), os.path.join(d, "a.ogg")
        await edge_tts.Communicate(text, base, pitch=PITCH[kind], rate=RATE[speed]).save(mp3)
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


# ---------- Matnlar (3 til) ----------
LANG_NAMES = {"uz": "🇺🇿 O'zbekcha", "ru": "🇷🇺 Русский", "en": "🇬🇧 English"}

T = {
    "uz": dict(
        contact="Davom etish uchun kontaktingizni yuboring 👇", btn_contact="📱 Kontaktni yuborish",
        bad_contact="Iltimos, faqat o'zingizning kontaktingizni pastdagi tugma orqali yuboring.",
        registered="✅ Rahmat! Ro'yxatdan o'tdingiz.", menu="Tanlang:",
        b1="1️⃣ Rasm yasash", b2="2️⃣ Matnni ovozga aylantirish", b3="3️⃣ Tarjimon", b4="4️⃣ Ovozni matnga aylantirish",
        ask_style="🎨 Uslubni tanlang:", st_none="✨ Oddiy", st_real="📷 Realistik", st_cartoon="🧸 Multfilm",
        st_anime="🌸 Anime", st_3d="🧊 3D",
        ask_size="📐 O'lchamni tanlang:", sz_sq="⬜ Kvadrat", sz_v="📱 Vertikal", sz_h="🖥 Gorizontal",
        ask_img="🖼 Qanday rasm kerak? Yozing.",
        ask_voice="🎙 Ovozni tanlang:", v_boy="👦 O'g'il bola", v_girl="👧 Qiz bola", v_robot="🤖 Robot",
        v_hacker="💻 Haker", v_old="👴 Chol", v_fem="👩‍🚀 Ayol-robot",
        ask_speed="⏱ Tezlikni tanlang:", sp_slow="🐢 Sekin", sp_norm="▶️ Normal", sp_fast="⚡ Tez",
        ask_text="✍️ Ovozli qilinadigan matnni yozing:",
        ask_tr="🌐 Qaysi tilga tarjima qilay?", ask_tr_text="✍️ Tarjima qilinadigan matnni yozing:",
        say="🔊 Ovozli qilish", ask_stt="🎤 Ovozli xabar yuboring, men uni matnga aylantiraman.",
        wait_img="Rasm tayyorlanmoqda...", wait_tts="Ovoz tayyorlanmoqda...", wait_stt="Ovoz tinglanmoqda...",
        too_long="Matn juda uzun (eng ko'pi 500 belgi).", stt_long="Ovoz juda uzun (eng ko'pi 60 soniya).",
        stt_empty="Ovozdan matn topilmadi.", stt_off="Bu funksiya hozir mavjud emas.",
        limit="Bugungi limit tugadi ({u}/{n}). Ertaga qayta urinib ko'ring.",
        cooldown="Biroz kuting ({n} soniya) va qayta urinib ko'ring.", err="Xatolik: {e}",
    ),
    "ru": dict(
        contact="Для продолжения отправьте свой контакт 👇", btn_contact="📱 Отправить контакт",
        bad_contact="Пожалуйста, отправьте только свой контакт кнопкой ниже.",
        registered="✅ Спасибо! Вы зарегистрированы.", menu="Выберите:",
        b1="1️⃣ Создать картинку", b2="2️⃣ Озвучить текст", b3="3️⃣ Переводчик", b4="4️⃣ Голос в текст",
        ask_style="🎨 Выберите стиль:", st_none="✨ Обычный", st_real="📷 Реалистичный", st_cartoon="🧸 Мультфильм",
        st_anime="🌸 Аниме", st_3d="🧊 3D",
        ask_size="📐 Выберите размер:", sz_sq="⬜ Квадрат", sz_v="📱 Вертикальный", sz_h="🖥 Горизонтальный",
        ask_img="🖼 Какую картинку создать? Напишите.",
        ask_voice="🎙 Выберите голос:", v_boy="👦 Мальчик", v_girl="👧 Девочка", v_robot="🤖 Робот",
        v_hacker="💻 Хакер", v_old="👴 Старик", v_fem="👩‍🚀 Робот-женщина",
        ask_speed="⏱ Выберите скорость:", sp_slow="🐢 Медленно", sp_norm="▶️ Обычно", sp_fast="⚡ Быстро",
        ask_text="✍️ Напишите текст для озвучки:",
        ask_tr="🌐 На какой язык перевести?", ask_tr_text="✍️ Напишите текст для перевода:",
        say="🔊 Озвучить", ask_stt="🎤 Отправьте голосовое сообщение, я переведу его в текст.",
        wait_img="Создаю картинку...", wait_tts="Создаю голос...", wait_stt="Слушаю голос...",
        too_long="Текст слишком длинный (максимум 500 символов).", stt_long="Голос слишком длинный (максимум 60 секунд).",
        stt_empty="Текст в голосе не найден.", stt_off="Эта функция сейчас недоступна.",
        limit="Лимит на сегодня исчерпан ({u}/{n}). Попробуйте завтра.",
        cooldown="Подождите ({n} сек.) и попробуйте снова.", err="Ошибка: {e}",
    ),
    "en": dict(
        contact="Please share your contact to continue 👇", btn_contact="📱 Share contact",
        bad_contact="Please send only your own contact using the button below.",
        registered="✅ Thanks! You are registered.", menu="Choose:",
        b1="1️⃣ Create image", b2="2️⃣ Text to voice", b3="3️⃣ Translator", b4="4️⃣ Voice to text",
        ask_style="🎨 Choose a style:", st_none="✨ Default", st_real="📷 Realistic", st_cartoon="🧸 Cartoon",
        st_anime="🌸 Anime", st_3d="🧊 3D",
        ask_size="📐 Choose a size:", sz_sq="⬜ Square", sz_v="📱 Vertical", sz_h="🖥 Horizontal",
        ask_img="🖼 What image do you want? Write it.",
        ask_voice="🎙 Choose a voice:", v_boy="👦 Boy", v_girl="👧 Girl", v_robot="🤖 Robot",
        v_hacker="💻 Hacker", v_old="👴 Old man", v_fem="👩‍🚀 Female robot",
        ask_speed="⏱ Choose the speed:", sp_slow="🐢 Slow", sp_norm="▶️ Normal", sp_fast="⚡ Fast",
        ask_text="✍️ Write the text to be voiced:",
        ask_tr="🌐 Translate into which language?", ask_tr_text="✍️ Write the text to translate:",
        say="🔊 Voice it", ask_stt="🎤 Send a voice message and I will turn it into text.",
        wait_img="Creating the image...", wait_tts="Creating the voice...", wait_stt="Listening...",
        too_long="Text is too long (max 500 characters).", stt_long="Voice is too long (max 60 seconds).",
        stt_empty="No text found in the voice.", stt_off="This feature is unavailable right now.",
        limit="Today's limit is used up ({u}/{n}). Try again tomorrow.",
        cooldown="Please wait ({n} sec.) and try again.", err="Error: {e}",
    ),
}

# ---------- Foydalanuvchi holati ----------
STATE: dict = {}    # uid -> {"mode": ..., ...}
PENDING: dict = {}  # uid -> tanlangan til (kontakt kutilmoqda)
PREFS: dict = {}    # uid -> {"style": ..., "size": ...}
LAST: dict = {}     # uid -> oxirgi so'rov vaqti
AWAY_SENT: dict = {}  # chat_id -> avto-javob vaqti


def get_user(uid):
    return db.execute("SELECT lang, phone FROM users WHERE id=?", (uid,)).fetchone()


def lang_of(uid) -> str:
    row = get_user(uid)
    return row[0] if row else "uz"


def prefs(uid) -> dict:
    return PREFS.setdefault(uid, {"style": "none", "size": "sq"})


def cooldown_left(uid) -> int:
    if uid == OWNER_ID:
        return 0
    left = COOLDOWN - (time.monotonic() - LAST.get(uid, 0))
    return int(left) + 1 if left > 0 else 0


# ---------- Tugmalar ----------
def ikb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


def menu_rows(l):
    return [[(T[l]["b1"], "m:img")], [(T[l]["b2"], "m:tts")], [(T[l]["b3"], "m:tr")], [(T[l]["b4"], "m:stt")]]


def menu_kb(l):
    return ikb(menu_rows(l))


def say_kb(l):
    return ikb([[(T[l]["say"], "say")]] + menu_rows(l))


def lang_kb():
    return ikb([[(LANG_NAMES[k], "lang:" + k) for k in LANG_NAMES]])


def tr_kb():
    return ikb([[(LANG_NAMES[k], "tl:" + k) for k in LANG_NAMES]])


def style_kb(l):
    b = lambda k: (T[l]["st_" + k], "sy:" + k)
    return ikb([[b("none"), b("real")], [b("cartoon"), b("anime")], [b("3d")]])


def size_kb(l):
    b = lambda k: (T[l]["sz_" + k], "sz:" + k)
    return ikb([[b("sq")], [b("v"), b("h")]])


def voice_kb(l):
    b = lamb
