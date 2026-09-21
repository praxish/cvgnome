# CVGnome engine

The engine owns local persistence and domain processing. It deliberately has no
dependency on FastAPI, PostgreSQL, hosted authentication, billing, or a shared
model-provider credential.

The RPC contract is versioned JSON Lines over standard input/output. The desktop
shell invokes an allowlisted one-shot command surface for status, canonical
profile import, manual profile start, source preview/commit/discard/list/reset,
opportunity matching and tracking, versioned profile review/edit/cleanup,
baseline resume rendering, and local-tailoring state transitions.
The same dispatcher is available through `rpc` for a future long-running
sidecar.

CVGnome 0.4.4 adds `profile.review` and `profile.review.section`. The summary
projects the exact current immutable profile version into allowlisted public
basics, contacts, and section counts. Section requests must repeat that opaque
profile-version ID and select an allowlisted section and bounded offset; unknown
or invalid requests fail closed. Pages contain only public review cards, never
canonical JSON, private metadata, source internals, or extracted content. Both
methods are read-only and make no model, provider, or network call. Editing and
review-inbox work was still tracked in the
[CVGnome roadmap](../docs/ROADMAP.md).

CVGnome 0.4.5 adds bounded profile-version list/get/diff projections and a
sparse Basic Details mutation. The write requires an exact current parent and
request UUID, records a durable input fingerprint, preserves untouched
canonical content, and appends one immutable version inside an immediate
transaction. Exact retries reuse the recorded outcome; stale parents and
request-ID reuse with different input fail without changing the vault. These
methods are local and make no provider, model, or network call. At that
milestone, the remaining structured editors and review inbox stayed sequenced
in the roadmap.

CVGnome 0.4.6 adds `profile.work.list` and `profile.work.update`. The bounded
list is pinned to an exact immutable profile version and returns allowlisted
work fields with the raw canonical list index used by typed add, update, and
remove operations. Dictionary entries with incomplete legacy fields remain
listable and repairable; non-dictionary values stay private and are never
silently renumbered into editable targets. Add and update require a non-empty
organization and position in the resulting entry. Sparse updates distinguish
omitted fields from explicit `null` clears, preserve untouched and unknown
canonical fields, and append a new parent-linked profile version. Schema 11's
immutable receipt records the expected parent, request fingerprint, operation,
raw index, and changed fields. Exact retries reuse the receipt; stale parents
and request-ID reuse with different input fail closed. The entire path is local
and makes no provider, model, network, or cloud-backend call.

CVGnome 0.4.7 adds `profile.versions.restore`. The request identifies both the
historical source version and the expected current parent, plus an idempotency
UUID. A successful restore copies the historical canonical JSON and checksum
exactly into a new `local_restore` version; it never rewrites, deletes, or
normalizes either prior version. Schema 12 durably records the source, parent,
output, fingerprint, and creation time, with database triggers enforcing that
the output is the next current child and an exact non-no-op copy of an older
snapshot. Exact retries return the recorded bounded public receipt even after
later profile changes, while stale parents, current-version targets, semantic
no-ops, and conflicting request reuse fail without appending history. Restore
is fully local and makes no provider, model, network, or cloud-backend call.

CVGnome 0.4.8 adds direct local editing for Projects, Education, and grouped
Skills through `profile.projects.*`, `profile.education.*`, and
`profile.skills.*` list/update pairs. Bounded list pages are pinned to an exact
immutable version and expose raw canonical indexes so malformed neighboring
values remain private without renumbering editable entries. Typed add, sparse
update, and remove operations require the exact current parent and an
idempotency UUID, preserve unexposed and unrelated canonical fields, and append
one `local_edit` version. Education edits add the engine-private
`meta.education_curated` marker for later source-review policy to distinguish
human-curated state from unreviewed signals. Schema 13 records all three section families
in one durable receipt table; exact retries reuse the verified receipt while
stale parents and conflicting request reuse fail closed. The entire path is
local and makes no provider, model, network, or cloud-backend call.

CVGnome 0.4.9 adds `workspace.reset`, a separately confirmed destructive
boundary for returning to the empty first-run state. The request carries a
canonical UUID and the exact profile, opportunity, artifact, import, preview,
and source-generation snapshot shown to the customer. A stale snapshot fails
closed. After validating the owner-controlled application-data root and its
managed targets, the engine journals and syncs removal of only the SQLite
database/WAL/SHM and the managed `sources/`, `imports/`, and `artifacts/`
trees. At that milestone recovery created and verified a pristine schema-13
vault and wrote a bounded durable receipt;
an exact retry returns that receipt without resetting newer work. Provider
configuration and credentials, interface preferences, original inputs, and exports outside application storage are not in the
deletion allowlist. This is a local product reset, not a forensic-erasure
guarantee.

