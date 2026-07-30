import os
import re
import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext import voice_recv
import numpy as np
import speech_recognition as sr

log = logging.getLogger(__name__)

# Модель Whisper и язык распознавания настраиваются через .env, чтобы можно
# было подобрать баланс скорости/точности под конкретный сервер без правки
# кода (см. README/памятку по установке — tiny/base/small).
WHISPER_MODEL_SIZE = os.getenv("VOICE_WHISPER_MODEL", "tiny")
WHISPER_LANGUAGE = os.getenv("VOICE_WHISPER_LANGUAGE", "ru")

# "Бот" / "бот," / "Бот:" в начале фразы — всё, что после этого, считаем
# командой. Сама фраза целиком (не только слово) обязательно проходит через
# Whisper по звуку тишины между репликами — заранее слово не ищем, значит
# на CPU оно проверяется постфактум, а не потоковым keyword-spotting.
WAKE_WORD_RE = re.compile(r"^\s*бот[,:]?\s+", re.IGNORECASE)


class VoiceControl(commands.Cog):
    """Постоянное голосовое управление музыкой в войсе: скажи что-то вроде
    «Бот, поставь Skrillex» — бот сам распознаёт речь локально (faster-whisper)
    и запускает трек, без сторонних платных API и без ручного ввода команд.

    Включается/выключается командой /голосуправление на конкретный голосовой
    сеанс — по умолчанию всегда выключено, чтобы не слушать канал без явного
    запроса (см. обсуждение приватности в чате перед реализацией)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._model = None  # faster-whisper модель — грузим лениво при первом /голосуправление вкл
        self._sinks: dict[int, voice_recv.AudioSink] = {}
        self._text_channels: dict[int, discord.abc.Messageable] = {}

    def _get_model(self):
        """Загружает модель Whisper один раз и переиспользует дальше —
        загрузка весов на каждую фразу была бы неприемлемо медленной
        на 1 vCPU. Вызывается из executor'а (блокирующая операция)."""
        if self._model is None:
            from faster_whisper import WhisperModel

            print(
                f"[voice_control] Загружаю модель Whisper '{WHISPER_MODEL_SIZE}' "
                f"(может занять время при первом запуске — скачивает веса)..."
            )
            self._model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
            print("[voice_control] Модель Whisper загружена")
        return self._model

    # ---------- Колбэки распознавания (внимание: выполняются в фоновом
    # потоке SpeechRecognition, а НЕ в asyncio event loop бота!) ----------

    def _transcribe(self, recognizer: sr.Recognizer, audio: sr.AudioData, user) -> str | None:
        """process_cb для SpeechRecognitionSink. Только синхронные вычисления —
        никакого discord.py API здесь, мы не в event loop'е."""
        try:
            # 48kHz моно (так подаёт DiscordSRAudioSource) -> 16kHz для Whisper
            raw = audio.get_raw_data(convert_rate=16000, convert_width=2)
            audio_array = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

            model = self._get_model()
            segments, _info = model.transcribe(
                audio_array,
                language=WHISPER_LANGUAGE,
                beam_size=1,
                vad_filter=False,  # сегментацию речи уже сделал SpeechRecognition
            )
            text = " ".join(seg.text for seg in segments).strip()
            return text or None
        except Exception as e:
            log.exception("Ошибка распознавания голоса: %s", e)
            return None

    def _handle_text(self, user, text: str | None):
        """text_cb — тоже вызывается из фонового потока, не из event loop'а."""
        if not text:
            return

        match = WAKE_WORD_RE.match(text)
        if not match:
            return  # реплика не адресована боту — молча игнорируем, это нормально

        query = text[match.end():].strip()
        if not query:
            return

        print(f"[voice_control] Услышал команду от {user}: «{query}»")

        # Переходим в event loop бота — дальше уже можно работать с discord.py
        asyncio.run_coroutine_threadsafe(self._run_voice_command(user, query), self.bot.loop)

    async def _run_voice_command(self, user, query: str):
        music_cog = self.bot.get_cog("Music")
        if music_cog is None:
            return

        guild = getattr(user, "guild", None)
        if guild is None:
            return

        text_channel = self._text_channels.get(guild.id)
        if text_channel is None:
            return

        try:
            result = await music_cog.play_query_for_voice(guild, text_channel, query)
        except Exception as e:
            result = f"Ошибка голосового управления: {e}"

        try:
            await text_channel.send(f"🎙️ **{user.display_name}**: «{query}»\n{result}")
        except Exception as e:
            print(f"[voice_control] Не удалось отправить сообщение: {e}")

    # ---------- Команда включения/выключения ----------

    @app_commands.command(
        name="голосуправление",
        description="Включить/выключить постоянное голосовое управление музыкой в этом войсе",
    )
    @app_commands.describe(состояние="вкл или выкл")
    @app_commands.choices(
        состояние=[
            app_commands.Choice(name="вкл", value="вкл"),
            app_commands.Choice(name="выкл", value="выкл"),
        ]
    )
    async def voice_control_toggle(
        self, interaction: discord.Interaction, состояние: app_commands.Choice[str]
    ):
        await interaction.response.defer()
        guild = interaction.guild

        if состояние.value == "выкл":
            voice_client = guild.voice_client
            if isinstance(voice_client, voice_recv.VoiceRecvClient) and voice_client.is_listening():
                voice_client.stop_listening()
            self._sinks.pop(guild.id, None)
            self._text_channels.pop(guild.id, None)
            await interaction.followup.send("🔇 Голосовое управление выключено.")
            return

        if interaction.user.voice is None:
            await interaction.followup.send("Сначала зайди в голосовой канал!")
            return

        voice_channel = interaction.user.voice.channel
        voice_client = guild.voice_client

        if voice_client is None:
            try:
                voice_client = await asyncio.wait_for(
                    voice_channel.connect(cls=voice_recv.VoiceRecvClient), timeout=15
                )
            except Exception as e:
                await interaction.followup.send(f"Ошибка подключения к каналу: {e}")
                return
        elif voice_client.channel != voice_channel:
            await voice_client.move_to(voice_channel)

        if not isinstance(voice_client, voice_recv.VoiceRecvClient):
            # Бот уже был подключён к войсу обычным способом (например, если
            # где-то в коде остался старый voice_channel.connect() без cls=) —
            # такой клиент физически не умеет принимать голос.
            await interaction.followup.send(
                "Бот уже в войсе, но подключён без поддержки приёма голоса. "
                "Выгони его командой /leave и запусти /голосуправление вкл ещё раз."
            )
            return

        if voice_client.is_listening():
            await interaction.followup.send("Голосовое управление уже включено в этом канале.")
            return

        await interaction.followup.send(
            "🎙️ Включаю голосовое управление, секунду (гружу модель распознавания)..."
        )

        # Загрузка модели — блокирующая операция, уводим в отдельный поток,
        # чтобы не подвесить event loop бота на время загрузки весов.
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._get_model)
        except Exception as e:
            await interaction.followup.send(f"Не удалось загрузить модель распознавания: {e}")
            return

        sink = voice_recv.extras.speechrecognition.SpeechRecognitionSink(
            process_cb=self._transcribe,
            text_cb=self._handle_text,
            phrase_time_limit=8,
        )
        self._sinks[guild.id] = sink
        self._text_channels[guild.id] = interaction.channel

        voice_client.listen(sink)

        await interaction.channel.send(
            "✅ Голосовое управление включено. Скажи, например: «Бот, поставь Skrillex» — "
            "работает постоянно в этом канале, пока не выключишь через /голосуправление выкл."
        )

    # ---------- Подчистка состояния, если бот сам вышел/был выгнан из войса ----------

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ):
        if member.id != self.bot.user.id:
            return
        if after.channel is not None:
            return

        guild = before.channel.guild if before.channel else None
        if guild is None:
            return

        self._sinks.pop(guild.id, None)
        self._text_channels.pop(guild.id, None)


async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceControl(bot))
