# EmlaLock AI Discord Bot

A conversational Discord assistant for managing EmlaLock sessions through the EmlaLock API.

## Planned capabilities

- Link each Discord account to its own EmlaLock account privately.
- Store API credentials encrypted at rest; never commit secrets or log credentials.
- Understand natural-language requests and ask follow-up questions when details are missing.
- Resolve the target session explicitly, check permissions, and confirm time changes before applying them.
- Support single-session and carefully previewed bulk operations.
- Synchronize Wearer, Keyholder, and Dual Role Discord roles from linked session relationships.
- Keep API operations deterministic and auditable; AI interprets intent but does not bypass API permissions.

## Requirements

- Python 3.11+
- A Discord bot token
- An OpenAI API key
- EmlaLock API credentials for each user

## Development setup

1. Create and activate a Python virtual environment.
2. Install dependencies: `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and fill in the required environment variables.
4. Run `python main.py`.

Never commit `.env`, database files, or real API credentials.

## Status

Initial project scaffold. EmlaLock endpoint behavior and authorization must be validated against the current official API documentation before enabling live account mutations.
