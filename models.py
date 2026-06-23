import asyncpg
from db import get_pool

class Match:
    @staticmethod
    async def create(pool, user_a: int, user_b: int):
        async with pool.acquire() as conn:
            result = await conn.fetchrow(
                "INSERT INTO matches (user_a, user_b) VALUES ($1, $2) RETURNING match_id;",
                user_a, user_b,
            )
            return result['match_id']

    @staticmethod
    async def get_by_user(pool, user_id: int):
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM matches WHERE user_a=$1 OR user_b=$1;",
                user_id,
            )
            return row
