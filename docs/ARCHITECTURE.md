# Architecture

CVGnome is a local desktop application with three boundaries:

```text
React interface → allowlisted Tauri commands → Rust host → Python engine
                                                   ↘ optional model provider
```

React owns interaction and displays bounded public projections. Rust owns file
dialogs, filesystem paths, credential storage, provider transport, and engine
process supervision. Python owns domain logic, document rendering, and SQLite
persistence. The engine has no network/provider client and receives no API key.

## Local storage and history

Profile versions, imported source lineage, review receipts, memories, generated
drafts, and user-authored revisions are immutable. Mutable role tracking and
review organization use revision checks. Profile changes pin the expected parent
and a durable request ID; an exact retry reuses the stored result, while a changed
request or stale parent fails. Schema migrations are transactional and tested
against prior states. SQLite uses WAL and FULL synchronous mode.

Source bytes are retained by content hash. Temporary import previews expire and
are reconciled; applying a preview verifies its base profile and source identity.
Starting a new import set changes its active generation without deleting history.
Career workspace reset is a separate confirmed, journaled operation over an
explicit managed-file allowlist. It preserves provider settings/credentials and
spend controls. Backup and migration have separate non-overwriting recovery
contracts; see BACKUP_AND_RECOVERY.md.

The public application identifier is `com.cvgnome.desktop`; the database name is
`cvgnome.sqlite3`. Old identifiers may appear only in the isolated compatibility
reader and its tests for explicit migration. They are not current product names,
shared data locations, or aliases for new writes. Upgrades must never silently
open or overwrite another workspace.

## Native boundary

The production WebView has a restrictive content security policy and a small
capability set. It does not render remote job or application pages. Remote URLs
are reference metadata. Imports and exports use native dialogs; external paths,
canonical private metadata, source hashes, raw files, and provider secrets stay
outside React. Public contact details and evidence excerpts enter the renderer
only through the documented review projections.

Engine messages are versioned JSON over stdin/stdout, matched to their request
ID, size bounded, and deadline bounded. The host launches the packaged engine
natively for the target architecture. Exports verify regular-file status, path
scope, byte length, and SHA-256 before copying a selected artifact.

Window close, Cmd+Q, and the application-menu Quit action protect unsaved drafts
and block interruption of an in-flight or uncertain operation. Dock Quit and OS
termination remain a known gap in the native framework; see known limitations.
Native close handling is required; a browser
`beforeunload` callback alone is not the desktop contract. Forced process
termination and operating-system shutdown cannot guarantee retention of an
unsaved in-memory draft.

## Untrusted document parsing

Imports reject symlinks and unsafe file types, stage owner-only copies, and
verify exact bytes before parsing and commit. Selection is bounded by file
count, total bytes, per-file bytes, and folder traversal depth.

PDF/DOCX parsing runs in a separate supervised process group. Defaults include
15 seconds per document, 80 PDF pages, bounded DOCX archive members, and 200,000
extracted characters. The aggregate structured-document preview budget is
75 seconds inside the host request deadline.

On macOS, a parent watchdog samples the entire parser process group's resident
memory, including launcher and worker descendants, at approximately 25 ms
intervals. It terminates the group when it observes use over the configured
512 MiB threshold or loses required telemetry. Scheduling and allocations can
cause overshoot between samples; this is not a hard allocation cap. Linux uses
a readback-verified address-space limit. POSIX CPU, descriptor, output-file, and
core-dump limits must be applied successfully. Unsupported platforms fail closed
for structured document parsing until a tested resource-control implementation
exists. Input/output limits and timeouts do not substitute for memory controls.

## Optional providers

Only Rust communicates with providers. LM Studio uses an explicit numeric
loopback HTTP endpoint with an exact `/v1` path. OpenAI uses the fixed official
HTTPS endpoint. Proxies, redirects, arbitrary remote endpoints, automatic
retries, and provider fallback are disabled. Provider configurations are separate;
only one is active. Changing a model, endpoint, or key invalidates verification.

The OS credential store contains the OpenAI key. Non-secret generation IDs bind
settings to the credential version and fail closed on mismatches. Discovery is
advisory and does not establish compatibility. Explicit synthetic embedding and
structured-output tests establish the required response contract.

Tailoring first records intent, embeds bounded role/evidence inputs, ranks them
locally, arms selected evidence, then requests a strict structured patch. The
engine validates provider output as untrusted content and saves a pinned preview.
It never mutates the canonical profile automatically. User revisions preserve the
original draft and are labeled as user-authored. Ambiguous paid requests are not
retried automatically. See PROVIDER_SPEND_CONTROL.md for local cost reservations.

## Build and testing

Dependencies are locked across npm, Cargo, and uv. The sidecar is built natively
per target and passes packaged document/domain smoke checks before bundling.
Dependency notices are generated from the actual release inputs and shipped with
the bundle. A clean-checkout check must generate required sidecar resources before
Cargo runs. Native-produced RPC regression tests exercise the Python boundary;
independent language-level fixtures alone are insufficient.

The first supported binary platform is macOS arm64. Additional platforms require
successful packaging, resource-bound verification, credential-store validation,
recovery tests, and UI QA before they are advertised as supported.

## Feature contracts

- MEMORY_CAPTURE_CONTRACT.md: private memories and explicit project suggestions.
- SOURCE_REVIEW_CONTRACT.md: conflicting facts, duplicates, and queued decisions.
- PROVIDER_SPEND_CONTROL.md: local usage estimates and conservative reservations.
- PRIVACY.md: storage, disclosure, retention, and reset boundaries.
- BACKUP_AND_RECOVERY.md: offline archives, restoration, and explicit migration.
