import logging
import asyncio
import datetime
import json
import os
import aiosqlite
import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, CallbackQuery
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.filters import CommandStart
from aiogram.fsm.state import StatesGroup, State
from aiogram_calendar import SimpleCalendar, SimpleCalendarCallback
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
load_dotenv()
TOKEN = os.getenv("TOKEN")
NOTIFICATION_CHAT_ID = os.getenv("NOTIFICATION_CHAT_ID")
if not TOKEN or not NOTIFICATION_CHAT_ID:
    raise ValueError("Токен или NOTIFICATION_CHAT_ID не найдены в .env!")
logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()
DB_PATH = "bookings.db"
# --- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ И ЗАГРУЗКА ---
EQUIPMENT_CACHE = {}
LAST_JSON_CONTENT = ""
# Группы связанного оборудования (взаимоисключающие)
EXCLUSION_GROUPS = [
    {"Интеркомы 6шт", "Интеркомы 4шт", "Интеркомы 2шт"}
]
def is_item_blocked_by_exclusion(item_name: str, booked_db: dict, cart: dict) -> bool:
    """Интеллектуальная проверка: не занята ли эта позиция через другую комплектацию."""
    for group in EXCLUSION_GROUPS:
        if item_name in group:
            for other_item in group:
                if other_item != item_name:
                    if booked_db.get(other_item, 0) > 0 or cart.get(other_item, 0) > 0:
                        return True
    return False
async def load_equipment():
    global EQUIPMENT_CACHE, LAST_JSON_CONTENT
    github_url = os.getenv("GITHUB_JSON_URL")
    
    if github_url:
        timestamp = int(datetime.datetime.now().timestamp())
        no_cache_url = f"{github_url}?t={timestamp}"
        headers = {
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache"
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(no_cache_url, headers=headers) as response:
                    if response.status == 200:
                        text_data = await response.text()
                        if text_data != LAST_JSON_CONTENT:
                            try:
                                new_data = json.loads(text_data)
                                EQUIPMENT_CACHE.clear()
                                EQUIPMENT_CACHE.update(new_data)
                                LAST_JSON_CONTENT = text_data
                                logging.info("✅ БАЗА ОБНОВЛЕНА: Данные с GitHub загружены.")
                            except json.JSONDecodeError as e:
                                logging.error(f"❌ ОШИБКА JSON: {e}")
                    else:
                        logging.error(f"❌ Ошибка ссылки (код {response.status}).")
        except Exception as e:
            logging.error(f"❌ Сетевая ошибка: {e}")
    else:
        try:
            with open("equipment.json", "r", encoding="utf-8") as f:
                EQUIPMENT_CACHE.clear()
                EQUIPMENT_CACHE.update(json.load(f))
        except Exception as e:
            logging.error(f"❌ Ошибка чтения локального файла: {e}")
# --- СОСТОЯНИЯ (FSM) ---
class BookingState(StatesGroup):
    choosing_start_date = State()
    choosing_end_date = State()
    choosing_category = State()
    choosing_items = State()
    confirmation = State()
    removing_items = State()
class DeletingBookingState(StatesGroup):
    choosing_booking_to_delete = State()
# --- КЛАВИАТУРЫ ---
main_menu_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Забронировать оборудование")],
        [KeyboardButton(text="Занятые даты")],
        [KeyboardButton(text="Мои бронирования"), KeyboardButton(text="Все бронирования")],
        [KeyboardButton(text="Удалить бронь"), KeyboardButton(text="Архив бронирований")]
    ],
    resize_keyboard=True
)
async def get_items_keyboard(category: str, start_date_str: str, end_date_str: str, current_cart: dict) -> ReplyKeyboardMarkup:
    """Клавиатура с остатками внутри категории."""
    booked_items = await get_max_booked_in_range(start_date_str, end_date_str)
    keyboard_buttons = []
    
    for item, details in EQUIPMENT_CACHE[category].items():
        total_available = details[0]
        already_booked = booked_items.get(item, 0)
        in_cart = current_cart.get(item, 0)
        available_now = total_available - already_booked - in_cart
        
        if is_item_blocked_by_exclusion(item, booked_items, current_cart):
            available_now = 0
            
        if available_now > 0:
            keyboard_buttons.append([KeyboardButton(text=f"{item} ({available_now} шт.)")])
        else:
            keyboard_buttons.append([KeyboardButton(text=f"❌ {item} (Временно нет)")])
            
    bottom_row = [KeyboardButton(text="Назад")]
    if current_cart:
        bottom_row.append(KeyboardButton(text="Удалить позицию"))
    bottom_row.append(KeyboardButton(text="Готово"))
    
    keyboard_buttons.append(bottom_row)
    return ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True)
