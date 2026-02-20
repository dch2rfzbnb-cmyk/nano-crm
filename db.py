"""Работа с базой данных SQLite."""
import logging
from datetime import datetime, date
from typing import Optional, List, Dict

import aiosqlite

from config import DB_PATH
from models import Order

logger = logging.getLogger(__name__)

# Таблицы для нормализации русского алфавита к lowercase
RUS_UPPER = "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
RUS_LOWER = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
# Создаём таблицу переводов: верхний регистр -> нижний для русского алфавита
RUS_TRANS_MAP = str.maketrans(RUS_UPPER + RUS_UPPER.lower(), RUS_LOWER + RUS_LOWER)


def normalize_for_search(text: str) -> str:
    """
    Упрощённая нормализация строки для регистронезависимого поиска по кириллице.
    Приводит русские буквы через явную карту, латиницу через .lower().
    Используется только для поиска, не меняет данные в БД.
    """
    if not text:
        return ""
    return text.translate(RUS_TRANS_MAP).lower()


async def create_table() -> None:
    """Создаёт таблицы orders и settings."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model TEXT,
                price TEXT,
                address TEXT,
                contact_raw TEXT,
                phone TEXT,
                customer_name TEXT,
                comment TEXT,
                manager_id INTEGER,
                manager_name TEXT,
                chat_id INTEGER,
                message_id INTEGER,
                created_at TEXT,
                status TEXT DEFAULT 'new',
                updated_at TEXT,
                reminder_at TEXT,
                reminder_sent INTEGER DEFAULT 0,
                comment_history TEXT
            )
            """
        )
        await db.commit()

        migrations = [
            ("price", "ALTER TABLE orders ADD COLUMN price TEXT"),
            ("status", "ALTER TABLE orders ADD COLUMN status TEXT DEFAULT 'new'"),
            ("updated_at", "ALTER TABLE orders ADD COLUMN updated_at TEXT"),
            ("reminder_at", "ALTER TABLE orders ADD COLUMN reminder_at TEXT"),
            ("reminder_sent", "ALTER TABLE orders ADD COLUMN reminder_sent INTEGER DEFAULT 0"),
            ("comment_history", "ALTER TABLE orders ADD COLUMN comment_history TEXT"),
        ]

        for field_name, sql in migrations:
            try:
                await db.execute(sql)
                await db.commit()
                logger.info(f"Миграция: добавлено поле {field_name} в таблицу orders")
            except aiosqlite.OperationalError:
                # Поле уже существует
                pass

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                chat_id INTEGER PRIMARY KEY,
                daily_report_enabled INTEGER DEFAULT 0,
                report_chat_id INTEGER,
                last_report_date TEXT
            )
            """
        )
        await db.commit()

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS authorized_users (
                user_id INTEGER PRIMARY KEY,
                authorized INTEGER DEFAULT 1
            )
            """
        )
        await db.commit()

        # Создаём UNIQUE INDEX для предотвращения дублей по model и contact_raw
        try:
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_order_contact ON orders(model, contact_raw)"
            )
            await db.commit()
            logger.info("Миграция: создан UNIQUE INDEX idx_order_contact")
        except aiosqlite.OperationalError as e:
            # Индекс уже существует или ошибка
            logger.debug(f"UNIQUE INDEX idx_order_contact: {e}")


