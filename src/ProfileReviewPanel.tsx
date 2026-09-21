// SPDX-License-Identifier: MPL-2.0
import { FormEvent, KeyboardEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { useDesktopCloseProtection } from "./DesktopCloseGuard";
import {
  BasicDetailsFields,
  validateBasicDetailsForm,
  type BasicDetailsField,
  type BasicDetailsForm,
} from "./BasicDetailsFields";
import {
  buildCollectionPatch,
  collectionFormFromEntry,
  collectionFormHasInput,
  collectionEntryTitle,
  collectionInputFromForm,
  collectionLabel,
  collectionSingular,
  emptyCollectionForm,
  isEditableProfileSection,
  ProfileCollectionEditor,
  ProfileCollectionSection,
  type EditableProfileSection,
  type ProfileCollectionEntry,
  type ProfileCollectionForm,
  type ProfileCollectionOperation,
  type ProfileCollectionPage,
  type ProfileCollectionUpdateReceipt,
  validateCollectionForm,
} from "./ProfileCollections";

export type ProfileReviewContact = {
  kind: "email" | "phone" | "location" | "website" | "profile";
  label: string;
  value: string;
};

export type ProfileReviewSectionSummary = {
  key: ProfileReviewSectionKey;
  label: string;
  count: number;
};

export type ProfileReviewSummary = {
  profile_version_id: string;
  version_number: number;
  created_at_ms: number;
  source_kind: ProfileReviewSourceKind;
  renderable: boolean;
  name: string;
  headline: string | null;
  summary: string | null;
  contacts: ProfileReviewContact[];
  sections: ProfileReviewSectionSummary[];
};

export type ProfileReviewDetail = {
  label: string;
  value: string;
};

export type ProfileReviewSectionItem = {
  ordinal: number;
  title: string;
  subtitle: string | null;
  date_range: string | null;
  location: string | null;
  summary: string | null;
  highlights: string[];
  tags: string[];
  details: ProfileReviewDetail[];
};

export type ProfileReviewSectionPage = {
  profile_version_id: string;
  key: ProfileReviewSectionKey;
  label: string;
  total_items: number;
  offset: number;
  limit: number;
  next_offset: number | null;
  items: ProfileReviewSectionItem[];
};

export type ProfileVersionSummary = {
  profile_version_id: string;
  parent_profile_version_id: string | null;
  version_number: number;
  created_at_ms: number;
  source_kind: ProfileReviewSourceKind;
  name: string;
  headline: string | null;
  renderable: boolean;
};

export type ProfileVersionPage = {
  current_profile_version_id: string | null;
  total_items: number;
  offset: number;
  limit: number;
  next_offset: number | null;
  items: ProfileVersionSummary[];
};

export type ProfileBasics = {
  profile_version_id: string;
  version_number: number;
  created_at_ms: number;
  renderable: boolean;
  name: string;
  headline: string | null;
  summary: string | null;
  email: string | null;
  phone: string | null;
  url: string | null;
  location: {
    city: string | null;
    region: string | null;
    country_code: string | null;
  };
};

export type ProfileBasicChange = {
  field: ProfileBasicField;
  label: string;
  before: string | null;
  after: string | null;
};

export type ProfileSectionChange = {
  key: ProfileReviewSectionKey;
  label: string;
  before_count: number;
  after_count: number;
  content_changed: boolean;
};

export type ProfileVersionDiff = {
  from_version: ProfileVersionSummary;
  to_version: ProfileVersionSummary;
  basic_changes: ProfileBasicChange[];
  section_changes: ProfileSectionChange[];
  total_changes: number;
};

export type ProfileBasicsUpdateReceipt = {
  request_id: string;
  profile_version_id: string;
  parent_profile_version_id: string;
  version_number: number;
  created_at_ms: number;
  created: boolean;
  changed_fields: ProfileBasicField[];
  profile_name: string;
  headline: string | null;
  renderable: boolean;
};

export type ProfileWorkEntry = {
  entry_index: number;
  name: string;
  position: string;
  url: string | null;
  start_date: string | null;
  end_date: string | null;
  summary: string | null;
  highlights: string[];
  location: string | null;
};

export type ProfileWorkPage = {
  profile_version_id: string;
  version_number: number;
  total_items: number;
  offset: number;
  limit: number;
  next_offset: number | null;
  items: ProfileWorkEntry[];
};

export type ProfileWorkUpdateReceipt = {
  request_id: string;
  profile_version_id: string;
  parent_profile_version_id: string;
  version_number: number;
  created_at_ms: number;
  created: boolean;
  operation: "add" | "update" | "remove";
  entry_index: number;
  changed_fields: ProfileWorkField[];
  profile_name: string;
  work_entries: number;
  renderable: boolean;
};

export type ProfileRestoreReceipt = {
  request_id: string;
  profile_version_id: string;
  parent_profile_version_id: string;
  restored_from_profile_version_id: string;
  restored_from_version_number: number;
  version_number: number;
  created_at_ms: number;
  created: boolean;
  profile_name: string;
  renderable: boolean;
};

type ProfileChangeSection = "work" | EditableProfileSection;

type ProfileChangeOperation =
  | {
      kind: "update";
      section: ProfileChangeSection;
      entry_index: number;
      patch: WorkEntryPatch | Record<string, string | string[] | null>;
    }
  | {
      kind: "remove";
      section: ProfileChangeSection;
      entry_index: number;
    }
  | {
      kind: "merge";
      section: ProfileChangeSection;
      keep_entry_index: number;
      remove_entry_index: number;
      suggestion_id: string;
    };

type ProfileMergeSuggestion = {
  suggestion_id: string;
  section: ProfileChangeSection;
  keep_entry_index: number;
  remove_entry_index: number;
  reason_codes: string[];
  merged_entry: Record<string, unknown>;
  changed_fields: string[];
};

type ProfileChangePreview = {
  profile_version_id: string;
  version_number: number;
  algorithm_version: number;
  suggestions: ProfileMergeSuggestion[];
  truncated: boolean;
};

export type ProfileChangesReceipt = {
  request_id: string;
  profile_version_id: string;
  parent_profile_version_id: string;
  version_number: number;
  created_at_ms: number;
  created: boolean;
  operation_count: number;
  changed_sections: ProfileChangeSection[];
  section_counts: Record<ProfileChangeSection, number>;
  profile_name: string;
  renderable: boolean;
};

type QueuedProfileChange = {
  operation: ProfileChangeOperation;
  suggestion: ProfileMergeSuggestion | null;
};

export type ProfileReviewPanelProps = {
  reloadKey: string;
  initialActiveSection: string;
  retainedSourceFiles: number;
  historicalSourceFiles: number;
  reviewItemCount: number;
  reviewInboxDisabled: boolean;
  canExport: boolean;
  exportBusy: "docx" | "pdf" | null;
  notice: { kind: "success" | "error" | "info"; message: string } | null;
  initialProfileVersionId: string | null;
  onBack: () => void;
  onActiveSectionChange: (section: string) => void;
  onDirtyChange: (dirty: boolean) => void;
  onMutationBusyChange: (busy: boolean) => void;
  onInspectedVersionChange: (profileVersionId: string | null) => void;
  onOpenImportedFiles: () => void;
  onOpenMemories: () => void;
  onOpenReviewInbox: () => void;
  onExport: (format: "docx" | "pdf", profileVersionId: string) => void;
  onProfileUpdated: (receipt: ProfileBasicsUpdateReceipt) => Promise<void>;
  onWorkUpdated: (receipt: ProfileWorkUpdateReceipt) => Promise<void>;
  onCollectionUpdated: (receipt: ProfileCollectionUpdateReceipt) => Promise<void>;
  onProfileChangesApplied: (receipt: ProfileChangesReceipt) => Promise<void>;
  onProfileRestored: (
    receipt: ProfileRestoreReceipt,
    currentProfile: ProfileReviewSummary | null,
  ) => Promise<void>;
  onProfileRefreshRequested: () => Promise<void>;
};

const OVERVIEW_KEY = "__overview__";
const HISTORY_PAGE_LIMIT = 20;

type ReviewMode = "profile" | "diff" | "edit" | "work-edit" | "collection-edit";

type ProfileReviewSourceKind =
  | "source_documents"
  | "structured_file"
  | "manual_start"
  | "local_edit"
  | "local_restore"
  | "other";
type ProfileReviewSectionKey =
  | "work"
  | "education"
  | "skills"
  | "projects"
  | "volunteer"
  | "certificates"
  | "awards"
  | "publications"
  | "languages"
  | "interests"
  | "references";
type ProfileBasicField = BasicDetailsField;
type ProfileWorkField =
  | "name"
  | "position"
  | "url"
  | "start_date"
  | "end_date"
  | "summary"
  | "highlights"
  | "location";

type BasicsForm = BasicDetailsForm;

type BasicsPatch = {
  name?: string;
  headline?: string | null;
  summary?: string | null;
  email?: string | null;
  phone?: string | null;
  url?: string | null;
  location?: {
    city?: string | null;
    region?: string | null;
    country_code?: string | null;
  };
};

type WorkForm = {
  name: string;
  position: string;
  url: string;
  startDate: string;
  endDate: string;
  summary: string;
  highlights: string;
  location: string;
};

type WorkEntryInput = {
  name: string;
  position: string;
  url: string | null;
  start_date: string | null;
  end_date: string | null;
  summary: string | null;
  highlights: string[];
  location: string | null;
};

type WorkEntryPatch = Partial<{
  name: string;
  position: string;
  url: string | null;
  start_date: string | null;
  end_date: string | null;
  summary: string | null;
  highlights: string[] | null;
  location: string | null;
}>;

type ProfileWorkOperation =
  | { kind: "add"; entry: WorkEntryInput }
  | { kind: "update"; entry_index: number; patch: WorkEntryPatch }
  | { kind: "remove"; entry_index: number };

type ProfileRestoreTarget = {
  sourceProfileVersionId: string;
  sourceVersionNumber: number;
  sourceRenderable: boolean;
  expectedParentProfileVersionId: string;
  expectedParentVersionNumber: number | null;
};

function humanize(value: string): string {
  const cleaned = value.trim().replace(/[_-]+/g, " ");
  if (!cleaned) return "Unknown";
  return cleaned.replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatTimestamp(value: number): string {
  const date = new Date(value);
  if (!Number.isFinite(value) || Number.isNaN(date.getTime())) return "Unknown";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function displayChangeValue(value: string | null): string {
  if (!value) return "Not set";
  return value.length > 600 ? `${value.slice(0, 599)}…` : value;
}

function safeProfileReviewError(caught: unknown, surface: "summary" | "section"): string {
  const value = (caught instanceof Error ? caught.message : String(caught)).toLowerCase();
  if (value.includes("vault_busy")) {
    return "The local vault is busy. Wait a moment and try again.";
  }
  if (
    value.includes("profile_missing")
    || value.includes("profile_review_not_found")
    || value.includes("profile_not_found")
    || value.includes("no_profile")
  ) {
    return "No saved profile is available to review yet.";
  }
  if (value.includes("profile_version_not_found") || value.includes("stale_profile_version")) {
    return "That saved profile version is no longer available. Refresh the profile review and try again.";
  }
  if (value.includes("vault_integrity_error")) {
    return "The local vault could not verify this saved profile version.";
  }
  return surface === "summary"
    ? "CVGnome could not open the saved profile review from the local vault."
    : "CVGnome could not open this profile section from the local vault.";
}

function safeProfileWorkspaceError(
  caught: unknown,
  action: "history" | "version" | "basics" | "diff" | "save" | "work" | "work-save" | "collection" | "collection-save" | "restore",
): string {
  const value = (caught instanceof Error ? caught.message : String(caught)).toLowerCase().slice(0, 2_000);
  if (
    (action === "work-save" || action === "collection-save")
    && (value.includes("must retain a non-empty name") || value.includes("edited profile must retain"))
  ) {
    return `${action === "work-save" ? "Work History" : "This profile section"} cannot be changed until Basic Details includes your name. Save a name there, then try this change again.`;
  }
  if (action === "restore" && value.includes("already matches the current profile")) {
    return "This historical snapshot already matches the current profile, so no new version is needed.";
  }
  if (action === "restore" && value.includes("must identify a historical version")) {
    return "That version is no longer a restorable historical snapshot. Reload and review the current profile.";
  }
  if (
    action === "restore"
    && (
      value.includes("profile_parent_conflict")
      || value.includes("profile_version_conflict")
      || value.includes("profile_update_conflict")
    )
  ) {
    return "The current profile changed before this snapshot could be restored. Reload the current version, then choose whether to restore this snapshot again.";
  }
  if (
    value.includes("profile_parent_conflict")
    || value.includes("profile_version_conflict")
    || value.includes("profile_update_conflict")
  ) {
    return "The current profile changed before this edit could be saved. Reload the latest version, review it, and apply your changes again.";
  }
  if (value.includes("request_conflict") || value.includes("idempotency")) {
    return action === "restore"
      ? "This restore request no longer matches the original snapshot. Reload the latest profile before trying again."
      : "This save request no longer matches the original edit. Reload the latest profile before trying again.";
  }
  if (value.includes("profile_version_not_found") || value.includes("stale_profile_version")) {
    return "That saved profile version is no longer available. Reload the current profile and try again.";
  }
  if (value.includes("profile_patch_empty") || value.includes("patch_empty")) {
    return action === "work-save" || action === "collection-save"
      ? "No profile-section changes are ready to save."
      : "No Basic Details changes are ready to save.";
  }
  if (value.includes("semantic change")) {
    return action === "work-save" || action === "collection-save"
      ? "Those edits do not change the saved profile entry after local normalization."
      : "Those edits do not change the saved Basic Details after local normalization.";
  }
  if (value.includes("vault_busy")) {
    return "The local vault is busy. Wait a moment and try again.";
  }
  if (value.includes("vault_integrity_error")) {
    return "The local vault could not verify the saved profile data.";
  }
  const fallbacks: Record<typeof action, string> = {
    history: "CVGnome could not open profile history from the local vault.",
    version: "CVGnome could not open that saved profile version.",
    basics: "CVGnome could not open Basic Details for this profile version.",
    diff: "CVGnome could not compare these saved profile versions.",
    save: "CVGnome could not save these Basic Details changes.",
    work: "CVGnome could not open Work History from the local vault.",
    "work-save": "CVGnome could not save this Work History change.",
    collection: "CVGnome could not open this profile section from the local vault.",
    "collection-save": "CVGnome could not save this profile-section change.",
    restore: "CVGnome could not restore this saved profile version.",
  };
  return fallbacks[action];
}

function formFromBasics(basics: ProfileBasics): BasicsForm {
  return {
    name: basics.name,
    headline: basics.headline ?? "",
    summary: basics.summary ?? "",
    email: basics.email ?? "",
    phone: basics.phone ?? "",
    url: basics.url ?? "",
    city: basics.location.city ?? "",
    region: basics.location.region ?? "",
    countryCode: (basics.location.country_code ?? "").toUpperCase(),
  };
}

function nullableFormValue(value: string): string | null {
  const normalized = value.trim();
  return normalized || null;
}

function buildBasicsPatch(original: ProfileBasics, form: BasicsForm): BasicsPatch {
  const patch: BasicsPatch = {};
  const normalizedName = form.name.trim();
  if (normalizedName !== original.name) patch.name = normalizedName;

  const nullableFields: Array<keyof Pick<BasicsForm, "headline" | "summary" | "email" | "phone" | "url">> = [
    "headline",
    "summary",
    "email",
    "phone",
    "url",
  ];
  nullableFields.forEach((field) => {
    const next = nullableFormValue(form[field]);
    if (next !== original[field]) patch[field] = next;
  });

  const location: NonNullable<BasicsPatch["location"]> = {};
  const locationFields: Array<{
    form: "city" | "region" | "countryCode";
    patch: "city" | "region" | "country_code";
    original: "city" | "region" | "country_code";
  }> = [
    { form: "city", patch: "city", original: "city" },
    { form: "region", patch: "region", original: "region" },
    { form: "countryCode", patch: "country_code", original: "country_code" },
  ];
  locationFields.forEach((field) => {
    const next = nullableFormValue(form[field.form]);
    if (next !== original.location[field.original]) location[field.patch] = next;
  });
  if (Object.keys(location).length > 0) patch.location = location;
  return patch;
}

function emptyWorkForm(): WorkForm {
  return {
    name: "",
    position: "",
    url: "",
    startDate: "",
    endDate: "",
    summary: "",
    highlights: "",
    location: "",
  };
}

function workFormFromEntry(entry: ProfileWorkEntry): WorkForm {
  return {
    name: entry.name,
    position: entry.position,
    url: entry.url ?? "",
    startDate: entry.start_date ?? "",
    endDate: entry.end_date ?? "",
    summary: entry.summary ?? "",
    highlights: entry.highlights.join("\n"),
    location: entry.location ?? "",
  };
}

function normalizedHighlights(value: string): string[] {
  return value
    .split(/\r?\n/)
    .map((highlight) => highlight.trim())
    .filter(Boolean);
}

function workEntryInputFromForm(form: WorkForm): WorkEntryInput {
  return {
    name: form.name.trim(),
    position: form.position.trim(),
    url: nullableFormValue(form.url),
    start_date: nullableFormValue(form.startDate),
    end_date: nullableFormValue(form.endDate),
    summary: nullableFormValue(form.summary),
    highlights: normalizedHighlights(form.highlights),
    location: nullableFormValue(form.location),
  };
}

function buildWorkPatch(original: ProfileWorkEntry, form: WorkForm): WorkEntryPatch {
  const input = workEntryInputFromForm(form);
  const patch: WorkEntryPatch = {};
  const scalarFields: Array<keyof Omit<WorkEntryInput, "highlights">> = [
    "name",
    "position",
    "url",
    "start_date",
    "end_date",
    "summary",
    "location",
  ];
  scalarFields.forEach((field) => {
    if (input[field] !== original[field]) patch[field] = input[field] as never;
  });
  if (JSON.stringify(input.highlights) !== JSON.stringify(original.highlights)) {
    patch.highlights = input.highlights.length > 0 ? input.highlights : null;
  }
  return patch;
}

function workEntryWithPatch(entry: ProfileWorkEntry, patch: WorkEntryPatch): ProfileWorkEntry {
  return {
    ...entry,
    ...patch,
    highlights: Object.prototype.hasOwnProperty.call(patch, "highlights")
      ? patch.highlights ?? []
      : entry.highlights,
  };
}

function collectionEntryWithPatch(
  section: EditableProfileSection,
  entry: ProfileCollectionEntry,
  patch: Record<string, string | string[] | null>,
): ProfileCollectionEntry {
  const next = { ...entry, ...patch };
  if (section === "projects") {
    const project = next as ProfileCollectionEntry & {
      highlights?: string[] | null;
      keywords?: string[] | null;
    };
    return {
      ...project,
      highlights: Array.isArray(project.highlights) ? project.highlights : [],
      keywords: Array.isArray(project.keywords) ? project.keywords : [],
    } as ProfileCollectionEntry;
  }
  if (section === "education") {
    const education = next as ProfileCollectionEntry & { courses?: string[] | null };
    return {
      ...education,
      courses: Array.isArray(education.courses) ? education.courses : [],
    } as ProfileCollectionEntry;
  }
  const skill = next as ProfileCollectionEntry & { keywords?: string[] | null };
  return {
    ...skill,
    keywords: Array.isArray(skill.keywords) ? skill.keywords : [],
  } as ProfileCollectionEntry;
}

function workEntryWithMerge(
  entry: ProfileWorkEntry,
  suggestion: ProfileMergeSuggestion,
): ProfileWorkEntry {
  return workEntryWithPatch(entry, suggestion.merged_entry as WorkEntryPatch);
}

function collectionEntryWithMerge(
  section: EditableProfileSection,
  entry: ProfileCollectionEntry,
  suggestion: ProfileMergeSuggestion,
): ProfileCollectionEntry {
  return collectionEntryWithPatch(
    section,
    entry,
    suggestion.merged_entry as Record<string, string | string[] | null>,
  );
}

function workFormHasInput(form: WorkForm): boolean {
  return Object.values(form).some((value) => value.trim().length > 0);
}

function mergeVersionItems(
  current: ProfileVersionSummary[],
  incoming: ProfileVersionSummary[],
): ProfileVersionSummary[] {
  const merged = new Map<string, ProfileVersionSummary>();
  current.forEach((item) => merged.set(item.profile_version_id, item));
  incoming.forEach((item) => merged.set(item.profile_version_id, item));
  return Array.from(merged.values()).sort((left, right) => right.version_number - left.version_number);
}

function mergeSectionItems(
  current: ProfileReviewSectionItem[],
  incoming: ProfileReviewSectionItem[],
): ProfileReviewSectionItem[] {
  const merged = new Map<number, ProfileReviewSectionItem>();
  current.forEach((item) => merged.set(item.ordinal, item));
  incoming.forEach((item) => merged.set(item.ordinal, item));
  return Array.from(merged.values()).sort((left, right) => left.ordinal - right.ordinal);
}

function mergeWorkItems(
  current: ProfileWorkEntry[],
  incoming: ProfileWorkEntry[],
): ProfileWorkEntry[] {
  const merged = new Map<number, ProfileWorkEntry>();
  current.forEach((item) => merged.set(item.entry_index, item));
  incoming.forEach((item) => merged.set(item.entry_index, item));
  return Array.from(merged.values()).sort((left, right) => left.entry_index - right.entry_index);
}

function mergeCollectionItems(
  current: ProfileCollectionEntry[],
  incoming: ProfileCollectionEntry[],
): ProfileCollectionEntry[] {
  const merged = new Map<number, ProfileCollectionEntry>();
  current.forEach((item) => merged.set(item.entry_index, item));
  incoming.forEach((item) => merged.set(item.entry_index, item));
  return Array.from(merged.values()).sort((left, right) => left.entry_index - right.entry_index);
}

const PROFILE_CHANGE_SECTIONS: readonly ProfileChangeSection[] = [
  "work",
  "projects",
  "education",
  "skills",
];

function isProfileChangeSection(value: unknown): value is ProfileChangeSection {
  return typeof value === "string"
    && PROFILE_CHANGE_SECTIONS.some((section) => section === value);
}

function profileChangeTargets(operation: ProfileChangeOperation): string[] {
  if (operation.kind === "merge") {
    return [
      `${operation.section}:${operation.keep_entry_index}`,
      `${operation.section}:${operation.remove_entry_index}`,
    ];
  }
  return [`${operation.section}:${operation.entry_index}`];
}

function queuedChangeForEntry(
  changes: QueuedProfileChange[],
  section: ProfileChangeSection,
  entryIndex: number,
): QueuedProfileChange | null {
  const target = `${section}:${entryIndex}`;
  return changes.find((change) => profileChangeTargets(change.operation).includes(target)) ?? null;
}

function queuedChangeLabel(
  change: QueuedProfileChange,
  entryIndex: number,
): "edit" | "removal" | "merge" | "merged-away" {
  if (change.operation.kind === "update") return "edit";
  if (change.operation.kind === "remove") return "removal";
  return change.operation.remove_entry_index === entryIndex ? "merged-away" : "merge";
}

function safeProfileChangesError(caught: unknown, action: "preview" | "apply"): string {
  const value = (caught instanceof Error ? caught.message : String(caught)).toLowerCase().slice(0, 2_000);
  if (value.includes("profile_changes_conflict")) {
    return "The current profile changed after these review choices were queued. Nothing was applied. Reload the latest profile or discard this queue.";
  }
  if (value.includes("profile_changes_request_conflict") || value.includes("idempotency")) {
    return "This apply request no longer matches the original queued changes. Nothing was applied again; reload the latest profile before continuing.";
  }
  if (value.includes("vault_busy")) {
    return "The local vault is busy. Wait a moment and try again.";
  }
  if (value.includes("vault_integrity_error")) {
    return "The local vault could not verify this profile change set.";
  }
  if (value.includes("invalid_params")) {
    return "One or more queued changes are no longer valid for this saved profile. Nothing was applied.";
  }
  return action === "preview"
    ? "CVGnome could not check this profile for conservative merge suggestions. Manual review still works."
    : "CVGnome could not apply these queued profile changes. Nothing was removed unless a saved receipt is shown.";
}

function mergeReasonLabel(reason: string): string {
  const labels: Record<string, string> = {
    matching_employer_role_and_dates: "Compatible employer, role, and dates",
    matching_project_name_and_dates: "Compatible project name and dates",
    matching_institution_program_and_dates: "Compatible institution, program, and dates",
    matching_skill_group: "Same skill group",
    same_identity: "Same identifying details",
    same_organization_role: "Same organization and role",
    overlapping_dates: "Overlapping dates",
    same_institution_study: "Same institution and study",
    same_name: "Same name",
    complementary_fields: "Complementary details",
  };
  return labels[reason] ?? humanize(reason);
}

function mergeSuggestionTitle(
  suggestion: ProfileMergeSuggestion,
  entryIndex: number,
  workItems: ProfileWorkEntry[],
  collectionItems: ProfileCollectionEntry[],
): string {
  if (suggestion.section === "work") {
    const entry = workItems.find((item) => item.entry_index === entryIndex);
    if (!entry) return `Work entry ${entryIndex + 1}`;
    return [entry.position, entry.name].filter(Boolean).join(" · ") || `Work entry ${entryIndex + 1}`;
  }
  const entry = collectionItems.find((item) => item.entry_index === entryIndex);
  return entry
    ? collectionEntryTitle(suggestion.section, entry)
    : `${collectionSingular(suggestion.section)} ${entryIndex + 1}`;
}

function mergedSuggestionTitle(suggestion: ProfileMergeSuggestion): string {
  const entry = suggestion.merged_entry;
  if (suggestion.section === "work") {
    return [entry.position, entry.name].filter((value) => typeof value === "string" && value).join(" · ")
      || "Combined work entry";
  }
  if (suggestion.section === "education") {
    return [entry.institution, entry.study_type, entry.area]
      .filter((value) => typeof value === "string" && value)
      .join(" · ") || "Combined education entry";
  }
  return typeof entry.name === "string" && entry.name
    ? entry.name
    : `Combined ${collectionSingular(suggestion.section)}`;
}

function mergedSuggestionDetail(
  suggestion: ProfileMergeSuggestion,
): string {
  const entry = suggestion.merged_entry;
  if (suggestion.changed_fields.length > 0) {
    const fieldLabels: Record<string, string> = {
      name: suggestion.section === "education" ? "Institution" : "Name",
      position: "Role",
      description: "Description",
      summary: "Summary",
      highlights: "Highlights",
      keywords: suggestion.section === "skills" ? "Skills" : "Keywords",
      courses: "Courses",
      level: "Level",
      area: "Area",
      study_type: "Credential",
      start_date: "Start",
      end_date: "End",
      location: "Location",
      url: "URL",
      score: "Score",
    };
    const outcomes = suggestion.changed_fields.slice(0, 3).map((field) => {
      const value = entry[field];
      if (Array.isArray(value)) {
        const shown = value.slice(0, 2).filter((item) => typeof item === "string");
        const suffix = value.length > shown.length ? ` +${value.length - shown.length}` : "";
        return `${fieldLabels[field] ?? humanize(field)}: ${shown.join(", ")}${suffix}`;
      }
      if (typeof value === "string" && value) {
        const clipped = value.length > 86 ? `${value.slice(0, 85)}…` : value;
        return `${fieldLabels[field] ?? humanize(field)}: ${clipped}`;
      }
      return fieldLabels[field] ?? humanize(field);
    });
    if (suggestion.changed_fields.length > outcomes.length) {
      outcomes.push(`+${suggestion.changed_fields.length - outcomes.length} more`);
    }
    return `Combined result · ${outcomes.join(" · ")}`;
  }
  const dates = [entry.start_date, entry.end_date]
    .filter((value) => typeof value === "string" && value)
    .join(" – ");
  const details: string[] = [];
  if (dates) details.push(dates);
  const listFields: Array<[unknown, string]> = suggestion.section === "education"
    ? [[entry.courses, "courses"]]
    : suggestion.section === "skills"
      ? [[entry.keywords, "skills"]]
      : [[entry.highlights, "highlights"], [entry.keywords, "keywords"]];
  listFields.forEach(([value, label]) => {
    if (Array.isArray(value) && value.length > 0) details.push(`${value.length} ${label}`);
  });
  return details.join(" · ") || "Keeps the first entry and removes the repeated copy";
}

function ProfileMergeSuggestions({
  suggestions,
  workItems,
  collectionItems,
  queuedChanges,
  busy,
  onQueue,
  onDismiss,
}: {
  suggestions: ProfileMergeSuggestion[];
  workItems: ProfileWorkEntry[];
  collectionItems: ProfileCollectionEntry[];
  queuedChanges: QueuedProfileChange[];
  busy: boolean;
  onQueue: (suggestion: ProfileMergeSuggestion, trigger: HTMLButtonElement) => void;
  onDismiss: (suggestionId: string, trigger: HTMLButtonElement) => void;
}) {
  if (suggestions.length === 0) return null;
  return (
    <section className="profile-merge-suggestions" aria-label="Suggested duplicate merges">
      <header>
        <div>
          <p className="section-kicker">Possible duplicates</p>
          <h4>{suggestions.length === 1 ? "One suggested merge" : `${suggestions.length} suggested merges`}</h4>
        </div>
        <span>Review each suggestion</span>
      </header>
      <div className="profile-merge-suggestions__list">
        {suggestions.map((suggestion) => {
          const suggestionTargets = new Set([
            `${suggestion.section}:${suggestion.keep_entry_index}`,
            `${suggestion.section}:${suggestion.remove_entry_index}`,
          ]);
          const overlapsQueue = queuedChanges.some((change) => (
            profileChangeTargets(change.operation).some((target) => suggestionTargets.has(target))
          ));
          return (
            <article key={suggestion.suggestion_id} className="profile-merge-suggestion">
              <div className="profile-merge-suggestion__material">
                <span>
                  {mergeSuggestionTitle(suggestion, suggestion.keep_entry_index, workItems, collectionItems)}
                  <b aria-hidden="true"> + </b>
                  {mergeSuggestionTitle(suggestion, suggestion.remove_entry_index, workItems, collectionItems)}
                </span>
                <strong><span aria-hidden="true">→</span> {mergedSuggestionTitle(suggestion)}</strong>
                <small className="profile-merge-suggestion__outcome">
                  {mergedSuggestionDetail(suggestion)}
                </small>
                <small>
                  {suggestion.reason_codes.map(mergeReasonLabel).join(" · ")}
                </small>
              </div>
              <div className="profile-merge-suggestion__actions">
                <button
                  type="button"
                  className="quiet-action"
                  onClick={(event) => onDismiss(suggestion.suggestion_id, event.currentTarget)}
                  disabled={busy}
                >
                  Keep separate
                </button>
                <button
                  type="button"
                  className="secondary-action"
                  onClick={(event) => onQueue(suggestion, event.currentTarget)}
                  disabled={busy || overlapsQueue}
                  title={overlapsQueue ? "Undo the overlapping queued change first" : undefined}
                >
                  Queue merge
                </button>
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}

const COLLECTION_LIST_COMMANDS: Record<EditableProfileSection, string> = {
  projects: "list_profile_projects",
  education: "list_profile_education",
  skills: "list_profile_skills",
};

const COLLECTION_UPDATE_COMMANDS: Record<EditableProfileSection, string> = {
  projects: "update_profile_projects",
  education: "update_profile_education",
  skills: "update_profile_skills",
};

export function ProfileReviewPanel({
  reloadKey,
  initialActiveSection,
  retainedSourceFiles,
  historicalSourceFiles,
  reviewItemCount,
  reviewInboxDisabled,
  canExport,
  exportBusy,
  notice,
  initialProfileVersionId,
  onBack,
  onActiveSectionChange,
  onDirtyChange,
  onMutationBusyChange,
  onInspectedVersionChange,
  onOpenImportedFiles,
  onOpenMemories,
  onOpenReviewInbox,
  onExport,
  onProfileUpdated,
  onWorkUpdated,
  onCollectionUpdated,
  onProfileChangesApplied,
  onProfileRestored,
  onProfileRefreshRequested,
}: ProfileReviewPanelProps) {
  const [profile, setProfile] = useState<ProfileReviewSummary | null>(null);
  const [currentProfileVersionId, setCurrentProfileVersionId] = useState<string | null>(null);
  const [activeSection, setActiveSection] = useState(initialActiveSection || OVERVIEW_KEY);
  const [reviewMode, setReviewMode] = useState<ReviewMode>("profile");
  const [sectionPage, setSectionPage] = useState<ProfileReviewSectionPage | null>(null);
  const [historyPage, setHistoryPage] = useState<ProfileVersionPage | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [loadingMoreHistory, setLoadingMoreHistory] = useState(false);
  const [switchingVersion, setSwitchingVersion] = useState(false);
  const [basics, setBasics] = useState<ProfileBasics | null>(null);
  const [basicsForm, setBasicsForm] = useState<BasicsForm | null>(null);
  const [basicsError, setBasicsError] = useState<string | null>(null);
  const [loadingBasics, setLoadingBasics] = useState(false);
  const [savingBasics, setSavingBasics] = useState(false);
  const [saveConflict, setSaveConflict] = useState(false);
  const [committedBasicsReceipt, setCommittedBasicsReceipt] = useState<ProfileBasicsUpdateReceipt | null>(null);
  const [workPage, setWorkPage] = useState<ProfileWorkPage | null>(null);
  const [workListError, setWorkListError] = useState<string | null>(null);
  const [workMutationError, setWorkMutationError] = useState<string | null>(null);
  const [loadingWork, setLoadingWork] = useState(false);
  const [loadingMoreWork, setLoadingMoreWork] = useState(false);
  const [workForm, setWorkForm] = useState<WorkForm | null>(null);
  const [workOriginal, setWorkOriginal] = useState<ProfileWorkEntry | null>(null);
  const [savingWork, setSavingWork] = useState(false);
  const [workSaveConflict, setWorkSaveConflict] = useState(false);
  const [workRequiresProfileName, setWorkRequiresProfileName] = useState(false);
  const [committedWorkReceipt, setCommittedWorkReceipt] = useState<ProfileWorkUpdateReceipt | null>(null);
  const [confirmingWorkRemoval, setConfirmingWorkRemoval] = useState<ProfileWorkEntry | null>(null);
  const [collectionPage, setCollectionPage] = useState<ProfileCollectionPage | null>(null);
  const [collectionListError, setCollectionListError] = useState<string | null>(null);
  const [collectionMutationError, setCollectionMutationError] = useState<string | null>(null);
  const [loadingCollection, setLoadingCollection] = useState(false);
  const [loadingMoreCollection, setLoadingMoreCollection] = useState(false);
  const [collectionForm, setCollectionForm] = useState<ProfileCollectionForm | null>(null);
  const [collectionOriginal, setCollectionOriginal] = useState<ProfileCollectionEntry | null>(null);
  const [savingCollection, setSavingCollection] = useState(false);
  const [collectionSaveConflict, setCollectionSaveConflict] = useState(false);
  const [collectionRequiresProfileName, setCollectionRequiresProfileName] = useState(false);
  const [committedCollectionReceipt, setCommittedCollectionReceipt] = useState<ProfileCollectionUpdateReceipt | null>(null);
  const [confirmingCollectionRemoval, setConfirmingCollectionRemoval] = useState<ProfileCollectionEntry | null>(null);
  const [changePreview, setChangePreview] = useState<ProfileChangePreview | null>(null);
  const [changePreviewError, setChangePreviewError] = useState<string | null>(null);
  const [loadingChangePreview, setLoadingChangePreview] = useState(false);
  const [dismissedSuggestionIds, setDismissedSuggestionIds] = useState<Set<string>>(() => new Set());
  const [queuedProfileChanges, setQueuedProfileChanges] = useState<QueuedProfileChange[]>([]);
  const [queuedParentProfileVersionId, setQueuedParentProfileVersionId] = useState<string | null>(null);
  const [confirmingChangeApply, setConfirmingChangeApply] = useState(false);
  const [applyingProfileChanges, setApplyingProfileChanges] = useState(false);
  const [profileChangesError, setProfileChangesError] = useState<string | null>(null);
  const [profileChangesConflict, setProfileChangesConflict] = useState(false);
  const [committedProfileChangesReceipt, setCommittedProfileChangesReceipt] = useState<ProfileChangesReceipt | null>(null);
  const [restoreTarget, setRestoreTarget] = useState<ProfileRestoreTarget | null>(null);
  const [committedRestoreReceipt, setCommittedRestoreReceipt] = useState<ProfileRestoreReceipt | null>(null);
  const [restoringProfile, setRestoringProfile] = useState(false);
  const [restoreError, setRestoreError] = useState<string | null>(null);
  const [restoreConflict, setRestoreConflict] = useState(false);
  const [versionDiff, setVersionDiff] = useState<ProfileVersionDiff | null>(null);
  const [diffError, setDiffError] = useState<string | null>(null);
  const [loadingDiff, setLoadingDiff] = useState(false);
  const [confirmingDiscard, setConfirmingDiscard] = useState(false);
  const [loadingProfile, setLoadingProfile] = useState(true);
  const [loadingSection, setLoadingSection] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [profileError, setProfileError] = useState<string | null>(null);
  const [sectionError, setSectionError] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const profileRequestRef = useRef(0);
  const sectionRequestRef = useRef(0);
  const historyRequestRef = useRef(0);
  const workRequestRef = useRef(0);
  const collectionRequestRef = useRef(0);
  const changePreviewRequestRef = useRef(0);
  const toolRequestRef = useRef(0);
  const historyPageRef = useRef<ProfileVersionPage | null>(null);
  const workPageRef = useRef<ProfileWorkPage | null>(null);
  const collectionPageRef = useRef<ProfileCollectionPage | null>(null);
  const basicsSaveRequestRef = useRef<{ key: string; requestId: string } | null>(null);
  const workSaveRequestRef = useRef<{ key: string; requestId: string } | null>(null);
  const collectionSaveRequestRef = useRef<{ key: string; requestId: string } | null>(null);
  const restoreRequestRef = useRef<{ key: string; requestId: string } | null>(null);
  const profileChangesSaveRequestRef = useRef<{ key: string; requestId: string } | null>(null);
  const workMutationInFlightRef = useRef(false);
  const basicsMutationInFlightRef = useRef(false);
  const collectionMutationInFlightRef = useRef(false);
  const restoreMutationInFlightRef = useRef(false);
  const profileChangesMutationInFlightRef = useRef(false);
  const editDirtyRef = useRef(false);
  const queuedChangesRef = useRef<QueuedProfileChange[]>([]);
  const currentProfileVersionIdRef = useRef<string | null>(null);
  const requestedInspectionRef = useRef<string | null>(initialProfileVersionId);
  const pendingNavigationRef = useRef<(() => void) | null>(null);
  const discardCancelRef = useRef<HTMLButtonElement>(null);
  const workRemovalCancelRef = useRef<HTMLButtonElement>(null);
  const workRemovalTriggerRef = useRef<HTMLButtonElement | null>(null);
  const collectionRemovalCancelRef = useRef<HTMLButtonElement>(null);
  const collectionRemovalTriggerRef = useRef<HTMLButtonElement | null>(null);
  const restoreCancelRef = useRef<HTMLButtonElement>(null);
  const restoreTriggerRef = useRef<HTMLButtonElement | null>(null);
  const editNameRef = useRef<HTMLInputElement>(null);
  const workPositionRef = useRef<HTMLInputElement>(null);
  const collectionFirstInputRef = useRef<HTMLInputElement>(null);
  const railButtonRefs = useRef(new Map<string, HTMLButtonElement>());
  const contentHeadingRef = useRef<HTMLHeadingElement>(null);
  const changeApplyCancelRef = useRef<HTMLButtonElement>(null);
  const changeApplyTriggerRef = useRef<HTMLButtonElement | null>(null);
  const suppressedReloadKeyRef = useRef<string | null>(null);
  const pendingNavigationPreservesQueueRef = useRef(false);

  const activeDescriptor = useMemo(
    () => profile?.sections.find((section) => section.key === activeSection) ?? null,
    [activeSection, profile],
  );
  const activeCollectionSection = isEditableProfileSection(activeSection) ? activeSection : null;
  workPageRef.current = workPage;
  collectionPageRef.current = collectionPage;

  const totalItems = useMemo(
    () => profile?.sections.reduce((total, section) => total + section.count, 0) ?? 0,
    [profile],
  );

  const isCurrentVersion = Boolean(
    profile && currentProfileVersionId && profile.profile_version_id === currentProfileVersionId,
  );

  const basicsPatch = useMemo(
    () => (basics && basicsForm ? buildBasicsPatch(basics, basicsForm) : {}),
    [basics, basicsForm],
  );
  const basicsDirty = !committedBasicsReceipt && Object.keys(basicsPatch).length > 0;
  const workPatch = useMemo(
    () => (workOriginal && workForm ? buildWorkPatch(workOriginal, workForm) : {}),
    [workForm, workOriginal],
  );
  const workDirty = reviewMode === "work-edit"
    && !committedWorkReceipt
    && Boolean(workForm)
    && (workOriginal ? Object.keys(workPatch).length > 0 : workFormHasInput(workForm as WorkForm));
  const collectionPatch = useMemo(
    () => (
      collectionOriginal && collectionForm
        ? buildCollectionPatch(collectionForm.section, collectionOriginal, collectionForm)
        : {}
    ),
    [collectionForm, collectionOriginal],
  );
  const collectionDirty = reviewMode === "collection-edit"
    && !committedCollectionReceipt
    && Boolean(collectionForm)
    && (collectionOriginal
      ? Object.keys(collectionPatch).length > 0
      : collectionFormHasInput(collectionForm as ProfileCollectionForm));
  const editorDirty = (reviewMode === "edit" && basicsDirty) || workDirty || collectionDirty;
  editDirtyRef.current = editorDirty;
  queuedChangesRef.current = queuedProfileChanges;
  const queuedChangeCount = queuedProfileChanges.length;
  const queuePinnedToCurrent = queuedChangeCount === 0 || Boolean(
    profile
    && isCurrentVersion
    && queuedParentProfileVersionId === profile.profile_version_id,
  );
  const queuedSectionCounts = useMemo(() => Object.fromEntries(
    PROFILE_CHANGE_SECTIONS.map((section) => [
      section,
      queuedProfileChanges.filter((change) => change.operation.section === section).length,
    ]),
  ) as Record<ProfileChangeSection, number>, [queuedProfileChanges]);
  const confirmingRestore = restoreTarget !== null;
  const profileMutationBusy = savingBasics
    || savingWork
    || savingCollection
    || restoringProfile
    || applyingProfileChanges;
  useDesktopCloseProtection({
    dirty: editorDirty || queuedChangeCount > 0,
    blocked: profileMutationBusy
      || Boolean(basicsSaveRequestRef.current && basicsError && !committedBasicsReceipt)
      || Boolean(workSaveRequestRef.current && workMutationError && !committedWorkReceipt)
      || Boolean(collectionSaveRequestRef.current && collectionMutationError && !committedCollectionReceipt)
      || Boolean(profileChangesSaveRequestRef.current && profileChangesError && !committedProfileChangesReceipt)
      || Boolean(restoreRequestRef.current && restoreError && !committedRestoreReceipt),
  });
  const workspaceBusy = loadingProfile
    || loadingHistory
    || loadingMoreHistory
    || switchingVersion
    || loadingBasics
    || savingBasics
    || loadingDiff
    || loadingWork
    || loadingMoreWork
    || loadingCollection
    || loadingMoreCollection
    || profileMutationBusy;

  const updateInspectedProfileVersion = useCallback((profileVersionId: string | null) => {
    requestedInspectionRef.current = profileVersionId;
    onInspectedVersionChange(profileVersionId);
  }, [onInspectedVersionChange]);

  const syncParentMutationBusy = useCallback(() => {
    onMutationBusyChange(
      basicsMutationInFlightRef.current
      || workMutationInFlightRef.current
      || collectionMutationInFlightRef.current
      || restoreMutationInFlightRef.current
      || profileChangesMutationInFlightRef.current,
    );
  }, [onMutationBusyChange]);

  const loadProfile = useCallback(async (clearInspection = true) => {
    const requestId = ++profileRequestRef.current;
    sectionRequestRef.current += 1;
    workRequestRef.current += 1;
    collectionRequestRef.current += 1;
    setLoadingProfile(true);
    setLoadingSection(false);
    setLoadingMore(false);
    setLoadingWork(false);
    setLoadingMoreWork(false);
    setLoadingCollection(false);
    setLoadingMoreCollection(false);
    setProfileError(null);
    setSectionError(null);
    setSectionPage(null);
    setWorkPage(null);
    setWorkListError(null);
    setCollectionPage(null);
    setCollectionListError(null);

    try {
      const next = await invoke<ProfileReviewSummary>("get_profile_review");
      if (requestId !== profileRequestRef.current) return null;
      setProfile(next);
      setCurrentProfileVersionId(next.profile_version_id);
      currentProfileVersionIdRef.current = next.profile_version_id;
      if (clearInspection) updateInspectedProfileVersion(null);
      setActiveSection((current) => (
        current === OVERVIEW_KEY || next.sections.some((section) => section.key === current)
          ? current
          : OVERVIEW_KEY
      ));
      setAnnouncement(`Opened saved profile version ${next.version_number}.`);
      return next;
    } catch (caught) {
      if (requestId !== profileRequestRef.current) return null;
      setProfile(null);
      setProfileError(safeProfileReviewError(caught, "summary"));
      setAnnouncement("The saved profile review could not be opened.");
      return null;
    } finally {
      if (requestId === profileRequestRef.current) setLoadingProfile(false);
    }
  }, [updateInspectedProfileVersion]);

  const loadHistory = useCallback(async (offset: number, append: boolean) => {
    const requestId = ++historyRequestRef.current;
    append ? setLoadingMoreHistory(true) : setLoadingHistory(true);
    if (!append) setHistoryError(null);
    try {
      let next = await invoke<ProfileVersionPage>("list_profile_versions", { offset });
      if (requestId !== historyRequestRef.current) return null;
      let shouldAppend = append;
      const previous = historyPageRef.current;
      if (
        append
        && previous
        && next.current_profile_version_id !== previous.current_profile_version_id
      ) {
        setAnnouncement("The current profile changed while older history was loading. Restarting from the newest version.");
        next = await invoke<ProfileVersionPage>("list_profile_versions", { offset: 0 });
        if (requestId !== historyRequestRef.current) return null;
        shouldAppend = false;
      }
      setCurrentProfileVersionId(next.current_profile_version_id);
      currentProfileVersionIdRef.current = next.current_profile_version_id;
      const resolved = shouldAppend && previous
        ? {
          ...next,
          items: mergeVersionItems(previous.items, next.items),
        }
        : next;
      historyPageRef.current = resolved;
      setHistoryPage(resolved);
      return resolved;
    } catch (caught) {
      if (requestId !== historyRequestRef.current) return null;
      setHistoryError(safeProfileWorkspaceError(caught, "history"));
      return null;
    } finally {
      if (requestId === historyRequestRef.current) {
        setLoadingHistory(false);
        setLoadingMoreHistory(false);
      }
    }
  }, []);

  const refreshCurrentProfileWorkspace = useCallback(async (clearInspection = true) => {
    for (let attempt = 0; attempt < 3; attempt += 1) {
      const nextHistory = await loadHistory(0, false);
      if (!nextHistory?.current_profile_version_id) return null;
      const nextProfile = await loadProfile(clearInspection);
      if (!nextProfile) return null;
      if (nextProfile.profile_version_id === nextHistory.current_profile_version_id) {
        return nextProfile;
      }
      setAnnouncement("The current profile advanced while its history was refreshing. Reading both again.");
    }
    setHistoryError("The current profile kept changing while CVGnome refreshed local history. Try refreshing again.");
    return null;
  }, [loadHistory, loadProfile]);

  const loadExactVersion = useCallback(async (profileVersionId: string) => {
    const requestId = ++profileRequestRef.current;
    sectionRequestRef.current += 1;
    workRequestRef.current += 1;
    collectionRequestRef.current += 1;
    toolRequestRef.current += 1;
    setLoadingSection(false);
    setLoadingMore(false);
    setLoadingWork(false);
    setLoadingMoreWork(false);
    setLoadingCollection(false);
    setLoadingMoreCollection(false);
    setSwitchingVersion(true);
    setAnnouncement("Opening the selected saved profile version.");
    setProfileError(null);
    setSectionError(null);
    setSectionPage(null);
    setWorkPage(null);
    setWorkListError(null);
    setCollectionPage(null);
    setCollectionListError(null);
    setWorkMutationError(null);
    setWorkRequiresProfileName(false);
    setWorkForm(null);
    setWorkOriginal(null);
    setWorkSaveConflict(false);
    setConfirmingWorkRemoval(null);
    setCommittedWorkReceipt(null);
    setCollectionForm(null);
    setCollectionOriginal(null);
    setCollectionMutationError(null);
    setCollectionSaveConflict(false);
    setCollectionRequiresProfileName(false);
    setCommittedCollectionReceipt(null);
    setConfirmingCollectionRemoval(null);
    setRestoreTarget(null);
    setCommittedRestoreReceipt(null);
    setRestoreError(null);
    setRestoreConflict(false);
    setReviewMode("profile");
    setVersionDiff(null);
    setDiffError(null);
    setBasics(null);
    setBasicsForm(null);
    setBasicsError(null);
    setSaveConflict(false);
    setCommittedBasicsReceipt(null);
    basicsSaveRequestRef.current = null;
    workSaveRequestRef.current = null;
    collectionSaveRequestRef.current = null;
    try {
      const next = await invoke<ProfileReviewSummary>("get_profile_review_version", {
        profileVersionId,
      });
      if (requestId !== profileRequestRef.current) return;
      if (next.profile_version_id !== profileVersionId) {
        setProfileError("The local vault returned a different profile version. Reload the current profile and try again.");
        setAnnouncement("The requested profile version could not be verified.");
        return;
      }
      setProfile(next);
      setActiveSection((current) => (
        current === OVERVIEW_KEY || next.sections.some((section) => section.key === current)
          ? current
          : OVERVIEW_KEY
      ));
      updateInspectedProfileVersion(
        next.profile_version_id === currentProfileVersionIdRef.current ? null : next.profile_version_id,
      );
      setAnnouncement(
        next.profile_version_id === currentProfileVersionIdRef.current
          ? `Returned to current profile version ${next.version_number}.`
          : `Opened historical profile version ${next.version_number}.`,
      );
    } catch (caught) {
      if (requestId !== profileRequestRef.current) return;
      setProfileError(safeProfileWorkspaceError(caught, "version"));
      setAnnouncement("The requested profile version could not be opened.");
    } finally {
      if (requestId === profileRequestRef.current) setSwitchingVersion(false);
    }
  }, [updateInspectedProfileVersion]);

  const loadSection = useCallback(async (
    profileVersionId: string,
    section: string,
    offset: number,
    append: boolean,
  ) => {
    const requestId = ++sectionRequestRef.current;
    append ? setLoadingMore(true) : setLoadingSection(true);
    if (!append) setSectionPage(null);
    setSectionError(null);

    try {
      const next = await invoke<ProfileReviewSectionPage>("get_profile_review_section", {
        profileVersionId,
        section,
        offset,
      });
      if (requestId !== sectionRequestRef.current) return;
      if (next.profile_version_id !== profileVersionId || next.key !== section) {
        setSectionPage(null);
        setSectionError("The local vault returned a different saved profile section. Refresh and try again.");
        setAnnouncement("The requested profile section could not be verified.");
        return;
      }
      setSectionPage((current) => {
        if (!append || !current || current.key !== next.key) return next;
        return {
          ...next,
          items: mergeSectionItems(current.items, next.items),
        };
      });
      const visibleItems = append
        ? Math.min(next.total_items, offset + next.items.length)
        : next.items.length;
      setAnnouncement(
        append
          ? `Loaded more ${next.label.toLowerCase()} items. ${visibleItems} of ${next.total_items} are available.`
          : `Opened ${next.label}. ${next.total_items} ${next.total_items === 1 ? "item" : "items"}.`,
      );
    } catch (caught) {
      if (requestId !== sectionRequestRef.current) return;
      setSectionError(safeProfileReviewError(caught, "section"));
      setAnnouncement("The requested profile section could not be opened.");
    } finally {
      if (requestId === sectionRequestRef.current) {
        setLoadingSection(false);
        setLoadingMore(false);
      }
    }
  }, []);

  const loadWorkHistory = useCallback(async (
    profileVersionId: string,
    offset: number,
    append: boolean,
  ) => {
    const requestId = ++workRequestRef.current;
    append ? setLoadingMoreWork(true) : setLoadingWork(true);
    setWorkListError(null);
    if (!append) {
      setWorkPage(null);
    }
    try {
      const next = await invoke<ProfileWorkPage>("list_profile_work_history", {
        profileVersionId,
        offset,
      });
      if (requestId !== workRequestRef.current) return;
      if (next.profile_version_id !== profileVersionId || next.offset !== offset) {
        setWorkListError("The local vault returned Work History for a different profile version. Reload and try again.");
        setAnnouncement("The requested Work History page could not be verified.");
        return;
      }
      setWorkListError(null);
      setWorkPage((current) => (
        append && current && current.profile_version_id === next.profile_version_id
          ? { ...next, items: mergeWorkItems(current.items, next.items) }
          : next
      ));
      const visibleItems = append
        ? Math.min(next.total_items, offset + next.items.length)
        : next.items.length;
      setAnnouncement(
        append
          ? `Loaded more Work History entries. ${visibleItems} of ${next.total_items} are available.`
          : `Opened Work History. ${next.total_items} ${next.total_items === 1 ? "entry" : "entries"}.`,
      );
    } catch (caught) {
      if (requestId !== workRequestRef.current) return;
      setWorkListError(safeProfileWorkspaceError(caught, "work"));
      setAnnouncement("Work History could not be opened.");
    } finally {
      if (requestId === workRequestRef.current) {
        setLoadingWork(false);
        setLoadingMoreWork(false);
      }
    }
  }, []);

  const loadCollection = useCallback(async (
    section: EditableProfileSection,
    profileVersionId: string,
    offset: number,
    append: boolean,
  ) => {
    const requestId = ++collectionRequestRef.current;
    append ? setLoadingMoreCollection(true) : setLoadingCollection(true);
    setCollectionListError(null);
    if (!append) setCollectionPage(null);
    try {
      const next = await invoke<ProfileCollectionPage>(COLLECTION_LIST_COMMANDS[section], {
        profileVersionId,
        offset,
      });
      if (requestId !== collectionRequestRef.current) return;
      if (
        next.profile_version_id !== profileVersionId
        || next.section !== section
        || next.offset !== offset
      ) {
        setCollectionListError(`The local vault returned ${collectionLabel(section)} for a different saved profile. Reload and try again.`);
        setAnnouncement(`The requested ${collectionLabel(section)} page could not be verified.`);
        return;
      }
      setCollectionPage((current) => (
        append
        && current
        && current.profile_version_id === next.profile_version_id
        && current.section === next.section
          ? { ...next, items: mergeCollectionItems(current.items, next.items) }
          : next
      ));
      const visibleItems = append
        ? Math.min(next.total_items, offset + next.items.length)
        : next.items.length;
      setAnnouncement(append
        ? `Loaded more ${collectionLabel(section)}. ${visibleItems} of ${next.total_items} are available.`
        : `Opened ${collectionLabel(section)}. ${next.total_items} ${next.total_items === 1 ? collectionSingular(section) : "entries"}.`);
    } catch (caught) {
      if (requestId !== collectionRequestRef.current) return;
      setCollectionListError(safeProfileWorkspaceError(caught, "collection"));
      setAnnouncement(`${collectionLabel(section)} could not be opened.`);
    } finally {
      if (requestId === collectionRequestRef.current) {
        setLoadingCollection(false);
        setLoadingMoreCollection(false);
      }
    }
  }, []);

  const loadChangePreview = useCallback(async (
    profileVersionId: string,
    versionNumber: number,
  ) => {
    const requestId = ++changePreviewRequestRef.current;
    setLoadingChangePreview(true);
    setChangePreviewError(null);
    try {
      const next = await invoke<ProfileChangePreview>("preview_profile_changes", {
        profileVersionId,
      });
      if (requestId !== changePreviewRequestRef.current) return;
      const suggestionIds = new Set<string>();
      const suggestionTargets = new Set<string>();
      const validSuggestions = Array.isArray(next.suggestions)
        && next.suggestions.length <= 64
        && next.suggestions.every((suggestion) => {
        if (
          typeof suggestion.suggestion_id !== "string"
          || !suggestion.suggestion_id
          || suggestionIds.has(suggestion.suggestion_id)
          || !isProfileChangeSection(suggestion.section)
          || !Number.isSafeInteger(suggestion.keep_entry_index)
          || suggestion.keep_entry_index < 0
          || !Number.isSafeInteger(suggestion.remove_entry_index)
          || suggestion.remove_entry_index < 0
          || suggestion.keep_entry_index >= suggestion.remove_entry_index
          || !Array.isArray(suggestion.reason_codes)
          || suggestion.reason_codes.some((reason) => typeof reason !== "string")
          || !Array.isArray(suggestion.changed_fields)
          || suggestion.changed_fields.some((field) => typeof field !== "string")
          || !suggestion.merged_entry
          || typeof suggestion.merged_entry !== "object"
          || Array.isArray(suggestion.merged_entry)
        ) return false;
        const targets = [
          `${suggestion.section}:${suggestion.keep_entry_index}`,
          `${suggestion.section}:${suggestion.remove_entry_index}`,
        ];
        if (targets.some((target) => suggestionTargets.has(target))) return false;
        suggestionIds.add(suggestion.suggestion_id);
        targets.forEach((target) => suggestionTargets.add(target));
        return true;
        });
      if (
        next.profile_version_id !== profileVersionId
        || next.version_number !== versionNumber
        || next.algorithm_version !== 1
        || typeof next.truncated !== "boolean"
        || !validSuggestions
      ) {
        setChangePreview(null);
        setChangePreviewError("CVGnome could not verify the local merge suggestions for this exact profile version. Manual review still works.");
        return;
      }
      setChangePreview(next);
      setDismissedSuggestionIds(new Set());
    } catch (caught) {
      if (requestId !== changePreviewRequestRef.current) return;
      setChangePreview(null);
      setChangePreviewError(safeProfileChangesError(caught, "preview"));
    } finally {
      if (requestId === changePreviewRequestRef.current) setLoadingChangePreview(false);
    }
  }, []);

  const runAfterDirtyCheck = useCallback((
    action: () => void,
    preserveQueuedChanges = false,
  ) => {
    if (
      basicsMutationInFlightRef.current
      || workMutationInFlightRef.current
      || collectionMutationInFlightRef.current
      || restoreMutationInFlightRef.current
      || profileChangesMutationInFlightRef.current
    ) {
      setAnnouncement("A profile change is still being saved. Wait for it to finish before leaving this view.");
      return;
    }
    if (editorDirty || (!preserveQueuedChanges && queuedChangesRef.current.length > 0)) {
      pendingNavigationRef.current = action;
      pendingNavigationPreservesQueueRef.current = preserveQueuedChanges;
      setConfirmingDiscard(true);
      return;
    }
    action();
  }, [editorDirty]);

  const discardEditsAndContinue = useCallback(() => {
    if (
      basicsMutationInFlightRef.current
      || workMutationInFlightRef.current
      || collectionMutationInFlightRef.current
      || restoreMutationInFlightRef.current
      || profileChangesMutationInFlightRef.current
    ) {
      pendingNavigationRef.current = null;
      setConfirmingDiscard(false);
      setAnnouncement("A profile change is still being saved. The saved result will remain in local history.");
      return;
    }
    const action = pendingNavigationRef.current;
    const preserveQueuedChanges = pendingNavigationPreservesQueueRef.current;
    pendingNavigationRef.current = null;
    pendingNavigationPreservesQueueRef.current = false;
    setConfirmingDiscard(false);
    setReviewMode("profile");
    setBasics(null);
    setBasicsForm(null);
    setBasicsError(null);
    setSaveConflict(false);
    setCommittedBasicsReceipt(null);
    basicsSaveRequestRef.current = null;
    setWorkForm(null);
    setWorkOriginal(null);
    setWorkMutationError(null);
    setWorkSaveConflict(false);
    setWorkRequiresProfileName(false);
    setCommittedWorkReceipt(null);
    setConfirmingWorkRemoval(null);
    workRemovalTriggerRef.current = null;
    workSaveRequestRef.current = null;
    setCollectionForm(null);
    setCollectionOriginal(null);
    setCollectionMutationError(null);
    setCollectionSaveConflict(false);
    setCollectionRequiresProfileName(false);
    setCommittedCollectionReceipt(null);
    setConfirmingCollectionRemoval(null);
    collectionRemovalTriggerRef.current = null;
    collectionSaveRequestRef.current = null;
    setRestoreTarget(null);
    setCommittedRestoreReceipt(null);
    setRestoreError(null);
    setRestoreConflict(false);
    if (!preserveQueuedChanges) {
      setQueuedProfileChanges([]);
      setQueuedParentProfileVersionId(null);
      setProfileChangesError(null);
      setProfileChangesConflict(false);
      setCommittedProfileChangesReceipt(null);
      profileChangesSaveRequestRef.current = null;
    }
    action?.();
  }, []);

  const keepEditing = useCallback(() => {
    pendingNavigationRef.current = null;
    pendingNavigationPreservesQueueRef.current = false;
    setConfirmingDiscard(false);
    window.requestAnimationFrame(() => {
      if (reviewMode === "work-edit") workPositionRef.current?.focus();
      else if (reviewMode === "collection-edit") collectionFirstInputRef.current?.focus();
      else if (reviewMode === "edit") editNameRef.current?.focus();
      else changeApplyTriggerRef.current?.focus();
    });
  }, [reviewMode]);

  const returnToCurrentProfile = useCallback(() => {
    toolRequestRef.current += 1;
    setReviewMode("profile");
    setVersionDiff(null);
    setDiffError(null);
    setBasics(null);
    setBasicsForm(null);
    setBasicsError(null);
    setSaveConflict(false);
    setCommittedBasicsReceipt(null);
    basicsSaveRequestRef.current = null;
    setWorkForm(null);
    setWorkOriginal(null);
    setWorkListError(null);
    setWorkMutationError(null);
    setWorkSaveConflict(false);
    setWorkRequiresProfileName(false);
    setCommittedWorkReceipt(null);
    setConfirmingWorkRemoval(null);
    workRemovalTriggerRef.current = null;
    workSaveRequestRef.current = null;
    setCollectionForm(null);
    setCollectionOriginal(null);
    setCollectionListError(null);
    setCollectionMutationError(null);
    setCollectionSaveConflict(false);
    setCollectionRequiresProfileName(false);
    setCommittedCollectionReceipt(null);
    setConfirmingCollectionRemoval(null);
    collectionRemovalTriggerRef.current = null;
    collectionSaveRequestRef.current = null;
    setRestoreTarget(null);
    setCommittedRestoreReceipt(null);
    setRestoreError(null);
    setRestoreConflict(false);
    restoreTriggerRef.current = null;
    void loadProfile();
  }, [loadProfile]);

  const requestProfileRestore = useCallback((trigger: HTMLButtonElement) => {
    if (!profile || isCurrentVersion || !currentProfileVersionId || workspaceBusy) return;
    restoreTriggerRef.current = trigger;
    setCommittedRestoreReceipt(null);
    setRestoreError(null);
    setRestoreConflict(false);
    setRestoreTarget({
      sourceProfileVersionId: profile.profile_version_id,
      sourceVersionNumber: profile.version_number,
      sourceRenderable: profile.renderable,
      expectedParentProfileVersionId: currentProfileVersionId,
      expectedParentVersionNumber: historyPageRef.current?.items.find(
        (item) => item.profile_version_id === currentProfileVersionId,
      )?.version_number ?? null,
    });
    setAnnouncement(`Confirm restoring profile version ${profile.version_number} as a new current version.`);
  }, [currentProfileVersionId, isCurrentVersion, profile, workspaceBusy]);

  const dismissProfileRestore = useCallback(() => {
    if (restoreMutationInFlightRef.current) return;
    const trigger = restoreTriggerRef.current;
    const targetKey = restoreTarget
      ? JSON.stringify({
        sourceProfileVersionId: restoreTarget.sourceProfileVersionId,
        expectedParentProfileVersionId: restoreTarget.expectedParentProfileVersionId,
      })
      : null;
    const requestMayHaveRun = Boolean(
      targetKey && restoreRequestRef.current?.key === targetKey,
    );
    setRestoreTarget(null);
    setCommittedRestoreReceipt(null);
    setRestoreError(null);
    setRestoreConflict(false);
    setAnnouncement(
      requestMayHaveRun
        ? "Restore dialog closed. Reload the current profile if you need to confirm its latest state."
        : "Restore cancelled. The current profile was not changed.",
    );
    window.requestAnimationFrame(() => {
      if (trigger?.isConnected) trigger.focus();
      restoreTriggerRef.current = null;
    });
  }, [restoreTarget]);

  const recoverRestoreConflict = useCallback(async () => {
    if (restoreMutationInFlightRef.current) return;
    const requestId = ++toolRequestRef.current;
    restoreMutationInFlightRef.current = true;
    onMutationBusyChange(true);
    setRestoringProfile(true);
    setRestoreError(null);
    setAnnouncement("Reloading the current profile and its local history.");
    try {
      const latest = await refreshCurrentProfileWorkspace();
      await onProfileRefreshRequested();
      if (requestId !== toolRequestRef.current) return;
      if (!latest) {
        setRestoreError("CVGnome could not refresh a coherent current profile and history. Try reloading again.");
        setRestoreConflict(true);
        setAnnouncement("The latest current profile could not be refreshed yet.");
        return;
      }
      setRestoreTarget(null);
      setCommittedRestoreReceipt(null);
      setRestoreError(null);
      setRestoreConflict(false);
      restoreRequestRef.current = null;
      restoreTriggerRef.current = null;
      setReviewMode("profile");
      setAnnouncement(`Opened current profile version ${latest.version_number} with refreshed local history.`);
      window.requestAnimationFrame(() => contentHeadingRef.current?.focus());
    } catch {
      if (requestId !== toolRequestRef.current) return;
      setRestoreError("CVGnome could not refresh the latest profile status. Try reloading again.");
      setRestoreConflict(true);
      setAnnouncement("The latest current profile could not be refreshed yet.");
    } finally {
      restoreMutationInFlightRef.current = false;
      syncParentMutationBusy();
      if (requestId === toolRequestRef.current) setRestoringProfile(false);
    }
  }, [onMutationBusyChange, onProfileRefreshRequested, refreshCurrentProfileWorkspace, syncParentMutationBusy]);

  const confirmProfileRestore = useCallback(async () => {
    if (
      !restoreTarget
      || restoreMutationInFlightRef.current
      || restoreConflict
    ) return;

    const {
      sourceProfileVersionId,
      sourceVersionNumber,
      sourceRenderable,
      expectedParentProfileVersionId,
      expectedParentVersionNumber,
    } = restoreTarget;
    const requestId = ++toolRequestRef.current;
    restoreMutationInFlightRef.current = true;
    onMutationBusyChange(true);
    setRestoringProfile(true);
    setRestoreError(null);
    setRestoreConflict(false);
    setAnnouncement(committedRestoreReceipt
      ? `Refreshing the workspace after restored profile version ${committedRestoreReceipt.version_number}.`
      : `Restoring profile version ${sourceVersionNumber} locally.`);

    let receipt = committedRestoreReceipt;

    try {
      if (!receipt) {
        const semanticKey = JSON.stringify({
          sourceProfileVersionId,
          expectedParentProfileVersionId,
        });
        if (restoreRequestRef.current?.key !== semanticKey) {
          restoreRequestRef.current = { key: semanticKey, requestId: crypto.randomUUID() };
        }
        const stableRequestId = restoreRequestRef.current.requestId;
        const nextReceipt = await invoke<ProfileRestoreReceipt>("restore_profile_version", {
          sourceProfileVersionId,
          expectedParentProfileVersionId,
          requestId: stableRequestId,
        });
        if (requestId !== toolRequestRef.current) return;
        if (
          nextReceipt.request_id !== stableRequestId
          || nextReceipt.parent_profile_version_id !== expectedParentProfileVersionId
          || nextReceipt.restored_from_profile_version_id !== sourceProfileVersionId
          || nextReceipt.restored_from_version_number !== sourceVersionNumber
          || nextReceipt.profile_version_id === sourceProfileVersionId
          || nextReceipt.profile_version_id === expectedParentProfileVersionId
          || nextReceipt.version_number <= sourceVersionNumber
          || (
            expectedParentVersionNumber !== null
            && nextReceipt.version_number !== expectedParentVersionNumber + 1
          )
          || nextReceipt.renderable !== sourceRenderable
        ) {
          setRestoreError("The local vault returned a restore receipt that does not match this saved version. Reload the current profile before trying again.");
          setRestoreConflict(true);
          setAnnouncement("The restored profile version could not be verified.");
          return;
        }
        receipt = nextReceipt;
        setCommittedRestoreReceipt(nextReceipt);
      }

      updateInspectedProfileVersion(null);
      const latest = await refreshCurrentProfileWorkspace();
      if (!latest) {
        await onProfileRestored(receipt, null);
        if (requestId !== toolRequestRef.current) return;
        setRestoreError(
          `Profile version ${receipt.restored_from_version_number} was restored successfully as version ${receipt.version_number}, but CVGnome could not refresh a coherent current profile and history. Retrying refresh will not create another version.`,
        );
        setRestoreConflict(false);
        setAnnouncement(
          `Profile version ${receipt.version_number} is saved. The current profile view still needs to be refreshed.`,
        );
        return;
      }
      await onProfileRestored(receipt, latest);
      if (requestId !== toolRequestRef.current) return;
      setRestoreTarget(null);
      setCommittedRestoreReceipt(null);
      setRestoreError(null);
      setRestoreConflict(false);
      restoreRequestRef.current = null;
      restoreTriggerRef.current = null;
      setReviewMode("profile");
      setAnnouncement(latest.profile_version_id === receipt.profile_version_id
        ? `Restored profile version ${receipt.restored_from_version_number} as new current version ${receipt.version_number}.`
        : `Profile version ${receipt.restored_from_version_number} was restored as version ${receipt.version_number}. Newer current version ${latest.version_number} has been opened.`);
      window.requestAnimationFrame(() => contentHeadingRef.current?.focus());
    } catch (caught) {
      if (requestId !== toolRequestRef.current) return;
      if (receipt) {
        setCommittedRestoreReceipt(receipt);
        void onProfileRestored(receipt, null).catch(() => undefined);
        setRestoreError(
          `Profile version ${receipt.restored_from_version_number} was restored successfully as version ${receipt.version_number}, but CVGnome could not finish refreshing the workspace. Retrying refresh will not create another version.`,
        );
        setRestoreConflict(false);
        setAnnouncement(
          `Profile version ${receipt.version_number} is saved. The workspace still needs to be refreshed.`,
        );
      } else {
        const normalizedError = (caught instanceof Error ? caught.message : String(caught)).toLowerCase();
        setRestoreError(safeProfileWorkspaceError(caught, "restore"));
        setRestoreConflict(
          normalizedError.includes("profile_restore_conflict")
          || normalizedError.includes("profile_parent_conflict")
          || normalizedError.includes("profile_version_conflict")
          || normalizedError.includes("profile_update_conflict")
          || normalizedError.includes("profile_update_request_conflict")
          || normalizedError.includes("already matches the current profile")
          || normalizedError.includes("must identify a historical version"),
        );
        setAnnouncement("The historical profile restore could not be confirmed.");
      }
    } finally {
      restoreMutationInFlightRef.current = false;
      syncParentMutationBusy();
      if (requestId === toolRequestRef.current) setRestoringProfile(false);
    }
  }, [committedRestoreReceipt, onMutationBusyChange, onProfileRestored, refreshCurrentProfileWorkspace, restoreConflict, restoreTarget, syncParentMutationBusy, updateInspectedProfileVersion]);

  const startBasicDetailsEdit = useCallback(async () => {
    if (!profile || !isCurrentVersion || workspaceBusy || queuedChangeCount > 0) return;
    const requestId = ++toolRequestRef.current;
    setLoadingBasics(true);
    setBasicsError(null);
    setSaveConflict(false);
    setCommittedBasicsReceipt(null);
    basicsSaveRequestRef.current = null;
    try {
      const next = await invoke<ProfileBasics>("get_profile_basics", {
        profileVersionId: profile.profile_version_id,
      });
      if (requestId !== toolRequestRef.current) return;
      if (next.profile_version_id !== profile.profile_version_id) {
        setBasicsError("The local vault returned Basic Details for a different version. Reload the current profile and try again.");
        return;
      }
      setBasics(next);
      setBasicsForm(formFromBasics(next));
      setReviewMode("edit");
      setActiveSection(OVERVIEW_KEY);
      setAnnouncement(`Editing Basic Details from profile version ${next.version_number}.`);
      window.requestAnimationFrame(() => editNameRef.current?.focus());
    } catch (caught) {
      if (requestId !== toolRequestRef.current) return;
      setBasicsError(safeProfileWorkspaceError(caught, "basics"));
      setAnnouncement("Basic Details could not be opened.");
    } finally {
      if (requestId === toolRequestRef.current) setLoadingBasics(false);
    }
  }, [isCurrentVersion, profile, queuedChangeCount, workspaceBusy]);

  const queueProfileChange = useCallback((
    operation: ProfileChangeOperation,
    suggestion: ProfileMergeSuggestion | null = null,
  ) => {
    if (!profile || !isCurrentVersion || profileMutationBusy) return false;
    if (
      queuedParentProfileVersionId
      && queuedParentProfileVersionId !== profile.profile_version_id
    ) {
      setProfileChangesConflict(true);
      setProfileChangesError("These queued choices belong to an older profile version. Reload or discard them; CVGnome will not silently move them onto the current profile.");
      return false;
    }
    const nextTargets = new Set(profileChangeTargets(operation));
    const conflicts = queuedProfileChanges.filter((change) => (
      profileChangeTargets(change.operation).some((target) => nextTargets.has(target))
    ));
    if (operation.kind === "merge" && conflicts.length > 0) {
      setProfileChangesError("That suggested merge overlaps another queued change. Undo the overlapping choice before adding this merge.");
      return false;
    }
    const retainedChanges = queuedProfileChanges.filter((change) => (
      !profileChangeTargets(change.operation).some((target) => nextTargets.has(target))
    ));
    if (retainedChanges.length >= 64) {
      setProfileChangesError("A review queue can contain at most 64 changes. Apply or discard this queue before adding another choice.");
      return false;
    }
    setQueuedParentProfileVersionId(profile.profile_version_id);
    setQueuedProfileChanges((current) => [
      ...current.filter((change) => (
        !profileChangeTargets(change.operation).some((target) => nextTargets.has(target))
      )),
      { operation, suggestion },
    ]);
    setProfileChangesError(null);
    setProfileChangesConflict(false);
    setCommittedProfileChangesReceipt(null);
    profileChangesSaveRequestRef.current = null;
    return true;
  }, [isCurrentVersion, profile, profileMutationBusy, queuedParentProfileVersionId, queuedProfileChanges]);

  const undoQueuedChange = useCallback((change: QueuedProfileChange) => {
    if (profileChangesMutationInFlightRef.current || committedProfileChangesReceipt) return;
    const remaining = queuedProfileChanges.filter((candidate) => candidate !== change);
    setQueuedProfileChanges(remaining);
    if (remaining.length === 0) {
      setQueuedParentProfileVersionId(null);
      setProfileChangesError(null);
      setProfileChangesConflict(false);
    } else if (
      profile
      && queuedParentProfileVersionId === profile.profile_version_id
    ) {
      setProfileChangesError(null);
      setProfileChangesConflict(false);
    }
    profileChangesSaveRequestRef.current = null;
    window.requestAnimationFrame(() => contentHeadingRef.current?.focus());
  }, [committedProfileChangesReceipt, profile, queuedParentProfileVersionId, queuedProfileChanges]);

  const discardQueuedChanges = useCallback(() => {
    if (profileChangesMutationInFlightRef.current) return;
    setQueuedProfileChanges([]);
    setQueuedParentProfileVersionId(null);
    setProfileChangesError(null);
    setProfileChangesConflict(false);
    setCommittedProfileChangesReceipt(null);
    setConfirmingChangeApply(false);
    profileChangesSaveRequestRef.current = null;
    setAnnouncement("Discarded the queued review choices. The saved profile was unchanged.");
    window.requestAnimationFrame(() => contentHeadingRef.current?.focus());
  }, []);

  const queueMergeSuggestion = useCallback((
    suggestion: ProfileMergeSuggestion,
    _trigger: HTMLButtonElement,
  ) => {
    if (!changePreview || suggestion.section !== activeSection) return;
    const queued = queueProfileChange({
      kind: "merge",
      section: suggestion.section,
      keep_entry_index: suggestion.keep_entry_index,
      remove_entry_index: suggestion.remove_entry_index,
      suggestion_id: suggestion.suggestion_id,
    }, suggestion);
    if (queued) {
      setAnnouncement(`Queued one conservative merge in ${suggestion.section === "work" ? "Work History" : collectionLabel(suggestion.section)}. Nothing is saved until you apply the queue.`);
      window.requestAnimationFrame(() => {
        document.querySelector<HTMLButtonElement>(
          `[data-profile-change-target="${suggestion.section}:${suggestion.keep_entry_index}"]`,
        )?.focus();
      });
    }
  }, [activeSection, changePreview, queueProfileChange]);

  const dismissMergeSuggestion = useCallback((
    suggestionId: string,
    _trigger: HTMLButtonElement,
  ) => {
    setDismissedSuggestionIds((current) => new Set(current).add(suggestionId));
    setAnnouncement("Kept those profile entries separate for this review session.");
    window.requestAnimationFrame(() => contentHeadingRef.current?.focus());
  }, []);

  const openChangeApply = useCallback((trigger: HTMLButtonElement) => {
    if (queuedChangeCount === 0 || applyingProfileChanges) return;
    if (reviewMode !== "profile" || editorDirty) {
      setProfileChangesError("Queue or cancel the open editor before applying the review queue.");
      setAnnouncement("Finish or cancel the open profile editor before applying queued changes.");
      return;
    }
    changeApplyTriggerRef.current = trigger;
    setConfirmingChangeApply(true);
    setProfileChangesError((current) => profileChangesConflict ? current : null);
    setAnnouncement(`Reviewing ${queuedChangeCount} queued profile ${queuedChangeCount === 1 ? "change" : "changes"} before applying.`);
  }, [applyingProfileChanges, editorDirty, profileChangesConflict, queuedChangeCount, reviewMode]);

  const closeChangeApply = useCallback(() => {
    if (profileChangesMutationInFlightRef.current) return;
    setConfirmingChangeApply(false);
    const trigger = changeApplyTriggerRef.current;
    window.requestAnimationFrame(() => {
      if (trigger?.isConnected) trigger.focus();
    });
  }, []);

  const reloadCurrentWithQueue = useCallback(async () => {
    if (profileChangesMutationInFlightRef.current) return;
    setApplyingProfileChanges(true);
    setProfileChangesError(null);
    const latest = await refreshCurrentProfileWorkspace();
    setApplyingProfileChanges(false);
    if (latest) {
      setProfileChangesConflict(
        queuedChangesRef.current.length > 0
        && queuedParentProfileVersionId !== latest.profile_version_id,
      );
      setProfileChangesError(
        queuedChangesRef.current.length > 0
        && queuedParentProfileVersionId !== latest.profile_version_id
          ? `Reloaded current profile version ${latest.version_number}. The existing queue remains pinned to its older parent and cannot be applied; discard it, then make fresh choices.`
          : null,
      );
      setAnnouncement(`Reloaded current profile version ${latest.version_number}.`);
    }
  }, [queuedParentProfileVersionId, refreshCurrentProfileWorkspace]);

  const applyQueuedProfileChanges = useCallback(async () => {
    if (profileChangesMutationInFlightRef.current || queuedChangesRef.current.length === 0) return;
    if (reviewMode !== "profile" || editDirtyRef.current) {
      setProfileChangesError("Queue or cancel the open editor before applying the review queue.");
      setAnnouncement("The queued profile changes were not applied because an editor is still open.");
      return;
    }
    if (
      !committedProfileChangesReceipt
      && (
        !profile
        || !isCurrentVersion
        || !queuedParentProfileVersionId
        || queuedParentProfileVersionId !== profile.profile_version_id
      )
    ) {
      setProfileChangesConflict(true);
      setProfileChangesError("These queued choices no longer target the current profile. Nothing was applied. Reload or discard the queue.");
      window.requestAnimationFrame(() => changeApplyCancelRef.current?.focus());
      return;
    }
    profileChangesMutationInFlightRef.current = true;
    onMutationBusyChange(true);
    setApplyingProfileChanges(true);
    setProfileChangesError(null);
    setProfileChangesConflict(false);
    const requestSequence = ++toolRequestRef.current;
    let receipt = committedProfileChangesReceipt;
    try {
      if (!receipt) {
        const operations = queuedChangesRef.current.map((change) => change.operation);
        const saveKey = JSON.stringify({
          expectedParentProfileVersionId: queuedParentProfileVersionId,
          operations,
        });
        if (profileChangesSaveRequestRef.current?.key !== saveKey) {
          profileChangesSaveRequestRef.current = {
            key: saveKey,
            requestId: crypto.randomUUID(),
          };
        }
        const stableRequestId = profileChangesSaveRequestRef.current.requestId;
        const nextReceipt = await invoke<ProfileChangesReceipt>("apply_profile_changes", {
          expectedParentProfileVersionId: queuedParentProfileVersionId,
          requestId: stableRequestId,
          operations,
        });
        if (requestSequence !== toolRequestRef.current) return;
        const expectedSections = PROFILE_CHANGE_SECTIONS.filter((section) => (
          operations.some((operation) => operation.section === section)
        ));
        const validSectionCounts = PROFILE_CHANGE_SECTIONS.every((section) => (
          Number.isSafeInteger(nextReceipt.section_counts?.[section])
          && nextReceipt.section_counts[section] >= 0
        ));
        if (
          nextReceipt.request_id !== stableRequestId
          || nextReceipt.parent_profile_version_id !== queuedParentProfileVersionId
          || typeof nextReceipt.profile_version_id !== "string"
          || !nextReceipt.profile_version_id
          || nextReceipt.profile_version_id === queuedParentProfileVersionId
          || !Number.isSafeInteger(nextReceipt.version_number)
          || nextReceipt.version_number !== (profile?.version_number ?? 0) + 1
          || typeof nextReceipt.created !== "boolean"
          || !Number.isSafeInteger(nextReceipt.operation_count)
          || nextReceipt.operation_count !== operations.length
          || !Array.isArray(nextReceipt.changed_sections)
          || JSON.stringify(nextReceipt.changed_sections) !== JSON.stringify(expectedSections)
          || !validSectionCounts
          || !Number.isSafeInteger(nextReceipt.created_at_ms)
          || nextReceipt.created_at_ms < 0
          || typeof nextReceipt.profile_name !== "string"
          || typeof nextReceipt.renderable !== "boolean"
        ) {
          setProfileChangesError("The local vault returned a receipt for a different profile change set. Reload the current profile before continuing.");
          setProfileChangesConflict(true);
          window.requestAnimationFrame(() => changeApplyCancelRef.current?.focus());
          return;
        }
        receipt = nextReceipt;
        setCommittedProfileChangesReceipt(nextReceipt);
      }

      updateInspectedProfileVersion(null);
      const latest = await refreshCurrentProfileWorkspace();
      if (requestSequence !== toolRequestRef.current) return;
      if (
        !latest
        || latest.profile_version_id !== receipt.profile_version_id
        || latest.version_number !== receipt.version_number
      ) {
        setProfileChangesError(`Profile version ${receipt.version_number} is saved, but the review could not refresh coherently. Retry refresh; the queued mutation will not run again.`);
        setAnnouncement(`Profile version ${receipt.version_number} is saved. The workspace still needs to refresh.`);
        return;
      }
      suppressedReloadKeyRef.current = `${receipt.version_number}:${receipt.profile_name}`;
      await onProfileChangesApplied(receipt);
      setReviewMode("profile");
      setBasics(null);
      setBasicsForm(null);
      setBasicsError(null);
      setSaveConflict(false);
      setCommittedBasicsReceipt(null);
      basicsSaveRequestRef.current = null;
      setWorkForm(null);
      setWorkOriginal(null);
      setWorkMutationError(null);
      setWorkSaveConflict(false);
      setWorkRequiresProfileName(false);
      setCommittedWorkReceipt(null);
      setConfirmingWorkRemoval(null);
      workRemovalTriggerRef.current = null;
      workSaveRequestRef.current = null;
      setCollectionForm(null);
      setCollectionOriginal(null);
      setCollectionMutationError(null);
      setCollectionSaveConflict(false);
      setCollectionRequiresProfileName(false);
      setCommittedCollectionReceipt(null);
      setConfirmingCollectionRemoval(null);
      collectionRemovalTriggerRef.current = null;
      collectionSaveRequestRef.current = null;
      setConfirmingDiscard(false);
      pendingNavigationRef.current = null;
      pendingNavigationPreservesQueueRef.current = false;
      setQueuedProfileChanges([]);
      setQueuedParentProfileVersionId(null);
      setCommittedProfileChangesReceipt(null);
      setProfileChangesError(null);
      setProfileChangesConflict(false);
      setConfirmingChangeApply(false);
      profileChangesSaveRequestRef.current = null;
      editDirtyRef.current = false;
      setAnnouncement(`Applied ${receipt.operation_count} reviewed ${receipt.operation_count === 1 ? "change" : "changes"} together as profile version ${receipt.version_number}.`);
      window.requestAnimationFrame(() => contentHeadingRef.current?.focus());
    } catch (caught) {
      if (requestSequence !== toolRequestRef.current) return;
      if (receipt) {
        setCommittedProfileChangesReceipt(receipt);
        setProfileChangesError(`Profile version ${receipt.version_number} is saved, but CVGnome could not finish refreshing the workspace. Retry refresh; the queued mutation will not run again.`);
        setAnnouncement(`Profile version ${receipt.version_number} is saved. The workspace still needs to refresh.`);
      } else {
        const normalizedError = (caught instanceof Error ? caught.message : String(caught)).toLowerCase();
        setProfileChangesError(safeProfileChangesError(caught, "apply"));
        setProfileChangesConflict(
          normalizedError.includes("profile_changes_conflict")
          || normalizedError.includes("profile_changes_request_conflict"),
        );
        if (
          normalizedError.includes("profile_changes_conflict")
          || normalizedError.includes("profile_changes_request_conflict")
        ) {
          window.requestAnimationFrame(() => changeApplyCancelRef.current?.focus());
        }
        setAnnouncement("The queued profile changes were not applied.");
      }
    } finally {
      profileChangesMutationInFlightRef.current = false;
      syncParentMutationBusy();
      if (requestSequence === toolRequestRef.current) setApplyingProfileChanges(false);
    }
  }, [committedProfileChangesReceipt, isCurrentVersion, onMutationBusyChange, onProfileChangesApplied, profile, queuedParentProfileVersionId, refreshCurrentProfileWorkspace, reviewMode, syncParentMutationBusy, updateInspectedProfileVersion]);

  const dismissWorkRemoval = useCallback(() => {
    if (workMutationInFlightRef.current) return;
    if (committedWorkReceipt) {
      returnToCurrentProfile();
      return;
    }
    const trigger = workRemovalTriggerRef.current;
    setConfirmingWorkRemoval(null);
    setWorkMutationError(null);
    setWorkSaveConflict(false);
    setWorkRequiresProfileName(false);
    window.requestAnimationFrame(() => {
      if (trigger?.isConnected) trigger.focus();
      workRemovalTriggerRef.current = null;
    });
  }, [committedWorkReceipt, returnToCurrentProfile]);

  const recoverWorkByEditingBasicDetails = useCallback(() => {
    if (workMutationInFlightRef.current) return;
    workRemovalTriggerRef.current = null;
    setConfirmingWorkRemoval(null);
    setWorkMutationError(null);
    setWorkSaveConflict(false);
    setWorkRequiresProfileName(false);
    void startBasicDetailsEdit();
  }, [startBasicDetailsEdit]);

  const startWorkAdd = useCallback(() => {
    if (!profile || !isCurrentVersion || workspaceBusy || queuedChangeCount > 0) return;
    toolRequestRef.current += 1;
    setWorkOriginal(null);
    setWorkForm(emptyWorkForm());
    setWorkMutationError(null);
    setWorkSaveConflict(false);
    setWorkRequiresProfileName(false);
    setCommittedWorkReceipt(null);
    setConfirmingWorkRemoval(null);
    workRemovalTriggerRef.current = null;
    workSaveRequestRef.current = null;
    setReviewMode("work-edit");
    setActiveSection("work");
    setAnnouncement(`Adding a Work History entry to profile version ${profile.version_number}.`);
    window.requestAnimationFrame(() => workPositionRef.current?.focus());
  }, [isCurrentVersion, profile, queuedChangeCount, workspaceBusy]);

  const startWorkUpdate = useCallback((entry: ProfileWorkEntry) => {
    if (!profile || !isCurrentVersion || workspaceBusy) return;
    const queued = queuedChangeForEntry(queuedProfileChanges, "work", entry.entry_index);
    if (queued && queued.operation.kind !== "update") return;
    toolRequestRef.current += 1;
    setWorkOriginal(entry);
    setWorkForm(workFormFromEntry(
      queued?.operation.kind === "update"
        ? workEntryWithPatch(entry, queued.operation.patch as WorkEntryPatch)
        : entry,
    ));
    setWorkMutationError(null);
    setWorkSaveConflict(false);
    setWorkRequiresProfileName(false);
    setCommittedWorkReceipt(null);
    setConfirmingWorkRemoval(null);
    workRemovalTriggerRef.current = null;
    workSaveRequestRef.current = null;
    setReviewMode("work-edit");
    setActiveSection("work");
    setAnnouncement(`Editing ${entry.position || "a role"} at ${entry.name || "an organization"}.`);
    window.requestAnimationFrame(() => workPositionRef.current?.focus());
  }, [isCurrentVersion, profile, queuedProfileChanges, workspaceBusy]);

  const commitWorkOperation = useCallback(async (operation: ProfileWorkOperation) => {
    if (workMutationInFlightRef.current) return;
    if (!committedWorkReceipt && (!profile || !isCurrentVersion)) {
      setWorkMutationError("The current profile changed before this Work History edit could be saved. Reload the latest version, review it, and apply your changes again.");
      setWorkSaveConflict(true);
      setWorkRequiresProfileName(false);
      setAnnouncement("Work History was not changed because this editor no longer targets the current profile.");
      return;
    }
    workMutationInFlightRef.current = true;
    onMutationBusyChange(true);
    const requestId = ++toolRequestRef.current;
    setSavingWork(true);
    setWorkMutationError(null);
    setWorkSaveConflict(false);
    setWorkRequiresProfileName(false);
    let receipt = committedWorkReceipt;
    try {
      if (!receipt) {
        if (!profile) {
          throw new Error("profile_update_conflict: the current profile is unavailable");
        }
        const saveKey = JSON.stringify({
          expectedParentProfileVersionId: profile.profile_version_id,
          operation,
        });
        if (workSaveRequestRef.current?.key !== saveKey) {
          workSaveRequestRef.current = { key: saveKey, requestId: crypto.randomUUID() };
        }
        const stableRequestId = workSaveRequestRef.current.requestId;
        const nextReceipt = await invoke<ProfileWorkUpdateReceipt>("update_profile_work_history", {
          expectedParentProfileVersionId: profile.profile_version_id,
          requestId: stableRequestId,
          operation,
        });
        if (requestId !== toolRequestRef.current) return;
        const expectedEntryIndex = operation.kind === "add" ? null : operation.entry_index;
        if (
          nextReceipt.parent_profile_version_id !== profile.profile_version_id
          || nextReceipt.request_id !== stableRequestId
          || nextReceipt.operation !== operation.kind
          || nextReceipt.profile_version_id === profile.profile_version_id
          || nextReceipt.version_number !== profile.version_number + 1
          || (expectedEntryIndex !== null && nextReceipt.entry_index !== expectedEntryIndex)
          || nextReceipt.changed_fields.length === 0
        ) {
          setWorkMutationError("The local vault returned a Work History receipt for a different save. Reload the current profile and try again.");
          setWorkSaveConflict(true);
          return;
        }
        receipt = nextReceipt;
        setCommittedWorkReceipt(nextReceipt);
      }

      updateInspectedProfileVersion(null);
      await onWorkUpdated(receipt);
      const latest = await refreshCurrentProfileWorkspace();
      if (requestId !== toolRequestRef.current) return;
      if (!latest) {
        setWorkMutationError(
          `Work History was saved as profile version ${receipt.version_number}, but the current profile and history could not be refreshed. Retrying refresh will not create another version.`,
        );
        setAnnouncement(`Profile version ${receipt.version_number} is saved. The workspace still needs to refresh.`);
        return;
      }
      const action = receipt.operation === "add"
        ? "Added"
        : receipt.operation === "remove"
          ? "Removed"
          : "Updated";
      setReviewMode("profile");
      setWorkForm(null);
      setWorkOriginal(null);
      setConfirmingWorkRemoval(null);
      workRemovalTriggerRef.current = null;
      setWorkSaveConflict(false);
      setWorkRequiresProfileName(false);
      setCommittedWorkReceipt(null);
      workSaveRequestRef.current = null;
      editDirtyRef.current = false;
      setAnnouncement(`${action} a Work History entry in profile version ${receipt.version_number}.`);
    } catch (caught) {
      if (requestId !== toolRequestRef.current) return;
      if (receipt) {
        setCommittedWorkReceipt(receipt);
        setWorkMutationError(
          `Work History was saved as profile version ${receipt.version_number}, but CVGnome could not finish refreshing the workspace. Retrying refresh will not create another version.`,
        );
        setWorkSaveConflict(false);
        setAnnouncement(`Profile version ${receipt.version_number} is saved. The workspace still needs to refresh.`);
      } else {
        const normalizedError = (caught instanceof Error ? caught.message : String(caught)).toLowerCase();
        setWorkMutationError(safeProfileWorkspaceError(caught, "work-save"));
        setWorkRequiresProfileName(
          normalizedError.includes("must retain a non-empty name")
          || normalizedError.includes("edited profile must retain"),
        );
        setWorkSaveConflict(
          normalizedError.includes("profile_update_conflict")
          || normalizedError.includes("profile_work_update_conflict")
          || normalizedError.includes("profile_update_request_conflict")
          || normalizedError.includes("profile_parent_conflict")
          || normalizedError.includes("profile_version_conflict"),
        );
        setAnnouncement("Work History was not changed.");
      }
    } finally {
      workMutationInFlightRef.current = false;
      syncParentMutationBusy();
      if (requestId === toolRequestRef.current) setSavingWork(false);
    }
  }, [committedWorkReceipt, isCurrentVersion, onMutationBusyChange, onWorkUpdated, profile, refreshCurrentProfileWorkspace, syncParentMutationBusy, updateInspectedProfileVersion]);

  const submitWorkEntry = useCallback(async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!workForm || savingWork) return;
    const input = workEntryInputFromForm(workForm);
    if (!input.position) {
      setWorkMutationError("Role or position is required.");
      setWorkRequiresProfileName(false);
      workPositionRef.current?.focus();
      return;
    }
    if (!input.name) {
      setWorkMutationError("Organization is required.");
      setWorkRequiresProfileName(false);
      return;
    }
    if (input.highlights.length > 8) {
      setWorkMutationError("Use at most eight highlights for one Work History entry.");
      setWorkRequiresProfileName(false);
      return;
    }
    if (input.highlights.some((highlight) => highlight.length > 360)) {
      setWorkMutationError("Each Work History highlight must be 360 characters or fewer.");
      setWorkRequiresProfileName(false);
      return;
    }
    if (input.highlights.reduce((total, highlight) => total + highlight.length, 0) > 1_800) {
      setWorkMutationError("Work History highlights must use 1,800 characters or fewer in total.");
      setWorkRequiresProfileName(false);
      return;
    }
    const operation: ProfileWorkOperation = workOriginal
      ? { kind: "update", entry_index: workOriginal.entry_index, patch: workPatch }
      : { kind: "add", entry: input };
    if (!committedWorkReceipt && operation.kind === "update" && Object.keys(operation.patch).length === 0) {
      setWorkMutationError("Make at least one Work History change before saving.");
      setWorkRequiresProfileName(false);
      return;
    }
    if (operation.kind === "update") {
      if (!queueProfileChange({ ...operation, section: "work" })) return;
      setReviewMode("profile");
      setWorkForm(null);
      setWorkOriginal(null);
      setWorkMutationError(null);
      setWorkSaveConflict(false);
      workSaveRequestRef.current = null;
      editDirtyRef.current = false;
      setAnnouncement("Queued this Work History edit. Nothing is saved until you apply the review queue.");
      window.requestAnimationFrame(() => contentHeadingRef.current?.focus());
      return;
    }
    await commitWorkOperation(operation);
  }, [commitWorkOperation, committedWorkReceipt, queueProfileChange, savingWork, workForm, workOriginal, workPatch]);

  const confirmWorkRemoval = useCallback(async () => {
    if (!confirmingWorkRemoval || savingWork) return;
    const queued = queueProfileChange({
      kind: "remove",
      section: "work",
      entry_index: confirmingWorkRemoval.entry_index,
    });
    if (!queued) return;
    const entryIndex = confirmingWorkRemoval.entry_index;
    setConfirmingWorkRemoval(null);
    setWorkMutationError(null);
    workRemovalTriggerRef.current = null;
    setAnnouncement("Queued this role for removal. Nothing is saved until you apply the review queue.");
    window.requestAnimationFrame(() => {
      document.querySelector<HTMLButtonElement>(
        `[data-profile-change-target="work:${entryIndex}"]`,
      )?.focus();
    });
  }, [confirmingWorkRemoval, queueProfileChange, savingWork]);

  const startCollectionAdd = useCallback((section: EditableProfileSection) => {
    if (!profile || !isCurrentVersion || workspaceBusy || queuedChangeCount > 0) return;
    toolRequestRef.current += 1;
    setCollectionOriginal(null);
    setCollectionForm(emptyCollectionForm(section));
    setCollectionMutationError(null);
    setCollectionSaveConflict(false);
    setCollectionRequiresProfileName(false);
    setCommittedCollectionReceipt(null);
    setConfirmingCollectionRemoval(null);
    collectionRemovalTriggerRef.current = null;
    collectionSaveRequestRef.current = null;
    setReviewMode("collection-edit");
    setActiveSection(section);
    setAnnouncement(`Adding a ${collectionSingular(section)} to profile version ${profile.version_number}.`);
    window.requestAnimationFrame(() => collectionFirstInputRef.current?.focus());
  }, [isCurrentVersion, profile, queuedChangeCount, workspaceBusy]);

  const startCollectionUpdate = useCallback((
    section: EditableProfileSection,
    entry: ProfileCollectionEntry,
  ) => {
    if (!profile || !isCurrentVersion || workspaceBusy) return;
    const queued = queuedChangeForEntry(queuedProfileChanges, section, entry.entry_index);
    if (queued && queued.operation.kind !== "update") return;
    toolRequestRef.current += 1;
    setCollectionOriginal(entry);
    setCollectionForm(collectionFormFromEntry(
      section,
      queued?.operation.kind === "update"
        ? collectionEntryWithPatch(
          section,
          entry,
          queued.operation.patch as Record<string, string | string[] | null>,
        )
        : entry,
    ));
    setCollectionMutationError(null);
    setCollectionSaveConflict(false);
    setCollectionRequiresProfileName(false);
    setCommittedCollectionReceipt(null);
    setConfirmingCollectionRemoval(null);
    collectionRemovalTriggerRef.current = null;
    collectionSaveRequestRef.current = null;
    setReviewMode("collection-edit");
    setActiveSection(section);
    setAnnouncement(`Editing a ${collectionSingular(section)} in ${collectionLabel(section)}.`);
    window.requestAnimationFrame(() => collectionFirstInputRef.current?.focus());
  }, [isCurrentVersion, profile, queuedProfileChanges, workspaceBusy]);

  const dismissCollectionRemoval = useCallback(() => {
    if (collectionMutationInFlightRef.current) return;
    if (committedCollectionReceipt) {
      returnToCurrentProfile();
      return;
    }
    const trigger = collectionRemovalTriggerRef.current;
    setConfirmingCollectionRemoval(null);
    setCollectionMutationError(null);
    setCollectionSaveConflict(false);
    setCollectionRequiresProfileName(false);
    setCommittedCollectionReceipt(null);
    window.requestAnimationFrame(() => {
      if (trigger?.isConnected) trigger.focus();
      collectionRemovalTriggerRef.current = null;
    });
  }, [committedCollectionReceipt, returnToCurrentProfile]);

  const recoverCollectionByEditingBasicDetails = useCallback(() => {
    if (collectionMutationInFlightRef.current) return;
    collectionRemovalTriggerRef.current = null;
    setConfirmingCollectionRemoval(null);
    setCollectionMutationError(null);
    setCollectionSaveConflict(false);
    setCollectionRequiresProfileName(false);
    setCommittedCollectionReceipt(null);
    void startBasicDetailsEdit();
  }, [startBasicDetailsEdit]);

  const commitCollectionOperation = useCallback(async (
    section: EditableProfileSection,
    operation: ProfileCollectionOperation,
  ) => {
    if (collectionMutationInFlightRef.current) return;
    if (!committedCollectionReceipt && (!profile || !isCurrentVersion)) {
      setCollectionMutationError(`The current profile changed before this ${collectionLabel(section)} edit could be saved. Reload the latest version, review it, and apply your changes again.`);
      setCollectionSaveConflict(true);
      setCollectionRequiresProfileName(false);
      setAnnouncement(`${collectionLabel(section)} was not changed because this editor no longer targets the current profile.`);
      return;
    }
    collectionMutationInFlightRef.current = true;
    onMutationBusyChange(true);
    const requestId = ++toolRequestRef.current;
    setSavingCollection(true);
    setCollectionMutationError(null);
    setCollectionSaveConflict(false);
    setCollectionRequiresProfileName(false);
    let receipt = committedCollectionReceipt;
    try {
      if (!receipt) {
        if (!profile) {
          throw new Error("profile_update_conflict: the current profile is unavailable");
        }
        const saveKey = JSON.stringify({
          section,
          expectedParentProfileVersionId: profile.profile_version_id,
          operation,
        });
        if (collectionSaveRequestRef.current?.key !== saveKey) {
          collectionSaveRequestRef.current = { key: saveKey, requestId: crypto.randomUUID() };
        }
        const stableRequestId = collectionSaveRequestRef.current.requestId;
        const nextReceipt = await invoke<ProfileCollectionUpdateReceipt>(
          COLLECTION_UPDATE_COMMANDS[section],
          {
            expectedParentProfileVersionId: profile.profile_version_id,
            requestId: stableRequestId,
            operation,
          },
        );
        if (requestId !== toolRequestRef.current) return;
        const expectedEntryIndex = operation.kind === "add" ? null : operation.entry_index;
        if (
          nextReceipt.request_id !== stableRequestId
          || nextReceipt.parent_profile_version_id !== profile.profile_version_id
          || nextReceipt.profile_version_id === profile.profile_version_id
          || nextReceipt.version_number !== profile.version_number + 1
          || nextReceipt.section !== section
          || nextReceipt.operation !== operation.kind
          || (expectedEntryIndex !== null && nextReceipt.entry_index !== expectedEntryIndex)
          || !Number.isSafeInteger(nextReceipt.entry_index)
          || nextReceipt.entry_index < 0
          || !Number.isSafeInteger(nextReceipt.section_entries)
          || nextReceipt.section_entries < 0
          || nextReceipt.changed_fields.length === 0
        ) {
          setCollectionMutationError(`The local vault returned a ${collectionLabel(section)} receipt for a different save. Reload the current profile and try again.`);
          setCollectionSaveConflict(true);
          setAnnouncement(`${collectionLabel(section)} could not be verified after saving.`);
          return;
        }
        receipt = nextReceipt;
        setCommittedCollectionReceipt(nextReceipt);
      }

      updateInspectedProfileVersion(null);
      await onCollectionUpdated(receipt);
      const latest = await refreshCurrentProfileWorkspace();
      if (requestId !== toolRequestRef.current) return;
      if (!latest) {
        setCollectionMutationError(
          `${collectionLabel(section)} was saved as profile version ${receipt.version_number}, but the current profile and history could not be refreshed. Retrying refresh will not create another version.`,
        );
        setAnnouncement(`Profile version ${receipt.version_number} is saved. The workspace still needs to refresh.`);
        return;
      }
      const action = receipt.operation === "add" ? "Added" : receipt.operation === "remove" ? "Removed" : "Updated";
      setReviewMode("profile");
      setCollectionForm(null);
      setCollectionOriginal(null);
      setConfirmingCollectionRemoval(null);
      collectionRemovalTriggerRef.current = null;
      setCollectionMutationError(null);
      setCollectionSaveConflict(false);
      setCollectionRequiresProfileName(false);
      setCommittedCollectionReceipt(null);
      collectionSaveRequestRef.current = null;
      editDirtyRef.current = false;
      setAnnouncement(`${action} a ${collectionSingular(section)} in profile version ${receipt.version_number}.`);
      window.requestAnimationFrame(() => contentHeadingRef.current?.focus());
    } catch (caught) {
      if (requestId !== toolRequestRef.current) return;
      if (receipt) {
        setCommittedCollectionReceipt(receipt);
        setCollectionMutationError(
          `${collectionLabel(section)} was saved as profile version ${receipt.version_number}, but CVGnome could not finish refreshing the workspace. Retrying refresh will not create another version.`,
        );
        setCollectionSaveConflict(false);
        setAnnouncement(`Profile version ${receipt.version_number} is saved. The workspace still needs to refresh.`);
      } else {
        const normalizedError = (caught instanceof Error ? caught.message : String(caught)).toLowerCase();
        setCollectionMutationError(safeProfileWorkspaceError(caught, "collection-save"));
        setCollectionRequiresProfileName(
          normalizedError.includes("must retain a non-empty name")
          || normalizedError.includes("edited profile must retain"),
        );
        setCollectionSaveConflict(
          normalizedError.includes("profile_update_conflict")
          || normalizedError.includes("profile_collection_update_conflict")
          || normalizedError.includes("profile_update_request_conflict")
          || normalizedError.includes("profile_parent_conflict")
          || normalizedError.includes("profile_version_conflict"),
        );
        setAnnouncement(`${collectionLabel(section)} was not changed.`);
      }
    } finally {
      collectionMutationInFlightRef.current = false;
      syncParentMutationBusy();
      if (requestId === toolRequestRef.current) setSavingCollection(false);
    }
  }, [committedCollectionReceipt, isCurrentVersion, onCollectionUpdated, onMutationBusyChange, profile, refreshCurrentProfileWorkspace, syncParentMutationBusy, updateInspectedProfileVersion]);

  const submitCollectionEntry = useCallback(async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!collectionForm || savingCollection) return;
    const validation = validateCollectionForm(collectionForm);
    if (validation) {
      setCollectionMutationError(validation.message);
      window.requestAnimationFrame(() => {
        document.querySelector<HTMLElement>(
          `[data-profile-collection-field="${validation.field}"]`,
        )?.focus();
      });
      return;
    }
    const operation: ProfileCollectionOperation = collectionOriginal
      ? { kind: "update", entry_index: collectionOriginal.entry_index, patch: collectionPatch }
      : { kind: "add", entry: collectionInputFromForm(collectionForm) };
    if (!committedCollectionReceipt && operation.kind === "update" && Object.keys(operation.patch).length === 0) {
      setCollectionMutationError(`Make at least one ${collectionLabel(collectionForm.section)} change before saving.`);
      return;
    }
    if (operation.kind === "update") {
      if (!queueProfileChange({ ...operation, section: collectionForm.section })) return;
      setReviewMode("profile");
      setCollectionForm(null);
      setCollectionOriginal(null);
      setCollectionMutationError(null);
      setCollectionSaveConflict(false);
      collectionSaveRequestRef.current = null;
      editDirtyRef.current = false;
      setAnnouncement(`Queued this ${collectionSingular(collectionForm.section)} edit. Nothing is saved until you apply the review queue.`);
      window.requestAnimationFrame(() => contentHeadingRef.current?.focus());
      return;
    }
    await commitCollectionOperation(collectionForm.section, operation);
  }, [collectionForm, collectionOriginal, collectionPatch, commitCollectionOperation, committedCollectionReceipt, queueProfileChange, savingCollection]);

  const confirmCollectionRemoval = useCallback(async () => {
    if (!confirmingCollectionRemoval || savingCollection || !isEditableProfileSection(activeSection)) return;
    const section = activeSection;
    const queued = queueProfileChange({
      kind: "remove",
      section,
      entry_index: confirmingCollectionRemoval.entry_index,
    });
    if (!queued) return;
    const entryIndex = confirmingCollectionRemoval.entry_index;
    setConfirmingCollectionRemoval(null);
    setCollectionMutationError(null);
    collectionRemovalTriggerRef.current = null;
    setAnnouncement(`Queued this ${collectionSingular(section)} for removal. Nothing is saved until you apply the review queue.`);
    window.requestAnimationFrame(() => {
      document.querySelector<HTMLButtonElement>(
        `[data-profile-change-target="${section}:${entryIndex}"]`,
      )?.focus();
    });
  }, [activeSection, confirmingCollectionRemoval, queueProfileChange, savingCollection]);

  const openVersionDiff = useCallback(async () => {
    if (!profile || !currentProfileVersionId || workspaceBusy) return;
    const olderVersion = historyPage?.items.find(
      (item) => item.version_number < profile.version_number,
    ) ?? null;
    const comparingCurrent = profile.profile_version_id === currentProfileVersionId;
    const fromProfileVersionId = comparingCurrent
      ? olderVersion?.profile_version_id ?? null
      : profile.profile_version_id;
    const toProfileVersionId = currentProfileVersionId;
    if (!fromProfileVersionId || fromProfileVersionId === toProfileVersionId) {
      setDiffError("There is no adjacent saved version available to compare yet.");
      setReviewMode("diff");
      return;
    }

    const requestId = ++toolRequestRef.current;
    setLoadingDiff(true);
    setDiffError(null);
    setVersionDiff(null);
    setReviewMode("diff");
    setActiveSection(OVERVIEW_KEY);
    try {
      const next = await invoke<ProfileVersionDiff>("diff_profile_versions", {
        fromProfileVersionId,
        toProfileVersionId,
      });
      if (requestId !== toolRequestRef.current) return;
      if (
        next.from_version.profile_version_id !== fromProfileVersionId
        || next.to_version.profile_version_id !== toProfileVersionId
      ) {
        setDiffError("The local vault returned a comparison for different profile versions. Reload and try again.");
        return;
      }
      setVersionDiff(next);
      setAnnouncement(
        `Compared profile versions ${next.from_version.version_number} and ${next.to_version.version_number}. ${next.total_changes} changes found.`,
      );
    } catch (caught) {
      if (requestId !== toolRequestRef.current) return;
      setDiffError(safeProfileWorkspaceError(caught, "diff"));
      setAnnouncement("The profile versions could not be compared.");
    } finally {
      if (requestId === toolRequestRef.current) setLoadingDiff(false);
    }
  }, [currentProfileVersionId, historyPage?.items, profile, workspaceBusy]);

  const submitBasicDetails = useCallback(async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!profile || !basics || !basicsForm || basicsMutationInFlightRef.current) return;
    const validationError = validateBasicDetailsForm(basicsForm);
    if (validationError) {
      setBasicsError(validationError.message);
      if (validationError.field === "name") editNameRef.current?.focus();
      return;
    }
    if (!committedBasicsReceipt && Object.keys(basicsPatch).length === 0) {
      setBasicsError("Make at least one Basic Details change before saving.");
      return;
    }

    basicsMutationInFlightRef.current = true;
    onMutationBusyChange(true);
    const requestId = ++toolRequestRef.current;
    setSavingBasics(true);
    setBasicsError(null);
    setSaveConflict(false);
    let receipt = committedBasicsReceipt;
    try {
      if (!receipt) {
        const saveKey = JSON.stringify({
          expectedParentProfileVersionId: profile.profile_version_id,
          patch: basicsPatch,
        });
        if (basicsSaveRequestRef.current?.key !== saveKey) {
          basicsSaveRequestRef.current = { key: saveKey, requestId: crypto.randomUUID() };
        }
        const stableRequestId = basicsSaveRequestRef.current.requestId;
        const nextReceipt = await invoke<ProfileBasicsUpdateReceipt>("update_profile_basics", {
          expectedParentProfileVersionId: profile.profile_version_id,
          requestId: stableRequestId,
          patch: basicsPatch,
        });
        if (requestId !== toolRequestRef.current) return;
        if (
          nextReceipt.parent_profile_version_id !== profile.profile_version_id
          || nextReceipt.request_id !== stableRequestId
          || nextReceipt.profile_version_id === profile.profile_version_id
          || nextReceipt.version_number !== profile.version_number + 1
          || nextReceipt.changed_fields.length === 0
        ) {
          setBasicsError("The local vault returned a save receipt for a different Basic Details edit. Reload the latest profile before editing again.");
          setSaveConflict(true);
          return;
        }
        receipt = nextReceipt;
        setCommittedBasicsReceipt(nextReceipt);
      }

      updateInspectedProfileVersion(null);
      await onProfileUpdated(receipt);
      const latest = await refreshCurrentProfileWorkspace();
      if (requestId !== toolRequestRef.current) return;
      if (!latest) {
        setBasicsError(
          `Basic Details were saved as profile version ${receipt.version_number}, but the current profile and history could not be refreshed. Retrying refresh will not create another version.`,
        );
        setAnnouncement(`Profile version ${receipt.version_number} is saved. The workspace still needs to refresh.`);
        return;
      }
      setReviewMode("profile");
      setBasics(null);
      setBasicsForm(null);
      setConfirmingDiscard(false);
      pendingNavigationRef.current = null;
      basicsSaveRequestRef.current = null;
      setCommittedBasicsReceipt(null);
      editDirtyRef.current = false;
      setAnnouncement(
        `Saved ${receipt.changed_fields.length} Basic Details ${receipt.changed_fields.length === 1 ? "change" : "changes"} as profile version ${receipt.version_number}.`,
      );
    } catch (caught) {
      if (requestId !== toolRequestRef.current) return;
      if (receipt) {
        setCommittedBasicsReceipt(receipt);
        setBasicsError(
          `Basic Details were saved as profile version ${receipt.version_number}, but CVGnome could not finish refreshing the workspace. Retrying refresh will not create another version.`,
        );
        setSaveConflict(false);
        setAnnouncement(`Profile version ${receipt.version_number} is saved. The workspace still needs to refresh.`);
      } else {
        setBasicsError(safeProfileWorkspaceError(caught, "save"));
        const normalizedError = (caught instanceof Error ? caught.message : String(caught)).toLowerCase();
        setSaveConflict(
          normalizedError.includes("profile_update_conflict")
          || normalizedError.includes("profile_update_request_conflict")
          || normalizedError.includes("profile_parent_conflict")
          || normalizedError.includes("profile_version_conflict"),
        );
        setAnnouncement("Basic Details were not saved.");
      }
    } finally {
      basicsMutationInFlightRef.current = false;
      syncParentMutationBusy();
      if (requestId === toolRequestRef.current) setSavingBasics(false);
    }
  }, [basics, basicsForm, basicsPatch, committedBasicsReceipt, onMutationBusyChange, onProfileUpdated, profile, refreshCurrentProfileWorkspace, syncParentMutationBusy, updateInspectedProfileVersion]);

  useEffect(() => {
    if (suppressedReloadKeyRef.current) {
      const suppressThisReload = suppressedReloadKeyRef.current === reloadKey;
      suppressedReloadKeyRef.current = null;
      if (suppressThisReload) return;
    }
    const openWorkspace = async () => {
      if (
        basicsMutationInFlightRef.current
        || workMutationInFlightRef.current
        || collectionMutationInFlightRef.current
        || restoreMutationInFlightRef.current
      ) {
        setAnnouncement("A profile change is still being saved. The workspace will refresh after it finishes.");
        return;
      }
      const pendingReceipt = committedBasicsReceipt
        ?? committedWorkReceipt
        ?? committedCollectionReceipt
        ?? committedRestoreReceipt;
      if (pendingReceipt) {
        setAnnouncement(
          `Profile version ${pendingReceipt.version_number} is saved. Use Retry refresh to finish updating this workspace.`,
        );
        return;
      }
      if (editDirtyRef.current) {
        const conflictMessage = "A newer current profile is available. Your unsaved form is still open; reload and review the latest version before applying these changes.";
        setBasicsError(conflictMessage);
        setWorkMutationError(conflictMessage);
        setCollectionMutationError(conflictMessage);
        setSaveConflict(true);
        setWorkSaveConflict(true);
        setCollectionSaveConflict(true);
        setWorkRequiresProfileName(false);
        setCollectionRequiresProfileName(false);
        setAnnouncement("The current profile changed while an editor had unsaved changes.");
        return;
      }
      toolRequestRef.current += 1;
      setSwitchingVersion(false);
      setLoadingBasics(false);
      setLoadingDiff(false);
      setLoadingWork(false);
      setLoadingMoreWork(false);
      setLoadingCollection(false);
      setLoadingMoreCollection(false);
      setSavingBasics(false);
      setSavingWork(false);
      setSavingCollection(false);
      setRestoringProfile(false);
      setReviewMode("profile");
      setVersionDiff(null);
      setDiffError(null);
      setBasics(null);
      setBasicsForm(null);
      setBasicsError(null);
      setSaveConflict(false);
      setCommittedBasicsReceipt(null);
      basicsSaveRequestRef.current = null;
      setWorkPage(null);
      setWorkListError(null);
      setWorkMutationError(null);
      setWorkForm(null);
      setWorkOriginal(null);
      setWorkSaveConflict(false);
      setWorkRequiresProfileName(false);
      setCommittedWorkReceipt(null);
      setConfirmingWorkRemoval(null);
      workRemovalTriggerRef.current = null;
      workSaveRequestRef.current = null;
      setCollectionPage(null);
      setCollectionListError(null);
      setCollectionMutationError(null);
      setCollectionForm(null);
      setCollectionOriginal(null);
      setCollectionSaveConflict(false);
      setCollectionRequiresProfileName(false);
      setCommittedCollectionReceipt(null);
      setConfirmingCollectionRemoval(null);
      collectionRemovalTriggerRef.current = null;
      collectionSaveRequestRef.current = null;
      setRestoreTarget(null);
      setCommittedRestoreReceipt(null);
      setRestoreError(null);
      setRestoreConflict(false);
      restoreTriggerRef.current = null;
      const requestedProfileVersionId = requestedInspectionRef.current;
      const preserveRequestedVersion = Boolean(requestedProfileVersionId);
      const latest = await refreshCurrentProfileWorkspace(!preserveRequestedVersion);
      if (!preserveRequestedVersion && latest) updateInspectedProfileVersion(null);
      if (
        latest
        && requestedProfileVersionId
        && requestedProfileVersionId !== latest.profile_version_id
      ) {
        await loadExactVersion(requestedProfileVersionId);
      } else if (latest && requestedProfileVersionId === latest.profile_version_id) {
        updateInspectedProfileVersion(null);
      }
    };
    void openWorkspace();
  }, [loadExactVersion, refreshCurrentProfileWorkspace, reloadKey, updateInspectedProfileVersion]);

  useEffect(() => {
    if (!profile || !isCurrentVersion) {
      changePreviewRequestRef.current += 1;
      setLoadingChangePreview(false);
      if (!profile || !isCurrentVersion) {
        setChangePreview(null);
        setChangePreviewError(null);
      }
      return;
    }
    void loadChangePreview(profile.profile_version_id, profile.version_number);
    return () => {
      changePreviewRequestRef.current += 1;
    };
  }, [isCurrentVersion, loadChangePreview, profile?.profile_version_id, profile?.version_number]);

  useEffect(() => {
    if (
      queuedChangeCount === 0
      || queuedChangesRef.current.length === 0
      || !profile
      || !isCurrentVersion
      || queuedParentProfileVersionId === profile.profile_version_id
    ) return;
    setProfileChangesConflict(true);
    setProfileChangesError("The current profile advanced after this queue was created. The queue is preserved, but CVGnome will not silently rebase it. Reload or discard it before continuing.");
  }, [isCurrentVersion, profile?.profile_version_id, queuedChangeCount, queuedParentProfileVersionId]);

  useEffect(() => () => {
      profileRequestRef.current += 1;
      sectionRequestRef.current += 1;
      historyRequestRef.current += 1;
      workRequestRef.current += 1;
      collectionRequestRef.current += 1;
      changePreviewRequestRef.current += 1;
      toolRequestRef.current += 1;
  }, []);

  useEffect(() => {
    onActiveSectionChange(activeSection);
  }, [activeSection, onActiveSectionChange]);

  useEffect(() => {
    if (!profile || !currentProfileVersionId) return;
    updateInspectedProfileVersion(
      profile.profile_version_id === currentProfileVersionId ? null : profile.profile_version_id,
    );
  }, [currentProfileVersionId, profile, updateInspectedProfileVersion]);

  useEffect(() => {
    onDirtyChange(
      editorDirty
      || queuedChangeCount > 0
      || savingBasics
      || savingWork
      || savingCollection
      || applyingProfileChanges,
    );
  }, [applyingProfileChanges, editorDirty, onDirtyChange, queuedChangeCount, savingBasics, savingCollection, savingWork]);

  useEffect(() => () => onDirtyChange(false), [onDirtyChange]);

  useEffect(() => {
    onMutationBusyChange(profileMutationBusy);
  }, [onMutationBusyChange, profileMutationBusy]);

  useEffect(() => () => onMutationBusyChange(false), [onMutationBusyChange]);

  useEffect(() => {
    const preventAccidentalClose = (event: BeforeUnloadEvent) => {
      if (
        !editDirtyRef.current
        && queuedChangesRef.current.length === 0
        && !basicsMutationInFlightRef.current
        && !workMutationInFlightRef.current
        && !collectionMutationInFlightRef.current
        && !restoreMutationInFlightRef.current
        && !profileChangesMutationInFlightRef.current
      ) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", preventAccidentalClose);
    return () => window.removeEventListener("beforeunload", preventAccidentalClose);
  }, []);

  useEffect(() => {
    if (!confirmingDiscard) return;
    window.requestAnimationFrame(() => discardCancelRef.current?.focus());
    const containDiscardDialog = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopImmediatePropagation();
        keepEditing();
        return;
      }
      if (event.metaKey || event.ctrlKey) {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
    };
    window.addEventListener("keydown", containDiscardDialog, true);
    return () => window.removeEventListener("keydown", containDiscardDialog, true);
  }, [confirmingDiscard, keepEditing]);

  useEffect(() => {
    if (!confirmingWorkRemoval) return;
    window.requestAnimationFrame(() => workRemovalCancelRef.current?.focus());
    const containRemovalDialog = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopImmediatePropagation();
        if (!workMutationInFlightRef.current) dismissWorkRemoval();
        return;
      }
      if (event.metaKey || event.ctrlKey) {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
    };
    window.addEventListener("keydown", containRemovalDialog, true);
    return () => window.removeEventListener("keydown", containRemovalDialog, true);
  }, [confirmingWorkRemoval, dismissWorkRemoval, savingWork]);

  useEffect(() => {
    if (!confirmingCollectionRemoval) return;
    window.requestAnimationFrame(() => collectionRemovalCancelRef.current?.focus());
    const containRemovalDialog = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopImmediatePropagation();
        if (!collectionMutationInFlightRef.current) dismissCollectionRemoval();
        return;
      }
      if (event.metaKey || event.ctrlKey) {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
    };
    window.addEventListener("keydown", containRemovalDialog, true);
    return () => window.removeEventListener("keydown", containRemovalDialog, true);
  }, [confirmingCollectionRemoval, dismissCollectionRemoval, savingCollection]);

  useEffect(() => {
    if (!confirmingRestore) return;
    window.requestAnimationFrame(() => restoreCancelRef.current?.focus());
    const containRestoreDialog = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopImmediatePropagation();
        if (!restoreMutationInFlightRef.current) dismissProfileRestore();
        return;
      }
      if (event.metaKey || event.ctrlKey) {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
    };
    window.addEventListener("keydown", containRestoreDialog, true);
    return () => window.removeEventListener("keydown", containRestoreDialog, true);
  }, [confirmingRestore, dismissProfileRestore, restoringProfile]);

  useEffect(() => {
    if (!confirmingChangeApply) return;
    window.requestAnimationFrame(() => changeApplyCancelRef.current?.focus());
    const containApplyDialog = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopImmediatePropagation();
        if (!profileChangesMutationInFlightRef.current) closeChangeApply();
        return;
      }
      if (event.metaKey || event.ctrlKey) {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
    };
    window.addEventListener("keydown", containApplyDialog, true);
    return () => window.removeEventListener("keydown", containApplyDialog, true);
  }, [closeChangeApply, confirmingChangeApply]);

  useEffect(() => {
    if (!profile || activeSection === OVERVIEW_KEY) {
      sectionRequestRef.current += 1;
      workRequestRef.current += 1;
      collectionRequestRef.current += 1;
      setLoadingSection(false);
      setLoadingMore(false);
      setLoadingWork(false);
      setLoadingMoreWork(false);
      setSectionError(null);
      setSectionPage(null);
      setWorkListError(null);
      setWorkPage(null);
      setLoadingCollection(false);
      setLoadingMoreCollection(false);
      setCollectionListError(null);
      setCollectionPage(null);
      return;
    }
    if (reviewMode !== "profile") {
      sectionRequestRef.current += 1;
      workRequestRef.current += 1;
      collectionRequestRef.current += 1;
      setLoadingSection(false);
      setLoadingMore(false);
      setLoadingWork(false);
      setLoadingMoreWork(false);
      setLoadingCollection(false);
      setLoadingMoreCollection(false);
      return;
    }
    if (!activeDescriptor) {
      setActiveSection(OVERVIEW_KEY);
      return;
    }
    if (activeSection === "work") {
      sectionRequestRef.current += 1;
      collectionRequestRef.current += 1;
      setLoadingSection(false);
      setLoadingMore(false);
      setSectionError(null);
      setSectionPage(null);
      setLoadingCollection(false);
      setLoadingMoreCollection(false);
      setCollectionListError(null);
      setCollectionPage(null);
      if (workPageRef.current?.profile_version_id === profile.profile_version_id) return;
      void loadWorkHistory(profile.profile_version_id, 0, false);
      return () => {
        workRequestRef.current += 1;
      };
    }
    if (isEditableProfileSection(activeSection)) {
      sectionRequestRef.current += 1;
      workRequestRef.current += 1;
      setLoadingSection(false);
      setLoadingMore(false);
      setSectionError(null);
      setSectionPage(null);
      setLoadingWork(false);
      setLoadingMoreWork(false);
      setWorkListError(null);
      setWorkPage(null);
      if (
        collectionPageRef.current?.profile_version_id === profile.profile_version_id
        && collectionPageRef.current.section === activeSection
      ) return;
      void loadCollection(activeSection, profile.profile_version_id, 0, false);
      return () => {
        collectionRequestRef.current += 1;
      };
    }
    workRequestRef.current += 1;
    collectionRequestRef.current += 1;
    setLoadingWork(false);
    setLoadingMoreWork(false);
    setWorkListError(null);
    setWorkPage(null);
    setLoadingCollection(false);
    setLoadingMoreCollection(false);
    setCollectionListError(null);
    setCollectionPage(null);
    void loadSection(profile.profile_version_id, activeDescriptor.key, 0, false);
    return () => {
      sectionRequestRef.current += 1;
    };
  }, [activeDescriptor, activeSection, loadCollection, loadSection, loadWorkHistory, profile, reviewMode]);

  const openSection = useCallback((key: string) => {
    runAfterDirtyCheck(() => {
      setReviewMode("profile");
      setActiveSection(key);
    }, true);
  }, [runAfterDirtyCheck]);

  useEffect(() => {
    if (
      confirmingDiscard
      || confirmingWorkRemoval
      || confirmingCollectionRemoval
      || confirmingRestore
      || confirmingChangeApply
    ) return;
    window.requestAnimationFrame(() => {
      if (reviewMode === "edit") {
        if (committedBasicsReceipt) contentHeadingRef.current?.focus();
        else editNameRef.current?.focus();
      } else if (reviewMode === "work-edit") {
        if (committedWorkReceipt) contentHeadingRef.current?.focus();
        else workPositionRef.current?.focus();
      } else if (reviewMode === "collection-edit") {
        if (committedCollectionReceipt) contentHeadingRef.current?.focus();
        else collectionFirstInputRef.current?.focus();
      } else contentHeadingRef.current?.focus();
    });
  }, [activeSection, committedBasicsReceipt, committedCollectionReceipt, committedWorkReceipt, confirmingChangeApply, confirmingCollectionRemoval, confirmingDiscard, confirmingRestore, confirmingWorkRemoval, profile?.profile_version_id, reviewMode]);

  const railKeys = useMemo(
    () => [OVERVIEW_KEY, ...(profile?.sections.map((section) => section.key) ?? [])],
    [profile],
  );

  const navigateRail = useCallback((event: KeyboardEvent<HTMLElement>) => {
    if (!["ArrowDown", "ArrowUp", "ArrowRight", "ArrowLeft", "Home", "End"].includes(event.key)) return;
    const focused = document.activeElement;
    const currentIndex = railKeys.findIndex((key) => railButtonRefs.current.get(key) === focused);
    if (currentIndex < 0) return;
    event.preventDefault();
    let nextIndex = currentIndex;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = railKeys.length - 1;
    if (event.key === "ArrowDown" || event.key === "ArrowRight") {
      nextIndex = Math.min(railKeys.length - 1, currentIndex + 1);
    }
    if (event.key === "ArrowUp" || event.key === "ArrowLeft") {
      nextIndex = Math.max(0, currentIndex - 1);
    }
    railButtonRefs.current.get(railKeys[nextIndex])?.focus();
  }, [railKeys]);

  const visibleItems = sectionPage?.items.length ?? 0;
  const nextOffset = sectionPage?.next_offset;
  const canLoadMore = Boolean(
    sectionPage
    && nextOffset !== null
    && nextOffset !== undefined
    && nextOffset > sectionPage.offset
    && visibleItems < sectionPage.total_items,
  );
  const historyItems = historyPage?.items ?? [];
  const historyNextOffset = historyPage?.next_offset;
  const canLoadMoreHistory = Boolean(
    historyPage
    && historyNextOffset !== null
    && historyNextOffset !== undefined
    && historyNextOffset >= HISTORY_PAGE_LIMIT
    && historyItems.length < historyPage.total_items,
  );
  const workItems = workPage?.items ?? [];
  const currentSectionEntryIndexes = new Set(
    activeSection === "work"
      ? workItems.map((entry) => entry.entry_index)
      : activeCollectionSection
        ? (collectionPage?.items ?? []).map((entry) => entry.entry_index)
        : [],
  );
  const allActiveMergeSuggestions = changePreview
    && profile
    && changePreview.profile_version_id === profile.profile_version_id
    && isProfileChangeSection(activeSection)
    ? changePreview.suggestions.filter((suggestion) => (
      suggestion.section === activeSection
      && !dismissedSuggestionIds.has(suggestion.suggestion_id)
      && !queuedProfileChanges.some((change) => (
        change.operation.kind === "merge"
        && change.operation.suggestion_id === suggestion.suggestion_id
      ))
    ))
    : [];
  const activeMergeSuggestions = allActiveMergeSuggestions.filter((suggestion) => (
    currentSectionEntryIndexes.has(suggestion.keep_entry_index)
    && currentSectionEntryIndexes.has(suggestion.remove_entry_index)
  ));
  const hiddenMergeSuggestionCount = allActiveMergeSuggestions.length - activeMergeSuggestions.length;
  const workNextOffset = workPage?.next_offset;
  const canLoadMoreWork = Boolean(
    workPage
    && workNextOffset !== null
    && workNextOffset !== undefined
    && workNextOffset > workPage.offset
    && workItems.length < workPage.total_items,
  );
  const collectionNextOffset = collectionPage?.next_offset;
  const hasComparisonVersion = Boolean(
    profile
    && currentProfileVersionId
    && (
      profile.profile_version_id !== currentProfileVersionId
      || historyItems.some((item) => item.version_number < profile.version_number)
    ),
  );
  const exportDisabled = workspaceBusy
    || queuedChangeCount > 0
    || reviewMode === "edit"
    || reviewMode === "work-edit"
    || reviewMode === "collection-edit"
    || !profile?.renderable
    || !canExport
    || exportBusy !== null;

  return (
    <section className="profile-review-workspace" aria-labelledby="profile-review-title">
      <p className="sr-only" role="status" aria-live="polite">{announcement}</p>

      <header className="profile-review-commandbar">
        <div className="profile-review-commandbar__title">
          <button
            type="button"
            className="profile-back-button"
            onClick={() => runAfterDirtyCheck(onBack)}
            disabled={savingBasics || profileMutationBusy}
          >
            <span aria-hidden="true">←</span>
            Update profile
          </button>
          <div>
            <p className="section-kicker">Saved profile</p>
            <h2 id="profile-review-title">Profile review</h2>
            <span>
              {profile
                ? `${profile.name || "Untitled profile"} · Version ${profile.version_number}${isCurrentVersion ? " · Current" : " · Historical"}`
                : "Inspect the locally saved profile"}
            </span>
          </div>
        </div>
        <div className="profile-review-commandbar__actions">
          <button
            type="button"
            className="quiet-action profile-review-memory-action"
            onClick={() => runAfterDirtyCheck(onOpenMemories)}
            disabled={workspaceBusy || reviewInboxDisabled}
          >
            Memories
          </button>
          {reviewItemCount > 0 && (
            <button
              type="button"
              className="quiet-action profile-review-files-action profile-review-inbox-action"
              onClick={() => runAfterDirtyCheck(onOpenReviewInbox)}
              disabled={workspaceBusy || reviewInboxDisabled}
              aria-label={`Open review inbox, ${reviewItemCount} ${reviewItemCount === 1 ? "item" : "items"}`}
              title="Review imported profile suggestions and source checks"
            >
              Review inbox
              <span>{reviewItemCount}</span>
            </button>
          )}
          <button
            type="button"
            className="quiet-action profile-review-files-action"
            onClick={() => runAfterDirtyCheck(onOpenImportedFiles)}
            disabled={savingBasics || profileMutationBusy || switchingVersion}
          >
            Imported files
            <span>{retainedSourceFiles}</span>
          </button>
          {profile && isCurrentVersion && (
            <button
              type="button"
              className="quiet-action profile-review-edit-action"
              onClick={() => void startBasicDetailsEdit()}
              disabled={workspaceBusy || queuedChangeCount > 0 || reviewMode !== "profile"}
            >
              Edit Basic Details
            </button>
          )}
          <div className="export-buttons" role="group" aria-label="Export saved profile">
            <button
              type="button"
              className="secondary-action"
              onClick={() => profile && onExport("docx", profile.profile_version_id)}
              disabled={exportDisabled}
            >
              {exportBusy === "docx" ? "Exporting DOCX…" : "Export DOCX"}
            </button>
            <button
              type="button"
              className="secondary-action"
              onClick={() => profile && onExport("pdf", profile.profile_version_id)}
              disabled={exportDisabled}
            >
              {exportBusy === "pdf" ? "Exporting PDF…" : "Export PDF"}
            </button>
          </div>
        </div>
      </header>

      {notice && (
        <p
          className={`profile-review-notice action-notice--${notice.kind}`}
          role={notice.kind === "error" ? "alert" : "status"}
          aria-live={notice.kind === "error" ? "assertive" : "polite"}
        >
          {notice.message}
        </p>
      )}

      {queuedChangeCount > 0 && (
        <section className="profile-review-queue" aria-label="Queued profile review changes">
          <div>
            <strong>{queuedChangeCount} {queuedChangeCount === 1 ? "change" : "changes"} queued</strong>
            <span>
              {committedProfileChangesReceipt
                ? `Profile version ${committedProfileChangesReceipt.version_number} is saved; refresh is still needed.`
                : reviewMode !== "profile"
                  ? "Queue or cancel the open editor before applying this review queue."
                : queuePinnedToCurrent
                  ? "Nothing is saved until you apply the queue. One apply creates one immutable version."
                  : "This queue is pinned to an older profile version and cannot be silently rebased."}
            </span>
            {profileChangesError && <p role="alert">{profileChangesError}</p>}
          </div>
          <div className="profile-review-queue__actions">
            {profileChangesConflict && (
              <button
                type="button"
                className="quiet-action"
                onClick={() => void reloadCurrentWithQueue()}
                disabled={applyingProfileChanges}
              >
                Reload latest
              </button>
            )}
            <button
              type="button"
              className="quiet-action"
              onClick={discardQueuedChanges}
              disabled={applyingProfileChanges || committedProfileChangesReceipt !== null}
            >
              Discard queue
            </button>
            <button
              type="button"
              className="primary-action"
              onClick={(event) => openChangeApply(event.currentTarget)}
              disabled={applyingProfileChanges
                || reviewMode !== "profile"
                || editorDirty
                || (
                  committedProfileChangesReceipt === null
                  && (profileChangesConflict || !queuePinnedToCurrent)
                )}
              title={reviewMode !== "profile" ? "Queue or cancel the open editor first" : undefined}
            >
              {committedProfileChangesReceipt
                ? "Retry refresh"
                : `Apply ${queuedChangeCount} ${queuedChangeCount === 1 ? "change" : "changes"}`}
            </button>
          </div>
        </section>
      )}

      {profile && !isCurrentVersion && (
        <div className="profile-review-history-banner" role="region" aria-label="Historical profile version">
          <span>You are inspecting immutable version {profile.version_number}. You can restore this snapshot by copying it into a new current version.</span>
          <div className="profile-review-history-banner__actions">
            <button
              type="button"
              className="secondary-action profile-review-restore-action"
              onClick={(event) => requestProfileRestore(event.currentTarget)}
              disabled={workspaceBusy}
            >
              Restore this version
            </button>
            <button
              type="button"
              className="quiet-action"
              onClick={() => runAfterDirtyCheck(returnToCurrentProfile)}
              disabled={workspaceBusy}
            >
              Return to current
            </button>
          </div>
        </div>
      )}

      {profile && profileError && (
        <p className="profile-review-notice action-notice--error" role="alert">{profileError}</p>
      )}

      <div className="profile-review-layout">
        <aside className="profile-review-rail" aria-label="Profile review sections">
          <div className="profile-review-rail__heading">
            <span>Contents</span>
            {profile && <small>{profile.sections.length + 1}</small>}
          </div>
          <nav aria-label="Saved profile contents" onKeyDown={navigateRail}>
            <button
              ref={(node) => {
                if (node) railButtonRefs.current.set(OVERVIEW_KEY, node);
                else railButtonRefs.current.delete(OVERVIEW_KEY);
              }}
              type="button"
              className={activeSection === OVERVIEW_KEY ? "is-active" : ""}
              aria-current={activeSection === OVERVIEW_KEY ? "page" : undefined}
              aria-controls="profile-review-content"
              onClick={() => openSection(OVERVIEW_KEY)}
              disabled={!profile || profileMutationBusy}
            >
              <span>Overview</span>
              <small>{profile ? totalItems : "—"}</small>
            </button>
            {profile?.sections.map((section) => (
              <button
                key={section.key}
                ref={(node) => {
                  if (node) railButtonRefs.current.set(section.key, node);
                  else railButtonRefs.current.delete(section.key);
                }}
                type="button"
                className={activeSection === section.key ? "is-active" : ""}
                aria-current={activeSection === section.key ? "page" : undefined}
                aria-controls="profile-review-content"
                onClick={() => openSection(section.key)}
                disabled={profileMutationBusy}
              >
                <span>{section.label}</span>
                <small>{section.count}</small>
              </button>
            ))}
          </nav>
          <div className="profile-review-rail__footnote">
            <span className="profile-review-readonly-dot" aria-hidden="true" />
            {reviewMode === "edit" || reviewMode === "work-edit" || reviewMode === "collection-edit"
              ? reviewMode === "work-edit"
                ? "Unsaved Work History"
                : reviewMode === "collection-edit" && collectionForm
                  ? `Unsaved ${collectionLabel(collectionForm.section)}`
                  : "Unsaved Basic Details"
              : queuedChangeCount > 0
                ? `${queuedChangeCount} review ${queuedChangeCount === 1 ? "change" : "changes"} queued`
              : isCurrentVersion
                ? "Current saved version"
                : "Read-only historical snapshot"}
          </div>
        </aside>

        <main
          id="profile-review-content"
          className="profile-review-document-pane"
          aria-busy={workspaceBusy || loadingSection || loadingMore}
        >
          {loadingProfile && (
            <div className="profile-review-state" role="status">
              <span className="working-indicator" aria-hidden="true" />
              <div>
                <h3>Opening saved profile…</h3>
                <p>Reading the current version from the local vault.</p>
              </div>
            </div>
          )}

          {!loadingProfile && profileError && !profile && (
            <div className="profile-review-state profile-review-state--error" role="alert">
              <div className="empty-document__glyph" aria-hidden="true">!</div>
              <h3>Profile review unavailable</h3>
              <p>{profileError}</p>
              <div>
                <button type="button" className="primary-action" onClick={() => void loadProfile()}>
                  Try again
                </button>
                {retainedSourceFiles > 0 && (
                  <button type="button" className="secondary-action" onClick={onOpenImportedFiles}>
                    Open imported files
                  </button>
                )}
              </div>
            </div>
          )}

          {!loadingProfile && profile && reviewMode === "profile" && activeSection === OVERVIEW_KEY && (
            <article className="profile-review-document" aria-labelledby="profile-review-overview-title">
              <header className="profile-review-identity">
                <div className="profile-review-avatar" aria-hidden="true">
                  {(profile.name || "CV").trim().slice(0, 2).toUpperCase()}
                </div>
                <div>
                  <p className="section-kicker">Profile overview</p>
                  <h3 id="profile-review-overview-title" ref={contentHeadingRef} tabIndex={-1}>
                    {profile.name || "Untitled profile"}
                  </h3>
                  {profile.headline && <p className="profile-review-headline">{profile.headline}</p>}
                </div>
                <span className={`profile-review-readiness${profile.renderable ? " is-ready" : ""}`}>
                  {profile.renderable ? "Ready" : "Needs attention"}
                </span>
              </header>

              {profile.summary ? (
                <section className="profile-review-overview-section" aria-labelledby="profile-review-summary-title">
                  <h4 id="profile-review-summary-title">Summary</h4>
                  <p className="profile-review-prose">{profile.summary}</p>
                </section>
              ) : (
                <section className="profile-review-overview-section profile-review-empty-section">
                  <h4>Summary</h4>
                  <p>This saved version does not include a profile summary.</p>
                </section>
              )}

              {profile.contacts.length > 0 && (
                <section className="profile-review-overview-section" aria-labelledby="profile-review-contacts-title">
                  <h4 id="profile-review-contacts-title">Contact &amp; links</h4>
                  <dl className="profile-review-contact-grid">
                    {profile.contacts.map((contact, index) => (
                      <div key={`${contact.kind}:${contact.label}:${index}`}>
                        <dt>{contact.label || humanize(contact.kind)}</dt>
                        <dd>{contact.value}</dd>
                      </div>
                    ))}
                  </dl>
                </section>
              )}

              <section className="profile-review-overview-section" aria-labelledby="profile-review-sections-title">
                <div className="profile-review-section-heading">
                  <div>
                    <h4 id="profile-review-sections-title">Profile sections</h4>
                    <p>Open a section to inspect the material preserved in this saved version.</p>
                  </div>
                  <span>{totalItems} {totalItems === 1 ? "item" : "items"}</span>
                </div>
                {profile.sections.length > 0 ? (
                  <div className="profile-review-section-cards">
                    {profile.sections.map((section) => (
                      <button key={section.key} type="button" onClick={() => openSection(section.key)}>
                        <span>
                          <strong>{section.label}</strong>
                          <small>Open saved section</small>
                        </span>
                        <b>{section.count}</b>
                      </button>
                    ))}
                  </div>
                ) : (
                  <div className="profile-review-empty-section">
                    <p>No structured sections are available in this saved version.</p>
                  </div>
                )}
              </section>

              <aside className="profile-review-next-note" aria-label="Profile tools">
                <span aria-hidden="true">◇</span>
                <div>
                  <strong>{isCurrentVersion ? "Current saved profile" : "Historical snapshot"}</strong>
                  <p>
                    {isCurrentVersion
                      ? "Open a structured section to queue review edits and removals, or edit Basic Details here. Apply a review queue as one new version."
                      : "This version is read-only. Compare it with the current profile or return to current before editing."}
                  </p>
                  <div className="profile-review-next-note__actions">
                    {isCurrentVersion && (
                      <button
                        type="button"
                        className="secondary-action"
                        onClick={() => void startBasicDetailsEdit()}
                        disabled={workspaceBusy || queuedChangeCount > 0}
                      >
                        Edit Basic Details
                      </button>
                    )}
                    <button
                      type="button"
                      className="quiet-action"
                      onClick={() => void openVersionDiff()}
                      disabled={workspaceBusy || !hasComparisonVersion}
                    >
                      {isCurrentVersion ? "Compare with previous" : "Compare with current"}
                    </button>
                  </div>
                </div>
              </aside>
            </article>
          )}

          {!loadingProfile && profile && reviewMode === "profile" && activeSection === "work" && (
            <article className="profile-review-document profile-work-history" aria-labelledby="profile-work-history-title">
              <header className="profile-review-section-header">
                <div>
                  <p className="section-kicker">Structured profile section</p>
                  <h3 id="profile-work-history-title" ref={contentHeadingRef} tabIndex={-1}>Work History</h3>
                  <p>
                    {isCurrentVersion
                      ? "Queue review edits and removals, then apply them together as one immutable profile version."
                      : `Read-only Work History from profile version ${profile.version_number}.`}
                  </p>
                </div>
                <div className="profile-work-history__heading-actions">
                  <span>{workPage?.total_items ?? activeDescriptor?.count ?? 0} roles</span>
                  {isCurrentVersion && (
                    <button
                      type="button"
                      className="primary-action"
                      onClick={startWorkAdd}
                      disabled={workspaceBusy || queuedChangeCount > 0}
                    >
                      Add role
                    </button>
                  )}
                </div>
              </header>

              {loadingWork && !workPage && (
                <div className="profile-review-section-loading" role="status">
                  <span className="working-indicator" aria-hidden="true" />
                  Opening Work History…
                </div>
              )}

              {!loadingWork && workListError && !workPage && (
                <div className="profile-review-inline-error" role="alert">
                  <p>{workListError}</p>
                  <button
                    type="button"
                    className="secondary-action"
                    onClick={() => void loadWorkHistory(profile.profile_version_id, 0, false)}
                  >
                    Try again
                  </button>
                </div>
              )}

              {isCurrentVersion && (
                <ProfileMergeSuggestions
                  suggestions={activeMergeSuggestions}
                  workItems={workItems}
                  collectionItems={[]}
                  queuedChanges={queuedProfileChanges}
                  busy={workspaceBusy}
                  onQueue={queueMergeSuggestion}
                  onDismiss={dismissMergeSuggestion}
                />
              )}

              {isCurrentVersion && loadingChangePreview && (
                <p className="profile-merge-suggestions__status" role="status">Checking for conservative duplicate suggestions…</p>
              )}

              {isCurrentVersion && changePreviewError && (
                <p className="profile-merge-suggestions__status profile-merge-suggestions__status--error">{changePreviewError}</p>
              )}
              {isCurrentVersion && changePreview?.truncated && (
                <p className="profile-merge-suggestions__status">Suggestion checking reached its local safety limit; the entries shown remain available for manual review.</p>
              )}
              {isCurrentVersion && hiddenMergeSuggestionCount > 0 && (
                <p className="profile-merge-suggestions__status">Load more Work History entries to inspect {hiddenMergeSuggestionCount} additional {hiddenMergeSuggestionCount === 1 ? "suggestion" : "suggestions"}; unseen pairs cannot be queued.</p>
              )}

              {!loadingWork && !workListError && workPage && workPage.items.length === 0 && (
                <div className="profile-review-empty-section profile-review-empty-section--large">
                  <div className="empty-document__glyph" aria-hidden="true">⌁</div>
                  <h4>No Work History yet</h4>
                  <p>{isCurrentVersion ? "Add the first role to this profile." : "This saved version has no Work History entries."}</p>
                  {isCurrentVersion && (
                    <button type="button" className="primary-action" onClick={startWorkAdd} disabled={queuedChangeCount > 0}>Add role</button>
                  )}
                </div>
              )}

              {workPage && workItems.length > 0 && (
                <div className="profile-work-history__list">
                  {workItems.map((entry, index) => {
                    const queuedChange = queuedChangeForEntry(
                      queuedProfileChanges,
                      "work",
                      entry.entry_index,
                    );
                    const queuedState = queuedChange
                      ? queuedChangeLabel(queuedChange, entry.entry_index)
                      : null;
                    const visibleEntry = queuedChange?.operation.kind === "update"
                      ? workEntryWithPatch(
                        entry,
                        queuedChange.operation.patch as WorkEntryPatch,
                      )
                      : queuedChange?.operation.kind === "merge"
                        && queuedChange.operation.keep_entry_index === entry.entry_index
                        && queuedChange.suggestion
                        ? workEntryWithMerge(entry, queuedChange.suggestion)
                      : entry;
                    const dateRange = [visibleEntry.start_date, visibleEntry.end_date].filter(Boolean).join(" – ");
                    const metadata = [dateRange, visibleEntry.location].filter(Boolean);
                    const roleName = visibleEntry.position || "untitled role";
                    const organizationName = visibleEntry.name || "organization not set";
                    return (
                      <article
                        key={`${workPage.profile_version_id}:${entry.entry_index}`}
                        className={`profile-work-entry${queuedState ? ` is-queued is-queued--${queuedState}` : ""}`}
                      >
                        <div className="profile-review-item__ordinal" aria-hidden="true">
                          {String(index + 1).padStart(2, "0")}
                        </div>
                        <div className="profile-work-entry__body">
                          <header>
                            <div>
                              <h4>{visibleEntry.position || "Untitled role"}</h4>
                              <p>{visibleEntry.name || "Organization not set"}</p>
                              {metadata.length > 0 && <small>{metadata.join(" · ")}</small>}
                            </div>
                            {isCurrentVersion && (
                              <div className="profile-work-entry__actions">
                                {queuedChange ? (
                                  <>
                                    {queuedChange.operation.kind === "update" && (
                                      <button
                                        type="button"
                                        className="quiet-action"
                                        aria-label={`Revise queued edit to ${roleName} at ${organizationName}`}
                                        onClick={() => startWorkUpdate(entry)}
                                        disabled={workspaceBusy}
                                      >
                                        Edit queued
                                      </button>
                                    )}
                                    <button
                                      type="button"
                                      className="quiet-action"
                                      data-profile-change-target={`work:${entry.entry_index}`}
                                      aria-label={`Undo queued change to ${roleName} at ${organizationName}`}
                                      onClick={() => undoQueuedChange(queuedChange)}
                                      disabled={workspaceBusy}
                                    >
                                      Undo
                                    </button>
                                  </>
                                ) : (
                                  <>
                                    <button
                                      type="button"
                                      className="quiet-action"
                                      aria-label={`Edit ${roleName} at ${organizationName}`}
                                      onClick={() => startWorkUpdate(entry)}
                                      disabled={workspaceBusy}
                                    >
                                      Edit
                                    </button>
                                    <button
                                      type="button"
                                      className="quiet-action profile-work-entry__remove"
                                      aria-label={`Queue removal of ${roleName} at ${organizationName}`}
                                      onClick={(event) => {
                                        workRemovalTriggerRef.current = event.currentTarget;
                                        setWorkMutationError(null);
                                        setWorkSaveConflict(false);
                                        setWorkRequiresProfileName(false);
                                        setConfirmingWorkRemoval(entry);
                                      }}
                                      disabled={workspaceBusy}
                                    >
                                      Remove
                                    </button>
                                  </>
                                )}
                              </div>
                            )}
                          </header>
                          {queuedState && (
                            <span className="profile-review-queued-label">
                              {queuedState === "edit"
                                ? "Edit queued"
                                : queuedState === "merge"
                                  ? "Merge queued"
                                  : queuedState === "merged-away" ? "Will merge into another entry" : "Removal queued"}
                            </span>
                          )}
                          {visibleEntry.summary && <p className="profile-review-prose">{visibleEntry.summary}</p>}
                          {visibleEntry.highlights.length > 0 && (
                            <ul className="profile-work-entry__highlights">
                              {visibleEntry.highlights.map((highlight, highlightIndex) => (
                                <li key={`${entry.entry_index}:highlight:${highlightIndex}`}>{highlight}</li>
                              ))}
                            </ul>
                          )}
                          {visibleEntry.url && <p className="profile-work-entry__url">{visibleEntry.url}</p>}
                        </div>
                      </article>
                    );
                  })}
                </div>
              )}

              {workPage && workListError && (
                <div className="profile-review-inline-error profile-review-inline-error--pagination" role="alert">
                  <p>{workListError}</p>
                  {workNextOffset !== null && workNextOffset !== undefined && (
                    <button
                      type="button"
                      className="secondary-action"
                      onClick={() => void loadWorkHistory(profile.profile_version_id, workNextOffset, true)}
                    >
                      Try loading more again
                    </button>
                  )}
                </div>
              )}

              {workPage && (
                <footer className="profile-review-pagination">
                  <span>Showing {workItems.length} of {workPage.total_items}</span>
                  {canLoadMoreWork && !workListError && (
                    <button
                      type="button"
                      className="secondary-action"
                      onClick={() => void loadWorkHistory(
                        profile.profile_version_id,
                        workNextOffset as number,
                        true,
                      )}
                      disabled={loadingMoreWork}
                    >
                      {loadingMoreWork ? "Loading more…" : `Load more (${workPage.total_items - workItems.length})`}
                    </button>
                  )}
                </footer>
              )}
            </article>
          )}

          {!loadingProfile && profile && reviewMode === "profile" && activeCollectionSection && (
            <>
              {isCurrentVersion && (
                <ProfileMergeSuggestions
                  suggestions={activeMergeSuggestions}
                  workItems={[]}
                  collectionItems={collectionPage?.items ?? []}
                  queuedChanges={queuedProfileChanges}
                  busy={workspaceBusy}
                  onQueue={queueMergeSuggestion}
                  onDismiss={dismissMergeSuggestion}
                />
              )}
              {isCurrentVersion && loadingChangePreview && (
                <p className="profile-merge-suggestions__status" role="status">Checking for conservative duplicate suggestions…</p>
              )}
              {isCurrentVersion && changePreviewError && (
                <p className="profile-merge-suggestions__status profile-merge-suggestions__status--error">{changePreviewError}</p>
              )}
              {isCurrentVersion && changePreview?.truncated && (
                <p className="profile-merge-suggestions__status">Suggestion checking reached its local safety limit; the entries shown remain available for manual review.</p>
              )}
              {isCurrentVersion && hiddenMergeSuggestionCount > 0 && (
                <p className="profile-merge-suggestions__status">Load more {collectionLabel(activeCollectionSection).toLowerCase()} to inspect {hiddenMergeSuggestionCount} additional {hiddenMergeSuggestionCount === 1 ? "suggestion" : "suggestions"}; unseen pairs cannot be queued.</p>
              )}
              <ProfileCollectionSection
                section={activeCollectionSection}
                profileVersionId={profile.profile_version_id}
                versionNumber={profile.version_number}
                current={isCurrentVersion}
                page={collectionPage}
                loading={loadingCollection}
                loadingMore={loadingMoreCollection}
                error={collectionListError}
                busy={workspaceBusy}
                addDisabled={queuedChangeCount > 0}
                headingRef={contentHeadingRef}
                onAdd={() => startCollectionAdd(activeCollectionSection)}
                onEdit={(entry) => startCollectionUpdate(activeCollectionSection, entry)}
                onRemove={(entry, trigger) => {
                  collectionRemovalTriggerRef.current = trigger;
                  setCollectionMutationError(null);
                  setCollectionSaveConflict(false);
                  setCollectionRequiresProfileName(false);
                  setCommittedCollectionReceipt(null);
                  collectionSaveRequestRef.current = null;
                  setConfirmingCollectionRemoval(entry);
                  setAnnouncement(`Confirm queuing removal of this ${collectionSingular(activeCollectionSection)}.`);
                }}
                queuedStateForEntry={(entry) => {
                  const change = queuedChangeForEntry(
                    queuedProfileChanges,
                    activeCollectionSection,
                    entry.entry_index,
                  );
                  return change ? queuedChangeLabel(change, entry.entry_index) : null;
                }}
                presentedEntry={(entry) => {
                  const change = queuedChangeForEntry(
                    queuedProfileChanges,
                    activeCollectionSection,
                    entry.entry_index,
                  );
                  return change?.operation.kind === "update"
                    ? collectionEntryWithPatch(
                      activeCollectionSection,
                      entry,
                      change.operation.patch as Record<string, string | string[] | null>,
                    )
                    : change?.operation.kind === "merge"
                      && change.operation.keep_entry_index === entry.entry_index
                      && change.suggestion
                      ? collectionEntryWithMerge(
                        activeCollectionSection,
                        entry,
                        change.suggestion,
                      )
                    : entry;
                }}
                onUndoQueued={(entry) => {
                  const change = queuedChangeForEntry(
                    queuedProfileChanges,
                    activeCollectionSection,
                    entry.entry_index,
                  );
                  if (change) undoQueuedChange(change);
                }}
                onRetry={() => void loadCollection(
                  activeCollectionSection,
                  profile.profile_version_id,
                  0,
                  false,
                )}
                onLoadMore={() => void loadCollection(
                  activeCollectionSection,
                  profile.profile_version_id,
                  collectionNextOffset ?? 0,
                  true,
                )}
              />
            </>
          )}

          {!loadingProfile && profile && reviewMode === "profile" && activeSection !== OVERVIEW_KEY && activeSection !== "work" && !activeCollectionSection && (
            <article className="profile-review-document" aria-labelledby="profile-review-section-title">
              <header className="profile-review-section-header">
                <div>
                  <p className="section-kicker">Saved profile section</p>
                  <h3 id="profile-review-section-title" ref={contentHeadingRef} tabIndex={-1}>
                    {activeDescriptor?.label ?? sectionPage?.label ?? "Profile section"}
                  </h3>
                  <p>Read-only material from profile version {profile.version_number}.</p>
                </div>
                <span>
                  {sectionPage?.total_items ?? activeDescriptor?.count ?? 0}
                  {" "}
                  {(sectionPage?.total_items ?? activeDescriptor?.count ?? 0) === 1 ? "item" : "items"}
                </span>
              </header>

              {loadingSection && !sectionPage && (
                <div className="profile-review-section-loading" role="status">
                  <span className="working-indicator" aria-hidden="true" />
                  Opening section…
                </div>
              )}

              {!loadingSection && sectionError && !sectionPage && (
                <div className="profile-review-inline-error" role="alert">
                  <p>{sectionError}</p>
                  {activeDescriptor && (
                    <button
                      type="button"
                      className="secondary-action"
                      onClick={() => void loadSection(
                        profile.profile_version_id,
                        activeDescriptor.key,
                        0,
                        false,
                      )}
                    >
                      Try again
                    </button>
                  )}
                </div>
              )}

              {!loadingSection && !sectionError && sectionPage && sectionPage.items.length === 0 && (
                <div className="profile-review-empty-section profile-review-empty-section--large">
                  <div className="empty-document__glyph" aria-hidden="true">⌁</div>
                  <h4>No saved items</h4>
                  <p>This section exists, but this saved profile version does not contain any entries.</p>
                </div>
              )}

              {sectionPage && sectionPage.items.length > 0 && (
                <div className="profile-review-item-list">
                  {sectionPage.items.map((item) => {
                    const metadata = [item.subtitle, item.date_range, item.location].filter(Boolean);
                    return (
                      <article
                        key={`${sectionPage.profile_version_id}:${sectionPage.key}:${item.ordinal}`}
                        className="profile-review-item"
                      >
                        <div className="profile-review-item__ordinal" aria-hidden="true">
                          {String(item.ordinal + 1).padStart(2, "0")}
                        </div>
                        <div className="profile-review-item__body">
                          <header>
                            <h4>{item.title || `${sectionPage.label} item`}</h4>
                            {metadata.length > 0 && <p>{metadata.join(" · ")}</p>}
                          </header>
                          {item.summary && <p className="profile-review-prose">{item.summary}</p>}
                          {item.highlights.length > 0 && (
                            <section className="profile-review-highlights" aria-label="Highlights">
                              <h5>Highlights</h5>
                              <ul>
                                {item.highlights.map((highlight, index) => (
                                  <li key={`${item.ordinal}:highlight:${index}`}>{highlight}</li>
                                ))}
                              </ul>
                            </section>
                          )}
                          {item.details.length > 0 && (
                            <dl className="profile-review-detail-grid">
                              {item.details.map((detail, index) => (
                                <div key={`${detail.label}:${index}`}>
                                  <dt>{detail.label}</dt>
                                  <dd>{detail.value}</dd>
                                </div>
                              ))}
                            </dl>
                          )}
                          {item.tags.length > 0 && (
                            <ul className="profile-review-tags" aria-label="Tags">
                              {item.tags.map((tag, index) => (
                                <li key={`${tag}:${index}`}>{tag}</li>
                              ))}
                            </ul>
                          )}
                        </div>
                      </article>
                    );
                  })}
                </div>
              )}

              {sectionPage && sectionError && (
                <div className="profile-review-inline-error profile-review-inline-error--pagination" role="alert">
                  <p>{sectionError}</p>
                  {nextOffset !== null && nextOffset !== undefined && (
                    <button
                      type="button"
                      className="secondary-action"
                      onClick={() => void loadSection(
                        profile.profile_version_id,
                        sectionPage.key,
                        nextOffset,
                        true,
                      )}
                    >
                      Try loading more again
                    </button>
                  )}
                </div>
              )}

              {sectionPage && (
                <footer className="profile-review-pagination">
                  <span>
                    Showing {visibleItems} of {sectionPage.total_items}
                  </span>
                  {canLoadMore && !sectionError && (
                    <button
                      type="button"
                      className="secondary-action"
                      onClick={() => void loadSection(
                        profile.profile_version_id,
                        sectionPage.key,
                        nextOffset as number,
                        true,
                      )}
                      disabled={loadingMore}
                    >
                      {loadingMore ? "Loading more…" : `Load more (${sectionPage.total_items - visibleItems})`}
                    </button>
                  )}
                </footer>
              )}
            </article>
          )}

          {!loadingProfile && profile && reviewMode === "diff" && (
            <article className="profile-review-document profile-version-diff" aria-labelledby="profile-version-diff-title">
              <header className="profile-review-section-header">
                <div>
                  <p className="section-kicker">Version comparison</p>
                  <h3 id="profile-version-diff-title" ref={contentHeadingRef} tabIndex={-1}>
                    What changed
                  </h3>
                  <p>
                    {versionDiff
                      ? `Version ${versionDiff.from_version.version_number} → version ${versionDiff.to_version.version_number}`
                      : "Compare two immutable local profile versions."}
                  </p>
                </div>
                <button
                  type="button"
                  className="secondary-action"
                  onClick={() => setReviewMode("profile")}
                  disabled={loadingDiff}
                >
                  Back to profile
                </button>
              </header>

              {loadingDiff && (
                <div className="profile-review-section-loading" role="status">
                  <span className="working-indicator" aria-hidden="true" />
                  Comparing saved versions…
                </div>
              )}

              {!loadingDiff && diffError && (
                <div className="profile-review-inline-error" role="alert">
                  <p>{diffError}</p>
                  {hasComparisonVersion && (
                    <button type="button" className="secondary-action" onClick={() => void openVersionDiff()}>
                      Try again
                    </button>
                  )}
                </div>
              )}

              {!loadingDiff && versionDiff && versionDiff.total_changes === 0 && (
                <div className="profile-review-empty-section profile-review-empty-section--large">
                  <div className="empty-document__glyph" aria-hidden="true">✓</div>
                  <h4>No visible profile changes</h4>
                  <p>These saved versions contain the same public Basic Details and section content.</p>
                </div>
              )}

              {!loadingDiff && versionDiff && versionDiff.total_changes > 0 && (
                <div className="profile-version-diff__body">
                  <div className="profile-version-diff__summary" role="status">
                    <strong>{versionDiff.total_changes}</strong>
                    <span>{versionDiff.total_changes === 1 ? "visible change" : "visible changes"}</span>
                  </div>

                  {versionDiff.basic_changes.length > 0 && (
                    <section aria-labelledby="profile-basic-changes-title">
                      <h4 id="profile-basic-changes-title">Basic Details</h4>
                      <div className="profile-version-diff__list">
                        {versionDiff.basic_changes.map((change) => (
                          <article key={change.field}>
                            <h5>{change.label}</h5>
                            <div>
                              <span><small>Before</small>{displayChangeValue(change.before)}</span>
                              <b aria-hidden="true">→</b>
                              <span><small>After</small>{displayChangeValue(change.after)}</span>
                            </div>
                          </article>
                        ))}
                      </div>
                    </section>
                  )}

                  {versionDiff.section_changes.some((section) => section.content_changed) && (
                    <section aria-labelledby="profile-section-changes-title">
                      <h4 id="profile-section-changes-title">Profile sections</h4>
                      <ul className="profile-version-diff__sections">
                        {versionDiff.section_changes
                          .filter((section) => section.content_changed)
                          .map((section) => (
                            <li key={section.key}>
                              <span>{section.label}</span>
                              <small>
                                {section.before_count === section.after_count
                                  ? `${section.after_count} ${section.after_count === 1 ? "item" : "items"} · content revised`
                                  : `${section.before_count} → ${section.after_count} items`}
                              </small>
                            </li>
                          ))}
                      </ul>
                    </section>
                  )}
                </div>
              )}
            </article>
          )}

          {!loadingProfile && profile && reviewMode === "edit" && basicsForm && basics && (
            <article className="profile-review-document profile-basics-editor" aria-labelledby="profile-basics-editor-title">
              <header className="profile-review-section-header">
                <div>
                  <p className="section-kicker">Current profile · Version {profile.version_number}</p>
                  <h3 id="profile-basics-editor-title" ref={contentHeadingRef} tabIndex={-1}>
                    Edit Basic Details
                  </h3>
                  <p>Saving creates a new immutable profile version. Existing versions remain available in history.</p>
                </div>
              </header>

              <form onSubmit={(event) => void submitBasicDetails(event)} noValidate={false}>
                <fieldset disabled={savingBasics || committedBasicsReceipt !== null}>
                  <BasicDetailsFields
                    value={basicsForm}
                    onChange={(field, value) => setBasicsForm((current) => (
                      current ? { ...current, [field]: value } : current
                    ))}
                    nameInputRef={editNameRef}
                    countryCodeHelpId="profile-country-code-help"
                  />
                </fieldset>

                {basicsError && (
                  <div className="profile-review-inline-error" role="alert">
                    <p>{basicsError}</p>
                    {saveConflict && !committedBasicsReceipt && (
                      <button
                        type="button"
                        className="secondary-action"
                        onClick={() => runAfterDirtyCheck(returnToCurrentProfile)}
                        disabled={savingBasics}
                      >
                        Reload and review current
                      </button>
                    )}
                  </div>
                )}

                <footer className="profile-basics-editor__actions">
                  <span aria-live="polite">
                    {committedBasicsReceipt
                      ? `Profile version ${committedBasicsReceipt.version_number} is saved; workspace refresh needed`
                      : basicsDirty
                      ? `${Object.keys(basicsPatch).length} ${Object.keys(basicsPatch).length === 1 ? "field" : "fields"} changed`
                      : "No unsaved changes"}
                  </span>
                  <div>
                    <button
                      type="button"
                      className="quiet-action"
                      onClick={() => {
                        if (savingBasics) return;
                        if (committedBasicsReceipt) {
                          returnToCurrentProfile();
                          return;
                        }
                        runAfterDirtyCheck(() => {
                          setReviewMode("profile");
                          setBasics(null);
                          setBasicsForm(null);
                          setBasicsError(null);
                          setSaveConflict(false);
                          basicsSaveRequestRef.current = null;
                        }, true);
                      }}
                      aria-disabled={savingBasics}
                    >
                      {committedBasicsReceipt ? "Close" : "Cancel"}
                    </button>
                    <button
                      type="submit"
                      className="primary-action"
                      disabled={savingBasics || (!basicsDirty && !committedBasicsReceipt) || (saveConflict && !committedBasicsReceipt)}
                    >
                      {savingBasics
                        ? committedBasicsReceipt ? "Refreshing workspace…" : "Saving new version…"
                        : committedBasicsReceipt ? "Retry refresh" : "Save as new version"}
                    </button>
                  </div>
                </footer>
              </form>
            </article>
          )}

          {!loadingProfile && profile && reviewMode === "work-edit" && workForm && (
            <article className="profile-review-document profile-work-editor" aria-labelledby="profile-work-editor-title">
              <header className="profile-review-section-header">
                <div>
                  <p className="section-kicker">Current profile · Version {profile.version_number}</p>
                  <h3 id="profile-work-editor-title" ref={contentHeadingRef} tabIndex={-1}>
                    {workOriginal ? "Edit Work History" : "Add Work History"}
                  </h3>
                  <p>
                    {workOriginal
                      ? "Queue this edit with the rest of your review; the saved profile does not change until Apply."
                      : "Saving this new role creates an immutable version and preserves fields this editor does not expose."}
                  </p>
                </div>
              </header>

              <form onSubmit={(event) => void submitWorkEntry(event)}>
                <fieldset disabled={savingWork || committedWorkReceipt !== null}>
                  <div className="profile-work-editor__grid">
                    <label>
                      <span>Role or position <b aria-hidden="true">*</b></span>
                      <input
                        ref={workPositionRef}
                        value={workForm.position}
                        onChange={(event) => setWorkForm((current) => current ? { ...current, position: event.target.value } : current)}
                        maxLength={240}
                        autoComplete="organization-title"
                        required
                      />
                    </label>
                    <label>
                      <span>Organization <b aria-hidden="true">*</b></span>
                      <input
                        value={workForm.name}
                        onChange={(event) => setWorkForm((current) => current ? { ...current, name: event.target.value } : current)}
                        maxLength={240}
                        autoComplete="organization"
                        required
                      />
                    </label>
                    <label>
                      <span>Start date</span>
                      <input
                        value={workForm.startDate}
                        onChange={(event) => setWorkForm((current) => current ? { ...current, startDate: event.target.value } : current)}
                        maxLength={80}
                        placeholder="2022-04 or Spring 2022"
                      />
                    </label>
                    <label>
                      <span>End date</span>
                      <input
                        value={workForm.endDate}
                        onChange={(event) => setWorkForm((current) => current ? { ...current, endDate: event.target.value } : current)}
                        maxLength={80}
                        placeholder="Present or 2025-08"
                      />
                    </label>
                    <label>
                      <span>Location</span>
                      <input
                        value={workForm.location}
                        onChange={(event) => setWorkForm((current) => current ? { ...current, location: event.target.value } : current)}
                        maxLength={200}
                        autoComplete="address-level2"
                      />
                    </label>
                    <label>
                      <span>Organization website</span>
                      <input
                        type="url"
                        value={workForm.url}
                        onChange={(event) => setWorkForm((current) => current ? { ...current, url: event.target.value } : current)}
                        maxLength={2048}
                        placeholder="https://example.com"
                        autoComplete="url"
                      />
                    </label>
                    <label className="profile-work-editor__wide">
                      <span>Role summary</span>
                      <textarea
                        value={workForm.summary}
                        onChange={(event) => setWorkForm((current) => current ? { ...current, summary: event.target.value } : current)}
                        maxLength={1000}
                        rows={5}
                        placeholder="Scope, team, responsibilities, and context"
                      />
                      <small>{workForm.summary.length.toLocaleString()} / 1,000</small>
                    </label>
                    <label className="profile-work-editor__wide">
                      <span>Highlights</span>
                      <textarea
                        value={workForm.highlights}
                        onChange={(event) => setWorkForm((current) => current ? { ...current, highlights: event.target.value } : current)}
                        maxLength={1807}
                        rows={8}
                        placeholder={"One accomplishment per line\nImproved…\nLed…"}
                        aria-describedby="profile-work-highlights-help"
                      />
                      <small id="profile-work-highlights-help">
                        {normalizedHighlights(workForm.highlights).length} / 8 highlights · one per line
                      </small>
                    </label>
                  </div>
                </fieldset>

                {workMutationError && (
                  <div className="profile-review-inline-error" role="alert">
                    <p>{workMutationError}</p>
                    {workRequiresProfileName && (
                      <button
                        type="button"
                        className="secondary-action"
                        onClick={() => runAfterDirtyCheck(recoverWorkByEditingBasicDetails)}
                        disabled={savingWork}
                      >
                        Edit Basic Details
                      </button>
                    )}
                    {workSaveConflict && !workRequiresProfileName && !committedWorkReceipt && (
                      <button
                        type="button"
                        className="secondary-action"
                        onClick={() => runAfterDirtyCheck(returnToCurrentProfile)}
                        disabled={savingWork}
                      >
                        Reload and review current
                      </button>
                    )}
                  </div>
                )}

                <footer className="profile-basics-editor__actions">
                  <span aria-live="polite">
                    {committedWorkReceipt
                      ? `Profile version ${committedWorkReceipt.version_number} is saved; workspace refresh needed`
                      : workDirty
                      ? workOriginal
                        ? `${Object.keys(workPatch).length} ${Object.keys(workPatch).length === 1 ? "field" : "fields"} changed`
                        : "New role not yet saved"
                      : "No unsaved changes"}
                  </span>
                  <div>
                    <button
                      type="button"
                      className="quiet-action"
                      onClick={() => {
                        if (savingWork) return;
                        if (committedWorkReceipt) {
                          returnToCurrentProfile();
                          return;
                        }
                        runAfterDirtyCheck(() => {
                          setReviewMode("profile");
                          setWorkForm(null);
                          setWorkOriginal(null);
                          setWorkMutationError(null);
                          setWorkSaveConflict(false);
                          setWorkRequiresProfileName(false);
                          workSaveRequestRef.current = null;
                        }, true);
                      }}
                      aria-disabled={savingWork}
                    >
                      {committedWorkReceipt ? "Close" : "Cancel"}
                    </button>
                    <button
                      type="submit"
                      className="primary-action"
                      disabled={savingWork || (!workDirty && !committedWorkReceipt) || (workSaveConflict && !committedWorkReceipt)}
                    >
                      {savingWork
                        ? committedWorkReceipt ? "Refreshing workspace…" : "Saving new version…"
                        : committedWorkReceipt
                          ? "Retry refresh"
                          : workOriginal ? "Queue edit" : "Add role as new version"}
                    </button>
                  </div>
                </footer>
              </form>
            </article>
          )}

          {!loadingProfile && profile && reviewMode === "collection-edit" && collectionForm && (
            <ProfileCollectionEditor
              form={collectionForm}
              setForm={(next) => setCollectionForm(next)}
              original={collectionOriginal}
              versionNumber={profile.version_number}
              saving={savingCollection}
              dirtyCount={collectionOriginal
                ? Object.keys(collectionPatch).length
                : collectionFormHasInput(collectionForm) ? 1 : 0}
              error={collectionMutationError}
              conflict={collectionSaveConflict}
              requiresProfileName={collectionRequiresProfileName}
              committedVersion={committedCollectionReceipt?.version_number ?? null}
              headingRef={contentHeadingRef}
              firstInputRef={collectionFirstInputRef}
              onSubmit={(event) => void submitCollectionEntry(event)}
              onCancel={() => {
                if (committedCollectionReceipt) {
                  returnToCurrentProfile();
                  return;
                }
                runAfterDirtyCheck(() => {
                  setReviewMode("profile");
                  setCollectionForm(null);
                  setCollectionOriginal(null);
                  setCollectionMutationError(null);
                  setCollectionSaveConflict(false);
                  setCollectionRequiresProfileName(false);
                  collectionSaveRequestRef.current = null;
                }, true);
              }}
              onReloadCurrent={() => runAfterDirtyCheck(returnToCurrentProfile)}
              onEditBasicDetails={() => runAfterDirtyCheck(recoverCollectionByEditingBasicDetails)}
            />
          )}
        </main>

        <aside className="profile-review-inspector" aria-label="Saved profile details">
          <section>
            <p className="section-kicker">Inspector</p>
            <h3>Version &amp; readiness</h3>
            {profile ? (
              <dl>
                <div><dt>Version</dt><dd>{profile.version_number}</dd></div>
                <div><dt>Saved</dt><dd>{formatTimestamp(profile.created_at_ms)}</dd></div>
                <div><dt>Source</dt><dd>{humanize(profile.source_kind)}</dd></div>
                <div>
                  <dt>Readiness</dt>
                  <dd className={profile.renderable ? "is-ready" : "is-warning"}>
                    {profile.renderable ? "Renderable" : "Needs attention"}
                  </dd>
                </div>
                <div><dt>Sections</dt><dd>{profile.sections.length}</dd></div>
                <div><dt>Profile items</dt><dd>{totalItems}</dd></div>
              </dl>
            ) : (
              <p className="profile-review-inspector__placeholder">
                {loadingProfile ? "Reading local version details…" : "No version details available."}
              </p>
            )}
            {profile && (
              <div className="profile-review-inspector__actions">
                {isCurrentVersion ? (
                  <button
                    type="button"
                    className="secondary-action"
                    onClick={() => void startBasicDetailsEdit()}
                    disabled={workspaceBusy || queuedChangeCount > 0 || reviewMode !== "profile"}
                  >
                    {loadingBasics ? "Opening editor…" : "Edit Basic Details"}
                  </button>
                ) : (
                  <button
                    type="button"
                    className="secondary-action"
                    onClick={() => runAfterDirtyCheck(returnToCurrentProfile)}
                    disabled={workspaceBusy}
                  >
                    Return to current
                  </button>
                )}
                <button
                  type="button"
                  className="quiet-action"
                  onClick={() => runAfterDirtyCheck(() => void openVersionDiff())}
                  disabled={workspaceBusy || !hasComparisonVersion}
                >
                  {isCurrentVersion ? "Compare with previous" : "Compare with current"}
                </button>
              </div>
            )}
            {basicsError && reviewMode !== "edit" && (
              <p className="profile-review-inspector__error" role="alert">{basicsError}</p>
            )}
          </section>

          <section className="profile-version-history" aria-labelledby="profile-version-history-title">
            <div className="profile-version-history__heading">
              <div>
                <p className="section-kicker">Local history</p>
                <h3 id="profile-version-history-title">Saved versions</h3>
              </div>
              {historyPage && <span>{historyPage.total_items}</span>}
            </div>
            {loadingHistory && !historyPage && (
              <p className="profile-review-inspector__placeholder" role="status">Opening local history…</p>
            )}
            {historyError && (
              <div className="profile-version-history__error" role="alert">
                <p>{historyError}</p>
                <button type="button" className="quiet-action" onClick={() => void loadHistory(0, false)}>
                  Try again
                </button>
              </div>
            )}
            {historyItems.length > 0 && (
              <ol className="profile-version-history__list">
                {historyItems.map((version) => {
                  const selected = profile?.profile_version_id === version.profile_version_id;
                  const current = currentProfileVersionId === version.profile_version_id;
                  return (
                    <li key={version.profile_version_id}>
                      <button
                        type="button"
                        className={selected ? "is-selected" : ""}
                        aria-current={selected ? "true" : undefined}
                        onClick={() => runAfterDirtyCheck(() => void loadExactVersion(version.profile_version_id))}
                        disabled={workspaceBusy || selected}
                      >
                        <span>
                          <strong>Version {version.version_number}</strong>
                          <small>{formatTimestamp(version.created_at_ms)}</small>
                        </span>
                        {current && <b>Current</b>}
                      </button>
                    </li>
                  );
                })}
              </ol>
            )}
            {canLoadMoreHistory && !historyError && (
              <button
                type="button"
                className="quiet-action profile-version-history__more"
                onClick={() => void loadHistory(historyNextOffset as number, true)}
                disabled={loadingMoreHistory || workspaceBusy}
              >
                {loadingMoreHistory ? "Loading older versions…" : "Load older versions"}
              </button>
            )}
          </section>

          <section>
            <p className="section-kicker">Evidence trail</p>
            <h3>Imported material</h3>
            <button
              type="button"
              className="profile-review-evidence-button"
              onClick={() => runAfterDirtyCheck(onOpenImportedFiles)}
              disabled={savingBasics || profileMutationBusy || switchingVersion}
            >
              <span>
                <strong>{retainedSourceFiles}</strong>
                <small>active {retainedSourceFiles === 1 ? "file" : "files"}</small>
                {historicalSourceFiles > 0 && (
                  <small>{historicalSourceFiles} in history</small>
                )}
              </span>
              <b aria-hidden="true">→</b>
            </button>
            <p>Source files stay local through active-set resets and remain available in import history.</p>
          </section>

          <section className="profile-review-inspector__note">
            <span aria-hidden="true">◇</span>
            <div>
              <strong>Immutable by design</strong>
              <p>Basic Details and structured-section edits create a new version. Historical snapshots never change in place.</p>
            </div>
          </section>

          {!canExport && profile?.renderable && (
            <p className="profile-review-export-note">
              Export is not available from the current application state.
            </p>
          )}
        </aside>
      </div>

      {confirmingDiscard && (
        <div className="profile-discard-layer" role="presentation">
          <section
            className="profile-discard-dialog"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="profile-discard-title"
            aria-describedby="profile-discard-description"
            onKeyDown={(event) => {
              if (event.key !== "Tab") return;
              const buttons = Array.from(
                event.currentTarget.querySelectorAll<HTMLButtonElement>("button:not(:disabled)"),
              );
              if (buttons.length === 0) return;
              const first = buttons[0];
              const last = buttons[buttons.length - 1];
              if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last.focus();
              } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first.focus();
              }
            }}
          >
            <p className="section-kicker">
              {queuedChangeCount > 0 && !pendingNavigationPreservesQueueRef.current
                ? "Unsaved review queue"
                : reviewMode === "work-edit"
                ? "Unsaved Work History"
                : reviewMode === "collection-edit" && collectionForm
                  ? `Unsaved ${collectionLabel(collectionForm.section)}`
                  : "Unsaved Basic Details"}
            </p>
            <h3 id="profile-discard-title">
              {queuedChangeCount > 0 && !pendingNavigationPreservesQueueRef.current
                ? "Discard the review queue?"
                : "Discard these changes?"}
            </h3>
            <p id="profile-discard-description">
              {queuedChangeCount > 0 && !pendingNavigationPreservesQueueRef.current
                ? `Your current saved profile is unchanged. Continuing will discard ${queuedChangeCount} queued ${queuedChangeCount === 1 ? "change" : "changes"}${editorDirty ? " and the open form" : ""}.`
                : "Your current saved profile is unchanged. Leaving the editor will discard only the unsaved form changes; previously queued review choices stay in place."}
            </p>
            <div>
              <button
                ref={discardCancelRef}
                type="button"
                className="secondary-action"
                onClick={keepEditing}
              >
                Keep reviewing
              </button>
              <button type="button" className="primary-action" onClick={discardEditsAndContinue}>
                Discard changes
              </button>
            </div>
          </section>
        </div>
      )}

      {confirmingChangeApply && queuedChangeCount > 0 && (
        <div className="profile-discard-layer" role="presentation">
          <section
            className="profile-discard-dialog profile-change-apply-dialog"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="profile-change-apply-title"
            aria-describedby="profile-change-apply-description"
            onKeyDown={(event) => {
              if (event.key !== "Tab") return;
              const buttons = Array.from(
                event.currentTarget.querySelectorAll<HTMLButtonElement>("button:not(:disabled)"),
              );
              if (buttons.length === 0) return;
              const first = buttons[0];
              const last = buttons[buttons.length - 1];
              if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last.focus();
              } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first.focus();
              }
            }}
          >
            <p className="section-kicker">Local profile review</p>
            <h3 id="profile-change-apply-title">
              {committedProfileChangesReceipt
                ? `Version ${committedProfileChangesReceipt.version_number} is saved`
                : `Apply ${queuedChangeCount} queued ${queuedChangeCount === 1 ? "change" : "changes"}?`}
            </h3>
            <p id="profile-change-apply-description">
              {committedProfileChangesReceipt
                ? "The mutation will not run again. Retry only the local workspace refresh."
                : "CVGnome will apply this queue atomically as one new immutable profile version. The current version remains available in local history."}
            </p>
            <ul className="profile-change-apply-dialog__summary">
              {PROFILE_CHANGE_SECTIONS.filter((section) => queuedSectionCounts[section] > 0).map((section) => (
                <li key={section}>
                  <span>{section === "work" ? "Work History" : collectionLabel(section)}</span>
                  <strong>{queuedSectionCounts[section]}</strong>
                </li>
              ))}
            </ul>
            {queuedProfileChanges.some((change) => change.operation.kind === "merge") && (
              <section className="profile-change-apply-dialog__merges" aria-label="Queued merge results">
                <h4>Queued merge results</h4>
                <ul>
                  {queuedProfileChanges.map((change) => {
                    if (change.operation.kind !== "merge") return null;
                    const sectionLabel = change.operation.section === "work"
                      ? "Work History"
                      : collectionLabel(change.operation.section);
                    return (
                      <li key={change.operation.suggestion_id}>
                        <span>{sectionLabel}</span>
                        <strong>
                          {change.suggestion
                            ? mergedSuggestionTitle(change.suggestion)
                            : `Keep entry ${change.operation.keep_entry_index + 1}`}
                        </strong>
                        <small>
                          {change.suggestion
                            ? mergedSuggestionDetail(change.suggestion)
                            : `Entry ${change.operation.remove_entry_index + 1} will merge into this entry.`}
                        </small>
                      </li>
                    );
                  })}
                </ul>
              </section>
            )}
            {profileChangesError && <p className="profile-review-inspector__error" role="alert">{profileChangesError}</p>}
            {profileChangesConflict && (
              <div className="profile-change-apply-dialog__recovery">
                <button type="button" className="secondary-action" onClick={() => void reloadCurrentWithQueue()} disabled={applyingProfileChanges}>Reload latest</button>
                <button type="button" className="quiet-action" onClick={discardQueuedChanges} disabled={applyingProfileChanges || committedProfileChangesReceipt !== null}>Discard queue</button>
              </div>
            )}
            <div>
              <button
                ref={changeApplyCancelRef}
                type="button"
                className="secondary-action"
                onClick={closeChangeApply}
                aria-disabled={applyingProfileChanges}
              >
                {applyingProfileChanges ? "Please wait…" : "Cancel"}
              </button>
              <button
                type="button"
                className="primary-action"
                onClick={() => void applyQueuedProfileChanges()}
                disabled={applyingProfileChanges
                  || reviewMode !== "profile"
                  || editorDirty
                  || (
                    committedProfileChangesReceipt === null
                    && (profileChangesConflict || !queuePinnedToCurrent)
                  )}
              >
                {applyingProfileChanges
                  ? committedProfileChangesReceipt ? "Refreshing workspace…" : "Applying changes…"
                  : committedProfileChangesReceipt
                    ? "Retry refresh"
                    : "Apply as one version"}
              </button>
            </div>
          </section>
        </div>
      )}

      {restoreTarget && (
        <div className="profile-discard-layer" role="presentation">
          <section
            className="profile-discard-dialog profile-restore-dialog"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="profile-restore-title"
            aria-describedby="profile-restore-description"
            onKeyDown={(event) => {
              if (event.key !== "Tab") return;
              const buttons = Array.from(
                event.currentTarget.querySelectorAll<HTMLButtonElement>("button:not(:disabled)"),
              );
              if (buttons.length === 0) return;
              const first = buttons[0];
              const last = buttons[buttons.length - 1];
              if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last.focus();
              } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first.focus();
              }
            }}
          >
            <p className="section-kicker">Local profile history</p>
            <h3 id="profile-restore-title">
              {committedRestoreReceipt
                ? `Version ${committedRestoreReceipt.version_number} is saved`
                : `Restore version ${restoreTarget.sourceVersionNumber}?`}
            </h3>
            <p id="profile-restore-description">
              {committedRestoreReceipt
                ? `CVGnome restored version ${committedRestoreReceipt.restored_from_version_number} as version ${committedRestoreReceipt.version_number}. The saved mutation will not run again; only the current profile and history view still need to refresh.`
                : `CVGnome will copy everything in version ${restoreTarget.sourceVersionNumber} into a new version and make that copy current. ${restoreTarget.expectedParentVersionNumber ? `Version ${restoreTarget.expectedParentVersionNumber}` : "Your current version"} and all imported evidence remain unchanged in local history.`}
            </p>
            {restoreError && (
              <p className="profile-review-inspector__error profile-restore-dialog__error" role="alert">
                {restoreError}
              </p>
            )}
            {restoreConflict && (
              <button
                type="button"
                className="secondary-action profile-restore-dialog__recovery"
                onClick={recoverRestoreConflict}
                disabled={restoringProfile}
              >
                Reload and review current
              </button>
            )}
            <div>
              <button
                ref={restoreCancelRef}
                type="button"
                className="secondary-action profile-restore-dialog__cancel"
                onClick={dismissProfileRestore}
                aria-disabled={restoringProfile}
              >
                {restoringProfile
                  ? committedRestoreReceipt ? "Refresh in progress…" : "Restore in progress…"
                  : committedRestoreReceipt || restoreError ? "Close" : "Cancel"}
              </button>
              <button
                type="button"
                className="primary-action"
                onClick={() => void confirmProfileRestore()}
                disabled={restoringProfile || restoreConflict}
              >
                {restoringProfile
                  ? committedRestoreReceipt ? "Refreshing workspace…" : "Restoring locally…"
                  : committedRestoreReceipt
                    ? "Retry refresh"
                    : restoreError ? "Try restore again" : "Restore as new version"}
              </button>
            </div>
          </section>
        </div>
      )}

      {confirmingWorkRemoval && (
        <div className="profile-discard-layer" role="presentation">
          <section
            className="profile-discard-dialog"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="profile-work-remove-title"
            aria-describedby="profile-work-remove-description"
            onKeyDown={(event) => {
              if (event.key !== "Tab") return;
              const buttons = Array.from(
                event.currentTarget.querySelectorAll<HTMLButtonElement>("button:not(:disabled)"),
              );
              if (buttons.length === 0) return;
              const first = buttons[0];
              const last = buttons[buttons.length - 1];
              if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last.focus();
              } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first.focus();
              }
            }}
          >
            <p className="section-kicker">Work History</p>
            <h3 id="profile-work-remove-title">
              {committedWorkReceipt
                ? `Version ${committedWorkReceipt.version_number} is saved`
                : "Queue this role for removal?"}
            </h3>
            <p id="profile-work-remove-description">
              {committedWorkReceipt
                ? "The role was removed successfully. The saved mutation will not run again; only the current profile and history view still need to refresh."
                : `Mark ${confirmingWorkRemoval.position || "this role"} at ${confirmingWorkRemoval.name || "this organization"} for removal. Nothing changes in the saved profile until you apply the queue.`}
            </p>
            {workMutationError && (
              <p className="profile-review-inspector__error" role="alert">{workMutationError}</p>
            )}
            {workRequiresProfileName && (
              <button
                type="button"
                className="secondary-action profile-work-removal-recovery"
                onClick={recoverWorkByEditingBasicDetails}
                disabled={savingWork}
              >
                Edit Basic Details
              </button>
            )}
            {workSaveConflict && !workRequiresProfileName && !committedWorkReceipt && (
              <button
                type="button"
                className="secondary-action profile-work-removal-recovery"
                onClick={returnToCurrentProfile}
                disabled={savingWork}
              >
                Reload and review current
              </button>
            )}
            <div>
              <button
                ref={workRemovalCancelRef}
                type="button"
                className="secondary-action profile-work-removal-cancel"
                onClick={dismissWorkRemoval}
                aria-disabled={savingWork}
              >
                {savingWork
                  ? committedWorkReceipt ? "Refresh in progress…" : "Removal in progress…"
                  : committedWorkReceipt ? "Close" : "Cancel"}
              </button>
              <button
                type="button"
                className="primary-action profile-work-remove-confirm"
                onClick={() => void confirmWorkRemoval()}
                disabled={savingWork || (workSaveConflict && !committedWorkReceipt)}
              >
                {savingWork
                  ? committedWorkReceipt ? "Refreshing workspace…" : "Removing as new version…"
                  : committedWorkReceipt ? "Retry refresh" : "Queue removal"}
              </button>
            </div>
          </section>
        </div>
      )}

      {confirmingCollectionRemoval && activeCollectionSection && (
        <div className="profile-discard-layer" role="presentation">
          <section
            className="profile-discard-dialog"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="profile-collection-remove-title"
            aria-describedby="profile-collection-remove-description"
            onKeyDown={(event) => {
              if (event.key !== "Tab") return;
              const buttons = Array.from(
                event.currentTarget.querySelectorAll<HTMLButtonElement>("button:not(:disabled)"),
              );
              if (buttons.length === 0) return;
              const first = buttons[0];
              const last = buttons[buttons.length - 1];
              if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last.focus();
              } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first.focus();
              }
            }}
          >
            <p className="section-kicker">{collectionLabel(activeCollectionSection)}</p>
            <h3 id="profile-collection-remove-title">
              {committedCollectionReceipt
                ? `Version ${committedCollectionReceipt.version_number} is saved`
                : `Queue this ${collectionSingular(activeCollectionSection)} for removal?`}
            </h3>
            <p id="profile-collection-remove-description">
              {committedCollectionReceipt
                ? `The ${collectionSingular(activeCollectionSection)} was removed successfully. The saved mutation will not run again; only the current profile and history view still need to refresh.`
                : `Mark ${collectionEntryTitle(activeCollectionSection, confirmingCollectionRemoval)} for removal. Nothing changes in the saved profile until you apply the queue.`}
            </p>
            {collectionMutationError && (
              <p className="profile-review-inspector__error" role="alert">{collectionMutationError}</p>
            )}
            {collectionRequiresProfileName && (
              <button
                type="button"
                className="secondary-action profile-work-removal-recovery"
                onClick={recoverCollectionByEditingBasicDetails}
                disabled={savingCollection}
              >
                Edit Basic Details
              </button>
            )}
            {collectionSaveConflict && !collectionRequiresProfileName && !committedCollectionReceipt && (
              <button
                type="button"
                className="secondary-action profile-work-removal-recovery"
                onClick={returnToCurrentProfile}
                disabled={savingCollection}
              >
                Reload and review current
              </button>
            )}
            <div>
              <button
                ref={collectionRemovalCancelRef}
                type="button"
                className="secondary-action profile-work-removal-cancel"
                onClick={dismissCollectionRemoval}
                aria-disabled={savingCollection}
              >
                {savingCollection
                  ? committedCollectionReceipt ? "Refresh in progress…" : "Removal in progress…"
                  : committedCollectionReceipt ? "Close" : "Cancel"}
              </button>
              <button
                type="button"
                className="primary-action profile-work-remove-confirm"
                onClick={() => void confirmCollectionRemoval()}
                disabled={savingCollection || (collectionSaveConflict && !committedCollectionReceipt)}
              >
                {savingCollection
                  ? committedCollectionReceipt ? "Refreshing workspace…" : "Removing as new version…"
                  : committedCollectionReceipt ? "Retry refresh" : "Queue removal"}
              </button>
            </div>
          </section>
        </div>
      )}
    </section>
  );
}
