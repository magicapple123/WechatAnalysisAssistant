import assert from 'node:assert/strict';
import test from 'node:test';

import {
  getInterfaceLabel,
  getInterfacePreset,
  groupedInterfacePresets,
  interfaceRequiresApiKey,
  resolveInterfacePreset,
} from './modelInterfaces.js';

test('restores explicit and legacy interface presets', () => {
  assert.equal(resolveInterfacePreset('analysis', { interface_preset: 'deepseek' }), 'deepseek');
  assert.equal(resolveInterfacePreset('analysis', { provider: 'anthropic' }), 'anthropic');
  assert.equal(resolveInterfacePreset('analysis', { provider: 'custom_json' }), 'custom');
  assert.equal(resolveInterfacePreset('transcription', { provider: 'dashscope_asr' }), 'dashscope_asr');
  assert.equal(resolveInterfacePreset('analysis', {
    provider: 'openai_compatible', base_url: 'https://api.deepseek.com/v1',
  }), 'deepseek');
  assert.equal(resolveInterfacePreset('analysis', {
    provider: 'openai_compatible',
    base_url: 'https://example.services.ai.azure.com/openai/v1',
  }), 'azure_openai');
  assert.equal(resolveInterfacePreset('analysis', {
    provider: 'openai_compatible', base_url: 'https://gateway.example.com/v1',
  }), 'openai_compatible');
  assert.equal(resolveInterfacePreset('transcription', {
    provider: 'openai_compatible',
    base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
  }), 'openai_compatible');
});

test('groups common interfaces and keeps custom option last', () => {
  const groups = groupedInterfacePresets('analysis');
  assert.ok(groups.length >= 4);
  assert.equal(groups.at(-1).presets.at(-1).id, 'custom');
});

test('local model presets are optional-key only on real loopback URLs', () => {
  assert.equal(interfaceRequiresApiKey('analysis', 'ollama', 'http://127.0.0.1:11434/v1'), false);
  assert.equal(interfaceRequiresApiKey('analysis', 'lm_studio', 'http://localhost:1234/v1'), false);
  assert.equal(interfaceRequiresApiKey('analysis', 'lm_studio', 'http://[::1]:1234/v1'), false);
  assert.equal(interfaceRequiresApiKey('analysis', 'ollama', 'https://ollama.example.com/v1'), true);
  assert.equal(interfaceRequiresApiKey('analysis', 'ollama', 'https://127.example.com/v1'), true);
  assert.equal(interfaceRequiresApiKey('analysis', 'deepseek', 'https://api.deepseek.com/v1'), true);
});

test('labels custom interfaces and exposes provider defaults', () => {
  assert.equal(getInterfaceLabel('analysis', { provider: 'custom', custom_name: '内部模型' }), '内部模型');
  assert.equal(
    getInterfaceLabel('transcription', {
      provider: 'custom_multipart',
      custom_name: '公司语音网关',
    }),
    '公司语音网关',
  );
  assert.equal(
    getInterfaceLabel('analysis', {
      provider: 'openai_compatible',
      interface_preset: 'deepseek',
    }),
    'DeepSeek',
  );
  assert.equal(
    getInterfaceLabel('vision', {
      provider: 'openai_compatible',
      interface_preset: 'ollama',
    }),
    'Ollama（本机）',
  );
  assert.equal(getInterfacePreset('vision', 'siliconflow').model, 'zai-org/GLM-4.6V');
  assert.equal(getInterfacePreset('analysis', 'lm_studio').apiKeyRequired, false);
  assert.equal(getInterfacePreset('transcription', 'groq').model, 'whisper-large-v3-turbo');
});