def get_remove_keyboard(cart: dict) -> ReplyKeyboardMarkup:
    """Клавиатура для режима удаления с тремя основными кнопками."""
    keyboard_buttons = [[KeyboardButton(text=f"{item} ({qty} шт.)")] for item, qty in cart.items()]
    keyboard_buttons.append([
        KeyboardButton(text="Добавить еще"),
        KeyboardButton(text="Отмена"),
        KeyboardButton(text="Готово")
    ])
    return ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True)
# --- ФУНКЦИИ ДЛЯ ЖИВОГО ИНТЕРФЕЙСА ---
def generate_receipt(cart: dict) -> tuple[str, int]:
    base_total = 0
    lines = []
    for item, qty in cart.items():
        price = 0
        for cat_items in EQUIPMENT_CACHE.values():
            if item in cat_items:
                price = cat_items[item][1]
                break
        cost = price * qty
        base_total += cost
        lines.append(f"▫️ {item} x{qty} ({cost} руб./день)")
    return "\n".join(lines), base_total
def get_live_text(cart: dict, days: int, prompt: str, is_final: bool = False) -> str:
    if not cart:
        return f"🛒 *Смета пока пуста*\n〰️〰️〰️〰️〰️〰️〰️\n{prompt}"
    
    receipt_text, base_total = generate_receipt(cart)
    final_total = base_total * days
    title = "Смета" if is_final else "Предварительная смета"
    
    return (f"🛒 *{title}:*\n{receipt_text}\n\n"
            f"💰 *Итого: {final_total} руб.* ({days} дн.)\n"
            f"〰️〰️〰️〰️〰️〰️〰️\n"
            f"{prompt}")
async def refresh_menu(message: Message, state: FSMContext, text: str, keyboard: ReplyKeyboardMarkup):
    """Обновляет интерфейс без прыжков клавиатуры."""
    data = await state.get_data()
    old_msg_id = data.get("menu_msg_id")
    
    new_msg = await bot.send_message(
        chat_id=message.chat.id, 
        text=text, 
        reply_markup=keyboard, 
        parse_mode="Markdown"
    )
    
    try:
        if old_msg_id:
            await bot.delete_message(chat_id=message.chat.id, message_id=old_msg_id)
    except Exception:
        pass
        
    try:
        if isinstance(message, Message):
            await message.delete()
    except Exception:
        pass
    await state.update_data(menu_msg_id=new_msg.message_id)
