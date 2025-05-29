# main_bot.py
import os
import json # برای کار با JSON احتمالی در آینده، هرچند در این نسخه مستقیم استفاده نشده
import uuid
import logging
import mysql.connector
from mysql.connector import errorcode
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
import threading
import time

from cryptography.fernet import Fernet

import requests # برای بازآوری توکن توسط ربات

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, CallbackContext, ConversationHandler, CallbackQueryHandler
)
from dotenv import load_dotenv

# --- پیکربندی و مقداردهی اولیه ---
load_dotenv() # بارگذاری متغیرهای محیطی از فایل .env
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=os.getenv('LOG_LEVEL', 'INFO').upper() # تنظیم سطح لاگ از متغیر محیطی
)
logger = logging.getLogger(__name__)

# --- بررسی و تنظیم متغیرهای محیطی ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ENCRYPTION_KEY_STR = os.getenv('ENCRYPTION_KEY')
ADMIN_TELEGRAM_IDS_STR = os.getenv('ADMIN_TELEGRAM_IDS', '')

MYSQL_HOST = os.getenv('MYSQL_HOST')
MYSQL_USER = os.getenv('MYSQL_USER')
MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD')
MYSQL_DATABASE_NAME_ENV = os.getenv('MYSQL_DATABASE')
MYSQL_PORT = os.getenv('MYSQL_PORT', '3306')

GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID')
GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET') # برای redirect_handler و بازآوری توکن
GOOGLE_REDIRECT_URI = os.getenv('GOOGLE_REDIRECT_URI')

ENABLE_EMAIL_FETCHING = os.getenv('ENABLE_EMAIL_FETCHING', 'false').lower() == 'true'
EMAIL_FETCH_INTERVAL_SECONDS = int(os.getenv('EMAIL_FETCH_INTERVAL_SECONDS', 300))

# اعتبارسنجی متغیرهای محیطی ضروری
essential_vars = {
    "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
    "ENCRYPTION_KEY": ENCRYPTION_KEY_STR,
    "MYSQL_HOST": MYSQL_HOST,
    "MYSQL_USER": MYSQL_USER,
    "MYSQL_PASSWORD": MYSQL_PASSWORD,
    "MYSQL_DATABASE": MYSQL_DATABASE_NAME_ENV,
    "GOOGLE_CLIENT_ID": GOOGLE_CLIENT_ID,
    "GOOGLE_REDIRECT_URI": GOOGLE_REDIRECT_URI,
}
missing_vars = [var_name for var_name, var_value in essential_vars.items() if not var_value]
if missing_vars:
    logger.critical(f"Missing essential environment variables: {', '.join(missing_vars)}. Exiting.")
    exit(1)

if ENABLE_EMAIL_FETCHING and not GOOGLE_CLIENT_SECRET:
    logger.warning("ENABLE_EMAIL_FETCHING is true, but GOOGLE_CLIENT_SECRET is not set. Token refresh in the bot will fail.")

# --- تنظیمات رمزنگاری ---
try:
    cipher_suite = Fernet(ENCRYPTION_KEY_STR.encode())
except Exception as e:
    logger.critical(f"Invalid ENCRYPTION_KEY: {e}. Exiting.")
    exit(1)

def encrypt_data(data: str) -> str:
    if not data: return ""
    return cipher_suite.encrypt(data.encode()).decode()

def decrypt_data(encrypted_data: str) -> str:
    if not encrypted_data: return ""
    try:
        return cipher_suite.decrypt(encrypted_data.encode()).decode()
    except Exception as e:
        logger.error(f"Failed to decrypt data (length: {len(encrypted_data)}): {e}")
        return ""

# --- شناسه‌های ادمین ---
ADMIN_TELEGRAM_IDS = []
if ADMIN_TELEGRAM_IDS_STR:
    try:
        ADMIN_TELEGRAM_IDS = [int(admin_id.strip()) for admin_id in ADMIN_TELEGRAM_IDS_STR.split(',') if admin_id.strip()]
    except ValueError:
        logger.warning("Invalid ADMIN_TELEGRAM_IDS format. Should be comma-separated integers.")
logger.info(f"Admin IDs loaded: {ADMIN_TELEGRAM_IDS}")


# --- تنظیمات پایگاه داده (MySQL) ---
def get_db_connection(db_name=None):
    """برقراری اتصال جدید به پایگاه داده MySQL."""
    try:
        conn_params = {
            'host': MYSQL_HOST,
            'user': MYSQL_USER,
            'password': MYSQL_PASSWORD,
            'port': MYSQL_PORT,
            'autocommit': False, # مدیریت commit به صورت دستی
            'connection_timeout': 10 # اضافه کردن connection_timeout
        }
        if db_name:
            conn_params['database'] = db_name
        
        conn = mysql.connector.connect(**conn_params)
        return conn
    except mysql.connector.Error as err:
        logger.error(f"Error connecting to MySQL (database: {db_name}): {err}")
        raise

