import os
import re
import json
import logging
import asyncio
import bcrypt
import io
import pytz
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
from typing import List, Optional, Dict, Union

from aiogram import Bot, Dispatcher, types, Router, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InputFile,
    InputMediaPhoto, InputMediaDocument, ContentType
)
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramBadRequest
from openpyxl import Workbook
from aiogram.types import FSInputFile

import aiosqlite
from PIL import Image, ImageDraw, ImageFont
print("✅ main.py запускается...")

# Загрузка переменных окружения
load_dotenv()

# Конфигурация бота
class Config:
    BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
    ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "776778155").split(",") if x]
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "sunshinepass")
    TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
    SLOTS_DAYS_AHEAD = int(os.getenv("SLOTS_DAYS_AHEAD", "30"))
    SESSION_TIMEOUT = int(os.getenv("SESSION_TIMEOUT_MINUTES", "30"))
    DEFAULT_LANGUAGE = os.getenv("DEFAULT_LANGUAGE", "ru")
    PHOTOGRAPHER_IDS = [int(x) for x in os.getenv("PHOTOGRAPHER_IDS", "").split(",") if x]
    DISCOUNT_PERCENT = int(os.getenv("DISCOUNT_PERCENT", "0"))
    MIN_REVIEWS_FOR_DISCOUNT = int(os.getenv("MIN_REVIEWS_FOR_DISCOUNT", "3"))
    PORTFOLIO_PHOTOS = os.getenv("PORTFOLIO_PHOTOS", "").split(",")

    @classmethod
    def validate_config(cls):
        if cls.BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
            raise ValueError("Bot token not configured in .env file!")
        if not cls.ADMIN_IDS:
            raise ValueError("No admin IDs configured in .env file!")
        if not cls.ADMIN_PASSWORD or len(cls.ADMIN_PASSWORD) < 8:
            raise ValueError("Admin password must be at least 8 characters long!")

# Глобальный хэш пароля
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH")
if not ADMIN_PASSWORD_HASH:
    salt = bcrypt.gensalt()
    ADMIN_PASSWORD_HASH = bcrypt.hashpw(Config.ADMIN_PASSWORD.encode(), salt).decode()
    with open(".env", "a") as f:
        f.write(f"\nADMIN_PASSWORD_HASH={ADMIN_PASSWORD_HASH}")
else:
    ADMIN_PASSWORD_HASH = ADMIN_PASSWORD_HASH.encode()

# Настройка логирования
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Handlers
file_handler = RotatingFileHandler("bot.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(console_handler)

# FSM состояния
class BookingState(StatesGroup):
    picking_date = State()
    picking_time = State()
    waiting_type = State()
    waiting_name = State()
    waiting_contact = State()
    confirming = State()
    viewing_booking = State()
    feedback_text = State()
    feedback_photo = State()
    feedback_rating = State()

class AdminState(StatesGroup):
    waiting_password = State()
    adding_slot = State()
    deleting_slot = State()
    waiting_template_key = State()
    waiting_template_text = State()
    changing_password = State()
    waiting_feedback_reply = State()
    waiting_discount = State()
    adding_photographer = State()

# Сессии администраторов
logged_in_admins = {}
SESSION_TIMEOUT = timedelta(minutes=Config.SESSION_TIMEOUT)

# Загрузка шаблонов сообщений
def load_templates():
    default_templates = {
        "start": "Привет! Я бот для записи на фотосессии. Нажмите /book, чтобы записаться.",
        "ask_date": "📆 Выберите дату для фотосессии:",
        "ask_time": "⏰ Выберите время для фотосессии:",
        "ask_type": "📷 Какой вид съёмки вы хотите? (например, портрет, свадьба)",
        "ask_name": "👤 Пожалуйста, введите ваше имя:",
        "ask_contact": "📞 Отправьте контактный телефон (или введите вручную):",
        "confirm_details": "Проверьте данные записи:\nДата: {date}\nВремя: {time}\nТип съёмки: {shoot_type}\nИмя: {name}\nТелефон: {phone}\n\nПодтвердить запись?",
        "booking_confirmed": "✅ Ваша запись подтверждена на {date} {time}! Спасибо!",
        "booking_cancelled": "❌ Запись отменена. Если хотите начать заново, отправьте /book.",
        "slot_taken_error": "❗ Этот слот уже занят, выберите другое время.",
        "double_booking_error": "❗ Вы уже записаны на эту дату.",
        "admin_enter_password": "🔐 Введите пароль администратора:",
        "admin_login_success": "✅ Режим администратора активирован.",
        "admin_login_fail": "❌ Неверный пароль.",
        "admin_menu": "⚙️ Админ-команды:\n/addslot - добавить слот\n/delslot - удалить слот\n/export - экспорт записей\n/templates - изменить шаблоны\n/logout - выйти",
        "admin_add_slot_prompt": "📅 Отправьте дату и время нового слота (ДД.ММ.ГГГГ ЧЧ:ММ):",
        "admin_add_slot_success": "✅ Слот {date} {time} добавлен.",
        "admin_add_slot_exists": "⚠️ Такой слот уже существует.",
        "admin_del_slot_prompt": "❌ Отправьте дату и время слота для удаления (ДД.ММ.ГГГГ ЧЧ:ММ):",
        "admin_del_slot_success": "✅ Слот {date} {time} удалён.",
        "admin_del_slot_not_found": "⚠️ Слот с такой датой и временем не найден.",
        "admin_del_slot_booked": "⚠️ Нельзя удалить слот: на него есть запись.",
        "admin_export_success": "✅ Экспортировано записей: {count}.",
        "admin_export_no_data": "⚠️ Записей для экспорта нет.",
        "admin_template_list": "📋 Список шаблонов: {keys}\nОтправьте ключ шаблона для редактирования.",
        "admin_template_prompt": "✏️ Отправьте новый текст для шаблона \"{key}\":",
        "admin_template_updated": "✅ Шаблон \"{key}\" обновлён.",
        "admin_template_invalid": "❌ Шаблон с ключом \"{key}\" не найден.",
        "reminder_client": "🔔 Напоминание: завтра в {time} у вас фотосессия!",
        "reminder_admin": "🔔 Напоминание: завтра в {time} фотосессия с {name} (тел: {phone}).",
        "confirmation_card": "📷 Ваша фотосессия подтверждена!\n\n📅 Дата: {date}\n⏰ Время: {time}\n👤 Имя: {name}\n📞 Телефон: {phone}\n📸 Тип съемки: {shoot_type}\n\nСохраните эту карточку!",
        "portfolio_error": "🚫 Портфолио временно недоступно. Приносим извинения!",
        "no_active_bookings": "ℹ️ У вас нет активных записей. Используйте /book для записи.",
        "help_text": "📋 Доступные команды:\n/start - начать работу\n/book - записаться\n/portfolio - портфолио\n/mybooking - ваша запись\n/faq - вопросы\n/help - справка",
        "faq_text": "❓ Часто задаваемые вопросы:\n\n1. Как записаться?\n - Используйте /book\n\n2. Можно ли перенести запись?\n - Да, напишите администратору",
        "admin_logout_confirm": "❓ Вы уверены, что хотите выйти из режима администратора?",
        "logout_cancelled": "✅ Выход отменён.",
        "logout_success": "✅ Вы успешно вышли из режима администратора.",
        "feedback_prompt": "📝 Пожалуйста, напишите ваш отзыв о фотосессии:",
        "feedback_photo_prompt": "📸 Хотите прикрепить фото к отзыву?",
        "feedback_rating_prompt": "⭐ Оцените фотосессию от 1 до 5:",
        "feedback_thanks": "🙏 Спасибо за ваш отзыв!",
        "feedback_received": "📩 Новый отзыв от {name} (ID: {user_id}):\n\n{feedback}\n\nРейтинг: {rating}/5",
        "stats_text": "📊 Статистика:\n\nВсего записей: {total}\nЗа неделю: {last_week}\nСвободных слотов: {free_slots}\nСредний рейтинг: {avg_rating}",
        "language_set": "🌐 Язык изменён на {language}",
        "language_select": "🌐 Выберите язык:",
        "discount_info": "🎉 Вам доступна скидка {percent}% за {reviews} отзывов!",
        "photographer_assigned": "📸 Ваш фотограф: @{username}",
        "photographer_notify": "📸 Новая запись:\nДата: {date}\nВремя: {time}\nКлиент: {name}\nТел: {phone}",
        "photographer_add_prompt": "📝 Введите ID и username фотографа (формат: id username):",
        "photographer_add_success": "✅ Фотограф @{username} добавлен!",
        "photographer_list": "📸 Список фотографов:\n{list}"
    }

    if not os.path.exists("templates.json"):
        with open("templates.json", "w", encoding="utf-8") as f:
            json.dump(default_templates, f, ensure_ascii=False, indent=2)
        return default_templates

    try:
        with open("templates.json", "r", encoding="utf-8") as f:
            custom_templates = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error(f"Ошибка загрузки шаблонов: {e}. Используются стандартные шаблоны")
        return default_templates

    for key in default_templates:
        if key not in custom_templates:
            custom_templates[key] = default_templates[key]
            logger.warning(f"В шаблонах отсутствует ключ '{key}', добавлен стандартный текст")

    return custom_templates

templates = load_templates()

# Инициализация бота
storage = MemoryStorage()
bot = Bot(token=Config.BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# Валидация данных
def validate_phone(phone: str) -> bool:
    return re.match(r'^\+?\d{10,15}$', phone) is not None

def validate_name(name: str) -> bool:
    return 2 <= len(name) <= 30 and bool(re.match(r'^[a-zA-Zа-яА-ЯёЁ\s\-]+$', name))

def validate_text(text: str) -> bool:
    return 1 <= len(text) <= 500

def validate_date_format(date_str: str) -> bool:
    try:
        datetime.strptime(date_str, "%d.%m.%Y")
        return True
    except ValueError:
        return False

# Управление сессиями
async def check_admin_session(admin_id: int) -> bool:
    if admin_id not in logged_in_admins:
        return False
    
    last_activity = logged_in_admins[admin_id]
    if datetime.now() - last_activity > SESSION_TIMEOUT:
        del logged_in_admins[admin_id]
        return False
    
    logged_in_admins[admin_id] = datetime.now()
    return True
# Безопасное создание inline-клавиатуры
def create_inline_keyboard(buttons: list) -> InlineKeyboardMarkup:
    """Гарантирует, что inline_keyboard всегда заполнен"""
    if not buttons or not any(buttons):
        buttons = [[InlineKeyboardButton(text="⏳ Недоступно", callback_data="none")]]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# Клавиатуры
contact_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📱 Отправить номер телефона", request_contact=True)]],
    resize_keyboard=True,
    one_time_keyboard=True
)

def get_confirm_keyboard():
    buttons = [
        [
            InlineKeyboardButton(text="✏️ Изменить имя", callback_data="edit:name"),
            InlineKeyboardButton(text="✏️ Изменить телефон", callback_data="edit:phone")
        ],
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm:yes"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="confirm:no")
        ]
    ]
    return create_inline_keyboard(buttons)


