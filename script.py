import logging
import json
import os
import time
import random
import colorlog
import psycopg2
from urllib.parse import urlparse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from dotenv import load_dotenv

# --- بارگذاری متغیرهای محیطی برای تست محلی ---
load_dotenv()

# --- تنظیمات لاگ رنگی ---
handler = colorlog.StreamHandler()
formatter = colorlog.ColoredFormatter(
    '%(log_color)s%(asctime)s - %(levelname)s - %(message)s',
    log_colors={'DEBUG': 'green', 'INFO': 'blue', 'WARNING': 'yellow', 'ERROR': 'red', 'CRITICAL': 'red,bg_white'},
    reset=True, style='%'
)
logger = colorlog.getLogger()
if not logger.handlers:
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# --- متغیرهای اصلی (آماده برای Render) ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID"))
ARCHIVE_PASSWORD = os.environ.get("ARCHIVE_PASSWORD")
# این متغیر به صورت خودکار توسط Render پر می‌شود وقتی دیتابیس را به سرویس متصل کنید
DATABASE_URL = os.environ.get("DATABASE_URL")

# --- تعریف حالت‌های مکالمه (States) ---
(SELECTING_ACTION,
 SELECTING_INTERVIEW, SELECTING_POLITICAL_CATEGORY, ANSWERING_QUESTIONS, CONFIRM_SUBMISSION,
 SELECTING_DESIGN_ACTION,
 SELECT_ADD_CAT, SELECT_ADD_POLITICAL_CAT, ADDING_QUESTION_TEXT, ASK_ADD_ANOTHER,
 SELECT_DEL_CAT, SELECT_DEL_POLITICAL_CAT, LISTING_QUESTIONS_FOR_DELETE, DELETING_QUESTION_BY_NUMBER,
 ARCHIVE_PASSWORD_PROMPT,
 LISTING_ARCHIVED_USERS, SELECTING_ARCHIVE_CATEGORY, SHOWING_USER_INTERVIEWS,
 SELECTING_REGULATIONS_TEST_TYPE,
 REGULATIONS_TEST_ANSWERING,
 SELECT_REGULATION_TYPE_FOR_ADD,
 ADDING_REGULATION_QUESTION_TEXT,
 ADDING_REGULATION_OPTION_1,
 ADDING_REGULATION_OPTION_2,
 ADDING_REGULATION_OPTION_3,
 ADDING_REGULATION_OPTION_4,
 SELECTING_REGULATION_CORRECT_ANSWER
 ) = range(27)


# --- توابع مدیریت پایگاه داده (PostgreSQL) - تغییر یافته ---
def get_db_connection():
    """یک اتصال جدید به دیتابیس ایجاد می‌کند."""
    return psycopg2.connect(DATABASE_URL)


