import os
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from google import genai
from groq import Groq

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# Flash — быстрая и самая "щедрая" по бесплатным лимитам модель, этого более
# чем достаточно для чата в Discord. Можно сменить на другую при желании.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

SYSTEM_PROMPT = (
    "Ты дружелюбный помощник в Discord-сервере. Отвечай кратко и по делу, "
    "на русском языке, если не попросили иначе."
)


class AIChat(commands.Cog):
    """ИИ-помощник: Google Gemini как основной вариант, Groq — как
    автоматический запасной, если Gemini недоступен/перегружен."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._gemini_client: genai.Client | None = None
        self._groq_client: Groq | None = None

    def get_gemini_client(self) -> genai.Client:
        if self._gemini_client is None:
            if not GEMINI_API_KEY:
                raise RuntimeError("GEMINI_API_KEY не задан в .env")
            self._gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        return self._gemini_client

    def get_groq_client(self) -> Groq:
        if self._groq_client is None:
            if not GROQ_API_KEY:
                raise RuntimeError("GROQ_API_KEY не задан в .env")
            self._groq_client = Groq(api_key=GROQ_API_KEY)
        return self._groq_client

    async def ask_gemini(self, prompt: str) -> str:
        client = self.get_gemini_client()
        loop = asyncio.get_event_loop()

        def _generate():
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=f"{SYSTEM_PROMPT}\n\nВопрос: {prompt}",
            )
            return response.text

        return await loop.run_in_executor(None, _generate)

    async def ask_groq(self, prompt: str) -> str:
        client = self.get_groq_client()
        loop = asyncio.get_event_loop()

        def _generate():
            completion = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            )
            return completion.choices[0].message.content

        return await loop.run_in_executor(None, _generate)

    async def ask_ai(self, prompt: str, retries: int = 1) -> str:
        """Пробует Groq (быстрее всего) в первую очередь, и только если он
        совсем недоступен — переключается на Gemini как запасной вариант."""
        last_error = None

        if GROQ_API_KEY:
            try:
                return await self.ask_groq(prompt)
            except Exception as e:
                last_error = e
                print(f"[ai_chat] Groq не сработал: {e}")

        if GEMINI_API_KEY:
            print("[ai_chat] Переключаюсь на Gemini (запасной вариант)...")
            for attempt in range(retries + 1):
                try:
                    return await self.ask_gemini(prompt)
                except Exception as e:
                    last_error = e
                    print(f"[ai_chat] Gemini попытка {attempt + 1} не удалась: {e}")
                    if attempt < retries:
                        await asyncio.sleep(2)

        if last_error:
            raise last_error
        raise RuntimeError(
            "Ни один ИИ-провайдер не настроен — впиши GEMINI_API_KEY и/или GROQ_API_KEY в .env"
        )

    @app_commands.command(name="ии", description="Задать вопрос ИИ")
    @app_commands.describe(вопрос="Что хочешь спросить")
    async def ask(self, interaction: discord.Interaction, вопрос: str):
        await interaction.response.defer()

        try:
            answer = await self.ask_ai(вопрос)
        except Exception as e:
            await interaction.followup.send(f"Не удалось получить ответ от ИИ: {e}")
            return

        if len(answer) > 1900:
            answer = answer[:1900] + "…"

        await interaction.followup.send(answer)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if self.bot.user not in message.mentions:
            return

        prompt = message.content
        for mention in message.mentions:
            prompt = prompt.replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "")
        prompt = prompt.strip()

        if not prompt:
            return

        async with message.channel.typing():
            try:
                answer = await self.ask_ai(prompt)
            except Exception as e:
                await message.reply(f"Не удалось получить ответ от ИИ: {e}")
                return

        if len(answer) > 1900:
            answer = answer[:1900] + "…"

        await message.reply(answer)


async def setup(bot: commands.Bot):
    await bot.add_cog(AIChat(bot))
