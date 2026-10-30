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
    b = lambda k: (T[l]["v_" + k], "v:" + k)
    return ikb([[b("boy"), b("girl")], [b("old"), b("robot")], [b("fem"), b("hacker")]])


def speed_kb(l):
    b = lambda k: (T[l]["sp_" + k], "sp:" + k)
    return ikb([[b("slow"), b("norm"), b("fast")]])


def contact_kb(l):
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=T[l]["btn_contact"], request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True)


PICKERS = {  # tanlash kutilayotgan holat -> (matn kaliti, tugmalar)
    "style_pick": ("ask_style", style_kb), "size_pick": ("ask_size", size_kb),
    "voice_pick": ("ask_voice", voice_kb), "speed_pick": ("ask_speed", speed_kb),
    "tr_pick": ("ask_tr", lambda l: tr_kb()),
}


async def ask_language(target: Message):
    await target.answer("Tilni tanlang / Выберите язык / Choose language:", reply_markup=lang_kb())


# ---------- Taqiqlangan foydalanuvchilar ----------
class BanMW(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = getattr(event, "from_user", None)
        if u and u.id in BANNED and u.id != OWNER_ID:
            return None
        return await handler(event, data)


dp.message.outer_middleware(BanMW())
dp.callback_query.outer_middleware(BanMW())


# ---------- Boshlash, til, kontakt ----------
@dp.message(CommandStart())
@dp.message(Command("menu"))
async def start(m: Message):
    uid = m.from_user.id
    if uid == OWNER_ID and not get_user(uid):
        db.execute("INSERT OR REPLACE INTO users VALUES (?,?,?,?,?,?)",
                   (uid, "uz", "-", m.from_user.full_name, m.from_user.username or "", now()))
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
    if l not in T:
        return await cb.answer()
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
               (uid, l, m.contact.phone_number, m.from_user.full_name, m.from_user.username or "", now()))
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


# ---------- Menyu tugmalari ----------
async def show_picker(target: Message, uid: int, mode: str):
    STATE[uid] = {"mode": mode}
    l = lang_of(uid)
    key, kb = PICKERS[mode]
    await target.answer(T[l][key], reply_markup=kb(l))


async def choose(target: Message, uid: int, what: str):
    if what == "img":
        await show_picker(target, uid, "style_pick")
    elif what == "tts":
        await show_picker(target, uid, "voice_pick")
    elif what == "tr":
        await show_picker(target, uid, "tr_pick")
    else:
        STATE[uid] = {"mode": "stt"}
        await target.answer(T[lang_of(uid)]["ask_stt"])


@dp.callback_query(F.data.startswith("m:"))
async def menu_cb(cb: CallbackQuery):
    uid, what = cb.from_user.id, cb.data.split(":")[1]
    await cb.answer()
    if not get_user(uid):
        await ask_language(cb.message)
    elif what in ("img", "tts", "tr", "stt"):
        await choose(cb.message, uid, what)


@dp.callback_query(F.data.startswith("sy:"))
async def style_cb(cb: CallbackQuery):
    uid, k = cb.from_user.id, cb.data.split(":")[1]
    await cb.answer()
    if get_user(uid) and k in STYLES:
        prefs(uid)["style"] = k
        await show_picker(cb.message, uid, "size_pick")


@dp.callback_query(F.data.startswith("sz:"))
async def size_cb(cb: CallbackQuery):
    uid, k = cb.from_user.id, cb.data.split(":")[1]
    await cb.answer()
    if get_user(uid) and k in SIZES:
        l, p = lang_of(uid), prefs(uid)
        p["size"] = k
        STATE[uid] = {"mode": "img"}
        await cb.message.answer(f"{T[l]['st_' + p['style']]} • {T[l]['sz_' + k]}\n{T[l]['ask_img']}")


@dp.callback_query(F.data.startswith("v:"))
async def voice_cb(cb: CallbackQuery):
    uid, k = cb.from_user.id, cb.data.split(":")[1]
    await cb.answer()
    if get_user(uid) and k in PITCH:
        STATE[uid] = {"mode": "speed_pick", "voice": k}
        l = lang_of(uid)
        await cb.message.answer(T[l]["ask_speed"], reply_markup=speed_kb(l))


