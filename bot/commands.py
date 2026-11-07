import logging
import secrets
import time

# EmlaLock accepts finite integer seconds for /addrandom. This conservative
# signed-32-bit ceiling is roughly 68 years and avoids an arbitrary week cap.
MAX_UNLIMITED_RANDOM_SECONDS = 2_147_483_647

import discord
from discord import app_commands
from discord.ext import commands, tasks

from emlalock.api import EmlaLockClient, EmlaLockError
from emlalock.time_parser import parse_duration
from bot.adoption import AdoptionPanelView


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
            emlalock_username = str(result.get("user", {}).get("username") or "").strip()
            if interaction.guild:
                cog = self.bot.get_cog("AccountCommands")
                if cog:
                    try:
                        member = interaction.guild.get_member(interaction.user.id)
                        if member and emlalock_username:
                            await cog.sync_member_nickname(member, emlalock_username)
                            role_note += f" Your server nickname is synced to {discord.utils.escape_markdown(emlalock_username[:32])}."
                        updated, failed = await cog.sync_guild_roles(interaction.guild)
                        role_note += f" Session roles checked: {updated} member(s) updated"
                        if failed:
                            role_note += f"; {failed} failed"
                        role_note += "."
                    except discord.Forbidden:
                        role_note += " I couldn't change your nickname; check the bot's Manage Nicknames permission and role position."
                    except Exception:
                        role_note += " Automatic nickname or role sync could not complete; an admin can check bot permissions."
            await interaction.followup.send(
                f"Your EmlaLock account ({result['user'].get('username', 'verified')}) is now linked. "
                "Your API key is encrypted in the bot database." + role_note, ephemeral=True)
        except EmlaLockError as exc:
            await interaction.followup.send(f"Account verification failed: {exc}", ephemeral=True)
        finally:
            await api.close()


