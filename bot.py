"""Winkly Dating Bot v2 - Complete Implementation"""
import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import random
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
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, BotCommand, BotCommandScopeDefault
from aiogram.filters.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '.env'))

BOT_TOKEN = os.getenv('BOT_TOKEN', '8624196108:***')
REDIS_URL = os.getenv('REDIS_URL', '')
WEBHOOK_URL = os.getenv('WEBHOOK_URL', 'https://winkly-kmsz.onrender.com')
PORT = int(os.getenv('PORT', '8080'))
ADMIN_CHAT_ID = int(os.getenv('ADMIN_CHAT_ID', '0'))
RAZORPAY_KEY_ID = os.getenv('RAZORPAY_KEY_ID', 'rzp_live_T5RFsK3b9AYBTX')
RAZORPAY_KEY_SECRET = os.getenv('RAZORPAY_KEY_SECRET', 'MBAphgobB9XnZ33SylDA9r7C')
RAZORPAY_WEBHOOK_SECRET = os.getenv('RAZORPAY_WEBHOOK_SECRET', 'winkly_webhook_secret')

LONG_PLANS = [
    {"name": "Monthly",   "price": 199, "duration": 30},
    {"name": "3 Months", "price": 299, "duration": 90},
    {"name": "6 Months", "price": 499, "duration": 180},
    {"name": "1 Year",    "price": 699, "duration": 365},
]

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot=bot, storage=storage)

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
    # Migrate: ensure new fields exist for existing profiles loaded from Redis
    for prof in user_profiles.values():
        if 'free_texts' not in prof:
            prof['free_texts'] = FREE_TEXTS_JOINING
        if 'rejected' not in prof:
            prof['rejected'] = []
        if 'dob' not in prof:
            prof['dob'] = ''
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

async def save_all():
    r = await get_redis()
    if r is None:
        return  # In-memory only, nothing to persist
    try:
        await r.set('winkly:profiles',  json.dumps(user_profiles))
        await r.set('winkly:matches',   json.dumps(active_matches))
        await r.set('winkly:queue',     json.dumps(waiting_queue))
        await r.set('winkly:premium',   json.dumps(premium_subscriptions))
        await r.set('winkly:chat',      json.dumps(current_chat))
        await r.set('winkly:likes',     json.dumps({k: list(v) for k, v in likes_sent.items()}))
        await r.set('winkly:processed', json.dumps(list(_processed_payments)))
    except Exception as e:
        logger.warning(f"Redis save failed: {e}")

user_profiles: Dict[int, dict] = {}
active_matches: Dict[int, Dict[int, dict]] = {}
likes_sent: Dict[int, Set[int]] = {}
waiting_queue: Dict[int, dict] = {}
premium_subscriptions: Dict[int, dict] = {}
current_chat: Dict[int, int] = {}
_verify_pending: Dict[int, str] = {}
_queue_msg_ids: Dict[int, int] = {}
_processed_payments: Set[str] = set()

class Signup(StatesGroup):
    name = State()
    gender = State()
    preferred = State()
    location = State()
    photo = State()
    bio = State()
    dob = State()

class EditProfile(StatesGroup):
    name = State()
    bio = State()
    gender = State()
    preferred = State()
    location = State()
    photo = State()
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

FREE_TEXTS_JOINING = 30  # One-time free texts for new users

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
        return f"FREE - {ft} free texts left. Upgrade from Rs49/day for unlimited."
    return "FREE LIMIT REACHED - Upgrade from Rs49/day for unlimited texts."

def referral_code(uid: int) -> str:
    return hashlib.md5(f"winkly_{uid}_ref".encode()).hexdigest()[:8].upper()

async def referral_count(uid: int) -> int:
    try:
        r = await get_redis(); return await r.scard(f'winkly:referrals:{uid}')
    except: return 0

async def award_free_premium(uid: int):
    exp = datetime.now() + timedelta(days=1)
    premium_subscriptions[uid] = {'expiry_date': exp.isoformat()}
    await save_all()
    try:
        await bot.send_message(uid,
            "FREE PREMIUM EARNED! You unlocked 1 day of FREE unlimited texts and matches! Valid 24 hours. Enjoy!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="FIND MATCHES", callback_data='do_match')],
            ]))
    except: pass
    logger.info(f"Free premium awarded to {uid}")

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

def find_queue_match(me: dict):
    me2 = {**me, '_uid': me.get('_uid')}
    for uid in list(waiting_queue.keys()):
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
        return result.get("short_url")
    except Exception as e:
        logger.error(f"Payment link error: {e}"); return None

async def make_payment_link(uid: int, name: str, price: int, days: int):
    return await asyncio.to_thread(make_link_sync, uid, name, price, days)

async def safe_delete(chat_id: int, message_id: int):
    if message_id:
        try: await bot.delete_message(chat_id, message_id)
        except: pass

def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="FIND MATCHES", callback_data='do_match')],
        [InlineKeyboardButton(text="EDIT PROFILE", callback_data='edit_profile')],
        [InlineKeyboardButton(text="PREMIUM", callback_data='see_premium')],
    ])

def reengage_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="FIND NEW MATCH", callback_data='do_match'),
         InlineKeyboardButton(text="MY PROFILE", callback_data='back_to_profile')],
        [InlineKeyboardButton(text="PREMIUM", callback_data='see_premium')],
    ])

