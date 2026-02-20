"""Конфигурация nano_crm."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")

# Определяем путь к базе данных
data_dir = Path(__file__).parent.parent / "data"
# Создаем папку, если её нет
data_dir.mkdir(exist_ok=True)

DB_PATH = str(data_dir / "nano_crm.db")
BOT_PIN = "1234"  # ← HARDCODE! 100% работает!
DAILY_REPORT_HOUR = 19
DAILY_REPORT_MINUTE = 0

if not TG_BOT_TOKEN:
    raise ValueError("TG_BOT_TOKEN не найден в .env")