def get_admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить слот", callback_data="admin:addslot")],
        [InlineKeyboardButton(text="🗑️ Удалить слот", callback_data="admin:delslot")],
        [InlineKeyboardButton(text="📤 Экспорт записей", callback_data="admin:export")],
        [InlineKeyboardButton(text="📝 Редактировать шаблоны", callback_data="admin:templates")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
        [InlineKeyboardButton(text="📸 Управление фотографами", callback_data="admin:photographers")],
        [InlineKeyboardButton(text="🎁 Управление скидками", callback_data="admin:discount")],
        [InlineKeyboardButton(text="📬 Отзывы", callback_data="admin:feedbacks")],
        [InlineKeyboardButton(text="🔐 Сменить пароль", callback_data="admin:changepw")],
        [InlineKeyboardButton(text="🚪 Выйти", callback_data="admin:logout")]
    ])

def get_photo_keyboard():
    buttons = [
        [
            InlineKeyboardButton(text="✅ Да", callback_data="feedback:yes"),
            InlineKeyboardButton(text="❌ Нет", callback_data="feedback:no")
        ]
    ]
    return create_inline_keyboard(buttons)

def get_rating_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="1⭐", callback_data="rating:1"),
            InlineKeyboardButton(text="2⭐", callback_data="rating:2"),
            InlineKeyboardButton(text="3⭐", callback_data="rating:3"),
            InlineKeyboardButton(text="4⭐", callback_data="rating:4"),
            InlineKeyboardButton(text="5⭐", callback_data="rating:5")
        ]
    ])

def get_language_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang:ru"),
            InlineKeyboardButton(text="🇬🇧 English", callback_data="lang:en")
        ]
    ])

def get_logout_confirmation_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да", callback_data="logout:yes"),
            InlineKeyboardButton(text="❌ Нет", callback_data="logout:no")
        ]
    ])

