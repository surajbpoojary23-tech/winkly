import os
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
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
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '.env'))
from aiohttp import web

BOT_TOKEN = os.getenv('BOT_TOKEN') or "8624196108:***"
REDIS_URL  = os.getenv('REDIS_URL')
WEBHOOK_URL = os.getenv('WEBHOOK_URL') or 'https://winkly-kmsz.onrender.com'

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
# {user_id: {"name":…, "age":…, "gender":…, "bio":…, "lat":…, "lon":…, "likes": set(), "rejected": set()}}
user_profiles: dict = {}

# ── Match & Chat stores ─────────────────────────────────────────────────────
# likes_sent: {liker_id: set(liked_ids)} — legacy, kept for compatibility
likes_sent: dict[int, set] = {}
# matches: {user_id: {partner_id: match_data}}
active_matches: dict[int, dict[int, dict]] = {}
# current_chat: {user_id: partner_id} — tracks who each user is chatting with
current_chat: dict[int, int] = {}

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
    return (
        "✅ *Your Profile*\n\n"
        f"📛  Name:       {data.get('name', '—')}\n"
        f"🎂  Age:        {data.get('age', '—')}\n"
        f"⚧  Gender:     {data.get('gender', '—')}\n"
        f"📝  Bio:        {data.get('bio', '—') or '—'}\n"
        f"❤️  Interested: {data.get('preferred_gender', '—')}\n"
        f"📍  Location:   {_lat_lon(data)}\n"
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
    await message.answer(
        "👋 Hey! I'm *Winkly*.\n\n"
        "I'll help you find people nearby. Let's set up your profile — "
        "it only takes ~30 seconds.\n\n"
        f"_{progress_bar(0)}_  Step 1 of {TOTAL_STEPS}\n\n"
        "📛 *What's your name?*",
        parse_mode='Markdown',
    )


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
    try:
        name = message.text.strip()
        if len(name) < 2:
            await message.answer("⚠️ Name must be at least 2 characters. Please enter a longer name:")
            return
        await state.update_data(name=name)
        await message.answer(f"📛 *{name}* — got it!")

        data = await state.get_data()
        if data.get('edit_mode'):
            await state.update_data(edit_mode=False)
            await advance_to(state, Setup.confirm, message.chat.id, message.from_user.id)
        else:
            await advance_to(state, Setup.age, message.chat.id, message.from_user.id)
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
    await message.answer(f"🎂 *{age}* years old — perfect!")

    data = await state.get_data()
    if data.get('edit_mode'):
        await state.update_data(edit_mode=False)
        await advance_to(state, Setup.confirm, message.chat.id, message.from_user.id)
    else:
        await advance_to(state, Setup.gender, message.chat.id, message.from_user.id)


# ── Gender ────────────────────────────────────────────────────────────────────

@dp.message(StateFilter(Setup.gender))
async def handle_gender(message: types.Message, state: FSMContext):
    raw = message.text.strip().lower()
    # Try exact match first (full button text like "👨 male"), then fall back to individual emoji
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
    await message.answer(f"⚧ *{gender}* — noted!", reply_markup=ReplyKeyboardRemove())

    data = await state.get_data()
    if data.get('edit_mode'):
        await state.update_data(edit_mode=False)
        await advance_to(state, Setup.confirm, message.chat.id, message.from_user.id)
    else:
        await advance_to(state, Setup.bio, message.chat.id, message.from_user.id)


@dp.callback_query(lambda cb: cb.data.startswith('gender_'), StateFilter(Setup.gender))
async def handle_gender_btn(cb: types.CallbackQuery, state: FSMContext):
    raw = cb.data.replace('gender_', '').lower()
    gender_map = {'male': 'Male', 'female': 'Female', 'other': 'Other', 'm': 'Male', 'f': 'Female'}
    gender = gender_map.get(raw, raw.title())
    await state.update_data(gender=gender)
    await cb.message.edit_text(f"⚧ *{gender}* — noted!")
    await cb.answer()

    data = await state.get_data()
    if data.get('edit_mode'):
        await advance_to(state, Setup.confirm, cb.message.chat.id, cb.from_user.id)
    else:
        await advance_to(state, Setup.bio, cb.message.chat.id, cb.from_user.id)


# ── Bio ───────────────────────────────────────────────────────────────────────

@dp.message(StateFilter(Setup.bio))
async def handle_bio(message: types.Message, state: FSMContext):
    raw = message.text.strip()
    
    # Allow /skip or "skip" as text command
    if raw.lower() in ('/skip', 'skip'):
        await state.update_data(bio="")
        await message.answer("📝 *Bio skipped.*")
        data = await state.get_data()
        if data.get('edit_mode'):
            await state.update_data(edit_mode=False)
            await advance_to(state, Setup.confirm, message.chat.id, message.from_user.id)
        else:
            await advance_to(state, Setup.preferred_gender, message.chat.id, message.from_user.id)
        return
    
    if len(raw) < 10:
        await message.answer("⚠️ Please write at least a sentence or two about yourself (or type /skip to skip):")
        return
    await state.update_data(bio=raw)
    await message.answer("📝 *Bio saved!*")

    data = await state.get_data()
    if data.get('edit_mode'):
        await state.update_data(edit_mode=False)
        await advance_to(state, Setup.confirm, message.chat.id, message.from_user.id)
    else:
        await advance_to(state, Setup.preferred_gender, message.chat.id, message.from_user.id)


@dp.callback_query(lambda cb: cb.data == 'skip_bio', StateFilter(Setup.bio))
async def skip_bio(cb: types.CallbackQuery, state: FSMContext):
    await state.update_data(bio="")
    await cb.message.edit_text("📝 *Bio skipped.*")
    await cb.answer()

    data = await state.get_data()
    if data.get('edit_mode'):
        await state.update_data(edit_mode=False)
        await advance_to(state, Setup.confirm, cb.message.chat.id, cb.from_user.id)
    else:
        await advance_to(state, Setup.preferred_gender, cb.message.chat.id, cb.from_user.id)


# ── Preferred Gender ─────────────────────────────────────────────────────────

@dp.message(StateFilter(Setup.preferred_gender))
async def handle_preferred_gender(message: types.Message, state: FSMContext):
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
    await message.answer(
        f"❤️ *{pref}* — noted!",
        reply_markup=ReplyKeyboardRemove(),
    )

    data = await state.get_data()
    if data.get('edit_mode'):
        await state.update_data(edit_mode=False)
        await advance_to(state, Setup.confirm, message.chat.id, message.from_user.id)
    else:
        await advance_to(state, Setup.location, message.chat.id, message.from_user.id)


# ── Location ──────────────────────────────────────────────────────────────────

import aiohttp

async def geocode_place(place_name: str):
    """Convert place name to lat/lon using OpenStreetMap Nominatim (free, no API key)."""
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": place_name, "format": "json", "limit": 1}
    headers = {"User-Agent": "WinklyBot/1.0"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, headers=headers, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data:
                    return float(data[0]["lat"]), float(data[0]["lon"])
    return None, None


@dp.message(lambda m: not m.location, StateFilter(Setup.location))
async def handle_location_text(message: types.Message, state: FSMContext):
    text = message.text.strip()
    
    # Try to geocode the place name
    lat, lon = await geocode_place(text)
    if lat and lon:
        await state.update_data(lat=str(lat), lon=str(lon))
        await message.answer(
            f"📍 *Location found!*\n_{lat:.5f}, {lon:.5f}_",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode='Markdown',
        )
        await advance_to(state, Setup.confirm, message.chat.id, message.from_user.id)
        return

    # If geocoding fails, show options
    await message.answer(
        "📍 Couldn't find that place. Try again, or use the buttons below:",
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
    loc = message.location
    await state.update_data(lat=str(loc.latitude), lon=str(loc.longitude))
    await message.answer(
        f"📍 *Location saved!*\n_{loc.latitude:.5f}, {loc.longitude:.5f}_",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode='Markdown',
    )
    await advance_to(state, Setup.confirm, message.chat.id, message.from_user.id)


# ── Confirm / review screen ─────────────────────────────────────────────────────

async def advance_to(state: FSMContext, next_state: State, chat_id: int, user_id: int):
    """Set next state and send the appropriate prompt."""
    await state.set_state(next_state)
    step = next_state.state.split(':')[-1]

    if step == 'age':
        idx = 1
        await bot.send_message(
            chat_id,
            f"_{progress_bar(idx)}_  Step {idx + 1} of {TOTAL_STEPS}\n\n"
            "🎂 *When were you born?*\n_(DD / MM / YYYY — e.g. 15 / 08 / 1995)_",
            parse_mode='Markdown',
        )
    elif step == 'gender':
        idx = 2
        await bot.send_message(
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
        await bot.send_message(
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
        await bot.send_message(
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
        await bot.send_message(
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
        # Merge: existing profile fields stay, new data (edit_mode stripped) overrides
        existing = user_profiles.get(uid, {})
        merged = _clean({**existing, **new_data})
        user_profiles[uid] = merged
        await state.clear()

        await bot.send_message(
            chat_id,
            "🎉 *Profile complete!*\n\n" + profile_summary(merged) +
            "\nDoes everything look right?",
            parse_mode='Markdown',
            reply_markup=profile_kb(),
        )


# ── Review screen interactions ─────────────────────────────────────────────────

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
        
        # Remove from waiting queue
        if uid in waiting_queue:
            del waiting_queue[uid]
        if partner in waiting_queue:
            del waiting_queue[partner]
        
        # Notify both users of instant match
        await bot.send_message(
            uid,
            f"🎉 *It's a Match!*\n\n"
            f"You and *{partner_name}* are compatible!\n\n"
            f"📛  {partner_name}\n"
            f"🎂  {waiting_match['age']}  |  ⚧ {waiting_match['gender']}\n"
            f"📝  {waiting_match.get('bio', '') or '—'}\n\n"
            "Tap below to start chatting:",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💬  Chat Now", callback_data=f'chat:{partner}')],
            ]),
        )
        
        await bot.send_message(
            partner,
            f"🎉 *It's a Match!*\n\n"
            f"You and *{me['name']}* are compatible!\n\n"
            f"📛  {me['name']}\n"
            f"🎂  {me['age']}  |  ⚧ {me['gender']}\n"
            f"📝  {me.get('bio', '') or '—'}\n\n"
            "Tap below to start chatting:",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💬  Chat Now", callback_data=f'chat:{uid}')],
            ]),
        )
        
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
        "This usually takes 5-30 seconds. You can tap 'Skip' to cancel.\n\n"
        "⏳ *Waiting in queue...*",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌  Skip", callback_data='skip_waiting')],
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
            
            # Remove from waiting queue
            if uid in waiting_queue:
                del waiting_queue[uid]
            if partner in waiting_queue:
                del waiting_queue[partner]
            
            # Update the searching message
            try:
                await bot.edit_message_text(
                    chat_id=cb.message.chat.id,
                    message_id=search_msg.message_id,
                    text=f"🎉 *{me['name']}*, you matched with *{partner_name}*!\n\n"
                         f"📛  {partner_name}\n"
                         f"🎂  {waiting_match['age']}  |  ⚧ {waiting_match['gender']}\n"
                         f"📝  {waiting_match.get('bio', '') or '—'}\n\n"
                         "Tap below to start chatting:",
                    parse_mode='Markdown',
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="💬  Chat Now", callback_data=f'chat:{partner}')],
                    ]),
                )
            except:
                pass
            
            # Notify the partner as well
            try:
                await bot.send_message(
                    partner,
                    f"🎉 *It's a Match!*\n\n"
                    f"You and *{me['name']}* are compatible!\n\n"
                    f"📛  {me['name']}\n"
                    f"🎂  {me['age']}  |  ⚧ {me['gender']}\n"
                    f"📝  {me.get('bio', '') or '—'}\n\n"
                    "Tap below to start chatting:",
                    parse_mode='Markdown',
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="💬  Chat Now", callback_data=f'chat:{uid}')],
                    ]),
                )
            except:
                pass
        else:
            # Still waiting - schedule another check
            if uid in waiting_queue:
                asyncio.create_task(check_for_match())
    
    # Start the background check
    asyncio.create_task(check_for_match())
    await cb.answer()