# --- БАЗА ДАННЫХ И ХРАНЕНИЕ ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS bookings (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            user_id INTEGER,
                            username TEXT,
                            start_date DATE,
                            end_date DATE,
                            days_count INTEGER,
                            items_json TEXT,
                            total_price INTEGER)''')
        
        await db.execute('''CREATE TABLE IF NOT EXISTS archive_bookings (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            user_id INTEGER,
                            username TEXT,
                            start_date DATE,
                            end_date DATE,
                            days_count INTEGER,
                            items_json TEXT,
                            total_price INTEGER)''')
        await db.commit()
async def get_max_booked_in_range(start_date_str: str, end_date_str: str) -> dict:
    start_date = datetime.datetime.strptime(start_date_str, "%Y-%m-%d").date()
    end_date = datetime.datetime.strptime(end_date_str, "%Y-%m-%d").date()
    delta = end_date - start_date
    
    max_booked = {}
    async with aiosqlite.connect(DB_PATH) as db:
        for i in range(delta.days + 1):
            current_day = start_date + datetime.timedelta(days=i)
            current_day_str = current_day.strftime("%Y-%m-%d")
            
            booked_today = {}
            query = "SELECT items_json FROM bookings WHERE start_date <= ? AND end_date >= ?"
            async with db.execute(query, (current_day_str, current_day_str)) as cursor:
                async for row in cursor:
                    items = json.loads(row[0])
                    for item_name, qty in items.items():
                        booked_today[item_name] = booked_today.get(item_name, 0) + qty
            
            for item_name, qty in booked_today.items():
                max_booked[item_name] = max(max_booked.get(item_name, 0), qty)
    return max_booked
async def archive_past_bookings():
    current_date = datetime.date.today().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''INSERT INTO archive_bookings (user_id, username, start_date, end_date, days_count, items_json, total_price) 
                            SELECT user_id, username, start_date, end_date, days_count, items_json, total_price FROM bookings WHERE end_date < ?''', (current_date,))
        await db.execute("DELETE FROM bookings WHERE end_date < ?", (current_date,))
        await db.commit()
async def send_notification(message: str):
    try:
        await bot.send_message(chat_id=NOTIFICATION_CHAT_ID, text=message, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Ошибка уведомления: {e}")
# --- ОБРАБОТЧИКИ БРОНИРОВАНИЯ ---
@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Привет! Я бот для бронирования оборудования.", reply_markup=main_menu_keyboard)
@dp.message(F.text == "Забронировать оборудование")
async def start_booking(message: Message, state: FSMContext):
    await load_equipment() # Мгновенная проверка GH при старте бронирования
    await state.set_state(BookingState.choosing_start_date)
    await message.answer("Выберите дату НАЧАЛА бронирования:", reply_markup=await SimpleCalendar().start_calendar(
        year=datetime.datetime.now().year, month=datetime.datetime.now().month
    ))
@dp.callback_query(SimpleCalendarCallback.filter())
async def process_calendar(callback_query: CallbackQuery, callback_data: dict, state: FSMContext):
    current_state = await state.get_state()
    selected, date = await SimpleCalendar().process_selection(callback_query, callback_data)
    
    if selected:
        selected_date = date.date()
        
        if current_state == BookingState.choosing_start_date.state:
            if selected_date < datetime.date.today():
                await callback_query.message.answer(
                    "❌ Нельзя выбрать прошедшую дату. Пожалуйста, выберите актуальную дату начала:",
                    reply_markup=await SimpleCalendar().start_calendar(year=datetime.datetime.now().year, month=datetime.datetime.now().month)
                )
                return
                
            start_date_str = selected_date.strftime("%Y-%m-%d")
            await state.update_data(start_date=start_date_str, items={})
            await state.set_state(BookingState.choosing_end_date)
            
            await bot.edit_message_text(
                text=f"Начало аренды: {start_date_str}\n\nТеперь выберите дату ОКОНЧАНИЯ бронирования:",
                chat_id=callback_query.message.chat.id,
                message_id=callback_query.message.message_id,
                reply_markup=await SimpleCalendar().start_calendar(year=selected_date.year, month=selected_date.month)
            )
            
        elif current_state == BookingState.choosing_end_date.state:
            data = await state.get_data()
            start_date_obj = datetime.datetime.strptime(data["start_date"], "%Y-%m-%d").date()
            
            if selected_date < start_date_obj:
                await bot.edit_message_text(
                    text="❌ Дата окончания не может быть раньше даты начала! Выберите корректную дату окончания:",
                    chat_id=callback_query.message.chat.id,
                    message_id=callback_query.message.message_id,
                    reply_markup=await SimpleCalendar().start_calendar(year=start_date_obj.year, month=start_date_obj.month)
                )
                return
                
            end_date_str = selected_date.strftime("%Y-%m-%d")
            days_count = (selected_date - start_date_obj).days + 1
            await state.update_data(end_date=end_date_str, days_count=days_count)
            
            await state.set_state(BookingState.choosing_category)
            keyboard = ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text=cat)] for cat in EQUIPMENT_CACHE.keys()] +
                         [[KeyboardButton(text="Отмена")]],
                resize_keyboard=True
            )
            text = get_live_text(data.get("items", {}), days_count, "📂 Выберите категорию оборудования:")
            
            await bot.delete_message(chat_id=callback_query.message.chat.id, message_id=callback_query.message.message_id)
            new_msg = await bot.send_message(chat_id=callback_query.message.chat.id, text=text, reply_markup=keyboard, parse_mode="Markdown")
            await state.update_data(menu_msg_id=new_msg.message_id)
