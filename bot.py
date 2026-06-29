"""Winkly Dating Bot v2 - Complete Implementation"""
import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
# import random
import re
import unicodedata
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, date
from typing import Any, Dict, List, Optional, Set

import aiohttp
import razorpay
from dotenv import load_dotenv
from aiohttp import web
import redis.asyncio as redis
from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, BotCommand, BotCommandScopeDefault, WebAppInfo
from aiogram.filters.state import State, StatesGroup
# import cv2
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def _faces_found(image_path: str) -> bool:
    """Return True if at least one face is detected in the image using OpenCV."""
    try:
        img = cv2.imread(image_path)
        if img is None:
            return False  # can't read image = reject
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
        faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30))
        return len(faces) >= 1
    except Exception as e:
        logger.warning(f"Face detection error: {e}")
        return False  # fail-closed: any error = reject


async def _verify_face(uid: int, file_id: str) -> bool:
    """Verification is now automatic — stub to avoid broken code paths."""
    return True  # auto-verify: no selfie check needed


load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '.env'))

BOT_TOKEN = os.getenv('BOT_TOKEN', '8624196108:***')
REDIS_URL = os.getenv('REDIS_URL', '')
WEBHOOK_URL = os.getenv('WEBHOOK_URL', 'https://winkly-kmsz.onrender.com')
PORT = int(os.getenv('PORT', '8080'))
ADMIN_CHAT_ID = int(os.getenv('ADMIN_CHAT_ID', '0'))
RAZORPAY_KEY_ID = os.getenv('RAZORPAY_KEY_ID', 'rzp_live_T5RFsK3b9AYBTX')
RAZORPAY_KEY_SECRET = os.getenv('RAZORPAY_KEY_SECRET', 'MBAphgobB9XnZ33SylDA9r7C')
RAZORPAY_WEBHOOK_SECRET = os.getenv('RAZORPAY_WEBHOOK_SECRET', 'winkly_webhook_secret')
BOT_USERNAME = os.getenv('BOT_USERNAME', 'Winkly_dating_bot')

LONG_PLANS = [
    {"name": "TEST 1 Day", "price": 1,  "duration": 1},
    {"name": "Monthly",    "price": 199, "duration": 30},
    {"name": "3 Months",   "price": 299, "duration": 90},
    {"name": "6 Months",   "price": 499, "duration": 180},
    {"name": "1 Year",      "price": 699, "duration": 365},
]

bot = Bot(token=BOT_TOKEN)
# RedisStorage created at module load (sync, no connection check — connection checked at first use)
# Falls back to MemoryStorage if REDIS_URL is missing/invalid
_redis_client_for_fsm = redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None
if _redis_client_for_fsm:
    try:
        _fsm_storage = RedisStorage(
            redis=_redis_client_for_fsm,
            state_ttl=timedelta(days=7),
            data_ttl=timedelta(days=7),
        )
    except Exception:
        _fsm_storage = MemoryStorage()
else:
    _fsm_storage = MemoryStorage()
dp = Dispatcher(bot=bot, storage=_fsm_storage)

try:
    razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
except Exception as e:
    logger.warning(f"Razorpay init failed: {e}")
    razorpay_client = None

_redis = None
_redis_failed_time = 0.0

async def get_redis():
    global _redis, _redis_failed_time
    if _redis is not None:
        return _redis
    # Don't retry more than once per 60 seconds
    if time.time() - _redis_failed_time < 60:
        return None
    if REDIS_URL:
        try:
            _redis = redis.from_url(REDIS_URL, decode_responses=True)
            await _redis.ping()
            _redis_failed_time = 0.0
        except Exception as e:
            logger.warning(f"Redis unavailable: {e}. Running in-memory only.")
            _redis = None
            _redis_failed_time = time.time()
    else:
        try:
            _redis = redis.Redis(host='localhost', port=6379, decode_responses=True)
            await _redis.ping()
            _redis_failed_time = 0.0
        except Exception:
            logger.warning("Local Redis not available. Running in-memory only.")
            _redis = None
            _redis_failed_time = time.time()
    return _redis

async def init_storage():
    global user_profiles, active_matches, likes_sent, waiting_queue, premium_subscriptions, current_chat
    r = await get_redis()
    if r is None:
        logger.info("Storage: in-memory only (Redis unavailable)")
        return
    # FSM storage (Redis-backed) was already configured at module load time.
    # Now load persisted data from Redis into memory.
    for key, dest in [
            ('winkly:profiles',  user_profiles),
            ('winkly:matches',   active_matches),
            ('winkly:queue',    waiting_queue),
            ('winkly:premium',  premium_subscriptions),
            ('winkly:chat',     current_chat),
        ]:
            raw = await r.get(key)
            if raw:
                try:
                    val = json.loads(raw)
                    # All dicts use integer uid keys — convert from Redis string keys
                    val = {int(k): v for k, v in val.items()}
                    dest.update(val)
                except Exception as e:
                    logger.error(f"Failed to load {key}: {e}")
            for prof in user_profiles.values():
                    if "free_texts" not in prof:
                        prof["free_texts"] = FREE_TEXTS_JOINING
                    if "rejected" not in prof:
                        prof["rejected"] = []
                    if "dob" not in prof:
                        prof["dob"] = ""
                    prof['dob'] = ''
                    if "received_texts" not in prof:
                        prof["received_texts"] = 0
        # Persist migrated fields back to Redis immediately
    await save_all()
    raw_likes = await r.get('winkly:likes')
    if raw_likes:
        for uid, lst in json.loads(raw_likes).items():
            likes_sent[int(uid)] = set(lst)
    raw_proc = await r.get('winkly:processed')
    if raw_proc:
        _processed_payments.update(json.loads(raw_proc))
    logger.info(f"Storage loaded: {len(user_profiles)} profiles, {len(active_matches)} matches, {len(waiting_queue)} queue")

    # ── FSM backup functions moved to module level ──

# ── FSM backup: write critical fields directly to Redis as fallback ──
async def fsm_backup_set(uid: int, field: str, value: str):
    r = await get_redis()
    if r:
        await r.set(f'winkly:fsm:{uid}:{field}', value, ex=86400*7)

async def fsm_backup_get(uid: int, field: str) -> str:
    r = await get_redis()
    if r:
        return await r.get(f'winkly:fsm:{uid}:{field}')
    return None

async def save_all():
    r = await get_redis()
    if r is None:
        return  # In-memory only, nothing to persist
    try:
        # ── ALWAYS rebuild user_profiles directly from RedisStorage FSM ──
        # This ensures profile data survives even if the in-memory dict is empty
        # (which happens when a different worker handles the request)
        all_state_keys = []
        cursor = 0
        while True:
            cursor_k, keys = await r.scan(cursor, match='fsm:*:data', count=200)
            all_state_keys.extend(keys)
            cursor = cursor_k
            if cursor == 0:
                break
        rebuilt = {}
        for key in all_state_keys:
            val = await r.get(key)
            if not val:
                continue
            try:
                data = json.loads(val)
            except:
                continue
            # Extract uid from key like "fsm:{uid}:{uid}:data"
            parts = key.split(':')
            if len(parts) >= 2:
                try:
                    uid = int(parts[1])
                except:
                    continue
                if not data.get('gender'):
                    continue  # Skip empty/minimal FSM data
                # Build profile from FSM data + backup keys
                name_val = (data.get('name') or
                            await r.get(f'winkly:fsm:{uid}:name') or '')
                username_val = (data.get('username') or
                                await r.get(f'winkly:fsm:{uid}:username') or '')
                existing = user_profiles.get(uid, {})
                rebuilt[uid] = {
                    'name': name_val or existing.get('name', ''),
                    'gender': data.get('gender', '') or existing.get('gender', ''),
                    'preferred_gender': data.get('preferred', '') or existing.get('preferred_gender', ''),
                    'bio': data.get('bio', '') or existing.get('bio', ''),
                    'dob': data.get('dob', '') or existing.get('dob', ''),
                    'lat': data.get('lat', '') or existing.get('lat', ''),
                    'lon': data.get('lon', '') or existing.get('lon', ''),
                    'location_name': data.get('location_name', '') or existing.get('location_name', ''),
                    'photo': data.get('photo') or existing.get('photo'),
                    'verified': data.get('verified', True) or existing.get('verified', True),
                    'verification_status': data.get('verification_status', 'verified') or existing.get('verification_status', 'verified'),
                    'username': username_val or existing.get('username', ''),
                    'free_texts': data.get('free_texts') if data.get('free_texts') is not None else existing.get('free_texts', FREE_TEXTS_JOINING),
                    'rejected': data.get('rejected') or existing.get('rejected', []),
                }
        # Update in-memory dict with rebuilt data
        for uid, prof in rebuilt.items():
            user_profiles[uid] = prof

        await r.set('winkly:profiles',  json.dumps(user_profiles))
        await r.set('winkly:matches',   json.dumps(active_matches))
        await r.set('winkly:queue',     json.dumps(waiting_queue))
        await r.set('winkly:premium',   json.dumps(premium_subscriptions))
        await r.set('winkly:chat',      json.dumps(current_chat))
        await r.set('winkly:likes',     json.dumps({k: list(v) for k, v in likes_sent.items()}))
        await r.set('winkly:processed', json.dumps(list(_processed_payments)))
    except Exception as e:
        logger.error(f"Redis save FAILED in save_all: {type(e).__name__}: {e}")

user_profiles: Dict[int, dict] = {}
active_matches: Dict[int, Dict[int, dict]] = {}
likes_sent: Dict[int, Set[int]] = {}
waiting_queue: Dict[int, dict] = {}
premium_subscriptions: Dict[int, dict] = {}
current_chat: Dict[int, int] = {}
_queue_msg_ids: Dict[int, int] = {}
_processed_payments: Set[str] = set()
_payment_link_map: Dict[str, dict] = {}  # payment_link_id -> {'uid': int, 'duration_days': int}
_quota_notif: Dict[int, dict] = {}  # uid -> {'mid': int, 'count': int}
_reconnect_tasks: Dict[int, asyncio.Task] = {}  # hold_user_uid -> reconnect loop task
# === Bot Protection: IP Rate Limiting ===
import time as _time

RATE_LIMIT_WINDOW = 3600   # 1 hour sliding window
RATE_LIMIT_MAX = 3           # max signups per IP per window
RATE_IP_PREFIX = "winkly:rateip:"

async def _get_client_ip(message: types.Message) -> str:
    """Get IP from message, try message.from_user.id as fallback proxy."""
    ip = ""
    # Try effectiveMessage for forwarded messages
    try:
        em = message.effective_message
        if hasattr(em, 'forward_from') and em.forward_from:
            # forwarded — use sender's IP indirectly via bot token
            return str(message.from_user.id)
        # Try via Telegram's extract update source
        update = message.update if hasattr(message, 'update') else None
        if update and hasattr(update, 'effective_user') and update.effective_user:
            return str(update.effective_user.id)
    except:
        pass
    # Fallback: use user ID as proxy for IP (each user = unique Telegram account)
    return f"uid:{message.from_user.id}"

async def check_ip_rate_limit(uid: int) -> bool:
    """
    Sliding window rate limit using Redis sorted sets.
    Returns True if signup allowed, False if blocked.
    Stores one entry per /start call (not per signup completion).
    """
    r = await get_redis()
    if r is None:
        return True  # fail-open if Redis unavailable
    key = f"{RATE_IP_PREFIX}{uid % 1000}"  # shard by uid modulo to spread keys
    now = _time.time()
    window_start = now - RATE_LIMIT_WINDOW

    pipe = r.pipeline()
    # Remove old entries outside the window
    pipe.zremrangebyscore(key, 0, window_start)
    # Count entries in window
    pipe.zcard(key)
    # Add this signup attempt
    pipe.zadd(key, {str(now): now})
    # Set expiry on the key
    pipe.expire(key, RATE_LIMIT_WINDOW + 10)
    results = await pipe.execute()

    count = results[1]  # zcard result
    if count >= RATE_LIMIT_MAX:
        # Remove the entry we just added (don't count blocked attempts)
        await r.zrem(key, str(now))
        return False
    return True

async def check_account_age(uid: int) -> tuple[bool, str]:
    """
    Check if Telegram account is old enough via getChatMember.
    Returns (allowed, reason). reason is empty if allowed.
    """
    try:
        member = await bot.get_chat_member(chat_id=uid, user_id=uid)
        status = member.status
        # getChatMember returns: 'member', 'restricted', 'left', 'kicked', 'creator', 'administrator'
        # For 'kicked' or 'left', user is not a valid member
        if status in ('kicked', 'left'):
            return False, "Your Telegram account is banned or left the bot. Please create a new Telegram account."
        # For valid members, also check join_date if available (available for member/restricted/administrator/creator)
        join_date = getattr(member, 'joined_date', None)
        if join_date:
            age_seconds = _time.time() - join_date
            if age_seconds < 86400:  # 24 hours
                hours_left = int((86400 - age_seconds) / 3600) + 1
                return False, f"Your Telegram account must be at least 24 hours old. Please try again in {hours_left} hour{'s' if hours_left > 1 else ''}."
        return True, ""
    except Exception as e:
        logger.warning(f"Account age check failed for {uid}: {e}")
        return True, ""  # fail-open — don't block legitimate users if API fails


class Signup(StatesGroup):
    name = State()
    gender = State()
    preferred = State()
    location = State()
    bio = State()
    dob = State()

class Verify(StatesGroup):
    photo = State()

class EditProfile(StatesGroup):
    name = State()
    bio = State()
    gender = State()
    preferred = State()
    location = State()
    dob = State()

GENDER_NORM = {'male':'Male','female':'Female','other':'Other','men':'Male','women':'Female','m':'Male','f':'Female',
                '\U0001f468\u200d\U0001f3fb':'Male','\U0001f469\u200d\U0001f3fb':'Female','\u2695\ufe0f':'Other',
                '\U0001f468 Male':'Male','\U0001f469 Female':'Female','\U0001f465 Everyone':'Everyone',
                '\U0001f468\u200d\U0001f3eb':'Male','\U0001f469\u200d\U0001f3fb':'Female','\U0001f465':'Everyone'}

