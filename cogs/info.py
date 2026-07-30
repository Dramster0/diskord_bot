import discord
from discord.ext import commands

# Понятные русские названия разделов для каждого модуля (cog).
# Ключ — точное имя класса cog'а, значение — как показывать раздел в /info.
CATEGORY_NAMES = {
    "Music": "🎵 Музыка",
    "Jokes": "😈 Шутки",
    "DotaTracker": "🎮 Dota 2 турнир",
    "Info": "ℹ️ Прочее",
}


class Info(commands.Cog):
    """Команда /info — список всех доступных команд бота по категориям."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @discord.app_commands.command(
        name="info", description="Показать список всех команд бота"
    )
    async def info(self, interaction: discord.Interaction):
        # Собираем команды, группируя по имени cog'а, к которому они привязаны
        grouped: dict[str, list[discord.app_commands.Command]] = {}

        for command in self.bot.tree.walk_commands():
            if isinstance(command, discord.app_commands.Group):
                continue  # подгруппы пропускаем, интересуют только конечные команды

            cog_name = command.binding.__class__.__name__ if command.binding else "Прочее"
            grouped.setdefault(cog_name, []).append(command)

        embed = discord.Embed(
            title="Список команд",
            description="Вот всё, что я умею на данный момент:",
            color=discord.Color.blurple(),
        )

        # Сначала — известные категории в заданном порядке, потом всё остальное
        ordered_keys = [k for k in CATEGORY_NAMES if k in grouped]
        remaining_keys = [k for k in grouped if k not in CATEGORY_NAMES]

        for cog_name in ordered_keys + remaining_keys:
            commands_list = grouped[cog_name]
            display_name = CATEGORY_NAMES.get(cog_name, cog_name)

            lines = []
            for cmd in sorted(commands_list, key=lambda c: c.name):
                lines.append(f"`/{cmd.name}` — {cmd.description}")

            embed.add_field(name=display_name, value="\n".join(lines), inline=False)

        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Info(bot))
