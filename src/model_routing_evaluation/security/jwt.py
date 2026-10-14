"""JWT verification against an issuer's JWKS (enterprise IdP or agent credential issuer)."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

import httpx
import jwt

from model_routing_evaluation.domain.errors import UnauthorizedError, UpstreamUnavailableError

ALLOWED_ALGORITHMS = ("EdDSA", "ES256", "RS256", "PS256")


class KeyResolver(Protocol):
    async def key_for(self, kid: str) -> jwt.PyJWK: ...


def _parse(document: Mapping[str, Any]) -> dict[str, jwt.PyJWK]:
    keys: dict[str, jwt.PyJWK] = {}
    for entry in document.get("keys", []):
        if not isinstance(entry, dict) or "kid" not in entry:
            continue
        try:
            keys[str(entry["kid"])] = jwt.PyJWK.from_dict(entry)
        except (jwt.PyJWKError, jwt.InvalidKeyError):
            continue
    return keys


class StaticJwks:
    def __init__(self, document: Mapping[str, Any]) -> None:
        self._keys = _parse(document)

    async def key_for(self, kid: str) -> jwt.PyJWK:
        try:
            return self._keys[kid]
        except KeyError:
            raise UnauthorizedError("unknown signing key") from None


class RemoteJwks:
    """HTTPS JWKS with caching; refreshes at most every ``min_refresh_s`` on unknown kids."""

    def __init__(
        self,
        url: str,
        *,
        client: httpx.AsyncClient,
        ttl_s: float = 300.0,
        min_refresh_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = url
        self._client = client
        self._ttl = ttl_s
        self._min_refresh = min_refresh_s
        self._clock = clock
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None

    async def _refresh(self) -> None:
        try:
            response = await self._client.get(self._url)
            response.raise_for_status()
            document = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            if self._keys:
                return  # keep serving the last known keys during an IdP outage
            raise UpstreamUnavailableError("cannot fetch signing keys") from exc
        if isinstance(document, dict):
            self._keys = _parse(document)
        self._fetched_at = self._clock()

    async def key_for(self, kid: str) -> jwt.PyJWK:
        now = self._clock()
        stale = self._fetched_at is None or now - self._fetched_at > self._ttl
        may_refresh = self._fetched_at is None or now - self._fetched_at > self._min_refresh
        if stale or (kid not in self._keys and may_refresh):
            await self._refresh()
        try:
            return self._keys[kid]
        except KeyError:
            raise UnauthorizedError("unknown signing key") from None


class JwtVerifier:
    def __init__(
        self,
        resolver: KeyResolver,
        *,
        issuer: str,
        audience: str,
        algorithms: Sequence[str] = ALLOWED_ALGORITHMS,
        leeway_s: int = 30,
    ) -> None:
        self._resolver = resolver
        self._issuer = issuer
        self._audience = audience
        self._algorithms = list(algorithms)
        self._leeway = leeway_s

    async def verify(self, token: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise UnauthorizedError("malformed bearer token") from exc
        alg, kid = header.get("alg"), header.get("kid")
        if alg not in self._algorithms or not isinstance(kid, str):
            raise UnauthorizedError("unsupported token algorithm or missing key id")
        key = await self._resolver.key_for(kid)
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key.key,
                algorithms=[alg],
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise UnauthorizedError(f"invalid bearer token: {exc}") from exc
        return claims


def bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise UnauthorizedError("bearer token required")
    token = authorization[7:].strip()
    if not token or len(token) > 8192:
        raise UnauthorizedError("bearer token required")
    return token
