"""Winkly Dating Bot v2 - Complete Implementation"""
import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
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
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, BotCommand, BotCommandScopeDefault, BotCommandScopeChat
from aiogram.filters.state import State, StatesGroup
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



load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '.env'))

def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value

BOT_TOKEN = required_env('BOT_TOKEN')
REDIS_URL = os.getenv('REDIS_URL', '')
DEFAULT_WEBHOOK_URL = 'https://winkly-kmsz.onrender.com'
WEBHOOK_URL = os.getenv('WEBHOOK_URL', DEFAULT_WEBHOOK_URL).rstrip('/')
PORT = int(os.getenv('PORT', '8080'))
ADMIN_CHAT_ID = int(os.getenv('ADMIN_CHAT_ID', '0'))
ALLOW_LONG_POLLING = os.getenv('ALLOW_LONG_POLLING', '').strip().lower() in {'1', 'true', 'yes'}
TELEGRAM_WEBHOOK_SECRET = os.getenv('TELEGRAM_WEBHOOK_SECRET', '')
TELEGRAM_WEBHOOK_PATH = os.getenv('TELEGRAM_WEBHOOK_PATH', '/telegram/webhook')
if not TELEGRAM_WEBHOOK_PATH.startswith('/'):
    TELEGRAM_WEBHOOK_PATH = '/' + TELEGRAM_WEBHOOK_PATH
if WEBHOOK_URL and not TELEGRAM_WEBHOOK_SECRET:
    TELEGRAM_WEBHOOK_SECRET = hmac.new(
        BOT_TOKEN.encode(),
        b'winkly-telegram-webhook-secret',
        hashlib.sha256,
    ).hexdigest()
RAZORPAY_KEY_ID = os.getenv('RAZORPAY_KEY_ID', '')
RAZORPAY_KEY_SECRET = os.getenv('RAZORPAY_KEY_SECRET', '')
RAZORPAY_WEBHOOK_SECRET = os.getenv('RAZORPAY_WEBHOOK_SECRET', '')
BOT_USERNAME = os.getenv('BOT_USERNAME', 'Winkly_dating_bot')

# Bot command definitions for menu button
ALL_COMMANDS = [
    BotCommand(command="start", description="Start or restart the bot"),
    BotCommand(command="profile", description="View your profile"),
    BotCommand(command="find", description="Find matches"),
    BotCommand(command="stop", description="End current chat"),
    BotCommand(command="premium", description="View premium plans"),
    BotCommand(command="refer", description="Refer friends"),
    BotCommand(command="report", description="Report a user"),
    BotCommand(command="feedback", description="Send feedback"),
]
NO_PREMIUM_COMMANDS = [c for c in ALL_COMMANDS if c.command != 'premium']

async def hide_premium_cmd(uid: int):
    """Override bot command scope to hide /premium for a specific user."""
    try:
        await bot.set_my_commands(
            NO_PREMIUM_COMMANDS,
            scope=BotCommandScopeChat(chat_id=uid)
        )
    except Exception as e:
        logger.warning(f"Failed to hide /premium for {uid}: {e}")

PLAN_CATALOG = {
    "trial_1d": {"name": "1 Day", "price": 49, "duration": 1},
    "weekly":   {"name": "1 Week", "price": 99, "duration": 7},
    "biweekly": {"name": "2 Weeks", "price": 149, "duration": 14},
    "monthly":  {"name": "1 Month", "price": 199, "duration": 30},
}
LONG_PLANS = [
    {"id": "trial_1d", **PLAN_CATALOG["trial_1d"]},
    {"id": "weekly",   **PLAN_CATALOG["weekly"]},
    {"id": "biweekly", **PLAN_CATALOG["biweekly"]},
    {"id": "monthly",  **PLAN_CATALOG["monthly"]},
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
    razorpay_client = (
        razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
        if RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET
        else None
    )
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
                    if "received_texts" not in prof:
                        prof["received_texts"] = 0
                    if "verified_female" not in prof:
                        prof["verified_female"] = False
                    if "verification_status" not in prof:
                        prof["verification_status"] = "not_required"
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
                    'verified': data.get('verified') if data.get('verified') is not None else existing.get('verified', False),
                    'verified_female': data.get('verified_female') if data.get('verified_female') is not None else existing.get('verified_female', False),
                    'verification_status': data.get('verification_status', 'not_required') or existing.get('verification_status', 'not_required'),
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
_payment_link_map: Dict[str, dict] = {}  # payment_link_id -> server-side pending payment metadata
_quota_notif: Dict[int, dict] = {}  # uid -> {'mid': int, 'count': int}
_partner_notified_on_hold: set[int] = set()  # uid → partner already notified of on-hold
_reconnect_tasks: Dict[int, asyncio.Task] = {}  # hold_user_uid -> reconnect loop task
_quota_locks: Dict[int, asyncio.Lock] = {}
# In-memory cache for has_text_quota — avoids Redis on every message
# Key = uid, Value = (remaining_int, expiry_time)
_QUOTA_CACHE_TTL = 3.0  # seconds
_quota_cache: Dict[int, tuple[int, float]] = {}
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

REPORT_THRESHOLD = 3

async def is_banned_user(uid: int) -> bool:
    try:
        r = await get_redis()
        return await r.sismember('banned_users', str(uid))
    except Exception:
        return False

async def add_report(target_uid: int, reporter_uid: int, reason: str):
    try:
        import json
        r = await get_redis()
        key = f'reports:{target_uid}'
        from datetime import datetime
        entry = json.dumps({
            'reporter': reporter_uid,
            'reason': reason,
            'time': datetime.now().isoformat()
        })
        await r.rpush(key, entry)
        count = await r.llen(key)
        if count >= REPORT_THRESHOLD:
            await r.sadd('banned_users', str(target_uid))
        return count
    except Exception as e:
        logger.error(f'add_report error: {e}')
        return 0

async def send_admin(text: str, parse_mode='HTML'):
    if ADMIN_CHAT_ID:
        try:
            await bot.send_message(chat_id=ADMIN_CHAT_ID, text=text, parse_mode=parse_mode)
        except Exception as e:
            logger.warning(f'send_admin failed: {e}')

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


class Report(StatesGroup):
    reason = State()
    confirm = State()

class Feedback(StatesGroup):
    message = State()
    confirm = State()

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


async def reverse_geocode(lat: float, lon: float) -> str:
    """
    Convert GPS coordinates to a human-readable place name using Nominatim (free).
    Returns the city/town name, or a coordinate fallback string on failure.
    """
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                'https://nominatim.openstreetmap.org/reverse',
                params={'lat': str(lat), 'lon': str(lon), 'format': 'json', 'zoom': '14'},
                headers={'User-Agent': 'WinklyBot/1.0'},
                timeout=aiohttp.ClientTimeout(total=8)
            ) as r:
                if r.status == 200:
                    d = await r.json()
                    addr = d.get('address', {})
                    for key in ('city', 'town', 'village', 'suburb', 'county'):
                        if addr.get(key):
                            return addr[key]
                    # Return display_name (full address) if no city-level match
                    dn = d.get('display_name', '')
                    if dn:
                        # Shorten: take just city/district from the full address
                        parts = dn.split(', ')
                        return parts[1] if len(parts) > 1 else dn[:60]
    except Exception as e:
        logger.warning(f"Reverse geocode failed: {e}")
    # Fallback: never return empty — always give coordinates string
    return f"{lat:.4f}, {lon:.4f}"

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
    """Only explicit server-side female verification gets unlimited free access."""
    p = user_profiles.get(uid)
    return bool(p and p.get('gender') == 'Female' and p.get('verified_female') is True)

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

def _quota_lock(uid: int) -> asyncio.Lock:
    lock = _quota_locks.get(uid)
    if lock is None:
        lock = asyncio.Lock()
        _quota_locks[uid] = lock
    return lock

