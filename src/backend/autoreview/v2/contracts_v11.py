"""New requests require explicit visual certainty; frozen 5.1 decoding is unchanged."""
from typing import Literal
from pydantic import Field
from .contracts import Contract, Box, strict_schema
from .contracts_v5 import StudentCellV51, STUDENT_SUPPLEMENT
from .core import digest


class AmbiguousGlyph(Contract):
    position: int = Field(ge=1)
    candidates: list[str]
    reason: str
    box: Box | None


class StudentCellV52(StudentCellV51):
    literal_evidence: Literal['clear','ambiguous','not_visible','not_applicable']
    ambiguous_glyphs: list[AmbiguousGlyph]


class StudentOutputV52(Contract):
    cells: list[StudentCellV52]
    photo_issues: list[str]


SUPPLEMENT='''\nFor every handwritten text response inspect actual strokes before choosing literal_evidence. o/a, u/v, n/h, m/n, b/d/p/q, i/l and c/e are easily confused. Do not accept a plausible word unless its distinguishing strokes are clearly visible. For example, visible dod must remain dod; do not repair it to dad. If the distinguishing stroke is faint, occluded, erased, joined or unresolved, set literal_evidence=ambiguous, transcription_uncertain=true and list ambiguous_glyphs with 1-based character position, literal alternatives, localized box when supported, and a concrete Chinese description of the unresolved stroke. unknown_reason must be Chinese and identify the letter/position and the specific visual uncertainty. Do not claim a handwriting defect merely from ambiguity. A clearly visible nonword uses literal_evidence=clear and is transcribed exactly; invalid English alone is never evidence of ambiguity. For clearly observable marked choices or traced lines with no relevant text use not_applicable and an empty ambiguous_glyphs list; uncertain optional annotations must not erase clear choice evidence. Use not_visible only for actual invisibility. photo_issues must be Chinese and identify affected supplied question aliases only when evidenced; do not invent location. No reference correct answers or inferred vocabulary may guide the transcription.\n'''


def record(prompt):
    schema=strict_schema(StudentOutputV52);text=prompt+STUDENT_SUPPLEMENT+SUPPLEMENT
    return {'id':'prompt-student-5.2-'+digest({'text':text,'schema':schema,'grading':'typed-5.2'})[:10],
        'purpose':'student','version':6,'template_text':text,'prompt_hash':digest(text),
        'schema':schema,'schema_hash':digest(schema),'schema_version':'5.2',
        'preprocessing_version':'geometry-5.1','grading_rule_version':'typed-5.2',
        'change_note':'v1.1 易混淆字母笔画证据与中文待核实原因；不改写冻结观察'}


def decode(raw):
    value=StudentOutputV52.model_validate_json(raw).model_dump()
    for cell in value['cells']:
        glyphs=cell['ambiguous_glyphs']
        if glyphs or cell['literal_evidence'] in ('ambiguous','not_visible'):
            cell['transcription_uncertain']=True
            if glyphs:
                cell['unknown_reason']='；'.join('第'+str(g['position'])+'个字母（'+'/'.join(g['candidates'])+'）：'+g['reason'] for g in glyphs)
            elif not cell.get('unknown_reason'):
                cell['unknown_reason']='笔画证据不明确，需教师查看原图核实。'
    return value
