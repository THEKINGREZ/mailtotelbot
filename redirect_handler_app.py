# redirect_handler_app.py
import os
import logging
from flask import Flask, request, redirect as flask_redirect, render_template_string
import requests # برای تبادل کد با توکن
from urllib.parse import urljoin
import mysql.connector
from mysql.connector import errorcode
from datetime import datetime, timezone
from cryptography.fernet import Fernet # برای رمزنگاری توکن‌ها
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# --- پیکربندی لاگ برای Flask ---
if __name__ != '__main__': # اگر توسط Gunicorn یا مشابه اجرا شود
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
else: # اگر به صورت مستقیم اجرا شود
    logging.basicConfig(level=logging.INFO)


# --- متغیرهای محیطی و تنظیمات ---
MYSQL_HOST = os.getenv('MYSQL_HOST')
MYSQL_USER = os.getenv('MYSQL_USER')
MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD')
MYSQL_DATABASE_NAME_ENV = os.getenv('MYSQL_DATABASE')
MYSQL_PORT = os.getenv('MYSQL_PORT', '3306')

GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID')
GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET')
# GOOGLE_REDIRECT_URI باید با آدرسی که این اپلیکیشن Flask در آن اجرا می‌شود، مطابقت داشته باشد
# و همچنین با آنچه در کنسول گوگل تنظیم شده است.
# مثال: http://localhost:5000/oauth2callback یا https://yourdomain.com/oauth2callback
# این مقدار باید از طریق متغیر محیطی به ربات اصلی (main_bot.py) نیز داده شود.
CURRENT_APP_REDIRECT_URI = os.getenv('GOOGLE_REDIRECT_URI') # آدرس همین اپلیکیشن

ENCRYPTION_KEY_STR = os.getenv('ENCRYPTION_KEY')
if not ENCRYPTION_KEY_STR:
    app.logger.critical("ENCRYPTION_KEY not set for redirect handler. Exiting.")
    exit(1)
try:
    cipher_suite = Fernet(ENCRYPTION_KEY_STR.encode())
except Exception as e:
    app.logger.critical(f"Invalid ENCRYPTION_KEY for redirect handler: {e}. Exiting.")
    exit(1)

def encrypt_data_rh(data: str) -> str: # rh for redirect_handler to avoid name clash if in same process
    if not data: return ""
    return cipher_suite.encrypt(data.encode()).decode()

# --- توابع کمکی پایگاه داده ---
def get_db_connection_rh():
    try:
        conn = mysql.connector.connect(
            host=MYSQL_HOST, user=MYSQL_USER, password=MYSQL_PASSWORD,
            database=MYSQL_DATABASE_NAME_ENV, port=MYSQL_PORT, autocommit=False
        )
        return conn
    except mysql.connector.Error as err:
        app.logger.error(f"RedirectHandler: Error connecting to MySQL: {err}")
        raise

def db_execute_rh(query, params=None, fetchone=False, commit=False):
    result = None; conn = None; cursor = None
    try:
        conn = get_db_connection_rh()
        cursor = conn.cursor(dictionary=True if fetchone else False)
        cursor.execute(query, params)
        if commit: conn.commit()
        if fetchone: result = cursor.fetchone()
    except mysql.connector.Error as err:
        app.logger.error(f"RedirectHandler: DB error: {err} \nQuery: {query} \nParams: {params}")
        if conn: conn.rollback()
    finally:
        if cursor: cursor.close()
        if conn and conn.is_connected(): conn.close()
    return result

