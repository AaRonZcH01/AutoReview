"""Transport recovery for optional annotation boxes; answer contracts stay strict."""
import json
from pydantic import ValidationError
from .contracts import Box


def decode_response(purpose, version, raw):
    from .contracts_v5 import decode_output
    try:
        return decode_output(purpose, version, raw), []
    except ValidationError as original:
        if purpose not in ('student', 'student_verify', 'writing'):
            raise
        value = json.loads(raw)
        key = 'items' if purpose == 'writing' else 'cells'
        rows = value.get(key) if isinstance(value, dict) else None
        if not isinstance(rows, list):
            raise original
        warnings = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or row.get('box') is None:
                continue
            try:
                Box.model_validate(row['box'])
            except (ValidationError, TypeError):
                # Never swap coordinates: the resulting region could cover a
                # different answer. Only nullable observation boxes are dropped.
                warnings.append({'question_id': row.get('question_id'), 'index': index,
                    'code': 'INVALID_OPTIONAL_BOX', 'original_box': row['box'],
                    'message': '模型定位框无效，已弃用；按已确认作答区域显示。'})
                row['box'] = None
        if not warnings:
            raise original
        # Any remaining identity/content/edge/required geometry error still fails.
        return decode_output(purpose, version, json.dumps(value, ensure_ascii=False)), warnings