class ConfirmTimeView(discord.ui.View):
    def __init__(self, bot, actor_id: int, operation: str, seconds: int,
                 target_id: int | None = None, expected_session_id: str | None = None,
                 random_from: int | None = None, random_to: int | None = None):
        super().__init__(timeout=90)
        self.bot = bot
        self.actor_id = actor_id
        self.operation = operation
        self.seconds = seconds
        self.target_id = target_id
        self.expected_session_id = expected_session_id
        self.random_from = random_from
        self.random_to = random_to
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
        random_claimed = False
        random_applied = False
        try:
            actor = self.bot.storage.get_account(self.actor_id)
            if not actor:
                raise EmlaLockError("Your EmlaLock account is no longer linked.")
            if self.operation == "randomtime":
                target = actor
                fresh = await api.info(target["emlalock_user_id"], target["api_key"])
                session = fresh.get("chastitysession") or {}
                if (int(session.get("status", 0)) != 1
                        or str(session.get("wearerid")) != str(target["emlalock_user_id"])):
                    raise EmlaLockError("Random time can only be used on your own active wearer session.")
                if self.expected_session_id and str(session.get("chastitysessionid")) != self.expected_session_id:
                    raise EmlaLockError("Your session changed after the request. Please submit it again.")
                holder_id = str(session.get("holderid") or "")
                holder = self.bot.storage.get_account_by_emlalock_id(holder_id) if holder_id else None
                if not holder:
                    raise EmlaLockError("A linked keyholder account is required. No time change was made.")
                holder_check = await api.info(holder["emlalock_user_id"], holder["api_key"])
                if str(holder_check.get("user", {}).get("userid", "")) != holder_id:
                    raise EmlaLockError("The linked keyholder account could not be verified. No time change was made.")
                # EmlaLock reports maxduration=0 when no maximum duration is set.
                # In that mode additions are effectively unbounded on the website;
                # /addrandom still needs a finite integer range, so use a very large
                # API-safe ceiling. Subtraction is limited to the currently remaining
                # session time so it cannot request removing more time than exists.
                raw_maximum = session.get("maxduration")
                try:
                    maximum_duration = int(float(raw_maximum)) if raw_maximum is not None else None
                except (TypeError, ValueError, OverflowError):
                    maximum_duration = None
                unlimited_maximum = maximum_duration == 0
                try:
                    random_initial_end = int(float(session.get("enddate")))
                    remaining_seconds = random_initial_end - int(time.time())
                except (TypeError, ValueError, OverflowError):
                    random_initial_end = None
                    remaining_seconds = 0
                if unlimited_maximum:
                    max_add_seconds = MAX_UNLIMITED_RANDOM_SECONDS
                    if remaining_seconds < 1:
                        try:
                            remaining_seconds = int(float(session.get("duration")))
                        except (TypeError, ValueError, OverflowError):
                            remaining_seconds = 0
                    max_subtract_seconds = min(MAX_UNLIMITED_RANDOM_SECONDS, remaining_seconds)
                    if max_subtract_seconds < 1:
                        raise EmlaLockError("EmlaLock did not provide a usable current duration for subtraction.")
                else:
                    if maximum_duration is None and random_initial_end is None:
                        raise EmlaLockError("EmlaLock did not provide a valid session end date or maximum-duration setting.")
                    max_add_seconds = remaining_seconds
                    max_subtract_seconds = remaining_seconds
                    if max_add_seconds < 1:
                        raise EmlaLockError("The session has no remaining time to use as the random limit.")
                try:
                    random_initial_duration = int(float(session.get("duration")))
                except (TypeError, ValueError, OverflowError):
                    random_initial_duration = None
                random_use_number = self.bot.storage.claim_daily_random_add(target["emlalock_user_id"])
                if not random_use_number:
                    raise EmlaLockError("You've already used /randomtime twice today. Your daily uses reset at 00:00 UTC.")
                random_claimed = True
                # First confirmed use is random. The second use is the penalty:
                # it is forced to add time, regardless of the random outcome.
                if random_use_number == 2 or secrets.randbelow(2) == 0:
                    self.random_direction = "add"
                    result = await api.add_random_time(
                        target["emlalock_user_id"], target["api_key"],
                        1, max_add_seconds, "Discord daily random time")
                else:
                    self.random_direction = "subtract"
                    self.seconds = secrets.randbelow(max_subtract_seconds) + 1
                    result = await api.subtract_time(
                        target["emlalock_user_id"], target["api_key"],
                        holder["api_key"], self.seconds, "Discord daily random time")
                random_applied = True
            elif self.operation == "add":
                target = actor if self.target_id is None else self.bot.storage.get_account(self.target_id)
                if not target:
                    raise EmlaLockError("The selected wearer has not linked an EmlaLock account.")
                fresh = await api.info(target["emlalock_user_id"], target["api_key"])
                session = fresh.get("chastitysession") or {}
                if int(session.get("status", 0)) != 1 or str(session.get("wearerid")) != str(target["emlalock_user_id"]):
                    raise EmlaLockError("The selected account does not currently have an active wearer session.")
                if self.target_id is not None and str(session.get("holderid")) != str(actor["emlalock_user_id"]):
                    raise EmlaLockError("EmlaLock no longer shows your linked account as the keyholder for this wearer.")
                if self.expected_session_id and str(session.get("chastitysessionid")) != self.expected_session_id:
                    raise EmlaLockError("The wearer session changed after the request. Please submit it again.")
                result = await api.add_time(target["emlalock_user_id"], target["api_key"], self.seconds,
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
            session_id = session_result.get("chastitysessionid", session.get("chastitysessionid", "confirmed"))
            # Re-read the session after the successful change: add/sub responses
            # do not consistently include the updated end date.
            updated_session = session_result
            try:
                updated_info = await api.info(target["emlalock_user_id"], target["api_key"])
                refreshed_session = updated_info.get("chastitysession") or {}
                if refreshed_session:
                    updated_session = refreshed_session
                    session_id = refreshed_session.get("chastitysessionid", session_id)
            except EmlaLockError:
                logging.exception("Time change succeeded, but updated session details could not be fetched for the announcement.")
            if self.operation == "randomtime" and getattr(self, "random_direction", None) == "add":
                try:
                    self.seconds = max(1, int(float(updated_session.get("enddate"))) - random_initial_end)
                except (TypeError, ValueError, OverflowError):
                    try:
                        updated_duration = int(float(updated_session.get("duration")))
                        self.seconds = max(1, updated_duration - random_initial_duration)
                    except (TypeError, ValueError, OverflowError):
                        self.seconds = 0
            await interaction.edit_original_response(
                content=f"Change applied successfully. Session: {session_id}.", view=self)
            await self._post_change_notification(interaction, actor, target, updated_session, session_id)
        except (EmlaLockError, ValueError) as exc:
            if random_claimed and not random_applied:
                self.bot.storage.release_daily_random_add(actor["emlalock_user_id"])
            await interaction.edit_original_response(content=f"No change was confirmed by the API: {exc}", view=self)
        except Exception:
            if random_claimed and not random_applied:
                self.bot.storage.release_daily_random_add(actor["emlalock_user_id"])
            await interaction.edit_original_response(
                content="Something went wrong. Check the bot logs; credentials are not included in error messages.", view=self)
        finally:
            await api.close()

    async def _post_change_notification(self, interaction, actor, target, session, session_id):
        if not interaction.guild_id:
            logging.info("Skipping time-change announcement because the confirmation was not in a server.")
            return

        # Time-change announcements belong in the configured session-updates
        # channel. Never fall back to the command/confirmation channel.
        settings = self.bot.storage.get_guild_roles(interaction.guild_id)
        channel_id = self.bot.storage.get_guild_channel(interaction.guild_id, "sessions")
        if not channel_id:
            channel_id = settings.get("notification_channel_id") if settings else None
        channel = self.bot.get_channel(int(channel_id)) if channel_id else None
        if channel is None and channel_id:
            try:
                channel = await self.bot.fetch_channel(int(channel_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logging.exception("Could not access the configured session-updates channel.")

        if channel is None or not hasattr(channel, "send"):
            logging.warning(
                "Time-change announcement not posted: configure a usable #session-updates channel with /setupchannels.")
            return
        # The selected target is already the verified linked wearer account. Use it
        # directly instead of performing a second database lookup that can fail
        # when EmlaLock formats the same ID differently.
        wearer = target
        holder_id = str(session.get("holderid") or "").strip()
        actor_emlalock_id = str(actor.get("emlalock_user_id") or "").strip()
        # When the person confirming the change is the session holder, their
        # already-verified linked account is the most reliable Discord mapping.
        if holder_id and actor_emlalock_id == holder_id:
            holder = actor
        else:
            holder = self.bot.storage.get_account_by_emlalock_id(holder_id) if holder_id else None
        wearer_text = f"<@{int(wearer['discord_user_id'])}>" if wearer else "Unlinked wearer"
        holder_text = f"<@{int(holder['discord_user_id'])}>" if holder else "Unlinked keyholder"
        action = (f"randomly {self.random_direction}ed" if self.operation == "randomtime"
                  else "added" if self.operation == "add" else "subtracted")
        hours, remainder = divmod(self.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        parts = ([f"{hours}h"] if hours else []) + ([f"{minutes}m"] if minutes else []) + ([f"{seconds}s"] if seconds else [])
        duration = " ".join(parts) or "0s"

        from datetime import datetime, timezone
        import time
        try:
            end_timestamp = int(float(session.get("enddate")))
            if end_timestamp <= 0:
                raise ValueError("Invalid end date")
            remaining_seconds = max(0, end_timestamp - int(time.time()))
            remaining_hours, remaining_remainder = divmod(remaining_seconds, 3600)
            remaining_minutes, remaining_seconds = divmod(remaining_remainder, 60)
            new_remaining = f"{remaining_hours:02d}:{remaining_minutes:02d}:{remaining_seconds:02d}"
            new_end_time = datetime.fromtimestamp(end_timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        except (TypeError, ValueError, OverflowError, OSError):
            new_remaining = "Unavailable"
            new_end_time = "Unavailable"
        message = (
            f"**EmlaLock time change confirmed**\n"
            f"Wearer: {wearer_text}\n"
            f"Keyholder: {holder_text}\n"
            f"Action: {discord.utils.escape_markdown(interaction.user.display_name)} {action} **{duration}**.\n"
            f"**New time remaining:** {new_remaining}\n"
            f"**New end time:** {new_end_time}\n"
            f"Session: **{discord.utils.escape_markdown(str(session_id))}**"
        )
        try:
            await channel.send(message, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            logging.info("Posted time-change announcement in channel %s.", getattr(channel, "id", "unknown"))
        except (discord.Forbidden, discord.HTTPException):
            logging.exception("Could not post the time-change announcement in the selected channel.")
        except Exception:
            # The EmlaLock change has already succeeded; announcement failures
            # must not make the confirmation appear to have failed.
            logging.exception("Unexpected error while posting the time-change announcement.")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.completed = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Cancelled. No EmlaLock change was made.", view=self)


class WearerChoiceSelect(discord.ui.Select):
    def __init__(self, choices, operation):
        options = [
            discord.SelectOption(
                label=discord.utils.escape_markdown(member.display_name)[:100],
                description=f"Active session {session.get('chastitysessionid', 'unknown')}"[:100],
                value=str(member.id),
            )
            for member, _account, session in choices[:25]
        ]
        super().__init__(
            placeholder=f"Choose which wearer to {operation} time {'to' if operation == 'add' else 'from'}",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await self.view.select_wearer(interaction, int(self.values[0]))


class WearerChoiceView(discord.ui.View):
    def __init__(self, bot, actor_id: int, operation: str, seconds: int, duration: str, choices):
        super().__init__(timeout=90)
        self.bot = bot
        self.actor_id = actor_id
        self.seconds = seconds
        self.operation = operation
        self.duration = duration
        self.choices = {member.id: (member, account, session)
                        for member, account, session in choices[:25]}
        self.add_item(WearerChoiceSelect(choices, operation))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message(
                "Only the keyholder who requested this change can choose a wearer.", ephemeral=True)
            return False
        return True

    async def select_wearer(self, interaction: discord.Interaction, wearer_id: int):
        selected = self.choices.get(wearer_id)
        if not selected:
            await interaction.response.send_message(
                f"That wearer is no longer available. Please run /{self.operation} again.", ephemeral=True)
            return
        member, _account, _old_session = selected
        await interaction.response.defer(ephemeral=True, thinking=True)
        api = EmlaLockClient(self.bot.settings.emlalock_api_base_url)
        try:
            actor = self.bot.storage.get_account(self.actor_id)
            target = self.bot.storage.get_account(member.id)
            if not actor or not target:
                raise EmlaLockError("The keyholder or selected wearer account is no longer linked.")
            data = await api.info(target["emlalock_user_id"], target["api_key"])
            session = data.get("chastitysession") or {}
            if (int(session.get("status", 0)) != 1
                    or str(session.get("wearerid")) != str(target["emlalock_user_id"])
                    or str(session.get("holderid")) != str(actor["emlalock_user_id"])):
                raise EmlaLockError(
                    "EmlaLock no longer shows you as the keyholder for that active wearer session.")
            session_id = str(session.get("chastitysessionid", ""))
            confirmation = ConfirmTimeView(
                self.bot, self.actor_id, self.operation, self.seconds,
                target_id=member.id, expected_session_id=session_id)
            await interaction.edit_original_response(
                content=(f"Please confirm: **{self.operation} {self.duration}** "
                         f"{'to' if self.operation == 'add' else 'from'} "
                         f"{discord.utils.escape_markdown(member.display_name)}'s session "
                         f"(**{session_id}**)? The session and keyholder permission will be checked again."),
                view=confirmation)
            self.stop()
        except (EmlaLockError, ValueError, TypeError) as exc:
            await interaction.edit_original_response(
                content=f"Could not verify that wearer session: {exc}", view=None)
        except Exception:
            await interaction.edit_original_response(
                content="Something went wrong while checking that wearer. Please try again.", view=None)
        finally:
            await api.close()


class AccountCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _post_channel_result(self, interaction: discord.Interaction, purpose: str, content: str):
        """Post a non-sensitive command result in its configured destination channel."""
        if not interaction.guild:
            return False
        channel_id = self.bot.storage.get_guild_channel(interaction.guild.id, purpose)
        channel = interaction.guild.get_channel(channel_id) if channel_id else None
        if channel is None and purpose == "commands":
            channel = interaction.channel
        if channel is None or not hasattr(channel, "send"):
            return False
        await channel.send(content)
        return True

    async def cog_load(self):
        async def commands_channel_check(interaction: discord.Interaction):
            if interaction.guild:
                channel_id = self.bot.storage.get_guild_channel(interaction.guild.id, "commands")
                if channel_id and interaction.channel_id != channel_id:
                    channel = interaction.guild.get_channel(channel_id)
                    destination = channel.mention if channel else "the configured bot-commands channel"
                    await interaction.response.send_message(
                        f"Please use bot commands in {destination}.", ephemeral=True)
                    return False
            return True

        for command in self.get_app_commands():
            command.add_check(commands_channel_check)
        if not self.role_sync_loop.is_running():
            self.role_sync_loop.start()

    async def cog_unload(self):
        self.role_sync_loop.cancel()

    @tasks.loop(minutes=5)
    async def role_sync_loop(self):
        for guild in self.bot.guilds:
            try:
                # This also refreshes EmlaLock-based nicknames when role sync
                # has not been configured for the server.
                await self.sync_guild_roles(guild)
            except Exception:
                continue

    @role_sync_loop.before_loop
    async def before_role_sync_loop(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if (self.bot.storage.get_guild_roles(member.guild.id)
                or self.bot.storage.get_adoption_roles(member.guild.id)):
            try:
                await self.sync_guild_roles(member.guild)
            except Exception:
                pass

    async def sync_member_nickname(self, member: discord.Member, emlalock_username: str) -> bool:
        # Discord nicknames are limited to 32 characters. Do not alter a member's
        # nickname when EmlaLock has not supplied a usable username.
        desired = emlalock_username.strip()[:32]
        if not desired or member.nick == desired:
            return False
        await member.edit(nick=desired, reason="EmlaLock username synchronization")
        return True

    async def sync_guild_roles(self, guild: discord.Guild) -> tuple[int, int]:
        settings = self.bot.storage.get_guild_roles(guild.id)
        members = [m async for m in guild.fetch_members(limit=None)]
        accounts = self.bot.storage.list_guild_accounts([m.id for m in members])
        api = EmlaLockClient(self.bot.settings.emlalock_api_base_url)
        active_sessions = []
        verified_user_ids = set()
        usernames = {}
        try:
            for account in accounts:
                try:
                    data = await api.info(account["emlalock_user_id"], account["api_key"])
                    verified_user_ids.add(account["emlalock_user_id"])
                    username = str((data.get("user") or {}).get("username") or "").strip()
                    if username:
                        usernames[int(account["discord_user_id"])] = username
                    session = data.get("chastitysession") or {}
                    if int(session.get("status", 0)) == 1:
                        active_sessions.append(session)
                except (EmlaLockError, ValueError, TypeError):
                    continue
        finally:
            await api.close()

        # Remove this bot's Friend Link posts once the wearer account has
        # been successfully checked and the corresponding session is no longer active.
        active_session_ids = {
            str(session.get("chastitysessionid") or "")
            for session in active_sessions
            if session.get("chastitysessionid")
        }
        ended_posts = self.bot.storage.list_ended_friendlink_messages(
            guild.id, active_session_ids, {str(uid) for uid in verified_user_ids})
        for post in ended_posts:
            try:
                channel = guild.get_channel(int(post["channel_id"]))
                if channel is None:
                    channel = await self.bot.fetch_channel(int(post["channel_id"]))
                message = await channel.fetch_message(int(post["message_id"]))
                await message.delete()
                self.bot.storage.delete_friendlink_message_record(int(post["message_id"]))
            except discord.NotFound:
                self.bot.storage.delete_friendlink_message_record(int(post["message_id"]))
            except (discord.Forbidden, discord.HTTPException):
                # Keep the record so a later sync can retry when permissions recover.
                continue

        # Keep linked members' server nicknames aligned with their verified
        # EmlaLock usernames, even when session roles have not been configured.
        member_by_id = {member.id: member for member in members}
        for discord_user_id, username in usernames.items():
            member = member_by_id.get(discord_user_id)
            if member:
                # Discord's Administrator permission does not bypass role hierarchy.
                # Check known hierarchy restrictions before making the API request.
                if member.id == guild.owner_id:
                    # Discord does not allow bots to edit the server owner's nickname.
                    # Skip silently; this is expected and not an actionable sync failure.
                    continue
                bot_member = guild.me
                if bot_member and member.top_role >= bot_member.top_role:
                    logging.warning(
                        "Cannot sync nickname for member_id=%s: their highest role (%s, position %s) "
                        "is not below the bot's highest role (%s, position %s). Move the bot role above theirs.",
                        member.id,
                        member.top_role.name,
                        member.top_role.position,
                        bot_member.top_role.name,
                        bot_member.top_role.position,
                    )
                    continue
                try:
                    await self.sync_member_nickname(member, username)
                except discord.Forbidden as exc:
                    logging.warning(
                        "Discord denied nickname sync for member_id=%s (HTTP %s, Discord code %s): %s",
                        member.id,
                        exc.status,
                        exc.code,
                        exc.text,
                    )
                except discord.HTTPException as exc:
                    logging.warning(
                        "Nickname sync failed for member_id=%s (HTTP %s, Discord code %s): %s",
                        member.id,
                        exc.status,
                        exc.code,
                        exc.text,
                    )

        adoption_settings = self.bot.storage.get_adoption_roles(guild.id)
        session_roles = {}
        if settings:
            role_ids = {
                "wearer": int(settings["wearer_role_id"]),
                "holder": int(settings["keyholder_role_id"]),
                "dual": int(settings["dual_role_id"]),
            }
            session_roles = {key: guild.get_role(value) for key, value in role_ids.items()}
            if any(role is None for role in session_roles.values()):
                return (0, len(members))

        adoption_roles = {}
        if adoption_settings:
            adoption_role_ids = {
                "looking_for_keyholder": int(adoption_settings["looking_for_keyholder_role_id"]),
                "looking_for_wearer": int(adoption_settings["looking_for_wearer_role_id"]),
            }
            adoption_roles = {
                key: guild.get_role(value) for key, value in adoption_role_ids.items()
            }
            adoption_roles = {key: role for key, role in adoption_roles.items() if role is not None}

        if not session_roles and not adoption_roles:
            return (0, 0)

        linked = {a["discord_user_id"]: a for a in accounts}
        changed = 0
        failed = 0
        managed = set(session_roles.values()) | set(adoption_roles.values())
        for member in members:
            account = linked.get(member.id)
            if account and account["emlalock_user_id"] not in verified_user_ids:
                continue
            uid = account["emlalock_user_id"] if account else None
            wearer_sessions = [
                session for session in active_sessions
                if uid and str(session.get("wearerid") or "").strip() == uid
            ]
            holder_sessions = [
                session for session in active_sessions
                if uid and str(session.get("holderid") or "").strip() == uid
            ]
            is_wearer = bool(wearer_sessions)
            is_holder = bool(holder_sessions)
            desired = set()
            if session_roles:
                if is_wearer:
                    desired.add(session_roles["wearer"])
                if is_holder:
                    desired.add(session_roles["holder"])
                if is_wearer and is_holder:
                    desired.add(session_roles["dual"])

            # Adoption roles are driven by the verified active EmlaLock session:
            # a wearer with no holder seeks a keyholder; a holder with no wearer seeks a wearer.
            missing_holder = is_wearer and any(
                str(session.get("holderid") or "").strip().lower() in ("", "0", "none", "null")
                for session in wearer_sessions
            )
            missing_wearer = is_holder and any(
                str(session.get("wearerid") or "").strip().lower() in ("", "0", "none", "null")
                for session in holder_sessions
            )
            if "looking_for_keyholder" in adoption_roles and missing_holder:
                desired.add(adoption_roles["looking_for_keyholder"])
            if "looking_for_wearer" in adoption_roles and missing_wearer:
                desired.add(adoption_roles["looking_for_wearer"])

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

    @app_commands.command(name="friendlink", description="Post a Friend Link for your own wearer session")
    async def friendlink(self, interaction: discord.Interaction):
        await self._post_friendlink(interaction)

    @app_commands.command(name="friendlink_for", description="Keyholders: post a linked wearer's Friend Link")
    @app_commands.default_permissions(discord.Permissions.none())
    @app_commands.describe(wearer="The linked wearer whose active session you hold")
    async def friendlink_for(self, interaction: discord.Interaction, wearer: discord.Member):
        if wearer.id == interaction.user.id:
            await interaction.response.send_message(
                "Use /friendlink to post your own session Friend Link.", ephemeral=True)
            return
        await self._post_friendlink(interaction, wearer)

    async def _post_friendlink(self, interaction: discord.Interaction, wearer: discord.Member | None = None):
        actor = self.bot.storage.get_account(interaction.user.id)
        if not actor:
            await interaction.response.send_message("Use /link first to link your EmlaLock account.", ephemeral=True)
            return
        if wearer and wearer.id == interaction.user.id:
            wearer = None
        await interaction.response.defer(ephemeral=True, thinking=True)
        api = EmlaLockClient(self.bot.settings.emlalock_api_base_url)
        try:
            target = actor
            session = None
            if wearer:
                target = self.bot.storage.get_account(wearer.id)
                if not target:
                    raise EmlaLockError("That wearer must link their EmlaLock account using /link first.")
                data = await api.info(target["emlalock_user_id"], target["api_key"])
                session = data.get("chastitysession") or {}
            else:
                own_data = await api.info(actor["emlalock_user_id"], actor["api_key"])
                own_session = own_data.get("chastitysession") or {}
                if (int(own_session.get("status", 0)) == 1
                        and str(own_session.get("wearerid")) == str(actor["emlalock_user_id"])):
                    session = own_session
                elif interaction.guild:
                    members = [member async for member in interaction.guild.fetch_members(limit=None)]
                    linked_accounts = self.bot.storage.list_guild_accounts([member.id for member in members])
                    held = []
                    for linked in linked_accounts:
                        if str(linked["emlalock_user_id"]) == str(actor["emlalock_user_id"]):
                            continue
                        try:
                            data = await api.info(linked["emlalock_user_id"], linked["api_key"])
                            candidate = data.get("chastitysession") or {}
                            if (int(candidate.get("status", 0)) == 1
                                    and str(candidate.get("wearerid")) == str(linked["emlalock_user_id"])
                                    and str(candidate.get("holderid")) == str(actor["emlalock_user_id"])):
                                held.append((linked, candidate))
                        except (EmlaLockError, ValueError, TypeError):
                            continue
                    if len(held) == 1:
                        target, session = held[0]
                    elif len(held) > 1:
                        choices = "\n".join(
                            f"• <@{item[0]['discord_user_id']}> — session {item[1].get('chastitysessionid', 'unknown')}"
                            for item in held)
                        await interaction.followup.send(
                            "You hold multiple linked active sessions. Use /friendlink_for and choose the wearer:\n" + choices,
                            ephemeral=True)
                        return
                if session is None:
                    raise EmlaLockError("I couldn't find an active wearer session for you or a linked wearer you keyhold.")
            if (int(session.get("status", 0)) != 1
                    or str(session.get("wearerid")) != str(target["emlalock_user_id"])):
                raise EmlaLockError("That account does not have an active wearer session.")
            is_keyholder = str(session.get("holderid") or "") == str(actor["emlalock_user_id"])
            is_wearer = str(session.get("wearerid") or "") == str(actor["emlalock_user_id"])
            if not is_wearer and not is_keyholder:
                raise EmlaLockError("EmlaLock does not show your linked account as the wearer or keyholder.")
            if is_keyholder and not is_wearer and str(session.get("holderfriendlink", "0")) not in ("1", "true", "True"):
                raise EmlaLockError("The wearer has not enabled Friend Link access for their keyholder.")
            session_id = str(session.get("chastitysessionid") or "").strip()
            if not session_id:
                raise EmlaLockError("EmlaLock did not provide a session link ID.")
            url = f"https://www.emlalock.com/#/f/{session_id}"
            message = ("Help with my session" if is_wearer else
                       "Help with the session I am keyholding") + f"\n{url}"

            # Tag the other participant when their EmlaLock account is linked
            # to a Discord member. The wearer is already the target account
            # when a keyholder posts; when the wearer posts, look up the holder.
            if is_wearer:
                other = self.bot.storage.get_account_by_emlalock_id(str(session.get("holderid") or ""))
            else:
                other = target
            if other and int(other["discord_user_id"]) != interaction.user.id:
                message += f"\n<@{int(other['discord_user_id'])}>"

            destination = interaction.channel
            if interaction.guild:
                configured_id = self.bot.storage.get_guild_channel(interaction.guild.id, "friendlinks")
                if configured_id:
                    destination = interaction.guild.get_channel(configured_id) or await self.bot.fetch_channel(configured_id)
            if not destination or not hasattr(destination, "send"):
                raise EmlaLockError("Configured Friend Links channel is unavailable.")
            posted = await destination.send(message, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            if interaction.guild:
                self.bot.storage.save_friendlink_message(
                    interaction.guild.id, destination.id, posted.id,
                    session_id, str(session.get("wearerid") or target["emlalock_user_id"]))
            await interaction.followup.send(f"Your Friend Link message has been posted in {destination.mention}.", ephemeral=True)
        except (EmlaLockError, ValueError, TypeError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                "I couldn't post in this channel. Check that I have permission to send messages.", ephemeral=True)
        finally:
            await api.close()

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
            # All /session lookup results belong in the commands channel.
            result_channel = "commands"
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
                        choices = "\n".join(
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

            session_message = (
                f"**Session ID:** {session.get('chastitysessionid', 'unknown')}\n"
                f"**Session start:** {start_display}\n"
                f"**Time elapsed:** {elapsed_display}\n"
                f"**Time remaining:** {remaining_display}\n"
                f"**Lock / device:** {discord.utils.escape_markdown(device_display)}\n"
                f"**Session status:** {status_display}\n"
                f"**Wearer:** {discord.utils.escape_markdown(wearer_name)}\n"
                f"**Keyholder:** {discord.utils.escape_markdown(holder_name)}"
            )
            if not await self._post_channel_result(interaction, result_channel, session_message):
                await interaction.followup.send(
                    "The session channel is not configured. Run /setupchannels first.", ephemeral=True)
        except EmlaLockError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        finally:
            await api.close()

    @app_commands.command(name="add", description="Add time to your session or a linked wearer session you hold")
    @app_commands.describe(wearer="Optional: choose a linked wearer session you hold", amount="Amount of time to change",
                           unit="Time unit: seconds, minutes, hours, days, or weeks")
    @app_commands.choices(unit=[
        app_commands.Choice(name="Seconds", value="seconds"),
        app_commands.Choice(name="Minutes", value="minutes"),
        app_commands.Choice(name="Hours", value="hours"),
        app_commands.Choice(name="Days", value="days"),
        app_commands.Choice(name="Weeks", value="weeks"),
    ])
    async def add(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100000],
                  unit: app_commands.Choice[str], wearer: discord.Member | None = None):
        actor = self.bot.storage.get_account(interaction.user.id)
        if not actor:
            await interaction.response.send_message("Use /link first to link your EmlaLock account.", ephemeral=True)
            return
        if wearer and wearer.id == interaction.user.id:
            wearer = None
        duration = f"{amount} {unit.value}"
        try:
            seconds = parse_duration(duration)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        api = EmlaLockClient(self.bot.settings.emlalock_api_base_url)
        try:
            # Prefer the caller's own active wearer session. A wearer who also
            # has a Keyholder role must not be prompted to select themselves.
            own_session_active = False
            if wearer is None:
                own_data = await api.info(actor["emlalock_user_id"], actor["api_key"])
                own_session = own_data.get("chastitysession") or {}
                own_session_active = (
                    int(own_session.get("status", 0)) == 1
                    and str(own_session.get("wearerid")) == str(actor["emlalock_user_id"])
                )

            # Only search linked wearer accounts when the caller has no active
            # wearer session of their own and is configured as a Keyholder.
            if wearer is None and not own_session_active and interaction.guild:
                role_settings = self.bot.storage.get_guild_roles(interaction.guild.id)
                keyholder_role_id = int(role_settings["keyholder_role_id"]) if role_settings else None
                member = interaction.guild.get_member(interaction.user.id)
                has_keyholder_role = bool(
                    member and keyholder_role_id
                    and any(role.id == keyholder_role_id for role in member.roles)
                )
                if has_keyholder_role:
                    members = [m async for m in interaction.guild.fetch_members(limit=None)]
                    linked_accounts = self.bot.storage.list_guild_accounts([m.id for m in members])
                    member_by_id = {m.id: m for m in members}
                    held_wearers = []
                    for linked in linked_accounts:
                        if str(linked["emlalock_user_id"]) == str(actor["emlalock_user_id"]):
                            continue
                        try:
                            wearer_data = await api.info(linked["emlalock_user_id"], linked["api_key"])
                            candidate = wearer_data.get("chastitysession") or {}
                            if (int(candidate.get("status", 0)) == 1
                                    and str(candidate.get("wearerid")) == str(linked["emlalock_user_id"])
                                    and str(candidate.get("holderid")) == str(actor["emlalock_user_id"])):
                                linked_member = member_by_id.get(int(linked["discord_user_id"]))
                                if linked_member:
                                    held_wearers.append((linked_member, linked, candidate))
                        except (EmlaLockError, ValueError, TypeError):
                            continue
                    if len(held_wearers) == 1:
                        wearer, _linked_account, _matched_session = held_wearers[0]
                    elif len(held_wearers) > 1:
                        view = WearerChoiceView(
                            self.bot, interaction.user.id, "add", seconds, duration, held_wearers)
                        await interaction.response.send_message(
                            "You hold multiple linked active wearer sessions. Select who to add time to:",
                            view=view, ephemeral=True)
                        return
                    else:
                        await interaction.response.send_message(
                            "Your Keyholder role is detected, but I couldn't find a linked active wearer session "
                            "that lists your EmlaLock account as the holder.",
                            ephemeral=True)
                        return

            target = actor if wearer is None else self.bot.storage.get_account(wearer.id)
            if wearer is not None and not target:
                await interaction.response.send_message(
                    "That wearer must link their EmlaLock account using /link first.", ephemeral=True)
                return
            if target is None:
                await interaction.response.send_message(
                    "The selected wearer has not linked an EmlaLock account.", ephemeral=True)
                return
            data = await api.info(target["emlalock_user_id"], target["api_key"])
            session = data.get("chastitysession") or {}
            if int(session.get("status", 0)) != 1 or str(session.get("wearerid")) != str(target["emlalock_user_id"]):
                await interaction.response.send_message("That linked account does not have an active wearer session.", ephemeral=True)
                return
            if wearer is not None and str(session.get("holderid")) != str(actor["emlalock_user_id"]):
                await interaction.response.send_message(
                    "EmlaLock does not show your linked account as the keyholder for that active wearer session.",
                    ephemeral=True)
                return
            session_id = str(session.get("chastitysessionid", ""))
        except (EmlaLockError, ValueError, TypeError) as exc:
            await interaction.response.send_message(f"Could not verify the active session: {exc}", ephemeral=True)
            return
        finally:
            await api.close()
        target_label = "your session" if wearer is None else f"{wearer.display_name}'s session"
        view = ConfirmTimeView(self.bot, interaction.user.id, "add", seconds,
                               target_id=None if wearer is None else wearer.id,
                               expected_session_id=session_id)
        await interaction.response.send_message(
            f"Please confirm: **Add {duration}** to **{target_label}** (session **{session_id}**)? "
            "The active session and keyholder permission will be checked again before applying.",
            view=view, ephemeral=True)

    @app_commands.command(name="randomtime", description="Once daily, randomly add or subtract time from your own session")
    async def randomtime(self, interaction: discord.Interaction):
        account = self.bot.storage.get_account(interaction.user.id)
        if not account:
            await interaction.response.send_message("Use /link first to link your EmlaLock account.", ephemeral=True)
            return
        if self.bot.storage.daily_random_add_used(account["emlalock_user_id"]):
            await interaction.response.send_message(
                "You have already used your daily random time change. You can use it again after 00:00 UTC.",
                ephemeral=True)
            return

        api = EmlaLockClient(self.bot.settings.emlalock_api_base_url)
        try:
            data = await api.info(account["emlalock_user_id"], account["api_key"])
            session = data.get("chastitysession") or {}
            if (int(session.get("status", 0)) != 1
                    or str(session.get("wearerid")) != str(account["emlalock_user_id"])):
                await interaction.response.send_message(
                    "Random time can only be used on your own active wearer session.", ephemeral=True)
                return
            holder_id = str(session.get("holderid") or "")
            if not holder_id or not self.bot.storage.get_account_by_emlalock_id(holder_id):
                await interaction.response.send_message(
                    "Your session's keyholder must link their EmlaLock account with /link before random time can be used. No time has been changed.",
                    ephemeral=True)
                return
            raw_maximum = session.get("maxduration")
            try:
                maximum_duration = int(float(raw_maximum)) if raw_maximum is not None else None
            except (TypeError, ValueError, OverflowError):
                maximum_duration = None
            unlimited_maximum = maximum_duration == 0
            try:
                end_timestamp = int(float(session.get("enddate")))
                remaining = end_timestamp - int(time.time())
            except (TypeError, ValueError, OverflowError):
                end_timestamp = None
                remaining = 0
            if unlimited_maximum:
                if remaining < 1:
                    try:
                        remaining = int(float(session.get("duration")))
                    except (TypeError, ValueError, OverflowError):
                        remaining = 0
                if remaining < 1:
                    await interaction.response.send_message(
                        "EmlaLock did not provide a usable current duration for the random subtraction option.", ephemeral=True)
                    return
                range_description = (
                    f"add between **1 second and {MAX_UNLIMITED_RANDOM_SECONDS:,} seconds** "
                    "(about 68 years), or subtract up to the currently remaining session time"
                )
            else:
                if end_timestamp is None:
                    await interaction.response.send_message(
                        "EmlaLock did not provide a valid session end date or maximum-duration setting.", ephemeral=True)
                    return
                if remaining < 1:
                    await interaction.response.send_message(
                        "Your session has no remaining time to use as the random limit.", ephemeral=True)
                    return
                range_description = f"add or subtract between **1 second and {remaining} seconds**"
            session_id = str(session.get("chastitysessionid") or "")
        except (EmlaLockError, ValueError, TypeError) as exc:
            await interaction.response.send_message(f"Could not verify your active session: {exc}", ephemeral=True)
            return
        finally:
            await api.close()

        view = ConfirmTimeView(
            self.bot, interaction.user.id, "randomtime", 0,
            expected_session_id=session_id)
        await interaction.response.send_message(
            f"Please confirm your **one daily random time change**. The bot will randomly choose to {range_description}. "
            "A linked keyholder account is required for either outcome. The session, keyholder and daily limit "
            "will be checked again when you confirm. Daily use resets at 00:00 UTC.",
            view=view, ephemeral=True)

    @app_commands.command(name="subtract", description="Subtract time from a linked wearer session you hold")
    @app_commands.default_permissions(discord.Permissions.none())
    @app_commands.describe(wearer="Optional: choose a linked wearer session you hold", amount="Amount of time to change",
                           unit="Time unit: seconds, minutes, hours, days, or weeks")
    @app_commands.choices(unit=[
        app_commands.Choice(name="Seconds", value="seconds"),
        app_commands.Choice(name="Minutes", value="minutes"),
        app_commands.Choice(name="Hours", value="hours"),
        app_commands.Choice(name="Days", value="days"),
        app_commands.Choice(name="Weeks", value="weeks"),
    ])
    async def subtract(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100000],
                       unit: app_commands.Choice[str], wearer: discord.Member | None = None):
        duration = f"{amount} {unit.value}"
        actor = self.bot.storage.get_account(interaction.user.id)
        if not actor:
            await interaction.response.send_message("Use /link first to link your EmlaLock account.", ephemeral=True)
            return
        if wearer and wearer.id == interaction.user.id:
            await interaction.response.send_message("Choose a wearer account, not your own holder account.", ephemeral=True)
            return
        try:
            seconds = parse_duration(duration)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        api = EmlaLockClient(self.bot.settings.emlalock_api_base_url)
        try:
            if wearer is None and interaction.guild:
                role_settings = self.bot.storage.get_guild_roles(interaction.guild.id)
                keyholder_role_id = int(role_settings["keyholder_role_id"]) if role_settings else None
                member = interaction.guild.get_member(interaction.user.id)
                has_keyholder_role = bool(
                    member and keyholder_role_id
                    and any(role.id == keyholder_role_id for role in member.roles)
                )
                if has_keyholder_role:
                    members = [m async for m in interaction.guild.fetch_members(limit=None)]
                    linked_accounts = self.bot.storage.list_guild_accounts([m.id for m in members])
                    member_by_id = {m.id: m for m in members}
                    held_wearers = []
                    for linked in linked_accounts:
                        if str(linked["emlalock_user_id"]) == str(actor["emlalock_user_id"]):
                            continue
                        try:
                            wearer_data = await api.info(linked["emlalock_user_id"], linked["api_key"])
                            candidate = wearer_data.get("chastitysession") or {}
                            if (int(candidate.get("status", 0)) == 1
                                    and str(candidate.get("wearerid")) == str(linked["emlalock_user_id"])
                                    and str(candidate.get("holderid")) == str(actor["emlalock_user_id"])):
                                linked_member = member_by_id.get(int(linked["discord_user_id"]))
                                if linked_member:
                                    held_wearers.append((linked_member, linked, candidate))
                        except (EmlaLockError, ValueError, TypeError):
                            continue
                    if len(held_wearers) == 1:
                        wearer = held_wearers[0][0]
                    elif len(held_wearers) > 1:
                        view = WearerChoiceView(
                            self.bot, interaction.user.id, "subtract", seconds, duration, held_wearers)
                        await interaction.response.send_message(
                            "You hold multiple linked active wearer sessions. Select who to subtract time from:",
                            view=view, ephemeral=True)
                        return
                    else:
                        await interaction.response.send_message(
                            "Your Keyholder role is detected, but I couldn't find a linked active wearer session "
                            "that lists your EmlaLock account as the holder.",
                            ephemeral=True)
                        return

            if wearer is None:
                await interaction.response.send_message(
                    "Select a wearer, or use /subtract in a server where your Keyholder role is configured.",
                    ephemeral=True)
                return
            target = self.bot.storage.get_account(wearer.id)
            if not target:
                await interaction.response.send_message(
                    "That wearer must link their EmlaLock account using /link first.", ephemeral=True)
                return
            data = await api.info(target["emlalock_user_id"], target["api_key"])
            session = data.get("chastitysession") or {}
            if (int(session.get("status", 0)) != 1
                    or str(session.get("wearerid")) != str(target["emlalock_user_id"])
                    or str(session.get("holderid")) != str(actor["emlalock_user_id"])):
                await interaction.response.send_message(
                    "EmlaLock does not show your linked account as the keyholder of that active wearer session.",
                    ephemeral=True)
                return
            session_id = str(session.get("chastitysessionid", ""))
        except (EmlaLockError, ValueError, TypeError) as exc:
            await interaction.response.send_message(f"Could not verify the active session: {exc}", ephemeral=True)
            return
        finally:
            await api.close()

        view = ConfirmTimeView(self.bot, interaction.user.id, "subtract", seconds, wearer.id, session_id)
        await interaction.response.send_message(
            f"Please confirm: **Subtract {duration}** from **{wearer.display_name}** "
            f"(session **{session_id}**)? The active session and keyholder permission will be checked again before applying.",
            view=view, ephemeral=True)

    @app_commands.command(name="setnotifications", description="Choose where confirmed time changes are announced")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setnotifications(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not interaction.guild:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        self.bot.storage.set_notification_channel(interaction.guild.id, channel.id)
        await interaction.response.send_message(
            f"Confirmed /add and /subtract changes will be announced in {channel.mention}, mentioning the linked wearer and keyholder.",
            ephemeral=True)

    @app_commands.command(name="disablenotifications", description="Stop posting time-change announcements")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def disablenotifications(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        self.bot.storage.set_notification_channel(interaction.guild.id, None)
        await interaction.response.send_message("Time-change announcements have been disabled.", ephemeral=True)

    @app_commands.command(name="setuproles", description="Create bot-managed session roles for this server")
    @app_commands.default_permissions(manage_roles=True)
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
            await self._post_channel_result(
                interaction, "commands",
                f"Session roles are configured and an initial sync has run ({changed} member(s) updated; "
                f"{failed} failed). Automatic checks now run every 5 minutes. "
                "The bot's top role must be above these roles.")
            await interaction.followup.send("Setup results posted in #bot-commands.", ephemeral=True)
            await self.bot._sync_guild_commands(interaction.guild)
        except discord.Forbidden:
            await interaction.followup.send("I need Manage Roles permission and a role position above the roles to manage.", ephemeral=True)

    @app_commands.command(name="syncroles", description="Synchronize session roles from linked EmlaLock accounts")
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.checks.has_permissions(manage_roles=True)
    async def syncroles(self, interaction: discord.Interaction):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return
        settings = self.bot.storage.get_guild_roles(guild.id)
        adoption_settings = self.bot.storage.get_adoption_roles(guild.id)
        if not settings and not adoption_settings:
            await interaction.response.send_message(
                "Run /setuproles or /setupadoption first.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            changed, failed = await self.sync_guild_roles(guild)
        except discord.Forbidden:
            await interaction.followup.send(
                "I need the Server Members Intent, Manage Roles permission, and a role position above the session roles.",
                ephemeral=True)
            return
        await self._post_channel_result(
            interaction, "commands",
            f"Role sync complete. Updated {changed} member(s); {failed} could not be updated. "
            "Automatic checks now run every 5 minutes.")
        await interaction.followup.send("Role sync results posted in #bot-commands.", ephemeral=True)


    @app_commands.command(name="setupchannels", description="Create missing EmlaLock channels and configure bot destinations")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setupchannels(self, interaction: discord.Interaction,
                            commands_channel: discord.TextChannel | None = None,
                            sessions_channel: discord.TextChannel | None = None,
                            adoption_channel: discord.TextChannel | None = None,
                            friendlinks_channel: discord.TextChannel | None = None,
                            logs_channel: discord.TextChannel | None = None):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_channels:
            await interaction.followup.send(
                "I do not currently have **Manage Channels** in this server. "
                "Please check the bot's server role permissions (not just your own permissions), "
                "then run /setupchannels again.",
                ephemeral=True,
            )
            return

        channel_names = {
            "commands": "bot-commands",
            "sessions": "session-updates",
            "adoption": "adoption",
            "friendlinks": "friend-links",
            "logs": "bot-logs",
        }
        overrides = {
            "commands": commands_channel,
            "sessions": sessions_channel,
            "adoption": adoption_channel,
            "friendlinks": friendlinks_channel,
            "logs": logs_channel,
        }
        try:
            category = discord.utils.find(
                lambda item: item.name.casefold() == "emlalock", guild.categories)
            if category is None:
                category = await guild.create_category(
                    "EmlaLock", reason="Create EmlaLock bot channel group")

            destinations = {}
            created = []
            for purpose, name in channel_names.items():
                channel = discord.utils.find(
                    lambda item: item.name.casefold() == name.casefold()
                    and item.category_id == category.id,
                    guild.text_channels,
                )
                if channel is None:
                    channel = await guild.create_text_channel(
                        name, category=category, reason="Create missing EmlaLock bot channel")
                    created.append(channel.mention)
                # Explicit channel selections override the automatic category channel.
                destinations[purpose] = overrides[purpose] or channel

            for purpose, channel in destinations.items():
                self.bot.storage.set_guild_channel(guild.id, purpose, channel.id)

            labels = {
                "commands": "Bot commands",
                "sessions": "Session updates",
                "adoption": "Adoption panel",
                "friendlinks": "Friend Links",
                "logs": "Bot logs",
            }
            lines = [
                f"**{label}:** {destinations[purpose].mention}"
                for purpose, label in labels.items()
            ]
            created_note = (
                "Created: " + ", ".join(created)
                if created else "All five EmlaLock channels already existed; nothing needed creating."
            )
            await self._post_channel_result(
                interaction, "commands",
                f"**EmlaLock category:** {category.name}\n"
                + "\n".join(lines) + f"\n{created_note}")
            await interaction.followup.send("Channel setup results posted in #bot-commands.", ephemeral=True)
        except discord.Forbidden as exc:
            logging.exception("Discord denied EmlaLock category/channel creation.")
            await interaction.followup.send(
                "Discord denied the channel-creation request (Forbidden). Confirm the bot itself has "
                "**Manage Channels** and that no server policy or integration restriction blocks it. "
                "Run /setupchannels again after fixing permissions.",
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            logging.exception("Could not create or configure the EmlaLock channels.")
            detail = f"Discord API error: HTTP {getattr(exc, 'status', 'unknown')}, code {getattr(exc, 'code', 'unknown')}: {str(exc)[:500]}"
            await interaction.followup.send(
                "Discord could not complete channel setup. " + detail +
                " Any channels already created will be reused next time; run /setupchannels again after resolving the error.",
                ephemeral=True,
            )

    @app_commands.command(name="setupadoption", description="Create automatic Looking for Keyholder and Looking for Wearer roles")
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.checks.has_permissions(manage_roles=True)
    async def setupadoption(self, interaction: discord.Interaction):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            role_specs = (
                ("Looking for Keyholder", "looking_for_keyholder_role_id"),
                ("Looking for Wearer", "looking_for_wearer_role_id"),
            )
            roles = {}
            for name, key in role_specs:
                role = discord.utils.get(guild.roles, name=name)
                if role is None:
                    role = await guild.create_role(
                        name=name, reason="EmlaLock adoption matching roles")
                roles[key] = role
            self.bot.storage.save_adoption_roles(
                guild.id, roles["looking_for_keyholder_role_id"].id,
                roles["looking_for_wearer_role_id"].id)
            changed, failed = await self.sync_guild_roles(guild)
            # Post the clickable panel in the same channel so setup is a
            # single step and users can immediately see how to participate.
            adoption_channel_id = self.bot.storage.get_guild_channel(guild.id, "adoption")
            adoption_channel = guild.get_channel(adoption_channel_id) if adoption_channel_id else interaction.channel
            if adoption_channel and hasattr(adoption_channel, "send"):
                await adoption_channel.send(
                    "**EmlaLock adoption matching**\n"
                    "If your verified active session is missing its wearer or keyholder, use the matching button "
                    "to find someone with the opposite adoption role. Requests are sent privately; accepting one "
                    "does not change EmlaLock.",
                    view=AdoptionPanelView(self.bot))
            await self._post_channel_result(
                interaction, "commands",
                f"Adoption roles are configured and the panel has been posted in {adoption_channel.mention if adoption_channel else 'no channel'}. "
                "Roles are assigned automatically when a verified active EmlaLock session has a wearer but no "
                "keyholder, or a keyholder but no wearer. "
                f"Initial sync: {changed} member(s) updated; {failed} failed. "
                "The bot's top role must be above both adoption roles. Use /syncroles to refresh manually.")
            await interaction.followup.send("Adoption setup results posted in #bot-commands.", ephemeral=True)
            await self.bot._sync_guild_commands(guild)
        except discord.Forbidden:
            await interaction.followup.send(
                "I need Manage Roles permission and a role position above the adoption roles.", ephemeral=True)

    @app_commands.command(name="adoptionpanel", description="Post the adoption request panel in the adoption channel")
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.checks.has_permissions(manage_roles=True)
    async def adoptionpanel(self, interaction: discord.Interaction):
        if not interaction.guild or not interaction.channel or not hasattr(interaction.channel, "send"):
            await interaction.response.send_message("Use this command in a server text channel.", ephemeral=True)
            return
        adoption_channel_id = self.bot.storage.get_guild_channel(interaction.guild.id, "adoption")
        if not adoption_channel_id:
            await interaction.response.send_message(
                "No adoption channel is configured. Run /setupchannels first.", ephemeral=True)
            return
        if interaction.channel_id != adoption_channel_id:
            adoption_channel = interaction.guild.get_channel(adoption_channel_id)
            mention = adoption_channel.mention if adoption_channel else "the configured adoption channel"
            await interaction.response.send_message(
                f"This command only works in {mention}.", ephemeral=True)
            return
        if not self.bot.storage.get_adoption_roles(interaction.guild.id):
            await interaction.response.send_message("Run /setupadoption first.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.channel.send(
            "**EmlaLock adoption matching**\\n"
            "If your verified active session is missing its wearer or keyholder, use the matching button "
            "to find someone with the opposite adoption role. Requests are sent privately; accepting one "
            "does not change EmlaLock.",
            view=AdoptionPanelView(self.bot))
        await interaction.followup.send("Adoption panel posted here.", ephemeral=True)

    @app_commands.command(name="help", description="Show bot usage")
    async def help_command(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "**EmlaLock assistant**\n/link — privately connect your account\n"
            "/session — view your current session\n/friendlink — post your own wearer session Friend Link\n/friendlink_for @wearer — keyholder posts a linked wearer's Friend Link\n/add duration — confirm adding time to your own session\n"
            "/subtract wearer duration — holder-authorized subtraction\n"
            "/setuproles and /syncroles — configure and synchronize session roles\n"
            "/setupadoption — create automatic adoption matching roles\n"
            "/adoptionpanel — post the clickable adoption request panel\n"
            "/unlink — disconnect your account\n\n"
            "You can also mention the bot in natural language for guidance. Time-changing actions always require confirmation.",
            ephemeral=True)