def create_database_if_not_exists():
    """تلاش برای ایجاد پایگاه داده مشخص شده در صورت عدم وجود."""
    conn = None
    cursor = None # تعریف cursor در اینجا برای دسترسی در finally
    try:
        conn = get_db_connection(db_name=None) # اتصال به سرور بدون دیتابیس خاص
        cursor = conn.cursor()
        target_db_name = MYSQL_DATABASE_NAME_ENV
        
        logger.info(f"Attempting to create database '{target_db_name}' if it does not exist...")
        # استفاده از بک‌تیک برای نام پایگاه داده
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{target_db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        conn.commit() # برای CREATE DATABASE هم commit لازم است
        logger.info(f"Database '{target_db_name}' checked/created successfully.")
        return True
    except mysql.connector.Error as err:
        logger.error(f"Could not create database '{MYSQL_DATABASE_NAME_ENV}': {err}. "
                     "This might be a permissions issue or the database server is not reachable. "
                     "The bot will try to connect assuming the database already exists.")
        if conn: conn.rollback() # اگرچه برای CREATE DATABASE معمولاً rollback معنایی ندارد
        return False
    except Exception as e:
        logger.error(f"An unexpected error occurred while trying to create database: {e}")
        if conn: conn.rollback()
        return False
    finally:
        if cursor: cursor.close()
        if conn and conn.is_connected(): conn.close()

def create_tables_in_database():
    """ایجاد جداول در پایگاه داده مشخص شده در صورت عدم وجود."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection(db_name=MYSQL_DATABASE_NAME_ENV)
        cursor = conn.cursor()
        logger.info(f"Successfully connected to MySQL database '{MYSQL_DATABASE_NAME_ENV}' for table creation.")

        # جدول کاربران
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id BIGINT PRIMARY KEY,
            username VARCHAR(255),
            is_admin BOOLEAN DEFAULT FALSE,
            subscription_expiry_timestamp BIGINT,
            max_allowed_emails INT DEFAULT 1,
            monthly_email_quota INT DEFAULT 10,
            current_month_emails_received INT DEFAULT 0,
            last_quota_reset_month VARCHAR(7)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """)
        # جدول وضعیت‌های OAuth
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS oauth_states (
            state_uuid VARCHAR(36) PRIMARY KEY,
            telegram_id BIGINT NOT NULL,
            provider VARCHAR(50) NOT NULL,
            timestamp_created BIGINT NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """)
        # جدول ایمیل‌های متصل شده با OAuth
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS connected_oauth_emails (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_telegram_id BIGINT NOT NULL,
            provider VARCHAR(50) NOT NULL,
            email_address VARCHAR(255) NOT NULL,
            encrypted_access_token TEXT,
            encrypted_refresh_token TEXT,
            token_expiry_timestamp BIGINT,
            is_active BOOLEAN DEFAULT TRUE,
            last_processed_email_marker TEXT,
            timestamp_added BIGINT NOT NULL,
            FOREIGN KEY (user_telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
            UNIQUE KEY idx_user_email_provider (user_telegram_id, email_address, provider)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """)
        conn.commit()
        logger.info(f"Database tables initialized/checked successfully in '{MYSQL_DATABASE_NAME_ENV}'.")
    except mysql.connector.Error as err:
        logger.critical(f"Failed to initialize database tables in '{MYSQL_DATABASE_NAME_ENV}': {err}. Exiting.")
        if conn: conn.rollback()
        exit(1)
    except Exception as e:
        logger.critical(f"An unexpected error occurred during table creation: {e}. Exiting.")
        if conn: conn.rollback()
        exit(1)
    finally:
        if cursor: cursor.close()
        if conn and conn.is_connected(): conn.close()

def init_db_main():
    """تابع اصلی برای مقداردهی اولیه پایگاه داده: ایجاد دیتابیس (در صورت امکان) و سپس جداول."""
    if not MYSQL_DATABASE_NAME_ENV:
        logger.critical("MYSQL_DATABASE environment variable is not set. Cannot proceed with DB initialization.")
        exit(1)
    create_database_if_not_exists() # این تابع خطاها را لاگ می‌کند اما برنامه را متوقف نمی‌کند
    create_tables_in_database() # این تابع در صورت بروز خطا، برنامه را متوقف خواهد کرد