@dp.message(BookingState.choosing_category)
async def choose_category(message: Message, state: FSMContext):
    data = await state.get_data()
    cart = data.get("items", {})
    days = data.get("days_count", 1)
    if message.text == "Отмена":
        await state.clear()
        try:
             old_id = data.get("menu_msg_id")
             if old_id: await bot.delete_message(message.chat.id, old_id)
             await message.delete()
        except: pass
        await message.answer("Бронирование отменено.", reply_markup=main_menu_keyboard)
        
    elif message.text in EQUIPMENT_CACHE:
        await state.update_data(category=message.text)
        keyboard = await get_items_keyboard(message.text, data["start_date"], data["end_date"], cart)
        text = get_live_text(cart, days, f"📦 Раздел: {message.text}")
        
        await state.set_state(BookingState.choosing_items)
        await refresh_menu(message, state, text, keyboard)
        
    elif message.text == "Готово":
        await show_confirmation(message, state)
        
    else:
        try: await message.delete()
        except: pass
@dp.message(BookingState.choosing_items)
async def choose_items(message: Message, state: FSMContext):
    data = await state.get_data()
    category = data.get("category")
    start_str, end_str = data["start_date"], data["end_date"]
    cart = data.get("items", {})
    days = data.get("days_count", 1)
    if message.text == "Готово":
        if not cart:
            text = get_live_text(cart, days, "❗️ Сначала выберите хотя бы одно оборудование.")
            keyboard = await get_items_keyboard(category, start_str, end_str, cart)
            await refresh_menu(message, state, text, keyboard)
        else:
            await show_confirmation(message, state)
        return
        
    elif message.text == "Назад":
        await state.set_state(BookingState.choosing_category)
        keyboard = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=cat)] for cat in EQUIPMENT_CACHE.keys()] +
                     [[KeyboardButton(text="Отмена")]],
            resize_keyboard=True
        )
        text = get_live_text(cart, days, "📂 Выберите категорию оборудования:")
        await refresh_menu(message, state, text, keyboard)
        return
        
    elif message.text == "Удалить позицию":
        if not cart:
            try: await message.delete()
            except: pass
            return
            
        await state.set_state(BookingState.removing_items)
        text = get_live_text(cart, days, "➖ Нажмите на позицию для удаления.")
        await refresh_menu(message, state, text, get_remove_keyboard(cart))
        return
    item_name = message.text.rsplit(" (", 1)[0]
    if item_name.startswith("❌ "):
        text = get_live_text(cart, days, "❗️ Этого оборудования больше нет в наличии.")
        keyboard = await get_items_keyboard(category, start_str, end_str, cart)
        await refresh_menu(message, state, text, keyboard)
        return
    if item_name in EQUIPMENT_CACHE[category]:
        booked_db = await get_max_booked_in_range(start_str, end_str)
        
        # Интеллектуальная проверка перед добавлением
        if is_item_blocked_by_exclusion(item_name, booked_db, cart):
            keyboard = await get_items_keyboard(category, start_str, end_str, cart)
            text = get_live_text(cart, days, "❗️ Это оборудование заблокировано (выбрана другая комплектация).")
            await refresh_menu(message, state, text, keyboard)
            return
        total_avail = EQUIPMENT_CACHE[category][item_name][0]
        currently_avail = total_avail - booked_db.get(item_name, 0) - cart.get(item_name, 0)
        
        if currently_avail > 0:
            cart[item_name] = cart.get(item_name, 0) + 1
            await state.update_data(items=cart)
            keyboard = await get_items_keyboard(category, start_str, end_str, cart)
            text = get_live_text(cart, days, "📂 Продолжайте выбор или нажмите 'Готово'.")
            await refresh_menu(message, state, text, keyboard)
        else:
            keyboard = await get_items_keyboard(category, start_str, end_str, cart)
            text = get_live_text(cart, days, "❗️ Лимит достигнут, больше нет в наличии.")
            await refresh_menu(message, state, text, keyboard)
    else:
        try: await message.delete()
        except: pass
