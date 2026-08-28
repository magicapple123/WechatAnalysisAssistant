# Third-party notices and Windows redistribution status

Audit date: 2026-08-27  
Audited target: Windows x64, CPython 3.12, PyAV 18.0.0, FFmpeg 8.1.2,
Electron 43.1.1

This file records the third-party software shipped by the desktop application
and the evidence used to evaluate its redistribution terms. It is not a
substitute for the licence texts shipped in `THIRD_PARTY_LICENSES`, and it is
not legal advice.

## Stop-ship notice for the current Windows installer

**The current PyAV 18.0.0 Windows wheel is not approved for public
redistribution. Do not publish a Windows installer built from it.** Source-only
and web deployments do not redistribute these Windows DLLs and are not blocked
by this finding.

The decision is fail-closed and is enforced by
`packaging/check_public_redistribution.py`,
`packaging/public_native_runtime.json`, and the PyInstaller spec. The approval
manifest intentionally contains no approved native bundle.

### Reproducible evidence

1. Installed PyAV metadata identifies version 18.0.0 and BSD-3-Clause for the
   Python binding. Its wheel contains 25 native DLLs in `av.libs`, but only the
   PyAV BSD licence is present in `av-18.0.0.dist-info`; the wheel contains no
   FFmpeg/x264/x265 licence bundle, source archive, or source offer.
2. The DLLs report FFmpeg 8.1.2. Calling `avcodec_configuration()` returns, in
   relevant part, `--enable-version3 --enable-libx264 --enable-libx265` and
   calling `avcodec_license()` returns `LGPL version 3 or later`.
3. PE import inspection shows that `avcodec-62-*.dll` directly imports both
   `libx264-165-*.dll` and `libx265-*.dll`.
