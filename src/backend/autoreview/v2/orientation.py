"""Local quarter-turn evidence only: no transcription or answer-dependent inference.

The detector locates long text lines, and the classifier distinguishes their two
reading directions. Their geometry supplies the other two quarter turns. Missing
models and ambiguous evidence leave every original pixel in place for the teacher.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import hashlib
from pathlib import Path
import threading
import time

import numpy as np
from PIL import Image

VERSION = 'orientation-13.0'
_MAX_SIDE = 1200
_MAX_LINES = 32
_CACHE_SIZE = 96
_lock = threading.RLock()
_engine = None
_cache = OrderedDict()


def _load_engine():
    """Use installed, explicitly named models; never initiate a model download."""
    global _engine
    if _engine is not None:
        return _engine
    import rapidocr
    from rapidocr.ch_ppocr_det import TextDetector
    from rapidocr.ch_ppocr_cls import TextClassifier
    from rapidocr.utils.parse_parameters import ParseParams
    from rapidocr.utils.process_img import get_rotate_crop_image

    root = Path(rapidocr.__file__).parent
    paths = {'Det': root / 'models' / 'PP-OCRv6_det_small.onnx',
             'Cls': root / 'models' / 'ch_ppocr_mobile_v2.0_cls_mobile.onnx'}
    if not all(path.is_file() for path in paths.values()):
        raise FileNotFoundError('Local orientation models are missing')
    cfg = ParseParams.load(root / 'config.yaml')
    cfg = ParseParams.update_batch(cfg, {
        'Det.model_path': str(paths['Det']), 'Cls.model_path': str(paths['Cls']),
        'Det.limit_type': 'max',
        'EngineConfig.onnxruntime.intra_op_num_threads': 1,
        'EngineConfig.onnxruntime.inter_op_num_threads': 1,
        'EngineConfig.onnxruntime.use_cuda': False,
        'EngineConfig.onnxruntime.use_dml': False,
        'EngineConfig.onnxruntime.use_cann': False,
        'EngineConfig.onnxruntime.use_coreml': False,
    })
    for name in ('Det', 'Cls'):
        cfg[name].engine_cfg = cfg.EngineConfig[cfg[name].engine_type.value]
        cfg[name].model_root_dir = str(root / 'models')
    _engine = TextDetector(cfg.Det), TextClassifier(cfg.Cls), get_rotate_crop_image
    return _engine


def _collect_evidence(pixels):
    detector, classifier, crop = _load_engine()
    detected = detector(pixels)
    if detected.boxes is None:
        return [], 0
    candidates = []
    height, width = pixels.shape[:2]
    for box in detected.boxes:
        box = np.asarray(box, dtype=np.float32)
        w = max(np.linalg.norm(box[0]-box[1]), np.linalg.norm(box[2]-box[3]))
        h = max(np.linalg.norm(box[0]-box[3]), np.linalg.norm(box[1]-box[2]))
        if min(w, h) < 7 or max(w, h) / min(w, h) < 2.4:
            continue  # Single letters, arrows and diagrams are not reading evidence.
        center = box.mean(axis=0)
        cell = (min(2, int(center[0]*3/width)), min(2, int(center[1]*3/height)))
        candidates.append((float(max(w, h)), cell, 90 if h/w >= 1.5 else 0, box))
    # Take long lines across the page, not only a repeated title in one corner.
    groups = {}
    for entry in sorted(candidates, key=lambda value: value[0], reverse=True):
        groups.setdefault(entry[1], []).append(entry)
    chosen = []
    while groups and len(chosen) < _MAX_LINES:
        for cell in list(groups):
            chosen.append(groups[cell].pop(0))
            if not groups[cell]:
                del groups[cell]
            if len(chosen) == _MAX_LINES:
                break
    if not chosen:
        return [], 0
    result = classifier([crop(pixels, entry[3]) for entry in chosen])
    evidence = []
    for entry, (label, score) in zip(chosen, result.cls_res):
        if str(label) in ('0', '180') and np.isfinite(score) and float(score) >= .95:
            evidence.append({'angle': (entry[2]+int(label)) % 360,
                             'confidence': float(score), 'cell': entry[1]})
    return evidence, len(chosen)


def _decide(evidence, considered):
    votes = {angle: [] for angle in (0, 90, 180, 270)}
    for entry in evidence:
        votes[entry['angle']].append(entry)
    best = max(votes, key=lambda angle: len(votes[angle]))
    agreeing = votes[best]
    fraction = len(agreeing) / max(1, len(evidence))
    confidence = (sum(entry['confidence'] for entry in agreeing) / max(1, len(agreeing))) * fraction
    details = {'candidate_degrees_ccw': best if evidence else None,
               'confidence': round(confidence, 4), 'text_lines': considered,
               'reliable_lines': len(evidence), 'agreeing_lines': len(agreeing),
               'consensus': round(fraction, 4),
               'votes': {str(angle): len(entries) for angle, entries in votes.items()}}
    # Require several independent long lines and clear agreement, including an
    # adequate share of classified lines; low confidence must not disappear from
    # the denominator and manufacture a strong result from weak evidence.
    reliable = (len(agreeing) >= 5 and fraction >= .85
                and len(evidence) / max(1, considered) >= .65
                and len({entry['cell'] for entry in agreeing}) >= 2)
    if not reliable:
        return {**details, 'status': 'needs_review', 'angle': None,
                'reason': '文字方向证据不足或不一致，保留原方向；请查看图片并手动旋转。'}
    angle = -90 if best == 270 else best
    names = {90: '逆时针90°', -90: '顺时针90°', 180: '180°'}
    reason = ('已检查，文字方向正确，无需旋转。' if angle == 0 else
              f'已根据{len(agreeing)}条一致文字方向旋转{names[angle]}，保留完整画面；请核对。')
    return {**details, 'status': 'completed', 'angle': angle, 'reason': reason}


def detect_orientation(image, *, enabled=True):
    started = time.perf_counter()
    base = {'operation': 'orientation', 'source': 'automatic', 'algorithm': VERSION}
    if not enabled:
        return {**base, 'status': 'disabled', 'angle': None, 'confidence': None,
                'reason': '自动图片正向已关闭，保留导入方向。', 'elapsed_ms': 0, 'cache_hit': False}
    small = image.convert('RGB')
    small.thumbnail((_MAX_SIDE, _MAX_SIDE), Image.Resampling.LANCZOS)
    rgb = np.asarray(small)
    if min(small.size) < 96 or float(np.std(rgb.astype(np.float32).mean(axis=2))) < 3:
        result = {'status': 'needs_review', 'angle': None, 'confidence': 0,
                  'reason': '图片太小或没有足够文字，保留原方向；请查看图片并手动旋转。'}
        return {**base, **result, 'elapsed_ms': round((time.perf_counter()-started)*1000), 'cache_hit': False}
    key = (image.size, small.size, hashlib.sha256(rgb.tobytes()).digest())
    cache_hit = False
    # Detector preprocessing is mutable. Serialize its bounded CPU work; cache
    # only small evidence dictionaries, never full images or recognized text.
    with _lock:
        if key in _cache:
            result = deepcopy(_cache[key])
            _cache.move_to_end(key)
            cache_hit = True
        else:
            try:
                evidence, considered = _collect_evidence(np.ascontiguousarray(rgb[:, :, ::-1]))
                result = _decide(evidence, considered)
            except (ImportError, FileNotFoundError):
                result = {'status': 'needs_review', 'angle': None, 'confidence': None,
                          'reason': '本地文字方向组件或模型缺失，未旋转；请手动旋转并检查本地依赖。',
                          'error_code': 'ORIENTATION_UNAVAILABLE'}
            except Exception as error:
                result = {'status': 'needs_review', 'angle': None, 'confidence': None,
                          'reason': '本地文字方向检测未完成，保留原方向；请手动旋转。',
                          'error_code': 'ORIENTATION_FAILED', 'error_type': type(error).__name__}
            if 'error_code' not in result:
                _cache[key] = deepcopy(result)
                while len(_cache) > _CACHE_SIZE:
                    _cache.popitem(last=False)
    return {**base, **result, 'elapsed_ms': round((time.perf_counter()-started)*1000), 'cache_hit': cache_hit}