# Database Operations
async def init_db():
    async with aiosqlite.connect("bot.db") as db:
        await db.execute("PRAGMA foreign_keys = ON")
        
        await db.execute("""
        CREATE TABLE IF NOT EXISTS slots (
            id INTEGER PRIMARY KEY,
            datetime TEXT UNIQUE,
            photographer_id INTEGER
        )
        """)
        
        await db.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY,
            slot_id INTEGER UNIQUE,
            user_id INTEGER,
            name TEXT,
            contact TEXT,
            shoot_type TEXT,
            reminder_sent INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(slot_id) REFERENCES slots(id) ON DELETE CASCADE
        )
        """)
        
        await db.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            user_name TEXT,
            text TEXT,
            photo_id TEXT,
            rating INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        
        await db.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            language TEXT DEFAULT 'ru',
            discount_eligible INTEGER DEFAULT 0
        )
        """)
        
        await db.execute("""
        CREATE TABLE IF NOT EXISTS photographers (
            id INTEGER PRIMARY KEY,
            user_id INTEGER UNIQUE,
            username TEXT,
            specialties TEXT
        )
        """)
        
        # Индексы
        await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_bookings_slot_unique ON bookings(slot_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_slots_datetime ON slots(datetime)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_bookings_user ON bookings(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_bookings_created ON bookings(created_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_feedback_rating ON feedback(rating)")
        
        await db.commit()

async def get_user_language(user_id: int) -> str:
    async with aiosqlite.connect("bot.db") as db:
        cursor = await db.execute(
            "SELECT language FROM user_settings WHERE user_id = ?",
            (user_id,)
        )
        result = await cursor.fetchone()
        return result[0] if result else Config.DEFAULT_LANGUAGE

async def set_user_language(user_id: int, language: str):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute(
            "INSERT OR REPLACE INTO user_settings (user_id, language) VALUES (?, ?)",
            (user_id, language)
        )
        await db.commit()

async def get_available_slots():
    now = datetime.now()
    next_month = now + timedelta(days=Config.SLOTS_DAYS_AHEAD)
    
    async with aiosqlite.connect("bot.db") as db:
        cursor = await db.execute(
            """SELECT s.id, s.datetime, s.photographer_id, p.username
            FROM slots s
            LEFT JOIN bookings b ON s.id = b.slot_id
            LEFT JOIN photographers p ON s.photographer_id = p.id
            WHERE b.slot_id IS NULL AND datetime >= ? AND datetime <= ?
            ORDER BY datetime""",
            (now.strftime("%Y-%m-%d %H:%M:%S"), next_month.strftime("%Y-%m-%d %H:%M:%S"))
        )
        return await cursor.fetchall()

async def add_booking(slot_id: int, user_id: int, name: str, contact: str, shoot_type: str):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute(
            """INSERT INTO bookings (slot_id, user_id, name, contact, shoot_type)
            VALUES (?, ?, ?, ?, ?)""",
            (slot_id, user_id, name, contact, shoot_type))
        await db.commit()

async def add_slot(dt: datetime, photographer_id: int = None):
    iso_dt = dt.strftime("%Y-%m-%d %H:%M:%S")
    
    async with aiosqlite.connect("bot.db") as db:
        cursor = await db.execute(
            "SELECT 1 FROM slots WHERE datetime = ?",
            (iso_dt,))
        if await cursor.fetchone():
            return False
        
        await db.execute(
            "INSERT INTO slots(datetime, photographer_id) VALUES (?, ?)",
            (iso_dt, photographer_id))
        await db.commit()
        return True

async def delete_slot(dt: datetime):
    iso_dt = dt.strftime("%Y-%m-%d %H:%M:%S")
    
    async with aiosqlite.connect("bot.db") as db:
        cursor = await db.execute(
            "SELECT id FROM slots WHERE datetime = ?",
            (iso_dt,))
        slot_row = await cursor.fetchone()
        
        if not slot_row:
            return "not_found"
        
        slot_id = slot_row[0]
        
        cursor = await db.execute(
            "SELECT 1 FROM bookings WHERE slot_id = ?",
            (slot_id,))
        if await cursor.fetchone():
            return "booked"
        
        await db.execute(
            "DELETE FROM slots WHERE id = ?",
            (slot_id,))
        await db.commit()
        return "success"

async def export_bookings():
    async with aiosqlite.connect("bot.db") as db:
        cursor = await db.execute(
            """SELECT s.datetime, b.name, b.contact, b.shoot_type, b.created_at, p.username
            FROM bookings b
            JOIN slots s ON b.slot_id = s.id
            LEFT JOIN photographers p ON s.photographer_id = p.id
            ORDER BY s.datetime""")
        rows = await cursor.fetchall()
        
        if not rows:
            return None
        
        csv_lines = ["Дата,Время,Имя,Телефон,Тип съёмки,Дата записи,Фотограф"]
        
        for dt_text, name, contact, shoot_type, created_at, photographer in rows:
            dt_obj = datetime.strptime(dt_text, "%Y-%m-%d %H:%M:%S")
            created = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
            
            name_escaped = f'"{name}"' if ',' in name else name
            shoot_type_escaped = f'"{shoot_type}"' if ',' in shoot_type else shoot_type
            photographer = photographer or "Не назначен"
            
            csv_lines.append(
                f"{dt_obj.strftime('%d.%m.%Y')},"
                f"{dt_obj.strftime('%H:%M')},"
                f"{name_escaped},"
                f"{contact},"
                f"{shoot_type_escaped},"
                f"{created.strftime('%d.%m.%Y %H:%M')},"
                f"{photographer}")
        
        return "\n".join(csv_lines)

async def get_stats():
    async with aiosqlite.connect("bot.db") as db:
        cursor = await db.execute("SELECT COUNT(*) FROM bookings")
        total = (await cursor.fetchone())[0]
        
        cursor = await db.execute(
            "SELECT COUNT(*) FROM bookings WHERE datetime(created_at) >= datetime('now', '-7 days')")
        last_week = (await cursor.fetchone())[0]
        
        cursor = await db.execute(
            """SELECT COUNT(*)
            FROM slots s
            LEFT JOIN bookings b ON s.id = b.slot_id
            WHERE b.slot_id IS NULL AND datetime >= datetime('now')""")
        free_slots = (await cursor.fetchone())[0]
        
        cursor = await db.execute("SELECT AVG(rating) FROM feedback WHERE rating IS NOT NULL")
        avg_rating = round((await cursor.fetchone())[0] or 0, 1)
        
        return {
            "total": total,
            "last_week": last_week,
            "free_slots": free_slots,
            "avg_rating": avg_rating
        }

async def add_feedback(user_id: int, user_name: str, text: str, photo_id: str = None, rating: int = None):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute(
            """INSERT INTO feedback (user_id, user_name, text, photo_id, rating)
            VALUES (?, ?, ?, ?, ?)""",
            (user_id, user_name, text, photo_id, rating))
        await db.commit()
        
        # Проверяем, достаточно ли отзывов для скидки
        cursor = await db.execute(
            "SELECT COUNT(*) FROM feedback WHERE user_id = ?",
            (user_id,))
        feedback_count = (await cursor.fetchone())[0]
        
        if feedback_count >= Config.MIN_REVIEWS_FOR_DISCOUNT:
            await db.execute(
                "UPDATE user_settings SET discount_eligible = 1 WHERE user_id = ?",
                (user_id,))
            await db.commit()
            return True
        
        return False

async def check_discount_eligible(user_id: int):
    async with aiosqlite.connect("bot.db") as db:
        cursor = await db.execute(
            "SELECT discount_eligible FROM user_settings WHERE user_id = ?",
            (user_id,))
        result = await cursor.fetchone()
        return result[0] if result else False

async def get_photographers():
    async with aiosqlite.connect("bot.db") as db:
        cursor = await db.execute("SELECT id, user_id, username, specialties FROM photographers")
        return await cursor.fetchall()

async def add_photographer(user_id: int, username: str, specialties: str = ""):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute(
            """INSERT INTO photographers (user_id, username, specialties)
            VALUES (?, ?, ?)""",
            (user_id, username, specialties))
        await db.commit()

async def assign_photographer_to_slot(slot_id: int, photographer_id: int):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute(
            "UPDATE slots SET photographer_id = ? WHERE id = ?",
            (photographer_id, slot_id))
        await db.commit()

# Helper functions
from typing import Optional

def parse_datetime_ru(dt_str: str) -> Optional[datetime]:

    try:
        return datetime.strptime(dt_str, "%d.%m.%Y %H:%M")
    except ValueError:
        return None

def format_datetime_ru(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y %H:%M")

async def generate_booking_card(data: dict) -> io.BytesIO:
    try:
        try:
            font = ImageFont.truetype("arial.ttf", 30)
        except IOError:
            font = ImageFont.load_default()
        
        image = Image.new('RGB', (800, 600), color=(73, 109, 137))
        draw = ImageDraw.Draw(image)
        
        text_lines = [
            "📷 Подтверждение записи",
            "",
            f"📅 Дата: {data['date']}",
            f"⏰ Время: {data['time']}",
            f"👤 Имя: {data['name']}",
            f"📞 Телефон: {data['phone']}",
            f"📸 Тип съемки: {data['shoot_type']}",
            "",
            "Сохраните эту карточку!"
        ]
        
        y_position = 50
        for line in text_lines:
            draw.text((50, y_position), line, fill=(255, 255, 255), font=font)
            y_position += 40
        
        buf = io.BytesIO()
        image.save(buf, format='PNG')
        buf.seek(0)
        return buf
    except Exception as e:
        logger.error(f"Error generating booking card: {e}")
        return None

async def send_confirmation_card(user_id: int, booking_data: dict):
    card_image = await generate_booking_card(booking_data)
    
    if card_image:
        try:
            await bot.send_photo(
                user_id,
                photo=InputFile(card_image, filename="booking.png"),
                caption=templates["confirmation_card"].format(**booking_data))
            return
        except Exception as e:
            logger.error(f"Failed to send image card: {e}")
    
    card_text = templates["confirmation_card"].format(
        date=booking_data["date"],
        time=booking_data["time"],
        name=booking_data["name"],
        phone=booking_data["phone"],
        shoot_type=booking_data["shoot_type"])
    
    await bot.send_message(user_id, card_text)

async def send_portfolio(user_id: int):
    try:
        if not Config.PORTFOLIO_PHOTOS:
            raise ValueError("No portfolio photos configured")
            
        media = []
        for photo_url in Config.PORTFOLIO_PHOTOS:
            if photo_url.strip():
                media.append(InputMediaPhoto(media=photo_url.strip()))
        
        if media:
            await bot.send_media_group(user_id, media)
        else:
            await bot.send_message(user_id, templates["portfolio_error"])
    except Exception as e:
        logger.error(f"Ошибка отправки портфолио: {e}")
        await bot.send_message(user_id, templates["portfolio_error"])

# User Handlers
@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(templates["start"])
    logger.info(f"User {message.from_user.id} started bot")

@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(templates["help_text"])
    logger.info(f"User {message.from_user.id} requested help")

@router.message(Command("faq"))
async def cmd_faq(message: Message):
    await message.answer(templates["faq_text"])
    logger.info(f"User {message.from_user.id} requested FAQ")

@router.message(Command("portfolio"))
async def cmd_portfolio(message: Message):
    await send_portfolio(message.chat.id)
    logger.info(f"User {message.from_user.id} requested portfolio")

@router.message(Command("mybooking"))
async def cmd_mybooking(message: Message):
    user_id = message.from_user.id
    
    async with aiosqlite.connect("bot.db") as db:
        cursor = await db.execute(
            """SELECT s.datetime, b.name, b.contact, b.shoot_type, p.username
            FROM bookings b
            JOIN slots s ON b.slot_id = s.id
            LEFT JOIN photographers p ON s.photographer_id = p.id
            WHERE b.user_id = ? AND s.datetime >= ?
            ORDER BY s.datetime LIMIT 1""",
            (user_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        booking = await cursor.fetchone()
        
        if booking:
            dt_obj = datetime.strptime(booking[0], "%Y-%m-%d %H:%M:%S")
            data = {
                "date": dt_obj.strftime("%d.%m.%Y"),
                "time": dt_obj.strftime("%H:%M"),
                "name": booking[1],
                "phone": booking[2],
                "shoot_type": booking[3],
                "photographer": booking[4] or "Не назначен"
            }
            
            # Generate and send card
            card_image = await generate_booking_card(data)
            if card_image:
                try:
                    await message.answer_photo(
                        photo=InputFile(card_image, filename="booking.png"),
                        caption="✅ Ваша текущая запись:"
                    )
                    logger.info(f"User {user_id} viewed their booking (image)")
                    return
                except Exception as e:
                    logger.error(f"Failed to send booking card: {e}")
            
            # Fallback to text
            booking_text = (
                "✅ Ваша текущая запись:\n\n"
                f"📅 Дата: {data['date']}\n"
                f"⏰ Время: {data['time']}\n"
                f"👤 Имя: {data['name']}\n"
                f"📞 Телефон: {data['phone']}\n"
                f"�� Тип съемки: {data['shoot_type']}\n"
                f"👨‍🎨 Фотограф: {data['photographer']}"
            )
            await message.answer(booking_text)
            logger.info(f"User {user_id} viewed their booking (text)")
        else:
            await message.answer(templates["no_active_bookings"])
            logger.info(f"User {user_id} has no active bookings")

@router.message(Command("feedback"))
async def cmd_feedback(message: Message, state: FSMContext):
    await message.answer(templates["feedback_prompt"])
    await state.set_state(BookingState.feedback_text)
    logger.info(f"User {message.from_user.id} started feedback")

@router.message(BookingState.feedback_text)
async def process_feedback_text(message: Message, state: FSMContext):
    feedback_text = message.text.strip()
    await state.update_data(feedback_text=feedback_text)
    await message.answer(templates["feedback_photo_prompt"], reply_markup=get_photo_keyboard())
    await state.set_state(BookingState.feedback_photo)

@router.callback_query(BookingState.feedback_photo, F.data.startswith("feedback:"))
async def process_feedback_photo(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split(":")[1]
    
    if action == "yes":
        await callback.message.answer("📸 Отправьте фото для отзыва:")
        await state.set_state(BookingState.feedback_rating)
    else:
        await callback.message.answer(templates["feedback_rating_prompt"], reply_markup=get_rating_keyboard())
        await state.set_state(BookingState.feedback_rating)
    
    await callback.answer()

@router.message(BookingState.feedback_photo, F.photo)
async def process_feedback_photo_upload(message: Message, state: FSMContext):
    photo_id = message.photo[-1].file_id
    await state.update_data(feedback_photo=photo_id)
    await message.answer(templates["feedback_rating_prompt"], reply_markup=get_rating_keyboard())
    await state.set_state(BookingState.feedback_rating)

@router.callback_query(BookingState.feedback_rating, F.data.startswith("rating:"))
async def process_feedback_rating(callback: CallbackQuery, state: FSMContext):
    rating = int(callback.data.split(":")[1])
    data = await state.get_data()
    
    user_id = callback.from_user.id
    user_name = callback.from_user.full_name
    feedback_text = data.get("feedback_text", "")
    photo_id = data.get("feedback_photo")
    
    await add_feedback(user_id, user_name, feedback_text, photo_id, rating)
    await callback.message.answer(templates["feedback_thanks"])
    
    # Notify admins
    for admin_id in Config.ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                templates["feedback_received"].format(
                    name=user_name,
                    user_id=user_id,
                    feedback=feedback_text,
                    rating=rating
                )
            )
        except Exception as e:
            logger.error(f"Failed to send feedback to admin {admin_id}: {e}")
    
    await state.clear()
    logger.info(f"User {user_id} submitted feedback with rating {rating}")

@router.message(Command("language"))
async def cmd_language(message: Message):

    await message.answer(
        templates["language_select"],
        reply_markup=get_language_keyboard()
    )

@router.callback_query(F.data.startswith("lang:"))
async def set_language(callback: CallbackQuery):
    language = callback.data.split(":")[1]
    await set_user_language(callback.from_user.id, language)
    await callback.answer(templates["language_set"].format(language=language))
    await callback.message.delete()

@router.message(Command("book"))
async def cmd_book(message: Message, state: FSMContext):
    free_slots = await get_available_slots()

    if not free_slots:
        await message.answer("😔 На данный момент нет свободных слотов для записи.")
        return

    date_to_slots = {}
    for slot in free_slots:
        dt_obj = datetime.strptime(slot[1], "%Y-%m-%d %H:%M:%S")
        date_str = dt_obj.strftime("%d.%m.%Y")
        time_str = dt_obj.strftime("%H:%M")
        photographer_info = f" (@{slot[3]})" if slot[3] else ""
        date_to_slots.setdefault(date_str, []).append((slot[0], time_str, photographer_info))

    buttons = []
    for date_str in sorted(date_to_slots.keys()):
        buttons.append([InlineKeyboardButton(text=date_str, callback_data=f"date:{date_str}")])

    if not buttons:
        await message.answer("😔 Нет доступных дат для записи.")
        return

    await message.answer(
    templates["ask_date"],
    reply_markup=create_inline_keyboard(buttons)
)

    
    await state.update_data(date_to_slots=date_to_slots)
    await state.set_state(BookingState.picking_date)

    logger.info(f"User {message.from_user.id} started booking")

@router.callback_query(F.data.startswith("date:"), BookingState.picking_date)
async def on_date_chosen(callback: CallbackQuery, state: FSMContext):
    date_str = callback.data.split(":", 1)[1]
    data = await state.get_data()
    date_to_slots = data.get("date_to_slots", {})

    if date_str not in date_to_slots:
        await callback.answer("❌ Неверная дата, попробуйте снова.", show_alert=True)
        return

    times = date_to_slots[date_str]
    time_buttons = [
        [InlineKeyboardButton(
            text=f"{time_str}{photographer_info}",
            callback_data=f"time:{slot_id}"
        )] for slot_id, time_str, photographer_info in times
    ]

    time_keyboard = create_inline_keyboard(time_buttons)

    await callback.answer()
    await callback.message.edit_text(
        f"📆 Дата: {date_str}\n{templates['ask_time']}",
        reply_markup=time_keyboard
    )
    await state.update_data(chosen_date=date_str)
    await state.set_state(BookingState.picking_time)


@router.callback_query(F.data.startswith("time:"), BookingState.picking_time)
async def on_time_chosen(callback: CallbackQuery, state: FSMContext):
    slot_id_str = callback.data.split(":", 1)[1]
    
    if not slot_id_str.isdigit():
        await callback.answer("❌ Неверный выбор времени.", show_alert=True)
        return

    slot_id = int(slot_id_str)
    data = await state.get_data()
    date_str = data.get("chosen_date")
    date_to_slots = data.get("date_to_slots", {})
    time_str = None
    photographer_info = ""

    if date_str and date_str in date_to_slots:
        for sid, t, p in date_to_slots[date_str]:
            if sid == slot_id:
                time_str = t
                photographer_info = p
                break

    if not time_str:
        await callback.answer("❌ Время не найдено, выберите другой слот.", show_alert=True)
        return

    await state.update_data(
        chosen_slot=slot_id,
        chosen_time=time_str,
        photographer_info=photographer_info
    )
    
    await callback.answer()
    await callback.message.edit_text(
        f"📆 Дата: {date_str}\n⏰ Время: {time_str}{photographer_info}\n{templates['ask_type']}",
        reply_markup=None
    )
    await state.set_state(BookingState.waiting_type)

@router.message(BookingState.waiting_type)
async def on_type_received(message: Message, state: FSMContext):
    shoot_type = message.text.strip()
    if shoot_type.lower() in ("отмена", "cancel"):
        return

    if not validate_text(shoot_type):
        await message.answer("❌ Текст слишком длинный. Пожалуйста, введите до 100 символов.")
        return

    try:
        await message.delete()
    except Exception:
        pass

    await state.update_data(shoot_type=shoot_type)
    data = await state.get_data()
    dialog_msg_id = data.get("dialog_msg_id")
    date_str = data.get("chosen_date")
    time_str = data.get("chosen_time")
    photographer_info = data.get("photographer_info", "")

    new_text = (f"📆 Дата: {date_str}\n"
                f"⏰ Время: {time_str}{photographer_info}\n"
                f"📷 Тип: {shoot_type}\n"
                f"{templates['ask_name']}")

    if not dialog_msg_id:
        await message.answer(new_text)
    else:
        try:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=dialog_msg_id,
                text=new_text
            )
        except TelegramBadRequest as e:
            logger.warning(f"Не удалось отредактировать сообщение (тип): {e}")
            await message.answer(new_text)

    await state.set_state(BookingState.waiting_name)

@router.message(BookingState.waiting_name)
async def on_name_received(message: Message, state: FSMContext):
    name = message.text.strip()
    if name.lower() in ("отмена", "cancel"):
        return

    if not validate_name(name):
        await message.answer("❌ Имя должно содержать только буквы и быть длиной 2-30 символов.")
        return

    try:
        await message.delete()
    except Exception:
        pass

    await state.update_data(client_name=name)
    data = await state.get_data()
    dialog_msg_id = data.get("dialog_msg_id")
    date_str = data.get("chosen_date")
    time_str = data.get("chosen_time")
    shoot_type = data.get("shoot_type")
    photographer_info = data.get("photographer_info", "")

    new_text = (f"📆 Дата: {date_str}\n"
                f"⏰ Время: {time_str}{photographer_info}\n"
                f"📷 Тип: {shoot_type}\n"
                f"👤 Имя: {name}")

    if not dialog_msg_id:
        await message.answer(new_text)
    else:
        try:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=dialog_msg_id,
                text=new_text
            )
        except TelegramBadRequest as e:
            logger.warning(f"Не удалось отредактировать сообщение (имя): {e}")
            await message.answer(new_text)

    await message.answer(templates["ask_contact"], reply_markup=contact_keyboard)
    await state.set_state(BookingState.waiting_contact)

@router.message(BookingState.waiting_contact, lambda m: m.contact or m.text)
async def on_contact_received(message: Message, state: FSMContext):
    if message.contact:
        phone = message.contact.phone_number
    else:
        phone = message.text.strip()

    if phone.lower() in ("отмена", "cancel"):
        return

    if not validate_phone(phone):
        await message.answer("❌ Неверный формат телефона. Пожалуйста, введите номер в формате +71234567890 или 81234567890.")
        return

    try:
        await message.delete()
    except Exception:
        pass

    dummy = await bot.send_message(message.chat.id, ".", reply_markup=ReplyKeyboardRemove())
    try:
        await dummy.delete()
    except Exception:
        pass

    await state.update_data(contact=phone)
    data = await state.get_data()
    dialog_msg_id = data.get("dialog_msg_id")
    date_str = data.get("chosen_date")
    time_str = data.get("chosen_time")
    shoot_type = data.get("shoot_type")
    name = data.get("client_name")
    photographer_info = data.get("photographer_info", "")

    confirm_text = templates["confirm_details"].format(
        date=date_str, time=f"{time_str}{photographer_info}",
        shoot_type=shoot_type, name=name, phone=phone
    )

    if not dialog_msg_id:
        await message.answer(confirm_text, reply_markup=get_confirm_keyboard())
    else:
        try:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=dialog_msg_id,
                text=confirm_text,
                reply_markup=get_confirm_keyboard()
            )
        except TelegramBadRequest as e:
            logger.warning(f"Не удалось отредактировать сообщение (контакт): {e}")
            await message.answer(confirm_text, reply_markup=get_confirm_keyboard())

    await state.set_state(BookingState.confirming)

@router.callback_query(F.data.startswith("edit:"), BookingState.confirming)
async def on_edit(callback: CallbackQuery, state: FSMContext):
    field = callback.data.split(":", 1)[1]
    
    if field == "name":
        await state.set_state(BookingState.waiting_name)
        await callback.message.edit_text("✏️ Введите новое имя:")
    elif field == "phone":
        await state.set_state(BookingState.waiting_contact)
        await callback.message.edit_text("✏️ Введите новый телефон:", reply_markup=contact_keyboard)
    
    await callback.answer()

@router.callback_query(F.data.startswith("confirm:"), BookingState.confirming)
async def on_confirm(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split(":", 1)[1]
    data = await state.get_data()
    user_id = callback.from_user.id
    dialog_msg_id = data.get("dialog_msg_id")
    date_str = data.get("chosen_date")
    time_str = data.get("chosen_time")
    shoot_type = data.get("shoot_type")
    name = data.get("client_name")
    phone = data.get("contact")
    photographer_info = data.get("photographer_info", "")

    if action == "yes":
        slot_id = data.get("chosen_slot")
        if not slot_id:
            await callback.answer("Ошибка: слот не найден.", show_alert=True)
            return

        appt_dt = datetime.strptime(f"{date_str} {time_str}", "%d.%m.%Y %H:%M")
        iso_dt = appt_dt.strftime("%Y-%m-%d %H:%M:%S")

        async with aiosqlite.connect("bot.db") as db:
            await db.execute("PRAGMA foreign_keys = ON")
            
            # Check if user already booked this slot
            cur = await db.execute(
                "SELECT 1 FROM bookings WHERE slot_id = ? AND user_id = ?",
                (slot_id, user_id)
            )
            if await cur.fetchone():
                await callback.message.edit_text(
                    "❗ Вы уже записаны на этот временной слот",
                    reply_markup=None
                )
                await state.clear()
                return

            # Check for double booking
            cur = await db.execute(
                "SELECT 1 FROM bookings b JOIN slots s ON b.slot_id = s.id "
                "WHERE b.user_id = ? AND date(s.datetime) = date(?)",
                (user_id, iso_dt)
            )
            if await cur.fetchone():
                await callback.message.edit_text(templates["double_booking_error"], reply_markup=None)
                await state.clear()
                logger.info(f"Booking failed: user {user_id} already has a booking on {date_str}.")
                await callback.answer()
                return

            # Insert booking with unique constraint protection
            try:
                await db.execute(
                    "INSERT INTO bookings (slot_id, user_id, name, contact, shoot_type) VALUES (?, ?, ?, ?, ?)",
                    (slot_id, user_id, name, phone, shoot_type)
                )
                await db.commit()
            except aiosqlite.IntegrityError:
                await callback.message.edit_text(templates["slot_taken_error"], reply_markup=None)
                await state.clear()
                logger.warning(f"Booking failed: slot already taken (race condition). User: {user_id}")
                await callback.answer()
                return

        # Send confirmation
        confirmed_text = templates["booking_confirmed"].format(date=date_str, time=f"{time_str}{photographer_info}")
        await callback.message.edit_text(confirmed_text, reply_markup=None)

        # Send confirmation card
        booking_data = {
            "date": date_str,
            "time": time_str,
            "name": name,
            "phone": phone,
            "shoot_type": shoot_type
        }
        await send_confirmation_card(user_id, booking_data)

        # Send notification to all admins and assigned photographer
        admin_text = (f"✅ Новая запись!\nДата: {date_str} {time_str}{photographer_info}\n"
                     f"Клиент: {name}\nТел: {phone}\nТип: {shoot_type}")

        # Notify admins
        for admin_id in Config.ADMIN_IDS:
            try:
                await bot.send_message(admin_id, admin_text)
            except Exception as e:
                logger.error(f"Failed to send notification to admin {admin_id}: {e}")

        # Notify assigned photographer if exists
        cursor = await db.execute(
            "SELECT photographer_id FROM slots WHERE id = ?",
            (slot_id,)
        )
        photographer_id = (await cursor.fetchone())[0]
        if photographer_id:
            try:
                await bot.send_message(
                    photographer_id,
                    templates["photographer_notify"].format(
                        date=date_str,
                        time=time_str,
                        name=name,
                        phone=phone
                    )
                )
            except Exception as e:
                logger.error(f"Failed to notify photographer {photographer_id}: {e}")

        logger.info(f"Booking confirmed for user {user_id}: {date_str} {time_str}, type={shoot_type}")
    else:
        await callback.message.edit_text(templates["booking_cancelled"], reply_markup=None)
        logger.info(f"User {user_id} canceled the booking")

    await state.clear()
    await callback.answer()

from aiogram.filters import StateFilter

@router.message(Command("cancel"), StateFilter("*"))
@router.message(F.text.lower().in_(["отмена", "cancel"]), StateFilter("*"))
async def cancel_process(message: Message, state: FSMContext):
    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    dialog_msg_id = data.get("dialog_msg_id")
    
    if dialog_msg_id:
        try:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=dialog_msg_id,
                text=templates["booking_cancelled"]
            )
        except Exception:
            pass

    try:
        dummy = await message.answer('.', reply_markup=ReplyKeyboardRemove())
        await dummy.delete()
    except Exception:
        pass

    await state.clear()
    logger.info(f"User {message.from_user.id} canceled the current operation.")

# Admin Handlers
@router.message(Command("admin"))
async def admin_panel(message: Message, state: FSMContext):
    if message.from_user.id not in Config.ADMIN_IDS:
        return
        
    if not await check_admin_session(message.from_user.id):
        await message.answer(templates["admin_enter_password"])
        await state.set_state(AdminState.waiting_password)
        return

    await message.answer("⚙️ Панель администратора:", reply_markup=get_admin_keyboard())

@router.message(AdminState.waiting_password)
async def admin_login_password(message: Message, state: FSMContext):
    if message.from_user.id not in Config.ADMIN_IDS:
        return

    pw = message.text
    try:
        await message.delete()
    except Exception:
        pass

    if bcrypt.checkpw(pw.encode(), ADMIN_PASSWORD_HASH):
        logged_in_admins[message.from_user.id] = datetime.now()
        await message.answer(templates["admin_login_success"])
        await message.answer("⚙️ Панель администратора:", reply_markup=get_admin_keyboard())
        logger.info(f"Admin {message.from_user.id} logged in")
    else:
        await message.answer(templates["admin_login_fail"])
        logger.warning(f"Admin login failed for user {message.from_user.id}")

    await state.clear()

@router.callback_query(F.data.startswith("admin:"))
async def admin_actions(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id

    if user_id not in Config.ADMIN_IDS or not await check_admin_session(user_id):
        await callback.answer("❌ Доступ запрещен")
        return

    if action == "addslot":
        await callback.message.answer(templates["admin_add_slot_prompt"])
        await state.set_state(AdminState.adding_slot)
    elif action == "feedbacks":
        await show_feedbacks(callback.message, state)
    elif action == "delslot":
        await callback.message.answer(templates["admin_del_slot_prompt"])
        await state.set_state(AdminState.deleting_slot)
    elif action == "export":
        await export_bookings_command(callback.message)
    elif action == "templates":
        await list_templates(callback.message, state)
    elif action == "stats":
        await show_stats(callback.message)
    elif action == "photographers":
        await manage_photographers(callback.message, state)
    elif action == "discount":
        await manage_discounts(callback.message, state)
    elif action == "changepw":
        await change_password_start(callback.message, state)
    elif action == "logout":
        await callback.message.answer(
            templates["admin_logout_confirm"],
            reply_markup=get_logout_confirmation_keyboard()
        )

    await callback.answer()

async def manage_photographers(message: Message, state: FSMContext):
    photographers = await get_photographers()
    if photographers:
        photographer_list = "\n".join(
            f"{idx+1}. ID: {p[1]}, @{p[2]} ({p[3] or 'без специализации'})"
            for idx, p in enumerate(photographers)
        )
        text = templates["photographer_list"].format(list=photographer_list)
    else:
        text = "📸 Нет добавленных фотографов"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить фотографа", callback_data="photographer:add")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back")]
    ])
    
    await message.answer(text, reply_markup=keyboard)

