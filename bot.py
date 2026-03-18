import logging
import asyncio
import datetime
import json
import os
import aiosqlite
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
# Загружаем переменные из .env
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
# Глобальная переменная для хранения оборудования (загружается один раз при старте)
EQUIPMENT_CACHE = {}
def load_equipment():
    global EQUIPMENT_CACHE
    with open("equipment.json", "r", encoding="utf-8") as f:
        EQUIPMENT_CACHE = json.load(f)
# Состояния FSM
class BookingState(StatesGroup):
    choosing_date = State()
    choosing_category = State()
    choosing_items = State()
    confirmation = State()
    removing_items = State()
class DeletingBookingState(StatesGroup):
    choosing_booking_to_delete = State()
# Главное меню
main_menu_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Забронировать оборудование")],
        [KeyboardButton(text="Занятые даты")],
        [KeyboardButton(text="Мои бронирования"), KeyboardButton(text="Все бронирования")],
        [KeyboardButton(text="Удалить бронь"), KeyboardButton(text="Архив бронирований")]
    ],
    resize_keyboard=True
)
# --- БАЗА ДАННЫХ И ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS bookings (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            user_id INTEGER,
                            username TEXT,
                            date DATE,
                            items_json TEXT,
                            total_price INTEGER)''')
        
        await db.execute('''CREATE TABLE IF NOT EXISTS archive_bookings (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            user_id INTEGER,
                            username TEXT,
                            date DATE,
                            items_json TEXT,
                            total_price INTEGER)''')
        await db.commit()
async def get_booked_items(date_str: str) -> dict:
    """Возвращает словарь забронированного оборудования на указанную дату."""
    booked = {}
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT items_json FROM bookings WHERE date = ?", (date_str,)) as cursor:
            async for row in cursor:
                items = json.loads(row[0])
                for item_name, qty in items.items():
                    booked[item_name] = booked.get(item_name, 0) + qty
    return booked
async def get_items_keyboard(category: str, date_str: str, current_cart: dict) -> ReplyKeyboardMarkup:
    """Генерирует клавиатуру оборудования с актуальным остатком."""
    booked_items = await get_booked_items(date_str)
    keyboard_buttons = []
    
    for item, details in EQUIPMENT_CACHE[category].items():
        total_available = details[0]
        already_booked = booked_items.get(item, 0)
        in_cart = current_cart.get(item, 0)
        
        available_now = total_available - already_booked - in_cart
        
        if available_now > 0:
            keyboard_buttons.append([KeyboardButton(text=f"{item} ({available_now} шт.)")])
        else:
            keyboard_buttons.append([KeyboardButton(text=f"❌ {item} (Временно нет)")])
            
    keyboard_buttons.append([KeyboardButton(text="Назад"), KeyboardButton(text="Готово")])
    keyboard_buttons.append([KeyboardButton(text="Изменить дату")])
    return ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True)
def generate_receipt(cart: dict) -> tuple[str, int]:
    """Считает сумму и формирует текст чека."""
    total = 0
    lines = []
    for item, qty in cart.items():
        price = 0
        for cat_items in EQUIPMENT_CACHE.values():
            if item in cat_items:
                price = cat_items[item][1]
                break
        cost = price * qty
        total += cost
        lines.append(f"▫️ {item} x{qty} ({cost} руб.)")
    return "\n".join(lines), total
async def archive_past_bookings():
    """Фоновая задача для переноса старых броней в архив."""
    current_date = datetime.date.today().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''INSERT INTO archive_bookings (user_id, username, date, items_json, total_price) 
                            SELECT user_id, username, date, items_json, total_price FROM bookings WHERE date < ?''', (current_date,))
        await db.execute("DELETE FROM bookings WHERE date < ?", (current_date,))
        await db.commit()
    logging.info("Архивация старых бронирований выполнена.")
