"""CPU-only geometry and local text direction. No cloud calls or answer-dependent matching."""
from __future__ import annotations
import time
from functools import lru_cache
from pathlib import Path
import cv2
import numpy as np
from PIL import Image
from .core import require

VERSION = 'geometry-5.1'


def rotate_exact(image, geometry, angle):
    """Rotate saved pixels without interpolation; map normalized image boundaries."""
    started = time.perf_counter()
    require(angle in (-90, 90, 180), 'ROTATION_RANGE', '快捷旋转仅支持90度或180度。')
    methods = {-90: Image.Transpose.ROTATE_270, 90: Image.Transpose.ROTATE_90,
               180: Image.Transpose.ROTATE_180}
    steps = {-90: [[0,-1,1],[1,0,0],[0,0,1]],
              90: [[0,1,0],[-1,0,1],[0,0,1]],
             180: [[-1,0,1],[0,-1,1],[0,0,1]]}
    result = image.transpose(methods[angle])
    forward = np.array(steps[angle], dtype=np.float64) @ np.array(geometry['forward'])
    return result, {**geometry, 'forward': forward.tolist(), 'inverse': np.linalg.inv(forward).tolist(),
        'size': list(result.size), 'elapsed_ms': round((time.perf_counter()-started)*1000), 'algorithm': VERSION}


def corners(box):
    return [[box['x1'], box['y1']], [box['x2'], box['y1']], [box['x2'], box['y2']], [box['x1'], box['y2']]]


def project(points, matrix):
    points = np.array(points, dtype=np.float64)
    homogeneous = np.column_stack([points, np.ones(len(points))]) @ np.array(matrix).T
    require(np.all(np.abs(homogeneous[:, 2]) > 1e-8), 'GEOMETRY_INVALID', '坐标映射无效。')
    return (homogeneous[:, :2] / homogeneous[:, 2:]).tolist()


def auto_operations(image, *, enabled=True):
    """Default processing is only a supported, pixel-preserving quarter turn."""
    from .orientation import detect_orientation
    result = detect_orientation(image, enabled=enabled)
    operations = []
    if result['status'] == 'completed' and result['angle']:
        operations.append({'kind': 'rotate', 'angle': result['angle'],
                           'source': 'automatic', 'pixel_preserving': True})
    return operations, [result]


def render(image, operations):
    started = time.perf_counter()
    if not operations:
        identity = np.eye(3).tolist()
        result = image.copy()
        return result, {'forward': identity, 'inverse': np.eye(3).tolist(),
            'source_size': list(image.size), 'size': list(image.size),
            'elapsed_ms': round((time.perf_counter()-started)*1000), 'algorithm': VERSION}
    # Newly requested quarter turns are explicitly staged. Legacy operations keep
    # their original composed-warp semantics, including historical display pixels.
    exact = next((i for i, o in enumerate(operations) if o.get('pixel_preserving')), None)
    if exact is not None:
        operation = operations[exact]
        require(operation['kind'] == 'rotate', 'IMAGE_OPERATION', '无效的快捷旋转步骤。')
        before, geometry = render(image, operations[:exact])
        result, geometry = rotate_exact(before, geometry, operation['angle'])
        if operations[exact+1:]:
            result, tail = render(result, operations[exact+1:])
            forward = np.array(tail['forward']) @ np.array(geometry['forward'])
            geometry = {**tail, 'source_size': geometry['source_size'], 'forward': forward.tolist(),
                        'inverse': np.linalg.inv(forward).tolist()}
        return result, {**geometry, 'elapsed_ms': round((time.perf_counter()-started)*1000)}
    width, height = image.size
    matrix = np.eye(3)
    w, h = width, height
    for operation in operations:
        kind = operation['kind']
        if kind == 'rotate':
            angle = float(operation['angle'])
            require(abs(angle) <= 360, 'ROTATION_RANGE', '旋转角度超出范围。')
            affine = cv2.getRotationMatrix2D((w/2, h/2), angle, 1)
            c, s = abs(affine[0, 0]), abs(affine[0, 1])
            nw, nh = max(1, int(np.ceil(h*s+w*c-1e-8))), max(1, int(np.ceil(h*c+w*s-1e-8)))
            affine[0, 2] += (nw-w)/2
            affine[1, 2] += (nh-h)/2
            step = np.vstack([affine, [0, 0, 1]])
        elif kind in ('perspective', 'perspective_keep', 'crop'):
            pts = corners(operation['box']) if kind == 'crop' else operation.get('points')
            require(len(pts) == 4 and all(0 <= v <= 1 for p in pts for v in p), 'CORNERS_INVALID', '请按左上、右上、右下、左下指定四角。')
            src = np.array(pts, np.float32) * [w, h]
            require(cv2.isContourConvex(src.astype(np.float32)) and cv2.contourArea(src.astype(np.float32)) > max(64, w*h*(.0001 if kind == 'crop' else .02)),
                    'CORNERS_INVALID', '四角必须组成足够大的凸四边形。')
            nw = max(1, round(max(np.linalg.norm(src[1]-src[0]), np.linalg.norm(src[2]-src[3]))))
            nh = max(1, round(max(np.linalg.norm(src[3]-src[0]), np.linalg.norm(src[2]-src[1]))))
            step = cv2.getPerspectiveTransform(src.astype(np.float32), np.array([[0,0],[nw,0],[nw,nh],[0,nh]],np.float32))
            if kind == 'perspective_keep':
                frame = np.array(project([[0,0],[w,0],[w,h],[0,h]], step))
                left, top = np.floor(frame.min(axis=0))
                right, bottom = np.ceil(frame.max(axis=0))
                step = np.array([[1,0,-left],[0,1,-top],[0,0,1]]) @ step
                nw, nh = int(right-left), int(bottom-top)
        else:
            raise ValueError('未知图片操作')
        matrix = step @ matrix
        w, h = nw, nh
        require(w*h <= 80_000_000, 'IMAGE_TOO_LARGE', '校正后的图片过大。')
    result = cv2.warpPerspective(np.array(image), matrix, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT, borderValue=(255,255,255))
    normalized = np.diag([1/w, 1/h, 1]) @ matrix @ np.diag([width, height, 1])
    return Image.fromarray(result), {'forward': normalized.tolist(), 'inverse': np.linalg.inv(normalized).tolist(),
        'source_size': [width,height], 'size': [w,h], 'elapsed_ms': round((time.perf_counter()-started)*1000), 'algorithm': VERSION}