# --- تابع کمکی برای اجرای کوئری‌های پایگاه داده ---
def db_execute(query, params=None, fetchone=False, fetchall=False, commit=False, last_row_id=False):
    """اجرای کوئری پایگاه داده. نتیجه یا شناسه آخرین ردیف را برمی‌گرداند."""
    result = None
    row_id = None
    conn = None
    cursor = None
    try:
        conn = get_db_connection(db_name=MYSQL_DATABASE_NAME_ENV)
        cursor = conn.cursor(dictionary=True if (fetchone or fetchall) else False) # dictionary=True برای دسترسی به ستون‌ها با نام
        cursor.execute(query, params)
        if commit:
            conn.commit()
        if fetchone:
            result = cursor.fetchone()
        elif fetchall:
            result = cursor.fetchall()
        if last_row_id:
            row_id = cursor.lastrowid
    except mysql.connector.Error as err:
        logger.error(f"MySQL Database error: {err} \nQuery: {query} \nParams: {params}")
        if conn: conn.rollback() # بازگرداندن تغییرات در صورت بروز خطا برای DML
    except Exception as e:
        logger.error(f"An unexpected error occurred in db_execute: {e}")
        if conn: conn.rollback()
    finally:
        if cursor: cursor.close()
        if conn and conn.is_connected(): conn.close()
    return (result, row_id) if last_row_id else result

# --- وضعیت‌های مکالمه برای دستور ادمین ---
A_TARGET_USER_ID, A_SUB_DAYS, A_MAX_EMAILS, A_MONTHLY_QUOTA = range(4)

# --- توابع کمکی (is_user_admin, check_and_create_user, check_and_reset_quota_for_user) ---
def is_user_admin(telegram_user_id: int) -> bool:
    """بررسی می‌کند که آیا کاربر ادمین است یا خیر."""
    return telegram_user_id in ADMIN_TELEGRAM_IDS

def check_and_create_user(telegram_id: int, username: str = None):
    """بررسی وجود کاربر، ایجاد در صورت عدم وجود، و به‌روزرسانی وضعیت ادمین."""
    user_row = db_execute("SELECT is_admin FROM users WHERE telegram_id = %s", (telegram_id,), fetchone=True)
    admin_flag = True if is_user_admin(telegram_id) else False # MySQL BOOLEAN can be True/False
    if not user_row:
        current_month_year_str = datetime.now(timezone.utc).strftime("%Y-%m")
        db_execute(
            "INSERT INTO users (telegram_id, username, is_admin, last_quota_reset_month, subscription_expiry_timestamp, max_allowed_emails, monthly_email_quota, current_month_emails_received) VALUES (%s, %s, %s, %s, NULL, 1, 10, 0)",
            (telegram_id, username, admin_flag, current_month_year_str), commit=True
        )
        logger.info(f"New user {telegram_id} (Admin: {admin_flag}) created.")
    elif user_row['is_admin'] != admin_flag: # is_admin در MySQL به صورت 0 یا 1 ذخیره می‌شود
        db_execute("UPDATE users SET is_admin = %s WHERE telegram_id = %s", (admin_flag, telegram_id), commit=True)
        logger.info(f"Admin status for user {telegram_id} updated to: {admin_flag}.")

def check_and_reset_quota_for_user(telegram_id: int):
    """بازنشانی سهمیه ماهانه ایمیل در صورت شروع ماه جدید."""
    user_data = db_execute("SELECT last_quota_reset_month FROM users WHERE telegram_id = %s", (telegram_id,), fetchone=True)
    now = datetime.now(timezone.utc)
    current_month_year_str = now.strftime("%Y-%m")
    if not user_data or not user_data['last_quota_reset_month'] or user_data['last_quota_reset_month'] != current_month_year_str:
        db_execute(
            "UPDATE users SET current_month_emails_received = 0, last_quota_reset_month = %s WHERE telegram_id = %s",
            (current_month_year_str, telegram_id), commit=True
        )
        logger.info(f"Initialized/Reset monthly email quota for user {telegram_id} for {current_month_year_str}")

# --- کیبورد اصلی ---
def get_main_keyboard():
    keyboard = [
        [InlineKeyboardButton("👤 حساب کاربری", callback_data='account_info')],
        [InlineKeyboardButton("🔗 اتصال ایمیل جدید (OAuth)", callback_data='connect_oauth_email_init')],
        [InlineKeyboardButton("📮 ایمیل‌های متصل من", callback_data='my_oauth_emails')],
    ]
    return InlineKeyboardMarkup(keyboard)

# --- کنترل‌کننده‌های دستورات و پاسخ‌ها ---
async def start_command(update: Update, context: CallbackContext) -> None:
    user = update.effective_user
    check_and_create_user(user.id, user.username)
    await update.message.reply_text(
        f"سلام {user.mention_markdown_v2()} عزیز!\nبه ربات مدیریت ایمیل با OAuth خوش آمدید.",
        reply_markup=get_main_keyboard(),
        parse_mode='MarkdownV2'
    )

