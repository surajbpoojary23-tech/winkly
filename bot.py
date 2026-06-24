import os
import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import BotCommand
from aiogram.filters.state import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
import razorpay
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '.env'))
from aiohttp import web

BOT_TOKEN = os.getenv('BOT_TOKEN') or "8624196108:***"
REDIS_URL  = os.getenv('REDIS_URL')
WEBHOOK_URL = os.getenv('WEBHOOK_URL') or 'https://winkly-kmsz.onrender.com'

# Razorpay configuration
RAZORPAY_KEY_ID = os.getenv('RAZORPAY_KEY_ID', 'rzp_live_T5RFsK3b9AYBTX')
RAZORPAY_KEY_SECRET = os.getenv('RAZORPAY_KEY_SECRET', 'MBAphgobB9XnZ33SylDA9r7C')
RAZORPAY_WEBHOOK_SECRET = os.getenv('RAZORPAY_WEBHOOK_SECRET', 'rzp_webhook_secret_here')
ADMIN_CHAT_ID = int(os.getenv('ADMIN_CHAT_ID', '0'))

# Initialize Razorpay client
try:
    razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
except Exception as e:
    print(f"⚠️ Failed to initialize Razorpay client: {e}")
    razorpay_client = None

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot=bot, storage=storage)


# ── FSM ───────────────────────────────────────────────────────────────────────

class Setup(StatesGroup):
    name              = State()
    age               = State()
    gender            = State()
    bio               = State()
    preferred_gender  = State()
    location          = State()
    confirm           = State()   # final review screen


# ── In-memory profile store ───────────────────────────────────────────────────
# {user_id: {"name":…, "age":…, "gender":…, "bio":…, "lat":…, "lon":…,
#            "photo":…, "selfie":…, "verified": bool, "verification_status": str,
#            "likes": set(), "rejected": set()}}
user_profiles: dict = {}

# ── Verification sessions ────────────────────────────────────────────────────
# Tracks multi-step verification photo uploads per user
# {user_id: "awaiting_photo" | "awaiting_selfie"}
_verify_sessions: dict[int, str] = {}

# ── Match & Chat stores ─────────────────────────────────────────────────────
# likes_sent: {liker_id: set(liked_ids)} — legacy, kept for compatibility
likes_sent: dict[int, set] = {}
# matches: {user_id: {partner_id: match_data}}
active_matches: dict[int, dict[int, dict]] = {}
# current_chat: {user_id: partner_id} — tracks who each user is chatting with
current_chat: dict[int, int] = {}

# ── Usage tracking for free vs premium ───────────────────────────────────────
# text_usage: {user_id: {'texts_sent': int, 'match_count': int, 'last_reset': timestamp}}
user_usage: dict[int, dict] = {}
# premium_subscriptions: {user_id: {'expiry_date': timestamp}}
premium_subscriptions: dict[int, dict] = {}

# ── Omegle-style waiting queue ───────────────────────────────────────────────
# users waiting for a match: {user_id: {'added_at': timestamp, 'message_id': int}}
waiting_queue: dict[int, dict] = {}
# background task references for waiting users
waiting_tasks: dict[int, asyncio.Task] = {}

PROGRESS_STEPS = ["name", "age", "gender", "bio", "preferred_gender", "location"]
TOTAL_STEPS = 6

STEP_LABELS = {
    "name":             "📛  Name",
    "age":              "🎂  DOB",
    "gender":           "⚧  Gender",
    "bio":              "📝  Bio",
    "preferred_gender": "❤️  Interested In",
    "location":         "📍  Location",
}


def progress_bar(current_step: int) -> str:
    """Returns ●●●○○ style bar, 0-indexed."""
    total = len(PROGRESS_STEPS)
    filled = "●" * current_step
    empty  = "○" * (total - current_step)
    return f"{filled}{empty}"


def step_index(state: str) -> int:
    for i, s in enumerate(PROGRESS_STEPS):
        if s in state.lower():
            return i
    return 0


def profile_summary(data: dict) -> str:
    verified_badge = "✅ Verified" if data.get('verified') else ""
    photo_status = "✅" if data.get('photo') else "❌"
    return (
        "✅ *Your Profile*\n\n"
        f"📛  Name:       {data.get('name', '—')}\n"
        f"🎂  Age:        {data.get('age', '—')}\n"
        f"⚧  Gender:     {data.get('gender', '—')}\n"
        f"📝  Bio:        {data.get('bio', '—') or '—'}\n"
        f"❤️  Interested: {data.get('preferred_gender', '—')}\n"
        f"📍  Location:   {_lat_lon(data)}\n"
        f"📷  Photo:      {photo_status}\n"
        + (f"🏅  Badge:      {verified_badge}\n" if verified_badge else "")
    )


def _clean(data: dict) -> dict:
    """Strip internal FSM fields before saving to user_profiles."""
    return {k: v for k, v in data.items() if k not in ('edit_mode', 'dob')}


def _lat_lon(data: dict) -> str:
    lat = data.get('lat')
    lon = data.get('lon')
    if lat and lon:
        return f"{float(lat):.4f}, {float(lon):.4f}"
    return "—"

import math
from datetime import datetime, timedelta

async def safe_delete_msg(chat_id: int, msg_id: int | None):
    if msg_id:
        try:
            await bot.delete_message(chat_id, msg_id)
        except:
            pass

# ── Usage tracking helpers ───────────────────────────────────────────────────

def is_premium_user(uid: int) -> bool:
    """Check if user has an active premium subscription."""
    if uid not in premium_subscriptions:
        return False
    expiry = premium_subscriptions[uid].get('expiry_date')
    if not expiry:
        return False
    return datetime.now() < expiry


def get_user_usage(uid: int) -> dict:
    """Get or initialize user usage data."""
    if uid not in user_usage:
        user_usage[uid] = {
            'texts_sent': 0,
            'match_count': 0,
            'last_reset': datetime.now(),
        }
    return user_usage[uid]


def is_verified_female(uid: int) -> bool:
    """Check if user is a verified female (gets unlimited free access)."""
    profile = user_profiles.get(uid, {})
    return profile.get('gender') in ('Women', 'Female') and profile.get('verified') is True


def check_text_limit(uid: int) -> bool:
    """Check if user has reached text limit (10 per match)."""
    if is_premium_user(uid) or is_verified_female(uid):
        return True
    
    usage = get_user_usage(uid)
    return usage['texts_sent'] < 10


def check_match_limit(uid: int) -> bool:
    """Check if user has reached match limit (10 total)."""
    if is_premium_user(uid) or is_verified_female(uid):
        return True
    
    usage = get_user_usage(uid)
    return usage['match_count'] < 10


def increment_text_count(uid: int):
    """Increment text count for user."""
    if not is_premium_user(uid):
        usage = get_user_usage(uid)
        usage['texts_sent'] += 1


def increment_match_count(uid: int):
    """Increment match count for user."""
    if not is_premium_user(uid):
        usage = get_user_usage(uid)
        usage['match_count'] += 1


def get_usage_summary(uid: int) -> str:
    """Get formatted usage summary for user."""
    if is_premium_user(uid):
        expiry = premium_subscriptions[uid].get('expiry_date')
        if expiry:
            days_left = (expiry - datetime.now()).days
            return f"💎 *Premium* - Unlimited texts and matches\nExpires in {days_left} days"
        return "💎 *Premium* - Unlimited texts and matches"
    
    usage = get_user_usage(uid)
    texts_left = max(0, 10 - usage['texts_sent'])
    matches_left = max(0, 10 - usage['match_count'])
    return f"📝 *Free* - {texts_left} texts left per match, {matches_left} matches left total\n💎 Upgrade from just Rs49/day — tap /premium"


# ── Gender normalisation ───────────────────────────────────────────────────────

GENDER_ALIASES = {
    'male':   'Men',
    'female': 'Women',
    'other':  'Other',
    'men':    'Men',
    'women':  'Women',
    'm':      'Men',
    'f':      'Women',
}


def _norm_gender(g: str) -> str:
    """Normalise any gender string to 'Men' / 'Women' / 'Other'."""
    return GENDER_ALIASES.get(g.lower().strip(), g)


