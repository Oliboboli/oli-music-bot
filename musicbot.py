"""Highrise music bot for Railway — stable internet, no proxy needed.

Commands:
    !play <song name> <artist> — search YouTube and queue (needs sub or VIP)
    !playlist — play random song from oli's playlist (needs sub or VIP)
    !sub — subscribe (required to play music)
    !q — queue (whispered to you)
    !np — now playing (whispered to you)
    !like / !dislike — vote on the song playing
    !skip — skip (mods/oli)
    !stop — stop (mods/oli)
    !summonbot — bring the Stargazing emote bot back if it's gone (oli only)

Pay-to-play: non-VIPs tip the bot gold for song credits
(5g=5 songs, 10g=10, 50g=50, 100g=100). VIPs (500g to the emote bot)
play free. Max 2 queued songs per player.
"""

import asyncio
import json
import os
import random
import subprocess
import sys
from collections import deque
from pathlib import Path

from highrise import BaseBot, Highrise
from highrise.models import Position, User

BOT_DIR = Path(__file__).parent
ROOM_ID = os.environ.get("ROOM_ID", "6a7537ddf1acd9746a2593bb")
RELAY_URL = os.environ.get("RELAY_URL", "https://responsible-vision-production-9695.up.railway.app/stream")
OLI_ID = "67a4b9fcaa2fc29f791c24f5"

# --- !summonbot: ask local bot2 to restart bot1 ---
# The DJ bot runs on Railway and can't touch the local bot1 process, so on
# !summonbot it whispers a signed summon to bot2 (FACbot, local, always
# connected). Bot2 validates the secret and kills bot1's process; bot1's
# watchdog restarts it and it rejoins the room within seconds.
# Must match bot2's SUMMON_SECRET.
BOT2_USER_ID = "6aba84c6508d4d5a769b6146"
SUMMON_SECRET = "1af77ba36396001d52e7e20cf8249a65"

# --- Pay-to-play (!sub) ---
# Non-VIPs tip the bot gold -> song credits. VIP status comes from the
# emote bot's VIP list (500g tippers), synced via the repo's "data" branch.
# Max 2 queued songs per player at a time.
SONG_TIERS = [(100, 100), (50, 50), (10, 10), (5, 5)]  # (gold, songs)
MAX_QUEUE_PER_USER = 2
DATA_RAW = "https://raw.githubusercontent.com/Oliboboli/oli-music-bot/data"

# Local MP3s (in repo) — bypass YouTube entirely
LOCAL_SONGS = {
    "hole dwelling": "hole-dwelling.mp3",
}

# Playlist — copied from local
with open(BOT_DIR / "playlist.json") as f:
    PLAYLIST = json.load(f)

# Pre-resolved YouTube URLs (YouTube blocks Railway's IP for searches,
# so we resolve them here where it works and ship the URLs with the bot)
try:
    with open(BOT_DIR / "song_urls.json") as f:
        SONG_URLS = json.load(f)
except FileNotFoundError:
    SONG_URLS = {}


def find_song(query: str) -> dict | None:
    """Find a song by name/artist in the pre-resolved URL map.
    Lenient matching: scores by how many query words appear in the key."""
    q = query.lower().strip()
    if not q:
        return None
    qwords = q.split()
    # exact key match first
    if q in SONG_URLS:
        return SONG_URLS[q]
    # score each key by matched words, prefer shortest key among best scores
    best = None
    best_score = 0
    best_len = 999999
    for key, song in SONG_URLS.items():
        score = sum(1 for w in qwords if w in key)
        if score > best_score or (score == best_score and score > 0 and len(key) < best_len):
            # require at least half the words to match (rounded up)
            if score >= (len(qwords) + 1) // 2:
                best = song
                best_score = score
                best_len = len(key)
    return best


