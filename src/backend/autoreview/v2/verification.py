"""Native-pixel blind rereading, conservative reconciliation and immutable evidence.

This module never sends an answer key or first-pass observation to the provider.
It only prepares local artifacts and reconciles returned observations. The runner
owns consent, attempts, retries, cancellation, concurrency and transaction fences.
"""
from __future__ import annotations
import copy
import math
import re
from pathlib import Path
from PIL import Image, ImageOps
from .core import require, atomic_write
from .imaging import project, corners
from .grading import grade_observation, comparable

LEGACY_POLICY_VERSION = 'blind-native-1'
POLICY_VERSION = 'blind-native-2'
MAX_TILE_PIXELS = 28_000_000
MAX_ATLAS_PIXELS = 32_000_000
MAX_ATLAS_IMAGES = 6
MAX_QUESTIONS = 40
MAX_IMAGE_PIXELS = 9_000_000


def candidates(reference, output, slot, policy=None):
    from .scoring import matching_sources, matching_source_status
    found = []
    for answer in reference['items']:
        if answer.get('excluded') or answer['slot'] != slot:
            continue
        cells = [c for c in output.get('cells', []) if c.get('question_id') == answer['id']]
        cell = cells[0] if len(cells) == 1 else None
        status, detail = grade_observation(cell, answer)
        sources = []
        if answer.get('kind') == 'matching' and detail:
            result = {**answer, 'status': status, 'grading_detail': detail}
            sources = [n['id'] for n in matching_sources(answer)
                       if matching_source_status(result, n['id']) == 'correct']
        partial = False
        if status != 'unknown':
            for unit in (policy or {}).get('units', []):
                if unit.get('question_id') != answer['id']:
                    continue
                if unit.get('rule') == 'sentence_component' and detail.get(unit.get('component')) is True:
                    partial = True
                if unit.get('rule') == 'sequence_position':
                    expected = re.split(r'[,，;；\s]+', answer.get('english', '').strip())
                    actual = re.split(r'[,，;；\s]+', (cell or {}).get('rank') or (cell or {}).get('raw_english') or '')
                    position = unit.get('position', -1)
                    partial |= 0 <= position < min(len(expected), len(actual)) and expected[position] == actual[position]
        if status == 'correct' or sources or partial:
            found.append({'question_id': answer['id'], 'source_ids': sources})
    return found


def _box_union(boxes):
    values = [b for b in boxes if b and all(isinstance(b.get(k), (int, float)) and math.isfinite(b[k]) for k in ('x1', 'y1', 'x2', 'y2'))]
    if not values:
        return None
    return {'x1': min(b['x1'] for b in values), 'y1': min(b['y1'] for b in values),
            'x2': max(b['x2'] for b in values), 'y2': max(b['y2'] for b in values)}


def _project_box(box, matrix):
    points = project(corners(box), matrix)
    return {'x1': min(p[0] for p in points), 'y1': min(p[1] for p in points),
            'x2': max(p[0] for p in points), 'y2': max(p[1] for p in points)}


def _native_rotation(asset):
    """Only restore an axis-aligned quarter turn, including translated crops.

    A perspective or arbitrary-angle transform cannot be applied to the native
    pixels without interpolation. It therefore keeps the original orientation.
    """
    matrix = asset.get('geometry', {}).get('forward')
    if not matrix or len(matrix) != 3 or any(len(row) != 3 for row in matrix):
        return 0
    if not all(isinstance(v, (float, int)) and math.isfinite(v) for row in matrix for v in row) or abs(matrix[2][2]) < 1e-8:
        return 0
    m = [[v / matrix[2][2] for v in row] for row in matrix]
    if abs(m[2][0]) > 1e-8 or abs(m[2][1]) > 1e-8:
        return 0
    a, b = m[0][:2]; c, d = m[1][:2]
    if abs(a) < 1e-8 and abs(d) < 1e-8:
        if b > 0 and c < 0: return 90
        if b < 0 and c > 0: return 270
    if abs(b) < 1e-8 and abs(c) < 1e-8 and a < 0 and d < 0:
        return 180
    return 0


