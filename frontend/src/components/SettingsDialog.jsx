import { useEffect, useRef, useState } from 'react';
import api from '../api';
import {
  checkForDesktopUpdates,
  downloadDesktopUpdate,
  getDesktopRuntimeInfo,
  getDesktopUpdateStatus,
  installDesktopUpdate,
  isDesktopApp,
  onDesktopUpdateStatus,
} from '../desktop';
import {
  getInterfacePreset,
  groupedInterfacePresets,
  interfaceRequiresApiKey,
  resolveInterfacePreset,
} from '../modelInterfaces';
import useDialogFocus from '../hooks/useDialogFocus';

const DEFAULT_CUSTOM_REQUEST_TEMPLATE = JSON.stringify({
  model: '{{model}}',
  messages: [{
    role: 'user',
    content: [
      { type: 'text', text: '{{prompt}}' },
      { type: 'image_url', image_url: { url: '{{image_data_url}}', detail: '{{detail}}' } },
    ],
  }],
}, null, 2);

const DEFAULT_VISION = {
  interface_preset: 'openai',
  base_url: 'https://api.openai.com/v1',
  model: 'gpt-5.4-mini',
  image_detail: 'low',
  max_images_per_task: 50,
};

const DEFAULT_TRANSCRIPTION = {
  interface_preset: 'openai',
  base_url: 'https://api.openai.com/v1',
  model: 'whisper-1',
  language: 'auto',
  max_voices_per_task: 50,
};

const DEFAULT_ANALYSIS_REQUEST_TEMPLATE = JSON.stringify({
  model: '{{model}}',
  messages: [
    { role: 'system', content: '{{system_prompt}}' },
    { role: 'user', content: '{{prompt}}' },
  ],
  temperature: '{{temperature}}',
  max_tokens: '{{max_output_tokens}}',
}, null, 2);

const DEFAULT_ANALYSIS = {
  interface_preset: 'openai',
  base_url: 'https://api.openai.com/v1',
  model: 'gpt-5.4-mini',
  timeout_seconds: 90,
};

const DEFAULT_ANALYSIS_PRESETS = [
  {
    id: 'quick-summary',
    name: '快速梳理',
    strength: 'quick',
    detail: 'brief',
    requirements: '快速概括主要话题、关键结论和需要跟进的事项。',
  },
  {
    id: 'balanced-review',
    name: '标准分析',
    strength: 'balanced',
    detail: 'standard',
    requirements: '按时间和主题梳理对话，识别重要观点、情绪变化、共识与分歧。',
  },
  {
    id: 'deep-insight',
    name: '深度洞察',
    strength: 'deep',
    detail: 'detailed',
    requirements: '深入分析关系、语境、潜在动机和长期趋势，并用原始内容中的证据支撑结论。',
  },
];

const ANALYSIS_STRENGTHS = {
  quick: '快速',
  balanced: '均衡',
  deep: '深入',
};

const ANALYSIS_DETAILS = {
  brief: '简洁',
  standard: '标准',
  detailed: '详细',
};

function normalizeAnalysisPresets(value) {
  if (!Array.isArray(value) || value.length === 0) {
    return DEFAULT_ANALYSIS_PRESETS.map((preset) => ({ ...preset }));
  }

  const usedIds = new Set();
  return value.map((preset, index) => {
    let id = String(preset?.id || '').trim() || `preset-${index + 1}`;
    while (usedIds.has(id)) id = `${id}-${index + 1}`;
    usedIds.add(id);
    const strength = Object.prototype.hasOwnProperty.call(
      ANALYSIS_STRENGTHS, preset?.strength
    )
      ? preset.strength
      : 'balanced';
    const detail = Object.prototype.hasOwnProperty.call(ANALYSIS_DETAILS, preset?.detail)
      ? preset.detail
      : 'standard';
    return {
      id,
      name: String(preset?.name || `分析预设 ${index + 1}`),
      strength,
      detail,
      requirements: String(preset?.requirements || ''),
    };
  });
}

function Toggle({ checked, onChange, label, description }) {
  return (
    <label className="flex items-center justify-between gap-4 cursor-pointer select-none">
      <span>
        <span className="block text-sm font-medium text-gray-700">{label}</span>
        {description && <span className="block text-xs text-gray-400 mt-0.5">{description}</span>}
      </span>
      <span className="relative inline-flex flex-shrink-0">
        <input
          type="checkbox"
          className="sr-only peer"
          checked={checked}
          onChange={(event) => onChange(event.target.checked)}
        />
        <span className="w-10 h-6 rounded-full bg-gray-300 peer-checked:bg-wechat-green transition-colors" />
        <span className="absolute left-1 top-1 w-4 h-4 rounded-full bg-white shadow transition-transform peer-checked:translate-x-4" />
      </span>
    </label>
  );
}

const SETTINGS_SECTIONS = [
  { id: 'images', icon: '图', label: '聊天图片', description: '显示、高清与解密' },
  { id: 'vision', icon: '视', label: '图片识别', description: '视觉模型接口' },
  { id: 'transcription', icon: '音', label: '语音转写', description: '语音识别接口' },
  { id: 'analysis', icon: '析', label: '内容分析', description: '聊天与朋友圈分析' },
  { id: 'presets', icon: '预', label: '分析预设', description: '强度与输出要求' },
  { id: 'export', icon: '出', label: '导出', description: '保存位置与全部导出' },
  { id: 'about', icon: 'i', label: '关于', description: '项目信息与使用帮助' },
];

const INPUT_CLASS = 'w-full rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm text-slate-800 shadow-sm outline-none transition focus:border-emerald-500 focus:ring-2 focus:ring-emerald-500/10';

const DEFAULT_ABOUT = {
  project_name: '微信解析助手',
  version: '1.0.0',
  description: '本地优先的微信 4.x 聊天记录与朋友圈解析、预览、导出和 AI 分析工具。',
  tutorial_url: '',
  qq_group: '',
  author: '',
  contact: '',
};

function normalizeModelBaseUrl(value) {
  return String(value || '').trim().replace(/\/+$/, '');
}

function PanelTitle({ title, description, status }) {
  return (
    <div className="mb-5 flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
      <div>
        <h3 className="text-lg font-semibold tracking-tight text-slate-900">{title}</h3>
        <p className="mt-1 text-sm leading-5 text-slate-500">{description}</p>
      </div>
      {status && (
        <span className="self-start rounded-full bg-slate-100 px-3 py-1 text-xs text-slate-600">
          {status}
        </span>
      )}
    </div>
  );
}

function SettingsCard({ title, description, children, className = '' }) {
  return (
    <section className={`rounded-xl border border-slate-200 bg-white p-4 shadow-sm sm:p-5 ${className}`}>
      {(title || description) && (
        <div className="mb-4">
          {title && <h4 className="text-sm font-semibold text-slate-800">{title}</h4>}
          {description && <p className="mt-1 text-xs leading-5 text-slate-500">{description}</p>}
        </div>
      )}
      {children}
    </section>
  );
}

function AdvancedSection({ open, onToggle, title = '高级参数', description, children }) {
  return (
    <section className="overflow-hidden rounded-xl border border-gray-200 bg-white">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-4 px-4 py-3 text-left hover:bg-gray-50 sm:px-5"
      >
        <span>
          <span className="block text-sm font-semibold text-gray-800">{title}</span>
          {description && (
            <span className="mt-0.5 block text-xs leading-5 text-gray-500">{description}</span>
          )}
        </span>
        <span className={`text-sm text-gray-400 transition-transform ${open ? 'rotate-180' : ''}`}>
          ▼
        </span>
      </button>
      {open && <div className="border-t border-gray-200 p-4 sm:p-5">{children}</div>}
    </section>
  );
}

function InterfacePresetSelect({
  kind,
  id,
  value,
  onChange,
  customName = '',
}) {
  const groups = groupedInterfacePresets(kind);
  const preset = getInterfacePreset(kind, value);
  return (
    <div>
      <label htmlFor={id} className="mb-1.5 block text-sm font-medium text-gray-700">
        接口服务
      </label>
      <select
        id={id}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className={INPUT_CLASS}
      >
        {groups.map(({ group, presets }) => (
          <optgroup key={group} label={group}>
            {presets.map((item) => (
              <option key={item.id} value={item.id}>
                {(item.id === 'custom' || item.id === 'custom_multipart') && customName.trim()
                  ? `自定义：${customName.trim()}`
                  : item.label}
              </option>
            ))}
          </optgroup>
        ))}
      </select>
      {preset && (
        <div className="mt-2 rounded-lg bg-gray-50 px-3 py-2">
          <div className="flex flex-wrap items-center gap-1.5 text-xs">
            <span className="font-medium text-gray-700">{preset.protocolLabel}</span>
            {(preset.capabilities || []).map((capability) => (
              <span
                key={capability}
                className="rounded-full bg-white px-2 py-0.5 text-gray-500 ring-1 ring-gray-200"
              >
                {capability}
              </span>
            ))}
          </div>
          {preset.description && (
            <p className="mt-1 text-xs leading-5 text-gray-500">{preset.description}</p>
          )}
        </div>
      )}
    </div>
  );
}

function SecretField({
  id,
  label = 'API Key',
  value,
  onChange,
  visible,
  onToggleVisible,
  revealing,
  configured,
  hint,
  clearPending,
  onToggleClear,
  canClear,
  placeholder,
  optional = false,
  optionalDescription = '',
}) {
  const status = clearPending
    ? '保存后清除'
    : configured
      ? `已配置${hint ? `（${hint}）` : ''}`
      : optional
        ? '可留空'
        : '未配置';
  return (
    <div>
      <div className="mb-1.5 flex items-center justify-between gap-3">
        <label htmlFor={id} className="text-sm font-medium text-gray-700">{label}</label>
        <span
          className={`text-xs ${
            clearPending
              ? 'text-amber-600'
              : configured
                ? 'text-green-600'
                : 'text-gray-400'
          }`}
        >
          {status}
        </span>
      </div>
      <div className="flex flex-col gap-2 sm:flex-row">
        <div className="relative min-w-0 flex-1">
          <input
            id={id}
            type={visible ? 'text' : 'password'}
            value={value}
            onChange={onChange}
            disabled={clearPending}
            autoComplete="new-password"
            placeholder={placeholder}
            className={`${INPUT_CLASS} pr-14 font-mono disabled:bg-gray-100`}
          />
          <button
            type="button"
            onClick={onToggleVisible}
            disabled={revealing}
            aria-label={visible ? `隐藏${label}` : `显示${label}`}
            aria-pressed={visible}
            className="absolute right-2 top-1/2 -translate-y-1/2 text-xs text-gray-400 hover:text-gray-600 disabled:cursor-wait disabled:opacity-60"
          >
            {revealing ? '读取中' : (visible ? '隐藏' : '显示')}
          </button>
        </div>
        {canClear && (
          <button
            type="button"
            onClick={onToggleClear}
            className={`self-start whitespace-nowrap rounded-lg border px-3 py-2 text-xs transition-colors sm:self-auto ${
              clearPending
                ? 'border-gray-300 bg-white text-gray-600'
                : 'border-red-200 bg-red-50 text-red-600 hover:bg-red-100'
            }`}
          >
            {clearPending ? '撤销清除' : '清除密钥'}
          </button>
        )}
      </div>
      {optional && optionalDescription && (
        <p className="mt-2 text-xs leading-5 text-green-700">{optionalDescription}</p>
      )}
    </div>
  );
}

