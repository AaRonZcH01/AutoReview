"""Freeze settings at submission; edits only affect later submissions."""
from .core import DEFAULT_BATCH_PARALLELISM, DEFAULT_CAPTURE_PARALLELISM, require

MAX_PARALLELISM = 13

def validate_settings(changes):
    if 'capture_countdown_seconds' in changes:
        seconds = changes['capture_countdown_seconds']
        require(type(seconds) is int and 0 <= seconds <= 60, 'CAPTURE_COUNTDOWN_INVALID', '拍摄间隔请输入0至60的整数秒。')
    for key in ('parallelism', 'capture_parallelism'):
        if key in changes:
            n = changes[key]
            require(type(n) is int and 1 <= n <= MAX_PARALLELISM, 'PARALLELISM_INVALID', f'并行度请选择1至{MAX_PARALLELISM}。')
    for key in ('conservative_review', 'auto_orient_images', 'cli_check_on_startup'):
        if key in changes:
            require(type(changes[key]) is bool, 'SETTING_INVALID', '开关值无效。')
    if 'verification_effort' in changes:
        require(changes['verification_effort'] in ('low','medium','high'), 'EFFORT_UNAVAILABLE', '复看强度请选择低、中或高。')

def freeze(settings, data=None, purpose='student'):
    data = data or {}
    result = {'profile_id': settings.get('active_profile_id'),
        'model': data.get('model') or settings['reference_model' if purpose == 'reference' else 'model'],
        'effort': data.get('effort') or settings['reference_effort' if purpose == 'reference' else 'effort'],
        'speed': data.get('speed', settings['speed']),
        'parallelism': settings.get('capture_parallelism', DEFAULT_CAPTURE_PARALLELISM) if purpose == 'capture' else settings.get('parallelism', DEFAULT_BATCH_PARALLELISM) if purpose == 'student' else 1,
        'conservative_review': settings.get('conservative_review', False) if purpose in ('student', 'capture') else False,
        'verification_effort': settings.get('verification_effort', 'medium')}
    validate_settings(result)
    return result
