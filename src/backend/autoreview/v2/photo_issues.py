"""Explain known photo diagnostics in Chinese, using frozen observer aliases only."""
import re


def describe(issues, results, structure):
    output=[]
    by_id={r['id']:r for r in results}
    for text in issues:
        raw=str(text);lower=raw.lower();ids=[]
        for n in re.findall(r'\bq(\d+)\b',raw,re.I):
            index=int(n)-1
            if 0<=index<len(structure) and structure[index].get('id') in by_id:
                ids.append(structure[index]['id'])
        if re.search(r'faint|light|low.contrast',lower):reason='笔迹较淡或对比度不足，请查看原图确认是否能判读。'
        elif re.search(r'blur|focus|motion',lower):reason='图片模糊或失焦，笔画细节可能无法判读。'
        elif re.search(r'glare|reflection|overexpos',lower):reason='反光或过曝可能遮住笔迹，请核对受影响区域。'
        elif re.search(r'crop|cut.off|missing|out.of.frame',lower):reason='图片可能缺少部分作答区域，请检查是否拍摄完整。'
        elif re.search(r'cover|occlud|obscur|shadow|dark',lower):reason='遮挡、阴影或画面过暗可能影响判读。'
        elif re.search(r'rotat|upside|orient',lower):reason='图片方向可能不正确，请检查并旋转后重新定位。'
        elif re.search(r'[\u4e00-\u9fff]',raw):reason=raw
        else:reason='模型报告照片可能影响判读；未提供可可靠翻译的具体原因，请检查原图。'
        boxes=[by_id[i]['box'] for i in dict.fromkeys(ids) if by_id[i].get('box')]
        output.append({'message':reason,'question_ids':list(dict.fromkeys(ids)),'boxes':boxes,
                       'original':raw,'located':bool(boxes)})
    return output
