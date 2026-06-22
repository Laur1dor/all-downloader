"""Parser for the legacy plain-text database dump (info.txt).

The old bot exported rows as Python tuples, one per line:

    USERS
    data: (1, 6321925656, 'vitaIy04', '15/04/2025 14:57')
    ...
    CONVERTATIONS
    data: (1, 5773003944, '15/04/2025 14:55', 'Done')
    ...
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_DATE_FORMAT = "%d/%m/%Y %H:%M"
_STATUS_MAP = {"Done": "done", "Failed": "failed"}


@dataclass(frozen=True, slots=True)
class LegacyUser:
    id: int
    telegram_id: int
    username: str | None
    registration_date: datetime


@dataclass(frozen=True, slots=True)
class LegacyConversion:
    id: int
    telegram_id: int
    created_at: datetime
    status: str


def _parse_date(raw: str) -> datetime:
    # The legacy dump stored naive local timestamps; UTC is assumed on import.
    return datetime.strptime(raw, _DATE_FORMAT).replace(tzinfo=UTC)


def parse_legacy_dump(path: Path) -> tuple[list[LegacyUser], list[LegacyConversion]]:
    users: list[LegacyUser] = []
    conversions: list[LegacyConversion] = []
    section = None

    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        if line == "USERS":
            section = "users"
            continue
        if line == "CONVERTATIONS":
            section = "conversions"
            continue
        if not line.startswith("data: "):
            continue

        try:
            row = ast.literal_eval(line.removeprefix("data: "))
            if section == "users":
                users.append(LegacyUser(row[0], row[1], row[2], _parse_date(row[3])))
            elif section == "conversions":
                status = _STATUS_MAP.get(row[3], str(row[3]).lower())
                conversions.append(LegacyConversion(row[0], row[1], _parse_date(row[2]), status))
        except (ValueError, SyntaxError, IndexError, TypeError):
            logger.warning("Skipping malformed dump line %d: %s", line_no, line)

    return users, conversions