def setup_database():
    """جداول مورد نیاز را در دیتابیس PostgreSQL ایجاد می‌کند."""
    conn = get_db_connection()
    cursor = conn.cursor()
    # تغییر AUTOINCREMENT به SERIAL PRIMARY KEY برای PostgreSQL
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS interview_questions (
            id SERIAL PRIMARY KEY,
            category TEXT NOT NULL,
            subcategory TEXT,
            question_text TEXT NOT NULL UNIQUE
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS archive (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            user_name TEXT NOT NULL,
            interview_type TEXT NOT NULL,
            full_text TEXT NOT NULL,
            timestamp BIGINT NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS regulation_questions (
            id SERIAL PRIMARY KEY,
            test_type TEXT NOT NULL,
            question TEXT NOT NULL UNIQUE,
            options JSONB NOT NULL,
            answer INTEGER NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_attempts (
            user_id BIGINT NOT NULL,
            test_type TEXT NOT NULL,
            timestamp BIGINT NOT NULL,
            PRIMARY KEY (user_id, test_type)
        )
    ''')
    conn.commit()
    cursor.close()
    conn.close()
    logger.info("پایگاه داده PostgreSQL با موفقیت آماده‌سازی شد.")


def db_query(query, params=(), fetchone=False, fetchall=False):
    """یک کوئری را روی دیتابیس PostgreSQL اجرا می‌کند."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # تغییر placeholder از ? به %s برای psycopg2
        cursor.execute(query, params)
        result = None
        if fetchone:
            result = cursor.fetchone()
        if fetchall:
            result = cursor.fetchall()
        conn.commit()
        cursor.close()
        return result
    except psycopg2.Error as e:
        logger.error(f"خطای دیتابیس: {e}")
        return None
    finally:
        if conn:
            conn.close()


# --- توابع کار با سوالات مصاحبه (بدون تغییر در منطق) ---
def add_interview_question_to_db(category, subcategory, question_text):
    db_query(
        "INSERT INTO interview_questions (category, subcategory, question_text) VALUES (%s, %s, %s) ON CONFLICT (question_text) DO NOTHING",
        (category, subcategory, question_text))


def get_interview_questions_from_db(category, subcategory=None):
    if subcategory:
        return db_query("SELECT id, question_text FROM interview_questions WHERE category = %s AND subcategory = %s",
                        (category, subcategory), fetchall=True)
    else:
        return db_query("SELECT id, question_text FROM interview_questions WHERE category = %s AND subcategory IS NULL",
                        (category,), fetchall=True)


def delete_interview_question_from_db(question_id):
    db_query("DELETE FROM interview_questions WHERE id = %s", (question_id,))


# --- توابع کار با سوالات آیین‌نامه (تغییر در نحوه ذخیره JSON) ---
def add_regulation_question_to_db(test_type, question, options, answer):
    options_json = json.dumps(options, ensure_ascii=False)
    db_query(
        "INSERT INTO regulation_questions (test_type, question, options, answer) VALUES (%s, %s, %s, %s) ON CONFLICT (question) DO NOTHING",
        (test_type, question, options_json, answer))


# --- سایر توابع دیتابیس (بدون تغییر در منطق) ---
def add_to_archive_db(user_id, user_name, interview_type, full_text):
    db_query(
        "INSERT INTO archive (user_id, user_name, interview_type, full_text, timestamp) VALUES (%s, %s, %s, %s, %s)",
        (user_id, user_name, interview_type, full_text, int(time.time())))


def get_archived_users_from_db():
    return db_query("SELECT DISTINCT user_id, user_name FROM archive ORDER BY user_name", fetchall=True)


def get_user_interviews_from_db(user_id, interview_type=None):
    if interview_type and interview_type != 'all':
        results = db_query(
            "SELECT full_text FROM archive WHERE user_id = %s AND interview_type = %s ORDER BY timestamp DESC",
            (user_id, interview_type), fetchall=True)
    else:
        results = db_query("SELECT full_text FROM archive WHERE user_id = %s ORDER BY timestamp DESC", (user_id,),
                           fetchall=True)
    return [item[0] for item in results] if results else []


def get_regulation_questions_from_db(test_type):
    results = db_query("SELECT question, options, answer FROM regulation_questions WHERE test_type = %s", (test_type,),
                       fetchall=True)
    if not results:
        return []
    # گزینه‌ها در PostgreSQL به صورت دیکشنری/لیست خوانده می‌شوند
    return [{"question": q, "options": opt, "answer": a} for q, opt, a in results]


def get_user_attempt_from_db(user_id, test_type):
    result = db_query("SELECT timestamp FROM user_attempts WHERE user_id = %s AND test_type = %s", (user_id, test_type),
                      fetchone=True)
    return result[0] if result else None


def set_user_attempt_in_db(user_id, test_type):
    # استفاده از ON CONFLICT برای PostgreSQL
    query = """
    INSERT INTO user_attempts (user_id, test_type, timestamp) VALUES (%s, %s, %s)
    ON CONFLICT (user_id, test_type) DO UPDATE SET timestamp = EXCLUDED.timestamp;
    """
    db_query(query, (user_id, test_type, int(time.time())))


def clear_user_attempt_in_db(user_id, test_type):
    db_query("DELETE FROM user_attempts WHERE user_id = %s AND test_type = %s", (user_id, test_type))


def escape_html(text: str) -> str:
    if not text:
        return ""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --- توابع عمومی و منوی اصلی ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [
        [InlineKeyboardButton("📝 انجام مصاحبه", callback_data='interview')],
        [InlineKeyboardButton("✍️ مدیریت سوالات (ادمین)", callback_data='design_question')],
        [InlineKeyboardButton("🗄️ بایگانی", callback_data='archive')],
        [InlineKeyboardButton("📜 آزمون آیین‌نامه انجمن", callback_data='regulations_test')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = 'سلام! به ربات مصاحبه خوش آمدید. لطفا انتخاب کنید:'

    if context.user_data.get('new_menu_message', False):
        await context.bot.send_message(chat_id=update.effective_chat.id, text="منوی اصلی:", reply_markup=reply_markup)
        context.user_data['new_menu_message'] = False
    elif update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text('منوی اصلی:', reply_markup=reply_markup)
        except Exception:
            await context.bot.send_message(chat_id=update.effective_chat.id, text='منوی اصلی:',
                                           reply_markup=reply_markup)
    return SELECTING_ACTION


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text('کاربر گرامی، پاسخ شما به سوالات برای مدیر ارسال خواهد شد.')


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.message or update.callback_query.message
    await message.reply_text('عملیات لغو شد. برای شروع مجدد /start را بزنید.')
    context.user_data.clear()
    return ConversationHandler.END


# --- بخش مصاحبه کاربر ---
async def show_interview_options(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("👤 شخصی", callback_data='personal'), InlineKeyboardButton("💼 شغلی", callback_data='job')],
        [InlineKeyboardButton("🏛 سیاسی", callback_data='political')],
        [InlineKeyboardButton("بازگشت ⬅️", callback_data='back_to_main')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text="لطفا نوع مصاحبه را انتخاب کنید:", reply_markup=reply_markup)
    return SELECTING_INTERVIEW


async def show_political_categories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    subcategories = ["جمهوری اسلامی", "پهلوی", "قاجار", "عهَد باستان", "ایران پس از اسلام", "نازیسم", "کمونیسم",
                     "لیبرالیسم", "یهودیت", "ووکیسم", "سرمایه داری"]
    keyboard = [[InlineKeyboardButton(cat, callback_data=f"political_{cat}")] for cat in subcategories]
    keyboard.append([InlineKeyboardButton("بازگشت ⬅️", callback_data='back_to_interview_menu')])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text="لطفا یکی از موضوعات مصاحبه سیاسی را انتخاب کنید:", reply_markup=reply_markup)
    return SELECTING_POLITICAL_CATEGORY


async def start_questions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    category_data = query.data
    questions_from_db = []
    category, subcategory = None, None
    if category_data in ['personal', 'job']:
        category = "شخصی" if category_data == 'personal' else "شغلی"
        questions_from_db = get_interview_questions_from_db(category)
        context.user_data.update({'category': category, 'subcategory': None})
    elif category_data.startswith('political_'):
        category = "سیاسی"
        subcategory = category_data.split('_', 1)[1]
        questions_from_db = get_interview_questions_from_db(category, subcategory)
        context.user_data.update({'category': category, 'subcategory': subcategory})
    if not questions_from_db:
        await query.edit_message_text("در این بخش سوالی وجود ندارد.", reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("بازگشت ⬅️", callback_data='back_to_interview_menu')]]))
        return SELECTING_POLITICAL_CATEGORY
    context.user_data['questions'] = [{"id": q_id, "text": q_text} for q_id, q_text in questions_from_db]
    context.user_data.update({'current_question_index': 0, 'answers': []})
    await query.edit_message_text(text=f"سوال ۱:\n\n{context.user_data['questions'][0]['text']}")
    return ANSWERING_QUESTIONS


async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['answers'].append(update.message.text)
    current_index = context.user_data.get('current_question_index', 0) + 1
    context.user_data['current_question_index'] = current_index
    questions = context.user_data['questions']
    if current_index < len(questions):
        await update.message.reply_text(f"سوال {current_index + 1}:\n\n{questions[current_index]['text']}")
        return ANSWERING_QUESTIONS
    else:
        keyboard = [[InlineKeyboardButton("✅ بله، ارسال کن", callback_data='confirm_yes'),
                     InlineKeyboardButton("❌ خیر، لغو کن", callback_data='confirm_no')]]
        await update.message.reply_text("✅ سوالات تمام شد. آیا پاسخ‌ها برای مدیر ارسال شود؟",
                                        reply_markup=InlineKeyboardMarkup(keyboard))
        return CONFIRM_SUBMISSION


async def confirm_submission(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == 'confirm_yes':
        user = query.from_user
        user_first_name = escape_html(user.first_name)
        user_last_name = escape_html(user.last_name or '')
        user_username = escape_html(user.username or 'N/A')
        final_text = f"📝 <b>پاسخ مصاحبه از کاربر:</b>\n"
        final_text += f"<b>نام:</b> {user_first_name} {user_last_name}\n<b>نام کاربری:</b> @{user_username}\n<b>شناسه:</b> <code>{user.id}</code>\n"
        category = escape_html(context.user_data['category'])
        subcategory = escape_html(context.user_data.get('subcategory') or "")
        final_text += f"<b>نوع مصاحبه:</b> {category}" + (
            f" - {subcategory}" if subcategory else "") + "\n------------------------------------\n\n"
        for i, q_data in enumerate(context.user_data['questions']):
            escaped_q = escape_html(q_data['text'])
            escaped_a = escape_html(context.user_data['answers'][i])
            final_text += f"<b>❓ سوال {i + 1}:</b> {escaped_q}\n<b>🗣️ پاسخ:</b> {escaped_a}\n\n"

        unique_id = f"{user.id}_{int(time.time())}"
        context.bot_data[unique_id] = {
            'text': final_text,
            'user_info': {'id': user.id, 'name': f"{user.first_name} {user.last_name or ''}".strip()},
            'interview_type': context.user_data['category']
        }

        keyboard = [[
            InlineKeyboardButton("➕ افزودن به بایگانی", callback_data=f"archive_add_{unique_id}"),
            InlineKeyboardButton("❌ نادیده گرفتن", callback_data=f"archive_ignore_{unique_id}")
        ]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=final_text, parse_mode=ParseMode.HTML,
                                           reply_markup=reply_markup)
            await query.edit_message_text("✅ پاسخ‌های شما با موفقیت برای مدیر ارسال شد.")
        except Exception as e:
            logger.error(f"ارسال پیام به ادمین ناموفق بود: {e}")
            await query.edit_message_text("خطا در ارسال پیام به مدیر.")
    else:
        await query.edit_message_text("ارسال پاسخ‌ها لغو شد.")
    context.user_data.clear()
    context.user_data['new_menu_message'] = True
    return await start(update, context)


# --- توابع مدیریت بایگانی ---
async def add_to_archive_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await check_admin(update): return

    unique_id = query.data.split('archive_add_')[1]
    if unique_id in context.bot_data:
        data_to_archive = context.bot_data.pop(unique_id)
        add_to_archive_db(
            user_id=data_to_archive['user_info']['id'],
            user_name=data_to_archive['user_info']['name'],
            interview_type=data_to_archive['interview_type'],
            full_text=data_to_archive['text']
        )
        await query.edit_message_text(query.message.text + "\n\n<b>✅ با موفقیت به بایگانی اضافه شد.</b>",
                                      parse_mode=ParseMode.HTML)
    else:
        await query.edit_message_text(
            query.message.text + "\n\n<b>⚠️ خطا: این مورد قبلا بایگانی شده یا منقضی شده است.</b>",
            parse_mode=ParseMode.HTML)


async def ignore_archive_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await check_admin(update): return
    unique_id = query.data.split('archive_ignore_')[1]
    if unique_id in context.bot_data:
        context.bot_data.pop(unique_id)
    await query.edit_message_text(query.message.text + "\n\n--- 🚮 نادیده گرفته شد ---", parse_mode=ParseMode.HTML)


# --- توابع کمکی ادمین ---
async def check_admin(update: Update) -> bool:
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        if update.callback_query: await update.callback_query.answer("🚫 شما دسترسی لازم برای این بخش را ندارید.",
                                                                     show_alert=True)
        return False
    return True


# --- بخش مدیریت سوالات (ادمین) ---
async def show_design_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if not await check_admin(update): return SELECTING_ACTION
    keyboard = [
        [InlineKeyboardButton("➕ ایجاد سوال مصاحبه", callback_data='design_create_interview')],
        [InlineKeyboardButton("🗑️ پاک کردن سوال مصاحبه", callback_data='design_delete_interview')],
        [InlineKeyboardButton("➕ ایجاد سوال آیین‌نامه", callback_data='design_create_regulation')],
        [InlineKeyboardButton("بازگشت ⬅️", callback_data='back_to_main')],
    ]
    await query.edit_message_text("بخش مدیریت سوالات:", reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECTING_DESIGN_ACTION


async def select_category_for_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [
        [InlineKeyboardButton("👤 شخصی", callback_data='add_cat_شخصی')],
        [InlineKeyboardButton("🏛 سیاسی", callback_data='add_cat_سیاسی')],
        [InlineKeyboardButton("💼 شغلی", callback_data='add_cat_شغلی')],
        [InlineKeyboardButton("بازگشت ⬅️", callback_data='back_to_design_menu')],
    ]
    await update.callback_query.edit_message_text("برای کدام بخش می‌خواهید سوال جدیدی طراحی کنید؟",
                                                  reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECT_ADD_CAT


async def select_political_category_for_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    subcategories = ["جمهوری اسلامی", "پهلوی", "قاجار", "عهَد باستان", "ایران پس از اسلام", "نازیسم", "کمونیسم",
                     "لیبرالیسم", "یهودیت", "ووکیسم", "سرمایه داری"]
    keyboard = [[InlineKeyboardButton(cat, callback_data=f"add_subcat_{cat}")] for cat in subcategories]
    keyboard.append([InlineKeyboardButton("بازگشت ⬅️", callback_data='back_to_add_menu')])
    await update.callback_query.edit_message_text("برای کدام زیرمجموعه سیاسی سوال اضافه می‌کنید؟",
                                                  reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECT_ADD_POLITICAL_CAT


async def prompt_for_new_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data.startswith('add_cat_'):
        category = query.data.split('_')[2]
        if category in ["شخصی", "شغلی"]:
            context.user_data.update({'design_category': category, 'design_subcategory': None})
            prompt_text = f"لطفا متن کامل سوال جدید برای بخش «{category}» را ارسال کنید."
            await query.edit_message_text(prompt_text)
            return ADDING_QUESTION_TEXT
        else:
            return await select_political_category_for_add(update, context)
    elif query.data.startswith('add_subcat_'):
        subcategory = query.data.split('_')[2]
        context.user_data.update({'design_category': "سیاسی", 'design_subcategory': subcategory})
        prompt_text = f"لطفا متن کامل سوال جدید برای بخش «سیاسی - {subcategory}» را ارسال کنید."
        await query.edit_message_text(prompt_text)
        return ADDING_QUESTION_TEXT


async def add_question_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    add_interview_question_to_db(context.user_data['design_category'], context.user_data.get('design_subcategory'),
                                 update.message.text)
    await update.message.reply_text("✅ سوال شما با موفقیت اضافه شد!")
    return await ask_add_another(update, context)


async def ask_add_another(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [[InlineKeyboardButton("✅ بله", callback_data='add_another_yes'),
                 InlineKeyboardButton("❌ خیر", callback_data='add_another_no')]]
    await update.message.reply_text("آیا می‌خواهید سوال دیگری در همین بخش اضافه کنید؟",
                                    reply_markup=InlineKeyboardMarkup(keyboard))
    return ASK_ADD_ANOTHER


async def handle_add_another(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == 'add_another_yes':
        category_name = context.user_data['design_category']
        subcategory_name = context.user_data.get('design_subcategory')
        prompt_text = f"لطفا متن سوال بعدی برای بخش «{category_name}{f' - {subcategory_name}' if subcategory_name else ''}» را ارسال کنید."
        await query.edit_message_text(prompt_text)
        return ADDING_QUESTION_TEXT
    else:
        await query.edit_message_text("عملیات طراحی سوال به پایان رسید.")
        context.user_data.clear()
        context.user_data['new_menu_message'] = True
        return await start(update, context)


async def select_category_for_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [
        [InlineKeyboardButton("👤 شخصی", callback_data='del_cat_شخصی')],
        [InlineKeyboardButton("🏛 سیاسی", callback_data='del_cat_سیاسی')],
        [InlineKeyboardButton("💼 شغلی", callback_data='del_cat_شغلی')],
        [InlineKeyboardButton("بازگشت ⬅️", callback_data='back_to_design_menu')],
    ]
    await update.callback_query.edit_message_text("از کدام بخش می‌خواهید سوالی را حذف کنید؟",
                                                  reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECT_DEL_CAT


async def select_political_category_for_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    subcategories = ["جمهوری اسلامی", "پهلوی", "قاجار", "عهَد باستان", "ایران پس از اسلام", "نازیسم", "کمونیسم",
                     "لیبرالیسم", "یهودیت", "ووکیسم", "سرمایه داری"]
    keyboard = [[InlineKeyboardButton(cat, callback_data=f"del_subcat_{cat}")] for cat in subcategories]
    keyboard.append([InlineKeyboardButton("بازگشت ⬅️", callback_data='back_to_delete_menu')])
    await update.callback_query.edit_message_text("از کدام زیرمجموعه سیاسی سوال حذف می‌کنید؟",
                                                  reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECT_DEL_POLITICAL_CAT


async def list_questions_for_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    category, subcategory = None, None
    category_display_name = ""
    if query.data.startswith('del_cat_'):
        category = query.data.split('_')[2]
        if category in ["شخصی", "شغلی"]:
            category_display_name = category
            context.user_data.update({'delete_category': category, 'delete_subcategory': None})
        else:
            return await select_political_category_for_delete(update, context)
    elif query.data.startswith('del_subcat_'):
        category = "سیاسی"
        subcategory = query.data.split('_')[2]
        category_display_name = f"سیاسی - {subcategory}"
        context.user_data.update({'delete_category': category, 'delete_subcategory': subcategory})

    questions = get_interview_questions_from_db(category, subcategory)
    context.user_data['questions_for_deletion'] = questions

    if not questions:
        await query.edit_message_text("در این بخش سوالی برای حذف وجود ندارد.", reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("بازگشت ⬅️", callback_data='back_to_delete_menu')]]))
        return SELECT_DEL_CAT

    question_list_text = f"لیست سوالات بخش «{category_display_name}»:\n\n"
    for i, (q_id, q_text) in enumerate(questions):
        question_list_text += f"{i + 1}. {q_text}\n"
    keyboard = [[InlineKeyboardButton("بازگشت به انتخاب بخش ⬅️", callback_data='back_to_delete_menu')]]
    await query.edit_message_text(f"{question_list_text}\nلطفا شماره سوالی که می‌خواهید حذف شود را ارسال کنید.",
                                  reply_markup=InlineKeyboardMarkup(keyboard))
    return LISTING_QUESTIONS_FOR_DELETE


async def delete_question_by_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        index_to_delete = int(update.message.text) - 1
        questions_for_deletion = context.user_data.get('questions_for_deletion', [])
        if 0 <= index_to_delete < len(questions_for_deletion):
            question_id_to_delete = questions_for_deletion[index_to_delete][0]
            deleted_question_text = questions_for_deletion[index_to_delete][1]
            delete_interview_question_from_db(question_id_to_delete)
            await update.message.reply_text(f"✅ سوال زیر با موفقیت حذف شد:\n'{deleted_question_text}'")

            # Refresh the list
            context.user_data['questions_for_deletion'].pop(index_to_delete)
            questions = context.user_data['questions_for_deletion']
            if not questions:
                await update.message.reply_text("دیگر سوالی در این بخش وجود ندارد.")
                return await select_category_for_delete(update, context)  # Or back to a higher menu

            question_list_text = ""
            for i, (q_id, q_text) in enumerate(questions):
                question_list_text += f"{i + 1}. {q_text}\n"
            await update.message.reply_text(
                f"{question_list_text}\nلطفا شماره سوال بعدی برای حذف را ارسال کنید یا /cancel را بزنید.")
            return LISTING_QUESTIONS_FOR_DELETE

        else:
            await update.message.reply_text("❌ شماره نامعتبر است. لطفا دوباره تلاش کنید.")
            return LISTING_QUESTIONS_FOR_DELETE
    except (ValueError, IndexError):
        await update.message.reply_text("❌ ورودی نامعتبر. لطفا فقط شماره سوال را ارسال کنید.")
        return LISTING_QUESTIONS_FOR_DELETE


# --- بخش جدید: جریان افزودن سوال آیین‌نامه ---
async def select_regulation_type_for_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("آیین‌نامه کلی", callback_data='add_reg_type_کلی')],
        [InlineKeyboardButton("آیین‌نامه جزئی", callback_data='add_reg_type_جزئی')],
        [InlineKeyboardButton("بازگشت ⬅️", callback_data='back_to_design_menu')]
    ]
    await query.edit_message_text("برای کدام آزمون آیین‌نامه سوال طراحی می‌کنید؟",
                                  reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECT_REGULATION_TYPE_FOR_ADD


async def prompt_for_regulation_question_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    test_type = query.data.split('_')[-1]
    context.user_data['new_regulation_question'] = {'test_type': test_type, 'options': []}
    await query.edit_message_text(f"✍️ لطفاً متن کامل سوال برای آزمون «{test_type}» را ارسال کنید:")
    return ADDING_REGULATION_QUESTION_TEXT


async def get_regulation_question_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_regulation_question']['question'] = update.message.text
    await update.message.reply_text("✅ سوال ثبت شد.\n\n1️⃣ لطفاً متن **گزینه اول** را ارسال کنید:")
    return ADDING_REGULATION_OPTION_1


async def get_regulation_option_1(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_regulation_question']['options'].append(update.message.text)
    await update.message.reply_text("2️⃣ لطفاً متن **گزینه دوم** را ارسال کنید:")
    return ADDING_REGULATION_OPTION_2


async def get_regulation_option_2(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_regulation_question']['options'].append(update.message.text)
    await update.message.reply_text("3️⃣ لطفاً متن **گزینه سوم** را ارسال کنید:")
    return ADDING_REGULATION_OPTION_3


async def get_regulation_option_3(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_regulation_question']['options'].append(update.message.text)
    await update.message.reply_text("4️⃣ لطفاً متن **گزینه چهارم** را ارسال کنید:")
    return ADDING_REGULATION_OPTION_4


async def get_regulation_option_4(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_regulation_question']['options'].append(update.message.text)

    question_data = context.user_data['new_regulation_question']
    options_text = ""
    keyboard_buttons = []
    for i, option in enumerate(question_data['options']):
        options_text += f"{i + 1}. {escape_html(option)}\n"
        keyboard_buttons.append([InlineKeyboardButton(f"گزینه {i + 1}", callback_data=f"select_correct_ans_{i}")])

    final_prompt = (
        f"🔍 پیش‌نمایش سوال:\n\n"
        f"<b>سوال:</b> {escape_html(question_data['question'])}\n"
        f"<b>گزینه‌ها:</b>\n{options_text}\n"
        f"❓ لطفاً **گزینه صحیح** را انتخاب کنید:"
    )

    await update.message.reply_text(
        final_prompt,
        reply_markup=InlineKeyboardMarkup(keyboard_buttons),
        parse_mode=ParseMode.HTML
    )
    return SELECTING_REGULATION_CORRECT_ANSWER


async def save_regulation_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    correct_answer_index = int(query.data.split('_')[-1])
    question_data = context.user_data['new_regulation_question']

    add_regulation_question_to_db(
        test_type=question_data['test_type'],
        question=question_data['question'],
        options=question_data['options'],
        answer=correct_answer_index
    )

    await query.edit_message_text("✅ سوال آیین‌نامه با موفقیت در پایگاه داده ذخیره شد!")

    context.user_data.clear()
    context.user_data['new_menu_message'] = True
    return await start(update, context)


# --- بخش بایگانی ---
async def archive_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("🔑 لطفا رمز عبور بخش بایگانی را وارد کنید:")
    return ARCHIVE_PASSWORD_PROMPT


async def archive_password_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text == ARCHIVE_PASSWORD:
        await update.message.reply_text("رمز عبور صحیح است. به بخش بایگانی خوش آمدید.")
        return await list_archived_users(update, context)
    else:
        await update.message.reply_text("❌ رمز عبور اشتباه است. لطفا دوباره تلاش کنید یا /cancel را بزنید.")
        return ARCHIVE_PASSWORD_PROMPT


async def list_archived_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        message = update.callback_query.message
    else:
        message = update.message

    archived_users = get_archived_users_from_db()
    if not archived_users:
        text = "بایگانی خالی است."
        keyboard = [[InlineKeyboardButton("بازگشت به منوی اصلی ⬅️", callback_data='back_to_main')]]
        await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return SELECTING_ACTION

    keyboard = [[InlineKeyboardButton(name, callback_data=f"view_user_{uid}")] for uid, name in archived_users]
    keyboard.append([InlineKeyboardButton("بازگشت به منوی اصلی ⬅️", callback_data='back_to_main')])

    text_to_send = "لطفا کاربری که می‌خواهید مصاحبه‌هایش را ببینید انتخاب کنید:"
    try:
        await message.edit_text(text_to_send, reply_markup=InlineKeyboardMarkup(keyboard))
    except:
        await message.reply_text(text_to_send, reply_markup=InlineKeyboardMarkup(keyboard))
    return LISTING_ARCHIVED_USERS


async def show_archive_user_options(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.data.split('view_user_')[1]
    context.user_data['selected_user_id'] = user_id
    keyboard = [
        [InlineKeyboardButton("شخصی", callback_data='view_cat_شخصی'),
         InlineKeyboardButton("سیاسی", callback_data='view_cat_سیاسی')],
        [InlineKeyboardButton("شغلی", callback_data='view_cat_شغلی'),
         InlineKeyboardButton("نمایش همه", callback_data='view_cat_all')],
        [InlineKeyboardButton("بازگشت ⬅️", callback_data='back_to_user_list')]
    ]
    try:
        user_name = [u[1] for u in get_archived_users_from_db() if str(u[0]) == user_id][0]
    except IndexError:
        user_name = "کاربر یافت نشد"
    await query.edit_message_text(f"کدام دسته از مصاحبه‌های کاربر «{escape_html(user_name)}» را می‌خواهید مشاهده کنید؟",
                                  reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    return SELECTING_ARCHIVE_CATEGORY


async def show_user_interviews_by_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    category_to_view = query.data.split('view_cat_')[1]
    user_id = context.user_data['selected_user_id']
    user_interviews = get_user_interviews_from_db(user_id, category_to_view)

    if not user_interviews:
        final_text = f"هیچ مصاحبه‌ای از نوع «{escape_html(category_to_view)}» برای این کاربر یافت نشد."
    else:
        final_text = "\n\n====================\n\n".join(user_interviews[:5])
        if len(user_interviews) > 5:
            final_text += f"\n\n... و {len(user_interviews) - 5} مصاحبه قدیمی‌تر."

    keyboard = [[InlineKeyboardButton("بازگشت به انتخاب دسته‌بندی ⬅️", callback_data=f'view_user_{user_id}')],
                [InlineKeyboardButton("بازگشت به لیست کاربران ⬅️", callback_data='back_to_user_list')]]

    try:
        await query.edit_message_text(final_text, parse_mode=ParseMode.HTML,
                                      reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logger.error(f"خطا در نمایش بایگانی: {e}")
        for chunk in [final_text[i:i + 4000] for i in range(0, len(final_text), 4000)]:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=chunk, parse_mode=ParseMode.HTML)
        await context.bot.send_message(chat_id=update.effective_chat.id, text="منوی ناوبری بایگانی:",
                                       reply_markup=InlineKeyboardMarkup(keyboard))
    return SHOWING_USER_INTERVIEWS


# --- بخش آزمون آیین‌نامه انجمن ---
async def show_regulations_test_options(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("آیین‌نامه کلی", callback_data='start_test_کلی')],
        [InlineKeyboardButton("آیین‌نامه جزئی", callback_data='start_test_جزئی')],
        [InlineKeyboardButton("بازگشت ⬅️", callback_data='back_to_main')]
    ]
    await query.edit_message_text("لطفا نوع آزمون آیین‌نامه را انتخاب کنید:",
                                  reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECTING_REGULATIONS_TEST_TYPE


async def regulations_test_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    test_type = query.data.split('_')[-1]
    context.user_data['test_type'] = test_type

    last_attempt = get_user_attempt_from_db(user_id, test_type)
    if last_attempt and time.time() - last_attempt < 24 * 60 * 60:
        remaining_time = 24 * 60 * 60 - (time.time() - last_attempt)
        hours, rem = divmod(remaining_time, 3600)
        minutes, _ = divmod(rem, 60)
        await query.edit_message_text(
            f"شما در آزمون قبلی «{escape_html(test_type)}» قبول نشده‌اید.\n"
            f"⏳ لطفا پس از **{int(hours)} ساعت و {int(minutes)} دقیقه** دیگر دوباره تلاش کنید.",
            parse_mode=ParseMode.HTML
        )
        context.user_data.clear()
        context.user_data['new_menu_message'] = True
        return await start(update, context)

    questions_for_test = get_regulation_questions_from_db(test_type)
    if not questions_for_test:
        await query.edit_message_text(f"در حال حاضر سوالی برای آزمون «{escape_html(test_type)}» وجود ندارد.",
                                      parse_mode=ParseMode.HTML)
        return SELECTING_ACTION

    shuffled_questions = random.sample(questions_for_test, len(questions_for_test))
    context.user_data.update({
        'regulations_test_questions': shuffled_questions, 'current_question_index': 0,
        'correct_answers': 0, 'incorrect_answers': 0, 'user_responses': []
    })
    return await ask_regulations_test_question(update, context)


async def ask_regulations_test_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    index = context.user_data['current_question_index']
    question_data = context.user_data['regulations_test_questions'][index]
    options = question_data['options'] + ["نمی‌دانم"]
    keyboard = [[InlineKeyboardButton(option, callback_data=f"rt_answer_{i}")] for i, option in enumerate(options)]
    question_text = (f"<b>سوال {index + 1} از {len(context.user_data['regulations_test_questions'])} "
                     f"(آزمون {escape_html(context.user_data['test_type'])}):</b>\n\n"
                     f"{escape_html(question_data['question'])}")
    await update.callback_query.edit_message_text(question_text, reply_markup=InlineKeyboardMarkup(keyboard),
                                                  parse_mode=ParseMode.HTML)
    return REGULATIONS_TEST_ANSWERING


async def handle_regulations_test_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    selected_option_index = int(query.data.split('_')[-1])
    index = context.user_data['current_question_index']
    question_data = context.user_data['regulations_test_questions'][index]
    correct_answer_index = question_data['answer']

    context.user_data['user_responses'].append({
        "question": question_data['question'], "options": question_data['options'],
        "user_answer_index": selected_option_index, "correct_answer_index": correct_answer_index
    })

    if selected_option_index == correct_answer_index:
        context.user_data['correct_answers'] += 1
    else:
        context.user_data['incorrect_answers'] += 1
        correct_answer_text = escape_html(question_data['options'][correct_answer_index])
        await context.bot.send_message(chat_id=query.from_user.id,
                                       text=f"❌ پاسخ شما اشتباه بود.\n<b>پاسخ صحیح:</b> {correct_answer_text}",
                                       parse_mode=ParseMode.HTML)

    context.user_data['current_question_index'] += 1
    if context.user_data['current_question_index'] < len(context.user_data['regulations_test_questions']):
        return await ask_regulations_test_question(update, context)
    else:
        return await finish_regulations_test(update, context)


async def finish_regulations_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    test_type, total_questions, correct, incorrect = (
        context.user_data['test_type'], len(context.user_data['regulations_test_questions']),
        context.user_data['correct_answers'], context.user_data['incorrect_answers']
    )

    negative_points = incorrect // 3
    final_score = max(0, ((correct - negative_points) / total_questions) * 100)
    passed = final_score >= 90

    result_text = f"--- 🏁 <b>نتیجه آزمون آیین‌نامه {escape_html(test_type)}</b> 🏁 ---\n\n" \
                  f"تعداد کل سوالات: {total_questions}\n" \
                  f"✅ پاسخ‌های صحیح: {correct}\n" \
                  f"❌ پاسخ‌های غلط/نمی‌دانم: {incorrect}\n" \
                  f"📉 نمره منفی کسر شده (معادل): {negative_points} پاسخ صحیح\n" \
                  f"💯 <b>نمره نهایی شما: {final_score:.2f}%</b>\n\n"

    if passed:
        result_text += "🎉 تبریک! شما در آزمون قبول شدید. 🎉"
        clear_user_attempt_in_db(user.id, test_type)
    else:
        result_text += "😔 متاسفانه شما در آزمون قبول نشدید. 😔\nشما تا ۲۴ ساعت آینده نمی‌توانید در این آزمون شرکت کنید."
        set_user_attempt_in_db(user.id, test_type)

    await update.callback_query.edit_message_text(result_text, parse_mode=ParseMode.HTML)

    user_first_name = escape_html(user.first_name)
    user_username = escape_html(user.username or 'N/A')
    admin_report = f"--- <b>نتیجه آزمون کاربر: {user_first_name} (@{user_username})</b> ---\n"
    admin_report += f"<b>شناسه کاربر:</b> <code>{user.id}</code>\n" + result_text + "\n\n--- <b>جزئیات پاسخ‌ها</b> ---\n"
    for i, resp in enumerate(context.user_data['user_responses']):
        user_ans = resp['options'][resp['user_answer_index']] if resp['user_answer_index'] < len(
            resp['options']) else "نمی‌دانم"
        correct_ans = resp['options'][resp['correct_answer_index']]
        admin_report += f"\n<b>{i + 1}. {escape_html(resp['question'])}</b>\n" \
                        f"   - پاسخ کاربر: {escape_html(user_ans)}\n" \
                        f"   - پاسخ صحیح: {escape_html(correct_ans)} {'✅' if user_ans == correct_ans else '❌'}\n"
    await context.bot.send_message(chat_id=ADMIN_ID, text=admin_report, parse_mode=ParseMode.HTML)

    context.user_data.clear()
    context.user_data['new_menu_message'] = True
    return await start(update, context)


# --- مدیریت خطا ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"خطا در پردازش آپدیت: {context.error}", exc_info=context.error)


# --- تابع اصلی ---
def main() -> None:
    if not BOT_TOKEN or not ADMIN_ID or not ARCHIVE_PASSWORD:
        logger.critical("یکی از متغیرهای محیطی BOT_TOKEN, ADMIN_ID, ARCHIVE_PASSWORD تعریف نشده است!")
        return
    if not DATABASE_URL:
        logger.critical("متغیر محیطی DATABASE_URL تعریف نشده است! برنامه متوقف می‌شود.")
        return

    setup_database()
    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            SELECTING_ACTION: [
                CallbackQueryHandler(show_interview_options, pattern='^interview$'),
                CallbackQueryHandler(show_design_menu, pattern='^design_question$'),
                CallbackQueryHandler(archive_start, pattern='^archive$'),
                CallbackQueryHandler(show_regulations_test_options, pattern='^regulations_test$'),
            ],
            SELECTING_INTERVIEW: [
                CallbackQueryHandler(start_questions, pattern='^(personal|job)$'),
                CallbackQueryHandler(show_political_categories, pattern='^political$'),
                CallbackQueryHandler(start, pattern='^back_to_main$'),
            ],
            SELECTING_POLITICAL_CATEGORY: [
                CallbackQueryHandler(start_questions, pattern='^political_.*$'),
                CallbackQueryHandler(show_interview_options, pattern='^back_to_interview_menu$')
            ],
            ANSWERING_QUESTIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_answer)],
            CONFIRM_SUBMISSION: [CallbackQueryHandler(confirm_submission, pattern='^confirm_.*$')],

            SELECTING_DESIGN_ACTION: [
                CallbackQueryHandler(select_category_for_add, pattern='^design_create_interview$'),
                CallbackQueryHandler(select_category_for_delete, pattern='^design_delete_interview$'),
                CallbackQueryHandler(select_regulation_type_for_add, pattern='^design_create_regulation$'),
                CallbackQueryHandler(start, pattern='^back_to_main$')
            ],
            SELECT_ADD_CAT: [
                CallbackQueryHandler(prompt_for_new_question, pattern='^add_cat_'),
                CallbackQueryHandler(show_design_menu, pattern='^back_to_design_menu$')
            ],
            SELECT_ADD_POLITICAL_CAT: [
                CallbackQueryHandler(prompt_for_new_question, pattern='^add_subcat_.*$'),
                CallbackQueryHandler(select_category_for_add, pattern='^back_to_add_menu$')
            ],
            ADDING_QUESTION_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_question_text)],
            ASK_ADD_ANOTHER: [CallbackQueryHandler(handle_add_another, pattern='^add_another_(yes|no)$')],

            SELECT_DEL_CAT: [
                CallbackQueryHandler(list_questions_for_delete, pattern='^del_cat_'),
                CallbackQueryHandler(show_design_menu, pattern='^back_to_design_menu$')
            ],
            SELECT_DEL_POLITICAL_CAT: [
                CallbackQueryHandler(list_questions_for_delete, pattern='^del_subcat_.*$'),
                CallbackQueryHandler(select_category_for_delete, pattern='^back_to_delete_menu$')
            ],
            LISTING_QUESTIONS_FOR_DELETE: [
                CallbackQueryHandler(select_category_for_delete, pattern='^back_to_delete_menu$'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, delete_question_by_number)
            ],

            ARCHIVE_PASSWORD_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, archive_password_check)],
            LISTING_ARCHIVED_USERS: [
                CallbackQueryHandler(show_archive_user_options, pattern='^view_user_'),
                CallbackQueryHandler(start, pattern='^back_to_main$'),
            ],
            SELECTING_ARCHIVE_CATEGORY: [
                CallbackQueryHandler(show_user_interviews_by_category, pattern='^view_cat_'),
                CallbackQueryHandler(list_archived_users, pattern='^back_to_user_list$'),
            ],
            SHOWING_USER_INTERVIEWS: [
                CallbackQueryHandler(show_archive_user_options, pattern=r'^view_user_.*'),
                CallbackQueryHandler(list_archived_users, pattern='^back_to_user_list$'),
            ],

            SELECTING_REGULATIONS_TEST_TYPE: [
                CallbackQueryHandler(regulations_test_start, pattern='^start_test_(کلی|جزئی)$'),
                CallbackQueryHandler(start, pattern='^back_to_main$'),
            ],
            REGULATIONS_TEST_ANSWERING: [CallbackQueryHandler(handle_regulations_test_answer, pattern='^rt_answer_')],

            SELECT_REGULATION_TYPE_FOR_ADD: [
                CallbackQueryHandler(prompt_for_regulation_question_text, pattern='^add_reg_type_'),
                CallbackQueryHandler(show_design_menu, pattern='^back_to_design_menu$')
            ],
            ADDING_REGULATION_QUESTION_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_regulation_question_text)],
            ADDING_REGULATION_OPTION_1: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_regulation_option_1)],
            ADDING_REGULATION_OPTION_2: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_regulation_option_2)],
            ADDING_REGULATION_OPTION_3: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_regulation_option_3)],
            ADDING_REGULATION_OPTION_4: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_regulation_option_4)],
            SELECTING_REGULATION_CORRECT_ANSWER: [
                CallbackQueryHandler(save_regulation_question, pattern='^select_correct_ans_')]
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        per_message=False
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(add_to_archive_handler, pattern='^archive_add_'))
    application.add_handler(CallbackQueryHandler(ignore_archive_handler, pattern='^archive_ignore_'))
    application.add_error_handler(error_handler)

    logger.info("ربات در حال اجرا است...")
    application.run_polling()


if __name__ == '__main__':
    main()