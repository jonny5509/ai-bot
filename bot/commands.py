import discord
from discord import app_commands
from discord.ext import commands

from emlalock.api import EmlaLockClient, EmlaLockError
from emlalock.time_parser import parse_duration


class LinkAccountModal(discord.ui.Modal, title="Link your EmlaLock account"):
    emlalock_user_id = discord.ui.TextInput(
        label="EmlaLock User ID", placeholder="Your User ID from EmlaLock settings",
        min_length=4, max_length=80)
    api_key = discord.ui.TextInput(
        label="EmlaLock API key", placeholder="Your API key (private)",
        min_length=8, max_length=200, style=discord.TextStyle.short)

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        user_id = str(self.emlalock_user_id.value).strip()
        key = str(self.api_key.value).strip()
        await interaction.response.defer(ephemeral=True, thinking=True)
        api = EmlaLockClient(self.bot.settings.emlalock_api_base_url)
        try:
            result = await api.info(user_id, key)
            returned_id = str(result.get("user", {}).get("userid", ""))
            if returned_id != user_id:
                await interaction.followup.send("EmlaLock did not confirm that User ID. Nothing was saved.", ephemeral=True)
                return
            self.bot.storage.link_account(interaction.user.id, user_id, key)
            await interaction.followup.send(
                f"Your EmlaLock account ({result['user'].get('username', 'verified')}) is now linked. "
                "Your API key is encrypted in the bot database.", ephemeral=True)
        except EmlaLockError as exc:
            await interaction.followup.send(f"Account verification failed: {exc}", ephemeral=True)
        finally:
            await api.close()