@lru_cache(maxsize=8)
def _landmarks(path, file_size, modified_ns, excluded):
    """Bounded, reconstructible feature cache; revision/file identity is in the key."""
    image = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    require(image is not None, 'IMAGE_INVALID', '定位图片无法读取。')
    scale = min(1,1600/max(image.shape))
    image = cv2.resize(image,(round(image.shape[1]*scale),round(image.shape[0]*scale)))
    mask = None
    if excluded:
        mask = np.full(image.shape,255,np.uint8)
        h,w = mask.shape
        for x1,y1,x2,y2 in excluded:
            cv2.rectangle(mask,(int(x1*w),int(y1*h)),(int(x2*w),int(y2*h)),0,-1)
    points, descriptors = cv2.SIFT_create(nfeatures=8000).detectAndCompute(image,mask)
    return image.shape, points, descriptors


def align(template_path, student_path, regions):
    def features(path, excluded):
        path = Path(path)
        state = path.stat()
        return _landmarks(str(path.resolve()),state.st_size,state.st_mtime_ns,excluded)
    excluded = tuple(tuple(box[k] for k in ('x1','y1','x2','y2')) for box in regions if box)
    shape_a,ka,da = features(template_path,excluded)
    shape_b,kb,db = features(student_path,())
    fallback = {'matrix': np.eye(3).tolist(), 'status': 'needs_review', 'reason': '印刷结构不足，当前显示比例映射；请逐题检查或手调位置。', 'algorithm': VERSION}
    if da is None or db is None:
        return fallback
    matches = cv2.BFMatcher(cv2.NORM_L2).knnMatch(da, db, k=2)
    good = [pair[0] for pair in matches if len(pair)==2 and pair[0].distance < .7*pair[1].distance]
    if len(good) < 16:
        return fallback
    a = np.float32([ka[m.queryIdx].pt for m in good])
    b = np.float32([kb[m.trainIdx].pt for m in good])
    transform, inliers = cv2.findHomography(a,b,cv2.RANSAC,3)
    if transform is None or inliers.sum() < 30 or inliers.mean() < .30:
        return fallback
    # Require landmarks spread across the page, not one repeated text fragment.
    covered = cv2.contourArea(cv2.convexHull(a[inliers.reshape(-1).astype(bool)])) / (shape_a[0]*shape_a[1])
    if covered < .25:
        return fallback
    fit_error = np.linalg.norm(cv2.perspectiveTransform(a.reshape(-1,1,2), transform).reshape(-1,2)-b,axis=1)[inliers.ravel().astype(bool)]
    if np.median(fit_error) > 2 or len(set((int(p[0]>shape_a[1]/2),int(p[1]>shape_a[0]/2)) for p in a[inliers.ravel().astype(bool)])) < 3:
        return fallback
    matrix = np.diag([1/shape_b[1],1/shape_b[0],1]) @ transform @ np.diag([shape_a[1],shape_a[0],1])
    projected = project([[0,0],[1,0],[1,1],[0,1]],matrix)
    if any(v < -.2 or v > 1.2 for p in projected for v in p):
        return fallback
    return {'matrix': matrix.tolist(), 'status': 'aligned', 'inliers': int(inliers.sum()), 'coverage': round(covered,3), 'median_error_px': round(float(np.median(fit_error)),3),
            'reason': '以答题区以外的印刷特征配准；仍需教师确认。', 'algorithm': VERSION}