@dp.callback_query(lambda cb: cb.data == 'skip_waiting')
async def skip_waiting(cb: types.CallbackQuery):
    """Skip the waiting queue and return to profile."""
    uid = cb.from_user.id
    
    if uid in waiting_queue:
        del waiting_queue[uid]
        if uid in user_profiles:
            await cb.message.answer(
                "❌ *Search cancelled.*\n\n"
                "You can tap 'Find Matches' again to start searching.\n"
                "Your profile is saved and ready.",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔄  Find Matches", callback_data='do_match')],
                    [InlineKeyboardButton(text="👤  View Profile", callback_data='back_to_profile')],
                ]),
            )
        else:
            await cb.message.answer("❌ Search cancelled.")
    else:
        await cb.answer("Not currently waiting.")


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
    await message.answer(
        f"👤 *{m['name']}*\n"
        f"🎂  Age: {m['age']}  |  ⚧ {m['gender']}\n"
        f"📍  {m['distance_km']} km away{bio_line}",
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

        await bot.send_message(
            liker,
            f"🎉 *It's a Match!*\n\n"
            f"You and *{_R['name']}* liked each other!\n\n"
            f"📛  {_R['name']}\n"
            f"🎂  {_R['age']}  |  ⚧ {_R['gender']}\n"
            f"📝  {_R.get('bio', '') or '—'}\n\n"
            "Tap below to start chatting:",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💬  Chat Now", callback_data=f'chat:{liked}')],
            ]),
        )

        await bot.send_message(
            liked,
            f"🎉 *It's a Match!*\n\n"
            f"You and *{_L['name']}* liked each other!\n\n"
            f"📛  {_L['name']}\n"
            f"🎂  {_L['age']}  |  ⚧ {_L['gender']}\n"
            f"📝  {_L.get('bio', '') or '—'}\n\n"
            "Tap below to start chatting:",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💬  Chat Now", callback_data=f'chat:{liker}')],
            ]),
        )
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

    partner_name = user_profiles[partner]['name']
    await cb.message.edit_text(
        f"💬 *Chat started with {partner_name}*\n\n"
        "Send your messages below. You can tap 'Skip' to end this chat early or 'End Chat' to finish.\n\n"
        "💡 *Pro tip:* Type 'skip' anytime to find someone new!",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭️  Skip", callback_data=f'skip_chat:{partner}')],
            [InlineKeyboardButton(text="🔚 End Chat", callback_data=f'end_chat:{partner}')],
        ]),
    )

    # Also tell the partner
    await bot.send_message(
        partner,
        f"💬 *{user_profiles[uid]['name']}* started chatting!\n\n"
        "You can tap 'Skip' to end this chat early or 'End Chat' to finish.",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭️  Skip", callback_data=f'skip_chat:{uid}')],
            [InlineKeyboardButton(text="🔚 End Chat", callback_data=f'end_chat:{uid}')],
        ]),
    )
    await cb.answer()