async def has_text_quota(uid: int) -> bool:
    """Check quota using cache (3s TTL) then Redis as fallback."""
    p = user_profiles.get(uid)
    if not p:
        return False
    if is_premium(uid) or is_verified_female(uid):
        return True
    now = time.time()
    # Check in-memory cache first
    cached = _quota_cache.get(uid)
    if cached:
        remaining, exp = cached
        if now < exp:
            return remaining > 0
    r = await get_redis()
    key = f'winkly:quota:{uid}:free_texts'
    if r:
        await r.setnx(key, int(p.get('free_texts', 0)))
        try:
            remaining = int(await r.get(key) or 0)
        except (TypeError, ValueError):
            remaining = 0
        p['free_texts'] = remaining
        _quota_cache[uid] = (remaining, now + _QUOTA_CACHE_TTL)
        return remaining > 0
    return p.get('free_texts', 0) > 0

async def reserve_text_quota(uid: int) -> bool:
    """Atomically reserve one outgoing text before delivery."""
    p = user_profiles.get(uid)
    if not p:
        return False
    if is_premium(uid) or is_verified_female(uid):
        return True
    async with _quota_lock(uid):
        r = await get_redis()
        if r:
            key = f'winkly:quota:{uid}:free_texts'
            await r.setnx(key, int(p.get('free_texts', 0)))
            script = """
local current = tonumber(redis.call('get', KEYS[1]) or '0')
if current <= 0 then
  return -1
end
current = current - 1
redis.call('set', KEYS[1], current)
return current
"""
            remaining = int(await r.eval(script, 1, key))
            if remaining < 0:
                return False
            p['free_texts'] = remaining
            return True
        remaining = int(p.get('free_texts', 0) or 0)
        if remaining <= 0:
            return False
        p['free_texts'] = remaining - 1
        return True

async def refund_text_quota(uid: int):
    p = user_profiles.get(uid)
    if not p or is_premium(uid) or is_verified_female(uid):
        return
    async with _quota_lock(uid):
        r = await get_redis()
        if r:
            key = f'winkly:quota:{uid}:free_texts'
            remaining = int(await r.incr(key))
            p['free_texts'] = remaining
        else:
            p['free_texts'] = int(p.get('free_texts', 0) or 0) + 1
        await save_all()

def quota_summary(uid: int) -> str:
    if is_premium(uid):
        exp_str = premium_subscriptions[uid].get('expiry_date', '')
        try:
            exp = datetime.fromisoformat(exp_str); days = (exp - datetime.now()).days
            return f"🌟 Premium Active — Unlimited. Expires in {days} day{'s' if days != 1 else ''}"
        except: return "🌟 Premium Active — Unlimited!"
    if is_verified_female(uid):
        return "✅ Verified Female — Unlimited free access, always."
    p = user_profiles.get(uid, {})
    ft = p.get('free_texts', 0)
    if ft > 0:
        return f"✉️ {ft} free message{'s' if ft != 1 else ''} remaining — send more with Premium."
    return "✉️ All free messages sent — 🌟 Upgrade for unlimited texts."

def referral_code(uid: int) -> str:
    secret = BOT_TOKEN.encode()
    return hmac.new(secret, f"winkly_ref:{uid}".encode(), hashlib.sha256).hexdigest()[:10].upper()

async def referral_count(uid: int) -> int:
    try:
        r = await get_redis(); return await r.scard(f'winkly:referrals:{uid}')
    except: return 0

async def activate_premium(uid: int, days: int) -> datetime:
    exp = datetime.now() + timedelta(days=days)
    premium_subscriptions[uid] = {'expiry_date': exp.isoformat()}
    if uid in user_profiles:
        user_profiles[uid]['received_texts'] = 0
    await save_all()
    return exp

async def award_free_premium(uid: int):
    await activate_premium(uid, 1)
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
                f"⏳ <b>{pname}</b> is on hold.\n\n"
                f"They can still receive your messages. We'll notify you when they reconnect.",
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
                _partner_notified_on_hold.discard(a_uid)
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
    if not ref_code:
        return False
    ref_code = ref_code.strip().upper()
    for uid in list(user_profiles.keys()):
        if referral_code(uid) == ref_code:
            if uid == new_uid:
                logger.info(f"Rejected self-referral for uid={new_uid}")
                return False
            r = await get_redis()
            if not r:
                logger.warning("Referral ignored because Redis is unavailable")
                return False
            claimed = await r.set(f'winkly:referred_by:{new_uid}', str(uid), nx=True, ex=86400 * 365)
            if not claimed:
                logger.info(f"Referral already claimed for uid={new_uid}")
                return False
            await r.sadd(f'winkly:referrals:{uid}', str(new_uid))
            cnt = await referral_count(uid)
            logger.info(f"Referrer {uid} has {cnt} referrals")
            if cnt >= 3:
                awarded = await r.set(f'winkly:referral_awarded:{uid}:3', '1', nx=True, ex=86400 * 365)
                if awarded:
                    await award_free_premium(uid)
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
        if uid in current_chat: continue
        m = find_compat(me2, {uid: user_profiles[uid]})
        if m: return {**m[0], 'wait_info': waiting_queue[uid]}
    return None

def make_link_sync(uid: int, plan_id: str):
    plan = PLAN_CATALOG.get(plan_id)
    if not razorpay_client or not plan:
        return None
    try:
        payload = {
            "amount": int(plan["price"]) * 100,
            "currency": "INR",
            "description": f"Winkly Premium - {plan['name']}",
            "notes": {"uid": str(uid), "plan_id": plan_id},
            "options": {
                "checkout": {
                    "name": "Winkly",
                    "description": "Premium Dating",
                    "prefill": {
                        "contact": "",
                        "email": "",
                        "name": ""
                    }
                }
            },
        }
        if WEBHOOK_URL:
            payload["callback_url"] = f"{WEBHOOK_URL}/payment/success"
            payload["callback_method"] = "get"
        result = razorpay_client.payment_link.create(payload)
        pl_id = result.get("id")
        if pl_id:
            _payment_link_map[pl_id] = {
                "uid": uid,
                "plan_id": plan_id,
                "amount": int(plan["price"]) * 100,
                "currency": "INR",
                "status": "pending",
            }
        return {"url": result.get("short_url"), "payment_link_id": pl_id}
    except Exception as e:
        logger.error(f"Payment link error: {e}"); return None

async def make_payment_link(uid: int, plan_id: str):
    plan = PLAN_CATALOG.get(plan_id)
    if not plan:
        logger.warning(f"Invalid premium plan requested: {plan_id}")
        return None
    result = await asyncio.to_thread(make_link_sync, uid, plan_id)
    if not result or not result.get("url") or not result.get("payment_link_id"):
        return None
    link_id = result["payment_link_id"]
    amount = int(plan["price"]) * 100
    pending = {
        "uid": str(uid),
        "plan_id": plan_id,
        "amount": str(amount),
        "currency": "INR",
        "status": "pending",
    }
    # Always store in-memory (fast path for success page fallback)
    _payment_link_map[link_id] = pending
    # Also persist in Redis for webhook resilience; best-effort only
    r = await get_redis()
    if r:
        try:
            await r.hset(f'winkly:payment:{link_id}', mapping=pending)
            await r.expire(f'winkly:payment:{link_id}', 86400)
        except Exception:
            logger.warning(f"Failed to persist pending payment to Redis: {link_id}", exc_info=True)
    return result["url"]

def fetch_payment_link_sync(link_id: str):
    if not razorpay_client or not link_id:
        return None
    try:
        return razorpay_client.payment_link.fetch(link_id)
    except Exception as e:
        logger.warning(f"Failed to fetch Razorpay payment link {link_id}: {e}")
        return None