# ── Matching helpers ───────────────────────────────────────────────────────────

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line distance in km between two lat/lon points."""
    R = 6371  # Earth radius in km
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def find_matches(me: dict, all_profiles: dict) -> list[dict]:
    """
    Find ALL mutually compatible profiles for 'me', sorted by distance (nearest first).
    No radius limit — we want to find anyone compatible worldwide.
    """
    me_lat = float(me['lat'])
    me_lon = float(me['lon'])
    my_prefs_raw  = me.get('preferred_gender', '')
    my_gender_raw = me.get('gender', '')

    # Normalise to Men/Women/Other
    my_gender  = _norm_gender(my_gender_raw)
    my_prefs   = _norm_gender(my_prefs_raw)

    # Build my preference pool
    if my_prefs == 'Everyone':
        pref_pool = {'Men', 'Women', 'Other'}
    else:
        pref_pool = {my_prefs}

    matches = []
    for uid, other in all_profiles.items():
        if uid == me.get('_uid'):
            continue
        if not other.get('lat') or not other.get('lon'):
            continue

        other_gender = _norm_gender(other.get('gender', ''))
        other_prefs  = _norm_gender(other.get('preferred_gender', ''))

        # Must be in my preferred pool
        if other_gender not in pref_pool:
            continue

        # Must also be interested in my gender (mutual)
        if other_prefs == 'Everyone':
            interested = True
        else:
            interested = my_gender in {other_prefs}

        if not interested:
            continue

        dist = haversine_km(me_lat, me_lon, float(other['lat']), float(other['lon']))
        matches.append({**other, 'uid': uid, 'distance_km': round(dist, 1)})

    matches.sort(key=lambda m: m['distance_km'])
    return matches


def find_best_waiting_match(me: dict) -> dict | None:
    """
    Find the best match from users currently waiting in queue.
    Returns the best compatible waiting user or None.
    """
    me_with_uid = {**me, '_uid': me.get('_uid')}
    for uid, wait_info in waiting_queue.items():
        if uid not in user_profiles:
            continue
        other = user_profiles[uid]
        # Check mutual compatibility
        matches = find_matches(me_with_uid, {uid: other})
        if matches:
            match = matches[0]
            match['wait_info'] = wait_info
            return match
    return None


def profile_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️  Edit Name",       callback_data='edit_name'),
         InlineKeyboardButton(text="✏️  Edit DOB",       callback_data='edit_age')],
        [InlineKeyboardButton(text="✏️  Edit Gender",    callback_data='edit_gender'),
         InlineKeyboardButton(text="✏️  Edit Interested", callback_data='edit_preferred_gender')],
        [InlineKeyboardButton(text="✏️  Edit Bio",       callback_data='edit_bio'),
         InlineKeyboardButton(text="✏️  Edit Location",  callback_data='edit_location')],
        [InlineKeyboardButton(text="❤️  Find Matches Now", callback_data='do_match')],
    ])


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« Back", callback_data='back')],
    ])


def profile_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« Back to Profile", callback_data='back_to_profile')],
    ])


# ── /start ─────────────────────────────────────────────────────────────────────

@dp.message(Command('start'))
async def cmd_start(message: types.Message, state: FSMContext):
    uid = message.from_user.id

    if uid in user_profiles:
        # Remove any lingering keyboard (from location step)
        try:
            remove_msg = await message.answer(".", reply_markup=ReplyKeyboardRemove())
            await safe_delete_msg(message.chat.id, remove_msg.message_id)
        except:
            pass
        
        await message.answer(
            f"👋 Hey again, *{user_profiles[uid]['name']}*!\n\n"
            "Your profile is ready. Want to find someone?",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❤️  Find Matches", callback_data='do_match')],
                [InlineKeyboardButton(text="🔄  Retake Profile", callback_data='retake_profile')],
            ]),
        )
        return

    # New user — start profile setup immediately
    await state.set_state(Setup.name)
    msg = await message.answer(
        "👋 Hey! I'm *Winkly*.\n\n"
        "I'll help you find people nearby. Let's set up your profile — "
        "it only takes ~30 seconds.\n\n"
        f"_{progress_bar(0)}_  Step 1 of {TOTAL_STEPS}\n\n"
        "📛 *What's your name?*",
        parse_mode='Markdown',
    )
    await state.update_data(last_bot_msg_id=msg.message_id, start_msg_id=message.message_id)


# ── /cancel ───────────────────────────────────────────────────────────────────

@dp.message(Command('cancel'), StateFilter(Setup))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "❌ Profile setup cancelled.\n\nSend /start to begin again.",
        reply_markup=ReplyKeyboardRemove(),
    )


# ── /back ─────────────────────────────────────────────────────────────────────

@dp.callback_query(lambda cb: cb.data == 'back', StateFilter(Setup))
async def go_back(cb: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    step = await state.get_state()
    idx  = step_index(step)

    if idx == 0:
        await cb.answer("Already at the start!", show_alert=True)
        return

    prev_step = PROGRESS_STEPS[idx - 1]
    prev_label = STEP_LABELS[prev_step]

    await state.set_state(getattr(Setup, prev_step))
    await cb.message.edit_text(
        f"_{progress_bar(idx - 1)}_  Step {idx} of {TOTAL_STEPS}\n\n"
        f"Go back — {prev_label}?\n\n_Enter your answer below._",
        parse_mode='Markdown',
    )
    await cb.answer()


@dp.callback_query(lambda cb: cb.data == 'back_to_profile', StateFilter(Setup))
async def back_to_profile(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    data = await state.get_data()
    user_profiles[uid] = _clean(data)
    await state.clear()
    await cb.message.edit_text(
        "✅ *Profile updated!*\n\n" + profile_summary(data),
        parse_mode='Markdown',
        reply_markup=profile_kb(),
    )
    await cb.answer()


# ── Cancel setup inline button ─────────────────────────────────────────────────

@dp.callback_query(lambda cb: cb.data == 'cancel_setup', StateFilter(Setup))
async def cancel_setup(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("❌ Profile setup cancelled.\n\nSend /start to begin again.")
    await cb.answer()


# ── /retake ───────────────────────────────────────────────────────────────────

@dp.callback_query(lambda cb: cb.data == 'retake_profile')
async def retake(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    if uid in user_profiles:
        del user_profiles[uid]
    await state.clear()
    await cb.message.edit_text(
        "🔄 Let's start fresh!\n\n"
        f"_{progress_bar(0)}_  Step 1 of {TOTAL_STEPS}\n\n"
        "📛 *What's your name?*",
        parse_mode='Markdown',
    )
    await state.set_state(Setup.name)
    await state.update_data(last_bot_msg_id=cb.message.message_id)
    await cb.answer()


# ── /edit inline (from review screen) ────────────────────────────────────────

@dp.callback_query(lambda cb: cb.data.startswith('edit_'))
async def edit_field(cb: types.CallbackQuery, state: FSMContext):
    field = cb.data.replace('edit_', '')
    if not hasattr(Setup, field):
        return
    await state.update_data(edit_mode=True)
    await state.set_state(getattr(Setup, field))
    idx = PROGRESS_STEPS.index(field)

    prompts = {
        "name":             "📛 *What's your name?*",
        "age":              "🎂 *When were you born?*\n_(DD / MM / YYYY — e.g. 15 / 08 / 1995)_",
        "gender":           "⚧ *What's your gender?*",
        "bio":              "📝 *Tell us about yourself:*\n_(hobbies, what you like, what you're looking for…)_",
        "preferred_gender": "❤️ *Who are you interested in?*",
        "location":         "📍 *Share your location* so we can find matches nearby:",
    }
    await cb.message.edit_text(
        f"_{progress_bar(idx)}_  Step {idx + 1} of {TOTAL_STEPS}\n\n"
        f"✏️  {prompts.get(field, 'Enter:')}",
        parse_mode='Markdown',
        reply_markup=profile_back_kb(),
    )
    await cb.answer()


# ── Name ───────────────────────────────────────────────────────────────────────

@dp.message(StateFilter(Setup.name))
async def handle_name(message: types.Message, state: FSMContext):
    d = await state.get_data()
    await safe_delete_msg(message.chat.id, d.get('start_msg_id'))
    await safe_delete_msg(message.chat.id, d.get('prev_bot_msg_id'))
    await safe_delete_msg(message.chat.id, d.get('last_user_msg_id'))
    try:
        name = message.text.strip()
        if len(name) < 2:
            await message.answer("⚠️ Name must be at least 2 characters. Please enter a longer name:")
            return
        await state.update_data(name=name, username=message.from_user.username)

        data = await state.get_data()
        next_state = Setup.age
        if data.get('edit_mode'):
            await state.update_data(edit_mode=False)
            next_state = Setup.confirm

        await state.update_data(prev_bot_msg_id=data.get('last_bot_msg_id'), last_user_msg_id=message.message_id)
        await advance_to(state, next_state, message.chat.id, message.from_user.id)
    except Exception as e:
        import traceback
        await message.answer(f"⚠️ Error: {e}")
        traceback.print_exc()


# ── Age ───────────────────────────────────────────────────────────────────────

# ── DOB → calculate age ─────────────────────────────────────────────────────────

from datetime import date
import re

def parse_dob(raw: str):
    """Return date object or None. Accepts many formats."""
    raw = raw.strip()
    
    # YYYY-MM-DD (ISO) - check FIRST to avoid confusion with DD-MM-YYYY
    if re.match(r'^\d{4}-\d{2}-\d{2}$', raw):
        y, m, d = raw.split('-')
        return date(int(y), int(m), int(d))
    
    # DDMMYYYY (compact)
    if re.match(r'^\d{8}$', raw):
        return date(int(raw[4:8]), int(raw[2:4]), int(raw[0:2]))
    
    # DD/MM/YYYY, DD-MM-YYYY, DD.MM.YYYY
    for sep in ('/', '-', '.'):
        if sep in raw:
            parts = raw.split(sep)
            if len(parts) == 3:
                d, m, y = parts
                if len(y) == 4 and y.isdigit():
                    return date(int(y), int(m), int(d))
                if len(y) == 2 and y.isdigit():
                    y = int(y) + (2000 if int(y) < 30 else 1900)
                    return date(y, int(m), int(d))
    
    # Month name: "15 Aug 1995", "15 August 1995", "Aug 15 1995"
    month_map = {
        'jan':1,'january':1,'feb':2,'february':2,'mar':3,'march':3,'apr':4,'april':4,
        'may':5,'jun':6,'june':6,'jul':7,'july':7,'aug':8,'august':8,'sep':9,'september':9,
        'oct':10,'october':10,'nov':11,'november':11,'dec':12,'december':12
    }
    parts = re.split(r'[\s,]+', raw)
    for i, part in enumerate(parts):
        if part.lower() in month_map:
            m = month_map[part.lower()]
            for d_part in parts:
                if d_part.isdigit() and 1 <= int(d_part) <= 31 and d_part != str(m):
                    for y_part in parts:
                        if y_part.isdigit() and len(y_part) in (2, 4) and y_part != d_part:
                            y = int(y_part)
                            if len(y_part) == 2:
                                y = y + (2000 if y < 30 else 1900)
                            return date(y, m, int(d_part))
    
    return None


def calc_age(born: date) -> int:
    today = date.today()
    age = today.year - born.year
    if (today.month, today.day) < (born.month, born.day):
        age -= 1
    return age


@dp.message(StateFilter(Setup.age))
async def handle_dob(message: types.Message, state: FSMContext):
    d = await state.get_data()
    await safe_delete_msg(message.chat.id, d.get('prev_bot_msg_id'))
    await safe_delete_msg(message.chat.id, d.get('last_user_msg_id'))
    raw = message.text.strip()
    dob = parse_dob(raw)
    if dob is None:
        await message.answer(
            "⚠️ Enter your date of birth in one of these formats:\n"
            "• *DD/MM/YYYY* or *DD-MM-YYYY* or *DD.MM.YYYY* (e.g. `15/08/1995`)\n"
            "• *DD/MM/YY* (e.g. `15/08/95`)\n"
            "• *YYYY-MM-DD* (e.g. `1995-08-15`)\n"
            "• *DDMMYYYY* (e.g. `15081995`)\n"
            "• *Month name* (e.g. `15 Aug 1995`, `August 15, 1995`)",
            parse_mode='Markdown',
        )
        return
    age = calc_age(dob)
    if not (18 <= age <= 100):
        await message.answer("⚠️ You must be at least 18 and no older than 100. Please enter a valid age:")
        return
    await state.update_data(age=str(age), dob=str(dob))

    data = await state.get_data()
    next_state = Setup.gender
    if data.get('edit_mode'):
        await state.update_data(edit_mode=False)
        next_state = Setup.confirm

    await state.update_data(prev_bot_msg_id=d.get('last_bot_msg_id'), last_user_msg_id=message.message_id)
    await advance_to(state, next_state, message.chat.id, message.from_user.id)


# ── Gender ────────────────────────────────────────────────────────────────────

@dp.message(StateFilter(Setup.gender))
async def handle_gender(message: types.Message, state: FSMContext):
    d = await state.get_data()
    await safe_delete_msg(message.chat.id, d.get('prev_bot_msg_id'))
    await safe_delete_msg(message.chat.id, d.get('last_user_msg_id'))
    raw = message.text.strip().lower()
    exact_map = {
        '👨 male': 'Male', '👩 female': 'Female', '⚧ other': 'Other',
    }
    if raw in exact_map:
        gender = exact_map[raw]
    else:
        emoji_map = {'👨': 'Male', '👩': 'Female', '⚧': 'Other'}
        gender = emoji_map.get(raw)
    if not gender:
        await message.answer(
            "⚠️ Please tap one of the gender buttons or type: Male / Female / Other",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text='👨 Male'), KeyboardButton(text='👩 Female'), KeyboardButton(text='⚧ Other')]],
                resize_keyboard=True, one_time_keyboard=True,
            ),
        )
        return
    await state.update_data(gender=gender)

    data = await state.get_data()
    next_state = Setup.bio
    if data.get('edit_mode'):
        await state.update_data(edit_mode=False)
        next_state = Setup.confirm

    await state.update_data(prev_bot_msg_id=d.get('last_bot_msg_id'), last_user_msg_id=message.message_id)
    await advance_to(state, next_state, message.chat.id, message.from_user.id)


@dp.callback_query(lambda cb: cb.data.startswith('gender_'), StateFilter(Setup.gender))
async def handle_gender_btn(cb: types.CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await safe_delete_msg(cb.message.chat.id, d.get('prev_bot_msg_id'))
    await safe_delete_msg(cb.message.chat.id, d.get('last_user_msg_id'))
    raw = cb.data.replace('gender_', '').lower()
    gender_map = {'male': 'Male', 'female': 'Female', 'other': 'Other', 'm': 'Male', 'f': 'Female'}
    gender = gender_map.get(raw, raw.title())
    await state.update_data(gender=gender)
    await cb.message.edit_text(f"⚧ *{gender}* — noted!")
    await cb.answer()

    data = await state.get_data()
    next_state = Setup.bio
    if data.get('edit_mode'):
        await state.update_data(edit_mode=False)
        next_state = Setup.confirm

    await state.update_data(prev_bot_msg_id=d.get('last_bot_msg_id'))
    await advance_to(state, next_state, cb.message.chat.id, cb.from_user.id)


# ── Bio ───────────────────────────────────────────────────────────────────────

@dp.message(StateFilter(Setup.bio))
async def handle_bio(message: types.Message, state: FSMContext):
    d = await state.get_data()
    await safe_delete_msg(message.chat.id, d.get('prev_bot_msg_id'))
    await safe_delete_msg(message.chat.id, d.get('last_user_msg_id'))
    raw = message.text.strip()

    if raw.lower() in ('/skip', 'skip'):
        await state.update_data(bio="")
        data = await state.get_data()
        next_state = Setup.preferred_gender
        if data.get('edit_mode'):
            await state.update_data(edit_mode=False)
            next_state = Setup.confirm
        await state.update_data(prev_bot_msg_id=d.get('last_bot_msg_id'), last_user_msg_id=message.message_id)
        await advance_to(state, next_state, message.chat.id, message.from_user.id)
        return

    if len(raw) < 10:
        await message.answer("⚠️ Please write at least a sentence or two about yourself (or type /skip to skip):")
        return
    await state.update_data(bio=raw)

    data = await state.get_data()
    next_state = Setup.preferred_gender
    if data.get('edit_mode'):
        await state.update_data(edit_mode=False)
        next_state = Setup.confirm

    await state.update_data(prev_bot_msg_id=d.get('last_bot_msg_id'), last_user_msg_id=message.message_id)
    await advance_to(state, next_state, message.chat.id, message.from_user.id)


@dp.callback_query(lambda cb: cb.data == 'skip_bio', StateFilter(Setup.bio))
async def skip_bio(cb: types.CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await safe_delete_msg(cb.message.chat.id, d.get('prev_bot_msg_id'))
    await safe_delete_msg(cb.message.chat.id, d.get('last_user_msg_id'))
    await safe_delete_msg(cb.message.chat.id, cb.message.message_id)
    await state.update_data(bio="")
    await cb.answer()

    data = await state.get_data()
    next_state = Setup.preferred_gender
    if data.get('edit_mode'):
        await state.update_data(edit_mode=False)
        next_state = Setup.confirm

    await state.update_data(prev_bot_msg_id=d.get('last_bot_msg_id'))
    await advance_to(state, next_state, cb.message.chat.id, cb.from_user.id)


# ── Preferred Gender ─────────────────────────────────────────────────────────

@dp.message(StateFilter(Setup.preferred_gender))
async def handle_preferred_gender(message: types.Message, state: FSMContext):
    d = await state.get_data()
    await safe_delete_msg(message.chat.id, d.get('prev_bot_msg_id'))
    await safe_delete_msg(message.chat.id, d.get('last_user_msg_id'))
    raw = message.text.strip().lower()
    exact_map = {
        '👨 men': 'Men', '👩 women': 'Women', '👥 everyone': 'Everyone',
    }
    if raw in exact_map:
        pref = exact_map[raw]
    else:
        emoji_map = {'👨': 'Men', '👩': 'Women', '👥': 'Everyone'}
        pref = emoji_map.get(raw)
    if not pref:
        await message.answer(
            "⚠️ Please tap one of the gender preference buttons: Men / Women / Everyone",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[
                    [KeyboardButton(text='👨 Men')],
                    [KeyboardButton(text='👩 Women')],
                    [KeyboardButton(text='👥 Everyone')],
                ],
                resize_keyboard=True, one_time_keyboard=True,
            ),
        )
        return
    await state.update_data(preferred_gender=pref)

    data = await state.get_data()
    next_state = Setup.location
    if data.get('edit_mode'):
        await state.update_data(edit_mode=False)
        next_state = Setup.confirm

    await state.update_data(prev_bot_msg_id=d.get('last_bot_msg_id'), last_user_msg_id=message.message_id)
    await advance_to(state, next_state, message.chat.id, message.from_user.id)


# ── Location ──────────────────────────────────────────────────────────────────

import aiohttp

async def geocode_place(place_name: str):
    """Convert place name to lat/lon using OpenStreetMap Nominatim (free, no API key)."""
    # Common city coordinates as fallback (approximate)
    common_cities = {
        'bangalore': (12.9716, 77.5946),
        'bengaluru': (12.9716, 77.5946),
        'mumbai': (19.0760, 72.8777),
        'delhi': (28.6139, 77.2090),
        'chennai': (13.0827, 80.2707),
        'kolkata': (22.5726, 88.3639),
        'hyderabad': (17.3850, 78.4867),
        'pune': (18.5204, 73.8567),
        'ahmedabad': (23.0225, 72.5714),
    }
    
    # Check lowercase for common cities
    normalized = place_name.lower().strip()
    if normalized in common_cities:
        return common_cities[normalized]
    
    # Try Nominatim API
    try:
        url = "https://nominatim.openstreetmap.org/search"
        params = {"q": place_name, "format": "json", "limit": 1}
        headers = {"User-Agent": "WinklyBot/1.0"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data:
                        return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        pass
    
    return None, None


@dp.message(lambda m: not m.location, StateFilter(Setup.location))
async def handle_location_text(message: types.Message, state: FSMContext):
    d = await state.get_data()
    await safe_delete_msg(message.chat.id, d.get('prev_bot_msg_id'))
    await safe_delete_msg(message.chat.id, d.get('last_user_msg_id'))
    text = message.text.strip()

    lat, lon = await geocode_place(text)
    if lat and lon:
        await state.update_data(lat=str(lat), lon=str(lon))
        await state.update_data(prev_bot_msg_id=d.get('last_bot_msg_id'), last_user_msg_id=message.message_id)
        await advance_to(state, Setup.confirm, message.chat.id, message.from_user.id)
        return

    await message.answer(
        "📍 Couldn't find that place. Try again, or use the buttons below:\n\n"
        "💡 *Tip:* Try common city names like 'Bangalore', 'Mumbai', 'Delhi', 'Chennai', or use 'Share My Location' for GPS.\n\n"
        "You can also try the full name with state/country (e.g., 'Bengaluru, Karnataka, India').",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text='📍 Share My Location', request_location=True)],
                [KeyboardButton(text='⌨️  Enter Place Name')],
            ],
            resize_keyboard=True, one_time_keyboard=True,
        ),
    )


@dp.message(lambda m: m.location, StateFilter(Setup.location))
async def handle_location_ok(message: types.Message, state: FSMContext):
    d = await state.get_data()
    await safe_delete_msg(message.chat.id, d.get('prev_bot_msg_id'))
    await safe_delete_msg(message.chat.id, d.get('last_user_msg_id'))
    loc = message.location
    await state.update_data(lat=str(loc.latitude), lon=str(loc.longitude))
    await state.update_data(prev_bot_msg_id=d.get('last_bot_msg_id'), last_user_msg_id=message.message_id)
    await advance_to(state, Setup.confirm, message.chat.id, message.from_user.id)


# ── Confirm / review screen ─────────────────────────────────────────────────────

async def advance_to(state: FSMContext, next_state: State, chat_id: int, user_id: int):
    """Set next state, send the appropriate prompt, and save its message_id."""
    await state.set_state(next_state)
    step = next_state.state.split(':')[-1]
    msg = None

    if step == 'age':
        idx = 1
        msg = await bot.send_message(
            chat_id,
            f"_{progress_bar(idx)}_  Step {idx + 1} of {TOTAL_STEPS}\n\n"
            "🎂 *When were you born?*\n_(DD / MM / YYYY — e.g. 15 / 08 / 1995)_",
            parse_mode='Markdown',
        )
    elif step == 'gender':
        idx = 2
        msg = await bot.send_message(
            chat_id,
            f"_{progress_bar(idx)}_  Step {idx + 1} of {TOTAL_STEPS}\n\n"
            "⚧ *What's your gender?*",
            parse_mode='Markdown',
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[
                    [KeyboardButton(text='👨 Male')],
                    [KeyboardButton(text='👩 Female')],
                    [KeyboardButton(text='⚧ Other')],
                ],
                resize_keyboard=True, one_time_keyboard=True,
            ),
        )
    elif step == 'bio':
        idx = 3
        msg = await bot.send_message(
            chat_id,
            f"_{progress_bar(idx)}_  Step {idx + 1} of {TOTAL_STEPS}\n\n"
            "📝 *Tell us a bit about yourself*\n"
            "_(hobbies, what you like, what you're looking for…)_\n\n"
            "_Or tap /skip to skip this step._",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⏭️  Skip Bio", callback_data='skip_bio')],
                [InlineKeyboardButton(text="« Back", callback_data='back')],
            ]),
        )
    elif step == 'preferred_gender':
        idx = 4
        msg = await bot.send_message(
            chat_id,
            f"_{progress_bar(idx)}_  Step {idx + 1} of {TOTAL_STEPS}\n\n"
            "❤️ *Who are you interested in?*",
            parse_mode='Markdown',
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[
                    [KeyboardButton(text='👨 Men')],
                    [KeyboardButton(text='👩 Women')],
                    [KeyboardButton(text='👥 Everyone')],
                ],
                resize_keyboard=True, one_time_keyboard=True,
            ),
        )
    elif step == 'location':
        idx = 5
        msg = await bot.send_message(
            chat_id,
            f"_{progress_bar(idx)}_  Step {idx + 1} of {TOTAL_STEPS}\n\n"
            "📍 *Share your location* or type a place name (city, area):",
            parse_mode='Markdown',
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[
                    [KeyboardButton(text='📍 Share My Location', request_location=True)],
                    [KeyboardButton(text='⌨️  Enter Place Name')],
                ],
                resize_keyboard=True, one_time_keyboard=True,
            ),
        )
    elif step == 'confirm':
        new_data = await state.get_data()
        uid  = user_id
        existing = user_profiles.get(uid, {})
        merged = _clean({**existing, **new_data})
        user_profiles[uid] = merged
        await state.clear()

        # Remove location keyboard
        try:
            remove_msg = await bot.send_message(chat_id, ".", reply_markup=ReplyKeyboardRemove())
            await safe_delete_msg(chat_id, remove_msg.message_id)
        except:
            pass
        
        await bot.send_message(
            chat_id,
            "🎉 *Profile complete!*\n\n" + profile_summary(merged) +
            "\nDoes everything look right?",
            parse_mode='Markdown',
            reply_markup=profile_kb(),
        )
        return

    if msg:
        await state.update_data(last_bot_msg_id=msg.message_id)


# ── Review screen interactions ─────────────────────────────────────────────────

# ── Match card helpers ──────────────────────────────────────────────────────────

async def _send_match_card(chat_id: int, partner_profile: dict, partner_uid: int, match_type: str = "match"):
    """Send a match notification with optional photo.
    
    match_type: "match" → "It's a Match!", "chat" → "Chat Now"
    """
    name = partner_profile.get('name', 'Someone')
    age = partner_profile.get('age', '?')
    gender = partner_profile.get('gender', '?')
    bio = partner_profile.get('bio', '') or '—'
    verified_badge = "  ✅" if partner_profile.get('verified') else ""
    photo_id = partner_profile.get('photo')
    
    card_text = (
        f"🎉 *It's a Match!*\n\n"
        f"You and *{name}* are compatible!\n\n"
        f"📛  {name}{verified_badge}\n"
        f"🎂  {age}  |  ⚧ {gender}\n"
        f"📝  {bio[:100]}\n\n"
        "Tap below to start chatting:"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬  Chat Now", callback_data=f'chat:{partner_uid}')],
    ])
    
    if photo_id:
        try:
            await bot.send_photo(chat_id, photo_id, caption=card_text, parse_mode='Markdown', reply_markup=kb)
            return
        except Exception as e:
            print(f"Match card photo error: {e}")
    
    await bot.send_message(chat_id, card_text, parse_mode='Markdown', reply_markup=kb)


# Store last shown matches per user for Like/Reject navigation
_LAST_MATCHES: dict[int, list] = {}

@dp.callback_query(lambda cb: cb.data == 'do_match')
async def do_match(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await cb.message.edit_reply_markup(reply_markup=None)

    if uid not in user_profiles:
        await cb.message.answer("⚠️ No profile found. Please send /start to set up your profile first.")
        await cb.answer()
        return

    if not check_match_limit(uid):
        me = user_profiles.get(uid, {})
        # Unverified females → verification popup instead of premium
        if me.get('gender') in ('Women', 'Female') and not me.get('verified'):
            await cb.message.answer(
                "⚠️ You've reached your free limit.\n\n"
                "📸 Verify your profile to continue chatting (free & unlimited).",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📸 Verify Now", callback_data='verify_now')],
                    [InlineKeyboardButton(text="👤 View Profile", callback_data='back_to_profile')],
                ]),
            )
        else:
            await cb.message.answer(
                "⚠️ You've used all your free matches.\n\n"
                "💎 Continue at just Rs49 — get unlimited matches for a day!",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💎 Get 1 Day - Rs49", callback_data='buy_1day')],
                    [InlineKeyboardButton(text="📋 See More Plans", callback_data='see_more_plans')],
                    [InlineKeyboardButton(text="👤 View Profile", callback_data='back_to_profile')],
                ]),
            )
        await cb.answer()
        return

    me = user_profiles[uid]
    
    # First, check if there's someone waiting for us in the queue
    waiting_match = find_best_waiting_match(me)
    if waiting_match:
        partner = waiting_match['uid']
        partner_name = waiting_match['name']
        
        # Create mutual match
        if uid not in active_matches:
            active_matches[uid] = {}
        if partner not in active_matches:
            active_matches[partner] = {}
        
        active_matches[uid][partner] = {'status': 'matched'}
        active_matches[partner][uid] = {'status': 'matched'}
        
        # Increment match count for both users
        increment_match_count(uid)
        increment_match_count(partner)
        
        # Remove from waiting queue
        if uid in waiting_queue:
            del waiting_queue[uid]
        if partner in waiting_queue:
            del waiting_queue[partner]
        
        # Notify both users of instant match
        await _send_match_card(uid, waiting_match, partner)
        await _send_match_card(partner, me, uid)
        
        # Clean up the "Find Matches" button message
        try:
            await cb.message.delete()
        except:
            pass
        
        await cb.answer()
        return
    
    # No instant match - add to waiting queue
    import time
    waiting_queue[uid] = {
        'added_at': time.time(),
        'message_id': cb.message.message_id,
        'state': state,
    }
    
    # Send "Searching..." message
    search_msg = await cb.message.answer(
        f"🔍 *{me['name']}*, searching for someone compatible...\n\n"
        "Looking for someone who matches your preferences (gender + location).\n"
        "This usually takes 5-30 seconds.\n\n"
        "⏳ *Waiting in queue...*",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        ]),
    )
    
    # Update the waiting queue entry with message ID
    waiting_queue[uid]['message_id'] = search_msg.message_id
    
    # Schedule background check for match
    async def check_for_match():
        await asyncio.sleep(5)  # Check every 5 seconds
        if uid not in waiting_queue:
            return
        
        # Try to find a match
        waiting_match = find_best_waiting_match(me)
        if waiting_match:
            partner = waiting_match['uid']
            partner_name = waiting_match['name']
            
            # Create mutual match
            if uid not in active_matches:
                active_matches[uid] = {}
            if partner not in active_matches:
                active_matches[partner] = {}
            
            active_matches[uid][partner] = {'status': 'matched'}
            active_matches[partner][uid] = {'status': 'matched'}
            
            # Increment match count for both users
            increment_match_count(uid)
            increment_match_count(partner)
            
            # Remove from waiting queue
            if uid in waiting_queue:
                del waiting_queue[uid]
            if partner in waiting_queue:
                del waiting_queue[partner]
            
            # Delete old messages and send fresh match card
            try:
                await cb.message.delete()  # "Find Matches" button message
            except:
                pass
            try:
                await bot.delete_message(cb.message.chat.id, search_msg.message_id)  # "Searching..." message
            except:
                pass
            
            # Send fresh match card (with photo if available)
            try:
                await _send_match_card(uid, waiting_match, partner)
            except:
                pass
            
            # Notify the partner as well
            try:
                await _send_match_card(partner, me, uid)
            except:
                pass
        else:
            # Still waiting - schedule another check
            if uid in waiting_queue:
                asyncio.create_task(check_for_match())
    
    # Start the background check
    asyncio.create_task(check_for_match())
    await cb.answer()





async def _show_next_match(message, uid: int, idx: int):
    matches = _LAST_MATCHES.get(uid, [])
    if idx >= len(matches):
        await message.answer(
            "🎉 That's everyone nearby! Check back later.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄  Search Again", callback_data='do_match')],
                [InlineKeyboardButton(text="👤  Back to Profile", callback_data='back_to_profile')],
            ]),
        )
        return

    m = matches[idx]
    # Check already liked/rejected
    if uid in likes_sent and m['uid'] in likes_sent[uid]:
        await _show_next_match(message, uid, idx + 1)
        return

    # Track this display context
    user_profiles[uid]['_current_match_idx'] = idx
    user_profiles[uid]['_current_match_uid'] = m['uid']

    bio_line = f"\n📝  {m.get('bio', '')[:100]}" if m.get('bio') else ""
    verified_badge = "  ✅" if m.get('verified') else ""
    card_text = (
        f"👤 *{m['name']}*{verified_badge}\n"
        f"🎂  Age: {m['age']}  |  ⚧ {m['gender']}\n"
        f"📍  {m['distance_km']} km away{bio_line}"
    )
    
    # Send photo if available, else text-only
    photo_id = m.get('photo')
    if photo_id:
        try:
            await bot.send_photo(
                chat_id=message.chat.id,
                photo=photo_id,
                caption=card_text,
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="❤️  Like", callback_data=f'like:{m["uid"]}'),
                     InlineKeyboardButton(text="❌  Skip", callback_data=f'skip:{m["uid"]}')],
                    [InlineKeyboardButton(text="⬇️  More matches", callback_data='show_more_matches')],
                ]),
            )
            return
        except Exception as e:
            print(f"Match card photo error: {e}")
            # Fall through to text-only
    
    await message.answer(
        card_text,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❤️  Like", callback_data=f'like:{m["uid"]}'),
             InlineKeyboardButton(text="❌  Skip", callback_data=f'skip:{m["uid"]}')],
            [InlineKeyboardButton(text="⬇️  More matches", callback_data='show_more_matches')],
        ]),
    )


@dp.callback_query(lambda cb: cb.data.startswith('like:'))
async def handle_like(cb: types.CallbackQuery):
    liker = cb.from_user.id
    liked = int(cb.data.split(':')[1])

    if liker not in likes_sent:
        likes_sent[liker] = set()
    likes_sent[liker].add(liked)

    # Check for mutual like
    if liked in likes_sent and liker in likes_sent.get(liked, set()):
        # Mutual match! Establish match
        if liker not in active_matches:
            active_matches[liker] = {}
        if liked not in active_matches:
            active_matches[liked] = {}

        # Store match metadata
        active_matches[liker][liked] = {'status': 'matched'}
        active_matches[liked][liker] = {'status': 'matched'}

        # Notify both users about the mutual match
        _L = user_profiles[liker]
        _R = user_profiles[liked]

        await _send_match_card(liker, _R, liked)
        await _send_match_card(liked, _L, liker)
    else:
        # One-sided like — send a subtle acknowledgment
        await bot.send_message(liker, "❤️ Liked! If they like you back, it's a match.")

    # Show next match
    me = user_profiles[liker]
    current_idx = me.get('_current_match_idx', 0)
    await cb.answer()
    await _show_next_match(cb.message, liker, current_idx + 1)


@dp.callback_query(lambda cb: cb.data.startswith('skip:'))
async def handle_skip(cb: types.CallbackQuery):
    uid = cb.from_user.id
    skipped = int(cb.data.split(':')[1])

    # Track rejection to avoid re-showing
    user_profiles[uid].setdefault('rejected', set()).add(skipped)

    me = user_profiles[uid]
    current_idx = me.get('_current_match_idx', 0)
    await cb.answer("Skipped")
    await _show_next_match(cb.message, uid, current_idx + 1)


# ── Chat system ───────────────────────────────────────────────────────────────

@dp.callback_query(lambda cb: cb.data.startswith('chat:'))
async def start_chat(cb: types.CallbackQuery):
    uid = cb.from_user.id
    partner = int(cb.data.split(':')[1])

    # Verify mutual match
    if uid not in active_matches or partner not in active_matches.get(uid, {}):
        await cb.answer("⚠️ You are not matched with this user.", show_alert=True)
        return

    current_chat[uid] = partner
    current_chat[partner] = uid  # Bidirectional

    # Remove any lingering location keyboard
    try:
        remove_msg = await bot.send_message(uid, ".", reply_markup=ReplyKeyboardRemove())
        await safe_delete_msg(uid, remove_msg.message_id)
    except:
        pass

    partner_name = user_profiles[partner]['name']
    await cb.message.edit_text(
        f"💬 *Chat started with {partner_name}*\n\n"
        "Send your messages below. Tap below to say 'Hi'.",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👋 Say Hi", callback_data=f'say_hi:{partner}')],
        ]),
    )

    # Also tell the partner
    await bot.send_message(
        partner,
        f"💬 *{user_profiles[uid]['name']}* started chatting!\n\n"
        "You can say 'Hi'.",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👋 Say Hi", callback_data=f'say_hi:{uid}')],
        ]),
    )
    await cb.answer()








LONG_PLANS = [
    {"name": "Monthly", "price": 199, "duration": 30},
    {"name": "3 Months", "price": 299, "duration": 90},
    {"name": "6 Months", "price": 499, "duration": 180},
    {"name": "1 Year", "price": 699, "duration": 365},
]

@dp.callback_query(lambda cb: cb.data == 'see_more_plans')
async def see_more_plans(cb: types.CallbackQuery):
    """Show long-term premium plans (Plans 2-5)."""
    uid = cb.from_user.id
    
    if is_premium_user(uid):
        await cb.message.edit_text(
            "💎 *Already Premium!*\n\n"
            "You already have an active premium subscription.\n\n"
            "Tap below to find matches:",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄  Find Matches", callback_data='do_match')],
            ]),
        )
        await cb.answer()
        return
    
    plan_keyboard = []
    for plan in LONG_PLANS:
        daily_rate = 49
        discount = int((1 - plan['price'] / (daily_rate * plan['duration'])) * 100)
        plan_keyboard.append([
            InlineKeyboardButton(
                text=f"💎 {plan['name']} - Rs{plan['price']} (Save {discount}%)",
                callback_data=f"select_premium:{plan['name']}:{plan['price']}:{plan['duration']}"
            )
        ])
    
    plan_keyboard.append([InlineKeyboardButton(text="◀️ Back", callback_data='back_to_limits')])
    plan_keyboard.append([InlineKeyboardButton(text="👤 View Profile", callback_data='back_to_profile')])
    
    lines = ["💎 *Premium Plans - Save More*\n"]
    for plan in LONG_PLANS:
        daily_rate = 49
        discount = int((1 - plan['price'] / (daily_rate * plan['duration'])) * 100)
        per_day = round(plan['price'] / plan['duration'])
        lines.append(f"• {plan['name']} — Rs{plan['price']}  (~Rs{per_day}/day, save {discount}%)")
    lines.append(f"\nOr go back and get 1 day for just Rs49.")
    
    await cb.message.edit_text(
        "\n".join(lines),
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=plan_keyboard),
    )
    await cb.answer()


def _create_payment_link_sync(uid: int, plan_name: str, price: int, duration: int) -> str | None:
    """Synchronous Razorpay Payment Link creation."""
    try:
        response = razorpay_client.payment_link.create({
            "amount": price * 100,
            "currency": "INR",
            "description": f"Winkly Premium - {plan_name}",
            "notes": {
                "uid": str(uid),
                "duration_days": str(duration),
            },
            "callback_url": f"{WEBHOOK_URL}/payment/success",
            "callback_method": "get",
        })
        return response.get("short_url")
    except Exception as e:
        print(f"Razorpay payment link error: {e}")
        return None

async def create_payment_link(uid: int, plan_name: str, price: int, duration: int) -> str | None:
    """Create a Razorpay Payment Link in a thread to avoid blocking the event loop."""
    if not razorpay_client:
        return None
    return await asyncio.to_thread(_create_payment_link_sync, uid, plan_name, price, duration)

@dp.callback_query(lambda cb: cb.data.startswith('select_premium:'))
async def select_premium(cb: types.CallbackQuery):
    """Handle premium plan selection — create real Razorpay Payment Link."""
    uid = cb.from_user.id
    parts = cb.data.split(':')
    plan_name = parts[1]
    price = int(parts[2])
    duration = int(parts[3])
    
    await cb.message.edit_text(
        f"⏳ Creating your payment link for *{plan_name}*...",
        parse_mode='Markdown',
    )
    
    payment_url = await create_payment_link(uid, plan_name, price, duration)
    
    if not payment_url:
        await cb.message.edit_text(
            "⚠️ Sorry, we couldn't create a payment link right now.\n\n"
            "Please try again later or contact support.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Back", callback_data='see_more_plans')],
            ]),
        )
        await cb.answer("Payment creation failed", show_alert=True)
        return
    
    await cb.message.edit_text(
        f"💎 *Premium Subscription - {plan_name}*\n\n"
        f"Price: Rs{price} for {duration} days\n\n"
        f"✅ Unlimited texts and matches\n"
        f"✅ Priority matching\n\n"
        "Tap below to complete your payment:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Pay Rs{price}", url=payment_url)],
            [InlineKeyboardButton(text="👤 View Profile", callback_data='back_to_profile')],
        ]),
    )
    await cb.answer()


@dp.callback_query(lambda cb: cb.data == 'buy_1day')
async def buy_1day(cb: types.CallbackQuery):
    """Handle 1-day premium purchase."""
    uid = cb.from_user.id
    
    if is_premium_user(uid):
        await cb.message.edit_text(
            "💎 You're already premium! Enjoy unlimited access.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄  Find Matches", callback_data='do_match')],
            ]),
        )
        await cb.answer()
        return
    
    await cb.message.edit_text(
        "⏳ Creating your payment link for *1 Day Premium*...",
        parse_mode='Markdown',
    )
    
    payment_url = await create_payment_link(uid, "1 Day", 49, 1)
    
    if not payment_url:
        await cb.message.edit_text(
            "⚠️ Sorry, we couldn't create a payment link right now.\n\n"
            "Please try again later or contact support.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Back", callback_data='see_more_plans')],
            ]),
        )
        await cb.answer("Payment creation failed", show_alert=True)
        return
    
    await cb.message.edit_text(
        f"💎 *1 Day Premium - Rs49*\n\n"
        f"✅ Unlimited texts and matches for 24 hours\n\n"
        "Tap below to complete your payment:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Pay Rs49", url=payment_url)],
            [InlineKeyboardButton(text="📋 See More Plans", callback_data='see_more_plans')],
            [InlineKeyboardButton(text="👤 View Profile", callback_data='back_to_profile')],
        ]),
    )
    await cb.answer()


@dp.callback_query(lambda cb: cb.data == 'back_to_limits')
async def back_to_limits(cb: types.CallbackQuery):
    """Show the upgrade options popup (1-day + See More Plans)."""
    await cb.message.edit_text(
        "💎 *Upgrade Winkly Premium*\n\n"
        "Continue at just Rs49 — get unlimited texts & matches for a day!",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💎 Get 1 Day - Rs49", callback_data='buy_1day')],
            [InlineKeyboardButton(text="📋 See More Plans", callback_data='see_more_plans')],
            [InlineKeyboardButton(text="👤 View Profile", callback_data='back_to_profile')],
        ]),
    )
    await cb.answer()


@dp.message(Command('premium'))
async def cmd_premium(message: types.Message):
    """Show premium subscription information."""
    uid = message.from_user.id
    
    if uid not in user_profiles:
        await message.answer("📝 You haven't set up a profile yet.\n\nSend /start to begin!")
        return
    
    if is_premium_user(uid):
        expiry = premium_subscriptions[uid].get('expiry_date')
        if expiry:
            days_left = (expiry - datetime.now()).days
            status = f"Active until {expiry.strftime('%Y-%m-%d')}\n{days_left} days remaining"
        else:
            status = "Active (no expiry date set)"
        
        await message.answer(
            f"💎 *Premium Subscription Status*\n\n"
            f"{status}\n\n"
            f"You have unlimited texts and matches!\n\n"
            f"Tap below to find matches:",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄  Find Matches", callback_data='do_match')],
            ]),
        )
    else:
        usage = get_user_usage(uid)
        texts_left = max(0, 10 - usage['texts_sent'])
        matches_left = max(0, 10 - usage['match_count'])
        await message.answer(
            f"💎 *Premium Plans*\n\n"
            f"Your usage: {texts_left} texts left, {matches_left} matches left\n\n"
            f"Choose how you'd like to upgrade:",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💎 1 Day - Rs49", callback_data='buy_1day')],
                [InlineKeyboardButton(text="📋 See More Plans", callback_data='see_more_plans')],
                [InlineKeyboardButton(text="👤 View Profile", callback_data='back_to_profile')],
            ]),
        )


@dp.callback_query(lambda cb: cb.data == 'back_to_profile')
async def back_to_profile(cb: types.CallbackQuery, state: FSMContext):
    """Navigate back to profile view."""
    uid = cb.from_user.id
    
    if uid not in user_profiles:
        await cb.message.answer("📝 You haven't set up a profile yet.\n\nSend /start to begin!")
        return
    
    data = user_profiles[uid]
    await cb.message.edit_text(
        profile_summary(data) + "\n_Use the buttons below to edit any field._",
        parse_mode='Markdown',
        reply_markup=profile_kb(),
    )
    await cb.answer()


# ── Female Verification System ─────────────────────────────────────────────────

def _verification_status_text(uid: int) -> str:
    """Return human-readable verification status text for a user."""
    profile = user_profiles.get(uid, {})
    status = profile.get('verification_status', 'none')
    if profile.get('verified'):
        return "✅ *Verified* — You have unlimited free access."
    if status == 'pending':
        return "⏳ *Verification under review* — Our team is checking your photos."
    if status == 'rejected':
        return "❌ *Verification rejected* — The selfie didn't match your profile photo.\n\nTap below to retry."
    return "📸 *Not verified* — Get your verified badge to unlock unlimited chatting."


async def _send_to_admin(uid: int):
    """Send verification request to the admin chat for review."""
    profile = user_profiles.get(uid, {})
    if not profile or not ADMIN_CHAT_ID:
        return
    
    name = profile.get('name', 'Unknown')
    age = profile.get('age', '?')
    gender = profile.get('gender', '?')
    bio = profile.get('bio', '—')
    username = f"@{profile.get('username', '—')}" if profile.get('username') else '—'
    photo_id = profile.get('photo')
    selfie_id = profile.get('selfie')
    
    text = (
        f"📸 *New verification request*\n\n"
        f"👤 {name} ({age}, {gender})\n"
        f"🆔 Telegram: `{uid}`\n"
        f"📛 Username: {username}\n"
        f"📝 Bio: {bio[:80] if bio else '—'}\n\n"
        f"*Profile photo (top) vs Selfie (bottom)* — do they match?"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Match — Approve", callback_data=f'admin_approve:{uid}'),
         InlineKeyboardButton(text="❌ No Match — Reject", callback_data=f'admin_reject:{uid}')],
    ])
    
    # Send profile photo then selfie with the review text
    if photo_id:
        try:
            await bot.send_photo(ADMIN_CHAT_ID, photo_id, caption=text, parse_mode='Markdown', reply_markup=kb)
        except Exception as e:
            print(f"Admin send photo error: {e}")
            await bot.send_message(ADMIN_CHAT_ID, f"{text}\n\n(Photo not accessible)", parse_mode='Markdown', reply_markup=kb)
    else:
        await bot.send_message(ADMIN_CHAT_ID, text + "\n\n(No profile photo)", parse_mode='Markdown', reply_markup=kb)
    
    if selfie_id:
        try:
            await bot.send_photo(ADMIN_CHAT_ID, selfie_id, caption="📸 Selfie (for comparison)")
        except Exception as e:
            print(f"Admin send selfie error: {e}")


@dp.callback_query(lambda cb: cb.data == 'verify_now')
async def verify_now(cb: types.CallbackQuery):
    """Start the verification flow — ask user to send a profile photo."""
    uid = cb.from_user.id
    await cb.answer()
    
    if uid not in user_profiles:
        await cb.message.answer("📝 Please set up your profile first with /start.")
        return
    
    # Check if already verified
    if user_profiles[uid].get('verified'):
        await cb.message.answer("✅ You're already verified! Enjoy unlimited access.")
        return
    
    # Start verification session
    _verify_sessions[uid] = 'awaiting_photo'
    await cb.message.answer(
        "📸 *Step 1: Profile Photo*\n\n"
        "Send a good, clear photo of yourself.\n"
        "This will be shown to other users as your profile picture.\n\n"
        "_Make sure your face is clearly visible._",
        parse_mode='Markdown',
    )


@dp.message(lambda m: m.from_user.id in _verify_sessions)
async def handle_verify_photo(message: types.Message):
    """Handle photo uploads during the verification flow."""
    uid = message.from_user.id
    step = _verify_sessions.get(uid)
    
    # If user sends text instead of photo during verification
    if not message.photo:
        if step == 'awaiting_photo':
            await message.answer("📸 Please send a *photo* (not text). Upload a clear picture of yourself.", parse_mode='Markdown')
        elif step == 'awaiting_selfie':
            await message.answer("📸 Please send a *selfie photo* (not text). A clear selfie looking at the camera.", parse_mode='Markdown')
        return
    """Handle photo uploads during the verification flow."""
    uid = message.from_user.id
    step = _verify_sessions.get(uid)
    
    if step == 'awaiting_photo':
        # Save profile photo
        file_id = message.photo[-1].file_id
        user_profiles[uid]['photo'] = file_id
        
        # Advance to selfie step
        _verify_sessions[uid] = 'awaiting_selfie'
        await message.answer(
            "✅ Great profile photo!\n\n"
            "📸 *Step 2: Verification Selfie*\n\n"
            "Now send a *selfie* looking straight at the camera.\n"
            "This is only visible to our admin team for verification.\n\n"
            "_Keep your face clearly visible, good lighting._",
            parse_mode='Markdown',
        )
    
    elif step == 'awaiting_selfie':
        # Save selfie
        file_id = message.photo[-1].file_id
        user_profiles[uid]['selfie'] = file_id
        user_profiles[uid]['verification_status'] = 'pending'
        
        # Clear session
        del _verify_sessions[uid]
        
        # Notify user
        await message.answer(
            "✅ *Photos received!*\n\n"
            "Our team will review your verification shortly.\n"
            "You'll be notified once it's approved.\n\n"
            "Thank you for your patience! 🎉",
            parse_mode='Markdown',
        )
        
        # Send to admin for review
        await _send_to_admin(uid)


@dp.callback_query(lambda cb: cb.data.startswith('admin_approve:') or cb.data.startswith('admin_reject:'))
async def admin_verification_action(cb: types.CallbackQuery):
    """Admin approves or rejects a verification request."""
    if cb.from_user.id != ADMIN_CHAT_ID:
        await cb.answer("⛔ You're not authorized.", show_alert=True)
        return
    
    action, uid_str = cb.data.split(':', 1)
    uid = int(uid_str)
    profile = user_profiles.get(uid)
    
    if not profile:
        await cb.answer("⚠️ User profile no longer exists.", show_alert=True)
        await cb.message.edit_text(cb.message.text + "\n\n_(User deleted)_")
        return
    
    if action == 'admin_approve':
        profile['verified'] = True
        profile['verification_status'] = 'approved'
        # Edit caption if message has photo, else edit text
        try:
            if cb.message.photo:
                await cb.message.edit_caption(
                    caption=cb.message.caption + f"\n\n✅ *Approved by admin*",
                    parse_mode='Markdown',
                )
            else:
                await cb.message.edit_text(
                    cb.message.text + f"\n\n✅ *Approved by admin*",
                    parse_mode='Markdown',
                )
        except Exception as e:
            print(f"Admin approve edit error: {e}")
        # Notify user
        try:
            await bot.send_message(
                uid,
                "🎉 *You're verified!*\n\n"
                "✅ Your profiles photos matched and you've been verified.\n"
                "You now have *unlimited free access* to chat with anyone!\n\n"
                "Go find your match! ❤️",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="❤️  Find Matches", callback_data='do_match')],
                ]),
            )
        except Exception as e:
            print(f"Admin notify user error: {e}")
        await cb.answer("✅ User approved!")
    
    elif action == 'admin_reject':
        profile['verification_status'] = 'rejected'
        # Clear the selfie but keep the profile photo
        profile['selfie'] = None
        try:
            if cb.message.photo:
                await cb.message.edit_caption(
                    caption=cb.message.caption + f"\n\n❌ *Rejected by admin*",
                    parse_mode='Markdown',
                )
            else:
                await cb.message.edit_text(
                    cb.message.text + f"\n\n❌ *Rejected by admin*",
                    parse_mode='Markdown',
                )
        except Exception as e:
            print(f"Admin reject edit error: {e}")
        # Notify user
        try:
            await bot.send_message(
                uid,
                "❌ *Verification not approved.*\n\n"
                "The selfie didn't match your profile photo.\n\n"
                "You can retry anytime with better photos using /verify.",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📸 Retry Verification", callback_data='verify_now')],
                ]),
            )
        except Exception as e:
            print(f"Admin notify user error: {e}")
        await cb.answer("❌ Rejected")


@dp.message(Command('admin'))
async def cmd_admin(message: types.Message):
    """Show pending verification requests for admin review."""
    uid = message.from_user.id
    if uid != ADMIN_CHAT_ID:
        await message.answer("⛔ You're not authorized to use this command.")
        return
    
    pending = [(uid, prof) for uid, prof in user_profiles.items()
               if prof.get('verification_status') == 'pending']
    
    if not pending:
        await message.answer("📋 *No pending verification requests.*", parse_mode='Markdown')
        return
    
    text_lines = [f"📋 *Pending Verifications: {len(pending)}*", ""]
    for puid, prof in pending:
        name = prof.get('name', 'Unknown')
        age = prof.get('age', '?')
        gender = prof.get('gender', '?')
        text_lines.append(f"• {name} ({age}, {gender}) — `{puid}`")
    
    text_lines.append("")
    text_lines.append("Requests are also sent to this chat as they come in.")
    
    await message.answer("\n".join(text_lines), parse_mode='Markdown')


@dp.message(Command('verify'))
async def cmd_verify(message: types.Message):
    """Show verification status and start/retry verification (#get_verified_badge)."""
    uid = message.from_user.id
    
    if uid not in user_profiles:
        await message.answer("📝 Please set up your profile first with /start.")
        return
    
    status_text = _verification_status_text(uid)
    profile = user_profiles[uid]
    current_status = profile.get('verification_status', 'none')
    
    if profile.get('verified'):
        await message.answer(
            f"🏅 *Verified Badge*\n\n{status_text}",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❤️  Find Matches", callback_data='do_match')],
            ]),
        )
    elif current_status == 'pending':
        await message.answer(
            f"🏅 *Verification Status*\n\n{status_text}",
            parse_mode='Markdown',
        )
    elif current_status == 'rejected':
        await message.answer(
            f"🏅 *Verification Status*\n\n{status_text}",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📸 Retry Verification", callback_data='verify_now')],
            ]),
        )
    else:
        await message.answer(
            f"🏅 *Get Verified*\n\n{status_text}\n\n"
            "Upload your profile photo + selfie to get verified.\n"
            "Female users get unlimited free access after verification!",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📸 Start Verification", callback_data='verify_now')],
            ]),
        )


