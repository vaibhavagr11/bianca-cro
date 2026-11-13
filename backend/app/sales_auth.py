"""Validate founder access tokens issued for the Bianca Sales MCP endpoint."""

import asyncio
from collections.abc import Collection
from urllib.parse import urlsplit

import jwt
from jwt.exceptions import PyJWTError
from mcp.server.auth.provider import AccessToken, TokenVerifier


class SalesTokenVerifier(TokenVerifier):
    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str,
        allowed_subjects: Collection[str],
    ) -> None:
        jwks_endpoint = urlsplit(jwks_url)
        if jwks_endpoint.scheme != "https" or not jwks_endpoint.hostname:
            raise ValueError("The sales connector JWKS endpoint must use HTTPS")

        self._issuer = issuer
        self._audience = audience
        self._allowed_subjects = frozenset(allowed_subjects)
        self._jwks = jwt.PyJWKClient(jwks_url, timeout=2)

    async def verify_token(self, token: str) -> AccessToken | None:
        # The JWKS client may fetch a new key set on a cache miss.
        try:
            async with asyncio.timeout(3):
                return await asyncio.to_thread(self._verify_token, token)
        except TimeoutError:
            return None

    def _verify_token(self, token: str) -> AccessToken | None:
        if len(token) > 16_384:
            return None

        try:
            header = jwt.get_unverified_header(token)
            key_id = header.get("kid")
            if header.get("alg") != "RS256" or not isinstance(key_id, str) or not key_id:
                return None
            signing_key = self._jwks.get_signing_key(key_id)
            if signing_key.algorithm_name != "RS256":
                return None
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                options={
                    "require": ["iss", "aud", "exp", "sub", "client_id"],
                    "enforce_minimum_key_length": True,
                },
            )
        except (PyJWTError, TypeError, ValueError, OverflowError):
            return None

        subject = claims["sub"]
        client_id = claims["client_id"]
        expires_at = claims["exp"]
        scope = claims.get("scope", "")
        if (
            not isinstance(subject, str)
            or subject not in self._allowed_subjects
            or not isinstance(client_id, str)
            or not client_id
            or type(expires_at) is not int
            or not isinstance(scope, str)
        ):
            return None

        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=scope.split(),
            expires_at=expires_at,
            resource=self._audience,
            subject=subject,
            claims={"iss": self._issuer},
        )
