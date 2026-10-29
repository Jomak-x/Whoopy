# Kokoro runtime and redistribution audit

The Kokoro model's Apache-2.0 declaration does not describe the complete speech
runtime. Both inspected Kokoro paths include eSpeak/GPL dependencies. Full-bundle
redistribution review remains incomplete; this audit grants no legal clearance
and leaves Whoopy's source license **UNDECIDED**. Model weight license IDs are
unchanged.

## Managed Sherpa/Kokoro

The owner's local inspection of the managed 2026-10-26 Kokoro archive found 391
`espeak-ng-data` entries and only a top-level Apache `LICENSE`. That file alone
does not establish the terms or source obligations for every bundled component.

The pinned [Sherpa ONNX 1.13.4 build](https://raw.githubusercontent.com/k2-fsa/sherpa-onnx/v1.13.4/cmake/piper-phonemize.cmake)
fetches `piper-phonemize` revision
`f3ff95afc03640bc1399e113e83361192a2fafb4` and builds it with shared libraries
disabled. Its [CMake source](https://raw.githubusercontent.com/csukuangfj/piper-phonemize/f3ff95afc03640bc1399e113e83361192a2fafb4/CMakeLists.txt)
links `espeak-ng` and, unless an external directory is supplied, fetches eSpeak
revision `7251208ee427ef87e94f39d0e99b15d287224ed6`. The owner's installed C API
dylib inspection found eSpeak strings and no separate eSpeak dylib. Together
these support embedded linkage; they do not establish the exact source and
build closure of the installed wheel. That closure is still unverified.

## Experimental MLX Kokoro

The audition reports pin MLX-Audio to
`8c244a1faff44cfa69e3151c195f52a4c1c90be5`. The owner's dependency inspection found
Misaki 0.9.4 with Apache terms, `phonemizer-fork` 3.3.2 with a GPLv3 `LICENSE`, and
`espeakng-loader` 0.2.4 containing eSpeak 1.52.0 plus its data but no license-named
file. These local observations are separate from the model-card declarations.
eSpeak 1.52.0's [upstream license statement](https://raw.githubusercontent.com/espeak-ng/espeak-ng/1.52.0/README.md)
specifies GPLv3 or later; its [COPYING](https://raw.githubusercontent.com/espeak-ng/espeak-ng/1.52.0/COPYING)
contains GPLv3. An absent license file in a dependency package does not remove
upstream obligations. The runtime, model weights, phonemizer, native library,
language data, reference recording, and output rights require separate review.

## Meaning and limits

GPL permits commercial use and charging for copies. Its conditions distinguish
running software from conveying it: network interaction alone without transfer
of a copy is not conveying under GPLv3. Distribution of covered binaries can
require notices, license terms, and corresponding source; the scope of a linked
combined work requires review of the actual components and delivery. GPL alone
does not establish a blanket ban on paid server/API use. These distinctions come
from GPLv3's preamble and sections 0, 2, and 4–6, not a clearance of a proposed
product. See the [license text](https://raw.githubusercontent.com/espeak-ng/espeak-ng/1.52.0/COPYING).

Before redistributing either runtime bundle, establish the exact dependency
versions and source/build correspondence, retain required notices and licenses,
and resolve the combined-work and language-data obligations. Local audition
success and permissive model weights do not complete that review. Voice consent,
reference recording rights, and output rights remain separate; no model payload
or audio is published by this audit.
