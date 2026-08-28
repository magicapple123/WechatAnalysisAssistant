export const BUILT_IN_ANALYSIS_PRESETS = Object.freeze([
  Object.freeze({
    id: 'comprehensive',
    name: '综合分析',
    description: '总结主要内容、核心话题、重要结论和需要关注的信息。',
    strength: 'balanced',
    detail: 'standard',
    requirements: '请对所选内容进行综合分析，概括核心话题、重要事实、关键结论和需要关注的信息。',
  }),
  Object.freeze({
    id: 'sentiment_relationship',
    name: '情绪与关系',
    description: '分析表达情绪、互动方式、关系变化与潜在分歧。',
    strength: 'deep',
    detail: 'detailed',
    requirements: '请分析所选内容中的主要情绪、互动方式、关系变化、共识与潜在分歧，并说明判断依据。',
  }),
  Object.freeze({
    id: 'topics_timeline',
    name: '话题与时间线',
    description: '梳理主要话题、事件顺序、转折点及前后联系。',
    strength: 'balanced',
    detail: 'detailed',
    requirements: '请梳理所选内容中的主要话题和事件时间线，指出重要转折点及事件之间的联系。',
  }),
  Object.freeze({
    id: 'actions_risks',
    name: '行动项与风险',
    description: '提取承诺、待办、决策、未解决问题和潜在风险。',
    strength: 'balanced',
    detail: 'standard',
    requirements: '请提取所选内容中的决策、承诺、待办事项、未解决问题和潜在风险，并给出清晰的行动项。',
  }),
]);

export const ANALYSIS_INTENSITY_OPTIONS = Object.freeze([
  Object.freeze({ value: 'quick', label: '快速', description: '抓取重点，速度更快、消耗更少' }),
  Object.freeze({ value: 'balanced', label: '均衡', description: '兼顾分析质量、速度与模型消耗' }),
  Object.freeze({ value: 'deep', label: '深入', description: '进行更充分的推理和交叉梳理' }),
]);

export const ANALYSIS_DETAIL_OPTIONS = Object.freeze([
  Object.freeze({ value: 'brief', label: '简洁', description: '只保留关键发现' }),
  Object.freeze({ value: 'standard', label: '标准', description: '结论与必要依据并重' }),
  Object.freeze({ value: 'detailed', label: '详细', description: '展开依据、脉络和细节' }),
]);

const VALID_INTENSITIES = new Set(ANALYSIS_INTENSITY_OPTIONS.map((item) => item.value));
const VALID_DETAILS = new Set(ANALYSIS_DETAIL_OPTIONS.map((item) => item.value));

function cleanText(value) {
  return typeof value === 'string' ? value.trim() : '';
}

export function normalizePresetList(presets = []) {
  if (!Array.isArray(presets)) return [];

  const usedIds = new Set();
  const normalized = [];
  presets.forEach((preset, index) => {
    if (!preset || typeof preset !== 'object') return;
    const id = cleanText(preset.id) || `preset-${index + 1}`;
    if (usedIds.has(id)) return;
    usedIds.add(id);
    normalized.push({
      id,
      name: cleanText(preset.name) || `分析预设 ${index + 1}`,
      description: cleanText(preset.description),
      strength: VALID_INTENSITIES.has(preset.strength) ? preset.strength : 'balanced',
      detail: VALID_DETAILS.has(preset.detail) ? preset.detail : 'standard',
      requirements: cleanText(preset.requirements || preset.prompt),
    });
  });
  return normalized;
}

export function encodePresetSelection(kind, id) {
  if (kind === 'adhoc') return 'adhoc';
  const safeKind = kind === 'custom' ? 'custom' : 'builtin';
  return `${safeKind}:${encodeURIComponent(cleanText(id))}`;
}

export function decodePresetSelection(value) {
  if (value === 'adhoc') return { kind: 'adhoc', id: 'custom' };
  const [rawKind, ...idParts] = String(value || '').split(':');
  const kind = rawKind === 'custom' ? 'custom' : 'builtin';
  let id = '';
  try {
    id = decodeURIComponent(idParts.join(':'));
  } catch {
    id = idParts.join(':');
  }
  return { kind, id: cleanText(id) };
}

export function getInitialPresetSelection(
  defaultOptions = {},
  builtInPresets = BUILT_IN_ANALYSIS_PRESETS,
  customPresets = [],
) {
  const builtIns = normalizePresetList(builtInPresets);
  const customs = normalizePresetList(customPresets);
  const requestedKind = defaultOptions?.presetType || defaultOptions?.presetKind;
  const requestedId = cleanText(defaultOptions?.presetId);

  if (requestedKind === 'custom' && customs.some((item) => item.id === requestedId)) {
    return encodePresetSelection('custom', requestedId);
  }
  if (requestedKind === 'builtin' && builtIns.some((item) => item.id === requestedId)) {
    return encodePresetSelection('builtin', requestedId);
  }
  if (requestedKind === 'adhoc' || requestedId === 'custom') return 'adhoc';
  if (builtIns.some((item) => item.id === requestedId)) {
    return encodePresetSelection('builtin', requestedId);
  }
  if (customs.some((item) => item.id === requestedId)) {
    return encodePresetSelection('custom', requestedId);
  }
  if (builtIns.length > 0) return encodePresetSelection('builtin', builtIns[0].id);
  if (customs.length > 0) return encodePresetSelection('custom', customs[0].id);
  return 'adhoc';
}

