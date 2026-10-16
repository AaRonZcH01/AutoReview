"""v1.2 handwritten-form guidance; the frozen 5.2 decoder remains untouched."""
from .contracts_v11 import record as prior_record, decode
from .core import digest
HANDWRITING='''\nPrioritize handwritten stroke structure rather than resemblance to a particular printed font. Accept clearly distinguished natural handwritten forms, including single-storey a/g and their clearly legible handwritten alternatives. These are not spelling corrections or automatic deductions. Inspect i/j dots, t crossbars, descenders and letter spacing as visible evidence; faint dots/crossbars or joined letters must be reported as ambiguous, not silently supplied. Any explicit teacher writing rule, such as lower-hook handwritten y, is a separate writing candidate only when visible evidence supports it; never convert low-confidence recognition into a handwriting error. Preserve the literal transcription and leave uncertain letter identities for teacher review.\n'''
def record(prompt):
    value=prior_record(prompt);text=value['template_text']+HANDWRITING
    return {**value,'id':'prompt-student-5.3-'+digest({'text':text,'schema':value['schema']})[:10],
            'template_text':text,'prompt_hash':digest(text),'schema_version':'5.3','version':7,
            'change_note':'v1.2 手写体字形优先，a/g 可辨替代体、i/j 点与 t 横证据不清交教师核实'}