@dp.callback_query(lambda cb: cb.data.startswith('say_hi:'))
async def say_hi(cb: types.CallbackQuery):
    """Send a 'Hi' message to the chat partner."""
    uid = cb.from_user.id
    partner = int(cb.data.split(':')[1])

    if uid not in active_matches or partner not in active_matches.get(uid, {}):
        await cb.answer("⚠️ You are not matched with this user.", show_alert=True)
        return

    if uid not in current_chat or current_chat[uid] != partner:
        await cb.answer("⚠️ You are not in an active chat with this user.", show_alert=True)
        return

    partner_name = user_profiles[partner]['name']

    # Disable the button immediately to prevent duplicate clicks
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer()

    # Send Hi to partner
    try:
        await bot.send_message(
            partner,
            f"👋 *{user_profiles[uid]['name']}* said: Hi!",
            parse_mode='Markdown',
        )
    except Exception as e:
        print(f"SayHi send error: {e}")

    # Show confirmation in sender's chat
    try:
        await cb.message.edit_text(
            f"💬 *Chat started with {partner_name}*\n\n"
            "Send your messages below.\n\n"
            "👋 *You said: Hi!*",
            parse_mode='Markdown',
        )
    except Exception as e:
        print(f"SayHi edit error: {e}")
        # Fallback: send a new message if edit fails
        try:
            await bot.send_message(
                uid,
                f"👋 *You said: Hi!* to {partner_name}",
                parse_mode='Markdown',
            )
        except:
            pass


