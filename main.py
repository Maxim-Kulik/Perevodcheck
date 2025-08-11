# main.py
import asyncio
import os
from typing import Dict, List, Set

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from flyerapi import Flyer

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
FLYER_KEY = os.getenv("FLYER_KEY")
TARGET_BOT_URL = os.getenv("TARGET_BOT_URL", "https://t.me/your_bot")
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "5"))

if not BOT_TOKEN or not FLYER_KEY:
    raise RuntimeError("BOT_TOKEN и FLYER_KEY обязательны (задать в переменных окружения).")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
flyer = Flyer(FLYER_KEY)

# простое состояние в памяти: user_id -> {stage, known_signatures, batch_signatures}
STATE: Dict[int, Dict] = {}


def _extract_task_url(t: dict) -> str:
    for k in ("url", "link", "tg_link", "button_url"):
        if t.get(k):
            return t[k]
    return "https://t.me/FlyerServiceBot"


def build_tasks_kb(tasks: List[dict], stage: int) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for i, t in enumerate(tasks, 1):
        title = t.get("title") or t.get("text") or f"Задание {i}"
        kb.button(text=f"🔗 {title}", url=_extract_task_url(t))
    kb.button(text="✅ Проверить выполнение", callback_data=f"verify:{stage}")
    kb.adjust(1)
    return kb


async def fetch_unique_tasks(user_id: int, language_code: str, exclude: Set[str], limit: int) -> List[dict]:
    """
    Возвращает до `limit` задач, подписи которых (signature) отсутствуют в exclude.
    Защищаемся от сбоев/нестандартных ответов Flyer.
    """
    unique: List[dict] = []
    seen = set(exclude)

    for _ in range(5):  # несколько попыток набрать уникальные задачи
        tasks: List[dict] = []
        try:
            raw = await flyer.get_tasks(user_id=user_id, language_code=language_code, limit=limit + 5)
            if isinstance(raw, list):
                tasks = raw
            else:
                # если библиотека вернула не список (или словарь-ошибку) — трактуем как отсутствие задач
                tasks = []
        except Exception as e:
            # сюда попадёт и KeyError внутри flyerapi, и сетевые ошибки
            print(f"[flyer.get_tasks] error: {repr(e)}")
            await asyncio.sleep(0.5)
            tasks = []

        for t in tasks:
            sig = t.get("signature")
            if not sig or sig in seen:
                continue
            unique.append(t)
            seen.add(sig)
            if len(unique) >= limit:
                return unique

        await asyncio.sleep(0.2)

    return unique


async def start_flow(message: types.Message):
    user_id = message.from_user.id
    lang = (message.from_user.language_