async def reconcile_paid_payment_link(link_id: str):
    if not link_id:
        return {"activated": False, "reason": "missing_link"}

    r = await get_redis()
    pending_key = f'winkly:payment:{link_id}'
    pending = None
    if r:
        try:
            pending = await r.hgetall(pending_key)
        except Exception:
            logger.warning(f"Failed to read pending payment from Redis: {link_id}", exc_info=True)
    if not pending:
        pending = _payment_link_map.get(link_id)
    if not pending:
        logger.warning(f"Payment reconciliation skipped for unknown link: {link_id}")
        return {"activated": False, "reason": "unknown_link"}
    if pending.get("status") == "paid" and pending.get("payment_id"):
        return {"activated": True, "reason": "already_paid"}

    plan_id = pending.get("plan_id")
    plan = PLAN_CATALOG.get(plan_id)
    if not plan:
        logger.warning(f"Payment reconciliation invalid plan for {link_id}: {plan_id}")
        return {"activated": False, "reason": "invalid_plan"}

    payment_link = await asyncio.to_thread(fetch_payment_link_sync, link_id)
    if not payment_link:
        return {"activated": False, "reason": "fetch_failed"}

    status = str(payment_link.get("status", "")).lower()
    amount_paid = int(payment_link.get("amount_paid") or 0)
    amount = int(payment_link.get("amount") or 0)
    expected_amount = int(pending.get("amount", "0"))
    currency = payment_link.get("currency")
    if status != "paid" or amount_paid < expected_amount or amount != expected_amount or currency != pending.get("currency"):
        logger.info(f"Payment reconciliation not paid/mismatched for {link_id}: status={status}")
        return {"activated": False, "reason": "not_paid"}

    notes = payment_link.get("notes") or {}
    if str(notes.get("uid", pending.get("uid"))) != pending.get("uid"):
        return {"activated": False, "reason": "uid_mismatch"}
    if str(notes.get("plan_id", plan_id)) != plan_id:
        return {"activated": False, "reason": "plan_mismatch"}

    payments = payment_link.get("payments") or []
    captured = [p for p in payments if str(p.get("status", "")).lower() == "captured"]
    payment_id = None
    if captured:
        payment_id = captured[-1].get("payment_id") or captured[-1].get("id")
    payment_id = payment_id or f"payment_link.paid:{link_id}"

    processed_keys = [
        f'winkly:processed_payment:{payment_id}',
        f'winkly:processed_payment:payment_link.paid:{link_id}',
    ]
    if r:
        claimed = await r.set(processed_keys[0], '1', nx=True, ex=86400 * 365)
        if not claimed:
            return {"activated": True, "reason": "already_processed"}
        await r.set(processed_keys[1], '1', ex=86400 * 365)
    elif payment_id in _processed_payments:
        return {"activated": True, "reason": "already_processed"}

    uid = int(pending["uid"])
    dur = int(plan["duration"])
    exp = await activate_premium(uid, dur)
    _processed_payments.add(payment_id)
    _processed_payments.add(f"payment_link.paid:{link_id}")
    if r:
        await r.hset(pending_key, mapping={
            "status": "paid",
            "payment_id": str(payment_id),
            "paid_at": datetime.now().isoformat(),
        })
        await r.expire(pending_key, 86400 * 30)
    logger.info(f"Premium reconciled for uid={uid}, plan_id={plan_id}, until={exp.isoformat()}")
    try:
        msg = f"\U0001f389 <b>Premium Activated!</b>\n\nYour Winkly premium is now active for {dur} day{'s' if dur > 1 else ''}.\n\n\u2705 Unlimited texts and matches!"
        await bot.send_message(uid, msg, parse_mode='HTML')
    except Exception as e:
        logger.warning(f"Premium reconciliation message failed for uid={uid}: {e}")
    return {"activated": True, "uid": uid, "duration": dur, "expiry": exp.isoformat()}

async def safe_delete(chat_id: int, message_id: int):
    if message_id:
        try: await bot.delete_message(chat_id, message_id)
        except: pass

def main_kb(uid: int = None):
    is_female = uid and user_profiles.get(uid, {}).get('gender') == 'Female'
    is_paid_premium = uid and is_premium(uid)
    rows = [
        [InlineKeyboardButton(text="✏️ Edit Profile", callback_data='edit_profile')],
        [InlineKeyboardButton(text="🔍 Find Matches", callback_data='do_match')],
    ]
    if not is_female and not is_paid_premium:
        rows.append([InlineKeyboardButton(text="🌟 Premium", callback_data='see_premium')])
    rows.append([InlineKeyboardButton(text="🚩 Report User", callback_data='report_user')])
    rows.append([InlineKeyboardButton(text="💬 Send Feedback", callback_data='send_feedback')])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def reengage_kb(uid: int = None):
    is_female = uid and user_profiles.get(uid, {}).get('gender') == 'Female'
    is_paid_premium = uid and is_premium(uid)
    rows = [
        [InlineKeyboardButton(text="🔍 Find New Match", callback_data='do_match'),
         InlineKeyboardButton(text="👤 My Profile", callback_data='back_to_profile')],
    ]
    if not is_female and not is_paid_premium:
        rows.append([InlineKeyboardButton(text="🌟 Premium", callback_data='see_premium')])
    rows.append([InlineKeyboardButton(text="🚩 Report User", callback_data='report_user')])
    rows.append([InlineKeyboardButton(text="💬 Send Feedback", callback_data='send_feedback')])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def edit_profile_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Change Name", callback_data='edit_name'),
         InlineKeyboardButton(text="📝 Edit Bio", callback_data='edit_bio')],
        [InlineKeyboardButton(text="👥 Change Preference", callback_data='edit_preferred')],
        [InlineKeyboardButton(text="📍 Update Location", callback_data='edit_location')],
        [InlineKeyboardButton(text="← Back", callback_data='back_to_profile')],
    ])

