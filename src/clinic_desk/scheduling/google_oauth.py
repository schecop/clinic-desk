"""OAuth 2.0 for an installed app (Google "Desktop app" client), written by hand on purpose.

The flow (RFC 8252, with PKCE from RFC 7636):
1. Make a random `code_verifier`, send its SHA-256 (`code_challenge`) and a random `state` in
   the consent URL, which the browser opens.
2. Google redirects the browser to a one-shot server on 127.0.0.1 with `?code=…&state=…`.
   A `state` that does not match ours is refused (it is not the request we started).
3. The code is exchanged at the token endpoint together with the verifier: a code stolen on its
   way back is useless without it.
4. The token (access + refresh) is saved to `token.json` (gitignored). `TokenSource` refreshes
   the access token a minute before it expires and saves the new one.

Scope: `calendar.events` only (events of calendars the user can reach, no settings or ACLs).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from pydantic import BaseModel

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/calendar.events"
TOKEN_PATH = Path("token.json")
EARLY = timedelta(seconds=60)  # refresh this long before the access token expires

PAGE = (
    b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nConnection: close\r\n\r\n"
    b"<p>Clinic Desk: authorisation received. You can close this tab.</p>"
)


class OAuthError(RuntimeError):
    pass


class Client(BaseModel):
    id: str
    secret: str  # a Desktop client's secret is not truly secret, but it stays out of git


class Token(BaseModel):
    access_token: str
    refresh_token: str
    expires_at: datetime
    scope: str = SCOPE

    def save(self, path: Path = TOKEN_PATH) -> None:
        path.write_text(self.model_dump_json(indent=1), "utf-8")

    @classmethod
    def load(cls, path: Path = TOKEN_PATH) -> Token:
        if not path.exists():
            raise OAuthError(f"no {path}: run `clinic-desk calendar-auth` first")
        return cls.model_validate_json(path.read_text("utf-8"))


def pkce() -> tuple[str, str]:
    """(code_verifier, code_challenge): the challenge is the verifier's SHA-256, base64url."""
    verifier = secrets.token_urlsafe(64)  # 86 characters, inside the 43-128 the RFC allows
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def consent_url(client: Client, redirect_uri: str, state: str, challenge: str) -> str:
    return (
        AUTH_URI
        + "?"
        + urlencode(
            {
                "response_type": "code",
                "client_id": client.id,
                "redirect_uri": redirect_uri,
                "scope": SCOPE,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "access_type": "offline",  # ask for a refresh token
                "prompt": "consent",  # ... every time, not only on the first consent
            }
        )
    )


def _token(data: dict[str, object], now: datetime, refresh_token: str = "") -> Token:
    if "access_token" not in data:
        raise OAuthError(f"token endpoint: {data.get('error', 'no access_token')}")
    return Token(
        access_token=str(data["access_token"]),
        # a refresh answer usually omits it: the old refresh token stays valid
        refresh_token=str(data.get("refresh_token") or refresh_token),
        expires_at=now + timedelta(seconds=int(str(data.get("expires_in", 3600)))),
        scope=str(data.get("scope", SCOPE)),
    )


async def _post_token(http: httpx.AsyncClient, form: dict[str, str]) -> dict[str, object]:
    response = await http.post(TOKEN_URI, data=form)
    data: dict[str, object] = response.json()
    if response.status_code != 200:
        raise OAuthError(f"token endpoint {response.status_code}: {data.get('error')}")
    return data


async def authorize(
    client: Client,
    http: httpx.AsyncClient,
    open_browser: Callable[[str], object],
    timeout: float = 300,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Token:
    """The whole consent flow: browser, loopback redirect, state check, code exchange."""
    verifier, challenge = pkce()
    state = secrets.token_urlsafe(24)
    got: asyncio.Future[dict[str, str]] = asyncio.get_running_loop().create_future()

    async def redirect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_line = (await reader.readline()).decode("latin-1")
        parts = request_line.split(" ")
        target = urlsplit(parts[1] if len(parts) > 1 else "/")
        writer.write(PAGE)
        await writer.drain()
        writer.close()
        if target.path == "/" and not got.done():  # ignore /favicon.ico and repeats
            got.set_result({k: v[0] for k, v in parse_qs(target.query).items()})

    server = await asyncio.start_server(redirect, "127.0.0.1", 0)  # a free port, loopback only
    port = server.sockets[0].getsockname()[1]
    redirect_uri = f"http://127.0.0.1:{port}"
    try:
        open_browser(consent_url(client, redirect_uri, state, challenge))
        params = await asyncio.wait_for(got, timeout)
    finally:
        server.close()
        await server.wait_closed()
    if params.get("state") != state:
        raise OAuthError("state mismatch: this redirect does not answer our request")
    if "error" in params:
        raise OAuthError(f"consent refused: {params['error']}")
    if "code" not in params:
        raise OAuthError("no code in the redirect")
    data = await _post_token(
        http,
        {
            "grant_type": "authorization_code",
            "code": params["code"],
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "client_id": client.id,
            "client_secret": client.secret,
        },
    )
    return _token(data, clock())


class TokenSource:
    """A valid access token on demand: refreshed a minute early, and saved when it changes."""

    def __init__(
        self,
        client: Client,
        token: Token,
        http: httpx.AsyncClient,
        path: Path | None = TOKEN_PATH,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.client, self.token, self.http, self.path, self.clock = client, token, http, path, clock
        self._lock = asyncio.Lock()

    async def access_token(self, force: bool = False) -> str:
        async with self._lock:  # two calls at once refresh once
            if force or self.clock() >= self.token.expires_at - EARLY:
                await self._refresh()
            return self.token.access_token

    async def _refresh(self) -> None:
        data = await _post_token(
            self.http,
            {
                "grant_type": "refresh_token",
                "refresh_token": self.token.refresh_token,
                "client_id": self.client.id,
                "client_secret": self.client.secret,
            },
        )
        self.token = _token(data, self.clock(), self.token.refresh_token)
        if self.path is not None:
            self.token.save(self.path)
