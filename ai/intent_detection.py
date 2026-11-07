import json
from openai import AsyncOpenAI

SYSTEM_PROMPT = """You are the intent parser for an EmlaLock Discord assistant.
Return only JSON with keys: intent, amount_text, target_hint, needs_clarification.
Allowed intents: add_time, subtract_time, random_add, random_subtract, session_info,
link_account, unlink_account, help, unknown.
Do not invent a duration, session, user ID, or permission. If required information is
missing or ambiguous, set needs_clarification to true. Never claim an API action occurred."""


class IntentDetector:
    ALLOWED = {"add_time", "subtract_time", "random_add", "random_subtract",
               "session_info", "link_account", "unlink_account", "help", "unknown"}

    def __init__(self, api_key: str, model: str):
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model

    async def detect(self, message: str) -> dict:
        result = await self.client.chat.completions.create(
            model=self.model, temperature=0, response_format={"type": "json_object"},
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": message}],
        )
        try:
            data = json.loads(result.choices[0].message.content or "{}")
        except (json.JSONDecodeError, TypeError):
            return {"intent": "unknown", "needs_clarification": True}
        if data.get("intent") not in self.ALLOWED:
            return {"intent": "unknown", "needs_clarification": True}
        return data