@dp.callback_query(lambda cb: cb.data.startswith('skip_chat:'))
async def skip_chat(cb: types.CallbackQuery):
    """Skip the current chat and return to profile."""
    uid = cb.from_user.id
    partner = int(cb.data.split(':')[1])

    # Remove from active matches
    if uid in active_matches and partner in active_matches[uid]:
        del active_matches[uid][partner]
    if partner in active_matches and uid in active_matches[partner]:
        del active_matches[partner][uid]
    
    # Remove from chat tracking
    if uid in current_chat:
        del current_chat[uid]
    if partner in current_chat:
        del current_chat[partner]
    
    # Notify both users
    await cb.message.edit_text(
        "⏭️ *Chat skipped.*\n\n"
        "You can start searching for someone new.\n\n"
        "Tap below to find matches:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄  Find Matches", callback_data='do_match')],
            [InlineKeyboardButton(text="👤  View Profile", callback_data='back_to_profile')],
        ]),
    )
    
    try:
        await bot.send_message(
            partner,
            f"⏭️ *{user_profiles[uid]['name']}* ended the chat early.\n\n"
            "You can start searching for someone new.",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄  Find Matches", callback_data='do_match')],
            ]),
        )
    except:
        pass
    
    await cb.answer("Chat skipped")


@dp.callback_query(lambda cb: cb.data.startswith('end_chat:'))
async def end_chat_handler(cb: types.CallbackQuery):
    uid = cb.from_user.id
    partner = int(cb.data.split(':')[1])

    # Remove from chat tracking
    if uid in current_chat:
        del current_chat[uid]
    if partner in current_chat:
        del current_chat[partner]

    await cb.message.edit_text("🔚 *Chat ended.*\n\n"
                               "You can find someone new or view your profile.",
                               parse_mode='Markdown',
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                   [InlineKeyboardButton(text="🔄  Find Matches", callback_data='do_match')],
                                   [InlineKeyboardButton(text="👤  View Profile", callback_data='back_to_profile')],
                               ]))

    # Also tell the partner
    try:
        await bot.send_message(
            partner,
            f"🔚 *{user_profiles[uid]['name']}* ended the chat.\n\n"
            "You can find someone new.",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄  Find Matches", callback_data='do_match')],
            ]),
        )
    except:
        pass
    
    await cb.answer()


@dp.callback_query(lambda cb: cb.data.startswith('end_chat:'))
async def end_chat_handler(cb: types.CallbackQuery):
    uid = cb.from_user.id
    partner = int(cb.data.split(':')[1])

    # Remove chat tracking
    current_chat.pop(uid, None)
    current_chat.pop(partner, None)

    await cb.message.edit_text("🔚 *Chat ended.*")
    await bot.send_message(partner, f"🔚 *{user_profiles[uid]['name']} ended the chat.*")
    await cb.answer()


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

    try:
        # Copy the full message (preserves formatting, photos, etc.)
        await bot.copy_message(partner, message.chat.id, message.message_id)
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


if __name__ == '__main__':
    asyncio.run(on_startup(dp))