async def show_confirmation(message: Message, state: FSMContext):
    data = await state.get_data()
    cart = data.get("items", {})
    days = data.get("days_count", 1)
    
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Подтвердить бронь")],
            [KeyboardButton(text="Добавить еще"), KeyboardButton(text="Удалить из списка")],
            [KeyboardButton(text="Отменить смету")]
        ], resize_keyboard=True
    )
    
    if data['start_date'] == data['end_date']:
        period_str = f"{data['start_date']} ({days} дн.)"
    else:
        period_str = f"с {data['start_date']} по {data['end_date']} ({days} дн.)"
    
    text = get_live_text(cart, days, f"📅 Выбранный период: {period_str}\n\nВнимательно проверьте смету и подтвердите бронь.", is_final=True)
    await state.set_state(BookingState.confirmation)
    await refresh_menu(message, state, text, keyboard)
@dp.message(BookingState.confirmation)
async def handle_confirmation(message: Message, state: FSMContext):
    data = await state.get_data()
    cart = data.get("items", {})
    days = data.get("days_count", 1)
    if message.text == "Подтвердить бронь":
        await confirm_booking(message, state)
        
    elif message.text == "Добавить еще":
        await state.set_state(BookingState.choosing_category)
        keyboard = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=cat)] for cat in EQUIPMENT_CACHE.keys()] +
                     [[KeyboardButton(text="Отмена")]],
            resize_keyboard=True
        )
        text = get_live_text(cart, days, "📂 Выберите категорию оборудования:")
        await refresh_menu(message, state, text, keyboard)
        
    elif message.text == "Удалить из списка":
        if not cart:
            try: await message.delete()
            except: pass
            return
            
        await state.set_state(BookingState.removing_items)
        text = get_live_text(cart, days, "➖ Нажмите на позицию для удаления.")
        await refresh_menu(message, state, text, get_remove_keyboard(cart))
                             
    elif message.text == "Отменить смету":
        await state.clear()
        try:
             old_id = data.get("menu_msg_id")
             if old_id: await bot.delete_message(message.chat.id, old_id)
             await message.delete()
        except: pass
        await message.answer("Смета аннулирована 🗑", reply_markup=main_menu_keyboard)
    else:
        try: await message.delete()
        except: pass
