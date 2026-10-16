"""One definition of unresolved teacher review for cards and confirmation gates."""


def pending_reasons(answer):
    reasons=[]
    if answer.get('status')=='unknown':reasons.append('内容判定待核实')
    if answer.get('writing_status')=='pending':reasons.append('书写判定待核实')
    return reasons


def needs_review(answer):
    return bool(pending_reasons(answer))


def pending_summary(answers):
    labels=[]
    for answer in answers[:3]:
        label=f"第{answer['slot']}页 · {answer.get('section') or '未分组'} · 第{answer['number']}题"
        if answer.get('part_id'):label+=f" · 第{answer['part_id']}空"
        labels.append(label+'：'+'、'.join(pending_reasons(answer)))
    return '；'.join(labels)+(f'；另有{len(answers)-3}题' if len(answers)>3 else '')