def norm_gender(g: str) -> str:
    return GENDER_NORM.get(g.lower().strip(), g)

async def geocode(place: str):
    cities = {'bangalore':(12.9716,77.5946),'bengaluru':(12.9716,77.5946),'mumbai':(19.0760,72.8777),
              'delhi':(28.6139,77.2090),'chennai':(13.0827,80.2707),'kolkata':(22.5726,88.3639),
              'hyderabad':(17.3850,78.4867),'pune':(18.5204,73.8567),'ahmedabad':(23.0225,72.5714),
              'jaipur':(26.9124,75.7873),'lucknow':(26.8467,80.9462),'chandigarh':(30.7333,76.7794),
              'nagpur':(21.1458,79.0882),'visakhapatnam':(17.6868,83.2185),'kochi':(9.9312,76.2673),
              'goa':(15.2993,74.1240),'surat':(21.1702,72.8311),'bhubaneswar':(20.2961,85.8245),
              'raipur':(21.2514,81.6296),'indore':(22.7196,75.8577)}
    n = place.lower().strip()
    if n in cities:
        return cities[n]
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get('https://nominatim.openstreetmap.org/search',
                params={'q': place, 'format': 'json', 'limit': 1},
                headers={'User-Agent': 'WinklyBot/1.0'},
                timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    d = await r.json()
                    if d:
                        return float(d[0]['lat']), float(d[0]['lon'])
    except Exception as e:
        logger.warning(f"Geocode failed: {e}")
    return None, None

def haversine(lat1, lon1, lat2, lon2):
    R = 6371; phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1); dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def parse_dob(raw: str):
    raw = raw.strip()
    if re.match(r'^\d{4}-\d{2}-\d{2}$', raw):
        y, m, d = raw.split('-'); return date(int(y), int(m), int(d))
    if re.match(r'^\d{8}$', raw):
        return date(int(raw[4:8]), int(raw[2:4]), int(raw[0:2]))
    for sep in '/-.':
        if sep in raw:
            parts = raw.split(sep)
            if len(parts) == 3 and parts[2].isdigit():
                dp, mp, yp = parts; y = int(yp)
                if len(yp) == 2: y += 2000 if y < 30 else 1900
                return date(y, int(mp), int(dp))
    months = {'jan':1,'january':1,'feb':2,'february':2,'mar':3,'march':3,'apr':4,'april':4,'may':5,'jun':6,'june':6,
              'jul':7,'july':7,'aug':8,'august':8,'sep':9,'september':9,'oct':10,'october':10,'nov':11,'november':11,'dec':12,'december':12}
    parts = re.split(r'[\s,]+', raw)
    for part in parts:
        if part.lower() in months:
            m = months[part.lower()]
            for dp in parts:
                if dp.isdigit() and 1 <= int(dp) <= 31 and dp != str(m):
                    for yp in parts:
                        if yp.isdigit() and len(yp) in (2,4) and yp != dp:
                            y = int(yp)
                            if len(yp) == 2: y += 2000 if y < 30 else 1900
                            return date(y, m, int(dp))
    return None

def calc_age(born: date):
    today = date.today(); age = today.year - born.year
    if (today.month, today.day) < (born.month, born.day): age -= 1
    return age

ONLINE_TTL = 120

async def mark_online(uid: int):
    try:
        r = await get_redis(); await r.setex(f'online:{uid}', ONLINE_TTL, datetime.now().isoformat())
    except Exception as e:
        logger.warning(f"mark_online failed: {e}")

async def get_online_count() -> int:
    try:
        r = await get_redis(); keys = [k async for k in r.scan_iter('online:*')]
        if not keys: return 0
        vals = await r.mget(keys); now = datetime.now(); c = 0
        for v in vals:
            if v:
                try:
                    if (now - datetime.fromisoformat(v)).total_seconds() < ONLINE_TTL: c += 1
                except: pass
        return c
    except: return len(user_profiles)

FREE_TEXTS_JOINING = 20
RECEIVE_LIMIT = 20

def is_premium(uid: int) -> bool:
    if uid not in premium_subscriptions: return False
    exp_str = premium_subscriptions[uid].get('expiry_date', '')
    if not exp_str: return False
    try: return datetime.now() < datetime.fromisoformat(exp_str)
    except: return False

def is_verified_female(uid: int) -> bool:
    """Verified females get unlimited free access (bypass text limits)."""
    p = user_profiles.get(uid)
    return bool(p and p.get('verified') and 'Female' in (p.get('gender', ''),))

def check_text_quota(uid: int) -> bool:
    """Check if user can send a message (free texts remaining, premium, or verified female)."""
    p = user_profiles.get(uid)
    if not p: return False
    if is_premium(uid) or is_verified_female(uid):
        return True
    return p.get('free_texts', 0) > 0

def check_match_quota(uid: int) -> bool:
    """Unlimited matching — no limit."""
    return True

def consume_text(uid: int):
    """Consume one free text (if not premium/verified female)."""
    p = user_profiles.get(uid)
    if not p: return
    if is_premium(uid) or is_verified_female(uid):
        return
    p['free_texts'] = max(0, p.get('free_texts', 0) - 1)

def quota_summary(uid: int) -> str:
    if is_premium(uid):
        exp_str = premium_subscriptions[uid].get('expiry_date', '')
        try:
            exp = datetime.fromisoformat(exp_str); days = (exp - datetime.now()).days
            return f"PREMIUM ACTIVE - Unlimited! Expires in {days} day{'s' if days != 1 else ''}"
        except: return "PREMIUM ACTIVE - Unlimited!"
    if is_verified_female(uid):
        return "VERIFIED FEMALE - Unlimited free access!"
    p = user_profiles.get(uid, {})
    ft = p.get('free_texts', 0)
    if ft > 0:
        return f"FREE - {ft} free texts left. Upgrade for unlimited."
    return "FREE LIMIT REACHED - Upgrade for unlimited texts."

def referral_code(uid: int) -> str:
    return hashlib.md5(f"winkly_{uid}_ref".encode()).hexdigest()[:8].upper()

async def referral_count(uid: int) -> int:
    try:
        r = await get_redis(); return await r.scard(f'winkly:referrals:{uid}')
    except: return 0

async def award_free_premium(uid: int):
    exp = datetime.now() + timedelta(days=1)
    premium_subscriptions[uid] = {'expiry_date': exp.isoformat()}
    if uid in user_profiles:
        user_profiles[uid]['received_texts'] = 0
    await save_all()
    try:
        await bot.send_message(uid,
            "FREE PREMIUM EARNED! You unlocked 1 day of FREE unlimited texts and matches! Valid 24 hours. Enjoy!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="FIND MATCHES", callback_data='do_match')],
            ]))
    except: pass
    logger.info(f"Free premium awarded to {uid}")

# ─── Reconnect loop (when user is on hold, partner waits) ──────────────────

def _cleanup_reconnect(uid: int):
    """Cancel and remove the reconnect loop task for uid (the hold user)."""
    task = _reconnect_tasks.pop(uid, None)
    if task and not task.done():
        task.cancel()

async def _reconnect_loop(a_uid: int, b_uid: int):
    """
    Background loop: 18 checks x 10s = 3 minutes.
    Checks if the hold user (a_uid) becomes premium.
    If found → notify both users chat is active again.
    If all 18 fail → notify partner with Wait/End Chat options.
    """
    try:
        # Notify partner once when loop starts
        try:
            pname = user_profiles.get(a_uid, {}).get('name', 'Someone')
            await bot.send_message(
                b_uid,
                f"⏳ <b>{pname}</b> reached the message limit.\n\n"
                f"They can still receive your messages. We'll let you know when they're back!",
                parse_mode='HTML'
            )
        except:
            pass

        for i in range(18):
            await asyncio.sleep(10)
            # If chat already ended, stop
            if a_uid not in current_chat or b_uid not in current_chat:
                return
            if a_uid not in user_profiles:
                return
            if is_premium(a_uid):
                # Reconnected — notify BOTH sides
                pname = user_profiles.get(a_uid, {}).get('name', 'Someone')
                bname = user_profiles.get(b_uid, {}).get('name', 'Someone')
                try:
                    await bot.send_message(
                        b_uid,
                        f"🎉 <b>{pname}</b> is back!\n\n"
                        f"Your conversation is active again. Say something nice! 💬",
                        parse_mode='HTML'
                    )
                except:
                    pass
                try:
                    await bot.send_message(
                        a_uid,
                        f"✅ You're back in the chat with <b>{bname}</b>!\n\n"
                        f"Your premium is active — keep the conversation going.",
                        parse_mode='HTML',
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="💬 Resume Chat", callback_data=f'chat:{b_uid}')],
                        ])
                    )
                except:
                    pass
                return

        # All 18 attempts failed → notify partner with options
        if a_uid in current_chat and b_uid in current_chat:
            pname = user_profiles.get(a_uid, {}).get('name', 'Someone')
            try:
                await bot.send_message(
                    b_uid,
                    f"⏳ <b>{pname}</b> hasn't reconnected yet.\n\n"
                    f"What would you like to do?",
                    parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="⏳ Keep Waiting", callback_data='wait_reconnect'),
                         InlineKeyboardButton(text="🔚 End Chat", callback_data='end_reconnect')],
                    ])
                )
            except:
                pass
    except asyncio.CancelledError:
        pass
    finally:
        _reconnect_tasks.pop(a_uid, None)

async def credit_referrer(ref_code: str, new_uid: int):
    for uid in list(user_profiles.keys()):
        if referral_code(uid) == ref_code:
            try:
                r = await get_redis(); await r.sadd(f'winkly:referrals:{uid}', new_uid)
            except: pass
            cnt = await referral_count(uid)
            logger.info(f"Referrer {uid} has {cnt} referrals")
            if cnt >= 3: await award_free_premium(uid)
            return True
    return False

def find_compat(me: dict, all_profiles: Dict[int, dict]):
    my_lat, my_lon = me.get('lat'), me.get('lon')
    if not my_lat or not my_lon: return []
    my_pref = norm_gender(me.get('preferred_gender', ''))
    my_g = norm_gender(me.get('gender', ''))
    pool = {'Male','Female','Other'} if my_pref == 'Everyone' else {norm_gender(my_pref)}
    rejected = set(me.get('rejected', []))
    my_uid = me.get('_uid')
    results = []
    for uid, other in all_profiles.items():
        if uid == my_uid or not other.get('lat') or not other.get('lon'): continue
        if uid in rejected: continue
        og = norm_gender(other.get('gender', '')); op = norm_gender(other.get('preferred_gender', ''))
        if og not in pool: continue
        if op != 'Everyone' and my_g not in {op}: continue
        d = haversine(float(my_lat), float(my_lon), float(other['lat']), float(other['lon']))
        results.append({**other, 'uid': uid, 'distance_km': round(d, 1)})
    results.sort(key=lambda m: m['distance_km']); return results

def find_queue_match(me: dict, my_uid: int):
    me2 = {**me, '_uid': my_uid}
    for uid in list(waiting_queue.keys()):
        if uid == my_uid: continue
        if uid not in user_profiles: continue
        m = find_compat(me2, {uid: user_profiles[uid]})
        if m: return {**m[0], 'wait_info': waiting_queue[uid]}
    return None

def make_link_sync(uid: int, name: str, price: int, days: int):
    if not razorpay_client: return None
    try:
        result = razorpay_client.payment_link.create({
            "amount": price*100, "currency": "INR", "description": f"Winkly Premium - {name}",
            "notes": {"uid": str(uid), "duration_days": str(days)},
            "callback_url": f"{WEBHOOK_URL}/payment/success", "callback_method": "get",
            "options": {
                "checkout": {
                    "name": "Winkly",
                    "description": "Premium Dating"
                }
            },
        })
        pl_id = result.get("id")
        if pl_id:
            _payment_link_map[pl_id] = {"uid": uid, "duration_days": days}
        return result.get("short_url")
    except Exception as e:
        logger.error(f"Payment link error: {e}"); return None

async def make_payment_link(uid: int, name: str, price: int, days: int):
    return await asyncio.to_thread(make_link_sync, uid, name, price, days)

async def safe_delete(chat_id: int, message_id: int):
    if message_id:
        try: await bot.delete_message(chat_id, message_id)
        except: pass

def main_kb(uid: int = None):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Edit Profile", callback_data='edit_profile')],
        [InlineKeyboardButton(text="🔍 Find Matches", callback_data='do_match')],
    ])

def reengage_kb(uid: int = None):
    is_female = uid and user_profiles.get(uid, {}).get('gender') == 'Female'
    rows = [
        [InlineKeyboardButton(text="🔍 Find New Match", callback_data='do_match'),
         InlineKeyboardButton(text="👤 My Profile", callback_data='back_to_profile')],
    ]
    if not is_female:
        rows.append([InlineKeyboardButton(text="🌟 Premium", callback_data='see_premium')])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def edit_profile_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Change Name", callback_data='edit_name'),
         InlineKeyboardButton(text="📝 Edit Bio", callback_data='edit_bio')],
        [InlineKeyboardButton(text="👥 Change Preference", callback_data='edit_preferred')],
        [InlineKeyboardButton(text="📍 Update Location", callback_data='edit_location')],
        [InlineKeyboardButton(text="← Back", callback_data='back_to_profile')],
    ])

def profile_text(p: dict) -> str:
    vb = " VERIFIED" if p.get('verified') else ""
    loc_name = p.get('location_name')
    lat = p.get('lat')
    if loc_name:
        loc = loc_name
    elif lat and lat != '':
        try:
            lon = p.get('lon', '')
            loc = f"{float(lat):.4f}, {float(lon):.4f}"
        except (ValueError, TypeError):
            loc = 'NO LOCATION'
    else:
        loc = 'NO LOCATION'
    bio = p.get('bio') or '—'
    age_str = ""
    dob_raw = p.get('dob')
    if dob_raw:
        dob = parse_dob(dob_raw)
        if dob:
            age_str = f" | {calc_age(dob)}"
    return (f"👤 <b>Your Profile</b>\n\n"
            f"Name: {p.get('name','?')}\n"
            f"Gender: {p.get('gender','?')} | Interested in: {p.get('preferred_gender','?')}{age_str}\n"
            f"Bio: {bio}\n"
            f"Location: {loc}{vb}")