def edit_profile_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="EDIT NAME", callback_data='edit_name'),
         InlineKeyboardButton(text="EDIT BIO", callback_data='edit_bio')],
        [InlineKeyboardButton(text="EDIT DOB", callback_data='edit_dob'),
         InlineKeyboardButton(text="EDIT GENDER", callback_data='edit_gender_preferred')],
        [InlineKeyboardButton(text="EDIT LOCATION", callback_data='edit_location')],
        [InlineKeyboardButton(text="CHANGE PHOTO", callback_data='edit_photo')],
        [InlineKeyboardButton(text="BACK", callback_data='back_to_profile')],
    ])

def profile_text(p: dict) -> str:
    vb = " VERIFIED" if p.get('verified') else ""
    ph = "HAS PHOTO" if p.get('photo') else "NO PHOTO"
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
    bio = p.get('bio') or 'NONE'
    age_str = ""
    dob_raw = p.get('dob')
    if dob_raw:
        dob = parse_dob(dob_raw)
        if dob:
            age_str = f" | Age: {calc_age(dob)}"
    return (f"YOUR PROFILE:\nName: {p.get('name','?')}\nGender: {p.get('gender','?')} | Interested: {p.get('preferred_gender','?')}{age_str}"
            f"\nBio: {bio}\nLocation: {loc}\n{ph}{vb}")



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
        try:
            r = await message.answer(".", reply_markup=ReplyKeyboardRemove())
            await safe_delete(message.chat.id, r.message_id)
        except:
            pass
        p = user_profiles[uid]
        await message.answer(
            profile_text(p) + f"\n\n{quota_summary(uid)}",
            parse_mode='HTML', reply_markup=main_kb()
        )
        return
    await state.set_state(Signup.name)
    await state.update_data(last_bot_msg=None, prev_bot_msg=None)
    msg = await message.answer(
        "\u1f44b Hey! I'm <b>Winkly</b>. I'll help you find people nearby.\n\n"
        "Let's set up your profile — it only takes ~30 seconds.\n\n"
        "<b>Step 1 of 7</b>\n\n"
        "\U0001f464 <b>What's your name?</b>",
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
    await state.set_state(Signup.gender)
    msg = await message.answer(
        "<b>Step 2 of 7</b>\n\n"
        "\u2696\ufe0f <b>What's your gender?</b>",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👨 Male", callback_data="signup_gender:Male")],
            [InlineKeyboardButton(text="👩 Female", callback_data="signup_gender:Female")],
            [InlineKeyboardButton(text="⚕ Other", callback_data="signup_gender:Other")],
        ])
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
        "<b>Step 3 of 7</b>\n\n"
        "\U0001f49d <b>Who are you interested in?</b>",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👨 Male", callback_data="pref:Male")],
            [InlineKeyboardButton(text="👩 Female", callback_data="pref:Female")],
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
    d = await state.get_data()
    if d.get('prev_bot_msg'):
        await safe_delete(message.chat.id, d['prev_bot_msg'])
    await state.set_state(Signup.location)
    msg = await message.answer(
        "<b>Step 4 of 7</b>\n\n"
        "\U0001f4cd <b>Share your location</b> or type a place name:",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📍 Share My Location", callback_data="loc_share_gps")],
            [InlineKeyboardButton(text="⌨️  Enter Place Name", callback_data="loc_enter_text")],
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
    loc = message.location

    if d.get('is_editing'):
        prof = user_profiles.get(uid, {})
        prof.update({'lat': str(loc.latitude), 'lon': str(loc.longitude), 'location_name': 'GPS'})
        user_profiles[uid] = prof
        await save_all()
        await state.clear()
        await message.answer("✅ Location updated!")
        return

    await state.update_data(lat=str(loc.latitude), lon=str(loc.longitude), location_name='GPS')
    await state.set_state(Signup.photo)
    msg = await message.answer(
        "📸 <b>Add a photo</b> (optional) — send one now or tap Skip:",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Skip", callback_data="signup_skip_photo")],
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
    await state.set_state(Signup.photo)
    msg = await message.answer(
        "📸 <b>Add a photo</b> (optional) — send one now or tap Skip:",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Skip", callback_data="signup_skip_photo")],
        ])
    )
    await state.update_data(prev_bot_msg=msg.message_id)





@dp.message(StateFilter(Signup.photo))
async def h_photo(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    await mark_online(uid)
    d = await state.get_data()
    if d.get('prev_bot_msg'):
        await safe_delete(message.chat.id, d['prev_bot_msg'])

    if message.text and 'skip' in message.text.lower():
        await state.set_state(Signup.bio)
        msg = await message.answer(
            "📝 <b>Tell us about yourself</b> (optional)\n\nWrite a short bio or tap Skip.",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⏭ Skip", callback_data="signup_skip_bio")],
            ])
        )
        await state.update_data(prev_bot_msg=msg.message_id)
        return

    if not message.photo:
        await message.answer("📸 Send a photo or tap Skip.")
        return

    photo_id = message.photo[-1].file_id
    await state.update_data(photo=photo_id)
    await state.set_state(Signup.bio)
    msg = await message.answer(
        "📝 <b>Tell us about yourself</b> (optional)\n\nWrite a short bio or tap Skip.",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Skip", callback_data="signup_skip_bio")],
        ])
    )
    await state.update_data(prev_bot_msg=msg.message_id)


