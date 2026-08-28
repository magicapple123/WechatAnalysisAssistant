import Avatar from './Avatar';

const ICONS = {
  chats: (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M7.5 18.5 3 21l1.25-4.5A8.25 8.25 0 1 1 7.5 18.5Z" />
      <path d="M8 9.5h.01M12 9.5h.01M16 9.5h.01" />
    </svg>
  ),
  contacts: (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="9" cy="8" r="3" />
      <path d="M3.5 18c.6-3.3 2.5-5 5.5-5s4.9 1.7 5.5 5" />
      <path d="M16 7h5M18.5 4.5v5" />
      <path d="M15.5 13.5h5M15.5 17h5" />
    </svg>
  ),
  moments: (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="3.2" />
      <path d="M12 2.5a9.5 9.5 0 0 1 8.23 4.75L15.2 8.4M20.23 7.25A9.5 9.5 0 0 1 20.5 16l-4.35-3.05M20.5 16A9.5 9.5 0 0 1 12 21.5l.7-5.25M12 21.5A9.5 9.5 0 0 1 3.77 16.75L8.8 15.6M3.77 16.75A9.5 9.5 0 0 1 3.5 8l4.35 3.05M3.5 8A9.5 9.5 0 0 1 12 2.5l-.7 5.25" />
    </svg>
  ),
  profile: (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="8" r="3.5" />
      <path d="M5.5 20c.7-4.1 2.9-6.2 6.5-6.2s5.8 2.1 6.5 6.2" />
    </svg>
  ),
  refresh: (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M20 7v5h-5" />
      <path d="M4.9 17A8 8 0 1 0 6 6.3L4 8" />
    </svg>
  ),
  settings: (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06-2.83 2.83-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1.04 1.57V21h-4v-.07a1.7 1.7 0 0 0-1.04-1.57 1.7 1.7 0 0 0-1.87.34l-.06.06-2.83-2.83.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-1.53-1H3v-4h.07A1.7 1.7 0 0 0 4.6 9a1.7 1.7 0 0 0-.34-1.87l-.06-.06 2.83-2.83.06.06A1.7 1.7 0 0 0 8.96 4.6 1.7 1.7 0 0 0 10 3.07V3h4v.07a1.7 1.7 0 0 0 1.04 1.53 1.7 1.7 0 0 0 1.87-.34l.06-.06 2.83 2.83-.06.06A1.7 1.7 0 0 0 19.4 9a1.7 1.7 0 0 0 1.53 1H21v4h-.07A1.7 1.7 0 0 0 19.4 15Z" />
    </svg>
  ),
  logout: (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M10 5H6a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h4" />
      <path d="M14 8l4 4-4 4m4-4H9" />
    </svg>
  ),
};

function DesktopAction({ icon, label, onClick, active = false, disabled = false }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={label}
      aria-label={label}
      aria-current={active ? 'page' : undefined}
      className={`group relative flex h-11 w-11 items-center justify-center rounded-xl transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-wechat-green focus-visible:ring-offset-2 focus-visible:ring-offset-[#202326] disabled:cursor-not-allowed disabled:opacity-35 ${
        active
          ? 'bg-wechat-green text-white'
          : 'text-zinc-400 hover:bg-white/10 hover:text-white'
      }`}
    >
      {icon}
      <span className="pointer-events-none absolute left-[calc(100%+12px)] z-50 hidden whitespace-nowrap rounded-md bg-zinc-900 px-2.5 py-1.5 text-xs font-medium text-white shadow-lg group-hover:block group-focus-visible:block">
        {label}
      </span>
    </button>
  );
}

function MobileAction({ icon, label, onClick, active = false, disabled = false }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={label}
      aria-label={label}
      aria-current={active ? 'page' : undefined}
      className={`relative flex min-w-0 flex-1 flex-col items-center justify-center gap-1 px-1 py-1.5 text-[10px] font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-wechat-green disabled:cursor-not-allowed disabled:opacity-35 ${
        active ? 'text-wechat-green' : 'text-zinc-400 hover:text-white'
      }`}
    >
      {active && <span className="absolute top-0 h-0.5 w-6 rounded-full bg-wechat-green" aria-hidden="true" />}
      {icon}
      <span className="max-w-full truncate">{label}</span>
    </button>
  );
}

