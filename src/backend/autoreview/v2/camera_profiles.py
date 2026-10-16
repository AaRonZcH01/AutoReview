"""Machine-local camera controls; no image or model processing."""
import math
from .core import require,now

MODES={'focusMode','exposureMode','whiteBalanceMode'}
NUMBERS={'focusDistance','exposureTime','brightness','contrast','sharpness','colorTemperature','saturation','exposureCompensation'}

def update(settings,data):
    label=data.get('camera_label')
    require(isinstance(label,str) and 0<len(label.strip())<=200,'CAMERA_PROFILE','请指定当前摄像头名称。')
    controls=data.get('controls',{})
    settle=data.get('settle_frames',0)
    require(type(settle) is int and settle in (0,1),'CAMERA_PROFILE','稳定帧数只能为0或1。')
    require(isinstance(controls,dict) and set(controls)<=MODES|NUMBERS,'CAMERA_PROFILE','相机参数格式不正确。')
    for key,value in controls.items():
        if key in MODES:
            require(value in ('continuous','manual'),'CAMERA_PROFILE','不支持此相机模式。')
        else:
            require(type(value) in (int,float) and -1000000<=value<=1000000 and math.isfinite(value),
                    'CAMERA_PROFILE','相机参数必须为设备范围内的有限数值。')
    require('focusDistance' not in controls or controls.get('focusMode')=='manual','CAMERA_PROFILE','手动对焦需同时选择手动模式。')
    require('exposureTime' not in controls or controls.get('exposureMode')=='manual','CAMERA_PROFILE','曝光时间需同时选择手动模式。')
    with settings._lock:
        profiles=dict(settings.data.get('camera_profiles',{}))
        if data.get('reset'):
            profiles.pop(label,None)
        else:
            require(label in profiles or len(profiles)<24,'CAMERA_PROFILE','已保存的设备过多，请先移除不用的配置。')
            profiles[label]={'controls':controls,'settle_frames':settle,'saved_at':now()}
        settings.save(camera_profiles=profiles)
        return {'camera_label':label,'profile':profiles.get(label),'profiles':profiles}
