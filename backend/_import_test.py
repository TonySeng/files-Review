import sys
from unittest.mock import MagicMock
for name in ['fitz', 'docx', 'openpyxl', 'xlrd', 'uvicorn']:
    sys.modules[name] = MagicMock()
sys.path.insert(0, '.')

import main
app = main.app
paths = sorted({r.path for r in app.routes})
print('TOTAL_ROUTES', len(paths))
want = ['/api/file-types', '/api/file-types/{file_id}',
        '/api/rule-groups', '/api/rule-groups/{group_id}',
        '/api/files/{file_id}/type']
for w in want:
    print(('FOUND ' if w in paths else 'MISSING '), w)
from models.schemas import FileValidation, FileType
rv = FileValidation(ok=False, level='error', messages=['x'])
print('schema FileValidation level:', rv.level)
ft = FileType(id='ft-1', name='测试', extensions=['.pdf'], rule_group_ids=['rg-completeness'])
print('schema FileType ok:', ft.name, ft.extensions)