export default function AppRail({
  status,
  activeView = 'chats',
  activeChat,
  momentsOpen = false,
  onChats,
  onContacts,
  onMoments,
  onProfile,
  onRefresh,
  onSettings,
  onLogout,
}) {
  const account = status?.config || {};
  const accountName = account.display_name || account.alias || account.wxid || '当前账号';
  const chatsLabel = activeChat?.display_name
    ? `聊天记录：${activeChat.display_name}`
    : '聊天记录';

  return (
    <>
      <aside
        className="hidden h-full w-[72px] flex-shrink-0 flex-col items-center border-r border-black/20 bg-[#202326] py-4 md:flex"
        aria-label="主功能导航"
      >
        <button
          type="button"
          onClick={onProfile}
          disabled={!onProfile}
          title={`我的账号：${accountName}`}
          aria-label={`查看我的账号：${accountName}`}
          aria-current={activeView === 'profile' && !momentsOpen ? 'page' : undefined}
          className={`relative mb-6 rounded-xl outline-none transition focus-visible:ring-2 focus-visible:ring-wechat-green focus-visible:ring-offset-2 focus-visible:ring-offset-[#202326] ${
            activeView === 'profile' && !momentsOpen
              ? 'ring-2 ring-wechat-green ring-offset-2 ring-offset-[#202326]'
              : 'hover:brightness-110'
          }`}
        >
          <Avatar
            src={account.avatar_url}
            name={accountName}
            className="h-10 w-10 rounded-xl ring-1 ring-white/10"
          />
          <span
            className="pointer-events-none absolute -bottom-0.5 -right-0.5 h-3 w-3 rounded-full border-2 border-[#202326] bg-wechat-green"
            aria-hidden="true"
          />
        </button>

        <nav className="flex flex-1 flex-col items-center gap-2" aria-label="主要功能">
          <DesktopAction
            icon={ICONS.chats}
            label={chatsLabel}
            onClick={onChats}
            active={activeView === 'chats' && !momentsOpen}
            disabled={!onChats}
          />
          <DesktopAction
            icon={ICONS.contacts}
            label="联系人"
            onClick={onContacts}
            active={activeView === 'contacts' && !momentsOpen}
            disabled={!onContacts}
          />
          {onMoments && (
            <DesktopAction
              icon={ICONS.moments}
              label="朋友圈"
              onClick={onMoments}
              active={momentsOpen}
            />
          )}
        </nav>

        <div className="flex flex-col items-center gap-2 border-t border-white/10 pt-3" aria-label="应用操作">
          <DesktopAction icon={ICONS.refresh} label="刷新数据" onClick={onRefresh} disabled={!onRefresh} />
          <DesktopAction icon={ICONS.settings} label="设置" onClick={onSettings} disabled={!onSettings} />
          <DesktopAction icon={ICONS.logout} label="退出当前账号" onClick={onLogout} disabled={!onLogout} />
        </div>
      </aside>

      <nav
        className="fixed inset-x-0 bottom-0 z-40 flex min-h-[60px] items-stretch border-t border-white/10 bg-[#202326] pb-[env(safe-area-inset-bottom)] shadow-[0_-8px_24px_rgba(0,0,0,0.12)] md:hidden"
        aria-label="移动端主功能导航"
      >
        <MobileAction
          icon={ICONS.chats}
          label="聊天"
          onClick={onChats}
          active={activeView === 'chats' && !momentsOpen}
          disabled={!onChats}
        />
        <MobileAction
          icon={ICONS.contacts}
          label="联系人"
          onClick={onContacts}
          active={activeView === 'contacts' && !momentsOpen}
          disabled={!onContacts}
        />
        {onMoments && (
          <MobileAction
            icon={ICONS.moments}
            label="朋友圈"
            onClick={onMoments}
            active={momentsOpen}
          />
        )}
        <MobileAction
          icon={ICONS.profile}
          label="我"
          onClick={onProfile}
          active={activeView === 'profile' && !momentsOpen}
          disabled={!onProfile}
        />
        <MobileAction icon={ICONS.settings} label="设置" onClick={onSettings} disabled={!onSettings} />
      </nav>
    </>
  );
}