async def get_user_orders_today(user_id: int) -> int:
    """Возвращает количество заказов пользователя за сегодня."""
    async with aiosqlite.connect(DB_PATH) as db:
        today_str = datetime.now().date().isoformat()
        cursor = await db.execute(
            """
            SELECT COUNT(*) FROM orders
            WHERE manager_id = ? AND DATE(created_at) = DATE(?)
            """,
            (user_id, today_str),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


async def check_duplicate_order(model: str, contact_raw: str) -> bool:
    """Проверяет, есть ли заказ с таким же model и contact_raw."""
    if not model or not contact_raw:
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT id FROM orders
            WHERE model = ? AND contact_raw = ?
            LIMIT 1
            """,
            (model, contact_raw),
        )
        row = await cursor.fetchone()
        return row is not None


async def insert_order(order: Order, reminder_at: Optional[str] = None) -> int:
    """Вставляет заказ в БД и возвращает его id."""
    now_str = datetime.now().isoformat()
    if reminder_at:
        logger.info(f"insert_order: reminder_at set to '{reminder_at}'")
    async with aiosqlite.connect(DB_PATH) as db:
        # Проверка дублей перед вставкой (на случай если индекс не сработал)
        existing = await check_duplicate_order(order.model, order.contact_raw)
        if existing:
            raise ValueError("❌ Уже существует!")
        
        cursor = await db.execute(
            """
            INSERT INTO orders (
                model,
                price,
                address,
                contact_raw,
                phone,
                customer_name,
                comment,
                manager_id,
                manager_name,
                chat_id,
                message_id,
                created_at,
                status,
                updated_at,
                reminder_at,
                reminder_sent
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order.model,
                order.price,
                order.address,
                order.contact_raw,
                order.phone,
                order.customer_name,
                order.comment,
                order.manager_id,
                order.manager_name,
                order.chat_id,
                order.message_id,
                # created_at – всегда текущее время
                now_str,
                order.status,
                # updated_at – тоже текущее время
                now_str,
                reminder_at,
                0,
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def update_order_comment(
    chat_id: int,
    message_id: int,
    additional_comment: str,
    manager_name: str,
) -> bool:
    """Обновляет комментарий заказа, добавляя новый текст, и пересчитывает напоминание."""
    from handlers.orders import parse_reminder_datetime
    
    async with aiosqlite.connect(DB_PATH) as db:
        # 1. Берём текущий комментарий
        cursor = await db.execute(
            "SELECT comment FROM orders WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        )
        row = await cursor.fetchone()

        if not row:
            return False

        current_comment = row[0] or ""

        # 2. ПАРСИМ ТОЛЬКО НОВЫЙ КОММЕНТАРИЙ (НЕ ВЕСЬ С TIMESTAMP!)
        # Иначе parse_reminder_datetime может поймать время из timestamp вместо комментария
        reminder_dt = parse_reminder_datetime(additional_comment)
        reminder_at = reminder_dt.isoformat() if reminder_dt else None

        # 3. Формируем новый комментарий (с timestamp)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        new_comment = (
            f"{current_comment}\n[{timestamp} {manager_name}]: {additional_comment}"
        ).strip()

        # 4. Обновляем БД
        await db.execute(
            """
            UPDATE orders 
            SET comment = ?, reminder_at = ?, reminder_sent = 0
            WHERE chat_id = ? AND message_id = ?
            """,
            (new_comment, reminder_at, chat_id, message_id),
        )
        await db.commit()

        # 5. ЛОГИ
        if reminder_at:
            logger.info(f"update_order_comment: Reply '{additional_comment}' → reminder={reminder_at}")
        else:
            logger.info(f"update_order_comment: no reminder in '{additional_comment}'")
        
        return True


async def update_order_status(order_id: int, new_status: str) -> bool:
    """Обновляет статус заказа по order_id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE orders SET status = ? WHERE id = ?",
            (new_status, order_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def bulk_update_order_status(order_ids: list[int], new_status: str) -> int:
    """Массово обновляет статус заказов. Возвращает количество обновлённых."""
    if not order_ids:
        return 0
    async with aiosqlite.connect(DB_PATH) as db:
        placeholders = ",".join("?" * len(order_ids))
        cursor = await db.execute(
            f"UPDATE orders SET status = ? WHERE id IN ({placeholders})",
            (new_status, *order_ids),
        )
        await db.commit()
        return cursor.rowcount


async def get_order_by_id(order_id: int) -> Optional[dict]:
    """Возвращает заказ по id."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
        row = await cursor.fetchone()
        if row:
            return dict(row)
        return None


async def get_order_by_chat_and_message(chat_id: int, message_id: int) -> Optional[dict]:
    """Возвращает заказ по chat_id и message_id."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM orders WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        )
        row = await cursor.fetchone()
        if row:
            return dict(row)
        return None


async def get_orders_by_status(status: str, limit: Optional[int] = None) -> list[dict]:
    """Возвращает список заказов с указанным статусом, отсортированных по created_at DESC."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = """
            SELECT
                id,
                created_at,
                manager_name,
                status,
                model,
                price,
                address,
                phone,
                customer_name,
                comment
            FROM orders
            WHERE status = ?
            ORDER BY created_at DESC
        """
        if limit:
            query += f" LIMIT {limit}"
        cursor = await db.execute(query, (status,))
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def search_orders(query: str, limit: Optional[int] = None) -> List[Dict]:
    """
    Поиск заказов по тексту.
    Полностью в Python, с регистронезависимой нормализацией (кириллица + латиница).
    Ищет по подстроке во всех основных текстовых полях.
    """
    q_raw = (query or "").strip()
    if not q_raw:
        logger.info("🔎 search_orders: пустой запрос, возвращаем пустой список")
        return []

    q_norm = normalize_for_search(q_raw)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        sql = """
            SELECT
                id,
                created_at,
                manager_name,
                status,
                model,
                price,
                address,
                phone,
                customer_name,
                comment,
                contact_raw
            FROM orders
            ORDER BY created_at DESC
        """
        cursor = await db.execute(sql)
        rows = await cursor.fetchall()
        all_orders = [dict(row) for row in rows]

        def matches(order: dict) -> bool:
            haystacks = [
                order.get("model") or "",
                order.get("price") or "",
                order.get("address") or "",
                order.get("contact_raw") or "",
                order.get("phone") or "",
                order.get("customer_name") or "",
                order.get("comment") or "",
                order.get("manager_name") or "",
            ]
            for h in haystacks:
                h_norm = normalize_for_search(str(h))
                if q_norm in h_norm:
                    return True
            return False

        filtered = [o for o in all_orders if matches(o)]

        if limit is not None:
            filtered = filtered[:limit]

        logger.info(
            "📊 search_orders(Python-only): query=%r total_rows=%d matched_rows=%d",
            query, len(all_orders), len(filtered),
        )

        return filtered


async def get_orders_for_report() -> list[dict]:
    """Возвращает все заказы для отчёта."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                id,
                created_at,
                manager_name,
                status,
                model,
                price,
                address,
                phone,
                customer_name,
                comment
            FROM orders
            ORDER BY created_at DESC
            """
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_orders_for_date(target_date: date) -> list[dict]:
    """Возвращает все заказы за указанную дату (по created_at)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        date_str = target_date.isoformat()
        cursor = await db.execute(
            """
            SELECT
                id,
                created_at,
                manager_name,
                status,
                model,
                price,
                address,
                phone,
                customer_name,
                comment
            FROM orders
            WHERE DATE(created_at) = DATE(?)
            ORDER BY created_at DESC
            """,
            (date_str,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_active_orders_for_date(target_date: date) -> list[dict]:
    """Возвращает активные заказы за указанную дату (созданные или обновлённые сегодня, статусы не paid/canceled)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        date_str = target_date.isoformat()
        cursor = await db.execute(
            """
            SELECT
                id,
                created_at,
                manager_name,
                status,
                model,
                price,
                address,
                phone,
                customer_name,
                comment
            FROM orders
            WHERE (DATE(created_at) = DATE(?) OR DATE(updated_at) = DATE(?))
              AND status NOT IN ('paid', 'canceled')
            ORDER BY created_at DESC
            """,
            (date_str, date_str),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_orders_with_reminders(now_datetime: datetime) -> list[dict]:
    """Возвращает заказы с активными напоминаниями (reminder_at <= now, reminder_sent = 0)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        now_str = now_datetime.isoformat()
        cursor = await db.execute(
            """
            SELECT
                id,
                chat_id,
                model,
                price,
                address,
                phone,
                customer_name,
                comment,
                manager_name,
                created_at,
                status,
                reminder_at
            FROM orders
            WHERE reminder_at IS NOT NULL
              AND reminder_at <= ?
              AND reminder_sent = 0
            ORDER BY reminder_at ASC
            """,
            (now_str,),
        )
        rows = await cursor.fetchall()
        result = [dict(row) for row in rows]
        if result:
            order_ids = [str(order["id"]) for order in result]
            reminder_times = [order["reminder_at"] for order in result]
            logger.info(f"get_orders_with_reminders: now={now_str}, found {len(result)} → #{', #'.join(order_ids)} ({', '.join(reminder_times)})")
        else:
            logger.info(f"get_orders_with_reminders: now={now_str}, found 0 orders")
        return result


async def mark_reminder_sent(order_id: int) -> None:
    """Отмечает напоминание как отправленное."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE orders SET reminder_sent = 1 WHERE id = ?",
            (order_id,),
        )
        await db.commit()


async def is_user_authorized(user_id: int) -> bool:
    """Проверяет, авторизован ли пользователь."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT authorized FROM authorized_users WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        if row:
            return bool(row[0])
        return False


async def authorize_user(user_id: int) -> None:
    """Авторизует пользователя (бессрочно)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO authorized_users (user_id, authorized) VALUES (?, 1)",
            (user_id,),
        )
        await db.commit()


async def get_daily_report_enabled(chat_id: int) -> bool:
    """Возвращает, включен ли ежедневный отчёт для чата."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT daily_report_enabled FROM settings WHERE chat_id = ?",
            (chat_id,),
        )
        row = await cursor.fetchone()
        if row:
            return bool(row[0])
        return False


async def set_daily_report_enabled(chat_id: int, enabled: bool) -> None:
    """Включает или выключает ежедневный отчёт для чата."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO settings (chat_id, daily_report_enabled)
            VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET daily_report_enabled = ?
            """,
            (chat_id, 1 if enabled else 0, 1 if enabled else 0),
        )
        await db.commit()


async def get_report_chat_id(chat_id: int) -> Optional[int]:
    """Возвращает chat_id для отправки отчётов (если не установлен, возвращает переданный chat_id)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT report_chat_id FROM settings WHERE chat_id = ?",
            (chat_id,),
        )
        row = await cursor.fetchone()
        if row and row[0]:
            return row[0]
        return chat_id


async def set_report_chat_id(chat_id: int, report_chat_id: int) -> None:
    """Устанавливает chat_id для отправки отчётов."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO settings (chat_id, report_chat_id)
            VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET report_chat_id = ?
            """,
            (chat_id, report_chat_id, report_chat_id),
        )
        await db.commit()


async def get_last_report_date(chat_id: int) -> Optional[date]:
    """Возвращает дату последнего отправленного отчёта."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT last_report_date FROM settings WHERE chat_id = ?",
            (chat_id,),
        )
        row = await cursor.fetchone()
        if row and row[0]:
            try:
                return date.fromisoformat(row[0])
            except ValueError:
                return None
        return None


async def set_last_report_date(chat_id: int, report_date: date) -> None:
    """Устанавливает дату последнего отправленного отчёта."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO settings (chat_id, last_report_date)
            VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET last_report_date = ?
            """,
            (chat_id, report_date.isoformat(), report_date.isoformat()),
        )
        await db.commit()


