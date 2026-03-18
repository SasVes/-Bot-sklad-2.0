import logging
import asyncio
import datetime
import sqlite3
import os
import json
from aiogram import Bot, Dispatcher
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, CallbackQuery
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.filters import CommandStart
from aiogram.fsm.state import StatesGroup, State
from aiogram_calendar import SimpleCalendar, SimpleCalendarCallback
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
# ---------- LOAD ----------
def load_equipment():
    with open("equipment.json","r",encoding="utf-8") as f:
        return json.load(f)
def get_item_data(item_name):
    equipment_data = load_equipment()
    for category, items in equipment_data.items():
        if item_name in items:
            return items[item_name]
    return None
def build_cart_text_and_keyboard(items, current_category):
    equipment = load_equipment()
    text = "📋 Ваша смета:\n\n"
    total_price = 0
    for category, cat_items in items.items():
        text += f"📦 {category}:\n"
        for item, qty in cat_items.items():
            price = equipment[category][item][1] * qty
            total_price += price
            text += f"{item} x{qty} — {price} руб.\n"
        text += "\n"
    if not items:
        text += "Пока ничего не выбрано\n\n"
    text += f"💰 Итого: {total_price} руб.\n\n"
    builder = InlineKeyboardBuilder()
    for item in equipment[current_category]:
        builder.button(text=f"➕ {item}", callback_data=f"add:{current_category}:{item}")
        builder.button(text=f"➖ {item}", callback_data=f"remove:{current_category}:{item}")
    builder.adjust(2)
    for cat in equipment.keys():
        builder.button(text=f"📂 {cat}", callback_data=f"cat:{cat}")
    builder.button(text="✅ Готово", callback_data="done")
    builder.adjust(2)
    return text, builder.as_markup()
