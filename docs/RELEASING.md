# Release process

The supported binary target is macOS arm64. Use a clean checkout and the pinned
Node, uv/Python, and Rust toolchains. Release only deliberately reviewed source
and synthetic examples. Never include a real workspace, credentials, personal
screenshots, predecessor repository, or private development history.

1. Update the version in npm, Tauri, Cargo, Python, lockfiles, and version tests.
   Never rewrite historical database migration SQL or its checksums.
2. Run `npm run bootstrap`, `npm run check`, and `npm run native:lint`.
   CI repeats these on a fresh arm64 macOS runner. Run dependency-advisory audits
   for npm, Cargo.lock, and engine/uv.lock; review the supported-target graph and
   record remaining findings rather than claiming a clean scan by exclusion.
3. Run `python3 scripts/source_release.py`. For the initial publication, add
   `--destination /absolute/path/to/a/new-source-directory` to create an explicit,
   history-free snapshot and SHA-256 manifest. Review every manifest entry and
   image. Pattern scans are aids, not proof that no private content exists.
4. Build with `npm run tauri -- build --bundles app --target aarch64-apple-darwin
   -- --locked`. The build regenerates dependency notices and freezes the Python
   engine. Run `scripts/verify_release.py` through the engine's uv environment
   against the engine actually inside the app bundle. Check app version,
   architecture, identifiers, resources, and signing status.
5. Exercise a fresh synthetic workspace: manual start, document import and
   review, edits/history, memories, empty/populated Roles, DOCX/PDF export,
   keyboard/zoom, close/discard protection, and offline backup/restore/migration.
   Do not use a maintainer's career data or paid provider account for release QA.
6. Archive the verified `.app` with macOS metadata preserved. Ship LICENSE,
   NOTICE, third-party notices/inventory, source, and SHA-256 checksums. Record the
   exact source commit, toolchains, test results, and unresolved limitations.
7. Publish the matching source tag and GitHub release, verify public downloads
   and checksum matches, and check CI on the published commit. Enable private
   vulnerability reporting and verify the SECURITY.md route.

Version 0.5 intentionally defers Developer ID signing/notarization. An ad-hoc
signature is not an identified publisher signature. State this clearly in release
notes and link [installation limitations](KNOWN_LIMITATIONS.md). Do not silently
replace an existing release artifact; issue a new patch version for corrections.