export function createAnalysisOptions({
  presetSelection,
  builtInPresets = BUILT_IN_ANALYSIS_PRESETS,
  customPresets = [],
  strength = 'balanced',
  detail = 'standard',
  requirements = '',
  thirdPartyConfirmed = false,
} = {}) {
  const builtIns = normalizePresetList(builtInPresets);
  const customs = normalizePresetList(customPresets);
  const decoded = decodePresetSelection(presetSelection);
  const source = decoded.kind === 'custom' ? customs : builtIns;
  const preset = decoded.kind === 'adhoc'
    ? null
    : source.find((item) => item.id === decoded.id) || null;
  const isAdHoc = decoded.kind === 'adhoc' || !preset;
  const supplemental = cleanText(requirements);

  return {
    presetId: isAdHoc ? 'custom' : preset.id,
    presetType: isAdHoc ? 'adhoc' : decoded.kind,
    presetName: isAdHoc ? '自定义分析' : preset.name,
    strength: VALID_INTENSITIES.has(strength) ? strength : 'balanced',
    detail: VALID_DETAILS.has(detail) ? detail : 'standard',
    // Saved preset requirements are resolved again by the local backend.
    // Only send the user's per-run addition here so a stale browser cannot
    // silently override an edited preset.
    requirements: supplemental,
    thirdPartyConfirmed: Boolean(thirdPartyConfirmed),
  };
}

export function sanitizeMarkdownHref(href) {
  const value = cleanText(href);
  if (/^(https?:|mailto:)/i.test(value)) return value;
  return '';
}

export function tokenizeMarkdownInline(text) {
  const source = String(text ?? '');
  const pattern = /(\[[^\]]+\]\([^)]+\)|\*\*[^*\n]+\*\*|__[^_\n]+__|`[^`\n]+`|\*[^*\n]+\*|_[^_\n]+_)/g;
  const tokens = [];
  let cursor = 0;

  for (const match of source.matchAll(pattern)) {
    if (match.index > cursor) tokens.push({ type: 'text', text: source.slice(cursor, match.index) });
    const raw = match[0];
    const link = raw.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
    if (link) {
      const href = sanitizeMarkdownHref(link[2]);
      tokens.push(href
        ? { type: 'link', text: link[1], href }
        : { type: 'text', text: link[1] });
    } else if (raw.startsWith('**') || raw.startsWith('__')) {
      tokens.push({ type: 'strong', text: raw.slice(2, -2) });
    } else if (raw.startsWith('`')) {
      tokens.push({ type: 'code', text: raw.slice(1, -1) });
    } else {
      tokens.push({ type: 'emphasis', text: raw.slice(1, -1) });
    }
    cursor = match.index + raw.length;
  }

  if (cursor < source.length) tokens.push({ type: 'text', text: source.slice(cursor) });
  return tokens;
}

export function parseMarkdownBlocks(markdown) {
  const lines = String(markdown ?? '').replace(/\r\n?/g, '\n').split('\n');
  const blocks = [];
  let paragraph = [];
  let list = null;
  let code = null;

  const flushParagraph = () => {
    if (paragraph.length > 0) {
      blocks.push({ type: 'paragraph', text: paragraph.join('\n') });
      paragraph = [];
    }
  };
  const flushList = () => {
    if (list) {
      blocks.push(list);
      list = null;
    }
  };
  const flushCode = () => {
    if (code) {
      blocks.push({ ...code, text: code.lines.join('\n') });
      code = null;
    }
  };
  const flushText = () => {
    flushParagraph();
    flushList();
  };

  lines.forEach((line) => {
    if (code) {
      if (/^\s*```/.test(line)) flushCode();
      else code.lines.push(line);
      return;
    }

    const fence = line.match(/^\s*```\s*([^\s`]*)/);
    if (fence) {
      flushText();
      code = { type: 'code', language: cleanText(fence[1]), lines: [] };
      return;
    }
    if (!line.trim()) {
      flushText();
      return;
    }

    const heading = line.match(/^\s*(#{1,6})\s+(.+)$/);
    if (heading) {
      flushText();
      blocks.push({ type: 'heading', level: heading[1].length, text: heading[2].trim() });
      return;
    }
    if (/^\s*(?:---+|___+|\*\*\*+)\s*$/.test(line)) {
      flushText();
      blocks.push({ type: 'divider' });
      return;
    }

    const unordered = line.match(/^\s*[-+*]\s+(.+)$/);
    const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (unordered || ordered) {
      flushParagraph();
      const orderedList = Boolean(ordered);
      if (!list || list.ordered !== orderedList) {
        flushList();
        list = { type: 'list', ordered: orderedList, items: [] };
      }
      list.items.push((unordered || ordered)[1].trim());
      return;
    }

    const quote = line.match(/^\s*>\s?(.*)$/);
    if (quote) {
      flushText();
      blocks.push({ type: 'quote', text: quote[1].trim() });
      return;
    }

    flushList();
    paragraph.push(line.trim());
  });

  flushText();
  flushCode();
  return blocks;
}