CVGnome 0.4.10 adds `profile.changes.preview` and
`profile.changes.apply`. Preview reads one exact immutable version and returns
only conservative, conflict-free duplicate pairs for Work History, Projects,
Education, and Skills, together with a bounded public merged-entry projection.
Apply accepts one to 64 disjoint update, remove, or engine-issued merge
operations whose raw indexes all refer to one exact current parent. It
revalidates every merge, preserves unknown fields, applies section changes in
one pass, and appends one `local_edit` child inside one immediate transaction.
Schema 14 stores an immutable fingerprinted receipt, so exact retries reuse the
same result while stale parents, changed request reuse, invalid targets, and
changed suggestions fail without a partial version. At that milestone,
workspace reset recreated and verified a pristine schema-14 vault. Both
profile-change methods are deterministic and make no model, provider, network,
or cloud call.

CVGnome 0.4.11 adds `profile.start.manual` for an empty workspace. Its strict
request contains an explicit `null` parent, an idempotency UUID, and the same
bounded Basic Details patch accepted by later profile editing, with a required
non-empty name. One immediate transaction rejects any existing profile or
unfinished source preview, appends exactly version 1 with `local_start`
lineage, and stores an immutable schema-15 receipt. Exact retries resolve from
that receipt before current-state checks, including after later edits; changed
request reuse and competing first writers fail closed. Manual start creates no
source, import, extraction, or evidence rows, and cancellation is entirely a
desktop concern that never calls the engine. Profile review and history expose
this lineage as `manual_start`. The path is fully local and makes no model,
provider, network, or cloud call.

CVGnome 0.4.12 adds `review.inbox.list` and `review.inbox.transition`. When a
follow-up source import starts from an already renderable profile, deterministic
Education, Project, and Skill additions or extensions are removed from the
canonical draft and stored as immutable schema-16 proposals with one to three
locator-grounded excerpts from the committed source. First-profile and
non-renderable-profile bootstrap imports are unchanged, and the separate
canonical JSON import remains authoritative. Fixed Inbox, Deferred, and History
pages expose only bounded presentation fields, source aliases, human locators,
and excerpts. Defer, reject, and reopen require an exact state/revision and
request UUID; exact retries resolve from immutable transition receipts before
current-state checks. Starting a new source-retention generation makes its older
items read-only History. No inbox action appends a profile version, and this
release deliberately has no apply operation. The workflow is entirely local.

CVGnome 0.4.13 adds the strict `review.inbox.apply` mutation and an editable,
kind-specific `candidate` projection to each review card. The request pins the
item state/revision and exact current profile parent, supplies a full bounded
Education, Project, or Skill candidate, and carries an idempotency UUID. Apply
revalidates the immutable proposal, checksums, source/extraction links, and the
retained source file before changing anything. A successful schema-17
transaction appends exactly one `local_edit` profile version and one immutable
terminal `applied` receipt; the proposal, evidence, and source rows are never
rewritten. User edits affect only the section's public allowlist while unknown
source fields remain intact. Exact retries return the same output after later
profile changes with only `created: false`; stale parents, states, generations,
targets, changed UUID reuse, invalid edits, duplicates/no-ops, and missing or
tampered evidence fail closed. Applied items move to read-only History. The
workflow remains entirely local and makes no model, provider, network, or cloud
call.

CVGnome 0.4.14 leaves engine schema 17 and its provider-neutral tailoring
protocol unchanged. The native host now wraps direct OpenAI test and tailoring
requests in a separate owner-only cost reservation and usage ledger. That
operating-cost state lives outside SQLite so career reset cannot erase an
active monthly limit; it contains no engine payload, career material, or
credential. LM Studio and every deterministic engine command remain outside
third-party token billing.

CVGnome 0.4.15 adds schema 18 source checks through
`profile.source_review.list` and `profile.source_review.apply`. A source import
can hold Basic Details scalar conflicts and exact-byte duplicate pairs for
review, including during first-profile construction. Batches of 1–50 queued
decisions validate every item, source generation, and current profile parent
before writing. Accepted source values create at most one immutable profile
version; other decisions create only append-only review events. Proposal proof
is regenerated from the exact checksummed extraction, and receipt replay
reconstructs the saved output from its parent and selected source facts. Source
paths, hashes, and raw extraction remain internal. Career reset v3 includes
source-check totals and continues recovery of older v1/v2 reset state.