@router.callback_query(F.data.startswith("photographer:"))
async def handle_photographer_actions(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split(":")[1]
    
    if action == "add":
        await callback.message.answer(templates["photographer_add_prompt"])
        await state.set_state(AdminState.adding_photographer)
    elif action == "back":
        await callback.message.answer("⚙️ Панель администратора:", reply_markup=get_admin_keyboard())
    
    await callback.answer()

@router.message(AdminState.adding_photographer)
async def add_photographer_handler(message: Message, state: FSMContext):
    try:
        parts = message.text.split()
        if len(parts) < 2:
            raise ValueError("Неверный формат")
            
        user_id = int(parts[0])
        username = parts[1].lstrip("@")
        specialties = " ".join(parts[2:]) if len(parts) > 2 else ""
        
        await add_photographer(user_id, username, specialties)
        await message.answer(templates["photographer_add_success"].format(username=username))
        logger.info(f"Added photographer: {user_id} @{username}")
    except ValueError as e:
        await message.answer("❌ Ошибка: неверный формат. Используйте: ID username [специализация]")
    except Exception as e:
        await message.answer("❌ Ошибка при добавлении фотографа")
        logger.error(f"Error adding photographer: {e}")
    
    await state.clear()

async def manage_discounts(message: Message, state: FSMContext):
    text = (f"🎁 Текущие настройки скидок:\n\n"
           f"Процент скидки: {Config.DISCOUNT_PERCENT}%\n"
           f"Минимальное количество отзывов: {Config.MIN_REVIEWS_FOR_DISCOUNT}\n\n"
           "Изменить настройки:")
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить процент", callback_data="discount:percent")],
        [InlineKeyboardButton(text="✏️ Изменить кол-во отзывов", callback_data="discount:reviews")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back")]
    ])
    
    await message.answer(text, reply_markup=keyboard)

