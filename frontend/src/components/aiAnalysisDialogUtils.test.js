import assert from 'node:assert/strict';
import test from 'node:test';

import {
  createAnalysisOptions,
  getInitialPresetSelection,
  normalizePresetList,
  parseMarkdownBlocks,
  sanitizeMarkdownHref,
  tokenizeMarkdownInline,
} from './aiAnalysisDialogUtils.js';

test('normalizePresetList cleans input and removes duplicate ids', () => {
  assert.deepEqual(normalizePresetList([
    { id: ' focus ', name: ' 重点 ', requirements: ' prompt ', strength: 'deep', detail: 'detailed' },
    { id: 'focus', name: '重复项' },
    null,
    { name: '自动编号' },
  ]), [
    { id: 'focus', name: '重点', description: '', strength: 'deep', detail: 'detailed', requirements: 'prompt' },
    { id: 'preset-4', name: '自动编号', description: '', strength: 'balanced', detail: 'standard', requirements: '' },
  ]);
});

test('getInitialPresetSelection supports built-in, saved custom and ad-hoc presets', () => {
  const builtIns = [{ id: 'summary', name: '总结' }];
  const customs = [{ id: 'work', name: '工作复盘' }];

  assert.equal(getInitialPresetSelection({}, builtIns, customs), 'builtin:summary');
  assert.equal(
    getInitialPresetSelection({ presetType: 'custom', presetId: 'work' }, builtIns, customs),
    'custom:work',
  );
  assert.equal(
    getInitialPresetSelection({ presetType: 'adhoc', presetId: 'custom' }, builtIns, customs),
    'adhoc',
  );
});

test('createAnalysisOptions returns normalized callback payload', () => {
  const options = createAnalysisOptions({
    presetSelection: 'custom:work',
    builtInPresets: [],
    customPresets: [{ id: 'work', name: '工作复盘', requirements: '提取工作事项' }],
    strength: 'deep',
    detail: 'detailed',
    requirements: '  只看最近一周  ',
    thirdPartyConfirmed: true,
  });

  assert.deepEqual(options, {
    presetId: 'work',
    presetType: 'custom',
    presetName: '工作复盘',
    strength: 'deep',
    detail: 'detailed',
    requirements: '只看最近一周',
    thirdPartyConfirmed: true,
  });
});

test('createAnalysisOptions falls back to safe option values', () => {
  const options = createAnalysisOptions({
    presetSelection: 'adhoc',
    strength: 'unknown',
    detail: 'unknown',
  });
  assert.equal(options.presetId, 'custom');
  assert.equal(options.presetType, 'adhoc');
  assert.equal(options.strength, 'balanced');
  assert.equal(options.detail, 'standard');
});

test('Markdown parser groups lists and preserves code blocks', () => {
  const blocks = parseMarkdownBlocks(`# 标题\n\n- 第一项\n- 第二项\n\n> 结论\n\n\`\`\`json\n{"ok": true}\n\`\`\``);
  assert.deepEqual(blocks, [
    { type: 'heading', level: 1, text: '标题' },
    { type: 'list', ordered: false, items: ['第一项', '第二项'] },
    { type: 'quote', text: '结论' },
    { type: 'code', language: 'json', lines: ['{"ok": true}'], text: '{"ok": true}' },
  ]);
});

test('inline Markdown allows safe links and drops unsafe hrefs', () => {
  assert.equal(sanitizeMarkdownHref('https://example.com/report'), 'https://example.com/report');
  assert.equal(sanitizeMarkdownHref('javascript:alert(1)'), '');

  const tokens = tokenizeMarkdownInline('**重点** [安全](https://example.com) [危险](javascript:alert(1))');
  assert.ok(tokens.some((token) => token.type === 'strong' && token.text === '重点'));
  assert.ok(tokens.some((token) => token.type === 'link' && token.href === 'https://example.com'));
  assert.ok(!tokens.some((token) => token.type === 'link' && token.href.startsWith('javascript:')));
});
