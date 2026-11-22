"""Messages that leave the engine: stored failure details, API errors and exports."""

from __future__ import annotations

import re

_ABS_PATH = re.compile(r"(?:[A-Za-z]:)?(?:[/\\][^\s/\\'\"]+){2,}")


def public_message(message: str, labels: dict[str, str] | None = None) -> str:
    """``message`` with known paths replaced by their labels and any other absolute path
    replaced by ``<file>``."""
    for path, label in (labels or {}).items():
        message = message.replace(path, label)
    return _ABS_PATH.sub("<file>", message)
