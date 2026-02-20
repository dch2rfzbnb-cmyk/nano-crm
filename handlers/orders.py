"""Обработка заказов и сообщений менеджеров."""
import asyncio
import logging
import re
from collections import defaultdict
from datetime import datetime, date, time, timedelta
from typing import Optional
from aiogram import Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReactionTypeEmoji,
)
from aiogram.filters import Command

from models import Order
from db import (
    insert_order,
    update_order_status,
    get_order_by_id,
    get_order_by_chat_and_message,
    search_orders,
    get_daily_report_enabled,
    update_order_after_edit,
    is_user_authorized,
    authorize_user,
    update_order_by_id,  # ← ПРАВИЛЬНЫЙ ИМПОРТ
    DB_PATH,
)
from keyboards import get_main_keyboard, KEYBOARD_BUTTONS, BUTTON_SEARCH
from config import BOT_PIN

logger = logging.getLogger(__name__)
router = Router()

# Глобальный словарь состояний для ожидания PIN
user_states = {}  # user_id -> "waiting_pin"

# Словарь состояний для редактирования заказов
user_edit_states = {}  # user_id -> {'editing_id': int}  # ← ПРОСТОЙ СЛОВАРЬ

# Словарь для связи message_id карточки с order_id (для reply)
card_message_to_order = {}  # (chat_id, message_id) -> order_id

# Словарь состояний для редактирования полей
edit_field_states = {}  # user_id -> {'order_id': int, 'field': str}

# Защита от злоупотреблений: отслеживание последнего сообщения от пользователя
last_message_time = defaultdict(lambda: datetime.min)

STATUS_OPTIONS = {
    "new": "🆕",
    "in_progress": "📦",
    "delivery": "🚚",
    "paid": "✅",
    "canceled": "❌",
}

STATUS_DISPLAY = {
    "new": "🆕 Новый",
    "in_progress": "📦 В работе",
    "delivery": "🚚 Доставка",
    "paid": "✅ Оплачен",
    "canceled": "❌ Отказ",
}

STATUS_TO_EMOJI = {
    "new": "👌",
    "in_progress": "🔥",
    "delivery": "🕊️",
    "paid": "👍",
    "canceled": "👎",
}


def _normalize_search_query(text: str) -> str:
    """
    Нормализует поисковый запрос:
    - trim
    - схлопывание множественных пробелов в один
    """
    if not text:
        return ""
    return " ".join(text.strip().split())


def parse_reminder_datetime(comment: str) -> Optional[datetime]:
    """
    Парсит дату и время из комментария для напоминания.
    
    НОВАЯ ЛОГИКА:
    1. ТОЛЬКО ВРЕМЯ ("20:00"):
       - Если сегодня это время еще не наступило → reminder_at = СЕГОДНЯ 19:55
       - Если уже наступило → reminder_at = ЗАВТРА 19:55
       - НЕ зависит от дня недели!
    
    2. ДАТА + ВРЕМЯ ("28.12 20:00"):
       - reminder_at = 28.12 19:55 (всегда за 5 мин)
       - НЕ проверять прошлое время! Даже если дата прошла, всё равно ставить напоминание
    
    Поддерживаемые форматы:
    - "завтра в 15:30" → завтра 15:25
    - "28.12 20:00" → 28.12 19:55 (даже если прошло)
    - "20:00" → сегодня 19:55 (если не прошло) или завтра 19:55
    - "28.12" → 28.12.2025 12:00 (без времени → None)
    
    Возвращает datetime или None, если не найдено.
    """
    if not comment:
        logger.info("parse_reminder: empty comment → None")
        return None
    
    comment_lower = comment.lower()
    now = datetime.now()
    parsed_date = None
    parsed_time = time(12, 0)  # По умолчанию 12:00, если только дата
    
    # Проверяем "завтра"
    if "завтра" in comment_lower:
        parsed_date = (now + timedelta(days=1)).date()
        # Ищем время после "завтра"
        time_match = re.search(r'(\d{1,2}):(\d{2})', comment)
        if time_match:
            hour, minute = map(int, time_match.groups())
            if 0 <= hour < 24 and 0 <= minute < 60:
                parsed_time = time(hour, minute)
    else:
        # Паттерны для дат
        date_patterns = [
            r'(\d{1,2})\.(\d{1,2})\.(\d{4})',  # dd.MM.yyyy
            r'(\d{1,2})\.(\d{1,2})',  # dd.MM
        ]
        
        # Ищем дату
        for pattern in date_patterns:
            match = re.search(pattern, comment)
            if match:
                if len(match.groups()) == 3:  # dd.MM.yyyy
                    day, month, year = map(int, match.groups())
                    parsed_date = date(year, month, day)
                else:  # dd.MM
                    day, month = map(int, match.groups())
                    parsed_date = date(now.year, month, day)
                    # Если дата прошла, считаем что это следующий год
                    if parsed_date < now.date():
                        parsed_date = date(now.year + 1, month, day)
                break
    
    # Ищем время (если еще не нашли для "завтра")
    if "завтра" not in comment_lower or parsed_time == time(12, 0):
        time_match = re.search(r'(\d{1,2}):(\d{2})', comment)
        if time_match:
            hour, minute = map(int, time_match.groups())
            if 0 <= hour < 24 and 0 <= minute < 60:
                parsed_time = time(hour, minute)
    
    # ЛОГИКА 1: ТОЛЬКО ВРЕМЯ (без даты и не дефолтное 12:00)
    if not parsed_date and parsed_time != time(12, 0):
        today_time = datetime.combine(now.date(), parsed_time)
        today_time = today_time.replace(second=0, microsecond=0)
        
        if today_time > now:  # Сегодня это время ещё не наступило
            reminder_dt = today_time - timedelta(minutes=5)
            logger.info(f"parse_reminder: '{comment}' → today time {parsed_time} → {reminder_dt}")
        else:  # Уже наступило → завтра
            tomorrow_time = datetime.combine(now.date() + timedelta(days=1), parsed_time)
            tomorrow_time = tomorrow_time.replace(second=0, microsecond=0)
            reminder_dt = tomorrow_time - timedelta(minutes=5)
            logger.info(f"parse_reminder: '{comment}' → tomorrow time {parsed_time} → {reminder_dt}")
        return reminder_dt
    
    # ЛОГИКА 2: ДАТА + ВРЕМЯ (всегда за 5 мин, НЕ проверяем прошлое!)
    if parsed_date and parsed_time != time(12, 0):
        reminder_dt = datetime.combine(parsed_date, parsed_time)
        reminder_dt = reminder_dt.replace(second=0, microsecond=0)
        reminder_dt = reminder_dt - timedelta(minutes=5)
        logger.info(f"parse_reminder: '{comment}' → date+time {parsed_date} {parsed_time} → {reminder_dt}")
        return reminder_dt  # НЕ проверяем прошлое!
    
    # Если только дата без времени (12:00 по умолчанию) → не создаём напоминание
    if parsed_date and parsed_time == time(12, 0):
        logger.info(f"parse_reminder: '{comment}' → only date {parsed_date} without time → None")
        return None
    
    # Ничего не найдено
    logger.info(f"parse_reminder: '{comment}' → parsed_date=None, parsed_time={parsed_time} → None")
    return None


