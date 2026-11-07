# EmlaLock AI Discord Bot

A Python Discord assistant for linking EmlaLock accounts, reading the linked user's current session, requesting time changes with explicit confirmation, and synchronizing server roles.

## What is implemented

- Private Discord modal for linking an EmlaLock User ID and API key.
- Credential verification against the official `/info` endpoint before saving.
- Automatically sets each linked member's server nickname to their verified EmlaLock username and refreshes it every 5 minutes. This changes only the nickname in that Discord server, not their global Discord username.
- Fernet encryption for API keys at rest in SQLite.
- Natural-language requests when the bot is mentioned, with OpenAI intent detection and duration parsing.
- Private DM confirmation before time-changing requests.
- `/session` and conversational session lookup.
- `/friendlink` posts the caller's own wearer-session Friend Link without a wearer selector. Keyholders use `/friendlink_for @wearer` to post a linked wearer's Friend Link when the session allows keyholder access. The bot tracks its posts and automatically deletes them after the wearer session ends (checked during the 5-minute account sync).
- `/session` displays the session start timestamp (UTC), elapsed time, remaining time, status, wearer, and keyholder. It shows lock/device information only if EmlaLock includes a corresponding field; otherwise it clearly says the API did not provide it.
- `/session` displays EmlaLock usernames for the wearer and keyholder when those accounts are linked; otherwise it safely falls back to the EmlaLock User ID.
- `/add duration` for the linked user's own active wearer session.
- `/randomtime` lets a wearer use the command twice per UTC calendar day: the first confirmed use randomly adds or subtracts time; the second confirmed use is a penalty that can only add time. Both uses reset at 00:00 UTC. Both the wearer and keyholder must have linked EmlaLock accounts. For sessions with a maximum duration, the random range is 1 second up to the time remaining until the current end date. When EmlaLock reports `maxduration=0` (maximum duration disabled), additions use a large finite API-compatible ceiling of 2,147,483,647 seconds (about 68 years); subtractions are limited to the currently remaining session time. A private confirmation is required, and the daily use is recorded only when the API accepts the change.
- `/subtract @wearer duration` when the caller's linked EmlaLock account is the holder of the target's active session. The official API holder key parameter is used.
- Re-checks session status and holder/wearer relationship immediately before a mutation.
- `/setuproles` and `/syncroles` for Wearer, Keyholder, and Dual Role roles. Roles are checked immediately after linking, when a member joins, and automatically every 5 minutes. Only those bot-managed roles are changed.
- `/setupadoption` creates automatic **Looking for Keyholder** and **Looking for Wearer** roles. A linked wearer with an active session and no holder receives Looking for Keyholder; a linked holder whose active session has no wearer receives Looking for Wearer.
- `/adoptionpanel` posts persistent buttons to find someone with the opposite adoption role.
- `/setupchannels` automatically creates or reuses an **EmlaLock** category and all five channels (`#bot-commands`, `#session-updates`, `#adoption`, `#friend-links`, and `#bot-logs`) whenever they are missing. Administrators can optionally override individual destinations with existing channels. Slash commands and bot mentions are restricted to the configured bot-command channel. Session announcements, adoption panels and Friend Link posts use their configured destinations. Warning and error logs are forwarded to the configured log channel.
- `/unlink` to disconnect an account.

## Important API limitations

The official EmlaLock `/info` endpoint returns the authenticated user's information and current session, not a server-wide session directory. This bot therefore only acts on accounts that have linked themselves and on the current session returned for those accounts.

Bulk operations across every EmlaLock session are not enabled: the public API documentation does not provide a general session-enumeration endpoint, and the bot will not guess targets. The wearer-only `/randomtime` command requires both wearer and keyholder accounts to be linked. It randomly adds time using EmlaLock's random-add endpoint or subtracts a bot-selected amount using the holder-authorized subtract endpoint. When `maxduration` is disabled (`0`), additions use a finite 2,147,483,647-second ceiling because the API requires a numeric upper bound; subtraction never requests more than the currently remaining session time. Otherwise, both directions are bounded by the time remaining until the current session end date, and it allows up to two successful uses per EmlaLock account per UTC calendar day. The first use randomly adds or subtracts; the second is forced to add time only. The limit resets at 00:00 UTC.

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
3. Invite the bot with the `bot` and `applications.commands` scopes. Grant Send Messages, Read Message History, Use Application Commands, and Manage Nicknames. If using role sync, also grant Manage Roles. The bot's highest role must be above members whose nicknames it should change, as well as above the three session roles and both adoption roles.
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

## Updating on Windows

1. Stop the bot with Ctrl+C.
2. Open the bot's main folder (the folder containing `main.py`).
3. Double-click `update.bat`.
4. When it finishes, restart the bot with `python main.py`.

The updater downloads the latest `main` branch from GitHub. It leaves `.env`, local database files, virtual environments, and Git metadata untouched. If dependencies changed, it prints the command to install them. Back up your folder before updating as a precaution.

## Commands

| Command | Purpose |
|---|---|
| `/link` | Privately link and verify an EmlaLock account |
| `/unlink` | Remove your linked account |
| `/account` | Check your linked account |
| `/session` | View your current session privately |\n| `/friendlink` | Post your own wearer-session Friend Link |\n| `/friendlink_for @wearer` | Keyholder posts a linked wearer's Friend Link when permitted |
| `/add 2 hours` | Request adding time to your own session |
| `/randomtime` | First daily use randomly adds/subtracts; second daily use can only add time (requires linked keyholder) |
| `/subtract @member 30 minutes` | Request holder-authorized subtraction |
| `/setupchannels` | Automatically create/reuse the EmlaLock category and any missing bot channels; optional channel parameters override destinations |
| `/setuproles` | Create the three managed session roles |
| `/syncroles` | Synchronize roles for linked members |
| `/setupadoption` | Create automatic adoption matching roles |
| `/adoptionpanel` | Post the clickable adoption request panel |
| `/help` | Show command help |

You can also mention the bot in a channel, for example `@Bot add 2 hours` or `@Bot session info`. Mutation confirmations are sent by DM.

## Safety and operations

- All time changes require an explicit confirmation button.
- The bot re-reads the relevant session before changing it.
- Accepting an adoption request only records mutual interest in Discord. It does not change an EmlaLock account, session, wearer, or keyholder relationship. The two members must complete that change on EmlaLock themselves; the bot will update the adoption roles after the API reports the new relationship.
- The EmlaLock API remains the final authority for authorization.
- Do not share API keys in public channels. Use the private `/link` modal.
- Never commit `.env`, SQLite databases, or encryption keys.
- Run the bot on a trusted host with persistent storage and restrict access to its data directory.
- Role synchronization runs automatically every 5 minutes after `/setuproles`, immediately after linking in a server, and when a member joins. `/syncroles` remains available for an immediate manual refresh. The bot needs Server Members Intent, Manage Roles permission, and its highest role positioned above the three managed roles.