def stop_chat_kb(uid: int = None):
    rows = [
        [InlineKeyboardButton(text="🔍 Find New Match", callback_data='do_match'),
         InlineKeyboardButton(text="👤 My Profile", callback_data='back_to_profile')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

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
            f"Name: {p.get('name','?')} | Age: {age_str.lstrip(' | ')}\n"
            f"Gender: {p.get('gender','?')} | Interested in: {p.get('preferred_gender','?')}\n"
            f"Bio: {bio}\n"
            f"Location: {loc}{vb}")




# ─── /start ─────────────────────────────────────────────────────────────────

@dp.message(Command('start'))
async def cmd_start(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    await mark_online(uid)
    if await is_banned_user(uid):
        await message.answer("\u26d4 Your account has been suspended.")
        return
    args = message.text.split(' ', 1)
    ref_code = args[1].strip() if len(args) > 1 else None
    if ref_code:
        await state.update_data(ref_code=ref_code)
    if uid in user_profiles:
        p = user_profiles[uid]
        # Hide /premium from menu button for all users
        await hide_premium_cmd(uid)
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
        "<b>What should we call you?</b>",
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
        "Great! <b>How old are you?</b>\n\n"
        "Enter your age (e.g. 27):",
        parse_mode='HTML'
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
        loc_name = await reverse_geocode(loc.latitude, loc.longitude)
        if not loc_name:
            loc_name = 'GPS'
        prof.update({'lat': str(loc.latitude), 'lon': str(loc.longitude), 'location_name': loc_name})
        user_profiles[uid] = prof
        await save_all()
        await state.clear()
        await message.answer("✅ Location updated!", reply_markup=ReplyKeyboardRemove())
        return

    loc_name = await reverse_geocode(loc.latitude, loc.longitude)
    if not loc_name:
        loc_name = 'GPS'
    await state.update_data(lat=str(loc.latitude), lon=str(loc.longitude), location_name=loc_name)
    await fsm_backup_set(uid, 'lat', str(loc.latitude))
    await fsm_backup_set(uid, 'lon', str(loc.longitude))
    await fsm_backup_set(uid, 'location_name', loc_name)
    await state.set_state(Signup.bio)
    await message.answer("📍 Got it!", reply_markup=ReplyKeyboardRemove())
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
        await message.answer("✅ Location updated!", reply_markup=ReplyKeyboardRemove())
        return

    await state.update_data(lat=str(lat), lon=str(lon), location_name=text)
    await fsm_backup_set(uid, 'lat', str(lat))
    await fsm_backup_set(uid, 'lon', str(lon))
    await fsm_backup_set(uid, 'location_name', text)
    await state.set_state(Signup.bio)
    await message.answer("📍 Saved!", reply_markup=ReplyKeyboardRemove())
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
            'verified': False,
            'verified_female': data.get('gender') == 'Female',
            'verification_status': 'not_required',
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
    # Hide /premium from menu button for all users
    await hide_premium_cmd(uid)
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


@dp.message(StateFilter(Signup.dob))
async def h_dob(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    await mark_online(uid)
    d = await state.get_data()
    if d.get('prev_bot_msg'):
        await safe_delete(message.chat.id, d['prev_bot_msg'])

    raw = (message.text or '').strip()
    if not raw or not raw.isdigit():
        await message.answer(
            "<b>Please enter your age as a number.</b>\n\n"
            "Example: 27",
            parse_mode='HTML'
        )
        return

    age = int(raw)
    if age < 18:
        await message.answer(
            "\u26d4\ufe0f <b>You must be 18+ to use this bot.</b>",
            parse_mode='HTML'
        )
        return
    if age > 100:
        await message.answer(
            "\u26a0\ufe0f <b>Please enter a valid age (18–100).</b>",
            parse_mode='HTML'
        )
        return

    birth_year = date.today().year - age
    raw_dob = f"01-01-{birth_year}"
    await state.update_data(dob=raw_dob)
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
        # Prevent rematch: add each user to the other's rejected list
        for u, p in ((uid, partner), (partner, uid)):
            if p and p in user_profiles:
                if 'rejected' not in user_profiles[u]:
                    user_profiles[u]['rejected'] = []
                if p not in user_profiles[u]['rejected']:
                    user_profiles[u]['rejected'].append(p)
    await message.answer("🔚 <b>Chat ended.</b>\n\nWhat would you like to do next?",
            parse_mode='HTML', reply_markup=stop_chat_kb(uid))

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
    # Premium is for men/other only
    if user_profiles.get(uid, {}).get('gender') == 'Female':
        await message.answer("🌟 You already have unlimited access as a verified female user!", parse_mode='HTML', reply_markup=main_kb(uid))
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
            [InlineKeyboardButton(text="🌟 1 Day Trial — ₹49", callback_data='premium_1day')],
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
    is_vf = is_verified_female(uid)
    if is_vf:
        await message.answer(
            f"\U0001f389 <b>Refer & Earn Free Premium!</b>\n\n"
            f"Your code: <code>{code}</code>\n\n"
            f"Share this bot with friends.",
            parse_mode='HTML'
        )
    else:
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

@dp.message(Command('report'))
async def cmd_report(message: types.Message):
    uid = message.from_user.id
    await mark_online(uid)
    if uid not in user_profiles:
        await message.answer("📝 Set up your profile first, then send /report")
        return
    partners = await get_chat_partners(uid)
    if not partners:
        await message.answer("🚫 No active chat to report from. Start a conversation first.")
        return
    rows = [[InlineKeyboardButton(
        text=user_profiles[p].get('name', 'User'),
        callback_data=f'report_pick:{p}'
    )] for p in partners if p in user_profiles]
    await message.answer(
        "🚩 <b>Report a User</b>\n\nSelect the user you want to report:",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )

@dp.message(Command('feedback'))
async def cmd_feedback(message: types.Message):
    uid = message.from_user.id
    await mark_online(uid)
    if uid not in user_profiles:
        await message.answer("📝 Set up your profile first, then send /feedback")
        return
    await Feedback.message.set()
    await message.answer(
        "💬 <b>Send Feedback</b>\n\nType your message, suggestion, or issue below:",
        parse_mode='HTML'
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

# ─── Report User ──────────────────────────────────────────────────────────────

@dp.callback_query(lambda cb: cb.data == 'report_user')
async def report_user(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    if uid not in user_profiles:
        await cb.message.edit_text("Set up your profile first via /start.")
        await cb.answer()
        return
    partner_uids = []
    for mid, chat in current_chat.items():
        if chat.get('other_uid') == uid:
            partner_uids.append(mid)
        elif mid == uid:
            partner_uids.append(chat.get('other_uid'))
    for a_uid in list(active_matches.keys()):
        p = user_profiles.get(a_uid, {})
        if p.get('partner') == uid:
            partner_uids.append(a_uid)
    partner_uids = [u for u in set(partner_uids) if u in user_profiles]
    if not partner_uids:
        await cb.message.edit_text(
            "No one to report yet. You can only report users you've matched with.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Back", callback_data='back_to_profile')]
            ])
        )
        await cb.answer()
        return
    buttons = []
    for puid in partner_uids[:10]:
        pname = user_profiles[puid].get('name', 'User')
        buttons.append([InlineKeyboardButton(text=f"{pname}", callback_data=f'report_pick:{puid}')])
    buttons.append([InlineKeyboardButton(text="Cancel", callback_data='back_to_profile')])
    await cb.message.edit_text(
        "Who do you want to report?",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await cb.answer()

@dp.callback_query(lambda cb: cb.data.startswith('report_pick:'))
async def report_pick(cb: types.CallbackQuery, state: FSMContext):
    target_uid = int(cb.data.split(':')[1])
    await state.update_data(target_uid=target_uid)
    tname = user_profiles.get(target_uid, {}).get('name', 'User')
    await cb.message.edit_text(
        f"Report: {tname}\n\nSelect a reason:",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Inappropriate messages", callback_data='reason:harassment')],
            [InlineKeyboardButton(text="Spam or fake profile", callback_data='reason:spam')],
            [InlineKeyboardButton(text="Something else", callback_data='reason:other')],
            [InlineKeyboardButton(text="Cancel", callback_data='report_user')],
        ])
    )
    await cb.answer()

@dp.callback_query(lambda cb: cb.data.startswith('reason:'))
async def report_reason(cb: types.CallbackQuery, state: FSMContext):
    reason_map = {'harassment': 'Inappropriate / Harassment', 'spam': 'Spam / Fake Profile', 'other': 'Other'}
    reason_key = cb.data.split(':')[1]
    reason_text = reason_map.get(reason_key, 'Unknown')
    await state.update_data(reason=reason_text)
    data = await state.get_data()
    tname = user_profiles.get(data['target_uid'], {}).get('name', 'User')
    await cb.message.edit_text(
        f"Confirm Report\n\nUser: {tname}\nReason: {reason_text}\n\nSubmit this report?",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Submit", callback_data='report_confirm')],
            [InlineKeyboardButton(text="Cancel", callback_data='back_to_profile')],
        ])
    )
    await cb.answer()

