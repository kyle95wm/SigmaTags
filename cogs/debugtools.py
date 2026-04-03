import discord
from discord import app_commands
from discord.ext import commands


class DebugTools(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="debugmsg", description="Inspect a message by ID")
    @app_commands.describe(message_id="The ID of the message to inspect")
    async def debugmsg(self, interaction: discord.Interaction, message_id: str):
        await interaction.response.defer(ephemeral=True)

        try:
            msg = await interaction.channel.fetch_message(int(message_id))
        except Exception as e:
            await interaction.followup.send(
                f"Failed to fetch message: {type(e).__name__}: {e}",
                ephemeral=True,
            )
            return

        info = {
            "author_id": msg.author.id,
            "content": msg.content,
            "embed_count": len(msg.embeds),
            "embed_dict": msg.embeds[0].to_dict() if msg.embeds else None,
        }

        output = str(info)
        if len(output) > 1900:
            output = output[:1900] + " ... (truncated)"

        await interaction.followup.send(f"```{output}```", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(DebugTools(bot))
