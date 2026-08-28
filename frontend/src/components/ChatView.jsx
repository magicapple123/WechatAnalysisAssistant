import { useState, useEffect, useRef, useCallback, useLayoutEffect } from 'react';
import { createPortal } from 'react-dom';
import api from '../api';
import { getInterfaceLabel } from '../modelInterfaces';
import Avatar from './Avatar';
import AiAnalysisDialog from './AiAnalysisDialog';

const PAGE_SIZE = 50;
const LATEST_PAGE_MAX_CHASES = 3;
const TASK_FINISHED_STATUSES = new Set(['completed', 'succeeded', 'failed', 'cancelled', 'error']);

const unwrapData = (response) => {
  if (response?.data && !response.task_id && typeof response.data === 'object') {
    return response.data;
  }
  return response || {};
};

const isRecognitionTaskFinished = (status) => TASK_FINISHED_STATUSES.has(status);

const messageSelectionKey = (message) => (
  message.message_key
  || `${message.id}:${message.create_time || 0}:${message.server_id || 0}`
);

const messageDomId = (message) => `msg-${encodeURIComponent(messageSelectionKey(message))}`;

const appendDownloadFlag = (url) => {
  const value = String(url || '').trim();
  if (!value) return '';
  return `${value}${value.includes('?') ? '&' : '?'}download=1`;
};

const startBrowserDownload = (url) => {
  if (!url || typeof document === 'undefined') return;
  const link = document.createElement('a');
  link.href = url;
  link.download = '';
  link.rel = 'noopener';
  document.body.appendChild(link);
  link.click();
  link.remove();
};

function ContextMenu({ point, items, onClose }) {
  const menuRef = useRef(null);
  const [position, setPosition] = useState(point);

  useLayoutEffect(() => {
    const menu = menuRef.current;
    if (!menu || !point || typeof window === 'undefined') return;
    const rect = menu.getBoundingClientRect();
    const margin = 8;
    setPosition({
      x: Math.max(margin, Math.min(point.x, window.innerWidth - rect.width - margin)),
      y: Math.max(margin, Math.min(point.y, window.innerHeight - rect.height - margin)),
    });
    const firstEnabled = menu.querySelector('button:not(:disabled)');
    firstEnabled?.focus({ preventScroll: true });
  }, [point]);

  useEffect(() => {
    if (!point) return undefined;
    const close = () => onClose?.();
    const handleKeyDown = (event) => {
      if (event.key === 'Escape') close();
    };
    window.addEventListener('pointerdown', close);
    window.addEventListener('resize', close);
    window.addEventListener('scroll', close, true);
    window.addEventListener('keydown', handleKeyDown);
    return () => {
      window.removeEventListener('pointerdown', close);
      window.removeEventListener('resize', close);
      window.removeEventListener('scroll', close, true);
      window.removeEventListener('keydown', handleKeyDown);
    };
  }, [point, onClose]);

  if (!point || typeof document === 'undefined') return null;
  return createPortal(
    <div
      ref={menuRef}
      role="menu"
      className="fixed z-[110] min-w-40 overflow-hidden rounded-xl border border-slate-200 bg-white py-1.5 text-sm shadow-[0_16px_48px_rgba(15,23,42,0.18)]"
      style={{ left: position.x, top: position.y }}
      onPointerDown={(event) => event.stopPropagation()}
      onContextMenu={(event) => event.preventDefault()}
    >
      {items.map((item) => (
        <button
          key={item.key || item.label}
          type="button"
          role="menuitem"
          disabled={item.disabled}
          onClick={() => {
            item.onSelect?.();
            onClose?.();
          }}
          className="flex w-full items-center gap-2 px-3.5 py-2 text-left text-slate-700 transition hover:bg-slate-50 focus:bg-slate-50 focus:outline-none disabled:cursor-not-allowed disabled:text-slate-400"
        >
          {item.icon && <span aria-hidden="true">{item.icon}</span>}
          <span className="whitespace-nowrap">{item.label}</span>
        </button>
      ))}
    </div>,
    document.body,
  );
}

function ImageMessage({
  message,
  talker,
  showImages,
  imageQuality = 'smart',
  refreshToken = 0,
  failed,
  onError,
  onRetry,
  compact = false,
  additionalContextItems = [],
}) {
  const status = message.image_recognition_status;
  const description = message.image_description;
  const error = message.image_recognition_error;
  const [viewerOpen, setViewerOpen] = useState(false);
  const [viewerLoaded, setViewerLoaded] = useState(false);
  const [viewerFailed, setViewerFailed] = useState(false);
  const [contextMenu, setContextMenu] = useState(null);
  const inlineQuality = imageQuality === 'high' ? 'best' : 'thumbnail';
  const inlineUrl = api.imageUrl(
    talker,
    message.id,
    message.create_time,
    message.message_key,
    inlineQuality,
    refreshToken,
  );
  const bestUrl = api.imageUrl(
    talker,
    message.id,
    message.create_time,
    message.message_key,
    'best',
    refreshToken,
  );

  const openViewer = (event) => {
    event.stopPropagation();
    setViewerLoaded(false);
    setViewerFailed(false);
    setViewerOpen(true);
  };

  const openContextMenu = (event) => {
    event.preventDefault();
    event.stopPropagation();
    setContextMenu({ x: event.clientX, y: event.clientY });
  };

  const exportUrl = appendDownloadFlag(bestUrl);

  return (
    <div className="space-y-2">
      {showImages && !failed ? (
        <button
          type="button"
          onClick={openViewer}
          onContextMenu={openContextMenu}
          className="group relative block max-w-full cursor-zoom-in overflow-hidden rounded-md bg-gray-100"
          title="点击查看本地最高可用画质"
        >
          <img
            src={inlineUrl}
            alt={description || '聊天图片'}
            loading="lazy"
            decoding="async"
            draggable="false"
            onError={onError}
            className={`block max-w-full object-contain bg-gray-100 ${compact ? 'max-h-36' : 'max-h-80'}`}
          />
          {imageQuality === 'smart' && (
            <span className="absolute bottom-1.5 right-1.5 rounded bg-black/55 px-1.5 py-0.5 text-[10px] text-white opacity-0 transition-opacity group-hover:opacity-100">
              点击查看高清
            </span>
          )}
        </button>
      ) : (
        <div
          className="min-w-40 rounded-md border border-dashed border-gray-300 bg-gray-50 px-4 py-5 text-center text-xs text-gray-500"
          onContextMenu={openContextMenu}
          title="右键导出图片（最高画质）"
        >
          <div className="text-2xl mb-1">🖼️</div>
          <div>{failed ? '图片暂时无法显示' : '图片显示已关闭'}</div>
          {failed && showImages && (
            <button
              type="button"
              onClick={(event) => {
                event.stopPropagation();
                onRetry();
              }}
              className="mt-1 text-wechat-green hover:underline"
            >
              重试加载
            </button>
          )}
        </div>
      )}

      {description && (
        <div className="rounded-md bg-black/5 px-2.5 py-2 text-xs leading-relaxed text-gray-700">
          <span className="font-medium text-gray-500">图片描述：</span>
          {description}
        </div>
      )}
      {!description && ['queued', 'pending', 'running', 'processing'].includes(status) && (
        <div className="flex items-center gap-1.5 text-xs text-blue-600">
          <span className="w-3 h-3 border-2 border-blue-500 border-t-transparent rounded-full animate-spin" />
          图片识别中...
        </div>
      )}
      {!description && status === 'failed' && (
        <div className="text-xs text-red-500 break-words">
          图片识别失败{error ? `：${error}` : ''}
        </div>
      )}

      {viewerOpen && (
        <div
          className="fixed inset-0 z-[80] flex items-center justify-center bg-black/85 p-4"
          onClick={(event) => {
            event.stopPropagation();
            setViewerOpen(false);
          }}
          role="dialog"
          aria-modal="true"
          aria-label="查看高清聊天图片"
        >
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation();
              setViewerOpen(false);
            }}
            className="absolute right-4 top-4 rounded-full bg-black/50 px-3 py-1.5 text-lg text-white hover:bg-black/70"
            aria-label="关闭高清图片"
          >
            ✕
          </button>
          {!viewerLoaded && !viewerFailed && (
            <div className="absolute flex items-center gap-2 text-sm text-white/80">
              <span className="h-4 w-4 animate-spin rounded-full border-2 border-white border-t-transparent" />
              正在加载本地最高画质…
            </div>
          )}
          {viewerFailed ? (
            <div className="rounded-lg bg-white/10 px-5 py-4 text-sm text-white">
              本地高清图片加载失败
            </div>
          ) : (
            <img
              src={bestUrl}
              alt={description || '高清聊天图片'}
              draggable="false"
              onLoad={() => setViewerLoaded(true)}
              onError={() => setViewerFailed(true)}
              onClick={(event) => event.stopPropagation()}
              onContextMenu={openContextMenu}
              className={`max-h-full max-w-full object-contain transition-opacity ${viewerLoaded ? 'opacity-100' : 'opacity-0'}`}
            />
          )}
        </div>
      )}

      {contextMenu && (
        <ContextMenu
          point={contextMenu}
          onClose={() => setContextMenu(null)}
          items={[
            {
              key: 'export-image',
              icon: '⇩',
              label: '导出图片（最高画质）',
              onSelect: () => startBrowserDownload(exportUrl),
            },
            ...additionalContextItems,
          ]}
        />
      )}
    </div>
  );
}

const normalizeSticker = (message) => {
  const raw = message?.sticker ?? message?.sticker_info ?? message?.emoji ?? {};
  const sticker = typeof raw === 'string' ? { url: raw } : (raw || {});
  const displayUrl = String(
    sticker.display_url
    || sticker.proxy_url
    || sticker.local_proxy_url
    || sticker.local_url
    || sticker.url
    || message?.sticker_url
    || message?.emoji_url
    || '',
  ).trim();
  const explicitExportUrl = String(
    sticker.export_url
    || sticker.download_url
    || message?.sticker_export_url
    || message?.emoji_export_url
    || '',
  ).trim();
  return {
    displayUrl,
    exportUrl: explicitExportUrl || appendDownloadFlag(displayUrl),
    caption: String(sticker.caption || sticker.description || message?.sticker_caption || '').trim(),
    md5: String(sticker.md5 || message?.sticker_md5 || message?.emoji_md5 || '').trim(),
    status: String(sticker.status || message?.sticker_status || '').trim(),
  };
};

function StickerMessage({
  message,
  showMedia = true,
  compact = false,
  additionalContextItems = [],
}) {
  const sticker = normalizeSticker(message);
  const [failed, setFailed] = useState(false);
  const [contextMenu, setContextMenu] = useState(null);

  useEffect(() => {
    setFailed(false);
    setContextMenu(null);
  }, [sticker.displayUrl]);

  const openContextMenu = (event) => {
    event.preventDefault();
    event.stopPropagation();
    setContextMenu({ x: event.clientX, y: event.clientY });
  };

  if (!showMedia) {
    return (
      <>
        <div
          className="flex min-h-16 min-w-28 items-center justify-center rounded-xl border border-dashed border-slate-300 bg-slate-50 px-4 py-3 text-xs text-slate-500"
          onContextMenu={sticker.exportUrl ? openContextMenu : undefined}
          title={sticker.exportUrl ? '右键导出表情' : '该表情暂无可导出的媒体'}
        >
          [表情已隐藏]
        </div>
        {contextMenu && (
          <ContextMenu
            point={contextMenu}
            onClose={() => setContextMenu(null)}
            items={[{
              key: 'export-sticker',
              icon: '⇩',
              label: '导出表情',
              disabled: !sticker.exportUrl,
              onSelect: () => startBrowserDownload(sticker.exportUrl),
            }]}
          />
        )}
      </>
    );
  }

  return (
    <div className="space-y-1.5">
      {sticker.displayUrl && !failed ? (
        <div
          className="inline-flex max-w-full items-center justify-center overflow-hidden rounded-xl bg-transparent"
          onContextMenu={openContextMenu}
          title="右键可导出表情"
        >
          <img
            src={sticker.displayUrl}
            alt={sticker.caption || '聊天表情'}
            loading="lazy"
            decoding="async"
            draggable="false"
            onError={() => setFailed(true)}
            className={`block max-w-full object-contain ${compact ? 'max-h-28 max-w-28' : 'max-h-40 max-w-40'}`}
          />
        </div>
      ) : (
        <div
          className="flex min-h-20 min-w-28 flex-col items-center justify-center rounded-xl border border-dashed border-slate-300 bg-slate-50 px-4 py-3 text-center text-xs text-slate-500"
          onContextMenu={sticker.exportUrl ? openContextMenu : undefined}
          title={sticker.exportUrl ? '媒体预览不可用，仍可右键尝试导出表情' : undefined}
        >
          <span className="mb-1 text-xl" aria-hidden="true">🙂</span>
          <span>{failed ? '表情暂时无法加载' : '表情媒体尚未加载'}</span>
          {sticker.status && <span className="mt-1 text-[10px] text-slate-400">{sticker.status}</span>}
        </div>
      )}
      {sticker.caption && (
        <div className="max-w-40 truncate text-[11px] text-slate-400" title={sticker.caption}>
          {sticker.caption}
        </div>
      )}
      {contextMenu && (
        <ContextMenu
          point={contextMenu}
          onClose={() => setContextMenu(null)}
          items={[
            {
              key: 'export-sticker',
              icon: '⇩',
              label: '导出表情',
              disabled: !sticker.exportUrl,
              onSelect: () => startBrowserDownload(sticker.exportUrl),
            },
            ...additionalContextItems,
          ]}
        />
      )}
    </div>
  );
}

const normalizePayment = (message) => {
  const hasStructuredPayment = Boolean(
    message?.payment && typeof message.payment === 'object'
  );
  const raw = hasStructuredPayment
    ? message.payment
    : {};
  const semanticLabel = String(raw.label || message?.type_name || '').trim();
  if (!['红包', '转账'].includes(semanticLabel)) return null;

  const kind = String(raw.kind || '').trim() === 'red_packet' || semanticLabel === '红包'
    ? 'red_packet'
    : 'transfer';
  const label = kind === 'red_packet' ? '红包' : '转账';
  const title = String(raw.title || '').trim();
  const content = String(message?.content || '').trim();
  const summary = content
    .replace(/^\[(?:红包|转账)(?:·[^\]]+)?\]\s*/u, '')
    .trim();

  return {
    kind,
    label,
    amount: String(raw.amount || '').trim(),
    status: String(raw.status || '').trim(),
    memo: String(raw.memo || '').trim(),
    title,
    summary,
    hasStructuredPayment,
  };
};

const isPaymentMessage = (message) => Boolean(normalizePayment(message));

function PaymentMessage({ message, compact = false }) {
  const payment = normalizePayment(message);
  if (!payment) return message?.content || '';

  const isRedPacket = payment.kind === 'red_packet';
  const genericTitles = new Set([
    payment.label,
    `微信${payment.label}`,
  ]);
  const detailTitle = payment.title && !genericTitles.has(payment.title)
    ? payment.title
    : '';
  const fallbackSummary = !payment.hasStructuredPayment ? payment.summary : '';

  if (compact) {
    const notice = detailTitle || fallbackSummary || payment.status || payment.label;
    return (
      <div
        className={`inline-flex max-w-[80%] items-center gap-2 rounded-full border px-3 py-1.5 text-xs shadow-sm ${
          isRedPacket
            ? 'border-red-100 bg-red-50 text-red-800'
            : 'border-amber-100 bg-amber-50 text-amber-800'
        }`}
      >
        <span
          className={`inline-flex h-5 min-w-9 items-center justify-center rounded-full px-2 text-[10px] font-semibold text-white ${
            isRedPacket ? 'bg-red-700' : 'bg-amber-700'
          }`}
        >
          {payment.label}
        </span>
        <span className="min-w-0 break-words text-left text-slate-600">{notice}</span>
        {payment.amount && <span className="font-semibold tabular-nums">{payment.amount}</span>}
      </div>
    );
  }

  const headline = detailTitle || (isRedPacket ? '微信红包' : '微信转账');
  const secondaryText = payment.memo
    ? `备注：${payment.memo}`
    : (fallbackSummary && fallbackSummary !== detailTitle ? fallbackSummary : '');

  return (
    <div className="w-56 max-w-full min-w-0 overflow-hidden rounded-xl border border-black/5 bg-white shadow-sm">
      <div
        className={`flex items-center gap-3 px-4 py-3 text-white ${
          isRedPacket
            ? 'bg-gradient-to-br from-orange-700 to-red-700'
            : 'bg-gradient-to-br from-amber-700 to-orange-700'
        }`}
      >
        <span className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-full bg-white/20 ring-1 ring-white/30">
          {isRedPacket ? (
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path d="M5 8.5h14v10H5zM4 5h16v3.5H4z" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" />
              <path d="M12 5v13.5M8.3 4.8c-1.7-1.5-.5-3.2 1-2.5 1.2.6 2.1 2.7 2.7 2.7M15.7 4.8c1.7-1.5.5-3.2-1-2.5-1.2.6-2.1 2.7-2.7 2.7" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          ) : (
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path d="M6 7.5h12M7.5 11h9M12 7.5V19M8.5 15.5l3.5 3.5 3.5-3.5M8 3.5l4 4 4-4" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          )}
        </span>
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium text-white/95">{headline}</div>
          {payment.amount ? (
            <div className="mt-0.5 text-xl font-semibold tracking-tight tabular-nums">
              {payment.amount}
            </div>
          ) : (
            <div className="mt-0.5 text-base font-semibold">{payment.label}</div>
          )}
        </div>
      </div>
      {(payment.status || secondaryText) && (
        <div className="space-y-1 border-b border-slate-100 px-4 py-2.5 text-xs">
          {payment.status && (
            <div className="font-medium text-slate-700">{payment.status}</div>
          )}
          {secondaryText && (
            <div className="break-words text-slate-500">{secondaryText}</div>
          )}
        </div>
      )}
      <div className="px-4 py-2 text-[11px] text-slate-500">
        <span>微信{payment.label}</span>
      </div>
    </div>
  );
}

function formatDuration(seconds) {
  const numeric = Number(seconds);
  if (!Number.isFinite(numeric) || numeric <= 0) return '';
  const totalSeconds = Math.max(1, Math.round(numeric));
  if (totalSeconds < 60) return `${totalSeconds}″`;
  const mins = Math.floor(totalSeconds / 60);
  const secs = totalSeconds % 60;
  if (mins > 0) {
    return `${mins}:${String(secs).padStart(2, '0')}`;
  }
  return `0:${String(secs).padStart(2, '0')}`;
}

