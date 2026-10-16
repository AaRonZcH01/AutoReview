"""Server-owned reference review receipts, bound to content and page identity."""
from .core import digest, now, require
from .contracts import Answer


def required_slots(item, draft=None):
    pages = (draft or {}).get('page_ids') or (draft or {}).get('asset_ids', [])
    option_slots = {pages.index(o['page_id'])+1 for o in item.get('options', []) if o.get('page_id') in pages}
    return {item.get('slot', 1), *option_slots, *item.get('dependencies', []),
            *(n.get('slot', 1) for n in (item.get('matching_rule') or {}).get('nodes', []))}


def fingerprint(draft, item, legacy=False):
    try:
        normalized = Answer.model_validate(item).model_dump()
    except Exception:
        normalized = item
    value = {k: v for k, v in normalized.items() if k not in ('geometry_checked','option_group_id')}
    value['options'] = [{k:v for k,v in o.items() if k not in ('page_id','region_id')} for o in value.get('options', [])]
    slots = required_slots(item, draft)
    slots.update(n.get('slot', 1) for n in (item.get('matching_rule') or {}).get('nodes', []))
    # Page assets, rather than array length, invalidate only dependent items.
    value['page_assets'] = {s: draft.get('asset_ids', [])[s-1] if 0 < s <= len(draft.get('asset_ids', [])) else None for s in slots}
    value['writing_rules'] = draft.get('writing_rules', [])
    if not legacy:
        pages = draft.get('page_ids') or draft.get('asset_ids', [])
        value['option_pages'] = [(o['id'], o.get('page_id') or (pages[item['slot']-1] if pages else None)) for o in item.get('options', [])]
        def identity(s): return pages[s-1] if isinstance(s,int) and 0 < s <= len(pages) else s
        value['slot'] = identity(item.get('slot',1))
        value['dependencies'] = sorted((identity(s) for s in item.get('dependencies',[])), key=str)
        value['page_assets'] = {identity(s):asset for s,asset in value['page_assets'].items()}
        if value.get('matching_rule'):
            value['matching_rule'] = {**value['matching_rule'], 'nodes':[{**n,'slot':identity(n['slot'])} for n in value['matching_rule'].get('nodes',[])]}


    return digest(value)