Source construction accepts the same native-staged manifest whether the
customer directly selected one or multiple files or used the bulk folder
picker. Supported inputs are bounded PDF, DOCX, JSON, Markdown, text, and CSV
files. PDF/DOCX extraction runs in a private
resource-bounded child process. Preview synthesis is deterministic and local;
the engine exposes only aggregate preview data to the renderer and creates no
profile version until explicit commit. Commit stores immutable SHA-256-addressed
source lineage, while discard or expiry removes temporary preview state and
staging. A separate bounded `profile.sources.list` method exposes only the
committed per-import alias and allowlisted lineage metadata for the desktop
Materials browser; it never returns a path, hash, internal source ID, source
content, extracted text, or canonical profile. This workflow makes no network,
provider, or model calls.

Opportunity work uses five allowlisted RPC methods:
`opportunities.save_match`, `opportunities.list`, `opportunities.get`, and
`opportunities.rematch`, plus `opportunities.update` for mutable tracker stage
and notes. A bounded pasted posting is stored as an immutable
snapshot, and every immutable evidence-alignment result pins both that snapshot
and the exact canonical profile version. Matching uses the explainable
`deterministic-local-v1` lexical contract; URLs are optional HTTPS metadata and
are never fetched. Lists omit posting descriptions and evidence details, while
detail receipts remain below the desktop sidecar response limit. Tracker writes
use optimistic concurrency and never alter prior posting snapshots or matches.

Model generation uses five allowlisted RPC methods: `tailoring.prepare`,
`tailoring.arm`, `tailoring.finalize`, `tailoring.fail`, and
`tailoring.latest`. Local user revisions add four more:
`tailoring.revisions.list`, `tailoring.revisions.get`,
`tailoring.revisions.create`, and `tailoring.revisions.export`. Prepare requires
a current matched role, pins the immutable
opportunity snapshot and profile version, derives the public baseline resume,
creates bounded evidence chunks, and commits an attempt before returning only
the embedding inputs. Oversized embedding queries are head-and-tail compacted
with a persisted quality code while the full bounded posting remains in the
later chat message. Arm validates finite same-dimension vectors, ranks
evidence locally, durably records at most eight selected chunks, and returns a
strict chat schema and bounded messages. Finalize parses the chat response as
untrusted JSON, permits only a narrow patch over existing resume content, runs
deterministic claim and language checks, and stores an immutable preview pinned
to both model IDs and an input fingerprint. Work-entry numbers must be grounded
in that exact source entry. Fail records one allowlisted safe failure code;
latest returns only the bounded most recent preview for a role. A new successful
prepare terminalizes any globally crash-stranded prepared or armed attempt
without deleting its immutable record.

The generated preview remains immutable and acts as revision zero. Revision
creation accepts only bounded edits to public headline, summary, and
summary/highlights on existing work entries. It uses an immediate transaction,
expected latest-parent ID/checksum, and a normalized request fingerprint to
append a linear immutable user-authored revision or safely recover an exact
retry. List returns only the newest 50 lightweight summaries; get returns the
selected full revision. Human edits receive local Unicode, structural, and size
validation, but they are not falsely labeled as having passed the generated
patch's evidence-grounding checks. These four methods make no provider call and
never mutate the canonical profile.

The engine itself still makes no network, provider, CLI, download, load, or
unload call and has no provider credential. The Rust host owns both implemented
transports: numeric-loopback LM Studio discovery and inference, or direct
OpenAI BYOK inference at the fixed official endpoint. The engine receives only
the provider kind and exact model IDs, so both paths use the same durable,
provider-neutral contract. Embedding and generation remain strictly sequential.
This ordering lets customer-controlled [LM Studio JIT loading and
auto-evict](https://lmstudio.ai/docs/developer/core/ttl-and-auto-evict) keep
model residency bounded. No failed request is retried or falls through to a
different provider or CVGnome cloud backend, because none exists.

Tailored results never alter the canonical profile or baseline resume
artifacts. Introduced in 0.3.1, original-draft export renders an exact immutable
generated preview as deterministic DOCX or PDF. Version 0.4.3 adds exact
revision export: it resolves the requested stored revision rather than the
latest state and pins the managed artifact to both its base draft and revision
plus the result checksum. Both paths safely reuse verified retries and make no
additional model request. Export is the customer's acceptance of those exact
bytes; there is still no mutable accept, canonical merge, or publish method. See
[`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) and LM
Studio's [OpenAI-compatible endpoint
documentation](https://lmstudio.ai/docs/developer/openai-compat).
