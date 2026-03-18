import logging
import asyncio
import datetime
import sqlite3
import os
import json
from aiogram import Bot, Dispatcher, types
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, CallbackQuery
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.filters import CommandStart, Command
from aiogram.fsm.state import StatesGroup, State
from aiogram_calendar import SimpleCalendar, SimpleCalendarCallback
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

def load_equipment():
    with open("equipment.json","r",encoding="utf-8") as f:
      return json.load(f)
    
def get_item_data(item_name):
    equipment_data = load_equipment()
    for category, items in equipment_data.items():
        if item_name in items:
            return items[item_name]  # [кол-во, цена]
    return None

# Загружаем переменные из .env
load_dotenv()

# Получаем токен
TOKEN = os.getenv("TOKEN")

if not TOKEN:
    print("Токен не найден в .env!")
else:
    print("Токен загружен:", TOKEN)
logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ID чата для уведомлений (замените на ваш)
NOTIFICATION_CHAT_ID = "-1002534379051"

# Подключение к базе данных
conn = sqlite3.connect("bookings.db", check_same_thread=False)
cursor = conn.cursor()

# Создаем таблицу для бронирований
cursor.execute('''CREATE TABLE IF NOT EXISTS bookings (
                    user_id INTEGER,
                    username TEXT,
                    date TEXT,
                    equipment TEXT,
                    quantity INTEGER,
                    price INTEGER)''')

# Создаем таблицу для архива
cursor.execute('''CREATE TABLE IF NOT EXISTS archive_bookings (
                    user_id INTEGER,
                    username TEXT,
                    date TEXT,
                    equipment TEXT,
                    quantity INTEGER,
                    price INTEGER)''')
conn.commit()

# Состояния для FSM
class BookingState(StatesGroup):
    choosing_date = State()
    choosing_category = State()
    choosing_items = State()
    confirmation = State()
    removing_items = State()  # Состояние для удаления оборудования

class DeletingBookingState(StatesGroup):
    choosing_booking_to_delete = State()  # Состояние для удаления бронирования

# Обновляем главное меню
main_menu_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Забронировать оборудование")],
        [KeyboardButton(text="Занятые даты")],
        [KeyboardButton(text="Мои бронирования")],
        [KeyboardButton(text="Все бронирования")],
        [KeyboardButton(text="Удалить бронь")],
        [KeyboardButton(text="Архив бронирований")]  # Новая кнопка
    ],
    resize_keyboard=True
)

