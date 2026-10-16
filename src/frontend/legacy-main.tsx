import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './styles.css';

type Status = 'correct' | 'incorrect' | 'missing' | 'unknown';
type Box = { x1: number; y1: number; x2: number; y2: number } | null;
type Answer = {
  question_number: number; english: string; chinese: string; variants: string[];
  raw_english: string; machine_raw_english: string | null; status: Status; machine_status: Status;
  uncertain: boolean; box: Box; row: number | null; column: number | null;
  reviewed_by_teacher: boolean; note: string;
};
type Attempt = {
  id: string; status: string; requested_model: string; requested_effort: string; requested_fast: boolean;
  answer_version: number; queued_at: string; started_at: string | null; finished_at: string | null;
  elapsed_ms: number | null; usage: Record<string, number> | null; turn_id: string | null; error_code: string | null;
  quota_before: QuotaSnapshot | null; quota_after: QuotaSnapshot | null;
};
type Page = {
  id: string; submission_id: string; tag: string; input_seq: number; file_name: string;
  width: number; height: number; created_at: string; status: string; image_url: string;
  revision: number; reviewed: boolean; paper_feedback: string; answers: Answer[];
  extra_cells: Array<{ question_number: number; raw_english: string; uncertain: boolean }>;
  error_code: string | null; attempts: Attempt[];
};
type Batch = {
  id: string; name: string; class_name: string; created_at: string; status: string; answer_version: number;
  answers: Array<{ question_number: number; english: string; chinese: string; variants: string[] }>;
  counts: { pages: number; queued: number; needs_review: number; reviewed: number; failed: number; paper_feedback_pending: number };
  usage: Record<string, number>; latest_event_id: number; pages: Page[];
};
type Model = { id: string; name: string; default: boolean; reasoning_efforts: string[]; default_effort: string | null; service_tiers: string[] };
type Draft = { status: Status; raw_english: string; note: string };
type QuotaWindow = { usedPercent?: number; windowDurationMins?: number; resetsAt?: string | number; limitName?: string };
type QuotaSnapshot = { sampled_at: string; source: string; rate_limits: Record<string, { primary?: QuotaWindow; secondary?: QuotaWindow }> };

const statusText: Record<Status, string> = { correct: '正确', incorrect: '不一致', missing: '空缺', unknown: '待辨认' };
const statusClass: Record<Status, string> = { correct: 'good', incorrect: 'bad', missing: 'muted', unknown: 'pending' };

async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, init);
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try { message = (await response.json()).detail ?? message; } catch { /* use status message */ }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}

function post(path: string, body?: unknown) {
  return api<Batch>(path, { method: 'POST', ...(body === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }) });
}

function parseAnswers(text: string) {
  const items: Array<{ question_number: number; english: string; chinese: string; variants: string[] }> = [];
  for (const line of text.split(/\r?\n/)) {
    if (!line.trim()) continue;
    const match = line.match(/^\s*(\d+)\s*[.)、]\s*(.+?)\s*$/);
    if (!match) throw new Error(`这一行格式不对：${line}`);
    const number = Number(match[1]);
    const [english, chinese = ''] = match[2].split(/[|｜]/, 2).map(part => part.trim());
    if (!english) throw new Error(`第 ${number} 题的英文答案为空。`);
    items.push({ question_number: number, english, chinese, variants: [] });
  }
  if (!items.length) throw new Error('请至少录入一道题。');
  if (new Set(items.map(item => item.question_number)).size !== items.length) throw new Error('题号重复，请检查后再保存。');
  return items.sort((a, b) => a.question_number - b.question_number);
}

function answerText(items: Batch['answers']) {
  return items.map(item => `${item.question_number}. ${item.english}${item.chinese ? ` | ${item.chinese}` : ''}`).join('\n');
}

function makeAssignments(files: File[], firstTag: string) {
  const match = firstTag.match(/^(.*?)(\d+)$/);
  return files.map((file, index) => ({
    file,
    tag: match ? match[1] + (BigInt(match[2]) + BigInt(index)).toString().padStart(match[2].length, '0') : files.length === 1 ? firstTag : `${firstTag}-${index + 1}`,
  }));
}

