import { describe, expect, it } from 'vitest';
import { appendQuestionHint, questionIntent } from './question-hints';

describe('Local question requirements', () => {
  it.each([
    ['今天上传了多少文件？', '统计口径'],
    ['帮我删除数据集', '实际操作需在对应功能页面完成'],
    ['对比这两个版本', '差异'],
    ['系统连接异常', '异常原因'],
    ['文件在哪里下载', '下载入口'],
    ['请查询最近上传的文件', '下载入口'],
    ['如何在数据集中上传文件', '实际操作需在对应功能页面完成'],
    ['有哪些数据集', '数据资产'],
  ])('suggests a qualified intention for %s', (question, expected) => {
    expect(questionIntent(question)?.summary).toContain(expected);
  });
  it('preserves the draft and intent, avoids duplicates and respects the message limit', () => {
    const question = '有哪些数据集';
    const original = questionIntent(question)!;
    const hint = original.hints[0];
    const next = appendQuestionHint(question, hint);
    expect(next.startsWith(question)).toBe(true);
    expect(next).toContain(hint.detail);
    expect(questionIntent(next)?.summary).toBe(original.summary);
    expect(questionIntent(next)?.hints).not.toContainEqual(hint);
    expect(appendQuestionHint(next, hint)).toBe(next);
    expect(appendQuestionHint('问'.repeat(3990), hint)).toHaveLength(3990);
    expect(questionIntent('  ')).toBeUndefined();
  });
});