# Функция для отправки уведомлений в чат
async def send_notification_to_chat(message: str):
    try:
        await bot.send_message(chat_id=NOTIFICATION_CHAT_ID, text=message, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Не удалось отправить уведомление в чат: {e}")

# Функция для переноса прошедших бронирований в архив
async def move_past_bookings_to_archive():
    current_date = datetime.date.today().strftime("%Y-%m-%d")
    
    # Выбираем прошедшие бронирования
    cursor.execute("SELECT * FROM bookings WHERE date < ?", (current_date,))
    past_bookings = cursor.fetchall()
    
    if past_bookings:
        # Переносим их в архив
        cursor.executemany("INSERT INTO archive_bookings VALUES (?, ?, ?, ?, ?, ?)", past_bookings)
        conn.commit()
        
        # Удаляем из основной таблицы
        cursor.execute("DELETE FROM bookings WHERE date < ?", (current_date,))
        conn.commit()

# Команда /start
@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await message.answer("Привет! Я бот для бронирования оборудования. Используйте кнопки ниже:", reply_markup=main_menu_keyboard)

# Обработка нажатия на кнопку "Забронировать оборудование"
@dp.message(lambda message: message.text == "Забронировать оборудование")
async def start_booking(message: Message, state: FSMContext):
    await state.set_state(BookingState.choosing_date)
    await message.answer("Выберите дату бронирования:", reply_markup=await SimpleCalendar().start_calendar(
    year=datetime.datetime.now().year,
    month=datetime.datetime.now().month
))

# Обработка выбора даты из календаря
@dp.callback_query(SimpleCalendarCallback.filter())
async def process_simple_calendar(callback_query: CallbackQuery, callback_data: dict, state: FSMContext):
    selected, date = await SimpleCalendar().process_selection(callback_query, callback_data)
    if selected:
        # Преобразуем datetime.datetime в datetime.date
        selected_date = date.date()  # Получаем только дату без времени
        if selected_date < datetime.date.today():
            await callback_query.message.answer("Ошибка! Нельзя выбрать прошедшую дату.")
            return
        await state.update_data(date=selected_date.strftime("%Y-%m-%d"))
        await callback_query.message.answer(f"Вы выбрали дату: {selected_date.strftime('%Y-%m-%d')}")
        
        # Устанавливаем состояние выбора категории
        await state.set_state(BookingState.choosing_category)
        
        # Создаем клавиатуру для выбора категории
        equipment = load_equipment()
        keyboard = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=cat)] for cat in equipment.keys()] +
                     [[KeyboardButton(text="Изменить дату"), KeyboardButton(text="Отмена"), KeyboardButton(text="Готово")]],
            resize_keyboard=True
        )
        await callback_query.message.answer("Выберите категорию оборудования:", reply_markup=keyboard)
        cursor.execute("SELECT equipment FROM bookings WHERE date = ?", (date,))
        booked_equipment = cursor.fetchall()
        booked_items = {}# Обработка выбора категории
        @dp.message(BookingState.choosing_category)
        async def choose_category(message: Message, state: FSMContext):
            equipment = load_equipment()
            if message.text in equipment:
                data = await state.get_data()
                items = data.get("items", {})
                text, keyboard = build_cart_text_and_keyboard(items, message.text)
                msg = await message.answer(text, reply_markup=keyboard)
                await state.update_data(
                    category=message.text,
                    main_msg_id=msg.message_id,
                    items=items
                )
        await state.set_state(BookingState.choosing_items)
    elif message.text == "Изменить дату":
        await state.set_state(BookingState.choosing_date)
        await message.answer("Выберите дату:", reply_markup=await SimpleCalendar().start_calendar())
    elif message.text == "Отмена":
        await state.clear()
        await message.answer("Отменено", reply_markup=main_menu_keyboard)
        # Клавиатура
        keyboard_buttons = []
        for item, details in equipment[message.text].items():
            total_available = details[0]
            booked = booked_items.get(item, 0)
            selected = items.get(message.text, {}).get(item, 0)
            available = total_available - booked - selected
            keyboard_buttons.append([
                KeyboardButton(text=f"{item} ({max(0, available)} шт.)")
            ])
        keyboard_buttons.append([KeyboardButton(text="Назад"), KeyboardButton(text="Готово")])
        keyboard_buttons.append([KeyboardButton(text="Изменить дату")])
        keyboard = ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True)
        # Смета
        text = "📋 Ваша смета:\n\n"
        total_price = 0
        for cat, cat_items in items.items():
            text += f"📦 {cat}:\n"
            for item, qty in cat_items.items():
                price = equipment[cat][item][1] * qty
                total_price += price
                text += f"{item} x{qty} — {price} руб.\n"
            text += "\n"
        if not items:
            text += "Пока ничего не выбрано\n"
        text += f"💰 Итого: {total_price} руб."
        # РЕДАКТИРУЕМ сообщение
        try:
            if msg_id:
                await message.bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=msg_id,
                    text=text,
                    reply_markup=keyboard
                )
            else:
                msg = await message.answer(text, reply_markup=keyboard)
                await state.update_data(main_msg_id=msg.message_id)
        except:
            msg = await message.answer(text, reply_markup=keyboard)
            await state.update_data(main_msg_id=msg.message_id)
        await state.set_state(BookingState.choosing_items)
    elif message.text == "Изменить дату":
        await state.set_state(BookingState.choosing_date)
        await message.answer("Выберите дату бронирования:", reply_markup=await SimpleCalendar().start_calendar())
    elif message.text == "Отмена":
        await state.clear()
        await message.answer("Бронирование отменено.", reply_markup=main_menu_keyboard)
    elif message.text == "Готово":
        await show_confirmation(message, state)
    else:
        await message.answer("Выберите категорию из списка.")
