"""Offline direction/retention tests using generated images."""
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from autoreview.v2 import imaging, orientation


@pytest.fixture(autouse=True)
def clear_direction_cache():
    orientation._cache.clear()
    yield
    orientation._cache.clear()


def evidence(angle, count=8):
    return [{'angle': angle, 'confidence': .99, 'cell': (i % 3, i // 3)} for i in range(count)]


def photograph():
    return Image.fromarray(np.random.default_rng(3).integers(0, 256, (191, 271, 3), dtype=np.uint8))


@pytest.mark.parametrize('angle', [0, 90, 180, 270])
def test_direction_operation_is_exact_and_geometry_reversible(monkeypatch, angle):
    monkeypatch.setattr(orientation, '_collect_evidence', lambda pixels: (evidence(angle), 8))
    source = photograph()
    before = np.array(source)
    operations, log = imaging.auto_operations(source)
    result, geometry = imaging.render(source, operations)
    assert log[0]['status'] == 'completed'
    assert log[0]['algorithm'] == 'orientation-13.0'
    assert np.array_equal(np.array(result), np.rot90(before, angle // 90))
    assert np.array_equal(np.array(source), before)
    assert geometry['algorithm'] == 'geometry-5.1'
    assert not operations if angle == 0 else operations[0]['pixel_preserving']
    points = [[.01, .98], [.3, .41], [.99, .01]]
    assert np.allclose(imaging.project(imaging.project(points, geometry['forward']), geometry['inverse']), points)


def test_disabled_and_blank_images_do_not_load_models(monkeypatch):
    monkeypatch.setattr(orientation, '_load_engine', lambda: pytest.fail('models should remain lazy'))
    operations, log = imaging.auto_operations(photograph(), enabled=False)
    assert not operations and log[0]['status'] == 'disabled'
    operations, log = imaging.auto_operations(Image.new('RGB', (800, 500), 'white'))
    assert not operations and log[0]['status'] == 'needs_review'


@pytest.mark.parametrize('lines,considered', [
    (evidence(90, 3), 3),
    (evidence(0, 6) + evidence(180, 3), 9),
    ([{**item, 'cell': (0, 0)} for item in evidence(90)], 8),
    (evidence(180), 32),
])
def test_ambiguous_sparse_or_concentrated_evidence_never_rotates(monkeypatch, lines, considered):
    monkeypatch.setattr(orientation, '_collect_evidence', lambda pixels: (lines, considered))
    operations, log = imaging.auto_operations(photograph())
    assert not operations and log[0]['status'] == 'needs_review'
    assert log[0]['angle'] is None


@pytest.mark.parametrize('error', [ImportError('missing'), FileNotFoundError('missing'), RuntimeError('internal')])
def test_local_failure_preserves_pixels_and_is_retryable(monkeypatch, error):
    def fail(pixels):
        raise error
    monkeypatch.setattr(orientation, '_collect_evidence', fail)
    source = photograph()
    operations, log = imaging.auto_operations(source)
    assert not operations and log[0]['status'] == 'needs_review'
    assert log[0]['error_code'] in ('ORIENTATION_UNAVAILABLE', 'ORIENTATION_FAILED')
    monkeypatch.setattr(orientation, '_collect_evidence', lambda pixels: (evidence(90), 8))
    assert imaging.auto_operations(source)[1][0]['status'] == 'completed'


def test_evidence_cache_is_bounded_and_cannot_be_mutated_by_callers(monkeypatch):
    calls = []
    def collect(pixels):
        calls.append(1)
        return evidence(90), 8
    monkeypatch.setattr(orientation, '_collect_evidence', collect)
    monkeypatch.setattr(orientation, '_CACHE_SIZE', 2)
    source = photograph()
    first = orientation.detect_orientation(source)
    first['votes']['90'] = 0
    second = orientation.detect_orientation(source)
    assert second['votes']['90'] == 8 and second['cache_hit']
    assert len(calls) == 1
    orientation.detect_orientation(source.transpose(Image.Transpose.ROTATE_90))
    orientation.detect_orientation(source.transpose(Image.Transpose.ROTATE_180))
    assert len(orientation._cache) == 2
    assert not orientation.detect_orientation(source)['cache_hit']


@pytest.mark.parametrize('vertical,label,expected', [(False,'0',0),(False,'180',180),(True,'0',90),(True,'180',270)])
def test_line_geometry_and_classifier_combine_to_four_directions(monkeypatch, vertical, label, expected):
    boxes = []
    for x, y in [(20,20), (120,20), (20,120), (120,120), (20,220), (120,220)]:
        w, h = (12, 70) if vertical else (70, 12)
        boxes.append([[x,y],[x+w,y],[x+w,y+h],[x,y+h]])
    detector = lambda pixels: SimpleNamespace(boxes=boxes)
    classifier = lambda images: SimpleNamespace(cls_res=[(label,.999)]*len(images))
    monkeypatch.setattr(orientation, '_load_engine', lambda: (detector, classifier, lambda pixels, box: pixels[:12,:70]))
    lines, count = orientation._collect_evidence(np.zeros((320,320,3), dtype=np.uint8))
    assert count == 6 and all(line['angle'] == expected for line in lines)
    assert orientation._decide(lines,count)['status'] == 'completed'


def test_empty_render_avoids_interpolation(monkeypatch):
    monkeypatch.setattr(imaging.cv2, 'warpPerspective', lambda *args, **kwargs: pytest.fail('identity does not resample'))
    image = photograph()
    out, geometry = imaging.render(image, [])
    assert out is not image and np.array_equal(np.array(out), np.array(image))
    assert geometry['source_size'] == geometry['size'] == list(image.size)
