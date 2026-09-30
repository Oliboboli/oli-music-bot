"""Highrise music bot for Railway — stable internet, no proxy needed.

Commands:
    !play <song name> <artist> — search YouTube and queue
    !playlist — play random song from oli's playlist
    !q — show queue
    !np — now playing
    !skip — skip (mods/oli)
    !stop — stop (mods/oli)
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

    async def on_start(self, session_metadata):
        print("MusicBot started, joining room...")
        await self.highrise.join_room(ROOM_ID)

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

    async def _cmd_play(self, user: User, query: str):
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
        # TODO: check mod — for now allow all
        if self.current:
            await self.highrise.chat("⏭ skipped")
            # The _play_loop will pick up next; we just clear current
            self.current = None

    async def _cmd_stop(self, user: User):
        self.queue.clear()
        self.current = None
        self.playing = False
        await self.highrise.chat("⏹ stopped")

    async def _play_loop(self):
        if self.playing:
            return
        self.playing = True
        try:
            while self.queue:
                song = self.queue.popleft()
                self.current = song
                await self.highrise.chat(f"🎵 now playing: {song['title']}")
                await self._stream_song(song)
                self.current = None
        finally:
            self.playing = False
            self.current = None

    async def _stream_song(self, song: dict):
        """Download audio via yt-dlp, transcode with FFmpeg, PUT MP3 to relay."""
        url = song["url"]
        title = song.get("title", "unknown")
        print(f"[audio] starting: {title}", flush=True)
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
            ytdlp = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "yt_dlp",
                "--no-playlist", "--quiet", "--no-warnings",
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