# ── Message relay (the core chat feature) ────────────────────────────────────
# NOTE: This MUST come after all FSM message handlers (which use StateFilter).
# Without StateFilter here, it catches all text including profile setup input.

@dp.message()
async def relay_message(message: types.Message, state: FSMContext):
    uid = message.from_user.id

    # Ignore if user is in the middle of profile setup (FSM state)
    current_state = await state.get_state()
    if current_state is not None:
        return  # Let the FSM handler deal with it

    # Check if this user is in an active chat
    if uid not in current_chat:
        # Not in chat — ignore or show help
        return

    partner = current_chat[uid]
    partner_name = user_profiles.get(partner, {}).get('name', 'Someone')
    sender_name = user_profiles.get(uid, {}).get('name', 'Someone')

    # Check text limit for the SENDER
    if not check_text_limit(uid):
        profile = user_profiles.get(uid, {})
        # Unverified females → verification popup instead of premium
        if profile.get('gender') in ('Women', 'Female') and not profile.get('verified'):
            await message.answer(
                "⚠️ You've reached your free limit for this match.\n\n"
                "📸 Verify your profile to continue chatting (free & unlimited).",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📸 Verify Now", callback_data='verify_now')],
                    [InlineKeyboardButton(text="👤 View Profile", callback_data='back_to_profile')],
                ]),
            )
        else:
            await message.answer(
                "⚠️ You've reached your free limit for this match.\n\n"
                "💎 Continue at just Rs49 — get unlimited texts & matches for a day!",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💎 Get 1 Day - Rs49", callback_data='buy_1day')],
                    [InlineKeyboardButton(text="📋 See More Plans", callback_data='see_more_plans')],
                    [InlineKeyboardButton(text="👤 View Profile", callback_data='back_to_profile')],
                ]),
            )
        return

    # Check if RECEIVER has reached their text limit — send teaser instead of actual message
    partner_profile = user_profiles.get(partner, {})
    if not check_text_limit(partner):
        increment_text_count(uid)  # Sender used one of their texts
        # Unverified female → verify teaser; male → premium teaser
        if partner_profile.get('gender') in ('Women', 'Female') and not partner_profile.get('verified'):
            try:
                await bot.send_message(
                    partner,
                    f"💬 *New message from {sender_name}*\n\n"
                    f"📸 Verify your profile to read & reply to messages (free & unlimited).",
                    parse_mode='Markdown',
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="📸 Verify Now", callback_data='verify_now')],
                    ]),
                )
            except Exception as e:
                print(f"Relay teaser error: {e}")
        else:
            try:
                await bot.send_message(
                    partner,
                    f"💬 *New message from {sender_name}*\n\n"
                    f"💎 Upgrade to premium to read & reply to all messages!",
                    parse_mode='Markdown',
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="💎 Get 1 Day - Rs49", callback_data='buy_1day')],
                        [InlineKeyboardButton(text="📋 See More Plans", callback_data='see_more_plans')],
                    ]),
                )
            except Exception as e:
                print(f"Relay teaser error: {e}")
        return

    try:
        # Copy the full message (preserves formatting, photos, etc.)
        await bot.copy_message(partner, message.chat.id, message.message_id)
        increment_text_count(uid)
        # Optional: show delivered checkmark
        # await message.react([types.ReactionTypeEmoji(emoji='✅')])
    except Exception as e:
        await message.answer(f"⚠️ Couldn't deliver your message. They may have blocked the bot or left the chat.")
        print(f"Relay error: {e}")