@router.callback_query(F.data.startswith("discount:"))
async def handle_discount_actions(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split(":")[1]
    
    if action == "percent":
        await callback.message.answer("Введите новый процент скидки (0-100):")
        await state.set_state(AdminState.waiting_discount)
        await state.update_data(discount_type="percent")
    elif action == "reviews":
        await callback.message.answer("Введите новое минимальное количество отзывов:")
        await state.set_state(AdminState.waiting_discount)
        await state.update_data(discount_type="reviews")
    elif action == "back":
        await callback.message.answer("⚙️ Панель администратора:", reply_markup=get_admin_keyboard())
    
    await callback.answer()

@router.message(AdminState.waiting_discount)
async def set_discount_value(message: Message, state: FSMContext):
    data = await state.get_data()
    discount_type = data.get("discount_type")
    
    try:
        value = int(message.text)
        if discount_type == "percent" and (value < 0 or value > 100):
            raise ValueError("Процент должен быть от 0 до 100")
        elif discount_type == "reviews" and value < 1:
            raise ValueError("Количество отзывов должно быть положительным")
            
        # Update config
        if discount_type == "percent":
            Config.DISCOUNT_PERCENT = value
            await message.answer(f"✅ Процент скидки изменен на {value}%")
        else:
            Config.MIN_REVIEWS_FOR_DISCOUNT = value
            await message.answer(f"✅ Минимальное количество отзывов изменено на {value}")
            
        # Update .env file
        with open(".env", "r") as f:
            env_lines = f.readlines()
            
        with open(".env", "w") as f:
            for line in env_lines:
                if discount_type == "percent" and line.startswith("DISCOUNT_PERCENT="):
                    f.write(f"DISCOUNT_PERCENT={value}\n")
                elif discount_type == "reviews" and line.startswith("MIN_REVIEWS_FOR_DISCOUNT="):
                    f.write(f"MIN_REVIEWS_FOR_DISCOUNT={value}\n")
                else:
                    f.write(line)
                    
        logger.info(f"Discount {discount_type} changed to {value}")
    except ValueError as e:
        await message.answer(f"❌ Ошибка: {str(e)}")
    except Exception as e:
        await message.answer("❌ Ошибка при изменении настроек")
        logger.error(f"Error changing discount settings: {e}")
    
    await state.clear()

async def show_stats(message: Message):
    stats = await get_stats()
    await message.answer(
        templates["stats_text"].format(
            total=stats["total"],
            last_week=stats["last_week"],
            free_slots=stats["free_slots"],
            avg_rating=stats["avg_rating"]
        )
    )
async def show_feedbacks(message: Message, state: FSMContext, page: int = 0):
    async with aiosqlite.connect("bot.db") as db:
        cursor = await db.execute(
            "SELECT user_name, text, photo_id, rating, created_at FROM feedback ORDER BY created_at DESC"
        )
        all_feedbacks = await cursor.fetchall()

    if not all_feedbacks:
        await message.answer("📭 Отзывов пока нет.")
        return

    if page >= len(all_feedbacks):
        await message.answer("✅ Отзывов больше нет.")
        return

    user_name, text, photo_id, rating, created_at = all_feedbacks[page]

    caption = (
        f"👤 <b>{user_name}</b>\n"
        f"🗓 {created_at}\n"
        f"⭐ Рейтинг: {rating}/5\n\n"
        f"{text}"
    )

    nav_buttons = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="➡️ Следующий", callback_data="feedback:page:1")]
    ])

    if photo_id:
        await message.answer_photo(photo_id, caption=caption, reply_markup=nav_buttons)
    else:
        await message.answer(caption, reply_markup=nav_buttons)

    await state.update_data(feedback_page=page)

