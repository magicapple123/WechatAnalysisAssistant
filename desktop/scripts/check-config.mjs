import assert from 'node:assert/strict';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const projectRoot = path.resolve(desktopRoot, '..');
const packageJson = JSON.parse(readFileSync(path.join(desktopRoot, 'package.json'), 'utf8'));
const packageLock = JSON.parse(readFileSync(path.join(desktopRoot, 'package-lock.json'), 'utf8'));
const frontendPackage = JSON.parse(readFileSync(path.join(projectRoot, 'frontend', 'package.json'), 'utf8'));
const backendVersionSource = readFileSync(path.join(projectRoot, 'backend', 'version.py'), 'utf8');
const backendVersion = backendVersionSource.match(/^APP_VERSION\s*=\s*["']([^"']+)["']/m)?.[1];

assert.equal(packageJson.main, 'src/main.mjs');
assert.equal(packageLock.version, packageJson.version, 'Desktop lockfile version is stale');
assert.equal(packageLock.packages?.['']?.version, packageJson.version, 'Desktop lockfile root version is stale');
assert.equal(backendVersion, packageJson.version, 'Backend and desktop application versions differ');
assert.equal(frontendPackage.version, packageJson.version, 'Frontend and desktop application versions differ');
assert.match(packageJson.engines?.node || '', /^>=20\.19\.0$/, 'Supported Node.js version is not declared');
assert.equal(packageJson.build.asar, true, 'Application code must be packaged in app.asar');
assert.equal(packageJson.build.directories.output, 'dist-electron');
assert.equal(packageJson.build.electronDist, 'node_modules/electron/dist');
assert.ok(
  existsSync(path.join(desktopRoot, packageJson.build.electronDist, 'electron.exe')),
  'Electron runtime is missing; run npm ci without --ignore-scripts',
);
assert.equal(packageJson.build.win.requestedExecutionLevel, 'asInvoker');
assert.ok(packageJson.build.win.icon, 'Windows application icon is not configured');
assert.ok(existsSync(path.join(desktopRoot, packageJson.build.win.icon)), 'Windows application icon is missing');
assert.equal(packageJson.build.nsis.oneClick, false);
assert.ok(packageJson.build.extraResources.some(
  (entry) => entry.from === '../frontend/dist' && entry.to === 'frontend',
));
assert.ok(packageJson.build.extraResources.some(
  (entry) => entry.to === 'backend/WechatAnalysisAssistantBackend',
));
assert.deepEqual(packageJson.build.electronFuses, {
  runAsNode: false,
  enableCookieEncryption: true,
  enableNodeOptionsEnvironmentVariable: false,
  enableNodeCliInspectArguments: false,
  enableEmbeddedAsarIntegrityValidation: true,
  onlyLoadAppFromAsar: true,
  grantFileProtocolExtraPrivileges: false,
});
assert.deepEqual(packageJson.build.publish, [{
  provider: 'github',
  owner: 'Magicapple-Coder',
  repo: 'WechatAnalysisAssistant',
}]);
assert.equal(packageJson.overrides?.['js-yaml'], '4.3.2');
assert.equal(packageLock.packages?.['node_modules/js-yaml']?.version, '4.3.2');
assert.equal(packageJson.devDependencies?.['@electron/fuses'], '1.8.0');
assert.equal(packageLock.packages?.['node_modules/@electron/fuses']?.version, '1.8.0');
const obsoleteFrontendRoot = path.join(desktopRoot, 'resources', 'frontend');
assert.equal(
  existsSync(obsoleteFrontendRoot)
    ? readdirSync(obsoleteFrontendRoot, { recursive: true, withFileTypes: true })
      .filter((entry) => !entry.isDirectory()).length
    : 0,
  0,
  'Generated frontend assets must come from frontend/dist, not desktop/resources/frontend',
);
for (const required of [
  'src/main.mjs',
  'src/preload.cjs',
  'src/backend-manager.mjs',
  'scripts/check-fuses.mjs',
]) {
  assert.ok(existsSync(path.join(desktopRoot, required)), `Missing ${required}`);
}

console.log('Electron desktop configuration is internally consistent.');
