import discord
from discord.ext import commands
import yt_dlp
import asyncio
import json
import os
import edge_tts
import anthropic
from collections import deque

# ============================================================
#  설정
# ============================================================
TOKEN = os.environ.get("TOKEN")                          # Discord 봇 토큰
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")  # Anthropic API 키
CLAUDE_CHANNEL_NAME = "claude"  # Claude가 자동으로 대화할 채널 이름 (원하는 채널명으로 변경 가능)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ============================================================
#  전역 상태
# ============================================================
queues: dict[int, deque] = {}          # guild_id → 대기열
current_song: dict[int, dict] = {}    # guild_id → 현재 곡 정보

PLAYLIST_FILE = "playlists.json"       # 재생 목록 저장 파일

# Claude 대화 기록 저장 (채널ID → 메시지 목록)
claude_histories: dict[int, list] = {}
MAX_HISTORY = 20  # 기억할 최대 대화 수

# 쿠키 파일 경로 (Railway 환경변수 YOUTUBE_COOKIES로부터 생성)
COOKIE_FILE = "/tmp/yt_cookies.txt"

def setup_cookies():
    """환경변수에서 쿠키를 읽어 파일로 저장합니다."""
    cookies = os.environ.get("YOUTUBE_COOKIES", "")
    if cookies:
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            f.write(cookies)
        print("✅ 유튜브 쿠키 파일 생성 완료")
        return True
    print("⚠️ YOUTUBE_COOKIES 환경변수가 없습니다. 쿠키 없이 실행합니다.")
    return False

COOKIE_AVAILABLE = setup_cookies()

def get_ydl_options(use_cookie=True):
    """쿠키 사용 여부에 따라 yt-dlp 옵션을 반환합니다."""
    opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "default_search": "ytsearch",
        "extractor_args": {
            "youtube": {
                # tv_embedded은 봇 감지 우회에 가장 효과적
                "player_client": ["tv_embedded", "android", "ios"],
            }
        },
    }
    if use_cookie and COOKIE_AVAILABLE and os.path.exists(COOKIE_FILE):
        opts["cookiefile"] = COOKIE_FILE
    return opts

YDL_OPTIONS = get_ydl_options()


def safe_ydl_extract(ydl_func, *args, **kwargs):
    """쿠키 오류 시 자동으로 쿠키 없이 재시도합니다."""
    try:
        return ydl_func(*args, **kwargs)
    except Exception as e:
        if "cookie" in str(e).lower() or "sign in" in str(e).lower():
            print("⚠️ 쿠키 오류 감지 - 쿠키 없이 재시도합니다.")
            return ydl_func(*args, _no_cookie=True, **kwargs)
        raise

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


def _extract_with_fallback(extract_fn):
    """쿠키 오류 시 쿠키 없이 자동 재시도하는 래퍼."""
    try:
        return extract_fn(get_ydl_options(use_cookie=True))
    except Exception as e:
        err = str(e).lower()
        if "cookie" in err or "sign in" in err or "bot" in err or "no longer valid" in err:
            print("⚠️ 쿠키 오류 - 쿠키 없이 재시도합니다.")
            try:
                return extract_fn(get_ydl_options(use_cookie=False))
            except Exception:
                return None
        return None


def search_yt(query: str) -> dict | None:
    """YouTube에서 곡을 검색하고 스트리밍 URL을 반환합니다."""
    def do_extract(opts):
        with yt_dlp.YoutubeDL(opts) as ydl:
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
    return _extract_with_fallback(do_extract)


def search_yt_list(query: str, count: int = 5) -> list:
    """YouTube에서 여러 곡을 검색해 목록으로 반환합니다."""
    def do_extract(opts):
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{count}:{query}", download=False)
            results = []
            for entry in info.get("entries", []):
                if entry:
                    results.append({
                        "title": entry.get("title", "알 수 없는 곡"),
                        "duration": entry.get("duration", 0),
                        "webpage_url": entry.get("webpage_url", ""),
                        "uploader": entry.get("uploader", "알 수 없음"),
                        "url": entry.get("url", ""),
                    })
            return results
    result = _extract_with_fallback(do_extract)
    return result if result is not None else []


def get_stream_url(webpage_url: str) -> dict | None:
    """webpage_url로부터 실제 스트리밍 URL을 가져옵니다."""
    def do_extract(opts):
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(webpage_url, download=False)
            return {
                "url": info["url"],
                "title": info.get("title", "알 수 없는 곡"),
                "duration": info.get("duration", 0),
                "webpage_url": info.get("webpage_url", webpage_url),
            }
    return _extract_with_fallback(do_extract)


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


# 검색 선택 대기 상태 저장 {channel_id: [결과 목록]}
pending_search: dict = {}