async def send_notification(message: str):
    try:
        await bot.send_message(chat_id=NOTIFICATION_CHAT_ID, text=message, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Ошибка уведомления: {e}")
# --- ОБРАБОТЧИКИ ---
@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Привет! Я бот для бронирования оборудования.", reply_markup=main_menu_keyboard)
@dp.message(F.text == "Забронировать оборудование")
async def start_booking(message: Message, state: FSMContext):
    await state.set_state(BookingState.choosing_date)
    await message.answer("Выберите дату бронирования:", reply_markup=await SimpleCalendar().start_calendar(
        year=datetime.datetime.now().year, month=datetime.datetime.now().month
    ))
@dp.callback_query(SimpleCalendarCallback.filter())
async def process_calendar(callback_query: CallbackQuery, callback_data: dict, state: FSMContext):
    selected, date = await SimpleCalendar().process_selection(callback_query, callback_data)
    if selected:
        selected_date = date.date()
        if selected_date < datetime.date.today():
            await callback_query.message.answer("Нельзя выбрать прошедшую дату. Попробуйте снова.")
            return
            
        date_str = selected_date.strftime("%Y-%m-%d")
        await state.update_data(date=date_str, items={}) # Очищаем корзину при смене даты
        await callback_query.message.answer(f"Вы выбрали дату: {date_str}")
        
        await state.set_state(BookingState.choosing_category)
        keyboard = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=cat)] for cat in EQUIPMENT_CACHE.keys()] +
                     [[KeyboardButton(text="Изменить дату"), KeyboardButton(text="Отмена")]],
            resize_keyboard=True
        )
        await callback_query.message.answer("Выберите категорию:", reply_markup=keyboard)
@dp.message(BookingState.choosing_category)
async def choose_category(message: Message, state: FSMContext):
    if message.text == "Изменить дату":
        await state.set_state(BookingState.choosing_date)
        await message.answer("Выберите дату:", reply_markup=await SimpleCalendar().start_calendar())
    elif message.text == "Отмена":
        await state.clear()
        await message.answer("Бронирование отменено.", reply_markup=main_menu_keyboard)
    elif message.text in EQUIPMENT_CACHE:
        await state.update_data(category=message.text)
        data = await state.get_data()
        keyboard = await get_items_keyboard(message.text, data["date"], data.get("items", {}))
        
        await state.set_state(BookingState.choosing_items)
        await message.answer("Выберите оборудование (нажмите для добавления):", reply_markup=keyboard)
    elif message.text == "Готово":
        await show_confirmation(message, state)
    else:
        await message.answer("Пожалуйста, используйте кнопки.")
@dp.message(BookingState.choosing_items)
async def choose_items(message: Message, state: FSMContext):
    data = await state.get_data()
    category = data["category"]
    date_str = data["date"]
    cart = data.get("items", {})
    if message.text == "Готово":
        if not cart:
            await message.answer("Вы ничего не выбрали.")
        else:
            await show_confirmation(message, state)
        return
    elif message.text == "Назад":
        await state.set_state(BookingState.choosing_category)
        keyboard = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=cat)] for cat in EQUIPMENT_CACHE.keys()] +
                     [[KeyboardButton(text="Изменить дату"), KeyboardButton(text="Отмена"), KeyboardButton(text="Готово")]],
            resize_keyboard=True
        )
        await message.answer("Выберите категорию:", reply_markup=keyboard)
        return
    elif message.text == "Изменить дату":
        await state.set_state(BookingState.choosing_date)
        await message.answer("Выберите дату:", reply_markup=await SimpleCalendar().start_calendar())
        return
    # Очищаем название от суффиксов
    item_name = message.text.rsplit(" (", 1)[0]
    if item_name.startswith("❌ "):
        await message.answer("Этого оборудования больше нет в наличии!")
        return
    if item_name in EQUIPMENT_CACHE[category]:
        # Проверяем доступность еще раз
        booked_db = await get_booked_items(date_str)
        total_avail = EQUIPMENT_CACHE[category][item_name][0]
        currently_avail = total_avail - booked_db.get(item_name, 0) - cart.get(item_name, 0)
        
        if currently_avail > 0:
            cart[item_name] = cart.get(item_name, 0) + 1
            await state.update_data(items=cart)
            
            # Динамически обновляем клавиатуру
            keyboard = await get_items_keyboard(category, date_str, cart)
            await message.answer(f"Добавлено: {item_name} (В корзине: {cart[item_name]} шт.)", reply_markup=keyboard)
        else:
            await message.answer("Лимит достигнут, больше нет в наличии.")
    else:
        await message.answer("Выберите оборудование из списка.")
