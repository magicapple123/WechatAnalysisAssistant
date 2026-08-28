import { useEffect, useMemo, useRef, useState } from 'react';

import {
  ANALYSIS_DETAIL_OPTIONS,
  ANALYSIS_INTENSITY_OPTIONS,
  BUILT_IN_ANALYSIS_PRESETS,
  createAnalysisOptions,
  decodePresetSelection,
  encodePresetSelection,
  getInitialPresetSelection,
  normalizePresetList,
  parseMarkdownBlocks,
  tokenizeMarkdownInline,
} from './aiAnalysisDialogUtils';

function InlineMarkdown({ text }) {
  return tokenizeMarkdownInline(text).map((token, index) => {
    const key = `${index}-${token.type}`;
    if (token.type === 'strong') return <strong key={key}>{token.text}</strong>;
    if (token.type === 'emphasis') return <em key={key}>{token.text}</em>;
    if (token.type === 'code') {
      return <code key={key} className="rounded bg-gray-100 px-1 py-0.5 font-mono text-[0.92em] text-gray-800">{token.text}</code>;
    }
    if (token.type === 'link') {
      return (
        <a
          key={key}
          href={token.href}
          target="_blank"
          rel="noreferrer noopener"
          className="text-blue-600 underline decoration-blue-300 underline-offset-2 hover:text-blue-800"
        >
          {token.text}
        </a>
      );
    }
    return <span key={key}>{token.text}</span>;
  });
}

function MarkdownPreview({ markdown }) {
  const blocks = useMemo(() => parseMarkdownBlocks(markdown), [markdown]);
  return (
    <div className="space-y-3 text-sm leading-6 text-gray-700">
      {blocks.map((block, index) => {
        const key = `${index}-${block.type}`;
        if (block.type === 'heading') {
          const sizes = ['text-xl', 'text-lg', 'text-base', 'text-sm', 'text-sm', 'text-sm'];
          return (
            <div key={key} className={`${sizes[block.level - 1]} font-semibold text-gray-900`}>
              <InlineMarkdown text={block.text} />
            </div>
          );
        }
        if (block.type === 'list') {
          const List = block.ordered ? 'ol' : 'ul';
          return (
            <List key={key} className={`${block.ordered ? 'list-decimal' : 'list-disc'} space-y-1 pl-5`}>
              {block.items.map((item, itemIndex) => (
                <li key={`${itemIndex}-${item.slice(0, 16)}`}><InlineMarkdown text={item} /></li>
              ))}
            </List>
          );
        }
        if (block.type === 'quote') {
          return (
            <blockquote key={key} className="border-l-4 border-green-300 bg-green-50 px-3 py-2 text-gray-600">
              <InlineMarkdown text={block.text} />
            </blockquote>
          );
        }
        if (block.type === 'code') {
          return (
            <pre key={key} className="overflow-x-auto rounded-lg bg-gray-900 p-3 text-xs leading-5 text-gray-100">
              <code>{block.text}</code>
            </pre>
          );
        }
        if (block.type === 'divider') return <hr key={key} className="border-gray-200" />;
        return (
          <p key={key} className="whitespace-pre-wrap">
            <InlineMarkdown text={block.text} />
          </p>
        );
      })}
    </div>
  );
}

function OptionCards({ name, value, options, onChange, disabled }) {
  return (
    <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
      {options.map((option) => (
        <label
          key={option.value}
          className={`rounded-lg border px-3 py-2.5 transition-colors ${
            value === option.value
              ? 'border-wechat-green bg-green-50 ring-1 ring-wechat-green/20'
              : 'border-gray-200 bg-white hover:bg-gray-50'
          } ${disabled ? 'cursor-not-allowed opacity-60' : 'cursor-pointer'}`}
        >
          <span className="flex items-center gap-2">
            <input
              type="radio"
              name={name}
              value={option.value}
              checked={value === option.value}
              disabled={disabled}
              onChange={() => onChange(option.value)}
              className="accent-wechat-green"
            />
            <span className="text-sm font-medium text-gray-800">{option.label}</span>
          </span>
          <span className="mt-1 block pl-5 text-xs leading-4 text-gray-500">{option.description}</span>
        </label>
      ))}
    </div>
  );
}

/**
 * 通用 AI 文本分析对话框，可同时用于聊天记录和朋友圈。
 *
 * onConfirm(options) 的 options 包含：
 * presetId、presetType（builtin/custom/adhoc）、presetName、
 * strength（quick/balanced/deep）、detail（brief/standard/detailed）、
 * requirements、thirdPartyConfirmed。
 *
 * onDownload({ markdown, options }) 在用户点击“下载报告”时调用。
 */