async def finish_signup(state: FSMContext, chat_id: int, uid: int):
    data = await state.get_data()
    try:
        r = await bot.send_message(chat_id, ".", reply_markup=ReplyKeyboardRemove())
        await safe_delete(chat_id, r.message_id)
    except:
        pass
    prof = {
        'name': data.get('name', ''),
        'gender': data.get('gender', ''),
        'preferred_gender': data.get('preferred', ''),
        'bio': data.get('bio', ''),
        'dob': data.get('dob', ''),
        'lat': data.get('lat', ''),
        'lon': data.get('lon', ''),
        'location_name': data.get('location_name', ''),
        'photo': data.get('photo'),
        'verified': False,
        'verification_status': 'none',
        'username': data.get('username', ''),
        'free_texts': FREE_TEXTS_JOINING,
        'rejected': [],
    }
    user_profiles[uid] = prof
    await save_all()
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
        parse_mode='HTML', reply_markup=main_kb()
    )

@dp.message(StateFilter(Signup.bio))
async def h_bio(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    await mark_online(uid)
    d = await state.get_data()
    if d.get('prev_bot_msg'):
        await safe_delete(message.chat.id, d['prev_bot_msg'])

    if message.text and 'skip' in message.text.lower():
        await state.set_state(Signup.dob)
        msg = await message.answer(
            "\U0001f4cc <b>When were you born?</b> (optional)\n\nEnter your date of birth in any format, e.g. 15-08-1998, 1998/08/15, or August 15 1998:",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⏭ Skip", callback_data="signup_skip_dob")],
            ])
        )
        await state.update_data(prev_bot_msg=msg.message_id)
        return

    bio = (message.text or '').strip()
    if bio:
        if len(bio) > 300:
            bio = bio[:300]
        await state.update_data(bio=bio)
    await state.set_state(Signup.dob)
    msg = await message.answer(
        "\U0001f4cc <b>When were you born?</b> (optional)\n\nEnter your date of birth in any format, e.g. 15-08-1998, 1998/08/15, or August 15 1998:",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Skip", callback_data="signup_skip_dob")],
        ])
    )
    await state.update_data(prev_bot_msg=msg.message_id)


@dp.message(StateFilter(Signup.dob))
async def h_dob(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    await mark_online(uid)
    d = await state.get_data()
    if d.get('prev_bot_msg'):
        await safe_delete(message.chat.id, d['prev_bot_msg'])

    raw = (message.text or '').strip()
    if not raw or 'skip' in raw.lower():
        await finish_signup(state, message.chat.id, uid)
        return

    dob = parse_dob(raw)
    if dob is None:
        await message.answer(
            "\u26a0\ufe0f <b>Couldn't understand that date.</b>\n\n"
            "Try: 15-08-1998  |  1998/08/15  |  August 15 1998",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⏭ Skip", callback_data="signup_skip_dob")],
            ])
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
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⏭ Skip", callback_data="signup_skip_dob")],
            ])
        )
        return

    await state.update_data(dob=raw)
    await finish_signup(state, message.chat.id, uid)


@dp.message(lambda m: m.photo and m.from_user.id in user_profiles, StateFilter(None))
async def h_profile_photo(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    await mark_online(uid)
    if uid in _verify_pending:
        await h_verify_photo(message)
        return
    user_profiles[uid]['photo'] = message.photo[-1].file_id
    await save_all()
    await message.answer("\u2705 Profile photo updated!", reply_markup=main_kb())

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
        parse_mode='HTML', reply_markup=main_kb()
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
                f"\U0001f51a <b>Chat ended.</b>\n\n{user_profiles[uid]['name']} left the chat.",
                parse_mode='HTML'
            )
        except:
            pass
    await message.answer("\U0001f51a <b>Chat ended.</b>\n\nWhat would you like to do next?",
                         parse_mode='HTML', reply_markup=reengage_kb())

# ─── /verify ────────────────────────────────────────────────────────────────

@dp.message(Command('verify'))
async def cmd_verify(message: types.Message):
    uid = message.from_user.id
    await mark_online(uid)
    if uid not in user_profiles:
        await message.answer("📝 Please set up your profile first with /start.")
        return
    p = user_profiles[uid]
    if p.get('verified'):
        await message.answer(
            "\U0001f3c5 <b>Already Verified!</b>\n\n"
            "\u2705 You have unlimited free access to chat.\n\nGo find your match! \u2764\ufe0f",
            parse_mode='HTML', reply_markup=main_kb()
        )
        return
    st = p.get('verification_status', 'none')
    if st == 'pending':
        await message.answer(
            "\u23f3 <b>Verification Under Review</b>\n\n"
            "Our team is checking your photos. You'll be notified once approved.",
            parse_mode='HTML'
        )
        return
    if st == 'rejected':
        await message.answer(
            "\u274c <b>Verification Not Approved</b>\n\n"
            "The selfie didn't match your profile photo.\n\nPlease try again with clearer photos.",
            parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="\U0001f4f7 Retry Verification", callback_data='verify_start')],
                [InlineKeyboardButton(text="\U0001f464 Back to Profile", callback_data='back_to_profile')],
            ])
        )
        return
    await message.answer(
        "\U0001f3c5 <b>Get Verified</b>\n\n"
        "\U0001f4f7 Upload a full-body photo + selfie to verify.\n"
        "Female users get <b>unlimited free access</b> after verification!",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="\U0001f4f7 Start Verification", callback_data='verify_start')],
        ])
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
                parse_mode='HTML', reply_markup=main_kb()
            )
        except:
            await message.answer("\U0001f3c6 <b>Premium Active!</b>\n\nUnlimited access!",
                                 parse_mode='HTML', reply_markup=main_kb())
        return
    await message.answer(
        f"\U0001f3c6 <b>Premium Plans</b>\n\n{quota_summary(uid)}\n\nChoose a plan:",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="\U0001f3c6 1 Day — Rs49", callback_data='premium_1day')],
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
        f"Send them this link: t.me/winklybot?start={code}\n\n"
        f"Or share your code: <code>{code}</code>\n\n"
        f"3 signups = 1 free day! \U0001f389",
        parse_mode='HTML'
    )
    await cb.answer()