# ─── /start ─────────────────────────────────────────────────────────────────

@dp.message(Command('start'))
async def cmd_start(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    await mark_online(uid)
    args = message.text.split(' ', 1)
    ref_code = args[1].strip() if len(args) > 1 else None
    if ref_code:
        await state.update_data(ref_code=ref_code)
    if uid in user_profiles:
        p = user_profiles[uid]
        await message.answer(
            profile_text(p),
            parse_mode='HTML', reply_markup=main_kb(uid)
        )
        return
    # Bot protection: check account age first
    allowed, reason = await check_account_age(uid)
    if not allowed:
        await message.answer(f"\u26a0\ufe0f {reason}", parse_mode='HTML')
        return
    # Bot protection: IP rate limit
    if not await check_ip_rate_limit(uid):
        await message.answer(
            "\u23f3 Too many signup attempts. Please wait a few minutes and try again.",
            parse_mode='HTML'
        )
        return
    await state.set_state(Signup.name)
    await state.update_data(last_bot_msg=None, prev_bot_msg=None)
    msg = await message.answer(
        "Hey there! 👋\n\n"
        "I'm <b>Winkly</b> — your wingman on Telegram.\n\n"
        "Finding someone real nearby shouldn't feel like a part-time job.\n\n"
        "Let's build your profile — it takes about 30 seconds.\n\n"
        "<b>What should people call you?</b>",
        parse_mode='HTML'
    )
    await state.update_data(last_bot_msg=msg.message_id)

@dp.message(StateFilter(Signup.name))
async def h_name(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    await mark_online(uid)
    d = await state.get_data()
    if d.get('prev_bot_msg'):
        await safe_delete(message.chat.id, d['prev_bot_msg'])
    name = message.text.strip()
    if len(name) < 2:
        await message.answer("\u26a0\ufe0f Name must be at least 2 characters.")
        return
    await state.update_data(name=name, username=message.from_user.username or '')
    await fsm_backup_set(uid, 'name', name)
    await fsm_backup_set(uid, 'username', message.from_user.username or '')
    await state.set_state(Signup.dob)
    msg = await message.answer(
        "Great! <b>When were you born?</b>\n\n"
        "Your age helps us introduce you to people in a similar stage of life.\n\n"
        "Try: 15-08-1998  |  1998/08/15  |  August 15 1998",
        parse_mode='HTML',
        reply_markup=dob_picker_kb(0)
    )
    await state.update_data(prev_bot_msg=msg.message_id)


@dp.message(StateFilter(Signup.gender))
async def h_gender(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    await mark_online(uid)
    raw = message.text.strip()
    nfd = unicodedata.normalize('NFD', raw.lower())
    keyword = ' '.join(re.findall(r'[a-z]+', ''.join(c for c in nfd if unicodedata.category(c) != 'Mn' and ord(c) != 0x200d)))
    GENDER_KW = {'male': 'Male', 'm': 'Male', 'female': 'Female', 'women': 'Female', 'f': 'Female', 'other': 'Other'}
    if keyword not in GENDER_KW:
        await message.answer("\u26a0\ufe0f Please tap a button above.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👨 Male", callback_data="signup_gender:Male")],
            [InlineKeyboardButton(text="👩 Female", callback_data="signup_gender:Female")],
            [InlineKeyboardButton(text="⚕ Other", callback_data="signup_gender:Other")],
        ]))
        return
    await state.update_data(gender=GENDER_KW[keyword])
    await state.set_state(Signup.preferred)
    msg = await message.answer(
        "<b>Who are you looking to meet?</b>",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👨 Men", callback_data="pref:Male")],
            [InlineKeyboardButton(text="👩 Women", callback_data="pref:Female")],
            [InlineKeyboardButton(text="👥 Everyone", callback_data="pref:Everyone")],
        ])
    )
    await state.update_data(prev_bot_msg=msg.message_id)


@dp.message(StateFilter(Signup.preferred))
async def h_preferred(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    await mark_online(uid)
    raw = message.text.strip()
    nfd = unicodedata.normalize('NFD', raw.lower())
    keyword = ' '.join(re.findall(r'[a-z]+', ''.join(c for c in nfd if unicodedata.category(c) != 'Mn' and ord(c) != 0x200d)))
    PREF_KW = {'male': 'Male', 'female': 'Female', 'everyone': 'Everyone',
                'men': 'Male', 'women': 'Female'}
    if keyword not in PREF_KW:
        await message.answer("\u26a0\ufe0f Please tap a button above.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👨 Male", callback_data="pref:Male")],
            [InlineKeyboardButton(text="👩 Female", callback_data="pref:Female")],
            [InlineKeyboardButton(text="👥 Everyone", callback_data="pref:Everyone")],
        ]))
        return
    await state.update_data(preferred=PREF_KW[keyword])
    await fsm_backup_set(uid, 'preferred', PREF_KW[keyword])
    d = await state.get_data()
    if d.get('prev_bot_msg'):
        await safe_delete(message.chat.id, d['prev_bot_msg'])
    await state.set_state(Signup.location)
    msg = await message.answer(
        "<b>Where should we look for matches?</b>\n\n"
        "Share your location or type a city/area name.\n\n"
        "Don't worry — we only show your general area, not your exact address.",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📍 Share My Location", callback_data="loc_share_gps")],
            [InlineKeyboardButton(text="⌨️ Type a Place", callback_data="loc_enter_text")],
        ])
    )
    await state.update_data(prev_bot_msg=msg.message_id)


@dp.message(lambda m: m.location, StateFilter(Signup.location))
async def h_loc_gps(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    await mark_online(uid)
    d = await state.get_data()
    if d.get('prev_bot_msg'):
        await safe_delete(message.chat.id, d['prev_bot_msg'])
    gps_msg_id = d.get('gps_msg_id')
    if gps_msg_id:
        await safe_delete(message.chat.id, gps_msg_id)
    loc = message.location

    if d.get('is_editing'):
        prof = user_profiles.get(uid, {})
        prof.update({'lat': str(loc.latitude), 'lon': str(loc.longitude), 'location_name': 'GPS'})
        user_profiles[uid] = prof
        await save_all()
        await state.clear()
        await message.answer("✅ Location updated!", reply_markup=ReplyKeyboardRemove())
        return

    await state.update_data(lat=str(loc.latitude), lon=str(loc.longitude), location_name='GPS')
    await fsm_backup_set(uid, 'lat', str(loc.latitude))
    await fsm_backup_set(uid, 'lon', str(loc.longitude))
    await fsm_backup_set(uid, 'location_name', 'GPS')
    await state.set_state(Signup.bio)
    msg = await message.answer(
        "<b>Almost done!</b>\n\n"
        "📝 <b>Write a short bio</b> — or skip for now.\n\n"
        "Something like: \"Coffee addict, weekend traveler, looking for a real connection.\"",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭️ Skip for now", callback_data="signup_skip_bio")],
        ])
    )
    await state.update_data(prev_bot_msg=msg.message_id)



@dp.message(StateFilter(Signup.location))
async def h_loc_text(message: types.Message, state: FSMContext):
    if message.photo:
        return  # Let photo handler take it
    uid = message.from_user.id
    await mark_online(uid)
    d = await state.get_data()
    if d.get('prev_bot_msg'):
        await safe_delete(message.chat.id, d['prev_bot_msg'])
    text = (message.text or '').strip()
    if not text:
        return
    # Keyboard button placeholder — silently ignore
    if text in ('\U0001f4cd Share My Location', '\u2328\ufe0f  Enter Place Name', '\U0001f4cd Share Location', 'Enter Place Name'):
        return
    lat, lon = await geocode(text)
    if not lat:
        await message.answer("📍 Couldn't find that place. Try a city name or use <b>Share My Location</b>.", parse_mode='HTML')
        return

    if d.get('is_editing'):
        prof = user_profiles.get(uid, {})
        prof.update({'lat': str(lat), 'lon': str(lon), 'location_name': text})
        user_profiles[uid] = prof
        await save_all()
        await state.clear()
        await message.answer("✅ Location updated!")
        return

    await state.update_data(lat=str(lat), lon=str(lon), location_name=text)
    await fsm_backup_set(uid, 'lat', str(lat))
    await fsm_backup_set(uid, 'lon', str(lon))
    await fsm_backup_set(uid, 'location_name', text)
    await state.set_state(Signup.bio)
    msg = await message.answer(
        "<b>Almost done!</b>\n\n"
        "📝 <b>Write a short bio</b> — or skip for now.\n\n"
        "Something like: \"Coffee addict, weekend traveler, looking for a real connection.\"",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭️ Skip for now", callback_data="signup_skip_bio")],
        ])
    )
    await state.update_data(prev_bot_msg=msg.message_id)







async def finish_signup(state: FSMContext, chat_id: int, uid: int):
    logger.info(f"finish_signup START: uid={uid}")
    try:
        data = await state.get_data()
        logger.info(f"finish_signup data keys: {list(data.keys())}")
        name_val = data.get('name') or await fsm_backup_get(uid, 'name') or ''
        logger.info(f"finish_signup name: '{name_val}'")
        prof = {
            'name': name_val,
            'gender': data.get('gender', ''),
            'preferred_gender': data.get('preferred', ''),
            'bio': data.get('bio', ''),
            'dob': data.get('dob', ''),
            'lat': data.get('lat', ''),
            'lon': data.get('lon', ''),
            'location_name': data.get('location_name', ''),
            'photo': data.get('photo'),
            'verified': True,
            'verification_status': 'verified',
            'username': (data.get('username') or await fsm_backup_get(uid, 'username') or ''),
            'free_texts': FREE_TEXTS_JOINING,
            'rejected': [],
            'received_texts': 0,
        }
        logger.info(f"finish_signup prof built: {prof.get('name')}")
        user_profiles[uid] = prof
        logger.info(f"finish_signup: user_profiles[{uid}] set, profiles_in_memory={len(user_profiles)}")
        logger.info(f"finish_signup: about to call get_redis()...")
        # Direct atomic write — bypasses save_all() which may fail across workers
        r = await get_redis()
        if r:
            try:
                raw = await r.get('winkly:profiles')
                all_profiles = json.loads(raw) if raw else {}
                all_profiles[str(uid)] = prof
                await r.set('winkly:profiles', json.dumps(all_profiles))
                logger.info(f"finish_signup: direct Redis write done, keys={list(all_profiles.keys())}")
            except Exception as e:
                logger.error(f"finish_signup Redis write FAILED: {e}")
        else:
            logger.warning("finish_signup: get_redis() returned None, profile only in memory")
    except Exception as e:
        logger.error(f"finish_signup EXCEPTION: {type(e).__name__}: {e}")
        raise
    ref = data.get('ref_code')
    ref_valid = False
    if ref:
        ref_valid = await credit_referrer(ref, uid)
    await state.clear()
    msg_text = "\U0001f389 <b>Profile complete!</b>\n\n" + profile_text(prof)
    if ref and not ref_valid:
        msg_text += "\n\n⚠️ <i>That referral code wasn't valid.</i>"
    await bot.send_message(
        chat_id,
        msg_text,
        parse_mode='HTML', reply_markup=main_kb(uid)
    )

@dp.message(StateFilter(Signup.bio))
async def h_bio(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    await mark_online(uid)
    d = await state.get_data()
    if d.get('prev_bot_msg'):
        await safe_delete(message.chat.id, d['prev_bot_msg'])

    if message.text and 'skip' in message.text.lower():
        await state.update_data(bio='')
        await finish_signup(state, message.chat.id, uid)
        return

    bio = (message.text or '').strip()
    if bio:
        if len(bio) > 300:
            bio = bio[:300]
        await state.update_data(bio=bio)
    await finish_signup(state, message.chat.id, uid)


def dob_picker_kb(page: int = 0) -> InlineKeyboardMarkup:
    """Year picker: page 0 = recent years (2006-1987), page 1 = older (1986-1967), page 2 = oldest (1966-1950)"""
    all_years = list(range(2006, 1949, -1))  # 2006 down to 1950
    page_size = 20
    start = page * page_size
    page_years = all_years[start:start + page_size]
    rows = []
    for i in range(0, len(page_years), 4):
        chunk = page_years[i:i+4]
        rows.append([InlineKeyboardButton(text=str(y), callback_data=f'signup_dob:{y}') for y in chunk])
    # Navigation
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text='◀️ Younger', callback_data='dob_page:0'))
    if start + page_size < len(all_years):
        nav.append(InlineKeyboardButton(text='Older ▶️', callback_data=f'dob_page:{page+1}'))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text='✏️ Type manually', callback_data='dob_manual')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def h_dob(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    await mark_online(uid)
    d = await state.get_data()
    if d.get('prev_bot_msg'):
        await safe_delete(message.chat.id, d['prev_bot_msg'])

    raw = (message.text or '').strip()
    if not raw:
        await message.answer(
            "<b>Please enter your date of birth.</b>\n\n"
            "Try: 15-08-1998  |  1998/08/15  |  August 15 1998",
            parse_mode='HTML'
        )
        return

    dob = parse_dob(raw)
    if dob is None:
        await message.answer(
            "<b>Hmm, we couldn't read that date.</b>\n\n"
            "Try: 15-08-1998  |  1998/08/15  |  August 15 1998",
            parse_mode='HTML'
        )
        return

    age = calc_age(dob)
    if age < 18:
        await message.answer(
            "\u26d4\ufe0f <b>You must be 18+ to use this bot.</b>",
            parse_mode='HTML'
        )
        return
    if age > 100:
        await message.answer(
            "\u26a0\ufe0f <b>Please enter a valid birth year.</b>",
            parse_mode='HTML'
        )
        return

    await state.update_data(dob=raw)
    await state.set_state(Signup.gender)
    msg = await message.answer(
        "<b>What's your gender?</b>\n\n"
        "This is shown on your profile so people know who they're talking to.",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👨 Male", callback_data="signup_gender:Male")],
            [InlineKeyboardButton(text="👩 Female", callback_data="signup_gender:Female")],
            [InlineKeyboardButton(text="⚕ Other", callback_data="signup_gender:Other")],
        ])
    )
    await state.update_data(prev_bot_msg=msg.message_id)




# ─── /profile ────────────────────────────────────────────────────────────────

