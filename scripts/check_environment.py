"""Offline import/PNG/SQLite checks without personal state or model calls."""
from io import BytesIO
import importlib
import json
import sqlite3
from PIL import Image

for name in ('fastapi', 'pydantic', 'uvicorn', 'openpyxl', 'cv2', 'numpy', 'rapidocr', 'onnxruntime', 'websockets'):
    importlib.import_module(name)
image = BytesIO()
Image.new('RGB', (16, 16), 'white').save(image, 'PNG')
assert Image.open(BytesIO(image.getvalue())).size == (16, 16)
with sqlite3.connect(':memory:') as db:
    assert db.execute('select 1').fetchone()[0] == 1
print(json.dumps({'imports': 'passed', 'png': 'passed', 'sqlite': 'passed', 'model_calls': 0}))