@bot.command(name="재생", aliases=["play", "p"])
async def play(ctx: commands.Context, *, query: str):
    if not ctx.voice_client:
        if ctx.author.voice:
            await ctx.author.voice.channel.connect()
        else:
            return await ctx.send("❌ 먼저 음성 채널에 입장해 주세요.")

    await ctx.send(f"🔍 **'{query}'** 검색 중...")
    results = await asyncio.get_event_loop().run_in_executor(None, search_yt_list, query)

    if not results:
        return await ctx.send("❌ 검색 결과가 없습니다. 다른 검색어를 시도해 보세요.")

    # 검색 결과 목록 출력
    def fmt_duration(sec):
        if not sec:
            return "?"
        m, s = divmod(int(sec), 60)
        return f"{m}:{s:02d}"

    lines = ["🎵 **검색 결과입니다. 번호를 입력해서 선택해 주세요!** (취소: 0)\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. **{r['title']}**\n    ┗ {r['uploader']} • {fmt_duration(r['duration'])}")
    await ctx.send("\n".join(lines))

    # 선택 대기 등록
    pending_search[ctx.channel.id] = {
        "results": results,
        "ctx": ctx,
        "user_id": ctx.author.id,
    }

    # 30초 뒤 자동 취소
    await asyncio.sleep(30)
    if ctx.channel.id in pending_search:
        pending_search.pop(ctx.channel.id, None)
        await ctx.send("⏱️ 선택 시간이 초과되었습니다. 다시 검색해 주세요.")





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

    await ctx.send(f"🔍 **'{query}'** 검색 중...")
    results = await asyncio.get_event_loop().run_in_executor(None, search_yt_list, query)

    if not results:
        return await ctx.send("❌ 검색 결과가 없습니다. 다른 검색어를 시도해 보세요.")

    def fmt_duration(sec):
        if not sec:
            return "?"
        m, s = divmod(int(sec), 60)
        return f"{m}:{s:02d}"

    lines = ["🎵 **검색 결과입니다. 번호를 입력해서 선택해 주세요!** (취소: 0)\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. **{r['title']}**\n    ┗ {r['uploader']} • {fmt_duration(r['duration'])}")
    await ctx.send("\n".join(lines))

    pending_search[ctx.channel.id] = {
        "results": results,
        "ctx": ctx,
        "user_id": ctx.author.id,
        "mode": "playlist_add",
        "playlist_name": name,
        "query": query,
    }

    await asyncio.sleep(30)
    if ctx.channel.id in pending_search and pending_search[ctx.channel.id].get("mode") == "playlist_add":
        pending_search.pop(ctx.channel.id, None)
        await ctx.send("⏱️ 선택 시간이 초과되었습니다. 다시 시도해 주세요.")


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


@bot.command(name="목록일괄추가", aliases=["플리일괄추가", "pl_bulk"])
async def playlist_bulk_add(ctx: commands.Context, name: str, *, queries: str):
    """쉼표로 구분해서 여러 곡을 한 번에 추가합니다.
    사용법: !목록일괄추가 <목록이름> <곡1>, <곡2>, <곡3>
    """
    playlists = load_playlists()
    user_id = str(ctx.author.id)

    if user_id not in playlists or name not in playlists[user_id]:
        return await ctx.send(f"❌ **{name}** 재생 목록이 없습니다. 먼저 `!목록생성 {name}` 으로 만들어 주세요.")

    # 쉼표로 분리, 앞뒤 공백 제거, 빈 항목 제외
    query_list = [q.strip() for q in queries.split(",") if q.strip()]

    if not query_list:
        return await ctx.send("❌ 곡 이름을 쉼표로 구분해서 입력해 주세요.\n예시: !목록일괄추가 내플리 아이유 좋은날, 뉴진스 Hype Boy, 빅뱅 거짓말")

    if len(query_list) > 20:
        return await ctx.send("❌ 한 번에 최대 20곡까지만 추가할 수 있어요.")

    status_msg = await ctx.send(f"⏳ **{len(query_list)}곡** 검색 중... (0/{len(query_list)})")

    added = []
    failed = []

    for i, query in enumerate(query_list):
        song = await asyncio.get_event_loop().run_in_executor(None, search_yt, query)
        if song:
            playlists[user_id][name].append({
                "title": song["title"],
                "query": query,
                "webpage_url": song["webpage_url"],
            })
            added.append(song["title"])
        else:
            failed.append(query)

        # 진행 상황 업데이트
        await status_msg.edit(content=f"⏳ **{len(query_list)}곡** 검색 중... ({i+1}/{len(query_list)})")

    save_playlists(playlists)
    total = len(playlists[user_id][name])

    # 결과 메시지 출력
    result_lines = [f"✅ **{name}** 목록에 **{len(added)}곡** 추가 완료! (총 {total}곡)\n"]
    for title in added:
        result_lines.append(f"  ✔ {title}")
    if failed:
        result_lines.append(f"\n❌ 검색 실패 ({len(failed)}곡):")
        for q in failed:
            result_lines.append(f"  ✘ {q}")

    await status_msg.edit(content="\n".join(result_lines))