function VoiceMessage({
  message,
  talker,
  ready,
  taskRunning,
  starting,
  onTranscribe,
}) {
  const transcription = message.voice_transcription;
  const status = message.voice_transcription_status;
  const error = message.voice_transcription_error;
  const isPending = ['queued', 'pending', 'running', 'processing'].includes(status);
  const parserDuration = [
    message.voice_duration_seconds,
    message.voice_duration,
    message.duration_seconds,
  ].map(Number).find((value) => Number.isFinite(value) && value > 0) || 0;
  const [playing, setPlaying] = useState(false);
  const [measuredDuration, setMeasuredDuration] = useState(0);
  const [contextMenu, setContextMenu] = useState(null);
  const audioRef = useRef(null);
  const duration = parserDuration > 0 ? parserDuration : measuredDuration;

  useEffect(() => {
    setPlaying(false);
    setMeasuredDuration(0);
  }, [message.id, message.create_time, message.server_id]);

  // Close context menu on any click outside
  useEffect(() => {
    if (!contextMenu) return;
    const close = () => setContextMenu(null);
    window.addEventListener('click', close);
    return () => window.removeEventListener('click', close);
  }, [contextMenu]);

  const handleContextMenu = (event) => {
    event.preventDefault();
    event.stopPropagation();
    setContextMenu({ x: event.clientX, y: event.clientY });
  };

  const handlePlay = () => {
    const audio = audioRef.current;
    if (!audio) return;
    if (playing) {
      audio.pause();
    } else {
      audio.play().catch(() => {});
    }
  };

  const handleAudioEnded = () => setPlaying(false);
  const handleAudioPlay = () => setPlaying(true);
  const handleAudioPause = () => setPlaying(false);

  const handleExportVoice = () => {
    const url = api.voiceExportUrl(
      talker,
      message.id,
      message.create_time || 0,
      message.server_id,
    );
    window.open(url, '_blank');
    setContextMenu(null);
  };

  const audioUrl = api.voiceAudioUrl(
    talker,
    message.id,
    message.create_time || 0,
    message.server_id,
  );

  return (
    <div className="min-w-48 space-y-2" onContextMenu={handleContextMenu}>
      <div
        className={`flex items-center gap-2 rounded-xl px-2 py-1.5 transition-colors ${
          playing ? 'bg-orange-100/90 text-orange-800' : 'text-orange-700'
        }`}
      >
        <button
          type="button"
          onClick={handlePlay}
          className={`flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full transition-all motion-reduce:transition-none ${
            playing
              ? 'bg-orange-500 text-white shadow-sm motion-safe:animate-pulse'
              : 'bg-orange-100 hover:bg-orange-200'
          }`}
          aria-label={playing ? '暂停语音' : '播放语音'}
          title={playing ? '暂停' : '播放'}
        >
          <span aria-hidden="true">{playing ? '⏸' : '▶'}</span>
        </button>
        <div className="flex items-end gap-0.5" aria-label="语音消息">
          {[2, 4, 6, 3, 7, 5, 3, 6].map((height, index) => (
            <span
              key={index}
              className={`w-0.5 origin-bottom rounded-full transition-colors motion-reduce:animate-none ${
                playing ? 'bg-orange-600 motion-safe:animate-bounce' : 'bg-orange-400'
              }`}
              style={{
                height: `${height * 2}px`,
                animationDelay: playing ? `${index * 90}ms` : undefined,
                animationDuration: playing ? `${520 + (index % 3) * 140}ms` : undefined,
              }}
            />
          ))}
        </div>
        <span className="text-xs text-gray-500">语音消息</span>
        {duration > 0 && (
          <span className="text-xs text-gray-400 ml-1">{formatDuration(duration)}</span>
        )}
      </div>

      <audio
        ref={audioRef}
        src={audioUrl}
        preload="none"
        onEnded={handleAudioEnded}
        onPlay={handleAudioPlay}
        onPause={handleAudioPause}
        onLoadedMetadata={(event) => {
          const value = Number(event.currentTarget.duration);
          if (Number.isFinite(value) && value > 0) setMeasuredDuration(value);
        }}
        className="hidden"
      />

      {transcription && (
        <div className="rounded-md bg-black/5 px-2.5 py-2 text-xs leading-relaxed text-gray-700">
          <span className="font-medium text-gray-500">转写：</span>
          {transcription}
        </div>
      )}
      {!transcription && isPending && (
        <div className="flex items-center gap-1.5 text-xs text-orange-600">
          <span className="h-3 w-3 animate-spin rounded-full border-2 border-orange-500 border-t-transparent" />
          语音转写中...
        </div>
      )}
      {status === 'failed' && (
        <div className="text-xs text-red-500 break-words">
          转写失败{error ? `：${error}` : ''}
        </div>
      )}
      <button
        type="button"
        onClick={(event) => {
          event.stopPropagation();
          onTranscribe(message);
        }}
        disabled={!ready || taskRunning || starting}
        title={ready ? (transcription ? '重新转写这条语音' : '转写这条语音') : '请先在设置中配置语音转文字'}
        className="rounded-md border border-orange-200 bg-orange-50 px-2.5 py-1 text-xs text-orange-700 hover:bg-orange-100 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {transcription ? '重新转写' : '转成文字'}
      </button>

      {contextMenu && (
        <div
          className="fixed z-[70] bg-white border border-gray-200 rounded-lg shadow-lg py-1 text-sm"
          style={{ left: contextMenu.x, top: contextMenu.y }}
        >
          <button
            type="button"
            onClick={handleExportVoice}
            className="w-full px-4 py-2 text-left hover:bg-gray-50 text-gray-700 whitespace-nowrap"
          >
            💾 导出语音
          </button>
        </div>
      )}
    </div>
  );
}

const normalizeReply = (message) => {
  const raw = message?.reply ?? message?.reply_info ?? message?.quote ?? message?.refer ?? null;
  const looksLikeReply = Boolean(
    raw
    || message?.reply_target
    || message?.quoted_message
    || Number(message?.sub_type) === 57
    || String(message?.type_name || '') === '回复',
  );
  if (!looksLikeReply) return null;

  const details = (raw && typeof raw === 'object') ? raw : {};
  let target = details.target_message
    || details.target
    || details.message
    || message?.reply_target
    || message?.quoted_message
    || null;
  if (!target && (
    details.target_id != null
    || details.refer_id != null
    || details.target_server_id
    || details.refer_svrid
    || details.server_id
  )) {
    target = {
      id: details.target_id ?? details.refer_id,
      create_time: details.target_create_time ?? details.refer_createtime ?? details.create_time,
      server_id: details.target_server_id ?? details.refer_svrid ?? details.server_id,
      message_key: details.target_message_key,
      type: details.target_type ?? details.refer_type,
      type_name: details.target_type_name,
      content: details.target_content ?? details.refer_content,
      sender_name: details.target_sender_name ?? details.refer_displayname,
      sticker: details.target_sticker,
    };
  }

  const flattened = String(message?.content || '');
  const flattenedParts = flattened.split(/\n\s*↳\s*/u, 2);
  const replyText = String(
    details.reply_text
    || details.text
    || details.title
    || flattenedParts[0]
    || '',
  ).trim();
  const senderName = String(
    details.target_sender_name
    || details.sender_name
    || details.refer_displayname
    || target?.sender_name
    || target?.display_name
    || '',
  ).trim();
  const targetType = Number(
    target?.type
    ?? details.target_type
    ?? details.refer_type
    ?? 0,
  );
  const summary = String(
    details.target_summary
    || details.summary
    || details.refer_summary
    || target?.content_preview
    || target?.content
    || flattenedParts[1]
    || '',
  ).replace(/^回复\s+[^:：]+[:：]\s*/u, '').trim();

  return {
    replyText,
    senderName,
    targetType,
    summary,
    target,
    media: (details.media && typeof details.media === 'object') ? details.media : null,
  };
};

const isReplyMessage = (message) => Boolean(normalizeReply(message));

function ReplyMessage({
  message,
  talker,
  showImages,
  imageQuality,
  imageRefreshToken,
  renderText,
  onLocate,
  onUnavailable,
}) {
  const reply = normalizeReply(message);
  const target = reply?.target;
  const media = reply?.media;
  const mediaKind = String(media?.kind || '').toLowerCase();
  const targetType = Number(reply?.targetType || target?.type || 0);
  const targetIsImage = [2, 3].includes(targetType) || mediaKind === 'image';
  const targetIsSticker = targetType === 47 || mediaKind === 'sticker' || Boolean(
    target?.sticker
    || target?.sticker_info
    || target?.emoji
    || target?.sticker_url
    || target?.emoji_url,
  );
  const stickerTarget = target ? {
    ...target,
    sticker: target.sticker || media,
    sticker_url: target.sticker_url || media?.sticker_url || media?.display_url || media?.proxy_url,
    sticker_export_url: target.sticker_export_url || media?.sticker_export_url || media?.export_url,
  } : (media ? { sticker: media, sticker_url: media.sticker_url || media.display_url || media.proxy_url } : null);
  const exactTarget = Boolean(
    target
    && target.id != null
    && target.create_time != null
    && target.message_key,
  );
  const [contextMenu, setContextMenu] = useState(null);
  const [imageFailed, setImageFailed] = useState(false);

  useEffect(() => {
    setImageFailed(false);
  }, [target?.id, target?.create_time, target?.message_key]);

  if (!reply) return message?.content || '[回复]';

  const openContextMenu = (event) => {
    event.preventDefault();
    event.stopPropagation();
    setContextMenu({ x: event.clientX, y: event.clientY });
  };
  const locateContextItem = exactTarget
    ? {
        key: 'locate-reply-target',
        icon: '⌖',
        label: '定位到原消息',
        onSelect: () => onLocate?.(target),
      }
    : {
        key: 'reply-target-unavailable',
        icon: '!',
        label: '原消息无法定位',
        onSelect: () => onUnavailable?.(),
      };

  return (
    <div className="space-y-2">
      <div className="whitespace-pre-wrap">
        {reply.replyText ? renderText(reply.replyText) : '[回复]'}
      </div>
      <div
        className="min-w-48 cursor-context-menu rounded-lg border-l-2 border-slate-300 bg-slate-100/90 px-3 py-2 text-xs text-slate-600 transition hover:bg-slate-100"
        onContextMenu={openContextMenu}
        title={exactTarget ? '右键可定位到原消息' : '原消息已不存在或无法精确定位'}
      >
        <div className="mb-1 flex items-center gap-1.5 font-medium text-slate-500">
          <span aria-hidden="true">↩</span>
          <span>{reply.senderName ? `回复 ${reply.senderName}` : '回复的消息'}</span>
        </div>
        {targetIsImage && target?.id != null && target?.create_time != null ? (
          <ImageMessage
            message={target}
            talker={talker}
            showImages={showImages}
            imageQuality={imageQuality}
            refreshToken={imageRefreshToken}
            compact
            failed={imageFailed}
            onError={() => setImageFailed(true)}
            onRetry={() => setImageFailed(false)}
            additionalContextItems={[locateContextItem]}
          />
        ) : targetIsSticker && stickerTarget ? (
          <StickerMessage
            message={stickerTarget}
            showMedia={showImages}
            compact
            additionalContextItems={[locateContextItem]}
          />
        ) : (
          <div className="line-clamp-3 break-words text-slate-600">
            {reply.summary || (
              targetIsImage ? '[图片]' : (targetIsSticker ? '[表情]' : '[原消息内容不可用]')
            )}
          </div>
        )}
        {!exactTarget && (
          <div className="mt-1.5 text-[10px] text-amber-600">原消息已不存在或无法精确定位</div>
        )}
      </div>
      {contextMenu && (
        <ContextMenu
          point={contextMenu}
          onClose={() => setContextMenu(null)}
          items={[locateContextItem]}
        />
      )}
    </div>
  );
}

function RecognitionRangeDialog({
  selectedCount,
  defaultStartDate,
  defaultEndDate,
  visionReady,
  maxImages,
  submitting,
  submitError = '',
  onConfirm,
  onClose,
}) {
  const [scope, setScope] = useState(selectedCount > 0 ? 'selected' : 'all');
  const [startDate, setStartDate] = useState(defaultStartDate || '');
  const [endDate, setEndDate] = useState(defaultEndDate || '');
  const [force, setForce] = useState(false);
  const [error, setError] = useState('');

  const handleConfirm = () => {
    setError('');
    if (!visionReady) {
      setError('请先在设置中填写 Base URL、模型和 API Key');
      return;
    }

    const options = { force };
    if (scope === 'selected') {
      if (selectedCount === 0) {
        setError('请先选择消息，或改用其他范围');
        return;
      }
      options.useSelected = true;
    } else if (scope === 'time') {
      if (!startDate && !endDate) {
        setError('请至少填写一个起止日期');
        return;
      }
      if (startDate) options.startTime = Math.floor(new Date(`${startDate}T00:00:00`).getTime() / 1000);
      if (endDate) options.endTime = Math.floor(new Date(`${endDate}T23:59:59`).getTime() / 1000);
      if (options.startTime != null && options.endTime != null && options.startTime > options.endTime) {
        setError('结束日期不能早于开始日期');
        return;
      }
    } else if (scope === 'all') {
      options.allImages = true;
    }
    onConfirm(options);
  };

  const scopes = [
    { value: 'selected', label: `已选消息（${selectedCount} 条）`, disabled: selectedCount === 0 },
    { value: 'time', label: '指定日期范围' },
    { value: 'all', label: '整个聊天' },
  ];

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-slate-950/55 p-0 backdrop-blur-[2px] sm:p-4">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="recognition-range-title"
        className="flex h-[100dvh] w-full max-w-md flex-col overflow-hidden bg-white shadow-2xl sm:h-auto sm:max-h-[92dvh] sm:rounded-2xl sm:ring-1 sm:ring-slate-900/10"
      >
        <div className="flex-shrink-0 border-b border-slate-200 bg-white px-6 py-4 text-slate-900">
          <h3 id="recognition-range-title" className="font-bold">图片识别</h3>
          <p className="mt-1 text-xs opacity-80">仅在确认后上传所选范围内的聊天图片</p>
        </div>
        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto bg-slate-50/60 p-5">
          <div>
            <p className="text-sm font-medium text-gray-700 mb-2">识别范围</p>
            <div className="space-y-2">
              {scopes.map((item) => (
                <label
                  key={item.value}
                  className={`flex items-center gap-2 rounded-lg border px-3 py-2 text-sm ${
                    item.disabled ? 'cursor-not-allowed bg-gray-50 text-gray-300' : 'cursor-pointer hover:bg-gray-50'
                  } ${scope === item.value ? 'border-wechat-green bg-green-50' : 'border-gray-200'}`}
                >
                  <input
                    type="radio"
                    name="recognition-scope"
                    value={item.value}
                    checked={scope === item.value}
                    disabled={item.disabled}
                    onChange={() => setScope(item.value)}
                    className="accent-wechat-green"
                  />
                  {item.label}
                </label>
              ))}
            </div>
          </div>

          {scope === 'time' && (
            <div className="grid grid-cols-[1fr_auto_1fr] items-end gap-2 rounded-lg bg-gray-50 p-3">
              <label className="text-xs text-gray-500">
                开始日期
                <input
                  type="date"
                  value={startDate}
                  onChange={(event) => setStartDate(event.target.value)}
                  className="mt-1 w-full rounded-md border border-gray-200 bg-white px-2 py-1.5 text-sm text-gray-700 focus:outline-none focus:border-wechat-green"
                />
              </label>
              <span className="pb-2 text-xs text-gray-400">至</span>
              <label className="text-xs text-gray-500">
                结束日期
                <input
                  type="date"
                  value={endDate}
                  onChange={(event) => setEndDate(event.target.value)}
                  className="mt-1 w-full rounded-md border border-gray-200 bg-white px-2 py-1.5 text-sm text-gray-700 focus:outline-none focus:border-wechat-green"
                />
              </label>
            </div>
          )}

          <label className="flex items-start gap-2 text-sm text-gray-600 cursor-pointer">
            <input
              type="checkbox"
              checked={force}
              onChange={(event) => setForce(event.target.checked)}
              className="mt-0.5 accent-wechat-green"
            />
            <span>
              重新识别已有描述
              <span className="block text-xs text-gray-400">会再次上传并可能产生额外模型费用</span>
            </span>
          </label>

          <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
            单次任务最多处理 {maxImages || 50} 张图片；超出时请缩小范围后再次识别。
          </div>
          {!visionReady && (
            <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-600">
              图片识别配置不完整，请先在设置中填写接口地址、模型和 API Key。
            </div>
          )}
          {(error || submitError) && (
            <div className="text-xs text-red-500">{error || submitError}</div>
          )}
        </div>
        <div className="flex flex-shrink-0 justify-end gap-3 border-t border-slate-200 bg-white px-5 py-3">
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            className="px-4 py-2 text-sm text-gray-600 disabled:opacity-50"
          >
            取消
          </button>
          <button
            type="button"
            onClick={handleConfirm}
            disabled={submitting || !visionReady}
            className="flex items-center gap-2 rounded-lg bg-wechat-green px-5 py-2 text-sm font-medium text-white hover:bg-wechat-green-dark disabled:opacity-50"
          >
            {submitting && <span className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />}
            {submitting ? '正在创建任务...' : '开始识别'}
          </button>
        </div>
      </div>
    </div>
  );
}

