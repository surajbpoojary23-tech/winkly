import os
import asyncpg
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '.env'))

DATABASE_URL = os.getenv('SUPABASE_URL')  # Supabase Postgres URL (e.g. https://xxx.supabase.co/rest/v1/?)

async def get_pool():
    return await asyncpg.create_pool(DATABASE_URL)