# ── Show more matches ──────────────────────────────────────────────────────────

_SHOW_MORE_CACHE: dict = {}

@dp.callback_query(lambda cb: cb.data == 'show_more_matches')
async def show_more_matches(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    me = user_profiles.get(uid, {})
    if not me:
        await cb.answer("⚠️ Profile not found. Please set up your profile first.", show_alert=True)
        return

    all_matches = find_matches({**me, '_uid': uid}, user_profiles)
    cached = _SHOW_MORE_CACHE.get(uid, 0)
    page = [all_matches[cached:cached+3]]
    _SHOW_MORE_CACHE[uid] = cached + 3

    remaining = len(all_matches) - cached
    if remaining <= 0:
        await cb.message.answer(
            "🎉 That's everyone nearby! Check back later or try expanding your preferences.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄  Search Again", callback_data='do_match')],
            ]),
        )
        await cb.answer()
        return

    batch = all_matches[cached:cached+3]
    lines = []
    for m in batch:
        lines.append(
            f"• *{m['name']}*, {m['age']} — 📍 {m['distance_km']} km\n"
            + (f"  _{m.get('bio', '')[:80]}_" if m.get('bio') else "")
        )

    kb_rows = []
    if remaining > 3:
        kb_rows.append([InlineKeyboardButton(text="⬇️  Show More", callback_data='show_more_matches')])
    kb_rows.append([InlineKeyboardButton(text="🔄  Search Again", callback_data='do_match')])

    await cb.message.answer(
        f"👥 *More matches* ({remaining} more nearby):\n\n" + "\n\n".join(lines),
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )
    await cb.answer()


