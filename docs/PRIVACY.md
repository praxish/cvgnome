# Privacy and local data

CVGnome is a desktop tool for one person's career workspace. It has no CVGnome
account, cloud backend, telemetry endpoint, model relay, or runtime subscription.
Building and editing a profile, saving memories, importing a pasted role,
deterministic matching, reviewing history, and exporting DOCX/PDF are local.

The application stores a SQLite vault, retained copies of imported documents,
managed exports, and non-secret provider settings in its application-data
directory. On supported macOS systems, app-managed data is restricted to the
owning user. This is not application-level encryption. Other software running
as that user, device backups, and an administrator may be able to access it.

Original files remain where the user selected them. Successful imports retain
verified exact copies so a profile's evidence and history can be inspected later.
Importing a file can therefore create another sensitive copy. A resume export
is a document copy, not a backup of the complete career workspace.

Memories preserve the original narrative locally. Saving a memory does not
change a profile or call a model. Preparing a project suggestion lets the user
choose public wording; applying the reviewed suggestion creates a profile version.

Provider use is optional:

- LM Studio runs on an explicit numeric loopback address on the same computer.
  CVGnome does not manage model downloads or the server lifecycle.
- Direct OpenAI use requires the user's own API key. The operating system's
  credential store holds that key; the vault and settings files do not. Model
  discovery sends the credential to the fixed official model-list endpoint,
  without career content. Discovery can run when the OpenAI Models view opens
  or the stored key changes. The synthetic compatibility test sends synthetic
  content. Tailoring is a separate explicit action that sends bounded role and
  public resume evidence to OpenAI; see the provider contract for exact fields.

Career narratives and resume evidence may themselves contain identifying text.
Omitting dedicated contact fields from a request is not anonymization. The
selected provider's policies govern its processing and retention. Optional API
charges belong to the user's provider account; the local spend guard is an
estimate for this installation, not an account-wide billing limit.

Starting a new import set preserves historical evidence and profile versions.
Resetting the career workspace is a separate confirmed operation that removes
managed career data while preserving provider configuration, the OS-stored key,
the spend ledger, and interface preferences. It does not remove original inputs,
external exports, backups, or filesystem snapshots, and is not forensic erasure.

Career-workspace backups contain personal information and are not encrypted by
CVGnome. Keep them in protected storage. OS-stored credentials and interface
preferences are outside the career backup. Re-enter the provider key on a new
device and verify model settings before making provider requests.

Use synthetic data in public issues and screenshots. Never upload a real vault
or backup to demonstrate a problem.