@dp.message(Command('profile'))
async def cmd_profile(message: types.Message):
    uid = message.from_user.id
    await mark_online(uid)
    if uid not in user_profiles:
        await message.answer("📝 You haven't set up a profile yet.\nSend /start to begin!")
        return
    await message.answer(
        profile_text(user_profiles[uid]),
        parse_mode='HTML', reply_markup=main_kb(uid)
    )

# ─── /find ──────────────────────────────────────────────────────────────────

@dp.message(Command('find'))
async def cmd_find(message: types.Message):
    uid = message.from_user.id
    await mark_online(uid)
    if uid not in user_profiles:
        await message.answer("📝 Set up your profile first with /start.")
        return
    await message.answer(
        "\u2764\ufe0f <b>Looking for matches?</b>\n\nTap below to find people nearby!",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="\u2764\ufe0f  Find Matches Now", callback_data='do_match')],
        ])
    )

# ─── /stop ──────────────────────────────────────────────────────────────────

@dp.message(Command('stop'))
async def cmd_stop(message: types.Message):
    uid = message.from_user.id
    await mark_online(uid)
    if uid not in current_chat:
        await message.answer("\U0001f51a You're not in any chat right now.")
        return
    partner = current_chat.pop(uid, None)
    if partner:
        current_chat.pop(partner, None)
        await save_all()
    if partner and partner in user_profiles:
        try:
            await bot.send_message(
                partner,
                f"🔚 <b>Chat ended.</b>\n\n{user_profiles[uid]['name']} left the chat.",
                parse_mode='HTML'
            )
        except:
            pass
    # Clean up active_matches so they can rematch
    if partner:
        active_matches.get(uid, {}).pop(partner, None)
        active_matches.get(partner, {}).pop(uid, None)
    await message.answer("🔚 <b>Chat ended.</b>\n\nWhat would you like to do next?",
            parse_mode='HTML', reply_markup=reengage_kb(uid))

# ─── /verify ────────────────────────────────────────────────────────────────

@dp.message(Command('verify'))
async def cmd_verify(message: types.Message):
    uid = message.from_user.id
    await mark_online(uid)
    if uid not in user_profiles:
        await message.answer("📝 Please set up your profile first with /start.")
        return
    await message.answer(
        "\U0001f3c5 <b>Already Verified!</b>\n\n"
        "\u2705 Your profile has a verified badge \u2714\ufe0f\n\n"
        "Go find your match! 💕",
        parse_mode='HTML', reply_markup=main_kb(uid)
    )

# ─── /premium ───────────────────────────────────────────────────────────────

@dp.message(Command('premium'))
async def cmd_premium(message: types.Message):
    uid = message.from_user.id
    await mark_online(uid)
    if uid not in user_profiles:
        await message.answer("📝 You haven't set up a profile yet.\nSend /start to begin!")
        return
    if is_premium(uid):
        exp_str = premium_subscriptions[uid].get('expiry_date', '')
        try:
            exp = datetime.fromisoformat(exp_str)
            days = (exp - datetime.now()).days
            await message.answer(
                f"\U0001f3c6 <b>Premium Active!</b>\n\nExpires in {days} day{'s' if days != 1 else ''}\n"
                "\u2705 Unlimited texts and matches\n\nWhat would you like to do?",
                parse_mode='HTML', reply_markup=main_kb(uid)
            )
        except:
            await message.answer("\U0001f3c6 <b>Premium Active!</b>\n\nUnlimited access!",
                                 parse_mode='HTML', reply_markup=main_kb(uid))
        return
    await message.answer(
        f"\U0001f3c6 <b>Premium Plans</b>\n\n{quota_summary(uid)}\n\nChoose a plan:",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🌟 1 Day Trial — ₹1", callback_data='premium_1day')],
            [InlineKeyboardButton(text="\U0001f4cb See All Plans", callback_data='premium_plans')],
        ])
    )

# ─── /refer ──────────────────────────────────────────────────────────────────

@dp.message(Command('refer'))
async def cmd_refer(message: types.Message):
    uid = message.from_user.id
    await mark_online(uid)
    if uid not in user_profiles:
        await message.answer("📝 Set up your profile first, then send /refer")
        return
    code = referral_code(uid)
    cnt = await referral_count(uid)
    await message.answer(
        f"\U0001f389 <b>Refer & Earn Free Premium!</b>\n\n"
        f"Your code: <code>{code}</code>\n\n"
        f"Share this bot with friends. When 3 of them complete their profile, "
        f"you get <b>1 Day Free Premium!</b> \U0001f3c6\n\n"
        f"Progress: {cnt}/3 referrals",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="\U0001f4e4 Share Bot", callback_data='share_bot')],
        ])
    )

@dp.callback_query(lambda cb: cb.data == 'share_bot')
async def share_bot(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    code = referral_code(uid)
    await cb.message.edit_text(
        f"\U0001f4e4 <b>Share Winkly with friends!</b>\n\n"
        f"Send them this link: t.me/{BOT_USERNAME}?start={code}\n\n"
        f"Or share your code: <code>{code}</code>\n\n"
        f"3 signups = 1 free day! \U0001f389",
        parse_mode='HTML'
    )
    await cb.answer()

# ─── Verification callbacks ────────────────────────────────────────────────────

@dp.callback_query(lambda cb: cb.data == 'verify_start')
async def verify_start(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await mark_online(uid)
    if uid not in user_profiles:
        await cb.message.edit_text("📝 Please set up your profile first with /start.")
        await cb.answer()
        return
    await cb.message.edit_text(
        "✅ <b>You're Verified!</b>\n\n"
        "All users are auto-verified at signup. Go find your match! 💕",
        parse_mode='HTML', reply_markup=main_kb(uid)
    )
    await cb.answer()


@dp.callback_query(lambda cb: cb.data == 'reverify')
async def reverify(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await mark_online(uid)
    if uid not in user_profiles:
        await cb.message.edit_text("📝 Please set up your profile first with /start.")
        await cb.answer()
        return
    await cb.message.edit_text(
        "✅ <b>You're Verified!</b>\n\n"
        "All users are auto-verified at signup. Go find your match! 💕",
        parse_mode='HTML', reply_markup=main_kb(uid)
    )
    await cb.answer()
    text = (
        '📸 <b>Selfie Verification</b>\n\n'
        'Tap the button below to open your <b>front camera</b>.\n'
        '✔️ All genders get a <b>verified badge</b>.\n'
        '🏆 <b>Female</b> users also get <b>unlimited free access</b>!'
    )
    markup = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text='📸 Take Selfie', request_photo=True)]],
        one_time_keyboard=True,
        resize_keyboard=True
    )
    try:
        await cb.message.edit_text(text, parse_mode='HTML', reply_markup=markup)
    except:
        await cb.message.answer(text, parse_mode='HTML', reply_markup=markup)
    await cb.answer()


@dp.message(StateFilter(Verify.photo))
async def h_verify_photo(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "✅ <b>You're Verified!</b>\n\n"
        "All users are auto-verified at signup. Go find your match! 💕",
        parse_mode='HTML', reply_markup=main_kb(message.from_user.id)
    )
    return  # all users already verified at signup

    # --- dead code below kept as stub references to avoid breaking other code ---


async def send_verification_to_admin(uid: int):
    """Stub — verification is now automatic, admin approval no longer needed."""
    pass

@dp.callback_query(lambda cb: cb.data.startswith('admin_approve:'))
async def admin_approve(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_CHAT_ID:
        await cb.answer("\u26d4 Not authorized.", show_alert=True)
        return
    uid = int(cb.data.split(':')[1])
    p = user_profiles.get(uid)
    if not p:
        await cb.message.edit_text(cb.message.text + "\n\n_(User deleted)_")
        await cb.answer("User gone.")
        return
    p['verified'] = True
    p['verification_status'] = 'approved'
    # p.pop('selfie', None)
    await save_all()
    try:
        txt = cb.message.caption or cb.message.text
        await cb.message.edit_caption(caption=txt + "\n\n\u2705 <b>Approved</b>", parse_mode='HTML')
    except:
        pass
    try:
        await bot.send_message(
            uid,
            "\U0001f389 <b>You're Verified!</b>\n\n"
            "Go find your match! \u2764\ufe0f",
            parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="\u2764\ufe0f  Find Matches", callback_data='do_match')],
            ])
        )
    except:
        pass
    await cb.answer("\u2705 Approved!")

@dp.callback_query(lambda cb: cb.data.startswith('admin_reject:'))
async def admin_reject(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_CHAT_ID:
        await cb.answer("\u26d4 Not authorized.", show_alert=True)
        return
    uid = int(cb.data.split(':')[1])
    p = user_profiles.get(uid)
    if not p:
        await cb.message.edit_text(cb.message.text + "\n\n_(User deleted)_")
        await cb.answer("User gone.")
        return
    p['verification_status'] = 'rejected'
    # p.pop('selfie', None)
    await save_all()
    try:
        txt = cb.message.caption or cb.message.text
        await cb.message.edit_caption(caption=txt + "\n\n\u274c <b>Rejected</b>", parse_mode='HTML')
    except:
        pass
    await bot.send_message(
            uid,
            "\u274c <b>Profile Removed</b>\n\n"
            "Your profile has been removed from the verification queue.",
            parse_mode='HTML', reply_markup=main_kb(uid)
        )
    await cb.answer("\u274c Rejected")

# ─── Match finding ───────────────────────────────────────────────────────────

@dp.callback_query(lambda cb: cb.data == 'do_match')
async def do_match(cb: types.CallbackQuery):
    uid = cb.from_user.id
    await mark_online(uid)
    if uid not in user_profiles:
        await cb.message.edit_text("⚠️ No profile found. Please send /start.")
        await cb.answer()
        return
    me = user_profiles[uid]
    m = find_queue_match(me, uid)
    if m:
        pid = m['uid']
        active_matches.setdefault(uid, {})
        active_matches.setdefault(pid, {})
        active_matches[uid][pid] = {'status': 'matched'}
        active_matches[pid][uid] = {'status': 'matched'}
        for x in (uid, pid):
            if x in waiting_queue:
                del waiting_queue[x]
            if x in _queue_msg_ids:
                await safe_delete(x, _queue_msg_ids.pop(x))
        await save_all()
        await cb.message.delete()
        await send_match_card(uid, m, pid)
        await send_match_card(pid, {**user_profiles[uid], 'uid': uid}, uid)
        await cb.answer()
        return
    waiting_queue[uid] = {'added_at': datetime.now().isoformat(), 'retries': 0}
    online = await get_online_count()
    ql = len(waiting_queue)
    sm = await cb.message.edit_text(
        f"\U0001f465 <b>{online} people online</b> | \u23f3 <b>{ql} in queue</b>\n\n"
        f"\U0001f464 <b>{me['name']}</b>, searching for someone compatible...\n\n"
        "\u23f3 <b>Searching (attempt 1/3)...</b>\n\n"
        "_The search will retry up to 3 times if no match is found._",
        parse_mode='Markdown'
    )
    _queue_msg_ids[uid] = sm.message_id
    await save_all()
    await cb.answer()

async def send_match_card(cid: int, partner: dict, pid: int):
    n = partner.get('name', '?')
    g = partner.get('gender', '?')
    txt = (
        f"🎉 <b>It's a match!</b>\n\n"
        f"You and <b>{n}</b> seem to be on the same wavelength.\n\n"
        f"Say something nice. 💬"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Chat Now", callback_data=f'chat:{pid}')],
    ])
    await bot.send_message(cid, txt, parse_mode='HTML', reply_markup=kb)

# ─── Chat ────────────────────────────────────────────────────────────────────

@dp.callback_query(lambda cb: cb.data.startswith('skip_match:'))
async def skip_match(cb: types.CallbackQuery):
    uid = cb.from_user.id
    await mark_online(uid)
    pid = int(cb.data.split(':')[1])
    p = user_profiles.get(uid)
    if p:
        if 'rejected' not in p:
            p['rejected'] = []
        if pid not in p['rejected']:
            p['rejected'].append(pid)
            await save_all()
    # Remove from active_matches if present
    active_matches.get(uid, {}).pop(pid, None)
    active_matches.get(pid, {}).pop(uid, None)
    # Delete old match card (may be a photo — edit_text fails on media messages)
    await safe_delete(uid, cb.message.message_id)
    await bot.send_message(
        uid,
        "⏭️ <b>Skipped.</b> You won't see this person again.\n\n"
        "What would you like to do next?",
        parse_mode='HTML', reply_markup=reengage_kb(uid)
    )
    await cb.answer()

@dp.callback_query(lambda cb: cb.data.startswith('chat:'))
async def start_chat(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await mark_online(uid)
    pid = int(cb.data.split(':')[1])
    if uid not in active_matches or pid not in active_matches.get(uid, {}):
        await cb.answer("⚠️ You are not matched with this user.", show_alert=True)
        return
    if not check_text_quota(uid):
        await cb.answer("⚠️ No free texts remaining. Upgrade to continue.", show_alert=True)
        return
    # Clear any stale FSM state (e.g. from an unfinished edit-profile flow)
    # so inline keyboard handlers don't intercept chat messages.
    await state.clear()
    # Set current_chat AFTER quota check passes
    current_chat[uid] = pid
    current_chat[pid] = uid
    await save_all()
    pname = user_profiles.get(pid, {}).get('name', 'Someone')
    # Delete old match card (may be a photo — edit_text fails on media messages)
    await safe_delete(uid, cb.message.message_id)
    # Send chat interface — user controls when to say hi
    await bot.send_message(
        uid,
        f"\U0001f4ac <b>Chat with {pname}</b>\n\n"
        "Send your messages below. Tap <b>Say Hi</b> to introduce yourself!\n\n"
        "Use /stop to end the chat.",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="\U0001f44b Say Hi", callback_data=f'say_hi:{pid}')],
            [InlineKeyboardButton(text="\U0001f51a End Chat", callback_data='end_chat')],
        ])
    )
    # Notify partner that someone started a chat — they will see your hi when you send it
    try:
        await bot.send_message(
            pid,
            f"💬 <b>{user_profiles[uid]['name']}</b> just started a chat with you! Say hello. 👋",
            parse_mode='HTML'
        )
    except:
        pass
    await cb.answer()

