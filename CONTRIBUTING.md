# Contributing to CVGnome

CVGnome helps an individual keep career evidence, review a profile, track roles,
and export resumes on their own computer. Keep the ordinary workflow usable
without an account, API key, model, or network connection.

Start with the build instructions in README.md. Open an issue describing the
user problem before a large change. Small fixes with a clear reproduction are
welcome. Use synthetic data and describe how to verify the resulting behavior.

Run `npm run check` before submitting a pull request. This prepares the native
engine as well as checking the source. Run `npm run native:lint` for Rust changes.
Changes to IPC must exercise native-produced requests against the engine;
matching independent unit tests alone can miss contract mismatches. Validate
packaged behavior when imports, document rendering, resource limits, or build
dependencies change.

Preserve these boundaries:

- React receives bounded public projections, not filesystem access, secrets,
  raw source contents, or complete canonical objects.
- Rust owns file dialogs, paths, key storage, provider transport, and request
  validation. No arbitrary shell commands or remote pages enter the WebView.
- Python owns local domain logic and SQLite. It performs no provider requests.
- Profile versions, retained evidence, and generated drafts remain immutable.
  Mutations must preserve expected-parent checks and safe idempotent retries.
- Model output is untrusted. Optional provider use must be explicit and must
  not silently fall back to another provider or retry an uncertain paid request.

Prefer focused modules and shared protocol fixtures. Include meaningful tests
for changed behavior, especially conflict handling, uncertain outcomes, data
recovery, and malformed inputs. Include keyboard and focus behavior in UI review.

Contributions to covered files are under MPL-2.0. Submit only material you have
the right to contribute, preserve third-party notices, and identify any copied
or adapted source and its license. No copyright assignment is requested.

Treat other contributors respectfully. Explain disagreements in terms of user
needs and evidence. Maintainers may close work outside the individual job-seeker
scope or work that introduces avoidable privacy and maintenance burdens.