def _orient_tile(image, angle):
    width, height = image.size
    matrices = {0: [[1,0,0],[0,1,0],[0,0,1]],
        90: [[0,1,0],[-1,0,width],[0,0,1]],
        180: [[-1,0,width],[0,-1,height],[0,0,1]],
        270: [[0,-1,height],[1,0,0],[0,0,1]]}
    if not angle:
        return image, matrices[0]
    methods = {90: Image.Transpose.ROTATE_90, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_270}
    turned = image.transpose(methods[angle])
    image.close()
    return turned, matrices[angle]


def prepare(service, work_id, pages, structure, candidates, *, policy_version=POLICY_VERSION):
    """Returns provider batches and explicit refusals; no business state changes.

    pages must cover the frozen owner and dependency image versions. Each output
    pixel in a tile is an unchanged EXIF-oriented original pixel. Source geometry
    maps current displayed regions back to that original, avoiding repeated warp.
    """
    require(policy_version in (LEGACY_POLICY_VERSION, POLICY_VERSION), 'VERIFY_POLICY', '复看策略版本不受支持，请创建新处理轮次。')
    require(bool(re.fullmatch(r'[A-Za-z0-9_-]+', str(work_id))), 'VERIFY_SCOPE', '复看作业标识无效。')
    root = service.store.root / 'attempt-artifacts' / str(work_id) / 'blind-verification'
    root.mkdir(parents=True, exist_ok=True)
    page_map = {p['slot']: p for p in pages}
    structures = {s['id']: s for s in structure}
    originals = {}
    rejected, groups = [], []
    try:
        for candidate in candidates:
            qid = candidate['question_id']
            value = structures.get(qid)
            if not value or not value.get('answer_zone'):
                rejected.append({'question_id': qid, 'source_ids': candidate.get('source_ids', []), 'reason': '缺少可靠的完整题目区域，未进行自动复看。'})
                continue
            tiles = []
            try:
                regions = {value['slot']: [value['answer_zone']]}
                for node in value.get('options', []) + value.get('nodes', []):
                    require(node.get('box'), 'VERIFY_GEOMETRY', '选项或连线端点缺少可靠区域，需教师核实。')
                    regions.setdefault(node.get('slot', value['slot']), []).append(node['box'])
                for slot, boxes in regions.items():
                    require(slot in page_map, 'VERIFY_DEPENDENCY', '必要的跨页图片缺失，需教师核实。')
                    p = page_map[slot]
                    if slot not in originals:
                        with service.store.connection() as db:
                            asset = service.store.get(db, 'assets', p['asset_id'])
                        with Image.open(service.asset(p['asset_id'], 'original')) as source:
                            original = ImageOps.exif_transpose(source).convert('RGB')
                        originals[slot] = (original, asset)
                    original, asset = originals[slot]
                    inverse = asset.get('geometry', {}).get('inverse')
                    require(inverse, 'VERIFY_GEOMETRY', '图片缺少原图坐标映射，需教师核实。')
                    for required_box in boxes:
                        mapped = _project_box(required_box, inverse)
                        require(mapped['x1'] >= -.015 and mapped['y1'] >= -.015 and mapped['x2'] <= 1.015 and mapped['y2'] <= 1.015,
                                'VERIFY_GEOMETRY', '完整作答区域超出原图，无法安全复看，需教师核实。')
                    box = _box_union(boxes)
                    require(box and box['x1'] < box['x2'] and box['y1'] < box['y2'], 'VERIFY_GEOMETRY', '题目区域无效，需教师核实。')
                    # Retain printed stems/context, full options and entire matching
                    # space. A one-word ink crop is insufficient independent input.
                    pad_x = max(.025, (box['x2']-box['x1'])*.12)
                    pad_y = max(.025, (box['y2']-box['y1'])*.24)
                    context = {'x1': max(0, box['x1']-pad_x), 'y1': max(0, box['y1']-pad_y),
                               'x2': min(1, box['x2']+pad_x), 'y2': min(1, box['y2']+pad_y)}
                    source_box = _project_box(context, inverse)
                    w, h = original.size
                    pixels = (max(0, math.floor(source_box['x1']*w)), max(0, math.floor(source_box['y1']*h)),
                              min(w, math.ceil(source_box['x2']*w)), min(h, math.ceil(source_box['y2']*h)))
                    require(pixels[2] > pixels[0] and pixels[3] > pixels[1], 'VERIFY_GEOMETRY', '题目区域未落在原图内，需教师核实。')
                    angle = _native_rotation(asset)
                    cropped, native_to_tile = _orient_tile(original.crop(pixels), angle)
                    tiles.append({'question_id': qid, 'slot': slot, 'pixels': pixels,
                        'image': cropped, 'source_size': [w, h],
                        'native_rotation_degrees': angle, 'native_to_tile': native_to_tile,
                        'image_version_id': p['image_version_id'], 'asset_id': p['asset_id'],
                        'forward': asset['geometry']['forward'], 'inverse': inverse})
                groups.append({'candidate': candidate, 'structure': value, 'tiles': tiles})
            except Exception as error:
                for tile in tiles:
                    tile['image'].close()
                rejected.append({'question_id': qid, 'source_ids': candidate.get('source_ids', []), 'reason': str(error) or '局部原图准备失败，需教师核实。'})
        # Preserve a question's complete cross-page evidence in one provider call.
        # Calls are bounded without downscaling oversized individual question tiles.
        if policy_version == LEGACY_POLICY_VERSION:
            # Preserve the exact grouping/paths/variables of paused v1 runs so
            # durable checkpoints can still be adopted without a new call.
            chunks, current, pixels = [], [], 0
            for group in groups:
                count = sum(t['image'].width*t['image'].height for t in group['tiles'])
                if current and (pixels+count > 14_000_000 or len(current) >= 40):
                    chunks.append(current); current = []; pixels = 0
                current.append(group); pixels += count
            if current:
                chunks.append(current)
        else:
            chunks = _bounded_chunks(groups)
        batches = []
        for index, chunk in enumerate(chunks):
            batch = _atlas(root, index, chunk, policy_version=policy_version)
            if policy_version != LEGACY_POLICY_VERSION:
                batch['manifest']['request_budget'] = _request_size(chunk)
            batches.append(batch)
        return {'batches': batches, 'rejected': rejected, 'policy_version': policy_version}
    finally:
        for image, _ in originals.values():
            image.close()
        for group in groups:
            for tile in group['tiles']:
                tile['image'].close()