# --- قالب‌های HTML ساده برای نمایش پیام به کاربر ---
SUCCESS_PAGE_TEMPLATE = """
<!DOCTYPE html><html lang="fa" dir="rtl"><head><meta charset="UTF-8"><title>اتصال موفق</title>
<style>body{font-family: sans-serif; display: flex; justify-content: center; align-items: center; height: 90vh; background-color: #f4f7f6; margin: 0;} .container{text-align: center; padding: 30px; background-color: white; border-radius: 8px; box-shadow: 0 4px 8px rgba(0,0,0,0.1);} h1{color: #4CAF50;} p{color: #333; font-size: 1.1em;}</style></head>
<body><div class="container"><h1>✅ اتصال موفقیت آمیز بود!</h1><p>ایمیل <strong>{{ email }}</strong> با موفقیت به ربات تلگرام شما متصل شد.</p><p>اکنون می‌توانید این پنجره را ببندید و به ربات در تلگرام بازگردید.</p></div></body></html>
"""
ERROR_PAGE_TEMPLATE = """
<!DOCTYPE html><html lang="fa" dir="rtl"><head><meta charset="UTF-8"><title>خطا در اتصال</title>
<style>body{font-family: sans-serif; display: flex; justify-content: center; align-items: center; height: 90vh; background-color: #f4f7f6; margin: 0;} .container{text-align: center; padding: 30px; background-color: white; border-radius: 8px; box-shadow: 0 4px 8px rgba(0,0,0,0.1);} h1{color: #F44336;} p{color: #333; font-size: 1.1em;}</style></head>
<body><div class="container"><h1>❌ خطا در اتصال</h1><p>{{ error_message }}</p><p>لطفاً دوباره از طریق ربات تلگرام تلاش کنید یا با پشتیبانی تماس بگیرید.</p></div></body></html>
"""