@router.callback_query(F.data.startswith("logout:"))
async def admin_logout_confirm(callback: CallbackQuery):
    action = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id

    if action == "yes":
        if user_id in logged_in_admins:
            del logged_in_admins[user_id]
        await callback.message.edit_text(templates["logout_success"])
        logger.info(f"Admin {user_id} logged out")
    else:
        await callback.message.edit_text(templates["logout_cancelled"])

    await callback.answer()

@router.callback_query(F.data.startswith("feedback:page:"))
async def paginate_feedbacks(callback: CallbackQuery, state: FSMContext):
    page_str = callback.data.split(":")[-1]
    try:
        page = int(page_str)
    except ValueError:
        await callback.answer("Ошибка страницы")
        return

    await callback.message.delete()
    await show_feedbacks(callback.message, state, page=page)
    await callback.answer()

@router.message(AdminState.adding_slot)
async def admin_addslot_save(message: Message, state: FSMContext):
    if message.from_user.id not in Config.ADMIN_IDS:
        return

    slot_text = message.text.strip()
    dt = parse_datetime_ru(slot_text)
    
    if not dt:
        await message.answer("❌ Неверный формат времени. " + templates["admin_add_slot_prompt"])
        return

    result = await add_slot(dt)
    rus_date = dt.strftime("%d.%m.%Y")
    rus_time = dt.strftime("%H:%M")

    if result:
        await message.answer(templates["admin_add_slot_success"].format(date=rus_date, time=rus_time))
        logger.info(f"Admin added new slot {rus_date} {rus_time}")
    else:
        await message.answer(templates["admin_add_slot_exists"])
        logger.info(f"Admin tried to add duplicate slot {rus_date} {rus_time}")

    await state.clear()

