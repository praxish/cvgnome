// SPDX-License-Identifier: MPL-2.0
import {
  TailoredResume,
  TailoredResumeRevisionEdits,
} from "./localProvider";

export const TAILORED_REVISION_LIMITS = {
  history: 50,
  headline: 200,
  summary: 700,
  workSummary: 420,
  highlight: 360,
  highlightsPerRole: 8,
  workEdits: 32,
} as const;

export type TailoredRevisionEditorWork = {
  sourceIndex: number;
  summary: string;
  highlightsText: string;
};

export type TailoredRevisionEditorState = {
  expectedParentRevisionId: string | null;
  expectedParentResumeChecksumSha256: string;
  parentResume: TailoredResume;
  label: string;
  summary: string;
  work: TailoredRevisionEditorWork[];
  requestId: string | null;
};

export type TailoredRevisionValidation = {
  edits: TailoredResumeRevisionEdits | null;
  errors: string[];
  hasChanges: boolean;
};

export type TailoredResumeDiffItem = {
  key: string;
  label: string;
  before: string | null;
  after: string | null;
};

export type TailoredRevisionErrorKind =
  | "conflict"
  | "invalid"
  | "missing"
  | "integrity"
  | "ambiguous";

const CONTROL_CHARACTER_RE = /\p{C}/u;

function utf8Length(value: string): number {
  return new TextEncoder().encode(value).length;
}

function cleanText(value: string): { value: string; hasUnsupportedControl: boolean } {
  const normalized = value.normalize("NFKC");
  const hasUnsupportedControl = Array.from(normalized).some(
    (character) => CONTROL_CHARACTER_RE.test(character) && !/\s/u.test(character),
  );
  return {
    value: normalized.split(/\s+/u).filter(Boolean).join(" "),
    hasUnsupportedControl,
  };
}

function parentText(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const cleaned = cleanText(value).value;
  return cleaned || null;
}