@app.route('/oauth2callback') # این مسیر باید با GOOGLE_REDIRECT_URI شما مطابقت داشته باشد
def oauth2callback():
    state_from_google = request.args.get('state')
    code_from_google = request.args.get('code')
    error_from_google = request.args.get('error')

    if error_from_google:
        app.logger.error(f"OAuth Error from Google: {error_from_google}")
        return render_template_string(ERROR_PAGE_TEMPLATE, error_message=f"گوگل خطایی را برگرداند: {error_from_google}"), 400

    if not state_from_google or not code_from_google:
        app.logger.error("OAuth callback missing state or code.")
        return render_template_string(ERROR_PAGE_TEMPLATE, error_message="پاسخ ناقص از سرویس احراز هویت دریافت شد."), 400

    # 1. اعتبارسنجی state و دریافت شناسه کاربر تلگرام
    state_data_row = db_execute_rh("SELECT telegram_id, provider FROM oauth_states WHERE state_uuid = %s", (state_from_google,), fetchone=True)
    if not state_data_row:
        app.logger.error(f"Invalid or expired OAuth state received: {state_from_google}")
        return render_template_string(ERROR_PAGE_TEMPLATE, error_message="وضعیت (state) احراز هویت نامعتبر یا منقضی شده است."), 400
    
    user_telegram_id = state_data_row['telegram_id']
    provider = state_data_row['provider'] # باید "google" باشد

    # 2. تبادل authorization_code با access_token و refresh_token
    token_url = "https://oauth2.googleapis.com/token"
    token_payload = {
        'code': code_from_google,
        'client_id': GOOGLE_CLIENT_ID,
        'client_secret': GOOGLE_CLIENT_SECRET,
        'redirect_uri': CURRENT_APP_REDIRECT_URI, # باید دقیقاً با آنچه در کنسول گوگل ثبت شده مطابقت داشته باشد
        'grant_type': 'authorization_code'
    }
    try:
        token_response = requests.post(token_url, data=token_payload, timeout=10)
        token_response.raise_for_status() # بررسی خطاهای HTTP
        tokens = token_response.json()
        
        access_token = tokens.get('access_token')
        refresh_token = tokens.get('refresh_token') # برای گوگل، refresh_token فقط در اولین بار ارسال می‌شود
        expires_in = tokens.get('expires_in') # معمولاً 3600 ثانیه

        if not access_token:
            app.logger.error(f"Access token not found in Google's response for user {user_telegram_id}. Response: {tokens}")
            return render_template_string(ERROR_PAGE_TEMPLATE, error_message="توکن دسترسی از گوگل دریافت نشد."), 500

    except requests.exceptions.RequestException as e:
        app.logger.error(f"Error exchanging code for token for user {user_telegram_id}: {e}")
        if hasattr(e, 'response') and e.response is not None:
            app.logger.error(f"Token exchange error response: {e.response.text}")
        return render_template_string(ERROR_PAGE_TEMPLATE, error_message="خطا در تبادل کد با توکن."), 500
    except Exception as e:
        app.logger.error(f"Unexpected error during token exchange for user {user_telegram_id}: {e}")
        return render_template_string(ERROR_PAGE_TEMPLATE, error_message="خطای پیش‌بینی نشده در سرور."), 500

    # 3. دریافت اطلاعات کاربر (ایمیل) با استفاده از access_token
    user_info_url = "https://www.googleapis.com/oauth2/v1/userinfo"
    headers = {'Authorization': f'Bearer {access_token}'}
    try:
        user_info_response = requests.get(user_info_url, headers=headers, timeout=10)
        user_info_response.raise_for_status()
        user_info = user_info_response.json()
        user_email = user_info.get('email')

        if not user_email:
            app.logger.error(f"Email not found in user_info for user {user_telegram_id}. Response: {user_info}")
            return render_template_string(ERROR_PAGE_TEMPLATE, error_message="ایمیل کاربر از گوگل دریافت نشد."), 500
            
    except requests.exceptions.RequestException as e:
        app.logger.error(f"Error fetching user info for user {user_telegram_id}: {e}")
        return render_template_string(ERROR_PAGE_TEMPLATE, error_message="خطا در دریافت اطلاعات کاربر از گوگل."), 500
    except Exception as e:
        app.logger.error(f"Unexpected error during user info fetch for user {user_telegram_id}: {e}")
        return render_template_string(ERROR_PAGE_TEMPLATE, error_message="خطای پیش‌بینی نشده در سرور."), 500

    # 4. ذخیره توکن‌های رمزنگاری شده و ایمیل کاربر در پایگاه داده
    encrypted_access_token = encrypt_data_rh(access_token)
    encrypted_refresh_token = encrypt_data_rh(refresh_token) if refresh_token else None # refresh_token ممکن است null باشد

    token_expiry_timestamp = int(datetime.now(timezone.utc).timestamp()) + expires_in if expires_in else None
    timestamp_added = int(datetime.now(timezone.utc).timestamp())

    try:
        # استفاده از INSERT ... ON DUPLICATE KEY UPDATE برای مدیریت اتصال مجدد همان ایمیل
        # این کوئری فرض می‌کند که UNIQUE KEY (user_telegram_id, email_address, provider) روی جدول وجود دارد
        insert_query = """
            INSERT INTO connected_oauth_emails 
            (user_telegram_id, provider, email_address, encrypted_access_token, encrypted_refresh_token, token_expiry_timestamp, is_active, timestamp_added)
            VALUES (%s, %s, %s, %s, %s, %s, TRUE, %s)
            ON DUPLICATE KEY UPDATE
            encrypted_access_token = VALUES(encrypted_access_token),
            encrypted_refresh_token = IF(VALUES(encrypted_refresh_token) IS NOT NULL, VALUES(encrypted_refresh_token), encrypted_refresh_token), -- فقط اگر توکن بازآوری جدیدی وجود دارد، آن را به‌روز کن
            token_expiry_timestamp = VALUES(token_expiry_timestamp),
            is_active = TRUE,
            timestamp_added = VALUES(timestamp_added)
        """
        params = (
            user_telegram_id, provider, user_email, 
            encrypted_access_token, encrypted_refresh_token, 
            token_expiry_timestamp, timestamp_added
        )
        db_execute_rh(insert_query, params, commit=True)
        app.logger.info(f"Successfully stored/updated OAuth tokens for user {user_telegram_id}, email {user_email}")

        # 5. حذف state استفاده شده از پایگاه داده
        db_execute_rh("DELETE FROM oauth_states WHERE state_uuid = %s", (state_from_google,), commit=True)
        app.logger.info(f"Deleted OAuth state: {state_from_google}")

        return render_template_string(SUCCESS_PAGE_TEMPLATE, email=user_email)

    except Exception as e: # گرفتن خطاهای پایگاه داده یا رمزنگاری
        app.logger.error(f"Error saving tokens or deleting state for user {user_telegram_id}, email {user_email}: {e}")
        return render_template_string(ERROR_PAGE_TEMPLATE, error_message="خطا در ذخیره‌سازی اطلاعات اتصال در سرور."), 500

if __name__ == '__main__':
    # این بخش برای اجرای مستقیم Flask برای تست است.
    # در محیط پروداکشن، از Gunicorn یا مشابه استفاده کنید.
    # مطمئن شوید که GOOGLE_REDIRECT_URI با آدرس این سرور Flask مطابقت دارد.
    # مثال: اگر این را در لوکال اجرا می‌کنید، GOOGLE_REDIRECT_URI باید http://localhost:5000/oauth2callback باشد.
    app.run(debug=True, port=5000)
