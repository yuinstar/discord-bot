import discord
from discord.ext import commands
import yt_dlp
import asyncio
from collections import deque

# ============================================================
#  설정
# ============================================================
import os
TOKEN = os.environ.get("TOKEN")
# Discord Developer Portal에서 복사한 토큰

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ============================================================
#  전역 상태
# ============================================================
queues: dict[int, deque] = {}          # guild_id → 대기열
current_song: dict[int, dict] = {}    # guild_id → 현재 곡 정보

YDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

# ============================================================
#  헬퍼 함수
# ============================================================
def get_queue(guild_id: int) -> deque:
    if guild_id not in queues:
        queues[guild_id] = deque()
    return queues[guild_id]


def search_yt(query: str) -> dict | None:
    """YouTube에서 곡을 검색하고 스트리밍 URL을 반환합니다."""
    with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
        try:
            info = ydl.extract_info(f"ytsearch:{query}", download=False)
            if "entries" in info:
                info = info["entries"][0]
            return {
                "url": info["url"],
                "title": info.get("title", "알 수 없는 곡"),
                "duration": info.get("duration", 0),
                "webpage_url": info.get("webpage_url", ""),
            }
        except Exception:
            return None


def play_next(ctx: commands.Context):
    """대기열에서 다음 곡을 재생합니다."""
    guild_id = ctx.guild.id
    queue = get_queue(guild_id)

    if not queue or not ctx.voice_client:
        current_song.pop(guild_id, None)
        return

    song = queue.popleft()
    current_song[guild_id] = song

    source = discord.FFmpegPCMAudio(song["url"], **FFMPEG_OPTIONS)
    ctx.voice_client.play(
        source,
        after=lambda e: (print(f"재생 오류: {e}") if e else None) or play_next(ctx),
    )

    asyncio.run_coroutine_threadsafe(
        ctx.send(f"🎵 **재생 중:** {song['title']}"),
        bot.loop,
    )


# ============================================================
#  이벤트
# ============================================================
@bot.event
async def on_ready():
    print(f"✅ 봇 로그인 완료: {bot.user} (ID: {bot.user.id})")


# ============================================================
#  명령어
# ============================================================
@bot.command(name="참가", aliases=["join"])
async def join(ctx: commands.Context):
    """봇을 음성 채널에 참가시킵니다."""
    if not ctx.author.voice:
        return await ctx.send("❌ 먼저 음성 채널에 입장해 주세요.")
    channel = ctx.author.voice.channel
    if ctx.voice_client:
        await ctx.voice_client.move_to(channel)
    else:
        await channel.connect()
    await ctx.send(f"🔊 **{channel.name}** 채널에 참가했습니다!")


@bot.command(name="재생", aliases=["play", "p"])
async def play(ctx: commands.Context, *, query: str):
    """곡을 검색하여 재생하거나 대기열에 추가합니다.
    사용법: !재생 <곡 제목 또는 YouTube URL>
    """
    # 음성 채널 자동 참가
    if not ctx.voice_client:
        if ctx.author.voice:
            await ctx.author.voice.channel.connect()
        else:
            return await ctx.send("❌ 먼저 음성 채널에 입장해 주세요.")

    async with ctx.typing():
        song = await asyncio.get_event_loop().run_in_executor(None, search_yt, query)

    if not song:
        return await ctx.send("❌ 곡을 찾을 수 없습니다. 다른 검색어를 시도해 보세요.")

    queue = get_queue(ctx.guild.id)

    if ctx.voice_client.is_playing() or ctx.voice_client.is_paused():
        queue.append(song)
        await ctx.send(f"📋 **대기열 추가:** {song['title']} (대기 {len(queue)}번째)")
    else:
        queue.append(song)
        play_next(ctx)


@bot.command(name="일시정지", aliases=["pause"])
async def pause(ctx: commands.Context):
    """현재 곡을 일시 정지합니다."""
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("⏸️ 일시 정지했습니다.")
    else:
        await ctx.send("❌ 현재 재생 중인 곡이 없습니다.")


@bot.command(name="재개", aliases=["resume"])
async def resume(ctx: commands.Context):
    """일시 정지된 곡을 다시 재생합니다."""
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("▶️ 재생을 재개합니다.")
    else:
        await ctx.send("❌ 일시 정지된 곡이 없습니다.")


@bot.command(name="스킵", aliases=["skip", "s"])
async def skip(ctx: commands.Context):
    """현재 곡을 건너뛰고 다음 곡을 재생합니다."""
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()   # stop → after 콜백 → play_next 호출
        await ctx.send("⏭️ 다음 곡으로 넘어갑니다.")
    else:
        await ctx.send("❌ 현재 재생 중인 곡이 없습니다.")


@bot.command(name="정지", aliases=["stop"])
async def stop(ctx: commands.Context):
    """재생을 멈추고 대기열을 초기화합니다."""
    guild_id = ctx.guild.id
    queues.pop(guild_id, None)
    current_song.pop(guild_id, None)
    if ctx.voice_client:
        ctx.voice_client.stop()
    await ctx.send("⏹️ 재생을 정지하고 대기열을 비웠습니다.")


@bot.command(name="대기열", aliases=["queue", "q"])
async def queue_list(ctx: commands.Context):
    """현재 대기열을 표시합니다."""
    guild_id = ctx.guild.id
    queue = get_queue(guild_id)
    now = current_song.get(guild_id)

    if not now and not queue:
        return await ctx.send("📋 대기열이 비어 있습니다.")

    embed = discord.Embed(title="🎵 재생 대기열", color=discord.Color.blurple())

    if now:
        embed.add_field(name="🔊 현재 재생 중", value=now["title"], inline=False)

    if queue:
        tracks = "\n".join(
            f"`{i+1}.` {song['title']}" for i, song in enumerate(queue)
        )
        embed.add_field(name="📋 다음 곡", value=tracks, inline=False)

    await ctx.send(embed=embed)


@bot.command(name="나가기", aliases=["leave", "disconnect"])
async def leave(ctx: commands.Context):
    """봇을 음성 채널에서 내보냅니다."""
    guild_id = ctx.guild.id
    queues.pop(guild_id, None)
    current_song.pop(guild_id, None)
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("👋 음성 채널에서 나갔습니다.")
    else:
        await ctx.send("❌ 봇이 음성 채널에 없습니다.")


@bot.command(name="현재곡", aliases=["now", "np"])
async def now_playing(ctx: commands.Context):
    """현재 재생 중인 곡 정보를 표시합니다."""
    song = current_song.get(ctx.guild.id)
    if not song:
        return await ctx.send("❌ 현재 재생 중인 곡이 없습니다.")

    embed = discord.Embed(
        title="🎵 현재 재생 중",
        description=f"[{song['title']}]({song['webpage_url']})",
        color=discord.Color.green(),
    )
    if song["duration"]:
        mins, secs = divmod(song["duration"], 60)
        embed.add_field(name="⏱️ 길이", value=f"{mins}:{secs:02d}")
    await ctx.send(embed=embed)


# ============================================================
#  봇 실행
# ============================================================
bot.run(TOKEN)