export default function SettingsDialog({
  settings,
  onSaveSettings,
  onExtractImageKey,
  onExportAll,
  onClose,
}) {
  const [exportDir, setExportDir] = useState('');
  const [about, setAbout] = useState(DEFAULT_ABOUT);
  const [showChatImages, setShowChatImages] = useState(false);
  const [chatImageQuality, setChatImageQuality] = useState('smart');
  const [hdAutomationTimeout, setHdAutomationTimeout] = useState(10);
  const [hdAutomationMinDwell, setHdAutomationMinDwell] = useState(0.5);

  const [visionInterfacePreset, setVisionInterfacePreset] = useState(
    DEFAULT_VISION.interface_preset
  );
  const [customInterfaceName, setCustomInterfaceName] = useState('');
  const [customProtocol, setCustomProtocol] = useState('openai_compatible');
  const [customApiKeyHeader, setCustomApiKeyHeader] = useState('Authorization');
  const [customApiKeyPrefix, setCustomApiKeyPrefix] = useState('Bearer ');
  const [customExtraHeaders, setCustomExtraHeaders] = useState('{}');
  const [customRequestTemplate, setCustomRequestTemplate] = useState(DEFAULT_CUSTOM_REQUEST_TEMPLATE);
  const [customResponsePath, setCustomResponsePath] = useState('choices.0.message.content');
  const [visionBaseUrl, setVisionBaseUrl] = useState(DEFAULT_VISION.base_url);
  const [visionModel, setVisionModel] = useState(DEFAULT_VISION.model);
  const [visionApiKey, setVisionApiKey] = useState('');
  const [showVisionApiKey, setShowVisionApiKey] = useState(false);
  const [visionApiKeyLoaded, setVisionApiKeyLoaded] = useState(false);
  const [revealingVisionApiKey, setRevealingVisionApiKey] = useState(false);
  const [clearVisionApiKey, setClearVisionApiKey] = useState(false);
  const [imageDetail, setImageDetail] = useState(DEFAULT_VISION.image_detail);
  const [maxImagesPerTask, setMaxImagesPerTask] = useState(DEFAULT_VISION.max_images_per_task);

  const [transcriptionInterfacePreset, setTranscriptionInterfacePreset] = useState(
    DEFAULT_TRANSCRIPTION.interface_preset
  );
  const [transcriptionBaseUrl, setTranscriptionBaseUrl] = useState(DEFAULT_TRANSCRIPTION.base_url);
  const [transcriptionModel, setTranscriptionModel] = useState(DEFAULT_TRANSCRIPTION.model);
  const [transcriptionLanguage, setTranscriptionLanguage] = useState(DEFAULT_TRANSCRIPTION.language);
  const [maxVoicesPerTask, setMaxVoicesPerTask] = useState(DEFAULT_TRANSCRIPTION.max_voices_per_task);
  const [transcriptionApiKey, setTranscriptionApiKey] = useState('');
  const [showTranscriptionApiKey, setShowTranscriptionApiKey] = useState(false);
  const [transcriptionApiKeyLoaded, setTranscriptionApiKeyLoaded] = useState(false);
  const [revealingTranscriptionApiKey, setRevealingTranscriptionApiKey] = useState(false);
  const [clearTranscriptionApiKey, setClearTranscriptionApiKey] = useState(false);
  const [transcriptionCustomName, setTranscriptionCustomName] = useState('');
  const [transcriptionApiKeyHeader, setTranscriptionApiKeyHeader] = useState('Authorization');
  const [transcriptionApiKeyPrefix, setTranscriptionApiKeyPrefix] = useState('Bearer ');
  const [transcriptionExtraHeaders, setTranscriptionExtraHeaders] = useState('{}');
  const [transcriptionAudioField, setTranscriptionAudioField] = useState('file');
  const [transcriptionModelField, setTranscriptionModelField] = useState('model');
  const [transcriptionLanguageField, setTranscriptionLanguageField] = useState('language');
  const [transcriptionExtraFormFields, setTranscriptionExtraFormFields] = useState('{}');
  const [transcriptionResponsePath, setTranscriptionResponsePath] = useState('text');
  const [transcriptionFilename, setTranscriptionFilename] = useState('audio.wav');

  const [analysisInterfacePreset, setAnalysisInterfacePreset] = useState(
    DEFAULT_ANALYSIS.interface_preset
  );
  const [analysisCustomName, setAnalysisCustomName] = useState('');
  const [analysisCustomProtocol, setAnalysisCustomProtocol] = useState('openai_compatible');
  const [analysisApiKeyHeader, setAnalysisApiKeyHeader] = useState('Authorization');
  const [analysisApiKeyPrefix, setAnalysisApiKeyPrefix] = useState('Bearer ');
  const [analysisExtraHeaders, setAnalysisExtraHeaders] = useState('{}');
  const [analysisRequestTemplate, setAnalysisRequestTemplate] = useState(
    DEFAULT_ANALYSIS_REQUEST_TEMPLATE
  );
  const [analysisResponsePath, setAnalysisResponsePath] = useState(
    'choices.0.message.content'
  );
  const [analysisBaseUrl, setAnalysisBaseUrl] = useState(DEFAULT_ANALYSIS.base_url);
  const [analysisModel, setAnalysisModel] = useState(DEFAULT_ANALYSIS.model);
  const [analysisTimeout, setAnalysisTimeout] = useState(DEFAULT_ANALYSIS.timeout_seconds);
  const [analysisApiKey, setAnalysisApiKey] = useState('');
  const [showAnalysisApiKey, setShowAnalysisApiKey] = useState(false);
  const [analysisApiKeyLoaded, setAnalysisApiKeyLoaded] = useState(false);
  const [revealingAnalysisApiKey, setRevealingAnalysisApiKey] = useState(false);
  const [clearAnalysisApiKey, setClearAnalysisApiKey] = useState(false);
  const [analysisPresets, setAnalysisPresets] = useState(() => normalizeAnalysisPresets());
  const [activeAnalysisPresetId, setActiveAnalysisPresetId] = useState('quick-summary');

  const [imageAesKey, setImageAesKey] = useState('');
  const [showImageAesKey, setShowImageAesKey] = useState(false);
  const [clearImageAesKey, setClearImageAesKey] = useState(false);
  const [imageKeyExtracted, setImageKeyExtracted] = useState(false);
  const [extractingImageKey, setExtractingImageKey] = useState(false);
  const [revealingImageKey, setRevealingImageKey] = useState(false);
  const [xorKey, setXorKey] = useState('');

  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState('');
  const messageTimerRef = useRef(null);
  const [activeSection, setActiveSection] = useState('images');
  const [desktopRuntime, setDesktopRuntime] = useState(null);
  const [updateStatus, setUpdateStatus] = useState({ state: 'idle' });
  const [updateActionBusy, setUpdateActionBusy] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState({
    imageDecrypt: false,
    vision: false,
    transcription: false,
    analysis: false,
  });
  const visionRevealRequestRef = useRef(0);
  const transcriptionRevealRequestRef = useRef(0);
  const analysisRevealRequestRef = useRef(0);

  useEffect(() => {
    setExportDir(settings?.export?.directory || '');
    setAbout({ ...DEFAULT_ABOUT, ...(settings?.about || {}) });
    setShowChatImages(settings?.ui?.show_chat_images ?? false);
    setChatImageQuality(settings?.ui?.image_quality || 'smart');
    const savedHdTimeout = Number(settings?.ui?.hd_automation_timeout_seconds ?? 10);
    setHdAutomationTimeout(
      Number.isInteger(savedHdTimeout) && savedHdTimeout >= 5 && savedHdTimeout <= 120
        ? savedHdTimeout
        : 10
    );
    const savedHdMinDwell = Number(settings?.ui?.hd_automation_min_dwell_seconds ?? 0.5);
    setHdAutomationMinDwell(
      Number.isFinite(savedHdMinDwell) && savedHdMinDwell >= 0 && savedHdMinDwell <= 5
        ? savedHdMinDwell
        : 0.5
    );
    const savedVisionPreset = resolveInterfacePreset('vision', settings?.vision);
    const visionPresetDefaults = getInterfacePreset('vision', savedVisionPreset);
    setVisionInterfacePreset(savedVisionPreset);
    setCustomInterfaceName(settings?.vision?.custom_name || '');
    setCustomProtocol(settings?.vision?.custom_protocol || 'openai_compatible');
    setCustomApiKeyHeader(settings?.vision?.custom_api_key_header || 'Authorization');
    setCustomApiKeyPrefix(
      settings?.vision?.custom_api_key_prefix == null
        ? 'Bearer '
        : String(settings.vision.custom_api_key_prefix)
    );
    setCustomExtraHeaders(settings?.vision?.custom_extra_headers || '{}');
    setCustomRequestTemplate(
      settings?.vision?.custom_request_template || DEFAULT_CUSTOM_REQUEST_TEMPLATE
    );
    setCustomResponsePath(
      settings?.vision?.custom_response_path || 'choices.0.message.content'
    );
    setVisionBaseUrl(
      settings?.vision?.base_url ?? visionPresetDefaults?.baseUrl ?? DEFAULT_VISION.base_url
    );
    setVisionModel(
      settings?.vision?.model ?? visionPresetDefaults?.model ?? DEFAULT_VISION.model
    );
    setImageDetail(settings?.vision?.image_detail || DEFAULT_VISION.image_detail);
    setMaxImagesPerTask(
      settings?.vision?.max_images_per_task || DEFAULT_VISION.max_images_per_task
    );
    const savedTranscriptionPreset = resolveInterfacePreset(
      'transcription', settings?.transcription
    );
    const transcriptionPresetDefaults = getInterfacePreset(
      'transcription', savedTranscriptionPreset
    );
    setTranscriptionInterfacePreset(savedTranscriptionPreset);
    setTranscriptionBaseUrl(
      settings?.transcription?.base_url
      ?? transcriptionPresetDefaults?.baseUrl
      ?? DEFAULT_TRANSCRIPTION.base_url
    );
    setTranscriptionModel(
      settings?.transcription?.model
      ?? transcriptionPresetDefaults?.model
      ?? DEFAULT_TRANSCRIPTION.model
    );
    setTranscriptionLanguage(
      settings?.transcription?.language || DEFAULT_TRANSCRIPTION.language
    );
    setMaxVoicesPerTask(
      settings?.transcription?.max_voices_per_task || DEFAULT_TRANSCRIPTION.max_voices_per_task
    );
    setTranscriptionCustomName(settings?.transcription?.custom_name || '');
    setTranscriptionApiKeyHeader(
      settings?.transcription?.custom_api_key_header || 'Authorization'
    );
    setTranscriptionApiKeyPrefix(
      settings?.transcription?.custom_api_key_prefix == null
        ? 'Bearer '
        : String(settings.transcription.custom_api_key_prefix)
    );
    setTranscriptionExtraHeaders(settings?.transcription?.custom_extra_headers || '{}');
    setTranscriptionAudioField(settings?.transcription?.custom_audio_field || 'file');
    setTranscriptionModelField(
      settings?.transcription?.custom_model_field == null
        ? 'model'
        : String(settings.transcription.custom_model_field)
    );
    setTranscriptionLanguageField(
      settings?.transcription?.custom_language_field == null
        ? 'language'
        : String(settings.transcription.custom_language_field)
    );
    setTranscriptionExtraFormFields(
      settings?.transcription?.custom_extra_form_fields || '{}'
    );
    setTranscriptionResponsePath(settings?.transcription?.custom_response_path || 'text');
    setTranscriptionFilename(settings?.transcription?.custom_filename || 'audio.wav');

    const savedAnalysisPreset = resolveInterfacePreset('analysis', settings?.analysis);
    const analysisPresetDefaults = getInterfacePreset('analysis', savedAnalysisPreset);
    setAnalysisInterfacePreset(savedAnalysisPreset);
    setAnalysisCustomName(settings?.analysis?.custom_name || '');
    const savedAnalysisProtocol = settings?.analysis?.custom_protocol;
    setAnalysisCustomProtocol(
      ['openai_compatible', 'anthropic', 'gemini', 'custom_json'].includes(
        savedAnalysisProtocol
      )
        ? savedAnalysisProtocol
        : 'openai_compatible'
    );
    setAnalysisApiKeyHeader(
      settings?.analysis?.custom_api_key_header || 'Authorization'
    );
    setAnalysisApiKeyPrefix(
      settings?.analysis?.custom_api_key_prefix == null
        ? 'Bearer '
        : String(settings.analysis.custom_api_key_prefix)
    );
    setAnalysisExtraHeaders(settings?.analysis?.custom_extra_headers || '{}');
    setAnalysisRequestTemplate(
      settings?.analysis?.custom_request_template || DEFAULT_ANALYSIS_REQUEST_TEMPLATE
    );
    setAnalysisResponsePath(
      settings?.analysis?.custom_response_path || 'choices.0.message.content'
    );
    setAnalysisBaseUrl(
      settings?.analysis?.base_url
      ?? analysisPresetDefaults?.baseUrl
      ?? DEFAULT_ANALYSIS.base_url
    );
    setAnalysisModel(
      settings?.analysis?.model
      ?? analysisPresetDefaults?.model
      ?? DEFAULT_ANALYSIS.model
    );
    const savedAnalysisTimeout = Number(
      settings?.analysis?.timeout_seconds ?? DEFAULT_ANALYSIS.timeout_seconds
    );
    setAnalysisTimeout(
      Number.isInteger(savedAnalysisTimeout)
      && savedAnalysisTimeout >= 5
      && savedAnalysisTimeout <= 600
        ? savedAnalysisTimeout
        : DEFAULT_ANALYSIS.timeout_seconds
    );
    const normalizedPresets = normalizeAnalysisPresets(settings?.analysis?.presets);
    setAnalysisPresets(normalizedPresets);
    setActiveAnalysisPresetId((current) => (
      normalizedPresets.some((preset) => preset.id === current)
        ? current
        : normalizedPresets[0].id
    ));
    setXorKey(settings?.images?.xor_key == null ? '' : String(settings.images.xor_key));

    // 普通设置接口不回传密钥明文；仅显式点击"显示"时单独读取。
    visionRevealRequestRef.current += 1;
    transcriptionRevealRequestRef.current += 1;
    analysisRevealRequestRef.current += 1;
    setVisionApiKey('');
    setVisionApiKeyLoaded(false);
    setShowVisionApiKey(false);
    setRevealingVisionApiKey(false);
    setTranscriptionApiKey('');
    setTranscriptionApiKeyLoaded(false);
    setShowTranscriptionApiKey(false);
    setRevealingTranscriptionApiKey(false);
    setAnalysisApiKey('');
    setAnalysisApiKeyLoaded(false);
    setShowAnalysisApiKey(false);
    setRevealingAnalysisApiKey(false);
    setImageAesKey('');
    setShowImageAesKey(false);
    setClearVisionApiKey(false);
    setClearTranscriptionApiKey(false);
    setClearAnalysisApiKey(false);
    setClearImageAesKey(false);
    setImageKeyExtracted(false);
  }, [settings]);

  useEffect(() => () => {
    visionRevealRequestRef.current += 1;
    transcriptionRevealRequestRef.current += 1;
    analysisRevealRequestRef.current += 1;
    if (messageTimerRef.current) window.clearTimeout(messageTimerRef.current);
  }, []);

  useEffect(() => {
    if (!isDesktopApp()) return undefined;
    let active = true;
    const unsubscribe = onDesktopUpdateStatus((status) => {
      if (active && status && typeof status === 'object') {
        setUpdateStatus((current) => ({ ...current, ...status }));
      }
    });
    void getDesktopRuntimeInfo()
      .then((info) => {
        if (active && info) setDesktopRuntime(info);
      })
      .catch(() => {});
    void getDesktopUpdateStatus()
      .then((status) => {
        if (active && status && typeof status === 'object') {
          setUpdateStatus((current) => ({ ...current, ...status }));
        }
      })
      .catch(() => {});
    return () => {
      active = false;
      unsubscribe();
    };
  }, []);

  const handleBrowse = async () => {
    try {
      const res = await api.selectFolder();
      if (res.path) setExportDir(res.path);
    } catch (error) {
      setMessage(`选择导出目录失败：${error?.message || '未知错误'}`);
    }
  };

  const handleDesktopUpdateAction = async (action) => {
    if (updateActionBusy) return;
    setUpdateActionBusy(true);
    try {
      if (action === 'download') {
        await downloadDesktopUpdate();
      } else if (action === 'install') {
        await installDesktopUpdate();
      } else {
        await checkForDesktopUpdates();
      }
    } catch (error) {
      setUpdateStatus({
        state: 'error',
        message: error?.message || '更新操作失败',
      });
    } finally {
      setUpdateActionBusy(false);
    }
  };

  const handleExportAllChats = () => {
    if (!onExportAll || busy) return;
    onClose?.();
    window.setTimeout(() => onExportAll(), 0);
  };

  const handleSave = async () => {
    const trimmedAesKey = imageAesKey.trim();
    const trimmedXorKey = xorKey.trim();
    const normalizedHdTimeout = Number(hdAutomationTimeout);
    const normalizedHdMinDwell = Number(hdAutomationMinDwell);
    const normalizedAnalysisTimeout = Number(analysisTimeout);
    const normalizedAnalysisPresets = analysisPresets.map((preset) => ({
      id: String(preset.id || '').trim(),
      name: String(preset.name || '').trim(),
      strength: preset.strength,
      detail: preset.detail,
      requirements: String(preset.requirements || '').trim(),
    }));
    const visionProtocol = (
      getInterfacePreset('vision', visionInterfacePreset)?.protocol || 'openai_compatible'
    );
    const transcriptionProtocol = (
      getInterfacePreset('transcription', transcriptionInterfacePreset)?.protocol
      || 'openai_compatible'
    );
    const analysisProtocol = (
      getInterfacePreset('analysis', analysisInterfacePreset)?.protocol
      || 'openai_compatible'
    );

    if (
      !Number.isInteger(normalizedHdTimeout)
      || normalizedHdTimeout < 5
      || normalizedHdTimeout > 120
    ) {
      setMessage('保存失败: 高清任务单张最大等待时间请填写 5–120 的整数秒');
      return;
    }
    if (
      !Number.isFinite(normalizedHdMinDwell)
      || normalizedHdMinDwell < 0
      || normalizedHdMinDwell > 5
    ) {
      setMessage('保存失败: 验证成功后的最短停留时间请填写 0–5 秒');
      return;
    }

    if (
      visionConnectionChanged
      && settings?.vision?.has_api_key
      && visionRequiresApiKey
      && !visionApiKey.trim()
      && !clearVisionApiKey
    ) {
      setActiveSection('vision');
      setMessage('保存失败: 图片识别接口已更换，请输入新 API Key，或明确清除旧密钥');
      return;
    }
    if (
      transcriptionConnectionChanged
      && settings?.transcription?.has_api_key
      && transcriptionRequiresApiKey
      && !transcriptionApiKey.trim()
      && !clearTranscriptionApiKey
    ) {
      setActiveSection('transcription');
      setMessage('保存失败: 语音转写接口已更换，请输入新 API Key，或明确清除旧密钥');
      return;
    }
    if (
      analysisConnectionChanged
      && settings?.analysis?.has_api_key
      && analysisRequiresApiKey
      && !analysisApiKey.trim()
      && !clearAnalysisApiKey
    ) {
      setActiveSection('analysis');
      setMessage('保存失败: 内容分析接口已更换，请输入新 API Key，或明确清除旧密钥');
      return;
    }

    if (visionInterfacePreset === 'custom' && !customInterfaceName.trim()) {
      setMessage('保存失败: 请填写自定义接口名称');
      return;
    }
    if (visionInterfacePreset === 'custom' && customProtocol === 'custom_json') {
      try {
        const template = JSON.parse(customRequestTemplate);
        if (template == null || typeof template !== 'object') throw new Error('invalid');
        const extraHeaders = JSON.parse(customExtraHeaders);
        if (extraHeaders == null || Array.isArray(extraHeaders) || typeof extraHeaders !== 'object') {
          throw new Error('invalid headers');
        }
      } catch {
        setMessage('保存失败: 自定义请求模板和附加请求头必须是有效 JSON 对象');
        return;
      }
      if (!customApiKeyHeader.trim() || !customResponsePath.trim()) {
        setMessage('保存失败: 请填写 API Key 请求头和响应文本路径');
        return;
      }
    }

    if (!transcriptionModel.trim()) {
      setMessage('保存失败: 请填写语音转文字模型名称');
      return;
    }
    if (
      transcriptionLanguage.trim()
      && transcriptionLanguage.trim() !== 'auto'
      && !/^[0-9A-Za-z_.-]{1,32}$/.test(transcriptionLanguage.trim())
    ) {
      setMessage('保存失败: 语音语言请填写 auto、zh、en 或接口支持的语言代码');
      return;
    }
    if (!transcriptionBaseUrl.trim()) {
      setMessage('保存失败: 请填写语音转文字接口地址');
      return;
    }
    if (transcriptionInterfacePreset === 'custom_multipart') {
      if (!transcriptionCustomName.trim()) {
        setMessage('保存失败: 请填写自定义语音接口名称');
        return;
      }
      try {
        const headers = JSON.parse(transcriptionExtraHeaders);
        const formFields = JSON.parse(transcriptionExtraFormFields);
        if (!headers || Array.isArray(headers) || typeof headers !== 'object') throw new Error('headers');
        if (!formFields || Array.isArray(formFields) || typeof formFields !== 'object') throw new Error('fields');
      } catch {
        setMessage('保存失败: 自定义语音接口的附加请求头和表单字段必须是有效 JSON 对象');
        return;
      }
      if (
        !transcriptionApiKeyHeader.trim()
        || !transcriptionAudioField.trim()
        || !transcriptionResponsePath.trim()
        || !transcriptionFilename.trim()
      ) {
        setMessage('保存失败: 请完整填写自定义语音接口的密钥请求头、音频字段、文件名和响应路径');
        return;
      }
    }

    if (!analysisBaseUrl.trim()) {
      setMessage('保存失败: 请填写 AI 分析接口地址');
      return;
    }
    if (!analysisModel.trim()) {
      setMessage('保存失败: 请填写 AI 分析模型名称');
      return;
    }
    if (
      !Number.isInteger(normalizedAnalysisTimeout)
      || normalizedAnalysisTimeout < 5
      || normalizedAnalysisTimeout > 600
    ) {
      setMessage('保存失败: AI 分析请求超时请填写 5–600 的整数秒');
      return;
    }
    if (analysisInterfacePreset === 'custom' && !analysisCustomName.trim()) {
      setMessage('保存失败: 请填写自定义 AI 分析接口名称');
      return;
    }
    if (analysisInterfacePreset === 'custom' && analysisCustomProtocol === 'custom_json') {
      try {
        const template = JSON.parse(analysisRequestTemplate);
        const headers = JSON.parse(analysisExtraHeaders);
        if (template == null || typeof template !== 'object') {
          throw new Error('template');
        }
        if (headers == null || Array.isArray(headers) || typeof headers !== 'object') {
          throw new Error('headers');
        }
      } catch {
        setMessage('保存失败: AI 分析自定义请求模板和附加请求头必须是有效 JSON 对象');
        return;
      }
      if (!analysisApiKeyHeader.trim() || !analysisResponsePath.trim()) {
        setMessage('保存失败: 请填写 AI 分析 API Key 请求头和响应文本路径');
        return;
      }
    }
    if (normalizedAnalysisPresets.length === 0) {
      setMessage('保存失败: 请至少保留一个分析预设');
      return;
    }
    const presetIds = new Set();
    for (const preset of normalizedAnalysisPresets) {
      if (!preset.id || presetIds.has(preset.id)) {
        setMessage('保存失败: 分析预设标识无效或重复，请删除后重新添加该预设');
        return;
      }
      presetIds.add(preset.id);
      if (!preset.name) {
        setMessage('保存失败: 请填写每个分析预设的名称');
        return;
      }
      if (!Object.prototype.hasOwnProperty.call(ANALYSIS_STRENGTHS, preset.strength)) {
        setMessage(`保存失败: “${preset.name}”的分析强度无效`);
        return;
      }
      if (!Object.prototype.hasOwnProperty.call(ANALYSIS_DETAILS, preset.detail)) {
        setMessage(`保存失败: “${preset.name}”的详细程度无效`);
        return;
      }
      if (preset.requirements.length > 4000) {
        setMessage(`保存失败: “${preset.name}”的附加要求不能超过 4000 字`);
        return;
      }
    }

    if (trimmedAesKey && (!/^[\x20-\x7E]+$/.test(trimmedAesKey) || trimmedAesKey.length !== 16)) {
      setMessage('保存失败: 图片 AES 密钥应为 16 位 ASCII 字符');
      return;
    }

    let normalizedXorKey = trimmedXorKey;
    if (trimmedXorKey && trimmedXorKey.toLowerCase() !== 'auto') {
      if (/^0x[0-9a-fA-F]{1,2}$/.test(trimmedXorKey)) {
        normalizedXorKey = `0x${trimmedXorKey.slice(2).toUpperCase()}`;
      } else if (/^\d{1,3}$/.test(trimmedXorKey) && Number(trimmedXorKey) <= 255) {
        normalizedXorKey = String(Number(trimmedXorKey));
      } else if (/^[0-9a-fA-F]{1,2}$/.test(trimmedXorKey) && /[a-fA-F]/.test(trimmedXorKey)) {
        normalizedXorKey = `0x${trimmedXorKey.toUpperCase()}`;
      } else {
        setMessage('保存失败: XOR 密钥应为 auto、0-255 或 0x00-0xFF');
        return;
      }
    }

    setSaving(true);
    setMessage('');
    try {
      const vision = {
        provider: visionProtocol,
        interface_preset: visionInterfacePreset,
        custom_name: customInterfaceName.trim(),
        custom_protocol: customProtocol,
        custom_api_key_header: customApiKeyHeader.trim(),
        custom_api_key_prefix: customApiKeyPrefix,
        custom_extra_headers: customExtraHeaders,
        custom_request_template: customRequestTemplate,
        custom_response_path: customResponsePath.trim(),
        base_url: visionBaseUrl.trim(),
        model: visionModel.trim(),
        image_detail: imageDetail,
        max_images_per_task: Math.max(1, Math.min(500, Number(maxImagesPerTask) || 1)),
      };
      const clearStaleVisionKey = (
        visionConnectionChanged
        && settings?.vision?.has_api_key
        && !visionRequiresApiKey
        && !visionApiKey.trim()
      );
      if (clearVisionApiKey || clearStaleVisionKey) {
        vision.clear_api_key = true;
      } else if (visionApiKey.trim()) {
        vision.api_key = visionApiKey.trim();
      }

      const transcription = {
        provider: transcriptionProtocol,
        interface_preset: transcriptionInterfacePreset,
        base_url: transcriptionBaseUrl.trim(),
        model: transcriptionModel.trim(),
        language: transcriptionLanguage.trim().toLowerCase() || 'auto',
        max_voices_per_task: Math.max(1, Math.min(500, Number(maxVoicesPerTask) || 1)),
        custom_name: transcriptionCustomName.trim(),
        custom_api_key_header: transcriptionApiKeyHeader.trim(),
        custom_api_key_prefix: transcriptionApiKeyPrefix,
        custom_extra_headers: transcriptionExtraHeaders,
        custom_audio_field: transcriptionAudioField.trim(),
        custom_model_field: transcriptionModelField.trim(),
        custom_language_field: transcriptionLanguageField.trim(),
        custom_extra_form_fields: transcriptionExtraFormFields,
        custom_response_path: transcriptionResponsePath.trim(),
        custom_filename: transcriptionFilename.trim(),
      };
      if (clearTranscriptionApiKey) {
        transcription.clear_api_key = true;
      } else if (transcriptionApiKey.trim()) {
        transcription.api_key = transcriptionApiKey.trim();
      }

      const analysis = {
        provider: analysisProtocol,
        interface_preset: analysisInterfacePreset,
        custom_name: analysisCustomName.trim(),
        custom_protocol: analysisCustomProtocol,
        custom_api_key_header: analysisApiKeyHeader.trim(),
        custom_api_key_prefix: analysisApiKeyPrefix,
        custom_extra_headers: analysisExtraHeaders,
        custom_request_template: analysisRequestTemplate,
        custom_response_path: analysisResponsePath.trim(),
        base_url: analysisBaseUrl.trim(),
        model: analysisModel.trim(),
        timeout_seconds: normalizedAnalysisTimeout,
        presets: normalizedAnalysisPresets,
      };
      const clearStaleAnalysisKey = (
        analysisConnectionChanged
        && settings?.analysis?.has_api_key
        && !analysisRequiresApiKey
        && !analysisApiKey.trim()
      );
      if (clearAnalysisApiKey || clearStaleAnalysisKey) {
        analysis.clear_api_key = true;
      } else if (analysisApiKey.trim()) {
        analysis.api_key = analysisApiKey.trim();
      }

      const images = {};
      if (normalizedXorKey) images.xor_key = normalizedXorKey;
      if (clearImageAesKey) {
        images.clear_aes_key = true;
      } else if (trimmedAesKey) {
        images.aes_key = trimmedAesKey;
      }

      await onSaveSettings({
        export: { directory: exportDir.trim() || settings?.export?.directory },
        ui: {
          show_chat_images: showChatImages,
          image_quality: chatImageQuality,
          hd_automation_timeout_seconds: normalizedHdTimeout,
          hd_automation_min_dwell_seconds: normalizedHdMinDwell,
        },
        vision,
        transcription,
        analysis,
        images,
        about,
      });
      visionRevealRequestRef.current += 1;
      transcriptionRevealRequestRef.current += 1;
      analysisRevealRequestRef.current += 1;
      setVisionApiKey('');
      setVisionApiKeyLoaded(false);
      setShowVisionApiKey(false);
      setRevealingVisionApiKey(false);
      setTranscriptionApiKey('');
      setTranscriptionApiKeyLoaded(false);
      setShowTranscriptionApiKey(false);
      setRevealingTranscriptionApiKey(false);
      setAnalysisApiKey('');
      setAnalysisApiKeyLoaded(false);
      setShowAnalysisApiKey(false);
      setRevealingAnalysisApiKey(false);
      setImageAesKey('');
      setShowImageAesKey(false);
      setClearVisionApiKey(false);
      setClearTranscriptionApiKey(false);
      setClearAnalysisApiKey(false);
      setClearImageAesKey(false);
      setImageKeyExtracted(false);
      setMessage('设置已保存');
      if (messageTimerRef.current) window.clearTimeout(messageTimerRef.current);
      messageTimerRef.current = window.setTimeout(() => {
        setMessage((current) => (current === '设置已保存' ? '' : current));
        messageTimerRef.current = null;
      }, 2000);
    } catch (error) {
      setMessage('保存失败: ' + error.message);
    } finally {
      setSaving(false);
    }
  };

  const handleExtractImageKey = async () => {
    if (!onExtractImageKey || extractingImageKey) return;
    setExtractingImageKey(true);
    setMessage('正在从当前微信账号数据中获取并验证图片密钥，请稍候...');
    try {
      const result = await onExtractImageKey();
      setImageAesKey('');
      setShowImageAesKey(false);
      setClearImageAesKey(false);
      setImageKeyExtracted(true);
      if (result?.data?.images?.xor_key != null) {
        setXorKey(String(result.data.images.xor_key));
      }
      const format = result?.meta?.verified_format;
      setMessage(`图片 AES 密钥已获取、验证并保存${format ? `（${format}）` : ''}`);
    } catch (error) {
      setMessage('获取失败: ' + error.message);
    } finally {
      setExtractingImageKey(false);
    }
  };

  const handleToggleImageAesKey = async () => {
    if (showImageAesKey) {
      setShowImageAesKey(false);
      return;
    }
    if (imageAesKey || clearImageAesKey || !settings?.images?.has_aes_key) {
      setShowImageAesKey(true);
      return;
    }
    setRevealingImageKey(true);
    setMessage('正在读取当前账号已保存的图片 AES 密钥...');
    try {
      const result = await api.revealImageKey();
      setImageAesKey(result?.aes_key || '');
      setShowImageAesKey(true);
      setMessage('已显示当前账号的图片 AES 密钥');
    } catch (error) {
      setMessage('获取失败: ' + error.message);
    } finally {
      setRevealingImageKey(false);
    }
  };

  const handleToggleVisionApiKey = async () => {
    if (showVisionApiKey) {
      setShowVisionApiKey(false);
      return;
    }
    if (
      !visionApiKey
      && visionConnectionChanged
      && settings?.vision?.has_api_key
    ) {
      setMessage('安全提示: 接口服务或地址已更换。为避免把旧密钥发送给新服务，请输入新 API Key。');
      return;
    }
    if (visionApiKey || clearVisionApiKey || !settings?.vision?.has_api_key) {
      setShowVisionApiKey(true);
      return;
    }

    const requestId = visionRevealRequestRef.current + 1;
    visionRevealRequestRef.current = requestId;
    setRevealingVisionApiKey(true);
    setMessage('正在读取已保存的图片识别 API Key...');
    try {
      const result = await api.revealVisionApiKey();
      if (visionRevealRequestRef.current !== requestId) return;
      if (!result?.api_key) throw new Error('后端未返回已保存的密钥');
      setVisionApiKey(String(result.api_key));
      setVisionApiKeyLoaded(true);
      setShowVisionApiKey(true);
      setMessage('已显示图片识别 API Key');
    } catch (error) {
      if (visionRevealRequestRef.current !== requestId) return;
      setShowVisionApiKey(false);
      setMessage('获取失败: ' + error.message);
    } finally {
      if (visionRevealRequestRef.current === requestId) {
        setRevealingVisionApiKey(false);
      }
    }
  };

  const handleToggleTranscriptionApiKey = async () => {
    if (showTranscriptionApiKey) {
      setShowTranscriptionApiKey(false);
      return;
    }
    if (
      !transcriptionApiKey
      && transcriptionConnectionChanged
      && settings?.transcription?.has_api_key
    ) {
      setMessage('安全提示: 接口服务或地址已更换。为避免把旧密钥发送给新服务，请输入新 API Key。');
      return;
    }
    if (
      transcriptionApiKey
      || clearTranscriptionApiKey
      || !settings?.transcription?.has_api_key
    ) {
      setShowTranscriptionApiKey(true);
      return;
    }

    const requestId = transcriptionRevealRequestRef.current + 1;
    transcriptionRevealRequestRef.current = requestId;
    setRevealingTranscriptionApiKey(true);
    setMessage('正在读取已保存的语音转写 API Key...');
    try {
      const result = await api.revealTranscriptionApiKey();
      if (transcriptionRevealRequestRef.current !== requestId) return;
      if (!result?.api_key) throw new Error('后端未返回已保存的密钥');
      setTranscriptionApiKey(String(result.api_key));
      setTranscriptionApiKeyLoaded(true);
      setShowTranscriptionApiKey(true);
      setMessage('已显示语音转写 API Key');
    } catch (error) {
      if (transcriptionRevealRequestRef.current !== requestId) return;
      setShowTranscriptionApiKey(false);
      setMessage('获取失败: ' + error.message);
    } finally {
      if (transcriptionRevealRequestRef.current === requestId) {
        setRevealingTranscriptionApiKey(false);
      }
    }
  };

  const handleToggleAnalysisApiKey = async () => {
    if (showAnalysisApiKey) {
      setShowAnalysisApiKey(false);
      return;
    }
    if (
      !analysisApiKey
      && analysisConnectionChanged
      && settings?.analysis?.has_api_key
    ) {
      setMessage('安全提示: 接口服务或地址已更换。为避免把旧密钥发送给新服务，请输入新 API Key。');
      return;
    }
    if (analysisApiKey || clearAnalysisApiKey || !settings?.analysis?.has_api_key) {
      setShowAnalysisApiKey(true);
      return;
    }

    const requestId = analysisRevealRequestRef.current + 1;
    analysisRevealRequestRef.current = requestId;
    setRevealingAnalysisApiKey(true);
    setMessage('正在读取已保存的 AI 分析 API Key...');
    try {
      const result = await api.revealAnalysisApiKey();
      if (analysisRevealRequestRef.current !== requestId) return;
      if (!result?.api_key) throw new Error('后端未返回已保存的密钥');
      setAnalysisApiKey(String(result.api_key));
      setAnalysisApiKeyLoaded(true);
      setShowAnalysisApiKey(true);
      setMessage('已显示 AI 分析 API Key');
    } catch (error) {
      if (analysisRevealRequestRef.current !== requestId) return;
      setShowAnalysisApiKey(false);
      setMessage('获取失败: ' + error.message);
    } finally {
      if (analysisRevealRequestRef.current === requestId) {
        setRevealingAnalysisApiKey(false);
      }
    }
  };

  const handleVisionInterfacePresetChange = (nextPresetId) => {
    const preset = getInterfacePreset('vision', nextPresetId);
    visionRevealRequestRef.current += 1;
    setRevealingVisionApiKey(false);
    setVisionInterfacePreset(nextPresetId);
    setVisionBaseUrl(preset?.baseUrl || '');
    setVisionModel(preset?.model || '');
    setVisionApiKey('');
    setVisionApiKeyLoaded(false);
    setShowVisionApiKey(false);
    setAdvancedOpen((current) => ({
      ...current,
      vision: nextPresetId === 'custom' ? current.vision : false,
    }));
  };

  const handleTranscriptionInterfacePresetChange = (nextPresetId) => {
    const preset = getInterfacePreset('transcription', nextPresetId);
    transcriptionRevealRequestRef.current += 1;
    setRevealingTranscriptionApiKey(false);
    setTranscriptionInterfacePreset(nextPresetId);
    setTranscriptionBaseUrl(preset?.baseUrl || '');
    setTranscriptionModel(preset?.model || '');
    setTranscriptionApiKey('');
    setTranscriptionApiKeyLoaded(false);
    setShowTranscriptionApiKey(false);
    setAdvancedOpen((current) => ({
      ...current,
      transcription: nextPresetId === 'custom_multipart' ? current.transcription : false,
    }));
  };

  const handleAnalysisInterfacePresetChange = (nextPresetId) => {
    const preset = getInterfacePreset('analysis', nextPresetId);
    analysisRevealRequestRef.current += 1;
    setRevealingAnalysisApiKey(false);
    setAnalysisInterfacePreset(nextPresetId);
    setAnalysisBaseUrl(preset?.baseUrl || '');
    setAnalysisModel(preset?.model || '');
    setAnalysisApiKey('');
    setAnalysisApiKeyLoaded(false);
    setShowAnalysisApiKey(false);
    setAdvancedOpen((current) => ({
      ...current,
      analysis: nextPresetId === 'custom' ? current.analysis : false,
    }));
  };

  const handleAddAnalysisPreset = () => {
    if (analysisPresets.length >= 50) return;
    const generatedId = globalThis.crypto?.randomUUID?.()
      || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    const id = `custom-${generatedId}`;
    setAnalysisPresets([
      ...analysisPresets,
      {
        id,
        name: `自定义分析 ${analysisPresets.length + 1}`,
        strength: 'balanced',
        detail: 'standard',
        requirements: '',
      },
    ]);
    setActiveAnalysisPresetId(id);
  };

  const handleUpdateAnalysisPreset = (id, field, value) => {
    setAnalysisPresets((current) => current.map((preset) => (
      preset.id === id ? { ...preset, [field]: value } : preset
    )));
  };

  const handleDeleteAnalysisPreset = (id) => {
    if (analysisPresets.length <= 1) {
      setMessage('保存失败: 至少需要保留一个分析预设');
      return;
    }
    const index = analysisPresets.findIndex((preset) => preset.id === id);
    const next = analysisPresets.filter((preset) => preset.id !== id);
    setAnalysisPresets(next);
    if (activeAnalysisPresetId === id) {
      setActiveAnalysisPresetId(next[Math.max(0, index - 1)]?.id || next[0]?.id || '');
    }
  };

  const accountName = settings?.images?.current_account || '当前微信账号';
  const visionPreset = getInterfacePreset('vision', visionInterfacePreset);
  const transcriptionPreset = getInterfacePreset(
    'transcription', transcriptionInterfacePreset
  );
  const analysisPreset = getInterfacePreset('analysis', analysisInterfacePreset);
  const visionRequiresApiKey = interfaceRequiresApiKey(
    'vision', visionInterfacePreset, visionBaseUrl
  );
  const transcriptionRequiresApiKey = interfaceRequiresApiKey(
    'transcription', transcriptionInterfacePreset, transcriptionBaseUrl
  );
  const analysisRequiresApiKey = interfaceRequiresApiKey(
    'analysis', analysisInterfacePreset, analysisBaseUrl
  );
  const visionConnectionChanged = Boolean(settings?.vision) && (
    resolveInterfacePreset('vision', settings.vision) !== visionInterfacePreset
    || normalizeModelBaseUrl(settings.vision.base_url) !== normalizeModelBaseUrl(visionBaseUrl)
  );
  const transcriptionConnectionChanged = Boolean(settings?.transcription) && (
    resolveInterfacePreset('transcription', settings.transcription)
      !== transcriptionInterfacePreset
    || normalizeModelBaseUrl(settings.transcription.base_url)
      !== normalizeModelBaseUrl(transcriptionBaseUrl)
  );
  const analysisConnectionChanged = Boolean(settings?.analysis) && (
    resolveInterfacePreset('analysis', settings.analysis) !== analysisInterfacePreset
    || normalizeModelBaseUrl(settings.analysis.base_url) !== normalizeModelBaseUrl(analysisBaseUrl)
  );
  const hasVisionApiKey = (
    (Boolean(settings?.vision?.has_api_key) && !visionConnectionChanged)
    || Boolean(visionApiKey.trim())
  ) && !clearVisionApiKey;
  const hasTranscriptionApiKey = (
    (Boolean(settings?.transcription?.has_api_key) && !transcriptionConnectionChanged)
    || Boolean(transcriptionApiKey.trim())
  ) && !clearTranscriptionApiKey;
  const hasAnalysisApiKey = (
    (Boolean(settings?.analysis?.has_api_key) && !analysisConnectionChanged)
    || Boolean(analysisApiKey.trim())
  ) && !clearAnalysisApiKey;
  const hasImageAesKey = (
    Boolean(settings?.images?.has_aes_key)
    || imageKeyExtracted
    || Boolean(imageAesKey.trim())
  ) && !clearImageAesKey;
  const hasCurrentAccount = Boolean(settings?.images?.current_account);
  const activeAnalysisPreset = (
    analysisPresets.find((preset) => preset.id === activeAnalysisPresetId)
    || analysisPresets[0]
  );
  const busy = (
    saving
    || extractingImageKey
    || revealingImageKey
    || revealingVisionApiKey
    || revealingTranscriptionApiKey
    || revealingAnalysisApiKey
  );
  const dialogRef = useDialogFocus({ onClose, closeDisabled: busy });

  const toggleAdvanced = (key) => {
    setAdvancedOpen((current) => ({ ...current, [key]: !current[key] }));
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/55 p-0 backdrop-blur-[2px] sm:p-4">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-dialog-title"
        aria-busy={busy}
        tabIndex={-1}
        className="flex h-[100dvh] max-h-none w-full flex-col overflow-hidden bg-white shadow-2xl outline-none sm:h-auto sm:max-h-[94vh] sm:max-w-6xl sm:rounded-2xl sm:ring-1 sm:ring-slate-900/10"
      >
        <div className="flex flex-shrink-0 items-start justify-between gap-4 border-b border-slate-200 bg-white px-4 py-4 sm:px-6">
          <div>
            <h2 id="settings-dialog-title" className="text-lg font-semibold tracking-tight text-slate-900">设置</h2>
            <p className="mt-1 text-sm text-slate-500">管理媒体、模型服务、分析预设与应用信息</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            aria-label="关闭设置"
            className="rounded-lg px-2 py-1 text-xl leading-none text-slate-400 hover:bg-slate-100 hover:text-slate-700 disabled:opacity-50"
          >
            ×
          </button>
        </div>

        <div className="flex min-h-0 flex-1 flex-col md:flex-row">
          <nav
            aria-label="设置分类"
            className="flex flex-shrink-0 gap-1 overflow-x-auto border-b border-slate-200 bg-slate-50 px-3 py-2 md:w-56 md:flex-col md:overflow-y-auto md:border-b-0 md:border-r md:px-3 md:py-4"
          >
            {SETTINGS_SECTIONS.map((section) => {
              const active = activeSection === section.id;
              return (
                <button
                  key={section.id}
                  type="button"
                  onClick={() => setActiveSection(section.id)}
                  aria-current={active ? 'page' : undefined}
                  className={`flex flex-shrink-0 items-center gap-2 rounded-lg px-3 py-2 text-left transition-colors md:w-full ${
                    active
                      ? 'bg-white text-emerald-700 shadow-sm ring-1 ring-slate-200'
                      : 'text-slate-600 hover:bg-white hover:text-slate-900'
                  }`}
                >
                  <span aria-hidden="true" className={`grid h-7 w-7 place-items-center rounded-lg text-xs font-semibold ${
                    active ? 'bg-emerald-50 text-emerald-700' : 'bg-slate-200/70 text-slate-500'
                  }`}>{section.icon}</span>
                  <span className="min-w-0">
                    <span className="block whitespace-nowrap text-sm font-medium">{section.label}</span>
                    <span className="hidden truncate text-xs text-slate-400 md:block">
                      {section.description}
                    </span>
                  </span>
                </button>
              );
            })}
          </nav>

          <div className="min-h-0 min-w-0 flex-1 space-y-4 overflow-y-auto bg-slate-50/70 p-4 sm:p-6">
          <section className={activeSection === 'images' ? 'space-y-4' : 'hidden'}>
            <PanelTitle
              title="聊天图片"
              description="控制聊天中的图片显示、高清自动获取和当前账号的解密密钥。"
              status={`当前账号：${accountName}`}
            />
            <SettingsCard
              title="显示与高清任务默认值"
              description="这些选项会作为聊天浏览和新建高清获取任务的默认配置。"
            >
              <Toggle
                checked={showChatImages}
                onChange={setShowChatImages}
                label="显示聊天图片"
                description="关闭后只显示图片占位符和已经生成的文字描述"
              />
              <div className="mt-4 border-t border-gray-200 pt-4">
                <label className="block text-sm font-medium text-gray-700 mb-1.5">聊天图片质量</label>
                <select
                  value={chatImageQuality}
                  onChange={(event) => setChatImageQuality(event.target.value)}
                  className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm bg-white focus:outline-none focus:border-wechat-green"
                >
                  <option value="smart">智能模式（列表缩略图，点击查看最高画质）</option>
                  <option value="high">高清优先（列表直接加载最高可用画质）</option>
                  <option value="thumbnail">流畅模式（始终优先缩略图）</option>
                </select>
                <p className="text-xs text-gray-400 mt-1">本地没有高清文件时会自动降级为缩略图，不会联网下载。</p>
              </div>
              <div className="mt-4 border-t border-gray-200 pt-4">
                <div className="mb-2">
                  <p className="text-sm font-medium text-gray-700">高清图片自动获取</p>
                  <p className="mt-0.5 text-xs text-gray-400">作为新任务的默认值，创建任务时仍可临时修改。</p>
                </div>
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <label className="text-xs text-gray-600">
                    单张最大等待时间
                    <div className="relative mt-1">
                      <input
                        type="number"
                        min="5"
                        max="120"
                        step="1"
                        value={hdAutomationTimeout}
                        onChange={(event) => setHdAutomationTimeout(event.target.value)}
                        className="w-full rounded-lg border border-gray-200 bg-white px-3 py-2 pr-10 text-sm focus:border-wechat-green focus:outline-none"
                      />
                      <span className="absolute right-3 top-2 text-sm text-gray-400">秒</span>
                    </div>
                  </label>
                  <label className="text-xs text-gray-600">
                    验证成功后最短停留
                    <div className="relative mt-1">
                      <input
                        type="number"
                        min="0"
                        max="5"
                        step="0.1"
                        value={hdAutomationMinDwell}
                        onChange={(event) => setHdAutomationMinDwell(event.target.value)}
                        className="w-full rounded-lg border border-gray-200 bg-white px-3 py-2 pr-10 text-sm focus:border-wechat-green focus:outline-none"
                      />
                      <span className="absolute right-3 top-2 text-sm text-gray-400">秒</span>
                    </div>
                  </label>
                </div>
                <p className="mt-2 text-xs leading-relaxed text-gray-400">
                  程序仍会自适应判断：已有高清缓存和明确过期图片会立即继续；未确认图片最多等待上面的时间；本次新验证成功的图片在翻页前至少再停留指定时长。
                </p>
              </div>
            </SettingsCard>
          </section>

          <section className={activeSection === 'images' ? '' : 'hidden'}>
            <SettingsCard
              title="当前账号图片解密"
              description="AES 密钥通常可自动获取；仅在特殊数据格式下才需要手动调整 XOR 密钥。"
            >
              <div className="space-y-4">
              <div>
                <div className="flex items-center justify-between mb-1.5">
                  <label className="text-sm font-medium text-gray-700">图片 AES 密钥</label>
                  <span className={`text-xs ${hasImageAesKey ? 'text-green-600' : 'text-gray-400'}`}>
                    {hasImageAesKey ? '已为当前账号配置' : '未配置'}
                  </span>
                </div>
                <div className="flex flex-col gap-2 sm:flex-row">
                  <div className="relative flex-1">
                    <input
                      type={showImageAesKey ? 'text' : 'password'}
                      value={imageAesKey}
                      onChange={(event) => {
                        setImageAesKey(event.target.value);
                        if (event.target.value) setClearImageAesKey(false);
                      }}
                      disabled={clearImageAesKey || extractingImageKey || revealingImageKey}
                      autoComplete="new-password"
                      placeholder={hasImageAesKey ? '已配置，留空则保留原密钥' : '16 位 ASCII 图片 AES 密钥'}
                      className="w-full border border-gray-200 rounded-lg px-3 py-2 pr-14 text-sm font-mono focus:outline-none focus:border-wechat-green disabled:bg-gray-100"
                    />
                    <button
                      type="button"
                      onClick={handleToggleImageAesKey}
                      disabled={revealingImageKey || extractingImageKey}
                      className="absolute right-2 top-1/2 -translate-y-1/2 text-xs text-gray-400 hover:text-gray-600 disabled:opacity-50"
                    >
                      {revealingImageKey ? '读取中' : (showImageAesKey ? '隐藏' : '显示')}
                    </button>
                  </div>
                  <button
                    type="button"
                    onClick={handleExtractImageKey}
                    disabled={
                      saving
                      || extractingImageKey
                      || revealingImageKey
                      || !hasCurrentAccount
                    }
                    title="从当前账号本地数据获取并验证图片密钥"
                    className="px-3 py-2 rounded-lg border border-wechat-green bg-green-50 text-wechat-green text-xs font-medium hover:bg-green-100 transition-colors whitespace-nowrap disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-1.5"
                  >
                    {extractingImageKey && (
                      <span className="w-3 h-3 border-2 border-wechat-green border-t-transparent rounded-full animate-spin" />
                    )}
                    {extractingImageKey ? '正在获取...' : (hasImageAesKey ? '重新获取' : '自动获取')}
                  </button>
                  {(settings?.images?.has_aes_key || imageKeyExtracted || clearImageAesKey) && (
                    <button
                      type="button"
                      onClick={() => {
                        setClearImageAesKey((value) => !value);
                        setImageAesKey('');
                      }}
                      disabled={extractingImageKey || revealingImageKey}
                      className={`px-3 py-2 rounded-lg border text-xs transition-colors disabled:opacity-50 ${
                        clearImageAesKey
                          ? 'border-gray-300 bg-white text-gray-600'
                          : 'border-red-200 bg-red-50 text-red-600 hover:bg-red-100'
                      }`}
                    >
                      {clearImageAesKey ? '撤销清除' : '清除密钥'}
                    </button>
                  )}
                </div>
              </div>

              <p className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-700">
                选择当前登录的微信 4.x 账号后点击“自动获取”即可；工具会优先从本地账号元数据派生并验证，必要时自动扫描微信内存。
              </p>
              <AdvancedSection
                open={advancedOpen.imageDecrypt}
                onToggle={() => toggleAdvanced('imageDecrypt')}
                title="高级解密参数"
                description="大多数用户无需修改 XOR 密钥。"
              >
                <label className="block text-sm font-medium text-gray-700 mb-1.5">图片 XOR 密钥</label>
                <input
                  type="text"
                  value={xorKey}
                  onChange={(event) => setXorKey(event.target.value)}
                  maxLength={5}
                  placeholder="auto 或 0x88"
                  className="w-32 border border-gray-200 rounded-lg px-3 py-2 text-sm font-mono uppercase focus:outline-none focus:border-wechat-green"
                />
                <p className="text-xs text-gray-500 mt-1">
                  可填 auto 自动推导、0-255 十进制或 0x00-0xFF；密钥仅作用于当前账号。
                </p>
              </AdvancedSection>
              </div>
            </SettingsCard>
          </section>

          <section className={activeSection === 'vision' ? 'space-y-4' : 'hidden'}>
            <PanelTitle
              title="图片识别"
              description="配置点击“图片识别”后使用的视觉模型服务。"
              status={visionPreset?.label}
            />
            <SettingsCard
              title="基础连接"
              description="选择常用服务会自动填写推荐的协议、地址和模型。"
            >
              <div className="space-y-4">
                <InterfacePresetSelect
                  kind="vision"
                  id="vision-interface-preset"
                  value={visionInterfacePreset}
                  onChange={handleVisionInterfacePresetChange}
                  customName={customInterfaceName}
                />
                {visionInterfacePreset === 'custom' && (
                  <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
                    <div>
                      <label className="mb-1.5 block text-sm font-medium text-gray-700">
                        自定义接口名称
                      </label>
                      <input
                        type="text"
                        value={customInterfaceName}
                        onChange={(event) => setCustomInterfaceName(event.target.value)}
                        placeholder="例如：公司内部视觉模型"
                        maxLength={60}
                        className={INPUT_CLASS}
                      />
                    </div>
                    <div>
                      <label className="mb-1.5 block text-sm font-medium text-gray-700">
                        请求格式
                      </label>
                      <select
                        value={customProtocol}
                        onChange={(event) => setCustomProtocol(event.target.value)}
                        className={INPUT_CLASS}
                      >
                        <option value="openai_compatible">OpenAI 兼容格式</option>
                        <option value="anthropic">Anthropic Messages 格式</option>
                        <option value="gemini">Google Gemini 格式</option>
                        <option value="custom_json">通用 JSON 模板（完全自定义）</option>
                      </select>
                    </div>
                  </div>
                )}
                <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
                  <div>
                    <label className="mb-1.5 block text-sm font-medium text-gray-700">
                      {visionInterfacePreset === 'custom' && customProtocol === 'custom_json'
                        ? '完整请求 URL'
                        : 'Base URL'}
                    </label>
                    <input
                      type="url"
                      value={visionBaseUrl}
                      onChange={(event) => {
                        visionRevealRequestRef.current += 1;
                        const discardSavedKey = visionApiKeyLoaded || revealingVisionApiKey;
                        setRevealingVisionApiKey(false);
                        setVisionBaseUrl(event.target.value);
                        if (discardSavedKey) {
                          setVisionApiKey('');
                          setVisionApiKeyLoaded(false);
                          setShowVisionApiKey(false);
                        }
                      }}
                      placeholder={visionPreset?.baseUrlPlaceholder || visionPreset?.baseUrl}
                      className={INPUT_CLASS}
                    />
                  </div>
                  <div>
                    <label className="mb-1.5 block text-sm font-medium text-gray-700">模型</label>
                    <input
                      type="text"
                      value={visionModel}
                      onChange={(event) => setVisionModel(event.target.value)}
                      placeholder={visionPreset?.modelPlaceholder || visionPreset?.model || '填写模型名称'}
                      className={INPUT_CLASS}
                    />
                  </div>
                </div>
              </div>
            </SettingsCard>

            <SettingsCard
              title="访问凭证"
              description="密钥只保存在本机后端；留空会保留已保存的密钥。"
            >
              <SecretField
                id="vision-api-key"
                value={visionApiKey}
                onChange={(event) => {
                  visionRevealRequestRef.current += 1;
                  setRevealingVisionApiKey(false);
                  setVisionApiKey(event.target.value);
                  setVisionApiKeyLoaded(false);
                  if (event.target.value) setClearVisionApiKey(false);
                }}
                visible={showVisionApiKey}
                onToggleVisible={handleToggleVisionApiKey}
                revealing={revealingVisionApiKey}
                configured={hasVisionApiKey}
                hint={visionApiKey.trim() && !visionApiKeyLoaded
                  ? ''
                  : settings?.vision?.api_key_hint}
                clearPending={clearVisionApiKey}
                onToggleClear={() => {
                  visionRevealRequestRef.current += 1;
                  setRevealingVisionApiKey(false);
                  setClearVisionApiKey((value) => !value);
                  setVisionApiKey('');
                  setVisionApiKeyLoaded(false);
                  setShowVisionApiKey(false);
                }}
                canClear={Boolean(settings?.vision?.has_api_key || clearVisionApiKey)}
                placeholder={hasVisionApiKey ? '已配置，留空则保留原密钥' : '输入模型服务的 API Key'}
                optional={!visionRequiresApiKey}
                optionalDescription={
                  visionConnectionChanged && settings?.vision?.has_api_key
                    ? '当前为本机免密接口；保存时会清除旧服务密钥。若本地服务启用了鉴权，请填写新的 Key。'
                    : '当前使用本机回环地址，API Key 可不填；若本地服务启用了鉴权，仍可在此填写。'
                }
              />
            </SettingsCard>

            <SettingsCard title="任务默认值" description="每次开始图片识别时仍可调整识别范围。">
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <div>
                  <label className="mb-1.5 block text-sm font-medium text-gray-700">图片细节</label>
                  <select
                    value={imageDetail}
                    onChange={(event) => setImageDetail(event.target.value)}
                    className={INPUT_CLASS}
                  >
                    <option value="low">低（最长边 1024，更省费用）</option>
                    <option value="auto">自动（最长边 2048）</option>
                    <option value="high">高（最长边 4096，更多细节）</option>
                  </select>
                </div>
                <div>
                  <label className="mb-1.5 block text-sm font-medium text-gray-700">
                    单次任务最多图片数
                  </label>
                  <input
                    type="number"
                    min="1"
                    max="500"
                    value={maxImagesPerTask}
                    onChange={(event) => setMaxImagesPerTask(event.target.value)}
                    className={INPUT_CLASS}
                  />
                </div>
              </div>
            </SettingsCard>

            {visionInterfacePreset === 'custom' && customProtocol === 'custom_json' && (
              <AdvancedSection
                open={advancedOpen.vision}
                onToggle={() => toggleAdvanced('vision')}
                title="高级请求参数"
                description="自定义鉴权头、JSON 请求模板和响应文本路径。"
              >
                <div className="space-y-4">
                  <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
                    <div>
                      <label className="mb-1.5 block text-sm font-medium text-gray-700">
                        API Key 请求头
                      </label>
                      <input
                        type="text"
                        value={customApiKeyHeader}
                        onChange={(event) => setCustomApiKeyHeader(event.target.value)}
                        placeholder="Authorization"
                        className={`${INPUT_CLASS} font-mono`}
                      />
                    </div>
                    <div>
                      <label className="mb-1.5 block text-sm font-medium text-gray-700">
                        API Key 前缀
                      </label>
                      <input
                        type="text"
                        value={customApiKeyPrefix}
                        onChange={(event) => setCustomApiKeyPrefix(event.target.value)}
                        placeholder="Bearer "
                        className={`${INPUT_CLASS} font-mono`}
                      />
                    </div>
                  </div>
                  <div>
                    <label className="mb-1.5 block text-sm font-medium text-gray-700">
                      附加请求头 JSON
                    </label>
                    <input
                      type="text"
                      value={customExtraHeaders}
                      onChange={(event) => setCustomExtraHeaders(event.target.value)}
                      placeholder={'{"X-API-Version":"2026-01-01"}'}
                      spellCheck={false}
                      className={`${INPUT_CLASS} font-mono`}
                    />
                  </div>
                  <div>
                    <label className="mb-1.5 block text-sm font-medium text-gray-700">
                      请求 JSON 模板
                    </label>
                    <textarea
                      value={customRequestTemplate}
                      onChange={(event) => setCustomRequestTemplate(event.target.value)}
                      rows={9}
                      spellCheck={false}
                      className={`${INPUT_CLASS} resize-y font-mono text-xs`}
                    />
                    <p className="mt-1 text-xs leading-5 text-gray-500">
                      可用变量：{'{{model}}'}、{'{{prompt}}'}、{'{{image_base64}}'}、
                      {'{{image_data_url}}'}、{'{{mime_type}}'}、{'{{detail}}'}。
                    </p>
                  </div>
                  <div>
                    <label className="mb-1.5 block text-sm font-medium text-gray-700">
                      响应文本路径
                    </label>
                    <input
                      type="text"
                      value={customResponsePath}
                      onChange={(event) => setCustomResponsePath(event.target.value)}
                      placeholder="choices.0.message.content"
                      className={`${INPUT_CLASS} font-mono`}
                    />
                  </div>
                </div>
              </AdvancedSection>
            )}

            <p className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-800">
              图片只会在你主动点击“图片识别”并确认范围后上传；保存配置不会上传图片。
            </p>
          </section>

          <section className={activeSection === 'transcription' ? 'space-y-4' : 'hidden'}>
            <PanelTitle
              title="语音转写"
              description="配置点击“语音转文字”后使用的云端语音识别服务。"
              status={transcriptionPreset?.label}
            />
            <div className="rounded-xl border border-gray-200 bg-white p-4 space-y-4 sm:p-5">
              <InterfacePresetSelect
                kind="transcription"
                id="transcription-interface-preset"
                value={transcriptionInterfacePreset}
                onChange={handleTranscriptionInterfacePresetChange}
                customName={transcriptionCustomName}
              />

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1.5">
                  {transcriptionInterfacePreset === 'custom_multipart' ? '完整请求 URL' : 'Base URL'}
                </label>
                <input
                  type="url"
                  value={transcriptionBaseUrl}
                  onChange={(event) => {
                    transcriptionRevealRequestRef.current += 1;
                    const discardSavedKey = (
                      transcriptionApiKeyLoaded || revealingTranscriptionApiKey
                    );
                    setRevealingTranscriptionApiKey(false);
                    setTranscriptionBaseUrl(event.target.value);
                    if (discardSavedKey) {
                      setTranscriptionApiKey('');
                      setTranscriptionApiKeyLoaded(false);
                      setShowTranscriptionApiKey(false);
                    }
                  }}
                  placeholder={transcriptionPreset?.baseUrlPlaceholder || transcriptionPreset?.baseUrl || 'https://example.com/transcribe'}
                  className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-wechat-green"
                />
                {transcriptionPreset?.protocol === 'openai_compatible' && (
                  <p className="text-xs text-gray-400 mt-1">会自动拼接 /audio/transcriptions；也可直接填写完整端点。</p>
                )}
                {transcriptionPreset?.protocol === 'dashscope_asr' && (
                  <p className="text-xs text-gray-400 mt-1">会自动拼接 /chat/completions；也可直接填写完整端点。</p>
                )}
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1.5">模型</label>
                  <input
                    type="text"
                    value={transcriptionModel}
                    onChange={(event) => setTranscriptionModel(event.target.value)}
                    placeholder="whisper-1"
                    className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-wechat-green"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1.5">语音语言</label>
                  <input
                    type="text"
                    list="transcription-languages"
                    value={transcriptionLanguage}
                    onChange={(event) => setTranscriptionLanguage(event.target.value)}
                    placeholder="auto"
                    className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-wechat-green"
                  />
                  <datalist id="transcription-languages">
                    <option value="auto">自动检测</option>
                    <option value="zh">中文</option>
                    <option value="en">英语</option>
                    <option value="ja">日语</option>
                    <option value="ko">韩语</option>
                    <option value="yue">粤语</option>
                  </datalist>
                </div>
              </div>

              {transcriptionInterfacePreset === 'custom_multipart' && (
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1.5">
                    自定义接口名称
                  </label>
                  <input
                    type="text"
                    value={transcriptionCustomName}
                    onChange={(event) => setTranscriptionCustomName(event.target.value)}
                    placeholder="例如：公司内部语音服务"
                    maxLength={60}
                    className={INPUT_CLASS}
                  />
                </div>
              )}

              {transcriptionInterfacePreset === 'custom_multipart' && (
                <AdvancedSection
                  open={advancedOpen.transcription}
                  onToggle={() => toggleAdvanced('transcription')}
                  title="高级 multipart 参数"
                  description="自定义鉴权头、音频字段、表单字段和响应文本路径。"
                >
                <div className="space-y-3">
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <div>
                      <label className="block text-sm font-medium text-gray-700 mb-1.5">API Key 请求头</label>
                      <input
                        type="text"
                        value={transcriptionApiKeyHeader}
                        onChange={(event) => setTranscriptionApiKeyHeader(event.target.value)}
                        placeholder="Authorization"
                        className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:border-wechat-green"
                      />
                    </div>
                    <div>
                      <label className="block text-sm font-medium text-gray-700 mb-1.5">API Key 前缀</label>
                      <input
                        type="text"
                        value={transcriptionApiKeyPrefix}
                        onChange={(event) => setTranscriptionApiKeyPrefix(event.target.value)}
                        placeholder="Bearer "
                        className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:border-wechat-green"
                      />
                    </div>
                  </div>
                  <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                    <div>
                      <label className="block text-xs font-medium text-gray-700 mb-1.5">音频字段</label>
                      <input
                        type="text"
                        value={transcriptionAudioField}
                        onChange={(event) => setTranscriptionAudioField(event.target.value)}
                        placeholder="file"
                        className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:border-wechat-green"
                      />
                    </div>
                    <div>
                      <label className="block text-xs font-medium text-gray-700 mb-1.5">模型字段（可留空）</label>
                      <input
                        type="text"
                        value={transcriptionModelField}
                        onChange={(event) => setTranscriptionModelField(event.target.value)}
                        placeholder="model"
                        className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:border-wechat-green"
                      />
                    </div>
                    <div>
                      <label className="block text-xs font-medium text-gray-700 mb-1.5">语言字段（可留空）</label>
                      <input
                        type="text"
                        value={transcriptionLanguageField}
                        onChange={(event) => setTranscriptionLanguageField(event.target.value)}
                        placeholder="language"
                        className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:border-wechat-green"
                      />
                    </div>
                  </div>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <div>
                      <label className="block text-sm font-medium text-gray-700 mb-1.5">上传文件名</label>
                      <input
                        type="text"
                        value={transcriptionFilename}
                        onChange={(event) => setTranscriptionFilename(event.target.value)}
                        placeholder="audio.wav"
                        className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:border-wechat-green"
                      />
                    </div>
                    <div>
                      <label className="block text-sm font-medium text-gray-700 mb-1.5">响应文本路径</label>
                      <input
                        type="text"
                        value={transcriptionResponsePath}
                        onChange={(event) => setTranscriptionResponsePath(event.target.value)}
                        placeholder="text"
                        className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:border-wechat-green"
                      />
                    </div>
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1.5">附加请求头 JSON</label>
                    <input
                      type="text"
                      value={transcriptionExtraHeaders}
                      onChange={(event) => setTranscriptionExtraHeaders(event.target.value)}
                      placeholder={'{"X-API-Version":"2026-01-01"}'}
                      spellCheck={false}
                      className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:border-wechat-green"
                    />
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1.5">附加表单字段 JSON</label>
                    <input
                      type="text"
                      value={transcriptionExtraFormFields}
                      onChange={(event) => setTranscriptionExtraFormFields(event.target.value)}
                      placeholder={'{"temperature":"0"}'}
                      spellCheck={false}
                      className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:border-wechat-green"
                    />
                    <p className="text-xs text-gray-400 mt-1">字符串值可使用 {'{{model}}'}、{'{{language}}'}、{'{{filename}}'} 占位符。</p>
                  </div>
                </div>
                </AdvancedSection>
              )}

              <SecretField
                id="transcription-api-key"
                value={transcriptionApiKey}
                onChange={(event) => {
                  transcriptionRevealRequestRef.current += 1;
                  setRevealingTranscriptionApiKey(false);
                  setTranscriptionApiKey(event.target.value);
                  setTranscriptionApiKeyLoaded(false);
                  if (event.target.value) setClearTranscriptionApiKey(false);
                }}
                visible={showTranscriptionApiKey}
                onToggleVisible={handleToggleTranscriptionApiKey}
                revealing={revealingTranscriptionApiKey}
                configured={hasTranscriptionApiKey}
                hint={transcriptionApiKey.trim() && !transcriptionApiKeyLoaded
                  ? ''
                  : settings?.transcription?.api_key_hint}
                clearPending={clearTranscriptionApiKey}
                onToggleClear={() => {
                  transcriptionRevealRequestRef.current += 1;
                  setRevealingTranscriptionApiKey(false);
                  setClearTranscriptionApiKey((value) => !value);
                  setTranscriptionApiKey('');
                  setTranscriptionApiKeyLoaded(false);
                  setShowTranscriptionApiKey(false);
                }}
                canClear={Boolean(settings?.transcription?.has_api_key || clearTranscriptionApiKey)}
                placeholder={hasTranscriptionApiKey ? '已配置，留空则保留原密钥' : '输入语音转写服务的 API Key'}
                optional={!transcriptionRequiresApiKey}
                optionalDescription="当前接口允许免密访问；若服务启用了鉴权，仍可在此填写。"
              />

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1.5">单次任务最多语音数</label>
                <input
                  type="number"
                  min="1"
                  max="500"
                  value={maxVoicesPerTask}
                  onChange={(event) => setMaxVoicesPerTask(event.target.value)}
                  className="w-32 border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-wechat-green"
                />
              </div>

              <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800 leading-relaxed">
                云端模式会上传所选语音。保存配置不会上传；每次点击"语音转文字"后仍需再次明确勾选同意。
              </div>
            </div>
          </section>

          <section className={activeSection === 'analysis' ? 'space-y-4' : 'hidden'}>
            <PanelTitle
              title="内容分析"
              description="聊天记录与朋友圈分析共用此模型连接。"
              status={analysisPreset?.label}
            />
            <div className="rounded-xl border border-gray-200 bg-white p-4 space-y-4 sm:p-5">
              <InterfacePresetSelect
                kind="analysis"
                id="analysis-interface-preset"
                value={analysisInterfacePreset}
                onChange={handleAnalysisInterfacePresetChange}
                customName={analysisCustomName}
              />

              {analysisInterfacePreset === 'custom' && (
                <div className="rounded-lg border border-green-200 bg-green-50 p-3 space-y-3">
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1.5">自定义接口名称</label>
                    <input
                      type="text"
                      value={analysisCustomName}
                      onChange={(event) => setAnalysisCustomName(event.target.value)}
                      placeholder="例如：公司内部分析模型"
                      maxLength={60}
                      className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-wechat-green"
                    />
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1.5">请求格式</label>
                    <select
                      value={analysisCustomProtocol}
                      onChange={(event) => setAnalysisCustomProtocol(event.target.value)}
                      className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm bg-white focus:outline-none focus:border-wechat-green"
                    >
                      <option value="openai_compatible">OpenAI 兼容格式</option>
                      <option value="anthropic">Anthropic Messages 格式</option>
                      <option value="gemini">Google Gemini 格式</option>
                      <option value="custom_json">通用 JSON 模板（完全自定义）</option>
                    </select>
                  </div>
                </div>
              )}

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1.5">
                  {analysisInterfacePreset === 'custom' && analysisCustomProtocol === 'custom_json'
                    ? '完整请求 URL'
                    : 'Base URL'}
                </label>
                <input
                  type="url"
                  value={analysisBaseUrl}
                  onChange={(event) => {
                    analysisRevealRequestRef.current += 1;
                    const discardSavedKey = analysisApiKeyLoaded || revealingAnalysisApiKey;
                    setRevealingAnalysisApiKey(false);
                    setAnalysisBaseUrl(event.target.value);
                    if (discardSavedKey) {
                      setAnalysisApiKey('');
                      setAnalysisApiKeyLoaded(false);
                      setShowAnalysisApiKey(false);
                    }
                  }}
                  placeholder={analysisPreset?.baseUrlPlaceholder || analysisPreset?.baseUrl || 'https://example.com/v1'}
                  className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-wechat-green"
                />
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1.5">模型</label>
                  <input
                    type="text"
                    value={analysisModel}
                    onChange={(event) => setAnalysisModel(event.target.value)}
                    placeholder={analysisPreset?.modelPlaceholder || analysisPreset?.model || 'model-name'}
                    className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-wechat-green"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1.5">请求超时</label>
                  <div className="relative">
                    <input
                      type="number"
                      min="5"
                      max="600"
                      step="1"
                      value={analysisTimeout}
                      onChange={(event) => setAnalysisTimeout(event.target.value)}
                      className="w-full border border-gray-200 rounded-lg px-3 py-2 pr-10 text-sm focus:outline-none focus:border-wechat-green"
                    />
                    <span className="absolute right-3 top-2 text-sm text-gray-400">秒</span>
                  </div>
                </div>
              </div>

              {analysisInterfacePreset === 'custom' && analysisCustomProtocol === 'custom_json' && (
                <AdvancedSection
                  open={advancedOpen.analysis}
                  onToggle={() => toggleAdvanced('analysis')}
                  title="高级 JSON 请求参数"
                  description="自定义鉴权头、请求模板与响应文本路径。"
                >
                <div className="space-y-3">
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <div>
                      <label className="block text-sm font-medium text-gray-700 mb-1.5">API Key 请求头</label>
                      <input
                        type="text"
                        value={analysisApiKeyHeader}
                        onChange={(event) => setAnalysisApiKeyHeader(event.target.value)}
                        placeholder="Authorization"
                        className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:border-wechat-green"
                      />
                    </div>
                    <div>
                      <label className="block text-sm font-medium text-gray-700 mb-1.5">API Key 前缀</label>
                      <input
                        type="text"
                        value={analysisApiKeyPrefix}
                        onChange={(event) => setAnalysisApiKeyPrefix(event.target.value)}
                        placeholder="Bearer "
                        className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:border-wechat-green"
                      />
                    </div>
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1.5">附加请求头 JSON</label>
                    <input
                      type="text"
                      value={analysisExtraHeaders}
                      onChange={(event) => setAnalysisExtraHeaders(event.target.value)}
                      placeholder={'{"X-API-Version":"2026-01-01"}'}
                      spellCheck={false}
                      className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:border-wechat-green"
                    />
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1.5">请求 JSON 模板</label>
                    <textarea
                      value={analysisRequestTemplate}
                      onChange={(event) => setAnalysisRequestTemplate(event.target.value)}
                      rows={7}
                      spellCheck={false}
                      className="w-full border border-gray-200 rounded-lg px-3 py-2 text-xs font-mono focus:outline-none focus:border-wechat-green"
                    />
                    <p className="text-xs text-gray-400 mt-1">
                      可用变量：{'{{model}}'}、{'{{prompt}}'}、{'{{system_prompt}}'}、
                      {'{{max_output_tokens}}'}、{'{{temperature}}'}、
                      {'{{analysis_strength}}'}、{'{{detail_level}}'}、{'{{source_type}}'}。
                    </p>
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1.5">响应文本路径</label>
                    <input
                      type="text"
                      value={analysisResponsePath}
                      onChange={(event) => setAnalysisResponsePath(event.target.value)}
                      placeholder="choices.0.message.content"
                      className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:border-wechat-green"
                    />
                    <p className="text-xs text-gray-400 mt-1">使用点号分隔 JSON 字段，数组下标直接填写数字。</p>
                  </div>
                </div>
                </AdvancedSection>
              )}

              <SecretField
                id="analysis-api-key"
                value={analysisApiKey}
                onChange={(event) => {
                  analysisRevealRequestRef.current += 1;
                  setRevealingAnalysisApiKey(false);
                  setAnalysisApiKey(event.target.value);
                  setAnalysisApiKeyLoaded(false);
                  if (event.target.value) setClearAnalysisApiKey(false);
                }}
                visible={showAnalysisApiKey}
                onToggleVisible={handleToggleAnalysisApiKey}
                revealing={revealingAnalysisApiKey}
                configured={hasAnalysisApiKey}
                hint={analysisApiKey.trim() && !analysisApiKeyLoaded
                  ? ''
                  : settings?.analysis?.api_key_hint}
                clearPending={clearAnalysisApiKey}
                onToggleClear={() => {
                  analysisRevealRequestRef.current += 1;
                  setRevealingAnalysisApiKey(false);
                  setClearAnalysisApiKey((value) => !value);
                  setAnalysisApiKey('');
                  setAnalysisApiKeyLoaded(false);
                  setShowAnalysisApiKey(false);
                }}
                canClear={Boolean(settings?.analysis?.has_api_key || clearAnalysisApiKey)}
                placeholder={hasAnalysisApiKey ? '已配置，留空则保留原密钥' : '输入 AI 分析模型的 API Key'}
                optional={!analysisRequiresApiKey}
                optionalDescription={
                  analysisConnectionChanged && settings?.analysis?.has_api_key
                    ? '当前为本机免密接口；保存时会清除旧服务密钥。若本地服务启用了鉴权，请填写新的 Key。'
                    : '当前使用本机回环地址，API Key 可不填；若本地服务启用了鉴权，仍可在此填写。'
                }
              />

              <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800 leading-relaxed">
                保存配置不会发送任何记录。只有用户主动开始分析并确认范围后，所选聊天或朋友圈文字才会提交给配置的模型接口；媒体文件不会随分析请求上传。
              </div>
            </div>

          </section>

          <section className={activeSection === 'presets' ? 'space-y-4' : 'hidden'}>
            <PanelTitle
              title="分析预设"
              description="为聊天记录和朋友圈分析准备可复用的强度、详细程度与输出要求。"
              status={`${analysisPresets.length} 个预设`}
            />
            <div className="rounded-xl border border-gray-200 bg-white p-4 sm:p-5">
              <div className="flex items-center justify-between gap-3 mb-3">
                <div>
                  <h4 className="text-sm font-bold text-gray-800">分析预设</h4>
                  <p className="mt-0.5 text-xs text-gray-400">预设决定分析强度、输出详细程度和额外要求。</p>
                </div>
                <button
                  type="button"
                  onClick={handleAddAnalysisPreset}
                  disabled={analysisPresets.length >= 50}
                  title={analysisPresets.length >= 50 ? '最多保存 50 个分析预设' : ''}
                  className="flex-shrink-0 rounded-lg border border-wechat-green bg-green-50 px-3 py-2 text-xs font-medium text-wechat-green transition-colors hover:bg-green-100 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  + 新增预设
                </button>
              </div>
              <div className="mb-4 flex gap-2 overflow-x-auto pb-1">
                {analysisPresets.map((preset) => {
                  const active = preset.id === activeAnalysisPreset?.id;
                  return (
                    <button
                      key={preset.id}
                      type="button"
                      onClick={() => setActiveAnalysisPresetId(preset.id)}
                      className={`flex-shrink-0 rounded-lg border px-3 py-2 text-left text-xs transition-colors ${
                        active
                          ? 'border-wechat-green bg-green-50 text-wechat-green'
                          : 'border-gray-200 bg-white text-gray-600 hover:bg-gray-50'
                      }`}
                    >
                      <span className="block max-w-40 truncate font-medium">{preset.name}</span>
                      <span className="mt-0.5 block text-[11px] opacity-70">
                        {ANALYSIS_STRENGTHS[preset.strength]} · {ANALYSIS_DETAILS[preset.detail]}
                      </span>
                    </button>
                  );
                })}
              </div>
              <div>
                {analysisPresets.map((preset, index) => (
                  <div
                    key={preset.id}
                    className={preset.id === activeAnalysisPreset?.id
                      ? 'space-y-3 rounded-lg border border-gray-200 bg-gray-50 p-3 sm:p-4'
                      : 'hidden'}
                  >
                    <div className="flex items-center justify-between gap-3">
                      <span className="text-xs font-medium text-gray-500">预设 {index + 1}</span>
                      <button
                        type="button"
                        onClick={() => handleDeleteAnalysisPreset(preset.id)}
                        disabled={analysisPresets.length <= 1}
                        title={analysisPresets.length <= 1 ? '至少保留一个分析预设' : '删除此预设'}
                        className="text-xs text-red-500 hover:text-red-700 disabled:cursor-not-allowed disabled:text-gray-300"
                      >
                        删除
                      </button>
                    </div>
                    <div>
                      <label className="block text-xs font-medium text-gray-600 mb-1.5">名称</label>
                      <input
                        type="text"
                        value={preset.name}
                        onChange={(event) => handleUpdateAnalysisPreset(
                          preset.id, 'name', event.target.value
                        )}
                        maxLength={60}
                        placeholder="例如：项目复盘"
                        className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-wechat-green"
                      />
                    </div>
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                      <div>
                        <label className="block text-xs font-medium text-gray-600 mb-1.5">分析强度</label>
                        <select
                          value={preset.strength}
                          onChange={(event) => handleUpdateAnalysisPreset(
                            preset.id, 'strength', event.target.value
                          )}
                          className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm bg-white focus:outline-none focus:border-wechat-green"
                        >
                          {Object.entries(ANALYSIS_STRENGTHS).map(([value, label]) => (
                            <option key={value} value={value}>{label}</option>
                          ))}
                        </select>
                      </div>
                      <div>
                        <label className="block text-xs font-medium text-gray-600 mb-1.5">详细程度</label>
                        <select
                          value={preset.detail}
                          onChange={(event) => handleUpdateAnalysisPreset(
                            preset.id, 'detail', event.target.value
                          )}
                          className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm bg-white focus:outline-none focus:border-wechat-green"
                        >
                          {Object.entries(ANALYSIS_DETAILS).map(([value, label]) => (
                            <option key={value} value={value}>{label}</option>
                          ))}
                        </select>
                      </div>
                    </div>
                    <div>
                      <label className="block text-xs font-medium text-gray-600 mb-1.5">附加要求</label>
                      <textarea
                        value={preset.requirements}
                        onChange={(event) => handleUpdateAnalysisPreset(
                          preset.id, 'requirements', event.target.value
                        )}
                        rows={3}
                        maxLength={4000}
                        placeholder="例如：重点关注双方的分歧、承诺和待办事项，并引用关键聊天内容。"
                        className="w-full resize-y border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-wechat-green"
                      />
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </section>

          <section className={activeSection === 'export' ? 'space-y-4' : 'hidden'}>
            <PanelTitle
              title="导出"
              description="设置聊天记录、朋友圈与 AI 报告的默认保存位置。"
            />
            <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm sm:p-5">
              <label className="block text-sm font-medium text-gray-700 mb-1.5">文件保存位置</label>
              <div className="flex flex-col gap-2 sm:flex-row">
                <input
                  type="text"
                  value={exportDir}
                  onChange={(event) => setExportDir(event.target.value)}
                  placeholder="C:\\Users\\...\\Downloads"
                  className="flex-1 min-w-0 border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-wechat-green"
                />
                <button
                  type="button"
                  onClick={handleBrowse}
                  className="flex-shrink-0 rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-600 transition-colors hover:bg-gray-100"
                >
                  浏览...
                </button>
              </div>
            </div>
            <SettingsCard
              title="低频操作"
              description="导出全部聊天会打开范围与格式确认窗口，不会立即写入文件。"
            >
              <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                <div>
                  <p className="text-sm font-medium text-slate-800">导出全部聊天记录</p>
                  <p className="mt-1 text-xs leading-5 text-slate-500">
                    适合完整归档。单个会话请继续使用聊天页面中的导出功能。
                  </p>
                </div>
                <button
                  type="button"
                  onClick={handleExportAllChats}
                  disabled={!onExportAll || busy}
                  className="shrink-0 rounded-xl border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 shadow-sm transition hover:border-emerald-300 hover:bg-emerald-50 hover:text-emerald-700 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  打开全部导出
                </button>
              </div>
              <p className="mt-3 text-xs text-amber-600">
                如有尚未保存的设置，请先点击底部“保存设置”。
              </p>
            </SettingsCard>
          </section>

          <section className={activeSection === 'about' ? 'space-y-4' : 'hidden'}>
            <PanelTitle
              title="关于微信解析助手"
              description="集中维护项目介绍、发布版本、教程入口与作者联系信息。"
              status={`版本 ${desktopRuntime?.appVersion || about.version || '暂未填写'}`}
            />
            <SettingsCard
              title="软件更新"
              description="内测版可由用户手动检查更新；只有你确认后才会下载并重启安装。"
            >
              {isDesktopApp() ? (
                <div className="space-y-3">
                  <div className="flex flex-col gap-3 rounded-xl border border-slate-200 bg-slate-50 p-4 sm:flex-row sm:items-center sm:justify-between">
                    <div className="min-w-0">
                      <p className="text-sm font-medium text-slate-800">
                        当前版本 {desktopRuntime?.appVersion || about.version || '读取中'}
                      </p>
                      <p className="mt-1 text-xs leading-5 text-slate-500">
                        更新通道：{desktopRuntime?.channel || 'beta'} · {desktopRuntime?.platform || 'win32'} {desktopRuntime?.arch || 'x64'}
                      </p>
                      {updateStatus.state === 'available' && (
                        <p className="mt-2 text-xs text-emerald-700">
                          发现新版本 {updateStatus.version || ''}，可在确认后开始下载。
                        </p>
                      )}
                      {updateStatus.state === 'not-available' && (
                        <p className="mt-2 text-xs text-slate-600">当前已经是此通道的最新版本。</p>
                      )}
                      {updateStatus.state === 'downloading' && (
                        <p className="mt-2 text-xs text-blue-700">
                          正在下载更新 {Number.isFinite(Number(updateStatus.percent))
                            ? `${Math.max(0, Math.min(100, Number(updateStatus.percent))).toFixed(1)}%`
                            : ''}
                        </p>
                      )}
                      {updateStatus.state === 'downloaded' && (
                        <p className="mt-2 text-xs text-emerald-700">
                          新版本 {updateStatus.version || ''} 已下载，重启后完成安装。
                        </p>
                      )}
                      {updateStatus.state === 'error' && (
                        <p className="mt-2 break-words text-xs text-red-600">
                          {updateStatus.message || '检查更新失败，请稍后重试。'}
                        </p>
                      )}
                      {updateStatus.state === 'disabled' && (
                        <p className="mt-2 break-words text-xs text-amber-700">
                          {updateStatus.message || '当前内测包尚未配置在线更新服务。'}
                        </p>
                      )}
                    </div>
                    <button
                      type="button"
                      disabled={
                        updateActionBusy
                        || updateStatus.state === 'checking'
                        || updateStatus.state === 'downloading'
                        || updateStatus.state === 'disabled'
                        || desktopRuntime?.isPackaged === false
                      }
                      onClick={() => handleDesktopUpdateAction(
                        updateStatus.state === 'available'
                          ? 'download'
                          : updateStatus.state === 'downloaded'
                            ? 'install'
                            : 'check'
                      )}
                      className="shrink-0 rounded-xl bg-emerald-600 px-4 py-2 text-sm font-medium text-white shadow-sm transition hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {updateStatus.state === 'checking'
                        ? '检查中…'
                        : updateStatus.state === 'downloading'
                          ? '下载中…'
                          : updateStatus.state === 'available'
                            ? '下载更新'
                            : updateStatus.state === 'downloaded'
                              ? '重启并安装'
                              : '检查更新'}
                    </button>
                  </div>
                  {desktopRuntime?.isPackaged === false && (
                    <p className="text-xs leading-5 text-amber-600">
                      当前为桌面开发模式；自动更新只在安装后的内测版本中启用。
                    </p>
                  )}
                  {updateStatus.releaseNotes && (
                    <div className="rounded-xl border border-slate-200 bg-white p-3">
                      <p className="text-xs font-medium text-slate-700">更新说明</p>
                      <p className="mt-1 whitespace-pre-wrap text-xs leading-5 text-slate-500">
                        {Array.isArray(updateStatus.releaseNotes)
                          ? updateStatus.releaseNotes.map((item) => item?.note || item).join('\n')
                          : String(updateStatus.releaseNotes)}
                      </p>
                    </div>
                  )}
                </div>
              ) : (
                <p className="text-sm leading-6 text-slate-600">
                  当前使用浏览器开发模式。安装 Electron 内测版后，这里会显示版本、更新通道、下载进度和重启安装按钮。
                </p>
              )}
            </SettingsCard>
            <SettingsCard title="项目信息" description="这些内容会显示在本机的“关于”页面。">
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <div>
                  <label className="mb-1.5 block text-sm font-medium text-slate-700">项目名称</label>
                  <input
                    value={about.project_name}
                    maxLength={80}
                    onChange={(event) => setAbout((current) => ({ ...current, project_name: event.target.value }))}
                    className={INPUT_CLASS}
                  />
                </div>
                <div>
                  <label className="mb-1.5 block text-sm font-medium text-slate-700">版本</label>
                  <input
                    value={about.version}
                    maxLength={32}
                    onChange={(event) => setAbout((current) => ({ ...current, version: event.target.value }))}
                    className={INPUT_CLASS}
                  />
                </div>
                <div className="sm:col-span-2">
                  <label className="mb-1.5 block text-sm font-medium text-slate-700">项目介绍</label>
                  <textarea
                    value={about.description}
                    maxLength={2000}
                    rows={3}
                    onChange={(event) => setAbout((current) => ({ ...current, description: event.target.value }))}
                    className={`${INPUT_CLASS} resize-y`}
                  />
                </div>
              </div>
            </SettingsCard>

            <SettingsCard title="快速教程" description="内置说明始终可用；也可以补充公开教程链接。">
              <ol className="mb-4 grid gap-2 text-sm leading-6 text-slate-600 sm:grid-cols-3">
                <li className="rounded-xl bg-slate-50 p-3"><b className="text-slate-800">1.</b> 选择微信 4.x 账号并解密数据库。</li>
                <li className="rounded-xl bg-slate-50 p-3"><b className="text-slate-800">2.</b> 在聊天、联系人或朋友圈中预览并选择范围。</li>
                <li className="rounded-xl bg-slate-50 p-3"><b className="text-slate-800">3.</b> 按需加载媒体、AI 分析或导出本地报告。</li>
              </ol>
              <label className="mb-1.5 block text-sm font-medium text-slate-700">教程链接（可选）</label>
              <input
                type="url"
                value={about.tutorial_url}
                maxLength={2048}
                placeholder="https://..."
                onChange={(event) => setAbout((current) => ({ ...current, tutorial_url: event.target.value }))}
                className={INPUT_CLASS}
              />
            </SettingsCard>

            <SettingsCard title="作者与联系" description="未填写的项目会明确显示“暂未填写”，不会发布虚构信息。">
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                {[
                  ['author', '作者（你自己）', 100],
                  ['qq_group', 'QQ群', 80],
                  ['contact', '联系方式', 500],
                ].map(([field, label, maxLength]) => (
                  <div key={field} className={field === 'contact' ? 'sm:col-span-2' : ''}>
                    <label className="mb-1.5 block text-sm font-medium text-slate-700">{label}</label>
                    <input
                      value={about[field]}
                      maxLength={maxLength}
                      placeholder="暂未填写"
                      onChange={(event) => setAbout((current) => ({ ...current, [field]: event.target.value }))}
                      className={INPUT_CLASS}
                    />
                  </div>
                ))}
              </div>
            </SettingsCard>

            <div className="rounded-xl border border-emerald-200 bg-emerald-50/70 p-4 text-xs leading-6 text-emerald-900">
              本项目面向微信 4.x，本地解析所得聊天、媒体和密钥默认不离开电脑；只有用户主动点击 AI
              识别、转写或分析时，所选内容才会发送到已配置的第三方模型接口。
            </div>
          </section>
          </div>
        </div>

        <div className="flex flex-shrink-0 flex-col gap-3 border-t border-slate-200 bg-white px-4 py-3 sm:flex-row sm:items-center sm:justify-between sm:px-6 sm:py-4">
          <div aria-live="polite" className="min-w-0 text-sm">
            {message && (
              <span className={
                message.includes('失败')
                  ? 'text-red-500'
                  : message.startsWith('安全提示')
                    ? 'text-amber-600'
                    : 'text-green-600'
              }>
                {message}
              </span>
            )}
          </div>
          <div className="flex flex-shrink-0 justify-end gap-3">
            <button
              type="button"
              onClick={onClose}
              disabled={busy}
              className="rounded-xl px-4 py-2 text-sm font-medium text-slate-600 hover:bg-slate-100 hover:text-slate-900 disabled:opacity-50"
            >
              关闭
            </button>
            <button
              type="button"
              onClick={handleSave}
              disabled={busy}
              className="flex items-center gap-2 rounded-xl bg-emerald-600 px-6 py-2 text-sm font-semibold text-white shadow-sm transition hover:bg-emerald-700 disabled:opacity-50"
            >
              {saving && <span className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />}
              {saving ? '保存中...' : '保存设置'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