@dp.callback_query(lambda cb: cb.data.startswith('say_hi:'))
async def say_hi(cb: types.CallbackQuery):
    uid = cb.from_user.id
    await mark_online(uid)
    pid = int(cb.data.split(':')[1])
    if current_chat.get(uid) != pid:
        await cb.answer("⚠️ Not in an active chat.", show_alert=True)
        return
    if not check_text_quota(uid):
            await cb.message.edit_text(
                "⏸️ <b>Text limit reached</b>\n\nYou've used all your free texts.",
                parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🌟 1 Day Trial — ₹1", callback_data='premium_1day'),
                     InlineKeyboardButton(text="\U0001f4cb Plans", callback_data='premium_plans')],
                ])
            )
            if uid not in _reconnect_tasks:
                _reconnect_tasks[uid] = asyncio.create_task(_reconnect_loop(uid, pid))
            await cb.answer()
            return
    pname = user_profiles[uid]['name']
    try:
        await bot.send_message(pid, f"\U0001f44b <b>{pname}</b> said: Hi!", parse_mode='HTML')
        consume_text(uid)
        await save_all()
    except:
        pass
    try:
        await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="\U0001f51a End Chat", callback_data='end_chat')],
        ]))
    except:
        pass
    await cb.answer()

@dp.callback_query(lambda cb: cb.data == 'cancel_queue')
async def cancel_queue(cb: types.CallbackQuery):
    uid = cb.from_user.id
    await mark_online(uid)
    if uid in waiting_queue:
        del waiting_queue[uid]
    if uid in _queue_msg_ids:
        await safe_delete(uid, _queue_msg_ids.pop(uid))
    await cb.message.edit_text(
        "\U0001f504 <b>Search cancelled.</b>\n\nWhat would you like to do next?",
        parse_mode='HTML', reply_markup=reengage_kb(uid)
    )
    await save_all()
    await cb.answer()

@dp.callback_query(lambda cb: cb.data == 'end_chat')
async def end_chat(cb: types.CallbackQuery):
    uid = cb.from_user.id
    await mark_online(uid)
    # Cancel reconnect loop if running (hold user ended chat)
    _cleanup_reconnect(uid)
    partner = current_chat.pop(uid, None)
    if partner:
        current_chat.pop(partner, None)
    if partner and partner in user_profiles:
        try:
            await bot.send_message(
                partner,
                f"🔚 <b>Chat ended.</b>\n\n{user_profiles[uid]['name']} left the chat.",
                parse_mode='HTML'
            )
        except:
            pass
    # Clean up active_matches so they can rematch
    active_matches.get(uid, {}).pop(partner, None)
    active_matches.get(partner, {}).pop(uid, None)
    await save_all()
    await cb.message.edit_text(
        "\U0001f51a <b>Chat ended.</b>\n\nWhat would you like to do next?",
        parse_mode='HTML', reply_markup=reengage_kb(uid)
    )
    await cb.answer()


@dp.callback_query(lambda cb: cb.data == 'wait_hold')
async def wait_hold(cb: types.CallbackQuery):
    """Dismiss the 'account on hold' message (relay path — separate message)."""
    await cb.message.delete()
    await cb.answer()


@dp.callback_query(lambda cb: cb.data == 'wait_reconnect')
async def wait_reconnect(cb: types.CallbackQuery):
    """Partner clicked Wait — restart the reconnect loop."""
    await cb.message.delete()
    await cb.answer()
    # Find the hold user's uid from current_chat
    b_uid = cb.from_user.id
    a_uid = current_chat.get(b_uid)
    if a_uid:
        _cleanup_reconnect(a_uid)
        if a_uid in current_chat and not is_premium(a_uid) and not check_text_quota(a_uid):
            _reconnect_tasks[a_uid] = asyncio.create_task(_reconnect_loop(a_uid, b_uid))


@dp.callback_query(lambda cb: cb.data == 'end_reconnect')
async def end_reconnect(cb: types.CallbackQuery):
    """Partner clicked End Chat — end the chat for both."""
    b_uid = cb.from_user.id
    await mark_online(b_uid)
    a_uid = current_chat.pop(b_uid, None)
    if a_uid:
        current_chat.pop(a_uid, None)
        _cleanup_reconnect(a_uid)
        if a_uid in user_profiles:
            try:
                await bot.send_message(
                    a_uid,
                    f"\U0001f51a <b>Chat ended.</b>\n\n{user_profiles[b_uid]['name']} ended the chat.",
                    parse_mode='HTML'
                )
            except:
                pass
        # Clean up active_matches
        active_matches.get(b_uid, {}).pop(a_uid, None)
        active_matches.get(a_uid, {}).pop(b_uid, None)
    await save_all()
    await cb.message.edit_text(
        "\U0001f51a <b>Chat ended.</b>\n\nWhat would you like to do next?",
        parse_mode='HTML', reply_markup=reengage_kb(b_uid)
    )
    await cb.answer()


@dp.callback_query(lambda cb: cb.data.startswith('wait_in_chat:'))
async def wait_in_chat(cb: types.CallbackQuery):
    """Restore chat interface after 'account on hold' (say_hi path — was edited in place)."""
    uid = cb.from_user.id
    await mark_online(uid)
    pid = int(cb.data.split(':', 1)[1])
    pname = user_profiles.get(pid, {}).get('name', 'Someone')
    await cb.message.edit_text(
        f"\U0001f4ac <b>Chat with {pname}</b>\n\n"
        "Send your messages below. Tap <b>Say Hi</b> to introduce yourself!\n\n"
        "Use /stop to end the chat.",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="\U0001f44b Say Hi", callback_data=f'say_hi:{pid}'),
             InlineKeyboardButton(text="\U0001f51a End Chat", callback_data='end_chat')],
        ])
    )
    await cb.answer()

# ─── Relay messages ───────────────────────────────────────────────────────────

@dp.message(lambda msg: msg.from_user.id in current_chat)
async def relay(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    await mark_online(uid)
    if uid not in user_profiles:
        return
    if uid not in current_chat:
        return
    # If user is in any FSM state (editing profile, signup, verify),
    # don't relay — the state-specific handler should process the message.
    current_state = await state.get_state()
    if current_state is not None:
        return
    pid = current_chat[uid]
    if not check_text_quota(uid):
                base = "⏸️ <b>Text limit reached</b>\n\nYou've used all your free messages. Upgrade to keep chatting or find a new match."
                await message.answer(
                    base,
                    parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🌟 1 Day Trial — ₹1", callback_data='premium_1day'),
                         InlineKeyboardButton(text="\U0001f4cb Plans", callback_data='premium_plans')],
                    ])
                )
                if uid not in _reconnect_tasks:
                    _reconnect_tasks[uid] = asyncio.create_task(_reconnect_loop(uid, pid))
                return
    # Receiver's receive limit: Male/Other without premium limited to RECEIVE_LIMIT total
    p_receiver = user_profiles.get(pid)
    if p_receiver and p_receiver.get('gender') in ('Male', 'Other') and not is_premium(pid):
        received = p_receiver.get('received_texts', 0)
        if received >= RECEIVE_LIMIT:
            # Reuse the existing "Text limit reached" bubble for the sender
            pname = user_profiles[uid].get('name', 'Someone')
            n = _quota_notif.get(uid)
            count = (n['count'] + 1) if n else 1
            text = "⏸️ <b>Text limit reached</b>\n\nYou've used all your free messages."
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🌟 1 Day Trial — ₹1", callback_data='premium_1day'),
                 InlineKeyboardButton(text="\U0001f4cb Plans", callback_data='premium_plans')],
            ])
            if n:
                try:
                    await bot.edit_message_text(text, uid, n['mid'], parse_mode='HTML', reply_markup=kb)
                except:
                    msg = await bot.send_message(uid, text, parse_mode='HTML', reply_markup=kb)
                    _quota_notif[uid] = {'mid': msg.message_id, 'count': count}
                else:
                    _quota_notif[uid] = {'mid': n['mid'], 'count': count}
            else:
                msg = await bot.send_message(uid, text, parse_mode='HTML', reply_markup=kb)
                _quota_notif[uid] = {'mid': msg.message_id, 'count': count}
            await save_all()
            return
    # Receiver can always receive messages — no quota check
    try:
        # If receiver has no quota, show notification instead of forwarding
        if not check_text_quota(pid):
            pname = user_profiles[uid].get('name', 'Someone')
            n = _quota_notif.get(pid)
            count = (n['count'] + 1) if n else 1
            text = f"📬 <b>{pname}</b> sent {count} message{'s' if count > 1 else ''}. Upgrade to read and reply."
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🌟 1 Day Trial — ₹1", callback_data='premium_1day')],
                [InlineKeyboardButton(text="\U0001f4cb See All Plans", callback_data='premium_plans')],
            ])
            if n:
                try:
                    await bot.edit_message_text(text, pid, n['mid'], parse_mode='HTML', reply_markup=kb)
                except:
                    # If edit fails (message deleted), send new one
                    msg = await bot.send_message(pid, text, parse_mode='HTML', reply_markup=kb)
                    _quota_notif[pid] = {'mid': msg.message_id, 'count': count}
                else:
                    _quota_notif[pid] = {'mid': n['mid'], 'count': count}
            else:
                msg = await bot.send_message(pid, text, parse_mode='HTML', reply_markup=kb)
                _quota_notif[pid] = {'mid': msg.message_id, 'count': count}
            await save_all()
            return
        await bot.copy_message(pid, message.chat.id, message.message_id)
        consume_text(uid)
        # Increment receiver's received_texts (for Male/Other without premium)
        p_recv = user_profiles.get(pid)
        if p_recv and p_recv.get('gender') in ('Male', 'Other') and not is_premium(pid):
            p_recv['received_texts'] = p_recv.get('received_texts', 0) + 1
        await save_all()
    except:
        await message.answer("⚠️ Couldn't deliver your message.")

# ─── Premium callbacks ───────────────────────────────────────────────────────

@dp.callback_query(lambda cb: cb.data == 'premium_1day')
async def prem_1(cb: types.CallbackQuery):
    uid = cb.from_user.id
    await mark_online(uid)
    if is_premium(uid):
        await cb.message.edit_text("\U0001f3c6 <b>Already Premium!</b>\n\nYou have unlimited access.",
                                    parse_mode='HTML', reply_markup=main_kb(uid))
        await cb.answer()
        return
    await cb.message.edit_text("\u23f3 Creating payment link...")
    url = await make_payment_link(uid, "TEST 1 Day", 1, 1)
    if not url:
        await cb.message.edit_text(
            "⚠️ Couldn't create payment link. Please try again later.",
            parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="\u25c0\ufe0f Back", callback_data='back_to_premium')],
            ])
        )
        await cb.answer("Payment failed", show_alert=True)
        return
    await cb.message.edit_text(
        "🌟 <b>1 Day Unlimited — ₹1</b>\n\n"
        "Chat freely with your match. No limits for 24 hours.\n\n"
        "Tap below to complete payment:",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Pay ₹1 — Activate Now", url=url)],
            [InlineKeyboardButton(text="\U0001f4cb See All Plans", callback_data='premium_plans')],
            [InlineKeyboardButton(text="\U0001f464 View Profile", callback_data='back_to_profile')],
        ])
    )
    await cb.answer()

@dp.callback_query(lambda cb: cb.data == 'premium_plans')
async def prem_plans(cb: types.CallbackQuery):
    uid = cb.from_user.id
    await mark_online(uid)
    rows = [[InlineKeyboardButton(
        text=f"\U0001f3c6 {p['name']} — Rs{p['price']} (Save {int((1 - p['price']/(49*p['duration']))*100)}%)",
        callback_data=f"premium_select:{p['name']}:{p['price']}:{p['duration']}"
    )] for p in LONG_PLANS]
    rows.append([InlineKeyboardButton(text="🌟 1 Day Trial — ₹1", callback_data='premium_1day')])
    rows.append([InlineKeyboardButton(text="\u25c0\ufe0f Back", callback_data='back_to_premium')])
    await cb.message.edit_text(
        "\U0001f3c6 <b>Premium Plans</b>\n\n" +
        "\n".join(f"• {p['name']} — Rs{p['price']} (~Rs{round(p['price']/p['duration'])}/day, "
                   f"Save {int((1-p['price']/(49*p['duration']))*100)}%)" for p in LONG_PLANS) +
        "\n\nOr get 1 day for just Rs49.",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await cb.answer()

@dp.callback_query(lambda cb: cb.data.startswith('premium_select:'))
async def prem_sel(cb: types.CallbackQuery):
    uid = cb.from_user.id
    await mark_online(uid)
    _, name, price, dur = cb.data.split(':')
    price, dur = int(price), int(dur)
    await cb.message.edit_text(f"\u23f3 Creating payment link for <b>{name}</b>...")
    url = await make_payment_link(uid, name, price, dur)
    if not url:
        await cb.message.edit_text(
            "⚠️ Couldn't create payment link. Please try again later.",
            parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="\u25c0\ufe0f Back", callback_data='premium_plans')],
            ])
        )
        await cb.answer("Payment failed", show_alert=True)
        return
    await cb.message.edit_text(
        f"\U0001f3c6 <b>{name} Premium — Rs{price}</b>\n\n"
        f"\u2705 Unlimited texts and matches for {dur} days\n\n"
        "Tap below to complete payment:",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"\U0001f4b3 Pay Rs{price}", url=url)],
            [InlineKeyboardButton(text="\u25c0\ufe0f Back", callback_data='premium_plans')],
        ])
    )
    await cb.answer()

@dp.callback_query(lambda cb: cb.data == 'back_to_premium')
async def back_prem(cb: types.CallbackQuery):
    uid = cb.from_user.id
    await mark_online(uid)
    if is_premium(uid):
        await cb.message.edit_text("\U0001f3c6 <b>Already Premium!</b>", parse_mode='HTML', reply_markup=main_kb(uid))
    else:
        await cb.message.edit_text(
            f"\U0001f3c6 <b>Premium Plans</b>\n\n{quota_summary(uid)}\n\nChoose a plan:",
            parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🌟 1 Day Trial — ₹1", callback_data='premium_1day')],
                [InlineKeyboardButton(text="\U0001f4cb See All Plans", callback_data='premium_plans')],
            ])
        )
    await cb.answer()

