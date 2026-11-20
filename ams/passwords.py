"""
Portal password hashing (PBKDF2-SHA256). Generate a hash for users.json:

    python -m ams.passwords "the-password"
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sys

ITERATIONS = 240_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2${ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt_b64, digest_b64 = stored.split("$")
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations))
    return hmac.compare_digest(actual, expected)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print('Usage: python -m ams.passwords "password"')
        sys.exit(1)
    print(hash_password(sys.argv[1]))
