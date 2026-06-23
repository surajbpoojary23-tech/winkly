import os
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '.env'))

from aiohttp import web

BOT_TOKEN = os.getenv('BOT_TOKEN') or "8624196108:***"
SUPABASE_URL = os.getenv('SUPABASE_URL')  # to be filled later
SUPABASE_KEY = os.getenv('SUPABASE_KEY')
REDIS_URL = os.getenv('REDIS_URL')

# Default to Render service URL; override with WEBHOOK_URL env var if needed
WEBHOOK_URL = os.getenv('WEBHOOK_URL') or 'https://winkly-kmsz.onrender.com'

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Simple start command
@dp.message(Command('start'))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Hey! I'm *Winkly*, your quick‑match dating bot.\n"
        "Use /profile to set up your preferences, then /match to find someone nearby.",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Profile', callback_data='profile')], [InlineKeyboardButton(text='Find Match', callback_data='find_match')]])
    )

# Placeholder handlers for callbacks (profile, find_match)
@dp.callback_query()
async def cb_handler(cb: types.CallbackQuery):
    data = cb.data
    if data == 'profile':
        await cb.message.answer('📝 Send me a short bio and your gender (M/F).')
        # Set FSM state in a full version
    elif data == 'find_match':
        await cb.message.answer('🔎 Looking for a match near you... (geo‑search not implemented in demo)')
    await cb.answer()

async def on_startup(_: Dispatcher):
    if WEBHOOK_URL:
        # Register the webhook with Telegram
        await bot.set_webhook(WEBHOOK_URL)
        print(f"Webhook set to {WEBHOOK_URL}")

        # Use aiogram's built‑in SimpleRequestHandler for webhook handling.
        from aiogram.webhook.aiohttp_server import SimpleRequestHandler
        handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
        app = web.Application()
        handler.register(app, path='/')
        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.getenv('PORT', '8080'))
        site = web.TCPSite(runner, host='0.0.0.0', port=port)
        await site.start()
        print(f'✅ Webhook server running on port {port} – press Ctrl+C to stop')
        await asyncio.Event().wait()
    else:
        print('WEBHOOK_URL not set – running in long‑polling mode for testing')
        # Delete any lingering webhook to avoid conflict with long-polling
        await bot.delete_webhook(drop_pending_updates=True)
        print("✅ Cleared any existing webhook, starting long-polling...")
        await dp.start_polling(bot, skip_updates=False)

if __name__ == '__main__':
    asyncio.run(on_startup(dp))