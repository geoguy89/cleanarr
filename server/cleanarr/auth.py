"""Who is allowed to use this, for an instance that is not just yours.

Off until somebody sets a username and password. That is deliberate: the thing
was built for one server on one LAN, and demanding a login before it will show
you anything is a poor first five minutes. But once it leaves that LAN - or
once somebody else downloads it - "no password" stops being a convenience and
starts being a problem, so setting one has to be one screen and no restarts.

The shape is the one Sonarr and NZBGet use, because it is the one people
already expect: a first-run form that creates the account, then a login page,
then a cookie that lasts.

No dependencies. scrypt and hmac are in the standard library, and a service
that otherwise needs ffmpeg and a speech model should not also need a web
framework's auth stack.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

COOKIE = "cleanarr_session"
SESSION_DAYS = 30

# scrypt at these parameters takes roughly a tenth of a second per attempt,
# which is nothing to a person signing in and a great deal to anyone trying
# passwords in bulk.
#
# maxmem has to be set explicitly. scrypt needs 128 * N * r bytes - 32 MB at
# these settings - and OpenSSL's default ceiling is exactly 32 MB, so the call
# fails with "memory limit exceeded" on the boundary. Asking for 64 gives it
# room without being a number anyone has to think about again.
_N, _R, _P = 2 ** 15, 8, 1
_MAXMEM = 64 * 1024 * 1024


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """(hash, salt), both base64. A fresh salt unless one is supplied."""
    raw_salt = base64.b64decode(salt) if salt else os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf8"), salt=raw_salt,
                            n=_N, r=_R, p=_P, dklen=32, maxmem=_MAXMEM)
    return base64.b64encode(digest).decode(), base64.b64encode(raw_salt).decode()


def check_password(password: str, stored_hash: str, salt: str) -> bool:
    if not stored_hash or not salt:
        return False
    try:
        attempt, _ = hash_password(password, salt)
    except Exception:  # noqa: BLE001 - a corrupt salt must not 500 the login
        return False
    # Constant time: a plain == leaks how much of the hash matched.
    return hmac.compare_digest(attempt, stored_hash)


def new_secret() -> str:
    """The key that signs session cookies. Rotating it logs everyone out."""
    return base64.b64encode(os.urandom(32)).decode()


def _sign(payload: bytes, secret: str) -> str:
    key = base64.b64decode(secret)
    return base64.urlsafe_b64encode(hmac.new(key, payload, hashlib.sha256).digest()).decode()


def issue(username: str, secret: str) -> str:
    """A cookie value: who, when it expires, and a signature over both.

    Self-contained rather than a session table, so a restart does not sign
    everybody out - which on a service that gets redeployed as often as this
    one does would be its own kind of annoying.
    """
    body = json.dumps({
        "u": username,
        "exp": int(time.time()) + SESSION_DAYS * 86400,
        "n": secrets.token_hex(8),
    }, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(body).decode().rstrip("=")
    return f"{encoded}.{_sign(encoded.encode(), secret)}"


def verify(token: str, secret: str, username: str) -> bool:
    """Whether this cookie is one we issued, to this user, and still valid."""
    if not token or not secret:
        return False
    try:
        encoded, signature = token.rsplit(".", 1)
    except ValueError:
        return False
    if not hmac.compare_digest(_sign(encoded.encode(), secret), signature):
        return False
    try:
        padding = "=" * (-len(encoded) % 4)
        claims = json.loads(base64.urlsafe_b64decode(encoded + padding))
    except Exception:  # noqa: BLE001
        return False
    if claims.get("u") != username:
        return False            # the account was renamed; old cookies die
    return int(claims.get("exp", 0)) > time.time()


# Reachable with no cookie, and they have to be.
#
#  /api/auth/*  - you cannot log in through a door that needs you logged in.
#  /static/*    - the login page is served by the same HTML and CSS as the app.
#  /            - serves the page, which then asks /api/auth/state and shows
#                 either the login form or the app. Guarding it would mean a
#                 browser-native popup instead of a page that looks like this
#                 one.
#  /api/health  - so a container healthcheck or an uptime monitor does not
#                 need a credential to ask whether the process is alive.
OPEN_PATHS = ("/api/auth/", "/static/", "/api/health")


def is_open(path: str) -> bool:
    return path == "/" or path.startswith(OPEN_PATHS)
