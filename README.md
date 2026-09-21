# CVGnome

A free desktop workspace for individual job seekers. Keep the evidence behind
your experience, build a profile you can review, track roles, and export resumes.
Your ordinary work stays on your computer and needs no account or AI provider.

- Start with career documents or enter your details manually.
- Review imported suggestions before they change your profile.
- Edit work, education, projects, and skills with recoverable version history.
- Save private career memories and choose what belongs in your public profile.
- Paste job descriptions, inspect profile connections, and track applications.
- Export DOCX or PDF. Optionally create tailored drafts with your own local model
  or OpenAI API account, then review and edit the result.

CVGnome is free and open source under [MPL-2.0](LICENSE). There is no CVGnome
subscription, activation, or paid feature unlock. Optional external API use may
incur charges from that provider.

## Get the app

Official downloads and release notes belong at
[GitHub Releases](https://github.com/praxish/cvgnome/releases). Check a release's
platform, signing status, known limitations, and checksums before installing it.
The first supported binary target is **macOS on Apple silicon (arm64)**.
Windows, Linux, and Intel Mac binaries are not currently verified or supported.

Version 0.5.0 is the first public release. Its macOS app is ad-hoc signed, with
no Apple Developer ID signature or notarization. See
[installation limitations](docs/KNOWN_LIMITATIONS.md) before downloading.
The binary requires macOS 11 or newer; release notes record the systems actually
tested. Download the arm64 ZIP, expand it, and move CVGnome.app to Applications.

## First use

Open CVGnome and choose documents, select a folder, or choose **Start manually**.
Review the resulting profile, correct its details, then export a resume or add a
role. **Models** is optional and can be left unconfigured. Nothing is submitted
to an employer by CVGnome.

If your first import has readable text but cannot build a complete profile,
choose **Complete details and keep files**. Confirm your name and optionally add
a summary. Leaving the summary blank keeps any summary already extracted. The
saved profile retains its original documents; finish missing sections in Profile
review before exporting. A file being read successfully does not mean every
resume field was recognized.

Use **Resume import review** for an unfinished first-profile import. After an uncertain
save, retry the exact request shown by the app before changing its details.
Unsubmitted edits are not autosaved, and temporary source previews expire.

Save a [career workspace backup](docs/BACKUP_AND_RECOVERY.md) before moving
computers or changing a workspace. Exported resumes do not contain the complete
history, memories, tracker, or retained documents.

## Build from source

Install Node.js/npm, uv, a stable Rust toolchain, and the
[Tauri platform prerequisites](https://v2.tauri.app/start/prerequisites/).
Use the toolchain versions recorded in this repository. On macOS, Python and
Rust must use the same architecture; avoid mixing Rosetta and native tools.

```sh
npm run bootstrap
npm run check
npm run desktop:dev
```

Bootstrap installs locked dependencies. The check command prepares the frozen
Python engine before native checks, so a fresh checkout does not need a
pre-existing sidecar binary. Development and builds use local toolchains;
installing dependencies requires network access, but ordinary app use does not.

```sh
npm run build          # TypeScript and frontend bundle
npm run engine:test    # Python domain tests
npm run contract:test  # native-generated requests against the engine
npm run native:lint    # Rust format and Clippy checks
npm run engine:smoke   # exercise an already built frozen engine
npm run desktop:build  # create local desktop bundles
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [architecture](docs/ARCHITECTURE.md).

## Privacy and optional models

CVGnome has no cloud backend or telemetry. Local files contain personal career
information and are not encrypted by the app. Retained imports and saved history
remain until the appropriate explicit reset. See [privacy](docs/PRIVACY.md),
[backup and recovery](docs/BACKUP_AND_RECOVERY.md), and
[provider spend controls](docs/PROVIDER_SPEND_CONTROL.md).

LM Studio connects through numeric loopback on your computer. OpenAI connects
directly to its fixed official endpoint with your own OS-stored API key.
Compatibility tests use synthetic content; tailoring sends bounded role and
resume evidence only when you request it. Review provider settings and costs
before use. The local spend guard estimates this installation's usage and does
not replace the provider's account billing controls.

## Project scope

CVGnome serves one person's job search. It is not a recruiting system, shared
candidate database, hosted career service, or automatic application-submission
agent. Deterministic profile connections are evidence to review, not a prediction
of hiring success. Review generated wording for accuracy before sharing it.

- [Release history](CHANGELOG.md)
- [Known limitations](docs/KNOWN_LIMITATIONS.md)
- [Synthetic examples](docs/examples/README.md)
- [Roadmap](docs/ROADMAP.md)
- [Report a vulnerability privately](SECURITY.md)
- [Source and asset provenance](docs/PROVENANCE.md)
- [Project identity](docs/BRANDING.md)

Third-party dependencies keep their own licenses. Official bundles include
license notices and an inventory. Original covered source and assets in this
repository use MPL-2.0; private repositories and unpublished work are outside
this publication.
