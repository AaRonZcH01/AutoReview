"""Deterministic scoring. Integer hundredths; unknown evidence never becomes zero."""
from decimal import Decimal, InvalidOperation
from .core import require, digest, uid, now, Problem


def minor(value):
    try:
        number = Decimal(str(value))
        require(number.is_finite() and number >= 0 and number * 100 == (number * 100).to_integral(),
                'SCORE_VALUE', '分值必须是非负数，最多两位小数。')
        return int(number * 100)
    except (InvalidOperation, ValueError, TypeError):
        require(False, 'SCORE_VALUE', '请填写有效分值，缺失分值不能当作零。')


def structure(items):
    # Drafts can omit defaults which are filled when an answer version is frozen.
    # Those represent the same structure and must not invalidate an already
    # published scoring rule merely because formal publication adds defaults.
    values=[]
    for a in items:
        rule=a.get('matching_rule')
        if rule:
            from .contracts import LocalMatchingRule
            rule=LocalMatchingRule.model_validate(rule).model_dump()
        values.append({'id':a.get('id'),'kind':a.get('kind','dictation'),'slot':a.get('slot'),
            'section':a.get('section',''),'number':a.get('number'),'part_id':a.get('part_id',''),
            'excluded':a.get('excluded',False),'matching_rule':rule})
    return digest(values)


def matching_sources(answer):
    """Use the explicit source group, or the first displayed group for a pair grid."""
    rule=answer.get('matching_rule') or {}; nodes=rule.get('nodes') or []
    group=rule.get('source_group') or (nodes[0].get('group') if nodes else None)
    return [n for n in nodes if n.get('group')==group] if group is not None else []


def matching_units(answer, whole):
    """Split a question maximum exactly, with visible, stable hundredth remainders."""
    nodes=matching_sources(answer)
    if not nodes: return [whole], None
    total=whole.get('max_points_minor')
    valid=isinstance(total,int) and not isinstance(total,bool) and total>=0
    share,remainder=divmod(total,len(nodes)) if valid else (None,0)
    base={k:v for k,v in whole.items() if k not in ('id','confirmed_hash','question_ids','source_id')}
    units=[{**base,'id':uid(),'rule':'matching_source','rule_selection':'default',
        'source_id':node['id'],'label':answer.get('section','')+' / '+str(answer['number'])+' · '+str(node.get('display_id') or node.get('label') or node['id']),
        'max_points_minor':share+(index<remainder) if valid else None,
        'matching_total_minor':total if valid else None,'matching_source_count':len(nodes),
        'matching_remainder_minor':remainder,'allocation_basis':'matching_source_equal'}
        for index,node in enumerate(nodes)]
    note=None
    if valid:
        note=f"{answer.get('section','')} / {answer['number']}：连线总分{total/100:g}分按{len(nodes)}个源端点分项计分，需逐项核对。"
        if remainder: note+=f' 最小精度尾差已按源端点显示顺序分配：前{remainder}项各{(share+1)/100:g}分，其余各{share/100:g}分，合计保持{total/100:g}分。'
        else: note+=f' 每个源端点{share/100:g}分，整组目标配对完全正确才得分。'
    return units,note


def seed(items, matching_default=True):
    units=[]
    for a in items:
        if a.get('excluded'): continue
        whole={'id':uid(),'question_id':a['id'],'label':a.get('section','')+' / '+str(a['number']),
            'rule':'all_correct','max_points_minor':None,'source':'manual','writing_error_policy':'incorrect'}
        if a.get('kind')=='matching' and matching_default:
            units.extend(matching_units(a,whole)[0])
        else: units.append(whole)
    return units