async def set_status_reaction(bot, chat_id: int, message_id: int, emoji: str) -> None:
    """
    Устанавливает реакцию на сообщение по chat_id и message_id.
    Ошибки логируются, но не отправляются пользователю.
    """
    try:
        await bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
            is_big=False,
        )
    except Exception as e:
        logger.warning(
            f"Не удалось поставить реакцию '{emoji}' на сообщение {message_id} в чате {chat_id}: {e}",
            exc_info=True,
        )


def get_status_keyboard(order_id: int, current_status: str = "new") -> InlineKeyboardMarkup:
    """Создаёт клавиатуру для смены статуса заказа: все кнопки в один ряд, только иконки."""
    buttons: list[list[InlineKeyboardButton]] = [[]]

    for status_key, icon in STATUS_OPTIONS.items():
        prefix = "✓ " if status_key == current_status else ""
        buttons[0].append(
            InlineKeyboardButton(
                text=f"{prefix}{icon}",
                callback_data=f"status:{order_id}:{status_key}",
            )
        )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _format_order_search_line(order: dict) -> str:
    """Форматирует строку заказа для результатов поиска."""
    order_id = order.get("id", "")
    status = order.get("status", "new")
    status_icon = STATUS_OPTIONS.get(status, "🆕")
    
    model = order.get("model", "") or ""
    price = order.get("price", "") or ""
    address = order.get("address", "") or ""
    phone = order.get("phone", "") or ""
    comment = order.get("comment", "") or ""
    
    # Форматируем дату/время
    created_at = order.get("created_at", "")
    date_str = ""
    if created_at:
        try:
            if "T" in created_at:
                dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            else:
                dt = datetime.fromisoformat(created_at)
            
            now = datetime.now()
            today = now.date()
            order_date = dt.date()
            
            if order_date == today:
                date_str = f"сегодня {dt.strftime('%H:%M')}"
            elif order_date == today - timedelta(days=1):
                date_str = f"вчера {dt.strftime('%H:%M')}"
            else:
                date_str = dt.strftime("%d.%m %H:%M")
        except Exception:
            if "T" in created_at:
                date_str = created_at.split("T")[0]
            else:
                date_str = created_at
    
    manager_name = order.get("manager_name", "") or ""
    
    # Обрезаем длинные поля
    if len(comment) > 20:
        comment = comment[:20] + "..."
    if len(model) > 30:
        model = model[:30] + "..."
    
    parts = [
        f"#{order_id}",
        status_icon,
        model,
        price,
        address,
        phone,
        comment,
        date_str,
        manager_name,
    ]
    
    # Убираем пустые части
    parts = [p for p in parts if p]
    
    return " • ".join(parts)


def _get_order_status_keyboard(order_id: int, current_status: str = "new") -> InlineKeyboardMarkup:
    """Создаёт клавиатуру со статусами + кнопка редактирования для новой карточки."""
    buttons: list[list[InlineKeyboardButton]] = [[]]
    
    # Кнопки статусов
    for status_key, icon in STATUS_OPTIONS.items():
        buttons[0].append(
            InlineKeyboardButton(
                text=icon,
                callback_data=f"status:{order_id}:{status_key}",
            )
        )
    
    # Кнопка редактирования
    buttons[0].append(
        InlineKeyboardButton(
            text="✏️",
            callback_data=f"edit_mode:{order_id}",
        )
    )
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def normalize_phone(contact_str: str) -> tuple[Optional[str], str]:
    """
    Нормализует телефон из строки контакта.

    Возвращает:
        tuple: (нормализованный_телефон, имя_клиента)
        Если телефон не удалось распарсить, возвращает (None, имя_клиента или вся строка)
    """
    contact_str = contact_str.strip()
    if not contact_str:
        return None, ""

    parts = contact_str.split()
    if not parts:
        return None, ""

    phone_str = parts[0]
    customer_name = " ".join(parts[1:]) if len(parts) > 1 else ""

    try:
        digits = "".join(filter(str.isdigit, phone_str))

        if len(digits) >= 10:
            if digits.startswith("8"):
                normalized = "+7" + digits[1:]
            elif digits.startswith("7"):
                normalized = "+7" + digits[1:]
            else:
                normalized = "+7" + digits
            return normalized, customer_name.strip()
        else:
            return None, contact_str

    except Exception:
        return None, contact_str


def parse_order_message(text: str) -> Optional[dict]:
    """
    Парсит сообщение с заказом.

    Формат (5 полей):
    [заказ] / [цена] / [адрес] / [контакт имя] / [комментарий]
    
    Пример: Цветы для Мэри Джейн / 20000 / Нью-Йорк / 89997772233 Питер Паркер / доставить 30.12
    """
    parts = [part.strip() for part in text.split("/")]

    if len(parts) != 5:
        return None

    try:
        return {
            "model": parts[0],
            "price": parts[1],
            "address": parts[2],
            "contact": parts[3],
            "comment": parts[4],
        }
    except Exception as e:
        logger.error(f"Ошибка при парсинге сообщения: {e}")
        return None


def _order_to_edit_string(order: dict) -> str:
    """Возвращает строку для копирования/правки"""
    return f"{order.get('model', '')}/{order.get('price', '')}/{order.get('address', '')}/{order.get('contact_raw', '')}/{order.get('comment', '')}"


def _format_order_card(order: dict) -> str:
    """Форматирует карточку заказа в новом формате."""
    order_id = order.get("id", "")
    model = order.get("model", "") or ""
    price = order.get("price", "") or ""
    address = order.get("address", "") or ""
    customer_name = order.get("customer_name", "") or ""
    phone = order.get("phone", "") or ""
    comment = order.get("comment", "") or ""
    
    # Форматируем дату
    created_at = order.get("created_at", "")
    if created_at and "T" in created_at:
        created_at = created_at.split("T")[0]
    
    manager_name = order.get("manager_name", "") or ""
    status = order.get("status", "new")
    status_display = STATUS_DISPLAY.get(status, status)
    
    card_lines = [f"🔸 #{order_id}. 📦 {model}"]
    
    if price:
        # Добавляем ₽ если нет символа валюты
        price_display = price if any(c in price for c in "₽$€") else f"{price}₽"
        card_lines.append(f"💰 {price_display}")
    
    if address:
        card_lines.append(f"📍 {address}")
    
    if customer_name or phone:
        contact_parts = []
        if customer_name:
            contact_parts.append(customer_name)
        if phone:
            contact_parts.append(f"📞 {phone}")
        card_lines.append("👤 " + " | ".join(contact_parts))
    
    if comment:
        card_lines.append(f"💬 {comment}")
    
    if created_at:
        card_lines.append(f"📅 {created_at}")
    
    if manager_name:
        card_lines.append(f"🤝 {manager_name}")
    
    card_lines.append(f"📊 {status_display}")
    
    return "\n".join(card_lines)