async def account_info_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    check_and_reset_quota_for_user(user_id)
    user_data_row = db_execute(
        "SELECT username, is_admin, subscription_expiry_timestamp, max_allowed_emails, monthly_email_quota, current_month_emails_received FROM users WHERE telegram_id = %s",
        (user_id,), fetchone=True
    )
    if not user_data_row:
        await query.edit_message_text("اطلاعات کاربری یافت نشد. لطفاً /start را مجددا اجرا کنید."); return
    sub_expiry_ts = user_data_row['subscription_expiry_timestamp']
    sub_expiry_formatted = "ندارد"
    if sub_expiry_ts:
        try:
            sub_expiry_dt = datetime.fromtimestamp(sub_expiry_ts, timezone.utc)
            sub_expiry_formatted = sub_expiry_dt.strftime("%Y-%m-%d %H:%M UTC")
        except Exception: sub_expiry_formatted = "تاریخ نامعتبر"
    connected_emails_count_row = db_execute("SELECT COUNT(*) AS count FROM connected_oauth_emails WHERE user_telegram_id = %s", (user_id,), fetchone=True)
    connected_emails_count = connected_emails_count_row['count'] if connected_emails_count_row else 0
    monthly_quota_val = user_data_row['monthly_email_quota']
    message = (
        f"👤 **اطلاعات حساب کاربری**\n\n"
        f"▫️ شناسه: `{user_id}`\n"
        f"▫️ نام کاربری: @{user_data_row['username'] or 'N/A'}\n"
        f"▫️ اعتبار اشتراک: {sub_expiry_formatted}\n"
        f"▫️ ایمیل‌های متصل: {connected_emails_count} / {user_data_row['max_allowed_emails']}\n"
        f"▫️ سهمیه ماهانه: {user_data_row['current_month_emails_received']} / {monthly_quota_val if monthly_quota_val > 0 else 'نامحدود'} ایمیل\n"
        f"▫️ ادمین: {'بله' if user_data_row['is_admin'] else 'خیر'}" # is_admin در MySQL به صورت 0 یا 1 است
    )
    await query.edit_message_text(text=message, reply_markup=get_main_keyboard(), parse_mode='Markdown')

async def connect_oauth_email_init_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_limits_row = db_execute("SELECT max_allowed_emails FROM users WHERE telegram_id = %s", (user_id,), fetchone=True)
    if not user_limits_row:
        await query.edit_message_text("خطا: کاربر یافت نشد. /start را بزنید."); return
    max_allowed = user_limits_row['max_allowed_emails']
    connected_count_row = db_execute("SELECT COUNT(*) AS count FROM connected_oauth_emails WHERE user_telegram_id = %s", (user_id,), fetchone=True)
    connected_count = connected_count_row['count'] if connected_count_row else 0
    if connected_count >= max_allowed:
        await query.edit_message_text(f"شما به سقف مجاز ({max_allowed}) اتصال ایمیل رسیده‌اید."); return
    if not GOOGLE_CLIENT_ID or not GOOGLE_REDIRECT_URI:
        await query.edit_message_text("پیکربندی OAuth ناقص است. امکان اتصال وجود ندارد."); return
    oauth_state = str(uuid.uuid4())
    try:
        db_execute(
            "INSERT INTO oauth_states (state_uuid, telegram_id, provider, timestamp_created) VALUES (%s, %s, %s, %s)",
            (oauth_state, user_id, "google", int(datetime.now(timezone.utc).timestamp())), commit=True
        )
    except Exception as e:
        logger.error(f"Error storing OAuth state for user {user_id}: {e}")
        await query.edit_message_text("خطا در شروع فرآیند اتصال. لطفاً دوباره تلاش کنید."); return
    params = {
        "client_id": GOOGLE_CLIENT_ID, "redirect_uri": GOOGLE_REDIRECT_URI, "response_type": "code",
        "scope": "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/userinfo.email",
        "access_type": "offline", "prompt": "consent", "state": oauth_state
    }
    auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
    message_text = ("برای اتصال حساب Gmail خود، روی دکمه زیر کلیک کرده و مراحل را در مرورگر دنبال کنید.\n\n"
                    "پس از اعطای دسترسی در صفحه گوگل و مشاهده پیام موفقیت از طرف سرویس وب ما، "
                    "به ربات بازگشته و روی دکمه '✅ بررسی اتصال' کلیک کنید.")
    keyboard = [[InlineKeyboardButton("اتصال به گوگل (Gmail)", url=auth_url)],
                [InlineKeyboardButton("✅ بررسی اتصال و تکمیل", callback_data=f'check_oauth_done_{oauth_state}')],
                [InlineKeyboardButton("بازگشت", callback_data='back_to_main')]]
    await query.edit_message_text(text=message_text, reply_markup=InlineKeyboardMarkup(keyboard))

