import { useMemo, useState } from 'react';
import Avatar from './Avatar';

function text(value, fallback = '') {
  const normalized = String(value ?? '').trim();
  return normalized || fallback;
}

function Capability({ available, children }) {
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium ${
      available
        ? 'border-emerald-100 bg-emerald-50 text-emerald-700'
        : 'border-slate-200 bg-slate-50 text-slate-400'
    }`}>
      <span className={`h-1.5 w-1.5 rounded-full ${available ? 'bg-emerald-500' : 'bg-slate-300'}`} />
      {children}
    </span>
  );
}

function DetailRow({ label, value, mono = false }) {
  return (
    <div className="grid grid-cols-[6rem_minmax(0,1fr)] gap-4 border-b border-slate-100 py-3.5 last:border-b-0 sm:grid-cols-[8rem_minmax(0,1fr)]">
      <dt className="text-sm text-slate-400">{label}</dt>
      <dd className={`min-w-0 break-all text-sm text-slate-700 ${mono ? 'font-mono text-xs leading-5' : ''}`}>{value || '未记录'}</dd>
    </div>
  );
}

export default function AccountProfile({
  status,
  onSwitchAccount,
  switchError = '',
  onRefresh,
  onLogout,
}) {
  const [switching, setSwitching] = useState(false);
  const config = status?.config || {};
  const accounts = Array.isArray(config.accounts) ? config.accounts : [];
  const activeIndex = Number.isInteger(Number(config.active_index)) ? Number(config.active_index) : 0;
  const activeAccount = accounts[activeIndex] || {};
  const displayName = text(config.display_name || activeAccount.display_name || config.alias || activeAccount.alias || config.wxid, '当前账号');
  const alias = text(config.alias || activeAccount.alias);
  const wxid = text(config.wxid || activeAccount.wxid);

  const messageDatabases = useMemo(() => {
    const values = Array.isArray(config.msg_dbs) && config.msg_dbs.length > 0
      ? config.msg_dbs
      : activeAccount.msg_dbs;
    return Array.isArray(values) ? values : [];
  }, [activeAccount.msg_dbs, config.msg_dbs]);

  const handleAccountChange = async (event) => {
    const nextIndex = Number(event.target.value);
    if (!Number.isInteger(nextIndex) || nextIndex === activeIndex || !onSwitchAccount) return;
    setSwitching(true);
    try {
      await onSwitchAccount(nextIndex);
    } finally {
      setSwitching(false);
    }
  };

  const handleLogout = () => {
    if (!onLogout) return;
    const confirmed = window.confirm('退出后会清除当前账号已保存的数据库密钥，需要重新验证才能继续浏览。确定退出吗？');
    if (confirmed) onLogout();
  };

  return (
    <div className="min-h-0 flex-1 overflow-y-auto bg-[#f5f7f6] px-4 py-5 sm:px-7 sm:py-8">
      <div className="mx-auto w-full max-w-5xl space-y-5">
        <section className="overflow-hidden rounded-2xl border border-slate-200/80 bg-white shadow-sm shadow-slate-200/40">
          <div className="flex flex-col gap-5 px-5 py-6 sm:flex-row sm:items-center sm:px-8 sm:py-8">
            <Avatar
              src={config.avatar_url}
              name={displayName}
              className="h-24 w-24 rounded-2xl text-2xl ring-1 ring-slate-200"
            />
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2.5">
                <h2 className="truncate text-2xl font-semibold tracking-tight text-slate-900">{displayName}</h2>
                <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-100 bg-emerald-50 px-2.5 py-1 text-xs font-medium text-emerald-700">
                  <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
                  当前账号
                </span>
              </div>
              <p className="mt-2 text-sm text-slate-500">{alias ? `微信号：${alias}` : wxid}</p>
              <div className="mt-4 flex flex-wrap gap-2">
                <Capability available={Boolean(config.has_key)}>聊天数据</Capability>
                <Capability available={Boolean(config.supports_chat_images)}>聊天图片</Capability>
                <Capability available={Boolean(config.supports_moments)}>朋友圈</Capability>
              </div>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2 border-t border-slate-100 bg-slate-50/70 px-5 py-4 sm:px-8">
            <button
              type="button"
              onClick={onRefresh}
              disabled={!onRefresh || switching}
              className="inline-flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-4 py-2 text-sm font-medium text-slate-600 transition hover:border-slate-300 hover:bg-slate-50 disabled:opacity-50"
            >
              <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                <path d="M20 7v5h-5" />
                <path d="M4.9 17A8 8 0 1 0 6 6.3L4 8" />
              </svg>
              刷新账号数据
            </button>
            <button
              type="button"
              onClick={handleLogout}
              disabled={!onLogout || switching}
              className="inline-flex items-center gap-2 rounded-xl border border-red-200 bg-white px-4 py-2 text-sm font-medium text-red-600 transition hover:bg-red-50 disabled:opacity-50 sm:ml-auto"
            >
              <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                <path d="M10 5H6a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h4" />
                <path d="M14 8l4 4-4 4m4-4H9" />
              </svg>
              清除密钥并退出
            </button>
          </div>
        </section>

        {accounts.length > 1 && (
          <section className="rounded-2xl border border-slate-200/80 bg-white p-5 shadow-sm shadow-slate-200/30 sm:p-6">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <h3 className="text-sm font-semibold text-slate-900">切换微信账号</h3>
                <p className="mt-1 text-xs leading-5 text-slate-400">切换后会重新读取对应账号的聊天、联系人和模型设置状态。</p>
              </div>
              <label className="relative min-w-0 sm:w-80">
                <span className="sr-only">选择微信账号</span>
                <select
                  value={activeIndex}
                  onChange={handleAccountChange}
                  disabled={switching}
                  className="h-10 w-full appearance-none rounded-xl border border-slate-200 bg-white py-2 pl-3 pr-9 text-sm text-slate-700 outline-none transition focus:border-emerald-500 focus:ring-2 focus:ring-emerald-100 disabled:bg-slate-100"
                >
                  {accounts.map((account, index) => {
                    const label = text(account.display_name || account.alias || account.wxid, `账号 ${index + 1}`);
                    return (
                      <option key={account.wxid || index} value={index}>
                        {label}{account.last_active ? ` · ${account.last_active}` : ''}{index === activeIndex ? ' ✓' : ''}
                      </option>
                    );
                  })}
                </select>
                <svg className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-slate-400" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true"><path d="m7 10 5 5 5-5" /></svg>
              </label>
            </div>
            {switching && (
              <div className="mt-3 flex items-center gap-2 text-xs text-emerald-700" role="status">
                <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-emerald-500 border-t-transparent" />
                正在切换并重新读取账号数据…
              </div>
            )}
            {!switching && switchError && (
              <div
                role="alert"
                className="mt-3 rounded-xl border border-red-200 bg-red-50 px-3 py-2 text-sm leading-5 text-red-700"
              >
                {switchError}
              </div>
            )}
          </section>
        )}

        <section className="rounded-2xl border border-slate-200/80 bg-white px-5 py-2 shadow-sm shadow-slate-200/30 sm:px-7">
          <div className="border-b border-slate-100 py-4">
            <h3 className="text-sm font-semibold text-slate-900">账号与本地数据</h3>
            <p className="mt-1 text-xs text-slate-400">这些信息只来自当前电脑上的微信 4.x 数据目录。</p>
          </div>
          <dl>
            <DetailRow label="昵称" value={displayName} />
            <DetailRow label="微信号" value={alias || wxid} />
            {alias && wxid && alias !== wxid && <DetailRow label="账号标识" value={wxid} mono />}
            <DetailRow label="微信版本" value={text(config.wechat_version, '4.x')} />
            <DetailRow label="最近数据" value={text(activeAccount.last_active, '时间未知')} />
            <DetailRow
              label="消息数据库"
              value={`${Number(activeAccount.db_count ?? messageDatabases.length) || 0} 个分片${messageDatabases.length > 0 ? `（${messageDatabases.join('、')}）` : ''}`}
            />
            <DetailRow label="数据目录" value={text(config.msg_dir || activeAccount.msg_dir, '未检测到')} mono />
          </dl>
        </section>

        <p className="px-1 pb-3 text-xs leading-5 text-slate-400">
          隐私说明：账号资料、联系人和聊天数据默认只在本机读取。只有你主动使用图片识别、语音转写或 AI 分析时，相应范围内的数据才会发送到你配置的模型服务。
        </p>
      </div>
    </div>
  );
}
