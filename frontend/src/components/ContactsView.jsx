import { useEffect, useMemo, useState } from 'react';
import Avatar from './Avatar';

const CONTACT_CATEGORIES = [
  { id: 'contacts', label: '联系人' },
  { id: 'groups', label: '群聊' },
  { id: 'official', label: '公众号' },
];

function text(value) {
  return String(value ?? '').trim();
}

function normalizeContact(contact, chatMap) {
  const username = text(contact?.username || contact?.user_name || contact?.talker);
  const chat = chatMap.get(username);
  const remark = text(contact?.remark || contact?.remark_name);
  const nickname = text(contact?.nick_name || contact?.nickname);
  const alias = text(contact?.alias);
  const displayName = text(
    contact?.display_name || contact?.displayName || remark || nickname || chat?.display_name || username
  );
  const isGroup = Boolean(contact?.is_group) || username.endsWith('@chatroom');
  const isOfficial = Boolean(contact?.is_official)
    || username.startsWith('gh_');
  const category = isGroup ? 'groups' : isOfficial ? 'official' : 'contacts';
  return {
    ...contact,
    username,
    displayName: displayName || '未知联系人',
    remark,
    nickname,
    alias,
    description: text(contact?.description),
    avatarUrl: text(contact?.avatar_url || contact?.avatarUrl || chat?.avatar_url),
    isGroup,
    isOfficial,
    isSelf: Boolean(contact?.is_self),
    category,
    chat,
    hasChat: Boolean(contact?.has_chat || chat),
    messageCount: Number(contact?.message_count ?? chat?.message_count ?? 0) || 0,
    lastMessage: text(contact?.last_message || chat?.last_message),
    lastTime: text(contact?.last_time_str || chat?.last_time_str),
  };
}

function categoryLabel(contact) {
  if (contact.isGroup) return '群聊';
  if (contact.isOfficial) return '公众号';
  return '联系人';
}

function SearchIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <circle cx="11" cy="11" r="7" />
      <path d="m20 20-4-4" />
    </svg>
  );
}

function EmptyContactIcon() {
  return (
    <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
      <circle cx="9" cy="8" r="3" />
      <path d="M3.5 18c.6-3.3 2.5-5 5.5-5s4.9 1.7 5.5 5" />
      <path d="M16 8h5M16 12h5M16 16h5" />
    </svg>
  );
}