# ─── Verification callbacks ────────────────────────────────────────────────────

@dp.callback_query(lambda cb: cb.data == 'verify_start')
async def verify_start(cb: types.CallbackQuery):
    uid = cb.from_user.id
    await mark_online(uid)
    if uid not in user_profiles:
        await cb.message.edit_text("📝 Please set up your profile first with /start.")
        await cb.answer()
        return
    if user_profiles[uid].get('verified'):
        await cb.message.edit_text("\u2705 You're already verified!")
        await cb.answer()
        return
    _verify_pending[uid] = 'awaiting_full_body'
    await cb.message.edit_text(
        "\U0001f4f7 <b>Verification — Step 1 of 2</b>\n\n"
        "Send a <b>full-body photo</b> of yourself. This will be your profile picture.\n\n"
        "_Make sure your full body is visible and lighting is good._",
        parse_mode='HTML'
    )
    await cb.answer()

async def h_verify_photo(message: types.Message):
    uid = message.from_user.id
    step = _verify_pending.get(uid)
    if not step:
        return
    if not message.photo:
        if step == 'awaiting_full_body':
            await message.answer(
                "\u26a0\ufe0f <b>Please send your full-body photo.</b>\n\n"
                "This will be your profile picture. Make sure your full body is visible.",
                parse_mode='HTML'
            )
        elif step == 'awaiting_selfie':
            await message.answer(
                "\u26a0\ufe0f <b>Please send a selfie.</b>\n\n"
                "Take a photo looking straight at the camera. This is only visible to our admin team.",
                parse_mode='HTML'
            )
        return
    fid = message.photo[-1].file_id
    if step == 'awaiting_full_body':
        user_profiles[uid]['photo'] = fid
        _verify_pending[uid] = 'awaiting_selfie'
        await message.answer(
            "\u2705 Great photo!\n\n"
            "\U0001f4f7 <b>Step 2 of 2: Verification Selfie</b>\n\n"
            "Now send a <b>selfie</b> looking straight at the camera. "
            "This is only visible to our admin team.\n\n"
            "_Keep your face clearly visible._",
            parse_mode='HTML'
        )
    elif step == 'awaiting_selfie':
        user_profiles[uid]['selfie'] = fid
        user_profiles[uid]['verification_status'] = 'pending'
        del _verify_pending[uid]
        await save_all()
        await send_verification_to_admin(uid)
        await message.answer(
            "\u2705 <b>Photos received!</b>\n\n"
            "Our team will review your verification shortly. "
            "You'll be notified once approved.\n\nThank you for your patience! \U0001f389",
            parse_mode='HTML'
        )

async def send_verification_to_admin(uid: int):
    if not ADMIN_CHAT_ID:
        return
    p = user_profiles.get(uid, {})
    n = p.get('name', '?')
    g = p.get('gender', '?')
    b = (p.get('bio') or '—')[:80]
    un = f"@{p.get('username', '—')}" if p.get('username') else '—'
    ph = p.get('photo')
    sf = p.get('selfie')
    txt = (
        f"\U0001f4f7 <b>Verification Request</b>\n\n"
        f"\U0001f464 {n} ({g})\n"
        f"ID: <code>{uid}</code>\n"
        f"\U0001f310 {un}\n"
        f"\U0001f4dd {b}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\u2705 Approve", callback_data=f'admin_approve:{uid}'),
         InlineKeyboardButton(text="\u274c Reject", callback_data=f'admin_reject:{uid}')],
    ])
    try:
        if ph:
            await bot.send_photo(ADMIN_CHAT_ID, ph, caption=txt, parse_mode='HTML', reply_markup=kb)
        else:
            await bot.send_message(ADMIN_CHAT_ID, txt + "\n_(No profile photo)_", parse_mode='HTML', reply_markup=kb)
    except:
        pass
    if sf:
        try:
            await bot.send_photo(ADMIN_CHAT_ID, sf, caption="\U0001f4f7 Selfie (for comparison)")
        except:
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
    p.pop('selfie', None)
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
            "\u2705 Your profile has been approved. You now have <b>unlimited free access</b> to chat!\n\n"
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
    p.pop('selfie', None)
    await save_all()
    try:
        txt = cb.message.caption or cb.message.text
        await cb.message.edit_caption(caption=txt + "\n\n\u274c <b>Rejected</b>", parse_mode='HTML')
    except:
        pass
    try:
        await bot.send_message(
            uid,
            "\u274c <b>Verification Not Approved</b>\n\n"
            "The selfie didn't match your profile photo.\n\n"
            "You can retry anytime with clearer photos.",
            parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="\U0001f4f7 Retry Verification", callback_data='verify_start')],
            ])
        )
    except:
        pass
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
    m = find_queue_match(me)
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
    waiting_queue[uid] = {'added_at': datetime.now().isoformat()}
    online = await get_online_count()
    ql = len(waiting_queue)
    sm = await cb.message.edit_text(
        f"\U0001f465 <b>{online} people online</b> | \u23f3 <b>{ql} in queue</b>\n\n"
        f"\U0001f464 <b>{me['name']}</b>, searching for someone compatible...\n\n"
        "Looking for someone who matches your preferences.\n"
        "This usually takes 5-30 seconds.\n\n"
        "\u23f3 <b>Waiting in queue...</b>\n\n"
        "_You'll be notified when a match is found._",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="\U0001f504 Cancel Search", callback_data='cancel_queue')],
        ])
    )
    _queue_msg_ids[uid] = sm.message_id
    await save_all()
    await cb.answer()

