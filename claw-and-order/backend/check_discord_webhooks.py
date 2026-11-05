"""Validate configured Discord destinations without posting a message.

Run with the same private configuration as the backend:
    python -m backend.check_discord_webhooks
"""

import asyncio
import sys

import aiohttp

from backend import config  # Load the same .env as the service.
from backend.services.discord_webhooks import load_routes


async def check() -> int:
    try:
        routes = load_routes()
    except ValueError as error:
        print(str(error))
        return 1
    if not routes:
        print("No Discord webhook destinations configured")
        return 1
    if config.DISCORD_TOKEN:
        print("Choose Discord webhooks or bot delivery; clear DISCORD_TOKEN for webhook mode")
        return 1
    failed = False
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        for route in routes:
            try:
                async with session.get(route.url, allow_redirects=False) as response:
                    if response.status != 200:
                        print(f"{route.name}: FAILED (Discord HTTP {response.status})")
                        failed = True
                        continue
                    body = await response.json()
                    channel_id = body.get("channel_id") if isinstance(body, dict) else None
                    if not isinstance(body, dict) or body.get("type") != 1 or not isinstance(channel_id, str) or not channel_id.isdigit():
                        print(f"{route.name}: FAILED (no channel identity returned)")
                        failed = True
                        continue
                    print(f"{route.name}: accessible (channel {channel_id}); delivery test still required")
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                print(f"{route.name}: FAILED (connection or response error)")
                failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(check()))
