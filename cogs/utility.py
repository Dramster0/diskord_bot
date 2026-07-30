import discord
from discord.ext import commands
from discord import app_commands


class Utility(commands.Cog):
    """Разные вспомогательные команды, не привязанные к конкретной теме."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="clear", description="Удалить сообщения бота в этом канале"
    )
    @app_commands.describe(
        количество="Сколько последних сообщений бота удалить, или 'all' для всех"
    )
    async def clear(self, interaction: discord.Interaction, количество: str):
        await interaction.response.defer(ephemeral=True)

        target = количество.strip().lower()
        if target == "all":
            limit_count = None
        else:
            try:
                limit_count = int(target)
            except ValueError:
                await interaction.followup.send(
                    "Укажи число (например 10) или слово `all` для удаления всех сообщений."
                )
                return
            if limit_count <= 0:
                await interaction.followup.send("Число должно быть больше нуля.")
                return

        channel = interaction.channel
        deleted = 0

        async for msg in channel.history(limit=None):
            if msg.author.id != self.bot.user.id:
                continue
            try:
                await msg.delete()
                deleted += 1
            except discord.HTTPException as e:
                print(f"[utility] Не удалось удалить сообщение: {e}")
                continue

            if limit_count is not None and deleted >= limit_count:
                break

        await interaction.followup.send(f"🗑️ Удалено сообщений бота: {deleted}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Utility(bot))