async def send_match_card(cid: int, partner: dict, pid: int):
    n = partner.get('name', '?')
    g = partner.get('gender', '?')
    b = (partner.get('bio') or '—')[:100]
    vb = " \u2705" if partner.get('verified') else ""
    ph = partner.get('photo')
    txt = (
        f"\U0001f389 <b>It's a Match!</b>\n\n"
        f"We found you a match with <b>{n}</b>!\n\n"
        f"\U0001f464 {n}{vb}\n"
        f"\u2696\ufe0f {g}\n"
        f"\U0001f4dd {b}\n\n"
        "Tap below to start chatting or skip:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\U0001f4ac  Chat Now", callback_data=f'chat:{pid}')],
        [InlineKeyboardButton(text="\U0001f51b Skip", callback_data=f'skip_match:{pid}')],
    ])
    if ph:
        try:
            await bot.send_photo(cid, ph, caption=txt, parse_mode='HTML', reply_markup=kb)
            return
        except:
            pass
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
        "\u274c <b>Skipped.</b> You won't be matched with this person again.\n\n"
        "What would you like to do next?",
        parse_mode='HTML', reply_markup=reengage_kb()
    )
    await cb.answer()

@dp.callback_query(lambda cb: cb.data.startswith('chat:'))
async def start_chat(cb: types.CallbackQuery):
    uid = cb.from_user.id
    await mark_online(uid)
    pid = int(cb.data.split(':')[1])
    if uid not in active_matches or pid not in active_matches.get(uid, {}):
        await cb.answer("⚠️ You are not matched with this user.", show_alert=True)
        return
    if not check_text_quota(uid):
        await cb.answer("⚠️ No free texts remaining. Upgrade to continue.", show_alert=True)
        return
    current_chat[uid] = pid
    current_chat[pid] = uid
    await save_all()
    try:
        r = await bot.send_message(uid, ".", reply_markup=ReplyKeyboardRemove())
        await safe_delete(uid, r.message_id)
    except:
        pass
    pname = user_profiles.get(pid, {}).get('name', 'Someone')
    # Delete old match card (may be a photo — edit_text fails on media messages)
    await safe_delete(uid, cb.message.message_id)
    # Send chat interface as a fresh message
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
    await bot.send_message(
        pid,
        f"\U0001f4ac <b>{user_profiles[uid]['name']}</b> started chatting!\n\nSay hi! \U0001f44b",
        parse_mode='HTML'
    )
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
        # End the chat — no quota
        partner = current_chat.pop(uid, None)
        if partner:
            current_chat.pop(partner, None)
            await save_all()
            try:
                await bot.send_message(
                    partner,
                    f"\U0001f51a <b>Chat ended.</b>\n\n{user_profiles[uid]['name']} has used all their free texts.",
                    parse_mode='HTML'
                )
            except:
                pass
        await cb.message.edit_text(
            "⚠️ <b>You've used all your free texts.</b>\n\n"
            "📸 Verify or upgrade for unlimited access.",
            parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📸 Verify Now", callback_data='verify_start')],
                [InlineKeyboardButton(text="\U0001f3c6 See Premium", callback_data='see_premium')],
            ])
        )
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
        parse_mode='HTML', reply_markup=reengage_kb()
    )
    await save_all()
    await cb.answer()

@dp.callback_query(lambda cb: cb.data == 'end_chat')
async def end_chat(cb: types.CallbackQuery):
    uid = cb.from_user.id
    await mark_online(uid)
    partner = current_chat.pop(uid, None)
    if partner:
        current_chat.pop(partner, None)
    if partner and partner in user_profiles:
        try:
            await bot.send_message(
                partner,
                f"\U0001f51a <b>Chat ended.</b>\n\n{user_profiles[uid]['name']} left the chat.",
                parse_mode='HTML'
            )
        except:
            pass
    await cb.message.edit_text(
        "\U0001f51a <b>Chat ended.</b>\n\nWhat would you like to do next?",
        parse_mode='HTML', reply_markup=reengage_kb()
    )
    await save_all()
    await cb.answer()

