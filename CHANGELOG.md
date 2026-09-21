# Changelog

## 0.5.1 — 2026-09-21

First-profile imports with readable text can now use **Complete details and keep
files** when the extracted draft needs attention. Confirm a name and optionally
add a summary, retain the original documents and extracted content, and finish
the profile in Profile review. An incomplete profile can be saved; export and
tailoring still require enough resume content.

Unfinished first-profile previews can be resumed while they remain valid.
Submitted corrections are pinned for exact retries, and an interrupted commit
rolls back to its resumable preview. Confirming an already extracted name is
accepted without requiring invented changes or an optional summary.

Document recognition now handles several common PDF text orders: contact details
before a name, repeated header/footer names, names beside contact details, and a
leading Summary section. Ambiguous names still require confirmation. This is a
bounded deterministic extraction improvement, not OCR or general PDF layout
understanding. Regression coverage exercises the source, native bridge, and
frozen engine recovery contracts.

Unlabeled work-history recognition now requires two unambiguous role/employer
columns and a separate date-only range. Repeated page headers, extra columns,
and hyphenated prose no longer become invented jobs or dates. A work section
that cannot be mapped raises a partial-import warning; its original file remains
available for review and manual completion.

## 0.5.0 — 2026-09-21

The initial public release brings CVGnome's local profile and role workspace to
an MPL-2.0 source repository, with a free individual job-seeker product scope.
The first binary targets macOS arm64, without Developer ID signing or
notarization. GitHub release notes list the exact artifacts, checksums, and
verification results.

This release fixes empty Roles searches, tests native/engine
contracts, enforces and documents document-parser resource protection, repairs
clean-checkout bootstrap, adds native close protection and frontend coverage,
improves readability/zoom, and adds offline career-workspace backup/recovery. Publication removes private development history and former
product branding, and includes third-party notices and contributor guidance.

## Development milestones through 0.4.16

- Local immutable profile versions, structured edits, history comparison and
  restoration, queued cleanup, and manual first-profile creation.
- Bounded document import, retained source evidence, suggestions, conflict and
  duplicate review, and explicit reviewed apply.
- Private memories and user-authored project suggestions.
- Local role tracking and explainable deterministic profile connections.
- Optional LM Studio or direct OpenAI tailoring, immutable drafts, local
  revisions, DOCX/PDF export, and local provider spend controls.

These entries describe private development milestones, not separately published
or supported public releases.
