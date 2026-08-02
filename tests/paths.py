"""Paths to the files the suite reads, resolved from this file's location.

Several tests read the real server.py, static/index.html or README.md rather
than a fixture -- checking that the shipped page defines a handler, say, or
that the README still documents a limit the code enforces. Those were written
as bare relative opens, which only worked while the tests sat in the repo
root and pytest was invoked from there.

Anchoring to __file__ means the suite runs the same from anywhere:
`pytest`, `pytest tests/`, or `pytest test_ui.py` from inside tests/.
"""
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

INDEX_HTML = os.path.join(PROJECT_ROOT, 'static', 'index.html')
SERVER_PY = os.path.join(PROJECT_ROOT, 'server.py')
README_MD = os.path.join(PROJECT_ROOT, 'README.md')
QUEUEEDIT_JS = os.path.join(PROJECT_ROOT, 'sonos-actions', 'queueedit.js')


def read(path):
    with open(path) as handle:
        return handle.read()
