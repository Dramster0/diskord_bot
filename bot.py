import os
import asyncio
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()  # читает переменные из файла .env
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True  # пригодится для приветствий, модерации и т.п.

bot = commands.Bot(command_prefix="!", intents=intents)

# Список модулей (cogs), которые нужно загрузить.
# Чтобы добавить новую функцию — создаёшь файл в cogs/ и дописываешь его сюда.
INITIAL_COGS = [
    "cogs.music",
    "cogs.dota_tracker",
    "cogs.info",
    "cogs.ai_chat",
    "cogs.utility",
    "cogs.voice_control",
]


@bot.event
async def on_ready():
    print(f"Бот запущен как {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"Синхронизировано {len(synced)} слэш-команд")
    except Exception as e:
        print(f"Ошибка синхронизации команд: {e}")


async def main():
    if not TOKEN:
        raise RuntimeError(
            "Токен не найден. Проверь, что файл .env существует и содержит DISCORD_TOKEN=..."
        )

    async with bot:
        for cog in INITIAL_COGS:
            await bot.load_extension(cog)
            print(f"Загружен модуль: {cog}")
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
