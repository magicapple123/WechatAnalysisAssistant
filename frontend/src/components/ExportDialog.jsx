import { useState, useEffect } from 'react';
import api from '../api';
import useDialogFocus from '../hooks/useDialogFocus';

/**
 * 导出对话框
 *
 * 选择导出格式和时间范围:
 * - HTML: 美观的聊天记录网页，可直接在浏览器查看
 * - JSON: 结构化数据，方便二次开发
 * - CSV: 表格格式，可用 Excel 打开
 * - TXT: 纯文本格式
 *
 * Props:
 * - chatName: 聊天名称
 * - selectedCount: 已选消息数量 (0 = 导出全部)
 * - onConfirm(format, options): 确认回调，options = { startTime, endTime }
 * - onClose: 关闭对话框
 */
const FORMATS = [
  {
    value: 'html',
    label: 'HTML 网页',
    desc: '美观的聊天记录网页，包含气泡样式，可直接在浏览器中查看',
  },
  {
    value: 'json',
    label: 'JSON 数据',
    desc: '结构化 JSON 格式，方便程序处理和二次开发',
  },
  {
    value: 'csv',
    label: 'CSV 表格',
    desc: '表格格式，可用 Excel / WPS 打开',
  },
  {
    value: 'txt',
    label: 'TXT 文本',
    desc: '纯文本格式，通用性最好',
  },
];

