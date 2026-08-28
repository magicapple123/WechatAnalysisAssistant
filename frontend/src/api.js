/**
 * API 客户端 - 与后端通信
 */
import axios from 'axios';
import { isDesktopApp, selectDesktopFolder } from './desktop.js';

const API_BASE = '/api';

const client = axios.create({
  baseURL: API_BASE,
  timeout: 30000,
});

function extractErrorDetail(detail) {
  if (typeof detail === 'string') return detail.trim();
  if (Array.isArray(detail)) {
    return detail
      .map((item) => extractErrorDetail(item))
      .filter(Boolean)
      .join('；');
  }
  if (!detail || typeof detail !== 'object') return '';

  const nested = detail.detail ?? detail.message ?? detail.msg ?? detail.error;
  const message = nested === detail ? '' : extractErrorDetail(nested);
  const location = Array.isArray(detail.loc)
    ? detail.loc.filter((part) => part !== 'body').join('.')
    : '';
  if (message && location) return `${location}：${message}`;
  return message;
}

/**
 * Convert Axios errors into a small, stable error contract for the UI.
 *
 * FastAPI validation failures use a structured `detail` array, while network,
 * timeout and cancellation errors do not have a response body. Keeping the
 * status/code on the normalized error lets callers distinguish those cases
 * without depending on Axios internals.
 */
export function normalizeApiError(error) {
  const code = error?.code || '';
  const status = error?.response?.status ?? error?.status;
  const responseData = error?.response?.data;
  const responseDetail = extractErrorDetail(
    typeof responseData === 'string'
      ? responseData
      : responseData?.detail ?? responseData?.message ?? responseData?.error,
  );

  let message = responseDetail;
  if (!message && (code === 'ERR_CANCELED' || error?.name === 'CanceledError')) {
    message = '请求已取消';
  } else if (!message && ['ECONNABORTED', 'ETIMEDOUT'].includes(code)) {
    message = '请求超时，请稍后重试';
  } else if (!message && (code === 'ERR_NETWORK' || (!error?.response && error?.message === 'Network Error'))) {
    message = '无法连接本机服务，请确认程序仍在运行';
  } else if (!message && status) {
    message = `请求失败（HTTP ${status}）`;
  } else if (!message) {
    message = String(error?.message || '请求失败');
  }

  const normalized = new Error(message);
  normalized.name = code === 'ERR_CANCELED' || error?.name === 'CanceledError'
    ? 'CanceledError'
    : 'ApiError';
  if (code) normalized.code = code;
  if (status != null) normalized.status = status;
  normalized.cause = error;
  return normalized;
}

// 响应拦截器：解包数据并对所有请求使用同一错误格式。
client.interceptors.response.use(
  (response) => response.data,
  (error) => Promise.reject(normalizeApiError(error)),
);