@dp.callback_query(lambda cb: cb.data == 'see_premium')
async def see_prem(cb: types.CallbackQuery):
    await back_prem(cb)

# ─── Edit profile callbacks ──────────────────────────────────────────────────

@dp.callback_query(lambda cb: cb.data == 'back_to_profile')
async def btp(cb: types.CallbackQuery):
    uid = cb.from_user.id
    await mark_online(uid)
    if uid not in user_profiles:
        await cb.message.answer("📝 No profile found. Send /start!")
        await cb.answer()
        return
    await cb.message.edit_text(
        profile_text(user_profiles[uid]),
        parse_mode='HTML', reply_markup=main_kb(uid)
    )
    await cb.answer()

@dp.callback_query(lambda cb: cb.data == 'edit_profile')
async def edit_p(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await mark_online(uid)
    await cb.message.edit_text(
        "\u270f\ufe0f <b>Edit Profile</b>\n\nSelect what you'd like to change:",
        parse_mode='HTML', reply_markup=edit_profile_kb()
    )
    await cb.answer()

@dp.callback_query(lambda cb: cb.data == 'edit_name')
async def edit_n(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await mark_online(uid)
    # Guard: don't activate during signup
    st = await state.get_state()
    if st and st.startswith('Signup:'):
        await cb.answer("⚠️ Finish signup first!", show_alert=True)
        return
    await state.set_state(EditProfile.name)
    await cb.message.edit_text(
        "\u270f\ufe0f <b>Edit Name</b>\n\nWhat's your new name?",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="\u00ab  Back", callback_data='edit_profile')],
        ])
    )
    await cb.answer()

@dp.callback_query(lambda cb: cb.data == 'edit_bio')
async def edit_b(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await mark_online(uid)
    st = await state.get_state()
    if st and st.startswith('Signup:'):
        await cb.answer("⚠️ Finish signup first!", show_alert=True)
        return
    await state.set_state(EditProfile.bio)
    await cb.message.edit_text(
        "\u270f\ufe0f <b>Edit Bio</b>\n\nTell us about yourself:",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="\u00ab  Back", callback_data='edit_profile')],
        ])
    )
    await cb.answer()

async def _guard_edit(state: FSMContext) -> bool:
    """Returns True if in Signup state (should block)."""
    st = await state.get_state()
    return bool(st and st.startswith('Signup:'))

@dp.callback_query(lambda cb: cb.data == 'edit_preferred')
async def edit_pref_start(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await mark_online(uid)
    if await _guard_edit(state):
        await cb.answer("⚠️ Finish signup first!", show_alert=True)
        return
    await state.set_state(EditProfile.preferred)
    await cb.message.edit_text(
        "\u270f\ufe0f <b>Edit Interested In</b>\n\n"
        "\U0001f49d <b>Who are you interested in?</b>",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👨 Male", callback_data="edit_pref:Male")],
            [InlineKeyboardButton(text="👩 Female", callback_data="edit_pref:Female")],
            [InlineKeyboardButton(text="👥 Everyone", callback_data="edit_pref:Everyone")],
            [InlineKeyboardButton(text="\u00ab  Back", callback_data='edit_profile')],
        ])
    )
    await cb.answer()


@dp.callback_query(lambda cb: cb.data == 'edit_gender_preferred')
async def edit_gp(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await mark_online(uid)
    if await _guard_edit(state):
        await cb.answer("⚠️ Finish signup first!", show_alert=True)
        return
    await cb.message.edit_text(
        "\u270f\ufe0f <b>Edit Interested In</b>\n\n"
        "\U0001f49d <b>Who are you interested in?</b>",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👨 Male", callback_data="edit_pref:Male")],
            [InlineKeyboardButton(text="👩 Female", callback_data="edit_pref:Female")],
            [InlineKeyboardButton(text="👥 Everyone", callback_data="edit_pref:Everyone")],
            [InlineKeyboardButton(text="\u00ab  Back", callback_data='edit_profile')],
        ])
    )
    await cb.answer()

@dp.callback_query(lambda cb: cb.data == 'edit_location')
async def edit_l(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await mark_online(uid)
    if await _guard_edit(state):
        await cb.answer("⚠️ Finish signup first!", show_alert=True)
        return
    await state.set_state(EditProfile.location)
    await state.update_data(is_editing=True)
    await cb.message.edit_text(
        "\u270f\ufe0f <b>Edit Location</b>\n\n"
        "📍 Please share your location or type a place name:",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📍 Share My Location", callback_data="loc_share_gps_edit")],
            [InlineKeyboardButton(text="⌨️  Enter Place Name", callback_data="loc_enter_text_edit")],
            [InlineKeyboardButton(text="\u00ab  Back", callback_data='edit_profile')],
        ])
    )
    await cb.answer()

# === DOB year picker callbacks ===
@dp.callback_query(lambda cb: cb.data.startswith('signup_dob:'))
async def cb_signup_dob(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await mark_online(uid)
    d = await state.get_data()
    year = int(cb.data.split(':')[1])
    await state.update_data(dob=str(year), prev_bot_msg=None)
    await state.set_state(Signup.gender)
    await cb.message.edit_text(
        "<b>Step 3 of 6</b>\n\n"
        "\u2696\ufe0f <b>What's your gender?</b>\n"
        "<i>This cannot be changed later.</i>",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👨 Male", callback_data="signup_gender:Male")],
            [InlineKeyboardButton(text="👩 Female", callback_data="signup_gender:Female")],
            [InlineKeyboardButton(text="⚕ Other", callback_data="signup_gender:Other")],
        ])
    )
    await cb.answer()

@dp.callback_query(lambda cb: cb.data.startswith('dob_page:'))
async def cb_signup_dob_page(cb: types.CallbackQuery):
    page = int(cb.data.split(':')[1])
    try:
        await cb.message.edit_reply_markup(reply_markup=dob_picker_kb(page))
    except:
        pass
    await cb.answer()

@dp.callback_query(lambda cb: cb.data == 'dob_manual')
async def cb_signup_dob_manual(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await mark_online(uid)
    await state.set_state(Signup.dob)
    await cb.message.edit_text(
        "\u26a0\ufe0f <b>Please enter your date of birth.</b>\n\n"
        "Try: 15-08-1998  |  1998/08/15  |  August 15 1998",
        parse_mode='HTML'
    )
    await cb.answer()


# === Signup inline button callbacks ===

@dp.callback_query(lambda cb: cb.data.startswith('signup_gender:'))
async def cb_signup_gender(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await mark_online(uid)
    gender = cb.data.split(':', 1)[1]
    await state.update_data(gender=gender, prev_bot_msg=None)
    await state.set_state(Signup.preferred)
    await cb.message.edit_text(
        "<b>Step 4 of 6</b>\n\n"
        "\U0001f49d <b>Who are you interested in?</b>",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👨 Male", callback_data="pref:Male")],
            [InlineKeyboardButton(text="👩 Female", callback_data="pref:Female")],
            [InlineKeyboardButton(text="👥 Everyone", callback_data="pref:Everyone")],
        ])
    )
    await cb.answer()


@dp.callback_query(lambda cb: cb.data.startswith('pref:'))
async def cb_pref(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await mark_online(uid)
    preferred = cb.data.split(':', 1)[1]
    await state.update_data(preferred=preferred, prev_bot_msg=None)
    await fsm_backup_set(uid, 'preferred', preferred)
    await state.set_state(Signup.location)
    await cb.message.edit_text(
        "<b>Step 5 of 6</b>\n\n"
        "\U0001f4cd <b>Share your location</b> or type a place name:",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📍 Share My Location", callback_data="loc_share_gps")],
            [InlineKeyboardButton(text="⌨️  Enter Place Name", callback_data="loc_enter_text")],
        ])
    )
    await cb.answer()


@dp.callback_query(lambda cb: cb.data == 'loc_share_gps')
async def cb_loc_share_gps(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await mark_online(uid)
    await state.set_state(Signup.location)
    await cb.message.delete()
    sent = await cb.message.answer(
        "\U0001f4cd <b>Share your location</b>\n\n"
        "Tap the button below to share your GPS location.\n"
        "If GPS is off, please turn it on in Settings, then try again.\n\n"
        "Or use \U0001f4ce \u2192 Location to pick from the map (works without GPS).\n"
        "Or type a city name.",
        parse_mode='HTML',
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="\U0001f4cd  Share My Location", request_location=True)]],
            resize_keyboard=True, one_time_keyboard=True
        )
    )
    await state.update_data(gps_msg_id=sent.message_id)
    await cb.answer()


@dp.callback_query(lambda cb: cb.data == 'loc_enter_text')
async def cb_loc_enter_text(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await mark_online(uid)
    await state.set_state(Signup.location)
    await cb.message.edit_text(
        "\U0001f4cd <b>Type a city or area name</b> (e.g. HSR Layout, Bangalore):",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📍 Share My Location", callback_data="loc_share_gps")],
        ])
    )
    await cb.answer()


@dp.callback_query(lambda cb: cb.data == 'signup_skip_bio')
async def cb_skip_bio(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    logger.info(f"cb_skip_bio: uid={uid}, state={await state.get_state()}")
    await mark_online(uid)
    d = await state.get_data()
    if d.get('prev_bot_msg'):
        await safe_delete(cb.message.chat.id, d['prev_bot_msg'])
    await state.update_data(bio='')
    await finish_signup(state, cb.message.chat.id, uid)
    logger.info(f"cb_skip_bio done: uid={uid}, profiles={len(user_profiles)}")
    await cb.answer()


# === Edit profile inline button callbacks ===

# cb_edit_gender removed


@dp.callback_query(lambda cb: cb.data.startswith('edit_pref:'))
async def cb_edit_pref(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await mark_online(uid)
    if await _guard_edit(state):
        await cb.answer("⚠️ Finish signup first!", show_alert=True)
        return
    preferred = cb.data.split(':', 1)[1]
    user_profiles[uid]['preferred_gender'] = preferred
    await save_all()
    await cb.message.edit_text(
        "\u2705 <b>Interested in updated!</b>",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="\U0001f464 View Profile", callback_data='back_to_profile')],
        ])
    )
    await cb.answer()


@dp.callback_query(lambda cb: cb.data == 'loc_share_gps_edit')
async def cb_loc_share_gps_edit(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await mark_online(uid)
    if await _guard_edit(state):
        await cb.answer("\u26a0\ufe0f Finish signup first!", show_alert=True)
        return
    await state.set_state(EditProfile.location)
    await state.update_data(is_editing=True)
    await cb.message.delete()
    sent = await cb.message.answer(
        "\U0001f4cd <b>Update your location</b>\n\n"
        "Tap the button below to share your GPS location.\n"
        "If GPS is off, please turn it on in Settings, then try again.\n\n"
        "Or use \U0001f4ce \u2192 Location to pick from the map (works without GPS).\n"
        "Or type a city name.",
        parse_mode='HTML',
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="\U0001f4cd  Share My Location", request_location=True)]],
            resize_keyboard=True, one_time_keyboard=True
        )
    )
    await state.update_data(gps_msg_id=sent.message_id)
    await cb.answer()


@dp.callback_query(lambda cb: cb.data == 'loc_enter_text_edit')
async def cb_loc_enter_text_edit(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await mark_online(uid)
    if await _guard_edit(state):
        await cb.answer("⚠️ Finish signup first!", show_alert=True)
        return
    await state.set_state(EditProfile.location)
    await cb.message.edit_text(
        "\U0001f4cd <b>Type a city or area name</b> (e.g. HSR Layout, Bangalore):",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📍 Share My Location", callback_data="loc_share_gps_edit")],
            [InlineKeyboardButton(text="\u00ab  Back", callback_data='edit_profile')],
        ])
    )
    await cb.answer()
# EditProfile handlers
@dp.message(StateFilter(EditProfile.name))
async def edit_name_h(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    await mark_online(uid)
    try:
        name = (message.text or '').strip()
        if len(name) < 2:
            await message.answer("⚠️ Name must be at least 2 characters.")
            return
        user_profiles[uid]['name'] = name
        await save_all()
        await state.clear()
        await message.answer("\u2705 Name updated to " + name + "!", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="\U0001f464 View Profile", callback_data='back_to_profile')],
        ]))
    except Exception as e:
        logger.error(f"edit_name_h failed for {uid}: {e}")
        await message.answer("⚠️ Something went wrong. Please try again.")

@dp.message(StateFilter(EditProfile.bio))
async def edit_bio_h(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    await mark_online(uid)
    user_profiles[uid]['bio'] = message.text.strip()[:300]
    await save_all()
    await state.clear()
    await message.answer("\u2705 Bio updated!", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\U0001f464 View Profile", callback_data='back_to_profile')],
    ]))

# edit_gender_h removed


@dp.message(StateFilter(EditProfile.preferred))
async def edit_preferred_h(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    await mark_online(uid)
    raw = message.text.strip()
    nfd = unicodedata.normalize('NFD', raw.lower())
    keyword = ' '.join(re.findall(r'[a-z]+', ''.join(c for c in nfd if unicodedata.category(c) != 'Mn' and ord(c) != 0x200d)))
    PREF_KW = {'male': 'Male', 'female': 'Female', 'everyone': 'Everyone',
                'men': 'Male', 'women': 'Female'}
    if keyword not in PREF_KW:
        await message.answer("\u26a0\ufe0f Please tap a button above.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👨 Male", callback_data="edit_pref:Male")],
            [InlineKeyboardButton(text="👩 Female", callback_data="edit_pref:Female")],
            [InlineKeyboardButton(text="👥 Everyone", callback_data="edit_pref:Everyone")],
        ]))
        return
    user_profiles[uid]['preferred_gender'] = PREF_KW[keyword]
    await save_all()
    await state.clear()
    await message.answer("\u2705 Interested in updated!", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\U0001f464 View Profile", callback_data='back_to_profile')],
    ]))