def candidate(scoring, items, attempt_id=None):
    """Expand observed section scores locally into reviewable, exact candidates.

    A section total is useful when it maps unambiguously to independent small
    questions. Never split an indivisible hundredth, guess a missing section,
    overwrite explicit item points, or infer a multiple-blank block maximum.
    A matching block's observed maximum is then divided across its declared
    source endpoints with exact hundredth allocation. Every amount is unconfirmed.
    """
    from collections import Counter
    # Section/item observations identify question maxima. Expand matching only
    # after validating those maxima so nested section totals stay comparable.
    units=seed(items,matching_default=False); by_question={u['question_id']:u for u in units}
    notes=list(scoring.get('notes_zh') or [])
    counts=Counter(c.get('question_index') for c in scoring.get('items',[]))
    for c in scoring.get('items',[]):
        idx=c.get('question_index',-1)
        if isinstance(idx,int) and 0<=idx<len(items) and items[idx]['id'] in by_question:
            u=by_question[items[idx]['id']]
            u.update(source='parsed',source_text=c.get('source_text',''))
            if c.get('points') is not None and not c.get('uncertain') and counts[idx]==1:
                try: u['max_points_minor']=minor(c['points'])
                except Problem: pass
    allocations=[]; sections=scoring.get('sections') or []
    path=lambda label:tuple(part for part in str(label or '').split('/') if part)
    def descendant(child,parent):
        return bool(parent) and len(child)>len(parent) and child[:len(parent)]==parent
    scopes=[]
    for s in sections:
        indices=s.get('question_indices') or []; p=path(s.get('section'))
        valid_indices=bool(indices) and all(isinstance(i,int) and not isinstance(i,bool) and 0<=i<len(items) for i in indices) and len(set(indices))==len(indices)
        targets=[items[i] for i in indices] if valid_indices else []
        expected={i for i,a in enumerate(items) if not a.get('excluded') and (path(a.get('section'))==p or descendant(path(a.get('section')),p)) and (s.get('slot') is None or a.get('slot')==s['slot'])}
        mapped=valid_indices and bool(p) and set(indices)==expected and all(a['id'] in by_question for a in targets)
        try: value=minor(s.get('total'))
        except Problem: value=None
        scopes.append({'path':p,'indices':set(indices) if valid_indices else set(),
            'mapped':mapped,'total':value,'targets':targets,
            'summary':any(descendant(path(a.get('section')),p) for a in targets)})
    def hierarchy(parent,child):
        a,b=scopes[parent],scopes[child]
        page_a,page_b=sections[parent].get('slot'),sections[child].get('slot')
        return a['mapped'] and b['mapped'] and descendant(b['path'],a['path']) and b['indices']<a['indices'] and (page_a is None or page_b is None or page_a==page_b)
    relations={(i,j) for i in range(len(sections)) for j in range(len(sections)) if i!=j and hierarchy(i,j)}
    overlap=set()
    for i,a in enumerate(scopes):
        for j,b in enumerate(scopes[:i]):
            if a['indices'] & b['indices'] and (i,j) not in relations and (j,i) not in relations:
                # Only a complete, correctly mapped path hierarchy is allowed.
                # Same-range duplicates and arbitrary intersections still fail.
                overlap.update((i,j))
    conflicts=set()
    for i,parent in enumerate(scopes):
        children=[j for a,j in relations if a==i and not any((i,k) in relations and (k,j) in relations for k in range(len(sections))) ]
        covered=set().union(*(scopes[j]['indices'] for j in children)) if children else set()
        if children and covered==parent['indices'] and parent['total'] is not None and not sections[i].get('uncertain') and all(scopes[j]['total'] is not None and not sections[j].get('uncertain') for j in children):
            if sum(scopes[j]['total'] for j in children)!=parent['total']:
                conflicts.add(i)
                conflicts.update(j for a,j in relations if a==i)
    # An invalid summary cannot escape by allowing its descendants to allocate.
    overlap.update(j for a,j in relations if a in overlap)
    # Older candidate records did not expose sections and remain readable.
    for index,s in enumerate(sections):
        scope=scopes[index]
        label=s.get('section') or '未注明大题'
        indices=s.get('question_indices') or []
        target=scope['targets']
        target_units=[by_question[a['id']] for a in target if a['id'] in by_question]
        why=''; total=scope['total']; summary=scope['summary']; explicit=False
        key={(a.get('slot'),a.get('section',''),a.get('number')) for a in target}
        if total is None:
            why='缺少有效大题总分'
        elif not indices or len(set(indices))!=len(indices) or len(target)!=len(indices) or len(target_units)!=len(indices):
            why='大题与题目对应不完整'
        elif not all(path(a.get('section'))==scope['path'] or descendant(path(a.get('section')),scope['path']) for a in target):
            why='大题对应包含其他分区'
        elif not scope['mapped']:
            why='大题总分未对应此范围内的全部小题'
        elif index in conflicts:
            why='父级汇总分与下级分值合计不一致'
        elif index in overlap or any(counts[i]>1 for i in indices):
            why='大题或小题分值出现重复候选'
        elif summary:
            # A parent total may validate child totals, but never allocates
            # evenly across differently weighted subsections.
            notes.append(f'{label}：汇总{total/100:g}分，仅核对下级合计，不跨小节均分。')
        elif all(u['max_points_minor'] is not None for u in target_units) and sum(u['max_points_minor'] for u in target_units)==total:
            explicit=True
            notes.append(f'{label}：{total/100:g}分已对应明确的小题分值，仍需教师核对。')
        elif len(key)!=len(target) or any(a.get('kind')=='matching' for a in target) and len(target)!=1:
            why='含多空或连线块，须明确计分单位后赋分'
        elif s.get('item_count') is not None and s['item_count']!=len(target) and not (len(target)==1 and target[0].get('kind')=='matching' and s['item_count']==len(matching_sources(target[0]))):
            why='印刷题数与结构化题数不一致'
        elif s.get('uncertain') or s.get('allocation_basis')=='unknown':
            why='大题分配依据不明确'
        elif total is not None and total % len(target):
            why='均分后超过两位小数，请手动分配尾差'
        if not why and not summary and not explicit:
            point=total//len(target)
            # Explicit per-question observations take precedence. A conflict
            # must remain visible rather than silently replacing either rule.
            unit_conflicts=[u for u in target_units if u['max_points_minor'] is not None and u['max_points_minor']!=point]
            if unit_conflicts: why='大题均分与已解析的小题分值冲突'
            else:
                for u in target_units:
                    if u['max_points_minor'] is None:
                        u.update(max_points_minor=point,source='section_equal',source_text=s.get('source_text',''),
                            allocation_basis=s.get('allocation_basis') or 'structured_units')
                notes.append(f'{label}：{total/100:g}分已按{len(target_units)}个独立小题均分，每题{point/100:g}分，需教师核对。')
        if why: notes.append(f'{label}：{why}，保留大题总分供手动赋分。')
        allocations.append({**s,'total_minor':total,'allocated':not bool(why) and not summary,'summary':summary,'message':why})
    # Partial child summaries may not cover the full parent range. When all
    # actual unit points are available, compare those too before deriving total.
    for i,s in enumerate(scopes):
        target_units=[by_question[a['id']] for a in s['targets'] if a['id'] in by_question]
        if s['summary'] and s['mapped'] and s['total'] is not None and not sections[i].get('uncertain') and target_units and all(u['max_points_minor'] is not None for u in target_units) and sum(u['max_points_minor'] for u in target_units)!=s['total']:
            conflicts.add(i)
            for u in target_units:
                if u.get('source')=='section_equal': u['max_points_minor']=None
            for j,a in enumerate(allocations):
                if j==i or (i,j) in relations:
                    a.update(allocated=False,message='父级汇总分与下级分值合计不一致')
            notes.append(f"{sections[i].get('section') or '未注明大题'}：父级汇总分与题目分值合计不一致，自动均分候选已撤回，请核对来源规则。")
    # A clear top-level maximum does not require every small-question amount
    # to be known. Use each complete outer scope once, never parent + child.
    # Unknown allocation basis only prevents distribution, not reading a total.
    total_conflicts=set(conflicts)
    for i,s in enumerate(scopes):
        if not s['mapped'] or s['total'] is None or sections[i].get('uncertain'): continue
        children=[j for a,j in relations if a==i and not any((i,k) in relations and (k,j) in relations for k in range(len(sections)))
            and scopes[j]['total'] is not None and not sections[j].get('uncertain')]
        covered=set().union(*(scopes[j]['indices'] for j in children)) if children else set()
        observed=sum(scopes[j]['total'] for j in children)
        for idx in s['indices']-covered:
            point=by_question[items[idx]['id']]['max_points_minor']
            if point is not None:
                covered.add(idx); observed+=point
        # Even an incomplete set of known lower-level amounts cannot exceed
        # its parent. Complete coverage must agree exactly, including leaves.
        if observed>s['total'] or covered==s['indices'] and observed!=s['total']:
            total_conflicts.add(i)
        if '冲突' in allocations[i]['message']: total_conflicts.add(i)
    top=[i for i in range(len(scopes)) if not any(child==i for _,child in relations)]
    required={i for i,a in enumerate(items) if not a.get('excluded')}
    coverage=set().union(*(scopes[i]['indices'] for i in top)) if top else set()
    clean_sections=not total_conflicts and not overlap and all(s['mapped'] for s in scopes) and not any(counts[i]>1 for i in required)
    section_total=None
    if (top and required and clean_sections and coverage==required
            and sum(len(scopes[i]['indices']) for i in top)==len(required)
            and all(scopes[i]['total'] is not None and not sections[i].get('uncertain') for i in top)):
        section_total=sum(scopes[i]['total'] for i in top)
    known_total=sum(u['max_points_minor'] for u in units if u['max_points_minor'] is not None)
    complete_units=bool(units) and all(u['max_points_minor'] is not None for u in units)
    total=None
    if scoring.get('total') is not None:
        try: total=minor(scoring['total'])
        except Problem: pass
        if total is not None and (total_conflicts or overlap or known_total>total
                or complete_units and known_total!=total
                or section_total is not None and section_total!=total):
            notes.append('明确识别的试卷总分已保留，但与分区或小题分值存在冲突，请核对来源规则；未自动确认。')
    elif section_total is not None:
        total=section_total
        notes.append(f'未见独立印刷总分；按完整覆盖全卷且互不重叠的顶层分区合计生成试卷总分{total/100:g}分，需教师核对。缺失的小题分值仍需单独填写。')
    elif clean_sections and complete_units:
        total=known_total
        notes.append('未见独立印刷总分；试卷总分按已解析题目分值合计生成，需教师核对。')
    elif sections:
        reason='分区或小题分值存在冲突、重叠或对应不完整' if not clean_sections else '顶层分区未完整覆盖全卷或分区总分尚不明确'
        notes.append(f'试卷总分未自动汇总：{reason}，请核对或手动填写总分。')
    expanded=[]
    answers={a['id']:a for a in items}
    for u in units:
        a=answers[u['question_id']]
        if a.get('kind')=='matching':
            parts,note=matching_units(a,u); expanded.extend(parts)
            if note: notes.append(note)
        else: expanded.append(u)
    return {'units':expanded,'declared_total_minor':total,'structure_hash':structure(items),
        'notes':notes,'section_candidates':allocations,'parse_attempt_id':attempt_id}


