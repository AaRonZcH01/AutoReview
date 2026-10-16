from __future__ import annotations

import re
from typing import Any


def comparable(value: str) -> str:
    """Normalize only outer whitespace and repeated separators, never spelling."""
    return re.sub(r'\s+', ' ', value).strip()


def classify(cell: dict[str, Any] | None, answer: dict[str, Any]) -> str:
    if cell is None:
        return 'unknown'
    raw = cell.get('raw_english', '')
    if cell.get('uncertain'):
        return 'unknown'
    if comparable(raw) == '':
        return 'missing'
    accepted = [answer['english'], *answer.get('variants', [])]
    if any(comparable(raw) == comparable(candidate) for candidate in accepted):
        return 'correct'
    return 'incorrect'


def graded_items(answers: list[dict[str, Any]], cells: list[dict[str, Any]], review: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    review = review or {}
    by_number: dict[int, list[dict[str, Any]]] = {}
    for cell in cells:
        by_number.setdefault(cell['question_number'], []).append(cell)
    result = []
    for answer in answers:
        number = answer['question_number']
        matches = by_number.get(number, [])
        cell = matches[0] if len(matches) == 1 else None
        base_status = classify(cell, answer) if len(matches) <= 1 else 'unknown'
        override = review.get(str(number))
        status = override['status'] if override else base_status
        raw = override.get('raw_english', cell['raw_english'] if cell else '') if override else (cell['raw_english'] if cell else '')
        result.append({
            **answer,
            'raw_english': raw,
            'machine_raw_english': cell['raw_english'] if cell else None,
            'status': status,
            'machine_status': base_status,
            'uncertain': bool(cell['uncertain']) if cell else True,
            'box': cell.get('box') if cell else None,
            'row': cell['row'] if cell else None,
            'column': cell['column'] if cell else None,
            'reviewed_by_teacher': bool(override),
            'note': override.get('note', '') if override else '',
        })
    return result