4. PyAV 18.0.0 fetches the vendor archive from the official
   [`pyav-ffmpeg` 8.1.2-1 release](https://github.com/PyAV-Org/pyav-ffmpeg/tree/8.1.2-1).
   At commit `a71bf9279f7a4659154b68ba6783e89be460bcd5`,
   [`patches/ffmpeg.patch`](https://github.com/PyAV-Org/pyav-ffmpeg/blob/a71bf9279f7a4659154b68ba6783e89be460bcd5/patches/ffmpeg.patch)
   removes `libx264` and `libx265` from FFmpeg's
   `EXTERNAL_LIBRARY_GPL_LIST` and adds them to
   `EXTERNAL_LIBRARY_VERSION3_LIST`.
5. The same vendor tag's
   [`scripts/pkg.py`](https://github.com/PyAV-Org/pyav-ffmpeg/blob/a71bf9279f7a4659154b68ba6783e89be460bcd5/scripts/pkg.py)
   builds x264 commit `b35605ace3ddf7c1a5d67a2eb553f034aef41d55`
   and x265 4.2. Both exact source archives state GPL-2.0-or-later and offer a
   separate commercial licence. Neither the wheel nor the two PyAV
   repositories contains evidence that a commercial licence was purchased or
   grants downstream redistribution rights.
6. Upstream FFmpeg 8.1.2's
   [`LICENSE.md`](https://github.com/FFmpeg/FFmpeg/blob/n8.1.2/LICENSE.md)
   explicitly lists x264 and x265 as GPL libraries and says a combined FFmpeg
   build must pass `--enable-gpl`. The vendor patch contradicts that upstream
   instruction; the self-reported LGPL string therefore cannot be used as
   proof that this binary is LGPL-only.
7. The vendor build also copies `libgcc_s_seh`, `libstdc++`, `libiconv`, and
   `libwinpthread` from the MSYS2 toolchain found on a floating
   `windows-latest` runner. The wheel does not record the MSYS2 package
   versions, source-package identities, or corresponding-source location.

Adding notices does not cure these findings. In particular, links to upstream
projects are not a distributor-controlled offer of the complete corresponding
source for the exact binaries and build patches being conveyed.

### Acceptable ways to remove the block

At least one reviewed solution is required:

- Build and pin a reproducible Windows PyAV wheel against unmodified FFmpeg
  without x264/x265, preferably with a pinned MSVC toolchain. Preserve the HEVC
  decoder needed by this project, publish the exact build scripts and source
  inputs, and include all required LGPL notices, source/relinking material, and
  licence texts.
- Obtain commercial x264 and x265 licences that expressly cover this use and
  downstream redistribution, resolve every MSYS2 runtime's exact provenance,
  and obtain legal review before adding the exact DLL hashes to the approval
  manifest.
- Deliberately distribute the complete combined work under compatible GPL
  terms, with complete corresponding source, installation/relinking material,
  notices, and legal review. Merely keeping the first-party source under MIT
  and linking to upstream repositories is insufficient.

For any solution, add an entry to `packaging/public_native_runtime.json` with
the complete sibling DLL filename-to-SHA-256 map and SHA-256 values for every
required licence file. Unknown or partially matching bundles remain blocked.

## PyAV wheel native inventory

The source versions below come from the exact `pyav-ffmpeg` 8.1.2-1 build
manifest and were cross-checked where the DLL exposes a version function or
Windows version resource.

| Component | Version/source identity | DLLs in the PyAV wheel | Licence evidence | Redistribution result |
|---|---|---|---|---|
| PyAV | 18.0.0, tag commit `54a4395bb4cdd9cdd53ff6216c50b69f6475c13d` | Python extensions that load `av.libs` | BSD-3-Clause metadata and wheel `LICENSE.txt` | Permitted with BSD notice; text is collected. |
| FFmpeg | 8.1.2; source SHA-256 `464beb5e7bf0c311e68b45ae2f04e9cc2af88851abb4082231742a74d97b524c` | `avcodec-62`, `avdevice-62`, `avfilter-11`, `avformat-62`, `avutil-60`, `swresample-6`, `swscale-9` | Binary reports LGPL-3.0-or-later; unmodified upstream licence says linking x264/x265 requires GPL | **Blocked:** vendor licence-classification patch and direct GPL-library imports are unresolved. |
| x264 | commit `b35605ace3ddf7c1a5d67a2eb553f034aef41d55`; archive SHA-256 `6eeb82934e69fd51e043bd8c5b0d152839638d1ce7aa4eea65a3fedcf83ff224` | `libx264-165-*.dll` | Source header: GPL-2.0-or-later or separate commercial licence | **Blocked:** no commercial grant or GPL distribution plan. |
| x265 | 4.2; archive SHA-256 `40b1ea0453e0309f0eba934e0ddf533f8f6295966679e8894e8f1c1c8d5e1210`; DLL reports `4.2+1-e444744` | `libx265-*.dll` | Source header: GPL-2.0-or-later or separate commercial licence | **Blocked:** no commercial grant or GPL distribution plan. |
| dav1d | 1.5.3; SHA-256 `e099f53253f6c247580c554d53a13f1040638f2066edc3c740e4c2f15174ce22` | `libdav1d-*.dll` | BSD-2-Clause | Permissive; copyright and licence text collected. |
| LAME | 3.100; SHA-256 `ddfe36cab873794038ae2c1210557ad34857a4b6bdc515785d1da9e175b1da1e` | `libmp3lame-0-*.dll` | GNU Library GPL 2.0-or-later; upstream `LICENSE` also requires acknowledgement and a project link | Copyleft source/relinking and notice obligations apply; texts collected, but the current binary remains blocked with the bundle. |
| OpenCORE AMR | 0.1.6; SHA-256 `483eb4061088e2b34b358e47540b5d495a96cd468e361050fae615b1809dc4a1` | `libopencore-amrnb-0-*.dll`, `libopencore-amrwb-0-*.dll` | Apache-2.0 plus upstream NOTICE and standards notices | Licence and NOTICE collected. Patent/standards warnings remain applicable. |
| Opus | 1.6.1; SHA-256 `6ffcb593207be92584df15b32466ed64bbec99109f007c82205f0194572411a1` | `libopus-0-*.dll` | BSD-3-Clause-style terms plus patent notices | Licence/patent notice collected. |
| SVT-AV1 | 4.1.0; SHA-256 `184162d3db3a4448882b17230413b4938ca252eef6b3c5e2f1236b2fcf497881` | `libSvtAv1Enc-*.dll` | BSD-3-Clause-Clear | Licence text collected. |
| libvpx | 1.16.0; SHA-256 `7a479a3c66b9f5d5542a4c6a1b7d3768a983b1e5c14c60a9396edc9b649e015c` | `libvpx-1-*.dll` | BSD-3-Clause plus Additional IP Rights Grant | Licence and patent grant collected. |
| libwebp / SharpYUV | 1.6.0; SHA-256 `93a852c2b3efafee3723efd4636de855b46f9fe1efddd607e1f42f60fc8f2136` | `libwebp-*.dll`, `libwebpmux-*.dll`, `libsharpyuv-*.dll` | BSD-3-Clause; SharpYUV is part of the same upstream source tree | Licence text collected. |
| oneVPL dispatcher | 2.16.0; SHA-256 `d60931937426130ddad9f1975c010543f0da99e67edb1c6070656b7947f633b6` | `libvpl-*.dll` | MIT in the official oneVPL source release | Permissive; retain the upstream licence in any approved bundle. |
| zlib | DLL reports 1.3.2 | `zlib1-*.dll` | zlib licence | Permissive, but this DLL is copied from MSYS2 rather than the pinned codec source list; future approval must record the exact package/build provenance and text. |
| GNU libiconv | DLL version resource reports 1.19 | `libiconv-2-*.dll` | DLL describes itself as LGPL; the exact MSYS2 package/build record is absent | **Blocked:** exact source/build provenance and LGPL delivery obligations are not documented. |
| GCC/libstdc++ runtime | Version not recoverable from the wheel metadata | `libgcc_s_seh-1-*.dll`, `libstdc++-6-*.dll` | The exact copied MSYS2 packages and applicable GCC Runtime Library Exception files are absent | **Blocked:** cannot make an exact corresponding-source/licence record for these DLLs. |
| MinGW-w64 winpthreads | Version not recoverable from the wheel metadata | `libwinpthread-1-*.dll` | The exact copied MSYS2 package and licence file are absent | **Blocked:** binary provenance and required notice are not established. |

The vendor repository's human-readable README names a different x264 commit
than `scripts/pkg.py`. For this audit, the exact build manifest and its verified
archive SHA-256 are authoritative; do not construct a source offer from the
stale README value.

Verbatim texts verified from the exact wheel or exact source archives are kept
under `packaging/licenses/`. Their presence is documentation, not an approval
of the blocked binary bundle.

## Electron runtime and production Node dependencies

Electron 43.1.1 is MIT-licensed. Its Windows distribution carries
`LICENSE.electron.txt` and the generated `LICENSES.chromium.html` beside the
application executable. Electron Builder preserves both files; they cover
Electron/Chromium and Chromium's bundled third-party components.

The application ASAR contains the following production dependency closure from
`electron-updater` 6.8.9. Build tooling such as Electron Builder is a
development dependency and is not shipped inside `app.asar`.

| npm package | Version | Declared licence | Shipped licence material |
|---|---:|---|---|
| `electron-updater` | 6.8.9 | MIT | `LICENSE` |
| `builder-util-runtime` | 9.7.0 | MIT | `LICENSE` |
| `debug` | 4.4.3 | MIT | `LICENSE` |
| `ms` | 2.1.3 | MIT | `license.md` |
| `sax` | 1.6.0 | BlueOak-1.0.0 | `LICENSE.md` |
| `fs-extra` | 10.1.0 | MIT | `LICENSE` |
| `graceful-fs` | 4.2.11 | ISC | `LICENSE` |
| `jsonfile` | 6.2.1 | MIT | `LICENSE` |
| `universalify` | 2.0.1 | MIT | `LICENSE` |
| `js-yaml` | 4.3.2 | MIT | `LICENSE` |
| `argparse` | 2.0.1 | Python-2.0 | `LICENSE` |
| `lazy-val` | 1.0.5 | MIT | The npm tarball and upstream repository contain no licence file; the shipped `package.json` preserves the MIT declaration and author field (`Vladimir Krivosheev`). |
| `lodash.escaperegexp` | 4.1.2 | MIT | `LICENSE` |
| `lodash.isequal` | 4.5.0 | MIT | `LICENSE` |
| `semver` (nested under `electron-updater`) | 7.7.4 | ISC | `LICENSE` |
| `tiny-typed-emitter` | 2.1.0 | MIT | `LICENSE` |

Electron Builder previously excluded these package-level licence files from
`app.asar`. The build now copies them to
`resources/THIRD_PARTY_LICENSES/electron-node/`. `lazy-val/package.json` is
copied because it is the only licence evidence published in that package.

## Expected installed locations

Once a native bundle is approved and packaging succeeds, the unpacked Windows
application must contain:

- `LICENSE.electron.txt` and `LICENSES.chromium.html` beside the application
  executable;
- `resources/THIRD_PARTY_NOTICES.md`;
- `resources/LICENSE.wechat-analysis-assistant.txt`;
- `resources/THIRD_PARTY_LICENSES/electron-node/`;
- `resources/backend/WechatAnalysisAssistantBackend/_internal/THIRD_PARTY_NOTICES.md`;
- `resources/backend/WechatAnalysisAssistantBackend/_internal/THIRD_PARTY_LICENSES/native/`.

Release automation must treat any missing path as a packaging failure.

## Maintainer verification

Run these checks whenever PyAV, Electron, `electron-updater`, the build image,
or a native DLL changes:

```powershell
python packaging/check_public_redistribution.py --installed-pyav
python -m unittest discover -s packaging/tests -p "test_*.py"
python -m ruff check packaging/check_public_redistribution.py packaging/tests --select F,E9
python -m compileall -q packaging/check_public_redistribution.py packaging/tests
npm ls --prefix desktop --omit=dev --all
npm audit --prefix desktop --registry=https://registry.npmjs.org/
```

The first command is expected to fail for the currently pinned PyAV 18.0.0
Windows wheel. A passing result is valid only when the exact complete DLL set
and required licence texts match a reviewed entry in
`packaging/public_native_runtime.json`.