async def show_confirmation(message: Message, state: FSMContext):
    data = await state.get_data()
    cart = data.get("items", {})
    
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Подтвердить бронь")],
            [KeyboardButton(text="Добавить еще"), KeyboardButton(text="Удалить из списка")],
            [KeyboardButton(text="Отменить смету")]
        ], resize_keyboard=True
    )
    
    if not cart:
        await message.answer("Ваш список пуст.", reply_markup=keyboard)
        await state.set_state(BookingState.confirmation)
        return
    receipt_text, total_price = generate_receipt(cart)
    await message.answer(
        f"🛒 *Структура заказа:*\n{receipt_text}\n\n*Итого:* {total_price} руб.",
        reply_markup=keyboard, parse_mode="Markdown"
    )
    await state.set_state(BookingState.confirmation)
@dp.message(BookingState.confirmation)
async def handle_confirmation(message: Message, state: FSMContext):
    if message.text == "Подтвердить бронь":
        await confirm_booking(message, state)
        
    elif message.text == "Добавить еще":
        await state.set_state(BookingState.choosing_category)
        keyboard = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=cat)] for cat in EQUIPMENT_CACHE.keys()] +
                     [[KeyboardButton(text="Отмена"), KeyboardButton(text="Готово")]],
            resize_keyboard=True
        )
        await message.answer("Выберите категорию:", reply_markup=keyboard)
        
    elif message.text == "Удалить из списка":
        data = await state.get_data()
        cart = data.get("items", {})
        if not cart:
            await message.answer("Список пуст.")
            return
            
        keyboard_buttons = [[KeyboardButton(text=f"{item} ({qty} шт.)")] for item, qty in cart.items()]
        keyboard_buttons.append([KeyboardButton(text="Назад к чеку")])
        
        await state.set_state(BookingState.removing_items)
        await message.answer("Нажмите на оборудование, чтобы убрать 1 шт.:", 
                             reply_markup=ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True))
                             
    elif message.text == "Отменить смету":
        await state.clear()
        await message.answer("Смета аннулирована.", reply_markup=main_menu_keyboard)
@dp.message(BookingState.removing_items)
async def remove_items(message: Message, state: FSMContext):
    if message.text == "Назад к чеку":
        await show_confirmation(message, state)
        return
        
    data = await state.get_data()
    cart = data.get("items", {})
    item_name = message.text.rsplit(" (", 1)[0]
    
    if item_name in cart:
        if cart[item_name] > 1:
            cart[item_name] -= 1
        else:
            del cart[item_name]
            
        await state.update_data(items=cart)
        
        if not cart:
            await message.answer("Корзина пуста.")
            await show_confirmation(message, state)
            return
            
        # Обновляем клавиатуру удаления
        keyboard_buttons = [[KeyboardButton(text=f"{item} ({qty} шт.)")] for item, qty in cart.items()]
        keyboard_buttons.append([KeyboardButton(text="Назад к чеку")])
        await message.answer(f"1 шт. убрана. Остаток {item_name}: {cart.get(item_name, 0)}", 
                             reply_markup=ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True))
async def confirm_booking(message: Message, state: FSMContext):
    data = await state.get_data()
    cart = data.get("items", {})
    if not cart:
        await message.answer("Корзина пуста. Бронь не создана.", reply_markup=main_menu_keyboard)
        await state.clear()
        return
    receipt_text, total_price = generate_receipt(cart)
    date_str = data["date"]
    items_json = json.dumps(cart, ensure_ascii=False)
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO bookings (user_id, username, date, items_json, total_price) VALUES (?, ?, ?, ?, ?)",
                         (message.from_user.id, message.from_user.username, date_str, items_json, total_price))
        await db.commit()
    await message.answer(f"✅ Успешно забронировано на {date_str}!\n\n{receipt_text}\n\nСумма: {total_price} руб.", reply_markup=main_menu_keyboard)
    await state.clear()
    
    await send_notification(
        f"📢 *Новое бронирование!*\n\n📅 *Дата:* {date_str}\n👤 *Пользователь:* @{message.from_user.username}\n\n"
        f"📦 *Оборудование:*\n{receipt_text}\n\n💵 *Итого:* {total_price} руб."
    )
