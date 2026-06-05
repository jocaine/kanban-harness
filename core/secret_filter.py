"""Secret-safe logging filter — redacts known secret patterns from log output."""

import logging
import os
import re


_SECRET_ENV_KEYS = [
    "ANTHROPIC_API_KEY",
    "LINEAR_API_KEY",
    "OPENAI_API_KEY",
    "HERMES_API_KEY",
    "GITHUB_TOKEN",
]

_SECRET_VALUES: set[str] = set()


def _load_secret_values():
    """Collect actual secret values from environment for redaction."""
    _SECRET_VALUES.clear()
    for key in _SECRET_ENV_KEYS:
        val = os.getenv(key, "")
        if val and len(val) > 8:
            _SECRET_VALUES.add(val)


_load_secret_values()

_SECRET_PATTERNS = [
    re.compile(r'sk-ant-[a-zA-Z0-9_-]{20,}'),
    re.compile(r'sk-[a-zA-Z0-9]{20,}'),
    re.compile(r'ghp_[a-zA-Z0-9]{36,}'),
    re.compile(r'gho_[a-zA-Z0-9]{36,}'),
    re.compile(r'lin_api_[a-zA-Z0-9]{20,}'),
]


class SecretFilter(logging.Filter):
    """Redact known secrets from log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for secret in _SECRET_VALUES:
            if secret in msg:
                record.msg = str(record.msg).replace(secret, "***")
                if record.args:
                    record.args = tuple(
                        str(a).replace(secret, "***") if isinstance(a, str) else a
                        for a in record.args
                    ) if isinstance(record.args, tuple) else record.args
        for pattern in _SECRET_PATTERNS:
            if pattern.search(str(record.msg)):
                record.msg = pattern.sub("***", str(record.msg))
        return True


def install_secret_filter():
    """Install the secret filter on the root logger."""
    root = logging.getLogger()
    root.addFilter(SecretFilter())
