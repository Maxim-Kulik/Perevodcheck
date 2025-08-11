# main.py
import asyncio
import os
import logging
from typing import Dict, List, Set

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from flyerapi import Flyer

logging.basicConfig(level=logging.INFO)
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
    Защищено от сбоев Flyer: не падаем на исключениях, пробуем несколько языков.
    """
    unique: List[dict] = []
    seen = set(exclude)

    # Порядок попыток: язык пользователя -> ru -> en -> без языкового фильтра
    attempts = []
    if language_code:
        attempts.append(language_code)
    attempts += ["ru", "en", None]

    for lang in attempts:
        for _ in range(3):
            try:
                kwargs = {"user_id": user_id, "limit": limit + 5}
                if lang is not None:
                    kwargs["language_code"] = lang
                resp = await flyer.get_tasks(**kwargs)
                tasks = resp if isinstance(resp, list) else []
            except Exception as e:
                logging.warning(f"[flyer.get_tasks lang={lang}] {e!r}")
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
    lang = (message.from_user.language_code or "ru")
    STATE[user_id] = {"stage": 1, "known_signatures": set(), "batch_signatures": set()}

    tasks = await fetch_unique_tasks(user_id, lang, set(), BATCH_SIZE)
    if len(tasks) < BATCH_SIZE:
        await message.answer("Пока нет доступных заданий. Попробуй позже.")
        return

    sigs = {t.get("signature") for t in tasks if t.get("signature")}
    STATE[user_id]["batch_signatures"] = sigs
    STATE[user_id]["known_signatures"] = set(sigs)

    text = (
        "<b>Доступ к функционалу</b>\n\n"
        f"1) Подпишись на {BATCH_SIZE} каналов ниже\n"
        "2) Нажми «Проверить выполнение»\n"
        "После этого я попрошу подписаться ещё на такую же пачку — и открою доступ."
    )
    await message.answer(text, reply_markup=build_tasks_kb(tasks, stage=1).as_markup())


@dp.message(CommandStart())
async def on_start(message: types.Message):
    await start_flow(message)


async def edit_or_send(call: types.CallbackQuery | types.Message, text: str, markup=None):
    """
    Всегда отправляем НОВОЕ сообщение (не редактируем),
    чтобы не ловить 'Prohibited method for a bot type'.
    """
    if isinstance(call, types.CallbackQuery):
        try:
            await call.answer()
        except Exception:
            pass
        chat_id = call.message.chat.id
    else:
        chat_id = call.chat.id
    await bot.send_message(chat_id, text, reply_markup=markup)


@dp.callback_query(F.data.startswith("verify:"))
async def on_verify(call: types.CallbackQuery):
    user_id = call.from_user.id
    lang = (call.from_user.language_code or "ru")
    st = STATE.get(user_id)

    if not st:
        await edit_or_send(call, "Сессия не найдена, начни заново: /start")
        return

    stage = int(call.data.split(":", 1)[1])
    if st["stage"] != stage:
        await call.answer("Эта кнопка устарела, нажми /start", show_alert=True)
        return

    batch = list(st["batch_signatures"])
    if not batch:
        await edit_or_send(call, "Нет активных заданий. Попробуй /start")
        return

    ok = 0
    for sig in batch:
        try:
            if await flyer.check_task(user_id=user_id, signature=sig):
                ok += 1
        except Exception as e:
            logging.warning(f"[flyer.check_task] {sig}: {e!r}")

    if ok < len(batch):
        await edit_or_send(call, f"Выполнено {ok}/{len(batch)}. Подпишись на все и нажми «Проверить выполнение».",
                           call.message.reply_markup)
        return

    if stage == 1:
        st["stage"] = 2
        tasks2 = await fetch_unique_tasks(user_id, lang, st["known_signatures"], BATCH_SIZE)
        sigs2 = {t.get("signature") for t in tasks2 if t.get("signature")}
        # гарантия, что 2-я пачка не пересекается с 1-й
        if (len(tasks2) < BATCH_SIZE) or (sigs2 & st["known_signatures"]):
            await edit_or_send(call, "Вторая пачка заданий недоступна. Попробуй позже /start")
            return
        st["batch_signatures"] = sigs2
        st["known_signatures"].update(sigs2)
        text = (
            "<b>Отлично!</b> Первая пачка готова.\n"
            f"Теперь подпишись ещё на {BATCH_SIZE} каналов и нажми «Проверить выполнение»."
        )
        await edit_or_send(call, text, build_tasks_kb(tasks2, stage=2).as_markup())
    else:
        await edit_or_send(call, "<b>Готово!</b> Ты выполнил все задания.\n" f"Твоя ссылка на бота: {TARGET_BOT_URL}")
        STATE.pop(user_id, None)


async def main():
    # убрать webhook, чтобы не конфликтовал с long polling
    await bot.delete_webhook(drop_pending_updates=True)

    # проверить токен — в логах увидишь имя бота
    me = await bot.get_me()
    logging.info(f"OK: logged in as @{me.username} (id={me.id})")

    # запустить long polling
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
