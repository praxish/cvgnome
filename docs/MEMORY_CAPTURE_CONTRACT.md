# Memory capture — 0.4.16 contract

## Customer flow

From Profile, open **Memories** and save a story in your own words. A title is
optional. The original narrative is immutable, stays on this computer, and can
be read again in full. Saving a memory does not edit the profile or create a
profile version.

**Prepare project suggestion** is a separate, explicit action. The customer
chooses a project name and an optional short public description. CVGnome does
not automatically copy the private story into the resume, extract skills, or
guess dates, metrics, role attachments, or accomplishments. The project enters
the existing Profile suggestions inbox. Apply, Edit before applying, Defer,
Reject, and History use the same profile-review boundary as imported proposals.
Only Apply appends a new immutable profile version.

This slice requires an existing named profile and makes no provider or model
call. It incurs no third-party model cost. Optional model-assisted extraction,
multiple proposal types per story, memory revisions, and work-role attachment
are later work. A memory supports one project suggestion in this first slice;
edit that suggestion before applying rather than creating duplicates.

## Evidence and lifecycle

- Schema 19 stores an immutable, checksummed original narrative, optional title,
  capture-time profile reference, retention generation, and save receipt.
- The narrative is preserved exactly, including line breaks, spacing, and tabs.
  It is limited to 12,000 Unicode characters and 48,000 UTF-8 bytes. Unsupported
  control characters and empty stories are rejected, not silently stripped.
- Titles are optional and limited to 160 characters. Project names are limited
  to 200 characters and descriptions to 600 characters, matching the existing
  project editor's bounded public fields.
- Saving and preparing suggestions pin the current profile and retention
  generation. Exact request retries reuse the original result; changed reuse,
  stale parents, and stale generations fail closed.
- Review evidence links directly to the original memory, separately from an
  imported document. Existing imported-source review provenance and immutable
  decision receipts remain valid after upgrade.
- Starting a new import set archives earlier memories and their outstanding
  suggestions as read-only history. It does not delete the originals. Reset
  career workspace removes memories with the managed career database; it still
  preserves provider settings, keys, and the independent spend ledger.

Workspace-reset contract v4 adds the memory count and recognizes earlier
v1–v3 receipts and interrupted journals. A populated schema-18 upgrade must
preserve imported proposals, decisions, and exact apply retries as well as
pass a foreign-key check before the migration commits.

## Narrow desktop boundary

The engine exposes `memory.list`, `memory.get`, `memory.save`, and
`memory.propose_project` through typed native commands. List pages contain at
most ten bounded summaries and no full narratives. Detail explicitly returns
one original story. Paths, checksums, raw database rows, unrelated profile
content, and private imported documents are not exposed by these commands.

The review inbox continues to expose a bounded source excerpt, proposal fields,
and a human-readable **Original memory** source label. Saving a memory and
queuing a project suggestion do not add imports or synthetic profile versions.
The source narrative is evidence, not a model instruction or an independently
verified claim.
