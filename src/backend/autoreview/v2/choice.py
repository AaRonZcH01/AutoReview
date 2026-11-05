"""Choice formatting only: never consult a correct answer to read a response."""
import re


def multiple(answer):
    if answer.get('kind') == 'choice':
        return answer.get('selection_rule') == 'multiple'
    # Only legacy choice kinds use this older explicit flag. A stale field on
    # a fill-in/text question must never enable letter splitting.
    return answer.get('kind') in ('letter_choice', 'marked_choice') and bool(answer.get('answer_spec', {}).get('multiple'))


def option_ids(value, options=()):
    """Parse explicit lists and unambiguous compact uppercase option letters.

    Compact splitting requires the full printed option vocabulary, never the
    expected answer. Preserve case, unknown letters, and malformed evidence;
    no filtering, spelling repair, or punctuation guessing is allowed.
    """
    known = {o['id'] for o in options}
    compact = bool(known) and all(re.fullmatch(r'[A-Z]', v) for v in known)
    parts = value if isinstance(value, list) else [value]
    result = []
    for part in parts:
        if not isinstance(part, str):
            return None
        part = part.strip()
        if not part:
            continue
        for left, right in [('(', ')'), ('（', '）'), ('[', ']'), ('【', '】')]:
            if part.startswith(left) and part.endswith(right):
                part = part[len(left):-len(right)].strip()
                break
        # A trailing separator or an empty comma-delimited item is not evidence
        # of a complete selection. Spaces between IDs are harmless formatting.
        if not part or re.search(r'^[,，、;；]|[,，、;；]$|[,，、;；]\s*[,，、;；]', part):
            return None
        for token in re.split(r'[,，、;；\s]+', part):
            if token in known:
                result.append(token)
            elif compact and len(token) > 1 and all(c in known for c in token):
                result.extend(token)
            elif re.fullmatch(r'[A-Z]|[0-9]+', token):
                # Keep a clear but wrong option instead of silently dropping it.
                result.append(token)
            else:
                return None
    return result


def channels(cell, answer):
    values = {}
    for name, field in [('marked', 'selected_options'), ('written', 'written_options'), ('raw', 'raw_english')]:
        raw = cell.get(field)
        if not raw:
            continue
        parsed = option_ids(raw, answer.get('options', []))
        if parsed is None:
            return {}, '多选作答含无法可靠拆分的选项或符号，需教师核实'
        if parsed:
            values[name] = frozenset(parsed)
    if len(set(values.values())) > 1:
        return values, '多选题的圈选、手写选项或原文记录存在分歧，需教师核实'
    return values, ''
