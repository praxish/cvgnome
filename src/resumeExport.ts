// SPDX-License-Identifier: MPL-2.0
export type ResumeFormat = "docx" | "pdf";

export type ResumeExportResult = {
  artifact_id: string;
  format: ResumeFormat;
  byte_size: number;
  checksum_sha256: string;
  suggested_filename: string;
  saved_filename: string | null;
};

export function formatBytes(byteSize: number): string {
  if (byteSize < 1_024) return `${byteSize} B`;
  if (byteSize < 1_048_576) return `${(byteSize / 1_024).toFixed(1)} KB`;
  return `${(byteSize / 1_048_576).toFixed(1)} MB`;
}

export function safeResumeExportError(
  caught: unknown,
  format: ResumeFormat,
  source: "profile" | "tailored-draft" | "tailored-revision",
): string {
  const raw = caught instanceof Error ? caught.message : String(caught);
  const value = raw.trim().toLowerCase();
  const formatLabel = format.toUpperCase();
  const sourceLabel = source === "profile"
    ? "current profile"
    : source === "tailored-revision"
      ? "selected user revision"
      : "generated original";

  if (value.includes("destination_exists") || value.includes("destination changed")) {
    return "The selected filename changed before the copy was saved. Choose another filename and try again.";
  }
  if (value.includes("ending in") || value.includes("extension")) {
    return `Choose a filename ending in .${format}.`;
  }
  if (
    value.includes("tailored_draft_not_found") ||
    value.includes("tailoring_draft_not_found") ||
    value.includes("draft no longer")
  ) {
    return "This generated draft is no longer available in the local vault. Reload the role before exporting.";
  }
  if (
    value.includes("tailored_revision_not_found") ||
    value.includes("revision_not_found") ||
    value.includes("revision no longer")
  ) {
    return "This user revision is no longer available in the local vault. Reload its history before exporting.";
  }
  if (value.includes("timed out")) {
    return `The local ${formatLabel} export took too long and was stopped. The ${sourceLabel} was not changed.`;
  }
  if (
    value.includes("unable to start the local engine") ||
    value.includes("local engine exited") ||
    value.includes("could not be monitored") ||
    value.includes("could not send")
  ) {
    return `The local engine could not create the ${formatLabel} copy. The ${sourceLabel} was not changed.`;
  }
  if (
    value.includes("integrity") ||
    value.includes("mismatch") ||
    value.includes("unsafe") ||
    value.includes("escaped") ||
    value.includes("artifact") ||
    value.includes("lineage")
  ) {
    return `CVGnome could not verify the local ${formatLabel} artifact, so no copy was saved.`;
  }
  if (value.includes("render") || value.includes("resume content")) {
    return `The ${sourceLabel} could not be rendered as ${formatLabel}. It was not changed.`;
  }
  return `CVGnome could not save a ${formatLabel} copy of the ${sourceLabel}. The ${sourceLabel} was not changed.`;
}