async def update_order_after_edit(
    chat_id: int,
    message_id: int,
    parsed: dict,
    phone: Optional[str],
    customer_name: str,
    reminder_at: Optional[str] = None,
) -> bool:
    """
    Обновляет заказ по chat_id + message_id на основе распарсенного текста.
    Используется при редактировании исходного сообщения заказа.
    """
    now_str = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            UPDATE orders
            SET
                model = ?,
                price = ?,
                address = ?,
                contact_raw = ?,
                phone = ?,
                customer_name = ?,
                comment = ?,
                updated_at = ?,
                reminder_at = ?,
                reminder_sent = 0
            WHERE chat_id = ? AND message_id = ?
            """,
            (
                parsed.get("model", ""),
                parsed.get("price", ""),
                parsed.get("address", ""),
                parsed.get("contact", ""),
                phone,
                customer_name,
                parsed.get("comment", ""),
                now_str,
                reminder_at,
                chat_id,
                message_id,
            ),
        )
        await db.commit()
        updated = cursor.rowcount > 0
        if updated:
            logger.info(f"✏️ Заказ обновлён (chat_id={chat_id}, message_id={message_id})")
            if reminder_at:
                logger.info(f"   Напоминание установлено на: {reminder_at}")
        return updated
async def update_order_by_id(
    order_id: int,
    parsed: dict,
    phone: Optional[str] = None,
    customer_name: str = ""
) -> bool:
    """Обновляет заказ по ID на основе распарсенного текста."""
    from handlers.orders import parse_reminder_datetime
    
    now_str = datetime.now().isoformat()
    
    async with aiosqlite.connect(DB_PATH) as db:
        # Проверяем существование заказа
        cursor = await db.execute("SELECT id FROM orders WHERE id = ?", (order_id,))
        if not await cursor.fetchone():
            return False
        
        # Парсим напоминание
        reminder_dt = parse_reminder_datetime(parsed.get("comment", ""))
        reminder_at = reminder_dt.isoformat() if reminder_dt else None
        
        cursor = await db.execute("""
            UPDATE orders SET 
                model = ?, 
                price = ?, 
                address = ?, 
                contact_raw = ?, 
                phone = ?, 
                customer_name = ?, 
                comment = ?, 
                updated_at = ?, 
                reminder_at = ?, 
                reminder_sent = 0 
            WHERE id = ?
        """, (
            parsed.get("model", ""),
            parsed.get("price", ""),
            parsed.get("address", ""),
            parsed.get("contact", ""),
            phone,
            customer_name,
            parsed.get("comment", ""),
            now_str,
            reminder_at,
            order_id
        ))
        await db.commit()
        
        updated = cursor.rowcount > 0
        if updated:
            logger.info(f"✅ Заказ #{order_id} обновлён через update_order_by_id")
            if reminder_at:
                logger.info(f"   Напоминание установлено на: {reminder_at}")
        
        return updated