def unit_hash(unit):
    return digest({k: v for k, v in unit.items() if k != 'confirmed_hash'})


def policy_signature(policy):
    return digest([policy.get('structure_hash'),policy.get('declared_total_minor'),
        [(u.get('id'),unit_hash(u)) for u in policy.get('units',[])]])


def normalize_writing_defaults(draft):
    """Normalize editable legacy drafts, never immutable published policies.

    Earlier drafts omitted this field while the editor now displays deduction.
    Make the draft's actual rule explicit and require confirmation again; an
    explicitly selected 'ignore' remains an intentional teacher decision.
    """
    changed=False; units=[]
    for u in draft.get('units',[]):
        if u.get('writing_error_policy') is None:
            units.append({**u,'writing_error_policy':'incorrect','confirmed_hash':None})
            changed=True
        else: units.append(u)
    return {**draft,'units':units,'total_confirmed_hash':None} if changed else draft


def validate(draft, items, receipts=True):
    problems, ids, grouped = [], set(), {}
    answers = {a['id']: a for a in items if not a.get('excluded')}
    for u in draft.get('units', []):
        reason = ''
        if not u.get('id') or u['id'] in ids:
            reason = '计分单元标识重复或缺失'
        ids.add(u.get('id'))
        a = answers.get(u.get('question_id'))
        if not a:
            reason = '计分单元对应的题目已删除或排除'
        members=u.get('question_ids') or [u.get('question_id')]
        if len(set(members)) != len(members) or u.get('question_id') not in members or any(q not in answers for q in members):
            reason='整小题计分包含重复或不存在的答案项'
        if len(members)>1 and u.get('rule')!='all_correct':
            reason='多空合并仅支持整小题全对计分'
        if len(members)>1 and len({(answers[q].get('slot'),answers[q].get('section'),answers[q].get('number')) for q in members if q in answers})!=1:
            reason='仅可合并同页同大题同小题的多个空位'
        for q in members: grouped.setdefault(q, []).append(u)
        if not isinstance(u.get('max_points_minor'), int) or isinstance(u.get('max_points_minor'), bool) or u['max_points_minor'] < 0:
            reason = '缺少有效分值，请手动赋分'
        if u.get('rule') not in ('all_correct','matching_source','sequence_position','sentence_component'):
            reason = '请选择明确的计分规则'
        if u.get('writing_error_policy') not in (None,'ignore','incorrect'):
            reason = '请选择明确的书写错误扣分方式'
        if a and u.get('rule') == 'matching_source':
            nodes = (a.get('matching_rule') or {}).get('nodes', [])
            if a['kind'] != 'matching' or u.get('source_id') not in {n['id'] for n in nodes}:
                reason = '请选择此连线题中的源端点'
            elif u.get('rule_selection') in ('default','teacher') and u.get('source_id') not in {n['id'] for n in matching_sources(a)}:
                # New editable rules cannot accidentally score the target group.
                # Unmarked, frozen legacy units retain their original contract.
                reason = '计分源端点不属于此题的源端点组，请重新选择'
        if a and u.get('rule') == 'sentence_component' and (a['kind'] != 'sentence' or u.get('component') not in ('word_inventory_equal','word_order_equal','case_equal','punctuation_equal')):
            reason = '请选择句子的字词、词序、大小写或标点分项'
        if a and u.get('rule') == 'sequence_position' and (a['kind'] != 'ordering' or not isinstance(u.get('position'), int) or u['position'] < 0):
            reason = '请选择排序题的位置（从0计数）'
        if reason:
            problems.append({'unit_id':u.get('id'),'question_id':u.get('question_id'),'message':reason})
        elif receipts and u.get('confirmed_hash') != unit_hash(u):
            problems.append({'code':'SCORE_UNREVIEWED','unit_id':u['id'],'question_id':u['question_id'],'message':'分值或规则尚未核对'})
    for q, a in answers.items():
        units = grouped.get(q, [])
        if not units:
            problems.append({'question_id':q,'message':'本题尚未赋分'})
        elif len(units) > 1 and any(u['rule'] == 'all_correct' for u in units):
            problems.append({'question_id':q,'message':'整题与子单元不能重复计分'})
        elif len({(u['rule'],u.get('source_id'),u.get('component'),u.get('position')) for u in units}) != len(units):
            problems.append({'question_id':q,'message':'同一计分对象出现多次'})
        if units and all(u['rule']=='matching_source' for u in units):
            rule=a.get('matching_rule') or {}; expected=rule.get('allowed_edges', [])
            assigned={u.get('source_id') for u in units}
            if not expected or any(sum(n in assigned for n in edge)!=1 for edge in expected):
                problems.append({'question_id':q,'message':'每条正确配对应恰好属于一个源端点计分单元'})
            if any(u.get('rule_selection') in ('default','teacher') for u in units) and assigned!={n['id'] for n in matching_sources(a)}:
                problems.append({'question_id':q,'message':'连线题须为源端点组的每个端点分别赋分，不得漏项或计入目标端点'})
        if units and all(u['rule']=='sequence_position' for u in units):
            import re
            count=len([x for x in re.split(r'[,，;；\s]+',a.get('english','').strip()) if x])
            if {u.get('position') for u in units} != set(range(count)):
                problems.append({'question_id':q,'message':'排序题须为每个实际位置分别赋分，不得漏项或增加不存在的位置'})
    total = draft.get('declared_total_minor')
    amount = sum(u.get('max_points_minor') or 0 for u in draft.get('units', []) if isinstance(u.get('max_points_minor'),int))
    if not isinstance(total,int) or isinstance(total,bool) or total < 0:
        problems.append({'code':'SCORE_TOTAL_MISSING','field':'declared_total_minor','message':'请填写有效的试卷总分，或载入自动解析的分值候选。'})
    elif total != amount:
        problems.append({'code':'SCORE_TOTAL_MISMATCH','field':'declared_total_minor','message':f'试卷总分为{total/100:g}分，各题合计为{amount/100:g}分，请核对。'})
    if draft.get('structure_hash') != structure(items):
        problems.append({'message':'题目结构已改变，请重新对应计分单元'})
    if receipts and draft.get('total_confirmed_hash') != digest([total, [(u['id'],unit_hash(u)) for u in draft.get('units',[])]]):
        problems.append({'code':'SCORE_TOTAL_UNREVIEWED','field':'declared_total_minor','message':'试卷总分尚未核对，请点击“确认试卷总分”。'})
    return problems