class ConfirmTimeView(discord.ui.View):
    def __init__(self, bot, actor_id: int, operation: str, seconds: int,
                 target_id: int | None = None):
        super().__init__(timeout=90)
        self.bot = bot
        self.actor_id = actor_id
        self.operation = operation
        self.seconds = seconds
        self.target_id = target_id
        self.completed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("Only the person who requested this change can confirm it.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm change", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.completed:
            await interaction.response.send_message("This confirmation has already been used.", ephemeral=True)
            return
        self.completed = True
        button.disabled = True
        self.cancel_button.disabled = True
        await interaction.response.edit_message(content="Checking permissions and applying the change…", view=self)
        api = EmlaLockClient(self.bot.settings.emlalock_api_base_url)
        try:
            actor = self.bot.storage.get_account(self.actor_id)
            if not actor:
                raise EmlaLockError("Your EmlaLock account is no longer linked.")
            if self.operation == "add":
                fresh = await api.info(actor["emlalock_user_id"], actor["api_key"])
                session = fresh.get("chastitysession") or {}
                if str(session.get("wearerid")) != actor["emlalock_user_id"] or int(session.get("status", 0)) != 1:
                    raise EmlaLockError("Your linked account does not currently have an active wearer session.")
                result = await api.add_time(actor["emlalock_user_id"], actor["api_key"], self.seconds,
                                            "Discord assistant")
            else:
                target = self.bot.storage.get_account(self.target_id)
                if not target:
                    raise EmlaLockError("The selected wearer has not linked an EmlaLock account.")
                fresh = await api.info(target["emlalock_user_id"], target["api_key"])
                session = fresh.get("chastitysession") or {}
                if int(session.get("status", 0)) != 1:
                    raise EmlaLockError("The selected account does not have an active session.")
                if str(session.get("wearerid")) != target["emlalock_user_id"]:
                    raise EmlaLockError("The selected account is not the wearer for its current session.")
                if str(session.get("holderid")) != actor["emlalock_user_id"]:
                    raise EmlaLockError("Your linked EmlaLock account is not the holder for this session.")
                result = await api.subtract_time(target["emlalock_user_id"], target["api_key"],
                                                 actor["api_key"], self.seconds, "Discord assistant")
            session_result = result.get("chastitysession") or {}
            session_id = session_result.get("chastitysessionid", "confirmed")
            await interaction.edit_original_response(
                content=f"Change applied successfully. Session: {session_id}.", view=self)
        except (EmlaLockError, ValueError) as exc:
            await interaction.edit_original_response(content=f"No change was confirmed by the API: {exc}", view=self)
        except Exception:
            await interaction.edit_original_response(
                content="Something went wrong. Check the bot logs; credentials are not included in error messages.", view=self)
        finally:
            await api.close()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.completed = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Cancelled. No EmlaLock change was made.", view=self)


class AccountCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="link", description="Securely link your EmlaLock account")
    async def link(self, interaction: discord.Interaction):
        await interaction.response.send_modal(LinkAccountModal(self.bot))

    @app_commands.command(name="unlink", description="Disconnect your linked EmlaLock account")
    async def unlink(self, interaction: discord.Interaction):
        removed = self.bot.storage.unlink_account(interaction.user.id)
        await interaction.response.send_message(
            "Your EmlaLock account has been disconnected." if removed else "You do not have a linked EmlaLock account.",
            ephemeral=True)

    @app_commands.command(name="account", description="Check your EmlaLock account link")
    async def account(self, interaction: discord.Interaction):
        account = self.bot.storage.get_account(interaction.user.id)
        if not account:
            await interaction.response.send_message("No account linked. Use /link to connect privately.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Linked EmlaLock User ID: {account['emlalock_user_id']}. API key is stored encrypted.",
            ephemeral=True)

    @app_commands.command(name="session", description="Show your current EmlaLock session")
    async def session(self, interaction: discord.Interaction):
        account = self.bot.storage.get_account(interaction.user.id)
        if not account:
            await interaction.response.send_message("Use /link first.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        api = EmlaLockClient(self.bot.settings.emlalock_api_base_url)
        try:
            data = await api.info(account["emlalock_user_id"], account["api_key"])
            session = data.get("chastitysession")
            if not session:
                await interaction.followup.send("Your EmlaLock account has no active session.", ephemeral=True)
                return
            await interaction.followup.send(
                f"**Session:** {session.get('chastitysessionid', 'unknown')}\n"
                f"**Status:** {session.get('status', 'unknown')}\n"
                f"**Wearer:** {session.get('wearerid', 'unknown')}\n"
                f"**Keyholder:** {session.get('holderid') or 'None'}\n"
                f"**Duration:** {session.get('duration', 'unknown')} seconds",
                ephemeral=True)
        except EmlaLockError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        finally:
            await api.close()

    @app_commands.command(name="add", description="Request adding time to your own active session")
    @app_commands.describe(duration="For example: 2 hours, 1 day 30 minutes, or 45m")
    async def add(self, interaction: discord.Interaction, duration: str):
        account = self.bot.storage.get_account(interaction.user.id)
        if not account:
            await interaction.response.send_message("Use /link first.", ephemeral=True)
            return
        try:
            seconds = parse_duration(duration)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        view = ConfirmTimeView(self.bot, interaction.user.id, "add", seconds)
        await interaction.response.send_message(
            f"Please confirm: **add {duration}** to your currently linked EmlaLock session? "
            "The active session and permissions will be checked again before applying.",
            view=view, ephemeral=True)

    @app_commands.command(name="subtract", description="Request subtracting time from a linked wearer you hold")
    @app_commands.describe(wearer="The Discord member whose EmlaLock session you hold",
                           duration="For example: 2 hours or 30 minutes")
    async def subtract(self, interaction: discord.Interaction, wearer: discord.Member, duration: str):
        if wearer.id == interaction.user.id:
            await interaction.response.send_message("Choose the wearer account, not your own holder account.", ephemeral=True)
            return
        actor = self.bot.storage.get_account(interaction.user.id)
        target = self.bot.storage.get_account(wearer.id)
        if not actor or not target:
            await interaction.response.send_message("Both you and the wearer must link EmlaLock accounts using /link.", ephemeral=True)
            return
        try:
            seconds = parse_duration(duration)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        api = EmlaLockClient(self.bot.settings.emlalock_api_base_url)
        try:
            data = await api.info(target["emlalock_user_id"], target["api_key"])
            session = data.get("chastitysession") or {}
            if int(session.get("status", 0)) != 1 or str(session.get("holderid")) != actor["emlalock_user_id"]:
                await interaction.response.send_message(
                    "EmlaLock does not show your linked account as the holder of that active wearer session.",
                    ephemeral=True)
                return
        except EmlaLockError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        finally:
            await api.close()
        view = ConfirmTimeView(self.bot, interaction.user.id, "subtract", seconds, wearer.id)
        await interaction.response.send_message(
            f"Please confirm: **subtract {duration}** from {wearer.mention}'s active session? "
            "The holder relationship will be checked again before applying.",
            view=view, ephemeral=True)

    @app_commands.command(name="setuproles", description="Create bot-managed session roles for this server")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def setuproles(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        names = ("Wearer", "Keyholder", "Dual Role")
        roles = []
        try:
            for name in names:
                role = discord.utils.get(interaction.guild.roles, name=name)
                if role is None:
                    role = await interaction.guild.create_role(name=name, reason="EmlaLock bot session roles")
                roles.append(role)
            self.bot.storage.save_guild_roles(interaction.guild.id, *(r.id for r in roles))
            await interaction.followup.send(
                "Session roles are configured. Use /syncroles to synchronize linked members. "
                "The bot's top role must be above these roles.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("I need Manage Roles permission and a role position above the roles to manage.", ephemeral=True)

    @app_commands.command(name="syncroles", description="Synchronize session roles from linked EmlaLock accounts")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def syncroles(self, interaction: discord.Interaction):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return
        settings = self.bot.storage.get_guild_roles(guild.id)
        if not settings:
            await interaction.response.send_message("Run /setuproles first.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        members = [m async for m in guild.fetch_members(limit=None)]
        accounts = self.bot.storage.list_guild_accounts([m.id for m in members])
        linked = {a["discord_user_id"]: a for a in accounts}
        api = EmlaLockClient(self.bot.settings.emlalock_api_base_url)
        active_sessions = []
        try:
            for account in accounts:
                try:
                    data = await api.info(account["emlalock_user_id"], account["api_key"])
                    session = data.get("chastitysession") or {}
                    if int(session.get("status", 0)) == 1:
                        active_sessions.append(session)
                except EmlaLockError:
                    continue
        finally:
            await api.close()

        role_ids = {
            "wearer": int(settings["wearer_role_id"]),
            "holder": int(settings["keyholder_role_id"]),
            "dual": int(settings["dual_role_id"]),
        }
        roles = {key: guild.get_role(value) for key, value in role_ids.items()}
        if any(role is None for role in roles.values()):
            await interaction.followup.send("One or more configured roles no longer exist. Run /setuproles again.", ephemeral=True)
            return

        changed = 0
        failed = 0
        for member in members:
            account = linked.get(member.id)
            if not account:
                continue
            uid = account["emlalock_user_id"]
            is_wearer = any(str(s.get("wearerid")) == uid for s in active_sessions)
            is_holder = any(str(s.get("holderid")) == uid for s in active_sessions)
            desired = set()
            if is_wearer:
                desired.add(roles["wearer"])
            if is_holder:
                desired.add(roles["holder"])
            if is_wearer and is_holder:
                desired.add(roles["dual"])
            managed = set(roles.values())
            current = set(member.roles)
            remove = (current & managed) - desired
            add = desired - current
            if add or remove:
                try:
                    await member.remove_roles(*remove, reason="EmlaLock role synchronization")
                    await member.add_roles(*add, reason="EmlaLock role synchronization")
                    changed += 1
                except discord.Forbidden:
                    failed += 1
        await interaction.followup.send(
            f"Role sync complete. Updated {changed} member(s); {failed} could not be updated. "
            "Only the three bot-managed roles were changed.", ephemeral=True)

    @app_commands.command(name="help", description="Show bot usage")
    async def help_command(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "**EmlaLock assistant**\n/link — privately connect your account\n"
            "/session — view your current session\n/add duration — confirm adding time to your own session\n"
            "/subtract wearer duration — holder-authorized subtraction\n"
            "/setuproles and /syncroles — configure and synchronize server roles\n"
            "/unlink — disconnect your account\n\n"
            "You can also mention the bot in natural language for guidance. Time-changing actions always require confirmation.",
            ephemeral=True)