@dp.message(BookingState.removing_items)
async def remove_items(message: Message, state: FSMContext):
    data = await state.get_data()
    cart = data.get("items", {})
    days = data.get("days_count", 1)
    if message.text == "Готово":
        if not cart:
            text = get_live_text(cart, days, "❗️ Смета пуста. Выберите категорию:")
            await state.set_state(BookingState.choosing_category)
            keyboard = ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text=cat)] for cat in EQUIPMENT_CACHE.keys()] +
                         [[KeyboardButton(text="Отмена")]],
                resize_keyboard=True
            )
            await refresh_menu(message, state, text, keyboard)
        else:
            await show_confirmation(message, state)
        return
        
    elif message.text == "Добавить еще":
        await state.set_state(BookingState.choosing_category)
        keyboard = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=cat)] for cat in EQUIPMENT_CACHE.keys()] +
                     [[KeyboardButton(text="Отмена")]],
            resize_keyboard=True
        )
        text = get_live_text(cart, days, "📂 Выберите категорию оборудования:")
        await refresh_menu(message, state, text, keyboard)
        return
        
    elif message.text == "Отмена":
        await state.clear()
        try:
             old_id = data.get("menu_msg_id")
             if old_id: await bot.delete_message(message.chat.id, old_id)
             await message.delete()
        except: pass
        await message.answer("Смета аннулирована 🗑", reply_markup=main_menu_keyboard)
        return
    item_name = message.text.rsplit(" (", 1)[0]
    
    if item_name in cart:
        if cart[item_name] > 1:
            cart[item_name] -= 1
        else:
            del cart[item_name]
            
        await state.update_data(items=cart)
        
        if not cart:
            await state.set_state(BookingState.choosing_category)
            keyboard = ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text=cat)] for cat in EQUIPMENT_CACHE.keys()] +
                         [[KeyboardButton(text="Отмена")]],
                resize_keyboard=True
            )
            text = get_live_text(cart, days, "Корзина пуста. 📂 Выберите категорию оборудования:")
            await refresh_menu(message, state, text, keyboard)
            return
            
        text = get_live_text(cart, days, "➖ Нажмите на позицию для удаления.")
        await refresh_menu(message, state, text, get_remove_keyboard(cart))
    else:
        try: await message.delete()
        except: pass
async def confirm_booking(message: Message, state: FSMContext):
    data = await state.get_data()
    cart = data.get("items", {})
    
    try:
        old_id = data.get("menu_msg_id")
        if old_id: await bot.delete_message(message.chat.id, old_id)
        await message.delete()
    except: pass
        
    if not cart:
        await message.answer("Корзина пуста. Бронь не создана.", reply_markup=main_menu_keyboard)
        await state.clear()
        return
    days = data.get("days_count", 1)
    receipt_text, base_total = generate_receipt(cart)
    final_total = base_total * days
    
    start_str, end_str = data["start_date"], data["end_date"]
    items_json = json.dumps(cart, ensure_ascii=False)
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO bookings (user_id, username, start_date, end_date, days_count, items_json, total_price) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (message.from_user.id, message.from_user.username, start_str, end_str, days, items_json, final_total))
        await db.commit()
    period_str = f"{start_str}" if start_str == end_str else f"с {start_str} по {end_str}"
    await message.answer(f"✅ Успешно забронировано {period_str}!\n\n{receipt_text}\n\nСумма: {final_total} руб.", reply_markup=main_menu_keyboard)
    await state.clear()
    
    await send_notification(
        f"📢 *Новое бронирование!*\n\n📅 *Период:* {period_str} ({days} дн.)\n"
        f"👤 *Пользователь:* @{message.from_user.username}\n\n"
        f"📦 *Оборудование:*\n{receipt_text}\n\n💵 *Итого:* {final_total} руб."
    )