function HdAutomationRangeDialog({
  loadedImageCount,
  defaultStartDate,
  defaultEndDate,
  defaultPerImageTimeout = 10,
  defaultMinDwellSeconds = 0.5,
  submitting,
  onConfirm,
  onClose,
}) {
  const [scope, setScope] = useState(
    loadedImageCount > 0 ? 'loaded' : ((defaultStartDate || defaultEndDate) ? 'time' : 'all')
  );
  const [startDate, setStartDate] = useState(defaultStartDate || '');
  const [endDate, setEndDate] = useState(defaultEndDate || '');
  const [direction, setDirection] = useState('next');
  const [perImageTimeout, setPerImageTimeout] = useState(() => {
    const value = Number(defaultPerImageTimeout);
    return Number.isInteger(value) && value >= 5 && value <= 120 ? value : 10;
  });
  const [minDwellSeconds, setMinDwellSeconds] = useState(() => {
    const value = Number(defaultMinDwellSeconds);
    return Number.isFinite(value) && value >= 0 && value <= 5 ? value : 0.5;
  });
  const [error, setError] = useState('');

  const handleConfirm = async () => {
    setError('');
    const timeout = Number(perImageTimeout);
    if (!Number.isInteger(timeout) || timeout < 5 || timeout > 120) {
      setError('每张图片等待时间请填写 5–120 的整数秒');
      return;
    }
    const minDwell = Number(minDwellSeconds);
    if (!Number.isFinite(minDwell) || minDwell < 0 || minDwell > 5) {
      setError('验证成功后的最短停留时间请填写 0–5 秒');
      return;
    }

    const options = {
      direction,
      perImageTimeout: timeout,
      minDwellSeconds: minDwell,
    };
    if (scope === 'loaded') {
      if (loadedImageCount === 0) {
        setError('当前已加载的消息中没有图片，请改用其他范围');
        return;
      }
      options.useLoaded = true;
    } else if (scope === 'time') {
      if (!startDate && !endDate) {
        setError('请至少填写一个起止日期');
        return;
      }
      if (startDate) options.startTime = Math.floor(new Date(`${startDate}T00:00:00`).getTime() / 1000);
      if (endDate) options.endTime = Math.floor(new Date(`${endDate}T23:59:59`).getTime() / 1000);
      if (options.startTime != null && options.endTime != null && options.startTime > options.endTime) {
        setError('结束日期不能早于开始日期');
        return;
      }
    } else {
      options.allImages = true;
    }
    try {
      await onConfirm(options);
    } catch (submitError) {
      setError(submitError.message || '创建高清图片自动化任务失败');
    }
  };

  const scopes = [
    {
      value: 'loaded',
      label: `当前已加载的连续时间范围（${loadedImageCount} 张图片）`,
      disabled: loadedImageCount === 0,
    },
    { value: 'time', label: '指定日期范围' },
    { value: 'all', label: '整个聊天' },
  ];

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-slate-950/55 p-0 backdrop-blur-[2px] sm:p-4">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="hd-automation-range-title"
        className="flex h-[100dvh] w-full max-w-lg flex-col overflow-hidden bg-white shadow-2xl sm:h-auto sm:max-h-[92dvh] sm:rounded-2xl sm:ring-1 sm:ring-slate-900/10"
      >
        <div className="flex-shrink-0 border-b border-slate-200 bg-white px-6 py-4 text-slate-900">
          <h3 id="hd-automation-range-title" className="font-bold">批量获取高清图片</h3>
          <p className="mt-1 text-xs opacity-90">通过 Windows UI 自动操作电脑版微信，不接入微信内部 CDN</p>
        </div>

        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto bg-slate-50/60 p-5">
          <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs leading-relaxed text-amber-900">
            创建任务后不会立即控制电脑。你需要先按提示在电脑版微信中手动打开第一张未高清图片，再按全局热键开始。运行期间请勿操作鼠标和键盘。
          </div>

          <div>
            <p className="mb-2 text-sm font-medium text-gray-700">处理范围</p>
            <div className="space-y-2">
              {scopes.map((item) => (
                <label
                  key={item.value}
                  className={`flex items-center gap-2 rounded-lg border px-3 py-2 text-sm ${
                    item.disabled ? 'cursor-not-allowed bg-gray-50 text-gray-300' : 'cursor-pointer hover:bg-gray-50'
                  } ${scope === item.value ? 'border-emerald-500 bg-emerald-50' : 'border-gray-200'}`}
                >
                  <input
                    type="radio"
                    name="hd-automation-scope"
                    value={item.value}
                    checked={scope === item.value}
                    disabled={item.disabled}
                    onChange={() => setScope(item.value)}
                    className="accent-emerald-600"
                  />
                  {item.label}
                </label>
              ))}
            </div>
          </div>

          {scope === 'time' && (
            <div className="grid grid-cols-[1fr_auto_1fr] items-end gap-2 rounded-lg bg-gray-50 p-3">
              <label className="text-xs text-gray-500">
                开始日期
                <input
                  type="date"
                  value={startDate}
                  onChange={(event) => setStartDate(event.target.value)}
                  className="mt-1 w-full rounded-md border border-gray-200 bg-white px-2 py-1.5 text-sm text-gray-700 focus:border-emerald-500 focus:outline-none"
                />
              </label>
              <span className="pb-2 text-xs text-gray-400">至</span>
              <label className="text-xs text-gray-500">
                结束日期
                <input
                  type="date"
                  value={endDate}
                  onChange={(event) => setEndDate(event.target.value)}
                  className="mt-1 w-full rounded-md border border-gray-200 bg-white px-2 py-1.5 text-sm text-gray-700 focus:border-emerald-500 focus:outline-none"
                />
              </label>
            </div>
          )}

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <label className="text-sm text-gray-700">
              图片切换方向
              <select
                value={direction}
                onChange={(event) => setDirection(event.target.value)}
                className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm focus:border-emerald-500 focus:outline-none"
              >
                <option value="next">下一张（从范围首张开始）</option>
                <option value="previous">上一张（从范围末张开始）</option>
              </select>
            </label>
            <label className="text-sm text-gray-700">
              每张图片最长等待
              <div className="relative mt-1">
                <input
                  type="number"
                  min="5"
                  max="120"
                  step="1"
                  value={perImageTimeout}
                  onChange={(event) => setPerImageTimeout(event.target.value)}
                  className="w-full rounded-lg border border-gray-200 px-3 py-2 pr-10 text-sm focus:border-emerald-500 focus:outline-none"
                />
                <span className="absolute right-3 top-2 text-sm text-gray-400">秒</span>
              </div>
            </label>
            <label className="text-sm text-gray-700">
              验证成功后最短停留
              <div className="relative mt-1">
                <input
                  type="number"
                  min="0"
                  max="5"
                  step="0.1"
                  value={minDwellSeconds}
                  onChange={(event) => setMinDwellSeconds(event.target.value)}
                  className="w-full rounded-lg border border-gray-200 px-3 py-2 pr-10 text-sm focus:border-emerald-500 focus:outline-none"
                />
                <span className="absolute right-3 top-2 text-sm text-gray-400">秒</span>
              </div>
            </label>
          </div>

          <div className="rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 text-xs leading-relaxed text-gray-600">
            已有高清缓存和明确过期的图片会立即继续；未确认图片最多等待设定时间。本次新验证成功的图片在翻页前会再停留指定时长。实际数量和单次上限由后端在创建任务时校验。
          </div>
          {error && <div className="text-xs text-red-500">{error}</div>}
        </div>

        <div className="flex flex-shrink-0 justify-end gap-3 border-t border-slate-200 bg-white px-5 py-3">
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            className="px-4 py-2 text-sm text-gray-600 disabled:opacity-50"
          >
            取消
          </button>
          <button
            type="button"
            onClick={handleConfirm}
            disabled={submitting}
            className="flex items-center gap-2 rounded-lg bg-emerald-600 px-5 py-2 text-sm font-medium text-white hover:bg-emerald-700 disabled:opacity-50"
          >
            {submitting && <span className="h-4 w-4 animate-spin rounded-full border-2 border-white border-t-transparent" />}
            {submitting ? '正在准备...' : '创建并查看操作提示'}
          </button>
        </div>
      </div>
    </div>
  );
}

function TranscriptionRangeDialog({
  selectedCount,
  defaultStartDate,
  defaultEndDate,
  singleMessage,
  transcriptionReady,
  providerLabel,
  maxVoices,
  submitting,
  submitError = '',
  onConfirm,
  onClose,
}) {
  const [scope, setScope] = useState(
    singleMessage ? 'single' : (selectedCount > 0 ? 'selected' : 'all')
  );
  const [startDate, setStartDate] = useState(defaultStartDate || '');
  const [endDate, setEndDate] = useState(defaultEndDate || '');
  const [force, setForce] = useState(Boolean(singleMessage?.voice_transcription));
  const [cloudConfirmed, setCloudConfirmed] = useState(false);
  const [error, setError] = useState('');

  const handleConfirm = () => {
    setError('');
    if (!transcriptionReady) {
      setError('语音转文字配置不完整，请先在设置中完成配置');
      return;
    }
    if (!cloudConfirmed) {
      setError('云端转写前必须明确勾选同意上传本次范围内的语音');
      return;
    }

    const options = {
      force,
      cloudUploadConfirmed: cloudConfirmed,
    };
    if (scope === 'single') {
      if (!singleMessage) {
        setError('未找到要转写的语音消息');
        return;
      }
      options.messageRefs = [{
        id: singleMessage.id,
        create_time: singleMessage.create_time || 0,
        message_key: singleMessage.message_key || messageSelectionKey(singleMessage),
      }];
    } else if (scope === 'selected') {
      if (selectedCount === 0) {
        setError('请先选择消息，或改用其他范围');
        return;
      }
      options.useSelected = true;
    } else if (scope === 'time') {
      if (!startDate && !endDate) {
        setError('请至少填写一个起止日期');
        return;
      }
      if (startDate) options.startTime = Math.floor(new Date(`${startDate}T00:00:00`).getTime() / 1000);
      if (endDate) options.endTime = Math.floor(new Date(`${endDate}T23:59:59`).getTime() / 1000);
      if (options.startTime != null && options.endTime != null && options.startTime > options.endTime) {
        setError('结束日期不能早于开始日期');
        return;
      }
    } else {
      options.allVoices = true;
    }
    onConfirm(options);
  };

  const scopes = singleMessage ? [
    { value: 'single', label: '仅这条语音' },
  ] : [
    { value: 'selected', label: `已选消息（${selectedCount} 条）`, disabled: selectedCount === 0 },
    { value: 'time', label: '指定日期范围' },
    { value: 'all', label: '整个聊天' },
  ];

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-slate-950/55 p-0 backdrop-blur-[2px] sm:p-4">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="transcription-range-title"
        className="flex h-[100dvh] w-full max-w-md flex-col overflow-hidden bg-white shadow-2xl sm:h-auto sm:max-h-[92dvh] sm:rounded-2xl sm:ring-1 sm:ring-slate-900/10"
      >
        <div className="flex-shrink-0 border-b border-slate-200 bg-white px-6 py-4 text-slate-900">
          <h3 id="transcription-range-title" className="font-bold">语音转文字</h3>
          <p className="mt-1 text-xs opacity-90">
            将使用云端接口：{providerLabel}
          </p>
        </div>
        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto bg-slate-50/60 p-5">
          <div>
            <p className="mb-2 text-sm font-medium text-gray-700">转写范围</p>
            <div className="space-y-2">
              {scopes.map((item) => (
                <label
                  key={item.value}
                  className={`flex items-center gap-2 rounded-lg border px-3 py-2 text-sm ${
                    item.disabled ? 'cursor-not-allowed bg-gray-50 text-gray-300' : 'cursor-pointer hover:bg-gray-50'
                  } ${scope === item.value ? 'border-emerald-500 bg-emerald-50' : 'border-gray-200'}`}
                >
                  <input
                    type="radio"
                    name="transcription-scope"
                    value={item.value}
                    checked={scope === item.value}
                    disabled={item.disabled}
                    onChange={() => setScope(item.value)}
                    className="accent-emerald-600"
                  />
                  {item.label}
                </label>
              ))}
            </div>
          </div>

          {scope === 'time' && (
            <div className="grid grid-cols-[1fr_auto_1fr] items-end gap-2 rounded-lg bg-gray-50 p-3">
              <label className="text-xs text-gray-500">
                开始日期
                <input
                  type="date"
                  value={startDate}
                  onChange={(event) => setStartDate(event.target.value)}
                  className="mt-1 w-full rounded-md border border-gray-200 bg-white px-2 py-1.5 text-sm text-gray-700 focus:outline-none focus:border-emerald-500"
                />
              </label>
              <span className="pb-2 text-xs text-gray-400">至</span>
              <label className="text-xs text-gray-500">
                结束日期
                <input
                  type="date"
                  value={endDate}
                  onChange={(event) => setEndDate(event.target.value)}
                  className="mt-1 w-full rounded-md border border-gray-200 bg-white px-2 py-1.5 text-sm text-gray-700 focus:outline-none focus:border-emerald-500"
                />
              </label>
            </div>
          )}

          <label className="flex cursor-pointer items-start gap-2 text-sm text-gray-600">
            <input
              type="checkbox"
              checked={force}
              onChange={(event) => setForce(event.target.checked)}
              className="mt-0.5 accent-emerald-600"
            />
            <span>
              重新转写已有文字
              <span className="block text-xs text-gray-400">会忽略当前模型配置对应的转写缓存</span>
            </span>
          </label>

          <label className="flex cursor-pointer items-start gap-2 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900">
            <input
              type="checkbox"
              checked={cloudConfirmed}
              onChange={(event) => setCloudConfirmed(event.target.checked)}
              className="mt-0.5 accent-emerald-600"
            />
            <span>
              我确认将本次所选范围内的语音上传到该接口
              <span className="block text-xs text-amber-700">此确认只对本次任务有效，下次不会自动沿用。</span>
            </span>
          </label>

          <div className="rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 text-xs text-gray-600">
            单次任务最多处理 {maxVoices || 50} 条语音。
          </div>
          {!transcriptionReady && (
            <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-600">
              语音转文字配置不完整，请先在设置中配置模型、接口地址和 API Key。
            </div>
          )}
          {(error || submitError) && (
            <div className="text-xs text-red-500">{error || submitError}</div>
          )}
        </div>
        <div className="flex flex-shrink-0 justify-end gap-3 border-t border-slate-200 bg-white px-5 py-3">
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            className="px-4 py-2 text-sm text-gray-600 disabled:opacity-50"
          >
            取消
          </button>
          <button
            type="button"
            onClick={handleConfirm}
            disabled={submitting || !transcriptionReady || !cloudConfirmed}
            className="flex items-center gap-2 rounded-lg bg-emerald-600 px-5 py-2 text-sm font-medium text-white hover:bg-emerald-700 disabled:opacity-50"
          >
            {submitting && <span className="h-4 w-4 animate-spin rounded-full border-2 border-white border-t-transparent" />}
            {submitting ? '正在创建任务...' : '开始转写'}
          </button>
        </div>
      </div>
    </div>
  );
}

