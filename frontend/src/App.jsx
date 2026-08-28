import { lazy, Suspense, useState, useEffect, useCallback, useRef } from 'react';
import api from './api';
import AppRail from './components/AppRail';
import StatusBar from './components/StatusBar';
import KeyInput from './components/KeyInput';
import ChatList from './components/ChatList';
import ContactsView from './components/ContactsView';
import AccountProfile from './components/AccountProfile';
import { setDesktopWindowMode } from './desktop';

// Heavy workspaces are loaded only when the user opens them. This keeps the
// connection screen fast while preserving normal Vite chunk caching.
const ChatView = lazy(() => import('./components/ChatView'));
const ExportDialog = lazy(() => import('./components/ExportDialog'));
const MomentsExportDialog = lazy(() => import('./components/MomentsExportDialog'));
const SettingsDialog = lazy(() => import('./components/SettingsDialog'));

/**
 * 应用状态枚举
 */
const STATE = {
  LOADING: 'loading',       // 初始加载中
  NEED_KEY: 'need_key',     // 需要输入密钥
  READY: 'ready',           // 就绪 - 显示聊天列表
  ERROR: 'error',           // 出错
};

const WORKSPACE = {
  CHATS: 'chats',
  CONTACTS: 'contacts',
  PROFILE: 'profile',
};

function WorkspaceLoading({ label = '正在加载界面…' }) {
  return (
    <div className="flex flex-1 items-center justify-center gap-2 text-sm text-slate-500" role="status">
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-emerald-500 border-t-transparent" aria-hidden="true" />
      {label}
    </div>
  );
}

function DialogLoading({ label }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/45 backdrop-blur-[2px]" role="status">
      <div className="flex items-center gap-2 rounded-xl bg-white px-5 py-3 text-sm text-slate-600 shadow-xl">
        <span className="h-4 w-4 animate-spin rounded-full border-2 border-emerald-500 border-t-transparent" aria-hidden="true" />
        {label}
      </div>
    </div>
  );
}

/**
 * 微信解析助手 - 主应用
 */
