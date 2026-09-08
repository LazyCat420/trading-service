"""Catalog generation must precede consumer image builds and publish whole files."""
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def generator(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[2] / 'scripts/build_tool_schemas.py'
    spec = importlib.util.spec_from_file_location('schema_build_audit', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / 'source' / 'trading'
    source.mkdir(parents=True)
    (source / 'notes.json').write_text(json.dumps([{'name': 'whiteboard_read', 'description': 'read', 'parameters': {}}]))
    targets = [tmp_path / name / 'tool_schemas.json' for name in ('adapter', 'backend', 'dashboard')]
    for target in targets:
        target.parent.mkdir()
        target.write_text('[{"name":"request_peer_analysis"}]')
    monkeypatch.setattr(module, 'FLAT_TARGETS', [str(p) for p in targets])
    return module, source.parent, targets


def test_rebuild_removes_retired_tools_from_every_consumer(generator):
    module, source, targets = generator
    module.build(str(source))
    assert len({p.read_bytes() for p in targets}) == 1
    for target in targets:
        assert [t['name'] for t in json.loads(target.read_text())] == ['whiteboard_read']


def test_failed_publication_keeps_previous_valid_catalog(generator, monkeypatch):
    module, source, targets = generator
    original = targets[0].read_bytes()
    def fail_replace(src, dst):
        # A reader still sees the complete original while the replacement is
        # being written; a failed publish cannot truncate that public file.
        assert Path(dst).read_bytes() == original
        assert json.loads(Path(src).read_text())[0]['name'] == 'whiteboard_read'
        raise OSError('fixture publish failure')
    monkeypatch.setattr(module.os, 'replace', fail_replace)
    with pytest.raises(OSError, match='fixture publish failure'):
        module.build(str(source))
    assert targets[0].read_bytes() == original
    assert not list(targets[0].parent.glob('.tool-schemas-*'))