@router.message(AdminState.deleting_slot)
async def admin_delslot_delete(message: Message, state: FSMContext):
    if message.from_user.id not in Config.ADMIN_IDS:
        return

    slot_text = message.text.strip()
    dt = parse_datetime_ru(slot_text)
    
    if not dt:
        await message.answer("❌ Неверный формат. " + templates["admin_del_slot_prompt"])
        return

    result = await delete_slot(dt)
    rus_date = dt.strftime("%d.%m.%Y")
    rus_time = dt.strftime("%H:%M")

    if result == "success":
        await message.answer(templates["admin_del_slot_success"].format(date=rus_date, time=rus_time))
        logger.info(f"Admin deleted slot {rus_date} {rus_time}")
    elif result == "not_found":
        await message.answer(templates["admin_del_slot_not_found"])
    elif result == "booked":
        await message.answer(templates["admin_del_slot_booked"])

    await state.clear()

async def export_bookings_command(message: Message):
    async with aiosqlite.connect("bot.db") as db:
        cursor = await db.execute(
            """SELECT s.datetime, b.name, b.contact, b.shoot_type, b.created_at, p.username
               FROM bookings b
               JOIN slots s ON b.slot_id = s.id
               LEFT JOIN photographers p ON s.photographer_id = p.id
               ORDER BY s.datetime"""
        )
        rows = await cursor.fetchall()

    if not rows:
        await message.answer(templates["admin_export_no_data"])
        return

    # Создание Excel-файла
    wb = Workbook()
    ws = wb.active
    ws.title = "Записи"

    headers = ["Дата", "Время", "Имя", "Телефон", "Тип съёмки", "Дата записи", "Фотограф"]
    ws.append(headers)

    for dt_text, name, contact, shoot_type, created_at, photographer in rows:
        dt_obj = datetime.strptime(dt_text, "%Y-%m-%d %H:%M:%S")
        created_obj = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
        photographer = photographer or "Не назначен"
        ws.append([
            dt_obj.strftime("%d.%m.%Y"),
            dt_obj.strftime("%H:%M"),
            name,
            contact,
            shoot_type,
            created_obj.strftime("%d.%m.%Y %H:%M"),
            photographer
        ])

    filename = f"bookings_export_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    try:
        wb.save(filename)
        await message.answer_document(
            FSInputFile(filename),
            caption=templates["admin_export_success"].format(count=len(rows))
        )
        logger.info(f"Admin exported bookings to Excel: {filename}")
        await asyncio.sleep(1)
        os.remove(filename)
    except Exception as e:
        logger.error(f"Export to Excel failed: {e}")
        await message.answer("❌ Ошибка при экспорте Excel-файла.")

