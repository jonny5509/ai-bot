import httpx


class EmlaLockError(Exception):
    """Safe, user-facing EmlaLock API error."""


class EmlaLockClient:
    BASE_URL = "https://api.emlalock.com"

    def __init__(self, timeout: float = 15.0):
        self._client = httpx.AsyncClient(base_url=self.BASE_URL, timeout=timeout)

    async def _get(self, path: str, params: dict):
        try:
            response = await self._client.get(path, params=params)
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            if response.status_code >= 400:
                error = payload.get("error", "API request failed") if isinstance(payload, dict) else "API request failed"
                raise EmlaLockError(f"EmlaLock rejected the request: {error}")
            if isinstance(payload, dict) and payload.get("error"):
                raise EmlaLockError(f"EmlaLock rejected the request: {payload['error']}")
            return payload
        except httpx.RequestError:
            raise EmlaLockError("Could not reach EmlaLock. Please try again later.") from None

    async def info(self, user_id: str, api_key: str) -> dict:
        if not user_id or not api_key:
            raise EmlaLockError("Both EmlaLock User ID and API key are required.")
        result = await self._get("/info", {"userid": user_id, "apikey": api_key})
        if not isinstance(result, dict) or not isinstance(result.get("user"), dict):
            raise EmlaLockError("EmlaLock returned an unexpected account response.")
        return result

    async def add_time(self, user_id: str, api_key: str, seconds: int, text: str = "") -> dict:
        if seconds <= 0:
            raise ValueError("Time must be greater than zero.")
        params = {"userid": user_id, "apikey": api_key, "value": seconds}
        if text:
            params["text"] = text[:49]
        return await self._get("/add", params)

    async def subtract_time(self, wearer_user_id: str, wearer_api_key: str,
                            holder_api_key: str, seconds: int, text: str = "") -> dict:
        if seconds <= 0:
            raise ValueError("Time must be greater than zero.")
        params = {
            "userid": wearer_user_id, "apikey": wearer_api_key,
            "holderapikey": holder_api_key, "value": seconds,
        }
        if text:
            params["text"] = text[:49]
        return await self._get("/sub", params)

    async def close(self):
        await self._client.aclose()
