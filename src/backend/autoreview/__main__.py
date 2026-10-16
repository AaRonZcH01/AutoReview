import os
from pathlib import Path

import uvicorn


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[3]
    os.chdir(root)
    uvicorn.run('autoreview.api:app', host='127.0.0.1', port=int(os.environ.get('AUTOREVIEW_API_PORT', '8000')), reload=False)
