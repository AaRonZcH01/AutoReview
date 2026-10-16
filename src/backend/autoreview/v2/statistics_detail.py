"""Human-readable, version-bound question summaries from final observations."""
KINDS={'dictation':'听写','copying':'抄写','choice':'选择题','letter_choice':'选择题','marked_choice':'选择题',
       'fill_blank':'填空题','matching':'连线题','open_response':'开放作答','true_false':'判断题','ordering':'排序题','sentence':'连词成句'}


def text(value):
    if isinstance(value,(list,tuple)):return ' → '.join(str(x) for x in value)
    return '' if value is None else str(value)


def expected(answer):
    spec=answer.get('answer_spec') or {}
    value=answer.get('english')
    if value in ('',None):
        value=next((spec[k] for k in ('rank','sequence','order','expected','accepted','symbol') if spec.get(k) not in ('',None,[])),None)
    if answer.get('kind')=='matching':
        rule=answer.get('matching_rule') or spec
        labels={n['id']:n.get('label') or n.get('display_id') or n['id'] for n in rule.get('nodes',[])}
        pairs=rule.get('allowed_edges',[])
        if pairs:return '；'.join(f'{labels.get(a,a)} → {labels.get(b,b)}' for a,b in pairs)
    return text(value) or '该历史版本未填写参考答案'


def _box(boxes):
    valid=[b for b in boxes if isinstance(b,dict) and all(isinstance(b.get(k),(int,float)) for k in ('x1','y1','x2','y2'))
           and b['x2']>b['x1'] and b['y2']>b['y1']]
    if not valid:return None
    return {'x1':max(0,min(b['x1'] for b in valid)-.02),'y1':max(0,min(b['y1'] for b in valid)-.025),
            'x2':min(1,max(b['x2'] for b in valid)+.02),'y2':min(1,max(b['y2'] for b in valid)+.025)}


def info(service,db,reference,answer,result,page,version_name):
    slot=answer['slot'];section=answer.get('section') or '未分组'
    label=f"第{slot}页 · {section} · 第{answer['number']}题"+(f" · 第{answer['part_id']}空" if answer.get('part_id') else '')
    same=[a for a in reference['items'] if not a.get('excluded') and
          (a['slot'],a.get('section',''),str(a['number']),a.get('part_id',''))==
          (slot,answer.get('section',''),str(answer['number']),answer.get('part_id',''))]
    if len(same)>1:label+=f" · 第{next(i for i,a in enumerate(same,1) if a['id']==answer['id'])}处"
    assets=reference.get('asset_ids',[])
    asset_id=assets[slot-1] if len(assets)>=slot else None
    boxes=[answer.get('answer_zone'),answer.get('box')]
    boxes.extend(n.get('box') for n in answer.get('options',[]))
    boxes.extend(n.get('box') for n in (answer.get('matching_rule') or {}).get('nodes',[]) if n.get('slot',slot)==slot)
    location=_box(boxes)
    source='教师范本'
    if not asset_id or not location:
        version=service.store.get(db,'image_versions',page['image_version_id'])
        asset_id=version['asset_id'];structure=result.get('observed_structure') or {}
        location=_box([structure.get('question_zone'),structure.get('answer_zone'),result.get('box'),
                       *(n.get('box') for n in structure.get('options',[])),*(n.get('box') for n in structure.get('nodes',[]))])
        source='已复核作业'
    value=expected(answer)
    return {'label':label,'kind':answer.get('kind','dictation'),'kind_label':KINDS.get(answer.get('kind'),'题目'),
        'section':answer.get('section',''),'part_id':answer.get('part_id',''),'expected_display':value,
        'reference_version_name':version_name,'locator':{'asset_id':asset_id,'box':location,'page_label':f'{source} · 第{slot}页','source':source}}


def wrong_answers(answer, policy=None):
    if answer['status'] not in ('incorrect','missing'):return []
    if answer['status']=='missing':return ['未作答']
    obs=answer.get('observation') or {};kind=answer.get('kind');encoding=answer.get('response_encoding')
    raw=answer.get('raw_english')
    if kind=='matching' or encoding=='edges':
        from .scoring import matching_sources,matching_source_status
        rule=answer.get('matching_rule') or answer.get('answer_spec') or {}
        labels={n['id']:n.get('label') or n.get('display_id') or n['id'] for n in rule.get('nodes',[])}
        errors=[]
        for source in matching_sources(answer):
            sid=source['id']
            marks=[m for m in answer.get('annotation_marks',[]) if m.get('source_id')==sid]
            status=marks[0]['status'] if marks else matching_source_status(answer,sid)
            # Per-source teacher judgment wins. Do not count already-correct
            # pairs as errors merely because another pair was wrong.
            if answer.get('score_unit_status'):
                unit_states=[answer['score_unit_status'][u['id']] for u in (policy or {}).get('units',[])
                    if u.get('question_id')==answer['id'] and u.get('source_id')==sid and u['id'] in answer['score_unit_status']]
                if unit_states:status=unit_states[0]
            if status not in ('incorrect','missing'):continue
            targets=[]
            for edge in obs.get('edges',[]):
                if edge.get('erased') or edge.get('uncertain'):continue
                a,b=edge.get('from_node'),edge.get('to_node')
                if a==sid:targets.append(b)
                elif b==sid and not rule.get('directed'):targets.append(a)
            target='、'.join(labels.get(t,t) for t in sorted(set(targets))) if targets else '未连线'
            errors.append(f"端点{labels.get(sid,sid)} → {target}")
        return errors or ['教师判错；具体连线请查看该题']
    if answer.get('teacher_transcription') is not None:
        observed=text(raw)
    elif encoding=='rank' or kind=='ordering':observed=text(obs.get('rank') if obs.get('rank') is not None else raw)
    elif encoding=='symbol' or kind=='true_false':observed=text(obs.get('symbol') if obs.get('symbol') is not None else raw)
    elif kind in ('choice','letter_choice','marked_choice') or encoding in ('written_choice','marked_choice','mixed'):
        observed='、'.join(sorted(set(obs.get('selected_options') or obs.get('written_options') or []))) or text(raw)
    else:observed=text(raw)
    if not observed:return ['未记录可读作答（教师判错）']
    if kind=='ordering':return [f'填为序号 {observed}（应为 {expected(answer)}）']
    if kind in ('choice','letter_choice','marked_choice'):return ['选 '+observed]
    if kind=='true_false':return ['填 '+observed]
    return [observed]
