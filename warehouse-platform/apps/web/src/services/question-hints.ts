export type QuestionHint = { label: string; detail: string };
export type QuestionIntent = { summary: string; hints: QuestionHint[] };

const scope = { label: '限定权限范围', detail: '请仅查询当前账号有权限访问的数据，并标明可追溯的数据来源。' };
const table = { label: '表格展示', detail: '请用表格展示结果，最后简要总结重点。' };

/** Local, advisory matching: drafts are never sent to a model or search service. */
export function questionIntent(input: string): QuestionIntent | undefined {
  const fullText = input.trim();
  const text = fullText.split('\n\n补充说明：\n')[0];
  if (text.length < 2) return undefined;
  let result: QuestionIntent;
  const operation = /删除|修改|重命名|创建|新增|导入/.test(text) && /怎么|如何|帮我|请|我要|想要/.test(text);
  const uploadHelp = /(?:怎么|如何).{0,12}上传|(?:帮我|请|我要|想要)上传/.test(text);
  if (operation || uploadHelp) {
    result = { summary: '你可能希望了解平台操作步骤。助手可以说明流程，实际操作需在对应功能页面完成。', hints: [
      { label: '分步说明', detail: '请分步骤说明操作入口、所需权限和完成后的检查方法。' },
      { label: '操作前确认', detail: '请先说明需要确认的对象、影响范围和注意事项，不直接执行修改。' },
      { label: '补齐必要条件', detail: '如果信息不足，请先列出需要我补充的条件。' },
    ] };
  } else if (/状态|健康|报错|异常|故障|连接|服务|正常/.test(text)) {
    result = { summary: '你可能希望检查系统运行情况，并了解异常原因与处理建议。', hints: [
      { label: '逐项检查服务', detail: '请分别说明应用服务、数据库、任务缓存、文件存储和千问服务的当前状态。' },
      { label: '优先关注异常', detail: '请优先列出异常项，区分已确认事实和可能原因，并给出排查顺序。' },
      { label: '简明诊断报告', detail: '请按“当前状态、影响范围、下一步建议”组织回答。' },
    ] };
  } else if (/版本|比较|对比|差异|变化/.test(text)) {
    result = { summary: '你可能希望比较数据或版本之间的差异，定位新增、缺失和变化。', hints: [
      { label: '明确比较对象', detail: '请先确认要比较的数据集、版本或时间范围，缺少条件时先向我询问。' },
      { label: '分类列出差异', detail: '请按新增、移除和变化分类汇总，并列出相关文件或版本。' }, table,
    ] };
  } else if (/统计|数量|多少|占比|趋势|汇总|容量|大小/.test(text)) {
    result = { summary: '你可能希望汇总数据规模或分布，并说明统计口径。', hints: [
      { label: '按数据集汇总', detail: '请按数据集汇总文件数量和占用空间，说明统计范围与单位。' },
      { label: '近七天概览', detail: '请限定为最近 7 天的数据，并说明采用的时间范围。' }, table,
    ] };
  } else if (/上传|文件|下载|格式|json|csv|图片/i.test(text)) {
    result = { summary: '你可能希望查找文件，了解所在数据集、上传时间或可用的下载入口。', hints: [
      { label: '补充文件信息', detail: '请列出文件名、所属数据集、文件大小和上传时间。' },
      { label: '最近上传优先', detail: '请优先展示最近上传的文件，并说明本次结果的查询范围。' }, scope,
    ] };
  } else if (/数据集|数仓|资产|数据/.test(text)) {
    result = { summary: '你可能希望发现可用的数据资产，并了解每个数据集的内容与规模。', hints: [
      { label: '展示数据集概况', detail: '请列出数据集名称、描述、文件数量及最近更新时间。' }, table, scope,
    ] };
  } else {
    result = { summary: '你可以补充目标、范围和希望的回答形式，让需求更明确。', hints: [
      { label: '先澄清需求', detail: '如果我的问题存在歧义，请先询问关键条件，再给出结论。' },
      { label: '给出可执行步骤', detail: '请先给出简短结论，再列出可执行的步骤和注意事项。' }, table,
    ] };
  }
  return { ...result, hints: result.hints.filter(hint => !fullText.includes(hint.detail)) };
}

export function appendQuestionHint(text: string, hint: QuestionHint): string {
  if (text.includes(hint.detail)) return text;
  const separator = text.includes('\n\n补充说明：\n') ? '\n' : '\n\n补充说明：\n';
  const next = `${text.trimEnd()}${separator}${hint.detail}`;
  return next.length <= 4000 ? next : text;
}