async def check_oauth_done_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query; await query.answer("در حال بررسی...")
    user_id = query.from_user.id
    try: original_state_from_callback = query.data.split('_')[-1]
    except IndexError: await query.edit_message_text("خطا: اطلاعات state یافت نشد.", reply_markup=get_main_keyboard()); return
    
    # سرویس redirect_uri باید state را پس از پردازش موفق حذف کند
    state_row = db_execute("SELECT telegram_id FROM oauth_states WHERE state_uuid = %s", (original_state_from_callback,), fetchone=True)
    
    newly_connected_email_address = None
    if not state_row: # اگر state وجود نداشته باشد، یعنی redirect_handler آن را پردازش و حذف کرده است
        email_row = db_execute(
            "SELECT email_address FROM connected_oauth_emails WHERE user_telegram_id = %s AND provider = %s ORDER BY timestamp_added DESC LIMIT 1",
            (user_id, "google"), fetchone=True
        )
        if email_row: newly_connected_email_address = email_row['email_address']
    
    if newly_connected_email_address:
        await query.edit_message_text(f"اتصال ایمیل {newly_connected_email_address} با موفقیت در سیستم ثبت شد!", reply_markup=get_main_keyboard())
    else:
        message_text = ("به نظر می‌رسد فرآیند اتصال هنوز کامل نشده یا مشکلی رخ داده است.\n"
                        "لطفاً مطمئن شوید که در مرورگر دسترسی لازم را اعطا کرده و پیام موفقیت را از سرویس وب ما دیده‌اید.\n"
                        "سپس دوباره دکمه 'بررسی اتصال' را بزنید.")
        if state_row: # اگر state هنوز وجود دارد
             message_text += "\n(راهنمایی: فرآیند در سمت وب کامل نشده یا سرویس redirect با خطا مواجه شده است.)"
        keyboard = [[InlineKeyboardButton("🔁 تلاش مجدد برای بررسی", callback_data=f'check_oauth_done_{original_state_from_callback}')],
                    [InlineKeyboardButton("شروع مجدد اتصال", callback_data='connect_oauth_email_init')],
                    [InlineKeyboardButton("منوی اصلی", callback_data='back_to_main')]]
        await query.edit_message_text(text=message_text, reply_markup=InlineKeyboardMarkup(keyboard))

async def my_oauth_emails_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query; await query.answer()
    user_id = query.from_user.id
    accounts_rows = db_execute("SELECT id, email_address, provider, is_active FROM connected_oauth_emails WHERE user_telegram_id = %s", (user_id,), fetchall=True)
    if not accounts_rows:
        await query.edit_message_text("شما هیچ حساب ایمیلی با OAuth متصل نکرده‌اید.", reply_markup=get_main_keyboard()); return
    keyboard = []
    for acc_row in accounts_rows:
        acc_id, email_addr, provider, is_active_db = acc_row['id'], acc_row['email_address'], acc_row['provider'], bool(acc_row['is_active'])
        status_emoji, toggle_text = ("✅", "غیرفعال کردن دریافت") if is_active_db else ("❌", "فعال کردن دریافت")
        keyboard.extend([
            [InlineKeyboardButton(f"{status_emoji} {email_addr} ({provider.capitalize()})", callback_data=f"noop_{acc_id}")],
            [InlineKeyboardButton(toggle_text, callback_data=f"toggle_email_{acc_id}"),
             InlineKeyboardButton("🗑️ قطع اتصال", callback_data=f"disconnect_email_{acc_id}")]
        ])
        if len(accounts_rows) > 1 and acc_row != accounts_rows[-1]: # جداکننده بین آیتم‌ها
             keyboard.append([InlineKeyboardButton(" ", callback_data=f"noop_sep_{acc_id}")])
    keyboard.append([InlineKeyboardButton("بازگشت به منوی اصلی", callback_data='back_to_main')])
    await query.edit_message_text("ایمیل‌های متصل شما (OAuth):", reply_markup=InlineKeyboardMarkup(keyboard))

async def toggle_email_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query; await query.answer()
    user_id = query.from_user.id
    email_db_id = int(query.data.split('_')[-1])
    current_status_row = db_execute(
        "SELECT is_active, email_address FROM connected_oauth_emails WHERE id = %s AND user_telegram_id = %s",
        (email_db_id, user_id), fetchone=True
    )
    if not current_status_row: await query.message.reply_text("خطا: ایمیل یافت نشد یا متعلق به شما نیست."); return
    new_status_bool = not bool(current_status_row['is_active'])
    db_execute("UPDATE connected_oauth_emails SET is_active = %s WHERE id = %s", (new_status_bool, email_db_id), commit=True)
    status_text = "فعال" if new_status_bool else "غیرفعال"
    await query.message.reply_text(f"دریافت ایمیل برای {current_status_row['email_address']} {status_text} شد.")
    await my_oauth_emails_callback(update, context) # به‌روزرسانی لیست

async def disconnect_email_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query; await query.answer()
    user_id = query.from_user.id
    email_db_id = int(query.data.split('_')[-1])
    email_data_row = db_execute(
        "SELECT email_address FROM connected_oauth_emails WHERE id = %s AND user_telegram_id = %s",
        (email_db_id, user_id), fetchone=True
    )
    if not email_data_row: await query.message.reply_text("خطا: ایمیل یافت نشد یا متعلق به شما نیست."); return
    db_execute("DELETE FROM connected_oauth_emails WHERE id = %s", (email_db_id,), commit=True)
    await query.message.reply_text(f"اتصال ایمیل {email_data_row['email_address']} با موفقیت قطع شد.")
    await my_oauth_emails_callback(update, context) # به‌روزرسانی لیست

