"""Fernet-based credential encryption for mainnet API keys."""

import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_ENV_KEY_NAME = "FERNET_KEY"


def ensure_fernet_key(env_path: str | Path = ".env") -> str:
    """
    Read FERNET_KEY from environment. If missing, generate a new one,
    append it to the .env file, and return it.
    """
    key = os.environ.get(_ENV_KEY_NAME, "")
    if key:
        return key

    new_key = Fernet.generate_key().decode()
    env_file = Path(env_path)
    with env_file.open("a") as f:
        f.write(f"\n{_ENV_KEY_NAME}={new_key}\n")

    os.environ[_ENV_KEY_NAME] = new_key
    logger.info("Generated new FERNET_KEY and appended to %s", env_file)
    return new_key


def encrypt(value: str, key: str) -> str:
    """Encrypt a plaintext string using Fernet. Returns base64 string."""
    f = Fernet(key.encode() if isinstance(key, str) else key)
    return f.encrypt(value.encode()).decode()


def decrypt(value: str, key: str) -> str:
    """Decrypt a Fernet-encrypted base64 string. Raises InvalidToken if key is wrong."""
    f = Fernet(key.encode() if isinstance(key, str) else key)
    return f.decrypt(value.encode()).decode()
