# Backend packaging dependency audit

Audit date: 2026-08-27. Environment: Windows x64, CPython 3.12.2 from
`.venv`. This is a build-maintainer record; it is not copied into the shipped
application.

## Public Windows redistribution gate

The current PyAV 18.0.0 Windows wheel is **not approved for public binary
distribution**. Its FFmpeg 8.1.2 DLL directly imports x264 and x265, while the
vendor build patches FFmpeg's licence classifier so that the binary reports
LGPLv3 instead of following upstream FFmpeg's required GPL classification.
Both exact codec sources remain GPL-2.0-or-later/commercial, and no commercial
grant is present. The wheel also copies several unversioned MSYS2 runtime DLLs
without enough provenance to provide an exact corresponding-source record.

`packaging/check_public_redistribution.py` and
`packaging/public_native_runtime.json` therefore fail closed before PyInstaller
freezes the sidecar. Approval requires an exact reviewed DLL filename/SHA-256
set and matching licence-file hashes; a different or apparently LGPL-only
`avcodec` DLL is not accepted automatically. See `THIRD_PARTY_NOTICES.md` for
the complete evidence, native inventory, obligations, and remediation paths.

This blocks the Windows installer only. Source publication and the browser
deployment do not convey the audited Windows DLL bundle.

## First-party import audit

| Capability | Imported module/distribution | Why the freezer needs help |
|---|---|---|
| HTTP sidecar | `fastapi`, `starlette`, `pydantic`, `uvicorn` | Uvicorn selects protocol and WebSocket implementations by string. The spec collects its submodules and metadata. |
| WeChat database/image crypto | `Crypto` / `pycryptodome` | Cipher implementations are extension modules selected dynamically. |
| Image validation/transcoding | `PIL` / Pillow | Pillow discovers image plugins dynamically. |
| HEIF/HEIC/AVIF | `pillow_heif`, `_pillow_heif`, `libheif*.dll` | The extension and DLL live at the top of `site-packages`, outside the Python package directory. A custom hook collects both. |
| WXGF/HEVC and media decode | `av` / PyAV, `av.libs/*.dll` | The PyAV wheel keeps FFmpeg DLLs in a sibling directory. A hook and runtime DLL-directory hook preserve that layout. |
| WeChat voice | `pysilk` / `silk-python` | `pysilk` chooses its Cython or CFFI backend at runtime. Both compiled backends are collected. |
| WeChat 4.x payloads | `zstandard` | The C extension backend is optional in source and therefore explicitly collected. |
| Process/key discovery | `pymem`, `psutil`, `pywin32` | Imports occur inside Windows-only functions or guarded blocks. `win32api`, `win32con`, `win32gui`, `win32process`, `pythoncom`, and `pywintypes` are hidden imports. |
| Optional Hook key capture | `wx_key` | This native enhancement has no upstream redistribution license and is not on PyPI, so public builds explicitly exclude it. An authorized maintainer can opt in with `-IncludeWxKey` after supplying a compatible local wheel; otherwise the supported PyMem/manual-key fallback remains available. |
| Auxiliary binary inspection | `yara`, `pefile` | These are release-contract dependencies even though the current first-party graph does not directly import them on every path. They remain explicit hidden imports. |
| Upload/static support | `aiofiles`, `multipart`, `httptools`, `websockets` | Framework/protocol extras are loaded dynamically. |

The audit found no runtime import of `faster_whisper`. It is not declared in
`backend/requirements.txt`; the obsolete forced import was removed from
`restart.bat`. Local transcription is exposed through configurable API
interfaces rather than an embedded Faster Whisper model.

## Versions observed in `.venv`

| Distribution | Version |
|---|---:|
| FastAPI | 0.141.1 |
| Uvicorn | 0.34.0 |
| PyMem | 1.13.0 |
| PyCryptodome | 3.23.0 |
| pefile | 2024.8.26 |
| yara-python | 4.5.4 |
| psutil | 7.2.2 |
| aiofiles | 24.1.0 |
| python-multipart | 0.0.32 |
| Pillow | 12.3.0 |
| pillow-heif | 1.4.0 |
| av | 18.0.0 |
| silk-python | 0.2.8 |
| zstandard | 0.25.0 |
| pywin32 | 312 |
| PyInstaller | 6.21.0 |
| pyinstaller-hooks-contrib | 2026.6 |

The build environment uses exact PyInstaller and hooks-contrib pins from
`packaging/requirements-build.txt`. Recreate the environment from that file on
a clean build machine rather than copying this virtual environment. Runtime
packages are still governed by `backend/requirements.txt`; update and verify
that file's bounded ranges independently when producing a new release.

`scripts/verify-packaging-runtime.py` is run before every freeze. Besides
imports, it creates a PyAV HEVC decoder and registers pillow-heif, catching a
missing FFmpeg/libheif DLL before Electron packaging begins. It reports a
missing `wx_key` enhancement as an explicit non-fatal warning. The packaged
Electron smoke test repeats required native imports through
`/api/desktop/self-test`, so missing required freezer hooks still fail the
release. Independently, the redistribution gate runs before a public freeze;
functional success cannot override a licence/provenance failure.

## Deliberate exclusions

The PyInstaller target contains no `frontend/dist` (Electron carries it), no
backend JSON configuration, no key files, no SQLite databases, no diagnostics,
and no source checkout. It also excludes tests and undeclared heavyweight ML
frameworks. `scripts/check-release.ps1` enforces these constraints against the
sidecar and the Electron unpacked staging directory.

The PyInstaller spec also collects `THIRD_PARTY_NOTICES.md` and the verified
texts under `packaging/licenses/`. Electron Builder separately preserves the
production Node dependency licences and Electron/Chromium's generated licence
files. These notices document obligations but do not approve a bundle rejected
by `check_public_redistribution.py`.
