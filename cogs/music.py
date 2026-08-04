import os
import re
import time
import asyncio
import tempfile
import discord
from discord.ext import commands
from discord import app_commands
import yt_dlp
import aiohttp
from yandex_music import Client as YandexClient

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",
    # source_address на 0.0.0.0 (IPv4) не нужен — IPv6 отключён на уровне ОС
    # сервера (/etc/sysctl.conf), так что весь трафик и так идёт по IPv4.
    "remote_components": ["ejs:github"],
}

# Отдельный "быстрый" экземпляр — только достаёт список видео из плейлиста
# (id, название), без разрешения потоковой ссылки для каждого видео сразу.
YTDL_FLAT_OPTIONS = {
    "quiet": True,
    "extract_flat": "in_playlist",
    "skip_download": True,
}

FFMPEG_OPTIONS = {
    # На входе — обычный полностью скачанный локальный файл (yt-dlp качает
    # его целиком заранее), не поток и не "труба" — так что специальные
    # флаги для живого вещания тут не нужны.
    "before_options": "",
    "options": "-vn",
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)
ytdl_flat = yt_dlp.YoutubeDL(YTDL_FLAT_OPTIONS)

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_PLAYLIST_RE = re.compile(r"open\.spotify\.com/playlist/([a-zA-Z0-9]+)")
YOUTUBE_PLAYLIST_RE = re.compile(r"[?&]list=([a-zA-Z0-9_-]+)")

YANDEX_MUSIC_TOKEN = os.getenv("YANDEX_MUSIC_TOKEN", "")
YANDEX_PLAYLIST_RE = re.compile(r"music\.yandex\.\w+/users/([^/?]+)/playlists/(\d+)")
YANDEX_PLAYLIST_UUID_RE = re.compile(
    r"music\.yandex\.\w+/playlists/([0-9a-fA-F-]{36})"
)


class MusicControlView(discord.ui.View):
    """Кнопки под сообщением 'Играю: ...' — пауза/продолжить, скип, стоп."""

    def __init__(self, cog: "Music"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="⏸️ Пауза", style=discord.ButtonStyle.secondary)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc is None:
            await interaction.response.send_message("Бот не в голосовом канале.", ephemeral=True)
            return

        if vc.is_playing():
            vc.pause()
            button.label = "▶️ Продолжить"
        elif vc.is_paused():
            vc.resume()
            button.label = "⏸️ Пауза"
        else:
            await interaction.response.send_message("Сейчас ничего не играет.", ephemeral=True)
            return

        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="⏭️ Скип", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc is None or not (vc.is_playing() or vc.is_paused()):
            await interaction.response.send_message("Сейчас ничего не играет.", ephemeral=True)
            return
        await interaction.response.edit_message(view=None)
        self.cog.now_playing_messages.pop(interaction.guild.id, None)
        vc.stop()  # запускает play_next через колбэк after_playing
        await interaction.followup.send("⏭️ Трек пропущен.", ephemeral=True)

    @discord.ui.button(label="⏹️ Стоп", style=discord.ButtonStyle.danger)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        self.cog.get_queue(interaction.guild.id).clear()
        await interaction.response.edit_message(view=None)
        self.cog.now_playing_messages.pop(interaction.guild.id, None)
        if vc:
            vc.stop()
            await vc.disconnect()
        await interaction.followup.send("⏹️ Остановлено, очередь очищена.", ephemeral=True)


