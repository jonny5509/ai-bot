import logging
import discord
from discord.ext import commands

from config import load_settings
from database.storage import Storage
from bot.commands import AccountCommands
from ai.intent_detection import IntentDetector
from emlalock.time_parser import parse_duration


class EmlaLockBot(commands.Bot):
    def __init__(self, settings):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.settings = settings
        self.storage = Storage(settings.database_path, settings.credential_encryption_key)
        self.intent_detector = IntentDetector(settings.openai_api_key, settings.openai_model)

    async def setup_hook(self):
        await self.add_cog(AccountCommands(self))
        await self.tree.sync()

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if self.user not in message.mentions:
            await self.process_commands(message)
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
        if action in {"add_time", "subtract_time", "random_add", "random_subtract"}:
            amount_text = intent.get("amount_text")
            if not amount_text:
                await message.reply("How much time should I use? For example, 2 hours or 30 minutes.")
                return
            try:
                seconds = parse_duration(amount_text)
            except ValueError as exc:
                await message.reply(str(exc))
                return
            await message.reply(
                f"I understood {amount_text} ({seconds} seconds). I haven't changed anything. "
                "Account linking, target selection, permission checks, and confirmation must be "
                "implemented before live time changes can run.")
        elif action == "session_info":
            await message.reply("Session lookup will be available after account linking and API verification.")
        elif action == "help":
            await message.reply("Mention me with add 2 hours, subtract 30 minutes, or session info.")
        else:
            await message.reply("I can help with EmlaLock time and session requests. What would you like to do?")


def main():
    settings = load_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO))
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