# ─── Relay messages ───────────────────────────────────────────────────────────

@dp.message(StateFilter(None))
async def relay(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    await mark_online(uid)
    if uid not in user_profiles:
        return
    if uid in _verify_pending:
        await h_verify_photo(message)
        return
    if uid not in current_chat:
        return
    pid = current_chat[uid]
    if not check_text_quota(uid):
        # End the active chat — user can no longer send OR receive
        partner = current_chat.pop(uid, None)
        if partner:
            current_chat.pop(partner, None)
            await save_all()
            try:
                await bot.send_message(
                    partner,
                    f"\U0001f51a <b>Chat ended.</b>\n\n{user_profiles[uid]['name']} has used all their free texts. "
                    "The chat has ended.\n\nFind a new match or upgrade for unlimited texts!",
                    parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="\U0001f4ac Find New Match", callback_data='do_match')],
                    ])
                )
            except:
                pass
        p = user_profiles[uid]
        if p.get('gender') in ('Male', 'Female', 'Other') and not p.get('verified'):
            await message.answer(
                "⚠️ <b>You've used all your free texts.</b>\n\n"
                "📸 Verify your profile for free unlimited access.",
                parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📸 Verify Now", callback_data='verify_start')],
                ])
            )
        else:
            await message.answer(
                f"⚠️ <b>You've used all your free texts.</b>\n\n{quota_summary(uid)}\n\n"
                "\U0001f3c6 Upgrade to continue:",
                parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="\U0001f3c6 1 Day — Rs49", callback_data='premium_1day')],
                    [InlineKeyboardButton(text="\U0001f4cb See All Plans", callback_data='premium_plans')],
                ])
            )
        return
    # Receiver can always receive messages — no quota check
    try:
        await bot.copy_message(pid, message.chat.id, message.message_id)
        consume_text(uid)
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
                                    parse_mode='HTML', reply_markup=main_kb())
        await cb.answer()
        return
    await cb.message.edit_text("\u23f3 Creating payment link...")
    url = await make_payment_link(uid, "1 Day", 49, 1)
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
        "\U0001f3c6 <b>1 Day Premium — Rs49</b>\n\n"
        "\u2705 Unlimited texts and matches for 24 hours\n\n"
        "Tap below to complete payment:",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="\U0001f4b3 Pay Rs49", url=url)],
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
    rows.append([InlineKeyboardButton(text="\U0001f3c6 1 Day — Rs49", callback_data='premium_1day')])
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
        await cb.message.edit_text("\U0001f3c6 <b>Already Premium!</b>", parse_mode='HTML', reply_markup=main_kb())
    else:
        await cb.message.edit_text(
            f"\U0001f3c6 <b>Premium Plans</b>\n\n{quota_summary(uid)}\n\nChoose a plan:",
            parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="\U0001f3c6 1 Day — Rs49", callback_data='premium_1day')],
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
        parse_mode='HTML', reply_markup=main_kb()
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

@dp.callback_query(lambda cb: cb.data == 'edit_gender_preferred')
async def edit_gp(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await mark_online(uid)
    if await _guard_edit(state):
        await cb.answer("⚠️ Finish signup first!", show_alert=True)
        return
    await state.set_state(EditProfile.gender)
    await cb.message.edit_text(
        "\u270f\ufe0f <b>Edit Gender/Interested</b>\n\n"
        "⚖️ <b>What's your gender?</b>",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👨 Male", callback_data="edit_gender:Male")],
            [InlineKeyboardButton(text="👩 Female", callback_data="edit_gender:Female")],
            [InlineKeyboardButton(text="⚕ Other", callback_data="edit_gender:Other")],
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

@dp.callback_query(lambda cb: cb.data == 'edit_dob')
async def edit_dob(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await mark_online(uid)
    if await _guard_edit(state):
        await cb.answer("⚠️ Finish signup first!", show_alert=True)
        return
    current_dob = user_profiles.get(uid, {}).get('dob', '')
    await state.set_state(EditProfile.dob)
    await cb.message.edit_text(
        f"\u270f\ufe0f <b>Edit Date of Birth</b>\n\nCurrent: {current_dob or 'Not set'}\n\n"
        "Enter your date of birth in any format (e.g. 15-08-1998):",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="\u00ab  Back", callback_data='edit_profile')],
        ])
    )
    await cb.answer()

@dp.callback_query(lambda cb: cb.data == 'edit_photo')
async def edit_ph(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await mark_online(uid)
    if await _guard_edit(state):
        await cb.answer("⚠️ Finish signup first!", show_alert=True)
        return
    await state.set_state(EditProfile.photo)
    await cb.message.edit_text(
        "\u270f\ufe0f <b>Change Photo</b>\n\nSend a new profile photo:",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="\u00ab  Back", callback_data='edit_profile')],
        ])
    )
    await cb.answer()

# === Signup inline button callbacks ===

@dp.callback_query(lambda cb: cb.data.startswith('signup_gender:'))
async def cb_signup_gender(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await mark_online(uid)
    gender = cb.data.split(':', 1)[1]
    await state.update_data(gender=gender)
    await state.set_state(Signup.preferred)
    await cb.message.edit_text(
        "<b>Step 3 of 7</b>\n\n"
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
    await state.update_data(preferred=preferred)
    await state.set_state(Signup.location)
    await cb.message.edit_text(
        "<b>Step 4 of 7</b>\n\n"
        "\U0001f4cd <b>Share your location</b> or type a place name:",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📍 Share My Location", callback_data="loc_share_gps")],
            [InlineKeyboardButton(text="⌨️  Enter Place Name", callback_data="loc_enter_text")],
        ])
    )
    await state.update_data(prev_bot_msg=cb.message.message_id)
    await cb.answer()


