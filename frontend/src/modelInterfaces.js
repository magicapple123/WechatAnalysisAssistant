const GROUP_ORDER = ['官方原生', '国内云服务', '聚合与高速服务', '本地与兼容', '自定义'];

export const MODEL_INTERFACE_PRESETS = {
  vision: [
    {
      id: 'openai', label: 'OpenAI', group: '官方原生', protocol: 'openai_compatible',
      protocolLabel: 'OpenAI Chat Completions', baseUrl: 'https://api.openai.com/v1',
      model: 'gpt-5.4-mini', capabilities: ['图片', '文字'],
      description: 'OpenAI 官方视觉模型接口。',
    },
    {
      id: 'anthropic', label: 'Anthropic Claude', group: '官方原生', protocol: 'anthropic',
      protocolLabel: 'Anthropic Messages', baseUrl: 'https://api.anthropic.com/v1',
      model: 'claude-sonnet-4-5', capabilities: ['图片', '文字'],
      description: '使用 Anthropic 原生 Messages 协议。',
    },
    {
      id: 'gemini', label: 'Google Gemini', group: '官方原生', protocol: 'gemini',
      protocolLabel: 'Gemini generateContent', baseUrl: 'https://generativelanguage.googleapis.com/v1beta',
      model: 'gemini-2.5-flash', capabilities: ['图片', '文字'],
      description: '使用 Google Gemini 原生 generateContent 协议。',
    },
    {
      id: 'azure_openai', label: 'Azure OpenAI / Microsoft Foundry', group: '官方原生',
      protocol: 'openai_compatible', protocolLabel: 'OpenAI Chat Completions（api-key 鉴权）',
      baseUrl: '', model: '', capabilities: ['图片', '文字'],
      baseUrlPlaceholder: 'https://你的资源名.openai.azure.com/openai/v1',
      modelPlaceholder: '填写 Azure 部署名称',
      description: '使用 Azure v1 端点；模型字段填写部署名称，程序会使用 api-key 请求头。',
    },
    {
      id: 'dashscope', label: '阿里云百炼 / 通义千问', group: '国内云服务',
      protocol: 'openai_compatible', protocolLabel: 'OpenAI Chat Completions',
      baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen3-vl-flash',
      capabilities: ['图片', '文字'], description: '适用于 Qwen-VL 等支持视觉输入的百炼模型。',
    },
    {
      id: 'kimi', label: 'Moonshot / Kimi', group: '国内云服务',
      protocol: 'openai_compatible', protocolLabel: 'OpenAI Chat Completions',
      baseUrl: 'https://api.moonshot.cn/v1', model: 'kimi-k2.6', capabilities: ['图片', '文字'],
      description: 'Kimi 开放平台中国区接口；境外账号可手动改为 moonshot.ai。',
    },
    {
      id: 'zhipu', label: '智谱 BigModel / GLM', group: '国内云服务',
      protocol: 'openai_compatible', protocolLabel: 'OpenAI Chat Completions',
      baseUrl: 'https://open.bigmodel.cn/api/paas/v4', model: '', capabilities: ['图片', '文字'],
      modelPlaceholder: '填写支持视觉的 GLM-V 模型',
      description: '请选择当前账号可用的 GLM-V 视觉模型。',
    },
    {
      id: 'volcengine', label: '火山方舟 / 豆包', group: '国内云服务',
      protocol: 'openai_compatible', protocolLabel: 'OpenAI Chat Completions',
      baseUrl: 'https://ark.cn-beijing.volces.com/api/v3', model: 'doubao-seed-2-0-lite-260215',
      capabilities: ['图片', '文字'], description: '模型名称也可能是控制台创建的 ep- 开头推理接入点。',
    },
    {
      id: 'tencent_tokenhub', label: '腾讯云 TokenHub', group: '国内云服务',
      protocol: 'openai_compatible', protocolLabel: 'OpenAI Chat Completions',
      baseUrl: 'https://tokenhub.tencentmaas.com/v1', model: 'hy-vision-2.0-instruct',
      capabilities: ['图片', '文字'], description: '腾讯云新一代统一模型网关，支持多模态理解模型。',
    },
    {
      id: 'siliconflow', label: '硅基流动 SiliconFlow', group: '国内云服务',
      protocol: 'openai_compatible', protocolLabel: 'OpenAI Chat Completions',
      baseUrl: 'https://api.siliconflow.cn/v1', model: 'zai-org/GLM-4.6V',
      capabilities: ['图片', '文字'], description: '硅基流动兼容接口；模型可替换为当前可用的视觉模型 ID。',
    },
    {
      id: 'openrouter', label: 'OpenRouter', group: '聚合与高速服务',
      protocol: 'openai_compatible', protocolLabel: 'OpenAI Chat Completions',
      baseUrl: 'https://openrouter.ai/api/v1', model: 'google/gemini-2.5-flash',
      capabilities: ['图片', '文字'], description: '统一接入多家视觉模型；需填写支持图片输入的模型 ID。',
    },
    {
      id: 'ollama', label: 'Ollama（本机）', group: '本地与兼容',
      protocol: 'openai_compatible', protocolLabel: 'OpenAI Chat Completions（本机免密）',
      baseUrl: 'http://127.0.0.1:11434/v1', model: 'qwen3-vl:4b', apiKeyRequired: false,
      capabilities: ['图片', '文字', '本机'], description: '仅本机回环地址允许不填写 API Key；模型需已在 Ollama 中安装。',
    },
    {
      id: 'lm_studio', label: 'LM Studio（本机）', group: '本地与兼容',
      protocol: 'openai_compatible', protocolLabel: 'OpenAI Chat Completions（本机可免密）',
      baseUrl: 'http://127.0.0.1:1234/v1', model: '', apiKeyRequired: false,
      modelPlaceholder: '填写 LM Studio 中已加载的视觉模型 ID',
      capabilities: ['图片', '文字', '本机'], description: '默认本机服务可不填密钥；启用 API Token 后仍可填写。',
    },
    {
      id: 'openai_compatible', label: '其他 OpenAI 兼容接口', group: '本地与兼容',
      protocol: 'openai_compatible', protocolLabel: 'OpenAI Chat Completions',
      baseUrl: '', model: '', capabilities: ['图片', '文字'],
      baseUrlPlaceholder: 'https://example.com/v1', modelPlaceholder: 'vision-model',
      description: '适用于其他实现 /chat/completions 且支持 image_url 的服务。',
    },
    {
      id: 'custom', label: '+ 完全自定义 JSON 接口', group: '自定义', protocol: 'custom',
      protocolLabel: '自定义协议', baseUrl: '', model: '', capabilities: ['自定义'],
      description: '自定义鉴权头、JSON 请求模板和响应文本路径。',
    },
  ],
  transcription: [
    {
      id: 'openai', label: 'OpenAI', group: '官方原生', protocol: 'openai_compatible',
      protocolLabel: 'OpenAI Audio Transcriptions', baseUrl: 'https://api.openai.com/v1',
      model: 'whisper-1', capabilities: ['语音'], description: 'OpenAI 官方 /audio/transcriptions 接口。',
    },
    {
      id: 'azure_openai', label: 'Azure OpenAI / Microsoft Foundry', group: '官方原生',
      protocol: 'openai_compatible', protocolLabel: 'OpenAI Audio Transcriptions（api-key 鉴权）',
      baseUrl: '', model: '', capabilities: ['语音'],
      baseUrlPlaceholder: 'https://你的资源名.openai.azure.com/openai/v1',
      modelPlaceholder: '填写音频模型部署名称',
      description: '部署需支持音频转写；程序会使用 api-key 请求头。',
    },
    {
      id: 'dashscope_asr', label: '阿里云百炼 Qwen-ASR', group: '国内云服务',
      protocol: 'dashscope_asr', protocolLabel: 'DashScope Chat Completions ASR',
      baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen3-asr-flash',
      capabilities: ['语音', '方言'], description: '支持多语种与多种中文方言，音频以 input_audio 提交。',
    },
    {
      id: 'groq', label: 'Groq Whisper', group: '聚合与高速服务',
      protocol: 'openai_compatible', protocolLabel: 'OpenAI Audio Transcriptions',
      baseUrl: 'https://api.groq.com/openai/v1', model: 'whisper-large-v3-turbo',
      capabilities: ['语音', '高速'], description: 'Groq 官方 OpenAI 兼容语音转写端点。',
    },
    {
      id: 'mistral', label: 'Mistral Voxtral', group: '聚合与高速服务',
      protocol: 'openai_compatible', protocolLabel: 'OpenAI Audio Transcriptions',
      baseUrl: 'https://api.mistral.ai/v1', model: 'voxtral-mini-latest',
      capabilities: ['语音'], description: 'Mistral Voxtral 同步音频转写接口。',
    },
    {
      id: 'openai_compatible', label: '其他 OpenAI 兼容转写接口', group: '本地与兼容',
      protocol: 'openai_compatible', protocolLabel: 'OpenAI Audio Transcriptions',
      baseUrl: '', model: '', capabilities: ['语音'],
      baseUrlPlaceholder: 'https://example.com/v1', modelPlaceholder: 'transcription-model',
      description: '适用于兼容 /audio/transcriptions 的同步 multipart 服务。',
    },
    {
      id: 'custom_multipart', label: '+ 完全自定义 multipart 接口', group: '自定义',
      protocol: 'custom_multipart', protocolLabel: '自定义 multipart', baseUrl: '', model: '',
      capabilities: ['自定义'], description: '自定义鉴权头、表单字段和响应文本路径。',
    },
  ],
  analysis: [
    {
      id: 'openai', label: 'OpenAI', group: '官方原生', protocol: 'openai_compatible',
      protocolLabel: 'OpenAI Chat Completions', baseUrl: 'https://api.openai.com/v1',
      model: 'gpt-5.4-mini', capabilities: ['文字'], description: 'OpenAI 官方文本分析接口。',
    },
    {
      id: 'anthropic', label: 'Anthropic Claude', group: '官方原生', protocol: 'anthropic',
      protocolLabel: 'Anthropic Messages', baseUrl: 'https://api.anthropic.com/v1',
      model: 'claude-sonnet-4-5', capabilities: ['文字'], description: '使用 Anthropic 原生 Messages 协议。',
    },
    {
      id: 'gemini', label: 'Google Gemini', group: '官方原生', protocol: 'gemini',
      protocolLabel: 'Gemini generateContent', baseUrl: 'https://generativelanguage.googleapis.com/v1beta',
      model: 'gemini-2.5-flash', capabilities: ['文字'], description: '使用 Google Gemini 原生 generateContent 协议。',
    },
    {
      id: 'azure_openai', label: 'Azure OpenAI / Microsoft Foundry', group: '官方原生',
      protocol: 'openai_compatible', protocolLabel: 'OpenAI Chat Completions（api-key 鉴权）',
      baseUrl: '', model: '', capabilities: ['文字'],
      baseUrlPlaceholder: 'https://你的资源名.openai.azure.com/openai/v1',
      modelPlaceholder: '填写 Azure 部署名称',
      description: '使用 Azure v1 端点；模型字段填写部署名称。',
    },
    {
      id: 'deepseek', label: 'DeepSeek', group: '国内云服务', protocol: 'openai_compatible',
      protocolLabel: 'OpenAI Chat Completions', baseUrl: 'https://api.deepseek.com/v1',
      model: 'deepseek-chat', capabilities: ['文字'], description: 'DeepSeek 官方兼容接口；不用于图片识别。',
    },
    {
      id: 'dashscope', label: '阿里云百炼 / 通义千问', group: '国内云服务',
      protocol: 'openai_compatible', protocolLabel: 'OpenAI Chat Completions',
      baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-plus',
      capabilities: ['文字'], description: '阿里云百炼中国区公共兼容端点，工作空间 URL 仍可手动修改。',
    },
    {
      id: 'kimi', label: 'Moonshot / Kimi', group: '国内云服务', protocol: 'openai_compatible',
      protocolLabel: 'OpenAI Chat Completions', baseUrl: 'https://api.moonshot.cn/v1',
      model: 'kimi-k2.6', capabilities: ['文字'], description: 'Kimi 开放平台中国区接口。',
    },
    {
      id: 'zhipu', label: '智谱 BigModel / GLM', group: '国内云服务',
      protocol: 'openai_compatible', protocolLabel: 'OpenAI Chat Completions',
      baseUrl: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-5.2',
      capabilities: ['文字'], description: '智谱 OpenAI 兼容接口。',
    },
    {
      id: 'volcengine', label: '火山方舟 / 豆包', group: '国内云服务',
      protocol: 'openai_compatible', protocolLabel: 'OpenAI Chat Completions',
      baseUrl: 'https://ark.cn-beijing.volces.com/api/v3', model: 'doubao-seed-2-0-lite-260215',
      capabilities: ['文字'], description: '也可填写控制台创建的 ep- 开头推理接入点。',
    },
    {
      id: 'baidu_qianfan', label: '百度智能云千帆', group: '国内云服务',
      protocol: 'openai_compatible', protocolLabel: 'OpenAI Chat Completions',
      baseUrl: 'https://qianfan.baidubce.com/v2', model: '', capabilities: ['文字'],
      modelPlaceholder: '填写千帆模型或应用服务 ID', description: '千帆 V2 OpenAI 兼容端点。',
    },
    {
      id: 'tencent_tokenhub', label: '腾讯云 TokenHub', group: '国内云服务',
      protocol: 'openai_compatible', protocolLabel: 'OpenAI Chat Completions',
      baseUrl: 'https://tokenhub.tencentmaas.com/v1', model: 'hy3-preview',
      capabilities: ['文字'], description: '腾讯云统一模型网关，可在控制台查询当前可用模型 ID。',
    },
    {
      id: 'siliconflow', label: '硅基流动 SiliconFlow', group: '国内云服务',
      protocol: 'openai_compatible', protocolLabel: 'OpenAI Chat Completions',
      baseUrl: 'https://api.siliconflow.cn/v1', model: 'Pro/zai-org/GLM-4.7',
      capabilities: ['文字', '多模型'], description: '硅基流动 OpenAI 兼容接口，可替换为账号当前可用的模型 ID。',
    },
    {
      id: 'openrouter', label: 'OpenRouter', group: '聚合与高速服务',
      protocol: 'openai_compatible', protocolLabel: 'OpenAI Chat Completions',
      baseUrl: 'https://openrouter.ai/api/v1', model: 'google/gemini-2.5-flash',
      capabilities: ['文字', '多模型'], description: '使用一个统一接口访问多家模型。',
    },
    {
      id: 'groq', label: 'Groq', group: '聚合与高速服务', protocol: 'openai_compatible',
      protocolLabel: 'OpenAI Chat Completions', baseUrl: 'https://api.groq.com/openai/v1',
      model: 'openai/gpt-oss-120b', capabilities: ['文字', '高速'], description: 'Groq 官方 OpenAI 兼容推理接口。',
    },
    {
      id: 'ollama', label: 'Ollama（本机）', group: '本地与兼容', protocol: 'openai_compatible',
      protocolLabel: 'OpenAI Chat Completions（本机免密）', baseUrl: 'http://127.0.0.1:11434/v1',
      model: 'qwen3:8b', apiKeyRequired: false, capabilities: ['文字', '本机'],
      description: '仅本机回环地址允许不填写 API Key；模型需已安装。',
    },
    {
      id: 'lm_studio', label: 'LM Studio（本机）', group: '本地与兼容', protocol: 'openai_compatible',
      protocolLabel: 'OpenAI Chat Completions（本机可免密）', baseUrl: 'http://127.0.0.1:1234/v1',
      model: '', modelPlaceholder: '填写 LM Studio 中已加载的模型 ID', apiKeyRequired: false,
      capabilities: ['文字', '本机'], description: '默认本机服务可不填密钥；启用 API Token 后仍可填写。',
    },
    {
      id: 'openai_compatible', label: '其他 OpenAI 兼容接口', group: '本地与兼容',
      protocol: 'openai_compatible', protocolLabel: 'OpenAI Chat Completions', baseUrl: '', model: '',
      capabilities: ['文字'], baseUrlPlaceholder: 'https://example.com/v1', modelPlaceholder: 'model-name',
      description: '适用于其他同步返回 OpenAI Chat Completions JSON 的服务。',
    },
    {
      id: 'custom', label: '+ 完全自定义 JSON 接口', group: '自定义', protocol: 'custom',
      protocolLabel: '自定义协议', baseUrl: '', model: '', capabilities: ['自定义'],
      description: '自定义鉴权头、JSON 请求模板和响应文本路径。',
    },
  ],
};