async def back_to_main_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query; await query.answer()
    await query.edit_message_text("منوی اصلی:", reply_markup=get_main_keyboard())

# --- دستور ادمین: /set_subscription ---
async def set_subscription_command(update: Update, context: CallbackContext) -> int:
    user_id = update.effective_user.id
    if not is_user_admin(user_id):
        await update.message.reply_text("شما اجازه استفاده از این دستور را ندارید."); return ConversationHandler.END
    await update.message.reply_text("لطفاً شناسه عددی کاربر تلگرام مورد نظر را وارد کنید:", reply_markup=ForceReply(selective=True, input_field_placeholder="شناسه عددی کاربر"))
    return A_TARGET_USER_ID

async def received_target_user_id(update: Update, context: CallbackContext) -> int:
    try:
        target_user_id = int(update.message.text)
        check_and_create_user(target_user_id, f"User_{target_user_id}") # اطمینان از وجود کاربر در دیتابیس
        context.user_data['target_user_id'] = target_user_id
        await update.message.reply_text("مدت زمان اشتراک به روز (مثلاً 30، 90، 365) یا 0 برای حذف/نامحدود وارد کنید:", reply_markup=ForceReply(selective=True, input_field_placeholder="تعداد روز (0 برای نامحدود)"))
        return A_SUB_DAYS
    except ValueError:
        await update.message.reply_text("شناسه کاربر باید یک عدد باشد. لطفاً دوباره تلاش کنید یا /cancel بزنید."); return A_TARGET_USER_ID

async def received_subscription_days(update: Update, context: CallbackContext) -> int:
    try:
        days = int(update.message.text); context.user_data['subscription_days'] = days
        await update.message.reply_text("حداکثر تعداد ایمیل قابل اتصال (مثلاً 1، 3، 5) را وارد کنید:", reply_markup=ForceReply(selective=True, input_field_placeholder="تعداد ایمیل مجاز"))
        return A_MAX_EMAILS
    except ValueError: await update.message.reply_text("تعداد روز باید عدد باشد. لطفاً دوباره تلاش کنید یا /cancel بزنید."); return A_SUB_DAYS

async def received_max_emails(update: Update, context: CallbackContext) -> int:
    try:
        max_e = int(update.message.text)
        if max_e < 0: raise ValueError("Max emails cannot be negative")
        context.user_data['max_allowed_emails'] = max_e
        await update.message.reply_text("سهمیه ماهانه دریافت ایمیل (مثلاً 100، 500، یا 0 برای نامحدود) را وارد کنید:", reply_markup=ForceReply(selective=True, input_field_placeholder="سهمیه ماهانه (0 برای نامحدود)"))
        return A_MONTHLY_QUOTA
    except ValueError: await update.message.reply_text("تعداد ایمیل باید عدد صحیح غیرمنفی باشد. /cancel"); return A_MAX_EMAILS

async def received_monthly_quota(update: Update, context: CallbackContext) -> int:
    try:
        quota = int(update.message.text)
        if quota < 0: raise ValueError("Quota cannot be negative")
        target_user_id = context.user_data['target_user_id']
        sub_days = context.user_data['subscription_days']
        max_allowed_emails = context.user_data['max_allowed_emails']
        monthly_q = quota
        new_expiry_timestamp = None
        if sub_days > 0:
            new_expiry_timestamp = int((datetime.now(timezone.utc) + timedelta(days=sub_days)).timestamp())
        db_execute(
            "UPDATE users SET subscription_expiry_timestamp = %s, max_allowed_emails = %s, monthly_email_quota = %s WHERE telegram_id = %s",
            (new_expiry_timestamp, max_allowed_emails, monthly_q, target_user_id), commit=True
        )
        expiry_text = f"تا {datetime.fromtimestamp(new_expiry_timestamp, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}" if new_expiry_timestamp else "نامحدود/حذف شد"
        await update.message.reply_text(
            f"✅ اشتراک کاربر {target_user_id} به‌روزرسانی شد:\n"
            f"▫️ انقضا: {expiry_text}\n"
            f"▫️ حداکثر ایمیل متصل: {max_allowed_emails}\n"
            f"▫️ سهمیه ماهانه: {monthly_q if monthly_q > 0 else 'نامحدود'}"
        )
    except ValueError: await update.message.reply_text("سهمیه ماهانه باید عدد صحیح غیرمنفی باشد. /cancel"); return A_MONTHLY_QUOTA
    except Exception as e:
        logger.error(f"Error updating subscription for {context.user_data.get('target_user_id')}: {e}")
        await update.message.reply_text(f"خطا در به‌روزرسانی اشتراک: {e}")
    finally: context.user_data.clear()
    return ConversationHandler.END