function sameStrings(left: string[], right: string[]): boolean {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

function normalizedParentHighlights(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((item): item is string => typeof item === "string")
    .map((item) => cleanText(item).value)
    .filter(Boolean);
}

export function sortedTailoredRevisions<T extends { id: string; revision_number: number }>(
  revisions: T[],
): T[] {
  return [...revisions].sort(
    (left, right) => left.revision_number - right.revision_number || left.id.localeCompare(right.id),
  );
}

export function latestTailoredRevision<T extends { revision_number: number }>(
  revisions: T[],
): T | null {
  return revisions.reduce<T | null>(
    (latest, revision) => !latest || revision.revision_number > latest.revision_number
      ? revision
      : latest,
    null,
  );
}

export function createTailoredRevisionEditor(
  parentResume: TailoredResume,
  expectedParentRevisionId: string | null,
  expectedParentResumeChecksumSha256: string,
): TailoredRevisionEditorState {
  const basics = parentResume.basics ?? {};
  return {
    expectedParentRevisionId,
    expectedParentResumeChecksumSha256,
    parentResume,
    label: typeof basics.label === "string" ? basics.label : "",
    summary: typeof basics.summary === "string" ? basics.summary : "",
    work: (parentResume.work ?? []).map((work, sourceIndex) => ({
      sourceIndex,
      summary: typeof work.summary === "string" ? work.summary : "",
      highlightsText: Array.isArray(work.highlights) ? work.highlights.join("\n") : "",
    })),
    requestId: null,
  };
}

export function isTailoredRevisionEditorDirty(editor: TailoredRevisionEditorState): boolean {
  const initial = createTailoredRevisionEditor(
    editor.parentResume,
    editor.expectedParentRevisionId,
    editor.expectedParentResumeChecksumSha256,
  );
  return editor.label !== initial.label ||
    editor.summary !== initial.summary ||
    editor.work.length !== initial.work.length ||
    editor.work.some((work, index) => (
      work.sourceIndex !== initial.work[index]?.sourceIndex ||
      work.summary !== initial.work[index]?.summary ||
      work.highlightsText !== initial.work[index]?.highlightsText
    ));
}

export function validateTailoredRevisionEditor(
  editor: TailoredRevisionEditorState,
): TailoredRevisionValidation {
  const errors: string[] = [];
  const basics: TailoredResumeRevisionEdits["basics"] = {};
  const workEdits: TailoredResumeRevisionEdits["work"] = [];
  const parentBasics = editor.parentResume.basics ?? {};

  const label = cleanText(editor.label);
  const parentLabel = parentText(parentBasics.label) ?? "";
  if (label.value !== parentLabel) {
    if (label.hasUnsupportedControl) errors.push("Headline contains an unsupported control character.");
    if (!label.value) errors.push("Headline cannot be empty.");
    if (label.value.length > TAILORED_REVISION_LIMITS.headline || utf8Length(label.value) > TAILORED_REVISION_LIMITS.headline) {
      errors.push(`Headline must fit within ${TAILORED_REVISION_LIMITS.headline} UTF-8 bytes.`);
    }
    if (label.value) basics.label = label.value;
  }

  const summary = cleanText(editor.summary);
  const parentSummary = parentText(parentBasics.summary);
  const nextSummary = summary.value || null;
  if (nextSummary !== parentSummary) {
    if (summary.hasUnsupportedControl) errors.push("Summary contains an unsupported control character.");
    if (summary.value.length > TAILORED_REVISION_LIMITS.summary || utf8Length(summary.value) > TAILORED_REVISION_LIMITS.summary) {
      errors.push(`Summary must fit within ${TAILORED_REVISION_LIMITS.summary} UTF-8 bytes.`);
    }
    basics.summary = nextSummary;
  }

  const parentWork = editor.parentResume.work ?? [];
  for (const editedWork of editor.work) {
    const parent = parentWork[editedWork.sourceIndex];
    if (!parent) {
      errors.push("A role in this revision no longer matches the immutable resume.");
      continue;
    }
    const edit: TailoredResumeRevisionEdits["work"][number] = {
      source_index: editedWork.sourceIndex,
    };

    const workSummary = cleanText(editedWork.summary);
    const nextWorkSummary = workSummary.value || null;
    if (nextWorkSummary !== parentText(parent.summary)) {
      if (workSummary.hasUnsupportedControl) {
        errors.push(`Work entry ${editedWork.sourceIndex + 1} summary contains an unsupported control character.`);
      }
      if (workSummary.value.length > TAILORED_REVISION_LIMITS.workSummary || utf8Length(workSummary.value) > TAILORED_REVISION_LIMITS.workSummary) {
        errors.push(`Work entry ${editedWork.sourceIndex + 1} summary must fit within ${TAILORED_REVISION_LIMITS.workSummary} UTF-8 bytes.`);
      }
      edit.summary = nextWorkSummary;
    }

    const rawHighlightLines = editedWork.highlightsText.split(/\r?\n/u);
    const cleanHighlights = rawHighlightLines
      .map((item) => cleanText(item))
      .filter((item) => item.value.length > 0);
    const nextHighlights = cleanHighlights.map((item) => item.value);
    if (!sameStrings(nextHighlights, normalizedParentHighlights(parent.highlights))) {
      if (cleanHighlights.some((item) => item.hasUnsupportedControl)) {
        errors.push(`Work entry ${editedWork.sourceIndex + 1} highlights contain an unsupported control character.`);
      }
      if (cleanHighlights.length > TAILORED_REVISION_LIMITS.highlightsPerRole) {
        errors.push(`Work entry ${editedWork.sourceIndex + 1} can contain at most ${TAILORED_REVISION_LIMITS.highlightsPerRole} highlights.`);
      }
      if (cleanHighlights.some((item) => item.value.length > TAILORED_REVISION_LIMITS.highlight || utf8Length(item.value) > TAILORED_REVISION_LIMITS.highlight)) {
        errors.push(`Each highlight in work entry ${editedWork.sourceIndex + 1} must fit within ${TAILORED_REVISION_LIMITS.highlight} UTF-8 bytes.`);
      }
      edit.highlights = nextHighlights;
    }

    if (Object.keys(edit).length > 1) workEdits.push(edit);
  }
  if (workEdits.length > TAILORED_REVISION_LIMITS.workEdits) {
    errors.push(`A single revision can update at most ${TAILORED_REVISION_LIMITS.workEdits} work entries.`);
  }

  const hasChanges = Object.keys(basics).length > 0 || workEdits.length > 0;
  return {
    edits: errors.length === 0 && hasChanges ? { basics, work: workEdits } : null,
    errors,
    hasChanges,
  };
}

export function applyTailoredRevisionEdits(
  parentResume: TailoredResume,
  edits: TailoredResumeRevisionEdits,
): TailoredResume {
  const result: TailoredResume = {
    ...parentResume,
    basics: { ...(parentResume.basics ?? {}) },
    work: (parentResume.work ?? []).map((work) => ({ ...work, highlights: work.highlights ? [...work.highlights] : undefined })),
  };
  const basics = result.basics ?? {};
  if (edits.basics.label !== undefined) basics.label = edits.basics.label;
  if (edits.basics.summary === null) delete basics.summary;
  else if (edits.basics.summary !== undefined) basics.summary = edits.basics.summary;
  result.basics = basics;

  const work = result.work ?? [];
  for (const edit of edits.work) {
    const entry = work[edit.source_index];
    if (!entry) continue;
    if (edit.summary === null) delete entry.summary;
    else if (edit.summary !== undefined) entry.summary = edit.summary;
    if (edit.highlights !== undefined) {
      if (edit.highlights.length === 0) delete entry.highlights;
      else entry.highlights = [...edit.highlights];
    }
  }
  result.work = work;
  return result;
}

function workLabel(resume: TailoredResume, sourceIndex: number): string {
  const work = resume.work?.[sourceIndex];
  const identity = [work?.position, work?.name].filter(Boolean).join(" · ");
  return identity || `Work entry ${sourceIndex + 1}`;
}

function highlightReview(value: unknown): string | null {
  const highlights = normalizedParentHighlights(value);
  return highlights.length ? highlights.map((item) => `• ${item}`).join("\n") : null;
}

export function diffTailoredResumes(
  generated: TailoredResume,
  selected: TailoredResume,
): TailoredResumeDiffItem[] {
  const result: TailoredResumeDiffItem[] = [];
  const generatedBasics = generated.basics ?? {};
  const selectedBasics = selected.basics ?? {};
  const basics: Array<["label" | "summary", string]> = [
    ["label", "Headline"],
    ["summary", "Professional summary"],
  ];
  for (const [field, label] of basics) {
    const before = parentText(generatedBasics[field]);
    const after = parentText(selectedBasics[field]);
    if (before !== after) result.push({ key: `basics-${field}`, label, before, after });
  }

  const workCount = Math.max(generated.work?.length ?? 0, selected.work?.length ?? 0);
  for (let sourceIndex = 0; sourceIndex < workCount; sourceIndex += 1) {
    const generatedWork = generated.work?.[sourceIndex];
    const selectedWork = selected.work?.[sourceIndex];
    const identity = workLabel(selectedWork ? selected : generated, sourceIndex);
    const beforeSummary = parentText(generatedWork?.summary);
    const afterSummary = parentText(selectedWork?.summary);
    if (beforeSummary !== afterSummary) {
      result.push({
        key: `work-${sourceIndex}-summary`,
        label: `${identity} — summary`,
        before: beforeSummary,
        after: afterSummary,
      });
    }
    const beforeHighlights = highlightReview(generatedWork?.highlights);
    const afterHighlights = highlightReview(selectedWork?.highlights);
    if (beforeHighlights !== afterHighlights) {
      result.push({
        key: `work-${sourceIndex}-highlights`,
        label: `${identity} — highlights`,
        before: beforeHighlights,
        after: afterHighlights,
      });
    }
  }
  return result;
}

export function classifyTailoredRevisionError(caught: unknown): TailoredRevisionErrorKind {
  const raw = caught instanceof Error ? caught.message : String(caught);
  const value = raw.trim().toLowerCase();
  if (
    value.includes("revision_conflict") ||
    value.includes("parent_revision") ||
    value.includes("parent_resume_checksum") ||
    value.includes("parent revision") ||
    value.includes("parent checksum") ||
    value.includes("expected parent") ||
    value.includes("tailoring_revision_conflict") ||
    value.includes("newer local revision") ||
    value.includes("changed before")
  ) return "conflict";
  if (
    value.includes("not_found") ||
    value.includes("not found") ||
    value.includes("no longer exists")
  ) return "missing";
  if (
    value.includes("integrity") ||
    value.includes("checksum") ||
    value.includes("lineage") ||
    value.includes("mismatch")
  ) return "integrity";
  if (
    value.includes("invalid") ||
    value.includes("unsupported") ||
    value.includes("too long") ||
    value.includes("too many") ||
    value.includes("no changes") ||
    value.includes("request_id_conflict") ||
    value.includes("idempotency")
  ) return "invalid";
  return "ambiguous";
}

export function safeTailoredRevisionListError(caught: unknown): string {
  const kind = classifyTailoredRevisionError(caught);
  if (kind === "missing") return "This generated draft is no longer available. Reload the role before opening its revision history.";
  if (kind === "integrity") return "CVGnome could not verify this draft’s local revision history. Nothing was changed.";
  return "CVGnome could not load this draft’s local revision history. The generated original remains available.";
}

export function safeTailoredRevisionGetError(caught: unknown): string {
  const kind = classifyTailoredRevisionError(caught);
  if (kind === "missing") return "This user revision is no longer available. Reload the revision history and choose another version.";
  if (kind === "integrity") return "CVGnome could not verify the selected revision’s immutable lineage, so it was not opened.";
  return "CVGnome could not open the selected local revision. The generated original remains unchanged.";
}

export function safeTailoredRevisionSaveError(caught: unknown): string {
  const kind = classifyTailoredRevisionError(caught);
  if (kind === "conflict") return "A newer local revision exists. CVGnome is reloading it and will keep your edits for review.";
  if (kind === "invalid") return "These edits did not pass CVGnome’s local revision rules. Review the marked limits and try again.";
  if (kind === "missing") return "The generated draft is no longer available, so this revision was not saved. Your edits remain on this screen.";
  if (kind === "integrity") return "CVGnome could not verify the revision’s immutable lineage, so it was not saved. Your edits remain on this screen.";
  return "CVGnome could not confirm whether the local revision save completed. Your edits and retry ID were kept; retrying is safe.";
}
