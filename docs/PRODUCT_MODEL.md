# Product scope

CVGnome is a free, open-source desktop tool for individual job seekers. It helps
one person maintain career evidence, review and improve a profile, track roles,
and produce resumes. No account, activation, subscription, or purchase is
required. There is no CVGnome cloud service or telemetry collection.

The offline workflow is the baseline: document import, manual profile creation,
structured editing, immutable history, review queues, memories, deterministic
role comparisons, tracking, and DOCX/PDF export. Public profile changes always
have a deliberate user action. Memories and retained source documents remain
separate from public resume content.

Optional tailoring can use a locally operated LM Studio server or the user's
own OpenAI API account. Users supply their own models, compute, or API key and
pay any external provider charges directly. CVGnome neither resells inference
nor holds a shared provider credential. After generation, revising and exporting
the saved draft remain local operations.

The source license is MPL-2.0. Source rights, third-party notices, and project
identity are described in LICENSE, NOTICE, and BRANDING.md. Free distribution
does not make the software a promise of employment outcomes or a managed
support service.

This release does not include team recruiting, multiple candidate workspaces,
shared account administration, hosted authentication, billing, background job
search aggregation, or automated employer application submission. New features
should improve an individual's workflow without making a provider or network
connection a prerequisite for existing local work.