@dp.callback_query(F.data.startswith("sp:"))
async def speed_cb(cb: CallbackQuery):
    uid, k = cb.from_user.id, cb.data.split(":")[1]
    await cb.answer()
    voice = STATE.get(uid, {}).get("voice")
    if not get_user(uid) or k not in RATE:
        return
    if not voice:
        return await show_picker(cb.message, uid, "voice_pick")
    l = lang_of(uid)
    STATE[uid] = {"mode": "tts", "voice": voice, "speed": k}
    await cb.message.answer(f"{T[l]['v_' + voice]} • {T[l]['sp_' + k]}\n{T[l]['ask_text']}")


@dp.callback_query(F.data.startswith("tl:"))
async def tl_cb(cb: CallbackQuery):
    uid, k = cb.from_user.id, cb.data.split(":")[1]
    await cb.answer()
    if get_user(uid) and k in VOICES:
        STATE[uid] = {"mode": "tr", "tl": k}
        await cb.message.answer(f"{LANG_NAMES[k]}\n{T[lang_of(uid)]['ask_tr_text']}")


@dp.callback_query(F.data == "say")
async def say_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    await cb.answer()
    last = STATE.get(uid, {}).get("last")
    if not get_user(uid) or not last:
        return
    l = lang_of(uid)
    if limit_hit(uid, "voice"):
        return await cb.message.answer(T[l]["limit"].format(u=used(uid, "voice"), n=LIMITS["voice"]))
    try:
        audio = await synth(last[0][:MAX_LEN], last[1], "boy")
        add_use(uid, "voice")
        await cb.message.answer_voice(BufferedInputFile(audio, "voice.ogg"), reply_markup=menu_kb(l))
    except Exception as e:
        logging.warning("TTS xatosi: %s", e)
        await cb.message.answer(T[l]["err"].format(e=str(e)[:200]))


# ---------- Ish: rasm, ovoz, tarjima, ovoz->matn ----------
async def guard(m: Message, l: str, kind: str = None, check_len: bool = True):
    """Cheklovlarni tekshiradi. Muammo bo'lsa xabar yuborib True qaytaradi."""
    uid = m.from_user.id
    if check_len and m.text and len(m.text) > MAX_LEN:
        await m.answer(T[l]["too_long"])
        return True
    if kind and (n := cooldown_left(uid)):
        await m.answer(T[l]["cooldown"].format(n=n))
        return True
    if kind and limit_hit(uid, kind):
        await m.answer(T[l]["limit"].format(u=used(uid, kind), n=LIMITS[kind]))
        return True
    if kind:
        LAST[uid] = time.monotonic()
    return False


async def do_image(m: Message, l: str):
    uid = m.from_user.id
    if await guard(m, l, "img"):
        return
    p = prefs(uid)
    wait = await m.answer(T[l]["wait_img"])
    try:
        img = await make_image(m.text, p["style"], p["size"])
        add_use(uid, "img")
        try:
            await m.answer_photo(BufferedInputFile(img, "image.png"), caption=m.text[:900], reply_markup=menu_kb(l))
        except Exception:
            await m.answer_document(BufferedInputFile(img, "image.webp"), caption=m.text[:900], reply_markup=menu_kb(l))
    except Exception as e:
        await m.answer(T[l]["err"].format(e=str(e)[:200]))
    finally:
        await wait.delete()


async def do_tts(m: Message, l: str, st: dict):
    uid = m.from_user.id
    if await guard(m, l, "voice"):
        return
    wait = await m.answer(T[l]["wait_tts"])
    try:
        audio = await synth(m.text, l, st["voice"], st.get("speed", "norm"))
        add_use(uid, "voice")
        await m.answer_voice(BufferedInputFile(audio, "voice.ogg"), reply_markup=menu_kb(l))
    except Exception as e:
        logging.warning("TTS xatosi: %s", e)
        await m.answer(T[l]["err"].format(e=str(e)[:200]))
    finally:
        await wait.delete()


async def do_translate(m: Message, l: str, st: dict):
    if await guard(m, l):
        return
    res = await translate(m.text, st["tl"])
    st["last"] = (res, st["tl"])
    await m.answer(res[:4000], reply_markup=say_kb(l))


async def gate(m: Message):
    """Ro'yxatdan o'tgan bo'lsa tilini qaytaradi, bo'lmasa ro'yxatdan o'tkazishni boshlaydi."""
    uid = m.from_user.id
    if get_user(uid):
        return lang_of(uid)
    if uid in PENDING:
        await m.answer(T[PENDING[uid]]["contact"], reply_markup=contact_kb(PENDING[uid]))
    else:
        await ask_language(m)
    return None