function App() {
  const [batches, setBatches] = useState<Array<Pick<Batch, 'id' | 'name' | 'class_name' | 'created_at' | 'status' | 'answer_version'>>>([]);
  const [batchId, setBatchId] = useState('');
  const [detail, setDetail] = useState<Batch | null>(null);
  const [pageId, setPageId] = useState('');
  const [models, setModels] = useState<Model[]>([]);
  const [modelId, setModelId] = useState('');
  const [effort, setEffort] = useState('');
  const [capabilityState, setCapabilityState] = useState<'loading' | 'ready' | 'error'>('loading');
  const [capabilityMessage, setCapabilityMessage] = useState('正在读取 Codex 图片模型…');
  const [answersDraft, setAnswersDraft] = useState('');
  const [answerBusy, setAnswerBusy] = useState(false);
  const [answerError, setAnswerError] = useState('');
  const [tag, setTag] = useState('017');
  const [assignments, setAssignments] = useState<Array<{ file: File; tag: string }>>([]);
  const [uploadBusy, setUploadBusy] = useState(false);
  const [drafts, setDrafts] = useState<Record<number, Draft>>({});
  const [reviewBusy, setReviewBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [showPerformance, setShowPerformance] = useState(false);
  const imageInput = useRef<HTMLInputElement>(null);
  const eventSource = useRef<EventSource | null>(null);
  const answerDraftVersion = useRef<{ batchId: string; version: number } | null>(null);

  const selectedPage = detail?.pages.find(page => page.id === pageId) ?? null;
  const currentModel = models.find(model => model.id === modelId) ?? null;
  const answersText = useMemo(() => answerText(detail?.answers ?? []), [detail?.answers]);

  const loadBatches = useCallback(async (prefer?: string) => {
    const rows = await api<typeof batches>('/api/batches');
    setBatches(rows);
    const next = prefer ?? batchId;
    if (!next && rows.length) setBatchId(rows[0].id);
    else if (next && !rows.some(row => row.id === next)) setBatchId(rows[0]?.id ?? '');
  }, [batchId]);

  const loadDetail = useCallback(async (id: string, keepPage = true) => {
    if (!id) { setDetail(null); return; }
    const next = await api<Batch>(`/api/batches/${id}`);
    setDetail(next);
    if (!keepPage || !next.pages.some(page => page.id === pageId)) {
      setPageId(next.pages.find(page => page.status === 'needs_review')?.id ?? next.pages[0]?.id ?? '');
    }
    if (!answerDraftVersion.current || answerDraftVersion.current.batchId !== next.id || answerDraftVersion.current.version !== next.answer_version) {
      setAnswersDraft(answerText(next.answers));
      answerDraftVersion.current = { batchId: next.id, version: next.answer_version };
    }
  }, [pageId]);

  useEffect(() => { void loadBatches().catch(error => setMessage(error.message)); }, []);
  useEffect(() => {
    let active = true;
    api<{ models: Model[] }>('/api/capabilities').then(data => {
      if (!active) return;
      setModels(data.models);
      const preferred = data.models.find(model => model.default) ?? data.models[0];
      setModelId(preferred?.id ?? '');
      setEffort(preferred?.default_effort ?? preferred?.reasoning_efforts[0] ?? '');
      setCapabilityState(preferred ? 'ready' : 'error');
      setCapabilityMessage(preferred ? 'ChatGPT 账户已连接' : '当前账户没有可用图片模型');
    }).catch(error => {
      if (!active) return;
      setCapabilityState('error');
      setCapabilityMessage(error.message);
    });
    return () => { active = false; };
  }, []);
  useEffect(() => { void loadDetail(batchId).catch(error => setMessage(error.message)); }, [batchId]);
  useEffect(() => {
    if (!detail) return;
    const activeBatch = detail.id;
    const refresh = () => { void Promise.all([loadDetail(activeBatch), loadBatches(activeBatch)]).catch(error => setMessage(error.message)); };
    const interval = window.setInterval(refresh, 2200);
    eventSource.current?.close();
    const source = new EventSource(`/api/batches/${activeBatch}/events?after_id=${detail.latest_event_id}`);
    eventSource.current = source;
    source.onmessage = refresh;
    for (const name of ['page.queued', 'page.processing', 'page.ready', 'page.failed', 'page.reviewed', 'page.review_changed', 'answers.updated', 'paper_feedback.changed', 'batch.finalized', 'batch.reopened']) source.addEventListener(name, refresh);
    return () => { window.clearInterval(interval); source.close(); };
  }, [detail?.id, detail?.latest_event_id, loadDetail, loadBatches]);
  useEffect(() => {
    if (currentModel && !currentModel.reasoning_efforts.includes(effort)) setEffort(currentModel.default_effort ?? currentModel.reasoning_efforts[0] ?? '');
  }, [currentModel, effort]);

  async function createBatch() {
    const name = window.prompt('给本次默写批次起个名字', '四年级英语默写');
    if (!name?.trim()) return;
    const className = window.prompt('班级（可留空）', '') ?? '';
    try {
      const created = await api<Batch>('/api/batches', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name.trim(), class_name: className.trim() }) });
      await loadBatches(created.id);
      setBatchId(created.id);
      setDetail(created);
      setPageId('');
      setAnswersDraft('');
      answerDraftVersion.current = { batchId: created.id, version: created.answer_version };
      setMessage('批次已建立。先确认参考答案，再放入作业图片。');
    } catch (error) { setMessage((error as Error).message); }
  }

  async function saveAnswers() {
    if (!detail) return;
    setAnswerError('');
    setAnswerBusy(true);
    try {
      const items = parseAnswers(answersDraft);
      const updated = await api<Batch>(`/api/batches/${detail.id}/answers`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_version: detail.answer_version, items }) });
      setDetail(updated);
      setAnswersDraft(answerText(updated.answers));
      answerDraftVersion.current = { batchId: updated.id, version: updated.answer_version };
      setMessage(`参考答案 v${updated.answer_version} 已确认。`);
    } catch (error) { setAnswerError((error as Error).message); }
    finally { setAnswerBusy(false); }
  }

  async function submitImages(event: React.FormEvent) {
    event.preventDefault();
    if (!detail || !assignments.length || !modelId || !effort) return;
    setUploadBusy(true);
    setMessage('');
    let submitted = 0;
    try {
      for (const assignment of assignments) {
        const form = new FormData();
        form.set('file', assignment.file);
        const query = new URLSearchParams({ tag: assignment.tag, model: modelId, effort });
        const result = await api<{ page_id: string }>(`/api/batches/${detail.id}/pages?${query}`, { method: 'POST', headers: { 'X-Client-Request-Id': crypto.randomUUID() }, body: form });
        submitted++;
        if (submitted === 1) setPageId(result.page_id);
      }
      setAssignments([]);
      setTag((current) => {
        const match = current.match(/^(.*?)(\d+)$/);
        return match ? match[1] + (BigInt(match[2]) + BigInt(submitted)).toString().padStart(match[2].length, '0') : current;
      });
      if (imageInput.current) imageInput.current.value = '';
      setMessage(`已将 ${submitted} 张图片按序放入后台队列；图片会发送给所选 ChatGPT 图片模型并消耗账户额度。`);
      await loadDetail(detail.id, false);
    } catch (error) {
      setMessage(`已提交 ${submitted} 张。${(error as Error).message}`);
      await loadDetail(detail.id).catch(() => undefined);
    } finally { setUploadBusy(false); }
  }

  function selectFiles(files: FileList | null) {
    if (!files) return;
    const selected = Array.from(files);
    setAssignments(makeAssignments(selected, tag.trim()));
  }

  function setDraft(number: number, patch: Partial<Draft>, answer: Answer) {
    setDrafts(current => ({ ...current, [number]: { status: current[number]?.status ?? answer.status, raw_english: current[number]?.raw_english ?? answer.raw_english, note: current[number]?.note ?? answer.note, ...patch } }));
  }

  async function savePageReview(confirmed: boolean) {
    if (!detail || !selectedPage) return;
    setReviewBusy(true);
    try {
      const items = Object.entries(drafts).map(([number, draft]) => ({ question_number: Number(number), ...draft }));
      const updated = await api<Batch>(`/api/pages/${selectedPage.id}/review`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_revision: selectedPage.revision, items, confirmed }) });
      setDetail(updated);
      setDrafts({});
      setMessage(confirmed ? `作业 ${selectedPage.tag} 已完成数字复核。` : '复核修改已保存。');
    } catch (error) { setMessage((error as Error).message); }
    finally { setReviewBusy(false); }
  }

  async function undoReview() {
    if (!detail || !selectedPage) return;
    try {
      const updated = await post(`/api/pages/${selectedPage.id}/undo?expected_revision=${selectedPage.revision}`);
      setDetail(updated);
      setDrafts({});
      setMessage('已撤销上一步复核，并保留了历史记录。');
    } catch (error) { setMessage((error as Error).message); }
  }

  async function togglePaperFeedback() {
    if (!detail || !selectedPage) return;
    try {
      const updated = await api<Batch>(`/api/submissions/${selectedPage.submission_id}/paper-feedback`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ done: selectedPage.paper_feedback !== 'done' }) });
      setDetail(updated);
      setMessage('纸面反馈状态已更新。');
    } catch (error) { setMessage((error as Error).message); }
  }

  async function retryPage() {
    if (!selectedPage) return;
    try {
      const updated = await post(`/api/pages/${selectedPage.id}/retry`);
      setDetail(updated);
      setMessage('已用原来的模型配置重新入队。');
    } catch (error) { setMessage((error as Error).message); }
  }

  async function finishBatch() {
    if (!detail) return;
    try {
      const updated = await api<Batch>(`/api/batches/${detail.id}/finalize`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_version: detail.answer_version }) });
      setDetail(updated);
      await loadBatches(updated.id);
      setMessage('本批次的数字复核结果已封存。纸面反馈状态继续单独显示。');
    } catch (error) { setMessage((error as Error).message); }
  }

  async function reopenBatch() {
    if (!detail) return;
    try {
      const updated = await post(`/api/batches/${detail.id}/reopen`);
      setDetail(updated);
      await loadBatches(updated.id);
    } catch (error) { setMessage((error as Error).message); }
  }

  const reviewedEditable = selectedPage?.status === 'needs_review' && detail?.status !== 'finalized';
  const activeAnswers = detail?.answers ?? [];

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark">A<span>✓</span></div><div><div className="brand-name">autocheck<span className="brand-dot">.</span></div><div className="brand-sub">ENGLISH WORKBOOK</div></div></div>
      <div className="sidebar-label">MY WORKSPACE <span className="tiny-dot" /></div>
      <button className="new-batch" onClick={() => void createBatch()}><span className="plus">＋</span> 新建批改批次 <span className="shortcut">⌘ K</span></button>
      <div className="batch-section-title"><span>近期批次</span><span className="count-pill">{batches.length}</span></div>
      <div className="batch-list">
        {batches.map((batch, index) => <button key={batch.id} className={`batch-nav ${batch.id === batchId ? 'active' : ''}`} onClick={() => { setBatchId(batch.id); setDrafts({}); }}>
          <span className={`batch-icon icon-${index % 4}`}>{index % 2 === 0 ? '文' : 'A'}</span><span className="batch-nav-copy"><strong>{batch.name}</strong><small>{batch.class_name || '个人批次'} · v{batch.answer_version}</small></span>{batch.status === 'finalized' && <span className="mini-check">✓</span>}
        </button>)}
        {!batches.length && <p className="sidebar-empty">这里会显示你的批改记录</p>}
      </div>
      <div className="sidebar-bottom"><div className="privacy-dot" /><span>本地保存 · 你的数据由你掌控</span><span className="local-lock">⌑</span></div>
    </aside>

    <main className="main-area">
      <header className="topbar">
        <div className="breadcrumb"><span>工作台</span><span className="crumb-slash">/</span><strong>{detail?.name ?? '欢迎使用 AutoReview'}</strong>{detail?.class_name && <span className="class-chip">{detail.class_name}</span>}</div>
        <div className="top-actions"><span className={`connection-chip ${capabilityState}`}><i />{capabilityMessage}</span><button className="avatar" title="个人工作台">英</button></div>
      </header>

      {!detail ? <Welcome onCreate={() => void createBatch()} capabilityMessage={capabilityMessage} /> : <>
        <section className="batch-overview">
          <div><div className="eyebrow">BATCH OVERVIEW <span className="live-dot" /> LIVE</div><h1>{detail.name}</h1><p>{detail.class_name || '英语默写'} <span className="dot-sep">·</span> 创建于 {formatDate(detail.created_at)} <span className="dot-sep">·</span> 参考答案 {detail.answer_version ? `v${detail.answer_version}` : '未确认'}</p></div>
          <div className="overview-actions"><button className="quiet-button" onClick={() => setShowPerformance(value => !value)}><span>◷</span> 性能记录</button>{detail.status === 'finalized' ? <button className="outline-button" onClick={() => void reopenBatch()}>重新打开</button> : <button className="finish-button" disabled={!detail.pages.length || detail.counts.queued > 0 || detail.counts.failed > 0 || detail.counts.needs_review > 0} onClick={() => void finishBatch()}><span>✓</span> 完成批次复核</button>}</div>
        </section>

        <section className="stats-row">
          <Stat icon="▧" label="已收作业" value={detail.counts.pages} sub="按输入顺序保留" tone="lavender" />
          <Stat icon="◌" label="后台处理中" value={detail.counts.queued} sub={detail.counts.queued ? '按序等待模型处理' : '队列已清空'} tone="blue" />
          <Stat icon="✳" label="等待复核" value={detail.counts.needs_review + detail.counts.failed} sub={detail.counts.failed ? `${detail.counts.failed} 项需要重试` : '模型初判需要教师确认'} tone="amber" />
          <Stat icon="✓" label="已复核" value={detail.counts.reviewed} sub="人工确认结果" tone="green" />
          <Stat icon="↗" label="待纸面反馈" value={detail.counts.paper_feedback_pending} sub="纸面进度独立记录" tone="rose" />
        </section>

        {showPerformance && <Performance detail={detail} />}
        {message && <div className="notice"><span>✦</span><span>{message}</span><button onClick={() => setMessage('')} aria-label="关闭">×</button></div>}

        <div className="work-grid">
          <section className="panel answer-panel">
            <PanelHeader kicker="01 / REFERENCE KEY" title="参考答案" meta={detail.answer_version ? `当前 v${detail.answer_version}` : '尚未确认'} icon="≋" />
            <p className="panel-description">先确认题号和标准英文。学生图片只用于独立转录，不会收到这份答案。</p>
            <label className="field-label" htmlFor="answer-list">编号答案 <span>一行一题，可选中文</span></label>
            <textarea id="answer-list" className="answer-editor" value={answersDraft} onChange={event => setAnswersDraft(event.target.value)} placeholder={'1. apple | 苹果\n2. school bag | 书包\n3. orange | 橙子'} disabled={detail.status === 'finalized'} spellCheck={false} />
            <div className="format-hint"><span className="hint-icon">i</span><span>格式示例：<code>1. apple | 苹果</code></span></div>
            {answerError && <div className="inline-error">{answerError}</div>}
            <button className="primary-button answer-save" disabled={answerBusy || detail.status === 'finalized' || capabilityState === 'loading'} onClick={() => void saveAnswers()}>{answerBusy ? <Spinner /> : '保存并确认答案'} <span>→</span></button>
            {detail.answer_version > 0 && <div className="answer-count"><span className="check-disc">✓</span> {activeAnswers.length} 道题已确认，修改会建立新版本并要求重新复核</div>}
          </section>

          <section className="panel intake-panel">
            <PanelHeader kicker="02 / INTAKE & QUEUE" title="放入作业图片" meta="后台异步处理" icon="↥" />
            <form onSubmit={event => void submitImages(event)}>
              <div className="intake-config">
                <div className="select-field"><label htmlFor="model-select">图像识别模型</label><select id="model-select" value={modelId} onChange={event => setModelId(event.target.value)} disabled={capabilityState !== 'ready'}>{models.map(model => <option key={model.id} value={model.id}>{model.name}{model.default ? ' · 默认' : ''}</option>)}</select></div>
                <div className="select-field effort-select"><label htmlFor="effort-select">推理强度</label><select id="effort-select" value={effort} onChange={event => setEffort(event.target.value)} disabled={!currentModel}>{currentModel?.reasoning_efforts.map(value => <option key={value} value={value}>{value}</option>)}</select></div>
              </div>
              <div className="quota-note"><span className="quota-icon">✳</span><span>提交后，原图会发送给所选 ChatGPT 图像模型，并使用已登录账户的 Codex 额度</span><span className="standard-tag">标准</span></div>
              <div className="intake-fields">
                <label className="field-label" htmlFor="paper-tag">纸签编号 <span>相同编号可以追加多页</span></label>
                <input id="paper-tag" className="text-input" value={tag} onChange={event => setTag(event.target.value)} maxLength={30} placeholder="例如 017" disabled={detail.status === 'finalized'} />
              </div>
              <input ref={imageInput} type="file" accept="image/jpeg,image/png" multiple hidden onChange={event => selectFiles(event.target.files)} />
              <button type="button" className={`drop-area ${assignments.length ? 'has-files' : ''}`} onClick={() => imageInput.current?.click()} disabled={detail.answer_version === 0 || detail.status === 'finalized'}>
                <span className="upload-icon">↑</span><strong>{assignments.length ? `已选择 ${assignments.length} 张图片` : '选择一张或多张作业图片'}</strong><span>{detail.answer_version === 0 ? '请先确认参考答案' : 'JPEG 或 PNG · 支持连续单张与批量导入'}</span>
              </button>
              {assignments.length > 0 && <div className="import-map"><div className="import-map-title"><span>导入清单</span><button type="button" onClick={() => { setAssignments([]); if (imageInput.current) imageInput.current.value = ''; }}>清空</button></div>{assignments.map((item, index) => <div className="import-row" key={`${item.file.name}-${index}`}><span className="import-index">{index + 1}</span><span className="import-filename" title={item.file.name}>{item.file.name}</span><input aria-label={`${item.file.name} 的纸签编号`} value={item.tag} onChange={event => setAssignments(current => current.map((entry, i) => i === index ? { ...entry, tag: event.target.value } : entry))} /></div>)}</div>}
              <div className="form-footer"><span><span className="privacy-dot" />图片保存在本机，原件会长期保留</span><button className="primary-button" type="submit" disabled={!assignments.length || uploadBusy || !detail.answer_version || capabilityState !== 'ready' || !effort}>{uploadBusy ? <><Spinner /> 入队中</> : <>加入后台队列 <span>→</span></>}</button></div>
            </form>
            {capabilityState === 'error' && <div className="auth-tip">账户模型未就绪。请在本机 PowerShell 执行 <code>codex login</code> 完成官方登录，再刷新页面。</div>}
          </section>
        </div>

        <section className="review-section">
          <div className="section-title-row"><div><div className="eyebrow">03 / FINAL REVIEW</div><h2>教师复核 <span>{detail.pages.length} <i>pages</i></span></h2></div><div className="review-legend"><span><i className="legend-dot wrong" /> 内容不一致</span><span><i className="legend-dot unknown" /> 待辨认</span><span><i className="legend-dot right" /> 符合答案</span></div></div>
          {!detail.pages.length ? <div className="empty-review"><span>▤</span><strong>还没有作业图片</strong><p>确认参考答案后，把第一张作业放进队列。</p></div> : <div className="review-workspace">
            <div className="page-rail"><div className="rail-heading"><strong>作业队列</strong><span>{detail.pages.length} 页</span></div>{detail.pages.map(page => <button key={page.id} className={`page-item ${selectedPage?.id === page.id ? 'selected' : ''}`} onClick={() => { setPageId(page.id); setDrafts({}); }}><div className="page-thumb"><img src={page.image_url} alt="" /><StatusMark page={page} /></div><div className="page-item-copy"><strong><span className="seq">{String(page.input_seq).padStart(2, '0')}</span> {page.tag}号作业</strong><small>{page.status === 'needs_review' ? page.reviewed ? '复核已确认' : `${page.answers.filter(item => item.status === 'incorrect' || item.status === 'missing').length} 项需确认` : page.status === 'queued' || page.status === 'processing' ? '后台处理中' : page.status === 'failed' ? '处理失败' : '待开始'}</small></div><span className={`page-state ${page.reviewed ? 'done' : page.status === 'failed' ? 'failed' : ''}`}>{page.reviewed ? '✓' : page.status === 'failed' ? '!' : page.status === 'needs_review' ? '·' : '◌'}</span></button>)}</div>
            {selectedPage ? <>
              <div className="image-panel panel">
                <div className="image-panel-toolbar"><div><span className="image-number">{String(selectedPage.input_seq).padStart(2, '0')}</span><div><strong>{selectedPage.tag}号作业</strong><small>{selectedPage.file_name}</small></div></div><span className={`status-chip ${selectedPage.reviewed ? 'status-reviewed' : selectedPage.status === 'failed' ? 'status-failed' : selectedPage.status === 'needs_review' ? 'status-review' : 'status-queued'}`}>{selectedPage.reviewed ? '教师已复核' : selectedPage.status === 'needs_review' ? '待教师确认' : selectedPage.status === 'failed' ? '识别失败' : selectedPage.status === 'processing' ? '正在识别' : '排队等待'}</span></div>
                <div className="image-canvas">
                  {selectedPage.status === 'needs_review' ? <ImageWithMarks page={selectedPage} onSelect={number => document.getElementById(`answer-${number}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })} /> : selectedPage.status === 'failed' ? <div className="image-state"><span>!</span><strong>图片保留，识别没有完成</strong><p>可以检查模型登录或网络状态，然后用原配置重试。</p><button className="outline-button" onClick={() => void retryPage()}>使用原配置重试</button></div> : <div className="image-state"><Spinner /><strong>{selectedPage.status === 'processing' ? 'ChatGPT 正在读取这张图片' : '图片已安全入队'}</strong><p>你可以继续放入下一张；每个结果都会按纸签保存。</p></div>}
                </div>
                <div className="image-panel-footer"><span><span className="privacy-dot" />原始图片 · {selectedPage.width} × {selectedPage.height}</span><span>输入序号 {selectedPage.input_seq}</span></div>
              </div>
              <div className="panel correction-panel">
                <PanelHeader kicker="04 / HUMAN DECISION" title="逐题核对" meta={selectedPage.answers.length ? `${selectedPage.answers.length} 项` : '识别完成后显示'} icon="✓" />
                {selectedPage.status !== 'needs_review' ? <div className="waiting-copy">{selectedPage.status === 'failed' ? '重试成功后这里会显示逐题转录和判定。' : '图片处理完成后，在这里核对模型转录和答案。'}</div> : <>
                  <div className="answer-key-strip"><span className="key-token">KEY</span><span>参考答案 v{detail.answer_version} · 仅在识别后进行比对</span></div>
                  <div className="answer-rows">{selectedPage.answers.map(answer => <AnswerRow key={answer.question_number} answer={answer} draft={drafts[answer.question_number]} editable={!!reviewedEditable} onChange={patch => setDraft(answer.question_number, patch, answer)} />)}</div>
                  {selectedPage.extra_cells.length > 0 && <div className="extra-cells"><strong>未对应答案的识别项</strong>{selectedPage.extra_cells.map((cell, index) => <span key={`${cell.question_number}-${index}`}>第 {cell.question_number} 题 · {cell.raw_english || '空白'}{cell.uncertain ? ' · 待辨认' : ''}</span>)}</div>}
                  {selectedPage.error_code && <div className="inline-error">有题目没有识别到或位置不确定，系统不会把它自动算作正确。</div>}
                  <div className="review-actions"><button className="quiet-button undo-button" disabled={!selectedPage.revision || !reviewedEditable} onClick={() => void undoReview()}>↶ 撤销上一步</button><button className="outline-button" disabled={!Object.keys(drafts).length || reviewBusy || !reviewedEditable} onClick={() => void savePageReview(false)}>{reviewBusy ? <Spinner /> : '保存修改'}</button><button className="primary-button" disabled={reviewBusy || !reviewedEditable} onClick={() => void savePageReview(true)}>{reviewBusy ? <Spinner /> : selectedPage.reviewed ? '保存并确认' : '确认本份'} <span>✓</span></button></div>
                  <div className="paper-feedback"><div><strong>纸面反馈</strong><small>{selectedPage.paper_feedback === 'done' ? '已找到纸本并完成圈点' : selectedPage.paper_feedback === 'needs_update' ? '数字结果有修改，请检查是否要更新纸面' : '找到纸本后，单独确认圈点进度'}</small></div><button className={selectedPage.paper_feedback === 'done' ? 'feedback-done' : 'outline-button'} disabled={detail.status === 'finalized'} onClick={() => void togglePaperFeedback()}>{selectedPage.paper_feedback === 'done' ? '✓ 已完成' : '标记完成'}</button></div>
                </>}
              </div>
            </> : <div className="panel no-selection">从左侧作业队列选择一页，开始最终复核。</div>}
          </div>}
        </section>
        <footer className="page-footer"><span>AUTOREVIEW PROTOTYPE <i>·</i> 图片识别由当前 ChatGPT 账户执行</span><span>原图、答案版本和复核历史保存在本机</span></footer>
      </>}
    </main>
  </div>;
}

function Welcome({ onCreate, capabilityMessage }: { onCreate: () => void; capabilityMessage: string }) {
  return <section className="welcome-screen"><div className="welcome-art"><div className="art-sheet"><div className="art-mark">A<span>✓</span></div><div className="art-line long"/><div className="art-line"/><div className="art-four"><span>apple</span><span>school</span><span>blue</span><span>friend</span></div><div className="art-orbit">✓</div><div className="art-pencil">✎</div></div><span className="art-spark spark-one">✳</span><span className="art-spark spark-two">✳</span></div><div className="welcome-copy"><div className="eyebrow">A LITTLE MORE TIME FOR TEACHING</div><h1>让批改，轻松一点。</h1><p>图片交给 ChatGPT 识别，参考答案帮助比对。<br/>每一张作业都留在自己的纸签下面，由你完成最终确认。</p><div className="welcome-steps"><span><i>01</i> 确认答案</span><b>→</b><span><i>02</i> 放入作业</span><b>→</b><span><i>03</i> 教师复核</span></div><button className="primary-button welcome-button" onClick={onCreate}>建立第一个批改批次 <span>→</span></button><div className="welcome-status"><span className={`connection-chip ${capabilityMessage.includes('已连接') ? 'ready' : 'loading'}`}><i />{capabilityMessage}</span><span className="privacy-dot"/> 所有作业图片保存在本机</div></div></section>;
}

function Stat({ icon, label, value, sub, tone }: { icon: string; label: string; value: number; sub: string; tone: string }) {
  return <div className="stat-card"><span className={`stat-icon ${tone}`}>{icon}</span><div className="stat-copy"><span>{label}</span><strong>{value.toString().padStart(2, '0')}</strong><small>{sub}</small></div></div>;
}

function PanelHeader({ kicker, title, meta, icon }: { kicker: string; title: string; meta: string; icon: string }) {
  return <div className="panel-heading"><div><div className="eyebrow">{kicker}</div><h3>{title}</h3></div><div className="panel-meta"><span className="panel-meta-icon">{icon}</span>{meta}</div></div>;
}

function ImageWithMarks({ page, onSelect }: { page: Page; onSelect: (number: number) => void }) {
  return <div className="marked-image-frame"><img src={page.image_url} alt={`${page.tag}号作业原图`} /><svg viewBox="0 0 1000 1000" preserveAspectRatio="none" aria-label="作业批注">
    {page.answers.filter(answer => answer.box).map(answer => {
      const box = answer.box!;
      const status = answer.status;
      return <g key={answer.question_number} className={`mark-group ${statusClass[status]}`} onClick={() => onSelect(answer.question_number)} role="button" tabIndex={0} aria-label={`${answer.question_number}题：${statusText[status]}`}>
        <rect x={box.x1} y={box.y1} width={box.x2 - box.x1} height={box.y2 - box.y1} rx="12" />
        {status !== 'correct' && <><circle cx={Math.min(box.x2 + 12, 975)} cy={Math.max(box.y1 - 8, 25)} r="18" /><text x={Math.min(box.x2 + 12, 975)} y={Math.max(box.y1 - 8, 25)}>{status === 'incorrect' ? '!' : '?'}</text></>}
      </g>;
    })}
  </svg></div>;
}

function AnswerRow({ answer, draft, editable, onChange }: { answer: Answer; draft?: Draft; editable: boolean; onChange: (patch: Partial<Draft>) => void }) {
  const status = draft?.status ?? answer.status;
  const raw = draft?.raw_english ?? answer.raw_english;
  const isDirty = draft !== undefined;
  return <article className={`answer-row ${isDirty ? 'edited' : ''}`} id={`answer-${answer.question_number}`}>
    <div className="answer-row-main"><span className={`question-number ${statusClass[status]}`}>{String(answer.question_number).padStart(2, '0')}</span><div className="answer-content"><div className="answer-pair"><strong>{answer.english}</strong>{answer.chinese && <span>{answer.chinese}</span>}</div><div className="transcript-line"><span className="transcript-label">原文</span><input aria-label={`${answer.question_number}题学生原文`} value={raw} onChange={event => onChange({ raw_english: event.target.value })} disabled={!editable} /><span className={`status-label ${statusClass[status]}`}>{statusText[status]}</span></div></div></div>
    {answer.row && answer.column && <div className="answer-position">第 {answer.row} 行 · 第 {answer.column} 列</div>}
    <div className="answer-controls"><label>有效判定</label><select aria-label={`${answer.question_number}题的有效判定`} value={status} disabled={!editable} onChange={event => onChange({ status: event.target.value as Status })}>{(['correct', 'incorrect', 'missing', 'unknown'] as Status[]).map(value => <option value={value} key={value}>{statusText[value]}</option>)}</select>{answer.reviewed_by_teacher && <span className="teacher-mark">人工</span>}</div>
  </article>;
}

function StatusMark({ page }: { page: Page }) {
  if (page.reviewed) return <span className="thumb-mark done">✓</span>;
  if (page.status === 'failed') return <span className="thumb-mark failed">!</span>;
  if (page.status === 'needs_review') return <span className="thumb-mark ready">◦</span>;
  return <span className="thumb-mark waiting"><Spinner /></span>;
}

function Performance({ detail }: { detail: Batch }) {
  return <section className="performance-panel"><div className="performance-heading"><div><div className="eyebrow">RECORDED USAGE</div><strong>性能记录</strong></div><span>账户窗口额度不等于单图精确消耗</span></div><div className="performance-metrics"><Metric label="已处理调用" value={detail.usage.attempts} /><Metric label="有返回用量" value={detail.usage.attempts_with_usage} /><Metric label="输入 Token" value={formatToken(detail.usage.input_tokens)} /><Metric label="输出 Token" value={formatToken(detail.usage.output_tokens)} /><Metric label="推理 Token 明细" value={formatToken(detail.usage.reasoning_output_tokens)} /><Metric label="缓存输入" value={formatToken(detail.usage.cached_input_tokens)} /></div><div className="attempt-list">{detail.pages.flatMap(page => page.attempts.map(attempt => <div key={attempt.id}><span>#{page.input_seq} · {page.tag}号</span><span>{attempt.requested_model}</span><span>{attempt.requested_effort} · 标准</span><span>{attempt.elapsed_ms === null ? '时长未知' : `${(attempt.elapsed_ms / 1000).toFixed(1)} 秒 attempt`}</span><span>{attempt.status === 'failed' || attempt.status === 'interrupted' ? `失败 · ${attempt.error_code ?? '原因未知'}` : attempt.usage ? 'Token 已记录' : '用量未提供'}</span><span title={quotaObservation(attempt.quota_before, attempt.quota_after)}>{quotaObservation(attempt.quota_before, attempt.quota_after)}</span></div>))}</div><div className="performance-footnote">缓存和推理 Token 是明细，未重复加总。窗口仅作调用前后观测；未观测到变化不代表零消耗，也不能归属为单图精确消耗。</div></section>;
}

function quotaObservation(before: QuotaSnapshot | null, after: QuotaSnapshot | null) {
  if (!before || !after) return '额度窗口未完整读取';
  const observations: string[] = [];
  for (const [limitId, beforeLimit] of Object.entries(before.rate_limits ?? {})) {
    const afterLimit = after.rate_limits?.[limitId];
    if (!afterLimit) continue;
    for (const period of ['primary', 'secondary'] as const) {
      const start = beforeLimit[period];
      const end = afterLimit[period];
      if (!start || !end || typeof start.usedPercent !== 'number' || typeof end.usedPercent !== 'number') continue;
      if (start.windowDurationMins !== end.windowDurationMins || start.resetsAt == null || String(start.resetsAt) !== String(end.resetsAt)) continue;
      const label = start.limitName || limitId;
      const delta = end.usedPercent - start.usedPercent;
      const change = delta === 0 ? '未观测到窗口变化' : `观测 ${delta > 0 ? '+' : ''}${delta.toFixed(2)} 个百分点`;
      const days = start.windowDurationMins ? ` · ${Math.round(start.windowDurationMins / 1440)}日窗` : '';
      observations.push(`${label}${period === 'primary' ? '主窗' : '次窗'}${days} ${start.usedPercent}%→${end.usedPercent}% · ${change}`);
    }
  }
  return observations.length ? observations.join('；') : '额度窗口已变化或不可比';
}

function Metric({ label, value }: { label: string; value: string | number }) { return <div className="performance-metric"><span>{label}</span><strong>{value}</strong></div>; }
function Spinner() { return <span className="spinner" aria-label="处理中" />; }
function formatDate(value: string) { return new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric' }).format(new Date(value)); }
function formatToken(value: number | undefined) { return value === undefined ? '—' : new Intl.NumberFormat('en-US').format(value); }

createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>);