# Функция для показа подтверждения бронирования
async def show_confirmation(message: Message, state: FSMContext):
    data = await state.get_data()
    items = data.get("items", {})
    total_price = 0
    user_friendly_details = []
    for item, quantity in items.items():
        item_data = get_item_data(item)
        if item_data:
            price_per_unit = item_data[1]
            total_item_price = price_per_unit * quantity
            total_price += total_item_price
            user_friendly_details.append(f"{item} x{quantity} ({total_item_price} руб.)")
    selected_items = "\n".join(user_friendly_details)
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Подтвердить бронь")],
            [KeyboardButton(text="Добавить еще оборудование")],
            [KeyboardButton(text="Удалить оборудование")],
            [KeyboardButton(text="Отменить смету")]
        ],
        resize_keyboard=True
    )
    if items:
        await message.answer(
            f"Текущий заказ:\n{selected_items}\n\n*Итого: {total_price} руб.*\n\nВыберите действие:",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    else:
        await message.answer("Вы не выбрали ни одного оборудования.", reply_markup=keyboard)
    await state.set_state(BookingState.confirmation)

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
    text, keyboard = build_cart_text_and_keyboard(items, category)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()
        
# Обработка подтверждения бронирования
@dp.message(BookingState.confirmation)
async def handle_confirmation(message: Message, state: FSMContext):
    if message.text == "Подтвердить бронь":
        await confirm_booking(message, state)
    elif message.text == "Добавить еще оборудование":
        await state.set_state(BookingState.choosing_category)
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
    elif message.text == "Отменить смету":  # Обработка новой кнопки
        await state.clear()
        await message.answer("Смета отменена. Вы вернулись в главное меню.", reply_markup=main_menu_keyboard)
    else:
        await message.answer("Используйте кнопки для выбора действия.")
        
        # Обновляем клавиатуру с новыми данными
        keyboard_buttons = []
        for item, quantity in items.items():
            keyboard_buttons.append([KeyboardButton(text=f"{item} ({quantity} шт.)")])
        
        keyboard_buttons.append([KeyboardButton(text="Назад")])
        keyboard = ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True)
        await message.answer("Выберите оборудование для удаления:", reply_markup=keyboard)
    elif message.text == "Назад":
        await show_confirmation(message, state)
    else:
        await message.answer("Используйте кнопки для выбора оборудования.")

# Обработка нажатия на кнопку "Занятые даты"
@dp.message(lambda message: message.text == "Занятые даты")
async def show_booked_dates(message: Message):
    cursor.execute("SELECT DISTINCT date FROM bookings")
    dates = cursor.fetchall()
    if dates:
        await message.answer("Занятые даты:\n" + "\n".join([date[0] for date in dates]))
    else:
        await message.answer("Нет занятых дат.")

# Обработка нажатия на кнопку "Мои бронирования"
@dp.message(lambda message: message.text == "Мои бронирования")
async def user_report(message: Message):
    # Переносим прошедшие бронирования в архив
    await move_past_bookings_to_archive()
    
    # Получаем актуальные бронирования
    cursor.execute("SELECT username, date, price FROM bookings WHERE user_id = ?", (message.from_user.id,))
    bookings = cursor.fetchall()
    
    if bookings:
        report = "📋 *Ваши бронирования:*\n\n"
        for booking in bookings:
            username, date, price = booking
            report += (
                f"👤 *Пользователь:* {username}\n"
                f"📅 *Дата:* {date}\n"
                f"💵 *Сумма:* {price} руб.\n"
                "————————————\n"
            )
        await message.answer(report, parse_mode="Markdown")
    else:
        await message.answer("У вас нет активных бронирований.")

# Обработка нажатия на кнопку "Все бронирования"
@dp.message(lambda message: message.text == "Все бронирования")
async def full_report(message: Message):
    # Переносим прошедшие бронирования в архив
    await move_past_bookings_to_archive()
    
    cursor.execute("SELECT username, date, price FROM bookings")
    bookings = cursor.fetchall()
    if bookings:
        report = "📋 *Все бронирования:*\n\n"
        for booking in bookings:
            username, date, price = booking
            report += (
                f"👤 *Пользователь:* {username}\n"
                f"📅 *Дата:* {date}\n"
                f"💵 *Сумма:* {price} руб.\n"
                "————————————\n"
            )
        await message.answer(report, parse_mode="Markdown")
    else:
        await message.answer("Нет активных бронирований.")

# Обработка нажатия на кнопку "Архив бронирований"
@dp.message(lambda message: message.text == "Архив бронирований")
async def show_archive(message: Message):
    # Получаем архивные бронирования пользователя
    cursor.execute("SELECT username, date, price FROM archive_bookings WHERE user_id = ?", (message.from_user.id,))
    archive_bookings = cursor.fetchall()
    
    if archive_bookings:
        report = "📋 *Ваши архивные бронирования:*\n\n"
        for booking in archive_bookings:
            username, date, price = booking
            report += (
                f"👤 *Пользователь:* {username}\n"
                f"📅 *Дата:* {date}\n"
                f"💵 *Сумма:* {price} руб.\n"
                "————————————\n"
            )
        await message.answer(report, parse_mode="Markdown")
    else:
        await message.answer("У вас нет архивных бронирований.")