@dp.callback_query(lambda cb: cb.data == 'report_confirm')
async def report_confirm(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    data = await state.get_data()
    target_uid = data.get('target_uid')
    reason = data.get('reason', 'Unknown')
    if not target_uid:
        await cb.message.edit_text("Something went wrong. Try again.", reply_markup=main_kb(uid))
        await state.clear()
        await cb.answer()
        return
    count = await add_report(target_uid, uid, reason)
    tname = user_profiles.get(target_uid, {}).get('name', 'User')
    banned = count >= REPORT_THRESHOLD
    await send_admin(
        f"New Report\n\nReported: {tname} (uid:{target_uid})\nReason: {reason}\nTotal: {count}/3\nReporter uid: {uid}"
    )
    if banned:
        await send_admin(f"AUTO-BANNED: {tname} (uid:{target_uid}) reached {count} reports!")
    await cb.message.edit_text(
        f"Report submitted!{(' This user has been suspended.' if banned else '')}",
        parse_mode='HTML', reply_markup=main_kb(uid)
    )
    await state.clear()
    await cb.answer()

# ─── Feedback ─────────────────────────────────────────────────────────────────

@dp.callback_query(lambda cb: cb.data == 'send_feedback')
async def feedback_start(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    if uid not in user_profiles:
        await cb.message.edit_text("Set up your profile first via /start.")
        await cb.answer()
        return
    await cb.message.edit_text(
        "Send Feedback\n\nType your message, suggestion, or issue below:",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Cancel", callback_data='back_to_profile')],
        ])
    )
    await state.set_state(Feedback.message)
    await cb.answer()

@dp.message(StateFilter(Feedback.message))
async def feedback_receive(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    text = (message.text or '').strip()
    if not text or len(text) < 5:
        await message.answer("Message too short. Please provide more detail.")
        return
    await state.update_data(message=text)
    await message.answer(
        f"Confirm\n\n{text}\n\nSend this feedback?",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Send", callback_data='feedback_confirm')],
            [InlineKeyboardButton(text="Edit", callback_data='feedback_edit')],
            [InlineKeyboardButton(text="Cancel", callback_data='back_to_profile')],
        ])
    )

@dp.callback_query(lambda cb: cb.data == 'feedback_edit')
async def feedback_edit(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.edit_text(
        "Edit your feedback:",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Cancel", callback_data='back_to_profile')],
        ])
    )
    await state.set_state(Feedback.message)
    await cb.answer()

@dp.callback_query(lambda cb: cb.data == 'feedback_confirm')
async def feedback_confirm(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    data = await state.get_data()
    text = data.get('message', '')
    uname = user_profiles.get(uid, {}).get('name', 'Unknown')
    await send_admin(f"Feedback\n\nFrom: {uname} (uid:{uid})\n\n{text}")
    await cb.message.edit_text(
        "Feedback sent! Thank you!",
        parse_mode='HTML', reply_markup=main_kb(uid)
    )
    await state.clear()
    await cb.answer()

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
    if uid in current_chat:
        await cb.answer("End your current chat before finding a new match.", show_alert=True)
        return
    me = user_profiles[uid]
    m = find_queue_match(me, uid)
    if m:
        pid = m['uid']
        if pid in current_chat:
            waiting_queue.pop(pid, None)
            await cb.answer("That match is already in a chat. Please try again.", show_alert=True)
            return
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
        f"\U0001f465 <b>{online}</b> online  \u00b7  \u23f3 <b>{ql}</b> in queue\n\n"
        f"\U0001f464 <b>{me['name']}</b>, finding someone great for you...\n\n"
        "<i>The right connection takes a moment \u2728</i>",
        parse_mode='HTML'
    )
    _queue_msg_ids[uid] = sm.message_id
    await save_all()
    await cb.answer()

async def send_match_card(cid: int, partner: dict, pid: int):
    n = partner.get('name', '?')
    g = partner.get('gender', '?')
    g_short = {"Male": "M", "Female": "F", "Other": "O"}.get(g, "")
    dob_raw = partner.get('dob', '')
    loc = partner.get('location_name', '')

    # Age
    age = ""
    if dob_raw:
        dob = parse_dob(dob_raw)
        if dob:
            age = str(calc_age(dob))

    # Verified badge
    vb = " ✅" if partner.get('verified') else ""

    # Distance
    dist_str = ""
    me = user_profiles.get(cid)
    if me and me.get('lat') is not None and partner.get('lat') is not None:
        try:
            d = haversine(float(me['lat']), float(me['lon']), float(partner['lat']), float(partner['lon']))
            dist_str = f"{'Less than 1' if d < 1 else f'{round(d, 1)}'} km away"
        except (ValueError, TypeError):
            pass

    lines = [
        f"🎉 <b>It's a match with {n}!</b>",
        "",
        f"👤 <b>{n}</b>, {age} {g_short}{vb}",
    ]
    if loc:
        lines.append(f"📍 location: {loc}")
    if dist_str:
        lines.append(f"📏 Distance: {dist_str}")
    lines += [
        "",
        "Say something nice. 💬",
    ]
    txt = "\n".join(lines)

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
    if current_chat.get(uid) not in (None, pid):
        await cb.answer("End your current chat first.", show_alert=True)
        return
    if current_chat.get(pid) not in (None, uid):
        await cb.answer("This match is already in another chat.", show_alert=True)
        return
    if not await has_text_quota(uid):
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
    if current_chat.get(pid) != uid:
        current_chat.pop(uid, None)
        await save_all()
        await cb.answer("This chat is no longer active.", show_alert=True)
        return
    if not is_verified_female(uid) and not await reserve_text_quota(uid):
            await cb.message.edit_text(
                "⏸️ <b>Text limit reached</b>\n\nYou've used all your free texts.",
                parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🌟 1 Day Trial — ₹49", callback_data='premium_1day'),
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
        await save_all()
    except:
        await refund_text_quota(uid)
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
    # Prevent rematch: add each user to the other's rejected list
    for u, p in ((uid, partner), (partner, uid)):
        if p and p in user_profiles:
            if 'rejected' not in user_profiles[u]:
                user_profiles[u]['rejected'] = []
            if p not in user_profiles[u]['rejected']:
                user_profiles[u]['rejected'].append(p)
    await save_all()
    await cb.message.edit_text(
        "🔚 <b>Chat ended.</b>\n\nWhat would you like to do next?",
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
        if a_uid in current_chat and not is_premium(a_uid) and not await has_text_quota(a_uid):
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
            [InlineKeyboardButton(text="\U0001f44b Say Hi", callback_data=f'say_hi:{pid}')],
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
    if current_chat.get(pid) != uid:
        current_chat.pop(uid, None)
        await save_all()
        await message.answer("This chat is no longer active.")
        return
    if not is_verified_female(uid) and not await has_text_quota(uid):
                base = "⏸️ <b>Text limit reached</b>\n\nYou've used all your free messages. Upgrade to keep chatting or find a new match."
                await message.answer(
                    base,
                    parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🌟 1 Day Trial — ₹49", callback_data='premium_1day'),
                         InlineKeyboardButton(text="\U0001f4cb Plans", callback_data='premium_plans')],
                    ])
                )
                # Notify partner immediately — once per on-hold episode
                if uid not in _partner_notified_on_hold:
                    _partner_notified_on_hold.add(uid)
                    pname = user_profiles.get(uid, {}).get('name', 'Someone')
                    try:
                        await bot.send_message(pid,
                            f"⏳ <b>{pname}</b> is on hold.\n\n"
                            "They can still receive your messages. We'll notify you when they reconnect.",
                            parse_mode='HTML'
                        )
                    except:
                        pass
                if uid not in _reconnect_tasks:
                    _reconnect_tasks[uid] = asyncio.create_task(_reconnect_loop(uid, pid))
                return
    # Receiver's receive limit: Male/Other without premium limited to RECEIVE_LIMIT total
    p_receiver = user_profiles.get(pid)
    if (p_receiver and p_receiver.get('gender') in ('Male', 'Other')
            and not is_premium(pid) and not is_verified_female(pid)):
        received = p_receiver.get('received_texts', 0)
        if not is_verified_female(uid) and received >= RECEIVE_LIMIT:
            # Reuse the existing "Text limit reached" bubble for the sender
            pname = user_profiles[uid].get('name', 'Someone')
            n = _quota_notif.get(uid)
            count = (n['count'] + 1) if n else 1
            text = "⏸️ <b>Text limit reached</b>\n\nYou've used all your free messages."
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🌟 1 Day Trial — ₹49", callback_data='premium_1day'),
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
    reserved_text = False
    try:
        # If receiver has no quota, show notification instead of forwarding
        if not await has_text_quota(pid):
            pname = user_profiles[uid].get('name', 'Someone')
            n = _quota_notif.get(pid)
            count = (n['count'] + 1) if n else 1
            text = f"📬 <b>{pname}</b> sent {count} message{'s' if count > 1 else ''}. Upgrade to read and reply."
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🌟 1 Day Trial — ₹49", callback_data='premium_1day')],
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
        if not is_verified_female(uid) and not await reserve_text_quota(uid):
            await message.answer(
                "Text limit reached. Upgrade to keep chatting.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="1 Day Trial", callback_data='premium_1day'),
                     InlineKeyboardButton(text="Plans", callback_data='premium_plans')],
                ])
            )
            if uid not in _reconnect_tasks:
                _reconnect_tasks[uid] = asyncio.create_task(_reconnect_loop(uid, pid))
            return
        reserved_text = True
        await bot.copy_message(pid, message.chat.id, message.message_id)
        # Increment receiver's received_texts (for Male/Other without premium)
        p_recv = user_profiles.get(pid)
        if p_recv and p_recv.get('gender') in ('Male', 'Other') and not is_premium(pid):
            p_recv['received_texts'] = p_recv.get('received_texts', 0) + 1
        await save_all()
    except:
        if reserved_text:
            await refund_text_quota(uid)
        await message.answer("⚠️ Couldn't deliver your message.")

