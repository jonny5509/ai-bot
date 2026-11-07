import logging
import re
import discord
from discord.ext import commands

from config import load_settings
from database.storage import Storage
from bot.commands import AccountCommands, ConfirmTimeView
from bot.adoption import AdoptionPanelView, AdoptionDecisionButton
from ai.intent_detection import IntentDetector
from emlalock.api import EmlaLockClient, EmlaLockError
from emlalock.time_parser import parse_duration



class DiscordLogHandler(logging.Handler):
    def __init__(self, bot):
        super().__init__(level=logging.WARNING)
        self.bot = bot

    def emit(self, record):
        if not self.bot.is_ready():
            return
        message = re.sub(r"([?&](?:userid|apikey|holderapikey)=)[^&\s"'<>]*", r"\1[REDACTED]", self.format(record), flags=re.IGNORECASE)
        for guild in self.bot.guilds:
            channel_id = self.bot.storage.get_guild_channel(guild.id, "logs")
            channel = guild.get_channel(channel_id) if channel_id else None
            if channel is not None:
                self.bot.loop.create_task(self._send(channel, message))

    async def _send(self, channel, message):
        try:
            await channel.send("LOG: " + message[:1800])
        except (discord.Forbidden, discord.HTTPException):
            pass


class EmlaLockBot(commands.Bot):
    def __init__(self, settings):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self.settings = settings
        self.storage = Storage(settings.database_path, settings.credential_encryption_key)
        self.intent_detector = IntentDetector(settings.openai_api_key, settings.openai_model)
        self.pending_actions = {}
        self._guild_commands_synced = False
        self._discord_log_handler = DiscordLogHandler(self)
        self._discord_log_handler.setFormatter(logging.Formatter('%(levelname)s %(name)s: %(message)s'))

    async def setup_hook(self):
        await self.add_cog(AccountCommands(self))
        # Persistent adoption panels and request buttons continue working after restarts.
        self.add_view(AdoptionPanelView(self))
        self.add_dynamic_items(AdoptionDecisionButton)

        # Register commands per server for immediate availability. Global
        # commands can take a long time to propagate and can appear alongside
        # older guild-scoped copies as duplicates.
        logging.info("Loaded %d application commands for guild synchronization",
                     len(self.tree.get_commands()))

    async def _sync_guild_commands(self, guild: discord.Guild):
        # Remove stale guild commands, then publish exactly the current local
        # command definitions to this guild.
        self.tree.clear_commands(guild=guild)
        self.tree.copy_global_to(guild=guild)
        # Hide one-time setup commands after their corresponding configuration
        # has been saved for this server. Other servers retain their own setup state.
        if self.storage.get_guild_roles(guild.id):
            self.tree.remove_command("setuproles", guild=guild)
        if self.storage.get_adoption_roles(guild.id):
            self.tree.remove_command("setupadoption", guild=guild)
        synced = await self.tree.sync(guild=guild)
        logging.info("Published %d application commands to %s (%s)",
                     len(synced), guild.name, guild.id)

    async def on_ready(self):
        if self._discord_log_handler not in logging.getLogger().handlers:
            logging.getLogger().addHandler(self._discord_log_handler)
        # Remove any previously published global commands so Discord has one
        # guild-scoped copy per server, not global + guild duplicates.
        if self.application_id:
            try:
                await self.http.bulk_upsert_global_commands(self.application_id, [])
                logging.info("Removed legacy global application commands")
            except discord.HTTPException:
                logging.exception("Could not remove legacy global application commands")

        if self._guild_commands_synced:
            return
        self._guild_commands_synced = True
        for guild in self.guilds:
            try:
                await self._sync_guild_commands(guild)
            except discord.HTTPException:
                logging.exception("Could not refresh application commands for guild %s (%s)", guild.name, guild.id)

    async def on_guild_join(self, guild: discord.Guild):
        try:
            await self._sync_guild_commands(guild)
        except discord.HTTPException:
            logging.exception("Could not publish application commands for new guild %s (%s)", guild.name, guild.id)

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if self.user not in message.mentions:
            pending = self.pending_actions.pop(message.author.id, None)
            if pending:
                try:
                    seconds = parse_duration(message.content.strip())
                except ValueError as exc:
                    await message.reply(str(exc))
                    return
                account = self.storage.get_account(message.author.id)
                if not account:
                    await message.reply("Use /link to securely connect your EmlaLock account first.")
                    return
                api = EmlaLockClient(self.settings.emlalock_api_base_url)
                try:
                    info = await api.info(account["emlalock_user_id"], account["api_key"])
                    session = info.get("chastitysession") or {}
                    if int(session.get("status", 0)) != 1 or str(session.get("wearerid")) != account["emlalock_user_id"]:
                        await message.reply("Your linked account does not have an active wearer session.")
                        return
                    session_id = str(session.get("chastitysessionid", ""))
                except EmlaLockError as exc:
                    await message.reply(str(exc))
                    return
                finally:
                    await api.close()
                view = ConfirmTimeView(self, message.author.id, "add", seconds,
                                       expected_session_id=session_id)
                try:
                    await message.author.send(
                        f"Please confirm: add {message.content.strip()} to your linked active session? "
                        "I'll re-check the session before applying.",
                        view=view)
                    await message.reply("I've sent you a private confirmation. No change has been made yet.")
                except discord.Forbidden:
                    await message.reply("I couldn't DM you. Enable DMs, then use /add for a private confirmation.")
                return
            await self.process_commands(message)
            return

        if message.guild:
            commands_channel_id = self.storage.get_guild_channel(message.guild.id, "commands")
            if commands_channel_id and message.channel.id != commands_channel_id:
                await message.reply(f"Please use bot commands in <#{commands_channel_id}>.")
                return

        content = message.content.replace(f"<@{self.user.id}>", "").replace(
            f"<@!{self.user.id}>", "").strip()
        if not content:
            await message.reply("What would you like help with?")
            return

        try:
            intent = await self.intent_detector.detect(content)
        except Exception:
            logging.exception("Intent detection failed")
            await message.reply("I couldn't understand that just now. Please try again.")
            return

        action = intent.get("intent", "unknown")
        if action in {"add_time", "subtract_time"}:
            amount_text = intent.get("amount_text")
            if not amount_text:
                if action == "add_time":
                    self.pending_actions[message.author.id] = {"action": "add_time"}
                await message.reply("How much time do you mean? For example, 2 hours or 30 minutes.")
                return
            try:
                seconds = parse_duration(amount_text)
            except ValueError as exc:
                await message.reply(str(exc))
                return

            target_id = None
            operation = "add"
            if action == "subtract_time":
                operation = "subtract"
                targets = [m for m in message.mentions if m.id != self.user.id]
                if not targets:
                    await message.reply("Who is the wearer? Mention their Discord account so I can check the linked EmlaLock session.")
                    return
                target_id = targets[0].id
                actor = self.storage.get_account(message.author.id)
                target = self.storage.get_account(target_id)
                if not actor or not target:
                    await message.reply("Both you and the wearer need linked EmlaLock accounts. Use /link privately.")
                    return
                api = EmlaLockClient(self.settings.emlalock_api_base_url)
                try:
                    info = await api.info(target["emlalock_user_id"], target["api_key"])
                    session = info.get("chastitysession") or {}
                    if int(session.get("status", 0)) != 1 or str(session.get("holderid")) != actor["emlalock_user_id"]:
                        await message.reply("EmlaLock does not show you as the holder of that active session.")
                        return
                    session_id = str(session.get("chastitysessionid", ""))
                except EmlaLockError as exc:
                    await message.reply(str(exc))
                    return
                finally:
                    await api.close()
            else:
                account = self.storage.get_account(message.author.id)
                if not account:
                    await message.reply("Use /link to securely connect your EmlaLock account first.")
                    return
                api = EmlaLockClient(self.settings.emlalock_api_base_url)
                try:
                    info = await api.info(account["emlalock_user_id"], account["api_key"])
                    session = info.get("chastitysession") or {}
                    if int(session.get("status", 0)) != 1 or str(session.get("wearerid")) != account["emlalock_user_id"]:
                        await message.reply("Your linked account does not have an active wearer session.")
                        return
                    session_id = str(session.get("chastitysessionid", ""))
                except EmlaLockError as exc:
                    await message.reply(str(exc))
                    return
                finally:
                    await api.close()

            view = ConfirmTimeView(self, message.author.id, operation, seconds, target_id, session_id)
            try:
                target_text = "your linked active session" if operation == "add" else f"<@{target_id}>'s session"
                await message.author.send(
                    f"Please confirm: **{operation} {amount_text}** to {target_text}? "
                    "I'll re-check the current session and permissions before applying.",
                    view=view)
                await message.reply("I've sent you a private confirmation in DMs. No change has been made yet.")
            except discord.Forbidden:
                await message.reply("I couldn't DM you. Enable server-member DMs, then use /add or /subtract for a private confirmation.")
        elif action == "session_info":
            account = self.storage.get_account(message.author.id)
            if not account:
                await message.reply("Use /link to securely connect your EmlaLock account first.")
                return
            api = EmlaLockClient(self.settings.emlalock_api_base_url)
            try:
                data = await api.info(account["emlalock_user_id"], account["api_key"])
                session = data.get("chastitysession") or {}
                if not session:
                    await message.reply("Your linked account has no active EmlaLock session.")
                else:
                    await message.author.send(
                        f"Your current session is {session.get('chastitysessionid', 'unknown')}. "
                        f"Status: {session.get('status', 'unknown')}; duration: "
                        f"{session.get('duration', 'unknown')} seconds.")
                    await message.reply("I've sent your session details privately.")
            except EmlaLockError as exc:
                await message.reply(str(exc))
            finally:
                await api.close()
        elif action == "help":
            await message.reply(
                "Use /link to connect privately, /session to view your session, "
                "/add duration to request adding time, or /subtract wearer duration "
                "for a holder-authorized change. I always ask for confirmation before changing time.")
        else:
            await message.reply(
                "I can help with EmlaLock session information and time changes. "
                "Try mentioning me with 'add 2 hours' or use /help.")


def main():
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # HTTPX logs request URLs at INFO level. EmlaLock puts account IDs and API
    # credentials in query parameters, so redact them before anything reaches
    # the terminal or log files.
    class SensitiveQueryFilter(logging.Filter):
        _pattern = re.compile(
            r"([?&](?:userid|apikey|holderapikey)=)[^&\\s\"'<>]*",
            re.IGNORECASE,
        )

        def filter(self, record):
            message = record.getMessage()
            redacted = self._pattern.sub(r"\\1[REDACTED]", message)
            if redacted != message:
                record.msg = redacted
                record.args = ()
            return True

    for handler in logging.getLogger().handlers:
        handler.addFilter(SensitiveQueryFilter())
    # Also avoid routine HTTP request URL output; warnings/errors remain visible.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    missing = [name for name, value in {
        "DISCORD_TOKEN": settings.discord_token,
        "OPENAI_API_KEY": settings.openai_api_key,
        "CREDENTIAL_ENCRYPTION_KEY": settings.credential_encryption_key,
    }.items() if not value]
    if missing:
        raise SystemExit("Missing required environment variables: " + ", ".join(missing))
    EmlaLockBot(settings).run(settings.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
