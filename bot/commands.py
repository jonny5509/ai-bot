import discord
from discord import app_commands
from discord.ext import commands, tasks

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
            try:
                self.bot.storage.link_account(interaction.user.id, user_id, key)
            except ValueError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            role_note = ""
            if interaction.guild:
                cog = self.bot.get_cog("AccountCommands")
                if cog:
                    try:
                        updated, failed = await cog.sync_guild_roles(interaction.guild)
                        role_note = f" Session roles checked: {updated} member(s) updated"
                        if failed:
                            role_note += f"; {failed} failed"
                        role_note += "."
                    except Exception:
                        role_note = " Automatic role sync could not complete; an admin can run /syncroles."
            await interaction.followup.send(
                f"Your EmlaLock account ({result['user'].get('username', 'verified')}) is now linked. "
                "Your API key is encrypted in the bot database." + role_note, ephemeral=True)
        except EmlaLockError as exc:
            await interaction.followup.send(f"Account verification failed: {exc}", ephemeral=True)
        finally:
            await api.close()


class ConfirmTimeView(discord.ui.View):
    def __init__(self, bot, actor_id: int, operation: str, seconds: int,
                 target_id: int | None = None, expected_session_id: str | None = None):
        super().__init__(timeout=90)
        self.bot = bot
        self.actor_id = actor_id
        self.operation = operation
        self.seconds = seconds
        self.target_id = target_id
        self.expected_session_id = expected_session_id
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
                if self.expected_session_id and str(session.get("chastitysessionid")) != self.expected_session_id:
                    raise EmlaLockError("Your active session changed after the request. Please submit it again.")
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
                if self.expected_session_id and str(session.get("chastitysessionid")) != self.expected_session_id:
                    raise EmlaLockError("The wearer session changed after the request. Please submit it again.")
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

    async def cog_load(self):
        if not self.role_sync_loop.is_running():
            self.role_sync_loop.start()

    async def cog_unload(self):
        self.role_sync_loop.cancel()

    @tasks.loop(minutes=5)
    async def role_sync_loop(self):
        for guild in self.bot.guilds:
            if self.bot.storage.get_guild_roles(guild.id):
                try:
                    await self.sync_guild_roles(guild)
                except Exception:
                    continue

    @role_sync_loop.before_loop
    async def before_role_sync_loop(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if self.bot.storage.get_guild_roles(member.guild.id):
            try:
                await self.sync_guild_roles(member.guild)
            except Exception:
                pass

    async def sync_guild_roles(self, guild: discord.Guild) -> tuple[int, int]:
        settings = self.bot.storage.get_guild_roles(guild.id)
        if not settings:
            return (0, 0)
        members = [m async for m in guild.fetch_members(limit=None)]
        accounts = self.bot.storage.list_guild_accounts([m.id for m in members])
        api = EmlaLockClient(self.bot.settings.emlalock_api_base_url)
        active_sessions = []
        verified_user_ids = set()
        try:
            for account in accounts:
                try:
                    data = await api.info(account["emlalock_user_id"], account["api_key"])
                    verified_user_ids.add(account["emlalock_user_id"])
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
            return (0, len(members))

        linked = {a["discord_user_id"]: a for a in accounts}
        changed = 0
        failed = 0
        managed = set(roles.values())
        for member in members:
            account = linked.get(member.id)
            if account and account["emlalock_user_id"] not in verified_user_ids:
                continue
            uid = account["emlalock_user_id"] if account else None
            is_wearer = bool(uid) and any(str(session.get("wearerid")) == uid for session in active_sessions)
            is_holder = bool(uid) and any(str(session.get("holderid")) == uid for session in active_sessions)
            desired = set()
            if is_wearer:
                desired.add(roles["wearer"])
            if is_holder:
                desired.add(roles["holder"])
            if is_wearer and is_holder:
                desired.add(roles["dual"])
            current = set(member.roles)
            remove = (current & managed) - desired
            add = desired - current
            if add or remove:
                try:
                    if remove:
                        await member.remove_roles(*remove, reason="EmlaLock automatic role synchronization")
                    if add:
                        await member.add_roles(*add, reason="EmlaLock automatic role synchronization")
                    changed += 1
                except discord.Forbidden:
                    failed += 1
        return changed, failed

    @app_commands.command(name="link", description="Securely link your EmlaLock account")
    async def link(self, interaction: discord.Interaction):
        await interaction.response.send_modal(LinkAccountModal(self.bot))

    @app_commands.command(name="unlink", description="Disconnect your linked EmlaLock account")
    async def unlink(self, interaction: discord.Interaction):
        removed = self.bot.storage.unlink_account(interaction.user.id)
        role_note = ""
        if removed and interaction.guild:
            try:
                changed, failed = await self.sync_guild_roles(interaction.guild)
                role_note = f" Session roles refreshed ({changed} member(s) updated)."
            except Exception:
                role_note = " Session roles will be refreshed by the automatic check."
        await interaction.response.send_message(
            ("Your EmlaLock account has been disconnected." + role_note)
            if removed else "You do not have a linked EmlaLock account.",
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
            own_user = data.get("user") or {}
            own_id = account["emlalock_user_id"]

            # A keyholder's own /info response may not include the wearer's
            # session. In a server, look through linked members for active
            # sessions whose holderid matches this keyholder's EmlaLock ID.
            if not session and interaction.guild:
                try:
                    guild_members = [member async for member in interaction.guild.fetch_members(limit=None)]
                    linked_accounts = self.bot.storage.list_guild_accounts(
                        [member.id for member in guild_members])
                    held_sessions = []
                    for linked in linked_accounts:
                        if str(linked["emlalock_user_id"]) == str(own_id):
                            continue
                        try:
                            wearer_data = await api.info(
                                linked["emlalock_user_id"], linked["api_key"])
                            candidate = wearer_data.get("chastitysession") or {}
                            if (int(candidate.get("status", 0)) == 1
                                    and str(candidate.get("holderid")) == str(own_id)):
                                held_sessions.append((candidate, linked))
                        except (EmlaLockError, ValueError, TypeError):
                            continue
                    if len(held_sessions) == 1:
                        session, _wearer_account = held_sessions[0]
                    elif len(held_sessions) > 1:
                        choices = "\\n".join(
                            f"• Session **{item[0].get('chastitysessionid', 'unknown')}** "
                            f"(wearer EmlaLock ID: {item[1]['emlalock_user_id']})"
                            for item in held_sessions)
                        await interaction.followup.send(
                            "I found multiple active sessions held by your linked account. "
                            "I won't guess which one you mean:\\n" + choices,
                            ephemeral=True)
                        return
                except (discord.Forbidden, discord.HTTPException):
                    pass

            if not session:
                message = "Your EmlaLock account has no active wearer session."
                if interaction.guild:
                    message += " I also couldn't find an active session for a linked wearer that lists you as keyholder."
                else:
                    message += " Use /session in the Discord server to check linked wearer sessions you hold."
                await interaction.followup.send(message, ephemeral=True)
                return
            # The /info response includes the authenticated account's username.
            # Look up the counterpart only when that EmlaLock account is also linked
            # to this bot; otherwise retain the ID as a reliable fallback.

            async def display_name(emlalock_id):
                if not emlalock_id:
                    return "None"
                if str(emlalock_id) == str(own_id):
                    return str(own_user.get("username") or emlalock_id)
                counterpart = self.bot.storage.get_account_by_emlalock_id(str(emlalock_id))
                if counterpart:
                    try:
                        counterpart_data = await api.info(
                            counterpart["emlalock_user_id"], counterpart["api_key"])
                        username = (counterpart_data.get("user") or {}).get("username")
                        if username:
                            return str(username)
                    except EmlaLockError:
                        pass
                return str(emlalock_id)

            wearer_name = await display_name(session.get("wearerid"))
            holder_name = await display_name(session.get("holderid"))
            from datetime import datetime, timezone
            import time

            def format_hms(value):
                try:
                    total_seconds = max(0, int(float(value)))
                except (TypeError, ValueError, OverflowError):
                    return "Not provided by EmlaLock"
                hours, remainder = divmod(total_seconds, 3600)
                minutes, seconds = divmod(remainder, 60)
                return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

            def format_timestamp(value):
                try:
                    timestamp = int(float(value))
                    if timestamp <= 0:
                        return "Not provided by EmlaLock"
                    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                except (TypeError, ValueError, OverflowError, OSError):
                    return "Not provided by EmlaLock"

            now = int(time.time())
            start_value = session.get("startdate")
            end_value = session.get("enddate")
            start_display = format_timestamp(start_value)
            try:
                elapsed_display = (
                    format_hms(now - int(float(start_value)))
                    if start_display != "Not provided by EmlaLock"
                    else "Not provided by EmlaLock"
                )
            except (TypeError, ValueError, OverflowError):
                elapsed_display = "Not provided by EmlaLock"
            try:
                remaining_display = (
                    format_hms(int(float(end_value)) - now)
                    if format_timestamp(end_value) != "Not provided by EmlaLock"
                    else "Not provided by EmlaLock"
                )
            except (TypeError, ValueError, OverflowError):
                remaining_display = "Not provided by EmlaLock"

            raw_status = session.get("status")
            status_display = {
                1: "Active", "1": "Active",
                0: "Inactive / ended", "0": "Inactive / ended",
            }.get(raw_status, str(raw_status) if raw_status is not None else "Not provided by EmlaLock")
            device_display = session.get("device") or session.get("devicename") or session.get("lock")
            device_display = str(device_display) if device_display else "Not provided by EmlaLock"

            await interaction.followup.send(
                f"**Session ID:** {session.get('chastitysessionid', 'unknown')}\n"
                f"**Session start:** {start_display}\n"
                f"**Time elapsed:** {elapsed_display}\n"
                f"**Time remaining:** {remaining_display}\n"
                f"**Lock / device:** {discord.utils.escape_markdown(device_display)}\n"
                f"**Session status:** {status_display}\n"
                f"**Wearer:** {discord.utils.escape_markdown(wearer_name)}\n"
                f"**Keyholder:** {discord.utils.escape_markdown(holder_name)}",
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
        api = EmlaLockClient(self.bot.settings.emlalock_api_base_url)
        try:
            data = await api.info(account["emlalock_user_id"], account["api_key"])
            session = data.get("chastitysession") or {}
            if int(session.get("status", 0)) != 1 or str(session.get("wearerid")) != account["emlalock_user_id"]:
                await interaction.response.send_message("Your linked account does not have an active wearer session.", ephemeral=True)
                return
            session_id = str(session.get("chastitysessionid", ""))
        except EmlaLockError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        finally:
            await api.close()
        view = ConfirmTimeView(self.bot, interaction.user.id, "add", seconds,
                               expected_session_id=session_id)
        await interaction.response.send_message(
            f"Please confirm: **add {duration}** to session **{session_id}**? "
            "The same session and permissions will be checked again before applying.",
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
        session_id = str(session.get("chastitysessionid", ""))
        view = ConfirmTimeView(self.bot, interaction.user.id, "subtract", seconds, wearer.id, session_id)
        await interaction.response.send_message(
            f"Please confirm: **subtract {duration}** from {wearer.mention}'s active session? "
            "The holder relationship will be checked again before applying.",
            view=view, ephemeral=True)

    @app_commands.command(name="setuproles", description="Create bot-managed session roles for this server")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def setuproles(self, interaction: discord.Interaction, wearer_role_name: str = "Wearer",
                         keyholder_role_name: str = "Keyholder", dual_role_name: str = "Dual Role"):
        if not interaction.guild:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        names = (wearer_role_name[:100], keyholder_role_name[:100], dual_role_name[:100])
        roles = []
        try:
            for name in names:
                role = discord.utils.get(interaction.guild.roles, name=name)
                if role is None:
                    role = await interaction.guild.create_role(name=name, reason="EmlaLock bot session roles")
                roles.append(role)
            self.bot.storage.save_guild_roles(interaction.guild.id, *(r.id for r in roles))
            changed, failed = await self.sync_guild_roles(interaction.guild)
            await interaction.followup.send(
                f"Session roles are configured and an initial sync has run ({changed} member(s) updated; "
                f"{failed} failed). Automatic checks now run every 5 minutes. "
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
        try:
            changed, failed = await self.sync_guild_roles(guild)
        except discord.Forbidden:
            await interaction.followup.send(
                "I need the Server Members Intent, Manage Roles permission, and a role position above the session roles.",
                ephemeral=True)
            return
        await interaction.followup.send(
            f"Role sync complete. Updated {changed} member(s); {failed} could not be updated. "
            "Automatic checks now run every 5 minutes.", ephemeral=True)

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