@dp.message(StateFilter(EditProfile.location))
async def edit_location_h(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    await mark_online(uid)

    if message.location:
        loc = message.location
    else:
        text = (message.text or '').strip()
        if not text:
            return
        # Keyboard button placeholder — silently ignore, user should type a real place
        if text in ('\U0001f4cd Share My Location', '\u2328\ufe0f  Enter Place Name', '\U0001f4cd Share Location', 'Enter Place Name'):
            return
        lat, lon = await geocode(text)
        if not lat:
            await message.answer("📍 Couldn't find that place. Try a city name or use <b>Share My Location</b>.", parse_mode='HTML')
            return
        loc = type('L', (), {'latitude': lat, 'longitude': lon})()

    lat, lon = loc.latitude, loc.longitude
    user_profiles[uid]['lat'] = str(lat)
    user_profiles[uid]['lon'] = str(lon)
    user_profiles[uid]['location_name'] = 'GPS' if message.location else text
    await save_all()
    await state.clear()
    await message.answer("✅ Location updated!", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 View Profile", callback_data='back_to_profile')],
    ]))




# h_edit_dob removed


# ─── Background Tasks ────────────────────────────────────────────────────────

QUEUE_TIMEOUT = 60  # 1 minute per search attempt (up to 3 retries)

async def update_counters_loop():
    while True:
        await asyncio.sleep(8)
        try:
            online = await get_online_count()
            ql = len(waiting_queue)
            for uid, mid in list(_queue_msg_ids.items()):
                if uid in waiting_queue and uid in user_profiles:
                    retries = waiting_queue[uid].get('retries', 0)
                    attempt = min(retries + 1, 3)
                    status_text = (
                        f"\U0001f465 {online} online | \u23f3 {ql} in queue\n\n"
                        f"\U0001f464 {user_profiles[uid].get('name', '?')}, searching...\n"
                        f"\u23f3 Attempt {attempt}/3"
                    )
                    try:
                        await bot.edit_message_text(status_text, uid, mid, parse_mode='Markdown')
                    except:
                        pass
        except Exception as e:
            logger.warning(f"update_counters error: {e}")

async def check_queue_loop():
    while True:
        await asyncio.sleep(5)
        try:
            now = datetime.now()
            remove = []
            for uid in list(waiting_queue.keys()):
                if uid not in user_profiles:
                    remove.append(uid)
                    continue
                me = user_profiles[uid]
                if not me.get('lat'):
                    remove.append(uid)
                    continue
                # Try to find a match first
                m = find_queue_match(me, uid)
                if m:
                    pid = m['uid']
                    active_matches.setdefault(uid, {})
                    active_matches.setdefault(pid, {})
                    active_matches[uid][pid] = {'status': 'matched'}
                    active_matches[pid][uid] = {'status': 'matched'}
                    for x in (uid, pid):
                        if x in waiting_queue:
                            del waiting_queue[x]
                        if x in _queue_msg_ids:
                            await safe_delete(x, _queue_msg_ids.pop(x))
                    await save_all()
                    await send_match_card(uid, m, pid)
                    await send_match_card(pid, {**user_profiles[uid], 'uid': uid}, uid)
                    continue
                # Timeout check
                added_str = waiting_queue[uid].get('added_at', '')
                if added_str:
                    try:
                        added = datetime.fromisoformat(added_str)
                        if (now - added).total_seconds() > QUEUE_TIMEOUT:
                            retries = waiting_queue[uid].get('retries', 0)
                            if retries < 3:
                                # Retry — reset timer, increment counter, notify
                                waiting_queue[uid]['retries'] = retries + 1
                                waiting_queue[uid]['added_at'] = now.isoformat()
                                if uid in _queue_msg_ids:
                                    try:
                                        await bot.edit_message_text(
                                            f"\U0001f937\u200d\u2642\ufe0f Nobody found yet. "
                                            f"Retrying automatically ({retries + 1}/3)...",
                                            uid, _queue_msg_ids[uid], parse_mode='Markdown'
                                        )
                                    except:
                                        pass
                            else:
                                # 3 attempts exhausted — give up
                                if uid in _queue_msg_ids:
                                    try:
                                        await bot.edit_message_text(
                                            "\U0001f937 <b>Nobody online nearby right now.</b>\n\n"
                                            "Please try again later!",
                                            uid, _queue_msg_ids[uid], parse_mode='HTML',
                                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                                [InlineKeyboardButton(text="\u2764\ufe0f  Try Again", callback_data='do_match')],
                                            ])
                                        )
                                    except:
                                        pass
                                    del _queue_msg_ids[uid]
                                remove.append(uid)
                    except:
                        pass
            for uid in remove:
                if uid in waiting_queue:
                    del waiting_queue[uid]
            if remove or waiting_queue:
                await save_all()
        except Exception as e:
            logger.warning(f"check_queue error: {e}")

# ─── Razorpay Webhook ───────────────────────────────────────────────────────

async def handle_razorpay_webhook(request):
    try:
        logger.info(f"Razorpay webhook hit: {request.headers.get('X-Razorpay-Signature', 'NO_SIG')[:20]}...")
        if not RAZORPAY_WEBHOOK_SECRET:
            return web.Response(status=400, text="Webhook secret not configured")
        sig = request.headers.get('X-Razorpay-Signature')
        if not sig:
            return web.Response(status=400, text="Missing signature")
        body = await request.text()
        logger.info(f"Webhook body preview: {body[:200]}")
        expected = hmac.new(RAZORPAY_WEBHOOK_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
        if sig != expected:
            logger.warning(f"Webhook invalid sig: {sig[:20]}... expected {expected[:20]}...")
            return web.Response(status=400, text="Invalid signature")
        data = json.loads(body)
        event = data.get('event', '')
        notes = {}
        payment_id = None
        if event == 'payment_link.paid':
            entity = data.get('payload', {}).get('payment_link', {}).get('entity', {})
            notes = entity.get('notes', {})
            payment_id = entity.get('id')
        elif event == 'payment.captured':
            entity = data.get('payload', {}).get('payment', {}).get('entity', {})
            notes = entity.get('notes', {})
            payment_id = entity.get('id')
        if not notes:
            entity = data.get('payload', {}).get('order', {}).get('entity', {})
            notes = entity.get('notes', {})
        uid_s = notes.get('uid')
        dur_s = notes.get('duration_days')
        if uid_s and dur_s and payment_id:
            if payment_id in _processed_payments:
                return web.Response(status=200, text="Already processed")
            uid = int(uid_s)
            dur = int(dur_s)
            exp = datetime.now() + timedelta(days=dur)
            premium_subscriptions[uid] = {'expiry_date': exp.isoformat()}
            if uid in user_profiles:
                user_profiles[uid]['received_texts'] = 0
            _processed_payments.add(payment_id)
            r = await get_redis()
            if r:
                try:
                    await r.set('winkly:premium', json.dumps(premium_subscriptions))
                    await r.set('winkly:processed', json.dumps(list(_processed_payments)))
                except Exception as e:
                    logger.warning(f"Webhook Redis save failed: {e}")
            logger.info(f"Premium activated for {uid}, plan={dur}d, until {exp}")
            try:
                msg = f"\U0001f389 <b>Premium Activated!</b>\n\nYour Winkly premium is now active for {dur} day{'s' if dur > 1 else ''}.\n\n\u2705 Unlimited texts and matches!"
                if uid in current_chat:
                    msg += "\n\n\U0001f4ac Continue chatting with your match!"
                    await bot.send_message(uid, msg, parse_mode='HTML')
                else:
                    await bot.send_message(
                        uid, msg,
                        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="\u2764\ufe0f  Find Matches", callback_data='do_match')],
                        ])
                    )
            except:
                pass
        else:
            logger.info(f"Webhook {event} missing uid/duration: {notes}")
        return web.Response(status=200, text="OK")
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return web.Response(status=500, text=str(e))

async def payment_success_page(request):
    logger.info(f"Payment success page hit: {dict(request.query)}")
    plink_id = request.query.get('razorpay_payment_link_id')
    payment_id = request.query.get('razorpay_payment_id')
    uid = dur = None
    # Use Razorpay payments API to verify status (authoritative)
    if payment_id and razorpay_client:
        try:
            p = razorpay_client.payments.fetch(payment_id)
            if p.get('status') == 'captured':
                notes = p.get('notes', {})
                uid_s = notes.get('uid')
                dur_s = notes.get('duration_days')
                if uid_s and dur_s:
                    uid = int(uid_s); dur = int(dur_s)
        except Exception as e:
            logger.warning(f"Payment fetch error: {e}")
    # Fallback: check our local map
    if not uid and plink_id and plink_id not in _processed_payments:
        info = _payment_link_map.get(plink_id)
        if info:
            uid = info['uid']; dur = info['duration_days']
    if uid and dur and plink_id not in _processed_payments:
        # Fast path: look up in our local map (no API call needed)
        info = _payment_link_map.get(plink_id)
        if info:
            uid = info['uid']
            dur = info['duration_days']
        elif payment_id and razorpay_client:
            # Slow path: fetch payment link details from Razorpay API
            try:
                p = razorpay_client.payment_link.fetch(plink_id)
                notes = p.get('notes', {})
                uid_s = notes.get('uid')
                dur_s = notes.get('duration_days')
                if uid_s and dur_s:
                    uid = int(uid_s)
                    dur = int(dur_s)
            except Exception as e:
                logger.warning(f"Payment link fetch failed: {e}")
        if uid and dur:
            exp = datetime.now() + timedelta(days=dur)
            premium_subscriptions[uid] = {'expiry_date': exp.isoformat()}
            if uid in user_profiles:
                user_profiles[uid]['received_texts'] = 0
            _processed_payments.add(plink_id)
            await save_all()
            logger.info(f"Premium activated via success page for {uid}, plan={dur}d")
            try:
                msg = f"\U0001f389 <b>Premium Activated!</b>\n\nYour Winkly premium is now active for {dur} day{'s' if dur > 1 else ''}.\n\n\u2705 Unlimited texts and matches!"
                if uid in current_chat:
                    msg += "\n\n\U0001f4ac Continue chatting with your match!"
                    await bot.send_message(uid, msg, parse_mode='HTML')
                else:
                    await bot.send_message(uid, msg,
                        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="\u2764\ufe0f  Find Matches", callback_data='do_match')],
                        ]))
            except:
                pass
    return web.Response(
        content_type='text/html',
        text=f'<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Payment Successful - Winkly</title><style>body{{font-family:sans-serif;background:#1a1a2e;color:#eee;text-align:center;padding:40px 20px}}.card{{background:#16213e;border-radius:16px;padding:32px;max-width:400px;margin:0 auto}}.check{{font-size:64px;margin-bottom:16px}}.btn{{background:#e94560;color:#fff;border:none;border-radius:8px;padding:14px 28px;font-size:16px;cursor:pointer;text-decoration:none;display:inline-block;margin-top:16px}}</style></head><body><div class="card"><div class="check">&#9989;</div><h1>Payment Successful!</h1><p>Your Winkly premium subscription is now active.</p><p>Return to Telegram to start matching.</p>        <a class="btn" href="https://t.me/{BOT_USERNAME}">Open Telegram</a></div></body></html>'
    )

async def auto_setup_webhook():
    if not razorpay_client:
        logger.info("Razorpay unavailable - skipping webhook setup")
        return
    try:
        existing = await asyncio.to_thread(lambda: razorpay_client.webhook.all())
        target = f"{WEBHOOK_URL}/razorpay/webhook"
        for w in existing.get('items', []):
            if w.get('url') == target:
                logger.info(f"Webhook already exists: {w.get('id')}")
                return
        result = await asyncio.to_thread(
            lambda: razorpay_client.webhook.create(
                {"url": target, "events": ["payment_link.paid", "payment.captured"],
                 "secret": RAZORPAY_WEBHOOK_SECRET, "active": True}
            )
        )
        logger.info(f"Webhook auto-created: {result.get('id')}")
    except Exception as e:
        logger.warning(f"Webhook setup failed: {e}")
        logger.info(f"Create manually: Razorpay Dashboard > Settings > Webhooks")
        logger.info(f"URL: {WEBHOOK_URL}/razorpay/webhook")