# ─── Premium callbacks ───────────────────────────────────────────────────────

@dp.callback_query(lambda cb: cb.data == 'premium_1day')
async def prem_1(cb: types.CallbackQuery):
    uid = cb.from_user.id
    await mark_online(uid)
    if is_premium(uid):
        await cb.message.edit_text("🌟 <b>Already Premium!</b>\n\nYou have unlimited access.",
                                    parse_mode='HTML', reply_markup=main_kb(uid))
        await cb.answer()
        return
    await cb.message.edit_text("\u23f3 Creating payment link...")
    url = await make_payment_link(uid, "trial_1d")
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
        "🌟 <b>1 Day Unlimited — ₹49</b>\n\n"
        "Chat freely with your match. No limits for 24 hours.\n\n"
        "Tap below to complete payment:",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Pay ₹49 — Activate Now", url=url)],
            [InlineKeyboardButton(text="\U0001f4cb See All Plans", callback_data='premium_plans')],
            [InlineKeyboardButton(text="\U0001f464 View Profile", callback_data='back_to_profile')],
        ])
    )
    await cb.answer()

@dp.callback_query(lambda cb: cb.data == 'premium_plans')
async def prem_plans(cb: types.CallbackQuery):
    uid = cb.from_user.id
    await mark_online(uid)
    plan_rows = [
        ("1 Day",    49,  1,  "\xf0\x9f\x8f\xa0"),
        ("1 Week",   99,  7,  "\xe2\x9c\x85 Best Value"),
        ("2 Weeks",  149, 14, ""),
        ("1 Month",  199, 30, "\xe2\x9c\x85 Popular"),
    ]
    rows = []
    for name, price, days, badge in plan_rows:
        per_day = round(price / days)
        saving = max(0, int(round((1 - price / (49 * days)) * 100)))
        badge_txt = badge + " " if badge else ""
        saving_txt = f" (Save {saving}%)" if saving > 0 else ""
        row_text = f"{badge_txt}\U0001f3c6 {name} \xe2\x80\x94 \xe2\x82\xb9{price} \xe2\x80\xa2 Rs{per_day}/day{saving_txt}"
        plan_id = ("trial_1d" if name == "1 Day" else
                   "weekly"   if name == "1 Week"  else
                   "biweekly" if name == "2 Weeks" else
                   "monthly")
        rows.append([InlineKeyboardButton(text=row_text, callback_data=f"premium_select:{plan_id}")])
    rows.append([InlineKeyboardButton(text="\u25c0\ufe0f Back", callback_data='back_to_premium')])
    await cb.message.edit_text(
        "\U0001f3c6 <b>Unlimited Premium Plans</b>\n\n"
        "\U0001f44d All plans include unlimited texts & matches\n\n"
        "\xe2\x9c\x85 1 Day    \xe2\x80\x94 \xe2\x82\xb949  \xe2\x80\xa2 Rs49/day\n"
        "\xe2\x9c\x85 1 Week   \xe2\x80\x94 \xe2\x82\xb999  \xe2\x80\xa2 Rs14/day (Save 71%)\n"
        "\xe2\x9c\x85 2 Weeks \xe2\x80\x94 \xe2\x82\xb9149 \xe2\x80\xa2 Rs11/day (Save 78%)\n"
        "\xe2\x9c\x85 1 Month  \xe2\x80\x94 \xe2\x82\xb9199 \xe2\x80\xa2 Rs7/day  (Save 86%)\n\n"
        "\xf0\x9f\x8f\xa0 <b>Save up to 86%</b> with longer plans!",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await cb.answer()

@dp.callback_query(lambda cb: cb.data.startswith('premium_select:'))
async def prem_sel(cb: types.CallbackQuery):
    uid = cb.from_user.id
    await mark_online(uid)
    _, plan_id = cb.data.split(':', 1)
    plan = PLAN_CATALOG.get(plan_id)
    if not plan:
        await cb.answer("Invalid plan.", show_alert=True)
        return
    name, price, dur = plan['name'], int(plan['price']), int(plan['duration'])
    await cb.message.edit_text(f"\U0001f3c6 Preparing your <b>{name} Premium</b>...")
    url = await make_payment_link(uid, plan_id)
    if not url:
        await cb.message.edit_text(
            "\xe2\x9a\xa0\xef\xb8\x8f Couldn't create payment link. Please try again later.",
            parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="\u25c0\ufe0f Back", callback_data='premium_plans')],
            ])
        )
        await cb.answer("Payment failed", show_alert=True)
        return
    day_word = "s" if dur > 1 else ""
    await cb.message.edit_text(
        f"\U0001f3c6 <b>{name} Premium</b>\n\n"
        f"\U0001f4ac Unlimited texts & matches for <b>{dur} day{day_word}</b>\n\n"
        f"\U0001f4b0 Total: <b>\xe2\x82\xb9{price}</b>\n\n"
        "\U0001f4b3 Tap below to pay & activate instantly:",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"\U0001f4b3 Pay \xe2\x82\xb9{price}", url=url)],
            [InlineKeyboardButton(text="\u25c0\ufe0f Back to Plans", callback_data='premium_plans')],
        ])
    )
    await cb.answer()@dp.callback_query(lambda cb: cb.data == 'back_to_premium')
