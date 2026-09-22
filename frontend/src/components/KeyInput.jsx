import { useState } from 'react';
import api from '../api';

/** Fixed-size desktop connection card for validating the local database key. */
export default function KeyInput({ onSubmit, error, status }) {
  const [key, setKey] = useState('');
  const [loading, setLoading] = useState(false);
  const [localError, setLocalError] = useState('');
  const [extracting, setExtracting] = useState(false);
  const [extractProgress, setExtractProgress] = useState('');

  const handleSubmit = async (event) => {
    event.preventDefault();
    const trimmed = key.trim().replace(/\s/g, '');
    if (!trimmed) {
      setLocalError('请输入密钥');
      return;
    }
    if (!/^[a-fA-F0-9]{64}$/.test(trimmed)) {
      setLocalError('密钥格式不正确，需要 64 位十六进制字符');
      return;
    }

    setLoading(true);
    setLocalError('');
    try {
      await onSubmit(trimmed.toLowerCase());
    } catch (submitError) {
      setLocalError(submitError.message);
    } finally {
      setLoading(false);
    }
  };

  const handleExtract = async () => {
    const confirmed = window.confirm(
      '自动提取会关闭并重新启动电脑版微信。请先保存尚未发送的内容，然后继续。'
    );
    if (!confirmed) return;

    setExtracting(true);
    setExtractProgress('正在检查组件并重新启动微信…');
    setLocalError('');
    try {
      const response = await api.extractKey();
      // 后端已验证并保存密钥，响应不含密钥明文；直接进入软件即可。
      if (response.success) {
        setExtractProgress('密钥已提取并通过验证，正在进入软件…');
        await onSubmit();
      } else {
        setLocalError(response.message || '提取失败');
      }
    } catch (extractError) {
      setLocalError(extractError.message || '自动提取失败，请手动输入密钥');
    } finally {
      setExtracting(false);
      setExtractProgress('');
    }
  };

  const config = status?.config || {};
  const databaseFiles = Array.isArray(config.msg_dbs) ? config.msg_dbs : [];
  const accountReady = Boolean(config.wxid);
  const databaseReady = Boolean(config.msg_dir && databaseFiles.length > 0);

  return (
    <div className="min-h-0 flex-1 overflow-y-auto bg-[radial-gradient(circle_at_50%_0%,rgba(16,185,129,0.08),transparent_45%)] px-5 py-6 sm:px-8 sm:py-8">
      <section className="mx-auto w-full max-w-4xl overflow-hidden rounded-[24px] border border-slate-200/80 bg-white shadow-[0_24px_70px_-36px_rgba(15,23,42,0.38)]">
        <div className="flex items-start justify-between gap-5 border-b border-slate-100 px-6 py-5 sm:px-7">
          <div className="flex min-w-0 items-center gap-4">
            <div className="flex h-11 w-11 flex-none items-center justify-center rounded-2xl bg-emerald-50 text-emerald-600 ring-1 ring-emerald-100">
              <svg width="23" height="23" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                <rect x="4" y="10" width="16" height="10" rx="2" />
                <path d="M8 10V7a4 4 0 0 1 8 0v3M12 14v2" />
              </svg>
            </div>
            <div className="min-w-0">
              <h2 className="text-lg font-semibold tracking-tight text-slate-900">验证数据库密钥</h2>
              <p className="mt-1 text-sm text-slate-500">密钥只用于本机解密，不会上传到任何服务器</p>
            </div>
          </div>
          <span className="flex flex-none items-center gap-1.5 rounded-full bg-emerald-50 px-3 py-1 text-xs font-medium text-emerald-700 ring-1 ring-emerald-100">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
            本机处理
          </span>
        </div>

        <div className="grid lg:grid-cols-[1.05fr_0.95fr]">
          <div className="border-b border-slate-100 p-6 sm:p-7 lg:border-b-0 lg:border-r">
            <div className="mb-4 flex items-center justify-between">
              <h3 className="text-sm font-semibold text-slate-800">已检测到的本机数据</h3>
              <span className={`text-xs font-medium ${accountReady && databaseReady ? 'text-emerald-600' : 'text-amber-600'}`}>
                {accountReady && databaseReady ? '可以继续' : '等待检测'}
              </span>
            </div>

            <div className="space-y-3">
              <div className="rounded-xl border border-slate-200/80 bg-slate-50/70 p-3.5">
                <div className="flex items-center gap-2 text-xs font-medium text-slate-500">
                  <span className={`h-2 w-2 rounded-full ${accountReady ? 'bg-emerald-500' : 'bg-amber-400'}`} />
                  当前微信账号
                </div>
                <p className="mt-1.5 truncate text-sm font-medium text-slate-800" title={config.wxid || ''}>
                  {config.wxid || '尚未检测到账号'}
                </p>
              </div>

              <div className="rounded-xl border border-slate-200/80 bg-slate-50/70 p-3.5">
                <div className="flex items-center gap-2 text-xs font-medium text-slate-500">
                  <span className={`h-2 w-2 rounded-full ${databaseReady ? 'bg-emerald-500' : 'bg-amber-400'}`} />
                  消息数据库
                </div>
                <p className="mt-1.5 truncate text-sm text-slate-700" title={config.msg_dir || ''}>
                  {config.msg_dir || '尚未找到数据库目录'}
                </p>
                {databaseFiles.length > 0 && (
                  <div className="mt-2.5 flex flex-wrap gap-1.5">
                    {databaseFiles.slice(0, 3).map((name) => (
                      <span key={name} className="rounded-md bg-white px-2 py-1 font-mono text-[11px] text-slate-500 ring-1 ring-slate-200">
                        {name}
                      </span>
                    ))}
                    {databaseFiles.length > 3 && (
                      <span className="rounded-md bg-white px-2 py-1 text-[11px] text-slate-500 ring-1 ring-slate-200">
                        +{databaseFiles.length - 3}
                      </span>
                    )}
                  </div>
                )}
              </div>
            </div>

            <details className="group mt-4 rounded-xl border border-slate-200/80 bg-white px-4 py-3">
              <summary className="cursor-pointer list-none text-xs font-medium text-slate-600">
                自动提取前需要知道什么？
                <span className="float-right text-slate-400 transition group-open:rotate-180">⌄</span>
              </summary>
              <p className="mt-2 text-xs leading-5 text-slate-500">
                自动提取会关闭并重新启动电脑版微信，请先保存未发送内容。也可以直接粘贴已有的 64 位密钥。
              </p>
            </details>
          </div>

          <div className="p-6 sm:p-7">
            <h3 className="text-sm font-semibold text-slate-800">数据库解密密钥</h3>
            <p className="mt-1 text-xs leading-5 text-slate-400">输入 64 位十六进制密钥，或让软件自动提取并验证。</p>

            {(error || localError) && (
              <div className="mt-4 rounded-xl border border-red-200 bg-red-50 px-3.5 py-3 text-sm leading-5 text-red-700">
                {error || localError}
              </div>
            )}

            <form onSubmit={handleSubmit} className="mt-4">
              <textarea
                value={key}
                onChange={(event) => setKey(event.target.value)}
                placeholder="粘贴 64 位十六进制密钥"
                className="w-full resize-none rounded-xl border border-slate-200 bg-slate-50/60 px-3.5 py-3 font-mono text-sm text-slate-700 outline-none transition focus:border-emerald-500 focus:bg-white focus:ring-4 focus:ring-emerald-500/10 disabled:opacity-60"
                rows={3}
                disabled={loading || extracting}
                spellCheck={false}
              />

              <button
                type="submit"
                disabled={loading || extracting}
                className="mt-3 w-full rounded-xl bg-emerald-600 py-2.5 text-sm font-semibold text-white shadow-sm shadow-emerald-600/20 transition hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {loading ? '正在验证…' : '连接并进入软件'}
              </button>
              <button
                type="button"
                onClick={handleExtract}
                disabled={extracting || loading || !accountReady}
                className="mt-2.5 flex w-full items-center justify-center gap-2 rounded-xl border border-slate-200 bg-white py-2.5 text-sm font-semibold text-slate-700 transition hover:border-slate-300 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {extracting ? (
                  <>
                    <span className="h-4 w-4 animate-spin rounded-full border-2 border-emerald-500 border-t-transparent" />
                    正在自动提取…
                  </>
                ) : (
                  '自动提取密钥'
                )}
              </button>
              {extractProgress && (
                <p className="mt-2.5 text-center text-xs text-emerald-600">{extractProgress}</p>
              )}
            </form>
          </div>
        </div>
      </section>
    </div>
  );
}
