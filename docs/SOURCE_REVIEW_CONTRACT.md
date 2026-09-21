# Source checks — 0.4.15 contract

The first source-check slice covers deterministic Basic Details scalar conflicts
(name, headline, summary, email, phone, URL) and exact-byte duplicate documents.
It does not infer approximate duplicates or change retained original files.
Checks appear in the local review area alongside profile suggestions. Decisions
can be queued, undone, and applied together in one local transaction.

## Commands and renderer contract

All fields below are required; nullable fields serialize explicitly as null.
Responses are bounded and contain no paths, hashes, generic canonical JSON,
internal source identifiers, or raw extraction text.

- `list_source_review_items` / engine `profile.source_review.list` takes
  `{scope: inbox|deferred|history, limit: 1..10, offset: 0..10000}`.
- Returns `{current_profile_version_id: UUID|null, scope, offset, limit,
  total_items, next_offset: number|null, counts: {inbox,deferred,history}, items}`.
- Each item is `{id: UUID, kind: basic_conflict|duplicate_document,
  state: inbox|deferred|resolved, state_revision: number, title: string,
  field: name|headline|summary|email|phone|url|null,
  previous_value: string|null, proposed_value: string|null,
  resolution: keep_profile|use_source|acknowledge_duplicate|null,
  evidence: [{display_name, source_format, source_kind,
  location_label: string|null, excerpt: string}],
  is_previous_import_set: boolean, created_at_ms: number}`.
- Evidence uses the existing format/kind enums, at most two entries; filenames
  max 240, location labels max 120, excerpts max 360. Titles max 160. Scalar
  values obey existing Basic Details field limits (summary max 4000). A conflict
  has one incoming source; a duplicate has two source records and no excerpt.
- `apply_source_review_decisions` / engine `profile.source_review.apply` takes
  `{request_id: UUID, expected_parent_profile_version_id: UUID,
  decisions: [{item_id: UUID, expected_state: inbox|deferred,
  expected_revision: number, action: keep_profile|use_source|
  acknowledge_duplicate|defer|reopen}]}` (1..50 unique items).
- Returns `{request_id, created: boolean, created_at_ms,
  parent_profile_version_id: UUID, profile_version_id: UUID,
  version_number: number, profile_changed: boolean,
  decisions: [{item_id, action, state: inbox|deferred|resolved,
  state_revision}], counts: {inbox,deferred,history}}`.

Only Inbox items can be resolved or deferred. Reopen moves a Deferred item to
Inbox. Resolved and previous-import-set records are read-only History. Applying
an exact retry returns its original receipt, including after reset of the import
set; reusing its request ID for different decisions fails. An uncertain native
result keeps the exact pending batch available for retry.

All decisions pin current profile and item state. Using a source value also
requires the target field to still equal the imported previous value. Reject a
batch that chooses two source values for the same field. A batch writes at most
one immutable profile version, only if a source value changes the profile;
organizing or keeping values writes review history only. Preserve untouched
canonical metadata and use the existing Basic Details normalization and locking.

## Persistence and integration

Schema 18 adds separate immutable source-check records and append-only decision
receipts/events. Scan checks are bounded and pinned to committed import evidence.
Conflict proof is regenerated from the checksummed extraction and verified
against the field, normalized value, locator, and excerpt. Receipt replay
reconstructs any new profile from its parent and selected source facts.
Creation is atomic with import commit and idempotent on retry. First-profile
imports may produce checks; authoritative structured-profile imports do not.
Import-set reset moves checks to history. Career reset clears checks alongside
other career data and continues to preserve provider spend state.

System status/reset snapshots add `source_review_inbox_items`,
`source_review_deferred_items`, and `source_review_history_items`. Import preview
adds `source_review_count` and commit adds `source_review_count`; they remain
separate from existing candidate counts. The UI surfaces both in import receipts
and the Profile review entry. Native boundaries validate the exact typed shapes.