@dp.message(F.voice | F.audio | F.video_note, F.chat.type == "private")
async def on_voice(m: Message):
    l = await gate(m)
    if not l:
        return
    uid = m.from_user.id
    obj = m.voice or m.audio or m.video_note
    if not GEMINI_KEY:
        return await m.answer(T[l]["stt_off"])
    if (obj.duration or 0) > 60:
        return await m.answer(T[l]["stt_long"])
    if await guard(m, l, "voice"):
        return
    mime = "audio/ogg" if m.voice else ("video/mp4" if m.video_note else (obj.mime_type or "audio/mpeg"))
    wait = await m.answer(T[l]["wait_stt"])
    try:
        buf = io.BytesIO()
        await bot.download(obj, destination=buf)
        text = await gemini_stt(buf.getvalue(), mime)
        add_use(uid, "voice")
        await m.reply(text[:4000] if text else T[l]["stt_empty"], reply_markup=menu_kb(l))
    except Exception as e:
        logging.warning("STT xatosi: %s", e)
        await m.answer(T[l]["err"].format(e=str(e)[:200]))
    finally:
        await wait.delete()


# ---------- Egasi buyruqlari ----------
OWNER_ONLY = F.from_user.id == OWNER_ID


def total(kind, day=None) -> int:
    q, a = "SELECT COALESCE(SUM(n),0) FROM usage WHERE kind=?", [kind]
    if day:
        q += " AND day=?"
        a.append(day)
    return db.execute(q, a).fetchone()[0]


@dp.message(Command("stats"), OWNER_ONLY)
async def cmd_stats(m: Message):
    n_users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    new_today = db.execute("SELECT COUNT(*) FROM users WHERE joined LIKE ?", (today() + "%",)).fetchone()[0]
    d = today()
    await m.answer(
        "📊 Statistika\n"
        f"👥 Foydalanuvchilar: {n_users} (bugun yangi: {new_today})\n"
        f"🚫 Bloklangan: {len(BANNED)}\n"
        f"🖼 Rasmlar: {total('img')} (bugun: {total('img', d)})\n"
        f"🎙 Ovoz amallari: {total('voice')} (bugun: {total('voice', d)})\n"
        f"💬 Avto-javob: {'yoqilgan' if get_setting('away') else 'o`chirilgan'}\n"
        f"⚙️ Limit: rasm {DAILY_IMAGES}/kun, ovoz {DAILY_VOICES}/kun")


@dp.message(Command("users"), OWNER_ONLY)
async def cmd_users(m: Message):
    rows = db.execute("SELECT id, name, username, phone, lang FROM users ORDER BY joined DESC LIMIT 40").fetchall()
    if not rows:
        return await m.answer("Hali foydalanuvchi yo'q.")
    lines = [f"{i}. {n} {('@' + u) if u else ''} {p} [{lg}] ID:{uid}" for i, (uid, n, u, p, lg) in enumerate(rows, 1)]
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) > 3800:
            await m.answer(chunk)
            chunk = ""
        chunk += line + "\n"
    await m.answer(chunk)


@dp.message(Command("broadcast"), OWNER_ONLY)
async def cmd_broadcast(m: Message, command: CommandObject):
    if not command.args:
        return await m.answer("Ishlatilishi: /broadcast matn")
    ids = [r[0] for r in db.execute("SELECT id FROM users") if r[0] not in BANNED and r[0] != OWNER_ID]
    ok = fail = 0
    for uid in ids:
        try:
            await bot.send_message(uid, command.args)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await m.answer(f"📢 Yuborildi: {ok}, yuborilmadi: {fail}")


@dp.message(Command("ban"), OWNER_ONLY)
async def cmd_ban(m: Message, command: CommandObject):
    if not command.args or not command.args.strip().isdigit():
        return await m.answer("Ishlatilishi: /ban ID")
    uid = int(command.args.strip())
    BANNED.add(uid)
    db.execute("INSERT OR IGNORE INTO banned VALUES (?)", (uid,))
    db.commit()
    await m.answer(f"🚫 {uid} bloklandi.")


@dp.message(Command("unban"), OWNER_ONLY)
async def cmd_unban(m: Message, command: CommandObject):
    if not command.args or not command.args.strip().isdigit():
        return await m.answer("Ishlatilishi: /unban ID")
    uid = int(command.args.strip())
    BANNED.discard(uid)
    db.execute("DELETE FROM banned WHERE id=?", (uid,))
    db.commit()
    await m.answer(f"✅ {uid} blokdan chiqarildi.")


