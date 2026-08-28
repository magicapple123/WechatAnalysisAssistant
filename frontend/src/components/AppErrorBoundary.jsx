import { Component } from 'react';

/** Last-resort UI for render and lazy-chunk failures. */
export default class AppErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { failed: false };
  }

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error, info) {
    // Keep the technical context in developer tools while showing users a
    // concise recovery action that does not expose local data.
    console.error('[UI Error]', error, info);
  }

  render() {
    if (!this.state.failed) return this.props.children;

    return (
      <main className="app-viewport flex items-center justify-center bg-slate-100 px-5 text-slate-800">
        <section
          role="alert"
          className="w-full max-w-md rounded-2xl border border-slate-200 bg-white p-7 text-center shadow-sm"
        >
          <h1 className="text-lg font-semibold text-slate-900">界面加载失败</h1>
          <p className="mt-2 text-sm leading-6 text-slate-500">
            本地数据不会因此丢失。请重新加载界面；如果问题持续，请重启应用。
          </p>
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="mt-5 rounded-xl bg-wechat-green px-5 py-2.5 text-sm font-medium text-white hover:bg-wechat-green-dark"
          >
            重新加载
          </button>
        </section>
      </main>
    );
  }
}