@dp.callback_query(lambda cb: cb.data == 'loc_share_gps')
async def cb_loc_share_gps(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await mark_online(uid)
    await state.set_state(Signup.location)
    await cb.message.edit_text(
        "\U0001f4cd <b>Share your location</b> - tap the paper clip icon -> Location button:",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⌨️  Enter Place Name Instead", callback_data="loc_enter_text")],
        ])
    )
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


@dp.callback_query(lambda cb: cb.data == 'signup_skip_photo')
async def cb_skip_photo(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await mark_online(uid)
    await state.set_state(Signup.bio)
    await cb.message.edit_text(
        "📝 <b>Tell us about yourself</b> (optional)\n\nWrite a short bio or tap Skip.",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Skip", callback_data="signup_skip_bio")],
        ])
    )
    await cb.answer()


@dp.callback_query(lambda cb: cb.data == 'signup_skip_bio')
async def cb_skip_bio(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await mark_online(uid)
    d = await state.get_data()
    if d.get('prev_bot_msg'):
        await safe_delete(cb.message.chat.id, d['prev_bot_msg'])
    await state.update_data(bio='')
    await state.set_state(Signup.dob)
    await cb.message.edit_text(
        "\U0001f4cc <b>When were you born?</b> (optional)\n\nEnter your date of birth in any format, e.g. 15-08-1998, 1998/08/15, or August 15 1998:",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Skip", callback_data="signup_skip_dob")],
        ])
    )
    await cb.answer()


@dp.callback_query(lambda cb: cb.data == 'signup_skip_dob')
async def cb_skip_dob(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await mark_online(uid)
    d = await state.get_data()
    if d.get('prev_bot_msg'):
        await safe_delete(cb.message.chat.id, d['prev_bot_msg'])
    await state.update_data(dob='')
    await finish_signup(state, cb.message.chat.id, uid)
    await cb.answer()


# === Edit profile inline button callbacks ===

@dp.callback_query(lambda cb: cb.data.startswith('edit_gender:'))
async def cb_edit_gender(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await mark_online(uid)
    if await _guard_edit(state):
        await cb.answer("⚠️ Finish signup first!", show_alert=True)
        return
    gender = cb.data.split(':', 1)[1]
    await state.update_data(gender=gender)
    await state.set_state(EditProfile.preferred)
    await cb.message.edit_text(
        "\U0001f49d <b>Who are you interested in?</b>",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👨 Male", callback_data="edit_pref:Male")],
            [InlineKeyboardButton(text="👩 Female", callback_data="edit_pref:Female")],
            [InlineKeyboardButton(text="👥 Everyone", callback_data="edit_pref:Everyone")],
            [InlineKeyboardButton(text="\u00ab  Back", callback_data='edit_profile')],
        ])
    )
    await cb.answer()


@dp.callback_query(lambda cb: cb.data.startswith('edit_pref:'))
async def cb_edit_pref(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await mark_online(uid)
    if await _guard_edit(state):
        await cb.answer("⚠️ Finish signup first!", show_alert=True)
        return
    preferred = cb.data.split(':', 1)[1]
    d = await state.get_data()
    user_profiles[uid]['gender'] = d.get('gender', user_profiles[uid].get('gender'))
    user_profiles[uid]['preferred_gender'] = preferred
    await save_all()
    await state.clear()
    await cb.message.edit_text(
        "\u2705 Gender preferences updated!",
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
        await cb.answer("⚠️ Finish signup first!", show_alert=True)
        return
    await state.set_state(EditProfile.location)
    await cb.message.edit_text(
        "\U0001f4cd <b>Share your location</b> - tap the paper clip icon -> Location button:",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⌨️  Enter Place Name Instead", callback_data="loc_enter_text_edit")],
            [InlineKeyboardButton(text="\u00ab  Back", callback_data='edit_profile')],
        ])
    )
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

@dp.message(StateFilter(EditProfile.gender))
async def edit_gender_h(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    await mark_online(uid)
    raw = message.text.strip()
    nfd = unicodedata.normalize('NFD', raw.lower())
    keyword = ' '.join(re.findall(r'[a-z]+', ''.join(c for c in nfd if unicodedata.category(c) != 'Mn' and ord(c) != 0x200d)))
    GENDER_KW = {'male': 'Male', 'm': 'Male', 'female': 'Female', 'women': 'Female', 'f': 'Female', 'other': 'Other'}
    if keyword not in GENDER_KW:
        await message.answer("\u26a0\ufe0f Please tap a button above.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👨 Male", callback_data="edit_gender:Male")],
            [InlineKeyboardButton(text="👩 Female", callback_data="edit_gender:Female")],
            [InlineKeyboardButton(text="⚕ Other", callback_data="edit_gender:Other")],
        ]))
        return
    await state.update_data(gender=GENDER_KW[keyword])
    await state.set_state(EditProfile.preferred)
    await message.answer("\U0001f49d <b>Who are you interested in?</b>", parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👨 Male", callback_data="edit_pref:Male")],
        [InlineKeyboardButton(text="👩 Female", callback_data="edit_pref:Female")],
        [InlineKeyboardButton(text="👥 Everyone", callback_data="edit_pref:Everyone")],
    ]))


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
    d = await state.get_data()
    user_profiles[uid]['gender'] = d.get('gender', user_profiles[uid].get('gender'))
    user_profiles[uid]['preferred_gender'] = PREF_KW[keyword]
    await save_all()
    await state.clear()
    await message.answer("\u2705 Gender preferences updated!", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
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


@dp.message(StateFilter(EditProfile.photo))
async def edit_photo_h(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    await mark_online(uid)
    if not message.photo:
        await message.answer("📸 Send a photo.")
        return
    user_profiles[uid]['photo'] = message.photo[-1].file_id
    await save_all()
    await state.clear()
    await message.answer("✅ Photo updated!", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 View Profile", callback_data='back_to_profile')],
    ]))


