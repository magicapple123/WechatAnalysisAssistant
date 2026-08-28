import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import api from '../api';
import { getInterfaceLabel } from '../modelInterfaces';
import Avatar from './Avatar';
import AiAnalysisDialog from './AiAnalysisDialog';

const MAX_CONTACT_SELECTION = 500;
const MAX_POST_SELECTION = 5000;
const PREVIEW_PAGE_SIZE = 20;

function emptyPreviewPagination() {
  return {
    page: 1,
    page_size: PREVIEW_PAGE_SIZE,
    total: 0,
    total_pages: 0,
    has_previous: false,
    has_next: false,
  };
}

const FORMATS = [
  {
    value: 'html',
    label: 'HTML 网页',
    icon: '🌐',
    desc: '朋友圈时间线网页，适合直接浏览',
  },
  {
    value: 'json',
    label: 'JSON 数据',
    icon: '📋',
    desc: '保留结构化动态、媒体与互动字段',
  },
  {
    value: 'csv',
    label: 'CSV 表格',
    icon: '📊',
    desc: '适合使用 Excel / WPS 查看和整理',
  },
  {
    value: 'txt',
    label: 'TXT 文本',
    icon: '📄',
    desc: '通用纯文本格式，便于后续分析',
  },
];

const SCOPE_MODES = [
  { value: 'all', label: '全部动态', desc: '导出所选联系人的全部本地记录' },
  { value: 'date', label: '日期范围', desc: '同一日期范围应用于所有所选联系人' },
  { value: 'selected', label: '勾选动态', desc: '从右侧预览中精确勾选要导出的动态' },
];

function normalizeTimestamp(value) {
  const parsed = Number(value || 0);
  if (!Number.isFinite(parsed) || parsed <= 0) return 0;
  return parsed > 10_000_000_000 ? Math.floor(parsed / 1000) : Math.floor(parsed);
}