export function getInterfacePresets(module) {
  return MODEL_INTERFACE_PRESETS[module] || [];
}

export function getInterfacePreset(module, id) {
  const presets = getInterfacePresets(module);
  return presets.find((preset) => preset.id === id) || presets[0];
}

function inferCompatiblePreset(module, baseUrl) {
  let parsed;
  try {
    parsed = new URL(String(baseUrl || ''));
  } catch {
    return '';
  }
  const host = parsed.hostname.toLowerCase().replace(/^\[|\]$/g, '');
  const port = parsed.port;
  if (host === 'api.openai.com') return 'openai';
  if (
    host.endsWith('.openai.azure.com')
    || host.endsWith('.services.ai.azure.com')
    || host.endsWith('.cognitiveservices.azure.com')
  ) return 'azure_openai';
  if (host === 'dashscope.aliyuncs.com') {
    // Qwen-ASR uses a dedicated wire protocol. A legacy generic multipart
    // configuration must remain generic unless its saved provider says ASR.
    return module === 'transcription' ? '' : 'dashscope';
  }
  if (host === 'api.moonshot.cn' || host === 'api.moonshot.ai') return 'kimi';
  if (host === 'open.bigmodel.cn') return 'zhipu';
  if (host.endsWith('.volces.com')) return 'volcengine';
  if (host === 'qianfan.baidubce.com' && module === 'analysis') return 'baidu_qianfan';
  if (host === 'tokenhub.tencentmaas.com') return 'tencent_tokenhub';
  if (host === 'api.siliconflow.cn' || host === 'api.siliconflow.com') return 'siliconflow';
  if (host === 'openrouter.ai') return 'openrouter';
  if (host === 'api.groq.com') return 'groq';
  if (host === 'api.mistral.ai' && module === 'transcription') return 'mistral';
  if (host === 'api.deepseek.com' && module === 'analysis') return 'deepseek';
  const loopback = host === 'localhost' || host === '::1' || /^127(?:\.\d{1,3}){3}$/.test(host);
  if (loopback && port === '11434' && module !== 'transcription') return 'ollama';
  if (loopback && port === '1234' && module !== 'transcription') return 'lm_studio';
  return '';
}

