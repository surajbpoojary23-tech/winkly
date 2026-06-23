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
    name     = State()
    age       = State()
    gender    = State()
    bio       = State()
    location  = State()
    confirm   = State()   # final review screen


# ── In-memory profile store ───────────────────────────────────────────────────
# {user_id: {"name":…, "age":…, "gender":…, "bio":…, "lat":…, "lon":…}}
user_profiles: dict = {}

PROGRESS_STEPS = ["name", "age", "gender", "bio", "location"]

STEP_LABELS = {
    "name":    "📛  Name",
    "age":     "🎂  Age",
    "gender":  "⚧  Gender",
    "bio":     "📝  Bio",
    "location":"📍  Location",
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
        f"📛  Name:    {data.get('name', '—')}\n"
        f"🎂  Age:     {data.get('age', '—')}\n"
        f"⚧  Gender:  {data.get('gender', '—')}\n"
        f"📝  Bio:     {data.get('bio', '—')}\n"
        f"📍  Location: {_lat_lon(data)}\n"
    )


def _lat_lon(data: dict) -> str:
    lat = data.get('lat')
    lon = data.get('lon')
    if lat and lon:
        return f"{float(lat):.4f}, {float(lon):.4f}"
    return "—"


def profile_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️  Edit Name",    callback_data='edit_name'),
         InlineKeyboardButton(text="✏️  Edit Age",    callback_data='edit_age')],
        [InlineKeyboardButton(text="✏️  Edit Gender", callback_data='edit_gender'),
         InlineKeyboardButton(text="✏️  Edit Bio",   callback_data='edit_bio')],
        [InlineKeyboardButton(text="✏️  Edit Location", callback_data='edit_location')],
        [InlineKeyboardButton(text="❤️  Find Matches Now", callback_data='do_match')],
    ])


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« Back", callback_data='back')],
        [InlineKeyboardButton(text="❌ Cancel Setup", callback_data='cancel_setup')],
    ])


# ── /start ─────────────────────────────────────────────────────────────────────

@dp.message(Command('start'))
async def cmd_start(message: types.Message):
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
    await message.answer(
        "👋 Hey! I'm *Winkly*.\n\n"
        "I'll help you find people nearby. Let's set up your profile — "
        "it only takes ~30 seconds.\n\n"
        f"_{progress_bar(0)}_  Step 1 of 5\n\n"
        "📛 *What's your name?*",
        parse_mode='Markdown',
    )
    await dp.current_state(chat=message.chat.id, user=uid).set_state(Setup.name)


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
        f"_{progress_bar(idx - 1)}_  Step {idx} of 5\n\n"
        f"Go back — {prev_label}?\n\n_Enter your answer below._",
        parse_mode='Markdown',
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
        f"_{progress_bar(0)}_  Step 1 of 5\n\n"
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
    await state.set_state(getattr(Setup, field))
    idx = PROGRESS_STEPS.index(field)

    prompts = {
        "name":    "📛 *What's your name?*",
        "age":     "🎂 *How old are you?* _(number only)_",
        "gender":  "⚧ *What's your gender?*",
        "bio":     "📝 *Tell us about yourself:*\n_(hobbies, what you like, what you're looking for…)_",
        "location":"📍 *Share your location* so we can find matches nearby:",
    }
    await cb.message.edit_text(
        f"_{progress_bar(idx)}_  Step {idx + 1} of 5\n\n"
        f"✏️  {prompts.get(field, 'Enter:')}",
        parse_mode='Markdown',
        reply_markup=back_kb(),
    )
    await cb.answer()


# ── Name ───────────────────────────────────────────────────────────────────────

@dp.message(StateFilter(Setup.name))
async def handle_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if len(name) < 2:
        await message.answer("⚠️ Name must be at least 2 characters. Try again:")
        return
    await state.update_data(name=name)
    await message.answer(f"📛 *{name}* — got it!")
    await advance_to(state, Setup.age, message.chat.id, message.from_user.id)


# ── Age ───────────────────────────────────────────────────────────────────────

@dp.message(StateFilter(Setup.age))
async def handle_age(message: types.Message, state: FSMContext):
    try:
        age = int(message.text.strip())
    except ValueError:
        await message.answer("⚠️ Enter a number, e.g. 28. Try again:")
        return
    if not (18 <= age <= 100):
        await message.answer("⚠️ You must be between 18 and 100. Try again:")
        return
    await state.update_data(age=str(age))
    await message.answer(f"🎂 *{age}* — perfect!")
    await advance_to(state, Setup.gender, message.chat.id, message.from_user.id)


# ── Gender ────────────────────────────────────────────────────────────────────

@dp.message(StateFilter(Setup.gender))
async def handle_gender(message: types.Message, state: FSMContext):
    raw = message.text.strip().lower()
    gender_map = {
        'male': 'Male', 'm': 'Male', '👨': 'Male',
        'female': 'Female', 'f': 'Female', '👩': 'Female',
        'other': 'Other', '⚧': 'Other',
    }
    gender = gender_map.get(raw)
    if not gender:
        await message.answer(
            "⚠️ Please tap one of the buttons or type: Male / Female / Other",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text='👨 Male'), KeyboardButton(text='👩 Female'), KeyboardButton(text='⚧ Other')]],
                resize_keyboard=True, one_time_keyboard=True,
            ),
        )
        return
    await state.update_data(gender=gender)
    await message.answer(f"⚧ *{gender}* — noted!", reply_markup=ReplyKeyboardRemove())
    await advance_to(state, Setup.bio, message.chat.id, message.from_user.id)


