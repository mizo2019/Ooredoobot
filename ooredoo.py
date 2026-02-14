import logging
import requests
import json
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup 
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters 
import sys 
import time 
import hashlib 
import hmac 
import sqlite3 
import datetime 
import uuid 
import os
DBNAME = os.environ.get("DBNAME", "/data/botusers.db")
import os
TELEGRAMBOTTOKEN = os.environ["TELEGRAMBOTTOKEN"]
import base64
import random
from datetime import datetime as dt_class

# --- CONFIGURATION ---


# --- LOGGING ---
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO) 
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# --- DATABASE ---
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            phone_number TEXT,
            access_token TEXT,
            refresh_token TEXT,
            token_expires_in INTEGER,
            last_updated TEXT,
            device_uuid TEXT,
            instant_id TEXT,
            plan_type TEXT,
            last_played_time TEXT
        )
    ''')
    cursor.execute("PRAGMA table_info(users)")
    cols = [c[1] for c in cursor.fetchall()]
    if 'device_uuid' not in cols:
        cursor.execute("ALTER TABLE users ADD COLUMN device_uuid TEXT")
    if 'instant_id' not in cols:
        cursor.execute("ALTER TABLE users ADD COLUMN instant_id TEXT")
    if 'plan_type' not in cols:
        cursor.execute("ALTER TABLE users ADD COLUMN plan_type TEXT")
    if 'last_played_time' not in cols:
        cursor.execute("ALTER TABLE users ADD COLUMN last_played_time TEXT")
    conn.commit()
    conn.close()

def generate_synced_instant_id():
    u = uuid.uuid1()
    ts_100ns = u.time
    ts_ms = int((ts_100ns - 0x01b21dd213814000) / 10000)
    ts_str = str(ts_ms).ljust(13, '0') 
    return f"{u}{ts_str}"

def get_or_create_device_info(chat_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('SELECT device_uuid, instant_id FROM users WHERE chat_id=?', (chat_id,))
    row = c.fetchone()
    
    instant_id = None
    if row: instant_id = row[1]
    
    updated = False
    if not instant_id or len(instant_id) != 49:
        instant_id = generate_synced_instant_id()
        updated = True
    
    device_uuid = instant_id[:36]

    if updated:
        c.execute('SELECT chat_id FROM users WHERE chat_id=?', (chat_id,))
        if c.fetchone():
            c.execute('UPDATE users SET device_uuid=?, instant_id=? WHERE chat_id=?', (device_uuid, instant_id, chat_id))
        else:
            c.execute('INSERT INTO users (chat_id, device_uuid, instant_id, last_updated) VALUES (?,?,?,?)', 
                      (chat_id, device_uuid, instant_id, dt_class.now().isoformat()))
        conn.commit()
        
    conn.close()
    return device_uuid, instant_id

def save_user_data(chat_id, phone, access, refresh, expires):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    now = dt_class.now().isoformat()
    try:
        c.execute('UPDATE users SET phone_number=?, access_token=?, refresh_token=?, token_expires_in=?, last_updated=? WHERE chat_id=?',
                  (phone, access, refresh, expires, now, chat_id))
        if c.rowcount == 0:
            c.execute('INSERT INTO users (chat_id, phone_number, access_token, refresh_token, token_expires_in, last_updated) VALUES (?,?,?,?,?,?)',
                      (chat_id, phone, access, refresh, expires, now))
        conn.commit()
    finally:
        conn.close()

def update_user_plan(chat_id, plan):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('UPDATE users SET plan_type=? WHERE chat_id=?', (plan, chat_id))
    conn.commit()
    conn.close()

def update_last_played(chat_id, played_time_str):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('UPDATE users SET last_played_time=? WHERE chat_id=?', (played_time_str, chat_id))
    conn.commit()
    conn.close()

def get_user_data(chat_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('SELECT phone_number, access_token, refresh_token, token_expires_in, last_updated, device_uuid, instant_id, plan_type, last_played_time FROM users WHERE chat_id=?', (chat_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            'phone_number': row[0], 'access_token': row[1], 'refresh_token': row[2], 
            'token_expires_in': row[3], 'last_updated': row[4], 
            'device_uuid': row[5], 'instant_id': row[6], 'plan_type': row[7], 'last_played_time': row[8]
        }
    return None

# --- CORE LOGIC ---
def generate_device_fingerprint(instance_id, phone, ts_str):
    key = ts_str.encode('utf-8')
    msg = (ts_str + instance_id + phone).encode('utf-8')
    return hmac.new(key, msg, hashlib.sha256).hexdigest()

# --- API HELPERS ---
URL_OTP = "https://apis.ooredoo.dz/api/auth/realms/myooredoo/protocol/openid-connect/token"
URL_CHECKPOINT = "https://apis.ooredoo.dz/api/ooredoo-bff/checkpoint/token"
URL_SNAP = "https://apis.ooredoo.dz/api/ooredoo-bff/snap-chat/eligibility"
URL_GIFT_STATUS = "https://apis.ooredoo.dz/api/ooredoo-bff/gamification/status"
URL_GIFT_PLAY = "https://apis.ooredoo.dz/api/ooredoo-bff/gamification/play"
URL_PACKAGES = "https://apis.ooredoo.dz/api/ooredoo-bff/bundle/getActivePackages"
URL_VALIDATE = "https://apis.ooredoo.dz/api/ooredoo-bff/users/validateUser"

# --- LOGIN HANDLERS ---

async def request_checkpoint(phone, device_uuid):
    headers = {
        "X-msisdn": phone,
        "X-platform-origin": "mobile-android",
        "X-path": "/api/auth/realms/myooredoo/protocol/openid-connect/token",
        "X-method": "POST",
        "X-Device-ID": device_uuid, 
        "User-Agent": "Dart/3.4 (dart:io)",
        "Content-Type": "application/x-www-form-urlencoded; charset=utf-8"
    }
    try:
        r = requests.post(URL_CHECKPOINT, headers=headers)
        if r.status_code == 202:
            return {"nonce": r.headers.get("X-Nonce-Id"), "chronos": r.headers.get("X-Chronos-Id"), "ok": True}
        return {"ok": False, "err": f"Checkpoint Failed: {r.status_code}"}
    except Exception as e:
        return {"ok": False, "err": str(e)}

async def send_otp_request(phone, nonce, chronos, device_uuid):
    headers = {
        "X-Nonce-Id": nonce,
        "X-Chronos-Id": chronos,
        "X-platform-origin": "mobile-android",
        "X-Device-ID": device_uuid,
        "User-Agent": "Dart/3.4 (dart:io)",
        "Content-Type": "application/x-www-form-urlencoded; charset=utf-8"
    }
    data = {"client_id": "myooredoo-app", "grant_type": "password", "username": phone}
    try:
        r = requests.post(URL_OTP, headers=headers, data=data)
        if r.status_code == 403: return {"ok": True}
        return {"ok": False, "err": f"Send OTP Failed: {r.status_code}\n{r.text}"}
    except Exception as e:
        return {"ok": False, "err": str(e)}

async def verify_otp_request(phone, otp, nonce, chronos, device_uuid):
    headers = {
        "X-Nonce-Id": nonce,
        "X-Chronos-Id": chronos,
        "X-platform-origin": "mobile-android",
        "X-Device-ID": device_uuid,
        "User-Agent": "Dart/3.4 (dart:io)",
        "Content-Type": "application/x-www-form-urlencoded; charset=utf-8"
    }
    data = {"client_id": "myooredoo-app", "grant_type": "password", "username": phone, "otp": otp}
    try:
        r = requests.post(URL_OTP, headers=headers, data=data)
        if r.status_code == 200:
            return {"ok": True, "access": r.json().get("access_token"), "refresh": r.json().get("refresh_token")}
        return {"ok": False, "err": f"Verify Failed: {r.status_code}\n{r.text}"}
    except Exception as e:
        return {"ok": False, "err": str(e)}

# --- DATA FETCHING FUNCTIONS ---

def get_headers_verified(access_token, phone, instant_id):
    clean_phone = phone
    if clean_phone.startswith("05"): clean_phone = "213" + clean_phone[1:]
    
    time.sleep(0.1) # Sync delay
    ts_now = str(int(time.time() * 1000))
    fp = generate_device_fingerprint(instant_id, clean_phone, ts_now)
    
    return {
        "X-Device-Fingerprint": fp,
        "X-Platform-Origin": "mobile-android",
        "Authorization": f"Bearer {access_token}",
        "X-Timestamp": ts_now,
        "X-Instance-Id": instant_id,
        "X-Msisdn": clean_phone
    }

async def fetch_user_plan(access_token, phone, instant_id):
    clean_phone = phone
    if clean_phone.startswith("05"): clean_phone = "213" + clean_phone[1:]
    headers = get_headers_verified(access_token, phone, instant_id)
    url = f"{URL_VALIDATE}?msisdn={clean_phone}"
    try:
        r = requests.get(url, headers=headers)
        if r.status_code == 200:
            return r.json().get("planType", "Unknown")
        return "Unknown"
    except:
        return "Error"

async def fetch_gift_info(chat_id, access_token, phone, instant_id, cached_last_played):
    """
    Checks Gift Status. 
    1. Checks DB Cache first.
    2. If cache empty or expired, calls API.
    """
    
    # 1. CACHE CHECK
    if cached_last_played:
        try:
            # Parse stored time (e.g., 2026-02-14T13:21:48.563...)
            clean_ts = cached_last_played.split(".")[0]
            last_dt = dt_class.strptime(clean_ts, "%Y-%m-%dT%H:%M:%S")
            next_gift_dt = last_dt + datetime.timedelta(hours=24)
            rem = next_gift_dt - dt_class.now()
            
            if rem.total_seconds() > 0:
                # Still in cooldown, rely on cache
                hrs, sec = divmod(rem.seconds, 3600)
                mins, _ = divmod(sec, 60)
                return f"⏱️ **الهدية:** باقي {hrs} ساعة و {mins} دقيقة", False
        except Exception as e:
            # Cache invalid, proceed to API
            pass

    # 2. API CHECK
    headers = get_headers_verified(access_token, phone, instant_id)
    try:
        r = requests.get(URL_GIFT_STATUS, headers=headers)
        if r.status_code == 200:
            data = r.json()
            played = data.get("played", False)
            last_played_str = data.get("lastPlayedTime")
            
            if played and last_played_str:
                # Update DB for next time
                update_last_played(chat_id, last_played_str)
                
                # Calc time
                try:
                    clean_ts = last_played_str.split(".")[0]
                    last_dt = dt_class.strptime(clean_ts, "%Y-%m-%dT%H:%M:%S")
                    next_dt = last_dt + datetime.timedelta(hours=24)
                    rem = next_dt - dt_class.now()
                    
                    if rem.total_seconds() > 0:
                        hrs, sec = divmod(rem.seconds, 3600)
                        mins, _ = divmod(sec, 60)
                        return f"⏱️ **الهدية:** باقي {hrs} ساعة و {mins} دقيقة", False
                    else:
                        return "🎉 **الهدية متوفرة!**", True
                except:
                    return "⚠️ خطأ في وقت الهدية", False
            else:
                return "🎉 **الهدية متوفرة!**", True
        else:
            return f"❌ خطأ هدية ({r.status_code})", False
    except Exception as e:
        return f"❌ خطأ شبكة", False

async def fetch_balance_bundles(access_token, phone, instant_id):
    clean_phone = phone
    if clean_phone.startswith("05"): clean_phone = "213" + clean_phone[1:]
    headers = get_headers_verified(access_token, phone, instant_id)
    url = f"{URL_PACKAGES}?msisdn={clean_phone}"
    try:
        r = requests.get(url, headers=headers)
        if r.status_code == 200:
            data = r.json()
            balance = data.get("accountBalance", "0")
            msg = f"💰 **الرصيد:** `{balance} DA`\n"
            msg += "─" * 20 + "\n"
            
            all_bundles = []
            if "activeBundles" in data: all_bundles.extend(data["activeBundles"])
            if "monthlyDataSmartBundlePurchases" in data:
                m = data["monthlyDataSmartBundlePurchases"]
                if "dataBundles" in m: all_bundles.extend(m["dataBundles"])
                if "smartBundles" in m: all_bundles.extend(m["smartBundles"])
            
            if not all_bundles:
                msg += "🚫 لا توجد اشتراكات.\n"
            else:
                for b in all_bundles:
                    name = b.get("allocationName", "Unknown")
                    rem = b.get("remainingBalance", "0")
                    unit = b.get("unit") or ""
                    
                    if name == "DATA": icon = "🌐"
                    elif name == "YOUTUBE": icon = "📺"
                    elif name == "VOICE": icon = "📞"
                    elif name == "SMS": icon = "✉️"
                    else: icon = "📦"
                    
                    days = ""
                    if b.get("expireDate"):
                        try:
                            exp = dt_class.strptime(b.get("expireDate").split(".")[0], "%Y-%m-%dT%H:%M:%S")
                            d = (exp - dt_class.now()).days
                            days = f"({d} يوم)" if d >= 0 else "(منتهي)"
                        except: pass
                    
                    msg += f"{icon} **{name}:** {rem} {unit} {days}\n"
            return msg
        else:
            return "⚠️ فشل جلب الرصيد"
    except:
        return "⚠️ خطأ في الرصيد"

# --- MAIN DASHBOARD ---

async def show_dashboard(update: Update, context, chat_id, access, phone, instant_id, last_played_db):
    plan = await fetch_user_plan(access, phone, instant_id)
    update_user_plan(chat_id, plan)
    
    bal_msg = await fetch_balance_bundles(access, phone, instant_id)
    
    # Pass DB value to cache function
    gift_msg, can_claim = await fetch_gift_info(chat_id, access, phone, instant_id, last_played_db)
    
    full_msg = f"📱 **الخطة:** {plan}\n{bal_msg}\n" + "─" * 20 + f"\n{gift_msg}"
    
    buttons = []
    if can_claim:
        buttons.append([InlineKeyboardButton("🎁 أحصل على الهدية الآن", callback_data="claim_gift")])
    if plan and plan.upper() == "YOOZ":
        buttons.append([InlineKeyboardButton("👻 التحقق من سناب شات", callback_data="check_snapchat")])
    buttons.append([InlineKeyboardButton("🔄 تحديث", callback_data="refresh_dash")])
    
    await update.message.reply_text(full_msg, reply_markup=InlineKeyboardMarkup(buttons), parse_mode='Markdown')

async def refresh_dashboard(update: Update, context):
    q = update.callback_query
    await q.answer("جاري التحديث...")
    chat_id = update.effective_chat.id
    u = get_user_data(chat_id)
    if not u: return
    
    plan = u['plan_type'] or await fetch_user_plan(u['access_token'], u['phone_number'], u['instant_id'])
    bal_msg = await fetch_balance_bundles(u['access_token'], u['phone_number'], u['instant_id'])
    # Pass last_played_time from DB to use cache
    gift_msg, can_claim = await fetch_gift_info(chat_id, u['access_token'], u['phone_number'], u['instant_id'], u['last_played_time'])
    
    full_msg = f"📱 **الخطة:** {plan}\n{bal_msg}\n" + "─" * 20 + f"\n{gift_msg}"
    
    buttons = []
    if can_claim: buttons.append([InlineKeyboardButton("🎁 أحصل على الهدية الآن", callback_data="claim_gift")])
    if plan and plan.upper() == "YOOZ": buttons.append([InlineKeyboardButton("👻 التحقق من سناب شات", callback_data="check_snapchat")])
    buttons.append([InlineKeyboardButton("🔄 تحديث", callback_data="refresh_dash")])
    
    try:
        await q.edit_message_text(full_msg, reply_markup=InlineKeyboardMarkup(buttons), parse_mode='Markdown')
    except:
        pass

# --- GAME PLAY LOGIC ---

async def claim_gift(update: Update, context):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("⏳ جاري تحضير الهدية (الخطوة 1/2)...")
    
    chat_id = update.effective_chat.id
    u = get_user_data(chat_id)
    if not u: return
    
    phone = u['phone_number']
    if phone.startswith("05"): phone = "213" + phone[1:]
    
    # 1. CHECKPOINT Request (To get Nonce/Chronos for Play)
    headers_cp = {
        "X-msisdn": phone,
        "X-platform-origin": "mobile-android",
        "X-path": "/api/auth/realms/myooredoo/protocol/openid-connect/token", 
        "X-method": "POST"
    }
    
    try:
        r1 = requests.post(URL_CHECKPOINT, headers=headers_cp)
        if r1.status_code == 202:
            nonce = r1.headers.get("X-Nonce-Id")
            chronos = r1.headers.get("X-Chronos-Id")
        else:
            await update.effective_chat.send_message(f"❌ فشل التحضير ({r1.status_code})")
            return
    except Exception as e:
        await update.effective_chat.send_message(f"❌ خطأ اتصال: {str(e)}")
        return

    # 2. PLAY Request
    await q.edit_message_text("⏳ جاري فتح الهدية (الخطوة 2/2)...")
    
    headers_play = get_headers_verified(u['access_token'], phone, u['instant_id'])
    
    headers_play.update({
        "X-Nonce-Id": nonce,
        "X-Chronos-Id": chronos,
        "X-platform-origin": "mobile-android"
    })
    
    try:
        r2 = requests.post(URL_GIFT_PLAY, headers=headers_play)
        
        if r2.status_code == 200:
            data = r2.json()
            gift_name = data.get("giftName", "هدية")
            validity = data.get("validityHour", "?")
            played_time = data.get("playedTime") 
            
            if played_time:
                update_last_played(chat_id, played_time)
            
            msg = f"🎉 **مبروك! حصلت على:**\n\n🎁 {gift_name}\n⏳ الصلاحية: {validity} ساعة"
            
            await update.effective_chat.send_message(msg, parse_mode='Markdown')
            
            u_new = get_user_data(chat_id)
            await show_dashboard(update, context, chat_id, u_new['access_token'], u_new['phone_number'], u_new['instant_id'], u_new['last_played_time'])
            
        else:
            await update.effective_chat.send_message(f"❌ خطأ أثناء الفتح ({r2.status_code}):\n{r2.text[:50]}")
            
    except Exception as e:
        await update.effective_chat.send_message(f"❌ خطأ: {str(e)}")

async def check_snapchat(update: Update, context):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text("👻 التحقق من سناب شات (قيد التنفيذ)...")


# --- MAIN ---
user_states = {}

async def start(update: Update, context):
    chat_id = update.effective_chat.id
    get_or_create_device_info(chat_id)
    u = get_user_data(chat_id)
    
    if u and u['access_token']:
        await update.message.reply_text("👋 مرحبًا بك مجددًا!")
        await show_dashboard(update, context, chat_id, u['access_token'], u['phone_number'], u['instant_id'], u['last_played_time'])
    else:
        user_states[chat_id] = "phone"
        await update.message.reply_text("📞 رقم الهاتف:")

async def handle_msg(update: Update, context):
    chat_id = update.effective_chat.id
    txt = update.message.text.strip()
    state = user_states.get(chat_id)
    
    device_uid, instant_id = get_or_create_device_info(chat_id)
    
    if state == "phone":
        if txt.startswith("05"): txt = "213" + txt[1:]
        elif txt.startswith("213"): pass
        else:
            await update.message.reply_text("❌ تنسيق الرقم خطأ (05...).")
            return

        sec = await request_checkpoint(txt, device_uid)
        if not sec["ok"]:
             await update.message.reply_text(f"❌ فشل الاتصال:\n{sec.get('err')}")
             return
             
        res = await send_otp_request(txt, sec["nonce"], sec["chronos"], device_uid)
        if res["ok"]:
            user_states[chat_id] = {"st": "otp", "ph": txt}
            await update.message.reply_text("✅ تم إرسال الرمز! أدخل OTP:")
        else:
            await update.message.reply_text(f"❌ فشل الإرسال:\n{res.get('err')}")
            
    elif isinstance(state, dict) and state["st"] == "otp":
        ph = state["ph"]
        sec = await request_checkpoint(ph, device_uid)
        if not sec["ok"]:
            await update.message.reply_text(f"❌ فشل تحديث الجلسة:\n{sec.get('err')}")
            return
            
        res = await verify_otp_request(ph, txt, sec["nonce"], sec["chronos"], device_uid)
        
        if res["ok"]:
            save_user_data(chat_id, ph, res["access"], res["refresh"], 3600)
            user_states[chat_id] = None
            await update.message.reply_text("✅ **تم تسجيل الدخول!**", parse_mode='Markdown')
            
            u_new = get_user_data(chat_id)
            await show_dashboard(update, context, chat_id, res["access"], ph, instant_id, u_new['last_played_time'])
        else:
            await update.message.reply_text(f"❌ رمز خاطئ:\n{res.get('err')}")

def main():
    init_db()
    app = Application.builder().token(TELEGRAMBOTTOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))
    app.add_handler(CallbackQueryHandler(claim_gift, pattern="^claim_gift$"))
    app.add_handler(CallbackQueryHandler(check_snapchat, pattern="^check_snapchat$"))
    app.add_handler(CallbackQueryHandler(refresh_dashboard, pattern="^refresh_dash$"))
    print("Bot Running...")
    app.run_polling()

if __name__ == "__main__":
    main()