export default function App() {
  const [appState, setAppState] = useState(STATE.LOADING);
  const [error, setError] = useState('');
  const [status, setStatus] = useState(null);
  const [chats, setChats] = useState([]);
  const [activeChat, setActiveChat] = useState(null);
  const [workspace, setWorkspace] = useState(WORKSPACE.CHATS);
  const [contacts, setContacts] = useState([]);
  const [contactsLoading, setContactsLoading] = useState(false);
  const [contactsLoaded, setContactsLoaded] = useState(false);
  const [contactsError, setContactsError] = useState('');
  const [showExport, setShowExport] = useState(false);
  const [showMomentsExport, setShowMomentsExport] = useState(false);
  const [showChatList, setShowChatList] = useState(true);
  const [selectedMessageIds, setSelectedMessageIds] = useState([]);
  const [selectedMessageRefs, setSelectedMessageRefs] = useState([]);
  const [selectedTimeRange, setSelectedTimeRange] = useState(null);
  const [chatRefreshKey, setChatRefreshKey] = useState(0);
  const [showSettings, setShowSettings] = useState(false);
  const [appSettings, setAppSettings] = useState(null);
  const [accountSwitchError, setAccountSwitchError] = useState('');
  const hasEnteredKey = useRef(false);  // 本次会话是否已验证过密钥
  const hasInitialized = useRef(false);
  const initRequestId = useRef(0);
  const chatsRequestId = useRef(0);
  const contactsRequestId = useRef(0);
  const initAppRef = useRef(null);
  const accountSwitchingRef = useRef(false);

  // --- 初始化 ---
  useEffect(() => {
    // React StrictMode intentionally replays effects in development. Avoid
    // running cache clearing and account detection twice during that replay.
    if (hasInitialized.current) return;
    hasInitialized.current = true;
    void initAppRef.current?.();
  }, []);

  useEffect(() => {
    void setDesktopWindowMode(appState === STATE.READY ? 'main' : 'connection');
  }, [appState]);

  const initApp = async ({ acceptDetectedKey = false } = {}) => {
    const requestId = initRequestId.current + 1;
    initRequestId.current = requestId;
    chatsRequestId.current += 1;
    contactsRequestId.current += 1;
    setAppState(STATE.LOADING);
    setError('');
    setContacts([]);
    setContactsLoaded(false);
    setContactsError('');
    setContactsLoading(false);

    try {
      // 清除服务端缓存，确保获取到微信最新数据
      await api.clearCache();
      if (requestId !== initRequestId.current) return false;

      // 1. 获取状态
      const statusRes = await api.getStatus();
      if (requestId !== initRequestId.current) return false;
      setStatus(statusRes.data);

      // 2. 自动检测
      const detectRes = await api.autoDetect();
      if (requestId !== initRequestId.current) return false;
      const { data } = detectRes;

      // 更新状态 (auto-detect 会设置密钥)
      const updatedStatus = await api.getStatus();
      if (requestId !== initRequestId.current) return false;
      setStatus(updatedStatus.data);

      // 首次进入仍展示密钥确认页；已验证过，或刚切换账号且当前账号
      // 自动检测到有效密钥时，才可以进入聊天列表。
      const keyFound = Boolean(data?.key_found);
      if (keyFound && (hasEnteredKey.current || acceptDetectedKey)) {
        hasEnteredKey.current = true;
        return await loadChats();
      } else {
        hasEnteredKey.current = false;
        setAppState(STATE.NEED_KEY);
        if (!data.wxid_found) {
          setError('未检测到本地微信数据，请确认微信已在本机登录过');
        }
      }
      return true;
    } catch (e) {
      if (requestId !== initRequestId.current) return false;
      setAppState(STATE.ERROR);
      setError(e.message || '初始化失败');
      return false;
    }
  };
  initAppRef.current = initApp;

  const loadChats = async () => {
    const requestId = chatsRequestId.current + 1;
    chatsRequestId.current = requestId;
    const appRequestId = initRequestId.current;
    try {
      // 清除缓存以获取最新消息
      await api.clearCache();
      if (requestId !== chatsRequestId.current || appRequestId !== initRequestId.current) return false;
      const res = await api.getChats();
      if (requestId !== chatsRequestId.current || appRequestId !== initRequestId.current) return false;
      setChats(res.data || []);
      setChatRefreshKey((k) => k + 1);  // 通知 ChatView 强制刷新
      setAppState(STATE.READY);
      // 加载应用设置
      try {
        const settingsRes = await api.getSettings();
        if (requestId === chatsRequestId.current && appRequestId === initRequestId.current) {
          setAppSettings(settingsRes.data);
        }
      } catch { /* 设置加载失败不影响主流程 */ }
      return true;
    } catch (e) {
      if (requestId !== chatsRequestId.current || appRequestId !== initRequestId.current) return false;
      setAppState(STATE.ERROR);
      setError('加载聊天列表失败: ' + e.message);
      return false;
    }
  };

  const loadContacts = useCallback(async () => {
    const requestId = contactsRequestId.current + 1;
    contactsRequestId.current = requestId;
    const appRequestId = initRequestId.current;
    setContactsLoading(true);
    setContactsError('');
    try {
      const response = await api.getContacts();
      if (requestId !== contactsRequestId.current || appRequestId !== initRequestId.current) return;
      const payload = Array.isArray(response)
        ? response
        : Array.isArray(response?.data)
          ? response.data
          : Array.isArray(response?.contacts)
            ? response.contacts
            : null;
      if (!payload) throw new Error('联系人接口返回格式不正确');
      setContacts(payload);
      setContactsLoaded(true);
    } catch (loadError) {
      if (requestId !== contactsRequestId.current || appRequestId !== initRequestId.current) return;
      setContacts([]);
      setContactsLoaded(false);
      setContactsError(loadError.message || '读取联系人失败');
    } finally {
      if (requestId === contactsRequestId.current && appRequestId === initRequestId.current) {
        setContactsLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    if (
      appState === STATE.READY
      && workspace === WORKSPACE.CONTACTS
      && !contactsLoaded
      && !contactsLoading
      && !contactsError
    ) {
      void loadContacts();
    }
  }, [
    appState,
    contactsError,
    contactsLoaded,
    contactsLoading,
    loadContacts,
    workspace,
  ]);

  // --- 密钥设置回调 ---
  const handleKeySet = useCallback(async (key) => {
    const requestId = initRequestId.current + 1;
    initRequestId.current = requestId;
    chatsRequestId.current += 1;
    contactsRequestId.current += 1;
    setError('');
    try {
      await api.setKey(key);
      if (requestId !== initRequestId.current) return;
      hasEnteredKey.current = true;  // 标记已验证，刷新时跳过密钥页
      const updatedStatus = await api.getStatus();
      if (requestId !== initRequestId.current) return;
      setStatus(updatedStatus.data);
      await loadChats();
    } catch (e) {
      if (requestId !== initRequestId.current) return;
      setError(e.message || '密钥设置失败');
    }
  }, []);

  // --- 聊天选择 ---
  const handleSelectChat = useCallback((chat) => {
    setWorkspace(WORKSPACE.CHATS);
    setShowMomentsExport(false);
    setActiveChat(chat);
    setShowChatList(false);
  }, []);

  const handleBackToList = useCallback(() => {
    setActiveChat(null);
    setShowChatList(true);
  }, []);

  const handleOpenChatWorkspace = useCallback(() => {
    setWorkspace(WORKSPACE.CHATS);
    setShowMomentsExport(false);
  }, []);

  const handleOpenContacts = useCallback(() => {
    setWorkspace(WORKSPACE.CONTACTS);
    setShowMomentsExport(false);
    if (!contactsLoaded && !contactsLoading) void loadContacts();
  }, [contactsLoaded, contactsLoading, loadContacts]);

  const handleOpenProfile = useCallback(() => {
    setWorkspace(WORKSPACE.PROFILE);
    setShowMomentsExport(false);
  }, []);

  const handleLogout = useCallback(async () => {
    initRequestId.current += 1;
    chatsRequestId.current += 1;
    contactsRequestId.current += 1;
    try { await api.deleteKey(); } catch { /* 密钥不存在时仍继续清理本地状态 */ }
    await api.clearCache();
    hasEnteredKey.current = false;
    setChats([]);
    setContacts([]);
    setContactsLoaded(false);
    setContactsError('');
    setAccountSwitchError('');
    setActiveChat(null);
    setWorkspace(WORKSPACE.CHATS);
    setShowChatList(true);
    setShowMomentsExport(false);
    setAppSettings(null);
    setAppState(STATE.NEED_KEY);
  }, []);

  const handleSwitchAccount = useCallback(async (index) => {
    if (accountSwitchingRef.current) return false;
    accountSwitchingRef.current = true;
    const requestId = initRequestId.current + 1;
    initRequestId.current = requestId;
    chatsRequestId.current += 1;
    contactsRequestId.current += 1;
    setAccountSwitchError('');
    let backendSwitched = false;
    try {
      const result = await api.switchAccount(index);
      if (requestId !== initRequestId.current) return false;
      if (!result?.success) {
        throw new Error(result?.message || '切换账号失败');
      }
      backendSwitched = true;
      setAppState(STATE.LOADING);
      setAppSettings(null);
      // 后端已切换账号；客户端也必须丢弃上一账号的验证状态。
      hasEnteredKey.current = false;
      setChats([]);
      setContacts([]);
      setContactsLoaded(false);
      setContactsError('');
      setActiveChat(null);
      setWorkspace(WORKSPACE.CHATS);
      setShowChatList(true);
      setShowMomentsExport(false);
      // 新账号只有在 auto-detect 验证出属于它的密钥后才进入 READY。
      return await initAppRef.current?.({ acceptDetectedKey: true }) ?? false;
    } catch (e) {
      if (requestId !== initRequestId.current) return false;
      console.error('切换账号失败:', e);
      const message = e.message || '切换账号失败，请重试';
      setAccountSwitchError(message);
      setError(message);
      if (backendSwitched) setAppState(STATE.ERROR);
      return false;
    } finally {
      accountSwitchingRef.current = false;
    }
  }, []);

  // --- 导出 ---
  const handleExportSingle = useCallback(() => {
    if (activeChat) {
      setSelectedMessageIds([]);
      setSelectedMessageRefs([]);
      setSelectedTimeRange(null);
      setShowExport(true);
    }
  }, [activeChat]);

  const handleExportAll = useCallback(() => {
    // “全部导出”是全局操作，从聊天详情触发时先切回聊天工作区，
    // 避免沿用 activeChat 而被误判成单个聊天导出。
    setActiveChat(null);
    setWorkspace(WORKSPACE.CHATS);
    setShowMomentsExport(false);
    setShowChatList(true);
    setSelectedMessageIds([]);
    setSelectedMessageRefs([]);
    setSelectedTimeRange(null);
    setShowExport(true);
  }, []);

  const handleSelectedExport = useCallback((messageRefs, startTime, endTime) => {
    setSelectedMessageRefs(messageRefs);
    setSelectedMessageIds([...new Set(messageRefs.map((reference) => reference.id))]);
    setSelectedTimeRange(startTime && endTime ? { startTime, endTime } : null);
    setShowExport(true);
  }, []);

  const handleOpenMomentsExport = useCallback(() => {
    setShowExport(false);
    setShowMomentsExport(true);
  }, []);

  // --- 设置 ---
  const handleOpenSettings = useCallback(() => {
    setShowSettings(true);
  }, []);

  const handleCloseSettings = useCallback(() => {
    setShowSettings(false);
  }, []);

  const handleSaveSettings = useCallback(async (settingsData) => {
    const res = await api.saveSettings(settingsData);
    setAppSettings(res.data);
  }, []);

  const handleExtractImageKey = useCallback(async () => {
    const res = await api.extractImageKey();
    if (res?.data) setAppSettings(res.data);
    return res;
  }, []);

  const handleExportConfirm = useCallback(async (format, options = {}) => {
    try {
      let result;
      if (activeChat) {
        result = await api.exportChat(
          activeChat.talker,
          activeChat.display_name,
          format,
          {
            messageIds: selectedMessageIds.length > 0 ? selectedMessageIds : undefined,
            messageRefs: selectedMessageRefs.length > 0 ? selectedMessageRefs : undefined,
            startTime: options.startTime ?? selectedTimeRange?.startTime,
            endTime: options.endTime ?? selectedTimeRange?.endTime,
            filename: options.filename,
            replaceImagesWithDescriptions: options.replaceImagesWithDescriptions,
            replaceVoicesWithTranscriptions: options.replaceVoicesWithTranscriptions,
            embedImages: options.embedImages,
            htmlImageQuality: options.htmlImageQuality,
          }
        );
      } else {
        result = await api.exportAll(format, {
          replaceImagesWithDescriptions: options.replaceImagesWithDescriptions,
          replaceVoicesWithTranscriptions: options.replaceVoicesWithTranscriptions,
          embedImages: options.embedImages,
          htmlImageQuality: options.htmlImageQuality,
        });
      }
      setShowExport(false);
      setSelectedMessageIds([]);
      setSelectedMessageRefs([]);
      setSelectedTimeRange(null);
      // 显示保存成功提示
      if (result?.path) {
        alert(`导出成功！\n文件已保存到:\n${result.path}`);
      }
    } catch (e) {
      alert('导出失败: ' + e.message);
    }
  }, [activeChat, selectedMessageIds, selectedMessageRefs, selectedTimeRange]);

  const handleMomentsExportConfirm = useCallback(async (options) => {
    try {
      const result = await api.exportMoments(options);
      setShowMomentsExport(false);
      const countText = Number.isFinite(Number(result?.posts_count))
        ? `\n共导出 ${Number(result.posts_count)} 条动态`
        : '';
      const mediaText = Number(result?.media_downloaded || 0) > 0 || Number(result?.media_failed || 0) > 0
        ? `\n图片下载：成功 ${Number(result?.media_downloaded || 0)}，未下载 ${Number(result?.media_failed || 0)}`
        : '';
      const parseText = Number(result?.parse_failures || 0) > 0
        ? `\n另有 ${Number(result.parse_failures)} 条本地动态因格式异常未能解析`
        : '';
      if (result?.path) {
        alert(`朋友圈导出成功！${countText}${mediaText}${parseText}\n文件已保存到:\n${result.path}`);
      } else {
        alert(`朋友圈导出成功！${countText}${mediaText}${parseText}`);
      }
      return result;
    } catch (e) {
      alert('朋友圈导出失败: ' + e.message);
      throw e;
    }
  }, []);

  const handleMomentsAnalyze = useCallback(async (options) => {
    return api.analyzeMoments(options);
  }, []);

  // --- 渲染 ---

  // 加载中
  if (appState === STATE.LOADING) {
    return (
      <div className="app-viewport flex items-center justify-center bg-[#f5f7f6] px-6" role="status" aria-live="polite">
        <div className="text-center">
          <div className="mx-auto mb-4 h-9 w-9 animate-spin rounded-full border-[3px] border-emerald-500 border-t-transparent" aria-hidden="true" />
          <p className="text-sm font-medium text-slate-600">正在检测微信数据…</p>
          <p className="mt-1 text-xs text-slate-400">所有数据库读取均在本机完成</p>
        </div>
      </div>
    );
  }

  // 需要密钥
  if (appState === STATE.NEED_KEY) {
    return (
      <div className="app-viewport flex flex-col bg-[#f5f7f6]">
        <StatusBar
          status={status}
          title="连接微信数据"
          subtitle="选择账号并验证数据库解密密钥"
          onRefresh={initApp}
          onSwitchAccount={handleSwitchAccount}
        />
        <KeyInput onSubmit={handleKeySet} error={error} status={status} />
      </div>
    );
  }

  // 错误
  if (appState === STATE.ERROR) {
    return (
      <div className="app-viewport flex items-center justify-center bg-[#f5f7f6] px-5">
        <div role="alert" className="w-full max-w-md rounded-2xl border border-slate-200/80 bg-white p-7 text-center shadow-sm shadow-slate-200/50">
          <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-xl bg-red-50 text-red-600">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
              <circle cx="12" cy="12" r="9" />
              <path d="M12 7.5v5M12 16.5h.01" />
            </svg>
          </div>
          <h2 className="text-lg font-semibold text-slate-900">读取数据时遇到问题</h2>
          <p className="mt-2 text-sm leading-6 text-slate-500">{error}</p>
          <button
            type="button"
            onClick={initApp}
            className="mt-5 rounded-xl bg-wechat-green px-6 py-2.5 text-sm font-medium text-white transition-colors hover:bg-wechat-green-dark"
          >
            重试
          </button>
        </div>
      </div>
    );
  }

  // 主界面 - READY
  const visibleContactCount = new Set(
    contacts
      .filter((contact) => !contact?.is_self)
      .map((contact) => String(contact?.username || contact?.talker || '').trim())
      .filter(Boolean)
  ).size;
  const workspaceHeader = workspace === WORKSPACE.CONTACTS
    ? {
        title: '联系人',
        subtitle: '查看当前账号保存在本机的联系人资料与聊天记录',
        count: contactsLoaded ? visibleContactCount : null,
      }
    : workspace === WORKSPACE.PROFILE
      ? {
          title: '我的账号',
          subtitle: '账号资料、本地数据状态与账号管理',
          count: null,
        }
      : {
          title: activeChat ? '聊天详情' : '聊天记录',
          subtitle: activeChat ? activeChat.display_name : '浏览、处理与分析本机微信记录',
          count: activeChat ? null : chats.length,
        };

  return (
    <div className="app-viewport flex min-h-0 overflow-hidden bg-slate-100 text-slate-800">
      <AppRail
        status={status}
        activeView={workspace}
        activeChat={activeChat}
        momentsOpen={showMomentsExport}
        onChats={handleOpenChatWorkspace}
        onContacts={handleOpenContacts}
        onMoments={status?.config?.supports_moments ? handleOpenMomentsExport : null}
        onProfile={handleOpenProfile}
        onRefresh={initApp}
        onSettings={handleOpenSettings}
        onLogout={handleLogout}
      />

      <div className="flex min-w-0 flex-1 flex-col overflow-hidden pb-[calc(60px+env(safe-area-inset-bottom))] md:pb-0">
        <StatusBar
          status={status}
          chats={chats}
          activeChat={workspace === WORKSPACE.CHATS ? activeChat : null}
          title={workspaceHeader.title}
          subtitle={workspaceHeader.subtitle}
          count={workspaceHeader.count}
          onBack={workspace === WORKSPACE.CHATS && activeChat ? handleBackToList : null}
        />

        {/* 主体 */}
        <div className="flex min-h-0 flex-1 overflow-hidden">
          {workspace === WORKSPACE.CONTACTS ? (
            <ContactsView
              contacts={contacts}
              chats={chats}
              loading={contactsLoading}
              error={contactsError}
              onRetry={loadContacts}
              onOpenChat={handleSelectChat}
            />
          ) : workspace === WORKSPACE.PROFILE ? (
            <AccountProfile
              status={status}
              onSwitchAccount={handleSwitchAccount}
              switchError={accountSwitchError}
              onRefresh={initApp}
              onLogout={handleLogout}
            />
          ) : (
            <>
              {/* 聊天列表 (左侧) */}
              <div
                className={`${
                  showChatList ? 'flex' : 'hidden'
                } min-w-0 w-full flex-shrink-0 overflow-hidden border-r border-slate-200/80 bg-slate-50 md:flex md:w-80 lg:w-[22rem]`}
              >
                <ChatList
                  chats={chats}
                  activeTalker={activeChat?.talker}
                  onSelect={handleSelectChat}
                  onRefresh={loadChats}
                />
              </div>

              {/* 聊天详情 (右侧) */}
              <div
                className={`${
                  !showChatList ? 'flex' : 'hidden'
                } min-w-0 flex-1 flex-col overflow-hidden bg-[#f5f7f6] md:flex`}
              >
                {activeChat ? (
                  <Suspense fallback={<WorkspaceLoading label="正在加载聊天详情…" />}>
                    <ChatView
                      chat={activeChat}
                      onExport={handleExportSingle}
                      onExportSelected={handleSelectedExport}
                      refreshKey={chatRefreshKey}
                      settings={appSettings}
                      onSaveSettings={handleSaveSettings}
                      selfAvatarUrl={status?.config?.avatar_url || ''}
                      selfDisplayName={status?.config?.display_name || '我'}
                    />
                  </Suspense>
                ) : (
                  <div className="flex flex-1 items-center justify-center bg-[radial-gradient(circle_at_center,_rgba(7,193,96,0.055),_transparent_48%)] px-6">
                    <div className="max-w-sm text-center text-slate-400">
                      <div className="mx-auto mb-5 flex h-16 w-16 items-center justify-center rounded-2xl border border-white bg-white/80 text-emerald-600 shadow-sm shadow-slate-200/70">
                        <svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
                          <path d="M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7A8.4 8.4 0 0 1 4 11.5a8.5 8.5 0 0 1 4.7-7.6A8.4 8.4 0 0 1 12.5 3H13a8.5 8.5 0 0 1 8 8v.5Z" />
                          <path d="M8.5 10.5h.01M12.5 10.5h.01M16.5 10.5h.01" strokeLinecap="round" strokeWidth="2.4" />
                        </svg>
                      </div>
                      <p className="text-sm font-semibold text-slate-600">选择一个聊天</p>
                      <p className="mt-1.5 text-xs leading-5 text-slate-400">在左侧会话列表中选择联系人，查看、处理或分析聊天记录。</p>
                    </div>
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      </div>

      {/* 导出对话框 */}
      {showExport && (
        <Suspense fallback={<DialogLoading label="正在加载导出设置…" />}>
          <ExportDialog
            chatName={activeChat?.display_name}
            talker={activeChat?.talker}
            selectedCount={selectedMessageRefs.length || selectedMessageIds.length}
            selectedTimeRange={selectedTimeRange}
            onConfirm={handleExportConfirm}
            onClose={() => {
              setShowExport(false);
              setSelectedMessageIds([]);
              setSelectedMessageRefs([]);
              setSelectedTimeRange(null);
            }}
          />
        </Suspense>
      )}

      {/* 朋友圈导出对话框 */}
      {showMomentsExport && (
        <Suspense fallback={<DialogLoading label="正在加载朋友圈工具…" />}>
          <MomentsExportDialog
            onConfirm={handleMomentsExportConfirm}
            onAnalyze={handleMomentsAnalyze}
            analysisSettings={appSettings?.analysis}
            onClose={() => setShowMomentsExport(false)}
          />
        </Suspense>
      )}

      {/* 设置对话框 */}
      {showSettings && (
        <Suspense fallback={<DialogLoading label="正在加载设置…" />}>
          <SettingsDialog
            settings={appSettings}
            onSaveSettings={handleSaveSettings}
            onExtractImageKey={handleExtractImageKey}
            onExportAll={handleExportAll}
            onClose={handleCloseSettings}
          />
        </Suspense>
      )}
    </div>
  );
}