def inspection(draft, items):
    problems=validate(draft,items)
    invalid=validate(draft,items,False)
    checked={u.get('id'):bool(u.get('confirmed_hash')==unit_hash(u)) for u in draft.get('units',[])}
    invalid_questions={p['question_id'] for p in invalid if p.get('question_id')}
    ordered=[a for a in sorted(items,key=lambda a:(a['slot'],items.index(a))) if not a.get('excluded')]
    pending=[]
    for a in ordered:
        units=[u for u in draft.get('units',[]) if a['id'] in (u.get('question_ids') or [u.get('question_id')])]
        if not units or a['id'] in invalid_questions or not all(checked.get(u.get('id'),False) for u in units): pending.append(a['id'])
    return {'issues':problems,'invalid_issues':invalid,'unit_reviewed':checked,
            'pending_question_ids':pending,'question_count':len(ordered),
            'total_reviewed':not any(p.get('field')=='declared_total_minor' for p in problems)}


def check(service, task_id, data):
    """Validate unsaved editor values without changing drafts or receipts."""
    with service.store.connection() as db:
        service.require_available(db,'tasks',task_id)
        task=service.store.get(db,'tasks',task_id)
        ref=service.store.get(db,'reference_drafts',task_id)
        old=service.store.get(db,'scoring_drafts',task_id,optional=True) or {}
        published=service.store.get(db,'scoring_versions',task.get('scoring_version_id') or '',optional=True)
    draft=normalize_writing_defaults({**old,**{k:data[k] for k in ('units','declared_total_minor','structure_hash','total_confirmed_hash') if k in data}})
    items=data.get('items',ref['items'])
    current=published if published and published.get('structure_hash')==structure(items) and not validate(published,items) and not validate(draft,items) and policy_signature(draft)==policy_signature(published) else None
    return {**inspection(draft,items),'ready_to_confirm':bool(current),'current_version_id':current['id'] if current else None}