@dp.message(Command("away"), OWNER_ONLY)
async def cmd_away(m: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg:
        cur = get_setting("away")
        return await m.answer(f"💬 Avto-javob: {cur}" if cur else
                              "Avto-javob o'chiq.\nYoqish: /away matn\nO'chirish: /away off")
    if arg.lower() == "off":
        set_setting("away", None)
        return await m.answer("Avto-javob o'chirildi.")
    set_setting("away", arg)
    await m.answer(f"✅ Avto-javob yoqildi:\n{arg}")


# ---------- Matn kelganda ----------
@dp.message(F.text, F.chat.type == "private")
async def on_text(m: Message):
    uid = m.from_user.id
    l = await gate(m)
    if not l:
        return
    t = m.text.strip()
    if t.startswith("/"):
        return await m.answer(T[l]["menu"], reply_markup=menu_kb(l))
    if t in ("1", "2", "3", "4"):
        return await choose(m, uid, {"1": "img", "2": "tts", "3": "tr", "4": "stt"}[t])
    st = STATE.get(uid)
    if not st:
        await m.answer(T[l]["menu"], reply_markup=menu_kb(l))
    elif st["mode"] == "img":
        await do_image(m, l)
    elif st["mode"] == "tts":
        await do_tts(m, l, st)
    elif st["mode"] == "tr":
        await do_translate(m, l, st)
    elif st["mode"] == "stt":
        await m.answer(T[l]["ask_stt"])
    elif st["mode"] in PICKERS:
        key, kb = PICKERS[st["mode"]]
        await m.answer(T[l][key], reply_markup=kb(l))


# ---------- Business: tahrirlangan / o'chirilgan / avto-javob ----------
async def maybe_away(m: Message):
    text = get_setting("away")
    if not text or not m.from_user or m.from_user.is_bot or m.from_user.id == OWNER_ID:
        return
    if time.monotonic() - AWAY_SENT.get(m.chat.id, -1e9) < 6 * 3600:  # bir odamga 6 soatda 1 marta
        return
    AWAY_SENT[m.chat.id] = time.monotonic()
    await m.answer(text)


@dp.business_message()
async def on_msg(m: Message):
    save(m)
    try:
        await maybe_away(m)
    except Exception as e:
        logging.warning("Avto-javob xatosi: %s", e)


@dp.edited_business_message()
async def on_edit(m: Message):
    old = load(m.chat.id, m.message_id)
    new_text = text_of(m)
    ft, fid = media_of(m)
    if old:
        changed_media = bool(old[3] and fid and old[3] != fid)
        if old[1] != new_text or changed_media:
            await bot.send_message(OWNER_ID, f"✏️ Tahrirlandi ({old[0]})\n\nOldin: {old[1]}\n\nHozir: {new_text}")
            if changed_media:
                try:
                    await send_saved(old[2], old[3], "Oldingi fayl")
                except Exception as e:
                    logging.warning("Eski faylni yuborib bo'lmadi: %s", e)
    save(m)


@dp.deleted_business_messages()
async def on_delete(e: BusinessMessagesDeleted):
    for mid in e.message_ids:
        row = load(e.chat.id, mid)
        if not row:
            await bot.send_message(OWNER_ID, "🗑 Xabar o'chirildi (bazada yo'q edi).")
            continue
        await bot.send_message(OWNER_ID, f"🗑 O'chirildi ({row[0]})\n\n{row[1]}")
        if row[3]:
            try:
                await send_saved(row[2], row[3], f"🗑 {row[0]}")
            except Exception as ex:
                logging.warning("O'chirilgan faylni yuborib bo'lmadi: %s", ex)


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


async def set_commands():
    try:
        user_cmds = [BotCommand(command="menu", description="Menu"), BotCommand(command="lang", description="Til / Язык / Language")]
        await bot.set_my_commands(user_cmds)
        if OWNER_ID:
            owner_cmds = user_cmds + [
                BotCommand(command="stats", description="Statistika"),
                BotCommand(command="users", description="Foydalanuvchilar"),
                BotCommand(command="broadcast", description="Hammaga xabar"),
                BotCommand(command="ban", description="Bloklash"),
                BotCommand(command="unban", description="Blokdan chiqarish"),
                BotCommand(command="away", description="Avto-javob"),
            ]
            await bot.set_my_commands(owner_cmds, scope=BotCommandScopeChat(chat_id=OWNER_ID))
    except Exception as e:
        logging.warning("Buyruqlar menyusi o'rnatilmadi: %s", e)


async def main():
    asyncio.create_task(keep_alive())
    await set_commands()
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
    