async def back_prem(cb: types.CallbackQuery):
    uid = cb.from_user.id
    await mark_online(uid)
    if user_profiles.get(uid, {}).get('gender') == 'Female':
        await cb.message.edit_text("🌟 You already have unlimited access!", parse_mode='HTML', reply_markup=main_kb(uid))
    elif is_premium(uid):
        await cb.message.edit_text("🌟 <b>Already Premium!</b>", parse_mode='HTML', reply_markup=main_kb(uid))
    else:
        await cb.message.edit_text(
            f"\U0001f3c6 <b>Premium Plans</b>\n\n{quota_summary(uid)}\n\nChoose a plan:",
            parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🌟 1 Day Trial — ₹49", callback_data='premium_1day')],
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
    await state.update_data(dob=f"01-01-{year}", prev_bot_msg=None)
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
    d = await state.get_data()
    gps_msg_id = d.get('gps_msg_id')
    if gps_msg_id:
        await safe_delete(cb.message.chat.id, gps_msg_id)
    await state.set_state(Signup.location)
    await cb.message.edit_text(
        "\U0001f4cd <b>Type a city or area name</b> (e.g. HSR Layout, Bangalore):",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📍 Share My Location", callback_data="loc_share_gps")],
        ])
    )
    await cb.message.answer("⌨️", reply_markup=ReplyKeyboardRemove())
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
    d = await state.get_data()
    gps_msg_id = d.get('gps_msg_id')
    if gps_msg_id:
        await safe_delete(cb.message.chat.id, gps_msg_id)
    await state.set_state(EditProfile.location)
    await cb.message.edit_text(
        "\U0001f4cd <b>Type a city or area name</b> (e.g. HSR Layout, Bangalore):",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📍 Share My Location", callback_data="loc_share_gps_edit")],
            [InlineKeyboardButton(text="\u00ab  Back", callback_data='edit_profile')],
        ])
    )
    await cb.message.answer("⌨️", reply_markup=ReplyKeyboardRemove())
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
    if message.location:
        loc_name = await reverse_geocode(lat, lon)
        if not loc_name:
            loc_name = f"{lat:.4f}, {lon:.4f}"
        user_profiles[uid]['location_name'] = loc_name
    else:
        user_profiles[uid]['location_name'] = text
    await save_all()
    await state.clear()
    await message.answer("✅ Location updated!", reply_markup=ReplyKeyboardRemove())
    await message.answer("\U0001f464 <b>Profile</b>", parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
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
                    status_text = (
                        f"\U0001f465 {online} online  \u00b7  \u23f3 {ql} in queue\n\n"
                        f"\U0001f464 {user_profiles[uid].get('name', '?')}, still looking... \u2728"
                    )
                    try:
                        await bot.edit_message_text(status_text, uid, mid, parse_mode='HTML')
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
                                            f"\U0001f50d Looking a little wider... \u2728",
                                            uid, _queue_msg_ids[uid], parse_mode='HTML'
                                        )
                                    except:
                                        pass
                            else:
                                # 3 attempts exhausted — give up
                                if uid in _queue_msg_ids:
                                    try:
                                        await bot.edit_message_text(
                                            "\U0001f4ab <b>No matches found right now</b>\n\n"
                                            "Timing is everything \u2014 come back soon!",
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
        if not RAZORPAY_WEBHOOK_SECRET:
            return web.Response(status=400, text="Webhook secret not configured")
        sig = request.headers.get('X-Razorpay-Signature')
        if not sig:
            return web.Response(status=400, text="Missing signature")
        body = await request.read()
        expected = hmac.new(RAZORPAY_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            logger.warning("Razorpay webhook invalid signature")
            return web.Response(status=400, text="Invalid signature")
        try:
            data = json.loads(body.decode('utf-8'))
        except json.JSONDecodeError:
            return web.Response(status=400, text="Invalid JSON")

        event = data.get('event', '')
        payload = data.get('payload', {})
        payment_link = payload.get('payment_link', {}).get('entity', {}) or {}
        payment = payload.get('payment', {}).get('entity', {}) or {}

        link_id = payment_link.get('id') or payment.get('payment_link_id')
        payment_id = payment.get('id') or f"{event}:{link_id}"
        if not link_id:
            logger.info(f"Ignoring Razorpay event without payment_link id: {event}")
            return web.Response(status=200, text="Ignored")

        link_status = str(payment_link.get('status', '')).lower()
        payment_status = str(payment.get('status', '')).lower()
        status_ok = (
            (event == 'payment_link.paid' and link_status == 'paid') or
            (event == 'payment.captured' and payment_status == 'captured')
        )
        if not status_ok:
            logger.info(f"Ignoring Razorpay event with non-paid status: event={event}")
            return web.Response(status=200, text="Ignored")

        r = await get_redis()
        pending_key = f'winkly:payment:{link_id}'
        pending = None
        resolved_from_memory = False
        # Try Redis first, then in-memory fallback
        if r:
            pending = await r.hgetall(pending_key)
        if not pending:
            pending = _payment_link_map.get(link_id)
            if pending:
                resolved_from_memory = True
                logger.info(f"Razorpay webhook resolved from in-memory map: {link_id}")
        if not pending:
            logger.warning(f"Razorpay webhook for unknown payment link: {link_id}")
            return web.Response(status=400, text="Unknown payment link")

        # Persist to Redis if we resolved from memory (so retries find it)
        if r and resolved_from_memory:
            try:
                await r.hset(pending_key, mapping=pending)
                await r.expire(pending_key, 86400)
            except Exception:
                pass

        plan_id = pending.get('plan_id')
        plan = PLAN_CATALOG.get(plan_id)
        if not plan:
            logger.warning(f"Razorpay webhook pending payment has invalid plan: {plan_id}")
            return web.Response(status=400, text="Invalid pending payment")

        expected_amount = int(pending.get('amount', '0'))
        amount = int(payment.get('amount') or payment_link.get('amount_paid') or payment_link.get('amount') or 0)
        currency = payment.get('currency') or payment_link.get('currency')
        if amount != expected_amount or currency != pending.get('currency'):
            logger.warning(
                f"Razorpay amount/currency mismatch for {link_id}: got {amount} {currency}, expected {expected_amount} {pending.get('currency')}"
            )
            return web.Response(status=400, text="Payment mismatch")

        notes = payment_link.get('notes') or payment.get('notes') or {}
        if notes:
            if str(notes.get('uid', pending.get('uid'))) != pending.get('uid'):
                return web.Response(status=400, text="UID mismatch")
            if str(notes.get('plan_id', plan_id)) != plan_id:
                return web.Response(status=400, text="Plan mismatch")

        processed_key = f'winkly:processed_payment:{payment_id}'
        claimed = await r.set(processed_key, '1', nx=True, ex=86400 * 365)
        if not claimed:
            return web.Response(status=200, text="Already processed")

        uid = int(pending['uid'])
        dur = int(plan['duration'])
        exp = await activate_premium(uid, dur)
        _processed_payments.add(payment_id)
        await r.hset(pending_key, mapping={
            'status': 'paid',
            'payment_id': str(payment_id),
            'paid_at': datetime.now().isoformat(),
        })
        await r.expire(pending_key, 86400 * 30)
        logger.info(f"Premium activated for uid={uid}, plan_id={plan_id}, until={exp.isoformat()}")
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
        except Exception as e:
            logger.warning(f"Premium activation message failed for uid={uid}: {e}")
        return web.Response(status=200, text="OK")
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return web.Response(status=500, text=str(e))

async def payment_success_page(request):
    logger.info(
        "Payment success callback hit: link_present=%s payment_present=%s",
        bool(request.query.get('razorpay_payment_link_id')),
        bool(request.query.get('razorpay_payment_id')),
    )
    result = await reconcile_paid_payment_link(request.query.get('razorpay_payment_link_id', ''))
    activated = bool(result.get('activated'))
    title = "Premium Activated - Winkly" if activated else "Payment Received - Winkly"
    heading = "Premium Activated!" if activated else "Payment Received"
    message = "Your premium is active. Open Telegram and start chatting!" if activated else "Your payment is being verified by Razorpay."
    followup = "" if activated else "<p>Premium activates in Telegram after secure server confirmation.</p>"
    return web.Response(
        content_type='text/html',
        text=f'<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title><style>*{{box-sizing:border-box}}body{{font-family:sans-serif;background:#1a1a2e;color:#eee;text-align:center;padding:40px 20px;margin:0}}.card{{background:#16213e;border-radius:16px;padding:32px;max-width:400px;width:100%;margin:0 auto}}.check{{font-size:64px;margin-bottom:16px}}.btn{{background:#e94560;color:#fff;border:none;border-radius:8px;padding:14px 28px;font-size:16px;cursor:pointer;text-decoration:none;display:inline-block;margin-top:16px}}</style></head><body><div class="card"><div class="check">&#9989;</div><h1>{heading}</h1><p>{message}</p>{followup}<a class="btn" href="https://t.me/{BOT_USERNAME}">Open Telegram</a></div></body></html>'
    )

    link_id = request.query.get('razorpay_payment_link_id')
    payment_id = request.query.get('razorpay_payment_id')
    uid = None
    activated = False

    if link_id:
        # Look up pending payment from in-memory map or Redis
        pending = _payment_link_map.get(link_id)
        if not pending:
            r = await get_redis()
            if r:
                try:
                    data = await r.hgetall(f'winkly:payment:{link_id}')
                    if data:
                        pending = data
                except:
                    pass
        if pending:
            try:
                uid = int(pending.get('uid', 0))
                plan_id = pending.get('plan_id', '')
                plan = PLAN_CATALOG.get(plan_id)
                if uid and plan:
                    dur = int(plan['duration'])
                    pass
            except Exception as e:
                logger.error(f"Payment success page activation error: {e}")

    return web.Response(
        content_type='text/html',
        text=f'<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{"Premium Activated - Winkly" if activated else "Payment Received - Winkly"}</title><style>body{{font-family:sans-serif;background:#1a1a2e;color:#eee;text-align:center;padding:40px 20px}}.card{{background:#16213e;border-radius:16px;padding:32px;max-width:400px;margin:0 auto}}.check{{font-size:64px;margin-bottom:16px}}.btn{{background:#e94560;color:#fff;border:none;border-radius:8px;padding:14px 28px;font-size:16px;cursor:pointer;text-decoration:none;display:inline-block;margin-top:16px}}</style></head><body><div class="card"><div class="check">{"&#9989;" if activated else "&#9989;"}</div><h1>{"Premium Activated! 🎉" if activated else "Payment Received"}</h1><p>{"Your premium is active. Open Telegram and start chatting!" if activated else "Your payment is being verified. Premium will activate shortly."}</p><a class="btn" href="https://t.me/{BOT_USERNAME}">Open Telegram</a></div></body></html>'
    )

async def auto_setup_webhook():
    if not razorpay_client:
        logger.info("Razorpay unavailable - skipping webhook setup")
        return
    if not WEBHOOK_URL or not RAZORPAY_WEBHOOK_SECRET:
        logger.info("Razorpay webhook setup skipped: WEBHOOK_URL or RAZORPAY_WEBHOOK_SECRET missing")
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



# ─── DEV: Direct premium activation for testing (remove in production) ───────
# ─── Startup ───────────────────────────────────────────────────────────────

async def on_startup(dispatcher: Dispatcher):
    logger.info("Starting Winkly Bot v2...")
    # Menu button (left of emoji bar) showing bot commands
    # Default: all 7 commands for unregistered users. /premium hidden per-user on signup or /start.
    try:
        await bot.set_my_commands(ALL_COMMANDS, scope=BotCommandScopeDefault())
        logger.info("Menu button added (7 commands for default scope)")
    except Exception as e:
        logger.error(f"Failed to set commands: {e}")

    await init_storage()
    logger.info(f"Loaded {len(user_profiles)} profiles, {len(active_matches)} matches")

    if WEBHOOK_URL:
        if not TELEGRAM_WEBHOOK_SECRET:
            raise RuntimeError("TELEGRAM_WEBHOOK_SECRET is required when WEBHOOK_URL is set")
        webhook_target = WEBHOOK_URL
        if not webhook_target.endswith(TELEGRAM_WEBHOOK_PATH):
            webhook_target = f"{WEBHOOK_URL}{TELEGRAM_WEBHOOK_PATH}"
        await bot.set_webhook(webhook_target, secret_token=TELEGRAM_WEBHOOK_SECRET)
        logger.info(f"Webhook set to {webhook_target}")
        await auto_setup_webhook()

        from aiogram.webhook.aiohttp_server import SimpleRequestHandler

        handler = SimpleRequestHandler(
            dispatcher=dispatcher,
            bot=bot,
            secret_token=TELEGRAM_WEBHOOK_SECRET,
        )
        app = web.Application(client_max_size=2 * 1024 * 1024)

        async def health(request):
            return web.Response(text='OK', status=200)
        app.router.add_get('/health', health)
        app.router.add_post('/health', health)

        async def razorpay_status(request):
            status = {
                'razorpay_configured': razorpay_client is not None,
                'key_id_present': bool(RAZORPAY_KEY_ID),
            }
            if razorpay_client:
                try:
                    # Try to create a test payment link
                    test_link = razorpay_client.payment_link.create({
                        'amount': 100,
                        'currency': 'INR',
                        'description': 'Winkly Test',
                        'callback_url': 'https://winkly-kmsz.onrender.com/payment/success',
                        'callback_method': 'get'
                    })
                    status['test_link'] = 'success'
                    status['test_url'] = test_link.get('short_url')
                except Exception as e:
                    status['test_link'] = 'failed'
                    status['test_error'] = str(e)
            return web.json_response(status)
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

        async def fsm_read(request):
            # Read FSM data for a specific user using aiogram's FSM context
            uid = int(request.query.get('uid', ADMIN))
            from aiogram.fsm.context import FSMContext
            ctx = FSMContext(storage=dp.storage, user_id=uid, chat_id=uid)
            data = await ctx.get_data()
            state = await ctx.get_state()
            return web.json_response({'uid': uid, 'state': state, 'data': data})
        async def test_route(request):
            return web.json_response({'test': 'ok', 'path': str(request.path)})

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
                'verified': False,
                'verified_female': False,
                'verification_status': 'not_required',
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
                'verified': False,
                'verified_female': False,
                'verification_status': 'not_required',
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
        app.router.add_post('/razorpay/webhook', handle_razorpay_webhook)
        app.router.add_get('/payment/success', payment_success_page)
        handler.register(app, path=TELEGRAM_WEBHOOK_PATH)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host='0.0.0.0', port=PORT)
        await site.start()
        logger.info(f"Server running on port {PORT}")

        asyncio.create_task(update_counters_loop())
        asyncio.create_task(check_queue_loop())
        await asyncio.Event().wait()
    else:
        if not ALLOW_LONG_POLLING:
            raise RuntimeError(
                "WEBHOOK_URL is required for deployment. "
                "Set ALLOW_LONG_POLLING=true only for local single-instance polling."
            )
        logger.info("No WEBHOOK_URL - long-polling mode explicitly enabled")
        await bot.delete_webhook(drop_pending_updates=True)
        asyncio.create_task(update_counters_loop())
        asyncio.create_task(check_queue_loop())
        await dp.start_polling(bot, skip_updates=False)

if __name__ == '__main__':
    asyncio.run(on_startup(dp))

# ─── /unban ───────────────────────────────────────────────────────────────────

@dp.message(Command('unban'))
async def cmd_unban(message: types.Message):
    uid = message.from_user.id
    if uid != ADMIN_CHAT_ID:
        return
    args = message.text.split(' ', 1)
    if len(args) < 2:
        await message.answer("Usage: /unban <uid>")
        return
    try:
        target_uid = int(args[1].strip())
    except ValueError:
        await message.answer("Invalid UID. Usage: /unban <uid>")
        return
    try:
        r = await get_redis()
        removed = await r.srem('banned_users', str(target_uid))
        if removed:
            await r.delete(f'reports:{target_uid}')
            await message.answer(f"Unbanned: {target_uid}")
        else:
            await message.answer(f"Not banned: {target_uid}")
    except Exception as e:
        await message.answer(f"Error: {e}")