@dp.callback_query(lambda cb: cb.data.startswith('gender_'), StateFilter(Setup.gender))
async def handle_gender_btn(cb: types.CallbackQuery, state: FSMContext):
    raw = cb.data.replace('gender_', '').lower()
    gender_map = {'male': 'Male', 'female': 'Female', 'other': 'Other', 'm': 'Male', 'f': 'Female'}
    gender = gender_map.get(raw, raw.title())
    await state.update_data(gender=gender)
    await cb.message.edit_text(f"⚧ *{gender}* — noted!")
    await cb.answer()
    await advance_to(state, Setup.bio, cb.message.chat.id, cb.from_user.id)


# ── Bio ───────────────────────────────────────────────────────────────────────

@dp.message(StateFilter(Setup.bio))
async def handle_bio(message: types.Message, state: FSMContext):
    bio = message.text.strip()
    if len(bio) < 10:
        await message.answer("⚠️ Please write at least a sentence or two:")
        return
    await state.update_data(bio=bio)
    await message.answer("📝 *Bio saved!*")
    await advance_to(state, Setup.location, message.chat.id, message.from_user.id)


@dp.callback_query(lambda cb: cb.data == 'skip_bio', StateFilter(Setup.bio))
async def skip_bio(cb: types.CallbackQuery, state: FSMContext):
    await state.update_data(bio="")
    await cb.message.edit_text("📝 *Bio skipped.*")
    await cb.answer()
    await advance_to(state, Setup.location, cb.message.chat.id, cb.from_user.id)


# ── Location ──────────────────────────────────────────────────────────────────

@dp.message(StateFilter(Setup.location))
async def handle_location_text(message: types.Message, state: FSMContext):
    await message.answer(
        "📍 Please use the *Share Location* button below:",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text='📍 Share Location', request_location=True)]],
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
    # Advance to confirm screen
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
            f"_{progress_bar(idx)}_  Step {idx + 1} of 5\n\n"
            "🎂 *How old are you?*\n_(just the number, e.g. 28)_",
            parse_mode='Markdown',
            reply_markup=back_kb(),
        )
    elif step == 'gender':
        idx = 2
        await bot.send_message(
            chat_id,
            f"_{progress_bar(idx)}_  Step {idx + 1} of 5\n\n"
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
            f"_{progress_bar(idx)}_  Step {idx + 1} of 5\n\n"
            "📝 *Tell us a bit about yourself*\n"
            "_(hobbies, what you like, what you're looking for…)_\n\n"
            "_Or tap /skip to skip this step._",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⏭️  Skip Bio", callback_data='skip_bio')],
                [InlineKeyboardButton(text="« Back", callback_data='back')],
            ]),
        )
    elif step == 'location':
        idx = 4
        await bot.send_message(
            chat_id,
            f"_{progress_bar(idx)}_  Step {idx + 1} of 5\n\n"
            "📍 *Share your location* so we can find matches near you:",
            parse_mode='Markdown',
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text='📍 Share My Location', request_location=True)]],
                resize_keyboard=True, one_time_keyboard=True,
            ),
        )
    elif step == 'confirm':
        data = await state.get_data()
        uid  = user_id
        user_profiles[uid] = data
        await state.clear()

        await bot.send_message(
            chat_id,
            "🎉 *Profile complete!*\n\n" + profile_summary(data) +
            "\nDoes everything look right?",
            parse_mode='Markdown',
            reply_markup=profile_kb(),
        )


# ── Review screen interactions ─────────────────────────────────────────────────

@dp.callback_query(lambda cb: cb.data == 'do_match')
async def do_match(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await cb.message.edit_reply_markup(reply_markup=None)

    if uid not in user_profiles:
        await cb.message.answer("⚠️ No profile found. Sending /start to set one up.")
        await cb.answer()
        return

    p = user_profiles[uid]
    await cb.message.answer(
        f"🔎 Looking for matches near *{p['name']}*…\n\n"
        f"📍 Location: {_lat_lon(p)}\n\n"
        "_Match‑finding logic coming soon!_",
        parse_mode='Markdown',
    )
    await cb.answer()


# ── /profile command (show current profile) ────────────────────────────────────

@dp.message(Command('profile'))
async def cmd_profile(message: types.Message):
    uid = message.from_user.id
    if uid not in user_profiles:
        await message.answer("📝 You haven't set up a profile yet.\n\nSend /start to begin!")
        return
    data = user_profiles[uid]
    await message.answer(
        profile_summary(data) + "\n_Use the buttons below to edit any field._",
        parse_mode='Markdown',
        reply_markup=profile_kb(),
    )


# ── Webhook ───────────────────────────────────────────────────────────────────

async def on_startup(_: Dispatcher):
    if WEBHOOK_URL:
        await bot.set_webhook(WEBHOOK_URL)
        print(f"Webhook set to {WEBHOOK_URL}")
        from aiogram.webhook.aiohttp_server import SimpleRequestHandler
        handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
        app = web.Application()
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