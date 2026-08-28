import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import path from 'node:path';
import electronFuses from '@electron/fuses';

const {
  FuseV1Options,
  FuseVersion,
  getCurrentFuseWire,
} = electronFuses;

// getCurrentFuseWire returns the bytes stored in Electron's fuse wire. The
// public package does not export these state constants at runtime.
const DISABLED_FUSE_STATE = '0'.charCodeAt(0);
const ENABLED_FUSE_STATE = '1'.charCodeAt(0);
const REMOVED_FUSE_STATE = 'r'.charCodeAt(0);
const INHERITED_FUSE_STATE = 0x90;

const executableArgument = process.argv[2];
assert.ok(
  executableArgument,
  'Usage: npm run verify:fuses -- <path-to-packaged-electron-exe>',
);

const executablePath = path.resolve(executableArgument);
assert.ok(existsSync(executablePath), `Packaged Electron executable is missing: ${executablePath}`);

const fuseWire = await getCurrentFuseWire(executablePath);
assert.equal(fuseWire.version, FuseVersion.V1, 'Packaged Electron uses an unexpected fuse version');

// Reading the packaged executable verifies that electron-builder actually
// applied the hardening requested in package.json. A configuration-only check
// cannot catch a packaging regression that leaves Electron's defaults intact.
const expectedFuses = new Map([
  [FuseV1Options.RunAsNode, DISABLED_FUSE_STATE],
  [FuseV1Options.EnableCookieEncryption, ENABLED_FUSE_STATE],
  [FuseV1Options.EnableNodeOptionsEnvironmentVariable, DISABLED_FUSE_STATE],
  [FuseV1Options.EnableNodeCliInspectArguments, DISABLED_FUSE_STATE],
  [FuseV1Options.EnableEmbeddedAsarIntegrityValidation, ENABLED_FUSE_STATE],
  [FuseV1Options.OnlyLoadAppFromAsar, ENABLED_FUSE_STATE],
  [FuseV1Options.GrantFileProtocolExtraPrivileges, DISABLED_FUSE_STATE],
]);

const stateNames = new Map([
  [DISABLED_FUSE_STATE, 'disabled'],
  [ENABLED_FUSE_STATE, 'enabled'],
  [REMOVED_FUSE_STATE, 'removed'],
  [INHERITED_FUSE_STATE, 'inherited'],
]);

for (const [fuse, expectedState] of expectedFuses) {
  const actualState = fuseWire[fuse];
  assert.equal(
    actualState,
    expectedState,
    `${FuseV1Options[fuse]} must be ${stateNames.get(expectedState)}; packaged value is ${stateNames.get(actualState) || actualState}`,
  );
}

console.log(`Verified ${expectedFuses.size} security fuses in ${path.basename(executablePath)}.`);
