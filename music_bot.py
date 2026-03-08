import discord
from discord.ext import commands
import yt_dlp
import asyncio
import json
import os
import edge_tts
from collections import deque

# ============================================================
#  설정
# ============================================================
TOKEN = os.environ.get("TOKEN")   # Railway 환경변수에서 토큰 가져오기

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ============================================================
#  전역 상태
# ============================================================
queues: dict[int, deque] = {}          # guild_id → 대기열
current_song: dict[int, dict] = {}    # guild_id → 현재 곡 정보

PLAYLIST_FILE = "playlists.json"       # 재생 목록 저장 파일

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
#  재생 목록 헬퍼 함수
# ============================================================
def load_playlists() -> dict:
    """저장된 재생 목록을 불러옵니다."""
    if os.path.exists(PLAYLIST_FILE):
        with open(PLAYLIST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_playlists(data: dict):
    """재생 목록을 파일에 저장합니다."""
    with open(PLAYLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ============================================================
#  일반 헬퍼 함수
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
                "query": query,
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
#  기존 명령어
# ============================================================
@bot.command(name="참가", aliases=["join"])
async def join(ctx: commands.Context):
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
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("⏸️ 일시 정지했습니다.")
    else:
        await ctx.send("❌ 현재 재생 중인 곡이 없습니다.")


@bot.command(name="재개", aliases=["resume"])
async def resume(ctx: commands.Context):
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("▶️ 재생을 재개합니다.")
    else:
        await ctx.send("❌ 일시 정지된 곡이 없습니다.")


@bot.command(name="스킵", aliases=["skip", "s"])
async def skip(ctx: commands.Context):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.send("⏭️ 다음 곡으로 넘어갑니다.")
    else:
        await ctx.send("❌ 현재 재생 중인 곡이 없습니다.")


@bot.command(name="정지", aliases=["stop"])
async def stop(ctx: commands.Context):
    guild_id = ctx.guild.id
    queues.pop(guild_id, None)
    current_song.pop(guild_id, None)
    if ctx.voice_client:
        ctx.voice_client.stop()
    await ctx.send("⏹️ 재생을 정지하고 대기열을 비웠습니다.")


@bot.command(name="대기열", aliases=["queue", "q"])
async def queue_list(ctx: commands.Context):
    guild_id = ctx.guild.id
    queue = get_queue(guild_id)
    now = current_song.get(guild_id)

    if not now and not queue:
        return await ctx.send("📋 대기열이 비어 있습니다.")

    embed = discord.Embed(title="🎵 재생 대기열", color=discord.Color.blurple())
    if now:
        embed.add_field(name="🔊 현재 재생 중", value=now["title"], inline=False)
    if queue:
        tracks = "\n".join(f"`{i+1}.` {song['title']}" for i, song in enumerate(queue))
        embed.add_field(name="📋 다음 곡", value=tracks, inline=False)
    await ctx.send(embed=embed)


@bot.command(name="나가기", aliases=["leave", "disconnect"])
async def leave(ctx: commands.Context):
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
#  재생 목록 명령어
# ============================================================
@bot.command(name="목록생성", aliases=["플리생성", "pl_create"])
async def playlist_create(ctx: commands.Context, *, name: str):
    """새 재생 목록을 만듭니다. 사용법: !목록생성 <이름>"""
    playlists = load_playlists()
    user_id = str(ctx.author.id)

    if user_id not in playlists:
        playlists[user_id] = {}

    if name in playlists[user_id]:
        return await ctx.send(f"❌ **{name}** 재생 목록이 이미 존재합니다.")

    playlists[user_id][name] = []
    save_playlists(playlists)
    await ctx.send(f"✅ **{name}** 재생 목록을 만들었습니다!")


@bot.command(name="목록추가", aliases=["플리추가", "pl_add"])
async def playlist_add(ctx: commands.Context, name: str, *, query: str):
    """재생 목록에 곡을 추가합니다. 사용법: !목록추가 <목록이름> <곡 제목>"""
    playlists = load_playlists()
    user_id = str(ctx.author.id)

    if user_id not in playlists or name not in playlists[user_id]:
        return await ctx.send(f"❌ **{name}** 재생 목록이 없습니다. 먼저 `!목록생성 {name}` 으로 만들어 주세요.")

    async with ctx.typing():
        song = await asyncio.get_event_loop().run_in_executor(None, search_yt, query)

    if not song:
        return await ctx.send("❌ 곡을 찾을 수 없습니다.")

    playlists[user_id][name].append({
        "title": song["title"],
        "query": query,
        "webpage_url": song["webpage_url"],
    })
    save_playlists(playlists)
    await ctx.send(f"✅ **{name}** 목록에 **{song['title']}** 을 추가했습니다! (총 {len(playlists[user_id][name])}곡)")


@bot.command(name="목록재생", aliases=["플리재생", "pl_play"])
async def playlist_play(ctx: commands.Context, *, name: str):
    """재생 목록의 모든 곡을 재생합니다. 사용법: !목록재생 <목록이름>"""
    playlists = load_playlists()
    user_id = str(ctx.author.id)

    if user_id not in playlists or name not in playlists[user_id]:
        return await ctx.send(f"❌ **{name}** 재생 목록이 없습니다.")

    songs = playlists[user_id][name]
    if not songs:
        return await ctx.send(f"❌ **{name}** 재생 목록이 비어 있습니다.")

    if not ctx.voice_client:
        if ctx.author.voice:
            await ctx.author.voice.channel.connect()
        else:
            return await ctx.send("❌ 먼저 음성 채널에 입장해 주세요.")

    await ctx.send(f"🔍 **{name}** 목록의 {len(songs)}곡을 불러오는 중... 잠시만 기다려 주세요!")

    queue = get_queue(ctx.guild.id)
    added = 0

    for song_info in songs:
        song = await asyncio.get_event_loop().run_in_executor(None, search_yt, song_info["query"])
        if song:
            queue.append(song)
            added += 1

    await ctx.send(f"📋 **{name}** 목록에서 {added}곡을 대기열에 추가했습니다!")

    if not ctx.voice_client.is_playing() and not ctx.voice_client.is_paused():
        play_next(ctx)


@bot.command(name="목록보기", aliases=["플리보기", "pl_list"])
async def playlist_list(ctx: commands.Context, name: str = None):
    """재생 목록을 확인합니다.
    사용법: !목록보기 → 내 모든 목록
            !목록보기 <이름> → 특정 목록의 곡 확인
    """
    playlists = load_playlists()
    user_id = str(ctx.author.id)

    if user_id not in playlists or not playlists[user_id]:
        return await ctx.send("📋 아직 만든 재생 목록이 없습니다. `!목록생성 <이름>` 으로 만들어 보세요!")

    if name:
        if name not in playlists[user_id]:
            return await ctx.send(f"❌ **{name}** 재생 목록이 없습니다.")
        songs = playlists[user_id][name]
        if not songs:
            return await ctx.send(f"📋 **{name}** 목록이 비어 있습니다.")
        embed = discord.Embed(title=f"📋 {name} ({len(songs)}곡)", color=discord.Color.blurple())
        embed.description = "\n".join(f"`{i+1}.` {s['title']}" for i, s in enumerate(songs))
        return await ctx.send(embed=embed)

    embed = discord.Embed(title="📋 내 재생 목록", color=discord.Color.blurple())
    for pl_name, songs in playlists[user_id].items():
        embed.add_field(name=f"🎵 {pl_name}", value=f"{len(songs)}곡", inline=True)
    await ctx.send(embed=embed)


@bot.command(name="목록삭제곡", aliases=["플리삭제곡", "pl_remove"])
async def playlist_remove_song(ctx: commands.Context, name: str, index: int):
    """재생 목록에서 특정 곡을 삭제합니다.
    사용법: !목록삭제곡 <목록이름> <번호>
    예시: !목록삭제곡 팝송 2
    """
    playlists = load_playlists()
    user_id = str(ctx.author.id)

    if user_id not in playlists or name not in playlists[user_id]:
        return await ctx.send(f"❌ **{name}** 재생 목록이 없습니다.")

    songs = playlists[user_id][name]
    if index < 1 or index > len(songs):
        return await ctx.send(f"❌ 올바른 번호를 입력해 주세요. (1 ~ {len(songs)})")

    removed = songs.pop(index - 1)
    save_playlists(playlists)
    await ctx.send(f"🗑️ **{name}** 목록에서 **{removed['title']}** 을 삭제했습니다.")


@bot.command(name="목록삭제", aliases=["플리삭제", "pl_delete"])
async def playlist_delete(ctx: commands.Context, *, name: str):
    """재생 목록 자체를 삭제합니다. 사용법: !목록삭제 <목록이름>"""
    playlists = load_playlists()
    user_id = str(ctx.author.id)

    if user_id not in playlists or name not in playlists[user_id]:
        return await ctx.send(f"❌ **{name}** 재생 목록이 없습니다.")

    del playlists[user_id][name]
    save_playlists(playlists)
    await ctx.send(f"🗑️ **{name}** 재생 목록을 삭제했습니다.")


# ============================================================
#  TTS 설정
# ============================================================
# 사용 가능한 한국어 여성 목소리 목록
TTS_VOICES = {
    "선희": "ko-KR-SunHiNeural",       # 기본 여성 목소리 (밝고 자연스러움)
    "유진": "ko-KR-YuJinNeural",       # 활발하고 친근한 목소리
    "하윤": "ko-KR-HyunsuNeural",      # 차분하고 부드러운 목소리
}
DEFAULT_VOICE = "ko-KR-SunHiNeural"

# 서버별 현재 선택된 목소리 저장
tts_voices: dict[int, str] = {}


# ============================================================
#  TTS 명령어
# ============================================================
@bot.command(name="tts", aliases=["말해", "읽어줘"])
async def tts(ctx: commands.Context, *, text: str):
    """입력한 텍스트를 음성 채널에서 TTS로 읽어줍니다.
    사용법: !tts <텍스트>
    예시: !tts 안녕하세요 반갑습니다
    """
    if len(text) > 200:
        return await ctx.send("❌ 텍스트가 너무 깁니다. 200자 이내로 입력해 주세요.")

    # 음성 채널 자동 참가
    if not ctx.voice_client:
        if ctx.author.voice:
            await ctx.author.voice.channel.connect()
        else:
            return await ctx.send("❌ 먼저 음성 채널에 입장해 주세요.")

    if ctx.voice_client.is_playing():
        return await ctx.send("❌ 현재 다른 오디오가 재생 중입니다. 잠시 후 다시 시도해 주세요.")

    # 현재 서버의 목소리 가져오기
    voice = tts_voices.get(ctx.guild.id, DEFAULT_VOICE)

    # edge-tts로 음성 파일 생성
    tts_file = f"tts_{ctx.guild.id}.mp3"
    try:
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(tts_file)
    except Exception as e:
        return await ctx.send(f"❌ TTS 생성 중 오류가 발생했습니다: {e}")

    # 음성 채널에서 재생
    ctx.voice_client.play(
        discord.FFmpegPCMAudio(tts_file),
        after=lambda e: os.remove(tts_file) if os.path.exists(tts_file) else None
    )

    try:
        await ctx.message.delete()
    except discord.Forbidden:
        pass

    await ctx.send(f"🔊 **{ctx.author.display_name}:** {text}")


@bot.command(name="tts목소리", aliases=["목소리변경", "tts_voice"])
async def tts_voice(ctx: commands.Context, name: str = None):
    """TTS 목소리를 변경합니다.
    사용법: !tts목소리 → 목소리 목록 확인
            !tts목소리 <이름> → 목소리 변경
    예시: !tts목소리 유진
    """
    if name is None:
        # 목소리 목록 출력
        embed = discord.Embed(title="🎙️ 사용 가능한 목소리", color=discord.Color.blurple())
        current = tts_voices.get(ctx.guild.id, DEFAULT_VOICE)
        for voice_name, voice_id in TTS_VOICES.items():
            indicator = " ✅ **(현재 사용 중)**" if voice_id == current else ""
            embed.add_field(name=f"🔊 {voice_name}", value=f"`!tts목소리 {voice_name}`{indicator}", inline=False)
        return await ctx.send(embed=embed)

    if name not in TTS_VOICES:
        return await ctx.send(f"❌ **{name}** 은 없는 목소리입니다. `!tts목소리` 로 목록을 확인해 주세요.")

    tts_voices[ctx.guild.id] = TTS_VOICES[name]
    await ctx.send(f"✅ TTS 목소리를 **{name}** 으로 변경했습니다!")


@bot.command(name="tts끄기", aliases=["말그만", "tts_off"])
async def tts_off(ctx: commands.Context):
    """현재 재생 중인 TTS를 중단합니다."""
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.send("🔇 TTS를 중단했습니다.")
    else:
        await ctx.send("❌ 현재 재생 중인 TTS가 없습니다.")


# ============================================================
#  봇 실행
# ============================================================
bot.run(TOKEN)