async def list_templates(message: Message, state: FSMContext):
    keys_list = ", ".join(templates.keys())
    await message.answer(templates["admin_template_list"].format(keys=keys_list))
    await state.set_state(AdminState.waiting_template_key)

@router.message(AdminState.waiting_template_key)
async def admin_template_key(message: Message, state: FSMContext):
    if message.from_user.id not in Config.ADMIN_IDS:
        return

    key = message.text.strip()
    
    if key not in templates:
        await message.answer(templates["admin_template_invalid"].format(key=key))
        return

    await state.update_data(edit_template_key=key)
    await message.answer(templates["admin_template_prompt"].format(key=key))
    await state.set_state(AdminState.waiting_template_text)

@router.message(AdminState.waiting_template_text)
async def admin_template_text(message: Message, state: FSMContext):
    if message.from_user.id not in Config.ADMIN_IDS:
        return

    new_text = message.text
    data = await state.get_data()
    key = data.get("edit_template_key")
    
    if not key:
        await message.answer("⚠️ Произошла ошибка: не выбран шаблон для редактирования.")
        await state.clear()
        return

    templates[key] = new_text
    
    try:
        with open("templates.json", "w", encoding="utf-8") as f:
            json.dump(templates, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to save templates.json: {e}")

    await message.answer(templates["admin_template_updated"].format(key=key))
    logger.info(f"Admin updated template '{key}'")
    await state.clear()

async def change_password_start(message: Message, state: FSMContext):
    await message.answer("🔐 Введите новый пароль (минимум 8 символов, заглавные, цифры, спецсимволы):")
    await state.set_state(AdminState.changing_password)

@router.message(AdminState.changing_password)
async def admin_changepassword_set(message: Message, state: FSMContext):
    new_password = message.text.strip()
    
    # Validate password
    if len(new_password) < 8:
        await message.answer("❌ Пароль должен содержать минимум 8 символов")
        return
    if not re.search(r"[A-Z]", new_password):
        await message.answer("❌ Пароль должен содержать хотя бы одну заглавную букву")
        return
    if not re.search(r"\d", new_password):
        await message.answer("❌ Пароль должен содержать хотя бы одну цифру")
        return
    if not re.search(r"[!@#$%^&*(),.?\":{}|<>]", new_password):
        await message.answer("❌ Пароль должен содержать хотя бы один спецсимвол")
        return

    # Update config
    Config.ADMIN_PASSWORD = new_password
    
    # Generate new hash and save to .env
    global ADMIN_PASSWORD_HASH
    salt = bcrypt.gensalt()
    ADMIN_PASSWORD_HASH = bcrypt.hashpw(new_password.encode(), salt)
    
    # Update .env file
    with open(".env", "r") as f:
        env_lines = f.readlines()
        
    with open(".env", "w") as f:
        for line in env_lines:
            if line.startswith("ADMIN_PASSWORD_HASH="):
                f.write(f"ADMIN_PASSWORD_HASH={ADMIN_PASSWORD_HASH.decode()}\n")
            else:
                f.write(line)

    await message.answer("✅ Пароль успешно изменен!")
    logger.info("Admin password changed")
    await state.clear()

# Background tasks
async def reminder_task():
    while True:
        try:
            now = datetime.now()
            next_24h = now + timedelta(hours=24)

            async with aiosqlite.connect("bot.db") as db:
                cursor = await db.execute(
                    """SELECT b.id, b.user_id, s.datetime, b.name, b.contact
                    FROM bookings b
                    JOIN slots s ON b.slot_id = s.id
                    WHERE b.reminder_sent = 0
                    AND s.datetime BETWEEN ? AND ?""",
                    (now.strftime("%Y-%m-%d %H:%M:%S"),
                     next_24h.strftime("%Y-%m-%d %H:%M:%S"))
                )
                bookings = await cursor.fetchall()

                for b_id, user_id, dt_text, name, contact in bookings:
                    appt_dt = datetime.strptime(dt_text, "%Y-%m-%d %H:%M:%S")
                    remind_time = appt_dt.strftime("%H:%M")

                    # Отправляем клиенту
                    try:
                        await bot.send_message(
                            user_id,
                            templates["reminder_client"].format(time=remind_time)
                        )
                        sent_client = True
                    except Exception as e:
                        logger.error(f"Reminder to user {user_id} failed: {e}")
                        sent_client = False

                    # Отправляем всем администраторам
                    sent_admin = False
                    for admin_id in Config.ADMIN_IDS:
                        try:
                            await bot.send_message(
                                admin_id,
                                templates["reminder_admin"].format(time=remind_time, name=name, phone=contact)
                            )
                            sent_admin = True
                        except Exception as e:
                            logger.error(f"Reminder to admin {admin_id} failed: {e}")

                    # Обновляем флаг, если хотя бы один отправлен
                    if sent_client or sent_admin:
                        await db.execute(
                            "UPDATE bookings SET reminder_sent = 1 WHERE id = ?",
                            (b_id,)
                        )
                        await db.commit()
                        logger.info(f"Sent reminder for booking {b_id}")

            # 💬 Просим оставить отзыв через сутки после съёмки
            async with aiosqlite.connect("bot.db") as db:
                cursor = await db.execute(
                    """
                    SELECT b.user_id, s.datetime, b.name, b.id
                    FROM bookings b
                    JOIN slots s ON b.slot_id = s.id
                    WHERE datetime(s.datetime) <= datetime('now', '-1 day')
                      AND b.review_requested = 0
                    """
                )
                rows = await cursor.fetchall()

                for user_id, dt_text, name, booking_id in rows:
                    try:
                        await bot.send_message(
                            user_id,
                            "🌟 Как прошла ваша фотосессия?\n"
                            "Пожалуйста, поделитесь впечатлением — отправьте команду /feedback 💬"
                        )
                        await db.execute(
                            "UPDATE bookings SET review_requested = 1 WHERE id = ?",
                            (booking_id,)
                        )
                        await db.commit()
                        logger.info(f"Review prompt sent to user {user_id}")
                    except Exception as e:
                        logger.error(f"Failed to send review prompt to {user_id}: {e}")

            await asyncio.sleep(600)  # каждые 10 минут

        except Exception as e:
            logger.error(f"Reminder task failed: {str(e)}")
            await asyncio.sleep(60)


async def session_cleanup_task():
    while True:
        try:
            now = datetime.now()
            expired = []
            
            for admin_id, last_activity in logged_in_admins.items():
                if (now - last_activity) > SESSION_TIMEOUT:
                    expired.append(admin_id)

            for admin_id in expired:
                del logged_in_admins[admin_id]
                logger.info(f"Admin session expired: {admin_id}")

            await asyncio.sleep(300)  # Check every 5 minutes
        except Exception as e:
            logger.error(f"Session cleanup failed: {str(e)}")
            await asyncio.sleep(60)
# Error handler
async def error_handler(event: types.ErrorEvent):
    logger.error(f"Unhandled exception: {str(event.exception)}")
    if isinstance(event.update, types.Message):
        await event.update.answer("⚠️ Произошла ошибка. Пожалуйста, попробуйте позже.")

# ✅ Новый on_startup
async def on_startup(dispatcher: Dispatcher, bot: Bot):
    asyncio.create_task(reminder_task())
    asyncio.create_task(session_cleanup_task())
    logger.info("✅ Background tasks started")

# ✅ Новый main
async def main():
    try:
        Config.validate_config()
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        return

    await init_db()
    dp.startup.register(on_startup)
    dp.errors.register(error_handler)

    logger.info("Bot starting...")
    await dp.start_polling(bot)

# ⏱ Запуск
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")

