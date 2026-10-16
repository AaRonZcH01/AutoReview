"""Independent, blind observations. Existing frozen contracts remain decodable."""
from typing import Literal
from .contracts import Contract, strict_schema
from .contracts_v11 import StudentCellV52, SUPPLEMENT
from .contracts_v5 import STUDENT_SUPPLEMENT
from .contracts_v12 import HANDWRITING
from .core import digest


class StudentCellV60(StudentCellV52):
    response_evidence: Literal['clear', 'ambiguous', 'not_visible']
    response_reason: str


class StudentOutputV60(Contract):
    cells: list[StudentCellV60]
    photo_issues: list[str]


EVIDENCE = '''\nFor EVERY response, including marked choices, handwritten option letters, symbols, ranks and matching lines, report response_evidence. Use clear only when the actual response is unambiguous from visible strokes; ambiguous or not_visible MUST also set uncertain=true and explain the specific problem in Chinese in response_reason. A clear text answer MUST use literal_evidence=clear. not_applicable is only for non-text evidence such as a clear circled option or traced matching line, never a way to bypass uncertain handwriting. An ambiguous handwritten option letter remains ambiguous even if one candidate would be plausible. Clear choice marks need not be rejected solely because unrelated optional annotations are illegible. Confidence in a plausible answer is not evidence of the student's actual strokes. Never infer from grammar, spelling frequency or apparent correctness.\n'''
BLIND = '''\nThis is an independent visual observation of student response regions. You are NOT given another observation or a correct answer. Do not guess why any region was selected. All images are native-resolution crops assembled without scaling. crop_manifest and structure describe image placement and question identities only. Inspect each region's complete question context and all linked regions (including options or endpoints on other images). Output each requested question_id EXACTLY once. Coordinates refer to the supplied atlas image; keep paths/boxes within its region, not a different tile. If a response or necessary context is cut off or missing, report not_visible or ambiguous. Do not treat a printed option as selected and do not infer missing matching lines.\n'''


def record(prompt, purpose='student'):
    schema = strict_schema(StudentOutputV60)
    text = prompt + STUDENT_SUPPLEMENT + SUPPLEMENT + HANDWRITING + EVIDENCE
    if purpose == 'student_verify':
        text += BLIND
    return {'id': 'prompt-' + purpose + '-6.0-' + digest({'text': text, 'schema': schema})[:12],
        'purpose': purpose, 'version': 8, 'template_text': text, 'prompt_hash': digest(text),
        'schema': schema, 'schema_hash': digest(schema), 'schema_version': '6.0',
        'preprocessing_version': 'native-crop-1', 'grading_rule_version': 'typed-6.0',
        'change_note': 'v1.3 全题型证据门禁与独立原像素盲复看；旧观察不改写'}


def decode(raw):
    value = StudentOutputV60.model_validate_json(raw).model_dump()
    for cell in value['cells']:
        if cell['response_evidence'] != 'clear':
            cell['uncertain'] = True
            cell['unknown_reason'] = cell['response_reason'] or '作答证据不明确，需教师核实。'
        if cell['ambiguous_glyphs'] or cell['literal_evidence'] in ('ambiguous', 'not_visible'):
            cell['transcription_uncertain'] = True
    return value
