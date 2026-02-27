# bot.py
import asyncio
import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import FSInputFile, Message
from dotenv import load_dotenv
import yt_dlp

# ────────────────────────────────────────────────
# Глобальна змінна для username бота
# ────────────────────────────────────────────────
BOT_USERNAME = None

# ────────────────────────────────────────────────
# Налаштування
# ────────────────────────────────────────────────

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не знайдено в .env")

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

MAX_FILE_SIZE_MB = 48
SEARCH_LIMIT = 3
MAX_PLAYLIST_ITEMS = 1
REQUEST_MAX_LENGTH = 100

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# ────────────────────────────────────────────────
# Функція для отримання username бота (викликається один раз)
# ────────────────────────────────────────────────

async def load_bot_username():
    global BOT_USERNAME
    try:
        me = await bot.get_me()
        BOT_USERNAME = me.username
        logger.info(f"Бот запущено як @{BOT_USERNAME}")
    except Exception as e:
        logger.error(f"Не вдалося отримати username бота: {e}")
        BOT_USERNAME = None

# ────────────────────────────────────────────────
# Стани
# ────────────────────────────────────────────────

class SearchForm(StatesGroup):
    waiting_for_query = State()

# ────────────────────────────────────────────────
# Допоміжні функції
# ────────────────────────────────────────────────

def sanitize_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', '_', name).strip()

def get_user_dir(user_id: int) -> Path:
    path = DOWNLOAD_DIR / f"user_{user_id}"
    path.mkdir(exist_ok=True)
    return path

def clean_user_dir(user_dir: Path):
    if user_dir.exists():
        shutil.rmtree(user_dir, ignore_errors=True)

def format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "??:??"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

async def download_and_send(
    message: Message,
    query: str,
    state: FSMContext = None
):
    user_id = message.from_user.id
    user_dir = get_user_dir(user_id)

    try:
        status_msg = await message.answer("🔍 Шукаю...")

        ydl_opts_search = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "default_search": "ytsearch",
        }

        with yt_dlp.YoutubeDL(ydl_opts_search) as ydl:
            try:
                search_result = ydl.extract_info(f"ytsearch{SEARCH_LIMIT}:{query}", download=False)
            except Exception as e:
                logger.exception("Помилка пошуку")
                await status_msg.edit_text("Не вдалося знайти трек 😔\nСпробуйте інший запит.")
                return

        if "entries" not in search_result or not search_result["entries"]:
            await status_msg.edit_text("Нічого не знайдено за запитом.\nСпробуйте змінити формулювання.")
            return

        entry = search_result["entries"][0]
        url = entry["url"]
        title = sanitize_filename(entry.get("title", "Unknown title"))
        duration = entry.get("duration")
        uploader = entry.get("uploader", "Unknown artist")
        thumbnail = entry.get("thumbnails", [{}])[0].get("url")

        await status_msg.edit_text(
            f"🎵 <b>{title}</b>\n"
            f"👤 {uploader}\n"
            f"⏱ {format_duration(duration)}\n\n"
            "Завантажую та конвертую в mp3... ⏳"
        )

        ydl_opts_download = {
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "0",
            }],
            "outtmpl": str(user_dir / f"{title}.%(ext)s"),
            "addmetadata": True,
            "embedthumbnail": True,
            "parse_metadata": "title:%(track)s",
            "parse_metadata": "uploader:%(artist)s",
            "quiet": True,
            "continuedl": True,
            "restrict_filenames": True,
        }

        with yt_dlp.YoutubeDL(ydl_opts_download) as ydl:
            info = ydl.extract_info(url, download=True)

        # Визначаємо шлях до файлу
        if "filepath" in info and info["filepath"]:
            filepath = Path(info["filepath"])
        else:
            mp3_files = list(user_dir.glob("*.mp3"))
            if mp3_files:
                filepath = mp3_files[0]
                logger.info(f"Використано fallback: знайдено {filepath}")
            else:
                await status_msg.edit_text("Не вдалося знайти готовий mp3 після конвертації 😢")
                return

        logger.info(f"Фінальний файл: {filepath}")

        if not filepath.exists():
            await status_msg.edit_text("Файл не створено після завантаження 😢")
            return

        file_size_mb = filepath.stat().st_size / (1024 * 1024)

        if file_size_mb > MAX_FILE_SIZE_MB:
            await status_msg.edit_text(
                f"Файл завеликий ({file_size_mb:.1f} MB > {MAX_FILE_SIZE_MB} MB).\n"
                "Telegram не дозволяє надсилати такі файли без Premium."
            )
            filepath.unlink(missing_ok=True)
            return

        await status_msg.edit_text("Надсилаю аудіо... 📤")

        audio = FSInputFile(filepath)
        caption_text = (
            f"<b>{title}</b>\n"
            f"Виконавець: {uploader}\n"
            f"Тривалість: {format_duration(duration)}\n"
            f"Запит: {query}"
        )
        if BOT_USERNAME:
            caption_text += f"\n@{BOT_USERNAME}"

        await message.answer_audio(
            audio=audio,
            title=title,
            performer=uploader,
            duration=int(duration) if duration else None,
            thumbnail=types.URLInputFile(thumbnail) if thumbnail else None,
            caption=caption_text
        )

        await status_msg.delete()

    except Exception as e:
        logger.exception("Критична помилка")
        error_text = f"Сталася помилка: {str(e)[:200]}..."
        try:
            await status_msg.edit_text(error_text)
        except:
            await message.answer(error_text)

    finally:
        clean_user_dir(user_dir)
        if state:
            await state.clear()

# ────────────────────────────────────────────────
# Хендлери
# ────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Привіт! Я бот, який шукає і надсилає музику 🎧\n\n"
        "Просто напиши назву пісні та/або виконавця, наприклад:\n"
        "• dua lipa houdini\n"
        "• the weeknd blinding lights\n"
        "• кравець пам’ятаєш\n\n"
        "<i>Працюю через YouTube → mp3</i>"
    )

@router.message(Command("search"))
async def cmd_search(message: Message, state: FSMContext):
    await message.answer("Напиши назву пісні / виконавця:")
    await state.set_state(SearchForm.waiting_for_query)

@router.message(F.text.startswith(("http://", "https://")))
async def handle_possible_link(message: Message):
    await message.answer("Я зараз приймаю тільки текстовий запит (назва + виконавець).\nНадішли, наприклад: «the weeknd blinding lights»")

@router.message()
async def handle_text_query(message: Message, state: FSMContext):
    query = message.text.strip()
    if len(query) < 3:
        await message.answer("Запит занадто короткий. Напиши хоча б 3 символи.")
        return
    if len(query) > REQUEST_MAX_LENGTH:
        await message.answer("Запит занадто довгий. Спробуй коротше.")
        return
    if re.search(r"https?://", query):
        await message.answer("Я зараз працюю тільки з текстовими запитами.\nНадішли назву пісні / виконавця.")
        return
    await download_and_send(message, query, state)

# ────────────────────────────────────────────────
# Запуск
# ────────────────────────────────────────────────

async def main():
    logger.info("Бот запускається...")
    await load_bot_username()          # ← отримуємо @username один раз
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