async def yt_search(query: str) -> dict | None:
    """Search YouTube via yt-dlp. No proxy needed on Railway."""
    def _search():
        try:
            r = subprocess.run(
                [sys.executable, "-m", "yt_dlp",
                 "--no-playlist", "--skip-download",
                 "--socket-timeout", "20",
                 "--print", "%(title)s\t%(duration)s\t%(webpage_url)s",
                 "--default-search", "ytsearch1", query],
                capture_output=True, text=True, timeout=35,
            )
            print(f"yt-dlp search for '{query}': returncode={r.returncode}", flush=True)
            print(f"yt-dlp stdout: {r.stdout[:500]}", flush=True)
            print(f"yt-dlp stderr: {r.stderr[:500]}", flush=True)
            line = r.stdout.strip().split("\n")[-1] if r.stdout.strip() else ""
            if line and r.returncode == 0:
                parts = line.split("\t")
                if len(parts) >= 3:
                    try:
                        dur = int(float(parts[1]))
                    except ValueError:
                        dur = 180
                    return {"title": parts[0], "url": parts[2], "duration": dur}
            print(f"Search failed for '{query}': no valid result", flush=True)
        except Exception as e:
            print(f"Search exception for '{query}': {e}", flush=True)
        return None
    try:
        return await asyncio.to_thread(_search)
    except Exception as e:
        print(f"Search thread exception: {e}", flush=True)
        return None