# ── /chat command ─────────────────────────────────────────────────────────────

@dp.message(Command('chat'))
async def cmd_chat(message: types.Message):
    uid = message.from_user.id
    if uid not in user_profiles:
        await message.answer("📝 You haven't set up a profile yet.\n\nSend /start to begin!")
        return
    
    # Check if they have active matches
    if uid not in active_matches or not active_matches[uid]:
        await message.answer(
            "💬 *No matches yet.*\n\n"
            "Send /find to look for matches, or /profile to review your profile.",
            parse_mode='Markdown',
        )
        return
    
    # Show list of active matches they can chat with
    match_buttons = []
    for pid, _ in active_matches[uid].items():
        if pid in user_profiles:
            match_buttons.append(InlineKeyboardButton(
                text=f"💬 Chat with {user_profiles[pid]['name']}",
                callback_data=f"chat:{pid}",
            ))
    
    if not match_buttons:
        await message.answer(
            "💬 *No active matches found.*\n\nSend /find to look for matches!",
            parse_mode='Markdown',
        )
        return
    
    await message.answer(
        "💬 *Your Matches*\n\nTap to start chatting:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[match_buttons]),
    )


# ── /stop command (end current chat) ─────────────────────────────────────────

@dp.message(Command('stop'))
async def cmd_stop(message: types.Message):
    uid = message.from_user.id

    if uid not in current_chat:
        await message.answer("🔚 You're not in any chat to stop.")
        return

    partner = current_chat[uid]
    partner_name = user_profiles.get(partner, {}).get('name', 'Someone')

    # Clear both sides of the chat
    current_chat.pop(uid, None)
    current_chat.pop(partner, None)

    await message.answer("🔚 *Chat ended.*", parse_mode='Markdown')

    # Notify partner (if they still have a profile)
    if partner in user_profiles:
        await bot.send_message(
            partner,
            f"🔚 *{user_profiles[uid].get('name', 'Someone')} ended the chat.*",
            parse_mode='Markdown',
        )



