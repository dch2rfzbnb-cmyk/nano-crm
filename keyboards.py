"""Reply-клавиатуры для бота."""
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

BUTTON_REPORT = "📊 Отчёт"
BUTTON_NEW = "🆕 Новые"
BUTTON_IN_PROGRESS = "📦 В работе"
BUTTON_PAID = "✅ Оплачены"
BUTTON_DELIVERED = "🚚 Доставка"
BUTTON_CANCELED = "❌ Отменены"
BUTTON_SEARCH = "🔍 Поиск"
BUTTON_DAILY_REPORT_TOGGLE_ON = "🔔 Ежедневный отчёт: ВКЛ"
BUTTON_DAILY_REPORT_TOGGLE_OFF = "🔕 Ежедневный отчёт: ВЫКЛ"

KEYBOARD_BUTTONS = [
    BUTTON_REPORT,
    BUTTON_NEW,
    BUTTON_IN_PROGRESS,
    BUTTON_PAID,
    BUTTON_DELIVERED,
    BUTTON_CANCELED,
    BUTTON_SEARCH,
    BUTTON_DAILY_REPORT_TOGGLE_ON,
    BUTTON_DAILY_REPORT_TOGGLE_OFF,
]


def get_main_keyboard(daily_report_enabled: bool = False) -> ReplyKeyboardMarkup:
    """Возвращает основную reply-клавиатуру."""
    daily_report_button = (
        BUTTON_DAILY_REPORT_TOGGLE_ON if daily_report_enabled else BUTTON_DAILY_REPORT_TOGGLE_OFF
    )
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BUTTON_REPORT)],
            [
                KeyboardButton(text=BUTTON_NEW),
                KeyboardButton(text=BUTTON_IN_PROGRESS),
            ],
            [
                KeyboardButton(text=BUTTON_PAID),
                KeyboardButton(text=BUTTON_DELIVERED),
                KeyboardButton(text=BUTTON_CANCELED),
            ],
            [KeyboardButton(text=BUTTON_SEARCH)],
            [KeyboardButton(text=daily_report_button)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )
