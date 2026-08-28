import { useState } from 'react';
import Avatar from './Avatar';

/**
 * 聊天列表组件
 *
 * 展示所有聊天联系人，支持搜索过滤。
 * 类似微信的聊天列表布局。
 */
export default function ChatList({ chats = [], activeTalker, onSelect, onRefresh }) {
  const [search, setSearch] = useState('');
  const [loading, setLoading] = useState(false);

  const handleRefresh = async () => {
    if (typeof onRefresh !== 'function') return;
    setLoading(true);
    try {
      await onRefresh();
    } finally {
      setLoading(false);
    }
  };

  // 搜索过滤
  const normalizedSearch = search.trim().toLocaleLowerCase('zh-CN');
  const filtered = normalizedSearch
    ? chats.filter((chat) => (
        [chat?.display_name, chat?.talker, chat?.remark]
          .some((value) => String(value || '').toLocaleLowerCase('zh-CN').includes(normalizedSearch))
      ))
    : chats;

  return (
    <div className="flex h-full w-full flex-col overflow-hidden bg-[#fafbfa]">
      <div className="px-4 pb-3 pt-4 sm:px-5">
        <div className="mb-3 flex items-end justify-between">
          <div>
            <h2 className="text-base font-semibold tracking-tight text-slate-900">会话</h2>
            <p className="mt-0.5 text-[11px] text-slate-400">共 {chats.length.toLocaleString()} 个聊天</p>
          </div>
          <button
            type="button"
            onClick={handleRefresh}
            disabled={loading || typeof onRefresh !== 'function'}
            className="app-icon-button !h-8 !w-8 disabled:cursor-wait disabled:opacity-50"
            title="刷新会话列表"
            aria-label="刷新会话列表"
          >
            <svg
              className={loading ? 'animate-spin' : ''}
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
              aria-hidden="true"
            >
              <path d="M20 7h-5V2" />
              <path d="M20 2v5h-5" />
              <path d="M20 7a8 8 0 1 0 1 7" />
            </svg>
          </button>
        </div>

        <div className="relative">
          <svg
            className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400"
            width="15"
            height="15"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
          >
            <circle cx="11" cy="11" r="8" />
            <path d="M21 21l-4.35-4.35" />
          </svg>
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="搜索联系人"
            aria-label="搜索联系人"
            className="h-9 w-full rounded-xl border border-slate-200/80 bg-white pl-9 pr-3 text-sm text-slate-700 shadow-sm shadow-slate-100 outline-none transition placeholder:text-slate-400 hover:border-slate-300 focus:border-emerald-500 focus:ring-2 focus:ring-emerald-100"
          />
        </div>
      </div>

      {/* 聊天列表 */}
      <div className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden px-2 pb-3 sm:px-3">
        {loading && (
          <div className="flex items-center justify-center gap-2 py-3 text-xs text-slate-400">
            <div className="h-4 w-4 animate-spin rounded-full border-2 border-emerald-500 border-t-transparent" />
            正在同步会话
          </div>
        )}

        {filtered.length === 0 ? (
          <div className="mx-2 mt-8 rounded-2xl border border-dashed border-slate-200 bg-white/70 px-4 py-10 text-center text-slate-400">
            <div className="mx-auto mb-3 flex h-10 w-10 items-center justify-center rounded-xl bg-slate-100 text-slate-400">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7">
                <path d="M21 15a4 4 0 0 1-4 4H7l-4 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4Z" />
                <path d="m9 9 6 6M15 9l-6 6" />
              </svg>
            </div>
            <p className="text-sm font-medium text-slate-500">
              {search.trim() ? '没有匹配的聊天' : '暂无聊天记录'}
            </p>
            {!search.trim() && (
              <button
                type="button"
                onClick={handleRefresh}
                disabled={loading || typeof onRefresh !== 'function'}
                className="mt-2 text-xs font-medium text-emerald-600 hover:text-emerald-700"
              >
                点击刷新
              </button>
            )}
          </div>
        ) : (
          <ul className="w-full space-y-1">
            {filtered.map((chat) => (
              <li key={chat.talker}>
                <button
                type="button"
                onClick={() => onSelect(chat)}
                disabled={typeof onSelect !== 'function'}
                aria-current={activeTalker === chat.talker ? 'page' : undefined}
                className={`group relative flex w-full cursor-pointer items-center gap-3 rounded-xl px-3 py-2.5 text-left transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-200 disabled:cursor-default ${
                  activeTalker === chat.talker
                    ? 'bg-white shadow-sm ring-1 ring-slate-200/70'
                    : 'hover:bg-white/80'
                }`}
              >
                {activeTalker === chat.talker && (
                  <span className="absolute inset-y-3 left-0 w-0.5 rounded-full bg-emerald-500" />
                )}
                {/* 头像 */}
                <Avatar
                  src={chat.avatar_url}
                  name={chat.display_name}
                  isGroup={chat.is_group}
                  className="h-11 w-11"
                />

                {/* 信息 */}
                <div className="flex-1 min-w-0">
                  <div className="flex items-center justify-between">
                    <h3 className="truncate text-sm font-medium text-slate-800">
                      {chat.display_name}
                      {chat.is_group && (
                        <span className="ml-1 rounded bg-slate-100 px-1 py-0.5 text-[9px] font-medium text-slate-400">
                          群聊
                        </span>
                      )}
                    </h3>
                    <span className="ml-2 flex-shrink-0 text-[10px] text-slate-400">
                      {chat.last_time_str}
                    </span>
                  </div>
                  <div className="flex items-center mt-0.5 min-w-0">
                    <p className="min-w-0 flex-1 truncate pr-3 text-xs text-slate-500">
                      {chat.last_message || '暂无消息'}
                    </p>
                    {chat.message_count > 0 && (
                      <span className="ml-2 flex-shrink-0 rounded-full bg-slate-100 px-1.5 py-0.5 text-[9px] leading-none text-slate-400">
                        {chat.message_count >= 10000
                          ? `${(chat.message_count / 10000).toFixed(1)}万`
                          : chat.message_count}
                      </span>
                    )}
                  </div>
                </div>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* 底部统计 */}
      {chats.length > 0 && (
        <div className="border-t border-slate-200/70 bg-white/70 px-4 py-2 text-center text-[10px] text-slate-400">
          已加载 {chats.length.toLocaleString()} 个聊天
          {search && filtered.length !== chats.length && (
            <span> (已过滤)</span>
          )}
        </div>
      )}
    </div>
  );
}