def issues(draft, include_review=True):
    output, keys, ids, zones = [], {}, {}, {}
    shared_options={}
    def add(a, code, message, field='answer_zone', target=None, related=None):
        slot = a.get('slot', 1)
        pages = draft.get('page_ids') or draft.get('asset_ids', [])
        output.append({'id': digest([a.get('id'), slot, code, field])[:20],
            'item_id': a.get('id'), 'slot': slot, 'page_id': pages[slot-1] if isinstance(slot,int) and 0 < slot <= len(pages) else None,
            'code': code, 'message': message, 'field_path': field, 'region_target': target,
            'related_item_ids': related or [], 'severity':'error', 'fix_hint':message,
            'draft_revision':draft.get('revision'), 'scope':'item' if a.get('id') else 'page' if a.get('slot') else 'task', 'action':'fix_field' if a.get('id') else 'view_page' if a.get('slot') else 'upload'})
    if not draft.get('items') and draft.get('validation_context') != 'editing':
        add({}, 'REFERENCE_EMPTY', '尚未填写参考答案。')
    if draft.get('asset_ids') and (draft.get('items') or draft.get('parse_attempt_id') or draft.get('validation_context') != 'editing'):
        occupied = {s for a in draft.get('items', []) for s in required_slots(a, draft)}
        for slot, asset in enumerate(draft['asset_ids'], 1):
            if slot not in occupied and draft.get('non_answer_pages', {}).get(str(slot)) != asset:
                add({'slot': slot}, 'PAGE_UNCHECKED', '本页没有答案，请检查是否漏题，或明确标记为无作答内容页。')
    for a in draft.get('items', []):
        group=a.get('option_group_id')
        if group:
            option_stamp=digest(a.get('options',[]))
            if group in shared_options and shared_options[group]!=option_stamp:
                add(a,'SHARED_OPTIONS_CONFLICT','共享选项组内容不一致，请重新选择共享来源或解除关联。','options')
            shared_options[group]=option_stamp
        key = (a.get('slot'), a.get('section', '').strip(), str(a.get('number', '')).strip(), str(a.get('part_id', '')).strip())
        if key in keys:
            add(a, 'REFERENCE_DUPLICATE', '同大题第'+str(a.get('number'))+'题空位编号重复，请修改题号/空位或删除重复答案。', 'number', related=[keys[key].get('id')])
            add(keys[key], 'REFERENCE_DUPLICATE', '同页、同大题的小题与空位编号重复，请修改小题或空位编号。', 'number', related=[a.get('id')])
        keys[key] = a
        if a.get('id') in ids:
            add(a, 'REFERENCE_DUPLICATE_ID', '答案内部标识重复。')
        ids[a.get('id')] = a
        try:
            Answer.model_validate(a)
        except Exception as error:
            errors = error.errors() if hasattr(error, 'errors') else []
            for detail in errors[:12] or [{'loc':[], 'type':'invalid'}]:
                path = '.'.join(map(str, detail['loc']))
                add(a, 'REFERENCE_SCHEMA', '字段 '+(path or '答案')+' 格式或区域边界无效，请修正。', path)
            continue
        if not 1 <= a.get('slot', 0) <= draft.get('page_count', 1):
            add(a, 'REFERENCE_SLOT', '答案页码超出范本页数。')
        if not str(a.get('number', '')).strip():
            add(a, 'REFERENCE_NUMBER', '缺少小题编号。')
        if a.get('excluded'):
            if not a.get('exclusion_reason', '').strip():
                add(a, 'EXCLUSION_REASON', '排除题目需填写原因。', 'exclusion_reason')
            continue
        zone = a.get('answer_zone')
        if zone:
            zone_key = (a.get('slot'), *(round(zone.get(k, 0), 4) for k in ('x1','y1','x2','y2')))
            if zone_key in zones:
                add(a, 'DUPLICATE_REGION', '不同答案使用完全相同的答题区，请区分各空位；共享选项区应放在选项位置中。')
                add(zones[zone_key], 'DUPLICATE_REGION', '此答题区与另一答案完全重合，请核对。')
            zones[zone_key] = a
        forms = {'dictation':('text',),'copying':('text',),'choice':('written_choice','marked_choice','mixed'),'fill_blank':('text','written_choice'),'true_false':('symbol','text'),'ordering':('rank','text'),'matching':('edges',),'sentence':('text',)}
        if a.get('kind') in forms and a.get('response_encoding','text') not in (*forms[a['kind']], 'unknown'):
            add(a, 'RESPONSE_COMBINATION', '此题型不支持所选作答形式，请重新选择。', 'response_encoding')
        if a.get('response_encoding') == 'unknown':
            add(a, 'RESPONSE_ENCODING', '作答形式仍待确认，请选择文字、字母、圈选或对应形式。', 'response_encoding')
        if a.get('uncertain'):
            add(a, 'REFERENCE_UNCERTAIN', '答案仍不确定，请修正或明确排除。', 'uncertain')
        if draft.get('asset_ids') and not a.get('answer_zone'):
            add(a, 'REFERENCE_GEOMETRY', '缺少完整答题区域，请点击“重画完整作答区”，在原图框出整个作答区。', 'answer_zone')
        if any(not 1 <= s <= draft['page_count'] for s in a.get('dependencies', [])):
            add(a, 'DEPENDENCY_SLOT', '跨页依赖页码无效。')
        if a.get('kind') == 'matching':
            rule = a.get('matching_rule') or {}
            nodes = rule.get('nodes', [])
            if len(nodes) < 2 or not rule.get('allowed_edges'):
                add(a, 'MATCHING_INCOMPLETE', '连线题缺少端点或正确配对。')
            if any(not n.get('box') or n.get('slot') not in [a['slot'], *a.get('dependencies', [])] for n in nodes):
                add(a, 'MATCHING_GEOMETRY', '连线端点缺少区域或跨页依赖。')
        elif not a.get('english', '').strip():
            add(a, 'REFERENCE_BLANK', '缺少参考答案。', 'english')
        if a.get('kind') in ('choice','letter_choice','marked_choice') or a.get('response_encoding') in ('marked_choice','written_choice','mixed'):
            opts = a.get('options', [])
            accepted = [s.strip() for s in a.get('english', '').split(',') if s.strip()]
            if len(opts) < 2:
                add(a, 'OPTIONS_INCOMPLETE', '选项不足两个，请在选项位置中补齐实际印刷选项。', 'options')
            seen_options = set()
            for o in opts:
                field = 'options['+o['id']+'].box'
                if o['id'] in seen_options:
                    add(a, 'OPTIONS_INCOMPLETE', o['id']+' 选项编号重复，请修改选项编号。', 'options')
                seen_options.add(o['id'])
                if draft.get('asset_ids') and not o.get('box'):
                    add(a, 'OPTIONS_INCOMPLETE', o['id']+' 选项缺少位置，点击“添加区域”后在图片框出该选项。', field, {'kind':'option','id':o['id']})
                pages = draft.get('page_ids') or draft.get('asset_ids', [])
                if o.get('page_id') and o['page_id'] not in pages:
                    add(a, 'DEPENDENCY_SLOT', o['id']+' 选项所关联页面不存在，请重新选择页面。', field)
            if not accepted or not set(accepted) <= {o['id'] for o in opts}:
                add(a, 'CHOICE_ANSWER', '参考答案须为实际选项标识，如 B，而非选项整句。', 'english')
            if a.get('selection_rule', 'single') == 'unknown' or (a.get('selection_rule', 'single') == 'single' and len(accepted) != 1):
                add(a, 'CHOICE_CARDINALITY', '请确认单选或多选及对应答案数量。', 'selection_rule')
        if include_review and draft.get('asset_ids') and not confirmed(draft, a):
            add(a, 'REVIEW_REQUIRED', '答案或位置尚待核对，可逐项或确认本页。')
    return output