def matching_source_status(a, source, local=True):
    verification = {} if a.get('verification_override') else a.get('verification') or {}
    if verification.get('status') != 'verified' and verification and not verification.get('source_states'):
        return 'unknown'
    if source in verification.get('source_states', {}) and verification['source_states'][source] != 'verified':
        return 'unknown'
    detail=a.get('grading_detail') or {}
    relevant = lambda key: {tuple(e) for e in detail.get(key, []) if source in e}
    unknown = detail.get('unknown_edges', [])
    
    source_ids={n['id'] for n in matching_sources(a)}
    def affects_source(edge):
        if not local:
            return not edge.get('from_node') or not edge.get('to_node') or source in (edge.get('from_node'),edge.get('to_node'))
        # An unreadable target attached to a known source affects
        # that source, not every independently observed correct pair.
        # Without any reliable source identity, keep all units unknown.
        possible=source_ids & {edge.get('from_node'),edge.get('to_node'),*(edge.get('endpoint_candidates') or [])}
        return source in possible if possible else True
    if local and (relevant('incorrect_edges') or relevant('extra_edges')):
        return 'incorrect'
    elif relevant('unresolved_expected_edges') or any(affects_source(e) for e in unknown):
        return 'unknown'
    elif relevant('incorrect_edges') or relevant('extra_edges') or relevant('missing_edges'):
        return 'incorrect'
    elif relevant('correct_edges'):
        # A source with several targets earns one unit only when
        # its complete expected set is observed, never for a single
        # correct edge accompanied by absent or surplus targets.
        rule=a.get('matching_rule') or {}
        expected={tuple(e) if rule.get('directed') else tuple(sorted(e))
                  for e in rule.get('allowed_edges',[]) if source in e}
        return 'correct' if not expected or relevant('correct_edges')==expected else 'incorrect'
    return 'unknown'


def evaluate(policy, results, reviewed=False, photo_pending=False):
    """Recompute from current observations/teacher decisions, never from error boxes."""
    by_id = {a['id']: a for a in results}
    units, earned, pending = [], 0, 0
    for u in policy['units']:
        a = by_id.get(u['question_id']); status = 'unknown'
        if len(u.get('question_ids',[]))>1:
            members=[by_id.get(q) for q in u['question_ids']]
            values=[(v or {}).get('status','unknown') for v in members]
            a={**(a or {}),'status':'unknown' if 'unknown' in values else 'incorrect' if any(v in ('incorrect','missing') for v in values) else 'correct',
               'writing_status':'pending' if any((v or {}).get('writing_status')=='pending' for v in members) else 'confirmed_error' if any((v or {}).get('writing_status')=='confirmed_error' for v in members) else 'clear'}
        override = (a or {}).get('score_unit_status', {}).get(u['id'])
        if a and not photo_pending and override:
            status = override
        elif a and not photo_pending and a.get('writing_status') != 'pending' and (a.get('status') != 'unknown'
                or u.get('rule')=='matching_source' and (u.get('rule_selection') in ('default','teacher') or a.get('score_unit_status'))
                and a.get('grading_detail') and not a.get('manual_status')):
            if u['rule'] == 'all_correct':
                status = a['status']
            elif a.get('manual_status'):
                # A parent-only override cannot identify which subunits are wrong.
                status = 'unknown'
            elif u['rule'] == 'matching_source':
                status=matching_source_status(a,u['source_id'],u.get('rule_selection') in ('default','teacher'))
            elif u['rule'] == 'sentence_component':
                value = a.get('grading_detail', {}).get(u.get('component'))
                status = 'correct' if value is True else 'incorrect' if value is False else 'unknown'
            elif u['rule'] == 'sequence_position':
                import re
                expected = re.split(r'[,，;；\s]+', a.get('english','').strip())
                actual = re.split(r'[,，;；\s]+', (a.get('raw_english') or '').strip())
                i = u['position']
                status = ('correct' if i < len(actual) and actual[i] == expected[i] else 'incorrect') if i < len(expected) else 'unknown'
            if status == 'correct' and not a.get('verification_override') and a.get('verification') and a['verification'].get('status') != 'verified' and u['rule'] != 'matching_source':
                status = 'unknown'
        maximum = u['max_points_minor']
        if a and not photo_pending and a.get('writing_status')=='confirmed_error' and u.get('writing_error_policy')=='incorrect':
            status='incorrect'
        points = maximum if status == 'correct' else 0 if status in ('incorrect','missing') else None
        earned += points or 0
        pending += maximum if points is None else 0
        units.append({'id':u['id'],'question_id':u['question_id'],'max_points_minor':maximum,'earned_minor':points,'status':status})
    total = policy['declared_total_minor']
    unresolved = any(u['earned_minor'] is None for u in units)
    return {'units':units,'total_minor':total,'earned_minor':earned,'pending_minor':pending,
            'deducted_minor':total-earned-pending, 'range_minor':[earned,earned+pending],
            'final_minor':earned if reviewed and not unresolved else None,
            'status':'final' if reviewed and not unresolved else 'pending_review'}


