# Windows speech runtime: license provenance

This is an engineering record of the supplied notices and examined binaries,
not a certification that a distribution satisfies every license condition.
The complete upstream texts below control over this summary. No model weights,
personal recordings, credentials or private installation paths are included.

## Project licenses

| Component | Included full text | Exact official source |
| --- | --- | --- |
| CTranslate2 4.8.4, MIT | [CTranslate2-MIT.txt](CTranslate2-MIT.txt) | [LICENSE at release commit](https://raw.githubusercontent.com/OpenNMT/CTranslate2/5de572ef60b67a0276c5989ee73d0072808ff4c3/LICENSE) |
| Hugging Face tokenizers 0.23.3, Apache 2.0 | [Tokenizers-Apache-2.0.txt](Tokenizers-Apache-2.0.txt) | [LICENSE at release commit](https://raw.githubusercontent.com/huggingface/tokenizers/301277072263fb3830eb66eec7e47adadf76f1dc/LICENSE) |
| oneDNN 3.1.1, Apache 2.0 and its supplied third-party notices | [license](oneDNN-3.1.1-Apache-2.0.txt), [THIRD-PARTY-PROGRAMS](oneDNN-3.1.1-THIRD-PARTY-PROGRAMS.txt) | [release source tree](https://github.com/oneapi-src/oneDNN/tree/v3.1.1) |

The CTranslate2 [4.8.4 Windows build script](https://github.com/OpenNMT/CTranslate2/blob/5de572ef60b67a0276c5989ee73d0072808ff4c3/python/tools/prepare_build_environment_windows.sh)
builds oneDNN 3.1.1 statically, enables DNNL and CUDA dynamic loading, sets
`WITH_CUDNN=OFF`, and nevertheless copies both vendor DLLs below into the wheel.
This is evidence from the published build recipe, not a claim that the MIT
license relicenses its vendor dependencies.

SHA256 of the full project-license source files, before any checkout newline
conversion:

- CTranslate2: `54aa79d9fe3c09e67a16dcd95b9e88676405a6ec174efda31036983cf7672ecb`.
- tokenizers: `c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4`.
- oneDNN LICENSE: `dd12452e1ae11e3282271aa5895d8175296b83e936a027209f9399d26a407d0f`.
- oneDNN THIRD-PARTY-PROGRAMS: `9117585dfc6b5cd0fafc063312f28a86ec82eb97bf68316dcefcaaa7fc11428e`.

The oneDNN texts were retrieved from the immutable release tree
`64f6bcbcbab628e96f33a62c3e975f8535a7bde4` at the official repository.

## Intel OpenMP runtime

The examined `libiomp5md.dll` is byte-identical to the same member in Intel's
official [`intel-openmp` 2025.3.0 Windows package](https://pypi.org/project/intel-openmp/2025.3.0/).
Its PE resource identifies Intel OpenMP 5.0, build `20250910`, and Intel's
1997–2025 copyright.

- Binary size: 1,614,192 bytes.
- Binary SHA256: `982233366b0afcda1e0f55a0b134097e35b779613f54ddb69e685e6cd06b755f`.
- Official wheel: `intel_openmp-2025.3.0-py2.py3-none-win_amd64.whl`.
- Wheel member: `intel_openmp-2025.3.0.data/data/Library/bin/libiomp5md.dll`.
- Full terms: [Intel-Developer-Tools-EULA-2025.3.txt](Intel-Developer-Tools-EULA-2025.3.txt).
- License source member: `intel_openmp-2025.3.0.dist-info/LICENSE.txt`.
- Source license SHA256: `febfb69a5b058f55892dc7f341108a9ca29d50e9fe85723a57eebbb826436958`.

The full supplied agreement is the **Intel End User License Agreement for
Developer Tools, Version August 2024**. The source uses Windows-1252; the included
copy is losslessly transcoded to UTF-8, preserving all clauses and original line
endings. Its pre-checkout UTF-8 SHA256 is
`28529142173be36cca1a01051e54c5b51a699e339d9cad2238ffbf77b79ec93e`.

Section 2.1(D) describes conditional redistribution; the definition in 1(I)
refers to product redistributable-file lists. The examined wheel supplies the
agreement but no separate `redist.txt` list. This record does not substitute a
forum post, an MIT license from another OpenMP implementation, or a blanket
commercial-distribution permission for those actual terms. Retain the complete
Intel agreement with the matching binary and evaluate the applicable conditions
for any redistributed product. Intel's conditions do not replace the separate
licenses of Qt, Piper, or other components.

## NVIDIA cuDNN DLL: supplied license and unresolved scope

The examined `cudnn64_9.dll` is byte-identical to the same member in NVIDIA's
official [`nvidia-cudnn-cu12` 9.10.2.21 Windows package](https://pypi.org/project/nvidia-cudnn-cu12/9.10.2.21/).
The PE resource identifies NVIDIA cuDNN 9.10.2.21 and NVIDIA's 2014–2023 copyright.

- Binary size: 266,288 bytes.
- Binary SHA256: `9edbcdff73b0af070eb160b2ce66e59feca04aa017351d8eedcc5e8e149967d2`.
- Official wheel: `nvidia_cudnn_cu12-9.10.2.21-py3-none-win_amd64.whl`.
- Wheel member: `nvidia/cudnn/bin/cudnn64_9.dll`.
- Complete supplied terms: [NVIDIA-cuDNN-9.10.2-LICENSE.txt](NVIDIA-cuDNN-9.10.2-LICENSE.txt).
- License source member: `nvidia_cudnn_cu12-9.10.2.21.dist-info/licenses/License.txt`.
- Source license SHA256: `49cf79bdb35734b52fe6203013b3bd759f81e998cd32aa2c65c51db9a88c61d2`.

The supplied text contains the NVIDIA SDK agreement and a cuDNN supplement
dated January 28, 2020. That supplement identifies `cudnn64_7.dll`, not the v9
filename, and limits its stated development scope to systems with NVIDIA GPUs.
Its presence in a newer wheel therefore is **not recorded here as established
blanket permission to redistribute v9 in an arbitrary CPU-only product**.
The full source text is retained rather than silently replacing it with a
different version's agreement. See also NVIDIA's official
[Windows distribution documentation](https://docs.nvidia.com/deeplearning/cudnn/installation/latest/windows.html).

## Passive PE evidence for a minimal CPU copy

The examined CTranslate2 CPython 3.12 Windows binaries have these direct imports:

| File | SHA256 | Relevant direct imports | Delay imports |
| --- | --- | --- | --- |
| `ctranslate2.dll` | `30e16517244907c60f97a09931e7bce282e86eb93011b55a343a652cb360eb06` | `libiomp5md.dll`, Windows and Microsoft C/C++ runtime libraries | None |
| `_ext.cp312-win_amd64.pyd` | `c60d877050bff4e93365ed2dd8f045f066d5cfa1573a75cf594768c5a4b0bcf9` | `ctranslate2.dll`, `python312.dll`, Windows and Microsoft C/C++ runtime libraries | None |

Neither table references cuDNN. The upstream Python initializer loops over
package `*.dll` files and loads each present library, which explains the
otherwise unnecessary cuDNN load. Direct-import inspection alone cannot rule
out every dynamic runtime dependency.

For the explicitly CPU-only STT application copy, the build may omit only the
identified `ctranslate2/cudnn64_9.dll` after checking the resolved generated
destination. Keep Intel OpenMP: it is a direct dependency. Validate the resulting
copied interpreter with real CPU int8 preparation and RU/EN transcription before
accepting that artifact. This record does not assert that the post-exclusion
native test has already run. Do not change the original environment, private
CUDA voice environment, Windows security policy or trust configuration.

## Acquisition evidence

Only official upstream release sources and publisher-owned PyPI distributions
were used. Vendor archive directory entries, the matching DLL member, and full
license members were read using bounded HTTPS byte ranges; the entire large
archives were not downloaded or executed. ZIP member CRC checks and SHA256
comparisons of the extracted DLL bytes establish the recorded matches. The
following complete-wheel hashes are the publisher's PyPI metadata, not hashes
recomputed over a full archive download:

- Intel wheel SHA256: `8ff899d01a41b92ffe0e618b221ea633127d16df726086082d381c431c31339d`.
- NVIDIA wheel SHA256: `c6288de7d63e6cf62988f0923f96dc339cea362decb1bf5b3141883392a7d65e`.

The wheel file URLs are available from the corresponding official
[Intel release metadata](https://pypi.org/pypi/intel-openmp/2025.3.0/json) and
[NVIDIA release metadata](https://pypi.org/pypi/nvidia-cudnn-cu12/9.10.2.21/json).
Notices do not make optional personal-use TTS models commercially licensed or
remove their separate model/output restrictions.