# --- ПРОСМОТР И УДАЛЕНИЕ БРОНЕЙ ---
@dp.message(F.text == "Занятые даты")
async def show_booked_dates(message: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT DISTINCT date FROM bookings ORDER BY date") as cursor:
            dates = await cursor.fetchall()
            
    if dates:
        await message.answer("📅 Занятые даты:\n" + "\n".join([f"• {d[0]}" for d in dates]))
    else:
        await message.answer("Нет занятых дат.")
@dp.message(F.text.in_({"Мои бронирования", "Все бронирования", "Архив бронирований"}))
async def text_reports(message: Message):
    is_archive = message.text == "Архив бронирований"
    is_my = message.text in ("Мои бронирования", "Архив бронирований")
    table = "archive_bookings" if is_archive else "bookings"
    
    query = f"SELECT username, date, items_json, total_price FROM {table} "
    params = ()
    if is_my:
        query += "WHERE user_id = ? ORDER BY date"
        params = (message.from_user.id,)
    else:
        query += "ORDER BY date"
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(query, params) as cursor:
            bookings = await cursor.fetchall()
    if not bookings:
        await message.answer("Список пуст.")
        return
    report = f"📋 *{message.text}:*\n\n"
    for b in bookings:
        items = json.loads(b[2])
        items_str = ", ".join([f"{k} (x{v})" for k, v in items.items()])
        report += f"👤 @{b[0]}\n📅 {b[1]}\n📦 {items_str}\n💵 {b[3]} руб.\n—\n"
        
    await message.answer(report, parse_mode="Markdown")
@dp.message(F.text == "Удалить бронь")
async def start_deleting(message: Message, state: FSMContext):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, date, items_json FROM bookings WHERE user_id = ?", (message.from_user.id,)) as cursor:
            bookings = await cursor.fetchall()
            
    if not bookings:
        await message.answer("У вас нет активных бронирований.")
        return
        
    builder = InlineKeyboardBuilder()
    for b in bookings:
        items = json.loads(b[2])
        short_names = list(items.keys())[:2]
        title = f"{b[1]} | {', '.join(short_names)}..."
        builder.button(text=title, callback_data=f"del_{b[0]}")
        
    builder.adjust(1)
    await message.answer("Выберите запись для отмены:", reply_markup=builder.as_markup())
    await state.set_state(DeletingBookingState.choosing_booking_to_delete)
@dp.callback_query(DeletingBookingState.choosing_booking_to_delete, F.data.startswith("del_"))
async def process_delete(callback: CallbackQuery, state: FSMContext):
    b_id = int(callback.data.split("_")[1])
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT date, items_json FROM bookings WHERE id = ? AND user_id = ?", (b_id, callback.from_user.id)) as cursor:
            row = await cursor.fetchone()
            
        if not row:
            await callback.message.answer("Бронь не найдена.")
            return
            
        await db.execute("DELETE FROM bookings WHERE id = ?", (b_id,))
        await db.commit()
    
    items = json.loads(row[1])
    items_str = "\n".join([f"▫️ {k} x{v}" for k, v in items.items()])
    
    await callback.message.answer("Бронирование удалено ✅", reply_markup=main_menu_keyboard)
    await state.clear()
    
    await send_notification(
        f"❌ *Бронь отменена!*\n\n📅 {row[0]}\n👤 @{callback.from_user.username}\nОсвобождено:\n{items_str}"
    )
# --- ЖИЗНЕННЫЙ ЦИКЛ БОТА ---
async def on_startup():
    logging.info("Инициализация БД и загрузка конфигурации...")
    load_equipment()
    await init_db()
    # Запуск планировщика в полночь
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