# ---------- INIT ----------
load_dotenv()
TOKEN = os.getenv("TOKEN")
logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())
conn = sqlite3.connect("bookings.db", check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''CREATE TABLE IF NOT EXISTS bookings (
    user_id INTEGER,
    username TEXT,
    date TEXT,
    equipment TEXT,
    quantity INTEGER,
    price INTEGER)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS archive_bookings (
    user_id INTEGER,
    username TEXT,
    date TEXT,
    equipment TEXT,
    quantity INTEGER,
    price INTEGER)''')
conn.commit()
# ---------- STATES ----------
class BookingState(StatesGroup):
    choosing_date = State()
    choosing_category = State()
    choosing_items = State()
    confirmation = State()
class DeletingBookingState(StatesGroup):
    choosing_booking_to_delete = State()
# ---------- MENU ----------
main_menu_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Забронировать оборудование")],
        [KeyboardButton(text="Занятые даты")],
        [KeyboardButton(text="Мои бронирования")],
        [KeyboardButton(text="Все бронирования")],
        [KeyboardButton(text="Удалить бронь")],
        [KeyboardButton(text="Архив бронирований")]
    ],
    resize_keyboard=True
)
# ---------- START ----------
@dp.message(CommandStart())
async def start(message: Message):
    await message.answer("Привет!", reply_markup=main_menu_keyboard)
# ---------- START BOOKING ----------
@dp.message(lambda m: m.text == "Забронировать оборудование")
async def start_booking(message: Message, state: FSMContext):
    await state.set_state(BookingState.choosing_date)
    await message.answer("Выберите дату:", reply_markup=await SimpleCalendar().start_calendar(
        year=datetime.datetime.now().year,
        month=datetime.datetime.now().month
    ))
# ---------- DATE ----------
@dp.callback_query(SimpleCalendarCallback.filter())
async def process_calendar(callback: CallbackQuery, callback_data: dict, state: FSMContext):
    selected, date = await SimpleCalendar().process_selection(callback, callback_data)
    if selected:
        if date.date() < datetime.date.today():
            await callback.message.answer("Нельзя выбрать прошедшую дату")
            return
        await state.update_data(date=str(date.date()))
        equipment = load_equipment()
        kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=cat)] for cat in equipment.keys()],
            resize_keyboard=True
        )
        await state.set_state(BookingState.choosing_category)
        await callback.message.answer("Выберите категорию:", reply_markup=kb)
# ---------- CATEGORY ----------
@dp.message(BookingState.choosing_category)
async def choose_category(message: Message, state: FSMContext):
    equipment = load_equipment()
    if message.text in equipment:
        data = await state.get_data()
        items = data.get("items", {})
        text, kb = build_cart_text_and_keyboard(items, message.text)
        msg = await message.answer(text, reply_markup=kb)
        await state.update_data(
            category=message.text,
            items=items,
            main_msg_id=msg.message_id
        )
        await state.set_state(BookingState.choosing_items)
# ---------- CART ----------
@dp.callback_query(BookingState.choosing_items)
async def handle_cart(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    items = data.get("items", {})
    category = data.get("category")
    action = callback.data
    if action.startswith("add:"):
        _, cat, item = action.split(":")
        cat_items = items.get(cat, {})
        cat_items[item] = cat_items.get(item, 0) + 1
        items[cat] = cat_items
    elif action.startswith("remove:"):
        _, cat, item = action.split(":")
        if cat in items and item in items[cat]:
            if items[cat][item] > 1:
                items[cat][item] -= 1
            else:
                del items[cat][item]
                if not items[cat]:
                    del items[cat]
    elif action.startswith("cat:"):
        _, category = action.split(":")
    elif action == "done":
        if not items:
            await callback.answer("Корзина пустая", show_alert=True)
            return
        await show_confirmation(callback.message, state)
        return
    await state.update_data(items=items, category=category)
    text, kb = build_cart_text_and_keyboard(items, category)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()
# ---------- CONFIRM ----------
async def show_confirmation(message: Message, state: FSMContext):
    data = await state.get_data()
    items = data.get("items", {})
    total = 0
    text = "Ваш заказ:\n\n"
    for cat, cat_items in items.items():
        text += f"{cat}:\n"
        for item, qty in cat_items.items():
            price = get_item_data(item)[1] * qty
            total += price
            text += f"{item} x{qty} ({price} руб.)\n"
    text += f"\nИтого: {total} руб."
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Подтвердить бронь")],
            [KeyboardButton(text="Добавить еще оборудование")],
            [KeyboardButton(text="Отменить смету")]
        ],
        resize_keyboard=True
    )
    await message.answer(text, reply_markup=keyboard)
    await state.set_state(BookingState.confirmation)
# ---------- CONFIRM HANDLER ----------
@dp.message(BookingState.confirmation)
async def handle_confirmation(message: Message, state: FSMContext):
    if message.text == "Подтвердить бронь":
        await confirm_booking(message, state)
    elif message.text == "Добавить еще оборудование":
        await state.set_state(BookingState.choosing_category)
    elif message.text == "Отменить смету":
        await state.clear()
        await message.answer("Отменено", reply_markup=main_menu_keyboard)
# ---------- SAVE ----------
async def confirm_booking(message: Message, state: FSMContext):
    data = await state.get_data()
    date = data["date"]
    items = data.get("items", {})
    total = 0
    details = []
    for cat, cat_items in items.items():
        for item, qty in cat_items.items():
            price = get_item_data(item)[1] * qty
            total += price
            details.append(f"{item} x{qty}")
    cursor.execute(
        "INSERT INTO bookings VALUES (?, ?, ?, ?, ?, ?)",
        (message.from_user.id, message.from_user.username, date, "\n".join(details), 0, total)
    )
    conn.commit()
    await message.answer("Бронирование завершено ✅", reply_markup=main_menu_keyboard)
    await state.clear()
# ---------- RUN ----------
async def main():
    await dp.start_polling(bot)
if __name__ == "__main__":
    asyncio.run(main())