def _get_order_edit_keyboard(order_id: int) -> InlineKeyboardMarkup:
    """Создаёт inline-клавиатуру для редактирования заказа."""
    buttons = [
        [
            InlineKeyboardButton(text="📦", callback_data=f"edit_status:{order_id}"),
            InlineKeyboardButton(text="💰", callback_data=f"edit_field:{order_id}:price"),
            InlineKeyboardButton(text="📍", callback_data=f"edit_field:{order_id}:address"),
        ],
        [
            InlineKeyboardButton(text="👤", callback_data=f"edit_field:{order_id}:customer_name"),
            InlineKeyboardButton(text="📞", callback_data=f"edit_field:{order_id}:phone"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _format_reminder_card(order: dict, index: int = 1) -> str:
    """Форматирует карточку заказа для напоминания."""
    order_id = order.get("id", "")
    status = order.get("status", "new")
    status_display = STATUS_DISPLAY.get(status, status)
    
    card_lines = [
        f"📋 Заказ #{order_id}",
        f"📊 Статус: {status_display}",
        "",
        f"📦 Модель: {order.get('model', '')}",
        f"💰 Цена: {order.get('price', '')}",
        f"📍 Адрес: {order.get('address', '')}",
        f"👤 Клиент: {order.get('customer_name', '')}",
        f"📞 Телефон: {order.get('phone', '')}",
        f"💬 Комментарий: {order.get('comment', '')}",
    ]
    
    created_at = order.get("created_at", "")
    if created_at:
        if "T" in created_at:
            created_at = created_at.split("T")[0]
        card_lines.append(f"📅 Дата: {created_at}")
    
    manager_name = order.get("manager_name", "")
    if manager_name:
        card_lines.append(f"🤝 Менеджер: {manager_name}")
    
    return "\n".join(card_lines)


@router.message(Command("start", "help"))
async def cmd_start(message: Message) -> None:
    """Обработка команд /start и /help."""
    user_id = message.from_user.id

    # Проверяем, авторизован ли пользователь
    if await is_user_authorized(user_id):
        # Уже авторизован — показываем меню
        daily_report_enabled = await get_daily_report_enabled(message.chat.id)
        await message.reply(
            "👋 Добро пожаловать в nano_crm!\n\n"
            "📝 Формат заказа (ровно 5 полей):\n"
            "заказ / цена / адрес / контакт / комментарий\n\n"
            "Для создания записи введите данные как в примере, через \"/\"\n\n"
            "💡 Пример:\n"
            "Цветы / 15000 / Нью-Йорк / 89991234567 Питер Паркер / завтра 15:00\n\n"
            "Чтобы поменять статус — нажмите кнопки под записью:\n"
            "🆕 Новый | 📦 В работе | 🚚 Доставка | ✅ Оплачен | ❌ Отказ\n\n"
            "Если запись отредактировать — она обновится в базе автоматически\n\n"
            "Reply на запись + текст — перепишет комментарий\n"
            "(дата/время в комментарии → напоминание с карточкой)\n\n"
            "🎛️ Для работы с базой используйте кнопки ниже:\n"
            "- 📊 Отчёт (PDF/Excel/CSV)\n"
            "- 🔍 Поиск по базе\n"
            "- 📈 Заказы по статусам (🆕📦🚚✅❌)\n\n"
            "✏️ Для правки заказа используйте /find &lt;id&gt;",
            reply_markup=get_main_keyboard(daily_report_enabled),
        )
        return

    # Не авторизован — просим PIN
    user_states[user_id] = "waiting_pin"
    await message.reply("🔐 Для доступа к боту введите PIN-код:")


@router.message(Command("find"))
async def cmd_find(message: Message) -> None:
    """Поиск заказа по ID для правки."""
    if not await is_user_authorized(message.from_user.id):
        await message.reply("🔐 Доступ запрещён. Введите /start и PIN-код.")
        return
    
    try:
        # Извлекаем ID из команды
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.reply("⚠️ Использование: /find &lt;id заказа&gt;")
            return
        
        order_id = int(parts[1].strip())
        
        # Получаем заказ из БД
        order = await get_order_by_id(order_id)
        if not order:
            await message.reply(f"⚠️ Заказ #{order_id} не найден")
            return
        
        # Форматируем для отображения
        status = order.get("status", "new")
        status_display = STATUS_DISPLAY.get(status, status)
        
        # Форматируем дату (убираем время если есть)
        created_at = order.get("created_at", "")
        if created_at and "T" in created_at:
            created_at = created_at.split("T")[0]
        
        # Создаём карточку
        card_lines = [
            f"🔍 ЗАКАЗ #{order_id}",
            f"📊 Статус: {status_display}",
            "",
            f"📦 Модель: {order.get('model', '')}",
            f"💰 Цена: {order.get('price', '')}",
            f"📍 Адрес: {order.get('address', '')}",
            f"👤 Клиент: {order.get('customer_name', '')}",
            f"📞 Телефон: {order.get('phone', '')}",
            f"💬 Комментарий: {order.get('comment', '')}",
        ]
        
        if created_at:
            card_lines.append(f"📅 Дата: {created_at}")
        
        manager_name = order.get("manager_name", "")
        if manager_name:
            card_lines.append(f"🤝 Менеджер: {manager_name}")
        
        card_lines.append("")
        card_lines.append("━━━━━━━━━━━━━━━━━━")
        card_lines.append("📝 ПРАВКА (скопируйте строку ниже):")
        card_lines.append("")
        
        # Строка для правки
        edit_string = _order_to_edit_string(order)
        card_lines.append(edit_string)
        card_lines.append("")
        card_lines.append("✏️ Отредактируйте нужные поля и отправьте обратно")
        
        text = "\n".join(card_lines)
        
        # Сохраняем состояние редактирования
        user_id = message.from_user.id
        user_edit_states[user_id] = {"editing_id": order_id}
        
        await message.reply(text)
        
    except ValueError:
        await message.reply("⚠️ ID заказа должен быть числом")
    except Exception as e:
        logger.error(f"Ошибка при поиске заказа для правки: {e}", exc_info=True)
        await message.reply("⚠️ Произошла ошибка при поиске заказа")


@router.message(Command("set_status"))
async def cmd_set_status(message: Message) -> None:
    """Команда для изменения статуса заказа."""
    if not await is_user_authorized(message.from_user.id):
        await message.reply("🔐 Доступ запрещён. Введите /start и PIN-код.")
        return
    try:
        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            await message.reply(
                "⚠️ Использование: /set_status &lt;id&gt; &lt;status&gt;\n"
                "Статусы: new, in_progress, delivery, paid, canceled"
            )
            return

        order_id = int(parts[1])
        new_status = parts[2].strip()

        if new_status not in STATUS_OPTIONS:
            await message.reply(
                f"⚠️ Неверный статус. Доступные: {', '.join(STATUS_OPTIONS.keys())}"
            )
            return

        updated = await update_order_status(order_id, new_status)
        if updated:
            status_label = STATUS_DISPLAY.get(new_status, new_status)
            await message.reply(f"✅ Статус заказа #{order_id}: {status_label}")
        else:
            await message.reply(f"⚠️ Заказ #{order_id} не найден")
    except ValueError:
        await message.reply("⚠️ ID заказа должен быть числом")
    except Exception as e:
        logger.error(f"Ошибка при изменении статуса: {e}", exc_info=True)
        await message.reply("⚠️ Произошла ошибка при изменении статуса")


@router.callback_query(F.data.startswith("status:"))
async def handle_status_callback(callback: CallbackQuery) -> None:
    """Обработка нажатия на inline-кнопку статуса."""
    if not await is_user_authorized(callback.from_user.id):
        await callback.answer("🔐 Доступ запрещён. Введите /start и PIN-код.", show_alert=True)
        return
    try:
        parts = callback.data.split(":")
        if len(parts) != 3:
            await callback.answer("⚠️ Ошибка формата", show_alert=True)
            return

        order_id = int(parts[1])
        new_status = parts[2]

        if new_status not in STATUS_OPTIONS:
            await callback.answer("⚠️ Неверный статус", show_alert=True)
            return

        updated = await update_order_status(order_id, new_status)
        if not updated:
            await callback.answer("⚠️ Заказ не найден", show_alert=True)
            return

        order = await get_order_by_id(order_id)
        if order:
            current_status = order.get("status", "new")
            
            # Обновляем карточку
            card_text = _format_order_card(order)
            
            # Определяем, какая клавиатура должна быть показана
            # Если это карточка с кнопкой редактирования - используем статусную клавиатуру
            # Иначе - клавиатуру редактирования
            card_key = (callback.message.chat.id, callback.message.message_id)
            if card_key in card_message_to_order:
                # Это карточка заказа - используем статусную клавиатуру
                new_keyboard = _get_order_status_keyboard(order_id, current_status)
            else:
                # Старая логика - используем клавиатуру редактирования
                new_keyboard = _get_order_edit_keyboard(order_id)

            await callback.message.edit_text(card_text, reply_markup=new_keyboard)

            src_chat_id = order.get("chat_id")
            src_message_id = order.get("message_id")
            emoji = STATUS_TO_EMOJI.get(new_status, "👌")

            if src_chat_id and src_message_id:
                await set_status_reaction(
                    callback.bot, src_chat_id, src_message_id, emoji
                )
            
            # Ставим реакцию на карточку
            try:
                await set_status_reaction(
                    callback.bot, callback.message.chat.id, callback.message.message_id, "✅"
                )
            except Exception:
                pass

            await callback.answer("Статус обновлён")
        else:
            await callback.answer("⚠️ Заказ не найден", show_alert=True)

    except ValueError:
        await callback.answer("⚠️ Ошибка формата", show_alert=True)
    except Exception as e:
        logger.error(f"Ошибка при обработке callback: {e}", exc_info=True)
        await callback.answer("⚠️ Произошла ошибка", show_alert=True)


@router.callback_query(F.data.startswith("edit_status:"))
async def handle_edit_status_callback(callback: CallbackQuery) -> None:
    """Обработка нажатия на кнопку 📦 (смена статуса)."""
    if not await is_user_authorized(callback.from_user.id):
        await callback.answer("🔐 Доступ запрещён.", show_alert=True)
        return
    
    try:
        order_id = int(callback.data.split(":")[1])
        
        # Создаём компактную клавиатуру со статусами (только emoji)
        status_buttons = [[]]
        for status_key, icon in STATUS_OPTIONS.items():
            status_buttons[0].append(
                InlineKeyboardButton(
                    text=icon,
                    callback_data=f"status_select:{order_id}:{status_key}"
                )
            )
        
        # Кнопка "Назад" (только emoji)
        status_buttons.append([
            InlineKeyboardButton(
                text="🔙",
                callback_data=f"edit_back:{order_id}"
            )
        ])
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=status_buttons)
        
        await callback.message.edit_reply_markup(reply_markup=keyboard)
        await callback.answer()
        
    except (ValueError, IndexError) as e:
        await callback.answer("⚠️ Ошибка формата", show_alert=True)
    except Exception as e:
        logger.error(f"Ошибка при обработке edit_status: {e}", exc_info=True)
        await callback.answer("⚠️ Произошла ошибка", show_alert=True)


@router.callback_query(F.data.startswith("status_select:"))
async def handle_status_select_callback(callback: CallbackQuery) -> None:
    """Обработка выбора статуса из списка."""
    if not await is_user_authorized(callback.from_user.id):
        await callback.answer("🔐 Доступ запрещён.", show_alert=True)
        return
    
    try:
        parts = callback.data.split(":")
        order_id = int(parts[1])
        new_status = parts[2]
        
        if new_status not in STATUS_OPTIONS:
            await callback.answer("⚠️ Неверный статус", show_alert=True)
            return
        
        updated = await update_order_status(order_id, new_status)
        if not updated:
            await callback.answer("⚠️ Заказ не найден", show_alert=True)
            return
        
        # Получаем обновлённый заказ
        order = await get_order_by_id(order_id)
        if not order:
            await callback.answer("⚠️ Заказ не найден", show_alert=True)
            return
        
        # Обновляем карточку
        card_text = _format_order_card(order)
        
        # Определяем, какая клавиатура должна быть показана
        card_key = (callback.message.chat.id, callback.message.message_id)
        if card_key in card_message_to_order:
            # Это карточка заказа - используем статусную клавиатуру
            edit_keyboard = _get_order_status_keyboard(order_id, new_status)
        else:
            # Старая логика - используем клавиатуру редактирования
            edit_keyboard = _get_order_edit_keyboard(order_id)
        
        await callback.message.edit_text(card_text, reply_markup=edit_keyboard)
        
        src_chat_id = order.get("chat_id")
        src_message_id = order.get("message_id")
        emoji = STATUS_TO_EMOJI.get(new_status, "👌")
        
        if src_chat_id and src_message_id:
            await set_status_reaction(
                callback.bot, src_chat_id, src_message_id, emoji
            )
        
        # Ставим реакцию на карточку
        try:
            await set_status_reaction(
                callback.bot, callback.message.chat.id, callback.message.message_id, "✅"
            )
        except Exception:
            pass
        
        await callback.answer("Статус обновлён")
        
    except (ValueError, IndexError) as e:
        await callback.answer("⚠️ Ошибка формата", show_alert=True)
    except Exception as e:
        logger.error(f"Ошибка при обработке status_select: {e}", exc_info=True)
        await callback.answer("⚠️ Произошла ошибка", show_alert=True)


@router.callback_query(F.data.startswith("edit_back:"))
async def handle_edit_back_callback(callback: CallbackQuery) -> None:
    """Обработка кнопки 'Назад' - возврат к основной клавиатуре редактирования."""
    if not await is_user_authorized(callback.from_user.id):
        await callback.answer("🔐 Доступ запрещён.", show_alert=True)
        return
    
    try:
        order_id = int(callback.data.split(":")[1])
        
        order = await get_order_by_id(order_id)
        if not order:
            await callback.answer("⚠️ Заказ не найден", show_alert=True)
            return
        
        card_text = _format_order_card(order)
        
        # Определяем, какая клавиатура должна быть показана
        card_key = (callback.message.chat.id, callback.message.message_id)
        if card_key in card_message_to_order:
            # Это карточка заказа - используем статусную клавиатуру
            edit_keyboard = _get_order_status_keyboard(order_id, order.get("status", "new"))
        else:
            # Старая логика - используем клавиатуру редактирования
            edit_keyboard = _get_order_edit_keyboard(order_id)
        
        await callback.message.edit_text(card_text, reply_markup=edit_keyboard)
        await callback.answer()
        
    except (ValueError, IndexError) as e:
        await callback.answer("⚠️ Ошибка формата", show_alert=True)
    except Exception as e:
        logger.error(f"Ошибка при обработке edit_back: {e}", exc_info=True)
        await callback.answer("⚠️ Произошла ошибка", show_alert=True)


@router.callback_query(F.data.startswith("edit_mode:"))
async def handle_edit_mode_callback(callback: CallbackQuery) -> None:
    """Обработка нажатия на кнопку ✏️ (редактировать) - показывает меню редактирования."""
    if not await is_user_authorized(callback.from_user.id):
        await callback.answer("🔐 Доступ запрещён.", show_alert=True)
        return
    
    try:
        order_id = int(callback.data.split(":")[1])
        
        order = await get_order_by_id(order_id)
        if not order:
            await callback.answer("⚠️ Заказ не найден", show_alert=True)
            return
        
        # Показываем меню редактирования
        card_text = _format_order_card(order)
        edit_keyboard = _get_order_edit_keyboard(order_id)
        
        await callback.message.edit_text(card_text, reply_markup=edit_keyboard)
        await callback.answer()
        
    except (ValueError, IndexError) as e:
        await callback.answer("⚠️ Ошибка формата", show_alert=True)
    except Exception as e:
        logger.error(f"Ошибка при обработке edit_mode: {e}", exc_info=True)
        await callback.answer("⚠️ Произошла ошибка", show_alert=True)


@router.callback_query(F.data.startswith("edit_field:"))
async def handle_edit_field_callback(callback: CallbackQuery) -> None:
    """Обработка нажатия на кнопки редактирования полей (💰📍👤📞)."""
    if not await is_user_authorized(callback.from_user.id):
        await callback.answer("🔐 Доступ запрещён.", show_alert=True)
        return
    
    try:
        parts = callback.data.split(":")
        order_id = int(parts[1])
        field = parts[2]
        
        if field not in ["price", "address", "customer_name", "phone"]:
            await callback.answer("⚠️ Неверное поле", show_alert=True)
            return
        
        # Сохраняем состояние редактирования
        user_id = callback.from_user.id
        edit_field_states[user_id] = {"order_id": order_id, "field": field}
        
        field_names = {
            "price": "цену",
            "address": "город/адрес",
            "customer_name": "имя клиента",
            "phone": "телефон"
        }
        
        await callback.answer(f"Введите новую {field_names.get(field, field)}")
        
    except (ValueError, IndexError) as e:
        await callback.answer("⚠️ Ошибка формата", show_alert=True)
    except Exception as e:
        logger.error(f"Ошибка при обработке edit_field: {e}", exc_info=True)
        await callback.answer("⚠️ Произошла ошибка", show_alert=True)


@router.edited_message(F.text)
async def handle_edited_message(message: Message) -> None:
    """Обработка редактирования сообщения с заказом."""
    if not message.text:
        return
    
    parsed = parse_order_message(message.text)
    if not parsed:
        return
    
    chat_id = message.chat.id
    message_id = message.message_id
    
    phone, customer_name = normalize_phone(parsed["contact"])
    
    logger.info(f"✏️ Редактирование заказа (chat_id={chat_id}, message_id={message_id})")
    
    reminder_dt = parse_reminder_datetime(parsed.get("comment", "")) if parsed.get("comment") else None
    reminder_at = reminder_dt.isoformat() if reminder_dt else None
    if reminder_at:
        logger.info(f"✏️ Edit: reminder recalculated → {reminder_at}")
    
    try:
        updated = await update_order_after_edit(
            chat_id=chat_id,
            message_id=message_id,
            parsed=parsed,
            phone=phone,
            customer_name=customer_name,
            reminder_at=reminder_at,
        )
        
        if updated:
            order = await get_order_by_chat_and_message(chat_id, message_id)
            if order:
                order_id = order.get("id")
                # Ставим реакцию вместо текстового сообщения
                try:
                    await set_status_reaction(message.bot, message.chat.id, message.message_id, "✅")
                except Exception:
                    pass
            else:
                logger.warning(f"Заказ не найден после обновления (chat_id={chat_id}, message_id={message_id})")
        else:
            logger.warning(f"Заказ не найден для обновления (chat_id={chat_id}, message_id={message_id})")
    
    except Exception as e:
        logger.error(f"Ошибка при обновлении заказа: {e}", exc_info=True)
        await message.reply("⚠️ Произошла ошибка при обновлении заказа")


@router.message(F.text & F.reply_to_message)
async def handle_reply(message: Message) -> None:
    """Обработка reply-сообщений для обновления комментариев или полей заказа."""
    if not message.reply_to_message or not message.text:
        return

    if not await is_user_authorized(message.from_user.id):
        return

    replied_msg = message.reply_to_message
    chat_id = replied_msg.chat.id
    message_id = replied_msg.message_id

    new_part = message.text.strip()
    logger.info(f"Reply to message (chat_id={chat_id}, message_id={message_id}) → '{new_part}'")

    try:
        # Проверяем, является ли это reply на карточку заказа
        card_key = (chat_id, message_id)
        order_id = card_message_to_order.get(card_key)
        
        if order_id:
            # Это reply на карточку заказа
            order = await get_order_by_id(order_id)
            if not order:
                await message.reply("⚠️ Заказ не найден")
                return
            
            # Проверяем формат /65000//// (массовая правка цены)
            if new_part.startswith("/") and new_part.count("/") >= 4:
                parts = new_part.split("/")
                if len(parts) >= 2 and parts[1].strip():
                    new_price = parts[1].strip()
                    # Обновляем только цену
                    import aiosqlite
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute(
                            "UPDATE orders SET price = ?, updated_at = ? WHERE id = ?",
                            (new_price, datetime.now().isoformat(), order_id)
                        )
                        await db.commit()
                    
                    # Обновляем карточку
                    updated_order = await get_order_by_id(order_id)
                    if updated_order:
                        card_text = _format_order_card(updated_order)
                        
                        # Определяем, какая клавиатура должна быть показана
                        if card_key in card_message_to_order:
                            # Это карточка заказа - используем статусную клавиатуру
                            edit_keyboard = _get_order_status_keyboard(order_id, updated_order.get("status", "new"))
                        else:
                            # Старая логика - используем клавиатуру редактирования
                            edit_keyboard = _get_order_edit_keyboard(order_id)
                        
                        try:
                            await message.bot.edit_message_text(
                                chat_id=chat_id,
                                message_id=message_id,
                                text=card_text,
                                reply_markup=edit_keyboard
                            )
                        except Exception as e:
                            logger.warning(f"Не удалось обновить карточку: {e}")
                    
                    # Ставим реакцию на карточку
                    try:
                        await set_status_reaction(message.bot, chat_id, message_id, "✅")
                    except Exception:
                        pass
                    
                    return
            
            # Обычный reply - обновляем комментарий
            manager_name = (
                message.from_user.full_name
                or message.from_user.username
                or "Unknown"
            )
            
            current_comment = order.get("comment", "") or ""
            
            # Формируем новый комментарий
            if current_comment:
                new_comment = f"{current_comment}; {new_part}"
            else:
                new_comment = new_part
            
            # Парсим напоминание
            reminder_dt = parse_reminder_datetime(new_part)
            reminder_at = reminder_dt.isoformat() if reminder_dt else None
            
            # Формируем запись истории
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            history_entry = f"{timestamp} {manager_name}: comment → '{new_part}'"
            
            # Сохраняем в БД
            import aiosqlite
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    """
                    UPDATE orders SET 
                        comment = ?,
                        reminder_at = ?,
                        reminder_sent = 0,
                        comment_history = COALESCE(comment_history, '') || ? || '\n'
                    WHERE id = ?
                    """,
                    (new_comment, reminder_at, history_entry, order_id),
                )
                await db.commit()
            
            # Обновляем карточку
            updated_order = await get_order_by_id(order_id)
            if updated_order:
                card_text = _format_order_card(updated_order)
                
                # Определяем, какая клавиатура должна быть показана
                if card_key in card_message_to_order:
                    # Это карточка заказа - используем статусную клавиатуру
                    edit_keyboard = _get_order_status_keyboard(order_id, updated_order.get("status", "new"))
                else:
                    # Старая логика - используем клавиатуру редактирования
                    edit_keyboard = _get_order_edit_keyboard(order_id)
                
                try:
                    await message.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=card_text,
                        reply_markup=edit_keyboard
                    )
                except Exception as e:
                    logger.warning(f"Не удалось обновить карточку: {e}")
            
            # Ставим реакцию на карточку вместо текстового ответа
            try:
                await set_status_reaction(message.bot, chat_id, message_id, "✅")
            except Exception:
                pass
            
            return
        
        # Старая логика для reply на исходные сообщения заказов
        order = await get_order_by_chat_and_message(chat_id, message_id)
        if not order:
            return

        current_comment = order.get("comment", "") or ""
        current_history = order.get("comment_history", "") or ""

        # Формируем новый комментарий (с разделителем ";")
        if current_comment:
            new_comment = f"{current_comment}; {new_part}"
        else:
            new_comment = new_part

        # ПАРСИМ ТОЛЬКО ПОСЛЕДНЮЮ ЧАСТЬ (после последнего ";")
        last_part = new_part
        reminder_dt = parse_reminder_datetime(last_part)
        reminder_at = reminder_dt.isoformat() if reminder_dt else None

        # Формируем запись истории
        manager_name = (
            message.from_user.full_name
            or message.from_user.username
            or "Unknown"
        )
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        history_entry = f"{timestamp} {manager_name}: comment → '{new_part}'"

        # Сохраняем в БД
        import aiosqlite
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                UPDATE orders SET 
                    comment = ?,
                    reminder_at = ?,
                    reminder_sent = 0,
                    comment_history = COALESCE(comment_history, '') || ? || '\n'
                WHERE chat_id = ? AND message_id = ?
                """,
                (new_comment, reminder_at, history_entry, chat_id, message_id),
            )
            await db.commit()

        # Ставим реакцию вместо текстового ответа
        try:
            await set_status_reaction(message.bot, message.chat.id, message.message_id, "✅")
        except Exception:
            pass

    except Exception as e:
        logger.error(
            f"Ошибка при обновлении комментария: {e}",
            exc_info=True,
        )
        await message.reply(
            "⚠️ Произошла ошибка при обновлении комментария"
        )


@router.message(
    F.text 
    & ~F.reply_to_message 
    & F.text.startswith("/")
)
async def handle_text_message(message: Message) -> None:
    """Обработка команд (начинающихся с /)."""
    if not message.text:
        return

    user_id = message.from_user.id
    text = message.text.strip()
    
    # Убираем / из начала, если есть (для обработки /1234)
    pin_text = text.lstrip("/") if text.startswith("/") else text

    # Если ждём PIN
    if user_id in user_states and user_states[user_id] == "waiting_pin":
        if pin_text == BOT_PIN:
            await authorize_user(user_id)
            del user_states[user_id]
            daily_report_enabled = await get_daily_report_enabled(message.chat.id)
            await message.reply(
                "👋 Добро пожаловать в nano_crm!\n\n"
                "📝 Формат заказа (ровно 5 полей):\n"
                "заказ / цена / адрес / контакт / комментарий\n\n"
                "Для создания записи введите данные как в примере, через \"/\"\n\n"
                "💡 Пример:\n"
                "Цветы / 15000 / Нью-Йорк / 89991234567 Питер Паркер / завтра 15:00\n\n"
                "Чтобы поменять статус — нажмите кнопки под записью:\n"
                "🆕 Новый | 📦 В работе | 🚚 Доставка | ✅ Оплачен | ❌ Отказ\n\n"
                "Если запись отредактировать — она обновится в базе автоматически\n\n"
                "Reply на запись + текст — перепишет комментарий\n"
                "(дата/время в комментарии → напоминание с карточкой)\n\n"
                "🎛️ Для работы с базой используйте кнопки ниже:\n"
                "- 📊 Отчёт (PDF/Excel/CSV)\n"
                "- 🔍 Поиск по базе\n"
                "- 📈 Заказы по статусам (🆕📦🚚✅❌)\n\n"
                "✏️ Для правки заказа используйте /find &lt;id&gt;",
                reply_markup=get_main_keyboard(daily_report_enabled)
            )
        else:
            await message.reply("❌ Неверный PIN. Попробуйте ещё раз: /start")
        return

    # ПРОВЕРКА АВТОРИЗАЦИИ ДЛЯ ВСЕХ ОСТАЛЬНЫХ команд
    if not await is_user_authorized(user_id):
        await message.reply("🔐 Доступ запрещён. Введите /start и PIN-код.")
        return

    # Если это не команда из известных, покажем справку
    await message.reply(
        "Доступные команды:\n"
        "/start - Начало работы\n"
        "/help - Помощь\n"
        "/find &lt;id&gt; - Найти заказ для правки\n"
        "/set_status &lt;id&gt; &lt;status&gt; - Изменить статус\n"
        "/test_search &lt;запрос&gt; - Тест поиска\n"
        "\nИли отправьте сообщение в формате 5 полей для создания заказа."
    )


@router.message(
    F.text 
    & ~F.reply_to_message 
    & ~F.text.startswith("/")
    & ~F.text.in_(KEYBOARD_BUTTONS)
)
async def handle_edit_or_search(message: Message) -> None:
    """Обработка сообщений: либо правка заказа, либо поиск/новый заказ."""
    user_id = message.from_user.id
    text = message.text.strip() if message.text else ""
    
    # ПРОВЕРКА PIN (если пользователь вводит PIN без /)
    if user_id in user_states and user_states[user_id] == "waiting_pin":
        if text == BOT_PIN:
            await authorize_user(user_id)
            del user_states[user_id]
            daily_report_enabled = await get_daily_report_enabled(message.chat.id)
            await message.reply(
                "👋 Добро пожаловать в nano_crm!\n\n"
                "📝 Формат заказа (ровно 5 полей):\n"
                "заказ / цена / адрес / контакт / комментарий\n\n"
                "Для создания записи введите данные как в примере, через \"/\"\n\n"
                "💡 Пример:\n"
                "Цветы / 15000 / Нью-Йорк / 89991234567 Питер Паркер / завтра 15:00\n\n"
                "Чтобы поменять статус — нажмите кнопки под записью:\n"
                "🆕 Новый | 📦 В работе | 🚚 Доставка | ✅ Оплачен | ❌ Отказ\n\n"
                "Если запись отредактировать — она обновится в базе автоматически\n\n"
                "Reply на запись + текст — перепишет комментарий\n"
                "(дата/время в комментарии → напоминание с карточкой)\n\n"
                "🎛️ Для работы с базой используйте кнопки ниже:\n"
                "- 📊 Отчёт (PDF/Excel/CSV)\n"
                "- 🔍 Поиск по базе\n"
                "- 📈 Заказы по статусам (🆕📦🚚✅❌)\n\n"
                "✏️ Для правки заказа используйте /find &lt;id&gt;",
                reply_markup=get_main_keyboard(daily_report_enabled)
            )
        else:
            await message.reply("❌ Неверный PIN. Попробуйте ещё раз: /start")
        return
    
    # ПРОВЕРКА АВТОРИЗАЦИИ
    if not await is_user_authorized(user_id):
        await message.reply("🔐 Доступ запрещён. Введите /start и PIN-код.")
        return
    
    # Проверяем, находится ли пользователь в режиме редактирования поля
    if user_id in edit_field_states:
        edit_state = edit_field_states[user_id]
        order_id = edit_state["order_id"]
        field = edit_state["field"]
        
        # Получаем заказ
        order = await get_order_by_id(order_id)
        if not order:
            del edit_field_states[user_id]
            await message.reply("⚠️ Заказ не найден")
            return
        
        new_value = text.strip()
        
        # Обновляем поле в БД
        try:
            import aiosqlite
            async with aiosqlite.connect(DB_PATH) as db:
                if field == "price":
                    await db.execute(
                        "UPDATE orders SET price = ?, updated_at = ? WHERE id = ?",
                        (new_value, datetime.now().isoformat(), order_id)
                    )
                elif field == "address":
                    await db.execute(
                        "UPDATE orders SET address = ?, updated_at = ? WHERE id = ?",
                        (new_value, datetime.now().isoformat(), order_id)
                    )
                elif field == "customer_name":
                    await db.execute(
                        "UPDATE orders SET customer_name = ?, updated_at = ? WHERE id = ?",
                        (new_value, datetime.now().isoformat(), order_id)
                    )
                elif field == "phone":
                    # Нормализуем телефон
                    phone, customer_name = normalize_phone(new_value)
                    await db.execute(
                        "UPDATE orders SET phone = ?, updated_at = ? WHERE id = ?",
                        (phone, datetime.now().isoformat(), order_id)
                    )
                    if customer_name and not order.get("customer_name"):
                        await db.execute(
                            "UPDATE orders SET customer_name = ? WHERE id = ?",
                            (customer_name, order_id)
                        )
                
                await db.commit()
            
            # Получаем обновлённый заказ
            updated_order = await get_order_by_id(order_id)
            if updated_order:
                # Находим карточку для обновления
                card_key = None
                for key, oid in card_message_to_order.items():
                    if oid == order_id:
                        card_key = key
                        break
                
                if card_key:
                    chat_id, card_message_id = card_key
                    card_text = _format_order_card(updated_order)
                    
                    # Определяем, какая клавиатура должна быть показана
                    if card_key in card_message_to_order:
                        # Это карточка заказа - используем статусную клавиатуру
                        edit_keyboard = _get_order_status_keyboard(order_id, updated_order.get("status", "new"))
                    else:
                        # Старая логика - используем клавиатуру редактирования
                        edit_keyboard = _get_order_edit_keyboard(order_id)
                    
                    try:
                        await message.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=card_message_id,
                            text=card_text,
                            reply_markup=edit_keyboard
                        )
                    except Exception as e:
                        logger.warning(f"Не удалось обновить карточку: {e}")
            
            # Очищаем состояние
            del edit_field_states[user_id]
            
            # Ставим реакцию на карточку (не на сообщение пользователя)
            if card_key:
                chat_id, card_message_id = card_key
                try:
                    await set_status_reaction(message.bot, chat_id, card_message_id, "✅")
                except Exception:
                    pass
            
            field_names = {
                "price": "Цена обновлена",
                "address": "Город обновлён",
                "customer_name": "Имя клиента обновлено",
                "phone": "Телефон обновлён"
            }
            # Не отправляем текстовое сообщение - только реакция на карточку
            
        except Exception as e:
            logger.error(f"Ошибка при обновлении поля {field}: {e}", exc_info=True)
            del edit_field_states[user_id]
            await message.reply("⚠️ Произошла ошибка при обновлении")
        
        return
    
    # Проверяем, находится ли пользователь в режиме редактирования
    if user_id in user_edit_states:
        editing = user_edit_states[user_id]
        order_id = editing['editing_id']
        
        # Парсим новую строку
        parsed = parse_order_message(message.text)
        if not parsed:
            # Очищаем состояние редактирования при ошибке парсинга
            del user_edit_states[user_id]
            await message.reply("⚠️ Ошибка парсинга. Формат: модель/цена/адрес/контакт/комментарий")
            return
        
        # Обрабатываем контакт и обновляем заказ
        phone, customer_name = normalize_phone(parsed["contact"])
        updated = await update_order_by_id(order_id, parsed, phone, customer_name)
        
        # Очищаем состояние редактирования (в любом случае)
        del user_edit_states[user_id]
        
        if updated:
            await message.reply(f"✅ Заказ #{order_id} изменён!")
        else:
            await message.reply(f"⚠️ Заказ #{order_id} не найден")
        
        return
    
    # Если не в режиме редактирования - обрабатываем как обычно
    await handle_order_message(message)


async def handle_order_message(message: Message) -> None:
    """Обработка сообщений с заказами (не reply и не команды) или поиск."""
    if not message.text:
        return

    user_id = message.from_user.id

    # ЗАЩИТА ОТ ЗЛОУПОТРЕБЛЕНИЙ: Задержка 3 секунды между сообщениями
    now = datetime.now()
    if now - last_message_time[user_id] < timedelta(seconds=3):
        logger.warning(f"⚠️ Rate limit: user {user_id} sent message too quickly")
        await message.reply("⏳ Пожалуйста, подождите несколько секунд перед отправкой следующего сообщения.")
        return
    last_message_time[user_id] = now

    # СНАЧАЛА ПЫТАЕМСЯ ПАРСИТЬ ЗАКАЗ
    parsed = parse_order_message(message.text)

    if parsed:
        # ЗАЩИТА ОТ ЗЛОУПОТРЕБЛЕНИЙ: Лимит комментария 500 символов
        if len(parsed.get("comment", "")) > 500:
            logger.warning(f"⚠️ Comment too long: user {user_id}, length={len(parsed.get('comment', ''))}")
            await message.reply("⚠️ Комментарий слишком длинный (максимум 500 символов). Заказ отклонён.")
            return
        
        # ЗАЩИТА ОТ ЗЛОУПОТРЕБЛЕНИЙ: Лимит 50 заказов в день
        from db import get_user_orders_today, check_duplicate_order
        orders_today = await get_user_orders_today(user_id)
        if orders_today >= 50:
            logger.warning(f"⚠️ Daily limit exceeded: user {user_id}, orders_today={orders_today}")
            await message.reply("⚠️ Превышен лимит заказов на сегодня (50 заказов). Попробуйте завтра.")
            return
        
        # Нормализуем телефон
        phone, customer_name = normalize_phone(parsed["contact"])
        
        # Проверка на дубликаты
        if await check_duplicate_order(parsed["model"], parsed["contact"]):
            await message.reply("❌ Уже существует заказ с такой моделью и контактом!")
            return
        
        # Парсим напоминание из комментария
        reminder_dt = parse_reminder_datetime(parsed.get("comment", "")) if parsed.get("comment") else None
        reminder_at = reminder_dt.isoformat() if reminder_dt else None
        
        # Создаём объект заказа
        manager_name = (
            message.from_user.full_name
            or message.from_user.username
            or "Unknown"
        )
        
        order = Order(
            model=parsed["model"],
            price=parsed["price"],
            address=parsed["address"],
            contact_raw=parsed["contact"],
            phone=phone,
            customer_name=customer_name,
            comment=parsed.get("comment", ""),
            manager_id=user_id,
            manager_name=manager_name,
            chat_id=message.chat.id,
            message_id=message.message_id,
            status="new",
        )
        
        try:
            # Вставляем заказ в БД
            order_id = await insert_order(order, reminder_at=reminder_at)
            
            # Получаем заказ из БД для форматирования карточки
            order_data = await get_order_by_id(order_id)
            if not order_data:
                await message.reply("⚠️ Заказ создан, но не найден в БД")
                return
            
            # Форматируем карточку
            card_text = _format_order_card(order_data)
            
            # Создаём клавиатуру со статусами + редактирование для новой карточки
            status_keyboard = _get_order_status_keyboard(order_id, "new")
            
            # Визуальное замедление: небольшая задержка перед удалением
            await asyncio.sleep(0.4)
            
            # Удаляем исходное сообщение пользователя
            try:
                await message.delete()
            except Exception as e:
                logger.warning(f"Не удалось удалить сообщение пользователя: {e}")
            
            # Отправляем карточку
            card_message = await message.answer(card_text, reply_markup=status_keyboard)
            
            # Сохраняем связь message_id карточки с order_id
            card_message_to_order[(message.chat.id, card_message.message_id)] = order_id
            
            logger.info(f"Создан заказ #{order_id} от пользователя {user_id}")
            
        except ValueError as e:
            # Ошибка дубликата (если проверка не сработала)
            await message.reply(str(e))
        except Exception as e:
            logger.error(f"Ошибка при создании заказа: {e}", exc_info=True)
            await message.reply("⚠️ Произошла ошибка при создании заказа")
    
    else:
        # Проверяем формат #66 для редактирования заказа
        text_stripped = message.text.strip()
        if text_stripped.startswith("#") and len(text_stripped) > 1:
            try:
                order_id = int(text_stripped[1:])
                order = await get_order_by_id(order_id)
                if order:
                    # Форматируем карточку
                    card_text = _format_order_card(order)
                    status_keyboard = _get_order_status_keyboard(order_id, order.get("status", "new"))
                    
                    card_message = await message.answer(card_text, reply_markup=status_keyboard)
                    
                    # Сохраняем связь message_id карточки с order_id
                    card_message_to_order[(message.chat.id, card_message.message_id)] = order_id
                    
                    # Удаляем исходное сообщение пользователя
                    try:
                        await message.delete()
                    except Exception as e:
                        logger.warning(f"Не удалось удалить сообщение пользователя: {e}")
                    
                    return
                else:
                    await message.reply(f"⚠️ Заказ #{order_id} не найден")
                    return
            except ValueError:
                # Не число после #, продолжаем поиск
                pass
        
        # Если не удалось распарсить как заказ - пробуем поиск
        query = _normalize_search_query(message.text)
        if query:
            try:
                results = await search_orders(query, limit=10)
                if results:
                    lines = [f"🔍 Найдено заказов: {len(results)}"]
                    for order in results:
                        lines.append(_format_order_search_line(order))
                    text = "\n".join(lines)
                    await message.reply(text)
                else:
                    await message.reply("🔍 Ничего не найдено")
            except Exception as e:
                logger.error(f"Ошибка при поиске: {e}", exc_info=True)
                await message.reply("⚠️ Произошла ошибка при поиске")
        else:
            await message.reply(
                "⚠️ Неверный формат. Используйте:\n"
                "модель / цена / адрес / контакт / комментарий\n\n"
                "Или введите поисковый запрос."
            )