export function resolveInterfacePreset(module, settings = {}) {
  const presets = getInterfacePresets(module);
  const saved = String(settings?.interface_preset || '').trim();
  if (presets.some((preset) => preset.id === saved)) return saved;

  const provider = String(settings?.provider || '').trim();
  const legacy = {
    vision: { anthropic: 'anthropic', gemini: 'gemini', custom: 'custom' },
    transcription: {
      dashscope_asr: 'dashscope_asr', custom: 'custom_multipart',
      custom_multipart: 'custom_multipart',
    },
    analysis: {
      anthropic: 'anthropic', gemini: 'gemini', custom: 'custom', custom_json: 'custom',
    },
  };
  if (legacy[module]?.[provider]) return legacy[module][provider];
  if (!provider || provider === 'openai_compatible') {
    const inferred = inferCompatiblePreset(module, settings?.base_url);
    if (presets.some((preset) => preset.id === inferred)) return inferred;
    return provider ? 'openai_compatible' : 'openai';
  }
  return 'openai';
}

export function groupedInterfacePresets(module) {
  const presets = getInterfacePresets(module);
  return GROUP_ORDER.map((group) => ({
    group,
    presets: presets.filter((preset) => preset.group === group),
  })).filter((item) => item.presets.length > 0);
}

export function getInterfaceLabel(module, settings = {}) {
  if (settings?.provider === 'custom' && settings?.custom_name) return settings.custom_name;
  if (settings?.provider === 'custom_multipart' && settings?.custom_name) return settings.custom_name;
  return getInterfacePreset(module, resolveInterfacePreset(module, settings))?.label || '模型接口';
}

export function interfaceRequiresApiKey(module, presetId, baseUrl = '') {
  const preset = getInterfacePreset(module, presetId);
  if (preset?.apiKeyRequired !== false) return true;
  try {
    const hostname = new URL(baseUrl || preset.baseUrl).hostname
      .toLowerCase()
      .replace(/^\[|\]$/g, '');
    const loopback = (
      hostname === 'localhost'
      || hostname === '::1'
      || /^127(?:\.\d{1,3}){3}$/.test(hostname)
    );
    return !loopback;
  } catch {
    return true;
  }
}
