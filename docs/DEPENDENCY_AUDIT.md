# Dependency advisory review — 2026-09-21

This review covers the locked package dependencies for CVGnome 0.5.0. The first
binary target is `aarch64-apple-darwin`; Windows and Linux are unverified and are
not release targets. This is a dated advisory check, not an assurance that the
application or every bundled native library is vulnerability-free.

## Method and result

[Google OSV-Scanner 2.6.0](https://github.com/google/osv-scanner/releases/tag/v2.6.0)
scanned all 557 entries in `src-tauri/Cargo.lock` and 17 entries in
`engine/uv.lock`, including entries for other platforms and build tooling. The
official macOS arm64 scanner binary was checked against its GitHub release
asset SHA-256 before execution:

```text
98c460dcd37de25819babd757d04542045b6243113e209edcd4d89fedb0256b4
```

Reproduce the package-version query with an installed OSV-Scanner:

```bash
osv-scanner scan source \
  --lockfile=src-tauri/Cargo.lock \
  --lockfile=engine/uv.lock \
  --format=json --output-file=dependency-audit.json
```

The Python lock had no reported advisories. The npm installation audit also
reported zero vulnerabilities after the frontend test dependencies were added.
These results do not audit the CPython interpreter, operating-system frameworks,
or every native library bundled inside a Python wheel.

The initial Rust result identified `rustls 0.23.43` as affected by
[RUSTSEC-2026-0285](https://rustsec.org/advisories/RUSTSEC-2026-0285.html), a TLS
1.3 encryption-level validation defect. The lock was updated precisely to
`rustls 0.23.45`, without other third-party version changes. A second OSV scan
confirmed that this advisory was no longer reported.

## Residual findings

The scanner still exits nonzero because the following findings remain. No
scanner suppression configuration was added.

| Locked package | Advisory | Relevance to this release |
| --- | --- | --- |
| `glib 0.18.5` | [RUSTSEC-2024-0429](https://rustsec.org/advisories/RUSTSEC-2024-0429.html), unsound `VariantStrIter` methods; fixed in 0.20.0 | Part of the Linux GTK/WebKit dependency graph; absent from the macOS arm64 target graph. Resolve before claiming Linux support. |
| `proc-macro-error 1.0.4` | [RUSTSEC-2024-0370](https://rustsec.org/advisories/RUSTSEC-2024-0370.html), unmaintained | Used by Linux `glib-macros` and `gtk3-macros`; absent from the macOS arm64 target graph. No patched version reported. |
| `unic-char-property 0.9.0` | [RUSTSEC-2025-0081](https://rustsec.org/advisories/RUSTSEC-2025-0081.html), unmaintained | Included in Tauri's URL-pattern dependency chain on macOS. |
| `unic-char-range 0.9.0` | [RUSTSEC-2025-0075](https://rustsec.org/advisories/RUSTSEC-2025-0075.html), unmaintained | Included in the same dependency chain. |
| `unic-common 0.9.0` | [RUSTSEC-2025-0080](https://rustsec.org/advisories/RUSTSEC-2025-0080.html), unmaintained | Included in the same dependency chain. |
| `unic-ucd-ident 0.9.0` | [RUSTSEC-2025-0100](https://rustsec.org/advisories/RUSTSEC-2025-0100.html), unmaintained | Included in the same dependency chain. |
| `unic-ucd-version 0.9.0` | [RUSTSEC-2025-0098](https://rustsec.org/advisories/RUSTSEC-2025-0098.html), unmaintained | Included in the same dependency chain. |

The five UNIC advisories describe maintenance status and report no patched
versions. They are retained maintenance debt for this preview, not findings
reclassified as harmless or absent. Revisit the upstream Tauri/URL-pattern
dependency chain during subsequent dependency updates.

## Target evidence and limits

The following commands showed no macOS-target dependencies for `glib` and
`proc-macro-error`:

```bash
cargo tree --locked --manifest-path src-tauri/Cargo.toml \
  --target aarch64-apple-darwin -e normal,build -i glib
cargo tree --locked --manifest-path src-tauri/Cargo.toml \
  --target aarch64-apple-darwin -e normal,build -i proc-macro-error
```

The corresponding `-i unic-ucd-ident` query showed
`unic-ucd-ident -> urlpattern 0.3.0 -> tauri-utils 2.9.3`, reached through both
Tauri runtime and build/procedural-macro dependencies. This establishes target
dependency presence, not execution of any particular function. No call-stack
reachability analysis or exploit test was performed. Rerun the audit when
lockfiles, supported targets, or upstream advisories change.