SELFIE_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#000;display:flex;flex-direction:column;align-items:center;justify-content:center;height:100dvh;color:#fff;font-family:-apple-system,sans-serif;padding:16px}
#file-input{display:none}
.video-wrap{display:none;flex-direction:column;align-items:center;width:100%;max-width:400px}
video{width:100%;border-radius:12px;transform:scaleX(-1)}
#capture{margin-top:16px;padding:14px 40px;font-size:18px;border:none;border-radius:40px;background:#2ea043;color:#fff;cursor:pointer;font-weight:600}
#capture:disabled{opacity:.5}
.open-camera-btn{margin-top:16px;padding:12px 24px;font-size:15px;border:1px solid #555;border-radius:40px;background:transparent;color:#aaa;cursor:pointer}
#status{margin-top:14px;font-size:14px;text-align:center;color:#888}
.main-wrap{display:flex;flex-direction:column;align-items:center;gap:16px;padding:24px}
.main-title{font-size:22px;font-weight:700}
.main-sub{font-size:14px;color:#888;text-align:center}
#selfie-btn{padding:18px 48px;font-size:20px;border:none;border-radius:50px;background:#2ea043;color:#fff;cursor:pointer;font-weight:600;width:100%;max-width:320px}
#selfie-btn:disabled{opacity:.5}
#cam-btn{padding:14px 32px;font-size:16px;border:1px solid #2ea043;border-radius:50px;background:transparent;color:#2ea043;cursor:pointer}
</style></head><body>
<input type="file" id="file-input" accept="image/*" capture="user">
<div class="main-wrap" id="main-wrap">
  <div class="main-title">📸 Selfie Verification</div>
  <div class="main-sub">Your face must be clearly visible.<br>Good lighting, front camera recommended.</div>
  <button id="selfie-btn">📸 Take Selfie</button>
  <button id="cam-btn">📹 Use Camera</button>
  <div id="status"></div>
</div>
<div class="video-wrap" id="video-wrap">
  <video id="video" autoplay playsinline muted></video>
  <canvas id="canvas" style="display:none"></canvas>
  <button id="capture">📸 Capture</button>
  <button id="stop-btn" class="open-camera-btn">← Back</button>
  <div id="status2"></div>
</div>
<script>
Telegram.WebApp.ready();Telegram.WebApp.expand();
const fi=document.getElementById('file-input'),uid=new URLSearchParams(window.location.search).get('uid');
const main=document.getElementById('main-wrap'),vw=document.getElementById('video-wrap');
const v=document.getElementById('video'),c=document.getElementById('canvas');
const cap=document.getElementById('capture'),st=document.getElementById('status'),st2=document.getElementById('status2');
const sbtn=document.getElementById('selfie-btn'),cbtn=document.getElementById('cam-btn'),stop=document.getElementById('stop-btn');
let stream=null;

function uploadBlob(b,txt){
  st.textContent=txt||'Uploading...';cap.disabled=1;
  const fd=new FormData();fd.append('photo',b,'selfie.jpg');fd.append('uid',uid);
  fetch('/api/upload_selfie',{method:'POST',body:fd}).then(r=>r.text()).then(res=>{
    if(res==='OK'){st.textContent='\u2705 Selfie uploaded! Check Telegram';setTimeout(()=>Telegram.WebApp.close(),1500)}
    else if(res==='NO_FACE'){st.textContent='\u274c No face detected. Try again';cap.disabled=0}
    else{st.textContent='\u274c Error: '+res;cap.disabled=0}
  }).catch(e=>{st.textContent='Upload error: '+e.message;cap.disabled=0});
}

// File input — primary path (opens camera app directly on mobile)
sbtn.onclick=()=>fi.click();
fi.onchange=()=>{const f=fi.files[0];if(f)uploadBlob(f,'Uploading...')};

// Camera path — getUserMedia (reliable on iOS, some Android, desktop)
cbtn.onclick=async()=>{
  main.style.display='none';vw.style.display='flex';st2.textContent='Opening camera...';
  try{
    stream=await navigator.mediaDevices.getUserMedia({audio:false,video:{facingMode:{ideal:'user'},width:{ideal:480},height:{ideal:640}}});
    v.srcObject=stream;st2.textContent='Look at the camera, then tap Capture';
    cap.style.display='';stop.style.display='';
  }catch(e){vw.style.display='none';main.style.display='flex';st.textContent='Camera not available. Use the button above.'}
};
cap.onclick=()=>{if(!stream)return;c.width=v.videoWidth;c.height=v.videoHeight;c.getContext('2d').drawImage(v,0,0);c.toBlob(b=>uploadBlob(b,'Uploading...'),'image/jpeg',0.85)};
stop.onclick=()=>{if(stream){stream.getTracks().forEach(t=>t.stop());stream=null}vw.style.display='none';main.style.display='flex';st.textContent=''};
</script></body></html>"""


async def handle_selfie_page(request):
    uid = request.query.get('uid', '')
    page = SELFIE_PAGE.replace('{uid}', uid)
    return web.Response(text=page, content_type='text/html')


async def handle_upload_selfie(request):
    try:
        data = await request.post()
        uid_str = data.get('uid', '')
        photo_field = data.get('photo')
        if not uid_str or not photo_field:
            return web.Response(text='MISSING_FIELDS', status=400)
        uid = int(uid_str)
        raw = photo_field.file.read()
        tmp = f"/tmp/selfie_upload_{uid}.jpg"
        with open(tmp, 'wb') as f:
            f.write(raw)
        ok = _faces_found(tmp)
        if ok:
            # Upload to Telegram to get a proper file_id we can use in send_photo
            try:
                file_id = await bot.upload_file(tmp, destination=bot.file)
                # Upload_file doesn't return file_id directly — use send_document approach
                # Instead, save locally and send as file path
                import shutil
                saved = f"/tmp/verified_selfie_{uid}.jpg"
                shutil.copy(tmp, saved)
                user_profiles[uid]['selfie'] = saved
                user_profiles[uid]['verified'] = True
                user_profiles[uid]['verification_status'] = 'verified'
                await save_all()
                g = user_profiles[uid].get('gender', '')
                if g == 'Female':
                    await bot.send_message(uid,
                        "\u2705 <b>Verified!</b>\n\nSelfie verified. You now have unlimited free access to chat.\n\nGo find your match! \u2764\ufe0f",
                        parse_mode='HTML', reply_markup=main_kb(uid))
                else:
                    await bot.send_message(uid,
                        "\u2705 <b>Verified!</b>\n\nSelfie verified. Your profile now shows a verified badge \u2714\ufe0f.",
                        parse_mode='HTML', reply_markup=main_kb(uid))
            except Exception as e:
                logger.error(f"Selfie upload to Telegram failed: {e}")
                user_profiles[uid]['verified'] = True
                user_profiles[uid]['verification_status'] = 'verified'
                await save_all()
        else:
            try:
                os.remove(tmp)
            except:
                pass
            await bot.send_message(uid,
                "\U0001f914 <b>No face detected.</b>\n\nPlease try again — make sure your face is clearly visible and front camera is used.",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📸 Try Again", web_app=WebAppInfo(url=f"{WEBHOOK_URL}/selfie?uid={uid}"))],
                ]))
        return web.Response(text='OK' if ok else 'NO_FACE')
    except Exception as e:
        logger.error(f"Upload selfie error: {e}")
        return web.Response(text='SERVER_ERROR', status=500)


# ─── DEV: Direct premium activation for testing (remove in production) ───────
@dp.message(Command('premium_activate'))
async def cmd_activate_premium(message: types.Message):
    uid = message.from_user.id
    if uid != ADMIN_CHAT_ID and uid != 8624196108:  # allow self-testing
        return
    days = 365
    exp = datetime.now() + timedelta(days=days)
    premium_subscriptions[uid] = {'expiry_date': exp.isoformat()}
    if uid in user_profiles:
        user_profiles[uid]['received_texts'] = 0
    await save_all()
    await message.answer(f"\U0001f389 <b>PREMIUM ACTIVATED!</b>\n\nValid for {days} days.\n\n\u2705 Unlimited texts and matches!",
        parse_mode='HTML', reply_markup=main_kb(uid))

# ─── Startup ───────────────────────────────────────────────────────────────

async def on_startup(dispatcher: Dispatcher):
    logger.info("Starting Winkly Bot v2...")
    # Menu button (left of emoji bar) showing bot commands
    commands = [
        BotCommand(command="start", description="Start or restart the bot"),
        BotCommand(command="profile", description="View your profile"),
        BotCommand(command="find", description="Find matches"),
        BotCommand(command="stop", description="End current chat"),
        # BotCommand(command="verify", description="Get Verified"),  # removed
        BotCommand(command="premium", description="View premium plans"),
        BotCommand(command="refer", description="Refer friends for free premium"),
    ]
    try:
        await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
        logger.info("Menu button added")
    except Exception as e:
        logger.error(f"Failed to set commands: {e}")

    await init_storage()
    logger.info(f"Loaded {len(user_profiles)} profiles, {len(active_matches)} matches")
    # Log FSM state of admin user to verify storage is working
    r_check = await get_redis()
    if r_check:
        test_key = f"fsm:{ADMIN_CHAT_ID}:{ADMIN_CHAT_ID}:data"
        test_val = await r_check.get(test_key)
        logger.info(f"FSM test after init: uid={ADMIN_CHAT_ID} key={test_key} val={test_val}")

    if WEBHOOK_URL:
        await bot.set_webhook(WEBHOOK_URL)
        logger.info(f"Webhook set to {WEBHOOK_URL}")
        await auto_setup_webhook()

        from aiogram.webhook.aiohttp_server import SimpleRequestHandler

        handler = SimpleRequestHandler(dispatcher=dispatcher, bot=bot)
        app = web.Application()

        async def health(request):
            return web.Response(text='OK', status=200)
        app.router.add_get('/health', health)
        app.router.add_post('/health', health)

        async def debug(request):
            r = await get_redis()
            if r:
                try:
                    await r.ping()
                    ping = 'PONG'
                except Exception as e:
                    ping = f'ERROR: {e}'
                redis_ok = True
            else:
                ping = 'N/A'
                redis_ok = False
            return web.json_response({
                'redis_connected': redis_ok,
                'redis_ping': ping,
                'profiles_count': len(user_profiles),
                'queue_count': len(waiting_queue),
                'premium_count': len(premium_subscriptions),
                'REDIS_URL_present': bool(REDIS_URL),
                'REDIS_URL_prefix': REDIS_URL[:20] + '...' if REDIS_URL else 'EMPTY',
            })
        app.router.add_get('/debug', debug)

        async def fsm_check(request):
            # Read all FSM keys from Redis to diagnose state
            r = await get_redis()
            # Check multiple possible key patterns used by aiogram RedisStorage
            patterns = ['fsm:*', '*:678498871', '*user*', '*678498871*', '*state*', '*data*']
            fsm_data = {}
            all_keys = []
            for pattern in patterns:
                ks = await r.keys(pattern) if r else []
                all_keys.extend(ks)
            all_keys = list(dict.fromkeys(all_keys))  # dedupe
            for key in all_keys:
                val = await r.get(key)
                ttl = await r.ttl(key) if val else -1
                fsm_data[key] = {'val': val, 'ttl': ttl}
            # Also check storage type
            storage_type = type(dp.storage).__name__
            return web.json_response({
                'fsm_keys': all_keys, 'fsm_data': fsm_data,
                'profile_in_memory': dict(user_profiles),
                'storage_type': storage_type,
                'redis_url_set': bool(REDIS_URL),
            })
        app.router.add_get('/fsm-check', fsm_check)

        async def fsm_read(request):
            # Read FSM data for a specific user using aiogram's FSM context
            uid = int(request.query.get('uid', ADMIN))
            from aiogram.fsm.context import FSMContext
            ctx = FSMContext(storage=dp.storage, user_id=uid, chat_id=uid)
            data = await ctx.get_data()
            state = await ctx.get_state()
            return web.json_response({'uid': uid, 'state': state, 'data': data})
        app.router.add_get('/fsm-read', fsm_read)
        async def test_route(request):
            return web.json_response({'test': 'ok', 'path': str(request.path)})
        app.router.add_get('/test-ok', test_route)

        async def test_fsm_backup_endpoint(request):
            '''Test direct Redis access'''
            uid = 678498871
            import time
            r = await get_redis()
            if not r:
                return web.json_response({'error': 'no redis'}, status=500)
            test_val = f'test_{int(time.time())}'
            # Write directly to Redis (simulating what fsm_backup_set does)
            await r.set(f'winkly:fsm:{uid}:test_key', test_val, ex=86400*7)
            result = await r.get(f'winkly:fsm:{uid}:test_key')
            return web.json_response({'set': test_val, 'got': result})

        app.router.add_get('/test-fsm-backup', test_fsm_backup_endpoint)

        async def test_save(request):
            # Read FSM data and save directly (not relying on user_profiles in this worker)
            uid = 678498871
            from aiogram.fsm.context import FSMContext
            ctx = FSMContext(storage=dp.storage, user_id=uid, chat_id=uid)
            data = await ctx.get_data()
            name_val = data.get('name') or await fsm_backup_get(uid, 'name') or ''
            prof = {
                'name': name_val,
                'gender': data.get('gender', ''),
                'preferred_gender': data.get('preferred', ''),
                'bio': data.get('bio', ''),
                'dob': data.get('dob', ''),
                'lat': data.get('lat', ''),
                'lon': data.get('lon', ''),
                'location_name': data.get('location_name', ''),
                'photo': data.get('photo'),
                'verified': True,
                'verification_status': 'verified',
                'username': data.get('username', ''),
                'free_texts': FREE_TEXTS_JOINING,
                'rejected': [],
                'received_texts': 0,
            }
            user_profiles[uid] = prof
            r2 = await get_redis()
            if r2:
                try:
                    raw = await r2.get('winkly:profiles') or '{}'
                    all_profiles = json.loads(raw)
                    all_profiles[str(uid)] = prof
                    await r2.set('winkly:profiles', json.dumps(all_profiles))
                    return web.json_response({'in_memory': len(user_profiles), 'saved': True, 'prof_name': prof.get('name')})
                except Exception as e:
                    return web.json_response({'error': str(e)}, status=500)
            return web.json_response({'in_memory': len(user_profiles), 'saved': False})
        async def force_finish(request):
            # Direct: read FSM for uid, save to user_profiles and Redis
            uid = 678498871
            from aiogram.fsm.context import FSMContext
            ctx = FSMContext(storage=dp.storage, user_id=uid, chat_id=uid)
            data = await ctx.get_data()
            name_val = data.get('name') or await fsm_backup_get(uid, 'name') or ''
            prof = {
                'name': name_val,
                'gender': data.get('gender', ''),
                'preferred_gender': data.get('preferred', ''),
                'bio': data.get('bio', ''),
                'dob': data.get('dob', ''),
                'lat': data.get('lat', ''),
                'lon': data.get('lon', ''),
                'location_name': data.get('location_name', ''),
                'photo': data.get('photo'),
                'verified': True,
                'verification_status': 'verified',
                'username': data.get('username', ''),
                'free_texts': FREE_TEXTS_JOINING,
                'rejected': [],
                'received_texts': 0,
            }
            user_profiles[uid] = prof
            await save_all()
            r2 = await get_redis()
            profiles_val = await r2.get('winkly:profiles') if r2 else None
            return web.json_response({'data': data, 'prof': prof, 'saved': profiles_val})
        app.router.add_get('/force-finish', force_finish)
        app.router.add_post('/force-finish', force_finish)

        app.router.add_post('/razorpay/webhook', handle_razorpay_webhook)
        app.router.add_get('/payment/success', payment_success_page)
        app.router.add_get('/selfie', handle_selfie_page)
        app.router.add_post('/api/upload_selfie', handle_upload_selfie)
        handler.register(app, path='/')

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host='0.0.0.0', port=PORT)
        await site.start()
        logger.info(f"Server running on port {PORT}")

        asyncio.create_task(update_counters_loop())
        asyncio.create_task(check_queue_loop())
        await asyncio.Event().wait()
    else:
        logger.info("No WEBHOOK_URL - long-polling mode")
        await bot.delete_webhook(drop_pending_updates=True)
        asyncio.create_task(update_counters_loop())
        asyncio.create_task(check_queue_loop())
        await dp.start_polling(bot, skip_updates=False)

if __name__ == '__main__':
    asyncio.run(on_startup(dp))
