# Version 0.5 limitations

- The first downloadable application supports Apple silicon Macs. Intel Mac,
  Windows, and Linux binaries have not been verified.
- The app is not signed with an Apple Developer ID and is not notarized. macOS
  may block its first launch. Download only from this project's GitHub release,
  compare its SHA-256 checksum, and consult
  [Apple's instructions](https://support.apple.com/102445) for allowing a trusted
  app through Privacy & Security. Do not disable system-wide security checks.
- Backup, restore, and migration currently require the
  [offline command line](BACKUP_AND_RECOVERY.md). Archives are unencrypted.
- PDF export uses Bitstream Vera. Latin text is the tested baseline; complex
  scripts, right-to-left layout, and broad Unicode coverage are not guaranteed.
  Review exported documents before sending them. DOCX may be a more suitable
  editable starting point for text the bundled PDF fonts cannot render.
- Document extraction is bounded. macOS samples parser-process-group memory
  and stops a worker observed above the threshold; this is not a hard allocation
  cap. Unsupported structured-parser platforms fail closed.
- Document text may be readable even when resume fields are not recognized.
  Header and Summary recognition covers selected common text layouts; there is
  no OCR or general PDF layout understanding. For a first import with readable
  text, **Complete details and keep files** can save a named incomplete profile
  while retaining the sources. Files without readable text still require another
  source or manual entry. Export and tailoring remain unavailable until there is
  enough resume content.
  Work history with wrapped or multiple columns may need manual completion;
  unsupported layouts are left unasserted and reported as a partial import.
- First-import resume preserves an unexpired preview and any submitted recovery
  request. It does not autosave unsubmitted form edits or replace a complete
  career-workspace backup. Retry an uncertain save with its original details.
- Use the window close button, **CVGnome > Quit**, or **Cmd+Q** to get unsaved-draft
  protection. **Dock > Quit and system termination can bypass that protection**;
  save your work first. A crash, forced quit, or power loss can also lose unsaved
  drafts. Retry an uncertain write from its existing screen before closing.
  Stored profile versions and exact retries protect committed work, not every
  in-progress keystroke.
- Optional models require user setup and review. The provider spend guard is a
  local estimate, not a guarantee about account-wide charges.
- The UI has improved text sizes, keyboard close handling, and zoom up to 300%.
  A comprehensive assistive-technology audit and further layout polish remain
  follow-up work.

Signed distribution, graphical workspace backup/restore, broader fonts/platforms,
and continued accessibility improvements are tracked in the [roadmap](ROADMAP.md).