async def cancel_admin_conversation(update: Update, context: CallbackContext) -> int:
    user = update.effective_user
    if is_user_admin(user.id): await update.message.reply_text("عملیات ادمین لغو شد.")
    context.user_data.clear()
    return ConversationHandler.END

# --- واکشی ایمیل در پس‌زمینه (مفهومی و بازآوری توکن) ---
def refresh_google_token_if_needed(user_telegram_id: int, account_db_id: int) -> str | None:
    """بازآوری توکن دسترسی گوگل با استفاده از توکن بازآوری ذخیره شده در دیتابیس."""
    account_row = db_execute(
        "SELECT encrypted_refresh_token, email_address FROM connected_oauth_emails WHERE id = %s AND user_telegram_id = %s",
        (account_db_id, user_telegram_id), fetchone=True
    )
    if not account_row or not account_row['encrypted_refresh_token']:
        logger.warning(f"No refresh token found for user {user_telegram_id}, account_id {account_db_id} to refresh.")
        return None
    refresh_token = decrypt_data(account_row['encrypted_refresh_token'])
    email_address = account_row['email_address']
    if not refresh_token:
        logger.error(f"Failed to decrypt refresh token for user {user_telegram_id}, email {email_address}.")
        return None
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        logger.error("Google Client ID or Secret not configured for token refresh."); return None
    token_uri = "https://oauth2.googleapis.com/token"
    payload = {
        'client_id': GOOGLE_CLIENT_ID, 'client_secret': GOOGLE_CLIENT_SECRET,
        'refresh_token': refresh_token, 'grant_type': 'refresh_token'
    }
    try:
        logger.info(f"Attempting to refresh token for user {user_telegram_id}, email {email_address}")
        response = requests.post(token_uri, data=payload, timeout=10)
        response.raise_for_status()
        token_data = response.json()
        new_access_token, new_expires_in = token_data.get('access_token'), token_data.get('expires_in')
        if not new_access_token or new_expires_in is None:
            logger.error(f"Failed to get new access token from refresh response for {email_address}: {token_data}"); return None
        new_encrypted_access_token = encrypt_data(new_access_token)
        new_token_expiry_timestamp = int(datetime.now(timezone.utc).timestamp()) + new_expires_in
        db_execute(
            "UPDATE connected_oauth_emails SET encrypted_access_token = %s, token_expiry_timestamp = %s WHERE id = %s",
            (new_encrypted_access_token, new_token_expiry_timestamp, account_db_id), commit=True
        )
        logger.info(f"Successfully refreshed access token for user {user_telegram_id}, email {email_address}")
        return new_access_token # برگرداندن توکن جدید رمزگشایی شده
    except requests.exceptions.RequestException as e:
        logger.error(f"HTTP error during token refresh for {email_address}: {e}")
        if e.response is not None:
            logger.error(f"Refresh token error response: {e.response.text}")
            if "invalid_grant" in e.response.text.lower() or "token has been expired or revoked" in e.response.text.lower():
                logger.warning(f"Refresh token for {email_address} is invalid/revoked. Disabling account.")
                db_execute("UPDATE connected_oauth_emails SET is_active = FALSE WHERE id = %s", (account_db_id,), commit=True)
        return None
    except Exception as e: logger.error(f"Unexpected error during token refresh for {email_address}: {e}"); return None

def fetch_emails_for_account(user_telegram_id: int, account_details: dict, bot_instance_ref):
    """مفهومی: واکشی ایمیل‌ها برای یک حساب متصل شده با OAuth."""
    email_address = account_details['email_address']
    account_db_id = account_details['id']
    logger.info(f"Checking emails for user {user_telegram_id}, account {email_address} (ID: {account_db_id})")
    user_subs_data = db_execute(
        "SELECT monthly_email_quota, current_month_emails_received FROM users WHERE telegram_id = %s",
        (user_telegram_id,), fetchone=True
    )
    if not user_subs_data: logger.warning(f"User {user_telegram_id} not found for email fetching."); return
    monthly_quota, received_this_month = user_subs_data['monthly_email_quota'], user_subs_data['current_month_emails_received']
    if monthly_quota > 0 and received_this_month >= monthly_quota:
        logger.info(f"User {user_telegram_id} reached monthly quota ({received_this_month}/{monthly_quota}). Skipping fetch for {email_address}."); return
    current_ts = int(datetime.now(timezone.utc).timestamp())
    access_token = decrypt_data(account_details['encrypted_access_token'])
    if not access_token or account_details['token_expiry_timestamp'] <= current_ts + 120: # بازآوری اگر منقضی شده یا تا 2 دقیقه دیگر منقضی می‌شود
        logger.info(f"Access token for {email_address} expired or needs refresh. Attempting.")
        access_token = refresh_google_token_if_needed(user_telegram_id, account_db_id)
    if not access_token:
        logger.warning(f"No valid access token for {email_address} after attempting refresh. Skipping fetch."); return
    if account_details['provider'] == 'google':
        try:
            # --- منطق واقعی تعامل با Gmail API در اینجا قرار می‌گیرد ---
            # از کتابخانه google-api-python-client استفاده کنید
            logger.info(f"CONCEPTUAL: Would fetch emails for {email_address} using token. (Not implemented)")
            # مثال:
            # creds = Credentials(token=access_token)
            # service = build('gmail', 'v1', credentials=creds)
            # results = service.users().messages().list(userId='me', q='is:unread', maxResults=5).execute()
            # messages = results.get('messages', [])
            # for msg_summary in messages:
            #     # پردازش هر پیام، ارسال به کاربر، به‌روزرسانی سهمیه و last_processed_email_marker
            #     pass
        except Exception as e:
            logger.error(f"Error fetching Google emails for {email_address} (User: {user_telegram_id}): {e}")
            # مدیریت خطاهای خاص API، مثلاً اگر توکن نامعتبر شد، حساب را غیرفعال کنید

