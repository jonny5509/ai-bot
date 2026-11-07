import httpx


class EmlaLockError(Exception):
    """Raised for EmlaLock API or authorization errors."""


class EmlaLockClient:
    """Small HTTP client. Endpoint paths should be verified against current API docs."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 15.0):
        if not base_url:
            raise ValueError("EMLALOCK_API_BASE_URL must be configured")
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=timeout,
        )

    async def request(self, method: str, path: str, **kwargs):
        try:
            response = await self._client.request(method, path.lstrip("/"), **kwargs)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Do not include request headers or credential-bearing request data in errors.
            raise EmlaLockError(f"EmlaLock returned HTTP {exc.response.status_code}") from None
        except httpx.RequestError:
            raise EmlaLockError("Could not reach the EmlaLock API") from None
        try:
            return response.json()
        except ValueError:
            raise EmlaLockError("EmlaLock returned an invalid JSON response") from None

    async def close(self):
        await self._client.aclose()