def gate(store, db, task_id):
    task = store.get(db,'tasks',task_id)
    if task.get('scoring_enabled'):
        version = store.get(db,'scoring_versions',task.get('scoring_version_id') or '',optional=True)
        draft=store.get(db,'scoring_drafts',task_id,optional=True)
        ref=store.get(db,'reference_drafts',task_id)
        require(version and version.get('reference_version_id') == task.get('reference_version_id') and draft
                and not validate(draft,ref['items']) and policy_signature(draft)==policy_signature(version),
                'SCORING_REQUIRED','请先完成当前正式答案的总分与每题分值核对，并发布计分规则。')


def prepare_reference_publication(service, db, task, items):
    """Validate the previously published score rule before freezing answers.

    Called inside confirm_reference's transaction. The teacher publishes scoring
    against the editable answer structure first, so there is no circular gate.
    """
    if not task.get('scoring_enabled'):
        return None
    version=service.store.get(db,'scoring_versions',task.get('scoring_version_id') or '',optional=True)
    require(version and version.get('structure_hash')==structure(items), 'SCORING_REQUIRED',
            '自动计分已开启，请先确认本页分值、核对试卷总分并发布当前题目结构的计分规则。')
    draft=service.store.get(db,'scoring_drafts',task['id'],optional=True)
    if draft: draft=normalize_writing_defaults(draft)
    require(draft and policy_signature(draft)==policy_signature(version),'SCORING_REQUIRED',
            '分值草稿已修改，请先重新确认并发布计分规则，再确认答案。')
    problems=validate(version,items)+validate(draft,items)
    require(not problems,'SCORING_REQUIRED','已发布计分规则与当前题目不一致，请修正并重新发布计分规则。',issues=problems)
    return version


def bind_reference_publication(service, db, task, reference):
    """Return the immutable policy bound to the newly confirmed answer version."""
    current=prepare_reference_publication(service,db,task,reference['items'])
    if current is None: return task.get('scoring_version_id')
    if current.get('reference_version_id')==reference['id']: return current['id']
    version=service.store.put(db,'scoring_versions',{**{k:v for k,v in current.items() if k not in ('revision','created_at','updated_at')},
        'id':uid(),'reference_version_id':reference['id'],'bound_from_id':current['id'],
        'version':service.store.sequence(db,'scoring:'+task['id']),'confirmed_at':now()})
    draft=service.store.get(db,'scoring_drafts',task['id'],optional=True)
    if draft and draft.get('structure_hash')==current['structure_hash']:
        service.store.put(db,'scoring_drafts',{**draft,'reference_version_id':reference['id']})
    return version['id']


def recompute(service, db, submission, pages):
    store=service.store; task=store.get(db,'tasks',submission['task_id'])
    version_id=submission.get('scoring_version_id')
    if not version_id:
        return {}
    policy=store.get(db,'scoring_versions',version_id)
    results=[a for p in pages if p.get('reference_version_id')==policy['reference_version_id'] for a in service.results(db,p)]
    value=evaluate(policy,results,submission['state']=='reviewed',any(p.get('photo_status') in ('pending','retake_required') for p in pages))
    signature=digest([version_id,[(p['id'],p['revision']) for p in pages],submission['state']])
    prior=store.get(db,'score_snapshots',submission.get('score_snapshot_id') or '',optional=True)
    if prior and prior.get('result_revision')==signature:
        return {'score_snapshot_id':prior['id'],'scoring_version_id':version_id}
    snapshot=store.put(db,'score_snapshots',{'parent_id':submission['id'],'task_id':submission['task_id'],
        'group_id':submission['group_id'],'scoring_version_id':version_id,'reference_version_id':policy['reference_version_id'],
        'review_revisions':{p['id']:p['revision'] for p in pages},'result_revision':signature,**value})
    return {'score_snapshot_id':snapshot['id'],'scoring_version_id':version_id}


