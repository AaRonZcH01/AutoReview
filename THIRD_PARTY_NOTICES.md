# Third-party notices

AutoReview's MIT license applies to its original code. The following separately licensed components retain their own terms. The supplied license directory is part of the distribution and must accompany redistribution where required.

- Official Codex CLI 0.163.0: Apache-2.0; source and releases at https://github.com/openai/codex. License and any upstream notice are under `third_party/licenses/codex`.
- Microsoft Edge WebView2 and its offline installer are Microsoft components, not relicensed under MIT. See https://developer.microsoft.com/microsoft-edge/webview2/ and the terms supplied by Microsoft with the redistributable.
- Python, Tcl/Tk, Python packages, React/React DOM, Rust/Tauri and their dependency licenses are listed below and retained in `third_party/licenses` or in the runtime's upstream data directories.
- RapidOCR includes CPU ONNX models. Their upstream license files are retained with RapidOCR; model provenance is documented by the upstream project at https://github.com/RapidAI/RapidOCR.

The inventory includes build dependencies and platform-specific transitive dependencies as well as runtime dependencies. It does not grant rights beyond the upstream terms. Public upstream copyright/author attributions are retained because they are required notices, not application user data.

| Ecosystem | Component | Version | License |
|---|---|---|---|
|python|PyYAML|6.0.3|MIT|
|python|Pygments|2.21.0|BSD-2-Clause|
|python|altgraph|0.17.5|MIT|
|python|annotated-doc|0.0.5|MIT|
|python|annotated-types|0.8.0|MIT|
|python|antlr4-python3-runtime|4.9.3|BSD|
|python|anyio|4.15.1|MIT|
|python|certifi|2026.7.22|MPL-2.0|
|python|charset-normalizer|3.5.1|MIT|
|python|click|8.5.0|BSD-3-Clause|
|python|colorama|0.4.6|See license files|
|python|colorlog|6.12.0|MIT License|
|python|et_xmlfile|2.0.0|MIT|
|python|fastapi|0.141.1|MIT|
|python|flatbuffers|25.12.19|Apache 2.0|
|python|h11|0.16.0|MIT|
|python|httpcore|1.0.9|BSD-3-Clause|
|python|httpx|0.28.1|BSD-3-Clause|
|python|idna|3.20|BSD-3-Clause|
|python|iniconfig|2.3.0|MIT|
|python|numpy|2.5.3|BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0|
|python|omegaconf|2.3.1|See license files|
|python|onnxruntime|1.30.0|MIT License|
|python|opencv-python|5.0.0.93|Apache 2.0|
|python|openpyxl|3.1.5|MIT|
|python|packaging|26.3|Apache-2.0 OR BSD-2-Clause|
|python|pefile|2024.8.26|MIT|
|python|pillow|12.3.0|MIT-CMU|
|python|pip|25.0.1|MIT|
|python|pluggy|1.6.0|MIT|
|python|protobuf|7.36.2|3-Clause BSD License|
|python|pyclipper|1.4.0|MIT|
|python|pydantic|2.13.5|MIT|
|python|pydantic_core|2.46.5|MIT|
|python|pyinstaller|6.22.3|GPLv2-or-later with a special exception which allows to use PyInstaller to build and distribute non-free programs (including commercial ones)|
|python|pyinstaller-hooks-contrib|2026.7|See license files|
|python|pytest|9.1.1|MIT|
|python|pytest-asyncio|1.4.0|Apache-2.0|
|python|python-multipart|0.0.32|Apache-2.0|
|python|pywin32-ctypes|0.2.3|BSD-3-Clause|
|python|rapidocr|3.9.2|Apache-2.0|
|python|requests|2.34.2|Apache-2.0|
|python|setuptools|84.0.0|MIT|
|python|shapely|2.1.2|BSD 3-Clause|
|python|six|1.17.0|MIT|
|python|starlette|1.7.0|BSD-3-Clause|
|python|tqdm|4.70.1|MPL-2.0 AND MIT|
|python|typing-inspection|0.4.4|MIT|
|python|typing_extensions|4.16.0|PSF-2.0|
|python|urllib3|2.8.0|MIT|
|python|uvicorn|0.54.0|BSD-3-Clause|
|python|websockets|16.1.1|BSD-3-Clause|
|rust|adler2|2.0.1|0BSD OR MIT OR Apache-2.0|
|rust|aho-corasick|1.1.5|Unlicense OR MIT|
|rust|alloc-no-stdlib|3.0.0|BSD-3-Clause|
|rust|alloc-stdlib|0.3.0|BSD-3-Clause|
|rust|anyhow|1.0.104|MIT OR Apache-2.0|
|rust|autocfg|1.5.1|Apache-2.0 OR MIT|
|rust|base64|0.22.1|MIT OR Apache-2.0|
|rust|base64|0.23.1|MIT OR Apache-2.0|
|rust|bit-set|0.8.0|Apache-2.0 OR MIT|
|rust|bit-vec|0.8.0|Apache-2.0 OR MIT|
|rust|bitflags|1.3.2|MIT/Apache-2.0|
|rust|bitflags|2.13.2|MIT OR Apache-2.0|
|rust|block-buffer|0.10.4|MIT OR Apache-2.0|
|rust|brotli|9.0.0|BSD-3-Clause AND MIT|
|rust|brotli-decompressor|6.0.1|BSD-3-Clause/MIT|
|rust|bs58|0.5.1|MIT/Apache-2.0|
|rust|byteorder|1.5.0|Unlicense OR MIT|
|rust|bytes|1.12.1|MIT|
|rust|camino|1.2.6|MIT OR Apache-2.0|
|rust|cargo-platform|0.1.9|MIT OR Apache-2.0|
|rust|cargo_metadata|0.19.2|MIT|
|rust|cargo_toml|1.0.1|Apache-2.0 OR MIT|
|rust|cc|1.5.1|MIT OR Apache-2.0|
|rust|cfb|0.14.0|MIT|
|rust|cfg-if|1.0.5|MIT OR Apache-2.0|
|rust|chrono|0.4.45|MIT OR Apache-2.0|
|rust|cookie|0.18.2|MIT OR Apache-2.0|
|rust|cpufeatures|0.2.17|MIT OR Apache-2.0|
|rust|crc32fast|1.5.2|MIT OR Apache-2.0|
|rust|crossbeam-channel|0.5.17|MIT OR Apache-2.0|
|rust|crossbeam-utils|0.8.23|MIT OR Apache-2.0|
|rust|crypto-common|0.1.7|MIT OR Apache-2.0|
|rust|cssparser|0.37.0|MPL-2.0|
|rust|cssparser-macros|0.7.1|MPL-2.0|
|rust|ctor|1.0.13|Apache-2.0 OR MIT|
|rust|darling|0.24.1|MIT|
|rust|darling_core|0.24.1|MIT|
|rust|darling_macro|0.24.1|MIT|
|rust|defmt|1.1.1|MIT OR Apache-2.0|
|rust|defmt-macros|1.1.1|MIT OR Apache-2.0|
|rust|defmt-parser|1.0.0|MIT OR Apache-2.0|
|rust|deranged|0.5.8|MIT OR Apache-2.0|
|rust|derive_more|2.1.1|MIT|
|rust|derive_more-impl|2.1.1|MIT|
|rust|digest|0.10.7|MIT OR Apache-2.0|
|rust|dirs|7.0.0|MIT OR Apache-2.0|
|rust|dirs-sys|0.5.0|MIT OR Apache-2.0|
|rust|displaydoc|0.2.7|MIT OR Apache-2.0|
|rust|dom_query|0.28.0|MIT|
|rust|dpi|0.1.2|Apache-2.0 AND MIT|
|rust|dtoa|1.0.11|MIT OR Apache-2.0|
|rust|dtoa-short|0.3.5|MPL-2.0|
|rust|dunce|1.0.5|CC0-1.0 OR MIT-0 OR Apache-2.0|
|rust|dyn-clone|1.0.20|MIT OR Apache-2.0|
|rust|embed-resource|3.0.11|MIT|
|rust|equivalent|1.0.2|Apache-2.0 OR MIT|
|rust|erased-serde|0.4.10|MIT OR Apache-2.0|
|rust|fastrand|2.5.0|Apache-2.0 OR MIT|
|rust|fdeflate|0.3.7|MIT OR Apache-2.0|
|rust|find-msvc-tools|0.1.14|MIT OR Apache-2.0|
|rust|flate2|1.1.10|MIT OR Apache-2.0|
|rust|fnv|1.0.7|Apache-2.0 / MIT|
|rust|foldhash|0.2.0|Zlib|
|rust|form_urlencoded|1.2.2|MIT OR Apache-2.0|
|rust|generic-array|0.14.7|MIT|
|rust|getrandom|0.3.4|MIT OR Apache-2.0|
|rust|getrandom|0.4.3|MIT OR Apache-2.0|
|rust|glob|0.3.4|MIT OR Apache-2.0|
|rust|hashbrown|0.12.3|MIT OR Apache-2.0|
|rust|hashbrown|0.17.1|MIT OR Apache-2.0|
|rust|heck|0.5.0|MIT OR Apache-2.0|
|rust|hex|0.4.3|MIT OR Apache-2.0|
|rust|html5ever|0.39.0|MIT OR Apache-2.0|
|rust|http|1.5.0|MIT OR Apache-2.0|
|rust|ico|0.5.0|MIT|
|rust|icu_collections|2.3.0|Unicode-3.0|
|rust|icu_locale_core|2.3.0|Unicode-3.0|
|rust|icu_normalizer|2.3.0|Unicode-3.0|
|rust|icu_normalizer_data|2.3.0|Unicode-3.0|
|rust|icu_properties|2.3.0|Unicode-3.0|
|rust|icu_properties_data|2.3.0|Unicode-3.0|
|rust|icu_provider|2.3.1|Unicode-3.0|
|rust|ident_case|1.0.1|MIT/Apache-2.0|
|rust|idna|1.1.0|MIT OR Apache-2.0|
|rust|idna_adapter|1.2.2|Apache-2.0 OR MIT|
|rust|indexmap|1.9.3|Apache-2.0 OR MIT|
|rust|indexmap|2.14.2|Apache-2.0 OR MIT|
|rust|infer|0.22.0|MIT|
|rust|itoa|1.0.18|MIT OR Apache-2.0|
|rust|jiff|0.2.37|Unlicense OR MIT|
|rust|jiff-core|0.1.1|Unlicense OR MIT|
|rust|jiff-tzdb|0.1.8|Unlicense OR MIT|
|rust|jiff-tzdb-platform|0.1.3|Unlicense OR MIT|
|rust|json-patch|4.2.0|MIT/Apache-2.0|
|rust|jsonptr|0.7.1|MIT OR Apache-2.0|
|rust|keyboard-types|0.8.3|MIT OR Apache-2.0|
|rust|libc|0.2.189|MIT OR Apache-2.0|
|rust|litemap|0.8.3|Unicode-3.0|
|rust|lock_api|0.4.14|MIT OR Apache-2.0|
|rust|log|0.4.34|MIT OR Apache-2.0|
|rust|markup5ever|0.39.0|MIT OR Apache-2.0|
|rust|memchr|2.8.3|Unlicense OR MIT|
|rust|mime|0.3.17|MIT OR Apache-2.0|
|rust|miniz_oxide|0.8.9|MIT OR Zlib OR Apache-2.0|
|rust|miniz_oxide|0.9.1|MIT OR Zlib OR Apache-2.0|
|rust|mio|1.2.3|MIT|
|rust|muda|0.20.0|Apache-2.0 OR MIT|
|rust|new_debug_unreachable|1.0.6|MIT|
|rust|num-conv|0.2.2|MIT OR Apache-2.0|
|rust|num-traits|0.2.19|MIT OR Apache-2.0|
|rust|once_cell|1.21.4|MIT OR Apache-2.0|
|rust|option-ext|0.2.0|MPL-2.0|
|rust|parking_lot|0.12.5|MIT OR Apache-2.0|
|rust|parking_lot_core|0.9.12|MIT OR Apache-2.0|
|rust|percent-encoding|2.3.2|MIT OR Apache-2.0|
|rust|phf|0.13.1|MIT|
|rust|phf_codegen|0.13.1|MIT|
|rust|phf_generator|0.13.1|MIT|
|rust|phf_macros|0.13.1|MIT|
|rust|phf_shared|0.13.1|MIT|
|rust|pin-project-lite|0.2.17|Apache-2.0 OR MIT|
|rust|plist|1.10.1|MIT|
|rust|png|0.17.16|MIT OR Apache-2.0|
|rust|png|0.18.1|MIT OR Apache-2.0|
|rust|potential_utf|0.1.6|Unicode-3.0|
|rust|powerfmt|0.2.0|MIT OR Apache-2.0|
|rust|precomputed-hash|0.1.1|MIT|
|rust|proc-macro2|1.0.107|MIT OR Apache-2.0|
|rust|quick-xml|0.42.0|MIT|
|rust|quote|1.0.47|MIT OR Apache-2.0|
|rust|raw-window-handle|0.6.2|MIT OR Apache-2.0 OR Zlib|
|rust|ref-cast|1.0.27|MIT OR Apache-2.0|
|rust|ref-cast-impl|1.0.27|MIT OR Apache-2.0|
|rust|regex|1.13.1|MIT OR Apache-2.0|
|rust|regex-automata|0.4.18|MIT OR Apache-2.0|
|rust|regex-syntax|0.8.11|MIT OR Apache-2.0|
|rust|rustc-hash|2.1.3|Apache-2.0 OR MIT|
|rust|rustc_version|0.4.1|MIT OR Apache-2.0|
|rust|same-file|1.0.6|Unlicense/MIT|
|rust|schemars|0.8.22|MIT|
|rust|schemars|0.9.0|MIT|
|rust|schemars|1.2.2|MIT|
|rust|schemars_derive|0.8.22|MIT|
|rust|scopeguard|1.2.0|MIT OR Apache-2.0|
|rust|selectors|0.38.0|MPL-2.0|
|rust|semver|1.0.28|MIT OR Apache-2.0|
|rust|serde|1.0.229|MIT OR Apache-2.0|
|rust|serde-untagged|0.1.9|MIT OR Apache-2.0|
|rust|serde_core|1.0.229|MIT OR Apache-2.0|
|rust|serde_derive|1.0.229|MIT OR Apache-2.0|
|rust|serde_derive_internals|0.29.1|MIT OR Apache-2.0|
|rust|serde_json|1.0.151|MIT OR Apache-2.0|
|rust|serde_repr|0.1.21|MIT OR Apache-2.0|
|rust|serde_spanned|1.1.1|MIT OR Apache-2.0|
|rust|serde_with|3.24.0|MIT OR Apache-2.0|
|rust|serde_with_macros|3.24.0|MIT OR Apache-2.0|
|rust|serialize-to-javascript|0.1.2|MIT OR Apache-2.0|
|rust|serialize-to-javascript-impl|0.1.2|MIT OR Apache-2.0|
|rust|servo_arc|0.4.3|MIT OR Apache-2.0|
|rust|sha2|0.10.9|MIT OR Apache-2.0|
|rust|shlex|2.0.1|MIT OR Apache-2.0|
|rust|simd-adler32|0.3.10|MIT|
|rust|siphasher|1.0.4|MIT OR Apache-2.0|
|rust|smallvec|1.16.2|MIT OR Apache-2.0|
|rust|socket2|0.6.5|MIT OR Apache-2.0|
|rust|softbuffer|0.4.8|MIT OR Apache-2.0|
|rust|stable_deref_trait|1.2.1|MIT OR Apache-2.0|
|rust|string_cache|0.9.0|MIT OR Apache-2.0|
|rust|string_cache_codegen|0.6.1|MIT OR Apache-2.0|
|rust|strsim|0.11.1|MIT|
|rust|syn|2.0.119|MIT OR Apache-2.0|
|rust|syn|3.0.6|MIT OR Apache-2.0|
|rust|synstructure|0.14.0|MIT|
|rust|tao|0.37.1|Apache-2.0|
|rust|tauri|2.12.0|Apache-2.0 OR MIT|
|rust|tauri-build|2.7.0|Apache-2.0 OR MIT|
|rust|tauri-codegen|2.7.0|Apache-2.0 OR MIT|
|rust|tauri-macros|2.7.0|Apache-2.0 OR MIT|
|rust|tauri-runtime|2.12.0|Apache-2.0 OR MIT|
|rust|tauri-runtime-wry|2.12.0|Apache-2.0 OR MIT|
|rust|tauri-utils|2.10.0|Apache-2.0 OR MIT|
|rust|tauri-winres|0.3.6|MIT|
|rust|tendril|0.5.1|MIT OR Apache-2.0|
|rust|thiserror|2.0.21|MIT OR Apache-2.0|
|rust|thiserror-impl|2.0.21|MIT OR Apache-2.0|
|rust|time|0.3.55|MIT OR Apache-2.0|
|rust|time-core|0.1.9|MIT OR Apache-2.0|
|rust|time-macros|0.2.32|MIT OR Apache-2.0|
|rust|tinystr|0.8.4|Unicode-3.0|
|rust|tinyvec|1.13.3|Zlib OR Apache-2.0 OR MIT|
|rust|tokio|1.53.1|MIT|
|rust|toml|1.1.6+spec-1.1.0|MIT OR Apache-2.0|
|rust|toml_datetime|1.1.1+spec-1.1.0|MIT OR Apache-2.0|
|rust|toml_parser|1.1.3+spec-1.1.0|MIT OR Apache-2.0|
|rust|toml_writer|1.1.2+spec-1.1.0|MIT OR Apache-2.0|
|rust|tracing|0.1.44|MIT|
|rust|tracing-core|0.1.36|MIT|
|rust|tray-icon|0.25.1|MIT OR Apache-2.0|
|rust|typeid|1.0.3|MIT OR Apache-2.0|
|rust|typenum|1.20.1|MIT OR Apache-2.0|
|rust|unicode-ident|1.0.26|(MIT OR Apache-2.0) AND Unicode-3.0|
|rust|unicode-segmentation|1.13.3|MIT OR Apache-2.0|
|rust|url|2.5.8|MIT OR Apache-2.0|
|rust|urlpattern|0.6.0|MIT|
|rust|utf8_iter|1.0.4|Apache-2.0 OR MIT|
|rust|uuid|1.26.1|Apache-2.0 OR MIT|
|rust|version_check|0.9.5|MIT/Apache-2.0|
|rust|vswhom|0.1.0|MIT|
|rust|vswhom-sys|0.1.3|MIT|
|rust|walkdir|2.5.0|Unlicense/MIT|
|rust|web-time|1.1.0|MIT OR Apache-2.0|
|rust|web_atoms|0.2.6|MIT OR Apache-2.0|
|rust|webview2-com|0.39.1|MIT|
|rust|webview2-com-macros|0.8.1|MIT|
|rust|webview2-com-sys|0.39.1|MIT|
|rust|winapi-util|0.1.11|Unlicense OR MIT|
|rust|window-vibrancy|0.8.1|Apache-2.0 OR MIT|
|rust|windows|0.62.2|MIT OR Apache-2.0|
|rust|windows-collections|0.3.2|MIT OR Apache-2.0|
|rust|windows-core|0.62.2|MIT OR Apache-2.0|
|rust|windows-future|0.3.2|MIT OR Apache-2.0|
|rust|windows-implement|0.60.2|MIT OR Apache-2.0|
|rust|windows-interface|0.59.3|MIT OR Apache-2.0|
|rust|windows-link|0.2.1|MIT OR Apache-2.0|
|rust|windows-numerics|0.3.1|MIT OR Apache-2.0|
|rust|windows-result|0.4.1|MIT OR Apache-2.0|
|rust|windows-strings|0.5.1|MIT OR Apache-2.0|
|rust|windows-sys|0.59.0|MIT OR Apache-2.0|
|rust|windows-sys|0.61.2|MIT OR Apache-2.0|
|rust|windows-targets|0.52.6|MIT OR Apache-2.0|
|rust|windows-threading|0.2.1|MIT OR Apache-2.0|
|rust|windows-version|0.1.7|MIT OR Apache-2.0|
|rust|windows_x86_64_msvc|0.52.6|MIT OR Apache-2.0|
|rust|winnow|1.0.4|MIT|
|rust|winreg|0.55.0|MIT|
|rust|writeable|0.6.4|Unicode-3.0|
|rust|wry|0.57.0|Apache-2.0 OR MIT|
|rust|yoke|0.8.3|Unicode-3.0|
|rust|yoke-derive|0.8.3|Unicode-3.0|
|rust|zerofrom|0.1.8|Unicode-3.0|
|rust|zerofrom-derive|0.1.8|Unicode-3.0|
|rust|zerotrie|0.2.5|Unicode-3.0|
|rust|zerovec|0.11.8|Unicode-3.0|
|rust|zerovec-derive|0.11.6|Unicode-3.0|
|rust|zlib-rs|0.6.8|Zlib|
|rust|zmij|1.0.23|MIT|
|javascript|react|19.3.0|MIT|
|javascript|react-dom|19.3.0|MIT|