const api = {
  /** 获取系统状态 */
  getStatus(options = {}) {
    return client.get('/status', options);
  },

  /** 自动检测微信数据和密钥 */
  autoDetect(options = {}) {
    return client.get('/auto-detect', options);
  },

  /** 设置解密密钥 */
  setKey(key) {
    return client.post('/set-key', { key });
  },

  /** 获取聊天列表 */
  getChats(options = {}) {
    return client.get('/chats', options);
  },

  /** 获取当前微信账号的联系人目录（包括没有聊天记录的联系人） */
  getContacts(options = {}) {
    return client.get('/contacts', options);
  },

  /** 获取聊天消息 */
  getMessages(talker, {
    page = 1,
    pageSize = 50,
    msgType,
    keyword,
    senderName,
    startTime,
    endTime,
    signal,
  } = {}) {
    const params = { page, page_size: pageSize };
    if (msgType) params.msg_type = msgType;
    if (keyword) params.keyword = keyword;
    if (senderName) params.sender_name = senderName;
    if (startTime != null) params.start_time = startTime;
    if (endTime != null) params.end_time = endTime;
    return client.get(`/chat/${encodeURIComponent(talker)}`, { params, signal });
  },

  /** 搜索消息 */
  searchMessages(keyword, limit = 100) {
    return client.get('/search', { params: { keyword, limit } });
  },

  /** 获取消息所在的页码 */
  getMessagePosition(talker, msgId, createTime, messageKey) {
    const params = {};
    if (createTime != null) params.create_time = createTime;
    if (messageKey) params.message_key = messageKey;
    return client.get(`/message-position/${encodeURIComponent(talker)}/${msgId}`, { params });
  },

  /** 获取统计信息 */
  getStatistics() {
    return client.get('/statistics');
  },

  /** 导出单个聊天 — 文件直接保存到服务端配置的目录，返回路径 */
  async exportChat(talker, displayName, format = 'html', options = {}) {
    const {
      messageIds,
      messageRefs,
      startTime,
      endTime,
      filename,
      replaceImagesWithDescriptions,
      replaceVoicesWithTranscriptions,
      embedImages,
      htmlImageQuality,
    } = options;
    const body = { talker, display_name: displayName, format };
    if (messageIds && messageIds.length > 0) body.message_ids = messageIds;
    if (messageRefs && messageRefs.length > 0) body.message_refs = messageRefs;
    if (startTime != null) body.start_time = startTime;
    if (endTime != null) body.end_time = endTime;
    if (filename) body.filename = filename;
    if (replaceImagesWithDescriptions != null) {
      body.replace_images_with_descriptions = Boolean(replaceImagesWithDescriptions);
    }
    if (replaceVoicesWithTranscriptions != null) {
      body.replace_voices_with_transcriptions = Boolean(replaceVoicesWithTranscriptions);
    }
    if (embedImages != null) body.embed_images = Boolean(embedImages);
    if (htmlImageQuality) body.html_image_quality = htmlImageQuality;

    const response = await client.post('/export', body, { timeout: 600000 });
    return response;  // { success, path, filename }
  },

  /** 导出所有聊天 — 文件直接保存到服务端配置的目录，返回路径 */
  async exportAll(format = 'html', options = {}) {
    return client.post('/export-all', null, {
      params: {
        fmt: format,
        replace_images_with_descriptions: Boolean(options.replaceImagesWithDescriptions),
        replace_voices_with_transcriptions: Boolean(options.replaceVoicesWithTranscriptions),
        embed_images: options.embedImages !== false,
        html_image_quality: options.htmlImageQuality || 'best',
      },
      timeout: 600000,
    });
  },

  /** 获取本机朋友圈数据库中实际存在动态的联系人 */
  getMomentsContacts(options = {}) {
    return client.get('/moments/contacts', {
      ...options,
      headers: { ...options.headers, 'X-Wechat-Assistant': '1' },
    });
  },

  /** 分页预览单个联系人的本地朋友圈；预览本身不会下载远程媒体。 */
  previewMoments({
    username,
    page = 1,
    pageSize = 20,
    startTime,
    endTime,
    keyword,
    signal,
  } = {}) {
    const body = {
      username,
      page,
      page_size: pageSize,
    };
    if (startTime != null) body.start_time = startTime;
    if (endTime != null) body.end_time = endTime;
    if (keyword) body.keyword = keyword;
    return client.post('/moments/preview', body, {
      headers: { 'X-Wechat-Assistant': '1' },
      signal,
    });
  },

  /** 构造已精确绑定的本地朋友圈媒体地址；端点不会联网。 */
  momentsMediaUrl(tid, mediaIndex, refreshToken = null) {
    const path = `${API_BASE}/moments/media/${encodeURIComponent(tid)}/${encodeURIComponent(mediaIndex)}`;
    if (refreshToken == null) return path;
    return `${path}?_refresh=${encodeURIComponent(refreshToken)}`;
  },

  /** 用户明确触发：扫描本地缓存并尝试加载指定动态或该联系人的全部微信媒体。 */
  loadMomentsMedia({ username, postIds, allPosts = false } = {}) {
    return client.post('/moments/media/load', {
      username,
      tids: Array.isArray(postIds) ? postIds : [],
      all_posts: Boolean(allPosts),
    }, {
      headers: { 'X-Wechat-Assistant': '1' },
      // 全量媒体会由后端分批处理，历史动态较多时允许请求持续更久。
      timeout: allPosts ? 1800000 : 180000,
    });
  },

  /** 导出所选联系人的朋友圈 */
  exportMoments({
    usernames,
    format = 'html',
    startTime,
    endTime,
    postIds,
    filename,
    downloadMedia = false,
  } = {}) {
    const body = {
      usernames: Array.isArray(usernames) ? usernames : [],
      format,
      download_media: format === 'html' && Boolean(downloadMedia),
    };
    if (startTime != null) body.start_time = startTime;
    if (endTime != null) body.end_time = endTime;
    if (Array.isArray(postIds)) body.tids = postIds;
    if (filename) body.filename = filename;
    return client.post('/moments/export', body, {
      headers: { 'X-Wechat-Assistant': '1' },
      timeout: 600000,
    });
  },

  /** 用户明确确认后，将所选聊天文本发送给已配置的分析模型。 */
  analyzeChat(talker, {
    displayName,
    messageRefs,
    startTime,
    endTime,
    allMessages = false,
    presetId,
    strength,
    detail,
    requirements,
    reportTitle,
    cloudUploadConfirmed = false,
    signal,
  } = {}) {
    const body = {
      display_name: displayName || talker,
      all_messages: Boolean(allMessages),
      cloud_upload_confirmed: Boolean(cloudUploadConfirmed),
    };
    if (Array.isArray(messageRefs)) body.message_refs = messageRefs;
    if (startTime != null) body.start_time = startTime;
    if (endTime != null) body.end_time = endTime;
    if (presetId) body.preset_id = presetId;
    if (strength) body.strength = strength;
    if (detail) body.detail = detail;
    if (requirements != null) body.requirements = requirements;
    if (reportTitle) body.report_title = reportTitle;
    return client.post(`/analysis/chat/${encodeURIComponent(talker)}`, body, {
      headers: { 'X-Wechat-Assistant': '1' },
      timeout: 900000,
      signal,
    });
  },

  /** 用户明确确认后，分析所选联系人/日期/逐条动态范围。 */
  analyzeMoments({
    usernames,
    startTime,
    endTime,
    postIds,
    presetId,
    strength,
    detail,
    requirements,
    reportTitle,
    cloudUploadConfirmed = false,
    signal,
  } = {}) {
    const body = {
      usernames: Array.isArray(usernames) ? usernames : [],
      cloud_upload_confirmed: Boolean(cloudUploadConfirmed),
    };
    if (startTime != null) body.start_time = startTime;
    if (endTime != null) body.end_time = endTime;
    if (Array.isArray(postIds)) body.tids = postIds;
    if (presetId) body.preset_id = presetId;
    if (strength) body.strength = strength;
    if (detail) body.detail = detail;
    if (requirements != null) body.requirements = requirements;
    if (reportTitle) body.report_title = reportTitle;
    return client.post('/analysis/moments', body, {
      headers: { 'X-Wechat-Assistant': '1' },
      timeout: 900000,
      signal,
    });
  },

  /** 构造聊天图片地址（由浏览器直接懒加载） */
  imageUrl(talker, messageId, createTime, messageKey, quality = 'thumbnail', refreshToken = null) {
    const path = `${API_BASE}/chat/${encodeURIComponent(talker)}/image/${encodeURIComponent(messageId)}`;
    if (createTime == null) return path;
    const params = new URLSearchParams({
      create_time: String(createTime),
      quality: quality === 'best' ? 'best' : 'thumbnail',
    });
    if (messageKey) params.set('message_key', messageKey);
    if (refreshToken != null) params.set('_refresh', String(refreshToken));
    return `${path}?${params.toString()}`;
  },

  /** 手动创建图片识别任务 */
  recognizeImages(talker, { messageRefs, messageIds, startTime, endTime, allImages = false, force = false } = {}) {
    const body = { force: Boolean(force), all_images: Boolean(allImages) };
    if (messageRefs && messageRefs.length > 0) body.message_refs = messageRefs;
    if (messageIds && messageIds.length > 0) body.message_ids = messageIds;
    if (startTime != null) body.start_time = startTime;
    if (endTime != null) body.end_time = endTime;
    return client.post(`/chat/${encodeURIComponent(talker)}/images/recognize`, body, {
      timeout: 120000,
    });
  },

  /** 查询图片识别任务进度 */
  getImageRecognitionTask(taskId) {
    return client.get(`/image-recognition/tasks/${encodeURIComponent(taskId)}`, {
      timeout: 120000,
    });
  },

  /**
   * 创建本机微信 UI 自动化任务。任务创建后会等待用户在微信中打开首张图片，
   * 只有按下全局启动热键后才会开始控制微信图片查看器。
   */
  startHdImageAutomation(talker, {
    messageRefs,
    startTime,
    endTime,
    allImages = false,
    direction = 'next',
    perImageTimeout = 10,
    minDwellSeconds = 0.5,
  } = {}) {
    const normalizedMinDwell = Number(minDwellSeconds);
    const body = {
      all_images: Boolean(allImages),
      direction: direction === 'previous' ? 'previous' : 'next',
      per_image_timeout: Number(perImageTimeout) || 10,
      min_dwell_seconds: Number.isFinite(normalizedMinDwell) ? normalizedMinDwell : 0.5,
    };
    if (messageRefs && messageRefs.length > 0) body.message_refs = messageRefs;
    if (startTime != null) body.start_time = startTime;
    if (endTime != null) body.end_time = endTime;
    return client.post(`/chat/${encodeURIComponent(talker)}/images/hd-automation`, body, {
      headers: { 'X-Wechat-Assistant': '1' },
      timeout: 600000,
    });
  },

  /** 查询微信图片 UI 自动化任务进度。 */
  getHdImageAutomationTask(taskId) {
    return client.get(`/image-hd-automation/tasks/${encodeURIComponent(taskId)}`, {
      timeout: 30000,
    });
  },

  /** 页面刷新后恢复仍在后台等待或运行的自动化任务。 */
  getActiveHdImageAutomationTask() {
    return client.get('/image-hd-automation/active', { timeout: 30000 });
  },

  /** 暂停微信图片 UI 自动化任务；恢复请在微信窗口使用全局启动热键。 */
  pauseHdImageAutomationTask(taskId) {
    return client.post(`/image-hd-automation/tasks/${encodeURIComponent(taskId)}/pause`, null, {
      headers: { 'X-Wechat-Assistant': '1' },
      timeout: 30000,
    });
  },

  /** 在任务暂停时切换后续翻页方式。 */
  setHdImageAutomationNavigationMode(taskId, mode) {
    return client.post(
      `/image-hd-automation/tasks/${encodeURIComponent(taskId)}/navigation-mode`,
      { mode: mode === 'manual' ? 'manual' : 'auto' },
      {
        headers: { 'X-Wechat-Assistant': '1' },
        timeout: 30000,
      },
    );
  },

  /** 停止微信图片 UI 自动化任务。 */
  cancelHdImageAutomationTask(taskId) {
    return client.post(`/image-hd-automation/tasks/${encodeURIComponent(taskId)}/cancel`, null, {
      headers: { 'X-Wechat-Assistant': '1' },
      timeout: 30000,
    });
  },

  /** 用户显式创建语音转文字任务；云端任务必须由界面逐次确认上传。 */
  transcribeVoices(talker, {
    messageRefs,
    messageIds,
    startTime,
    endTime,
    allVoices = false,
    force = false,
    cloudUploadConfirmed = false,
  } = {}) {
    const body = {
      force: Boolean(force),
      all_voices: Boolean(allVoices),
      cloud_upload_confirmed: Boolean(cloudUploadConfirmed),
    };
    if (messageRefs && messageRefs.length > 0) body.message_refs = messageRefs;
    if (messageIds && messageIds.length > 0) body.message_ids = messageIds;
    if (startTime != null) body.start_time = startTime;
    if (endTime != null) body.end_time = endTime;
    return client.post(`/chat/${encodeURIComponent(talker)}/voices/transcribe`, body, {
      headers: { 'X-Wechat-Assistant': '1' },
      timeout: 120000,
    });
  },

  /** 查询语音转文字后台任务。 */
  getVoiceTranscriptionTask(taskId) {
    return client.get(`/voice-transcription/tasks/${encodeURIComponent(taskId)}`, {
      timeout: 120000,
    });
  },

  /** 构造语音音频播放地址（由浏览器直接播放） */
  voiceAudioUrl(talker, messageId, createTime, serverId) {
    const path = `${API_BASE}/chat/${encodeURIComponent(talker)}/voice/${encodeURIComponent(messageId)}/audio`;
    const params = new URLSearchParams({ create_time: String(createTime) });
    if (serverId != null && serverId !== '') params.set('server_id', String(serverId));
    return `${path}?${params.toString()}`;
  },

  /** 构造语音导出下载地址 */
  voiceExportUrl(talker, messageId, createTime, serverId) {
    const path = `${API_BASE}/chat/${encodeURIComponent(talker)}/voice/${encodeURIComponent(messageId)}/export`;
    const params = new URLSearchParams({ create_time: String(createTime) });
    if (serverId != null && serverId !== '') params.set('server_id', String(serverId));
    return `${path}?${params.toString()}`;
  },

  /** 取消仍在执行的语音转文字任务。 */
  cancelVoiceTranscriptionTask(taskId) {
    return client.post(`/voice-transcription/tasks/${encodeURIComponent(taskId)}/cancel`, null, {
      headers: { 'X-Wechat-Assistant': '1' },
      timeout: 120000,
    });
  },

  /** 用户显式触发：从本机微信 4.x 进程获取并保存图片 AES 密钥 */
  extractImageKey() {
    return client.post('/image-key/extract', null, {
      headers: { 'X-Wechat-Assistant': '1' },
      timeout: 120000,
    });
  },

  /** 用户显式点击显示后，读取当前账号已保存的图片 AES 密钥 */
  revealImageKey() {
    return client.get('/image-key/reveal', {
      headers: { 'X-Wechat-Assistant': '1' },
    });
  },

  /** 切换到指定微信账号 */
  switchAccount(index) {
    return client.post('/switch-account', { index });
  },

  /** 自动从微信提取密钥 (Hook 模式，会重启微信) */
  async extractKey() {
    const response = await client.post('/extract-key', null, {
      timeout: 150000,  // 2.5 minutes
    });
    return response;
  },

  /** 删除已保存的密钥 */
  async deleteKey() {
    return client.delete('/key');
  },

  /** 获取聊天消息时间范围 */
  getChatTimeRange(talker) {
    return client.get(`/chat/${encodeURIComponent(talker)}/timerange`);
  },

  /** 清除服务端缓存 */
  async clearCache(options = {}) {
    return client.post('/clear-cache', null, options);
  },

  /** 获取应用设置 */
  getSettings(options = {}) {
    return client.get('/settings', options);
  },

  /** 仅在用户显式点击“显示”时读取已保存的图片识别 API Key。 */
  revealVisionApiKey() {
    return client.get('/settings/vision/api-key/reveal', {
      headers: { 'X-Wechat-Assistant': '1' },
    });
  },

  /** 仅在用户显式点击“显示”时读取已保存的语音转写 API Key。 */
  revealTranscriptionApiKey() {
    return client.get('/settings/transcription/api-key/reveal', {
      headers: { 'X-Wechat-Assistant': '1' },
    });
  },

  /** 仅在用户显式点击“显示”时读取已保存的 AI 分析 API Key。 */
  revealAnalysisApiKey() {
    return client.get('/settings/analysis/api-key/reveal', {
      headers: { 'X-Wechat-Assistant': '1' },
    });
  },

  /** 保存应用设置（只需传要修改的字段） */
  saveSettings(data) {
    return client.post('/settings', data);
  },

  /** 打开原生文件夹选择对话框 */
  async selectFolder() {
    if (isDesktopApp()) {
      const result = await selectDesktopFolder();
      if (!result || result.canceled || !result.path) {
        return { canceled: true, path: null };
      }
      return { canceled: false, path: result.path };
    }
    return client.post('/select-folder');
  },
};

export default api;