def manage(service, task_id, data=None):
    store=service.store
    with store.connection(write=data is not None) as db:
        task=store.get(db,'tasks',task_id)
        ref=store.get(db,'reference_drafts',task_id)
        old=store.get(db,'scoring_drafts',task_id,optional=True)
        draft=old or {'id':task_id,'parent_id':task_id,'task_id':task_id,'revision':0,'units':seed(ref['items']),
                     'declared_total_minor':None,'structure_hash':structure(ref['items']),'reference_version_id':task.get('reference_version_id')}
        draft=normalize_writing_defaults(draft)
        if data is None:
            published=store.get(db,'scoring_versions',task.get('scoring_version_id') or '',optional=True)
            current=published if published and published.get('structure_hash')==structure(ref['items']) and not validate(published,ref['items']) and not validate(draft,ref['items']) and policy_signature(draft)==policy_signature(published) else None
            return {'enabled':bool(task.get('scoring_enabled')),'draft':draft,**inspection(draft,ref['items']),
                    'versions':store.list(db,'scoring_versions',task_id=task_id),'current_version_id':current['id'] if current else None,
                    'published_version_id':task.get('scoring_version_id'),'latest_published_version_id':max(store.list(db,'scoring_versions',task_id=task_id),key=lambda v:v.get('version',0),default={}).get('id'),'ready_to_confirm':bool(current)}
        service.require_available(db,'tasks',task_id)
        action=data.get('action','save')
        if action=='toggle':
            store.put(db,'tasks',{**task,'scoring_enabled':bool(data.get('enabled'))})
            return {'enabled':bool(data.get('enabled'))}
        require(data.get('revision')==draft['revision'],'REVISION_CONFLICT','赋分草稿已变化，请刷新。',409)
        if action=='sync':
            # The editor may have explicitly selected whole-question scoring
            # immediately before syncing. Honor that current draft selection.
            for k in ('units','declared_total_minor'):
                if k in data: draft[k]=data[k]
            valid={a['id'] for a in ref['items'] if not a.get('excluded')}
            retained=[u for u in draft['units'] if set(u.get('question_ids') or [u['question_id']])<=valid]
            published=store.get(db,'scoring_versions',task.get('scoring_version_id') or '',optional=True)
            protected={u['id']:unit_hash(u) for u in (published or {}).get('units',[])}
            answers={a['id']:a for a in ref['items']}; expanded=[]; notes=list(draft.get('notes') or [])
            for u in retained:
                a=answers[u['question_id']]
                is_default=(a.get('kind')=='matching' and u.get('rule')=='all_correct'
                    and u.get('rule_selection')!='teacher' and not u.get('question_ids')
                    and sum(v.get('question_id')==a['id'] for v in retained)==1
                    and protected.get(u['id'])!=unit_hash(u))
                if is_default:
                    parts,note=matching_units(a,u); expanded.extend(parts)
                    if note and note not in notes: notes.append(note)
                else: expanded.append(u)
            retained=expanded
            existing={q for u in retained for q in (u.get('question_ids') or [u['question_id']])}
            draft={**draft,'units':retained+[u for u in seed(ref['items']) if u['question_id'] not in existing],
                   'notes':notes,'structure_hash':structure(ref['items']),'reference_version_id':task.get('reference_version_id'),'total_confirmed_hash':None}
        elif action in ('save','confirm','publish','revoke','unpublish'):
            for k in ('units','declared_total_minor'):
                if k in data: draft[k]=data[k]
            if 'total_confirmed_hash' in data and data['total_confirmed_hash'] is None: draft['total_confirmed_hash']=None
            if data.get('adopt_candidate'):
                candidate=draft.get('machine_candidate')
                require(candidate and candidate.get('structure_hash')==structure(ref['items']), 'SCORE_CANDIDATE', '赋分候选与当前题目结构不同，请重新解析或手动赋分。')
                draft={**draft,**candidate,'machine_candidate':None,'total_confirmed_hash':None}
            draft=normalize_writing_defaults(draft)
            if data.get('total_confirmed_hash') is not None:
                expected=digest([draft.get('declared_total_minor'),[(u['id'],unit_hash(u)) for u in draft.get('units',[])]])
                require(data['total_confirmed_hash']==expected,'SCORE_CONFIRMATION','总分核对凭据与当前分值内容不一致，请重新核对。',409)
                # Editor undo can restore a previously returned receipt, but a
                # stale receipt cannot authorize a changed amount or unit rule.
                draft['total_confirmed_hash']=data['total_confirmed_hash']
            official=store.get(db,'reference_versions',task.get('reference_version_id') or '',optional=True)
            draft['reference_version_id']=official['id'] if official and structure(official['items'])==structure(ref['items']) else None
            if action=='unpublish':
                require(data.get('confirm'),'CONFIRM_REQUIRED','撤回规则后不能开始新批改，已有作业的规则和成绩保留，请确认。')
                store.put(db,'tasks',{**task,'scoring_version_id':None})
            if action=='revoke':
                selected=set(data.get('unit_ids',[]))
                if data.get('page_slot') is not None:
                    page_questions={a['id'] for a in ref['items'] if a['slot']==data['page_slot']}
                    selected={u['id'] for u in draft['units'] if set(u.get('question_ids') or [u.get('question_id')]) <= page_questions}
                if not data.get('total_only'):
                    require(selected,'SCORE_UNIT','请选择需要取消核对的题目或页面。')
                    require(selected <= {u['id'] for u in draft['units']},'SCORE_UNIT','题目分值已变化，请刷新。')
                    draft['units']=[{**u,'confirmed_hash':None} if u['id'] in selected else u for u in draft['units']]
                draft['total_confirmed_hash']=None
            if action=='confirm':
                problems=validate(draft,ref['items'],False)
                if data.get('total_only'):
                    pending=inspection(draft,ref['items'])['pending_question_ids']
                    total_problem=next((p for p in problems if p.get('field')=='declared_total_minor'),None)
                    require(not problems,'SCORE_INVALID',(total_problem or problems[0])['message'] if problems else '',issues=problems)
                    require(not pending,'SCORE_UNREVIEWED',f'请先完成所有题目分值核对，尚有{len(pending)}题未核对。',question_ids=pending)
                    draft['total_confirmed_hash']=digest([draft['declared_total_minor'],[(u['id'],unit_hash(u)) for u in draft['units']]])
                selected=set(data.get('unit_ids',[]))
                if data.get('page_slot') is not None:
                    slot=data['page_slot']
                    require(isinstance(slot,int) and not isinstance(slot,bool) and slot>=1,'SCORE_PAGE','请选择有效页码。')
                    page_questions={a['id'] for a in ref['items'] if a.get('slot')==slot and not a.get('excluded')}
                    page_units={u['id'] for u in draft['units'] if set(u.get('question_ids') or [u.get('question_id')]) <= page_questions}
                    require(page_units,'SCORE_PAGE_EMPTY','本页没有需要确认的计分单元。')
                    if selected: require(selected <= page_units,'SCORE_PAGE','所选分值不全属于本页。')
                    else: selected=page_units
                if selected:
                    require(selected <= {u['id'] for u in draft['units']},'SCORE_UNIT','请选择当前规则中的计分单元。')
                    questions={u['question_id'] for u in draft['units'] if u['id'] in selected}
                    problems=[p for p in problems if p.get('unit_id') in selected or p.get('question_id') in questions and not p.get('unit_id') or p.get('message')=='题目结构已改变，请重新对应计分单元']
                require(not problems,'SCORE_INVALID',problems[0]['message'] if problems else '',issues=problems)
                if not data.get('total_only'):
                    draft['units']=[{**u,'confirmed_hash':unit_hash(u)} if not selected or u['id'] in selected else u for u in draft['units']]
                if not data.get('total_only') and (not selected or data.get('confirm_total')):
                    require(not validate(draft,ref['items'],False),'SCORE_INVALID','确认总分前请完成所有题目的有效赋分。')
                    draft['total_confirmed_hash']=digest([draft['declared_total_minor'],[(u['id'],unit_hash(u)) for u in draft['units']]])
            if action=='publish':
                problems=validate(draft,ref['items'])
                require(not problems,'SCORE_INVALID',f'分值尚未全部确认，请完成所有题目分值及试卷总分核对后再发布。{problems[0]["message"]}' if problems else '',issues=problems)
                scope=data.get('scope','future')
                require(scope in ('future','selected','all'),'SCORE_SCOPE','请选择重算范围。')
                if scope!='future':
                    official=store.get(db,'reference_versions',task.get('reference_version_id') or '',optional=True)
                    require(official and structure(official['items'])==draft['structure_hash'],'SCORE_REFERENCE',
                            '当前规则尚未绑定正式答案，请先选择仅后续批改使用，确认答案后再重算已有作业。')
                affected=[s for s in store.list(db,'submissions',task_id=task_id) if scope=='all' or scope=='selected' and s['id'] in data.get('submission_ids',[])]
                active_pages={w.get('page_id') for w in store.list(db,'work_items',task_id=task_id) if w['state'] in ('pending','queued','running')}
                busy_submissions=[s['id'] for s in affected if any(p['id'] in active_pages for p in store.list(db,'pages',parent_id=s['id']))]
                impact={'draft_revision':draft['revision'],'scope':scope,'submissions':[(s['id'],s['revision']) for s in affected], 'hash':digest(draft)}
                if data.get('preview',True):
                    changes=[]
                    for sub in affected:
                        pages=store.list(db,'pages',parent_id=sub['id'],include_deleted=False)
                        compatible=all(p.get('reference_version_id')==draft['reference_version_id'] for p in pages)
                        prior=store.get(db,'score_snapshots',sub.get('score_snapshot_id') or '',optional=True)
                        after=evaluate(draft,[a for p in pages for a in service.results(db,p)],sub['state']=='reviewed',any(p.get('photo_status') in ('pending','retake_required') for p in pages)) if compatible else None
                        changes.append({'id':sub['id'],'paper_tag':sub['paper_tag'],'old_final_minor':(prior or {}).get('final_minor'),'new_final_minor':(after or {}).get('final_minor'),'compatible':compatible})
                    return {**impact,'changes':changes,'busy_submissions':busy_submissions,'fingerprint':digest(impact)}
                require(data.get('fingerprint')==digest(impact),'SCORE_CHANGED','重算范围或规则已变化，请重新预览。',409)
                require(not busy_submissions,'SCORE_BUSY','选择的作业正在处理，请等待完成或选择仅后续使用，避免改变冻结规则。',409)
                version=store.put(db,'scoring_versions',{**{k:v for k,v in draft.items() if k not in ('revision','created_at','updated_at')},
                    'id':uid(),'state':'confirmed','version':store.sequence(db,'scoring:'+task_id),'confirmed_at':now()})
                store.put(db,'tasks',{**task,'scoring_version_id':version['id']})
                for sub in affected:
                    pages=store.list(db,'pages',parent_id=sub['id'],include_deleted=False)
                    require(all(p.get('reference_version_id')==version['reference_version_id'] for p in pages),'SCORE_REFERENCE','重算包含其他答案版本的作业，请缩小范围。')
                    store.put(db,'submissions',{**sub,'scoring_version_id':version['id']})
                    service.update_submission(db,sub['id'])
        elif action=='restore_published':
            versions=store.list(db,'scoring_versions',task_id=task_id)
            current=store.get(db,'scoring_versions',task.get('scoring_version_id') or '',optional=True)
            current=current or max(versions,key=lambda v:v.get('version',0),default=None)
            require(current,'SCORE_NO_PUBLISHED','尚无已发布的计分规则，不能回退。')
            require(current.get('reference_version_id')==task.get('reference_version_id') and current.get('structure_hash')==structure(ref['items']),
                    'SCORE_REFERENCE','已发布规则与当前答案结构不兼容。请恢复对应答案版本或手动核对，不可直接回退。')
            if data.get('preview',True):
                return {'published_version_id':current['id'],'version':current.get('version'),
                        'reference_version_id':current.get('reference_version_id'),'total_minor':current.get('declared_total_minor'),
                        'draft_revision':draft['revision'],'unit_count':len(current['units'])}
            require(data.get('confirm') is True and data.get('published_version_id')==current['id'],
                    'SCORE_CONFIRM','请确认放弃未发布修改，并恢复当前已发布规则。')
            restored={k:v for k,v in current.items() if k not in ('id','revision','created_at','updated_at','state','version','confirmed_at','bound_from_id')}
            draft={**restored,'id':task_id,'parent_id':task_id,'task_id':task_id,'revision':draft['revision']}
            store.put(db,'tasks',{**task,'scoring_version_id':current['id']})
            store.event(db,'scoring.restored_published',task,scoring_version_id=current['id'])
        else:
            require(False,'SCORING_ACTION','不支持此计分操作。')
        saved=store.put(db,'scoring_drafts',draft)
        return {'draft':saved,**inspection(saved,ref['items'])}
