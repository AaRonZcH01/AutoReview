from __future__ import annotations
import re
from collections import Counter
from .contracts import MatchingEdge, MatchingRule


def comparable(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def grade_text(raw: str | None, answer: dict, uncertain=False) -> str:
    if raw is None or uncertain:
        return "unknown"
    if not comparable(raw):
        return "missing"
    accepted = [answer.get("english", ""), *answer.get("variants", [])]
    return "correct" if any(comparable(raw) == comparable(x) for x in accepted) else "incorrect"


def grade_matching(rule: MatchingRule, edges: list[MatchingEdge]) -> dict:
    nodes = {node.id: node for node in rule.nodes}
    def pair(a, b):
        return (a, b) if rule.directed else tuple(sorted((a, b)))
    expected = {pair(a, b) for a, b in rule.allowed_edges}
    observed, unknown = set(), []
    for edge in edges:
        if edge.erased:
            continue
        if edge.uncertain or edge.from_node not in nodes or edge.to_node not in nodes or edge.from_node == edge.to_node:
            unknown.append(edge.model_dump())
        else:
            observed.add(pair(edge.from_node, edge.to_node))
    conflict = []
    if rule.cardinality == "one_to_one":
        counts = Counter(node for edge in observed for node in edge)
        conflict = [node for node, count in counts.items() if count > 1]
    elif rule.cardinality == "one_to_many":
        source_group = rule.source_group
        targets = [node for edge in observed for node in edge if nodes[node].group != source_group]
        conflict = [node for node, count in Counter(targets).items() if count > 1]
    absent = expected - observed
    # Unknown strokes can explain an absent edge; do not turn an unreadable edge into missing.
    unresolved = absent if unknown else set()
    status = "unknown" if unknown else "correct" if observed == expected and not conflict else "incorrect"
    return {
        "status": status, "correct_edges": sorted(observed & expected),
        "incorrect_edges": sorted(observed - expected), "extra_edges": sorted(observed - expected),
        "missing_edges": sorted(absent - unresolved), "unresolved_expected_edges": sorted(unresolved),
        "unknown_edges": unknown, "cardinality_conflicts": conflict,
        "grading_unit": rule.grading_unit,
    }


def grade_choice(observed: list[str], accepted: list[str], *, uncertain=False, multiple=False):
    values = set(observed)
    if uncertain:
        return "unknown"
    if not multiple and len(values) > 1:
        return 'incorrect'
    if not values:
        return "missing"
    return "correct" if values == set(accepted) else "incorrect"


def grade_observation(cell, answer):
    if not cell or cell.get('uncertain') or cell.get('visibility', 'visible') != 'visible':
        return 'unknown', {'reason': (cell or {}).get('unknown_reason') or '作答区域或观察证据不清楚，需教师核实'}
    if cell.get('response_evidence') in ('ambiguous', 'not_visible'):
        return 'unknown', {'reason': cell.get('response_reason') or cell.get('unknown_reason') or '作答笔画证据不足，需教师核实'}
    encoding = answer.get('response_encoding', 'text')
    kind = answer.get('kind')
    if encoding == 'edges' or kind == 'matching':
        rule = answer.get('matching_rule') or answer.get('answer_spec')
        if not rule:
            return 'unknown', {}
        detail = grade_matching(__import__("autoreview.v2.contracts", fromlist=["LocalMatchingRule"]).LocalMatchingRule.model_validate(rule), [MatchingEdge.model_validate(e) for e in cell.get('edges', [])])
        return detail['status'], detail
    if encoding in ('written_choice', 'marked_choice', 'mixed') or kind in ('choice', 'letter_choice', 'marked_choice'):
        marked = cell.get('selected_options', [])
        written = cell.get('written_options', [])
        if (written or not marked) and (cell.get('transcription_uncertain') or cell.get('ambiguous_glyphs') or cell.get('literal_evidence') in ('ambiguous','not_visible')):
            return 'unknown', {'reason': cell.get('unknown_reason') or '手写选项字母笔画不明确，需教师核实'}
        if (written or not marked and cell.get('raw_english')) and cell.get('literal_evidence') == 'not_applicable':
            return 'unknown', {'reason': '手写选项缺少明确的字形证据，需教师核实'}
        if marked and written and set(marked) != set(written):
            return 'unknown', {'reason': '圈选与手写选项冲突'}
        observed = marked or written or ([cell['raw_english'].strip()] if cell.get('raw_english', '').strip() else [])
        accepted = ([x.strip() for x in answer.get('english', '').split(',') if x.strip()] if kind == 'choice' else
                    answer.get('answer_spec', {}).get('accepted') or [x.strip() for x in answer.get('english', '').split(',') if x.strip()])
        return grade_choice(observed, accepted, multiple=(answer.get('selection_rule') == 'multiple' if kind == 'choice' else answer.get('answer_spec', {}).get('multiple', False))), {'observed_options': observed}
    if cell.get('transcription_uncertain') or cell.get('ambiguous_glyphs') or cell.get('literal_evidence') in ('ambiguous','not_visible'):
        return 'unknown', {'reason': cell.get('unknown_reason') or '字母笔画证据不明确，文字或符号转录待教师核实'}
    raw = cell.get('symbol') if encoding == 'symbol' else cell.get('rank') if encoding == 'rank' else cell.get('raw_english')
    if cell.get('literal_evidence') == 'not_applicable' and raw and encoding not in ('symbol', 'rank'):
        return 'unknown', {'reason': '文字作答缺少明确的字形证据，需教师核实'}
    status = grade_text(raw, answer)
    detail = {}
    if kind == 'sentence' and raw is not None:
        expected = next((v for v in [answer.get('english',''), *answer.get('variants',[])] if comparable(v)==comparable(raw)), answer.get('english',''))
        words = lambda s: re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", s)
        observed_words, expected_words = words(raw), words(expected)
        lower, target = [w.lower() for w in observed_words], [w.lower() for w in expected_words]
        detail = {'word_inventory_equal': Counter(lower)==Counter(target), 'word_order_equal': lower==target,
                  'case_equal': observed_words==expected_words, 'punctuation_equal': re.findall(r'[^\w\s]',raw)==re.findall(r'[^\w\s]',expected),
                  'method': 'literal_comparison_no_autocorrection'}
    return status, detail


def resolve_page(reference: dict, page: dict, observation: dict | None) -> list[dict]:
    answers = [a for a in reference["items"] if a["slot"] == page["slot"] and not a.get("excluded")]
    cells = observation.get("cells", []) if observation else []
    reviews = page.get("overrides", {})
    results = []
    for answer in answers:
        matches = [c for c in cells if c.get("question_id") == answer["id"]]
        if not matches:
            matches = [c for c in cells if c.get("slot") == answer["slot"]
                       and not c.get('question_id') and c.get('section', '') == answer.get('section', '')
                       and c.get('part_id', '') == answer.get('part_id', '')
                       and str(c.get("number")) == str(answer["number"])]
        cell = matches[0] if len(matches) == 1 else None
        machine_raw = cell.get("raw_english") if cell else None
        status, typed_detail = grade_observation(cell, answer)
        verification = (observation or {}).get('verification', {}).get(answer['id'])
        if verification and status == 'correct' and verification.get('status') != 'verified':
            status = 'unknown'
            typed_detail = {**typed_detail, 'reason': verification.get('reason') or '独立复看尚未确认一致，需教师核实'}
        machine_status = status
        override = reviews.get(answer["id"], {})
        raw = override.get("raw_english", machine_raw)
        effective_cell = {**(cell or {}), **override.get('observation', {})}
        if 'observation' in override:
            status, typed_detail = grade_observation(effective_cell, answer)
        if "raw_english" in override and "status" not in override:
            status = grade_text(raw, answer)
        writing = next((item for item in (observation or {}).get("writing", [])
                        if item["question_id"] == answer["id"]), {})
        structure = next((s for s in (observation or {}).get('structure', []) if s['id'] == answer['id']), {})
        # A blank has no ink box, but its confirmed response area still needs an annotation.
        location = ((cell or {}).get('box') or structure.get('answer_zone') or
                    (page.get('preparation') or {}).get('zones', {}).get(answer['id']))
        if not location and not reference.get('asset_ids'):
            location = answer.get('box')
        effective_status=override.get('status',status)
        writing_status=override.get('writing_status',
            'pending' if writing.get('status') in ('needs_review','unknown') else
            'clear' if writing.get('status')=='no_issue' else 'not_assessed')
        # A wrong/blank answer already loses its content points. Do not demand
        # a second teacher action solely for the machine's handwriting doubt.
        # Older definite teacher overrides follow the same current rule; raw
        # writing candidates and immutable observations remain available.
        if writing_status=='pending' and 'writing_status' not in override and (
                effective_status in ('incorrect','missing') or
                'status' in override and effective_status in ('correct','incorrect','missing')):
            writing_status='clear'
        results.append({**answer, "machine_raw_english": machine_raw, "raw_english": raw,
                        "machine_status": machine_status, 'observation': effective_cell, 'machine_observation': cell, 'grading_detail': typed_detail,
                        'observed_structure': structure, 'verification': verification,
                        'geometry_warning': next((w['message'] for w in (observation or {}).get('geometry_warnings', []) if w.get('question_id') == answer['id']), None),
                        'verification_override': bool({'status','raw_english','observation'} & set(override)),
                        "status": effective_status,
                        "box": override.get("box", location),
                        "writing_candidate": writing, "writing_status": writing_status,
                         "score_unit_status":override.get("score_unit_status",{}), "manual_status":"status" in override and not override.get('score_unit_status'),
                         "teacher_transcription":override.get('raw_english'), "note": override.get("note", ""), "teacher_override": bool(override)})
    return results