def _request_size(groups):
    """Dry-run the unchanged shelf layout without allocating/resampling pixels.

    Actual atlas pixels include blank padding, unlike the legacy tile budget.
    An indivisible question may exceed a limit; it retains all native context in
    an isolated request rather than silently resizing or dropping dependencies.
    """
    dimensions = []
    has_tiles = False
    width = height = x = y = row_h = total = largest = 0
    for group in groups:
        for tile in group['tiles']:
            w, h = tile['image'].size
            total += w*h; largest = max(largest, w*h)
            if x and x+w > 3000:
                x = 0; y += row_h+24; row_h = 0
            if has_tiles and (y+h > 3000 or max(width, x+w)*max(height, y+h) > MAX_IMAGE_PIXELS):
                dimensions.append((width, height))
                has_tiles = False
                width = height = x = y = row_h = 0
            has_tiles = True
            width = max(width, x+w); height = max(height, y+h)
            x += w+24; row_h = max(row_h, h)
    if has_tiles:
        dimensions.append((width, height))
    return {'question_count':len(groups), 'tile_pixels':total,
            'atlas_pixels':sum(w*h for w,h in dimensions), 'image_count':len(dimensions),
            'largest_tile_pixels':largest,
            'oversized_question_isolated':len(groups)==1 and (largest>MAX_IMAGE_PIXELS or
                total>MAX_TILE_PIXELS or sum(w*h for w,h in dimensions)>MAX_ATLAS_PIXELS or len(dimensions)>MAX_ATLAS_IMAGES)}


def _within_budget(value):
    return (value['tile_pixels'] <= MAX_TILE_PIXELS and value['atlas_pixels'] <= MAX_ATLAS_PIXELS
            and value['image_count'] <= MAX_ATLAS_IMAGES and value['question_count'] <= MAX_QUESTIONS)