class MusicBot(BaseBot):
    # DJ booth spot in Stargazing — the bot walks/teleports here on startup
    # (same spot as bot1's "dj" teleport)
    DJ_BOOTH = {"x": 11.142864227295, "y": 0.25, "z": 29.183265686035,
                "facing": "FrontRight"}

    def __init__(self):
        super().__init__()
        self.queue = deque()
        self.current = None
        self.playing = False
        self.bot_id = None
        self.credits: dict[str, int] = {}   # user_id -> song credits
        self.vip_users: set[str] = {OLI_ID}  # synced from emote bot's VIP list
        self.subscribers: set[str] = set()  # users who typed !sub (required to play)

    # DJ bot dance emotes — always dancing, cycling through, music on or not
    # (emote_id, duration_seconds)
    DANCE_EMOTES = [
        ("emote-tapdance", 11.1),
        ("dance-tiktok8", 10.2),
        ("dance-russian", 9.6),
    ("dance-orangejustice", 6.5),
    ("dance-blackpink", 6.4),
    ("dance-zombie", 12.9),
    ("dance-tiktok9", 11.5),
    ("idle-dance-casual", 8.8),
    ("dance-icecream", 6.3),
    ("dance-tiktok10", 7.5),
    ("dance-sexy", 12.3),
    ("dance-pinguin", 10.8),
]

    async def on_start(self, session_metadata):
        print("MusicBot started, joining room...")
        self.session_metadata = session_metadata
        self.bot_id = session_metadata.user_id
        self._load_credits()
        self._load_subscribers()
        await self._refresh_remote_data(first=True)
        # keep the emote-bot VIP list fresh (poll the data branch)
        asyncio.create_task(self._data_sync_loop())
        await self.highrise.join_room(ROOM_ID)
        # head to the DJ booth automatically, then start dancing
        asyncio.create_task(self._go_to_dj_booth())
        # the DJ bot is always dancing, cycling through emotes
        asyncio.create_task(self._dance_loop())

    # ---------- pay-to-play (!sub) ----------

    def _credits_path(self):
        return BOT_DIR / "credits.json"

    def _load_credits(self):
        try:
            with open(self._credits_path()) as f:
                self.credits = {k: int(v) for k, v in json.load(f).items()}
        except Exception:
            self.credits = {}

    def _save_credits(self):
        try:
            with open(self._credits_path(), "w") as f:
                json.dump(self.credits, f)
        except Exception as e:
            print(f"[sub] save credits failed: {e}", flush=True)

    def _subs_path(self):
        return BOT_DIR / "subscribers.json"

    def _load_subscribers(self):
        try:
            with open(self._subs_path()) as f:
                self.subscribers = set(json.load(f))
        except Exception:
            self.subscribers = set()

    def _save_subscribers(self):
        try:
            with open(self._subs_path(), "w") as f:
                json.dump(sorted(self.subscribers), f)
        except Exception as e:
            print(f"[sub] save subscribers failed: {e}", flush=True)

    def _is_vip(self, user_id: str) -> bool:
        return user_id == OLI_ID or user_id in self.vip_users

    async def _refresh_remote_data(self, first: bool = False):
        """Pull vip.json (always) and credits.json (startup only) from the
        repo's data branch. The emote bot pushes vip.json there on change."""
        import aiohttp
        try:
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.get(f"{DATA_RAW}/vip.json",
                                           timeout=aiohttp.ClientTimeout(total=15)) as r:
                        if r.status == 200:
                            self.vip_users = set(await r.json()) | {OLI_ID}
                except Exception as e:
                    print(f"[sub] vip refresh failed: {e}", flush=True)
                if first:
                    try:
                        async with session.get(
                                f"{DATA_RAW}/credits.json",
                                timeout=aiohttp.ClientTimeout(total=15)) as r:
                            if r.status == 200:
                                data = await r.json()
                                for uid, n in data.items():
                                    self.credits[uid] = max(
                                        self.credits.get(uid, 0), int(n))
                                self._save_credits()
                    except Exception as e:
                        print(f"[sub] credits refresh failed: {e}", flush=True)
        except Exception as e:
            print(f"[sub] remote data failed: {e}", flush=True)

    async def _data_sync_loop(self):
        await asyncio.sleep(10)
        while True:
            try:
                await self._refresh_remote_data()
            except Exception:
                pass
            await asyncio.sleep(300)  # refresh VIP list every 5 min

    def _play_allowed(self, user: User):
        """Returns (ok, whisper_message)."""
        if user.id not in self.subscribers and user.id != OLI_ID:
            return False, "type !sub to play music"
        if self._is_vip(user.id):
            return True, ""
        queued = sum(1 for s in self.queue if s.get("requested_by") == user.id)
        if queued >= MAX_QUEUE_PER_USER:
            return False, "u already have 2 songs queued — wait for one to play"
        if self.credits.get(user.id, 0) <= 0:
            return False, "ur out of songs — tip the bot gold for more"
        return True, ""

    def _spend_credit(self, user: User):
        if not self._is_vip(user.id):
            self.credits[user.id] = max(0, self.credits.get(user.id, 0) - 1)
            self._save_credits()

    async def on_tip(self, sender, receiver, tip) -> None:
        # gold tip to the bot = song credits (5g=5, 10g=10, 50g=50, 100g=100)
        try:
            if not self.bot_id or receiver.id != self.bot_id:
                return
            if getattr(tip, "type", "") != "gold":
                return
            amount = int(getattr(tip, "amount", 0) or 0)
            songs = 0
            for gold, s in SONG_TIERS:
                if amount >= gold:
                    songs = s
                    break
            if songs <= 0:
                return
            self.credits[sender.id] = self.credits.get(sender.id, 0) + songs
            self._save_credits()
            print(f"[sub] {getattr(sender, 'username', sender.id)} tipped "
                  f"{amount}g -> +{songs} songs "
                  f"(total {self.credits[sender.id]})", flush=True)
            try:
                await self.highrise.send_whisper(
                    sender.id,
                    f"+{songs} songs! u now have {self.credits[sender.id]} 🎶")
            except Exception:
                pass
        except Exception as e:
            print(f"[sub] on_tip error: {e}", flush=True)

    async def _cmd_sub(self, user: User):
        # !sub subscribes you — the only way to be able to play music.
        # (VIPs play free, everyone else uses song credits from tips.)
        if user.id in self.subscribers:
            n = self.credits.get(user.id, 0)
            await self.highrise.send_whisper(
                user.id, f"ur already subbed 🎶 ({n} song{'s' if n != 1 else ''} left)")
            return
        self.subscribers.add(user.id)
        self._save_subscribers()
        print(f"[sub] new subscriber: {getattr(user, 'username', user.id)}", flush=True)
        await self.highrise.send_whisper(
            user.id,
            "ur subbed! 🎶 tip the bot gold for songs:\n"
            "5g = 5 songs · 10g = 10 songs · "
            "50g = 50 songs · 100g = 100 songs")

    async def _cmd_vote(self, user: User, kind: str):
        if not self.current:
            await self.highrise.send_whisper(user.id, "nothing playing rn")
            return
        song = self.current
        likes = song.setdefault("likes", set())
        dislikes = song.setdefault("dislikes", set())
        if kind == "like":
            dislikes.discard(user.id)
            likes.add(user.id)
        else:
            likes.discard(user.id)
            dislikes.add(user.id)
        await self.highrise.send_whisper(
            user.id,
            f"{'👍' if kind == 'like' else '👎'} {song['title']} "
            f"({len(likes)} 👍 / {len(dislikes)} 👎)")

    # ---------- /pay-to-play ----------

    async def _go_to_dj_booth(self):
        """Walk/teleport to the DJ booth on startup, return if moved."""
        await asyncio.sleep(8)  # let the join settle
        try:
            my_id = self.session_metadata.user_id
        except Exception:
            print("dj booth: couldn't find bot user ID, skipping")
            return
        for _ in range(3):
            try:
                try:
                    await self.highrise.teleport(my_id, Position(**self.DJ_BOOTH))
                except Exception:
                    await self.highrise.walk_to(Position(**self.DJ_BOOTH))
                print("dj booth: bot is at the booth")
                break
            except Exception as e:
                print(f"dj booth: move failed ({e}), retrying")
                await asyncio.sleep(5)
        # stay at the booth — go back if anything moves the bot
        while True:
            try:
                await asyncio.sleep(30)
                resp = await self.highrise.get_room_users()
                from highrise.models import Error
                if isinstance(resp, Error):
                    continue
                positions = {u.id: pos for u, pos in resp.content}
                if my_id not in positions:
                    continue
                pos = positions[my_id]
                if not hasattr(pos, "x"):
                    continue
                dx = pos.x - self.DJ_BOOTH["x"]
                dz = pos.z - self.DJ_BOOTH["z"]
                if dx * dx + dz * dz > 4.0:  # moved more than ~2m away
                    try:
                        await self.highrise.teleport(my_id, Position(**self.DJ_BOOTH))
                    except Exception:
                        await self.highrise.walk_to(Position(**self.DJ_BOOTH))
            except Exception:
                pass

    async def _dance_loop(self):
        """DJ bot dances forever, cycling through emotes. Music on or not."""
        await asyncio.sleep(5)  # let the join settle
        # the bot dances on itself — get our own user ID from the session
        try:
            my_id = self.session_metadata.user_id
        except Exception:
            print("dance loop: couldn't find bot user ID, skipping")
            return
        print(f"dance loop starting for bot {my_id}")
        while True:
            for emote_id, duration in self.DANCE_EMOTES:
                try:
                    await self.highrise.send_emote(emote_id, my_id)
                except Exception as e:
                    print(f"dance emote {emote_id} failed: {e}")
                await asyncio.sleep(duration)

    async def on_chat(self, user: User, message: str):
        msg = message.strip()
        low = msg.lower()
        if low.startswith("!play "):
            query = msg[6:].strip()
            if query:
                await self._cmd_play(user, query)
        elif low == "!playlist":
            await self._cmd_playlist(user)
        elif low == "!q":
            await self._cmd_queue(user)
        elif low == "!np":
            await self._cmd_np(user)
        elif low == "!sub":
            await self._cmd_sub(user)
        elif low == "!like":
            await self._cmd_vote(user, "like")
        elif low == "!dislike":
            await self._cmd_vote(user, "dislike")
        elif low == "!skip":
            await self._cmd_skip(user)
        elif low == "!stop":
            await self._cmd_stop(user)
        elif low == "!summonbot":
            await self._cmd_summonbot(user)

    async def _cmd_play(self, user: User, query: str):
        ok, msg = self._play_allowed(user)
        if not ok:
            await self.highrise.send_whisper(user.id, msg)
            return
        # Check local MP3s first (bypasses YouTube)
        qlow = query.lower().strip()
        for key, filename in LOCAL_SONGS.items():
            if key in qlow or qlow in key:
                song = {"title": key.title(), "local_file": filename, "url": "",
                        "requested_by": user.id, "likes": set(), "dislikes": set()}
                self._spend_credit(user)
                self.queue.append(song)
                await self.highrise.send_whisper(user.id, f"queued: {song['title']}")
                if not self.playing:
                    asyncio.create_task(self._play_loop())
                return
        song = find_song(query)
        if not song:
            # fallback to live search (likely blocked on Railway, but try)
            song = await yt_search(query)
        if not song:
            await self.highrise.send_whisper(user.id, "couldn't find that song 😢")
            return
        song = dict(song)  # copy — never mutate the shared SONG_URLS dicts
        song["requested_by"] = user.id
        song.setdefault("likes", set())
        song.setdefault("dislikes", set())
        self._spend_credit(user)
        self.queue.append(song)
        await self.highrise.send_whisper(
            user.id, f"queued: {song['title']}"
        )
        if not self.playing:
            asyncio.create_task(self._play_loop())

    async def _cmd_playlist(self, user: User):
        ok, msg = self._play_allowed(user)
        if not ok:
            await self.highrise.send_whisper(user.id, msg)
            return
        if not PLAYLIST:
            await self.highrise.send_whisper(user.id, "playlist is empty 😢")
            return
        track = random.choice(PLAYLIST)
        query = f"{track.get('title', '')} {track.get('artist', '')}".strip()
        await self.highrise.chat(f"🎲 playlist pick: {track.get('title')} — {track.get('artist')}")
        song = find_song(query)
        if not song:
            song = await yt_search(query)
        if not song:
            await self.highrise.send_whisper(user.id, "couldn't find that song 😢")
            return
        song = dict(song)
        song["requested_by"] = user.id
        song.setdefault("likes", set())
        song.setdefault("dislikes", set())
        self._spend_credit(user)
        self.queue.append(song)
        if not self.playing:
            asyncio.create_task(self._play_loop())

    async def _cmd_queue(self, user: User):
        if not self.queue and not self.current:
            await self.highrise.send_whisper(user.id, "queue is empty")
            return
        lines = []
        if self.current:
            lines.append(f"▶ {self.current['title']}")
        for i, s in enumerate(list(self.queue)[:10], 1):
            lines.append(f"{i}. {s['title']}")
        await self.highrise.send_whisper(user.id, "\n".join(lines))

    async def _cmd_np(self, user: User):
        if self.current:
            likes = len(self.current.get("likes", ()))
            dislikes = len(self.current.get("dislikes", ()))
            votes = f" ({likes} 👍 / {dislikes} 👎)" if (likes or dislikes) else ""
            await self.highrise.send_whisper(
                user.id, f"🎵 {self.current['title']}{votes}")
        else:
            await self.highrise.send_whisper(user.id, "nothing playing")

    async def _cmd_skip(self, user: User):
        # Clear relay buffer to stop current audio
        try:
            import aiohttp
            clear_url = RELAY_URL.replace('/stream', '/clear')
            async with aiohttp.ClientSession() as session:
                async with session.post(clear_url) as resp:
                    pass
        except Exception:
            pass
        
        self.current = None
        # Signal the play loop to move to next song
        if hasattr(self, '_skip_event'):
            self._skip_event.set()
        
        if self.queue:
            await self.highrise.chat("⏭ skipped")
        else:
            await self.highrise.chat("⏭ skipped - queue empty")
            self.playing = False

    async def _cmd_stop(self, user: User):
        self.queue.clear()
        self.current = None
        self.playing = False
        await self.highrise.chat("⏹ stopped")

    async def _cmd_summonbot(self, user: User):
        # Oli only: bring the Stargazing emote bot (bot1) back if it vanished.
        # Whispers a signed summon to bot2 (local, always connected); bot2
        # restarts bot1's process and its watchdog rejoins the room.
        if user.id != OLI_ID:
            return
        import time as _time
        msg = f"summon_bot1:{_time.time()}:{SUMMON_SECRET}"
        try:
            await self.highrise.send_whisper(BOT2_USER_ID, msg)
            await self.highrise.chat("summoning the emote bot back 🫶")
        except Exception:
            await self.highrise.send_whisper(
                user.id, "summon signal failed 😢 try again in a bit")

    async def _play_loop(self):
        if self.playing:
            return
        self.playing = True
        self._skip_event = asyncio.Event()
        try:
            while self.queue:
                song = self.queue.popleft()
                self.current = song
                self._skip_event.clear()
                await self.highrise.chat(f"🎵 now playing: {song['title']}")
                duration = await self._stream_song(song)
                self.current = None
                # Wait for song to finish, or skip
                if duration and duration > 0:
                    try:
                        await asyncio.wait_for(self._skip_event.wait(), timeout=duration)
                        # Skipped - continue to next song
                        continue
                    except asyncio.TimeoutError:
                        # Song finished naturally
                        pass
        finally:
            self.playing = False
            self.current = None

    async def _stream_song(self, song: dict):
        """Stream audio to relay. Uses local MP3 if available, else yt-dlp + FFmpeg."""
        title = song.get("title", "unknown")
        requested_by = song.get("requested_by")
        print(f"[audio] starting: {title}", flush=True)

        async def _whisper_err(msg: str):
            if requested_by:
                try:
                    await self.highrise.send_whisper(requested_by, f"audio error: {msg}")
                except Exception:
                    pass

        # Check for local MP3 file first (bypasses YouTube entirely)
        local_file = song.get("local_file")
        if local_file:
            import os
            filepath = os.path.join(os.path.dirname(__file__), local_file)
            if os.path.exists(filepath):
                print(f"[audio] using local file: {filepath}", flush=True)
                try:
                    with open(filepath, "rb") as f:
                        mp3_data = f.read()
                    print(f"[audio] read {len(mp3_data)} bytes from local file", flush=True)
                    # Estimate duration: assume 192kbps (file_size * 8 / bitrate)
                    duration = (len(mp3_data) * 8) / (192 * 1000)
                    print(f"[audio] estimated duration: {duration:.1f}s", flush=True)
                    # PUT directly to relay (already MP3, no transcoding needed)
                    import aiohttp
                    async with aiohttp.ClientSession() as session:
                        async with session.put(
                            RELAY_URL,
                            data=mp3_data,
                            headers={"Content-Type": "audio/mpeg"}
                        ) as resp:
                            print(f"[audio] relay PUT status: {resp.status}", flush=True)
                            if resp.status != 200:
                                await _whisper_err(f"relay returned {resp.status}")
                    return duration
                except Exception as e:
                    print(f"[audio] local file error: {e}", flush=True)
                    await _whisper_err(f"local file failed: {e}")
                    # fall through to yt-dlp
            else:
                print(f"[audio] local file not found: {filepath}", flush=True)
                await _whisper_err(f"file not found on server: {local_file}")

        # Fallback: Download via yt-dlp (YouTube)
        url = song["url"]
        print(f"[audio] url: {url}", flush=True)
        try:
            # Get FFmpeg binary path (from imageio-ffmpeg package, works without system ffmpeg)
            try:
                import imageio_ffmpeg
                ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
                print(f"[audio] using ffmpeg: {ffmpeg_bin}", flush=True)
            except Exception as e:
                print(f"[audio] imageio-ffmpeg not available, trying system ffmpeg: {e}", flush=True)
                ffmpeg_bin = "ffmpeg"

            # yt-dlp: bestaudio to stdout (as PIPE)
            # Use android client to bypass datacenter IP blocks
            ytdlp = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "yt_dlp",
                "--no-playlist", "--quiet", "--no-warnings",
                "--extractor-args", "youtube:player_client=android",
                "-f", "bestaudio",
                "-o", "-", url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            # FFmpeg: transcode to 128k MP3, stdin as PIPE (we'll pump data manually)
            ffmpeg = await asyncio.create_subprocess_exec(
                ffmpeg_bin, "-hide_banner", "-loglevel", "error",
                "-i", "pipe:0",
                "-vn", "-c:a", "libmp3lame", "-b:a", "128k",
                "-f", "mp3", "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            async def _pump():
                """Forward yt-dlp stdout to FFmpeg stdin."""
                try:
                    while True:
                        chunk = await ytdlp.stdout.read(65536)
                        if not chunk:
                            break
                        ffmpeg.stdin.write(chunk)
                        await ffmpeg.stdin.drain()
                except Exception as e:
                    print(f"[audio] pump error: {e}", flush=True)
                finally:
                    try:
                        ffmpeg.stdin.close()
                    except Exception:
                        pass

            pump_task = asyncio.create_task(_pump())

            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.put(
                    RELAY_URL,
                    data=ffmpeg.stdout,
                    headers={"Content-Type": "audio/mpeg"},
                    timeout=aiohttp.ClientTimeout(total=song.get("duration", 180) + 60),
                ) as resp:
                    print(f"[audio] relay PUT status: {resp.status}", flush=True)
                    await resp.read()

            await pump_task
            ytdlp_rc = await ytdlp.wait()
            ffmpeg_stderr = await ffmpeg.stderr.read() if ffmpeg.stderr else b""
            ffmpeg_rc = await ffmpeg.wait()
            print(f"[audio] yt-dlp rc={ytdlp_rc}, ffmpeg rc={ffmpeg_rc}", flush=True)
            if ffmpeg_stderr:
                print(f"[audio] ffmpeg stderr: {ffmpeg_stderr.decode()[:500]}", flush=True)
            if ffmpeg_rc != 0:
                print(f"[audio] FAILED to transcode: {title}", flush=True)
            else:
                print(f"[audio] done: {title}", flush=True)
        except Exception as e:
            print(f"[audio] stream error for {title}: {e}", flush=True)
            import traceback
            traceback.print_exc()


async def main():
    token = os.environ.get("HIGHRISE_TOKEN", "")
    if not token:
        print("HIGHRISE_TOKEN not set!", flush=True)
        return
    bot = MusicBot()
    await Highrise().run(bot, ROOM_ID, token)


if __name__ == "__main__":
    asyncio.run(main())