def confirmed(draft, item):
    receipt = draft.get('review_receipts', {}).get(item.get('id'), {})
    return required_slots(item, draft) <= reviewed_slots(draft, item)


def reviewed_slots(draft, item):
    receipt = draft.get('review_receipts', {}).get(item.get('id'), {})
    expected = fingerprint(draft, item, legacy=receipt.get('stamp_version', 1) == 1)
    if receipt.get('fingerprint') != expected: return set()
    pages = draft.get('page_ids') or draft.get('asset_ids', [])
    if 'page_ids' in receipt: return {pages.index(p)+1 for p in receipt['page_ids'] if p in pages}
    return set(receipt.get('pages', [item.get('slot',1)]))


def decorated(draft):
    return {**{k:v for k,v in draft.items() if k!='edit_history_archive'}, 'items': [{**a, 'geometry_checked': confirmed(draft, a)} for a in draft.get('items', [])],
            'reviewed_pages': {a['id']:sorted(reviewed_slots(draft,a)) for a in draft.get('items',[])}, 'validation_issues': issues(draft)}


def review(service, task_id, data):
    with service.store.connection(write=True) as db:
        service.require_available(db, 'tasks', task_id)
        key = 'review:'+task_id+':'+data['request_id'] if data.get('request_id') else None
        request = {**data, 'task_id':task_id}
        if key:
            cached = service.store.once(db, key, request)
            if cached is not None:
                return decorated(service.store.get(db,'reference_drafts',task_id)) if cached.get('operation')=='review' else cached
        draft = service.store.get(db, 'reference_drafts', task_id)
        require(draft['revision'] == data.get('revision') and draft['state'] != 'parsing', 'REFERENCE_CHANGED', '范本已变化，请先保存并重新核对。', 409)
        slot = data.get('slot')
        if data.get('page_id'):
            pages = draft.get('page_ids') or draft.get('asset_ids', [])
            require(data['page_id'] in pages, 'REFERENCE_SLOT', '范本页面已变化。',409)
            slot = pages.index(data['page_id'])+1
        revoke = data.get('revoke') or data.get('action') == 'revoke'
        targets = [a for a in draft['items'] if (a['id'] in data['item_ids'] if data.get('item_ids') else slot in required_slots(a, draft))]
        if data.get('no_answers'):
            require(slot and 0 < slot <= len(draft.get('asset_ids', [])) and not targets,
                    'PAGE_HAS_ANSWERS', '仅没有答案及跨页关联的图片可标记为无作答内容页。')
            result = service.store.put(db, 'reference_drafts', {**draft, 'non_answer_pages': {
                **draft.get('non_answer_pages', {}), str(slot): draft['asset_ids'][slot-1]}})
            service.store.event(db, 'reference.empty_page_reviewed', result, slot=slot)
            value = decorated(result)
            if key: service.store.remember(db, key, request, {'operation':'review','task_id':task_id,'committed_revision':value['revision']})
            return value
        require(targets, 'REFERENCE_EMPTY', '本次没有可确认的答案。')
        selected = {a['id'] for a in targets}
        blockers = [i for i in issues(draft, False) if i['item_id'] in selected]
        require(revoke or not blockers, 'REFERENCE_ISSUES', '请先修正本页问题；批量确认不能跳过。', issues=blockers)
        receipts = dict(draft.get('review_receipts', {}))
        for a in targets:
            if revoke:
                old = receipts.get(a['id'], {})
                if slot and not data.get('all_pages'):
                    receipts[a['id']] = {**old, 'pages':[p for p in old.get('pages', []) if p != slot], **({'page_ids':[p for p in old['page_ids'] if p != (draft.get('page_ids') or draft.get('asset_ids',[]))[slot-1]]} if 'page_ids' in old else {})}
                else:
                    receipts.pop(a['id'], None)
            else:
                stamp = fingerprint(draft, a)
                old = receipts.get(a['id'], {})
                pages = reviewed_slots(draft, a)
                pages.add(slot or a['slot'])
                receipts[a['id']] = {'fingerprint': stamp, 'stamp_version': 2, 'pages': sorted(pages), 'page_ids':[(draft.get('page_ids') or draft.get('asset_ids',[]))[p-1] for p in sorted(pages) if p <= len(draft.get('asset_ids',[]))], 'confirmed_at': now(), 'scope': 'page' if slot else 'item'}
        result = service.store.put(db, 'reference_drafts', {**draft, 'review_receipts': receipts})
        service.store.event(db, 'reference.reviewed', result, count=len(targets), scope='page' if data.get('slot') else 'item')
        value = decorated(result)
        if key: service.store.remember(db, key, request, {'operation':'review','task_id':task_id,'committed_revision':value['revision']})
        return value
