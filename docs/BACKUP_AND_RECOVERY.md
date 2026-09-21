# Backup, restore, and migration

A resume export is not a workspace backup. The offline commands below preserve
profile history, memories, roles, retained documents, managed exports, and local
provider settings/spend records. They exclude OS-stored API keys and interface
preferences. Backup ZIPs are **unencrypted personal data**; keep them in protected
storage and never attach them to a public issue.

## Before you begin

Quit CVGnome and any earlier development build. Wait for imports, exports, or
model requests to finish first. `--app-closed` acknowledges this requirement; it
does not close the app or acquire a writer lock. The tool detects ordinary source
changes during copying and stops, but cannot make a running app safe to copy.

These commands use the engine inside an app installed in `/Applications`:

```sh
CVGNOME_ENGINE="/Applications/CVGnome.app/Contents/MacOS/cvgnome-engine"
CVGNOME_DATA="$HOME/Library/Application Support/com.cvgnome.desktop"
```

If your app is elsewhere, change `CVGNOME_ENGINE`. From a source checkout, replace
`"$CVGNOME_ENGINE"` with `uv run --locked --project engine cvgnome-engine`.
Choose paths yourself; the tools do not search for another application's data.

## Create a backup

Choose a new filename in an existing directory outside the workspace:

```sh
"$CVGNOME_ENGINE" backup --data-dir "$CVGNOME_DATA" \
  --output "$HOME/Documents/cvgnome-backup-2026-09-21.zip" --app-closed
```

Success prints JSON containing `"ok": true` and exits with status zero. Existing
files are never overwritten. The archive includes a consistent SQLite snapshot
(including committed WAL content), files, and a checksum manifest. Limits are
20,000 workspace entries, 1 GiB per file, and 2 GiB total uncompressed content.
Links and special files are refused.

Test a backup by restoring it to a new temporary workspace directory, then
inspecting the status. A successful restore checks paths, sizes, checksums,
database identity, migration history, integrity, and relationships:

```sh
"$CVGNOME_ENGINE" restore \
  --archive "$HOME/Documents/cvgnome-backup-2026-09-21.zip" \
  --data-dir "$HOME/Documents/cvgnome-backup-check" --app-closed
"$CVGNOME_ENGINE" status --data-dir "$HOME/Documents/cvgnome-backup-check"
```

The check directory must not already exist and contains sensitive data too.
Checksums detect accidental corruption; they do not authenticate an archive's
author. Restore only your own trusted backups.

## Restore the application's workspace

Quit the app. Preserve any existing `com.cvgnome.desktop` directory by moving it
to a clearly named, protected location before restoring. Do not delete your only
copy. The destination must not exist, even as an empty directory.

```sh
"$CVGNOME_ENGINE" restore \
  --archive "$HOME/Documents/cvgnome-backup-2026-09-21.zip" \
  --data-dir "$CVGNOME_DATA" --app-closed
```

Only after success, open CVGnome and inspect your profile, history, roles, and
retained sources. Keep the original backup until that review is complete.
Re-enter your API key if needed; check Models before requesting paid inference.
A failed restore leaves the original archive and an existing destination alone.

## Move an earlier development workspace

Version 0.5 uses a new application identity and database filename. It deliberately
starts independently rather than silently opening an older application's data.
**Migrate before first launching 0.5** if you want the earlier workspace to become
the new default. If you already launched it, preserve its new directory as
described above. Close both versions before migrating.

```sh
"$CVGNOME_ENGINE" migrate \
  --source-dir "/absolute/path/to/your/previous/application-data-directory" \
  --data-dir "$CVGNOME_DATA" --app-closed
```

Replace the source placeholder with the exact earlier application-data directory.
Recognized legacy identifiers are isolated in
[`legacy_compatibility.py`](../engine/src/cvgnome_engine/legacy_compatibility.py).
The command validates the original database and copies into a new destination;
it never renames, writes to, or deletes the old workspace. It does not copy OS
credentials. Keep the original until you have inspected the migrated result.

If a command reports an unfinished reset or database journal, reopen and cleanly
quit the original app, then retry. For an integrity/schema failure, preserve the
inputs and ask for help with the error code and version only. Never post the
database or archive publicly. A graphical recovery flow is planned after 0.5.
