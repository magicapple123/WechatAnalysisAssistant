function RefreshIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <path d="M3 12a9 9 0 0 1 15.4-6.4L21 8" />
      <path d="M21 3v5h-5" />
      <path d="M21 12a9 9 0 0 1-15.4 6.4L3 16" />
      <path d="M3 21v-5h5" />
    </svg>
  );
}

/**
 * 工作区顶栏只承载页面上下文和连接状态。
 * READY 状态的账号资料与账号切换位于“我的账号”；密钥引导页仍可在此切换账号。
 */
export default function StatusBar({
  status,
  chats = [],
  activeChat,
  title,
  subtitle,
  count,
  onRefresh,
  onBack,
  onSwitchAccount,
}) {
  const config = status?.config;
  const resolvedTitle = title || (activeChat ? '聊天详情' : '聊天记录');
  const resolvedSubtitle = subtitle ?? (
    activeChat ? activeChat.display_name : '浏览、处理与分析本机微信记录'
  );
  const resolvedCount = count !== undefined
    ? count
    : (!activeChat && chats.length > 0 ? chats.length : null);
  const showAccountSwitcher = Boolean(onSwitchAccount && (config?.account_count || 0) > 0);

  return (
    <header className="app-topbar flex h-16 flex-shrink-0 items-center border-b border-slate-200/80 bg-white/90 px-4 backdrop-blur-xl sm:px-5">
      <div className="flex min-w-0 flex-1 items-center gap-3">
        {onBack && (
          <button
            type="button"
            onClick={onBack}
            className="app-icon-button md:hidden"
            title="返回"
            aria-label="返回"
          >
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
              <path d="m15 18-6-6 6-6" />
            </svg>
          </button>
        )}

        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h1 className="truncate text-[15px] font-semibold tracking-tight text-slate-900">
              {resolvedTitle}
            </h1>
            {Number.isFinite(Number(resolvedCount)) && Number(resolvedCount) > 0 && (
              <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-500">
                {Number(resolvedCount).toLocaleString()}
              </span>
            )}
          </div>
          {resolvedSubtitle && (
            <p className="mt-0.5 hidden truncate text-xs text-slate-400 sm:block">
              {resolvedSubtitle}
            </p>
          )}
        </div>
      </div>

      <div className="flex min-w-0 items-center justify-end gap-2 sm:gap-3">
        {status && (
          <span
            className={`hidden items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium sm:flex ${
              config?.has_key
                ? 'border-emerald-100 bg-emerald-50 text-emerald-700'
                : 'border-amber-100 bg-amber-50 text-amber-700'
            }`}
          >
            <span className={`h-1.5 w-1.5 rounded-full ${config?.has_key ? 'bg-emerald-500' : 'bg-amber-500'}`} />
            {config?.has_key ? '数据已连接' : '等待密钥'}
          </span>
        )}

        {showAccountSwitcher && (
          <label className="relative min-w-0">
            <span className="sr-only">切换微信账号</span>
            <select
              value={config.active_index}
              onChange={(event) => onSwitchAccount(Number(event.target.value))}
              className="h-9 max-w-[150px] appearance-none rounded-lg border border-slate-200 bg-white py-1 pl-3 pr-8 text-xs font-medium text-slate-700 outline-none transition hover:border-slate-300 focus:border-emerald-500 focus:ring-2 focus:ring-emerald-100 sm:max-w-[220px]"
              aria-label="切换微信账号"
            >
              {config.accounts?.map((account, index) => {
                const label = account.display_name || account.alias || account.wxid;
                const hint = account.last_active ? ` · ${account.last_active}` : '';
                return (
                  <option key={account.wxid} value={index}>
                    {label}{hint}{index === config.active_index ? ' ✓' : ''}
                  </option>
                );
              })}
            </select>
            <svg
              className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-400"
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              aria-hidden="true"
            >
              <path d="m7 10 5 5 5-5" />
            </svg>
          </label>
        )}

        {onRefresh && (
          <button
            type="button"
            onClick={onRefresh}
            className="app-icon-button"
            title="重新检测"
            aria-label="重新检测"
          >
            <RefreshIcon />
          </button>
        )}
      </div>
    </header>
  );
}