# Обработка нажатия на кнопку "Удалить бронь"
@dp.message(lambda message: message.text == "Удалить бронь")
async def start_deleting_booking(message: Message, state: FSMContext):
    # Переносим прошедшие бронирования в архив
    await move_past_bookings_to_archive()
    
    # Получаем все актуальные бронирования пользователя
    cursor.execute("SELECT rowid, date, equipment FROM bookings WHERE user_id = ?", (message.from_user.id,))
    bookings = cursor.fetchall()
    
    if not bookings:
        await message.answer("У вас нет активных бронирований.")
        return
    
    # Создаем клавиатуру с кнопками
    builder = InlineKeyboardBuilder()
    for booking in bookings:
        rowid, date, equipment = booking
        # Берем первые несколько позиций оборудования для отображения
        equipment_list = equipment.split("\n")
        short_equipment = ", ".join(equipment_list[:3])  # Показываем первые 3 позиции
        if len(equipment_list) > 3:
            short_equipment += "..."  # Добавляем многоточие, если позиций больше 3
        # Формируем текст кнопки
        button_text = f"{date} - {short_equipment}"
        # Добавляем кнопку с callback_data, содержащим ID бронирования
        builder.button(text=button_text, callback_data=f"delete_booking:{rowid}")
    builder.adjust(1)  # Располагаем кнопки по одной в строке
    
    # Отправляем сообщение с клавиатурой
    await message.answer("Выберите бронирование для удаления:", reply_markup=builder.as_markup())
    await state.set_state(DeletingBookingState.choosing_booking_to_delete)

# Обработка выбора бронирования для удаления
@dp.callback_query(DeletingBookingState.choosing_booking_to_delete, lambda c: c.data.startswith("delete_booking:"))
async def process_booking_deletion(callback_query: CallbackQuery, state: FSMContext):
    # Извлекаем ID бронирования из callback_data
    selected_id = int(callback_query.data.split(":")[1])
    
    # Проверяем, что бронирование принадлежит текущему пользователю
    cursor.execute("SELECT rowid, date, equipment FROM bookings WHERE rowid = ? AND user_id = ?", (selected_id, callback_query.from_user.id))
    selected_booking = cursor.fetchone()
    
    if not selected_booking:
        await callback_query.message.answer("Бронирование с таким ID не найдено или оно принадлежит другому пользователю.")
        return
    
    # Удаляем бронирование из базы данных
    cursor.execute("DELETE FROM bookings WHERE rowid = ?", (selected_id,))
    conn.commit()
    
    await callback_query.message.answer(f"Бронирование на {selected_booking[1]} успешно удалено!", reply_markup=main_menu_keyboard)
    await state.clear()

    # Уведомление в чат об отмене бронирования
    notification_message = (
        "❌ *Бронирование отменено!*\n\n"
        f"📅 *Дата:* {selected_booking[1]}\n"
        f"👤 *Пользователь:* @{callback_query.from_user.username}\n"
        f"📦 *Оборудование:* {selected_booking[2]}\n\n"
        "Оборудование снова доступно для бронирования! 🎉"
    )
    await send_notification_to_chat(notification_message)

# Подтверждение бронирования
async def confirm_booking(message: Message, state: FSMContext):
    data = await state.get_data()
    date = data["date"]
    items = data.get("items", {})
    
    # Рассчитываем общую стоимость и формируем данные для сохранения
    total_price = 0
    booking_details = []
    for item, quantity in items.items():
        item_data = get_item_data(item)
        if item_data:
            price = item_data[1] * quantity
            total_price += price
            booking_details.append(f"{item} x{quantity}")
    
    # Сохраняем бронирование в базу данных
    cursor.execute(
        "INSERT INTO bookings (user_id, username, date, equipment, quantity, price) VALUES (?, ?, ?, ?, ?, ?)",
        (message.from_user.id, message.from_user.username, date, "\n".join(booking_details), sum(items.values()), total_price)
    )
    conn.commit()
    
    # Формируем сообщение с ценами для пользователя
    user_friendly_details = []
    for item, quantity in items.items():
        item_data = get_item_data(item)
        if item_data:
            price = item_data[1] * quantity
            user_friendly_details.append(f"{item} x{quantity} ({price} руб.)")
    
    # Отправляем сообщение пользователю
    await message.answer(
        f"Вы забронировали:\n" + "\n".join(user_friendly_details) + f"\nИтого: {total_price} руб."
    )
    await message.answer("Бронирование завершено, спасибо!", reply_markup=main_menu_keyboard)
    await state.clear()

    # Уведомление в чат о новом бронировании
    notification_message = (
        "📢 *Новое бронирование!*\n\n"
        f"📅 *Дата:* {date}\n"
        f"👤 *Пользователь:* @{message.from_user.username}\n"
        f"📦 *Оборудование:*\n" + "\n".join(user_friendly_details) + "\n"
        f"💵 *Итого:* {total_price} руб.\n\n"
    )
    await send_notification_to_chat(notification_message)

# Завершение работы бота
async def on_shutdown(dp):
    conn.close()
    logging.info("Закрытие соединения с базой данных")

# Запуск бота
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logging.error(f"Ошибка: {e}")
    finally:
        conn.close()









