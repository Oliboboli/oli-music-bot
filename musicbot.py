"""Highrise music bot for Railway — stable internet, no proxy needed.

Commands:
    !play <song name> <artist> — search YouTube and queue
    !playlist — play random song from oli's playlist
    !q — show queue
    !np — now playing
    !skip — skip (mods/oli)
    !stop — stop (mods/oli)
    !summonbot — bring the Stargazing emote bot back if it's gone (oli only)
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
from highrise.models import User

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
    def __init__(self):
        super().__init__()
        self.queue = deque()
        self.current = None
        self.playing = False

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
        await self.highrise.join_room(ROOM_ID)
        # the DJ bot is always dancing, cycling through emotes
        asyncio.create_task(self._dance_loop())

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
        elif low == "!skip":
            await self._cmd_skip(user)
        elif low == "!stop":
            await self._cmd_stop(user)
        elif low == "!summonbot":
            await self._cmd_summonbot(user)

    async def _cmd_play(self, user: User, query: str):
        # Check local MP3s first (bypasses YouTube)
        qlow = query.lower().strip()
        for key, filename in LOCAL_SONGS.items():
            if key in qlow or qlow in key:
                song = {"title": key.title(), "local_file": filename, "url": "", "requested_by": user.id}
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
        self.queue.append(song)
        await self.highrise.send_whisper(
            user.id, f"queued: {song['title']}"
        )
        if not self.playing:
            asyncio.create_task(self._play_loop())

    async def _cmd_playlist(self, user: User):
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
            await self.highrise.send_whisper(user.id, f"🎵 {self.current['title']}")
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