class Music(commands.Cog):
    """Всё, что связано с проигрыванием музыки в голосовых каналах."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Очередь треков для каждого сервера: {guild_id: [ {title, url, ...}, ... ]}
        self.queues: dict[int, list[dict]] = {}
        # Канал, куда постить "Играю: ..." при автоматической смене трека —
        # запоминаем последний канал, откуда была команда /play или /playlist
        self.now_playing_channels: dict[int, discord.abc.Messageable] = {}
        # Последнее отправленное сообщение "Играю: ..." — чтобы убрать с него
        # кнопки, когда трек закончится (сам, по скипу или по стопу)
        self.now_playing_messages: dict[int, discord.Message] = {}
        self._spotify_token: str | None = None
        self._spotify_token_expires: float = 0
        self._yandex_client: YandexClient | None = None

    def get_queue(self, guild_id: int) -> list[dict]:
        if guild_id not in self.queues:
            self.queues[guild_id] = []
        return self.queues[guild_id]

    # ---------- Поиск и скачивание треков ----------

    async def extract_track(self, query: str, retries: int = 2) -> dict:
        loop = asyncio.get_event_loop()
        last_error = None
        search_start = time.monotonic()

        for attempt in range(retries + 1):
            try:
                data = await loop.run_in_executor(
                    None, lambda: ytdl.extract_info(query, download=False)
                )
                if "entries" in data:
                    data = data["entries"][0]

                search_time = time.monotonic() - search_start
                print(f"[music] Поиск трека «{query}» занял {search_time:.1f} сек")

                return {
                    "title": data.get("title", "Неизвестный трек"),
                    "url": data.get("webpage_url", query),
                }
            except Exception as e:
                last_error = e
                # DNS-обрывы и подобные сетевые сбои часто временные —
                # пробуем ещё раз перед тем, как сдаться
                if attempt < retries:
                    print(f"[music] Попытка {attempt + 1} не удалась ({e}), пробую снова...")
                    await asyncio.sleep(1.5)

        raise last_error

    async def fetch_youtube_playlist_videos(self, playlist_url: str) -> list[str]:
        """Быстро получает список прямых ссылок на видео YouTube-плейлиста
        (без разрешения потоковых ссылок — это происходит по одному позже)."""
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None, lambda: ytdl_flat.extract_info(playlist_url, download=False)
        )
        entries = data.get("entries", [])
        urls = []
        for entry in entries:
            if not entry:
                continue
            video_id = entry.get("id") or entry.get("url")
            if video_id:
                urls.append(f"https://www.youtube.com/watch?v={video_id}")
        return urls

    # ---------- Spotify: получение токена и списка треков плейлиста ----------

    async def get_spotify_token(self) -> str:
        """Получает токен доступа Spotify через Client Credentials Flow —
        этого достаточно для чтения публичных плейлистов, вход пользователя
        не требуется. Токен кэшируется и переиспользуется, пока не истечёт."""
        if self._spotify_token and time.time() < self._spotify_token_expires:
            return self._spotify_token

        if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
            raise RuntimeError(
                "Spotify не настроен — впиши SPOTIFY_CLIENT_ID и SPOTIFY_CLIENT_SECRET в .env"
            )

        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://accounts.spotify.com/api/token",
                data={"grant_type": "client_credentials"},
                auth=aiohttp.BasicAuth(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

        self._spotify_token = data["access_token"]
        self._spotify_token_expires = time.time() + data.get("expires_in", 3600) - 60
        return self._spotify_token

    async def fetch_spotify_playlist_tracks(self, playlist_id: str) -> list[str]:
        """Возвращает список треков плейлиста в виде строк 'Исполнитель - Название'.
        Примечание: с февраля 2026 Spotify отдаёт полный список треков только
        для ПУБЛИЧНЫХ плейлистов, принадлежащих самому владельцу API-приложения —
        для чужих плейлистов вернётся 403 Forbidden, это ограничение самого Spotify."""
        token = await self.get_spotify_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = (
            f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks"
            f"?fields=items(track(name,artists(name))),next&limit=100"
        )

        tracks = []
        async with aiohttp.ClientSession(headers=headers) as session:
            while url:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

                for item in data.get("items", []):
                    track = item.get("track")
                    if not track:
                        continue
                    name = track.get("name", "")
                    artists = ", ".join(a["name"] for a in track.get("artists", []))
                    if name:
                        tracks.append(f"{artists} - {name}" if artists else name)

                url = data.get("next")

        return tracks

    # ---------- Яндекс Музыка (неофициальная библиотека) ----------

    async def get_yandex_client(self) -> YandexClient:
        if self._yandex_client is not None:
            return self._yandex_client

        if not YANDEX_MUSIC_TOKEN:
            raise RuntimeError(
                "Яндекс Музыка не настроена — впиши YANDEX_MUSIC_TOKEN в .env "
                "(получи его через get_yandex_token.py)"
            )

        loop = asyncio.get_event_loop()
        self._yandex_client = await loop.run_in_executor(
            None, lambda: YandexClient(YANDEX_MUSIC_TOKEN).init()
        )
        return self._yandex_client

    async def fetch_yandex_playlist_tracks(self, owner: str, kind: int) -> list[str]:
        """Возвращает список треков плейлиста Яндекс Музыки в виде строк
        'Исполнитель - Название'. Примечание: Яндекс Музыка ограничивает доступ
        по географии (451 Unavailable For Legal Reasons для не-РФ/СНГ серверов)."""
        client = await self.get_yandex_client()
        loop = asyncio.get_event_loop()

        def _fetch():
            playlist = client.users_playlists(kind, user_id=owner)
            return playlist.fetch_tracks()

        full_tracks = await loop.run_in_executor(None, _fetch)

        result = []
        for track in full_tracks:
            title = track.title or ""
            artists = ", ".join(a.name for a in track.artists) if track.artists else ""
            if title:
                result.append(f"{artists} - {title}" if artists else title)
        return result

    async def fetch_yandex_playlist_by_uuid(self, playlist_uuid: str) -> list[str]:
        """Возвращает список треков плейлиста Яндекс Музыки по его UUID
        (новый формат коротких ссылок вида music.yandex.ru/playlists/<uuid>)."""
        client = await self.get_yandex_client()
        loop = asyncio.get_event_loop()

        def _fetch():
            playlist = client.playlist(playlist_uuid)
            if playlist is None:
                return []
            return playlist.fetch_tracks()

        full_tracks = await loop.run_in_executor(None, _fetch)

        result = []
        for track in full_tracks:
            title = track.title or ""
            artists = ", ".join(a.name for a in track.artists) if track.artists else ""
            if title:
                result.append(f"{artists} - {title}" if artists else title)
        return result

    # ---------- Воспроизведение ----------

    async def play_next(self, guild: discord.Guild, voice_client: discord.VoiceClient):
        # Убираем кнопки с предыдущего сообщения "Играю" — трек уже закончился
        # (сам по себе, по скипу или иначе), кнопки на нём больше не актуальны
        previous_message = self.now_playing_messages.pop(guild.id, None)
        if previous_message is not None:
            try:
                await previous_message.edit(view=None)
            except Exception as e:
                print(f"[music] Не удалось убрать кнопки со старого сообщения: {e}")

        queue = self.get_queue(guild.id)
        if not queue:
            return

        track = queue.pop(0)

        # ВАЖНО: качаем трек через встроенный загрузчик yt-dlp (download=True),
        # а НЕ через ручной curl/aiohttp! Проверено на практике: ручной curl
        # тянул файлы по 50-100+ секунд без видимой причины (хотя сеть в
        # остальном быстрая), тогда как встроенный загрузчик yt-dlp скачивает
        # тот же файл меньше чем за секунду — у него свои оптимизации под
        # googlevideo.com, которых не хватает голому curl.
        tmp_dir = tempfile.mkdtemp()
        tmp_template = os.path.join(tmp_dir, "track.%(ext)s")

        download_options = dict(YTDL_OPTIONS)
        download_options["outtmpl"] = tmp_template
        download_options["quiet"] = True

        loop = asyncio.get_event_loop()
        tmp_path = None

        def _download():
            with yt_dlp.YoutubeDL(download_options) as dl:
                info = dl.extract_info(track["url"], download=True)
                if "entries" in info:
                    info = info["entries"][0]
                return dl.prepare_filename(info)

        try:
            download_start = time.monotonic()
            tmp_path = await loop.run_in_executor(None, _download)
            download_time = time.monotonic() - download_start
            file_size = os.path.getsize(tmp_path) if tmp_path and os.path.exists(tmp_path) else 0
            print(
                f"[music] Скачивание заняло {download_time:.1f} сек, "
                f"размер файла {file_size / 1024:.0f} КБ"
            )
        except Exception as e:
            print(f"[music] Ошибка скачивания трека: {e}")

        source = discord.FFmpegPCMAudio(tmp_path, **FFMPEG_OPTIONS)

        def after_playing(error):
            if error:
                print(f"Ошибка воспроизведения: {error}")
            try:
                if tmp_path and os.path.exists(tmp_path):
                    os.remove(tmp_path)
                os.rmdir(tmp_dir)
            except Exception:
                pass
            fut = asyncio.run_coroutine_threadsafe(
                self.play_next(guild, voice_client), self.bot.loop
            )
            try:
                fut.result()
            except Exception as e:
                print(f"Ошибка при переходе к след. треку: {e}")

        voice_client.play(source, after=after_playing)

        channel = self.now_playing_channels.get(guild.id)
        if channel is not None:
            try:
                sent_message = await channel.send(
                    f"▶️ Играю: **{track['title']}**", view=MusicControlView(self)
                )
                self.now_playing_messages[guild.id] = sent_message
            except Exception as e:
                print(f"[music] Не удалось отправить сообщение 'Играю': {e}")

    # ---------- Команды ----------

    @app_commands.command(name="play", description="Включить трек по названию или ссылке")
    @app_commands.describe(query="Название песни или ссылка (YouTube и т.д.)")
    async def play(self, interaction: discord.Interaction, query: str):
        print("[play] команда получена, query =", query)
        await interaction.response.defer()
        print("[play] defer() прошёл")

        if interaction.user.voice is None:
            await interaction.followup.send("Сначала зайди в голосовой канал!")
            return

        voice_channel = interaction.user.voice.channel
        voice_client = interaction.guild.voice_client
        print(f"[play] пользователь в канале: {voice_channel}, voice_client={voice_client}")

        if voice_client is None:
            print("[play] пытаюсь подключиться к голосовому каналу...")
            try:
                voice_client = await asyncio.wait_for(voice_channel.connect(), timeout=15)
                print("[play] подключение к голосовому каналу успешно")
            except asyncio.TimeoutError:
                print("[play] ТАЙМАУТ подключения к голосовому каналу (15 сек)")
                await interaction.followup.send(
                    "Не получилось подключиться к голосовому каналу за 15 секунд "
                    "(похоже, блокируются голосовые сервера Discord)."
                )
                return
            except Exception as e:
                print(f"[play] ОШИБКА подключения к голосовому каналу: {e}")
                await interaction.followup.send(f"Ошибка подключения к каналу: {e}")
                return
        elif voice_client.channel != voice_channel:
            await voice_client.move_to(voice_channel)

        print("[play] начинаю поиск трека через yt-dlp...")
        try:
            track = await asyncio.wait_for(self.extract_track(query), timeout=20)
            print(f"[play] трек найден: {track['title']}")
        except asyncio.TimeoutError:
            print("[play] ТАЙМАУТ поиска трека (20 сек)")
            await interaction.followup.send("Поиск трека занял слишком много времени.")
            return
        except Exception as e:
            print(f"[play] ОШИБКА поиска трека: {e}")
            await interaction.followup.send(f"Не удалось найти трек: {e}")
            return

        self.now_playing_channels[interaction.guild.id] = interaction.channel

        queue = self.get_queue(interaction.guild.id)
        queue.append(track)

        if not voice_client.is_playing() and not voice_client.is_paused():
            await self.play_next(interaction.guild, voice_client)
            await interaction.followup.send(f"🎶 Добавил в очередь: **{track['title']}**")
        else:
            await interaction.followup.send(f"➕ Добавлено в очередь: **{track['title']}**")

    @app_commands.command(
        name="playlist", description="Добавить в очередь весь плейлист (Spotify, YouTube или Яндекс Музыка) по ссылке"
    )
    @app_commands.describe(ссылка="Ссылка на публичный плейлист Spotify, YouTube или Яндекс Музыки")
    async def playlist(self, interaction: discord.Interaction, ссылка: str):
        await interaction.response.defer()

        if interaction.user.voice is None:
            await interaction.followup.send("Сначала зайди в голосовой канал!")
            return

        spotify_match = SPOTIFY_PLAYLIST_RE.search(ссылка)
        youtube_match = YOUTUBE_PLAYLIST_RE.search(ссылка)
        yandex_match = YANDEX_PLAYLIST_RE.search(ссылка)
        yandex_uuid_match = YANDEX_PLAYLIST_UUID_RE.search(ссылка)

        if spotify_match:
            try:
                track_queries = await self.fetch_spotify_playlist_tracks(spotify_match.group(1))
            except Exception as e:
                await interaction.followup.send(f"Не удалось получить плейлист Spotify: {e}")
                return
        elif youtube_match:
            try:
                track_queries = await self.fetch_youtube_playlist_videos(ссылка)
            except Exception as e:
                await interaction.followup.send(f"Не удалось получить плейлист YouTube: {e}")
                return
        elif yandex_match:
            try:
                owner, kind = yandex_match.group(1), int(yandex_match.group(2))
                track_queries = await self.fetch_yandex_playlist_tracks(owner, kind)
            except Exception as e:
                await interaction.followup.send(f"Не удалось получить плейлист Яндекс Музыки: {e}")
                return
        elif yandex_uuid_match:
            try:
                track_queries = await self.fetch_yandex_playlist_by_uuid(yandex_uuid_match.group(1))
            except Exception as e:
                await interaction.followup.send(f"Не удалось получить плейлист Яндекс Музыки: {e}")
                return
        else:
            await interaction.followup.send(
                "Пока понимаю только ссылки на плейлисты Spotify "
                "(open.spotify.com/playlist/...), YouTube (список с ?list=...) "
                "или Яндекс Музыки (music.yandex.ru/users/.../playlists/... "
                "или music.yandex.ru/playlists/<uuid>)."
            )
            return

        if not track_queries:
            await interaction.followup.send("В этом плейлисте не нашлось треков.")
            return

        voice_channel = interaction.user.voice.channel
        voice_client = interaction.guild.voice_client
        if voice_client is None:
            try:
                voice_client = await asyncio.wait_for(voice_channel.connect(), timeout=15)
            except Exception as e:
                await interaction.followup.send(f"Ошибка подключения к каналу: {e}")
                return
        elif voice_client.channel != voice_channel:
            await voice_client.move_to(voice_channel)

        self.now_playing_channels[interaction.guild.id] = interaction.channel

        await interaction.followup.send(
            f"🎵 Нашёл **{len(track_queries)}** треков в плейлисте, начинаю добавлять "
            f"в очередь по одному (это может занять пару минут)..."
        )

        asyncio.create_task(
            self._queue_playlist_tracks(interaction, voice_client, track_queries)
        )

    async def _queue_playlist_tracks(self, interaction, voice_client, track_queries: list[str]):
        guild = interaction.guild
        queue = self.get_queue(guild.id)
        added = 0
        skipped = 0

        for query in track_queries:
            try:
                track = await self.extract_track(query, retries=1)
                queue.append(track)
                added += 1

                if not voice_client.is_playing() and not voice_client.is_paused():
                    await self.play_next(guild, voice_client)
            except Exception as e:
                print(f"[playlist] Пропускаю трек «{query}»: {e}")
                skipped += 1

        summary = f"✅ Готово: добавлено **{added}** треков из плейлиста."
        if skipped:
            summary += f" Не удалось найти **{skipped}**."
        await interaction.followup.send(summary)

    @app_commands.command(name="skip", description="Пропустить текущий трек")
    async def skip(self, interaction: discord.Interaction):
        voice_client = interaction.guild.voice_client
        if voice_client is None or not (voice_client.is_playing() or voice_client.is_paused()):
            await interaction.response.send_message("Сейчас ничего не играет.")
            return
        voice_client.stop()
        await interaction.response.send_message("⏭️ Трек пропущен.")

    @app_commands.command(name="pause", description="Поставить на паузу")
    async def pause(self, interaction: discord.Interaction):
        voice_client = interaction.guild.voice_client
        if voice_client and voice_client.is_playing():
            voice_client.pause()
            await interaction.response.send_message("⏸️ Пауза.")
        else:
            await interaction.response.send_message("Сейчас ничего не играет.")

    @app_commands.command(name="unpause", description="Продолжить воспроизведение")
    async def unpause(self, interaction: discord.Interaction):
        voice_client = interaction.guild.voice_client
        if voice_client and voice_client.is_paused():
            voice_client.resume()
            await interaction.response.send_message("▶️ Продолжаю.")
        else:
            await interaction.response.send_message("Плеер не на паузе.")

    @app_commands.command(name="queue", description="Показать очередь треков")
    async def show_queue(self, interaction: discord.Interaction):
        queue = self.get_queue(interaction.guild.id)
        if not queue:
            await interaction.response.send_message("Очередь пуста.")
            return
        lines = [f"{i+1}. {t['title']}" for i, t in enumerate(queue[:10])]
        await interaction.response.send_message("**Очередь:**\n" + "\n".join(lines))

    @app_commands.command(name="stop", description="Остановить музыку и очистить очередь")
    async def stop(self, interaction: discord.Interaction):
        voice_client = interaction.guild.voice_client
        self.get_queue(interaction.guild.id).clear()

        previous_message = self.now_playing_messages.pop(interaction.guild.id, None)
        if previous_message is not None:
            try:
                await previous_message.edit(view=None)
            except Exception:
                pass

        if voice_client:
            voice_client.stop()
            await voice_client.disconnect()
        await interaction.response.send_message("⏹️ Остановлено, очередь очищена.")

    @app_commands.command(name="leave", description="Выгнать бота из голосового канала")
    async def leave(self, interaction: discord.Interaction):
        voice_client = interaction.guild.voice_client
        if voice_client:
            previous_message = self.now_playing_messages.pop(interaction.guild.id, None)
            if previous_message is not None:
                try:
                    await previous_message.edit(view=None)
                except Exception:
                    pass
            await voice_client.disconnect()
            await interaction.response.send_message("Вышел из канала.")
        else:
            await interaction.response.send_message("Я и так не в канале.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