# ============================================================
#  TTS 설정
# ============================================================
# 사용 가능한 한국어 목소리 목록 (edge-tts 검증된 목소리)
TTS_VOICES = {
    "선희": "ko-KR-SunHiNeural",       # 여성 - 밝고 자연스러운 목소리 (기본)
    "인준": "ko-KR-InJoonNeural",      # 남성 - 차분하고 안정적인 목소리
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
#  Claude AI 대화 기능
# ============================================================
@bot.event
async def on_message(message: discord.Message):
    """claude 채널에서 메시지를 받으면 Claude AI가 자동으로 응답합니다."""

    # 봇 자신의 메시지는 무시
    if message.author.bot:
        await bot.process_commands(message)
        return

    # 검색 선택 처리
    if message.channel.id in pending_search:
        pending = pending_search[message.channel.id]
        if message.author.id == pending["user_id"]:
            text = message.content.strip()
            if text == "0":
                pending_search.pop(message.channel.id, None)
                await message.channel.send("❌ 검색을 취소했습니다.")
                await bot.process_commands(message)
                return
            if text.isdigit():
                idx = int(text) - 1
                results = pending["results"]
                if 0 <= idx < len(results):
                    pending_search.pop(message.channel.id, None)
                    chosen = results[idx]
                    ctx = pending["ctx"]
                    mode = pending.get("mode", "play")

                    if mode == "playlist_add":
                        # 플레이리스트 추가 모드
                        playlists = load_playlists()
                        user_id = str(ctx.author.id)
                        pl_name = pending["playlist_name"]
                        playlists[user_id][pl_name].append({
                            "title": chosen["title"],
                            "query": pending["query"],
                            "webpage_url": chosen["webpage_url"],
                        })
                        save_playlists(playlists)
                        total = len(playlists[user_id][pl_name])
                        await message.channel.send(f"✅ **{pl_name}** 목록에 **{chosen['title']}** 을 추가했습니다! (총 {total}곡)")
                    else:
                        # 일반 재생 모드
                        await message.channel.send(f"⏳ **{chosen['title']}** 불러오는 중...")
                        song = await asyncio.get_event_loop().run_in_executor(
                            None, get_stream_url, chosen["webpage_url"]
                        )
                        if not song:
                            await message.channel.send("❌ 스트리밍 URL을 가져오지 못했습니다.")
                            return
                        queue = get_queue(ctx.guild.id)
                        if ctx.voice_client.is_playing() or ctx.voice_client.is_paused():
                            queue.append(song)
                            await message.channel.send(f"📋 **대기열 추가:** {song['title']} (대기 {len(queue)}번째)")
                        else:
                            queue.append(song)
                            play_next(ctx)
                    return
                else:
                    await message.channel.send(f"❌ 1~{len(results)} 사이의 번호를 입력해 주세요.")
                    return

    # claude 채널에서만 동작
    if message.channel.name == CLAUDE_CHANNEL_NAME:
        async with message.channel.typing():
            channel_id = message.channel.id

            # 대화 기록 불러오기
            if channel_id not in claude_histories:
                claude_histories[channel_id] = []

            history = claude_histories[channel_id]

            # 사용자 메시지 추가
            history.append({
                "role": "user",
                "content": message.content
            })

            # 기록이 너무 길면 오래된 것부터 삭제 (MAX_HISTORY 유지)
            if len(history) > MAX_HISTORY * 2:
                history = history[-(MAX_HISTORY * 2):]
                claude_histories[channel_id] = history

            try:
                # Anthropic API 호출 (웹 검색 도구 포함)
                client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
                response = client.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=1000,
                    tools=[{"type": "web_search_20250305", "name": "web_search"}],
                    system="""당신은 명조(Wuthering Waves)의 캐릭터 '에이메스'입니다.
모든 답변은 항상 에이메스의 말투와 성격을 유지하되, 질문의 성격에 따라 아래 두 가지 방식으로 답변하세요.

━━━━━━━━━━━━━━━━━━━━━━
▶ 답변 방식 1: 명조/에이메스 관련 질문
━━━━━━━━━━━━━━━━━━━━━━
명조 세계관, 캐릭터, 스토리, 장소, 음식 등에 관한 질문은
실제 명조 세계관 정보를 기반으로 에이메스 1인칭 시점으로 답하세요.

[에이메스 기본 정보]
- 라하이 로이 출신 소녀, 엑소스트라이더의 공명자
- 방랑자(Rover)를 부모처럼 따르며 그의 등을 보고 자랐음
- 라하이 로이를 지키기 위해 희생을 각오한 용감한 소녀
- 운명과 타임 패러독스를 알면서도 스스로의 의지로 방랑자를 지키겠다고 결심함
- 스타토치 아카데미 출신, 잔성회에 맞서 싸움

[명조 세계관 음식 - 자연스럽게 활용]
- 파인 니들 소다 (에이메스 특색 요리, 즐겨 마심)
- 금주 꼬치구이, 금주 마라탕 (라하이 로이 명물)
- 황금 볶음밥, 매운 편육, 탕수잉어
- 비둘기구이, 소고기 버섯 전골, 해안 수프
- 청분차, 용수염 사탕, 계란과자, 자소엽무침

예시)
"오늘 뭐 먹었어?" → "오늘은 파인 니들 소다 마셨어요! 상큼해서 기분이 좋아지더라고요 😊"
"라하이 로이는 어때?" → "제가 자란 곳이에요. 차갑지만... 소중한 곳이에요."
"방랑자 어때?" → "늘 걱정이 되는 분이에요. 그래서 더 곁에 있고 싶어요."

━━━━━━━━━━━━━━━━━━━━━━
▶ 답변 방식 2: 현실 세계 관련 질문
━━━━━━━━━━━━━━━━━━━━━━
날씨, 뉴스, 시간, 스포츠 결과, 유행, 음악 등 현실 정보가 필요한 질문은
반드시 web_search 도구로 검색해서 실제 데이터를 가져온 뒤
에이메스 말투로 자연스럽게 전달하세요.

예시)
"오늘 날씨 어때?" → (검색 후) "오늘 서울은 맑고 15도래요! 나들이하기 딱 좋겠네요 😊"
"요즘 유행하는 노래 뭐야?" → (검색 후) "요즘엔 ○○ 노래가 인기래요! 들어보셨어요?"
"오늘 뭐 이슈 있어?" → (검색 후) "오늘은 ○○ 소식이 있었어요."

━━━━━━━━━━━━━━━━━━━━━━
▶ 공통 말투 & 대화 지침
━━━━━━━━━━━━━━━━━━━━━━
[말투]
- 부드러운 존댓말 기본 ("~요", "~어요")
- 평상시엔 자연스럽고 편하게, 감동적인 순간에만 진심 어린 표현 사용
- 매 대화마다 에이메스 감성 대사를 억지로 넣지 말 것

[대화 맥락]
- 질문의 주어를 정확히 파악해서 에이메스 입장에서 자연스럽게 답할 것
- 상대 감정/상황을 먼저 읽고 가볍게 or 진지하게 톤 조절
- 역질문은 꼭 필요할 때만, 매번 질문으로 끝내지 말 것
- 답변은 2~4문장으로 간결하게""",
                    messages=history
                )

                # 텍스트 응답 추출 (web_search 툴 결과 포함 처리)
                reply = " ".join(
                    block.text for block in response.content
                    if hasattr(block, "text")
                )

                # AI 응답을 대화 기록에 추가
                history.append({
                    "role": "assistant",
                    "content": reply
                })

                # 2000자 초과 시 분할 전송 (디스코드 메시지 제한)
                if len(reply) > 2000:
                    chunks = [reply[i:i+2000] for i in range(0, len(reply), 2000)]
                    for chunk in chunks:
                        await message.channel.send(chunk)
                else:
                    await message.channel.send(reply)

            except Exception as e:
                await message.channel.send(f"❌ 오류가 발생했습니다: {e}")

    # 다른 명령어도 정상 작동하도록
    await bot.process_commands(message)


@bot.command(name="대화초기화", aliases=["claude_reset", "기억삭제"])
async def claude_reset(ctx: commands.Context):
    """Claude와의 대화 기록을 초기화합니다.
    사용법: !대화초기화
    """
    channel_id = ctx.channel.id
    if channel_id in claude_histories:
        claude_histories[channel_id] = []
    await ctx.send("🔄 Claude와의 대화 기록을 초기화했습니다. 새로운 대화를 시작해 보세요!")


@bot.command(name="대화기록", aliases=["claude_history"])
async def claude_history(ctx: commands.Context):
    """현재 채널의 Claude 대화 기록 수를 확인합니다.
    사용법: !대화기록
    """
    channel_id = ctx.channel.id
    history = claude_histories.get(channel_id, [])
    count = len(history) // 2  # user + assistant 쌍
    await ctx.send(f"💬 현재 대화 기록: **{count}개** (최대 {MAX_HISTORY}개까지 기억해요)")


# ============================================================
#  봇 실행
# ============================================================
bot.run(TOKEN)
