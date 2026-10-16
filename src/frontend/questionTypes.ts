type R=Record<string,any>;
export const questionTypes=[['dictation','默写'],['copying','抄写'],['choice','选择题'],['fill_blank','填空'],['true_false','判断'],['ordering','排序'],['matching','连线'],['sentence','连词成句']];
const names:Record<string,string>={text:'文字',written_choice:'填写选项编号',marked_choice:'圈选／勾选选项',mixed:'字母或圈选均可',symbol:'勾叉等判断符号',rank:'排序数字',edges:'端点连线',unknown:'待确认（确认前须修改）'};
const forms:Record<string,string[]>={dictation:['text'],copying:['text'],choice:['written_choice','marked_choice','mixed'],fill_blank:['text','written_choice'],true_false:['symbol','text'],ordering:['rank','text'],matching:['edges'],sentence:['text']};
export const canonicalKind=(kind:string)=>['letter_choice','marked_choice'].includes(kind)?'choice':kind;
export const responseForms=(kind:string)=>[...(forms[canonicalKind(kind)]||['text']),'unknown'].map(v=>[v,names[v]]);
export const defaultForm=(kind:string)=>kind==='choice'?'mixed':forms[kind]?.[0]||'text';
export const answerHelp:Record<string,string>={dictation:'按原要求填写英文单词或短语。可接受的其他写法放在“可接受变体”。',copying:'填写要求抄写的完整英文原文，不自动改写拼写和语法。',choice:'先设置实际选项，再填写正确选项编号，例如 B；多选为 A,C。圈在 A/B/C 上仍属于选择题。',fill_blank:'每空独立填写一个答案；同题多空使用不同空位号。填写编号时需设置对应选项。',true_false:'选择正确或错误；符号写法只影响识别形式，不改变判定。',ordering:'单个图片位置填写它的排序数字；整串顺序使用逗号分隔。',matching:'分别添加两侧端点与印刷标识（如 1、A），框出位置，再添加正确配对。无需填写另一份文字答案。',sentence:'填写完整句子，保留标点及大小写；允许的其他正确句式写入变体。'};
export function nodeName(n:R|undefined){if(!n)return '缺少端点';if(n.display_id)return String(n.display_id);const label=String(n.label||'').trim();return /^([A-Za-z]|\d+)\s*[:：]/.exec(label)?.[1]||label||'待填写标识'}
export function matchingSummary(a:R){const r=a.matching_rule;if(!r?.allowed_edges?.length)return '尚未设置配对';return r.allowed_edges.map((e:string[])=>e.map(id=>nodeName(r.nodes.find((n:R)=>n.id===id))).join(r.directed?' → ':'—')).sort((a:string,b:string)=>a.localeCompare(b,'zh-CN',{numeric:true})).join('；')}
const translations:Record<string,string>={
 'All coordinates are approximate and require teacher review.':'提取的位置为初步定位，请教师对照原图核对完整作答区。',
 'Picture-only options are identified by visual description.':'纯图片选项采用图像描述辅助识别，请核对选项标识及位置。',
 'Listening answers are transcribed only where visible teacher ink supplies them.':'本次听力范本仅转录图片中可辨认的教师作答；未依据缺失音频推算答案。'};
export function parserNote(value:any){const raw=typeof value==='string'?value:value.raw||value.message_zh||'';return {raw,text:value.message_zh||translations[raw.trim().replace(/[;,；]+$/,'')]||(/[\u4e00-\u9fff]/.test(raw)?raw:'存在一条未分类解析说明，请展开查看原始内容。')}}