def email_check_loop(application: Application):
    """به صورت دوره‌ای ایمیل‌ها را برای تمام حساب‌های فعال با اشتراک معتبر بررسی می‌کند."""
    bot_instance_ref = application.bot
    while True:
        logger.info("Starting email check cycle...")
        try:
            current_timestamp = int(datetime.now(timezone.utc).timestamp())
            active_accounts_rows = db_execute(
                """SELECT coe.* FROM connected_oauth_emails coe
                   JOIN users u ON coe.user_telegram_id = u.telegram_id
                   WHERE coe.is_active = TRUE 
                     AND (u.subscription_expiry_timestamp IS NULL OR u.subscription_expiry_timestamp > %s)""",
                (current_timestamp,), fetchall=True
            )
            if active_accounts_rows:
                logger.info(f"Found {len(active_accounts_rows)} active email accounts to check.")
                for acc_row in active_accounts_rows:
                    check_and_reset_quota_for_user(acc_row['user_telegram_id'])
                    fetch_emails_for_account(acc_row['user_telegram_id'], acc_row, bot_instance_ref)
            else: logger.info("No active email accounts with valid subscriptions to check.")
        except Exception as e: logger.error(f"Error in email_check_loop: {e}")
        logger.info(f"Email check cycle finished. Sleeping for {EMAIL_FETCH_INTERVAL_SECONDS} seconds.")
        time.sleep(EMAIL_FETCH_INTERVAL_SECONDS)

def run_bot():
    """ربات را راه‌اندازی و اجرا می‌کند."""
    # مقداردهی اولیه پایگاه داده در ابتدای اجرای ربات
    init_db_main()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # کنترل‌کننده مکالمه برای دستور ادمین
    admin_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("set_subscription", set_subscription_command, filters=filters.ChatType.PRIVATE)],
        states={
            A_TARGET_USER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, received_target_user_id)],
            A_SUB_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, received_subscription_days)],
            A_MAX_EMAILS: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, received_max_emails)],
            A_MONTHLY_QUOTA: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, received_monthly_quota)],
        },
        fallbacks=[CommandHandler('cancel', cancel_admin_conversation, filters=filters.ChatType.PRIVATE)],
    )

    application.add_handler(CommandHandler("start", start_command, filters=filters.ChatType.PRIVATE))
    application.add_handler(admin_conv_handler)

    # کنترل‌کننده‌های پاسخ به دکمه‌های شیشه‌ای
    application.add_handler(CallbackQueryHandler(account_info_callback, pattern='^account_info$'))
    application.add_handler(CallbackQueryHandler(connect_oauth_email_init_callback, pattern='^connect_oauth_email_init$'))
    application.add_handler(CallbackQueryHandler(check_oauth_done_callback, pattern='^check_oauth_done_'))
    application.add_handler(CallbackQueryHandler(my_oauth_emails_callback, pattern='^my_oauth_emails$'))
    application.add_handler(CallbackQueryHandler(toggle_email_callback, pattern='^toggle_email_'))
    application.add_handler(CallbackQueryHandler(disconnect_email_callback, pattern='^disconnect_email_'))
    application.add_handler(CallbackQueryHandler(back_to_main_callback, pattern='^back_to_main$'))
    application.add_handler(CallbackQueryHandler(lambda u,c: u.callback_query.answer("این دکمه عملیاتی ندارد."), pattern='^noop_')) # برای جداکننده‌ها و غیره

    # شروع نخ واکشی ایمیل در پس‌زمینه (در صورت فعال بودن)
    if ENABLE_EMAIL_FETCHING:
        email_thread = threading.Thread(target=email_check_loop, args=(application,), daemon=True)
        email_thread.start()
        logger.info("Email fetching thread started.")
    else:
        logger.info("Email fetching is disabled via ENABLE_EMAIL_FETCHING environment variable.")

    logger.info("Bot starting to poll...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    run_bot()
