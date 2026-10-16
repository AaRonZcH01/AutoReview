import io
import json
from pathlib import Path
from openpyxl import load_workbook
from autoreview.v2.reference_pack import export_pack, import_pack
from autoreview.v2.reports import export_report
from test_workflow import service


def test_repeated_reference_import_keeps_linear_history(service):
    task = service.create('tasks', {'name': 'Repeated reference import'})
    service.draft(task['id'], {'items': [{'id': 'q', 'slot': 1, 'number': '1', 'english': 'apple'}]})
    content = Path(export_pack(service, task['id'])['path']).read_bytes()
    revisions = []
    sizes = []
    for _ in range(8):
        revisions.append(service.draft(task['id'])['revision'])
        imported = import_pack(service, task['id'], content)
        sizes.append(len(json.dumps(imported)))
        history = imported['replaced_drafts']
        assert [row['revision'] for row in history] == revisions
        assert all('replaced_drafts' not in row for row in history)
        assert all(row['items'][0]['english'] == 'apple' for row in history)
    # Eight imports retain eight exact old drafts, not 255 recursive copies.
    assert sizes[-1] < sizes[0] * 7


def test_xlsx_summary_reference_is_literal_text(service, monkeypatch):
    task = service.create('tasks', {'name': 'Literal export'})
    formula = '=1+2'
    row = {'paper_tag': '001', 'group_id': '=group', 'slot': 1, 'number': '=number',
           'expected': formula, 'raw_english': '=student', 'status': 'incorrect'}
    stats = {'reviewed_submissions': 1, 'page_revisions': {}, 'rows': [row],
             'questions': [{'slot': 1, 'number': '=number', 'expected': formula,
                            'known': 1, 'errors': 1, 'unknown': 0, 'error_rate': 1.0}]}
    # Export formatting is independent of grading; no model or assets involved.
    row['page_id'] = None
    monkeypatch.setattr('autoreview.v2.reports.statistics', lambda *a, **k: stats)
    # Snapshot contains no page material for this formatting-only fixture.
    original = service.store.get
    def get(db, table, identifier, **kwargs):
        if table == 'pages' and identifier is None:
            return {'id': None, 'revision': 1, 'image_version_id': None}
        return original(db, table, identifier, **kwargs)
    stats['page_revisions'][None] = 1
    monkeypatch.setattr(service.store, 'get', get)
    monkeypatch.setattr(service, 'results', lambda *a, **k: [])
    exported = export_report(service, task['id'], {'format': 'xlsx'})
    book = load_workbook(io.BytesIO(Path(exported['output_path']).read_bytes()), data_only=False)
    assert book['错题统计']['B2'].value == '=number'
    assert book['错题统计']['C2'].value == formula
    for sheet in book:
        assert all(cell.data_type != 'f' for row in sheet for cell in row)
    assert book['错题统计']['D2'].value == 1