function formatDate(value) {
  const timestamp = normalizeTimestamp(value);
  if (!timestamp) return '';
  const date = new Date(timestamp * 1000);
  if (Number.isNaN(date.getTime())) return '';
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

function normalizeContact(contact) {
  const username = String(contact?.username || contact?.user_name || '').trim();
  const displayName = String(
    contact?.display_name || contact?.nickname || contact?.nick_name || username
  ).trim();
  return {
    username,
    displayName: displayName || username,
    avatarUrl: String(contact?.avatar_url || '').trim(),
    isSelf: Boolean(contact?.is_self),
    postCount: Math.max(0, Number(contact?.post_count || contact?.moments_count || 0)),
    startTime: normalizeTimestamp(contact?.start_time || contact?.first_time),
    endTime: normalizeTimestamp(contact?.end_time || contact?.last_time),
  };
}

function resolveDateRange(scopeMode, startDate, endDate) {
  if (scopeMode !== 'date') {
    return { startTime: undefined, endTime: undefined, error: '' };
  }
  if (!startDate && !endDate) {
    return { startTime: undefined, endTime: undefined, error: '' };
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
    return { startTime, endTime, error: '日期格式无效，请重新选择' };
  }
  if (startTime != null && endTime != null && startTime > endTime) {
    return { startTime, endTime, error: '开始日期不能晚于结束日期' };
  }
  return { startTime, endTime, error: '' };
}

function MomentMediaGrid({ post }) {
  const media = Array.isArray(post?.media) ? post.media : [];
  const visibleMedia = media.slice(0, 9);
  // Type 54 can also be a Live Photo/image album in WeChat 4.x. Only the
  // actual link-card families use the compact WeChat-style thumbnail here.
  const isLinkMedia = [3, 5, 30, 42].includes(Number(post?.content_type));
  const [failedIndexes, setFailedIndexes] = useState(() => new Set());
  useEffect(() => {
    setFailedIndexes(new Set());
  }, [post?.media_refresh_token]);
  if (media.length === 0) return null;

  const readyCount = media.filter((item, arrayIndex) => (
    item?.status === 'ready' && !failedIndexes.has(Number(item?.index ?? arrayIndex))
  )).length;
  const qualityLabel = (quality) => {
    if (quality === 'high') return '高清';
    if (quality === 'thumbnail') return '缩略图';
    return '画质未知';
  };

  return (
    <div className="mt-2 inline-block max-w-full rounded-lg border border-slate-200 bg-slate-50 p-2 align-top">
      <div className={`mb-2 flex gap-1 text-[11px] ${
        isLinkMedia
          ? 'w-[72px] flex-col items-start'
          : 'items-center justify-between'
      }`}>
        <span className="font-medium text-slate-700">媒体预览</span>
        <span className="whitespace-nowrap text-slate-500">
          已加载 {readyCount} / {Number(post?.media_count || media.length)}
        </span>
      </div>
      <div className={`grid max-w-full gap-1.5 ${
        isLinkMedia
          ? 'w-[72px] grid-cols-1'
          : visibleMedia.length === 1
            ? 'w-[208px] grid-cols-1'
            : visibleMedia.length === 2
              ? 'w-[278px] grid-cols-2'
              : 'w-[420px] grid-cols-3'
      }`}>
        {visibleMedia.map((item, arrayIndex) => {
          const mediaIndex = Number(item?.index ?? arrayIndex);
          const failed = failedIndexes.has(mediaIndex);
          const ready = item?.status === 'ready' && !failed;
          const unsupported = item?.status === 'unsupported';
          const partial = item?.status === 'partial';
          const url = ready
            ? api.momentsMediaUrl(
              post.tid,
              mediaIndex,
              item?.version || post.media_refresh_token,
            )
            : '';
          return (
            <div
              key={`${post?.tid || 'moment'}-${mediaIndex}`}
              className="relative aspect-square overflow-hidden rounded border border-slate-200 bg-white"
            >
              {ready ? (
                <a
                  href={url}
                  target="_blank"
                  rel="noreferrer"
                  className="block h-full w-full"
                  title={`查看第 ${mediaIndex + 1} 项媒体`}
                >
                  <img
                    src={url}
                    alt={`朋友圈媒体 ${mediaIndex + 1}`}
                    loading="lazy"
                    className="h-full w-full object-cover"
                    onError={() => setFailedIndexes((current) => {
                      const next = new Set(current);
                      next.add(mediaIndex);
                      return next;
                    })}
                  />
                </a>
              ) : (
                <div className="flex h-full flex-col items-center justify-center gap-1 px-1 text-center text-[11px] text-gray-400">
                  <span className="text-lg" aria-hidden="true">{unsupported ? '🎬' : '🖼️'}</span>
                  <span>
                    {failed
                      ? '读取失败'
                      : unsupported
                        ? '暂不支持预览'
                        : partial
                          ? '缓存不完整'
                          : '未加载或未关联'}
                  </span>
                </div>
              )}
              <span className={`absolute bottom-1 left-1 rounded px-1 py-0.5 text-[10px] ${
                ready ? 'bg-black/60 text-white' : 'bg-gray-100/90 text-gray-500'
              }`}>
                {ready ? `已加载 · ${qualityLabel(item?.quality)}` : `第 ${mediaIndex + 1} 项`}
              </span>
            </div>
          );
        })}
      </div>
      {Number(post?.media_count || media.length) > visibleMedia.length && (
        <p className="mt-1.5 text-[11px] text-slate-500">
          另有 {Number(post?.media_count || media.length) - visibleMedia.length} 项媒体未在九宫格中显示
        </p>
      )}
    </div>
  );
}

function MomentCard({ post, contact, selecting, selected, onToggle }) {
  const interactions = Array.isArray(post?.interactions) ? post.interactions : [];
  const [interactionsExpanded, setInteractionsExpanded] = useState(false);
  const likes = interactions.filter((interaction) => Number(interaction?.type) === 1);
  const comments = interactions.filter((interaction) => Number(interaction?.type) === 2);
  const interactionsTotal = Math.max(
    likes.length + comments.length,
    Number(post?.interactions_total || 0),
  );
  const visibleLikes = interactionsExpanded ? likes : likes.slice(0, 10);
  const visibleComments = interactionsExpanded ? comments : comments.slice(0, 3);
  const visibleInteractionCount = visibleLikes.length + visibleComments.length;
  const hiddenInteractions = Math.max(0, interactionsTotal - visibleInteractionCount);
  const collapsedInteractionCount = Math.min(likes.length, 10) + Math.min(comments.length, 3);
  const canToggleInteractions = interactionsTotal > collapsedInteractionCount;
  const unloadedInteractions = Math.max(0, interactionsTotal - interactions.length);
  const hasText = Boolean(post?.content || post?.title || post?.description);
  return (
    <article
      className={`rounded-xl border bg-white p-3 transition-colors ${
        selected ? 'border-wechat-green ring-1 ring-wechat-green/30' : 'border-gray-200'
      }`}
    >
      <div className="flex items-start gap-2">
        {selecting && (
          <input
            type="checkbox"
            checked={selected}
            onChange={onToggle}
            className="mt-1 h-4 w-4 flex-shrink-0 accent-wechat-green"
            aria-label={`选择 ${post?.create_time_str || '时间未知'} 的${post?.content_type_name || '朋友圈动态'}`}
          />
        )}
        <Avatar
          src={post?.avatar_url || contact?.avatarUrl}
          name={contact?.displayName || post?.nickname || post?.username}
          className="h-10 w-10"
        />
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-semibold text-[#576b95]">
            {contact?.displayName || post?.nickname || post?.username || '未知用户'}
          </div>
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
            <span className="font-medium text-gray-600">
              {post?.create_time_str || '时间未知'}
            </span>
            <span className="rounded bg-blue-50 px-1.5 py-0.5 text-blue-600">
              {post?.content_type_name || '未知类型'}
            </span>
            {post?.is_private && (
              <span className="rounded bg-amber-50 px-1.5 py-0.5 text-amber-700">私密</span>
            )}
            {post?.is_top && (
              <span className="rounded bg-amber-50 px-1.5 py-0.5 font-medium text-amber-700">
                📌 置顶
              </span>
            )}
          </div>

          {post?.content && (
            <p className="mt-2 whitespace-pre-wrap break-words text-sm leading-6 text-gray-800">
              {post.content}{post.content_truncated ? '…' : ''}
            </p>
          )}
          {post?.title && (
            <p className="mt-2 break-words rounded-lg bg-gray-50 px-2.5 py-2 text-sm font-medium text-gray-700">
              {post.title}
            </p>
          )}
          {post?.description && (
            <p className="mt-1 break-words text-xs leading-5 text-gray-500">
              {post.description}{post.description_truncated ? '…' : ''}
            </p>
          )}
          {!hasText && Number(post?.media_count || 0) === 0 && !post?.has_finder_feed && (
            <p className="mt-2 text-sm italic text-gray-400">这条动态没有可预览的文字内容</p>
          )}

          <MomentMediaGrid post={post} />

          <div className="mt-2 flex flex-wrap gap-2 text-xs">
            {Number(post?.media_count || 0) > 0 && (
              <span className="rounded-lg bg-slate-100 px-2 py-1 text-slate-600">
                🖼️ 媒体 {Number(post.media_count)} 项
              </span>
            )}
            {post?.has_finder_feed && (
              <span className="rounded-lg bg-slate-100 px-2 py-1 text-slate-600">
                🎬 视频号内容
              </span>
            )}
            {post?.location?.poi_name && (
              <span className="rounded-lg bg-green-50 px-2 py-1 text-green-700">
                📍 {post.location.poi_name}
              </span>
            )}
            {Number(post?.likes_count || 0) > 0 && (
              <span className="rounded-lg bg-red-50 px-2 py-1 text-red-600">
                ❤️ {Number(post.likes_count)}
              </span>
            )}
            {Number(post?.comments_count || 0) > 0 && (
              <span className="rounded-lg bg-blue-50 px-2 py-1 text-blue-600">
                💬 {Number(post.comments_count)}
              </span>
            )}
          </div>

          {interactions.length > 0 && (
            <div className="mt-2 overflow-hidden rounded-lg border border-gray-200 bg-white text-xs leading-5">
              {visibleLikes.length > 0 && (
                <section className="bg-rose-50/70 px-2.5 py-2" aria-label="点赞名单">
                  <div className="mb-0.5 flex items-center gap-1 font-semibold text-rose-600">
                    <span aria-hidden="true">❤️</span>
                    <span>点赞 {Math.max(likes.length, Number(post?.likes_count || 0))}</span>
                  </div>
                  <div className="flex flex-wrap items-baseline gap-x-1 break-words text-rose-700">
                    {visibleLikes.map((interaction, index) => (
                      <span
                        key={`like-${interaction.create_time || 0}-${index}`}
                        className="inline-flex items-baseline font-medium"
                      >
                        <span>{interaction.from_name || '未知用户'}</span>
                        {index < visibleLikes.length - 1 && <span aria-hidden="true">、</span>}
                      </span>
                    ))}
                  </div>
                </section>
              )}
              {visibleComments.length > 0 && (
                <section
                  className={`px-2.5 py-2 ${visibleLikes.length > 0 ? 'border-t border-gray-200' : ''}`}
                  aria-label="评论列表"
                >
                  <div className="mb-0.5 flex items-center gap-1 font-semibold text-gray-600">
                    <span aria-hidden="true">💬</span>
                    <span>评论 {Math.max(comments.length, Number(post?.comments_count || 0))}</span>
                  </div>
                  <div className="space-y-1">
                    {visibleComments.map((interaction, index) => (
                      <div
                        key={`comment-${interaction.create_time || 0}-${index}`}
                        className="break-words text-gray-700"
                      >
                        <span className="font-medium text-blue-700">
                          {interaction.from_name || '未知用户'}
                        </span>
                        {interaction.to_name && (
                          <>
                            <span className="text-gray-500"> 回复 </span>
                            <span className="font-medium text-blue-700">{interaction.to_name}</span>
                          </>
                        )}
                        <span className="text-gray-700">：{interaction.content || ''}</span>
                      </div>
                    ))}
                  </div>
                </section>
              )}
              {(canToggleInteractions || (interactionsExpanded && unloadedInteractions > 0)) && (
                <div className="border-t border-gray-200 bg-gray-50 px-2.5 py-1.5">
                  {canToggleInteractions && (
                    <button
                      type="button"
                      aria-expanded={interactionsExpanded}
                      onClick={() => setInteractionsExpanded((current) => !current)}
                      className="rounded text-left font-medium text-blue-600 hover:text-blue-700 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-400"
                    >
                      {interactionsExpanded
                        ? '收起互动'
                        : `还有 ${hiddenInteractions} 条互动，点击展开`}
                    </button>
                  )}
                  {interactionsExpanded && unloadedInteractions > 0 && (
                    <div className="mt-0.5 text-amber-600">
                      另有 {unloadedInteractions} 条互动尚未载入，请刷新预览后重试
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </article>
  );
}

export default function MomentsExportDialog({
  onConfirm,
  onAnalyze,
  analysisSettings,
  onClose,
}) {
  const dialogRef = useRef(null);
  const previousFocusRef = useRef(null);
  const exportingRef = useRef(false);
  const nestedDialogOpenRef = useRef(false);
  const onCloseRef = useRef(onClose);
  const [step, setStep] = useState('browse');
  const [contacts, setContacts] = useState([]);
  const [selectedUsernames, setSelectedUsernames] = useState(() => new Set());
  const [activeUsername, setActiveUsername] = useState('');
  const [search, setSearch] = useState('');
  const [momentsKeywordInput, setMomentsKeywordInput] = useState('');
  const [momentsKeyword, setMomentsKeyword] = useState('');
  const [scopeMode, setScopeMode] = useState('all');
  const [startDate, setStartDate] = useState('');
  const [endDate, setEndDate] = useState('');
  const [selectedPostRefs, setSelectedPostRefs] = useState(() => new Map());
  const [showAnalysisDialog, setShowAnalysisDialog] = useState(false);
  const [analysisRunning, setAnalysisRunning] = useState(false);
  const [analysisError, setAnalysisError] = useState('');
  const [analysisReport, setAnalysisReport] = useState(null);
  const [analysisScopeSnapshot, setAnalysisScopeSnapshot] = useState(null);

  const [pinnedPosts, setPinnedPosts] = useState([]);
  const [unavailablePinnedTotal, setUnavailablePinnedTotal] = useState(0);
  const [collapsedPinnedUsernames, setCollapsedPinnedUsernames] = useState(
    () => new Set(),
  );
  const [previewPosts, setPreviewPosts] = useState([]);
  const [previewPage, setPreviewPage] = useState(1);
  const [previewPagination, setPreviewPagination] = useState(emptyPreviewPagination);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState('');
  const [previewReloadToken, setPreviewReloadToken] = useState(0);
  const [mediaLoading, setMediaLoading] = useState(false);
  const [mediaNotice, setMediaNotice] = useState('');
  const [mediaError, setMediaError] = useState('');
  const previewRequestId = useRef(0);
  const previewContextRef = useRef('');
  const analysisAbortRef = useRef(null);
  const contactsAbortRef = useRef(null);

  const [selectedFormat, setSelectedFormat] = useState('html');
  const [filename, setFilename] = useState('');
  const [downloadMedia, setDownloadMedia] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [validationError, setValidationError] = useState('');
  const [exporting, setExporting] = useState(false);

  useEffect(() => {
    exportingRef.current = exporting || analysisRunning || showAnalysisDialog;
    nestedDialogOpenRef.current = showAnalysisDialog;
  }, [analysisRunning, exporting, showAnalysisDialog]);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => () => {
    analysisAbortRef.current?.abort();
    analysisAbortRef.current = null;
    contactsAbortRef.current?.abort();
    contactsAbortRef.current = null;
  }, []);

  useEffect(() => {
    previousFocusRef.current = document.activeElement;
    const focusTimer = window.setTimeout(() => dialogRef.current?.focus(), 0);
    const handleKeyDown = (event) => {
      if (nestedDialogOpenRef.current) return;
      if (event.key === 'Escape' && !exportingRef.current) {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== 'Tab') return;
      const dialog = dialogRef.current;
      if (!dialog) return;
      const focusable = [...dialog.querySelectorAll(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [href], [tabindex]:not([tabindex="-1"])'
      )].filter((element) => !element.hidden && element.getClientRects().length > 0);
      if (focusable.length === 0) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const current = document.activeElement;
      if (event.shiftKey && (current === first || current === dialog || !dialog.contains(current))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (current === last || current === dialog || !dialog.contains(current))) {
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

  const loadContacts = useCallback(async () => {
    contactsAbortRef.current?.abort();
    const abortController = new AbortController();
    contactsAbortRef.current = abortController;
    setLoading(true);
    setLoadError('');
    try {
      const response = await api.getMomentsContacts({ signal: abortController.signal });
      if (abortController.signal.aborted) return;
      const source = response?.data?.contacts;
      if (!Array.isArray(source)) {
        throw new Error('朋友圈联系人接口返回格式不正确');
      }
      const normalized = source
        .map(normalizeContact)
        .filter((contact) => contact.username)
        .sort((left, right) => {
          if (left.isSelf !== right.isSelf) return left.isSelf ? -1 : 1;
          return left.displayName.localeCompare(right.displayName, 'zh-CN');
        });
      const available = new Set(normalized.map((contact) => contact.username));
      setContacts(normalized);
      setSelectedUsernames((current) => (
        new Set([...current].filter((username) => available.has(username)))
      ));
      setSelectedPostRefs((current) => (
        new Map([...current].filter(([, ref]) => available.has(ref.username)))
      ));
      setActiveUsername((current) => (
        available.has(current) ? current : (normalized[0]?.username || '')
      ));
    } catch (error) {
      if (abortController.signal.aborted) return;
      setContacts([]);
      setActiveUsername('');
      setLoadError(error.message || '加载朋友圈联系人失败');
    } finally {
      if (contactsAbortRef.current === abortController) {
        contactsAbortRef.current = null;
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    loadContacts();
  }, [loadContacts]);

  useEffect(() => {
    if (selectedFormat !== 'html') setDownloadMedia(false);
  }, [selectedFormat]);

  const dateRange = useMemo(
    () => resolveDateRange(scopeMode, startDate, endDate),
    [scopeMode, startDate, endDate]
  );

  const invalidatePreview = useCallback(() => {
    previewRequestId.current += 1;
    previewContextRef.current = '';
    setPinnedPosts([]);
    setPreviewPosts([]);
    setPreviewPagination(emptyPreviewPagination());
    setPreviewError('');
    setPreviewLoading(true);
  }, []);

  const openPreview = useCallback((username) => {
    invalidatePreview();
    setMediaNotice('');
    setMediaError('');
    setActiveUsername(username);
    setPreviewPage(1);
    setPreviewReloadToken((value) => value + 1);
  }, [invalidatePreview]);

  const changeScopeMode = (mode) => {
    invalidatePreview();
    setMediaNotice('');
    setMediaError('');
    setScopeMode(mode);
    setPreviewPage(1);
    setPreviewReloadToken((value) => value + 1);
    setValidationError('');
  };

  const changeDate = (setter, value) => {
    invalidatePreview();
    setMediaNotice('');
    setMediaError('');
    setter(value);
    setPreviewPage(1);
    setPreviewReloadToken((token) => token + 1);
    setValidationError('');
  };

  const applyMomentsSearch = (event) => {
    event.preventDefault();
    if (!activeUsername) return;
    const nextKeyword = momentsKeywordInput.trim();
    invalidatePreview();
    setMomentsKeyword(nextKeyword);
    setPreviewPage(1);
    setPreviewReloadToken((token) => token + 1);
  };

  const clearMomentsSearch = () => {
    const hadAppliedKeyword = Boolean(momentsKeyword);
    setMomentsKeywordInput('');
    if (!hadAppliedKeyword) return;
    invalidatePreview();
    setMomentsKeyword('');
    setPreviewPage(1);
    setPreviewReloadToken((token) => token + 1);
  };

  const reloadPreview = () => {
    invalidatePreview();
    setPreviewReloadToken((value) => value + 1);
  };

  const changePreviewPage = (updater) => {
    invalidatePreview();
    setPreviewPage(updater);
    setPreviewReloadToken((value) => value + 1);
  };

  useEffect(() => {
    const requestId = previewRequestId.current + 1;
    previewRequestId.current = requestId;
    const contextKey = JSON.stringify({
      username: activeUsername,
      page: previewPage,
      scopeMode,
      startTime: scopeMode === 'date' ? dateRange.startTime : null,
      endTime: scopeMode === 'date' ? dateRange.endTime : null,
      keyword: momentsKeyword,
      reload: previewReloadToken,
    });
    previewContextRef.current = contextKey;
    setPinnedPosts([]);
    setUnavailablePinnedTotal(0);
    setPreviewPosts([]);
    setPreviewPagination(emptyPreviewPagination());
    if (!activeUsername || dateRange.error) {
      setPreviewError(dateRange.error);
      setPreviewLoading(false);
      return undefined;
    }

    let active = true;
    const abortController = new AbortController();
    setPreviewLoading(true);
    setPreviewError('');
    api.previewMoments({
      username: activeUsername,
      page: previewPage,
      pageSize: PREVIEW_PAGE_SIZE,
      startTime: scopeMode === 'date' ? dateRange.startTime : undefined,
      endTime: scopeMode === 'date' ? dateRange.endTime : undefined,
      keyword: momentsKeyword || undefined,
      signal: abortController.signal,
    }).then((response) => {
      if (
        !active
        || previewRequestId.current !== requestId
        || previewContextRef.current !== contextKey
      ) return;
      const data = response?.data;
      if (
        !data
        || !Array.isArray(data.posts)
        || !Array.isArray(data.pinned_posts || [])
        || !data.pagination
      ) {
        throw new Error('朋友圈预览接口返回格式不正确');
      }
      setPinnedPosts(data.pinned_posts || []);
      setUnavailablePinnedTotal(Number(data.unavailable_pinned_total || 0));
      setPreviewPosts(data.posts);
      setPreviewPagination(data.pagination);
      const totalPages = Number(data.pagination.total_pages || 0);
      if (totalPages > 0 && previewPage > totalPages) {
        setPreviewPage(totalPages);
      }
    }).catch((error) => {
      if (
        !active
        || previewRequestId.current !== requestId
        || previewContextRef.current !== contextKey
      ) return;
      setPinnedPosts([]);
      setUnavailablePinnedTotal(0);
      setPreviewPosts([]);
      setPreviewPagination(emptyPreviewPagination());
      setPreviewError(error.message || '加载朋友圈预览失败');
    }).finally(() => {
      if (
        active
        && previewRequestId.current === requestId
        && previewContextRef.current === contextKey
      ) {
        setPreviewLoading(false);
      }
    });
    return () => {
      active = false;
      abortController.abort();
    };
  }, [
    activeUsername,
    previewPage,
    previewReloadToken,
    scopeMode,
    dateRange.startTime,
    dateRange.endTime,
    dateRange.error,
    momentsKeyword,
  ]);

  const filteredContacts = useMemo(() => {
    const keyword = search.trim().toLocaleLowerCase('zh-CN');
    if (!keyword) return contacts;
    return contacts.filter((contact) => (
      contact.displayName.toLocaleLowerCase('zh-CN').includes(keyword)
      || contact.username.toLocaleLowerCase().includes(keyword)
    ));
  }, [contacts, search]);

  const selectedContacts = useMemo(() => (
    contacts.filter((contact) => selectedUsernames.has(contact.username))
  ), [contacts, selectedUsernames]);

  const activeContact = useMemo(
    () => contacts.find((contact) => contact.username === activeUsername) || null,
    [contacts, activeUsername]
  );

  const selectableContacts = useMemo(
    () => contacts.slice(0, MAX_CONTACT_SELECTION),
    [contacts]
  );
  const allSelected = selectableContacts.length > 0
    && selectedUsernames.size === selectableContacts.length
    && selectableContacts.every((contact) => selectedUsernames.has(contact.username));

  const exactScopeUsernames = useMemo(() => {
    const usernames = new Set();
    selectedPostRefs.forEach((ref) => usernames.add(ref.username));
    return usernames;
  }, [selectedPostRefs]);

  const selectedPostCountByUsername = useMemo(() => {
    const counts = new Map();
    selectedPostRefs.forEach((ref) => {
      counts.set(ref.username, (counts.get(ref.username) || 0) + 1);
    });
    return counts;
  }, [selectedPostRefs]);

  const exportContacts = useMemo(() => (
    scopeMode === 'selected'
      ? contacts.filter((contact) => exactScopeUsernames.has(contact.username))
      : selectedContacts
  ), [contacts, exactScopeUsernames, scopeMode, selectedContacts]);

  const toggleAllContacts = () => {
    setValidationError(
      !allSelected && contacts.length > MAX_CONTACT_SELECTION
        ? `一次最多导出 ${MAX_CONTACT_SELECTION} 位联系人，已选择列表中的前 ${MAX_CONTACT_SELECTION} 位`
        : ''
    );
    if (allSelected) {
      setSelectedUsernames(new Set());
    } else {
      setSelectedUsernames(
        new Set(selectableContacts.map((contact) => contact.username))
      );
    }
  };

  const toggleContact = (username) => {
    const currentlySelected = selectedUsernames.has(username);
    if (!currentlySelected && selectedUsernames.size >= MAX_CONTACT_SELECTION) {
      setValidationError(`一次最多导出 ${MAX_CONTACT_SELECTION} 位联系人`);
      return;
    }
    setValidationError('');
    setSelectedUsernames((current) => {
      const next = new Set(current);
      if (next.has(username)) next.delete(username);
      else next.add(username);
      return next;
    });
    if (!currentlySelected) openPreview(username);
  };

  const togglePost = (post) => {
    const tid = String(post?.tid || '');
    if (!tid || !activeUsername) return;
    const alreadySelected = selectedPostRefs.has(tid);
    if (!alreadySelected && selectedPostRefs.size >= MAX_POST_SELECTION) {
      setValidationError(`一次最多精确选择 ${MAX_POST_SELECTION} 条朋友圈动态`);
      return;
    }
    if (
      !alreadySelected
      && !exactScopeUsernames.has(activeUsername)
      && exactScopeUsernames.size >= MAX_CONTACT_SELECTION
    ) {
      setValidationError(`一次最多导出 ${MAX_CONTACT_SELECTION} 位联系人`);
      return;
    }
    setValidationError('');
    setSelectedPostRefs((current) => {
      const next = new Map(current);
      if (next.has(tid)) next.delete(tid);
      else {
        next.set(tid, {
          tid,
          username: activeUsername,
          createTime: normalizeTimestamp(post?.create_time),
        });
      }
      return next;
    });
  };

  const currentPageSelectablePosts = [...pinnedPosts, ...previewPosts]
    .filter((post) => post?.tid);
  const currentPageAllSelected = currentPageSelectablePosts.length > 0
    && currentPageSelectablePosts.every((post) => selectedPostRefs.has(String(post.tid)));

  const pinnedCollapsed = Boolean(
    activeUsername && collapsedPinnedUsernames.has(activeUsername),
  );

  const togglePinnedCollapsed = () => {
    if (!activeUsername) return;
    setCollapsedPinnedUsernames((current) => {
      const next = new Set(current);
      if (next.has(activeUsername)) next.delete(activeUsername);
      else next.add(activeUsername);
      return next;
    });
  };

  const loadAllContactMedia = async () => {
    if (!activeUsername || mediaLoading) return;
    const confirmed = window.confirm(
      `将检查“${activeContact?.displayName || activeUsername}”全部朋友圈的本地媒体缓存，`
      + '并分批尝试访问所有动态中记录的微信图片地址。\n\n'
      + '动态较多时可能需要等待一段时间，历史地址也可能已经过期。是否继续？',
    );
    if (!confirmed) return;
    setMediaLoading(true);
    setMediaNotice('');
    setMediaError('');
    try {
      const response = await api.loadMomentsMedia({
        username: activeUsername,
        allPosts: true,
      });
      const result = response?.data || {};
      const loaded = Number(result.loaded || 0);
      const readyAfter = Number(result.ready_after || 0);
      const remaining = Number(result.remaining || 0);
      const truncated = Number(result.truncated || 0);
      const expired = Number(result.expired || 0);
      const failed = Number(result.failed || 0);
      setMediaNotice(
        `该联系人全部媒体检查完成：新加载 ${loaded} 项，已加载 ${readyAfter} 项，仍未加载 ${remaining} 项`
        + (expired > 0 ? `；地址已过期 ${expired} 项` : '')
        + (failed > 0 ? `；加载失败 ${failed} 项` : '')
        + (truncated > 0 ? `；另有 ${truncated} 项超过本次安全上限` : ''),
      );
      reloadPreview();
    } catch (error) {
      setMediaError(error.message || '加载朋友圈媒体失败');
    } finally {
      setMediaLoading(false);
    }
  };

  const toggleCurrentPagePosts = () => {
    if (!activeUsername || currentPageSelectablePosts.length === 0) return;
    if (currentPageAllSelected) {
      setSelectedPostRefs((current) => {
        const next = new Map(current);
        currentPageSelectablePosts.forEach((post) => next.delete(String(post.tid)));
        return next;
      });
      setValidationError('');
      return;
    }
    if (
      !exactScopeUsernames.has(activeUsername)
      && exactScopeUsernames.size >= MAX_CONTACT_SELECTION
    ) {
      setValidationError(`一次最多导出 ${MAX_CONTACT_SELECTION} 位联系人`);
      return;
    }

    const missingPosts = currentPageSelectablePosts.filter(
      (post) => !selectedPostRefs.has(String(post.tid))
    );
    const capacity = Math.max(0, MAX_POST_SELECTION - selectedPostRefs.size);
    const postsToAdd = missingPosts.slice(0, capacity);
    setSelectedPostRefs((current) => {
      const next = new Map(current);
      postsToAdd.forEach((post) => {
        const tid = String(post.tid);
        next.set(tid, {
          tid,
          username: activeUsername,
          createTime: normalizeTimestamp(post.create_time),
        });
      });
      return next;
    });
    setValidationError(
      postsToAdd.length < missingPosts.length
        ? `已达到 ${MAX_POST_SELECTION} 条上限，本页仅新增 ${postsToAdd.length} 条`
        : ''
    );
  };

  const clearSelectedPostsForContact = (contact) => {
    const count = selectedPostCountByUsername.get(contact.username) || 0;
    if (count <= 0) return;
    if (!window.confirm(`确定取消已为“${contact.displayName}”勾选的 ${count} 条动态吗？`)) {
      return;
    }
    setSelectedPostRefs((current) => (
      new Map([...current].filter(([, ref]) => ref.username !== contact.username))
    ));
    setValidationError('');
  };

  const clearAllSelectedPosts = () => {
    if (selectedPostRefs.size === 0) return;
    if (!window.confirm(`确定取消已勾选的全部 ${selectedPostRefs.size} 条动态吗？`)) {
      return;
    }
    setSelectedPostRefs(new Map());
    setValidationError('');
  };

  const validateScope = () => {
    if (scopeMode === 'selected') {
      if (selectedPostRefs.size === 0) {
        return '请在右侧预览中至少勾选一条朋友圈动态';
      }
      if (exactScopeUsernames.size > MAX_CONTACT_SELECTION) {
        return `一次最多导出 ${MAX_CONTACT_SELECTION} 位联系人`;
      }
      return '';
    }
    if (selectedUsernames.size === 0) {
      return '请至少选择一位联系人';
    }
    if (scopeMode === 'date') {
      if (!startDate && !endDate) return '请填写开始日期或结束日期';
      if (dateRange.error) return dateRange.error;
    }
    return '';
  };

  const handleNext = () => {
    const error = validateScope();
    if (error) {
      setValidationError(error);
      return;
    }
    setValidationError('');
    setStep('options');
  };

  const defaultFilename = useMemo(() => {
    if (exportContacts.length === 1) {
      return `${exportContacts[0].displayName}_朋友圈`;
    }
    if (exportContacts.length > 1) {
      return `${exportContacts.length}位联系人_朋友圈`;
    }
    return '朋友圈导出';
  }, [exportContacts]);

  const handleExport = async () => {
    const error = validateScope();
    if (error) {
      setValidationError(error);
      setStep('browse');
      return;
    }
    const usernames = scopeMode === 'selected'
      ? exportContacts.map((contact) => contact.username)
      : selectedContacts.map((contact) => contact.username);
    setValidationError('');
    setExporting(true);
    try {
      await onConfirm({
        usernames,
        format: selectedFormat,
        startTime: scopeMode === 'date' ? dateRange.startTime : undefined,
        endTime: scopeMode === 'date' ? dateRange.endTime : undefined,
        postIds: scopeMode === 'selected' ? [...selectedPostRefs.keys()] : undefined,
        filename: filename.trim() || undefined,
        downloadMedia: selectedFormat === 'html' && downloadMedia,
      });
    } catch (_error) {
      // App 统一展示导出错误；保留当前选择，便于调整后重试。
    } finally {
      setExporting(false);
    }
  };

  const analysisReady = Boolean(
    analysisSettings?.base_url
    && analysisSettings?.model
    && (
      analysisSettings?.requires_api_key === false
      || analysisSettings?.has_api_key
    )
  );
  const analysisProviderLabel = getInterfaceLabel('analysis', analysisSettings);

  const openMomentsAnalysis = () => {
    const error = validateScope();
    if (error) {
      setValidationError(error);
      setStep('browse');
      return;
    }
    if (!analysisReady || typeof onAnalyze !== 'function') {
      setValidationError('请先在设置中完成 AI 分析模型配置');
      return;
    }
    setValidationError('');
    setAnalysisError('');
    setAnalysisReport(null);
    const frozenUsernames = scopeMode === 'selected'
      ? exportContacts.map((contact) => contact.username)
      : selectedContacts.map((contact) => contact.username);
    const frozenPostIds = scopeMode === 'selected'
      ? [...selectedPostRefs.keys()]
      : undefined;
    setAnalysisScopeSnapshot({
      usernames: frozenUsernames,
      startTime: scopeMode === 'date' ? dateRange.startTime : undefined,
      endTime: scopeMode === 'date' ? dateRange.endTime : undefined,
      postIds: frozenPostIds,
      scopeSummary: scopeMode === 'selected'
        ? `${frozenPostIds.length} 条动态，涉及 ${frozenUsernames.length} 位联系人`
        : `${frozenUsernames.length} 位联系人${scopeMode === 'date' ? '，使用同一日期范围' : '，全部本地动态'}`,
      reportTitle: exportContacts.length === 1
        ? `${exportContacts[0].displayName}_朋友圈AI分析`
        : `${exportContacts.length}位联系人_朋友圈AI分析`,
    });
    setShowAnalysisDialog(true);
  };

  const handleAiAnalysis = async (options) => {
    if (!analysisReady || typeof onAnalyze !== 'function') {
      setAnalysisError('AI 分析配置不完整，请先在设置中填写接口地址、模型和 API Key');
      return;
    }
    const snapshot = analysisScopeSnapshot;
    if (!snapshot || !Array.isArray(snapshot.usernames) || snapshot.usernames.length === 0) {
      setAnalysisError('朋友圈分析范围已经变化，请关闭窗口后重新选择');
      return;
    }
    analysisAbortRef.current?.abort();
    const abortController = new AbortController();
    analysisAbortRef.current = abortController;
    setAnalysisRunning(true);
    setAnalysisError('');
    try {
      const response = await onAnalyze({
        usernames: [...snapshot.usernames],
        startTime: snapshot.startTime,
        endTime: snapshot.endTime,
        postIds: snapshot.postIds ? [...snapshot.postIds] : undefined,
        presetId: options.presetId,
        strength: options.strength,
        detail: options.detail,
        requirements: options.requirements,
        reportTitle: snapshot.reportTitle,
        cloudUploadConfirmed: options.thirdPartyConfirmed,
        signal: abortController.signal,
      });
      const result = response?.data?.report_markdown ? response.data : response;
      const markdown = result?.report_markdown || result?.markdown || '';
      if (!markdown) throw new Error('服务端未返回分析报告');
      setAnalysisReport({
        markdown,
        title: result?.report_title || '朋友圈 AI 分析报告',
        filename: result?.filename || '朋友圈AI分析报告.md',
        path: result?.path || '',
        warning: result?.warning || '',
        metadata: result?.metadata || {},
      });
    } catch (error) {
      setAnalysisError(
        abortController.signal.aborted
          ? '朋友圈 AI 分析已取消'
          : (error.message || '朋友圈 AI 分析失败，请重试'),
      );
    } finally {
      if (analysisAbortRef.current === abortController) {
        analysisAbortRef.current = null;
      }
      setAnalysisRunning(false);
    }
  };

  const cancelAiAnalysis = () => {
    if (!analysisAbortRef.current) return;
    setAnalysisError('正在停止朋友圈 AI 分析…');
    analysisAbortRef.current.abort();
  };

  const downloadAnalysisReport = ({ markdown }) => {
    const content = markdown || analysisReport?.markdown;
    if (!content) return;
    const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = analysisReport?.filename || '朋友圈AI分析报告.md';
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  };

  const previewTotalPages = Number(previewPagination.total_pages || 0);
  const scopeSummary = scopeMode === 'selected'
    ? `${selectedPostRefs.size} 条动态，涉及 ${exportContacts.length} 位联系人`
    : `${selectedUsernames.size} 位联系人${scopeMode === 'date' ? '，使用同一日期范围' : '，全部本地动态'}`;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/45 p-0 backdrop-blur-[2px] sm:p-4">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="moments-export-title"
        aria-hidden={showAnalysisDialog ? 'true' : undefined}
        inert={showAnalysisDialog ? '' : undefined}
        tabIndex={-1}
        className={`flex h-full max-h-none w-full flex-col overflow-hidden rounded-none border-0 border-slate-200 bg-white shadow-2xl outline-none sm:h-auto sm:max-h-[94vh] sm:max-w-6xl sm:rounded-2xl sm:border ${
          showAnalysisDialog ? 'pointer-events-none select-none' : ''
        }`}
      >
        <div className="flex flex-shrink-0 items-center justify-between gap-4 border-b border-slate-200 bg-white px-5 py-4 sm:px-6">
          <div>
            <h2 id="moments-export-title" className="text-lg font-semibold text-slate-900">朋友圈导出与 AI 分析</h2>
            <p className="mt-1 text-sm text-slate-500">{scopeSummary}</p>
          </div>
          <div className="flex items-center gap-2 text-xs">
            <span
              aria-current={step === 'browse' ? 'step' : undefined}
              className={`rounded-full border px-3 py-1.5 font-medium transition-colors ${step === 'browse' ? 'border-wechat-green/30 bg-green-50 text-wechat-green' : 'border-slate-200 bg-slate-50 text-slate-500'}`}
            >
              1 查看与选择
            </span>
            <span
              aria-current={step === 'options' ? 'step' : undefined}
              className={`rounded-full border px-3 py-1.5 font-medium transition-colors ${step === 'options' ? 'border-wechat-green/30 bg-green-50 text-wechat-green' : 'border-slate-200 bg-slate-50 text-slate-500'}`}
            >
              2 导出设置
            </span>
          </div>
        </div>

        {step === 'browse' ? (
          <div className="min-h-0 flex-1 overflow-y-auto bg-slate-50/70 p-4 sm:p-5">
            <div className="mb-4 rounded-lg border border-blue-200 bg-blue-50 px-3 py-2.5 text-xs leading-5 text-blue-700">
              右侧会直接显示已精确关联的本地朋友圈图片，并标记未加载、缓存不完整和已加载状态。如需补全，请手动点击“加载全部媒体”；该操作才会访问该联系人动态中记录的微信图片地址。
            </div>

            <div className="grid min-h-0 grid-cols-1 gap-4 lg:grid-cols-[340px_minmax(0,1fr)]">
              <section className="min-w-0 rounded-xl border border-slate-200 bg-white p-3">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <div>
                    <h3 className="text-sm font-semibold text-gray-800">
                      {scopeMode === 'selected' ? '浏览联系人' : '选择联系人'}
                    </h3>
                    <p className="text-xs text-gray-400">
                      {scopeMode === 'selected'
                        ? `共 ${contacts.length} 位，已勾选 ${selectedPostRefs.size} 条动态`
                        : `共 ${contacts.length} 位，已选 ${selectedUsernames.size} 位`}
                    </p>
                  </div>
                  {scopeMode === 'selected' ? (
                    <button
                      type="button"
                      onClick={clearAllSelectedPosts}
                      disabled={selectedPostRefs.size === 0}
                      className="rounded-lg border border-indigo-200 bg-indigo-50/60 px-2.5 py-1.5 text-xs font-medium text-indigo-700 hover:bg-indigo-100 disabled:opacity-40"
                    >
                      清空全部勾选
                    </button>
                  ) : (
                    <button
                      type="button"
                      onClick={toggleAllContacts}
                      disabled={loading || contacts.length === 0 || exporting}
                      className="rounded-lg border border-wechat-green px-2.5 py-1.5 text-xs font-medium text-wechat-green hover:bg-green-50 disabled:opacity-40"
                    >
                      {allSelected
                        ? '取消全选'
                        : contacts.length > MAX_CONTACT_SELECTION
                          ? `选择前 ${MAX_CONTACT_SELECTION} 位`
                          : '全选'}
                    </button>
                  )}
                </div>

                <div className="relative mb-2">
                  <span className="absolute left-3 top-2 text-gray-400">⌕</span>
                  <input
                    type="text"
                    value={search}
                    onChange={(event) => setSearch(event.target.value)}
                    placeholder="搜索联系人名称或微信 ID"
                    aria-label="搜索朋友圈联系人"
                    disabled={loading || exporting}
                    className="w-full rounded-lg border border-gray-200 py-2 pl-8 pr-3 text-sm focus:border-wechat-green focus:outline-none disabled:bg-gray-100"
                  />
                </div>

                <div className="max-h-[52vh] min-h-64 overflow-y-auto rounded-lg border border-gray-200 bg-white">
                  {loading && (
                    <div className="flex items-center justify-center gap-2 px-4 py-12 text-sm text-gray-500">
                      <div className="h-4 w-4 animate-spin rounded-full border-2 border-wechat-green border-t-transparent" />
                      正在读取联系人...
                    </div>
                  )}
                  {!loading && loadError && (
                    <div className="px-4 py-10 text-center">
                      <p role="alert" className="text-sm text-red-500">{loadError}</p>
                      <button
                        type="button"
                        onClick={loadContacts}
                        className="mt-3 rounded-lg bg-wechat-green px-3 py-1.5 text-xs font-medium text-white"
                      >
                        重新加载
                      </button>
                    </div>
                  )}
                  {!loading && !loadError && contacts.length === 0 && (
                    <div className="px-4 py-12 text-center text-sm text-gray-500">
                      本机朋友圈数据库中没有可预览的动态
                    </div>
                  )}
                  {!loading && !loadError && contacts.length > 0 && filteredContacts.length === 0 && (
                    <div className="px-4 py-10 text-center text-sm text-gray-500">没有匹配的联系人</div>
                  )}
                  {!loading && !loadError && filteredContacts.map((contact) => {
                    const firstDate = formatDate(contact.startTime);
                    const lastDate = formatDate(contact.endTime);
                    const dateText = firstDate && lastDate
                      ? (firstDate === lastDate ? firstDate : `${firstDate} 至 ${lastDate}`)
                      : (firstDate || lastDate || '时间未知');
                    const active = contact.username === activeUsername;
                    const exactCount = selectedPostCountByUsername.get(contact.username) || 0;
                    return (
                      <div
                        key={contact.username}
                        className={`flex items-center gap-2 border-b border-gray-100 px-2 py-2 last:border-b-0 ${
                          active
                            ? 'bg-green-50'
                            : scopeMode === 'selected'
                              ? (exactCount > 0 ? 'bg-slate-100' : 'bg-white')
                              : selectedUsernames.has(contact.username)
                                ? 'bg-green-50/60'
                                : 'bg-white'
                        }`}
                      >
                        {scopeMode !== 'selected' && (
                          <input
                            type="checkbox"
                            checked={selectedUsernames.has(contact.username)}
                            onChange={() => toggleContact(contact.username)}
                            disabled={exporting}
                            className="h-4 w-4 flex-shrink-0 accent-wechat-green"
                            aria-label={`选择 ${contact.displayName}`}
                          />
                        )}
                        <Avatar
                          src={contact.avatarUrl}
                          name={contact.displayName}
                          className="h-9 w-9"
                        />
                        <button
                          type="button"
                          onClick={() => openPreview(contact.username)}
                          aria-pressed={active}
                          className="min-w-0 flex-1 rounded px-1 py-0.5 text-left hover:bg-white/70"
                          title="查看该联系人的朋友圈"
                        >
                          <span className="flex items-center gap-1.5">
                            <span className="truncate text-sm font-medium text-gray-800">{contact.displayName}</span>
                            {contact.isSelf && (
                              <span className="rounded bg-green-100 px-1.5 py-0.5 text-[10px] font-semibold text-wechat-green">我</span>
                            )}
                          </span>
                          <span className="block truncate text-[11px] text-gray-400">{dateText}</span>
                        </button>
                        <div className="flex-shrink-0 text-right">
                          <div className="text-xs font-medium text-gray-600">{contact.postCount} 条</div>
                          {scopeMode === 'selected' && exactCount > 0 ? (
                            <button
                              type="button"
                              onClick={() => clearSelectedPostsForContact(contact)}
                              className="mt-0.5 text-[11px] font-medium text-indigo-700 hover:text-indigo-900"
                              title={`取消为该联系人勾选的 ${exactCount} 条动态`}
                            >
                              清除已选 {exactCount} 条
                            </button>
                          ) : (
                            <button
                              type="button"
                              onClick={() => openPreview(contact.username)}
                              aria-current={active ? 'true' : undefined}
                              className={`mt-0.5 text-[11px] ${active ? 'font-medium text-blue-700' : 'text-blue-500 hover:text-blue-700'}`}
                            >
                              {active ? '正在查看' : '查看'}
                            </button>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </section>

              <section className="min-w-0 rounded-xl border border-slate-200 bg-white p-3">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="flex min-w-0 items-center gap-2">
                    {activeContact && (
                      <Avatar
                        src={activeContact.avatarUrl}
                        name={activeContact.displayName}
                        className="h-10 w-10"
                      />
                    )}
                    <div className="min-w-0">
                      <h3 className="truncate text-sm font-semibold text-gray-800">
                        {activeContact ? `${activeContact.displayName} 的朋友圈` : '朋友圈预览'}
                      </h3>
                      <p className="mt-0.5 text-xs text-gray-400">
                        {scopeMode === 'date'
                          ? `当前日期范围：可预览置顶 ${pinnedPosts.length} 条，时间线 ${Number(previewPagination.total || 0)} 条`
                          : `本地可预览置顶 ${pinnedPosts.length} 条，时间线 ${Number(previewPagination.total || 0)} 条`}
                        {unavailablePinnedTotal > 0
                          ? `；另有 ${unavailablePinnedTotal} 条置顶仅保留引用`
                          : ''}
                      </p>
                    </div>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <button
                      type="button"
                      onClick={loadAllContactMedia}
                      disabled={
                        !activeUsername
                        || previewLoading
                        || mediaLoading
                      }
                      className="rounded-lg border border-indigo-200 bg-indigo-50/60 px-2.5 py-1.5 text-xs font-medium text-indigo-700 hover:bg-indigo-100 disabled:opacity-40"
                    >
                      {mediaLoading ? '正在加载全部媒体...' : '加载全部媒体'}
                    </button>
                    <button
                      type="button"
                      onClick={reloadPreview}
                      disabled={!activeUsername || previewLoading || mediaLoading}
                      className="rounded-lg border border-gray-200 bg-white px-2.5 py-1.5 text-xs text-gray-600 hover:bg-gray-100 disabled:opacity-40"
                    >
                      刷新预览
                    </button>
                  </div>
                </div>

                <form
                  role="search"
                  onSubmit={applyMomentsSearch}
                  className="mt-3 rounded-lg border border-gray-200 bg-white p-2.5"
                >
                  <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
                    <label htmlFor="moments-content-search" className="flex-shrink-0 text-xs font-medium text-gray-600">
                      搜索朋友圈文案
                    </label>
                    <div className="flex min-w-0 flex-1 gap-2">
                      <input
                        id="moments-content-search"
                        type="search"
                        value={momentsKeywordInput}
                        maxLength={200}
                        onChange={(event) => setMomentsKeywordInput(event.target.value)}
                        placeholder="输入文案关键词后按回车或点击搜索"
                        className="min-w-0 flex-1 rounded-lg border border-gray-200 px-2.5 py-1.5 text-sm text-gray-700 placeholder:text-gray-400 focus:border-wechat-green focus:outline-none focus:ring-1 focus:ring-wechat-green/20"
                      />
                      <button
                        type="submit"
                        disabled={!activeUsername || previewLoading}
                        className="flex-shrink-0 rounded-lg bg-wechat-green px-3 py-1.5 text-xs font-medium text-white hover:brightness-95 disabled:opacity-40"
                      >
                        搜索
                      </button>
                      {(momentsKeywordInput || momentsKeyword) && (
                        <button
                          type="button"
                          onClick={clearMomentsSearch}
                          disabled={!activeUsername || previewLoading}
                          className="flex-shrink-0 rounded-lg border border-gray-200 bg-white px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-40"
                        >
                          清除
                        </button>
                      )}
                    </div>
                  </div>
                  {momentsKeyword && (
                    <div className="mt-2 text-xs leading-5" aria-live="polite">
                      <p className="text-blue-600">
                        当前仅显示文案中包含“{momentsKeyword}”的朋友圈
                      </p>
                      <p className="text-gray-400">
                        搜索只过滤预览；如需仅导出搜索结果，请选择“勾选动态”并勾选当前结果。
                      </p>
                    </div>
                  )}
                </form>

                {mediaNotice && (
                  <div className="mt-3 rounded-lg border border-green-200 bg-green-50 px-3 py-2 text-xs text-green-700">
                    {mediaNotice}
                  </div>
                )}
                {mediaError && (
                  <div role="alert" className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-600">
                    {mediaError}
                  </div>
                )}

                <div aria-label="朋友圈导出范围" className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-3">
                  {SCOPE_MODES.map((mode) => (
                    <button
                      type="button"
                      aria-pressed={scopeMode === mode.value}
                      key={mode.value}
                      onClick={() => changeScopeMode(mode.value)}
                      className={`rounded-lg border px-3 py-2 text-left transition-colors ${
                        scopeMode === mode.value
                          ? 'border-wechat-green bg-green-50 text-wechat-green'
                          : 'border-gray-200 bg-white text-gray-600 hover:border-gray-300'
                      }`}
                    >
                      <span className="block text-sm font-medium">{mode.label}</span>
                      <span className="mt-0.5 block text-[11px] leading-4 opacity-75">{mode.desc}</span>
                    </button>
                  ))}
                </div>

                {scopeMode === 'date' && (
                  <div className="mt-3 grid grid-cols-1 gap-2 rounded-lg border border-gray-200 bg-white p-2.5 sm:grid-cols-2">
                    <label className="text-xs text-gray-500">
                      开始日期（可留空）
                      <input
                        type="date"
                        value={startDate}
                        onChange={(event) => changeDate(setStartDate, event.target.value)}
                        className="mt-1 w-full rounded-lg border border-gray-200 px-2.5 py-2 text-sm text-gray-700 focus:border-wechat-green focus:outline-none"
                      />
                    </label>
                    <label className="text-xs text-gray-500">
                      结束日期（可留空）
                      <input
                        type="date"
                        value={endDate}
                        onChange={(event) => changeDate(setEndDate, event.target.value)}
                        className="mt-1 w-full rounded-lg border border-gray-200 px-2.5 py-2 text-sm text-gray-700 focus:border-wechat-green focus:outline-none"
                      />
                    </label>
                  </div>
                )}

                {scopeMode === 'selected' && (
                  <div className="mt-3 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-600">
                    <span>已精确选择 {selectedPostRefs.size} 条；翻页或切换联系人后选择仍会保留。</span>
                    <button
                      type="button"
                      onClick={toggleCurrentPagePosts}
                      disabled={previewLoading || currentPageSelectablePosts.length === 0}
                      className="rounded border border-slate-300 bg-white px-2 py-1 font-medium text-slate-700 hover:bg-slate-100 disabled:opacity-40"
                    >
                      {currentPageAllSelected ? '取消本页' : '选择本页'}
                    </button>
                  </div>
                )}

                <div className="mt-3 max-h-[38vh] min-h-64 space-y-2 overflow-y-auto pr-1">
                  {!activeContact && !loading && (
                    <div className="flex min-h-64 items-center justify-center text-sm text-gray-400">请从左侧选择要查看的联系人</div>
                  )}
                  {activeContact && previewLoading && (
                    <div className="flex min-h-64 items-center justify-center gap-2 text-sm text-gray-500">
                      <div className="h-5 w-5 animate-spin rounded-full border-2 border-wechat-green border-t-transparent" />
                      正在读取本地朋友圈...
                    </div>
                  )}
                  {activeContact && !previewLoading && previewError && (
                    <div className="flex min-h-64 flex-col items-center justify-center px-4 text-center">
                      <p role="alert" className="text-sm text-red-500">{previewError}</p>
                      <button
                        type="button"
                        onClick={reloadPreview}
                        className="mt-3 rounded-lg bg-wechat-green px-3 py-1.5 text-xs font-medium text-white"
                      >
                        重试
                      </button>
                    </div>
                  )}
                  {activeContact && !previewLoading && !previewError
                    && pinnedPosts.length === 0 && unavailablePinnedTotal === 0
                    && previewPosts.length === 0 && (
                    <div className="flex min-h-64 items-center justify-center text-sm text-gray-400">
                      {momentsKeyword
                        ? `没有找到文案中包含“${momentsKeyword}”的朋友圈`
                        : '当前范围内没有本地朋友圈动态'}
                    </div>
                  )}
                  {activeContact && !previewLoading && !previewError
                    && (pinnedPosts.length > 0 || unavailablePinnedTotal > 0) && (
                    <section
                      aria-labelledby="pinned-moments-heading"
                      className="space-y-2 rounded-xl border border-amber-200 bg-amber-50/60 p-2.5"
                    >
                      <div className="flex items-center justify-between gap-2 px-1">
                        <h4 id="pinned-moments-heading" className="text-xs font-semibold text-amber-800">
                          📌 置顶朋友圈
                        </h4>
                        <button
                          type="button"
                          onClick={togglePinnedCollapsed}
                          aria-expanded={!pinnedCollapsed}
                          aria-controls="pinned-moments-content"
                          className="inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-[11px] font-medium text-amber-700 hover:bg-amber-100"
                        >
                          <span>{pinnedPosts.length} 条可预览</span>
                          <span aria-hidden="true">{pinnedCollapsed ? '展开⌄' : '收起⌃'}</span>
                        </button>
                      </div>
                      {!pinnedCollapsed && (
                        <div id="pinned-moments-content" className="space-y-2">
                          {unavailablePinnedTotal > 0 && (
                            <div className="rounded-lg border border-amber-200 bg-white/70 px-3 py-2 text-[11px] leading-5 text-amber-700">
                              另有 {unavailablePinnedTotal} 条置顶仅保留了微信本地引用，完整正文尚未写入本地数据库，暂时无法安全预览或导出。
                            </div>
                          )}
                          {pinnedPosts.map((post) => (
                            <MomentCard
                              key={`pinned-${post.tid}`}
                              post={post}
                              contact={activeContact}
                              selecting={scopeMode === 'selected'}
                              selected={selectedPostRefs.has(String(post.tid))}
                              onToggle={() => togglePost(post)}
                            />
                          ))}
                        </div>
                      )}
                    </section>
                  )}
                  {activeContact && !previewLoading && !previewError && previewPosts.length > 0 && (
                    <div className="flex items-center gap-2 px-1 pt-1 text-xs font-semibold text-gray-500">
                      <span>时间线</span>
                      <span className="h-px flex-1 bg-gray-200" />
                    </div>
                  )}
                  {activeContact && !previewLoading && !previewError && previewPosts.map((post) => (
                    <MomentCard
                      key={post.tid}
                      post={post}
                      contact={activeContact}
                      selecting={scopeMode === 'selected'}
                      selected={selectedPostRefs.has(String(post.tid))}
                      onToggle={() => togglePost(post)}
                    />
                  ))}
                </div>

                <div className="mt-3 flex items-center justify-between gap-3 border-t border-gray-200 pt-3 text-xs text-gray-500">
                  <span>
                    {previewTotalPages > 0
                      ? `时间线第 ${Number(previewPagination.page || previewPage)} / ${previewTotalPages} 页`
                      : '时间线暂无结果'}
                  </span>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => changePreviewPage((page) => Math.max(1, page - 1))}
                      disabled={previewLoading || !previewPagination.has_previous}
                      className="rounded-lg border border-gray-200 bg-white px-3 py-1.5 hover:bg-gray-100 disabled:opacity-40"
                    >
                      上一页
                    </button>
                    <button
                      type="button"
                      onClick={() => changePreviewPage((page) => page + 1)}
                      disabled={previewLoading || !previewPagination.has_next}
                      className="rounded-lg border border-gray-200 bg-white px-3 py-1.5 hover:bg-gray-100 disabled:opacity-40"
                    >
                      下一页
                    </button>
                  </div>
                </div>
              </section>
            </div>

            {validationError && (
              <div role="alert" className="mt-4 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-600">
                {validationError}
              </div>
            )}
          </div>
        ) : (
          <div className="min-h-0 flex-1 overflow-y-auto bg-slate-50/70 p-4 sm:p-6">
            <div className="mx-auto max-w-3xl">
              <div className="mb-5 rounded-xl border border-green-200 bg-green-50 px-4 py-3">
                <h3 className="text-sm font-semibold text-green-800">已确定导出范围</h3>
                <p className="mt-1 text-sm text-green-700">{scopeSummary}</p>
                {scopeMode === 'date' && (
                  <p className="mt-1 text-xs text-green-600">
                    {startDate || '最早记录'} 至 {endDate || '最新记录'}
                  </p>
                )}
              </div>

              <section>
                <h3 className="mb-2 text-sm font-semibold text-gray-800">导出格式</h3>
                <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                  {FORMATS.map((format) => (
                    <label
                      key={format.value}
                      className={`flex cursor-pointer items-start gap-2 rounded-lg border-2 p-3 transition-colors ${
                        selectedFormat === format.value
                          ? 'border-wechat-green bg-green-50'
                          : 'border-gray-200 hover:border-gray-300'
                      }`}
                    >
                      <input
                        type="radio"
                        name="moments-export-format"
                        value={format.value}
                        checked={selectedFormat === format.value}
                        onChange={() => setSelectedFormat(format.value)}
                        disabled={exporting}
                        className="mt-1 accent-wechat-green"
                      />
                      <span className="text-lg leading-5">{format.icon}</span>
                      <span>
                        <span className="block text-sm font-medium text-gray-800">{format.label}</span>
                        <span className="block text-xs text-gray-500">{format.desc}</span>
                      </span>
                    </label>
                  ))}
                </div>
              </section>

              <section className="mt-4 space-y-2">
                <div className={`rounded-lg border px-3 py-2.5 ${
                  selectedFormat === 'html'
                    ? 'border-green-200 bg-green-50 text-green-800'
                    : 'border-gray-200 bg-gray-50 text-gray-500'
                }`}>
                  <span className="block text-sm font-medium">已加载媒体自动嵌入</span>
                  <span className="mt-0.5 block text-xs leading-5">
                    {selectedFormat === 'html'
                      ? '预览中已经加载的本地图片会直接嵌入 HTML，无需再次勾选或重新下载。'
                      : '仅 HTML 支持嵌入图片；其他格式仍保留媒体数量或结构化元数据。'}
                  </span>
                </div>
                <label className={`flex items-start gap-2 rounded-lg border px-3 py-3 select-none ${
                  selectedFormat === 'html'
                    ? 'cursor-pointer border-amber-200 bg-amber-50'
                    : 'cursor-not-allowed border-gray-200 bg-gray-50'
                }`}>
                  <input
                    type="checkbox"
                    checked={downloadMedia}
                    onChange={(event) => setDownloadMedia(event.target.checked)}
                    disabled={exporting || selectedFormat !== 'html'}
                    className="mt-0.5 accent-wechat-green"
                  />
                  <span>
                    <span className="block text-sm font-medium text-gray-800">联网补充尚未加载的朋友圈图片</span>
                    <span className={`mt-0.5 block text-xs leading-5 ${
                      selectedFormat === 'html' ? 'text-amber-700' : 'text-gray-500'
                    }`}>
                      {selectedFormat === 'html'
                        ? '可选且默认关闭。开启后只为缺失媒体访问动态中记录的微信地址；历史链接可能已经过期。'
                        : '该选项仅用于 HTML 导出。'}
                    </span>
                  </span>
                </label>
              </section>

              <section className="mt-4">
                <label className="mb-1 block text-xs font-medium text-gray-500">自定义文件名</label>
                <input
                  type="text"
                  value={filename}
                  onChange={(event) => setFilename(event.target.value)}
                  placeholder={defaultFilename}
                  disabled={exporting}
                  className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm focus:border-wechat-green focus:outline-none disabled:bg-gray-100"
                />
                <p className="mt-1 text-xs text-gray-400">不填写则由程序根据联系人自动命名</p>
              </section>

              {validationError && (
                <div role="alert" className="mt-4 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-600">
                  {validationError}
                </div>
              )}
            </div>
          </div>
        )}

        <div className="flex flex-shrink-0 items-center justify-between gap-3 border-t border-slate-200 bg-white px-4 py-3 sm:px-6 sm:py-4">
          <span className="hidden text-xs text-gray-500 sm:block">
            {step === 'browse' ? '预览本身不联网；仅“加载全部媒体”会访问该联系人朋友圈中的微信图片地址' : scopeSummary}
          </span>
          <div className="ml-auto flex items-center gap-2 sm:gap-3">
            {step === 'options' && (
              <button
                type="button"
                onClick={() => setStep('browse')}
                disabled={exporting || analysisRunning || showAnalysisDialog}
                className="rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm text-gray-600 hover:bg-gray-100 disabled:opacity-50 sm:px-4"
              >
                返回修改范围
              </button>
            )}
            <button
              type="button"
              onClick={onClose}
              disabled={exporting || analysisRunning || showAnalysisDialog}
              className="px-3 py-2 text-sm text-gray-600 hover:text-gray-800 disabled:opacity-50 sm:px-4"
            >
              取消
            </button>
            {step === 'browse' ? (
              <button
                type="button"
                onClick={handleNext}
                disabled={loading || Boolean(loadError) || contacts.length === 0}
                className="rounded-lg bg-wechat-green px-4 py-2 text-sm font-medium text-white hover:bg-wechat-green-dark disabled:cursor-not-allowed disabled:opacity-50 sm:px-6"
              >
                下一步：导出设置
              </button>
            ) : (
              <>
                <button
                  type="button"
                  onClick={openMomentsAnalysis}
                  disabled={exporting || analysisRunning || !analysisReady}
                  title={analysisReady ? '分析当前朋友圈范围并生成总结报告' : '请先在设置中配置 AI 分析模型'}
                  className="flex items-center gap-2 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50 sm:px-5"
                >
                  <span aria-hidden="true">✨</span>
                  AI 分析总结
                </button>
                <button
                  type="button"
                  onClick={handleExport}
                  disabled={exporting || analysisRunning}
                  className="flex items-center gap-2 rounded-lg bg-wechat-green px-5 py-2 text-sm font-medium text-white hover:bg-wechat-green-dark disabled:cursor-not-allowed disabled:opacity-50 sm:px-7"
                >
                  {exporting && (
                    <div className="h-4 w-4 animate-spin rounded-full border-2 border-white border-t-transparent" />
                  )}
                  {exporting ? '导出中...' : '开始导出'}
                </button>
              </>
            )}
          </div>
        </div>
      </div>
      {showAnalysisDialog && (
        <AiAnalysisDialog
          title="朋友圈 · AI 分析总结"
          contentLabel="所选朋友圈正文、位置以及点赞评论文字"
          scopeSummary={analysisScopeSnapshot?.scopeSummary || scopeSummary}
          providerLabel={analysisProviderLabel}
          builtInPresets={[]}
          customPresets={analysisSettings?.presets || []}
          running={analysisRunning}
          runningText="正在分块阅读所选朋友圈内容并生成总结报告…"
          error={analysisError}
          warning={analysisReport?.warning || ''}
          reportMarkdown={analysisReport?.markdown || ''}
          reportTitle={analysisReport?.title || '朋友圈 AI 分析报告'}
          onConfirm={handleAiAnalysis}
          onDownload={downloadAnalysisReport}
          onCancel={cancelAiAnalysis}
          onClose={() => {
            if (analysisRunning) return;
            setShowAnalysisDialog(false);
            setAnalysisScopeSnapshot(null);
          }}
        />
      )}
    </div>
  );
}