@dp.message(StateFilter(EditProfile.dob))
async def h_edit_dob(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    await mark_online(uid)
    raw = (message.text or '').strip()
    if not raw:
        await state.clear()
        await message.answer("\u2705 DOB cleared.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👤 View Profile", callback_data='back_to_profile')],
        ]))
        return
    dob = parse_dob(raw)
    if dob is None:
        await message.answer(
            "\u26a0\ufe0f <b>Couldn't understand that date.</b>\n\nTry: 15-08-1998  |  1998/08/15  |  August 15 1998",
            parse_mode='HTML'
        )
        return
    age = calc_age(dob)
    if age < 18:
        await message.answer("\u26d4\ufe0f <b>You must be 18+ to use this bot.</b>", parse_mode='HTML')
        return
    if age > 100:
        await message.answer("\u26a0\ufe0f <b>Please enter a valid birth year.</b>", parse_mode='HTML')
        return
    user_profiles[uid]['dob'] = raw
    await save_all()
    await state.clear()
    await message.answer(f"✅ DOB updated! Age: {age}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 View Profile", callback_data='back_to_profile')],
    ]))


# ─── Background Tasks ────────────────────────────────────────────────────────

QUEUE_TIMEOUT = 240  # 4 minutes

async def update_counters_loop():
    while True:
        await asyncio.sleep(8)
        try:
            online = await get_online_count()
            ql = len(waiting_queue)
            status_text = f"\U0001f465 {online} online | \u23f3 {ql} in queue\n\nSearching for your match..."
            for uid, mid in list(_queue_msg_ids.items()):
                if uid in user_profiles:
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
                # Timeout check
                added_str = waiting_queue[uid].get('added_at', '')
                if added_str:
                    try:
                        added = datetime.fromisoformat(added_str)
                        if (now - added).total_seconds() > QUEUE_TIMEOUT:
                            if uid in _queue_msg_ids:
                                try:
                                    await bot.edit_message_text(
                                        "\u23f9 <b>Queue timeout.</b>\n\nNo matches found this time. Try again!",
                                        uid, _queue_msg_ids[uid], parse_mode='HTML'
                                    )
                                except:
                                    pass
                                del _queue_msg_ids[uid]
                            remove.append(uid)
                            continue
                    except:
                        pass
                m = find_queue_match(me)
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
        body = await request.text()
        expected = hmac.new(RAZORPAY_WEBHOOK_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
        if sig != expected:
            logger.warning(f"Webhook invalid sig: {sig[:20]}...")
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
                await bot.send_message(
                    uid,
                    f"\U0001f389 <b>Payment Successful!</b>\n\n"
                    f"Your Winkly premium is now active for {dur} day{'s' if dur > 1 else ''}.\n\n"
                    f"\u2705 Unlimited texts and matches!",
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
    return web.Response(
        content_type='text/html',
        text='<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Payment Successful - Winkly</title><style>body{font-family:sans-serif;background:#1a1a2e;color:#eee;text-align:center;padding:40px 20px}.card{background:#16213e;border-radius:16px;padding:32px;max-width:400px;margin:0 auto}.check{font-size:64px;margin-bottom:16px}.btn{background:#e94560;color:#fff;border:none;border-radius:8px;padding:14px 28px;font-size:16px;cursor:pointer;text-decoration:none;display:inline-block;margin-top:16px}</style></head><body><div class="card"><div class="check">&#9989;</div><h1>Payment Successful!</h1><p>Your Winkly premium subscription is now active.</p><p>Return to Telegram to start matching.</p><a class="btn" href="https://t.me/winklybot">Open Telegram</a></div></body></html>'
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

# ─── Startup ───────────────────────────────────────────────────────────────

async def on_startup(dispatcher: Dispatcher):
    logger.info("Starting Winkly Bot v2...")
    # Menu button (left of emoji bar) showing bot commands
    commands = [
        BotCommand(command="start", description="Start or restart the bot"),
        BotCommand(command="profile", description="View your profile"),
        BotCommand(command="find", description="Find matches"),
        BotCommand(command="stop", description="End current chat"),
        BotCommand(command="verify", description="Get verified for unlimited access"),
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
        app.router.add_post('/razorpay/webhook', handle_razorpay_webhook)
        app.router.add_get('/payment/success', payment_success_page)
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