export default function AiAnalysisDialog({
  title = 'AI 内容分析',
  contentLabel = '所选内容',
  scopeSummary = '当前所选范围',
  providerLabel = '',
  builtInPresets = BUILT_IN_ANALYSIS_PRESETS,
  customPresets = [],
  defaultOptions = {},
  running = false,
  runningText = '正在生成分析报告…',
  error = '',
  warning = '',
  reportMarkdown = '',
  reportTitle = '分析报告',
  onConfirm,
  onDownload,
  onCancel,
  onClose,
}) {
  const dialogRef = useRef(null);
  const previousFocusRef = useRef(null);
  const busyRef = useRef(false);
  const onCloseRef = useRef(onClose);
  const normalizedBuiltIns = useMemo(() => normalizePresetList(builtInPresets), [builtInPresets]);
  const normalizedCustoms = useMemo(() => normalizePresetList(customPresets), [customPresets]);
  const initialPresetSelection = useMemo(() => getInitialPresetSelection(
    defaultOptions,
    normalizedBuiltIns,
    normalizedCustoms,
  ), [defaultOptions, normalizedBuiltIns, normalizedCustoms]);
  const initialPreset = useMemo(() => {
    const decoded = decodePresetSelection(initialPresetSelection);
    const presets = decoded.kind === 'custom' ? normalizedCustoms : normalizedBuiltIns;
    return presets.find((item) => item.id === decoded.id) || null;
  }, [initialPresetSelection, normalizedBuiltIns, normalizedCustoms]);
  const [presetSelection, setPresetSelection] = useState(initialPresetSelection);
  const [strength, setStrength] = useState(
    ['quick', 'balanced', 'deep'].includes(defaultOptions?.strength)
      ? defaultOptions.strength
      : (initialPreset?.strength || 'balanced'),
  );
  const [detail, setDetail] = useState(
    ['brief', 'standard', 'detailed'].includes(defaultOptions?.detail)
      ? defaultOptions.detail
      : (initialPreset?.detail || 'standard'),
  );
  const [requirements, setRequirements] = useState(defaultOptions?.requirements || '');
  const [thirdPartyConfirmed, setThirdPartyConfirmed] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [localError, setLocalError] = useState('');

  const busy = running || submitting;
  const markdown = typeof reportMarkdown === 'string' ? reportMarkdown.trim() : '';
  const decodedSelection = decodePresetSelection(presetSelection);
  const selectedPreset = decodedSelection.kind === 'builtin'
    ? normalizedBuiltIns.find((item) => item.id === decodedSelection.id)
    : decodedSelection.kind === 'custom'
      ? normalizedCustoms.find((item) => item.id === decodedSelection.id)
      : null;
  const isAdHoc = decodedSelection.kind === 'adhoc' || !selectedPreset;

  useEffect(() => {
    busyRef.current = busy;
  }, [busy]);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    previousFocusRef.current = document.activeElement;
    const focusTimer = window.setTimeout(() => dialogRef.current?.focus(), 0);
    const handleKeyDown = (event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        if (!busyRef.current) onCloseRef.current?.();
        return;
      }
      if (event.key !== 'Tab' || !dialogRef.current) return;
      const focusable = [...dialogRef.current.querySelectorAll(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
      )].filter((element) => (
        !element.hasAttribute('hidden') && element.getClientRects().length > 0
      ));
      if (focusable.length === 0) {
        event.preventDefault();
        dialogRef.current.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const current = document.activeElement;
      const focusIsOutside = !dialogRef.current.contains(current);
      if (
        event.shiftKey
        && (current === first || current === dialogRef.current || focusIsOutside)
      ) {
        event.preventDefault();
        last.focus();
      } else if (
        !event.shiftKey
        && (current === last || current === dialogRef.current || focusIsOutside)
      ) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => {
      window.clearTimeout(focusTimer);
      document.removeEventListener('keydown', handleKeyDown);
      previousFocusRef.current?.focus?.();
    };
  }, []);

  const currentOptions = useMemo(() => createAnalysisOptions({
    presetSelection,
    builtInPresets: normalizedBuiltIns,
    customPresets: normalizedCustoms,
    strength,
    detail,
    requirements,
    thirdPartyConfirmed,
  }), [
    requirements,
    detail,
    strength,
    normalizedBuiltIns,
    normalizedCustoms,
    presetSelection,
    thirdPartyConfirmed,
  ]);

  const handleConfirm = async () => {
    setLocalError('');
    if (typeof onConfirm !== 'function') {
      setLocalError('当前页面尚未配置分析操作');
      return;
    }
    if (isAdHoc && !requirements.trim()) {
      setLocalError('选择“自定义分析”时，请填写具体的分析要求');
      return;
    }
    if (!thirdPartyConfirmed) {
      setLocalError('请先确认已知晓所选文本会发送到第三方模型');
      return;
    }

    setSubmitting(true);
    try {
      await onConfirm(currentOptions);
    } catch (submitError) {
      setLocalError(submitError?.message || '创建分析任务失败，请重试');
    } finally {
      setSubmitting(false);
    }
  };

  const handleDownload = async () => {
    if (!markdown || typeof onDownload !== 'function') return;
    setLocalError('');
    try {
      await onDownload({ markdown, options: currentOptions });
    } catch (downloadError) {
      setLocalError(downloadError?.message || '下载报告失败，请重试');
    }
  };

  return (
    <div
      className="fixed inset-0 z-[70] flex items-center justify-center bg-slate-950/45 p-0 backdrop-blur-[2px] sm:p-4"
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="ai-analysis-dialog-title"
        tabIndex={-1}
        className="flex h-full max-h-none w-full flex-col overflow-hidden rounded-none border-0 border-slate-200 bg-white shadow-2xl outline-none sm:h-auto sm:max-h-[92vh] sm:max-w-3xl sm:rounded-2xl sm:border"
      >
        <div className="flex flex-shrink-0 items-start justify-between gap-4 border-b border-slate-200 bg-white px-5 py-4 sm:px-6">
          <div>
            <h2 id="ai-analysis-dialog-title" className="text-lg font-semibold text-slate-900">{title}</h2>
            <p className="mt-1 text-xs leading-5 text-slate-500">配置分析方式，确认后再发送所选文本</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            className="rounded-lg px-2 py-1 text-xl leading-none text-slate-400 transition-colors hover:bg-slate-100 hover:text-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
            aria-label="关闭 AI 分析对话框"
          >
            ×
          </button>
        </div>

        <div className="min-h-0 flex-1 space-y-5 overflow-y-auto bg-slate-50/70 p-4 sm:p-6">
          <section className="rounded-xl border border-slate-200 bg-white px-4 py-3">
            <div className="text-xs font-medium uppercase tracking-wide text-gray-400">分析范围</div>
            <div className="mt-1 text-sm font-medium text-gray-800">{scopeSummary || '当前所选范围'}</div>
          </section>

          <section>
            <label htmlFor="ai-analysis-preset" className="mb-2 block text-sm font-semibold text-gray-800">
              分析预设
            </label>
            <select
              id="ai-analysis-preset"
              value={presetSelection}
              disabled={busy}
              onChange={(event) => {
                setPresetSelection(event.target.value);
                const decoded = decodePresetSelection(event.target.value);
                const presets = decoded.kind === 'custom' ? normalizedCustoms : normalizedBuiltIns;
                const preset = presets.find((item) => item.id === decoded.id);
                if (preset) {
                  setStrength(preset.strength);
                  setDetail(preset.detail);
                }
                setLocalError('');
              }}
              className="w-full rounded-lg border border-gray-200 bg-white px-3 py-2.5 text-sm text-gray-700 outline-none focus:border-wechat-green focus:ring-2 focus:ring-green-100 disabled:bg-gray-100"
            >
              {normalizedBuiltIns.length > 0 && (
                <optgroup label="内置预设">
                  {normalizedBuiltIns.map((preset) => (
                    <option key={preset.id} value={encodePresetSelection('builtin', preset.id)}>{preset.name}</option>
                  ))}
                </optgroup>
              )}
              {normalizedCustoms.length > 0 && (
                <optgroup label="自定义预设">
                  {normalizedCustoms.map((preset) => (
                    <option key={preset.id} value={encodePresetSelection('custom', preset.id)}>{preset.name}</option>
                  ))}
                </optgroup>
              )}
              <option value="adhoc">自定义分析要求…</option>
            </select>
            {(selectedPreset?.description || isAdHoc) && (
              <p className="mt-1.5 text-xs leading-5 text-gray-500">
                {selectedPreset?.description || '不使用固定预设，完全按照你填写的分析要求生成报告。'}
              </p>
            )}
          </section>

          <section>
            <div className="mb-2 text-sm font-semibold text-gray-800">分析强度</div>
            <OptionCards
              name="ai-analysis-strength"
              value={strength}
              options={ANALYSIS_INTENSITY_OPTIONS}
              onChange={setStrength}
              disabled={busy}
            />
          </section>

          <section>
            <div className="mb-2 text-sm font-semibold text-gray-800">报告详细程度</div>
            <OptionCards
              name="ai-analysis-detail"
              value={detail}
              options={ANALYSIS_DETAIL_OPTIONS}
              onChange={setDetail}
              disabled={busy}
            />
          </section>

          <section>
            <label htmlFor="ai-analysis-instructions" className="mb-2 block text-sm font-semibold text-gray-800">
              {isAdHoc ? '自定义分析要求（必填）' : '补充分析要求（可选）'}
            </label>
            <textarea
              id="ai-analysis-instructions"
              value={requirements}
              disabled={busy}
              maxLength={4000}
              rows={4}
              onChange={(event) => {
                setRequirements(event.target.value);
                setLocalError('');
              }}
              placeholder={isAdHoc
                ? '例如：重点分析过去一个月的话题变化，并列出有日期依据的重要事件。'
                : '例如：忽略日常寒暄，重点关注工作安排和未完成事项。'}
              className="w-full resize-y rounded-lg border border-gray-200 px-3 py-2.5 text-sm leading-6 text-gray-700 outline-none focus:border-wechat-green focus:ring-2 focus:ring-green-100 disabled:bg-gray-100"
            />
            <div className="mt-1 text-right text-xs text-gray-400">{requirements.length} / 4000</div>
          </section>

          <section className="rounded-xl border border-amber-300 bg-amber-50 px-4 py-3">
            <div className="flex items-start gap-2 text-sm font-semibold text-amber-900">
              <span aria-hidden="true">⚠️</span>
              <span>第三方模型数据发送提示</span>
            </div>
            <p className="mt-1.5 text-xs leading-5 text-amber-800">
              {contentLabel}中的文本将发送到
              {providerLabel ? `“${providerLabel}”` : '你在设置中配置的第三方模型服务商'}进行分析。
              请确认所选范围不包含你不希望上传的隐私或敏感信息；模型服务商可能按其隐私政策处理这些数据。
            </p>
            <label className={`mt-3 flex items-start gap-2 text-xs text-amber-900 ${busy ? 'cursor-not-allowed opacity-60' : 'cursor-pointer'}`}>
              <input
                type="checkbox"
                checked={thirdPartyConfirmed}
                disabled={busy}
                onChange={(event) => {
                  setThirdPartyConfirmed(event.target.checked);
                  setLocalError('');
                }}
                className="mt-0.5 accent-wechat-green"
              />
              <span>我已知晓并同意将上述范围内的文本发送到第三方模型服务商</span>
            </label>
          </section>

          {busy && (
            <section className="flex items-center gap-3 rounded-xl border border-indigo-200 bg-indigo-50 px-4 py-3 text-sm text-indigo-800">
              <span className="h-4 w-4 flex-shrink-0 animate-spin rounded-full border-2 border-indigo-500 border-t-transparent" />
              <span>{running ? runningText : '正在提交分析任务…'}</span>
            </section>
          )}

          {(error || localError) && (
            <section role="alert" className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
              {localError || error}
            </section>
          )}

          {warning && (
            <section role="status" className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
              {warning}
            </section>
          )}

          {markdown && (
            <section className="overflow-hidden rounded-xl border border-green-200 bg-white">
              <div className="flex items-center justify-between gap-3 border-b border-green-100 bg-green-50 px-4 py-3">
                <div>
                  <div className="flex items-center gap-2 text-sm font-semibold text-green-800">
                    <span aria-hidden="true">✓</span>
                    <span>生成成功</span>
                  </div>
                  <div className="mt-0.5 text-xs text-green-700">{reportTitle}</div>
                </div>
                {typeof onDownload === 'function' && (
                  <button
                    type="button"
                    onClick={handleDownload}
                    disabled={busy}
                    className="rounded-lg border border-green-300 bg-white px-3 py-1.5 text-xs font-medium text-green-700 hover:bg-green-100 disabled:opacity-50"
                  >
                    下载报告
                  </button>
                )}
              </div>
              <div className="max-h-[45vh] overflow-y-auto p-4 sm:p-5">
                <MarkdownPreview markdown={markdown} />
              </div>
            </section>
          )}
        </div>

        <div className="flex flex-shrink-0 flex-wrap items-center justify-end gap-3 border-t border-slate-200 bg-white px-4 py-3 sm:px-6">
          {busy && typeof onCancel === 'function' && (
            <button
              type="button"
              onClick={onCancel}
              className="rounded-lg border border-red-200 bg-white px-4 py-2 text-sm font-medium text-red-600 hover:bg-red-50"
            >
              停止分析
            </button>
          )}
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            className="rounded-lg px-4 py-2 text-sm text-gray-600 hover:bg-gray-100 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {markdown ? '关闭' : '取消'}
          </button>
          <button
            type="button"
            onClick={handleConfirm}
            disabled={busy || !thirdPartyConfirmed || (isAdHoc && !requirements.trim())}
            className="flex items-center gap-2 rounded-lg bg-wechat-green px-5 py-2 text-sm font-medium text-white hover:bg-wechat-green-dark disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy && <span className="h-4 w-4 animate-spin rounded-full border-2 border-white border-t-transparent" />}
            {busy ? '分析中…' : markdown ? '重新生成' : '确认并开始分析'}
          </button>
        </div>
      </div>
    </div>
  );
}
