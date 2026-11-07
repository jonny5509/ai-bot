import discord
from discord import app_commands
from discord.ext import commands


class AccountCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="unlink", description="Disconnect your linked EmlaLock account")
    async def unlink(self, interaction: discord.Interaction):
        removed = self.bot.storage.unlink_account(interaction.user.id)
        await interaction.response.send_message(
            "Your EmlaLock account has been disconnected." if removed
            else "You do not have a linked EmlaLock account.", ephemeral=True)

    @app_commands.command(name="account", description="Check your EmlaLock link status")
    async def account(self, interaction: discord.Interaction):
        linked = self.bot.storage.get_account(interaction.user.id) is not None
        await interaction.response.send_message(
            "Your EmlaLock account is linked." if linked
            else "No EmlaLock account is linked yet; secure linking is not enabled yet.",
            ephemeral=True)

    @app_commands.command(name="help", description="Show bot usage")
    async def help_command(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "Mention me with a request such as add 2 hours, subtract 30 minutes, or session info. "
            "Time changes remain disabled until account linking, session selection, permission "
            "checks, and explicit confirmation are implemented.", ephemeral=True)