# --- ПРОСМОТР И УДАЛЕНИЕ БРОНЕЙ ---
@dp.message(F.text == "Занятые даты")
async def show_booked_dates(message: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT start_date, end_date FROM bookings ORDER BY start_date") as cursor:
            dates = await cursor.fetchall()
            
    if dates:
        msg = "📅 Занятые периоды:\n"
        for start, end in dates:
            msg += f"• {start}\n" if start == end else f"• с {start} по {end}\n"
        await message.answer(msg)
    else:
        await message.answer("Нет занятых дат.")
@dp.message(F.text.in_({"Мои бронирования", "Все бронирования", "Архив бронирований"}))
async def text_reports(message: Message):
    is_archive = message.text == "Архив бронирований"
    is_my = message.text in ("Мои бронирования", "Архив бронирований")
    table = "archive_bookings" if is_archive else "bookings"
    
    query = f"SELECT username, start_date, end_date, days_count, items_json, total_price FROM {table} "
    params = ()
    if is_my:
        query += "WHERE user_id = ? ORDER BY start_date"
        params = (message.from_user.id,)
    else:
        query += "ORDER BY start_date"
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(query, params) as cursor:
            bookings = await cursor.fetchall()
    if not bookings:
        await message.answer("Список пуст.")
        return
    report = f"📋 *{message.text}:*\n\n"
    for b in bookings:
        items = json.loads(b[4])
        items_str = ", ".join([f"{k} (x{v})" for k, v in items.items()])
        date_str = f"📅 {b[1]} ({b[3]} дн.)" if b[1] == b[2] else f"📅 с {b[1]} по {b[2]} ({b[3]} дн.)"
        report += f"👤 @{b[0]}\n{date_str}\n📦 {items_str}\n💵 {b[5]} руб.\n—\n"
        
    await message.answer(report, parse_mode="Markdown")
@dp.message(F.text == "Удалить бронь")
async def start_deleting(message: Message, state: FSMContext):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, start_date, end_date, items_json FROM bookings WHERE user_id = ?", (message.from_user.id,)) as cursor:
            bookings = await cursor.fetchall()
            
    if not bookings:
        await message.answer("У вас нет активных бронирований.")
        return
        
    builder = InlineKeyboardBuilder()
    for b in bookings:
        b_id, start_date, end_date, items_json = b
        items = json.loads(items_json)
        short_names = list(items.keys())[:2]
        period = start_date if start_date == end_date else f"{start_date}—{end_date}"
        title = f"{period} | {', '.join(short_names)}..."
        builder.button(text=title, callback_data=f"del_{b_id}")
        
    builder.adjust(1)
    await message.answer("Выберите запись для отмены:", reply_markup=builder.as_markup())
    await state.set_state(DeletingBookingState.choosing_booking_to_delete)
@dp.callback_query(DeletingBookingState.choosing_booking_to_delete, F.data.startswith("del_"))
async def process_delete(callback: CallbackQuery, state: FSMContext):
    b_id = int(callback.data.split("_")[1])
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT start_date, end_date, items_json FROM bookings WHERE id = ? AND user_id = ?", (b_id, callback.from_user.id)) as cursor:
            row = await cursor.fetchone()
            
        if not row:
            await callback.message.answer("Бронь не найдена.")
            return
            
        await db.execute("DELETE FROM bookings WHERE id = ?", (b_id,))
        await db.commit()
    
    items = json.loads(row[2])
    items_str = "\n".join([f"▫️ {k} x{v}" for k, v in items.items()])
    period_str = f"{row[0]}" if row[0] == row[1] else f"{row[0]} — {row[1]}"
    
    await callback.message.answer("Бронирование удалено ✅", reply_markup=main_menu_keyboard)
    try: await callback.message.delete()
    except: pass
    await state.clear()
    
    await send_notification(
        f"❌ *Бронь отменена!*\n\n📅 Период: {period_str}\n👤 @{callback.from_user.username}\nОсвобождено:\n{items_str}"
    )
# --- ЗАПУСК БОТА ---
async def on_startup():
    logging.info("Инициализация БД и загрузка конфигурации...")
    await load_equipment()
    await init_db()
    
    scheduler.add_job(load_equipment, 'interval', hours=1)
    scheduler.add_job(archive_past_bookings, 'cron', hour=0, minute=0)
    scheduler.start()
async def on_shutdown():
    logging.info("Остановка бота...")
    scheduler.shutdown()
async def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    await dp.start_polling(bot)
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот выключен.")