def _bounded_chunks(groups):
    chunks, current = [], []
    for group in groups:
        single = _request_size([group])
        if single['oversized_question_isolated']:
            if current:
                chunks.append(current); current = []
            chunks.append([group])
            continue
        proposed = [*current, group]
        if current and not _within_budget(_request_size(proposed)):
            chunks.append(current); current = []
        current.append(group)
    if current:
        chunks.append(current)
    return chunks


def _atlas(root, index, groups, *, policy_version=POLICY_VERSION):
    import io
    import hashlib
    atlases = []
    layout, width, height, x, y, row_h = [], 0, 0, 0, 0, 0
    for group in groups:
        for tile in group['tiles']:
            w, h = tile['image'].size
            if x and x+w > 3000:
                x = 0; y += row_h+24; row_h = 0
            if layout and (y+h > 3000 or max(width, x+w)*max(height, y+h) > 9_000_000):
                atlases.append((layout, width, height)); layout, width, height, x, y, row_h = [], 0, 0, 0, 0, 0
            tile['atlas_index'] = len(atlases)+1
            tile['atlas_pixels'] = [x, y, x+w, y+h]
            layout.append(tile); width = max(width, x+w); height = max(height, y+h)
            x += w+24; row_h = max(row_h, h)
    if layout:
        atlases.append((layout, width, height))
    paths, manifests = [], []
    for ai, (layout, width, height) in enumerate(atlases):
        image = Image.new('RGB', (width, height), 'white')
        for tile in layout:
            image.paste(tile['image'], tile['atlas_pixels'][:2])
            tile['atlas_size'] = [width, height]
        buffer = io.BytesIO(); image.save(buffer, 'PNG', compress_level=2); image.close()
        content = buffer.getvalue(); path = root / f'{index+1}-{ai+1}-{hashlib.sha256(content).hexdigest()[:12]}.png'
        atomic_write(path, content); paths.append(path)
        manifests.append({'image_index': ai+1, 'width': width, 'height': height, 'pixel_scale': 1,
                          'relative_path': path.relative_to(root.parents[2]).as_posix(),
                          'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content)})
    aliases, structures, targets = {}, [], []
    for gi, group in enumerate(groups):
        alias = f'q{gi+1}'; original = group['structure']; qid = original['id']
        aliases[alias] = qid
        value = {k: original[k] for k in ('number','section','part_id','kind','response_encoding','selection_rule','row','column') if original.get(k) is not None}
        value['id'] = alias
        owner = next(t for t in group['tiles'] if t['slot'] == original['slot'])
        value['slot'] = owner['atlas_index']
        value['answer_zone'] = _to_atlas(original['answer_zone'], owner)
        for field in ('options', 'nodes'):
            value[field] = []
            for node in original.get(field, []):
                tile = next(t for t in group['tiles'] if t['slot'] == node.get('slot', original['slot']))
                value[field].append({**{k: node[k] for k in ('id','group','label') if k in node},
                    'slot': tile['atlas_index'], 'box': _to_atlas(node['box'], tile)})
        structures.append(value)
        targets.append({**group['candidate'], 'alias': alias, 'kind': original.get('kind'), 'slot': original['slot'],
            'number': original.get('number'), 'section': original.get('section',''), 'part_id': original.get('part_id',''),
            'tiles': [{k: v for k, v in tile.items() if k != 'image'} for tile in group['tiles']],
            'crop_images': [m for m in manifests if m['image_index'] in {t['atlas_index'] for t in group['tiles']}]})
    public = {'policy_version': policy_version, 'images': [{k: m[k] for k in ('image_index','width','height','pixel_scale')} for m in manifests],
        'regions': [{'question_id': t['alias'], 'image_index': tile['atlas_index'],
                     'box': dict(zip(('x1','y1','x2','y2'), [tile['atlas_pixels'][0]/tile['atlas_size'][0],tile['atlas_pixels'][1]/tile['atlas_size'][1],tile['atlas_pixels'][2]/tile['atlas_size'][0],tile['atlas_pixels'][3]/tile['atlas_size'][1]]))}
                    for t in targets for tile in t['tiles']]}
    return {'paths': paths, 'variables': {'slot': 1, 'image_page_order': list(range(1,len(paths)+1)), 'structure': structures, 'crop_manifest': public},
        'manifest': {'policy_version': policy_version, 'aliases': aliases, 'targets': targets, 'images': manifests}}


def _to_atlas(box, tile):
    source = _project_box(box, tile['inverse'])
    sw, sh = tile['source_size']; aw, ah = tile['atlas_size']; px, py, _, _ = tile['pixels']; ax, ay, _, _ = tile['atlas_pixels']
    crop_box = {'x1': source['x1']*sw-px, 'y1': source['y1']*sh-py,
                'x2': source['x2']*sw-px, 'y2': source['y2']*sh-py}
    turned = _project_box(crop_box, tile['native_to_tile'])
    return {'x1': max(0,(turned['x1']+ax)/aw), 'y1': max(0,(turned['y1']+ay)/ah),
            'x2': min(1,(turned['x2']+ax)/aw), 'y2': min(1,(turned['y2']+ay)/ah)}


def _literal(cell, answer, source=None):
    kind = answer.get('kind'); encoding = answer.get('response_encoding')
    if kind == 'matching' or encoding == 'edges':
        edges = []
        directed = (answer.get('matching_rule') or {}).get('directed', False)
        for edge in cell.get('edges', []):
            if edge.get('erased'):
                continue
            if source and source not in (edge.get('from_node'),edge.get('to_node')):
                continue
            pair = [edge.get('from_node'), edge.get('to_node')]
            edges.append(pair if directed else sorted(pair, key=lambda v: str(v)))
        return sorted(edges, key=str)
    if kind in ('choice','letter_choice','marked_choice') or encoding in ('marked_choice','written_choice','mixed'):
        # Preserve response form as well as chosen IDs; form disagreement must
        # not be hidden by equal grading outcomes.
        return {'marked': sorted(set(cell.get('selected_options', []))),
                'written': sorted(set(cell.get('written_options', []))),
                'raw': comparable(cell.get('raw_english') or '')}
    raw = cell.get('symbol') if encoding == 'symbol' else cell.get('rank') if encoding == 'rank' else cell.get('raw_english')
    return comparable(raw or '')


def _agreement(first, second, answer, *, policy_version):
    if not first or not second:
        return False, []
    same = _literal(first, answer) == _literal(second, answer)
    choice = answer.get('kind') in ('choice','letter_choice','marked_choice') or answer.get('response_encoding') in ('marked_choice','written_choice','mixed')
    if policy_version == LEGACY_POLICY_VERSION or not choice:
        return same, []

    def channels(cell):
        values = {'marked':frozenset(comparable(v) for v in cell.get('selected_options', [])),
                  'written':frozenset(comparable(v) for v in cell.get('written_options', [])),
                  'raw':frozenset([comparable(cell['raw_english'])]) if cell.get('raw_english') else frozenset()}
        return {k:v for k,v in values.items() if v}

    left, right = channels(first), channels(second)
    # A contradictory nonempty raw channel must not be ignored merely because
    # the marked channel happened to match the answer key. No expected answer
    # is involved in this agreement comparison.
    if any(len(set(ch.values())) > 1 for ch in (left, right)):
        return False, []
    if same:
        return True, []
    clear = all(not c.get('uncertain') and c.get('visibility','visible')=='visible'
        and c.get('response_evidence')=='clear' and not c.get('transcription_uncertain')
        and not c.get('ambiguous_glyphs') and c.get('literal_evidence') not in ('ambiguous','not_visible')
        for c in (first,second))
    common = left.keys() & right.keys() & {'marked','written'}
    # Only tolerate a redundant agreeing channel. A pure circle-to-handwriting
    # substitution, changed letter, multiple marks or ambiguous evidence remains
    # pending. response_encoding describes a form; it is not a strict-form rule.
    values = set(left.values()) | set(right.values())
    redundant = clear and bool(common) and len(values)==1 and len(next(iter(values)))==1
    if redundant:
        return True, ['两轮明确作答相同；额外圈选或手写记录属于同选项的冗余通道，原始形式记录均已保留。']
    return False, []


def merge(reference, first_output, second_outputs, first_attempt_id=None, rejected=None, *, policy_version=POLICY_VERSION):
    from .scoring import matching_source_status
    output = copy.deepcopy(first_output)
    evidence = copy.deepcopy(first_output.get('verification', {}))
    answers = {a['id']: a for a in reference['items']}
    first = {a['id']: [c for c in first_output.get('cells', []) if c.get('question_id') == a['id']] for a in reference['items']}
    targets = {}
    for batch in second_outputs:
        manifest = batch['manifest']
        photo_issues = [str(value).strip() for value in batch.get('output', {}).get('photo_issues', []) if str(value).strip()]
        for target in manifest['targets']:
            qid = target['question_id']
            cells = [c for c in batch.get('output', {}).get('cells', []) if c.get('question_id') == target['alias']]
            second = copy.deepcopy(cells[0]) if len(cells) == 1 else None
            if second:
                second['question_id'] = qid
                second.update({k: target[k] for k in ('number','section','part_id','slot')})
                if 'value' in second:
                    value = second.pop('value')
                    second['symbol'] = value if target.get('kind') == 'true_false' else None
                    second['rank'] = value if target.get('kind') == 'ordering' else None
            targets[qid] = {**target, 'second': second, 'second_attempt_id': batch.get('attempt_id'),
                'second_photo_issues': photo_issues,
                'reason': '' if second else '独立复看未返回唯一有效的本题观察，需教师核实。'}
    for refusal in rejected or []:
        targets[refusal['question_id']] = {**refusal, 'second': None, 'second_attempt_id': refusal.get('attempt_id')}
    for qid, target in targets.items():
        answer = answers.get(qid)
        if not answer:
            continue
        a = first[qid][0] if len(first[qid]) == 1 else None
        b = target.get('second')
        ast, ad = grade_observation(a, answer)
        bst, bd = grade_observation(b, answer)
        differences = []
        same, form_differences = _agreement(a, b, answer, policy_version=policy_version)
        if a and b and not same:
            differences.append('两次识别的学生作答不一致')
        if bst == 'unknown':
            differences.append(bd.get('reason') or '独立复看存在不确定证据')
        if ast == 'unknown':
            differences.append(ad.get('reason') or '首次观察存在不确定证据')
        # Page-level quality findings can contradict a cell's optimistic clear
        # flag. Their free text cannot safely be used to exclude other tiles,
        # so an unresolved crop-quality issue requires review for this batch.
        photo_issues = target.get('second_photo_issues', [])
        if photo_issues:
            differences.append('独立复看报告图片问题：' + '；'.join(photo_issues))
        source_states = {}
        for source in target.get('source_ids', []):
            ar = {**answer, 'status': ast, 'grading_detail': ad}; br = {**answer, 'status': bst, 'grading_detail': bd}
            source_same = a and b and _literal(a,answer,source) == _literal(b,answer,source)
            source_states[source] = 'verified' if source_same and not photo_issues and matching_source_status(ar,source) == matching_source_status(br,source) == 'correct' else 'pending'
        verified = same and ast != 'unknown' and bst != 'unknown' and not differences
        if ast == 'correct':
            verified &= bst == 'correct'
        if source_states:
            verified &= all(v == 'verified' for v in source_states.values())
        reason = '' if verified else target.get('reason') or '；'.join(dict.fromkeys(differences)) or '复看未确认原有拟正确结论，需教师核实。'
        evidence[qid] = {'policy_version': policy_version, 'status': 'verified' if verified else 'pending',
            'reason': reason, 'differences': list(dict.fromkeys(differences)), 'source_states': source_states,
            'first': copy.deepcopy(a), 'second': b, 'first_attempt_id': first_attempt_id,
            'second_attempt_id': target.get('second_attempt_id'), 'crop_tiles': target.get('tiles', []),
            'crop_images': target.get('crop_images', []), 'second_photo_issues': photo_issues}
        if form_differences:
            evidence[qid]['form_differences'] = form_differences
    output['verification'] = evidence
    output['verification_policy'] = policy_version
    return output
