# EmlaLock AI Discord Bot

A Python Discord assistant for linking EmlaLock accounts, reading the linked user's current session, requesting time changes with explicit confirmation, and synchronizing server roles.

## What is implemented

- Private Discord modal for linking an EmlaLock User ID and API key.
- Credential verification against the official `/info` endpoint before saving.
- Fernet encryption for API keys at rest in SQLite.
- Natural-language requests when the bot is mentioned, with OpenAI intent detection and duration parsing.
- Private DM confirmation before time-changing requests.
- `/session` and conversational session lookup.
- `/add duration` for the linked user's own active wearer session.
- `/subtract @wearer duration` when the caller's linked EmlaLock account is the holder of the target's active session. The official API holder key parameter is used.
- Re-checks session status and holder/wearer relationship immediately before a mutation.
- `/setuproles` and `/syncroles` for Wearer, Keyholder, and Dual Role roles. Only those bot-managed roles are changed.
- `/unlink` to disconnect an account.

## Important API limitations

The official EmlaLock `/info` endpoint returns the authenticated user's information and current session, not a server-wide session directory. This bot therefore only acts on accounts that have linked themselves and on the current session returned for those accounts.

Bulk operations across every EmlaLock session are not enabled: the public API documentation does not provide a general session-enumeration endpoint, and the bot will not guess targets. Random-time operations are also not exposed in the current command set.

The API requires User ID and API key as GET query parameters over HTTPS. The bot does not log request URLs or credentials. Use a trusted host and keep the database and encryption key private.

## Requirements

- Python 3.11+
- Discord application and bot token
- OpenAI API key
- Each user's EmlaLock User ID and API key
- A persistent disk for the SQLite database and encryption key

## Setup

### Guided web setup (recommended)

1. Clone the repository and install dependencies with `pip install -r requirements.txt`.
2. Run `python setup.py`.
3. Open `http://127.0.0.1:8765` in a browser on the same computer.
4. Enter your Discord bot token and OpenAI API key, choose the model, and save. The wizard generates the Fernet encryption key automatically and writes secrets to `.env` with owner-only file permissions where supported.
5. Stop the wizard with Ctrl+C, then start the bot using `python main.py`.
6. In Discord, use `/link` to connect each member's EmlaLock account privately.

The setup wizard intentionally listens only on `127.0.0.1`; it is not a public hosted dashboard. It never displays saved credentials again and does not log submitted form values. To change settings later, stop the bot and run `python setup.py` again.

### Manual setup

1. Create a Discord application and add a bot.
2. In the Discord Developer Portal, enable **Message Content Intent** and **Server Members Intent**. These are needed for natural-language mentions and role synchronization.
3. Invite the bot with the `bot` and `applications.commands` scopes. Grant Send Messages, Read Message History, Use Application Commands, and (if using role sync) Manage Roles. Put the bot's highest role above the three session roles.
4. Install Python 3.11 or newer.
5. Create a virtual environment and install dependencies:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

   On Windows, activate with `.venv\\Scripts\\activate`.

6. Copy `.env.example` to `.env`.
7. Generate an encryption key:

   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

8. Set `DISCORD_TOKEN`, `OPENAI_API_KEY`, and `CREDENTIAL_ENCRYPTION_KEY` in `.env`. Keep the encryption key backed up securely; losing it makes stored API keys unrecoverable.
9. Start the bot:

   ```bash
   python main.py
   ```

10. In your server, run `/link` and submit your EmlaLock credentials in the private modal. Use `/account` to check link status.

### Docker deployment

After creating `.env`, run `docker compose up -d --build`. Docker Compose keeps the SQLite database in a named volume. Back up that volume and the `CREDENTIAL_ENCRYPTION_KEY` together.

## Commands

| Command | Purpose |
|---|---|
| `/link` | Privately link and verify an EmlaLock account |
| `/unlink` | Remove your linked account |
| `/account` | Check your linked account |
| `/session` | View your current session privately |
| `/add 2 hours` | Request adding time to your own session |
| `/subtract @member 30 minutes` | Request holder-authorized subtraction |
| `/setuproles` | Create the three managed session roles |
| `/syncroles` | Synchronize roles for linked members |
| `/help` | Show command help |

You can also mention the bot in a channel, for example `@Bot add 2 hours` or `@Bot session info`. Mutation confirmations are sent by DM.

## Safety and operations

- All time changes require an explicit confirmation button.
- The bot re-reads the relevant session before changing it.
- The EmlaLock API remains the final authority for authorization.
- Do not share API keys in public channels. Use the private `/link` modal.
- Never commit `.env`, SQLite databases, or encryption keys.
- Run the bot on a trusted host with persistent storage and restrict access to its data directory.
- Role synchronization is manual via `/syncroles`; automatic periodic monitoring is not configured.
