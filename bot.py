import os
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, StateFilter
from magic_filter import F
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
REDIS_URL = os.getenv('REDIS_URL')

WEBHOOK_URL = os.getenv('WEBHOOK_URL') or 'https://winkly-kmsz.onrender.com'

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot=bot, storage=storage)


# ── FSM States ────────────────────────────────────────────────────────────────

class ProfileSetup(StatesGroup):
    waiting_name = State()
    waiting_age = State()
    waiting_gender = State()
    waiting_bio = State()
    waiting_location = State()


# ── Helpers ───────────────────────────────────────────────────────────────────

async def clear_reply_markup(message: types.Message):
    """Remove the inline keyboard from the last bot message."""
    try:
        await bot.edit_message_reply_markup(
            chat_id=message.chat.id,
            message_id=message.message_id - 1,
            reply_markup=None,
        )
    except Exception:
        pass


def confirm_kb() -> InlineKeyboardMarkup:
    """Yes / No confirmation keyboard."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text='✅ Yes, confirm', callback_data='confirm_yes'),
                InlineKeyboardButton(text='✏️  Edit', callback_data='confirm_no'),
            ]
        ]
    )


# ── Profile data store (in-memory for demo; swap for Redis/SQLite in prod) ──
# Format: {user_id: {"name":…, "age":…, "gender":…, "bio":…, "lat":…, "lon":…}}
user_profiles: dict = {}

PROFILE_QUESTIONS = {
    "name": "📛 What should we call you?\n\n_(e.g. Alex)_",
    "age": "🎂 How old are you?\n\n_(enter a number, e.g. 28)_",
    "gender": "⚧ What's your gender?\n\n_Tap or type:_",
    "bio": "📝 Tell us a little about yourself:\n\n_(hobbies, interests, what you're looking for…)_",
    "location": "📍 Share your location so we can find people near you:\n\n_Tap the button below_",
}


# ── /start ────────────────────────────────────────────────────────────────────

@dp.message(Command('start'))
async def cmd_start(message: types.Message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='👤 Set up Profile', callback_data='profile_start')],
            [InlineKeyboardButton(text='❤️  Find a Match', callback_data='find_match')],
        ]
    )
    await message.answer(
        "👋 Hey! I'm *Winkly*, your quick‑match dating bot.\n\n"
        "Set up your profile first, then I'll find people near you!",
        parse_mode='Markdown',
        reply_markup=keyboard,
    )


# ── Profile Setup ─────────────────────────────────────────────────────────────

@dp.callback_query(F.data == 'profile_start', StateFilter(None))
async def profile_start(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer("👤 Let's set up your profile!\n\nI'll ask you a few quick questions.")
    await ask_name(cb.message, state)
    await cb.answer()


async def ask_name(message: types.Message, state: FSMContext):
    await state.set_state(ProfileSetup.waiting_name)
    await message.answer(PROFILE_QUESTIONS["name"])


async def ask_age(message: types.Message, state: FSMContext):
    await state.set_state(ProfileSetup.waiting_age)
    await message.answer(PROFILE_QUESTIONS["age"])


async def ask_gender(message: types.Message, state: FSMContext):
    await state.set_state(ProfileSetup.waiting_gender)
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text='👨 Male')],
            [KeyboardButton(text='👩 Female')],
            [KeyboardButton(text='⚧ Other')],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer(PROFILE_QUESTIONS["gender"], reply_markup=kb)


async def ask_bio(message: types.Message, state: FSMContext):
    await state.set_state(ProfileSetup.waiting_bio)
    # Remove the reply keyboard
    await message.answer("👩 What's your gender? → *Confirmed* ✅", reply_markup=ReplyKeyboardRemove())
    await message.answer(PROFILE_QUESTIONS["bio"])


async def ask_location(message: types.Message, state: FSMContext):
    await state.set_state(ProfileSetup.waiting_location)
    # Remove the reply keyboard
    await message.answer("📝 Bio → *Confirmed* ✅")
    await message.answer(PROFILE_QUESTIONS["location"],
                          reply_markup=ReplyKeyboardMarkup(
                              keyboard=[[KeyboardButton(text='📍 Share Location', request_location=True)]],
                              resize_keyboard=True,
                              one_time_keyboard=True,
                          ))


# ── Confirmation flow ──────────────────────────────────────────────────────────

@dp.callback_query(F.data == 'confirm_yes')
async def confirm_yes(cb: types.CallbackQuery, state: FSMContext):
    """User confirmed their last answer — advance to the next question."""
    await cb.message.edit_reply_markup(reply_markup=None)
    step = await state.get_state()
    message = cb.message

    if step == ProfileSetup.waiting_name.state:
        await ask_age(message, state)
    elif step == ProfileSetup.waiting_age.state:
        await ask_gender(message, state)
    elif step == ProfileSetup.waiting_gender.state:
        await ask_bio(message, state)
    elif step == ProfileSetup.waiting_bio.state:
        await ask_location(message, state)
    elif step == ProfileSetup.waiting_location.state:
        await finish_profile(message, state)
    await cb.answer()


@dp.callback_query(F.data == 'confirm_no')
async def confirm_no(cb: types.CallbackQuery, state: FSMContext):
    """User wants to re-enter their answer — go back to that step."""
    await cb.message.edit_reply_markup(reply_markup=None)
    step = await state.get_state()
    message = cb.message

    if step == ProfileSetup.waiting_name.state:
        await ask_name(message, state)
    elif step == ProfileSetup.waiting_age.state:
        await ask_age(message, state)
    elif step == ProfileSetup.waiting_gender.state:
        await ask_gender(message, state)
    elif step == ProfileSetup.waiting_bio.state:
        await ask_bio(message, state)
    elif step == ProfileSetup.waiting_location.state:
        await ask_location(message, state)
    await cb.answer()


async def finish_profile(message: types.Message, state: FSMContext):
    """All steps confirmed — show summary and clear state."""
    data = await state.get_data()
    uid = message.from_user.id
    user_profiles[uid] = data
    await state.clear()

    await message.answer("📍 Location → *Confirmed* ✅", reply_markup=ReplyKeyboardRemove())

    summary = (
        "✅ *Profile complete!*\n\n"
        f"👤 Name:   {data.get('name','—')}\n"
        f"🎂 Age:    {data.get('age','—')}\n"
        f"⚧ Gender: {data.get('gender','—')}\n"
        f"📝 Bio:    {data.get('bio','—')}\n"
        f"📍 Lat/Lon: {data.get('lat','—')}, {data.get('lon','—')}"
    )
    await message.answer(summary, parse_mode='Markdown')
    await message.answer(
        "❤️ You're all set! Tap *Find a Match* whenever you're ready to meet someone.",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text='❤️  Find a Match', callback_data='find_match')]
            ]
        ),
    )


# ── Step handlers (text inputs) ───────────────────────────────────────────────

@dp.message(StateFilter(ProfileSetup.waiting_name))
async def handle_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if len(name) < 2:
        await message.answer("⚠️ Please enter a name with at least 2 characters.")
        return
    await state.update_data(name=name)
    msg = await message.answer(f"📛 *{name}* — is that right?", reply_markup=confirm_kb(), parse_mode='Markdown')


@dp.message(StateFilter(ProfileSetup.waiting_age))
async def handle_age(message: types.Message, state: FSMContext):
    try:
        age = int(message.text.strip())
    except ValueError:
        await message.answer("⚠️ Please enter a valid number (e.g. 28).")
        return
    if age < 18 or age > 100:
        await message.answer("⚠️ You must be between 18 and 100 years old.")
        return
    await state.update_data(age=str(age))
    msg = await message.answer(f"🎂 Age *{age}* — correct?", reply_markup=confirm_kb(), parse_mode='Markdown')


@dp.message(StateFilter(ProfileSetup.waiting_gender))
async def handle_gender(message: types.Message, state: FSMContext):
    gender_map = {
        '👨 male': 'Male', '👩 female': 'Female', '⚧ other': 'Other',
        'male': 'Male', 'female': 'Female', 'other': 'Other',
        'm': 'Male', 'f': 'Female',
    }
    raw = message.text.strip().lower()
    gender = gender_map.get(raw)
    if not gender:
        await message.answer("⚠️ Please tap one of the buttons or type Male / Female / Other.")
        return
    await state.update_data(gender=gender)
    await message.answer(
        f"⚧ Gender: *{gender}* — is that right?",
        reply_markup=confirm_kb(),
        parse_mode='Markdown',
    )


@dp.message(StateFilter(ProfileSetup.waiting_bio))
async def handle_bio(message: types.Message, state: FSMContext):
    bio = message.text.strip()
    if len(bio) < 10:
        await message.answer("⚠️ Please write at least a short sentence about yourself.")
        return
    await state.update_data(bio=bio)
    await message.answer(
        f"📝 Bio:\n_{bio}_",
        reply_markup=confirm_kb(),
        parse_mode='Markdown',
    )


@dp.message(StateFilter(ProfileSetup.waiting_location))
async def handle_location(message: types.Message, state: FSMContext):
    # If user typed text instead of sharing location
    await message.answer(
        "📍 Please use the *Share Location* button below to share your location.",
        parse_mode='Markdown',
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text='📍 Share Location', request_location=True)]],
            resize_keyboard=True,
            one_time_keyboard=True,
        ),
    )


@dp.message(lambda m: m.location, StateFilter(ProfileSetup.waiting_location))
async def handle_location_ok(message: types.Message, state: FSMContext):
    loc = message.location
    await state.update_data(lat=str(loc.latitude), lon=str(loc.longitude))
    await message.answer(
        f"📍 Location saved!\n_{loc.latitude:.4f}, {loc.longitude:.4f}_",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode='Markdown',
    )
    await message.answer(
        "✅ All done! Tap *Confirm* to finish setting up your profile.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text='✅ Confirm & Finish', callback_data='confirm_yes'),
            ]]
        ),
        parse_mode='Markdown',
    )


# ── Find Match (placeholder) ───────────────────────────────────────────────────
async def find_match(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    profile = user_profiles.get(uid)
    if not profile:
        await cb.message.edit_reply_markup(reply_markup=None)
        await cb.message.answer(
            "🔎 No profile found. Let's set one up first!",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text='👤 Set up Profile', callback_data='profile_start')]]
            ),
        )
        await cb.answer()
        return

    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(
        f"🔎 Looking for matches near *{profile['name']}*…\n\n"
        f"(Lat: {profile.get('lat','?')}, Lon: {profile.get('lon','?')})\n\n"
        "_Match‑finding logic coming soon!_",
        parse_mode='Markdown',
    )
    await cb.answer()


# ── Webhook / startup ──────────────────────────────────────────────────────────

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
        print('WEBHOOK_URL not set – running in long‑polling mode')
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, skip_updates=False)

if __name__ == '__main__':
    asyncio.run(on_startup(dp))