export default function ContactsView({
  contacts = [],
  chats = [],
  loading = false,
  error = '',
  onRetry,
  onOpenChat,
}) {
  const [query, setQuery] = useState('');
  const [category, setCategory] = useState('contacts');
  const [activeUsername, setActiveUsername] = useState('');
  const [showDetails, setShowDetails] = useState(false);

  const chatMap = useMemo(() => new Map(
    chats
      .map((chat) => [text(chat?.talker), chat])
      .filter(([username]) => Boolean(username))
  ), [chats]);

  const normalizedContacts = useMemo(() => {
    const unique = new Map();
    contacts
      .map((contact) => normalizeContact(contact, chatMap))
      .filter((contact) => contact.username && !contact.isSelf)
      .forEach((contact) => {
        if (!unique.has(contact.username)) unique.set(contact.username, contact);
      });
    return [...unique.values()].sort((left, right) => left.displayName.localeCompare(
      right.displayName,
      'zh-CN',
      { numeric: true, sensitivity: 'base' }
    ));
  }, [contacts, chatMap]);

  const counts = useMemo(() => normalizedContacts.reduce((result, contact) => {
    result[contact.category] = (result[contact.category] || 0) + 1;
    return result;
  }, { contacts: 0, groups: 0, official: 0 }), [normalizedContacts]);

  const filteredContacts = useMemo(() => {
    const keyword = query.trim().toLocaleLowerCase();
    return normalizedContacts.filter((contact) => {
      if (contact.category !== category) return false;
      if (!keyword) return true;
      return [
        contact.displayName,
        contact.remark,
        contact.nickname,
        contact.alias,
        contact.username,
      ].some((value) => value.toLocaleLowerCase().includes(keyword));
    });
  }, [normalizedContacts, query, category]);

  const activeContact = useMemo(() => normalizedContacts.find(
    (contact) => contact.username === activeUsername
  ) || null, [normalizedContacts, activeUsername]);

  useEffect(() => {
    if (filteredContacts.some((contact) => contact.username === activeUsername)) return;
    setActiveUsername(filteredContacts[0]?.username || '');
    setShowDetails(false);
  }, [activeUsername, filteredContacts]);

  const selectContact = (contact) => {
    setActiveUsername(contact.username);
    setShowDetails(true);
  };

  const openChat = () => {
    if (!activeContact?.hasChat || !onOpenChat) return;
    onOpenChat({
      ...(activeContact.chat || {}),
      talker: activeContact.username,
      display_name: activeContact.displayName,
      avatar_url: activeContact.avatarUrl,
      is_group: activeContact.isGroup,
      message_count: activeContact.messageCount,
    });
  };

  return (
    <div className="flex min-h-0 flex-1 overflow-hidden bg-[#f5f7f6]">
      <aside className={`${showDetails ? 'hidden' : 'flex'} min-w-0 w-full flex-shrink-0 flex-col border-r border-slate-200/80 bg-slate-50 md:flex md:w-80 lg:w-[22rem]`}>
        <div className="flex-shrink-0 border-b border-slate-200/80 bg-white px-4 py-4">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h2 className="text-sm font-semibold text-slate-900">通讯录</h2>
              <p className="mt-0.5 text-xs text-slate-400">当前账号共 {normalizedContacts.length.toLocaleString()} 项</p>
            </div>
            <button
              type="button"
              onClick={onRetry}
              disabled={loading || !onRetry}
              className="app-icon-button"
              title="刷新联系人"
              aria-label="刷新联系人"
            >
              <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                <path d="M20 7v5h-5" />
                <path d="M4.9 17A8 8 0 1 0 6 6.3L4 8" />
              </svg>
            </button>
          </div>

          <label className="relative mt-3 block">
            <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400"><SearchIcon /></span>
            <span className="sr-only">搜索联系人</span>
            <input
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索备注、昵称或微信号"
              className="h-10 w-full rounded-xl border border-slate-200 bg-slate-50 pl-9 pr-3 text-sm text-slate-700 outline-none transition placeholder:text-slate-400 focus:border-emerald-500 focus:bg-white focus:ring-2 focus:ring-emerald-100"
            />
          </label>

          <div className="mt-3 grid grid-cols-3 gap-1 rounded-xl bg-slate-100 p-1" aria-label="联系人分类">
            {CONTACT_CATEGORIES.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => setCategory(item.id)}
                aria-pressed={category === item.id}
                className={`rounded-lg px-2 py-1.5 text-xs font-medium transition ${
                  category === item.id
                    ? 'bg-white text-emerald-700 shadow-sm'
                    : 'text-slate-500 hover:text-slate-800'
                }`}
              >
                {item.label} <span className="text-[10px] opacity-60">{counts[item.id] || 0}</span>
              </button>
            ))}
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto">
          {loading && (
            <div className="flex h-full min-h-48 items-center justify-center gap-2 text-sm text-slate-500">
              <span className="h-4 w-4 animate-spin rounded-full border-2 border-emerald-500 border-t-transparent" />
              正在读取联系人…
            </div>
          )}
          {!loading && error && (
            <div className="m-4 rounded-xl border border-red-200 bg-red-50 p-4 text-center">
              <p role="alert" className="text-sm leading-5 text-red-700">{error}</p>
              <button
                type="button"
                onClick={onRetry}
                className="mt-3 rounded-lg bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-800"
              >
                重新加载
              </button>
            </div>
          )}
          {!loading && !error && filteredContacts.length === 0 && (
            <div className="flex h-full min-h-56 flex-col items-center justify-center px-6 text-center text-slate-400">
              <EmptyContactIcon />
              <p className="mt-3 text-sm font-medium text-slate-500">没有匹配的{CONTACT_CATEGORIES.find((item) => item.id === category)?.label}</p>
              <p className="mt-1 text-xs">尝试更换分类或搜索关键词</p>
            </div>
          )}
          {!loading && !error && filteredContacts.map((contact) => (
            <button
              key={contact.username}
              type="button"
              onClick={() => selectContact(contact)}
              aria-current={contact.username === activeUsername ? 'true' : undefined}
              className={`flex w-full items-center gap-3 border-b border-slate-100 px-4 py-3 text-left transition-colors ${
                contact.username === activeUsername
                  ? 'bg-emerald-50/80'
                  : 'bg-white hover:bg-slate-50'
              }`}
            >
              <Avatar
                src={contact.avatarUrl}
                name={contact.displayName}
                isGroup={contact.isGroup}
                className="h-10 w-10 rounded-lg"
              />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm font-medium text-slate-800">{contact.displayName}</span>
                <span className="mt-0.5 block truncate text-xs text-slate-400">
                  {contact.lastMessage || contact.nickname || contact.alias || contact.username}
                </span>
              </span>
              {contact.hasChat && (
                <span className="rounded-full bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-500">
                  {contact.messageCount > 0 ? contact.messageCount.toLocaleString() : '记录'}
                </span>
              )}
            </button>
          ))}
        </div>
      </aside>

      <main className={`${showDetails ? 'flex' : 'hidden'} min-w-0 flex-1 flex-col overflow-y-auto md:flex`}>
        {activeContact ? (
          <div className="mx-auto flex w-full max-w-4xl flex-1 flex-col px-4 py-5 sm:px-8 sm:py-8">
            <button
              type="button"
              onClick={() => setShowDetails(false)}
              className="mb-4 inline-flex w-fit items-center gap-1 rounded-lg px-2 py-1.5 text-sm text-slate-500 hover:bg-white hover:text-slate-800 md:hidden"
            >
              <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true"><path d="m15 18-6-6 6-6" /></svg>
              返回联系人
            </button>

            <section className="overflow-hidden rounded-2xl border border-slate-200/80 bg-white shadow-sm shadow-slate-200/40">
              <div className="flex flex-col gap-5 border-b border-slate-100 px-5 py-6 sm:flex-row sm:items-center sm:px-7">
                <Avatar
                  src={activeContact.avatarUrl}
                  name={activeContact.displayName}
                  isGroup={activeContact.isGroup}
                  className="h-20 w-20 rounded-2xl text-xl"
                />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <h2 className="truncate text-xl font-semibold tracking-tight text-slate-900">{activeContact.displayName}</h2>
                    <span className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[11px] font-medium text-slate-500">
                      {categoryLabel(activeContact)}
                    </span>
                  </div>
                  {activeContact.remark && activeContact.nickname && activeContact.remark !== activeContact.nickname && (
                    <p className="mt-1.5 text-sm text-slate-500">昵称：{activeContact.nickname}</p>
                  )}
                  <p className="mt-1 truncate text-xs text-slate-400">{activeContact.username}</p>
                </div>
              </div>

              <dl className="divide-y divide-slate-100 px-5 sm:px-7">
                {[
                  ['备注', activeContact.remark || '未设置'],
                  ['昵称', activeContact.nickname || '未记录'],
                  ['微信号', activeContact.alias || activeContact.username],
                  ['聊天记录', activeContact.hasChat
                    ? `${activeContact.messageCount.toLocaleString()} 条${activeContact.lastTime ? ` · 最近 ${activeContact.lastTime}` : ''}`
                    : '当前本地消息库中没有记录'],
                ].map(([label, value]) => (
                  <div key={label} className="grid grid-cols-[5rem_minmax(0,1fr)] gap-4 py-3.5 text-sm">
                    <dt className="text-slate-400">{label}</dt>
                    <dd className="break-all text-slate-700">{value}</dd>
                  </div>
                ))}
              </dl>

              {activeContact.description && (
                <div className="border-t border-slate-100 px-5 py-4 sm:px-7">
                  <p className="text-xs font-medium text-slate-400">联系人说明</p>
                  <p className="mt-1.5 whitespace-pre-wrap text-sm leading-6 text-slate-600">{activeContact.description}</p>
                </div>
              )}

              <div className="flex items-center justify-end border-t border-slate-100 bg-slate-50/70 px-5 py-4 sm:px-7">
                <button
                  type="button"
                  onClick={openChat}
                  disabled={!activeContact.hasChat || !onOpenChat}
                  className="inline-flex items-center gap-2 rounded-xl bg-wechat-green px-5 py-2.5 text-sm font-medium text-white transition hover:bg-wechat-green-dark disabled:cursor-not-allowed disabled:bg-slate-200 disabled:text-slate-400"
                  title={activeContact.hasChat ? '进入只读聊天记录' : '当前联系人没有本地聊天记录'}
                >
                  <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                    <path d="M7.5 18.5 3 21l1.25-4.5A8.25 8.25 0 1 1 7.5 18.5Z" />
                    <path d="M8 9.5h.01M12 9.5h.01M16 9.5h.01" />
                  </svg>
                  查看聊天记录
                </button>
              </div>
            </section>
          </div>
        ) : (
          <div className="flex flex-1 items-center justify-center px-6 text-center text-slate-400">
            <div>
              <div className="mx-auto flex h-16 w-16 items-center justify-center rounded-2xl border border-white bg-white/80 text-emerald-600 shadow-sm"><EmptyContactIcon /></div>
              <p className="mt-4 text-sm font-semibold text-slate-600">选择一个联系人</p>
              <p className="mt-1 text-xs">查看本地保存的基本资料和聊天记录状态</p>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