@dp.message(Command('find'))
async def cmd_find(message: types.Message):
    """Start finding matches."""
    uid = message.from_user.id
    if uid not in user_profiles:
        await message.answer("📝 Set up your profile first with /start.")
        return
    # Simulate do_match by sending a message with the callback
    await message.answer(
        "❤️ Looking for matches?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❤️  Find Matches Now", callback_data='do_match')],
        ])
    )


# ── /profile command (show current profile) ───────────────────────────────────
# ── Profile command ──
@dp.message(Command('profile'))
async def cmd_profile(message: types.Message):
    uid = message.from_user.id
    if uid not in user_profiles:
        await message.answer("📝 You haven't set up a profile yet.\n\nSend /start to begin!")
        return
    data = user_profiles[uid]
    # Check for active matches to show chat button
    kb_buttons = []
    if uid in active_matches and active_matches[uid]:
        matches_info = []
        for pid, _ in active_matches[uid].items():
            if pid in user_profiles:
                matches_info.append(InlineKeyboardButton(
                    text=f"💬 Chat with {user_profiles[pid]['name']}",
                    callback_data=f"chat:{pid}",
                ))
        kb_buttons.append(matches_info)

    await message.answer(
        profile_summary(data) + "\n_Use the buttons below to edit any field._",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons) if kb_buttons else profile_kb(),
    )


