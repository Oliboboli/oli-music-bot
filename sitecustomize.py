"""Compatibility shim for highrise-bot-sdk 25.x.

The pinned SDK (25.1.0) removed Highrise.run() and Highrise.join_room().
Python auto-imports this module at startup (sitecustomize), restoring
those methods so musicbot.py works unchanged. Also forces
line-buffered stdout so bot logs appear promptly.
"""
import sys

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

try:
    from highrise import Highrise
    from highrise.__main__ import BotDefinition, main as highrise_main

    if not hasattr(Highrise, "run"):
        async def _run(self, bot, room_id, token):
            await highrise_main([BotDefinition(bot, room_id, token)])

        Highrise.run = _run

    if not hasattr(Highrise, "join_room"):
        async def _join_room(self, room_id):
            # The v25 runner already connects to the room before on_start.
            return None

        Highrise.join_room = _join_room
except Exception:
    pass