export default function ExportDialog({ chatName, talker, selectedCount = 0, selectedTimeRange = null, onConfirm, onClose }) {
  const [selected, setSelected] = useState('html');
  const [exporting, setExporting] = useState(false);
  const [startDate, setStartDate] = useState('');
  const [startTime, setStartTime] = useState('');
  const [endDate, setEndDate] = useState('');
  const [endTime, setEndTime] = useState('');
  const [useTimeRange, setUseTimeRange] = useState(false);
  const [chatTimeRange, setChatTimeRange] = useState(null);  // 聊天实际时间范围
  const [replaceImagesWithDescriptions, setReplaceImagesWithDescriptions] = useState(false);
  const [replaceVoicesWithTranscriptions, setReplaceVoicesWithTranscriptions] = useState(true);
  const [embedImages, setEmbedImages] = useState(true);
  const [htmlImageQuality, setHtmlImageQuality] = useState('best');
  const [validationError, setValidationError] = useState('');
  const dialogRef = useDialogFocus({ onClose, closeDisabled: exporting });

  // 获取聊天的实际消息时间范围
  useEffect(() => {
    let active = true;
    if (talker && !useTimeRange && !selectedTimeRange) {
      api.getChatTimeRange(talker).then((res) => {
        if (active && res.start_time != null && res.end_time != null) {
          setChatTimeRange({ startTime: res.start_time, endTime: res.end_time });
        }
      }).catch(() => {});
    }
    return () => {
      active = false;
    };
  }, [talker, useTimeRange, selectedTimeRange]);

  // 生成默认文件名（仅用于占位符显示）
  const getDefaultFilename = () => {
    let name = chatName || '聊天记录';
    if (useTimeRange && startDate) {
      const s = startDate;
      const e = endDate || startDate;
      name = `${s}至${e}_${name}`;
    } else if (selectedTimeRange) {
      const s = new Date(selectedTimeRange.startTime * 1000);
      const e = new Date(selectedTimeRange.endTime * 1000);
      const ds = `${s.getFullYear()}-${String(s.getMonth()+1).padStart(2,'0')}-${String(s.getDate()).padStart(2,'0')}`;
      const de = `${e.getFullYear()}-${String(e.getMonth()+1).padStart(2,'0')}-${String(e.getDate()).padStart(2,'0')}`;
      name = `${ds}至${de}_${name}`;
    } else if (chatTimeRange) {
      const s = new Date(chatTimeRange.startTime * 1000);
      const e = new Date(chatTimeRange.endTime * 1000);
      const ds = `${s.getFullYear()}-${String(s.getMonth()+1).padStart(2,'0')}-${String(s.getDate()).padStart(2,'0')}`;
      const de = `${e.getFullYear()}-${String(e.getMonth()+1).padStart(2,'0')}-${String(e.getDate()).padStart(2,'0')}`;
      name = `${ds}至${de}_${name}`;
    } else {
      name = `${name}`;
    }
    return name;
  };
  const [filename, setFilename] = useState('');  // 空=使用默认名

  const getSubtitle = () => {
    const parts = [];
    if (selectedCount > 0) {
      parts.push(`已选 ${selectedCount} 条消息`);
    }
    if (parts.length === 0) {
      parts.push('选择导出格式');
    }
    return parts.join(' · ');
  };

  const handleExport = async () => {
    setValidationError('');
    const options = {};
    if (useTimeRange && !startDate && !endDate) {
      setValidationError('请至少填写开始日期或结束日期');
      return;
    }
    try {
      if (useTimeRange && startDate) {
        const startDateTime = startTime
          ? new Date(`${startDate}T${startTime}:00`)
          : new Date(`${startDate}T00:00:00`);
        options.startTime = Math.floor(startDateTime.getTime() / 1000);
      }
      if (useTimeRange && endDate) {
        const endDateTime = endTime
          ? new Date(`${endDate}T${endTime}:59`)
          : new Date(`${endDate}T23:59:59`);
        options.endTime = Math.floor(endDateTime.getTime() / 1000);
      }
      if (
        options.startTime != null
        && options.endTime != null
        && options.startTime > options.endTime
      ) {
        setValidationError('结束时间不能早于开始时间');
        return;
      }
      // 只在用户输入了自定义名称时才发送
      if (filename.trim()) {
        options.filename = filename.trim();
      }
      options.replaceImagesWithDescriptions = replaceImagesWithDescriptions;
      options.replaceVoicesWithTranscriptions = replaceVoicesWithTranscriptions;
      options.embedImages = selected === 'html' && embedImages;
      options.htmlImageQuality = htmlImageQuality;
      setExporting(true);
      await onConfirm(selected, options);
    } catch (e) {
      setValidationError(e?.message || '导出失败，请重试');
    } finally {
      setExporting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/45 p-0 backdrop-blur-[2px] sm:p-4">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="export-dialog-title"
        aria-busy={exporting}
        tabIndex={-1}
        className="flex h-[100dvh] w-full max-w-lg flex-col overflow-hidden bg-white shadow-[0_24px_80px_rgba(15,23,42,0.24)] outline-none sm:h-auto sm:max-h-[92dvh] sm:rounded-2xl sm:border sm:border-slate-200/80"
      >
        {/* 标题 */}
        <div className="flex flex-shrink-0 items-start justify-between gap-4 border-b border-slate-200/80 bg-white px-5 py-4 sm:px-6">
          <div className="min-w-0">
            <h2 id="export-dialog-title" className="truncate text-lg font-semibold tracking-tight text-slate-900">
              {chatName ? `导出：${chatName}` : '导出所有聊天记录'}
            </h2>
            <p className="mt-1 text-sm text-slate-400">{getSubtitle()}</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={exporting}
            className="app-icon-button"
            title="关闭导出设置"
            aria-label="关闭导出设置"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
              <path d="m6 6 12 12M18 6 6 18" />
            </svg>
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto bg-slate-50/50">

        {validationError && (
          <div role="alert" className="mx-4 mt-4 rounded-xl border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
            {validationError}
          </div>
        )}

        {/* 时间范围 (可选) */}
        {talker && <div className="px-4 pt-4">
          <label className="flex items-center gap-2 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={useTimeRange}
              onChange={(e) => setUseTimeRange(e.target.checked)}
              className="accent-wechat-green"
            />
            <span className="text-sm font-medium text-gray-700">指定时间范围</span>
            <span className="text-xs text-gray-400">（不勾选则导出全部）</span>
          </label>

          {useTimeRange && (
            <div className="mt-3 bg-gray-50 rounded-lg p-3 space-y-3">
              {/* 开始时间 */}
              <div>
                <label className="text-xs text-gray-500 mb-1 block">开始时间</label>
                <div className="flex gap-2">
                  <input
                    type="date"
                    value={startDate}
                    onChange={(e) => setStartDate(e.target.value)}
                    className="flex-1 border border-gray-200 rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:border-wechat-green"
                  />
                  <input
                    type="time"
                    value={startTime}
                    onChange={(e) => setStartTime(e.target.value)}
                    className="w-28 border border-gray-200 rounded-lg px-2 py-1.5 text-sm focus:outline-none focus:border-wechat-green"
                  />
                </div>
              </div>
              {/* 结束时间 */}
              <div>
                <label className="text-xs text-gray-500 mb-1 block">结束时间</label>
                <div className="flex gap-2">
                  <input
                    type="date"
                    value={endDate}
                    onChange={(e) => setEndDate(e.target.value)}
                    className="flex-1 border border-gray-200 rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:border-wechat-green"
                  />
                  <input
                    type="time"
                    value={endTime}
                    onChange={(e) => setEndTime(e.target.value)}
                    className="w-28 border border-gray-200 rounded-lg px-2 py-1.5 text-sm focus:outline-none focus:border-wechat-green"
                  />
                </div>
              </div>
            </div>
          )}
        </div>}

        {/* 图片消息导出方式 */}
        <div className="px-4 pt-3 space-y-2">
          {selected === 'html' && (
            <div className="space-y-3 rounded-xl border border-slate-200 bg-white px-3 py-3">
              <label className="flex items-start gap-2 cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={embedImages}
                  onChange={(event) => {
                    const checked = event.target.checked;
                    setEmbedImages(checked);
                    if (checked) setReplaceImagesWithDescriptions(false);
                  }}
                  className="mt-0.5 accent-wechat-green"
                />
                <span>
                  <span className="block text-sm font-medium text-gray-700">将聊天图片嵌入 HTML</span>
                  <span className="block text-xs text-gray-500 mt-0.5">默认开启，图片直接写入单个 HTML 文件，无需额外图片目录。</span>
                </span>
              </label>
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">嵌入图片清晰度</label>
                <select
                  value={htmlImageQuality}
                  onChange={(event) => setHtmlImageQuality(event.target.value)}
                  disabled={!embedImages}
                  className="w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm focus:outline-none focus:border-wechat-green disabled:bg-gray-100 disabled:text-gray-400"
                >
                  <option value="best">高清优先（推荐，缺失时自动降级）</option>
                  <option value="thumbnail">缩略图（文件更小、导出更快）</option>
                </select>
                {embedImages && htmlImageQuality === 'best' && (
                  <p className="mt-1 text-xs text-amber-600">大量高清图片会显著增大 HTML 文件体积。</p>
                )}
              </div>
            </div>
          )}
          <label className="flex cursor-pointer select-none items-start gap-2 rounded-xl border border-slate-200 bg-white px-3 py-2.5">
            <input
              type="checkbox"
              checked={replaceImagesWithDescriptions}
              onChange={(event) => {
                const checked = event.target.checked;
                setReplaceImagesWithDescriptions(checked);
                if (checked) setEmbedImages(false);
              }}
              className="mt-0.5 accent-wechat-green"
            />
            <span>
              <span className="block text-sm font-medium text-gray-700">
                用图片描述替换图片消息
              </span>
              <span className="block text-xs text-gray-500 mt-0.5">
                开启后不嵌入图片；已识别图片导出为文字描述，未识别图片仍显示为 [图片]
              </span>
            </span>
          </label>
        </div>

        {/* 语音消息导出方式 */}
        <div className="px-4 pt-3">
          <label className="flex cursor-pointer select-none items-start gap-2 rounded-xl border border-slate-200 bg-white px-3 py-2.5">
            <input
              type="checkbox"
              checked={replaceVoicesWithTranscriptions}
              onChange={(event) => setReplaceVoicesWithTranscriptions(event.target.checked)}
              className="mt-0.5 accent-wechat-green"
            />
            <span>
              <span className="block text-sm font-medium text-gray-700">
                用转写文字替换语音消息
              </span>
              <span className="block text-xs text-gray-500 mt-0.5">
                HTML、TXT 和 CSV 会将已转写语音导出为 [语音转文字]；未转写语音保持原占位文本。JSON 始终保留独立的转写字段。
              </span>
            </span>
          </label>
        </div>

        {/* 文件名 */}
        {talker && <div className="px-4 pb-2">
          <label className="text-xs text-gray-400 font-medium mb-1 block">文件名</label>
          <input
            type="text"
            value={filename}
            onChange={(e) => setFilename(e.target.value)}
            placeholder={getDefaultFilename()}
            className="w-full border border-gray-200 rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:border-wechat-green"
          />
          <p className="text-xs text-gray-400 mt-0.5">可自定义，不填则使用默认名称</p>
        </div>}

        {/* 格式选项 */}
        <div className="p-4 space-y-2">
          <p className="text-xs text-gray-400 font-medium">导出格式</p>
          {FORMATS.map((fmt) => (
            <label
              key={fmt.value}
              className={`flex items-start gap-3 p-3 rounded-lg border-2 cursor-pointer transition-all ${
                selected === fmt.value
                  ? 'border-wechat-green bg-green-50'
                  : 'border-gray-200 hover:border-gray-300'
              }`}
            >
              <input
                type="radio"
                name="export-format"
                value={fmt.value}
                checked={selected === fmt.value}
                onChange={() => setSelected(fmt.value)}
                className="mt-0.5 accent-wechat-green"
              />
              <div className="flex-1">
                <div className="flex items-center gap-2">
                  <span className="inline-flex h-7 min-w-10 items-center justify-center rounded-lg border border-slate-200 bg-slate-50 px-1.5 text-[10px] font-bold tracking-wide text-slate-500">
                    {fmt.value.toUpperCase()}
                  </span>
                  <span className="font-medium text-gray-800">{fmt.label}</span>
                </div>
                <p className="text-xs text-gray-500 mt-1">{fmt.desc}</p>
              </div>
            </label>
          ))}
        </div>

        </div>

        {/* 固定在弹窗边框底部的操作栏 */}
        <div className="flex flex-shrink-0 items-center justify-end gap-3 border-t border-slate-200/80 bg-white/95 px-5 py-4 backdrop-blur sm:px-6">
          <button
            type="button"
            onClick={onClose}
            disabled={exporting}
            className="rounded-xl px-4 py-2.5 text-sm font-medium text-slate-500 transition-colors hover:bg-slate-100 hover:text-slate-800 disabled:opacity-50"
          >
            取消
          </button>
          <button
            type="button"
            onClick={handleExport}
            disabled={exporting}
            className="flex items-center gap-2 rounded-xl bg-wechat-green px-6 py-2.5 text-sm font-medium text-white transition-colors hover:bg-wechat-green-dark disabled:cursor-not-allowed disabled:opacity-50"
          >
            {exporting ? (
              <>
                <div className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
                导出中...
              </>
            ) : (
              '开始导出'
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