# ── Error handler ─────────────────────────────────────────────────────────────

@dp.errors()
async def handle_errors(event: types.ErrorEvent):
    import traceback
    tb = ''.join(traceback.format_exception_only(type(event.exception), event.exception))
    try:
        await event.update.message.answer(f"⚠️ Bot error:\n`{tb[:200]}`", parse_mode='Markdown')
    except Exception:
        print(f"⚠️ Handler error: {tb[:200]}")
    print(f"⚠️ Bot error: {tb[:200]}")
    traceback.print_exc()

async def on_startup(dispatcher: Dispatcher):
    print("🚀 on_startup called, WEBHOOK_URL =", WEBHOOK_URL)
    if WEBHOOK_URL:
        await bot.set_webhook(WEBHOOK_URL)
        print(f"Webhook set to {WEBHOOK_URL}")
        from aiogram.webhook.aiohttp_server import SimpleRequestHandler
        app = web.Application()

        # Health check endpoint — Render health checker sends GET /health
        async def health(request):
            return web.Response(text='OK', status=200)

        app.router.add_get('/health', health)
        app.router.add_post('/health', health)

        handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
        handler.register(app, path='/')
        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.getenv('PORT', '8080'))
        site = web.TCPSite(runner, host='0.0.0.0', port=port)
        await site.start()
        print(f'✅ Webhook server running on port {port}')
        await asyncio.Event().wait()
    else:
        print('No WEBHOOK_URL – long‑polling mode')
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, skip_updates=False)


# ── Razorpay webhook handler ───────────────────────────────────────────────
# Track processed payment IDs to prevent duplicate activation
_processed_payments: set = set()

async def handle_razorpay_webhook(request: web.Request):
    """Handle Razorpay payment webhook (payment_link.paid / payment.captured)."""
    try:
        webhook_secret = RAZORPAY_WEBHOOK_SECRET
        if not webhook_secret:
            return web.Response(status=400, text="Webhook secret not configured")

        signature = request.headers.get('X-Razorpay-Signature')
        if not signature:
            return web.Response(status=400, text="Missing signature")

        body = await request.text()

        expected_signature = hmac.new(
            webhook_secret.encode('utf-8'),
            body.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        if signature != expected_signature:
            return web.Response(status=400, text="Invalid signature")

        data = json.loads(body)
        event = data.get('event', '')

        # Extract notes from payment_link.paid or payment.captured
        notes = {}
        pid = None
        if event == 'payment_link.paid':
            entity = data.get('payload', {}).get('payment_link', {}).get('entity', {})
            notes = entity.get('notes', {})
            pid = entity.get('id')
        elif event == 'payment.captured':
            entity = data.get('payload', {}).get('payment', {}).get('entity', {})
            notes = entity.get('notes', {})
            pid = entity.get('id')

        # If no notes from event level, try order notes
        if not notes:
            order_entity = data.get('payload', {}).get('order', {}).get('entity', {})
            notes = order_entity.get('notes', {})

        uid_str = notes.get('uid')
        duration_str = notes.get('duration_days')

        if uid_str and duration_str and pid:
            if pid in _processed_payments:
                return web.Response(status=200, text="Already processed")

            uid = int(uid_str)
            duration_days = int(duration_str)
            expiry = datetime.now() + timedelta(days=duration_days)
            premium_subscriptions[uid] = {'expiry_date': expiry}

            if uid in user_usage:
                del user_usage[uid]

            _processed_payments.add(pid)
            print(f"💎 Premium activated for user {uid}, plan={duration_days}d, until {expiry}")
        else:
            print(f"Webhook event={event} missing uid/duration in notes: {notes}")

        return web.Response(status=200, text="Webhook processed successfully")

    except Exception as e:
        print(f"Webhook error: {e}")
        return web.Response(status=500, text=f"Webhook error: {str(e)}")


async def payment_success_page(request: web.Request):
    """Simple payment success page shown after Razorpay checkout."""
    return web.Response(
        content_type='text/html',
        text="""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Payment Successful - Winkly</title>
<style>body{font-family:sans-serif;background:#1a1a2e;color:#eee;text-align:center;padding:40px 20px}
.card{background:#16213e;border-radius:16px;padding:32px;max-width:400px;margin:0 auto}
.check{font-size:64px;margin-bottom:16px}
.btn{background:#e94560;color:#fff;border:none;border-radius:8px;padding:14px 28px;font-size:16px;cursor:pointer;text-decoration:none;display:inline-block;margin-top:16px}
</style></head><body>
<div class="card"><div class="check">✅</div>
<h1>Payment Successful!</h1>
<p>Your Winkly premium subscription is now active.</p>
<p>Return to Telegram to start matching.</p>
<a class="btn" href="https://t.me/winklybot">Open Telegram</a>
</div></body></html>"""
    )


async def auto_setup_razorpay_webhook():
    """Auto-create Razorpay webhook via API if not already configured."""
    if not razorpay_client:
        print("⚠️ Razorpay client unavailable — skip auto webhook setup")
        return
    try:
        existing = await asyncio.to_thread(lambda: razorpay_client.webhook.all())
        target_url = f"{WEBHOOK_URL}/razorpay/webhook"
        for wh in existing.get('items', []):
            if wh.get('url') == target_url:
                print(f"✅ Razorpay webhook already exists: {wh.get('id')}")
                return
        resp = await asyncio.to_thread(
            lambda: razorpay_client.webhook.create({
                "url": target_url,
                "events": ["payment_link.paid", "payment.captured"],
                "secret": RAZORPAY_WEBHOOK_SECRET,
                "active": True,
            })
        )
        print(f"✅ Razorpay webhook auto-created: {resp.get('id')}")
    except Exception as e:
        print(f"⚠️ Auto webhook setup failed ({e})")
        print(f"   Create manually: Razorpay Dashboard → Settings → Webhooks")
        print(f"   URL: {WEBHOOK_URL}/razorpay/webhook")
        print(f"   Events: payment_link.paid, payment.captured")
        print(f"   Secret: {RAZORPAY_WEBHOOK_SECRET}")


async def on_startup(dispatcher: Dispatcher):
    print("🚀 on_startup called, WEBHOOK_URL =", WEBHOOK_URL)
    
    # Register bot commands for the menu button (next to input field)
    commands = [
        BotCommand(command="start", description="🏠 Start / Restart"),
        BotCommand(command="stop", description="🔚 End current chat"),
        BotCommand(command="profile", description="👤 View my profile"),
        BotCommand(command="find", description="❤️ Find matches"),
        BotCommand(command="verify", description="🏅 Get verified badge"),
        BotCommand(command="premium", description="💎 Premium plans"),
    ]
    try:
        await bot.set_my_commands(commands)
        print("✅ Bot commands registered")
    except Exception as e:
        print(f"⚠️ Failed to set commands: {e}")
    
    if WEBHOOK_URL:
        await bot.set_webhook(WEBHOOK_URL)
        print(f"Webhook set to {WEBHOOK_URL}")
        await auto_setup_razorpay_webhook()

        from aiogram.webhook.aiohttp_server import SimpleRequestHandler
        handler = SimpleRequestHandler(dispatcher=dispatcher, bot=bot)

        # Main app — serves health, Razorpay webhook, payment success, and bot webhook
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
        port = int(os.getenv('PORT', '8080'))
        site = web.TCPSite(runner, host='0.0.0.0', port=port)
        await site.start()
        print(f'✅ Main webhook server running on port {port}')
        await asyncio.Event().wait()
    else:
        print('No WEBHOOK_URL – long‑polling mode')
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, skip_updates=False)


if __name__ == '__main__':
    asyncio.run(on_startup(dp))