function ChatAnalysisRangeDialog({
  selectedCount,
  totalCount,
  defaultStartDate,
  defaultEndDate,
  analysisReady,
  onConfirm,
  onClose,
}) {
  const dialogRef = useRef(null);
  const previousFocusRef = useRef(null);
  const onCloseRef = useRef(onClose);
  const [scope, setScope] = useState(selectedCount > 0 ? 'selected' : 'time');
  const [startDate, setStartDate] = useState(defaultStartDate || '');
  const [endDate, setEndDate] = useState(defaultEndDate || '');
  const [error, setError] = useState('');

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    previousFocusRef.current = document.activeElement;
    const focusTimer = window.setTimeout(() => dialogRef.current?.focus(), 0);
    const handleKeyDown = (event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onCloseRef.current?.();
        return;
      }
      if (event.key !== 'Tab' || !dialogRef.current) return;
      const focusable = [...dialogRef.current.querySelectorAll(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
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

  const handleConfirm = () => {
    setError('');
    if (!analysisReady) {
      setError('AI 分析配置不完整，请先在设置中填写接口地址、模型和 API Key');
      return;
    }
    if (scope === 'selected') {
      if (selectedCount <= 0) {
        setError('请先勾选需要分析的聊天记录');
        return;
      }
      onConfirm({
        useSelected: true,
        scopeSummary: `已选 ${selectedCount} 条聊天记录`,
      });
      return;
    }
    if (scope === 'time') {
      if (!startDate && !endDate) {
        setError('日期范围至少需要填写开始日期或结束日期');
        return;
      }
      const startTime = startDate
        ? Math.floor(new Date(`${startDate}T00:00:00`).getTime() / 1000)
        : undefined;
      const endTime = endDate
        ? Math.floor(new Date(`${endDate}T23:59:59`).getTime() / 1000)
        : undefined;
      if (
        (startTime != null && !Number.isFinite(startTime))
        || (endTime != null && !Number.isFinite(endTime))
      ) {
        setError('日期格式无效，请重新选择');
        return;
      }
      if (startTime != null && endTime != null && startTime > endTime) {
        setError('开始日期不能晚于结束日期');
        return;
      }
      onConfirm({
        startTime,
        endTime,
        scopeSummary: `${startDate || '最早记录'} 至 ${endDate || '最新记录'}`,
      });
      return;
    }
    onConfirm({
      allMessages: true,
      scopeSummary: `整个聊天（共 ${Number(totalCount || 0).toLocaleString()} 条）`,
    });
  };

  const options = [
    { value: 'selected', label: `已选消息（${selectedCount} 条）`, disabled: selectedCount <= 0 },
    { value: 'time', label: '日期范围', disabled: false },
    { value: 'all', label: '整个聊天', disabled: false },
  ];

  return (
    <div className="fixed inset-0 z-[65] flex items-center justify-center bg-slate-950/55 p-0 backdrop-blur-[2px] sm:p-4">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="chat-analysis-range-title"
        tabIndex={-1}
        className="flex h-[100dvh] w-full max-w-lg flex-col overflow-hidden bg-white shadow-2xl outline-none sm:h-auto sm:max-h-[92dvh] sm:rounded-2xl sm:ring-1 sm:ring-slate-900/10"
      >
        <div className="flex-shrink-0 border-b border-slate-200 bg-white px-6 py-4 text-slate-900">
          <h3 id="chat-analysis-range-title" className="font-bold">选择 AI 分析范围</h3>
          <p className="mt-1 text-xs text-slate-500">下一步可选择分析预设、强度和详细程度</p>
        </div>
        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto bg-slate-50/60 p-5">
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
            {options.map((item) => (
              <label
                key={item.value}
                className={`rounded-lg border p-3 text-sm ${
                  scope === item.value ? 'border-indigo-500 bg-indigo-50' : 'border-gray-200'
                } ${item.disabled ? 'cursor-not-allowed opacity-50' : 'cursor-pointer'}`}
              >
                <input
                  type="radio"
                  name="chat-analysis-scope"
                  value={item.value}
                  checked={scope === item.value}
                  disabled={item.disabled}
                  onChange={() => setScope(item.value)}
                  className="mr-2 accent-indigo-600"
                />
                {item.label}
              </label>
            ))}
          </div>
          {scope === 'time' && (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <label className="text-xs font-medium text-gray-600">
                开始日期（可选）
                <input
                  type="date"
                  value={startDate}
                  onChange={(event) => setStartDate(event.target.value)}
                  className="mt-1 w-full rounded-lg border border-gray-200 px-3 py-2 text-sm"
                />
              </label>
              <label className="text-xs font-medium text-gray-600">
                结束日期（可选）
                <input
                  type="date"
                  value={endDate}
                  onChange={(event) => setEndDate(event.target.value)}
                  className="mt-1 w-full rounded-lg border border-gray-200 px-3 py-2 text-sm"
                />
              </label>
            </div>
          )}
          <p className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-800">
            单次最多分析 5,000 条记录；图片使用已有文字描述，语音使用已有转写，不会额外上传媒体文件。
          </p>
          {!analysisReady && (
            <p className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-600">
              尚未完成 AI 分析模型配置，请先前往设置填写。
            </p>
          )}
          {error && <p className="text-xs text-red-600">{error}</p>}
        </div>
        <div className="flex flex-shrink-0 justify-end gap-3 border-t border-slate-200 bg-white px-5 py-3">
          <button type="button" onClick={onClose} className="px-4 py-2 text-sm text-gray-600">取消</button>
          <button
            type="button"
            onClick={handleConfirm}
            disabled={!analysisReady}
            className="rounded-lg bg-indigo-600 px-5 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
          >
            下一步：分析设置
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * 聊天消息视图
 *
 * 显示聊天消息列表，类似微信聊天界面:
 * - 自己发送的消息靠右，绿色气泡
 * - 对方发送的消息靠左，白色气泡
 * - 支持分页加载、消息类型过滤
 * - 支持消息选择模式 (勾选导出)
 */
export default function ChatView({
  chat,
  onExport,
  onExportSelected,
  refreshKey = 0,
  settings,
  onSaveSettings,
  selfAvatarUrl = '',
  selfDisplayName = '我',
}) {
  const [messages, setMessages] = useState([]);
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);
  const [keyword, setKeyword] = useState('');
  const [msgType, setMsgType] = useState(null);
  const [hasMore, setHasMore] = useState(true);
  const [hasNewer, setHasNewer] = useState(false);
  const [messageError, setMessageError] = useState('');
  const [messageReloadToken, setMessageReloadToken] = useState(0);

  // 消息选择
  const [selectMode, setSelectMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState(new Set());

  // 搜索定位
  const [searchResults, setSearchResults] = useState([]);
  const [searching, setSearching] = useState(false);
  const [searchAttempted, setSearchAttempted] = useState(false);
  const [searchError, setSearchError] = useState('');
  const [highlightId, setHighlightId] = useState(null);
  const [filterSender, setFilterSender] = useState('');
  const [filterStart, setFilterStart] = useState('');
  const [filterEnd, setFilterEnd] = useState('');

  // 图片显示与手动识别
  const [showImages, setShowImages] = useState(settings?.ui?.show_chat_images ?? false);
  const [imageLoadErrors, setImageLoadErrors] = useState(new Set());
  const [imageRefreshToken, setImageRefreshToken] = useState(0);
  const [showRecognitionDialog, setShowRecognitionDialog] = useState(false);
  const [recognitionTask, setRecognitionTask] = useState(null);
  const [recognitionError, setRecognitionError] = useState('');
  const [startingRecognition, setStartingRecognition] = useState(false);
  const [showHdAutomationDialog, setShowHdAutomationDialog] = useState(false);
  const [hdAutomationTask, setHdAutomationTask] = useState(null);
  const [hdAutomationError, setHdAutomationError] = useState('');
  const [startingHdAutomation, setStartingHdAutomation] = useState(false);
  const [pausingHdAutomation, setPausingHdAutomation] = useState(false);
  const [switchingHdNavigationMode, setSwitchingHdNavigationMode] = useState(false);
  const [cancellingHdAutomation, setCancellingHdAutomation] = useState(false);
  const [showTranscriptionDialog, setShowTranscriptionDialog] = useState(false);
  const [singleVoiceMessage, setSingleVoiceMessage] = useState(null);
  const [transcriptionTask, setTranscriptionTask] = useState(null);
  const [transcriptionError, setTranscriptionError] = useState('');
  const [startingTranscription, setStartingTranscription] = useState(false);
  const [cancellingTranscription, setCancellingTranscription] = useState(false);
  const [showAnalysisRangeDialog, setShowAnalysisRangeDialog] = useState(false);
  const [showAnalysisDialog, setShowAnalysisDialog] = useState(false);
  const [analysisScope, setAnalysisScope] = useState(null);
  const [analysisRunning, setAnalysisRunning] = useState(false);
  const [analysisError, setAnalysisError] = useState('');
  const [analysisReport, setAnalysisReport] = useState(null);
  const [navigationNotice, setNavigationNotice] = useState('');
  const imageQuality = ['smart', 'high', 'thumbnail'].includes(
    settings?.ui?.image_quality
  ) ? settings.ui.image_quality : 'smart';

  const messagesEndRef = useRef(null);
  const containerRef = useRef(null);
  const messageContentRef = useRef(null);
  const isFirstLoad = useRef(true);
  const lastPageRef = useRef(0);  // 记住总页数，用于首次加载最后一页
  const scrollRestoreRef = useRef(0); // 恢复滚动位置
  const shouldScrollToBottom = useRef(false); // 切换消息类型后滚到底部
  const conversationGenerationRef = useRef(0);
  const renderedConversationRef = useRef({ talker: chat.talker, refreshKey });
  const bottomPinRef = useRef({ active: false, generation: 0 });
  const bottomPinTimerRef = useRef(null);
  const bottomScrollFrameRef = useRef(null);
  const pendingLocateRef = useRef(null);
  const navigationNoticeTimerRef = useRef(null);
  const recognitionPollTimerRef = useRef(null);
  const hdAutomationPollTimerRef = useRef(null);
  const hdAutomationRefreshRef = useRef({ taskId: '', downloaded: 0 });
  const transcriptionPollTimerRef = useRef(null);
  const analysisContextRef = useRef(chat.talker);
  const analysisRequestIdRef = useRef(0);
  const analysisAbortRef = useRef(null);
  const searchAbortRef = useRef(null);

  // 在新会话真正提交副作用前就让旧请求失效，消除响应恰好落在切换渲染阶段的竞态。
  if (
    renderedConversationRef.current.talker !== chat.talker
    || renderedConversationRef.current.refreshKey !== refreshKey
  ) {
    renderedConversationRef.current = { talker: chat.talker, refreshKey };
    conversationGenerationRef.current += 1;
    bottomPinRef.current = {
      active: false,
      generation: conversationGenerationRef.current,
    };
  }

  const visionReady = Boolean(
    settings?.vision?.base_url
    && settings?.vision?.model
    && (
      settings?.vision?.requires_api_key === false
      || settings?.vision?.has_api_key
    )
  );
  const transcriptionIsCloud = true;
  const transcriptionReady = Boolean(
    settings?.transcription?.model
    && settings?.transcription?.base_url
    && (
      settings?.transcription?.requires_api_key === false
      || settings?.transcription?.has_api_key
    )
  );
  const transcriptionProviderLabel = getInterfaceLabel(
    'transcription',
    settings?.transcription,
  );

  useEffect(() => {
    setShowImages(settings?.ui?.show_chat_images ?? false);
  }, [settings?.ui?.show_chat_images]);

  useEffect(() => {
    analysisAbortRef.current?.abort();
    analysisAbortRef.current = null;
    analysisRequestIdRef.current += 1;
    analysisContextRef.current = chat.talker;
    setShowAnalysisRangeDialog(false);
    setShowAnalysisDialog(false);
    setAnalysisScope(null);
    setAnalysisError('');
    setAnalysisReport(null);
    setAnalysisRunning(false);
  }, [chat.talker, refreshKey]);

  useEffect(() => {
    setImageLoadErrors(new Set());
  }, [settings?.images, imageQuality]);

  const applyRecognitionResults = useCallback((results = []) => {
    if (!Array.isArray(results) || results.length === 0) return;

    const mergeResults = (items) => items.map((message) => {
      const result = results.find((item) => {
        if (String(item.message_id) !== String(message.id)) return false;
        return item.create_time == null || Number(item.create_time) === Number(message.create_time);
      });
      if (!result) return message;
      return {
        ...message,
        image_description: result.description || message.image_description,
        image_recognition_status: result.status ?? message.image_recognition_status,
        image_recognition_error: result.error ?? null,
      };
    });

    setMessages((previous) => mergeResults(previous));
    setSearchResults((previous) => mergeResults(previous));
  }, []);

  const handleToggleImages = async () => {
    const nextValue = !showImages;
    setShowImages(nextValue);
    setRecognitionError('');
    if (!onSaveSettings) return;
    try {
      await onSaveSettings({ ui: { show_chat_images: nextValue } });
    } catch (error) {
      setShowImages(!nextValue);
      setRecognitionError(`保存图片显示偏好失败：${error.message}`);
    }
  };

  const startHdAutomation = useCallback(async ({
    useLoaded = false,
    startTime,
    endTime,
    allImages = false,
    direction = 'next',
    perImageTimeout = 10,
    minDwellSeconds = 0.5,
  } = {}) => {
    const loadedImageMessages = useLoaded
      ? messages.filter((message) => [2, 3].includes(Number(message.type)))
      : [];
    const messageRefs = useLoaded
      ? loadedImageMessages.map((message) => ({
          id: message.id,
          create_time: message.create_time || 0,
          message_key: message.message_key || messageSelectionKey(message),
        }))
      : undefined;
    if (useLoaded && messageRefs.length === 0) {
      setHdAutomationError('当前已加载的消息中没有图片，请改用日期范围或整个聊天');
      return;
    }

    setStartingHdAutomation(true);
    setHdAutomationError('');
    try {
      const response = await api.startHdImageAutomation(chat.talker, {
        messageRefs,
        startTime,
        endTime,
        allImages,
        direction,
        perImageTimeout,
        minDwellSeconds,
      });
      const task = unwrapData(response);
      if (!task.task_id) {
        throw new Error('服务端未返回 UI 自动化任务编号');
      }
      setHdAutomationTask({
        total: 0,
        completed: 0,
        downloaded: 0,
        skipped: 0,
        expired_count: 0,
        failed_count: 0,
        unconfirmed_navigation_count: 0,
        status: 'awaiting_viewer',
        ...task,
        task_id: task.task_id,
        chat_talker: chat.talker,
        chat_display_name: chat.display_name,
      });
      setShowHdAutomationDialog(false);
    } catch (error) {
      setHdAutomationError(error.message || '创建高清图片自动化任务失败');
      throw error;
    } finally {
      setStartingHdAutomation(false);
    }
  }, [chat.display_name, chat.talker, messages]);

  useEffect(() => {
    const taskId = hdAutomationTask?.task_id;
    if (!taskId || isRecognitionTaskFinished(hdAutomationTask.status)) return undefined;

    let cancelled = false;
    let consecutiveFailures = 0;
    const poll = async () => {
      try {
        const response = await api.getHdImageAutomationTask(taskId);
        if (cancelled) return;
        const task = unwrapData(response);
        consecutiveFailures = 0;
        setHdAutomationError('');
        const downloaded = Number(task.downloaded || 0);
        const refreshState = hdAutomationRefreshRef.current;
        if (refreshState.taskId !== taskId) {
          refreshState.taskId = taskId;
          refreshState.downloaded = 0;
        }
        if (downloaded > refreshState.downloaded) {
          refreshState.downloaded = downloaded;
          setImageLoadErrors(new Set());
          setImageRefreshToken((current) => current + 1);
        }
        setHdAutomationTask((previous) => ({
          ...previous,
          ...task,
          task_id: task.task_id || taskId,
        }));
        if (!isRecognitionTaskFinished(task.status)) {
          hdAutomationPollTimerRef.current = setTimeout(poll, 1000);
        }
      } catch (error) {
        if (cancelled) return;
        consecutiveFailures += 1;
        setHdAutomationError(`获取高清图片自动化进度失败：${error.message}`);
        if (consecutiveFailures < 5) {
          hdAutomationPollTimerRef.current = setTimeout(
            poll,
            Math.min(10000, 1000 * (2 ** consecutiveFailures)),
          );
        } else {
          // The backend may still be sending foreground keys. Keep the task
          // active and the stop control visible, then retry at a low rate.
          hdAutomationPollTimerRef.current = setTimeout(poll, 30000);
        }
      }
    };

    hdAutomationPollTimerRef.current = setTimeout(poll, 400);
    return () => {
      cancelled = true;
      if (hdAutomationPollTimerRef.current) clearTimeout(hdAutomationPollTimerRef.current);
    };
  }, [hdAutomationTask?.task_id, hdAutomationTask?.status]);

  useEffect(() => {
    let cancelled = false;
    api.getActiveHdImageAutomationTask()
      .then((response) => {
        if (cancelled) return;
        const task = unwrapData(response);
        if (!task?.task_id) return;
        setHdAutomationTask((previous) => previous || {
          ...task,
          task_id: task.task_id,
          chat_display_name: '目标聊天',
        });
      })
      .catch(() => {
        // Normal when an older backend is still starting; regular task
        // creation remains available.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const pauseHdAutomation = async () => {
    const taskId = hdAutomationTask?.task_id;
    if (!taskId || pausingHdAutomation) return;
    setPausingHdAutomation(true);
    setHdAutomationError('');
    try {
      const response = await api.pauseHdImageAutomationTask(taskId);
      const task = unwrapData(response);
      setHdAutomationTask((previous) => ({
        ...previous,
        ...task,
        task_id: task.task_id || taskId,
        status: task.status || 'paused',
      }));
    } catch (error) {
      setHdAutomationError(`暂停高清图片任务失败：${error.message}`);
    } finally {
      setPausingHdAutomation(false);
    }
  };

  const switchHdNavigationMode = async (mode) => {
    const taskId = hdAutomationTask?.task_id;
    if (!taskId || switchingHdNavigationMode) return;
    setSwitchingHdNavigationMode(true);
    setHdAutomationError('');
    try {
      const response = await api.setHdImageAutomationNavigationMode(taskId, mode);
      const task = unwrapData(response);
      setHdAutomationTask((previous) => ({
        ...previous,
        ...task,
        task_id: task.task_id || taskId,
      }));
    } catch (error) {
      setHdAutomationError(`切换高清图片翻页模式失败：${error.message}`);
    } finally {
      setSwitchingHdNavigationMode(false);
    }
  };

  const cancelHdAutomation = async () => {
    const taskId = hdAutomationTask?.task_id;
    if (!taskId || cancellingHdAutomation) return;
    setCancellingHdAutomation(true);
    setHdAutomationError('');
    try {
      const response = await api.cancelHdImageAutomationTask(taskId);
      const task = unwrapData(response);
      setHdAutomationTask((previous) => ({
        ...previous,
        ...task,
        task_id: task.task_id || taskId,
        status: task.status || 'cancelling',
      }));
    } catch (error) {
      setHdAutomationError(`停止高清图片任务失败：${error.message}`);
    } finally {
      setCancellingHdAutomation(false);
    }
  };

  const startRecognition = useCallback(async ({
    useSelected = false,
    messageRefs,
    messageIds,
    startTime,
    endTime,
    allImages = false,
    force = false,
  } = {}) => {
    if (!visionReady) {
      setRecognitionError('图片识别配置不完整，请先在设置中填写接口地址、模型和 API Key');
      return;
    }

    const selectedMessages = useSelected
      ? messages.filter((message) => selectedIds.has(messageSelectionKey(message)))
      : [];
    const selectedMessageRefs = messageRefs || (useSelected
      ? selectedMessages.map((message) => ({
          id: message.id,
          create_time: message.create_time || 0,
          message_key: message.message_key || messageSelectionKey(message),
        }))
      : undefined);
    if (useSelected && (!selectedMessageRefs || selectedMessageRefs.length === 0)) {
      setRecognitionError('请先选择需要识别的消息');
      return;
    }

    setStartingRecognition(true);
    setRecognitionError('');
    try {
      const response = await api.recognizeImages(chat.talker, {
        messageRefs: selectedMessageRefs,
        messageIds,
        startTime,
        endTime,
        allImages,
        force,
      });
      const task = unwrapData(response);
      if (!task.task_id && !isRecognitionTaskFinished(task.status)) {
        throw new Error('服务端未返回识别任务编号');
      }
      applyRecognitionResults(task.results);
      setRecognitionTask({
        task_id: task.task_id,
        total: task.total || 0,
        completed: task.completed || 0,
        failed: task.failed || 0,
        status: task.status || 'queued',
        results: task.results || [],
      });
      setShowRecognitionDialog(false);
    } catch (error) {
      setRecognitionError(error.message || '创建图片识别任务失败');
    } finally {
      setStartingRecognition(false);
    }
  }, [
    applyRecognitionResults,
    chat.talker,
    messages,
    selectedIds,
    visionReady,
  ]);

  useEffect(() => {
    const taskId = recognitionTask?.task_id;
    if (!taskId || isRecognitionTaskFinished(recognitionTask.status)) return undefined;

    let cancelled = false;
    let consecutiveFailures = 0;
    const poll = async () => {
      try {
        const response = await api.getImageRecognitionTask(taskId);
        if (cancelled) return;
        const task = unwrapData(response);
        consecutiveFailures = 0;
        setRecognitionError('');
        applyRecognitionResults(task.results);
        setRecognitionTask((previous) => ({
          ...previous,
          ...task,
          task_id: task.task_id || taskId,
          results: task.results || previous?.results || [],
        }));
        if (!isRecognitionTaskFinished(task.status)) {
          recognitionPollTimerRef.current = setTimeout(poll, 1200);
        }
      } catch (error) {
        if (cancelled) return;
        consecutiveFailures += 1;
        setRecognitionError(`获取图片识别进度失败：${error.message}`);
        if (consecutiveFailures < 5) {
          recognitionPollTimerRef.current = setTimeout(
            poll,
            Math.min(10000, 1200 * (2 ** consecutiveFailures)),
          );
        } else {
          setRecognitionTask((previous) => ({ ...previous, status: 'error' }));
        }
      }
    };

    recognitionPollTimerRef.current = setTimeout(poll, 500);
    return () => {
      cancelled = true;
      if (recognitionPollTimerRef.current) clearTimeout(recognitionPollTimerRef.current);
    };
  }, [applyRecognitionResults, recognitionTask?.task_id, recognitionTask?.status]);

  const applyTranscriptionResults = useCallback((results = []) => {
    if (!Array.isArray(results) || results.length === 0) return;

    const mergeResults = (items) => items.map((message) => {
      const messageKey = messageSelectionKey(message);
      const result = results.find((item) => {
        const resultKey = String(item.message_key || '');
        if (resultKey) {
          return resultKey === messageKey || resultKey === String(message.message_key || '');
        }
        if (String(item.message_id) !== String(message.id)) return false;
        if (item.create_time != null && Number(item.create_time) !== Number(message.create_time)) return false;
        if (
          item.server_id
          && message.server_id
          && String(item.server_id) !== String(message.server_id)
        ) return false;
        return true;
      });
      if (!result) return message;
      return {
        ...message,
        voice_transcription: result.transcription || message.voice_transcription || '',
        voice_transcription_status: result.status ?? message.voice_transcription_status,
        voice_transcription_error: result.error ?? '',
      };
    });

    setMessages((previous) => mergeResults(previous));
    setSearchResults((previous) => mergeResults(previous));
  }, []);

  const openSingleVoiceTranscription = useCallback((message) => {
    setTranscriptionError('');
    setSingleVoiceMessage(message);
    setShowTranscriptionDialog(true);
  }, []);

  const startTranscription = useCallback(async ({
    useSelected = false,
    messageRefs,
    messageIds,
    startTime,
    endTime,
    allVoices = false,
    force = false,
    cloudUploadConfirmed = false,
  } = {}) => {
    if (!transcriptionReady) {
      setTranscriptionError('语音转文字配置不完整，请先在设置中完成配置');
      return;
    }
    if (transcriptionIsCloud && !cloudUploadConfirmed) {
      setTranscriptionError('云端转写前必须明确确认上传本次范围内的语音');
      return;
    }

    const selectedVoiceMessages = useSelected
      ? messages.filter((message) => (
          selectedIds.has(messageSelectionKey(message))
          && [4, 34].includes(Number(message.type))
        ))
      : [];
    const selectedMessageRefs = messageRefs || (useSelected
      ? selectedVoiceMessages.map((message) => ({
          id: message.id,
          create_time: message.create_time || 0,
          message_key: message.message_key || messageSelectionKey(message),
        }))
      : undefined);
    if (useSelected && (!selectedMessageRefs || selectedMessageRefs.length === 0)) {
      setTranscriptionError('选中的消息中没有语音，请重新选择');
      return;
    }

    setStartingTranscription(true);
    setTranscriptionError('');
    try {
      const response = await api.transcribeVoices(chat.talker, {
        messageRefs: selectedMessageRefs,
        messageIds,
        startTime,
        endTime,
        allVoices,
        force,
        cloudUploadConfirmed,
      });
      const task = unwrapData(response);
      if (!task.task_id && !isRecognitionTaskFinished(task.status)) {
        throw new Error('服务端未返回语音转写任务编号');
      }
      applyTranscriptionResults(task.results);
      setTranscriptionTask({
        task_id: task.task_id,
        total: task.total || 0,
        completed: task.completed || 0,
        cached: task.cached || 0,
        failed: task.failed || 0,
        provider: task.provider || transcriptionProviderLabel,
        is_cloud: task.is_cloud ?? transcriptionIsCloud,
        status: task.status || 'queued',
        results: task.results || [],
      });
      setShowTranscriptionDialog(false);
      setSingleVoiceMessage(null);
    } catch (error) {
      setTranscriptionError(error.message || '创建语音转写任务失败');
    } finally {
      setStartingTranscription(false);
    }
  }, [
    applyTranscriptionResults,
    chat.talker,
    messages,
    selectedIds,
    transcriptionIsCloud,
    transcriptionProviderLabel,
    transcriptionReady,
  ]);

  useEffect(() => {
    const taskId = transcriptionTask?.task_id;
    if (!taskId || isRecognitionTaskFinished(transcriptionTask.status)) return undefined;

    let cancelled = false;
    let consecutiveFailures = 0;
    const poll = async () => {
      try {
        const response = await api.getVoiceTranscriptionTask(taskId);
        if (cancelled) return;
        const task = unwrapData(response);
        consecutiveFailures = 0;
        setTranscriptionError('');
        applyTranscriptionResults(task.results);
        setTranscriptionTask((previous) => ({
          ...previous,
          ...task,
          task_id: task.task_id || taskId,
          results: task.results || previous?.results || [],
        }));
        if (!isRecognitionTaskFinished(task.status)) {
          transcriptionPollTimerRef.current = setTimeout(poll, 1200);
        }
      } catch (error) {
        if (cancelled) return;
        consecutiveFailures += 1;
        setTranscriptionError(`获取语音转写进度失败：${error.message}`);
        if (consecutiveFailures < 5) {
          transcriptionPollTimerRef.current = setTimeout(
            poll,
            Math.min(10000, 1200 * (2 ** consecutiveFailures)),
          );
        } else {
          setTranscriptionTask((previous) => ({ ...previous, status: 'error' }));
        }
      }
    };

    transcriptionPollTimerRef.current = setTimeout(poll, 500);
    return () => {
      cancelled = true;
      if (transcriptionPollTimerRef.current) clearTimeout(transcriptionPollTimerRef.current);
    };
  }, [applyTranscriptionResults, transcriptionTask?.task_id, transcriptionTask?.status]);

  const cancelTranscription = async () => {
    const taskId = transcriptionTask?.task_id;
    if (!taskId || cancellingTranscription) return;
    setCancellingTranscription(true);
    setTranscriptionError('');
    try {
      const response = await api.cancelVoiceTranscriptionTask(taskId);
      const task = unwrapData(response);
      setTranscriptionTask((previous) => ({
        ...previous,
        ...task,
        task_id: task.task_id || taskId,
        status: task.status || 'cancelling',
      }));
    } catch (error) {
      setTranscriptionError(`取消语音转写失败：${error.message}`);
    } finally {
      setCancellingTranscription(false);
    }
  };

  const schedulePinnedBottomScroll = useCallback(() => {
    if (typeof window === 'undefined') return;
    if (bottomScrollFrameRef.current) {
      window.cancelAnimationFrame(bottomScrollFrameRef.current);
    }
    bottomScrollFrameRef.current = window.requestAnimationFrame(() => {
      const pin = bottomPinRef.current;
      const container = containerRef.current;
      if (!pin.active || pin.generation !== conversationGenerationRef.current || !container) return;
      container.scrollTop = container.scrollHeight;
      bottomScrollFrameRef.current = window.requestAnimationFrame(() => {
        const latestPin = bottomPinRef.current;
        const latestContainer = containerRef.current;
        if (
          latestPin.active
          && latestPin.generation === conversationGenerationRef.current
          && latestContainer
        ) {
          latestContainer.scrollTop = latestContainer.scrollHeight;
        }
      });
    });
  }, []);

  const cancelBottomPin = useCallback(() => {
    bottomPinRef.current = {
      active: false,
      generation: conversationGenerationRef.current,
    };
    if (bottomPinTimerRef.current) {
      clearTimeout(bottomPinTimerRef.current);
      bottomPinTimerRef.current = null;
    }
    if (bottomScrollFrameRef.current && typeof window !== 'undefined') {
      window.cancelAnimationFrame(bottomScrollFrameRef.current);
      bottomScrollFrameRef.current = null;
    }
  }, []);

  const beginBottomPin = useCallback(() => {
    const generation = conversationGenerationRef.current;
    bottomPinRef.current = { active: true, generation };
    if (bottomPinTimerRef.current) clearTimeout(bottomPinTimerRef.current);
    bottomPinTimerRef.current = setTimeout(() => {
      if (bottomPinRef.current.generation === generation) {
        bottomPinRef.current = { active: false, generation };
      }
    }, 5000);
    schedulePinnedBottomScroll();
  }, [schedulePinnedBottomScroll]);

  const showNavigationNotice = useCallback((message) => {
    setNavigationNotice(message);
    if (navigationNoticeTimerRef.current) clearTimeout(navigationNoticeTimerRef.current);
    navigationNoticeTimerRef.current = setTimeout(() => setNavigationNotice(''), 3200);
  }, []);

  useEffect(() => {
    const content = messageContentRef.current;
    if (!content || typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(() => {
      if (bottomPinRef.current.active) schedulePinnedBottomScroll();
    });
    observer.observe(content);
    return () => observer.disconnect();
  }, [chat.talker, schedulePinnedBottomScroll]);

  useEffect(() => () => {
    cancelBottomPin();
    if (navigationNoticeTimerRef.current) clearTimeout(navigationNoticeTimerRef.current);
  }, [cancelBottomPin]);

  // 加载消息
  const loadMessages = useCallback(
    async (pageNum, reset = false, overrideKeyword = undefined, prepend = true, overrideMsgType = undefined, clearFilters = false) => {
      if (loading) return;
      const requestGeneration = conversationGenerationRef.current;
      setLoading(true);
      setMessageError('');

      // 加载更早消息前，记住当前滚动位置
      if (!reset && prepend && containerRef.current) {
        scrollRestoreRef.current = containerRef.current.scrollHeight;
      }

      try {
        const kw = overrideKeyword !== undefined ? overrideKeyword : (keyword || undefined);
        const mt = overrideMsgType !== undefined ? overrideMsgType : msgType;
        const res = await api.getMessages(chat.talker, {
          page: pageNum,
          pageSize: PAGE_SIZE,
          msgType: mt,
          keyword: kw,
          senderName: clearFilters ? undefined : (filterSender || undefined),
          startTime: clearFilters ? undefined : (filterStart ? Math.floor(new Date(filterStart).getTime() / 1000) : undefined),
          endTime: clearFilters ? undefined : (filterEnd ? Math.floor(new Date(filterEnd + 'T23:59:59').getTime() / 1000) : undefined),
        });
        if (requestGeneration !== conversationGenerationRef.current) return;

        const newMessages = res.messages || [];
        const totalP = res.total_pages || 1;

        if (reset) {
          setMessages(newMessages);
          setPage(pageNum);
          setHasMore(pageNum > 1);
          setHasNewer(pageNum < totalP);
        } else if (prepend) {
          // 向上加载更早的历史消息 (添加到头部)
          setMessages((prev) => [...newMessages, ...prev]);
          setPage(pageNum);
          setHasMore(pageNum > 1);
        } else {
          // 向下加载更新的消息 (添加到尾部)
          setMessages((prev) => [...prev, ...newMessages]);
          setPage(pageNum);
          setHasNewer(pageNum < totalP);
        }
      } catch (e) {
        if (requestGeneration === conversationGenerationRef.current) {
          console.error('加载消息失败:', e);
          setMessageError(e?.message || '加载聊天消息失败');
        }
      } finally {
        if (requestGeneration === conversationGenerationRef.current) setLoading(false);
      }
    },
    [chat.talker, msgType, keyword, loading, filterSender, filterStart, filterEnd]
  );

  // 加载更早消息后，恢复滚动位置（保持在当前阅读位置）
  useEffect(() => {
    if (scrollRestoreRef.current && containerRef.current) {
      const container = containerRef.current;
      const prevHeight = scrollRestoreRef.current;
      const newHeight = container.scrollHeight;
      container.scrollTop = newHeight - prevHeight;
      scrollRestoreRef.current = 0;
    }
  }, [messages]);

  // 初次加载和切换聊天 (或刷新) —— 从最后一页开始加载
  useEffect(() => {
    const generation = conversationGenerationRef.current + 1;
    conversationGenerationRef.current = generation;
    cancelBottomPin();
    setMessages([]);
    setPage(1);
    setHasMore(true);
    setHasNewer(false);
    setLoading(true);
    setKeyword('');
    setMsgType(null);
    setSelectMode(false);
    setSelectedIds(new Set());
    setFilterSender('');
    setFilterStart('');
    setFilterEnd('');
    setSearchResults([]);
    setSearching(false);
    setSearchAttempted(false);
    setSearchError('');
    searchAbortRef.current?.abort();
    searchAbortRef.current = null;
    setHighlightId(null);
    setImageLoadErrors(new Set());
    setShowRecognitionDialog(false);
    setRecognitionTask(null);
    setRecognitionError('');
    setStartingRecognition(false);
    setShowHdAutomationDialog(false);
    setHdAutomationError('');
    setStartingHdAutomation(false);
    setPausingHdAutomation(false);
    setSwitchingHdNavigationMode(false);
    setCancellingHdAutomation(false);
    setShowTranscriptionDialog(false);
    setSingleVoiceMessage(null);
    setTranscriptionTask(null);
    setTranscriptionError('');
    setStartingTranscription(false);
    setCancellingTranscription(false);
    setNavigationNotice('');
    if (navigationNoticeTimerRef.current) {
      clearTimeout(navigationNoticeTimerRef.current);
      navigationNoticeTimerRef.current = null;
    }
    if (recognitionPollTimerRef.current) clearTimeout(recognitionPollTimerRef.current);
    if (transcriptionPollTimerRef.current) clearTimeout(transcriptionPollTimerRef.current);
    isFirstLoad.current = true;
    lastPageRef.current = 0;
    scrollRestoreRef.current = 0;
    shouldScrollToBottom.current = false;
    pendingLocateRef.current = null;

    let disposed = false;
    const abortController = new AbortController();
    setMessageError('');
    const loadLatestPage = async () => {
      try {
        const firstPage = await api.getMessages(chat.talker, {
          page: 1,
          pageSize: PAGE_SIZE,
          signal: abortController.signal,
        });
        if (disposed || generation !== conversationGenerationRef.current) return;
        let loadedPage = 1;
        let latestPage = firstPage;
        let reportedLastPage = Math.max(1, Number(firstPage.total_pages) || 1);
        let chaseCount = 0;

        // The chat can receive a message while page 1 and the then-current
        // last page are being requested. Follow a moving last page a bounded
        // number of times so a busy chat cannot create an endless request loop.
        while (loadedPage !== reportedLastPage && chaseCount < LATEST_PAGE_MAX_CHASES) {
          const requestedPage = reportedLastPage;
          const response = await api.getMessages(chat.talker, {
            page: requestedPage,
            pageSize: PAGE_SIZE,
            signal: abortController.signal,
          });
          if (disposed || generation !== conversationGenerationRef.current) return;
          latestPage = response;
          loadedPage = requestedPage;
          reportedLastPage = Math.max(
            1,
            Number(response.total_pages) || requestedPage,
          );
          chaseCount += 1;
        }

        lastPageRef.current = loadedPage;
        const latestMessages = latestPage.messages || [];
        const totalP = Math.max(
          1,
          Number(latestPage.total_pages) || loadedPage,
        );
        setMessages(latestMessages);
        setPage(loadedPage);
        setHasMore(loadedPage > 1);
        setHasNewer(loadedPage < totalP);
      } catch (error) {
        if (!disposed && generation === conversationGenerationRef.current) {
          console.error('加载最新消息失败:', error);
          setMessages([]);
          setHasMore(false);
          setHasNewer(false);
          setMessageError(error?.message || '加载最新聊天消息失败');
        }
      } finally {
        if (!disposed && generation === conversationGenerationRef.current) {
          setLoading(false);
        }
      }
    };
    loadLatestPage();
    return () => {
      disposed = true;
      abortController.abort();
    };
  }, [chat.talker, refreshKey, messageReloadToken, cancelBottomPin]);

  // 初次进入会话和切换消息类型时，在图片/表情稳定前短期保持贴底。
  useLayoutEffect(() => {
    if (messages.length > 0 && containerRef.current) {
      if (isFirstLoad.current || shouldScrollToBottom.current) {
        beginBottomPin();
        isFirstLoad.current = false;
        shouldScrollToBottom.current = false;
      }
    }
  }, [messages, chat.talker, beginBottomPin]);

  // 消息类型变更时重新加载 —— 跳到最新页并滚到底部
  useEffect(() => {
    const generation = conversationGenerationRef.current;
    let disposed = false;
    let abortController = null;
    if (!isFirstLoad.current) {
      setMessages([]);
      setHasMore(true);
      setLoading(true);
      setMessageError('');
      shouldScrollToBottom.current = true;
      abortController = new AbortController();
      const loadFilteredLatest = async () => {
        try {
          const firstPage = await api.getMessages(
            chat.talker,
            { page: 1, pageSize: PAGE_SIZE, msgType, signal: abortController.signal },
          );
          if (disposed || generation !== conversationGenerationRef.current) return;
          const lastPage = Math.max(1, Number(firstPage.total_pages) || 1);
          lastPageRef.current = lastPage;
          const latestPage = lastPage === 1
            ? firstPage
            : await api.getMessages(
                chat.talker,
                { page: lastPage, pageSize: PAGE_SIZE, msgType, signal: abortController.signal },
              );
          if (disposed || generation !== conversationGenerationRef.current) return;
          const totalP = Math.max(1, Number(latestPage.total_pages) || lastPage);
          setMessages(latestPage.messages || []);
          setPage(lastPage);
          setHasMore(lastPage > 1);
          setHasNewer(lastPage < totalP);
        } catch (error) {
          if (!disposed && generation === conversationGenerationRef.current) {
            console.error('加载筛选后的最新消息失败:', error);
            setMessages([]);
            setHasMore(false);
            setHasNewer(false);
            setMessageError(error?.message || '加载筛选后的聊天消息失败');
          }
        } finally {
          if (!disposed && generation === conversationGenerationRef.current) {
            setLoading(false);
          }
        }
      };
      void loadFilteredLatest();
    }
    return () => {
      disposed = true;
      abortController?.abort();
    };
  }, [chat.talker, msgType]);

  const searchTimerRef = useRef(null);

  const doSearch = useCallback(async (kw) => {
    if (!chat?.talker) return;
    // 允许纯发送人筛选、纯时间筛选或组合筛选（无关键词）
    if (!kw && !filterSender.trim() && !filterStart && !filterEnd) return;
    searchAbortRef.current?.abort();
    const abortController = new AbortController();
    searchAbortRef.current = abortController;
    const generation = conversationGenerationRef.current;
    const talker = chat.talker;
    setHighlightId(null);
    setSearching(true);
    setSearchAttempted(false);
    setSearchError('');
    try {
      const allResults = [];
      let p = 1;
      while (true) {
        const res = await api.getMessages(talker, {
          page: p, pageSize: 100, keyword: kw,
          senderName: filterSender || undefined,
          startTime: filterStart ? Math.floor(new Date(filterStart).getTime() / 1000) : undefined,
          endTime: filterEnd ? Math.floor(new Date(filterEnd + 'T23:59:59').getTime() / 1000) : undefined,
          signal: abortController.signal,
        });
        if (
          abortController.signal.aborted
          || generation !== conversationGenerationRef.current
          || searchAbortRef.current !== abortController
        ) return;
        allResults.push(...(res.messages || []));
        if (p >= (res.total_pages || 1)) break;
        p++;
      }
      if (
        generation === conversationGenerationRef.current
        && searchAbortRef.current === abortController
      ) {
        setSearchResults(allResults.reverse());
        setSearchAttempted(true);
      }
    } catch (e) {
      if (
        !abortController.signal.aborted
        && generation === conversationGenerationRef.current
        && searchAbortRef.current === abortController
      ) {
        console.error('搜索失败:', e);
        setSearchResults([]);
        setSearchError(e?.message || '搜索聊天记录失败');
      }
    } finally {
      if (
        generation === conversationGenerationRef.current
        && searchAbortRef.current === abortController
      ) {
        searchAbortRef.current = null;
        setSearching(false);
      }
    }
  }, [chat.talker, filterSender, filterStart, filterEnd]);

  // 输入即搜索（300ms 防抖，搜索框/发送人/时间任一变化都触发）
  useEffect(() => {
    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    searchAbortRef.current?.abort();
    searchAbortRef.current = null;
    setSearching(false);
    setSearchError('');
    setSearchAttempted(false);
    if (!keyword.trim() && !filterSender.trim() && !filterStart && !filterEnd) {
      setSearchResults([]);
      return;
    }
    setSearchResults([]);
    searchTimerRef.current = setTimeout(() => doSearch(keyword.trim()), 300);
    return () => { if (searchTimerRef.current) clearTimeout(searchTimerRef.current); };
  }, [keyword, filterSender, filterStart, filterEnd, doSearch]);

  useEffect(() => () => {
    searchAbortRef.current?.abort();
    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
  }, []);

  const handleSearch = async (e) => {
    e.preventDefault();
    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    if (!keyword.trim() && !filterSender.trim() && !filterStart && !filterEnd) {
      setSearchResults([]);
      setSearchAttempted(false);
      loadMessages(1, true);
      return;
    }
    doSearch(keyword.trim());
  };

  const locateMessage = useCallback(async (msg, { requireExact = false } = {}) => {
    if (!msg || msg.id == null || msg.create_time == null) {
      showNavigationNotice('原消息已不存在或无法定位');
      return false;
    }
    if (requireExact && !msg.message_key) {
      showNavigationNotice('原消息缺少精确标识，已停止定位以避免跳错消息');
      return false;
    }
    const generation = conversationGenerationRef.current;
    const talker = chat.talker;
    cancelBottomPin();
    setLoading(true);
    try {
      const pos = await api.getMessagePosition(
        talker,
        msg.id,
        msg.create_time,
        msg.message_key,
      );
      if (generation !== conversationGenerationRef.current || talker !== chat.talker) return false;
      if (pos && pos.page) {
        // 直接加载该页全部消息（不带任何筛选条件），确保页码与 getMessagePosition 一致
        const res = await api.getMessages(talker, {
          page: pos.page,
          pageSize: PAGE_SIZE,
        });
        if (generation !== conversationGenerationRef.current || talker !== chat.talker) return false;
        const newMessages = res.messages || [];
        const totalP = res.total_pages || 1;
        const renderedTarget = newMessages.find((candidate) => (
          (msg.message_key && candidate.message_key === msg.message_key)
          || (
            Number(candidate.id) === Number(msg.id)
            && Number(candidate.create_time) === Number(msg.create_time)
            && (
              !msg.server_id
              || String(candidate.server_id || '') === String(msg.server_id || '')
            )
          )
        ));
        if (!renderedTarget) {
          showNavigationNotice('已找到历史页，但原消息记录已变化，无法安全定位');
          return false;
        }
        const targetKey = messageSelectionKey(renderedTarget);
        pendingLocateRef.current = {
          generation,
          domId: messageDomId(renderedTarget),
        };
        isFirstLoad.current = false;
        shouldScrollToBottom.current = false;
        setMessages(newMessages);
        setPage(pos.page);
        setHasMore(pos.page > 1);
        setHasNewer(pos.page < totalP);
        setHighlightId(targetKey);
        return true;
      }
      showNavigationNotice('原消息已不存在或无法定位');
      return false;
    } catch (e) {
      console.error('定位失败:', e);
      if (generation === conversationGenerationRef.current) {
        showNavigationNotice(`定位失败：${e.message || '原消息不可用'}`);
      }
      return false;
    } finally {
      if (generation === conversationGenerationRef.current) setLoading(false);
    }
  }, [chat.talker, cancelBottomPin, showNavigationNotice]);

  useLayoutEffect(() => {
    const pending = pendingLocateRef.current;
    if (!pending || pending.generation !== conversationGenerationRef.current) return;
    const element = document.getElementById(pending.domId);
    if (!element) return;
    pendingLocateRef.current = null;
    element.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }, [messages]);

  const handleSearchResultClick = async (msg) => {
    setSearchResults([]);
    setSelectMode(false);
    setKeyword('');  // 清空搜索词
    setFilterSender('');  // 清空筛选条件，加载完整上下文
    setFilterStart('');
    setFilterEnd('');
    setSearchAttempted(false);
    setSearchError('');
    await locateMessage(msg);
  };

  // 加载更早的消息 (上一页)
  const handleLoadMore = () => {
    if (hasMore && !loading) {
      loadMessages(page - 1);
    }
  };

  // 加载更新的消息 (下一页，添加到尾部)
  const handleLoadNewer = () => {
    if (hasNewer && !loading) {
      loadMessages(page + 1, false, undefined, false);
    }
  };

  // --- 选择功能 ---
  const toggleSelectMode = () => {
    if (selectMode) {
      setSelectMode(false);
      setSelectedIds(new Set());
    } else {
      setSelectMode(true);
    }
  };

  const toggleMessage = (selectionKey) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(selectionKey)) {
        next.delete(selectionKey);
      } else {
        next.add(selectionKey);
      }
      return next;
    });
  };

  const selectAll = () => {
    const allIds = messages.map(messageSelectionKey);
    setSelectedIds(new Set(allIds));
  };

  const deselectAll = () => {
    setSelectedIds(new Set());
  };

  const handleExportSelected = () => {
    if (selectedIds.size > 0 && onExportSelected) {
      // 从选中的消息中提取时间范围
      const selectedMsgs = messages.filter((message) => (
        selectedIds.has(messageSelectionKey(message))
      ));
      let startTime, endTime;
      if (selectedMsgs.length > 0) {
        const times = selectedMsgs.map((m) => m.create_time).filter(Boolean);
        if (times.length > 0) {
          startTime = Math.min(...times);
          endTime = Math.max(...times);
        }
      }
      const messageRefs = selectedMsgs.map((message) => ({
        id: message.id,
        create_time: message.create_time || 0,
        message_key: message.message_key || messageSelectionKey(message),
      }));
      onExportSelected(messageRefs, startTime, endTime);
    }
  };

  const openAnalysisForScope = (scope) => {
    let frozenScope = { ...scope };
    if (scope?.useSelected) {
      const selectedMessages = messages.filter((message) => (
        selectedIds.has(messageSelectionKey(message))
      ));
      if (selectedMessages.length !== selectedIds.size) {
        const message = '部分已选消息已因筛选或刷新离开当前列表。为避免遗漏，请取消选择后重新勾选分析范围。';
        setAnalysisError(message);
        window.alert(message);
        return;
      }
      const messageRefs = selectedMessages.map((message) => ({
        id: message.id,
        create_time: message.create_time || 0,
        message_key: message.message_key || messageSelectionKey(message),
      }));
      frozenScope = {
        ...scope,
        messageRefs,
        scopeSummary: `已选 ${messageRefs.length} 条聊天记录`,
      };
    }
    setShowAnalysisRangeDialog(false);
    setAnalysisScope(frozenScope);
    setAnalysisError('');
    setAnalysisReport(null);
    setShowAnalysisDialog(true);
  };

  const startAiAnalysis = async (options) => {
    if (!analysisReady) {
      setAnalysisError('AI 分析配置不完整，请先在设置中填写接口地址、模型和 API Key');
      return;
    }
    if (!analysisScope) {
      setAnalysisError('尚未选择聊天记录分析范围');
      return;
    }
    const messageRefs = analysisScope.useSelected
      ? [...(analysisScope.messageRefs || [])]
      : undefined;
    if (
      analysisScope.useSelected
      && (!messageRefs.length || messageRefs.length !== selectedIds.size)
    ) {
      setAnalysisError('所选消息范围已经变化，请关闭窗口后重新选择');
      return;
    }

    const analysisTalker = chat.talker;
    const requestId = analysisRequestIdRef.current + 1;
    analysisRequestIdRef.current = requestId;
    analysisAbortRef.current?.abort();
    const abortController = new AbortController();
    analysisAbortRef.current = abortController;
    setAnalysisRunning(true);
    setAnalysisError('');
    try {
      const response = await api.analyzeChat(analysisTalker, {
        displayName: chat.display_name,
        messageRefs,
        startTime: analysisScope.startTime,
        endTime: analysisScope.endTime,
        allMessages: Boolean(analysisScope.allMessages),
        presetId: options.presetId,
        strength: options.strength,
        detail: options.detail,
        requirements: options.requirements,
        reportTitle: `${chat.display_name}_聊天记录AI分析`,
        cloudUploadConfirmed: options.thirdPartyConfirmed,
        signal: abortController.signal,
      });
      const result = response?.data?.report_markdown ? response.data : response;
      const markdown = result?.report_markdown || result?.markdown || '';
      if (!markdown) throw new Error('服务端未返回分析报告');
      if (
        analysisContextRef.current !== analysisTalker
        || analysisRequestIdRef.current !== requestId
      ) return;
      setAnalysisReport({
        markdown,
        title: result?.report_title || `${chat.display_name}聊天记录 AI 分析报告`,
        filename: result?.filename || `${chat.display_name}_聊天记录AI分析.md`,
        path: result?.path || '',
        warning: result?.warning || '',
        metadata: result?.metadata || {},
      });
    } catch (error) {
      if (
        analysisContextRef.current !== analysisTalker
        || analysisRequestIdRef.current !== requestId
      ) return;
      setAnalysisError(
        abortController.signal.aborted
          ? 'AI 分析已取消'
          : (error.message || 'AI 分析失败，请重试'),
      );
    } finally {
      if (analysisAbortRef.current === abortController) {
        analysisAbortRef.current = null;
      }
      if (
        analysisContextRef.current === analysisTalker
        && analysisRequestIdRef.current === requestId
      ) {
        setAnalysisRunning(false);
      }
    }
  };

  const cancelAiAnalysis = () => {
    if (!analysisAbortRef.current) return;
    setAnalysisError('正在停止 AI 分析…');
    analysisAbortRef.current.abort();
  };

  const downloadAnalysisReport = ({ markdown }) => {
    const content = markdown || analysisReport?.markdown;
    if (!content) return;
    const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = analysisReport?.filename || `${chat.display_name}_聊天记录AI分析.md`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  };

  // 格式化消息内容
  const formatContent = (content) => {
    if (!content) return '[空消息]';

    // 将链接转为可点击
    const urlRegex = /(https?:\/\/[^\s]+)/g;
    const parts = content.split(urlRegex);

    return parts.map((part, i) => {
      if (urlRegex.test(part)) {
        return (
          <a
            key={i}
            href={part}
            target="_blank"
            rel="noopener noreferrer"
            className="text-blue-500 underline break-all"
            onClick={(e) => e.stopPropagation()}
          >
            {part}
          </a>
        );
      }
      return <span key={i}>{part}</span>;
    });
  };

  // 消息类型标签颜色
  const typeBadgeStyle = (type, typeName = '') => {
    if (typeName === '红包') return 'bg-red-100 text-red-700';
    if (typeName === '转账') return 'bg-amber-100 text-amber-700';
    const colors = {
      1: 'bg-blue-100 text-blue-700',      // 文本
      3: 'bg-purple-100 text-purple-700',  // 图片
      4: 'bg-orange-100 text-orange-700',  // 语音（兼容类型）
      34: 'bg-orange-100 text-orange-700', // 语音
      43: 'bg-pink-100 text-pink-700',     // 视频
      47: 'bg-yellow-100 text-yellow-700', // 表情
      49: 'bg-cyan-100 text-cyan-700',     // 链接
    };
    return colors[type] || 'bg-gray-100 text-gray-600';
  };

  const recognitionRunning = Boolean(
    recognitionTask && !isRecognitionTaskFinished(recognitionTask.status)
  );
  const analysisReady = Boolean(
    settings?.analysis?.base_url
    && settings?.analysis?.model
    && (
      settings?.analysis?.requires_api_key === false
      || settings?.analysis?.has_api_key
    )
  );
  const analysisProviderLabel = getInterfaceLabel('analysis', settings?.analysis);
  const hdAutomationActive = Boolean(
    hdAutomationTask && !isRecognitionTaskFinished(hdAutomationTask.status)
  );
  const hdAutomationTotal = Number(hdAutomationTask?.total || 0);
  const hdAutomationCompleted = Math.min(
    hdAutomationTotal,
    Number(hdAutomationTask?.completed || 0),
  );
  const hdAutomationDownloaded = Number(hdAutomationTask?.downloaded || 0);
  const hdAutomationSkipped = Number(hdAutomationTask?.skipped || 0);
  const hdAutomationExpired = Number(hdAutomationTask?.expired_count || 0);
  const hdAutomationFailed = Number(hdAutomationTask?.failed_count || 0);
  const hdAutomationUnconfirmed = Number(
    hdAutomationTask?.unconfirmed_navigation_count || 0
  );
  const hdAutomationManualNavigations = Number(
    hdAutomationTask?.manual_navigation_count || 0
  );
  const hdAutomationNavigationMode = hdAutomationTask?.navigation_mode === 'manual'
    ? 'manual'
    : 'auto';
  const hdAutomationPauseKind = String(hdAutomationTask?.navigation_pause_kind || '');
  const hdAutomationDirectionLabel = hdAutomationTask?.direction === 'previous'
    ? '上一项'
    : '下一项';
  const hdAutomationPercent = hdAutomationTotal > 0
    ? Math.round((hdAutomationCompleted / hdAutomationTotal) * 100)
    : (isRecognitionTaskFinished(hdAutomationTask?.status) ? 100 : 0);
  const hdAutomationStatusText = {
    awaiting_viewer: '等待你在微信中打开首张图片',
    running: '正在通过电脑版微信获取高清图片',
    paused: hdAutomationPauseKind
      ? (hdAutomationNavigationMode === 'manual' ? '等待手动翻页' : '自动翻页已暂停')
      : '任务已暂停',
    cancelling: '正在停止任务',
    completed: hdAutomationTotal === 0 ? '范围内没有需要处理的图片' : '高清图片批量处理完成',
    cancelled: '任务已停止',
    failed: '高清图片任务失败',
    error: '高清图片任务状态获取失败',
  }[hdAutomationTask?.status] || '高清图片自动化任务';
  const hdAutomationFirstTargetTime = (
    hdAutomationTask?.first_target?.time_str
    || (hdAutomationTask?.first_target?.create_time
      ? new Date(Number(hdAutomationTask.first_target.create_time) * 1000).toLocaleString()
      : '')
  );
  const hdAutomationCurrentText = (() => {
    const current = hdAutomationTask?.current;
    if (!current) return '';
    if (typeof current === 'string' || typeof current === 'number') return String(current);
    return current.time_str
      || (current.create_time ? new Date(Number(current.create_time) * 1000).toLocaleString() : '')
      || (current.index != null ? `第 ${Number(current.index) + 1} 张` : '');
  })();
  const hdAutomationStartHotkey = (
    hdAutomationTask?.hotkeys?.start_pause
    || hdAutomationTask?.hotkeys?.start_resume
    || 'Ctrl+Alt+F9'
  );
  const hdAutomationPauseHotkey = hdAutomationTask?.hotkeys?.pause || 'Ctrl+Alt+F10';
  const hdAutomationStopHotkey = hdAutomationTask?.hotkeys?.stop || 'Ctrl+Alt+F11';
  const loadedImageCount = messages.filter(
    (message) => [2, 3].includes(Number(message.type))
  ).length;
  const recognitionTotal = Number(recognitionTask?.total || 0);
  const recognitionCompleted = Number(recognitionTask?.completed || 0);
  const recognitionFailed = Number(recognitionTask?.failed || 0);
  const recognitionProcessed = Math.min(
    recognitionTotal,
    recognitionCompleted
  );
  const recognitionPercent = recognitionTotal > 0
    ? Math.round((recognitionProcessed / recognitionTotal) * 100)
    : (isRecognitionTaskFinished(recognitionTask?.status) ? 100 : 0);

  const recognitionStatusText = {
    queued: '等待识别',
    pending: '等待识别',
    running: '正在识别',
    processing: '正在识别',
    completed: recognitionTotal === 0 ? '范围内没有需要识别的图片' : '识别完成',
    succeeded: '识别完成',
    failed: '任务失败',
    cancelled: '任务已取消',
    error: '任务状态获取失败',
  }[recognitionTask?.status] || '图片识别任务';

  const transcriptionRunning = Boolean(
    transcriptionTask && !isRecognitionTaskFinished(transcriptionTask.status)
  );
  const transcriptionTotal = Number(transcriptionTask?.total || 0);
  const transcriptionCompleted = Number(transcriptionTask?.completed || 0);
  const transcriptionFailed = Number(transcriptionTask?.failed || 0);
  const transcriptionProcessed = Math.min(transcriptionTotal, transcriptionCompleted);
  const transcriptionPercent = transcriptionTotal > 0
    ? Math.round((transcriptionProcessed / transcriptionTotal) * 100)
    : (isRecognitionTaskFinished(transcriptionTask?.status) ? 100 : 0);
  const transcriptionStatusText = {
    queued: '等待语音转写',
    pending: '等待语音转写',
    running: '正在转写语音',
    processing: '正在转写语音',
    cancelling: '正在取消语音转写',
    completed: transcriptionTotal === 0 ? '范围内没有语音消息' : '语音转写完成',
    succeeded: '语音转写完成',
    failed: '语音转写任务失败',
    cancelled: '语音转写任务已取消',
    error: '语音转写任务状态获取失败',
  }[transcriptionTask?.status] || '语音转写任务';

  return (
    <div className="flex h-full flex-col bg-[#f5f7f6]">
      {navigationNotice && createPortal(
        <div
          role="status"
          className="pointer-events-none fixed bottom-20 left-1/2 z-[120] max-w-[min(90vw,32rem)] -translate-x-1/2 rounded-xl bg-slate-900/90 px-4 py-2.5 text-center text-sm text-white shadow-xl backdrop-blur"
        >
          {navigationNotice}
        </div>,
        document.body,
      )}
      {/* 聊天标题栏 */}
      <div className="flex flex-wrap items-center justify-between gap-x-3 bg-white px-4 pt-3 sm:px-5">
        <div className="flex items-center gap-3 min-w-0">
          <Avatar
            src={chat.avatar_url}
            name={chat.display_name}
            isGroup={chat.is_group}
            className="h-9 w-9"
          />
          <div className="min-w-0">
            <h2 className="truncate text-sm font-semibold tracking-tight text-slate-900">{chat.display_name}</h2>
            <p className="mt-0.5 text-xs text-slate-400">
              {chat.message_count.toLocaleString()} 条消息
              {selectMode && selectedIds.size > 0 && (
                <span className="text-wechat-green font-medium ml-2">
                  · 已选 {selectedIds.size} 条
                </span>
              )}
            </p>
          </div>
        </div>

        <div
          className="mt-3 flex flex-wrap items-center gap-1.5 border-y border-slate-200/80 bg-slate-50/80 px-4 py-2 sm:px-5"
          aria-label="聊天处理工具"
        >
          <span className="mr-1 flex-shrink-0 text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-400">浏览</span>
          {/* 图片显示开关 */}
          <button
            type="button"
            onClick={handleToggleImages}
            className={`flex h-8 flex-shrink-0 items-center gap-1.5 rounded-lg border px-2.5 text-xs font-medium transition ${
              showImages
                ? 'border-emerald-200 bg-emerald-50 text-emerald-700 hover:bg-emerald-100'
                : 'border-slate-200 bg-white text-slate-500 hover:border-slate-300 hover:text-slate-700'
            }`}
            title={showImages ? '点击隐藏聊天图片' : '点击显示聊天图片'}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
              {showImages ? (
                <>
                  <path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z" />
                  <circle cx="12" cy="12" r="2.5" />
                </>
              ) : (
                <>
                  <path d="m3 3 18 18" />
                  <path d="M10.6 6.2A10.6 10.6 0 0 1 12 6c6 0 9.5 6 9.5 6a17 17 0 0 1-2.1 2.8M6.2 6.2C3.8 8 2.5 12 2.5 12s3.5 6 9.5 6c1 0 1.9-.2 2.7-.4" />
                </>
              )}
            </svg>
            {showImages
              ? `显示图片 · ${imageQuality === 'high' ? '高清' : (imageQuality === 'smart' ? '智能' : '流畅')}`
              : '图片已隐藏'}
          </button>

          <select
            value={msgType || ''}
            onChange={(e) => setMsgType(e.target.value ? Number(e.target.value) : null)}
            className="h-8 flex-shrink-0 rounded-lg border border-slate-200 bg-white px-2 text-xs font-medium text-slate-600 outline-none transition hover:border-slate-300 focus:border-emerald-500 focus:ring-2 focus:ring-emerald-100"
            aria-label="消息类型"
          >
            <option value="">全部类型</option>
            <option value="1">文本</option>
            <option value="3">图片</option>
            <option value="34">语音</option>
            <option value="4">语音（兼容类型）</option>
            <option value="43">视频</option>
            <option value="47">表情</option>
            <option value="49">链接/卡片（含红包、转账）</option>
            <option value="10000">系统消息</option>
          </select>

          <span className="mx-1 h-5 w-px flex-shrink-0 bg-slate-200" aria-hidden="true" />
          <span className="mr-1 flex-shrink-0 text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-400">处理</span>

          {/* 仅在用户显式创建任务并按全局热键后，才控制电脑版微信图片查看器。 */}
          <button
            type="button"
            onClick={() => {
              setHdAutomationError('');
              setShowHdAutomationDialog(true);
            }}
            disabled={
              startingHdAutomation
              || hdAutomationActive
              || recognitionRunning
              || transcriptionRunning
            }
            className="flex h-8 flex-shrink-0 items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-2.5 text-xs font-medium text-slate-600 transition hover:border-slate-300 hover:bg-slate-50 hover:text-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
            title="选择范围，通过 Windows UI 自动操作电脑版微信获取高清缓存"
          >
            {hdAutomationActive ? (
              <span className="h-3 w-3 animate-spin rounded-full border-2 border-slate-400 border-t-transparent" />
            ) : (
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                <rect x="3" y="4" width="18" height="16" rx="2" />
                <circle cx="8.5" cy="9" r="1.5" />
                <path d="m21 15-5-5L5 20" />
              </svg>
            )}
            {hdAutomationActive ? '高清获取中' : '批量获取高清'}
          </button>

          {/* 用户点击后才会创建图片识别任务 */}
          <button
            type="button"
            onClick={() => {
              setRecognitionError('');
              setShowRecognitionDialog(true);
            }}
            disabled={startingRecognition || recognitionRunning || transcriptionRunning || hdAutomationActive}
            className="flex h-8 flex-shrink-0 items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-2.5 text-xs font-medium text-slate-600 transition hover:border-slate-300 hover:bg-slate-50 hover:text-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
            title={visionReady ? '选择范围并识别图片' : '需要先在设置中配置图片识别模型'}
          >
            {recognitionRunning ? (
              <span className="h-3 w-3 animate-spin rounded-full border-2 border-slate-400 border-t-transparent" />
            ) : (
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                <circle cx="10.5" cy="10.5" r="6" />
                <path d="m15 15 5 5M10.5 7.5v6M7.5 10.5h6" />
              </svg>
            )}
            {recognitionRunning ? '识别中' : '图片识别'}
          </button>

          {/* 语音只在用户点击并确认范围后转写；云端还会逐次确认上传。 */}
          <button
            type="button"
            onClick={() => {
              setTranscriptionError('');
              setSingleVoiceMessage(null);
              setShowTranscriptionDialog(true);
            }}
            disabled={startingTranscription || transcriptionRunning || recognitionRunning || hdAutomationActive}
            className="flex h-8 flex-shrink-0 items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-2.5 text-xs font-medium text-slate-600 transition hover:border-slate-300 hover:bg-slate-50 hover:text-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
            title={transcriptionReady ? '选择范围并转写语音' : '需要先在设置中配置语音转文字'}
          >
            {transcriptionRunning ? (
              <span className="h-3 w-3 animate-spin rounded-full border-2 border-slate-400 border-t-transparent" />
            ) : (
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                <rect x="9" y="3" width="6" height="11" rx="3" />
                <path d="M5 11a7 7 0 0 0 14 0M12 18v3M8 21h8" />
              </svg>
            )}
            {transcriptionRunning ? '转写中' : '语音转文字'}
          </button>

          <button
            type="button"
            onClick={() => {
              setAnalysisError('');
              setShowAnalysisRangeDialog(true);
            }}
            disabled={analysisRunning}
            className="flex h-8 flex-shrink-0 items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-2.5 text-xs font-medium text-slate-600 transition hover:border-slate-300 hover:bg-slate-50 hover:text-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
            title={analysisReady ? '选择聊天记录范围并生成 AI 分析报告' : '需要先在设置中配置 AI 分析模型'}
          >
            {analysisRunning ? (
              <span className="h-3 w-3 animate-spin rounded-full border-2 border-slate-400 border-t-transparent" />
            ) : (
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                <path d="m12 3 1.3 4.2L17.5 8.5l-4.2 1.3L12 14l-1.3-4.2-4.2-1.3 4.2-1.3L12 3Z" />
                <path d="m18 14 .8 2.2 2.2.8-2.2.8L18 20l-.8-2.2L15 17l2.2-.8L18 14Z" />
              </svg>
            )}
            {analysisRunning ? '分析中' : 'AI 分析'}
          </button>

          <span className="mx-1 h-5 w-px flex-shrink-0 bg-slate-200" aria-hidden="true" />
          <span className="mr-1 flex-shrink-0 text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-400">整理</span>

          {/* 选择模式按钮 */}
          <button
            onClick={toggleSelectMode}
            className={`flex h-8 flex-shrink-0 items-center gap-1.5 rounded-lg border px-2.5 text-xs font-medium transition ${
              selectMode
                ? 'border-emerald-500 bg-emerald-500 text-white'
                : 'border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:bg-slate-50'
            }`}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M9 11l3 3L22 4" />
              <path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11" />
            </svg>
            {selectMode ? '取消选择' : '选择消息'}
          </button>

          {/* 导出按钮 (选择模式下变灰) */}
          <button
            onClick={onExport}
            disabled={selectMode}
            className={`flex h-8 flex-shrink-0 items-center gap-1.5 rounded-lg border px-2.5 text-xs font-semibold transition ${
              selectMode
                ? 'cursor-not-allowed border-slate-200 bg-slate-100 text-slate-400'
                : 'border-emerald-600 bg-emerald-600 text-white shadow-sm hover:bg-emerald-700'
            }`}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M7 10l5 5 5-5M12 15V3" />
            </svg>
            导出
          </button>

        </div>
      </div>

      {/* 搜索栏 */}
      <form onSubmit={handleSearch} className="border-b border-slate-200/80 bg-white px-4 py-2 sm:px-5">
        <div className="relative">
          <input
            type="text"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            placeholder="搜索聊天内容..."
            aria-label="搜索聊天内容"
            className="h-9 w-full rounded-xl border border-slate-200 bg-slate-50/70 pl-9 pr-3 text-sm text-slate-700 outline-none transition placeholder:text-slate-400 hover:border-slate-300 focus:border-emerald-500 focus:bg-white focus:ring-2 focus:ring-emerald-100"
          />
          <svg
            className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-400"
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
          >
            <circle cx="11" cy="11" r="8" />
            <path d="M21 21l-4.35-4.35" />
          </svg>
        </div>
      </form>

      {/* 搜索筛选栏 */}
      {chat.is_group && (
        <div className="flex items-center gap-2 overflow-x-auto border-b border-slate-200/80 bg-white px-4 py-2 text-xs sm:px-5">
          <span className="flex-shrink-0 font-medium text-slate-400">高级筛选</span>
          <input
            type="text"
            value={filterSender}
            onChange={(e) => setFilterSender(e.target.value)}
            placeholder="发送人"
            className="h-8 w-24 flex-shrink-0 rounded-lg border border-slate-200 bg-slate-50 px-2 text-xs outline-none focus:border-emerald-500 focus:bg-white"
          />
          <input
            type="date"
            value={filterStart}
            onChange={(e) => setFilterStart(e.target.value)}
            className="h-8 flex-shrink-0 rounded-lg border border-slate-200 bg-slate-50 px-2 text-xs outline-none focus:border-emerald-500 focus:bg-white"
          />
          <span className="text-slate-400">至</span>
          <input
            type="date"
            value={filterEnd}
            onChange={(e) => setFilterEnd(e.target.value)}
            className="h-8 flex-shrink-0 rounded-lg border border-slate-200 bg-slate-50 px-2 text-xs outline-none focus:border-emerald-500 focus:bg-white"
          />
          {(filterSender || filterStart || filterEnd) && (
            <button
              onClick={() => { setFilterSender(''); setFilterStart(''); setFilterEnd(''); }}
              className="flex-shrink-0 font-medium text-slate-400 hover:text-slate-700"
            >
              ✕ 清除
            </button>
          )}
        </div>
      )}

      {(searching || searchError || (searchAttempted && searchResults.length === 0)) && (
        <div
          role={searchError ? 'alert' : 'status'}
          className={`flex items-center gap-2 border-b px-4 py-2 text-xs sm:px-5 ${
            searchError
              ? 'border-red-200 bg-red-50 text-red-700'
              : 'border-slate-200 bg-slate-50 text-slate-500'
          }`}
        >
          {searching && (
            <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-emerald-500 border-t-transparent" aria-hidden="true" />
          )}
          <span className="min-w-0 flex-1">
            {searching
              ? '正在搜索聊天记录…'
              : searchError || '没有符合条件的搜索结果'}
          </span>
          {searchError && (
            <button
              type="button"
              onClick={() => doSearch(keyword.trim())}
              className="flex-shrink-0 font-medium underline underline-offset-2"
            >
              重试
            </button>
          )}
        </div>
      )}

      {/* 搜索结果列表 */}
      {searchResults.length > 0 && (
        <div className="bg-white border-b border-gray-200 max-h-80 overflow-y-auto">
          <div className="px-4 py-1.5 bg-gray-50 flex items-center justify-between text-xs text-gray-500">
            <span>找到 {searchResults.length} 条结果</span>
            <button
              type="button"
              onClick={() => {
                setSearchResults([]);
                setSearchAttempted(false);
                setHighlightId(null);
              }}
              className="text-gray-400 hover:text-gray-600"
              aria-label="关闭搜索结果"
            >
              ✕
            </button>
          </div>
          {searchResults.map((msg) => (
            <button
              type="button"
              key={messageSelectionKey(msg)}
              onClick={() => handleSearchResultClick(msg)}
              className="block w-full border-b border-gray-100 px-4 py-2 text-left text-xs hover:bg-green-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-emerald-300"
            >
              <div className="flex items-center gap-2 text-gray-400 mb-0.5">
                <span className={`font-medium ${msg.is_sender ? 'text-wechat-green' : 'text-blue-500'}`}>
                  {msg.is_sender ? '我' : (msg.sender_name || chat.display_name)}
                </span>
                <span>{msg.time_str}</span>
              </div>
              <div className="text-gray-700 truncate">{msg.content_preview}</div>
            </button>
          ))}
        </div>
      )}

      {/* Windows UI 自动化任务状态。恢复只使用全局热键，避免网页抢走微信焦点。 */}
      {(hdAutomationTask || hdAutomationError) && (
        <div className={`border-b px-4 py-3 ${
          hdAutomationError ? 'border-red-200 bg-red-50' : 'border-purple-200 bg-purple-50'
        }`}>
          {hdAutomationTask && (
            <div className="space-y-2">
              <div className="flex items-start gap-3">
                {['running', 'cancelling'].includes(hdAutomationTask.status) && (
                  <span className="mt-0.5 h-4 w-4 flex-shrink-0 animate-spin rounded-full border-2 border-purple-500 border-t-transparent" />
                )}
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
                    <span className="font-medium text-purple-900">{hdAutomationStatusText}</span>
                    <span className="text-purple-700">
                      {hdAutomationCompleted} / {hdAutomationTotal}
                      {hdAutomationDownloaded > 0 && ` · 下载 ${hdAutomationDownloaded}`}
                      {hdAutomationSkipped > 0 && ` · 跳过 ${hdAutomationSkipped}`}
                      {hdAutomationExpired > 0 && ` · 过期 ${hdAutomationExpired}`}
                      {hdAutomationFailed > 0 && ` · 失败 ${hdAutomationFailed}`}
                      {hdAutomationUnconfirmed > 0 && ` · 弱变化翻页 ${hdAutomationUnconfirmed}`}
                      {hdAutomationManualNavigations > 0 && ` · 手动翻页 ${hdAutomationManualNavigations}`}
                    </span>
                  </div>
                  <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-purple-100">
                    <div
                      className="h-full rounded-full bg-purple-600 transition-all duration-300"
                      style={{ width: `${hdAutomationPercent}%` }}
                    />
                  </div>
                  {hdAutomationCurrentText && hdAutomationTask.status === 'running' && (
                    <div className="mt-1 text-xs text-purple-700">
                      当前图片：{hdAutomationCurrentText}
                    </div>
                  )}
                  {hdAutomationTask.navigation_warning && (
                    <div className="mt-1 text-xs text-amber-700">
                      {hdAutomationTask.navigation_warning}
                    </div>
                  )}
                </div>

                {hdAutomationTask.status === 'running' && (
                  <button
                    type="button"
                    onClick={pauseHdAutomation}
                    disabled={pausingHdAutomation}
                    className="flex-shrink-0 rounded-md border border-purple-300 bg-white px-2.5 py-1 text-xs text-purple-700 hover:bg-purple-100 disabled:opacity-50"
                    title={`也可以在微信窗口按 ${hdAutomationPauseHotkey}`}
                  >
                    {pausingHdAutomation ? '暂停中...' : '暂停'}
                  </button>
                )}
                {hdAutomationActive && (
                  <button
                    type="button"
                    onClick={cancelHdAutomation}
                    disabled={cancellingHdAutomation || hdAutomationTask.status === 'cancelling'}
                    className="flex-shrink-0 rounded-md border border-red-300 bg-white px-2.5 py-1 text-xs text-red-600 hover:bg-red-50 disabled:opacity-50"
                    title={`紧急停止也可按 ${hdAutomationStopHotkey}`}
                  >
                    {cancellingHdAutomation ? '停止中...' : '停止'}
                  </button>
                )}
                {!hdAutomationActive && (
                  <button
                    type="button"
                    onClick={() => setHdAutomationTask(null)}
                    className="flex-shrink-0 text-xs text-purple-500 hover:text-purple-700"
                    title="关闭任务状态"
                  >
                    ✕
                  </button>
                )}
              </div>

              {hdAutomationTask.status === 'awaiting_viewer' && (
                <div className="rounded-lg border border-purple-200 bg-white/80 px-3 py-2 text-xs leading-relaxed text-gray-700">
                  <p className="font-medium text-purple-900">请完成以下准备后再启动：</p>
                  <ol className="mt-1 list-decimal space-y-1 pl-4">
                    <li>切换到电脑版微信并进入“{hdAutomationTask.chat_display_name || '目标聊天'}”。</li>
                    <li>
                      手动打开提示时间附近的第一张未高清图片
                      {hdAutomationFirstTargetTime && <strong className="ml-1 text-purple-800">（{hdAutomationFirstTargetTime}）</strong>}。
                    </li>
                    <li>
                      保持微信图片查看器在前台，按
                      <kbd className="mx-1 rounded border border-purple-300 bg-purple-100 px-1.5 py-0.5 font-mono font-semibold text-purple-900">
                        {hdAutomationStartHotkey}
                      </kbd>
                      开始。
                    </li>
                  </ol>
                  <p className="mt-2 font-medium text-red-600">启动后请勿操作鼠标键盘，也不要关闭或遮挡微信窗口。</p>
                </div>
              )}

              {hdAutomationTask.status === 'running' && (
                <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900">
                  微信窗口正在由自动化程序控制，请勿操作鼠标键盘。暂停热键：
                  <kbd className="mx-1 rounded border border-amber-300 bg-white px-1 py-0.5 font-mono">{hdAutomationPauseHotkey}</kbd>
                  ，紧急停止：
                  <kbd className="mx-1 rounded border border-amber-300 bg-white px-1 py-0.5 font-mono">{hdAutomationStopHotkey}</kbd>。
                </div>
              )}

              {hdAutomationTask.status === 'paused' && (
                <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs leading-relaxed text-amber-900">
                  {hdAutomationTask.pause_reason && <p className="mb-1">暂停原因：{hdAutomationTask.pause_reason}</p>}
                  {hdAutomationPauseKind === 'auto_before_send' && (
                    <p>
                      本次方向键尚未发出。
                      {hdAutomationNavigationMode === 'manual'
                        ? `请回到微信，手动且只切换一次到${hdAutomationDirectionLabel}媒体（图片或视频），再按 `
                        : '如需重试自动翻页，回到微信后直接按 '}
                      <kbd className="mx-1 rounded border border-amber-300 bg-white px-1.5 py-0.5 font-mono font-semibold">
                        {hdAutomationStartHotkey}
                      </kbd>
                      。
                    </p>
                  )}
                  {hdAutomationPauseKind === 'auto_after_send' && (
                    <p>
                      本次自动方向键已经发出。请先查看微信当前位置：如果仍是原媒体，只手动切换一次到{hdAutomationDirectionLabel}；如果已经切换，请不要再翻页。确认后按
                      <kbd className="mx-1 rounded border border-amber-300 bg-white px-1.5 py-0.5 font-mono font-semibold">
                        {hdAutomationStartHotkey}
                      </kbd>
                      继续。
                    </p>
                  )}
                  {hdAutomationPauseKind === 'manual_required' && (
                    <p>
                      {hdAutomationNavigationMode === 'manual'
                        ? `请回到微信查看器，手动且只切换一次到${hdAutomationDirectionLabel}媒体（图片或视频），等待画面稳定后按 `
                        : '已经切回自动翻页。请不要手动翻页，回到微信查看器后直接按 '}
                      <kbd className="mx-1 rounded border border-amber-300 bg-white px-1.5 py-0.5 font-mono font-semibold">
                        {hdAutomationStartHotkey}
                      </kbd>
                      {hdAutomationNavigationMode === 'manual' ? ' 确认。' : ' 让程序自动继续。'}
                    </p>
                  )}
                  {!hdAutomationPauseKind && (
                    <p>
                      请先让电脑版微信图片查看器回到前台，然后直接在微信窗口按
                      <kbd className="mx-1 rounded border border-amber-300 bg-white px-1.5 py-0.5 font-mono font-semibold">
                        {hdAutomationStartHotkey}
                      </kbd>
                      恢复；不要回到网页点击恢复，以免抢走微信焦点。
                    </p>
                  )}
                  <div className="mt-2 flex flex-wrap gap-2">
                    {hdAutomationNavigationMode === 'auto' && hdAutomationTask.can_switch_to_manual && (
                      <button
                        type="button"
                        onClick={() => switchHdNavigationMode('manual')}
                        disabled={switchingHdNavigationMode}
                        className="rounded-md border border-amber-400 bg-white px-2.5 py-1 font-medium text-amber-800 hover:bg-amber-100 disabled:opacity-50"
                      >
                        {switchingHdNavigationMode ? '切换中...' : '改用手动翻页'}
                      </button>
                    )}
                    {hdAutomationNavigationMode === 'manual' && hdAutomationPauseKind && (
                      <button
                        type="button"
                        onClick={() => switchHdNavigationMode('auto')}
                        disabled={switchingHdNavigationMode}
                        className="rounded-md border border-purple-300 bg-white px-2.5 py-1 font-medium text-purple-700 hover:bg-purple-50 disabled:opacity-50"
                      >
                        {switchingHdNavigationMode ? '切换中...' : '改回自动翻页'}
                      </button>
                    )}
                  </div>
                </div>
              )}

              {hdAutomationTask?.error && (
                <div className="break-words text-xs text-red-600">{hdAutomationTask.error}</div>
              )}
            </div>
          )}
          {hdAutomationError && (
            <div className="mt-1 flex items-start justify-between gap-3 text-xs text-red-600">
              <span>{hdAutomationError}</span>
              <button
                type="button"
                onClick={() => setHdAutomationError('')}
                className="flex-shrink-0 hover:text-red-800"
                title="关闭错误提示"
              >
                ✕
              </button>
            </div>
          )}
        </div>
      )}

      {/* 图片识别任务状态 */}
      {(recognitionTask || recognitionError) && (
        <div className={`border-b px-4 py-2 ${
          recognitionError ? 'border-red-200 bg-red-50' : 'border-blue-200 bg-blue-50'
        }`}>
          {recognitionTask && (
            <div className="flex items-center gap-3">
              {recognitionRunning && (
                <span className="w-4 h-4 flex-shrink-0 border-2 border-blue-500 border-t-transparent rounded-full animate-spin" />
              )}
              <div className="flex-1 min-w-0">
                <div className="flex items-center justify-between gap-3 text-xs">
                  <span className="font-medium text-blue-800">{recognitionStatusText}</span>
                  <span className="text-blue-600 flex-shrink-0">
                    {recognitionProcessed} / {recognitionTotal}
                    {Number(recognitionTask?.cached || 0) > 0 && ` · 缓存 ${recognitionTask.cached}`}
                    {recognitionFailed > 0 && ` · 失败 ${recognitionFailed}`}
                  </span>
                </div>
                <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-blue-100">
                  <div
                    className="h-full rounded-full bg-blue-500 transition-all duration-300"
                    style={{ width: `${recognitionPercent}%` }}
                  />
                </div>
                {recognitionTask?.error && (
                  <div className="mt-1 text-xs text-red-600 break-words">
                    {recognitionTask.error}
                  </div>
                )}
              </div>
              {!recognitionRunning && (
                <button
                  type="button"
                  onClick={() => setRecognitionTask(null)}
                  className="text-xs text-blue-500 hover:text-blue-700"
                  title="关闭任务状态"
                >
                  ✕
                </button>
              )}
            </div>
          )}
          {recognitionError && (
            <div className="mt-1 flex items-start justify-between gap-3 text-xs text-red-600">
              <span>{recognitionError}</span>
              <button
                type="button"
                onClick={() => setRecognitionError('')}
                className="flex-shrink-0 hover:text-red-800"
                title="关闭错误提示"
              >
                ✕
              </button>
            </div>
          )}
        </div>
      )}

      {/* 语音转写使用独立任务状态，不会占用或覆盖图片识别进度。 */}
      {(transcriptionTask || transcriptionError) && (
        <div className={`border-b px-4 py-2 ${
          transcriptionError ? 'border-red-200 bg-red-50' : 'border-orange-200 bg-orange-50'
        }`}>
          {transcriptionTask && (
            <div className="flex items-center gap-3">
              {transcriptionRunning && (
                <span className="h-4 w-4 flex-shrink-0 animate-spin rounded-full border-2 border-orange-500 border-t-transparent" />
              )}
              <div className="min-w-0 flex-1">
                <div className="flex items-center justify-between gap-3 text-xs">
                  <span className="font-medium text-orange-800">
                    {transcriptionStatusText}
                    {transcriptionTask?.is_cloud && (
                      <span className="ml-1 font-normal text-amber-700">· 云端</span>
                    )}
                  </span>
                  <span className="flex-shrink-0 text-orange-700">
                    {transcriptionProcessed} / {transcriptionTotal}
                    {Number(transcriptionTask?.cached || 0) > 0 && ` · 缓存 ${transcriptionTask.cached}`}
                    {transcriptionFailed > 0 && ` · 失败 ${transcriptionFailed}`}
                  </span>
                </div>
                <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-orange-100">
                  <div
                    className="h-full rounded-full bg-orange-500 transition-all duration-300"
                    style={{ width: `${transcriptionPercent}%` }}
                  />
                </div>
                {transcriptionTask?.error && (
                  <div className="mt-1 break-words text-xs text-red-600">
                    {transcriptionTask.error}
                  </div>
                )}
              </div>
              {transcriptionRunning ? (
                <button
                  type="button"
                  onClick={cancelTranscription}
                  disabled={cancellingTranscription || transcriptionTask?.status === 'cancelling'}
                  className="flex-shrink-0 rounded-md border border-orange-300 bg-white px-2.5 py-1 text-xs text-orange-700 hover:bg-orange-100 disabled:opacity-50"
                >
                  {cancellingTranscription ? '取消中...' : '取消任务'}
                </button>
              ) : (
                <button
                  type="button"
                  onClick={() => setTranscriptionTask(null)}
                  className="text-xs text-orange-600 hover:text-orange-800"
                  title="关闭任务状态"
                >
                  ✕
                </button>
              )}
            </div>
          )}
          {transcriptionError && (
            <div className="mt-1 flex items-start justify-between gap-3 text-xs text-red-600">
              <span>{transcriptionError}</span>
              <button
                type="button"
                onClick={() => setTranscriptionError('')}
                className="flex-shrink-0 hover:text-red-800"
                title="关闭错误提示"
              >
                ✕
              </button>
            </div>
          )}
        </div>
      )}

      {/* 选择工具栏 (选择模式下显示) */}
      {selectMode && (
        <div className="bg-green-50 border-b border-green-200 px-4 py-2 flex items-center justify-between">
          <div className="flex items-center gap-3 text-sm">
            <span className="text-gray-600">
              已选 <span className="font-bold text-wechat-green">{selectedIds.size}</span> 条
            </span>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={selectAll}
              className="text-xs text-wechat-green hover:underline"
            >
              全选本页
            </button>
            <button
              onClick={deselectAll}
              className="text-xs text-gray-400 hover:text-gray-600"
            >
              取消全选
            </button>
          </div>
        </div>
      )}

      {/* 消息列表 */}
      <div
        ref={containerRef}
        className="flex-1 overflow-y-auto bg-wechat-bg px-4 py-4"
        onWheel={cancelBottomPin}
        onTouchStart={cancelBottomPin}
        onPointerDown={cancelBottomPin}
        onLoadCapture={() => {
          if (bottomPinRef.current.active) schedulePinnedBottomScroll();
        }}
      >
        <div ref={messageContentRef}>
        {messageError && (
          <div role="alert" className="mb-4 flex items-center justify-center gap-3 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
            <span>{messageError}</span>
            <button
              type="button"
              onClick={() => setMessageReloadToken((value) => value + 1)}
              className="flex-shrink-0 font-medium underline underline-offset-2"
            >
              重新加载
            </button>
          </div>
        )}

        {loading && messages.length === 0 && (
          <div className="flex items-center justify-center gap-2 py-20 text-sm text-slate-400" role="status">
            <span className="h-4 w-4 animate-spin rounded-full border-2 border-emerald-500 border-t-transparent" aria-hidden="true" />
            正在加载聊天记录…
          </div>
        )}

        {/* 加载更多 */}
        {hasMore && messages.length > 0 && (
          <div className="text-center mb-4">
            <button
              onClick={handleLoadMore}
              disabled={loading}
              className="text-xs text-wechat-green hover:underline disabled:opacity-50"
            >
              {loading ? '加载中...' : '加载更早的消息'}
            </button>
          </div>
        )}

        {/* 空状态 */}
        {!loading && !messageError && messages.length === 0 && (
          <div className="text-center text-gray-400 py-20">
            <div className="text-5xl mb-3">📝</div>
            <p>{keyword ? '没有匹配的消息' : '暂无消息'}</p>
          </div>
        )}

        {/* 消息气泡 */}
        {messages.map((msg, index) => {
          const prevDate = index > 0 ? messages[index - 1].date_str : null;
          const showDate = msg.date_str !== prevDate;
          const selectionKey = messageSelectionKey(msg);
          const isSelected = selectedIds.has(selectionKey);
          const imageKey = `${msg.id}:${msg.create_time || 0}`;
          const imageLoadFailed = imageLoadErrors.has(imageKey);
          const paymentMessage = isPaymentMessage(msg);

          return (
            <div key={selectionKey || index} id={messageDomId(msg)} className="message-enter">
              {/* 日期分隔 */}
              {showDate && (
                <div className="flex justify-center my-4">
                  <span className="text-xs bg-gray-200/70 text-gray-500 px-3 py-0.5 rounded-full">
                    {msg.date_str}
                  </span>
                </div>
              )}

              {/* 消息行 */}
              {msg.is_sender === null ? (
                /* 系统消息：居中显示 */
                <div
                  className="flex justify-center mb-3"
                  title={(() => {
                    const ts = msg.create_time;
                    if (!ts) return '';
                    const d = new Date(ts * 1000);
                    const pad = (n) => String(n).padStart(2, '0');
                    return `${d.getFullYear()}年${d.getMonth()+1}月${d.getDate()}日 ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
                  })()}
                >
                  {paymentMessage ? (
                    <PaymentMessage message={msg} compact />
                  ) : (
                    <span className="text-xs text-gray-400 bg-gray-100/70 px-3 py-1 rounded-full">
                      {msg.content}
                    </span>
                  )}
                </div>
              ) : (
              <div
                className={`flex mb-3 items-start gap-2 ${
                  msg.is_sender ? 'flex-row-reverse' : 'flex-row'
                } ${selectMode ? 'cursor-pointer' : ''}`}
                onClick={() => selectMode && toggleMessage(selectionKey)}
              >
                {/* 复选框 (选择模式下显示) */}
                {selectMode && (
                  <div className="flex-shrink-0 pt-1.5">
                    <div
                      className={`w-5 h-5 rounded border-2 flex items-center justify-center transition-colors ${
                        isSelected
                          ? 'bg-wechat-green border-wechat-green'
                          : 'border-gray-300 bg-white hover:border-wechat-green'
                      }`}
                      onClick={(e) => {
                        e.stopPropagation();
                        toggleMessage(selectionKey);
                      }}
                    >
                      {isSelected && (
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="3">
                          <path d="M20 6L9 17l-5-5" />
                        </svg>
                      )}
                    </div>
                  </div>
                )}

                {/* 发送者头像：群聊成员不用群头像冒充，缺失时按发送者名称降级 */}
                <Avatar
                  src={
                    msg.sender_avatar_url
                    || (msg.is_sender ? selfAvatarUrl : (!chat.is_group ? chat.avatar_url : ''))
                  }
                  name={
                    msg.is_sender
                      ? (selfDisplayName || '我')
                      : (
                        msg.sender_name
                        || (!chat.is_group ? chat.display_name : '')
                        || msg.sender_username
                        || (chat.is_group ? '未知成员' : chat.display_name)
                      )
                  }
                  className="h-9 w-9"
                />

                {/* 消息气泡 */}
                <div
                  title={(() => { const ts = msg.create_time; if (!ts) return ''; const d = new Date(ts * 1000); return `${d.getFullYear()}年${d.getMonth()+1}月${d.getDate()}日`; })()}
                  className={`max-w-[70%] rounded-lg relative transition-colors ${
                    paymentMessage
                      ? `${isSelected && selectMode ? 'ring-2 ring-wechat-green ring-offset-1' : ''} ${selectionKey === highlightId ? 'ring-2 ring-yellow-400 ring-offset-1' : ''}`
                      : `px-3.5 py-2.5 ${msg.is_sender
                        ? `bg-wechat-bubble-self rounded-tr-sm ${isSelected && selectMode ? 'ring-2 ring-wechat-green ring-offset-1' : ''} ${selectionKey === highlightId ? 'ring-2 ring-yellow-400 bg-yellow-50' : ''}`
                        : `bg-white border border-gray-200 rounded-tl-sm ${isSelected && selectMode ? 'ring-2 ring-wechat-green ring-offset-1' : ''} ${selectionKey === highlightId ? 'ring-2 ring-yellow-400 bg-yellow-50' : ''}`
                      }`
                  }`}
                >
                  {/* 群聊发送者名称 */}
                  {msg.sender_name && !msg.is_sender && (
                    <div className="text-xs font-medium text-wechat-green mb-0.5">
                      {msg.sender_name}
                    </div>
                  )}

                  {/* 消息类型标签 */}
                  {msg.type !== 1 && !paymentMessage && (
                    <span
                      className={`inline-block text-xs px-1.5 py-0.5 rounded mb-1 ${typeBadgeStyle(
                        msg.type,
                        msg.type_name
                      )}`}
                    >
                      {msg.type_name}
                    </span>
                  )}

                  {/* 内容 */}
                  <div className="text-sm leading-relaxed break-words whitespace-pre-wrap text-gray-800">
                    {[2, 3].includes(Number(msg.type)) ? (
                      <ImageMessage
                        message={msg}
                        talker={chat.talker}
                        showImages={showImages}
                        imageQuality={imageQuality}
                        refreshToken={imageRefreshToken}
                        failed={imageLoadFailed}
                        onError={() => {
                          setImageLoadErrors((previous) => {
                            const next = new Set(previous);
                            next.add(imageKey);
                            return next;
                          });
                        }}
                        onRetry={() => {
                          setImageLoadErrors((previous) => {
                            const next = new Set(previous);
                            next.delete(imageKey);
                            return next;
                          });
                        }}
                      />
                    ) : [4, 34].includes(Number(msg.type)) ? (
                      <VoiceMessage
                        message={msg}
                        talker={chat.talker}
                        ready={transcriptionReady}
                        taskRunning={transcriptionRunning || recognitionRunning || hdAutomationActive}
                        starting={startingTranscription}
                        onTranscribe={openSingleVoiceTranscription}
                      />
                    ) : Number(msg.type) === 47 ? (
                      <StickerMessage message={msg} showMedia={showImages} />
                    ) : paymentMessage ? (
                      <PaymentMessage message={msg} />
                    ) : isReplyMessage(msg) ? (
                      <ReplyMessage
                        message={msg}
                        talker={chat.talker}
                        showImages={showImages}
                        imageQuality={imageQuality}
                        imageRefreshToken={imageRefreshToken}
                        renderText={formatContent}
                        onLocate={(target) => locateMessage(target, { requireExact: true })}
                        onUnavailable={() => showNavigationNotice('原消息已不存在或无法精确定位')}
                      />
                    ) : Number(msg.type) === 1 ? (
                      formatContent(msg.content)
                    ) : (
                      msg.content || `[${msg.type_name}]`
                    )}
                  </div>

                  {/* 时间 */}
                  <div className="text-[10px] text-gray-400 text-right mt-1">
                    {msg.time_str?.slice(-8) || ''}
                  </div>
                </div>
              </div>
              )}
            </div>
          );
        })}

        {/* 加载更新的消息 */}
        {hasNewer && messages.length > 0 && (
          <div className="text-center mt-4">
            <button
              onClick={handleLoadNewer}
              disabled={loading}
              className="text-xs text-wechat-green hover:underline disabled:opacity-50"
            >
              {loading ? '加载中...' : '加载更新的消息'}
            </button>
          </div>
        )}

        <div ref={messagesEndRef} />
        </div>
      </div>

      {/* 浮层操作栏 (已选消息且选择模式下) */}
      {selectMode && selectedIds.size > 0 && (
        <div className="z-10 flex flex-wrap items-center justify-between gap-3 border-t border-slate-200 bg-white/95 px-4 py-3 shadow-[0_-8px_24px_rgba(15,23,42,0.06)] backdrop-blur sm:px-5">
          <div className="text-sm text-slate-600">
            已选 <span className="text-base font-semibold text-emerald-600">{selectedIds.size}</span> 条消息
          </div>
          <div className="flex flex-wrap items-center justify-end gap-2">
            <button
              onClick={deselectAll}
              className="px-3 py-2 text-sm font-medium text-slate-400 transition-colors hover:text-slate-700"
            >
              取消
            </button>
            <button
              type="button"
              onClick={() => startRecognition({ useSelected: true })}
              disabled={startingRecognition || recognitionRunning || transcriptionRunning || hdAutomationActive || !visionReady}
              title={visionReady ? '识别选中范围内的图片' : '请先在设置中配置图片识别模型'}
              className="flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-slate-600 transition hover:border-slate-300 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {(startingRecognition || recognitionRunning) ? (
                <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-slate-400 border-t-transparent" />
              ) : (
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                  <circle cx="10.5" cy="10.5" r="6" />
                  <path d="m15 15 5 5M10.5 7.5v6M7.5 10.5h6" />
                </svg>
              )}
              识别选中图片
            </button>
            <button
              type="button"
              onClick={() => {
                setTranscriptionError('');
                setSingleVoiceMessage(null);
                setShowTranscriptionDialog(true);
              }}
              disabled={startingTranscription || transcriptionRunning || recognitionRunning || hdAutomationActive || !transcriptionReady}
              title={transcriptionReady ? '转写选中范围内的语音' : '请先在设置中配置语音转文字'}
              className="flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-slate-600 transition hover:border-slate-300 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {(startingTranscription || transcriptionRunning) ? (
                <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-slate-400 border-t-transparent" />
              ) : (
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                  <rect x="9" y="3" width="6" height="11" rx="3" />
                  <path d="M5 11a7 7 0 0 0 14 0M12 18v3M8 21h8" />
                </svg>
              )}
              转写选中语音
            </button>
            <button
              type="button"
              onClick={() => openAnalysisForScope({
                useSelected: true,
                scopeSummary: `已选 ${selectedIds.size} 条聊天记录`,
              })}
              disabled={analysisRunning || !analysisReady}
              title={analysisReady ? '分析选中的聊天记录并生成报告' : '请先在设置中配置 AI 分析模型'}
              className="flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-slate-600 transition hover:border-slate-300 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {analysisRunning ? (
                <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-slate-400 border-t-transparent" />
              ) : (
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                  <path d="m12 3 1.3 4.2L17.5 8.5l-4.2 1.3L12 14l-1.3-4.2-4.2-1.3 4.2-1.3L12 3Z" />
                  <path d="m18 14 .8 2.2 2.2.8-2.2.8L18 20l-.8-2.2L15 17l2.2-.8L18 14Z" />
                </svg>
              )}
              AI 分析选中
            </button>
            <button
              onClick={handleExportSelected}
              className="flex items-center gap-1.5 rounded-lg bg-emerald-600 px-3.5 py-2 text-sm font-semibold text-white shadow-sm transition hover:bg-emerald-700"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M7 10l5 5 5-5M12 15V3" />
              </svg>
              导出选中
            </button>
          </div>
        </div>
      )}

      {/* 底栏 */}
      <div className="border-t border-slate-200/80 bg-white px-4 py-2 text-center text-xs text-slate-400">
        共 {chat.message_count.toLocaleString()} 条消息
        {keyword && ' (搜索结果)'}
      </div>

      {showRecognitionDialog && (
        <RecognitionRangeDialog
          selectedCount={selectedIds.size}
          defaultStartDate={filterStart}
          defaultEndDate={filterEnd}
          visionReady={visionReady}
          maxImages={settings?.vision?.max_images_per_task}
          submitting={startingRecognition}
          submitError={recognitionError}
          onConfirm={startRecognition}
          onClose={() => !startingRecognition && setShowRecognitionDialog(false)}
        />
      )}
      {showHdAutomationDialog && (
        <HdAutomationRangeDialog
          loadedImageCount={loadedImageCount}
          defaultStartDate={filterStart}
          defaultEndDate={filterEnd}
          defaultPerImageTimeout={settings?.ui?.hd_automation_timeout_seconds ?? 10}
          defaultMinDwellSeconds={settings?.ui?.hd_automation_min_dwell_seconds ?? 0.5}
          submitting={startingHdAutomation}
          onConfirm={startHdAutomation}
          onClose={() => !startingHdAutomation && setShowHdAutomationDialog(false)}
        />
      )}
      {showTranscriptionDialog && (
        <TranscriptionRangeDialog
          selectedCount={selectedIds.size}
          defaultStartDate={filterStart}
          defaultEndDate={filterEnd}
          singleMessage={singleVoiceMessage}
          transcriptionReady={transcriptionReady}
          providerLabel={transcriptionProviderLabel}
          maxVoices={settings?.transcription?.max_voices_per_task}
          submitting={startingTranscription}
          submitError={transcriptionError}
          onConfirm={startTranscription}
          onClose={() => {
            if (startingTranscription) return;
            setShowTranscriptionDialog(false);
            setSingleVoiceMessage(null);
          }}
        />
      )}
      {showAnalysisRangeDialog && (
        <ChatAnalysisRangeDialog
          selectedCount={selectedIds.size}
          totalCount={chat.message_count}
          defaultStartDate={filterStart}
          defaultEndDate={filterEnd}
          analysisReady={analysisReady}
          onConfirm={openAnalysisForScope}
          onClose={() => setShowAnalysisRangeDialog(false)}
        />
      )}
      {showAnalysisDialog && analysisScope && (
        <AiAnalysisDialog
          title={`${chat.display_name} · AI 聊天分析`}
          contentLabel="所选聊天记录"
          scopeSummary={analysisScope.scopeSummary}
          providerLabel={analysisProviderLabel}
          builtInPresets={[]}
          customPresets={settings?.analysis?.presets || []}
          running={analysisRunning}
          runningText="正在分块阅读所选聊天记录并生成总结报告…"
          error={analysisError}
          warning={analysisReport?.warning || ''}
          reportMarkdown={analysisReport?.markdown || ''}
          reportTitle={analysisReport?.title || '聊天记录 AI 分析报告'}
          onConfirm={startAiAnalysis}
          onDownload={downloadAnalysisReport}
          onCancel={cancelAiAnalysis}
          onClose={() => {
            if (analysisRunning) return;
            setShowAnalysisDialog(false);
          }}
        />
      )}
    </div>
  );
}
