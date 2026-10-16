"""Visual marks use observed geometry; matching judgments stay source-specific."""
from .scoring import matching_sources, matching_source_status


def marks(answer, policy=None):
    if answer.get('kind')!='matching':
        status=answer.get('status','unknown')
        if answer.get('writing_status')=='confirmed_error':status='incorrect'
        elif status=='correct' and answer.get('writing_status')=='pending':status='unknown'
        return [] if status=='correct' or not answer.get('box') else [
            {'id':answer['id'],'status':status,'box':answer['box'],'label':str(answer['number'])}]
    nodes={n['id']:n for n in (answer.get('observed_structure') or {}).get('nodes',[])}
    units=(policy or {}).get('units',[]);overrides=answer.get('score_unit_status') or {}
    output=[]
    for source in matching_sources(answer):
        identifier=source['id'];node=nodes.get(identifier) or {};box=node.get('box')
        # Reference endpoint coordinates cannot locate a student's actual photo.
        if not box:continue
        unit=next((u for u in units if u.get('question_id')==answer['id'] and u.get('rule')=='matching_source' and u.get('source_id')==identifier),{})
        status=overrides.get(unit.get('id')) or matching_source_status(answer,identifier)
        if answer.get('manual_status') and not overrides:
            status='correct' if answer.get('status')=='correct' else 'unknown'
        if answer.get('writing_status')=='confirmed_error':status='incorrect'
        elif answer.get('writing_status')=='pending' and status=='correct':status='unknown'
        if status!='correct':output.append({'id':answer['id']+':'+identifier,'source_id':identifier,'status':status,'box':box,'label':str(node.get('label') or source.get('display_id') or identifier)})
    return sorted(output,key=lambda mark:(mark['box']['y1'],mark['box']['x1']))


def apply(results, policy=None):
    for answer in results:
        answer['annotation_marks']=marks(answer,policy)
        if answer.get('kind')=='matching' and not (answer.get('observed_structure') or {}).get('nodes'):
            answer['annotation_note']='缺少此页端点位置，无法准确圈出错误端点，请检查整题或补充位置。'
    return results
