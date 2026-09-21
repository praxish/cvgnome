// SPDX-License-Identifier: MPL-2.0
import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import "./App.css";
import {
  BasicDetailsFields,
  basicDetailsFormHasInput,
  basicDetailsPatchFields,
  buildNewProfileBasicDetailsPatch,
  emptyBasicDetailsForm,
  validateBasicDetailsForm,
  type BasicDetailsField,
  type BasicDetailsForm,
  type NewProfileBasicDetailsPatch,
} from "./BasicDetailsFields";
import { CvGnomeMark } from "./CvGnomeLogo";
import { DesktopCloseGuard } from "./DesktopCloseGuard";
import { OpportunitiesPanel } from "./OpportunitiesPanel";
import { MemoryPanel } from "./MemoryPanel";
import {
  ProfileReviewPanel,
  type ProfileBasicsUpdateReceipt,
  type ProfileChangesReceipt,
  type ProfileRestoreReceipt,
  type ProfileWorkUpdateReceipt,
} from "./ProfileReviewPanel";
import type { ProfileCollectionUpdateReceipt } from "./ProfileCollections";
import { ProviderSettingsPanel } from "./ProviderSettingsPanel";
import {
  type ReviewItemApplyReceipt,
  type ReviewInboxCounts,
} from "./ReviewInboxPanel";
import { ReviewWorkspace } from "./ReviewWorkspace";
import type { SourceReviewReceipt } from "./SourceReviewPanel";
import {
  formatBytes,
  ResumeExportResult,
  ResumeFormat,
  safeResumeExportError,
} from "./resumeExport";
import { SourcesPanel } from "./SourcesPanel";
import {
  applyUiZoom,
  DEFAULT_UI_ZOOM,
  readUiZoom,
  steppedUiZoom,
  uiZoomPercent,
} from "./uiZoom";

type ActiveView = "profile" | "roles" | "settings";
type ProfileSection = "overview" | "review" | "materials" | "inbox" | "memories";

type EngineStatus = {
  engine_version: string;
  schema_version: number;
  profile_versions: number;
  opportunities: number;
  artifacts: number;
  pending_jobs: number;
  source_snapshots: number;
  source_imports: number;
  source_previews: number;
  latest_profile_name: string | null;
  latest_profile_renderable: boolean | null;
  source_retention_generation: number;
  retained_source_files: number;
  retained_source_bytes: number;
  retained_source_imports: number;
  historical_source_files: number;
  historical_source_imports: number;
  last_source_import_at_ms: number | null;
  source_retention_reset_at_ms: number | null;
  review_inbox_items: number;
  review_deferred_items: number;
  review_history_items: number;
  source_review_inbox_items: number;
  source_review_deferred_items: number;
  source_review_history_items: number;
  memory_count: number;
};

type RetainedSourceStatus = Pick<
  EngineStatus,
  | "source_retention_generation"
  | "retained_source_files"
  | "retained_source_bytes"
  | "retained_source_imports"
  | "historical_source_files"
  | "historical_source_imports"
  | "last_source_import_at_ms"
  | "source_retention_reset_at_ms"
>;

type CareerWorkspaceResetExpected = Pick<
  EngineStatus,
  | "profile_versions"
  | "opportunities"
  | "artifacts"
  | "source_imports"
  | "source_previews"
  | "source_retention_generation"
  | "review_inbox_items"
  | "review_deferred_items"
  | "review_history_items"
  | "source_review_inbox_items"
  | "source_review_deferred_items"
  | "source_review_history_items"
  | "memory_count"
>;

type CareerWorkspaceResetReceipt = CareerWorkspaceResetExpected & {
  request_id: string;
  reset_at_ms: number;
  schema_version: number;
  created: boolean;
};

type CareerWorkspaceResetFailure = {
  message: string;
  retryable: boolean;
};

type CanonicalProfileImportResult = {
  profile_version_id: string;
  version_number: number;
  created: boolean;
  checksum_sha256: string;
  profile_name: string;
  work_entries: number;
  education_entries: number;
  skill_groups: number;
  renderable: boolean;
};

type ManualProfileStartReceipt = {
  request_id: string;
  profile_version_id: string;
  parent_profile_version_id: null;
  version_number: number;
  created_at_ms: number;
  created: boolean;
  changed_fields: BasicDetailsField[];
  profile_name: string;
  headline: string | null;
  renderable: boolean;
};

type ManualProfileStartRecovery = "submit" | "retry" | "refresh" | "blocked";

type SourceKind = "resume" | "evidence" | "preferences" | "unclassified";
type SourceFormat = "pdf" | "docx" | "json" | "txt" | "md" | "csv";
type SourceWarningStage = "scan" | "parse" | "synthesis";
type SourceWarningSeverity = "info" | "warning" | "blocking";
type SourceWarningCode =
  | "unsupported_file_type"
  | "file_too_large"
  | "encrypted_document"
  | "unreadable_document"
  | "duplicate_content"
  | "unclassified_source"
  | "partial_parse"
  | "scan_limit_reached"
  | "conflicting_facts"
  | "missing_identity"
  | "insufficient_resume_content"
  | "base_profile_changed";

type AggregateSourceWarning = {
  code: SourceWarningCode;
  stage: SourceWarningStage;
  severity: SourceWarningSeverity;
  count: number;
};

type ProfileReference = {
  profile_version_id: string;
  version_number: number;
  profile_name: string;
};

type ProfileSourceScanResult = {
  scan_id: string;
  expires_at_ms: number;
  base_profile: ProfileReference | null;
  file_counts: {
    discovered: number;
    staged: number;
    parsed: number;
    duplicates: number;
    skipped: number;
    failed: number;
  };
  source_counts: Record<SourceKind, number>;
  format_counts: Partial<Record<SourceFormat, number>>;
  warnings: AggregateSourceWarning[];
  review_candidate_count: number;
  source_review_count: number;
  can_build: boolean;
};

type ProfileSourceBuildResult = {
  scan_id: string;
  profile_version_id: string;
  version_number: number;
  created: boolean;
  checksum_sha256: string;
  profile_name: string;
  renderable: boolean;
  source_counts: {
    parsed: number;
    used: number;
    unused: number;
  };
  profile_counts: {
    work_entries: number;
    education_entries: number;
    skill_groups: number;
    evidence_claims: number;
    preference_items: number;
  };
  warnings: AggregateSourceWarning[];
  review_item_count: number;
  source_review_count: number;
};

type SourceRecoveryPatch = { name: string; summary: string | null };
type ResumedSourceScan = {
  scan: ProfileSourceScanResult;
  recovery_patch: SourceRecoveryPatch | null;
};
type SourceRecoveryForm = {
  scanId: string;
  name: string;
  summary: string;
  request: SourceRecoveryPatch | null;
  recovery: "edit" | "retry" | "conflict";
  error: string | null;
};

type SourceSelectionMode = "files" | "folder";

type ActiveAction =
  | "canonical-import"
  | "manual-start"
  | "source-files-scan"
  | "source-folder-scan"
  | "source-build"
  | "source-discard"
  | "source-recovery"
  | "source-resume"
  | "source-correction"
  | "source-reset"
  | ResumeFormat
  | null;

type SourceFlowState =
  | { status: "idle" }
  | { status: "scanning" }
  | { status: "review"; scan: ProfileSourceScanResult }
  | { status: "building"; scan: ProfileSourceScanResult }
  | {
      status: "complete";
      scan: ProfileSourceScanResult;
      result: ProfileSourceBuildResult;
    }
  | {
      status: "error";
      message: string;
      scan?: ProfileSourceScanResult;
      canRetryBuild?: boolean;
    };

type Notice = {
  kind: "success" | "error" | "info";
  message: string;
};

const SOURCE_KIND_LABELS: Record<SourceKind, string> = {
  resume: "Resume sources",
  evidence: "Evidence sources",
  preferences: "Preference sources",
  unclassified: "Unclassified",
};

const SOURCE_FORMAT_LABELS: Record<SourceFormat, string> = {
  pdf: "PDF",
  docx: "DOCX",
  json: "JSON",
  txt: "Text",
  md: "Markdown",
  csv: "CSV",
};

const SOURCE_WARNING_SEVERITY_LABELS: Record<SourceWarningSeverity, string> = {
  info: "Info",
  warning: "Warning",
  blocking: "Blocking",
};

const NON_RETRYABLE_SOURCE_BUILD_ERROR_CODES: ReadonlySet<string> = new Set([
  "base_profile_changed",
  "engine_schema_error",
  "input_changed",
  "profile_source_base_changed",
  "profile_source_commit_invalid",
  "profile_source_input_changed",
  "profile_source_manifest_invalid",
  "profile_source_not_buildable",
  "profile_source_report_invalid",
  "profile_source_scan_committed",
  "profile_source_scan_conflict",
  "profile_source_scan_expired",
  "profile_source_scan_interrupted",
  "profile_source_scan_not_found",
  "source_extraction_conflict",
  "source_integrity_error",
  "source_path_invalid",
  "vault_integrity_error",
  "vault_migration_history_invalid",
  "vault_identity_mismatch",
  "vault_schema_newer",
  "vault_storage_error",
]);

const RETRYABLE_SOURCE_BUILD_ERROR_CODES: ReadonlySet<string> = new Set([
  "profile_source_scan_busy",
  "vault_busy",
]);

const RETRYABLE_SOURCE_BUILD_ERROR_FRAGMENTS = [
  "timed out and was stopped",
  "unable to start the local engine",
  "could not send the request to the local engine",
  "local engine process could not be monitored",
  "local engine exited unexpectedly",
] as const;

function errorMessage(caught: unknown): string {
  return caught instanceof Error ? caught.message : String(caught);
}

function sourceErrorCode(caught: unknown): string | null {
  const normalized = errorMessage(caught).trim().toLowerCase();
  const match = /^([a-z0-9][a-z0-9_.-]{0,63})(?::|$)/.exec(normalized);
  return match?.[1] ?? null;
}

function canRetrySourceBuild(caught: unknown): boolean {
  const code = sourceErrorCode(caught);
  if (code !== null) {
    if (NON_RETRYABLE_SOURCE_BUILD_ERROR_CODES.has(code)) return false;
    return RETRYABLE_SOURCE_BUILD_ERROR_CODES.has(code);
  }

  const error = errorMessage(caught).toLowerCase();
  return RETRYABLE_SOURCE_BUILD_ERROR_FRAGMENTS.some((fragment) => error.includes(fragment));
}

function safeSourceError(caught: unknown, fallback: string): string {
  const code = sourceErrorCode(caught);
  const error = errorMessage(caught).toLowerCase();
  if (code === "source_selection_empty") {
    return "Choose at least one supported source file.";
  }
  if (code === "source_selection_count_limit" || error.includes("more than 64 supported files")) {
    return "Choose no more than 64 supported source files at a time.";
  }
  if (error.includes("too many entries") && error.includes("2,000")) {
    return "The selected folder exceeds the 2,000-entry scan limit. Choose a smaller folder or select up to 64 supported files directly.";
  }
  if (code === "source_selection_size_limit" || (error.includes("exceed") && error.includes("40 mib"))) {
    return "The supported source set exceeds the 40 MiB total limit. Choose fewer or smaller files.";
  }
  if (code === "source_file_size_limit" || error.includes("per-file size limit")) {
    return "A selected file exceeds its local size limit. PDF and DOCX files may be up to 10 MiB; other supported files may be up to 2 MiB.";
  }
  if (code === "source_file_empty") {
    return "Empty files cannot be used as profile sources.";
  }
  if (code === "source_file_type_unsupported") {
    return "Choose only supported PDF, DOCX, JSON, TXT, Markdown, or CSV files.";
  }
  if (code === "source_file_non_regular" || code === "source_file_symlink") {
    return "Choose regular local files. Symbolic links and other special files are not supported.";
  }
  if (code === "source_file_duplicate") {
    return "Select each source file only once.";
  }
  if (code === "source_file_managed") {
    return "Files already inside CVGnome’s managed local vault cannot be selected as new sources.";
  }
  if (error.includes("profile_source_base_changed") || error.includes("base_profile_changed")) {
    return "The current profile changed after this scan. Discard it and select the sources again.";
  }
  if (error.includes("expired") || error.includes("not_found")) {
    return "This source review is no longer available. Select the sources again to create a fresh review.";
  }
  if (error.includes("timed out")) {
    return "The local source operation took too long and was stopped. Your profile was not changed.";
  }
  return fallback;
}

function safeSourceBuildError(caught: unknown): string {
  const code = sourceErrorCode(caught);
  const error = errorMessage(caught).toLowerCase();
  if (
    code === "profile_source_base_changed" ||
    code === "base_profile_changed" ||
    error.includes("profile_source_base_changed")
  ) {
    return "The current profile changed after this scan. Discard it and select the sources again.";
  }
  if (code === "profile_source_scan_expired" || code === "profile_source_scan_not_found") {
    return "This source review is no longer available. Select the sources again to create a fresh review.";
  }
  if (code === "profile_source_scan_committed" || code === "profile_source_commit_invalid") {
    return "The source operation may have completed, but its result could not be confirmed. Close this review and check the latest profile before selecting sources again.";
  }
  if (
    code === "input_changed" ||
    code === "profile_source_input_changed" ||
    code === "profile_source_manifest_invalid" ||
    code === "profile_source_not_buildable" ||
    code === "profile_source_report_invalid" ||
    code === "profile_source_scan_conflict" ||
    code === "profile_source_scan_interrupted"
  ) {
    return "This source review is no longer intact and cannot be retried safely. Discard it and select the sources again.";
  }
  if (
    code === "engine_schema_error" ||
    code === "source_extraction_conflict" ||
    code === "source_integrity_error" ||
    code === "source_path_invalid" ||
    code === "vault_integrity_error" ||
    code === "vault_migration_history_invalid" ||
    code === "vault_identity_mismatch" ||
    code === "vault_schema_newer" ||
    code === "vault_storage_error"
  ) {
    return "This source review failed a local consistency check and cannot be retried safely. Close it and check the local engine before selecting sources again.";
  }
  if (canRetrySourceBuild(caught)) {
    return "The local engine could not confirm the result. Try creating again to recover or confirm this reviewed operation.";
  }
  return "CVGnome could not safely complete or verify this source review. Close it and check the latest local profile before selecting sources again.";
}

function safeSourceResetError(caught: unknown): string {
  const code = sourceErrorCode(caught);
  const error = errorMessage(caught).toLowerCase();
  if (code === "profile_source_reset_busy" || error.includes("preview") || error.includes("review")) {
    return "An unfinished import review remains in the local vault. Use Clear unfinished review in Imported evidence, or finish the review shown in this window, then try again.";
  }
  if (error.includes("generation") || error.includes("conflict")) {
    return "The active import set changed before the reset completed. Check the local engine, then review the current generation and try again.";
  }
  if (code === "vault_busy" || error.includes("vault is busy")) {
    return "The local vault is busy. Wait for the current operation to finish, then try again.";
  }
  return "CVGnome could not start a new retained import set. Nothing was deleted or changed in the current profile.";
}

function safeSourceRecoveryError(caught: unknown): string {
  const code = sourceErrorCode(caught);
  const error = errorMessage(caught).toLowerCase();
  if (code === "vault_busy" || error.includes("vault is busy")) {
    return "The local vault is busy. Wait for the current operation to finish, then try clearing the unfinished review again.";
  }
  return "CVGnome could not clear the unfinished review. No retained evidence or current profile data was deleted.";
}

const TERMINAL_SOURCE_CORRECTION_CODES = new Set([
  "profile_source_scan_expired",
  "profile_source_scan_not_found",
  "profile_source_base_changed",
  "profile_source_scan_committed",
  "profile_source_scan_not_recoverable",
]);

function normalizedSourceRecoveryPatch(form: SourceRecoveryForm): SourceRecoveryPatch {
  const name = form.name.normalize("NFC").replace(/\u00a0/g, " ").trim();
  const summary = form.summary.normalize("NFC").replace(/\u00a0/g, " ")
    .replace(/\r\n?/g, "\n").replace(/\t/g, " ").trim();
  return { name, summary: summary || null };
}

function manualProfileStartFailure(caught: unknown): {
  message: string;
  recovery: ManualProfileStartRecovery;
} {
  const code = sourceErrorCode(caught);
  const error = errorMessage(caught).toLowerCase();
  if (
    code === "profile_start_conflict"
    || code === "profile_exists"
    || error.includes("profile already exists")
  ) {
    return {
      message: "A local profile may already exist. Check the workspace and open its saved Profile review.",
      recovery: "refresh",
    };
  }
  if (
    code === "profile_start_preview_conflict"
    || error.includes("unfinished source preview")
    || error.includes("unfinished import review")
  ) {
    return {
      message: "An unfinished import review must be completed or cleared before starting manually. Nothing was written by this form.",
      recovery: "blocked",
    };
  }
  if (
    code === "profile_start_request_conflict"
    || code === "manual_profile_request_conflict"
    || error.includes("request conflict")
  ) {
    return {
      message: "This retry no longer matches the original Basic Details request. Cancel and start a fresh manual profile form.",
      recovery: "blocked",
    };
  }
  const rejectedBasicDetails = [
    "profile update patch must include",
    "name is required to create a profile",
    "name cannot be cleared",
    "must not be blank",
    "contains unsupported control",
    "exceeds its local size limit",
    "email must be a valid address",
    "phone must contain a usable number",
    "profile website must",
    "country code must contain two ascii letters",
    "location must include at least one field",
  ].some((fragment) => error.includes(fragment));
  if (code === "invalid_params" || error.includes("invalid params") || rejectedBasicDetails) {
    return {
      message: "The local engine rejected these Basic Details. Review the field values and try again.",
      recovery: "submit",
    };
  }
  if (code === "manual_profile_receipt_invalid") {
    return {
      message: "CVGnome received an unexpected save receipt and cannot treat it as confirmation. Retry the exact same request so the local vault can reconcile it safely.",
      recovery: "retry",
    };
  }
  if (
    code === "vault_integrity_error"
    || code === "vault_migration_history_invalid"
    || code === "vault_identity_mismatch"
    || code === "vault_schema_newer"
    || code === "vault_storage_error"
  ) {
    return {
      message: "The local vault could not safely verify this profile start. Keep this form open and check again; do not start a different request.",
      recovery: "refresh",
    };
  }
  if (code === "manual_profile_refresh_missing" || code === "manual_profile_request_missing") {
    return {
      message: "CVGnome still cannot see the saved profile in workspace status. Check again before starting a different request.",
      recovery: "refresh",
    };
  }
  if (code === "vault_busy" || error.includes("vault is busy")) {
    return {
      message: "The local vault is busy. Wait for the current operation to finish, then retry this same request.",
      recovery: "submit",
    };
  }
  return {
    message: "CVGnome could not confirm whether the profile was saved. Retry safely with the same request; CVGnome will not create a duplicate version.",
    recovery: "retry",
  };
}

function validManualProfileStartReceipt(
  receipt: ManualProfileStartReceipt,
  requestId: string,
  patch: NewProfileBasicDetailsPatch,
): boolean {
  const expectedFields = basicDetailsPatchFields(patch);
  return receipt.request_id === requestId
    && typeof receipt.profile_version_id === "string"
    && receipt.profile_version_id.trim().length > 0
    && receipt.parent_profile_version_id === null
    && receipt.version_number === 1
    && Number.isSafeInteger(receipt.created_at_ms)
    && receipt.created_at_ms > 0
    && typeof receipt.created === "boolean"
    && Array.isArray(receipt.changed_fields)
    && receipt.changed_fields.length === expectedFields.length
    && receipt.changed_fields.every((field, index) => field === expectedFields[index])
    && receipt.profile_name === patch.name
    && receipt.headline === (patch.headline ?? null)
    && typeof receipt.renderable === "boolean";
}

function safeCareerWorkspaceResetError(caught: unknown): CareerWorkspaceResetFailure {
  const code = sourceErrorCode(caught);
  const error = errorMessage(caught).toLowerCase();
  if (code === "workspace_reset_conflict" || error.includes("workspace changed")) {
    return {
      message: "The career workspace changed after this confirmation opened. Nothing was reset. Close this dialog, review the current workspace, and try again.",
      retryable: false,
    };
  }
  if (code === "workspace_reset_request_conflict") {
    return {
      message: "This reset retry no longer matches its original confirmation. Cancel and open a new reset confirmation.",
      retryable: false,
    };
  }
  if (code === "workspace_reset_unsafe" || error.includes("unsafe")) {
    return {
      message: "CVGnome found an unsafe local storage path and refused to reset anything. Your active workspace remains protected. Reopen the app before trying again.",
      retryable: false,
    };
  }
  if (code === "workspace_reset_busy" || code === "vault_busy" || error.includes("busy")) {
    return {
      message: "The local workspace is busy. Wait for the current operation to finish, then try this same reset again.",
      retryable: true,
    };
  }
  if (
    code === "workspace_reset_recovery_failed" ||
    code === "workspace_reset_journal_invalid" ||
    code === "workspace_reset_receipt_invalid"
  ) {
    return {
      message: "CVGnome could not safely recover the local reset record. Reopen the app and check the local workspace before trying again.",
      retryable: false,
    };
  }
  return {
    message: "CVGnome could not verify a complete reset. Retrying this same request is safe; if it still fails, reopen the app and check the local workspace.",
    retryable: true,
  };
}

function counted(count: number, singular: string, plural = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : plural}`;
}

function safeWarningSeverity(value: string): SourceWarningSeverity {
  return value === "info" || value === "blocking" ? value : "warning";
}

function sourceWarningText(warning: AggregateSourceWarning): string {
  const count = Math.max(0, warning.count);
  switch (warning.code) {
    case "unsupported_file_type":
      return `${counted(count, "unsupported file was", "unsupported files were")} skipped.`;
    case "file_too_large":
      return count === 1
        ? "1 file exceeded the local size limit and was skipped."
        : `${count} files exceeded the local size limit and were skipped.`;
    case "encrypted_document":
      return `${counted(count, "protected document could", "protected documents could")} not be read.`;
    case "unreadable_document":
      return `${counted(count, "document could", "documents could")} not be parsed.`;
    case "duplicate_content":
      return `${counted(count, "duplicate source was", "duplicate sources were")} ignored.`;
    case "unclassified_source":
      return `${counted(count, "source could", "sources could")} not be classified as resume, evidence, or preferences.`;
    case "partial_parse":
      return `${counted(count, "document was", "documents were")} only partially parsed.`;
    case "scan_limit_reached":
      return "The bounded local scan stopped at its safety limit.";
    case "conflicting_facts":
      return `${counted(count, "fact needs", "facts need")} review because the sources disagree.`;
    case "missing_identity":
      return "The parsed sources did not establish a profile name.";
    case "insufficient_resume_content":
      return "The resulting profile does not yet contain enough material for resume export.";
    case "base_profile_changed":
      return "The current profile changed after this source set was scanned.";
    default:
      return `${counted(count, "source needs", "sources need")} attention.`;
  }
}

function SourceWarnings({ warnings }: { warnings: AggregateSourceWarning[] }) {
  if (warnings.length === 0) return null;
  const warningCount = warnings.reduce((total, warning) => total + warning.count, 0);
  return (
    <div className="source-warnings">
      <h5>{counted(warningCount, "warning")}</h5>
      <ul>
        {warnings.map((warning, index) => (
          <li key={`${warning.stage}-${warning.code}-${index}`}>
            <span className={`warning-level warning-level--${safeWarningSeverity(warning.severity)}`}>
              {SOURCE_WARNING_SEVERITY_LABELS[safeWarningSeverity(warning.severity)]}
            </span>
            <span>{sourceWarningText(warning)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function App() {
  const [activeView, setActiveView] = useState<ActiveView>("profile");
  const [profileSection, setProfileSection] = useState<ProfileSection>("overview");
  const [roleEditorDirty, setRoleEditorDirty] = useState(false);
  const [modelSettingsDirty, setModelSettingsDirty] = useState(false);
  const [profileEditorDirty, setProfileEditorDirty] = useState(false);
  const [profileMutationBusy, setProfileMutationBusy] = useState(false);
  const [roleTailoringBusy, setRoleTailoringBusy] = useState(false);
  const [selectedOpportunityId, setSelectedOpportunityId] = useState<string | null>(null);
  const [uiZoom, setUiZoom] = useState(() => readUiZoom());
  const [status, setStatus] = useState<EngineStatus | null>(null);
  const [engineError, setEngineError] = useState<string | null>(null);
  const [checking, setChecking] = useState(true);
  const [activeAction, setActiveAction] = useState<ActiveAction>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [latestRenderable, setLatestRenderable] = useState<boolean | null>(null);
  const [sourceFlow, setSourceFlow] = useState<SourceFlowState>({ status: "idle" });
  const [sourceSelectionMode, setSourceSelectionMode] = useState<SourceSelectionMode>("files");
  const [sourceAnnouncement, setSourceAnnouncement] = useState("");
  const [manualStartForm, setManualStartForm] = useState<BasicDetailsForm | null>(null);
  const [manualStartError, setManualStartError] = useState<string | null>(null);
  const [manualStartRecovery, setManualStartRecovery] = useState<ManualProfileStartRecovery>("submit");
  const [committedManualStartReceipt, setCommittedManualStartReceipt] = useState<ManualProfileStartReceipt | null>(null);
  const [confirmingSourceReset, setConfirmingSourceReset] = useState(false);
  const [sourceResetError, setSourceResetError] = useState<string | null>(null);
  const [confirmingSourceRecovery, setConfirmingSourceRecovery] = useState(false);
  const [sourceRecoveryError, setSourceRecoveryError] = useState<string | null>(null);
  const [sourceRecoveryForm, setSourceRecoveryForm] = useState<SourceRecoveryForm | null>(null);
  const [confirmingCareerReset, setConfirmingCareerReset] = useState(false);
  const [careerResetConfirmation, setCareerResetConfirmation] = useState("");
  const [careerResetError, setCareerResetError] = useState<string | null>(null);
  const [careerResetRetryable, setCareerResetRetryable] = useState(true);
  const [careerResetBusy, setCareerResetBusy] = useState(false);
  const sourcePanelRef = useRef<HTMLElement>(null);
  const sourceResultRef = useRef<HTMLElement>(null);
  const sourceFilesButtonRef = useRef<HTMLButtonElement>(null);
  const sourceFolderButtonRef = useRef<HTMLButtonElement>(null);
  const importedFilesButtonRef = useRef<HTMLButtonElement>(null);
  const reviewInboxSourceButtonRef = useRef<HTMLButtonElement>(null);
  const importedFilesHeadingRef = useRef<HTMLHeadingElement>(null);
  const newUserHeadingRef = useRef<HTMLHeadingElement>(null);
  const manualStartButtonRef = useRef<HTMLButtonElement>(null);
  const manualStartNameRef = useRef<HTMLInputElement>(null);
  const careerResetDialogRef = useRef<HTMLElement>(null);
  const careerResetCancelRef = useRef<HTMLButtonElement>(null);
  const careerResetReturnFocusRef = useRef<HTMLElement | null>(null);
  const careerResetRequestIdRef = useRef<string | null>(null);
  const initialProfileSectionResolvedRef = useRef(false);
  const materialsReturnSectionRef = useRef<"overview" | "review">("overview");
  const reviewInboxReturnSectionRef = useRef<"overview" | "review" | "materials" | "memories">("review");
  const profileReviewSectionRef = useRef("__overview__");
  const profileReviewVersionRef = useRef<string | null>(null);
  const returnToImportedFilesButtonRef = useRef(false);
  const returnToReviewInboxSourceButtonRef = useRef(false);
  const sourceScanInFlightRef = useRef(false);
  const sourceCorrectionInFlightRef = useRef(false);
  const sourceResumeInFlightRef = useRef(false);
  const sourceRecoveryNameRef = useRef<HTMLInputElement>(null);
  const manualStartInFlightRef = useRef(false);
  const manualStartRequestRef = useRef<{
    key: string;
    requestId: string;
    patch: NewProfileBasicDetailsPatch;
  } | null>(null);
  const roleTailoringBusyRef = useRef(false);
  const profileMutationBusyRef = useRef(false);

  const updateRoleTailoringBusy = useCallback((nextBusy: boolean) => {
    roleTailoringBusyRef.current = nextBusy;
    setRoleTailoringBusy(nextBusy);
  }, []);

  const updateProfileMutationBusy = useCallback((nextBusy: boolean) => {
    profileMutationBusyRef.current = nextBusy;
    setProfileMutationBusy(nextBusy);
  }, []);

  const rememberProfileReviewSection = useCallback((section: string) => {
    profileReviewSectionRef.current = section;
  }, []);

  const rememberInspectedProfileVersion = useCallback((profileVersionId: string | null) => {
    profileReviewVersionRef.current = profileVersionId;
  }, []);

  const checkEngine = useCallback(async () => {
    setChecking(true);
    setEngineError(null);
    try {
      const nextStatus = await invoke<EngineStatus>("engine_status");
      setStatus(nextStatus);
      setLatestRenderable(nextStatus.latest_profile_renderable);
    } catch (caught) {
      setStatus(null);
      setEngineError(errorMessage(caught));
    } finally {
      setChecking(false);
    }
  }, []);

  // Memory saves are independent of profile versions. A failed status read must
  // not unmount their editor or turn a confirmed save into an uncertain one.
  const refreshMemoryStatus = useCallback(async () => {
    const nextStatus = await invoke<EngineStatus>("engine_status");
    setStatus(nextStatus);
    setLatestRenderable(nextStatus.latest_profile_renderable);
    setEngineError(null);
  }, []);

  useEffect(() => {
    void checkEngine();
  }, [checkEngine]);

  useEffect(() => {
    if (checking || !status || initialProfileSectionResolvedRef.current) return;
    initialProfileSectionResolvedRef.current = true;
    if (activeView === "profile" && profileSection === "overview" && status.profile_versions > 0) {
      setProfileSection("review");
    }
  }, [activeView, checking, profileSection, status]);

  useEffect(() => {
    void applyUiZoom(uiZoom);
  }, [uiZoom]);

  useEffect(() => {
    if (sourceFlow.status === "review" || sourceFlow.status === "error") {
      sourcePanelRef.current?.focus();
    } else if (sourceFlow.status === "complete") {
      sourceResultRef.current?.focus();
    }
  }, [sourceFlow.status]);

  useEffect(() => {
    if (status?.source_previews === 0) {
      setConfirmingSourceRecovery(false);
      setSourceRecoveryError(null);
    }
  }, [status?.source_previews]);

  useEffect(() => {
    if (activeView !== "profile") return;
    if (profileSection === "materials") {
      if (returnToReviewInboxSourceButtonRef.current) {
        returnToReviewInboxSourceButtonRef.current = false;
        window.requestAnimationFrame(() => reviewInboxSourceButtonRef.current?.focus());
      } else {
        importedFilesHeadingRef.current?.focus();
      }
      return;
    }
    if (returnToReviewInboxSourceButtonRef.current) {
      returnToReviewInboxSourceButtonRef.current = false;
      window.requestAnimationFrame(() => reviewInboxSourceButtonRef.current?.focus());
      return;
    }
    if (returnToImportedFilesButtonRef.current) {
      returnToImportedFilesButtonRef.current = false;
      window.requestAnimationFrame(() => importedFilesButtonRef.current?.focus());
    }
  }, [activeView, profileSection]);

  useEffect(() => {
    if (!confirmingCareerReset) return;
    const dialog = careerResetDialogRef.current;
    if (!dialog) return;

    window.requestAnimationFrame(() => careerResetCancelRef.current?.focus());
    const handleDialogKeydown = (event: KeyboardEvent) => {
      const dialogBusy = dialog.getAttribute("aria-busy") === "true";
      if (event.key === "Escape" && !dialogBusy) {
        event.preventDefault();
        careerResetRequestIdRef.current = null;
        setCareerResetConfirmation("");
        setCareerResetError(null);
        setCareerResetRetryable(true);
        setConfirmingCareerReset(false);
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(
        dialog.querySelectorAll<HTMLElement>(
          "button:not(:disabled), input:not(:disabled), [href], [tabindex]:not([tabindex='-1'])",
        ),
      ).filter((element) => !element.hidden);
      if (focusable.length === 0) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (document.activeElement === dialog || !dialog.contains(document.activeElement)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handleDialogKeydown);
    return () => {
      document.removeEventListener("keydown", handleDialogKeydown);
      const returnTarget = careerResetReturnFocusRef.current;
      if (returnTarget?.isConnected) window.requestAnimationFrame(() => returnTarget.focus());
    };
  }, [confirmingCareerReset]);

  useEffect(() => {
    if (careerResetBusy) careerResetDialogRef.current?.focus();
  }, [careerResetBusy]);

  const updateProfileSummary = useCallback(
    (result: { profile_name: string; version_number: number; renderable: boolean }) => {
      profileReviewVersionRef.current = null;
      setLatestRenderable(result.renderable);
      setStatus((current) =>
        current
          ? {
              ...current,
              latest_profile_name: result.profile_name,
              profile_versions: Math.max(current.profile_versions, result.version_number),
            }
          : current,
      );
    },
    [],
  );

  const clearManualProfileStart = useCallback(() => {
    setManualStartForm(null);
    setManualStartError(null);
    setManualStartRecovery("submit");
    setCommittedManualStartReceipt(null);
    manualStartRequestRef.current = null;
  }, []);

  const finishManualProfileStart = useCallback((
    freshStatus: EngineStatus,
    receipt: ManualProfileStartReceipt | null,
  ) => {
    setStatus(freshStatus);
    setEngineError(null);
    setChecking(false);
    setLatestRenderable(freshStatus.latest_profile_renderable ?? receipt?.renderable ?? false);
    clearManualProfileStart();
    profileReviewSectionRef.current = "__overview__";
    profileReviewVersionRef.current = null;
    initialProfileSectionResolvedRef.current = true;
    setActiveView("profile");
    setProfileSection("review");
    setNotice(receipt
      ? {
          kind: "success",
          message: receipt.created
            ? `Started ${receipt.profile_name} as local profile version ${receipt.version_number}. Add structured career details from Profile review.`
            : `Confirmed ${receipt.profile_name} as local profile version ${receipt.version_number}. No duplicate version was created.`,
        }
      : {
          kind: "info",
          message: "A saved local profile is already available, so CVGnome opened Profile review without creating another version.",
        });
    setSourceAnnouncement(receipt
      ? `Manual profile version ${receipt.version_number} is saved and open for review.`
      : "A saved local profile is open for review.");
  }, [clearManualProfileStart]);

  const openFreshManualProfile = useCallback(() => {
    if (
      !status
      || checking
      || status.profile_versions > 0
      || status.pending_jobs > 0
      || status.source_previews > 0
      || sourceFlow.status !== "idle"
      || activeAction !== null
      || careerResetBusy
      || confirmingCareerReset
      || profileMutationBusyRef.current
      || roleTailoringBusyRef.current
      || manualStartInFlightRef.current
    ) return;
    setNotice(null);
    setManualStartError(null);
    setManualStartRecovery("submit");
    setCommittedManualStartReceipt(null);
    manualStartRequestRef.current = null;
    setManualStartForm(emptyBasicDetailsForm());
    setSourceAnnouncement("Manual profile start opened. Nothing is saved until Basic Details are submitted.");
    window.requestAnimationFrame(() => manualStartNameRef.current?.focus());
  }, [activeAction, careerResetBusy, checking, confirmingCareerReset, sourceFlow.status, status]);

  const cancelManualProfileStart = useCallback(() => {
    if (
      manualStartInFlightRef.current
      || committedManualStartReceipt
      || manualStartRecovery === "retry"
      || manualStartRecovery === "refresh"
    ) return;
    if (
      manualStartForm
      && basicDetailsFormHasInput(manualStartForm)
      && !window.confirm("Discard the manually entered Basic Details?")
    ) return;
    clearManualProfileStart();
    setSourceAnnouncement("Manual profile start cancelled. Nothing was saved.");
    window.requestAnimationFrame(() => manualStartButtonRef.current?.focus());
  }, [clearManualProfileStart, committedManualStartReceipt, manualStartForm, manualStartRecovery]);

  const submitManualProfileStart = useCallback(async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!manualStartForm || manualStartInFlightRef.current || manualStartRecovery === "blocked") return;

    if (!committedManualStartReceipt && manualStartRecovery === "submit") {
      const validationError = validateBasicDetailsForm(manualStartForm);
      if (validationError) {
        setManualStartError(validationError.message);
        if (validationError.field === "name") manualStartNameRef.current?.focus();
        return;
      }
      if (
        !status
        || checking
        || status.profile_versions > 0
        || status.pending_jobs > 0
        || status.source_previews > 0
        || sourceFlow.status !== "idle"
        || activeAction !== null
        || careerResetBusy
        || confirmingCareerReset
        || profileMutationBusyRef.current
        || roleTailoringBusyRef.current
      ) {
        setManualStartError("The local workspace changed while this form was open. Cancel and review the current workspace before trying again.");
        setManualStartRecovery("blocked");
        return;
      }
    }

    const draftPatch = buildNewProfileBasicDetailsPatch(manualStartForm);
    const exactRetry = manualStartRecovery === "retry" ? manualStartRequestRef.current : null;
    if (manualStartRecovery === "retry" && !exactRetry) {
      setManualStartError("The original manual profile request is no longer available. Reopen CVGnome and check the local workspace before starting again.");
      setManualStartRecovery("refresh");
      return;
    }
    const patch = exactRetry?.patch ?? draftPatch;
    manualStartInFlightRef.current = true;
    setActiveAction("manual-start");
    setManualStartError(null);
    setSourceAnnouncement(committedManualStartReceipt || manualStartRecovery === "refresh"
      ? "Checking for the saved manual profile."
      : manualStartRecovery === "retry"
        ? "Retrying the exact same manual profile request."
        : "Saving the first local profile version from Basic Details.");
    let receipt = committedManualStartReceipt;
    try {
      if (!receipt && manualStartRecovery !== "refresh") {
        const requestKey = JSON.stringify({
          expectedParentProfileVersionId: null,
          patch,
        });
        if (manualStartRecovery === "submit" && manualStartRequestRef.current?.key !== requestKey) {
          manualStartRequestRef.current = {
            key: requestKey,
            requestId: crypto.randomUUID().toLowerCase(),
            patch,
          };
        }
        if (!manualStartRequestRef.current || manualStartRequestRef.current.key !== requestKey) {
          throw new Error("manual_profile_request_missing: The exact manual profile request could not be recovered.");
        }
        const stableRequestId = manualStartRequestRef.current.requestId;
        const nextReceipt = await invoke<ManualProfileStartReceipt>("start_manual_profile", {
          expectedParentProfileVersionId: null,
          requestId: stableRequestId,
          patch,
        });
        if (!validManualProfileStartReceipt(nextReceipt, stableRequestId, patch)) {
          throw new Error("manual_profile_receipt_invalid: The local engine returned an invalid manual profile receipt.");
        }
        receipt = nextReceipt;
        setCommittedManualStartReceipt(nextReceipt);
      }

      const freshStatus = await invoke<EngineStatus>("engine_status");
      if (
        freshStatus.profile_versions < (receipt?.version_number ?? 1)
        || !freshStatus.latest_profile_name
      ) {
        throw new Error("manual_profile_refresh_missing: The saved profile is not visible in the current workspace status.");
      }
      finishManualProfileStart(freshStatus, receipt);
    } catch (caught) {
      if (receipt) {
        setCommittedManualStartReceipt(receipt);
        setManualStartRecovery("refresh");
        setManualStartError(
          `Profile version ${receipt.version_number} is saved, but CVGnome could not finish opening Profile review. Retry opening it; the profile version will not be created again.`,
        );
        setSourceAnnouncement(`Profile version ${receipt.version_number} is saved. The workspace still needs to refresh.`);
      } else {
        let reconciledStatus: EngineStatus | null = null;
        try {
          reconciledStatus = await invoke<EngineStatus>("engine_status");
          setStatus(reconciledStatus);
          setLatestRenderable(reconciledStatus.latest_profile_renderable);
        } catch {
          // Keep the last confirmed status. Retrying uses the same request ID.
        }
        if (reconciledStatus && reconciledStatus.profile_versions > 0 && reconciledStatus.latest_profile_name) {
          finishManualProfileStart(reconciledStatus, null);
        } else {
          const failure = manualProfileStartFailure(caught);
          const recovery = manualStartRecovery === "retry"
            ? "retry"
            : manualStartRecovery === "refresh" ? "refresh" : failure.recovery;
          setManualStartError(failure.message);
          setManualStartRecovery(recovery);
          setSourceAnnouncement(
            recovery === "retry"
              ? "Manual profile start was not confirmed. The exact same request can be retried safely."
              : recovery === "submit"
                ? "Manual profile start was not saved. Review the fields and try again."
              : "Manual profile start is paused until the workspace is reviewed.",
          );
        }
      }
    } finally {
      manualStartInFlightRef.current = false;
      setActiveAction(null);
    }
  }, [
    activeAction,
    careerResetBusy,
    checking,
    committedManualStartReceipt,
    confirmingCareerReset,
    finishManualProfileStart,
    manualStartForm,
    manualStartRecovery,
    sourceFlow.status,
    status,
  ]);

  const importProfile = useCallback(async () => {
    setActiveAction("canonical-import");
    setNotice(null);
    try {
      const result = await invoke<CanonicalProfileImportResult | null>("import_canonical_profile");
      if (result === null) {
        setNotice({ kind: "info", message: "Import cancelled. Your vault was not changed." });
        return;
      }

      updateProfileSummary(result);
      if (!result.renderable) {
        setNotice({
          kind: "info",
          message: `${result.profile_name} was saved, and its canonical source file is retained locally. It needs a name plus resume content before export.`,
        });
      } else if (result.created) {
        setNotice({
          kind: "success",
          message: `Imported ${result.profile_name} as local profile version ${result.version_number}. The canonical source file is retained locally.`,
        });
      } else {
        setNotice({
          kind: "success",
          message: `${result.profile_name} already matches the latest local version. The canonical source file is retained locally.`,
        });
      }
      await checkEngine();
      setProfileSection("review");
    } catch (caught) {
      setNotice({ kind: "error", message: errorMessage(caught) });
      await checkEngine();
    } finally {
      setActiveAction(null);
    }
  }, [checkEngine, updateProfileSummary]);

  const scanProfileSources = useCallback(async (mode: SourceSelectionMode) => {
    if (sourceScanInFlightRef.current) return;

    sourceScanInFlightRef.current = true;
    const selectingFiles = mode === "files";
    setSourceSelectionMode(mode);
    setActiveAction(selectingFiles ? "source-files-scan" : "source-folder-scan");
    setNotice(null);
    setSourceRecoveryForm(null);
    setSourceAnnouncement(
      selectingFiles
        ? "Scanning the selected source files locally."
        : "Scanning the selected source folder locally.",
    );
    setSourceFlow({ status: "scanning" });
    try {
      const scan = await invoke<ProfileSourceScanResult | null>(
        selectingFiles ? "scan_profile_source_files" : "scan_profile_source_folder",
      );
      if (scan === null) {
        setSourceFlow({ status: "idle" });
        setNotice({
          kind: "info",
          message: `${selectingFiles ? "File" : "Folder"} selection cancelled. Your vault was not changed.`,
        });
        setSourceAnnouncement(`${selectingFiles ? "File" : "Folder"} selection cancelled. Nothing changed.`);
        return;
      }
      setSourceFlow({ status: "review", scan });
      setSourceAnnouncement(
        `Source scan complete. ${counted(scan.file_counts.parsed, "file was", "files were")} parsed. Review the aggregate result before creating a profile version.`,
      );
    } catch (caught) {
      const message = safeSourceError(
        caught,
        `The selected ${selectingFiles ? "files" : "folder"} could not be scanned safely. Your profile was not changed.`,
      );
      setSourceFlow({ status: "error", message });
      setSourceAnnouncement("The source scan failed. Your profile was not changed.");
    } finally {
      sourceScanInFlightRef.current = false;
      setActiveAction(null);
    }
  }, []);

  const buildSourceProfile = useCallback(async (scan: ProfileSourceScanResult) => {
    setActiveAction("source-build");
    setNotice(null);
    setSourceFlow({ status: "building", scan });
    setSourceAnnouncement(scan.base_profile
      ? "Saving the reviewed local import."
      : "Creating a local profile version from the reviewed source set.");
    try {
      const result = await invoke<ProfileSourceBuildResult>("build_profile_from_source_scan", {
        scanId: scan.scan_id,
      });
      updateProfileSummary(result);
      setSourceFlow({ status: "complete", scan, result });
      setSourceAnnouncement(
        result.review_item_count > 0
          ? `${result.created ? `Profile version ${result.version_number} was created. ` : ""}${result.review_item_count} ${result.review_item_count === 1 ? "suggestion was" : "suggestions were"} held in the local review inbox. The imported files were retained, and no inbox item was added to the profile.`
          : result.created
            ? `Profile version ${result.version_number} was created. Its imported source files were retained locally.`
            : `No new version was needed. The source set matches profile version ${result.version_number}, and its imported files were retained locally.`,
      );
      await checkEngine();
    } catch (caught) {
      const canRetryBuild = canRetrySourceBuild(caught);
      setSourceFlow({
        status: "error",
        message: safeSourceBuildError(caught),
        scan,
        canRetryBuild,
      });
      setSourceAnnouncement(
        canRetryBuild
          ? "Profile construction could not be confirmed. You can try creating again."
          : "This source review cannot be retried safely. Close it before selecting sources again.",
      );
      await checkEngine();
    } finally {
      setActiveAction(null);
    }
  }, [checkEngine, updateProfileSummary]);

  const resumeSourceScan = useCallback(async () => {
    if (sourceResumeInFlightRef.current || sourceCorrectionInFlightRef.current) return;
    sourceResumeInFlightRef.current = true;
    setActiveAction("source-resume");
    setSourceRecoveryError(null);
    try {
      const resumed = await invoke<ResumedSourceScan | null>("resume_profile_source_scan");
      if (resumed === null) {
        const message = "No resumable first-profile review remains. Check the current workspace, or clear the unfinished review and choose your original files again.";
        setSourceRecoveryError(message);
        setNotice({ kind: "info", message });
        setSourceRecoveryForm(null);
        setSourceFlow((current) => "scan" in current && current.scan
          ? { status: "error", scan: current.scan, message, canRetryBuild: false }
          : { status: "idle" });
        await checkEngine();
        return;
      }
      const { scan, recovery_patch: patch } = resumed;
      setSourceFlow({ status: "review", scan });
      setSourceRecoveryForm(patch === null ? null : {
        scanId: scan.scan_id, name: patch.name, summary: patch.summary ?? "",
        request: patch, recovery: "retry", error: null,
      });
      setConfirmingSourceRecovery(false);
      setNotice(null);
      setActiveView("profile");
      setProfileSection("overview");
      setSourceAnnouncement(patch
        ? "The saved recovery request is ready for an exact retry. Its outcome must be confirmed before changing these details."
        : "The unfinished source review is open. Its staged files are available to continue.");
    } catch {
      const message = "CVGnome could not resume the review. Its files have not been discarded; try resuming again.";
      setSourceRecoveryError(message);
      setSourceRecoveryForm((current) => current ? { ...current, error: message } : current);
    } finally {
      sourceResumeInFlightRef.current = false;
      setActiveAction(null);
    }
  }, [checkEngine]);

  const recoverSourceProfile = useCallback(async (event: FormEvent<HTMLFormElement>, scan: ProfileSourceScanResult) => {
    event.preventDefault();
    if (!sourceRecoveryForm || sourceRecoveryForm.scanId !== scan.scan_id
      || sourceRecoveryForm.recovery === "conflict" || sourceCorrectionInFlightRef.current
      || sourceResumeInFlightRef.current) return;
    const patch = sourceRecoveryForm.request ?? normalizedSourceRecoveryPatch(sourceRecoveryForm);
    if (!patch.name || patch.name.length > 160 || (patch.summary?.length ?? 0) > 4000) {
      setSourceRecoveryForm((current) => current ? { ...current, error: "Enter a name of up to 160 characters and a summary of up to 4,000 characters." } : current);
      sourceRecoveryNameRef.current?.focus();
      return;
    }
    sourceCorrectionInFlightRef.current = true;
    setActiveAction("source-correction");
    // Once dispatched, retain this exact payload until a receipt or an explicit
    // engine rejection resolves it. A transport failure never makes it editable.
    setSourceRecoveryForm((current) => current ? { ...current, request: patch, error: null } : current);
    setNotice(null);
    try {
      const result = await invoke<ProfileSourceBuildResult>("recover_profile_sources", { scanId: scan.scan_id, patch });
      if (result.scan_id !== scan.scan_id || result.profile_name !== patch.name
        || typeof result.renderable !== "boolean" || typeof result.created !== "boolean"
        || !Number.isSafeInteger(result.version_number) || result.version_number < 1) {
        throw new Error("Unexpected source recovery receipt");
      }
      updateProfileSummary(result);
      setSourceRecoveryForm(null);
      setSourceFlow({ status: "complete", scan, result });
      setSourceAnnouncement(result.renderable
        ? "The profile and imported files were saved. Review the profile before exporting."
        : "The incomplete profile and imported files were saved. Add resume content in Profile review before exporting.");
      await checkEngine();
    } catch (caught) {
      const code = sourceErrorCode(caught);
      if (code === "invalid_params") {
        setSourceRecoveryForm((current) => current ? {
          ...current, request: null, recovery: "edit",
          error: "These corrections were rejected. Check the name and optional summary, then try again.",
        } : current);
      } else if (code !== null && TERMINAL_SOURCE_CORRECTION_CODES.has(code)) {
        setSourceRecoveryForm(null);
        setSourceFlow({ status: "error", scan, canRetryBuild: false,
          message: "This source review can no longer accept corrections. Check the saved workspace before closing this review or choosing the original files again." });
        await checkEngine();
      } else {
        const conflict = code === "profile_source_recovery_conflict";
        setSourceRecoveryForm((current) => current ? {
          ...current, request: patch, recovery: conflict ? "conflict" : "retry",
          error: conflict
            ? "This request differs from the recovery already saved with these files. Resume the original recovery before continuing."
            : "CVGnome could not confirm whether the profile was saved. Retry the exact request to confirm the outcome without creating another version.",
        } : current);
      }
    } finally {
      sourceCorrectionInFlightRef.current = false;
      setActiveAction(null);
    }
  }, [checkEngine, sourceRecoveryForm, updateProfileSummary]);

  const discardSourceScan = useCallback(async (scanId: string) => {
    setActiveAction("source-discard");
    setNotice(null);
    try {
      await invoke<{ discarded: boolean }>("discard_profile_source_scan", { scanId });
      setSourceRecoveryForm(null);
      setSourceFlow({ status: "idle" });
      setSourceAnnouncement("Source review cleared.");
      await checkEngine();
      window.requestAnimationFrame(() => {
        const sourceButton =
          sourceSelectionMode === "folder"
            ? sourceFolderButtonRef.current
            : sourceFilesButtonRef.current;
        sourceButton?.focus();
      });
    } catch (caught) {
      const message = safeSourceError(
        caught,
        "The local source review could not be cleared. Try again before selecting other sources.",
      );
      setSourceFlow((current) => {
        if (
          current.status === "review" ||
          current.status === "building" ||
          current.status === "complete"
        ) {
          return { status: "error", message, scan: current.scan };
        }
        return { status: "error", message };
      });
      setSourceAnnouncement("The source review could not be cleared.");
    } finally {
      setActiveAction(null);
    }
  }, [checkEngine, sourceSelectionMode]);

  const closeTerminalSourceReview = useCallback((scanId: string) => {
    setSourceRecoveryForm(null);
    setSourceFlow({ status: "idle" });
    setNotice({
      kind: "info",
      message: "Review closed. Temporary local source data will be cleaned up when the vault is available.",
    });
    setSourceAnnouncement("Source review closed locally.");
    window.requestAnimationFrame(() => {
      const sourceButton =
        sourceSelectionMode === "folder"
          ? sourceFolderButtonRef.current
          : sourceFilesButtonRef.current;
      sourceButton?.focus();
    });

    // This control is only rendered when no commit result was confirmed. The
    // engine is authoritative and never discards committed lineage; cleanup
    // remains best-effort because it may be unavailable here.
    void invoke<{ discarded: boolean }>("discard_profile_source_scan", { scanId })
      .then(() => checkEngine())
      .catch(() => undefined);
  }, [checkEngine, sourceSelectionMode]);

  const closeCommittedSourceResult = useCallback(() => {
    setSourceFlow({ status: "idle" });
    setNotice({
      kind: "success",
      message: "Result closed. The exact imported source files remain retained in your local evidence archive.",
    });
    setSourceAnnouncement("Source result closed. The imported files remain retained locally.");
    window.requestAnimationFrame(() => {
      const sourceButton =
        sourceSelectionMode === "folder"
          ? sourceFolderButtonRef.current
          : sourceFilesButtonRef.current;
      sourceButton?.focus();
    });
  }, [sourceSelectionMode]);

  const clearUnfinishedSourceReviews = useCallback(async () => {
    setActiveAction("source-recovery");
    setSourceRecoveryError(null);
    setNotice(null);
    try {
      const result = await invoke<{ discarded_previews: number }>(
        "discard_unfinished_profile_source_reviews",
      );
      setStatus((current) =>
        current
          ? {
              ...current,
              source_previews: Math.max(0, current.source_previews - result.discarded_previews),
            }
          : current,
      );
      setConfirmingSourceRecovery(false);
      const cleared = counted(result.discarded_previews, "unfinished review");
      setNotice({
        kind: "info",
        message: `Cleared ${cleared}. Temporary uncommitted review data was removed; retained evidence and the current profile were unchanged.`,
      });
      setSourceAnnouncement(
        `Cleared ${cleared}. Retained evidence and the current profile were unchanged.`,
      );
      await checkEngine();
    } catch (caught) {
      setSourceRecoveryError(safeSourceRecoveryError(caught));
      setSourceAnnouncement("The unfinished source review was not cleared. Retained evidence was unchanged.");
      await checkEngine();
    } finally {
      setActiveAction(null);
    }
  }, [checkEngine]);

  const resetActiveSourceImports = useCallback(async () => {
    if (!status) return;

    setActiveAction("source-reset");
    setSourceResetError(null);
    setNotice(null);
    try {
      const retainedStatus = await invoke<RetainedSourceStatus>("reset_profile_source_imports", {
        expectedGeneration: status.source_retention_generation,
      });
      setStatus((current) => (current ? { ...current, ...retainedStatus } : current));
      setConfirmingSourceReset(false);
      setNotice({
        kind: "info",
        message: `New import set started. New imports will enter retained set ${retainedStatus.source_retention_generation}; prior evidence remains in local history.`,
      });
      setSourceAnnouncement(
        `A new retained import set was started. Generation ${retainedStatus.source_retention_generation} is now active, and prior evidence remains in local history.`,
      );
      await checkEngine();
    } catch (caught) {
      setSourceResetError(safeSourceResetError(caught));
      setSourceAnnouncement("The active import set was not reset. Nothing was deleted.");
      await checkEngine();
    } finally {
      setActiveAction(null);
    }
  }, [checkEngine, status]);

  const openCareerWorkspaceReset = useCallback(() => {
    if (
      !status
      || careerResetBusy
      || activeAction !== null
      || sourceFlow.status === "scanning"
      || sourceFlow.status === "review"
      || sourceFlow.status === "building"
      || (sourceFlow.status === "error" && Boolean(sourceFlow.scan))
      || status.source_previews > 0
      || profileMutationBusyRef.current
      || roleTailoringBusyRef.current
    ) return;
    careerResetReturnFocusRef.current = document.activeElement as HTMLElement | null;
    careerResetRequestIdRef.current = crypto.randomUUID();
    setCareerResetConfirmation("");
    setCareerResetError(null);
    setCareerResetRetryable(true);
    setConfirmingCareerReset(true);
  }, [activeAction, careerResetBusy, sourceFlow.status, status]);

  const closeCareerWorkspaceReset = useCallback(() => {
    if (careerResetBusy) return;
    careerResetRequestIdRef.current = null;
    setCareerResetConfirmation("");
    setCareerResetError(null);
    setCareerResetRetryable(true);
    setConfirmingCareerReset(false);
  }, [careerResetBusy]);

  const resetCareerWorkspace = useCallback(async () => {
    if (!status || careerResetConfirmation !== "RESET") return;
    const requestId = careerResetRequestIdRef.current ?? crypto.randomUUID();
    careerResetRequestIdRef.current = requestId;
    const expected: CareerWorkspaceResetExpected = {
      profile_versions: status.profile_versions,
      opportunities: status.opportunities,
      artifacts: status.artifacts,
      source_imports: status.source_imports,
      source_previews: status.source_previews,
      source_retention_generation: status.source_retention_generation,
      review_inbox_items: status.review_inbox_items,
      review_deferred_items: status.review_deferred_items,
      review_history_items: status.review_history_items,
      source_review_inbox_items: status.source_review_inbox_items,
      source_review_deferred_items: status.source_review_deferred_items,
      source_review_history_items: status.source_review_history_items,
      memory_count: status.memory_count,
    };

    setCareerResetBusy(true);
    setCareerResetError(null);
    try {
      const receipt = await invoke<CareerWorkspaceResetReceipt>("reset_career_workspace", {
        requestId,
        expected: {
          profileVersions: expected.profile_versions,
          opportunities: expected.opportunities,
          artifacts: expected.artifacts,
          sourceImports: expected.source_imports,
          sourcePreviews: expected.source_previews,
          sourceRetentionGeneration: expected.source_retention_generation,
          reviewInboxItems: expected.review_inbox_items,
          reviewDeferredItems: expected.review_deferred_items,
          reviewHistoryItems: expected.review_history_items,
          sourceReviewInboxItems: expected.source_review_inbox_items,
          sourceReviewDeferredItems: expected.source_review_deferred_items,
          sourceReviewHistoryItems: expected.source_review_history_items,
          memoryCount: expected.memory_count,
        },
      });
      if (
        receipt.request_id !== requestId || receipt.schema_version !== 19 ||
        receipt.profile_versions !== 0 || receipt.opportunities !== 0 ||
        receipt.artifacts !== 0 || receipt.source_imports !== 0 ||
        receipt.source_previews !== 0 || receipt.source_retention_generation !== 1 ||
        receipt.review_inbox_items !== 0 || receipt.review_deferred_items !== 0 ||
        receipt.review_history_items !== 0 || receipt.source_review_inbox_items !== 0 ||
        receipt.source_review_deferred_items !== 0 || receipt.source_review_history_items !== 0 || receipt.memory_count !== 0
      ) {
        throw new Error("workspace_reset_invalid: The local engine returned an invalid reset receipt.");
      }

      const freshStatus = await invoke<EngineStatus>("engine_status");
      if (
        freshStatus.profile_versions !== 0 || freshStatus.opportunities !== 0 ||
        freshStatus.artifacts !== 0 || freshStatus.pending_jobs !== 0 ||
        freshStatus.source_snapshots !== 0 || freshStatus.source_imports !== 0 ||
        freshStatus.source_previews !== 0 || freshStatus.source_retention_generation !== 1 ||
        freshStatus.retained_source_files !== 0 || freshStatus.retained_source_imports !== 0 ||
        freshStatus.historical_source_files !== 0 || freshStatus.historical_source_imports !== 0 ||
        freshStatus.review_inbox_items !== 0 || freshStatus.review_deferred_items !== 0 ||
        freshStatus.review_history_items !== 0 ||
        freshStatus.source_review_inbox_items !== 0 || freshStatus.source_review_deferred_items !== 0 ||
        freshStatus.source_review_history_items !== 0 || freshStatus.memory_count !== 0 ||
        freshStatus.latest_profile_name !== null || freshStatus.latest_profile_renderable !== null
      ) {
        throw new Error("workspace_reset_invalid: The local workspace did not reopen empty.");
      }

      setStatus(freshStatus);
      setEngineError(null);
      setChecking(false);
      setLatestRenderable(null);
      setSourceFlow({ status: "idle" });
      setSourceSelectionMode("files");
      setManualStartForm(null);
      setManualStartError(null);
      setManualStartRecovery("submit");
      setCommittedManualStartReceipt(null);
      manualStartRequestRef.current = null;
      setConfirmingSourceReset(false);
      setSourceResetError(null);
      setConfirmingSourceRecovery(false);
      setSourceRecoveryError(null);
      setSelectedOpportunityId(null);
      setProfileEditorDirty(false);
      setRoleEditorDirty(false);
      setModelSettingsDirty(false);
      profileReviewSectionRef.current = "__overview__";
      profileReviewVersionRef.current = null;
      materialsReturnSectionRef.current = "overview";
      reviewInboxReturnSectionRef.current = "review";
      returnToImportedFilesButtonRef.current = false;
      initialProfileSectionResolvedRef.current = true;
      setActiveView("profile");
      setProfileSection("overview");
      setNotice({
        kind: "success",
        message: "Career workspace reset. CVGnome-managed career data was removed; model settings, the stored API key, original files, external exports, and interface zoom were preserved.",
      });
      setSourceAnnouncement("The local career workspace is empty and the new-user flow is ready.");
      careerResetRequestIdRef.current = null;
      setCareerResetConfirmation("");
      setCareerResetRetryable(true);
      setConfirmingCareerReset(false);
      window.requestAnimationFrame(() => newUserHeadingRef.current?.focus());
    } catch (caught) {
      const failure = safeCareerWorkspaceResetError(caught);
      setCareerResetError(failure.message);
      setCareerResetRetryable(failure.retryable);
    } finally {
      setCareerResetBusy(false);
    }
  }, [careerResetConfirmation, status]);

  const exportResume = useCallback(async (
    format: ResumeFormat,
    profileVersionId?: string,
  ) => {
    setActiveAction(format);
    setNotice(null);
    try {
      const result = await invoke<ResumeExportResult | null>(
        "export_resume",
        profileVersionId ? { format, profileVersionId } : { format },
      );
      if (result === null) {
        setNotice({ kind: "info", message: `${format.toUpperCase()} export cancelled.` });
        await checkEngine();
        return;
      }

      const filename = result.saved_filename ?? result.suggested_filename;
      setNotice({
        kind: "success",
        message: `Saved ${filename} (${formatBytes(result.byte_size)}).`,
      });
      await checkEngine();
    } catch (caught) {
      setNotice({ kind: "error", message: safeResumeExportError(caught, format, "profile") });
      await checkEngine();
    } finally {
      setActiveAction(null);
    }
  }, [checkEngine]);

  const handleProfileBasicsUpdated = useCallback(async (receipt: ProfileBasicsUpdateReceipt) => {
    const changed = receipt.changed_fields.length;
    setNotice({
      kind: "success",
      message: `Saved ${changed} Basic Details ${changed === 1 ? "change" : "changes"} as profile version ${receipt.version_number}.`,
    });
    await checkEngine();
  }, [checkEngine]);

  const handleProfileWorkUpdated = useCallback(async (receipt: ProfileWorkUpdateReceipt) => {
    const action = receipt.operation === "add"
      ? "Added"
      : receipt.operation === "remove"
        ? "Removed"
        : "Updated";
    setNotice({
      kind: "success",
      message: `${action} a Work History entry as profile version ${receipt.version_number}.`,
    });
    await checkEngine();
  }, [checkEngine]);

  const handleProfileCollectionUpdated = useCallback(async (
    receipt: ProfileCollectionUpdateReceipt,
  ) => {
    const action = receipt.operation === "add"
      ? "Added"
      : receipt.operation === "remove"
        ? "Removed"
        : "Updated";
    const entry = receipt.section === "projects"
      ? "project"
      : receipt.section === "education" ? "education entry" : "skill group";
    setNotice({
      kind: "success",
      message: `${action} a ${entry} as profile version ${receipt.version_number}.`,
    });
    await checkEngine();
  }, [checkEngine]);

  const handleReviewItemApplied = useCallback(async (receipt: ReviewItemApplyReceipt) => {
    const entry = receipt.candidate_kind === "projects"
      ? "project"
      : receipt.candidate_kind === "education" ? "education entry" : "skill group";
    setStatus((current) => current ? {
      ...current,
      profile_versions: Math.max(current.profile_versions, receipt.version_number),
      review_inbox_items: Math.max(0, current.review_inbox_items - 1),
      review_history_items: current.review_history_items + 1,
    } : current);
    setNotice({
      kind: "success",
      message: `Applied a reviewed ${entry} as profile version ${receipt.version_number}.`,
    });
    await checkEngine();
  }, [checkEngine]);

  const handleProfileChangesApplied = useCallback(async (receipt: ProfileChangesReceipt) => {
    updateProfileSummary(receipt);
    setNotice({
      kind: "success",
      message: `Applied ${receipt.operation_count} reviewed ${receipt.operation_count === 1 ? "change" : "changes"} together as profile version ${receipt.version_number}.`,
    });
  }, [updateProfileSummary]);

  const handleSourceChoicesApplied = useCallback(async (receipt: SourceReviewReceipt) => {
    setStatus((current) => current ? {
      ...current,
      profile_versions: Math.max(current.profile_versions, receipt.version_number),
      source_review_inbox_items: receipt.counts.inbox,
      source_review_deferred_items: receipt.counts.deferred,
      source_review_history_items: receipt.counts.history,
    } : current);
    setNotice({
      kind: "success",
      message: receipt.profile_changed
        ? `Applied ${receipt.decisions.length} source review ${receipt.decisions.length === 1 ? "choice" : "choices"} together as profile version ${receipt.version_number}.`
        : `Saved ${receipt.decisions.length} source review ${receipt.decisions.length === 1 ? "choice" : "choices"}. Your profile is unchanged.`,
    });
    await checkEngine();
  }, [checkEngine]);

  const handleProfileRestored = useCallback(async (
    receipt: ProfileRestoreReceipt,
    currentProfile: { profile_version_id: string; version_number: number } | null,
  ) => {
    if (!currentProfile) {
      setNotice({
        kind: "info",
        message: `Restored profile version ${receipt.restored_from_version_number} as version ${receipt.version_number}. The profile review still needs to refresh; retrying refresh will not create another version.`,
      });
      await checkEngine();
      return;
    }
    const restoredIsCurrent = currentProfile.profile_version_id === receipt.profile_version_id;
    if (restoredIsCurrent) updateProfileSummary(receipt);
    setNotice({
      kind: restoredIsCurrent ? "success" : "info",
      message: restoredIsCurrent
        ? `Restored profile version ${receipt.restored_from_version_number} as new current version ${receipt.version_number}.`
        : `Restored profile version ${receipt.restored_from_version_number} as version ${receipt.version_number}. A newer version ${currentProfile.version_number} is current and has been opened.`,
    });
    await checkEngine();
  }, [checkEngine, updateProfileSummary]);

  const busy = activeAction !== null;
  const manualStartBusy = activeAction === "manual-start";
  const manualStartDirty = Boolean(
    manualStartForm
    && !committedManualStartReceipt
    && basicDetailsFormHasInput(manualStartForm),
  );
  const manualStartUncertain = Boolean(
    manualStartForm
    && (
      committedManualStartReceipt
      || manualStartRecovery === "retry"
      || manualStartRecovery === "refresh"
    ),
  );
  const manualStartValidationError = manualStartForm
    ? validateBasicDetailsForm(manualStartForm)
    : null;
  const manualStartFormStatus = committedManualStartReceipt
    ? `Profile version ${committedManualStartReceipt.version_number} is saved; Profile review still needs to open`
    : manualStartRecovery === "retry"
      ? "Save outcome unconfirmed · exact request retained"
      : manualStartRecovery === "refresh"
        ? "Checking the workspace without sending a new save"
        : manualStartDirty ? "Basic Details entered · not yet saved" : "Nothing saved yet";
  const manualStartActionLabel = manualStartBusy
    ? committedManualStartReceipt || manualStartRecovery === "refresh"
      ? "Opening Profile review…"
      : manualStartRecovery === "retry" ? "Retrying exact request…" : "Creating profile…"
    : committedManualStartReceipt
      ? "Retry opening Profile review"
      : manualStartRecovery === "retry"
        ? "Retry exact request"
        : manualStartRecovery === "refresh"
          ? "Check for saved profile"
          : manualStartRecovery === "blocked" ? "Start unavailable" : "Create profile";
  const sourceBusy = sourceFlow.status === "scanning" || sourceFlow.status === "building";
  const sourceRecoveryUncertain = sourceRecoveryForm?.request != null;
  const sourceScan =
    sourceFlow.status === "review" ||
    sourceFlow.status === "building" ||
    sourceFlow.status === "complete" ||
    (sourceFlow.status === "error" && sourceFlow.scan)
      ? sourceFlow.scan
      : null;
  const hasPendingSourceReview = sourceScan !== null;
  const hasPersistedUnfinishedReview = Boolean(
    status && status.source_previews > 0 && !hasPendingSourceReview,
  );
  const sourceReviewBlocksReset = hasPendingSourceReview || Boolean(status?.source_previews);
  const hasProfile = Boolean(status && status.profile_versions > 0);
  const reviewInboxCounts: ReviewInboxCounts = {
    inbox: status?.review_inbox_items ?? 0,
    deferred: status?.review_deferred_items ?? 0,
    history: status?.review_history_items ?? 0,
  };
  const sourceReviewCounts: ReviewInboxCounts = {
    inbox: status?.source_review_inbox_items ?? 0,
    deferred: status?.source_review_deferred_items ?? 0,
    history: status?.source_review_history_items ?? 0,
  };
  const reviewItemTotal = reviewInboxCounts.inbox
    + reviewInboxCounts.deferred
    + reviewInboxCounts.history
    + sourceReviewCounts.inbox
    + sourceReviewCounts.deferred
    + sourceReviewCounts.history;
  const workspaceState: "checking" | "ready" | "unavailable" = status
    ? "ready"
    : checking
      ? "checking"
      : "unavailable";
  const careerWorkspaceHasData = Boolean(status && (
    status.profile_versions > 0 || status.opportunities > 0 || status.artifacts > 0 || status.pending_jobs > 0 ||
    status.source_snapshots > 0 || status.source_imports > 0 || status.source_previews > 0 ||
    status.retained_source_files > 0 || status.historical_source_files > 0 ||
    status.review_inbox_items > 0 || status.review_deferred_items > 0 || status.review_history_items > 0 ||
    status.source_review_inbox_items > 0 || status.source_review_deferred_items > 0 || status.source_review_history_items > 0 ||
    status.memory_count > 0 ||
    status.source_retention_generation > 1
  ));
  const canExport = Boolean(status && hasProfile && latestRenderable !== false && !checking && !busy);
  const canExportSelectedProfile = Boolean(status && hasProfile && !checking && !busy);

  const updateOpportunityCount = useCallback((count: number) => {
    setStatus((current) => (current ? { ...current, opportunities: count } : current));
  }, []);

  const canImportSources = Boolean(
    status && !checking && !busy && !careerResetBusy && !confirmingCareerReset &&
    !hasPendingSourceReview && !hasPersistedUnfinishedReview && manualStartForm === null,
  );

  const canStartManualProfile = Boolean(
    status
    && status.profile_versions === 0
    && status.pending_jobs === 0
    && sourceFlow.status === "idle"
    && !checking
    && !busy
    && !careerResetBusy
    && !confirmingCareerReset
    && !profileMutationBusy
    && !roleTailoringBusy
    && !hasPendingSourceReview
    && !hasPersistedUnfinishedReview
    && manualStartForm === null,
  );

  const reviewInboxBlocked = Boolean(
    busy
    || careerResetBusy
    || confirmingCareerReset
    || manualStartBusy
    || manualStartUncertain
    || profileMutationBusy
    || roleTailoringBusy
    || hasPersistedUnfinishedReview
    || sourceFlow.status === "scanning"
    || sourceFlow.status === "review"
    || sourceFlow.status === "building"
    || (sourceFlow.status === "error" && sourceFlow.scan),
  );

  useEffect(() => {
    if (!manualStartDirty) return;
    const preventAccidentalClose = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", preventAccidentalClose);
    return () => window.removeEventListener("beforeunload", preventAccidentalClose);
  }, [manualStartDirty]);

  const requestActiveView = useCallback((nextView: ActiveView) => {
    if (
      activeAction !== null
      || careerResetBusy
      || confirmingCareerReset
      || roleTailoringBusyRef.current
      || (activeView === "profile" && profileMutationBusyRef.current)
      || manualStartInFlightRef.current
      || manualStartUncertain
      || sourceRecoveryUncertain
    ) return;
    if (nextView === activeView) {
      if (nextView === "profile" && profileSection === "memories" && profileEditorDirty
        && !window.confirm("Return to Profile and discard this unsaved memory draft?")) return;
      if (nextView === "profile") setProfileSection(hasProfile ? "review" : "overview");
      return;
    }
    if (
      activeView === "profile" &&
      manualStartDirty &&
      !window.confirm("Leave Profile and discard the manually entered Basic Details?")
    ) {
      return;
    }
    if (
      activeView === "profile" &&
      !manualStartForm &&
      profileEditorDirty &&
      !window.confirm("Leave Profile and discard unsaved profile changes?")
    ) {
      return;
    }
    if (
      activeView === "roles" &&
      roleEditorDirty &&
      !window.confirm("Leave Roles and discard unsaved editor changes?")
    ) {
      return;
    }
    if (
      activeView === "settings" &&
      modelSettingsDirty &&
      !window.confirm("Leave Models and discard the unsaved configuration or API-key entry?")
    ) {
      return;
    }
    if (activeView === "profile" && manualStartForm) clearManualProfileStart();
    if (nextView === "profile") setProfileSection(hasProfile ? "review" : "overview");
    setActiveView(nextView);
  }, [activeAction, activeView, careerResetBusy, clearManualProfileStart, confirmingCareerReset, hasProfile, manualStartDirty, manualStartForm, manualStartUncertain, modelSettingsDirty, profileEditorDirty, profileSection, roleEditorDirty, sourceRecoveryUncertain]);

  const openMemories = useCallback(() => {
    if (profileMutationBusyRef.current || roleTailoringBusyRef.current || reviewInboxBlocked) return;
    setProfileSection("memories");
  }, [reviewInboxBlocked]);

  const openImportedFiles = useCallback(() => {
    if (manualStartInFlightRef.current || manualStartUncertain || sourceRecoveryUncertain) return;
    if (
      manualStartForm
      && basicDetailsFormHasInput(manualStartForm)
      && !window.confirm("Open Imported files and discard the manually entered Basic Details?")
    ) return;
    if (manualStartForm) clearManualProfileStart();
    setConfirmingSourceReset(false);
    setConfirmingSourceRecovery(false);
    materialsReturnSectionRef.current = profileSection === "review" ? "review" : "overview";
    setProfileSection("materials");
  }, [clearManualProfileStart, manualStartForm, manualStartUncertain, profileSection, sourceRecoveryUncertain]);

  const closeImportedFiles = useCallback(() => {
    if (sourceCorrectionInFlightRef.current || sourceRecoveryUncertain) return;
    setConfirmingSourceReset(false);
    setConfirmingSourceRecovery(false);
    const returnSection = materialsReturnSectionRef.current;
    returnToImportedFilesButtonRef.current = returnSection === "overview";
    setProfileSection(returnSection);
  }, [sourceRecoveryUncertain]);

  const openReviewInbox = useCallback(() => {
    if (
      reviewInboxBlocked
      || profileMutationBusyRef.current
      || roleTailoringBusyRef.current
      || manualStartInFlightRef.current
      || manualStartUncertain
    ) return;
    returnToReviewInboxSourceButtonRef.current = document.activeElement === reviewInboxSourceButtonRef.current;
    reviewInboxReturnSectionRef.current = profileSection === "materials" || profileSection === "memories"
      ? profileSection
      : profileSection === "overview" ? "overview" : "review";
    setProfileSection("inbox");
  }, [manualStartUncertain, profileSection, reviewInboxBlocked]);

  const closeReviewInbox = useCallback(() => {
    setProfileSection(reviewInboxReturnSectionRef.current);
  }, []);

  useEffect(() => {
    const handleShortcut = (event: KeyboardEvent) => {
      if (!event.metaKey && !event.ctrlKey) return;

      if (event.key === "+" || event.key === "=") {
        event.preventDefault();
        setUiZoom((current) => steppedUiZoom(current, 1));
        return;
      }
      if (event.key === "-" || event.key === "_") {
        event.preventDefault();
        setUiZoom((current) => steppedUiZoom(current, -1));
        return;
      }
      if (event.key === "0") {
        event.preventDefault();
        setUiZoom(DEFAULT_UI_ZOOM);
        return;
      }
      if (event.key === ",") {
        event.preventDefault();
        requestActiveView("settings");
        return;
      }

      if (confirmingCareerReset || careerResetBusy) {
        if (event.key === "1" || event.key === "2" || event.key.toLowerCase() === "o") {
          event.preventDefault();
        }
        return;
      }

      const target = event.target as HTMLElement | null;
      const isEditing = Boolean(
        target?.isContentEditable ||
        target?.closest("input, textarea, select, [contenteditable='true']"),
      );
      if (isEditing) return;

      if (event.key === "1" || event.key === "2") {
        event.preventDefault();
        requestActiveView(event.key === "1" ? "profile" : "roles");
        return;
      }

      if (event.key.toLowerCase() === "o" && activeView === "profile") {
        event.preventDefault();
        if (profileSection === "overview" && canImportSources) void scanProfileSources("files");
      }
    };
    window.addEventListener("keydown", handleShortcut);
    return () => window.removeEventListener("keydown", handleShortcut);
  }, [activeView, canImportSources, careerResetBusy, confirmingCareerReset, profileSection, requestActiveView, scanProfileSources]);

  const renderSourceReview = (scan: ProfileSourceScanResult) => {
    const formatEntries = (Object.entries(scan.format_counts) as Array<[SourceFormat, number | undefined]>)
      .filter(([format, count]) => format in SOURCE_FORMAT_LABELS && typeof count === "number" && count > 0);
    const isBuilding = sourceFlow.status === "building";
    const error = sourceFlow.status === "error" ? sourceFlow.message : null;
    const canRetryBuild =
      sourceFlow.status === "error" && sourceFlow.canRetryBuild === true;
    const buildUnavailable = error !== null && !canRetryBuild;
    const correction = sourceRecoveryForm?.scanId === scan.scan_id ? sourceRecoveryForm : null;
    const canRecover = !scan.base_profile && !scan.can_build && scan.file_counts.parsed > 0 && !buildUnavailable;
    const correctionLocked = correction?.request != null;
    const isRecovering = activeAction === "source-correction" || activeAction === "source-resume";

    return (
      <section
        className="source-flow"
        aria-labelledby="source-review-title"
        aria-busy={isBuilding || isRecovering}
        ref={sourcePanelRef}
        tabIndex={-1}
      >
        <div className="source-flow__heading">
          <div>
            <p className="label">Aggregate scan review</p>
            <h4 id="source-review-title">
              {isRecovering
                ? "Confirming the profile and imported files…"
                : correctionLocked
                  ? "Confirm the saved recovery request"
                : isBuilding
                ? scan.base_profile ? "Saving the reviewed import…" : "Creating a profile version…"
                : scan.base_profile ? "Review before saving this import" : "Review before creating a version"}
            </h4>
          </div>
          <span className={`review-badge ${scan.can_build && !buildUnavailable && !correctionLocked ? "review-badge--ready" : ""}`}>
            {buildUnavailable ? "Cannot retry" : correctionLocked ? "Awaiting confirmation" : scan.can_build ? "Ready to build" : "Needs attention"}
          </span>
        </div>

        <p className="source-flow__intro">
          {scan.base_profile
            ? `This source set will update ${scan.base_profile.profile_name}, profile version ${scan.base_profile.version_number}.`
            : "This source set will create the first local profile version."}
          {" "}Only totals and fixed warnings are shown here; source details remain inside the local engine.
        </p>

        {scan.review_candidate_count > 0 && (
          <div className="source-review-candidate-note" role="note">
            <span aria-hidden="true">◇</span>
            <p>
              <strong>
                {counted(scan.review_candidate_count, "suggestion")} will be held for review.
              </strong>
              Proposed Education, Project, and Skill changes stay separate from the saved profile. The review inbox retains the suggestions with their evidence.
            </p>
          </div>
        )}

        {scan.source_review_count > 0 && (
          <div className="source-review-candidate-note" role="note">
            <span aria-hidden="true">◇</span>
            <p><strong>{counted(scan.source_review_count, "source check")} will be available after import.</strong>Compare conflicting Basic Details and review identical documents. Original files stay in your evidence trail.</p>
          </div>
        )}

        <dl className="scan-metrics" aria-label="Source scan totals">
          <div><dt>Discovered</dt><dd>{scan.file_counts.discovered}</dd></div>
          <div><dt>Supported</dt><dd>{scan.file_counts.staged}</dd></div>
          <div><dt>Read successfully</dt><dd>{scan.file_counts.parsed}</dd></div>
          <div><dt>Duplicates</dt><dd>{scan.file_counts.duplicates}</dd></div>
          <div><dt>Skipped</dt><dd>{scan.file_counts.skipped}</dd></div>
          <div><dt>Failed</dt><dd>{scan.file_counts.failed}</dd></div>
        </dl>

        <div className="source-breakdown">
          <div>
            <h5>Recognized sources</h5>
            <dl className="compact-counts">
              {(Object.keys(SOURCE_KIND_LABELS) as SourceKind[]).map((kind) => (
                <div key={kind}>
                  <dt>{SOURCE_KIND_LABELS[kind]}</dt>
                  <dd>{scan.source_counts[kind]}</dd>
                </div>
              ))}
            </dl>
          </div>
          <div>
            <h5>Supported formats</h5>
            {formatEntries.length > 0 ? (
              <ul className="format-chips" aria-label="Parsed file formats">
                {formatEntries.map(([format, count]) => (
                  <li key={format}>{SOURCE_FORMAT_LABELS[format]} <strong>{count}</strong></li>
                ))}
              </ul>
            ) : (
              <p className="empty-breakdown">No supported formats were staged.</p>
            )}
          </div>
        </div>

        <SourceWarnings warnings={scan.warnings} />

        {error && <p className="source-error" role="alert">{error}</p>}
        {!scan.can_build && !error && (
          <p className="source-guidance">{canRecover
            ? "Text was extracted from your files, but CVGnome could not build a complete profile from it. Reading a document does not mean its name, work history, or other fields were recognized. Complete your details below to keep the imported draft and files, then review and edit the profile."
            : "These sources do not yet contain enough recognized profile information to create a version. Review the warnings before choosing another source set."}</p>
        )}

        {correction && <form className="source-recovery-form" onSubmit={(event) => void recoverSourceProfile(event, scan)}>
          <h5>Complete details and keep files</h5>
          <p>Your corrections are entered by you. They do not change the original documents or claim that unrecognized fields were extracted.</p>
          <fieldset disabled={busy || correctionLocked}>
            <label>
              <span>Name <b aria-hidden="true">*</b></span>
              <input ref={sourceRecoveryNameRef} value={correction.name} required maxLength={160}
                autoComplete="name" onChange={(event) => setSourceRecoveryForm((current) => current
                  ? { ...current, name: event.target.value, error: null } : current)} />
            </label>
            <label>
              <span>Summary (optional)</span>
              <textarea value={correction.summary} maxLength={4000} rows={4}
                aria-describedby="source-recovery-summary-help"
                onChange={(event) => setSourceRecoveryForm((current) => current
                  ? { ...current, summary: event.target.value, error: null } : current)} />
            </label>
            <p id="source-recovery-summary-help">Add a summary if needed. Leave this blank to keep any summary already extracted. You can finish other sections in Profile review; export stays unavailable until there is enough resume content.</p>
          </fieldset>
          {correction.error && <p className="source-error" role="alert">{correction.error}</p>}
          {correctionLocked && <p className="source-guidance" role="status">The original recovery request is retained. Confirm its outcome before changing these details or closing the app.</p>}
          <div className="source-flow__actions">
            {!correctionLocked && <button type="button" className="quiet-action" disabled={busy}
              onClick={() => setSourceRecoveryForm(null)}>Cancel corrections</button>}
            {correction.recovery === "conflict"
              ? <button type="button" className="primary-action" disabled={busy} onClick={() => void resumeSourceScan()}>Resume original recovery</button>
              : <button type="submit" className="primary-action" disabled={busy || !correction.name.trim()}>
                {activeAction === "source-correction" ? "Saving profile and files…" : correctionLocked ? "Retry exact request" : "Save profile and keep files"}
              </button>}
          </div>
        </form>}

        <div className="source-flow__actions">
          <button
            className="tertiary-action"
            type="button"
            onClick={() => {
              if (buildUnavailable) {
                closeTerminalSourceReview(scan.scan_id);
              } else {
                void discardSourceScan(scan.scan_id);
              }
            }}
            disabled={busy || correctionLocked}
          >
            {buildUnavailable
              ? "Close review"
              : activeAction === "source-discard"
                ? "Clearing…"
                : "Discard review"}
          </button>
          {canRecover && !correction && <button className="primary-action" type="button" disabled={busy}
            onClick={() => {
              setSourceRecoveryForm({ scanId: scan.scan_id, name: "", summary: "", request: null, recovery: "edit", error: null });
              window.requestAnimationFrame(() => sourceRecoveryNameRef.current?.focus());
            }}>Complete details and keep files</button>}
          {!canRecover && !correction && (!error || canRetryBuild) && (
            <button
              className="primary-action"
              type="button"
              onClick={() => void buildSourceProfile(scan)}
              disabled={!scan.can_build || busy}
              aria-describedby={!scan.can_build ? "source-build-disabled" : undefined}
            >
              {isBuilding
                ? scan.base_profile ? "Saving import…" : "Creating version…"
                : error ? "Try saving again"
                  : scan.base_profile ? "Save reviewed import" : "Create profile version"}
            </button>
          )}
        </div>
        {!scan.can_build && (
          <span id="source-build-disabled" className="sr-only">
            The aggregate scan contains no buildable profile source set.
          </span>
        )}
      </section>
    );
  };

  const renderSourceResult = (result: ProfileSourceBuildResult) => (
    <section
      className="source-flow source-result"
      aria-labelledby="source-result-title"
      ref={sourceResultRef}
      tabIndex={-1}
    >
      <div className="source-flow__heading">
        <div>
          <p className="label">Profile construction result</p>
          <h4 id="source-result-title">
            {result.created
              ? `Profile version ${result.version_number} created`
              : result.review_item_count > 0
                ? `${counted(result.review_item_count, "review suggestion")} ready`
                : "No new version needed"}
          </h4>
        </div>
        <span className="review-badge review-badge--ready">
          {result.renderable ? "Resume ready" : "Profile saved"}
        </span>
      </div>

      <p className="source-flow__intro">
        {result.created
          ? `${result.profile_name} is now the latest immutable local profile.`
          : result.review_item_count > 0
            ? `The reviewed sources did not change saved profile version ${result.version_number}.`
            : `The reviewed sources produce the same canonical profile as version ${result.version_number}.`}
        {" "}{result.source_counts.used} of {result.source_counts.parsed} parsed sources were processed in the synthesis pass.
        {" "}The exact imported files are retained in the local evidence archive.
      </p>

      {result.review_item_count > 0 && (
        <div className="source-review-candidate-note" role="note">
          <span aria-hidden="true">◇</span>
          <p>
            <strong>{counted(result.review_item_count, "suggestion")} entered the local review inbox.</strong>
            None was added to the saved profile. Review each item there; only an explicit Apply creates a new profile version.
          </p>
        </div>
      )}

      {result.source_review_count > 0 && (
        <div className="source-review-candidate-note" role="note">
          <span aria-hidden="true">◇</span>
          <p><strong>{counted(result.source_review_count, "source check")} entered the review inbox.</strong>Compare conflicting details and review identical documents. Queue your choices there, then Apply them together.</p>
        </div>
      )}

      <dl className="scan-metrics profile-result-metrics" aria-label="Constructed profile totals">
        <div><dt>Sources processed</dt><dd>{result.source_counts.used}</dd></div>
        <div><dt>Work entries</dt><dd>{result.profile_counts.work_entries}</dd></div>
        <div><dt>Education</dt><dd>{result.profile_counts.education_entries}</dd></div>
        <div><dt>Skill groups</dt><dd>{result.profile_counts.skill_groups}</dd></div>
        <div><dt>Evidence claims</dt><dd>{result.profile_counts.evidence_claims}</dd></div>
        <div><dt>Preferences</dt><dd>{result.profile_counts.preference_items}</dd></div>
      </dl>

      <SourceWarnings warnings={result.warnings} />
      {!result.renderable && (
        <p className="source-guidance">This incomplete profile and its files are saved. Add or correct resume content in Profile review before exporting.</p>
      )}

      <div className="source-flow__actions">
        {result.review_item_count + result.source_review_count > 0 && (
          <button
            ref={reviewInboxSourceButtonRef}
            className="secondary-action"
            type="button"
            onClick={openReviewInbox}
            disabled={reviewInboxBlocked}
          >
            Open review inbox ({result.review_item_count + result.source_review_count})
          </button>
        )}
        <button
          className="secondary-action"
          type="button"
          onClick={() => setProfileSection("review")}
          disabled={busy}
        >
          Review profile
        </button>
        <button
          className="tertiary-action"
          type="button"
          onClick={closeCommittedSourceResult}
          disabled={busy}
        >
          Close result
        </button>
      </div>
    </section>
  );

  const workspaceNavigationLocked = busy
    || roleTailoringBusy
    || profileMutationBusy
    || careerResetBusy
    || confirmingCareerReset
    || manualStartBusy
    || manualStartUncertain
    || sourceRecoveryUncertain;
  const workspaceNavigationLockMessage = roleTailoringBusy
    ? "Workspace navigation is paused until the model-assisted resume operation finishes."
    : profileMutationBusy
      ? "Workspace navigation is paused until the local profile or review action finishes."
      : careerResetBusy
        ? "Workspace navigation is paused until the career workspace reset finishes."
        : confirmingCareerReset
          ? "Close the reset confirmation before changing workspaces."
          : manualStartBusy
            ? "Workspace navigation is paused until the first profile version finishes saving."
            : manualStartUncertain
              ? committedManualStartReceipt
                ? "Profile version 1 is saved. Finish opening Profile review before changing workspaces."
                : "The manual profile save outcome is unresolved. Reconcile this exact request before changing workspaces."
              : sourceRecoveryUncertain
                ? "The import recovery outcome is unresolved. Retry the exact request before changing workspaces."
              : sourceBusy
                ? "Workspace navigation is paused until the local import operation finishes."
                : busy
                  ? "Workspace navigation is paused until the current local action finishes."
                  : "";

  return (
    <DesktopCloseGuard
      dirty={manualStartDirty || (sourceFlow.status === "review") || (sourceFlow.status === "error" && Boolean(sourceFlow.scan))}
      blocked={busy || sourceBusy || careerResetBusy || manualStartUncertain || sourceRecoveryUncertain
        || (sourceFlow.status === "error" && Boolean(sourceFlow.canRetryBuild))
        || (confirmingCareerReset && careerResetRequestIdRef.current !== null && careerResetError !== null)}
    >
    <main className="app-shell">
      <p className="sr-only" role="status" aria-live="polite" aria-atomic="true">
        {sourceAnnouncement}
      </p>
      <p id="workspace-navigation-lock" className="sr-only" role="status" aria-live="polite" aria-atomic="true">
        {workspaceNavigationLockMessage}
      </p>

      <header className="masthead">
        <CvGnomeMark decorative />
        <h1 className="sr-only">CVGnome</h1>
      </header>

      <nav className="workspace-tabs" aria-label="CVGnome workspace">
        <button
          type="button"
          className={activeView === "profile" ? "workspace-tab workspace-tab--active" : "workspace-tab"}
          onClick={() => requestActiveView("profile")}
          disabled={workspaceNavigationLocked}
          aria-describedby={workspaceNavigationLocked ? "workspace-navigation-lock" : undefined}
          aria-current={activeView === "profile" ? "page" : undefined}
          title="Profile (Ctrl/⌘+1)"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="3.5"/><path d="M5.5 20c.7-4.1 2.9-6.2 6.5-6.2s5.8 2.1 6.5 6.2"/></svg>
          <span>Profile</span>
        </button>
        <button
          type="button"
          className={activeView === "roles" ? "workspace-tab workspace-tab--active" : "workspace-tab"}
          onClick={() => requestActiveView("roles")}
          disabled={workspaceNavigationLocked}
          aria-describedby={workspaceNavigationLocked ? "workspace-navigation-lock" : undefined}
          aria-current={activeView === "roles" ? "page" : undefined}
          title="Roles (Ctrl/⌘+2)"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7.5h16v12H4zM9 7.5v-3h6v3M4 12h16M10 12v2h4v-2"/></svg>
          <span>Roles</span>
        </button>
        <button
          type="button"
          className={`workspace-tab workspace-tab--settings${activeView === "settings" ? " workspace-tab--active" : ""}`}
          onClick={() => requestActiveView("settings")}
          disabled={workspaceNavigationLocked}
          aria-describedby={workspaceNavigationLocked ? "workspace-navigation-lock" : undefined}
          aria-current={activeView === "settings" ? "page" : undefined}
          title="Models — optional, for tailored drafts (Ctrl/⌘+,)"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3A1.7 1.7 0 0 0 10 3V2.8h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z"/></svg>
          <span>Models</span>
        </button>
      </nav>

      {activeView === "profile" && !status ? (
        <section
          className="empty-document empty-document--workspace"
          aria-labelledby="profile-workspace-state-title"
          aria-live="polite"
          aria-busy={workspaceState === "checking"}
        >
          <div className="empty-document__glyph" aria-hidden="true">
            {workspaceState === "checking" ? "…" : "!"}
          </div>
          <h2 id="profile-workspace-state-title">
            {workspaceState === "checking" ? "Opening your local workspace" : "Local workspace needs attention"}
          </h2>
          <p>
            {workspaceState === "checking"
              ? "CVGnome is checking the private vault on this computer."
              : engineError ?? "CVGnome could not confirm the local vault contents."}
          </p>
          {workspaceState === "unavailable" && (
            <button type="button" className="secondary-action" onClick={() => void checkEngine()}>
              Check again
            </button>
          )}
        </section>
      ) : activeView === "profile" && profileSection === "review" ? (
        <ProfileReviewPanel
          reloadKey={`${status?.profile_versions ?? 0}:${status?.latest_profile_name ?? ""}`}
          initialActiveSection={profileReviewSectionRef.current}
          retainedSourceFiles={status?.retained_source_files ?? 0}
          historicalSourceFiles={status?.historical_source_files ?? 0}
          reviewItemCount={reviewItemTotal}
          reviewInboxDisabled={reviewInboxBlocked}
          canExport={canExportSelectedProfile}
          exportBusy={activeAction === "docx" || activeAction === "pdf" ? activeAction : null}
          notice={notice}
          initialProfileVersionId={profileReviewVersionRef.current}
          onBack={() => setProfileSection("overview")}
          onActiveSectionChange={rememberProfileReviewSection}
          onDirtyChange={setProfileEditorDirty}
          onMutationBusyChange={updateProfileMutationBusy}
          onInspectedVersionChange={rememberInspectedProfileVersion}
          onOpenImportedFiles={openImportedFiles}
          onOpenMemories={openMemories}
          onOpenReviewInbox={openReviewInbox}
          onExport={(format, profileVersionId) => void exportResume(format, profileVersionId)}
          onProfileUpdated={handleProfileBasicsUpdated}
          onWorkUpdated={handleProfileWorkUpdated}
          onCollectionUpdated={handleProfileCollectionUpdated}
          onProfileChangesApplied={handleProfileChangesApplied}
          onProfileRestored={handleProfileRestored}
          onProfileRefreshRequested={checkEngine}
        />
      ) : activeView === "profile" && profileSection === "memories" ? (
        <MemoryPanel
          onBack={() => setProfileSection("review")}
          onReviewSuggestions={openReviewInbox}
          onStatusRefresh={refreshMemoryStatus}
          onDirtyChange={setProfileEditorDirty}
          onMutationBusyChange={updateProfileMutationBusy}
        />
      ) : activeView === "profile" && profileSection === "inbox" ? (
        <ReviewWorkspace
          suggestionCounts={reviewInboxCounts}
          sourceCounts={sourceReviewCounts}
          initialTab={reviewInboxReturnSectionRef.current === "memories" ? "suggestions" : undefined}
          backLabel={reviewInboxReturnSectionRef.current === "materials"
            ? "Imported files"
            : reviewInboxReturnSectionRef.current === "memories" ? "Memories"
            : reviewInboxReturnSectionRef.current === "review" ? "Profile review" : "Profile"}
          onBack={closeReviewInbox}
          onStatusRefresh={checkEngine}
          onSuggestionApplied={handleReviewItemApplied}
          onSourceChoicesApplied={handleSourceChoicesApplied}
          onMutationBusyChange={updateProfileMutationBusy}
        />
      ) : activeView === "profile" && profileSection === "overview" ? (
      <section className={`hero${hasProfile ? "" : " hero--new-user"}`}>
        <div className="hero-intro">
          <section className={`workflow-card${hasProfile ? "" : " new-user-workflow"}`} aria-labelledby="workflow-title" aria-busy={busy || sourceBusy}>
            <div className="workflow-card__heading">
              <div>
                <p className="label">{hasProfile ? "Current profile" : "Welcome to CVGnome"}</p>
                <h3
                  id="workflow-title"
                  ref={hasProfile ? undefined : newUserHeadingRef}
                  tabIndex={hasProfile ? undefined : -1}
                >
                  {hasProfile ? status?.latest_profile_name ?? "Profile workspace" : "Start your private career profile"}
                </h3>
              </div>
              <div className="workflow-card__heading-actions">
                {hasProfile && <span className="profile-badge profile-badge--ready">Profile ready</span>}
                {hasProfile && (
                  <button
                    className="profile-review-button"
                    type="button"
                    onClick={() => setProfileSection("review")}
                  >
                    Review profile
                  </button>
                )}
                {(hasProfile || hasPersistedUnfinishedReview || Boolean(status?.retained_source_files)) && (
                  <button
                    ref={importedFilesButtonRef}
                    className={`profile-files-button${hasPersistedUnfinishedReview ? " profile-files-button--attention" : ""}`}
                    type="button"
                    onClick={openImportedFiles}
                    disabled={manualStartBusy || manualStartUncertain}
                  >
                    <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 2.5h7l4 4v11H4z"/><path d="M11 2.5v4h4M7 10h5M7 13h5"/></svg>
                    <span>{hasPersistedUnfinishedReview ? "Review imported files" : "Imported files"}</span>
                    {status && <small>{status.retained_source_files}</small>}
                  </button>
                )}
              </div>
            </div>

            {hasProfile ? (
              <>
                <div className="profile-summary">
                  <span>Latest local profile</span>
                  <strong title={status?.latest_profile_name ?? undefined}>
                    {status?.latest_profile_name ?? "Unnamed profile"}
                  </strong>
                  {status && (
                    <small>
                      {status.profile_versions} saved {status.profile_versions === 1 ? "version" : "versions"}
                    </small>
                  )}
                </div>

                <div className="profile-routes">
                  <section className="route-card route-card--sources" aria-labelledby="sources-route-title">
                    <p className="route-kicker"><span aria-hidden="true">A</span> Your documents</p>
                    <h4 id="sources-route-title">Build a profile from your files</h4>
                    <p id="sources-route-description">
                      Add resumes, work notes, evidence, or preferences. Select PDF, DOCX, JSON,
                      TXT, Markdown, and CSV files individually or together.
                    </p>
                    <div className="source-picker-actions">
                      <button
                        className="primary-action"
                        type="button"
                        ref={sourceFilesButtonRef}
                        onClick={() => void scanProfileSources("files")}
                        disabled={!canImportSources}
                        aria-describedby="sources-route-description"
                        title="Choose files (Ctrl/⌘+O)"
                      >
                        {activeAction === "source-files-scan" ? "Scanning files…" : "Choose source files…"}
                      </button>
                      <button
                        className="secondary-action"
                        type="button"
                        ref={sourceFolderButtonRef}
                        onClick={() => void scanProfileSources("folder")}
                        disabled={!canImportSources}
                        aria-describedby="sources-route-description source-folder-description"
                      >
                        {activeAction === "source-folder-scan" ? "Scanning folder…" : "Import a folder…"}
                      </button>
                    </div>
                    <p id="source-folder-description" className="route-helper">
                      Folder import is the bulk option for a larger source set.
                    </p>
                    {hasPendingSourceReview && <small>A source review is open below.</small>}
                    {hasPersistedUnfinishedReview && <small>Open Imported files above to clear the unfinished review.</small>}
                  </section>

                  <section className="route-card" aria-labelledby="canonical-route-title">
                    <p className="route-kicker"><span aria-hidden="true">B</span> Structured profile</p>
                    <h4 id="canonical-route-title">Import an existing profile JSON</h4>
                    <p id="canonical-route-description">
                      Use this only when you already have a complete CVGnome-compatible canonical profile.
                      Select the JSON document itself, not its containing folder.
                    </p>
                    <button
                      className="secondary-action"
                      type="button"
                      onClick={() => void importProfile()}
                      disabled={!canImportSources}
                      aria-describedby="canonical-route-description"
                    >
                      {activeAction === "canonical-import" ? "Importing…" : "Choose profile JSON…"}
                    </button>
                    {hasPendingSourceReview && <small>Finish or discard the source review first.</small>}
                    {hasPersistedUnfinishedReview && <small>Open Imported files above to resume or clear the unfinished review.</small>}
                  </section>
                </div>
              </>
            ) : (
              <div className="new-user-start">
                <div className="new-user-privacy">
                  <span className="new-user-privacy__mark" aria-hidden="true">✓</span>
                  <div>
                    <strong>Your material stays on this computer.</strong>
                    <p>No account, cloud backend, model, or API key is needed to build and edit your profile.</p>
                  </div>
                </div>

                {!status ? (
                  <section className={`new-user-engine-state${engineError ? " new-user-engine-state--error" : ""}`} aria-live="polite">
                    {checking ? (
                      <>
                        <span className="working-indicator" aria-hidden="true" />
                        <div><strong>Preparing your local workspace…</strong><p>CVGnome is checking its private on-device vault.</p></div>
                      </>
                    ) : (
                      <>
                        <div><strong>The local workspace needs attention</strong><p>{engineError ?? "CVGnome could not open its private local vault."}</p></div>
                        <button type="button" className="secondary-action" onClick={() => void checkEngine()}>Check again</button>
                      </>
                    )}
                  </section>
                ) : hasPersistedUnfinishedReview && !manualStartForm ? (
                  <section className="new-user-recovery" aria-labelledby="new-user-recovery-title">
                    <div>
                      <p className="route-kicker"><span aria-hidden="true">!</span> Continue safely</p>
                      <h4 id="new-user-recovery-title">An import review was left unfinished</h4>
                      <p>Resume the latest available review to keep working with its files, or manage the {counted(status?.source_previews ?? 0, "unfinished review")} before choosing another source set. No profile has been created.</p>
                      {sourceRecoveryError && <p className="source-error" role="alert">{sourceRecoveryError}</p>}
                    </div>
                    <div className="source-picker-actions">
                      <button type="button" className="primary-action" disabled={busy} onClick={() => void resumeSourceScan()}>
                        {activeAction === "source-resume" ? "Resuming…" : "Resume import review"}
                      </button>
                      <button type="button" className="secondary-action" disabled={busy} onClick={openImportedFiles}>Manage unfinished reviews</button>
                    </div>
                  </section>
                ) : manualStartForm ? (
                  <section className="new-user-manual-start" aria-labelledby="manual-profile-title">
                    <header>
                      <p className="route-kicker"><span aria-hidden="true">2</span> Start without documents</p>
                      <h4 id="manual-profile-title">Enter Basic Details</h4>
                      <p>
                        Begin with your identity and contact details, then add Work History, Projects,
                        Education, and Skills from Profile review. Nothing is saved until you create the profile.
                      </p>
                    </header>
                    <form onSubmit={(event) => void submitManualProfileStart(event)}>
                      <fieldset disabled={manualStartBusy || manualStartRecovery === "retry" || manualStartRecovery === "refresh" || committedManualStartReceipt !== null}>
                        <BasicDetailsFields
                          value={manualStartForm}
                          onChange={(field, value) => {
                            setManualStartForm((current) => current ? { ...current, [field]: value } : current);
                            if (manualStartRecovery === "submit") setManualStartError(null);
                          }}
                          nameInputRef={manualStartNameRef}
                          countryCodeHelpId="manual-profile-country-code-help"
                        />
                      </fieldset>

                      {manualStartError && (
                        <div className="profile-review-inline-error new-user-manual-start__error" role="alert">
                          <p>{manualStartError}</p>
                        </div>
                      )}

                      <footer className="profile-basics-editor__actions">
                        <span aria-live="polite">{manualStartFormStatus}</span>
                        <div>
                          {!committedManualStartReceipt && manualStartRecovery !== "retry" && manualStartRecovery !== "refresh" && (
                            <button
                              type="button"
                              className="quiet-action"
                              onClick={cancelManualProfileStart}
                              disabled={manualStartBusy}
                            >
                              Cancel
                            </button>
                          )}
                          <button
                            type="submit"
                            className="primary-action"
                            disabled={
                              manualStartBusy
                              || manualStartRecovery === "blocked"
                              || (
                                !committedManualStartReceipt
                                && manualStartRecovery === "submit"
                                && manualStartValidationError !== null
                              )
                            }
                          >
                            {manualStartActionLabel}
                          </button>
                        </div>
                      </footer>
                    </form>
                  </section>
                ) : sourceFlow.status === "idle" ? (
                  <>
                    <section className="new-user-primary-step" aria-labelledby="sources-route-title">
                      <p className="route-kicker"><span aria-hidden="true">1</span> Start with what you have</p>
                      <h4 id="sources-route-title">Choose your career documents</h4>
                      <p id="sources-route-description">
                        Select one or more resumes, work notes, project records, evidence files, or preferences.
                        CVGnome accepts PDF, DOCX, JSON, TXT, Markdown, and CSV.
                      </p>
                      <div className="source-picker-actions">
                        <button
                          className="primary-action"
                          type="button"
                          ref={sourceFilesButtonRef}
                          onClick={() => void scanProfileSources("files")}
                          disabled={!canImportSources}
                          aria-describedby="sources-route-description new-user-next-step"
                          title="Choose files (Ctrl/⌘+O)"
                        >
                          {activeAction === "source-files-scan" ? "Scanning files…" : "Choose files…"}
                        </button>
                        <button
                          className="secondary-action"
                          type="button"
                          ref={sourceFolderButtonRef}
                          onClick={() => void scanProfileSources("folder")}
                          disabled={!canImportSources}
                          aria-describedby="sources-route-description source-folder-description"
                        >
                          {activeAction === "source-folder-scan" ? "Scanning folder…" : "Choose a folder…"}
                        </button>
                      </div>
                      <p id="source-folder-description" className="route-helper">Use a folder when your material is already collected in one place.</p>
                    </section>

                    <p className="new-user-next" id="new-user-next-step">
                      <span aria-hidden="true">2</span>
                      Next, you’ll review a local scan before CVGnome creates the first saved profile version.
                    </p>

                    <div className="new-user-manual-option">
                      <div>
                        <strong>No documents ready?</strong>
                        <span>Start with Basic Details and add structured sections afterward.</span>
                      </div>
                      <button
                        ref={manualStartButtonRef}
                        className="secondary-action"
                        type="button"
                        onClick={openFreshManualProfile}
                        disabled={!canStartManualProfile}
                      >
                        Start manually
                      </button>
                    </div>

                    <details className="new-user-advanced">
                      <summary>Already have a CVGnome-compatible profile?</summary>
                      <div>
                        <p id="canonical-route-description">Import the canonical JSON document itself, not its containing folder.</p>
                        <button
                          className="secondary-action"
                          type="button"
                          onClick={() => void importProfile()}
                          disabled={!canImportSources}
                          aria-describedby="canonical-route-description"
                        >
                          {activeAction === "canonical-import" ? "Importing…" : "Choose profile JSON…"}
                        </button>
                      </div>
                    </details>
                  </>
                ) : null}
              </div>
            )}

            {sourceFlow.status === "scanning" && (
              <section className="source-flow source-flow--working" aria-labelledby="source-scanning-title" aria-busy="true">
                <span className="working-indicator" aria-hidden="true" />
                <div>
                  <p className="label">Local source scan</p>
                  <h4 id="source-scanning-title">
                    {sourceSelectionMode === "files"
                      ? "Scanning and parsing the selected files…"
                      : "Scanning and parsing the selected folder…"}
                  </h4>
                  <p>Supported files are bounded, deduplicated, and classified locally. No profile version has been created.</p>
                </div>
              </section>
            )}

            {sourceScan && sourceFlow.status !== "complete" && renderSourceReview(sourceScan)}
            {sourceFlow.status === "error" && !sourceFlow.scan && (
              <section
                className="source-flow"
                aria-labelledby="source-error-title"
                ref={sourcePanelRef}
                tabIndex={-1}
              >
                <p className="label">Source scan</p>
                <h4 id="source-error-title">
                  {sourceSelectionMode === "files"
                    ? "The files could not be reviewed"
                    : "The folder could not be reviewed"}
                </h4>
                <p className="source-error" role="alert">{sourceFlow.message}</p>
                <div className="source-flow__actions">
                  <button
                    className="secondary-action"
                    type="button"
                    onClick={() => void scanProfileSources(sourceSelectionMode)}
                    disabled={busy}
                  >
                    {sourceSelectionMode === "files" ? "Choose files again…" : "Choose another folder…"}
                  </button>
                </div>
              </section>
            )}
            {sourceFlow.status === "complete" && renderSourceResult(sourceFlow.result)}

            {hasProfile && <div className="workflow-actions workflow-actions--export">
              <div className="action-copy">
                <strong>Resume output</strong>
                <span>
                  {latestRenderable === false
                    ? "The latest profile does not yet contain enough resume content."
                    : hasProfile
                      ? "Choose an editable document or a ready-to-share PDF."
                      : "Resume export unlocks after a profile version is created."}
                </span>
              </div>
              <div className="export-buttons" aria-label="Resume export formats">
                <button
                  className="secondary-action"
                  type="button"
                  onClick={() => void exportResume("docx")}
                  disabled={!canExport}
                >
                  {activeAction === "docx" ? "Creating…" : "Export DOCX"}
                </button>
                <button
                  className="secondary-action"
                  type="button"
                  onClick={() => void exportResume("pdf")}
                  disabled={!canExport}
                >
                  {activeAction === "pdf" ? "Creating…" : "Export PDF"}
                </button>
              </div>
            </div>}

            {notice && (
              <p
                className={`action-notice action-notice--${notice.kind}`}
                role={notice.kind === "error" ? "alert" : "status"}
                aria-live={notice.kind === "error" ? "assertive" : "polite"}
              >
                {notice.message}
              </p>
            )}
          </section>
        </div>

        <div className="hero-sidebar">
        <aside className={`engine-card ${engineError ? "engine-card--error" : ""}`}>
          <div className="engine-card__heading">
            <div>
              <p className="label">Local engine</p>
              <p className="engine-state">
                <span className={`status-dot ${status ? "status-dot--ready" : ""}`} aria-hidden="true" />
                {checking ? "Checking…" : status ? "Ready" : "Needs attention"}
              </p>
            </div>
            <button type="button" onClick={() => void checkEngine()} disabled={checking || busy}>
              {checking ? "Working" : "Check again"}
            </button>
          </div>

          {status ? (
            <div className="engine-details">
              <div><span>Engine</span><strong>v{status.engine_version}</strong></div>
              <div><span>Vault schema</span><strong>v{status.schema_version}</strong></div>
              <div><span>Profile versions</span><strong>{status.profile_versions}</strong></div>
              <div><span>Artifacts</span><strong>{status.artifacts}</strong></div>
              <p className="engine-footnote">Vault storage is private to this app on your computer.</p>
            </div>
          ) : (
            <p className="engine-error">
              {engineError ?? "The local engine has not reported its status yet."}
            </p>
          )}
        </aside>

        </div>
      </section>
      ) : activeView === "profile" && profileSection === "materials" ? (
        <section className="materials-workspace" aria-labelledby="materials-view-title">
          <header className="materials-commandbar">
            <div className="materials-commandbar__title">
              <button type="button" className="profile-back-button" onClick={closeImportedFiles}>
                <span aria-hidden="true">←</span> Profile
              </button>
              <div>
                <p className="section-kicker">Local evidence archive</p>
                <h2 id="materials-view-title" ref={importedFilesHeadingRef} tabIndex={-1}>Imported files</h2>
                <span>
                  {status
                    ? `Active set ${status.source_retention_generation} · ${status.retained_source_files} files · ${formatBytes(status.retained_source_bytes)}`
                    : "Local engine unavailable"}
                </span>
              </div>
            </div>
            <div className="commandbar-actions" aria-label="Import files">
              <button
                type="button"
                className="primary-action"
                ref={sourceFilesButtonRef}
                onClick={() => void scanProfileSources("files")}
                disabled={!canImportSources}
                title="Choose files (Ctrl/⌘+O)"
              >
                {activeAction === "source-files-scan" ? "Scanning…" : "Import files…"}
              </button>
              <button
                type="button"
                className="secondary-action"
                ref={sourceFolderButtonRef}
                onClick={() => void scanProfileSources("folder")}
                disabled={!canImportSources}
              >
                {activeAction === "source-folder-scan" ? "Scanning…" : "Import folder…"}
              </button>
              <button
                type="button"
                className="secondary-action canonical-import-action"
                onClick={() => void importProfile()}
                disabled={!canImportSources}
              >
                {activeAction === "canonical-import" ? "Importing…" : "Profile JSON…"}
              </button>
              <button
                type="button"
                className="quiet-action"
                onClick={() => {
                  setConfirmingSourceReset(true);
                  setSourceResetError(null);
                }}
                disabled={
                  !status || checking || busy || sourceReviewBlocksReset ||
                  (status.retained_source_files === 0 && status.retained_source_imports === 0 && status.memory_count === 0)
                }
                aria-expanded={confirmingSourceReset}
                aria-controls="materials-reset-confirmation"
              >
                Start new import set…
              </button>
            </div>
          </header>

          {notice && (
            <p
              className={`workspace-notice action-notice--${notice.kind}`}
              role={notice.kind === "error" ? "alert" : "status"}
            >
              {notice.message}
            </p>
          )}

          {hasPersistedUnfinishedReview && status && (
            <section className="inline-recovery" aria-labelledby="materials-recovery-title">
              <div>
                <strong id="materials-recovery-title">Unfinished import review</strong>
                <span>{counted(status.source_previews, "temporary review")} {hasProfile
                  ? "must be cleared before another import."
                  : "can be resumed where available, or cleared before another import."}</span>
              </div>
              {!confirmingSourceRecovery ? (
                <div className="source-picker-actions">
                  {!hasProfile && <button type="button" className="primary-action" disabled={busy} onClick={() => void resumeSourceScan()}>
                    {activeAction === "source-resume" ? "Resuming…" : "Resume import review"}
                  </button>}
                  <button
                    type="button"
                    className="secondary-action"
                    onClick={() => setConfirmingSourceRecovery(true)}
                    disabled={busy}
                  >
                    Clear review…
                  </button>
                </div>
              ) : (
                <div className="inline-confirmation">
                  <span>Only uncommitted temporary data is removed.</span>
                  <button type="button" className="quiet-action" onClick={() => setConfirmingSourceRecovery(false)} disabled={busy}>Cancel</button>
                  <button type="button" className="secondary-action" onClick={() => void clearUnfinishedSourceReviews()} disabled={busy}>
                    {activeAction === "source-recovery" ? "Clearing…" : "Clear unfinished review"}
                  </button>
                </div>
              )}
              {sourceRecoveryError && <p className="source-error" role="alert">{sourceRecoveryError}</p>}
            </section>
          )}

          {confirmingSourceReset && status && (
            <section className="inline-recovery" id="materials-reset-confirmation" aria-labelledby="materials-reset-title">
              <div>
                <strong id="materials-reset-title">Start a new active import set?</strong>
                <span>
                  Existing files and saved memories remain retained in history.
                  {status.review_inbox_items + status.review_deferred_items + status.source_review_inbox_items + status.source_review_deferred_items > 0
                    ? ` ${counted(status.review_inbox_items + status.review_deferred_items + status.source_review_inbox_items + status.source_review_deferred_items, "Inbox or Deferred item")} will become read-only History.`
                    : " Any Inbox or Deferred items would become read-only History."}
                  {" "}The current profile does not change.
                </span>
              </div>
              <div className="inline-confirmation">
                <button type="button" className="quiet-action" onClick={() => setConfirmingSourceReset(false)} disabled={busy}>Cancel</button>
                <button type="button" className="secondary-action" onClick={() => void resetActiveSourceImports()} disabled={busy || sourceReviewBlocksReset}>
                  {activeAction === "source-reset" ? "Starting…" : "Start new set"}
                </button>
              </div>
              {sourceResetError && <p className="source-error" role="alert">{sourceResetError}</p>}
            </section>
          )}

          {sourceFlow.status === "scanning" && (
            <section className="source-flow source-flow--working workflow-drawer" aria-labelledby="materials-scanning-title" aria-busy="true">
              <span className="working-indicator" aria-hidden="true" />
              <div><p className="label">Local source scan</p><h4 id="materials-scanning-title">Scanning and parsing selected {sourceSelectionMode === "files" ? "files" : "folder"}…</h4></div>
            </section>
          )}
          {sourceScan && sourceFlow.status !== "complete" && <div className="workflow-drawer">{renderSourceReview(sourceScan)}</div>}
          {sourceFlow.status === "error" && !sourceFlow.scan && (
            <section className="source-flow workflow-drawer" ref={sourcePanelRef} tabIndex={-1}>
              <p className="label">Source scan</p>
              <h4>The selection could not be reviewed</h4>
              <p className="source-error" role="alert">{sourceFlow.message}</p>
              <div className="source-flow__actions"><button type="button" className="secondary-action" onClick={() => void scanProfileSources(sourceSelectionMode)} disabled={busy}>Choose again…</button></div>
            </section>
          )}
          {sourceFlow.status === "complete" && <div className="workflow-drawer">{renderSourceResult(sourceFlow.result)}</div>}

          <SourcesPanel
            reloadKey={`${status?.source_retention_generation ?? 0}:${status?.retained_source_imports ?? 0}:${status?.historical_source_imports ?? 0}:${sourceFlow.status}`}
          />
        </section>
      ) : activeView === "roles" ? (
        <OpportunitiesPanel
          hasProfile={hasProfile}
          workspaceState={workspaceState}
          currentProfileVersion={status?.profile_versions ?? null}
          initialOpportunityId={selectedOpportunityId}
          onCountChange={updateOpportunityCount}
          onDirtyChange={setRoleEditorDirty}
          onSelectedOpportunityChange={setSelectedOpportunityId}
          onOpenProfile={() => requestActiveView("profile")}
          onOpenModelSettings={() => requestActiveView("settings")}
          onEngineStatusRefresh={checkEngine}
          onTailoringBusyChange={updateRoleTailoringBusy}
        />
      ) : (
        <ProviderSettingsPanel
          onDirtyChange={setModelSettingsDirty}
          careerWorkspaceHasData={careerWorkspaceHasData}
          careerWorkspaceResetDisabled={
            !status
            || checking
            || careerResetBusy
            || busy
            || sourceFlow.status === "scanning"
            || sourceFlow.status === "review"
            || sourceFlow.status === "building"
            || (sourceFlow.status === "error" && Boolean(sourceFlow.scan))
            || status.source_previews > 0
            || profileMutationBusy
            || roleTailoringBusy
          }
          onRequestCareerWorkspaceReset={openCareerWorkspaceReset}
        />
      )}

      {confirmingCareerReset && status && (
        <div className="modal-backdrop" role="presentation">
          <section
            className="confirmation-dialog"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="career-reset-title"
            aria-describedby="career-reset-description career-reset-preserved"
            aria-busy={careerResetBusy}
            ref={careerResetDialogRef}
            tabIndex={-1}
          >
            <div className="confirmation-dialog__heading">
              <p className="section-kicker">Data & privacy</p>
              <h2 id="career-reset-title">Reset career workspace?</h2>
            </div>
            <p id="career-reset-description">
              This permanently removes CVGnome’s managed profile history and evidence lineage,
              retained imported copies, saved memories, review items and their decision history, roles and tracker notes,
              tailored drafts, and managed resume artifacts.
            </p>
            <dl className="reset-summary" aria-label="Local career data selected for reset">
              <div><dt>Profile versions</dt><dd>{status.profile_versions}</dd></div>
              <div><dt>Imported sets</dt><dd>{status.source_imports}</dd></div>
              <div><dt>Review items</dt><dd>{reviewItemTotal}</dd></div>
              <div><dt>Saved memories</dt><dd>{status.memory_count}</dd></div>
              <div><dt>Saved roles</dt><dd>{status.opportunities}</dd></div>
              <div><dt>Managed artifacts</dt><dd>{status.artifacts}</dd></div>
            </dl>
            <p className="confirmation-dialog__preserved" id="career-reset-preserved">
              CVGnome keeps your model configuration, OS-stored API key, and interface zoom.
              Original files and resume copies saved outside CVGnome are not touched. This is a product reset,
              not guaranteed forensic erasure from SSD snapshots or backups.
            </p>
            <label className="field confirmation-dialog__field">
              <span>Type <strong>RESET</strong> to continue</span>
              <input
                value={careerResetConfirmation}
                onChange={(event) => setCareerResetConfirmation(event.target.value)}
                disabled={careerResetBusy}
                autoComplete="off"
                autoCapitalize="characters"
                spellCheck={false}
              />
            </label>
            {careerResetError && <p className="settings-message settings-message--error" role="alert">{careerResetError}</p>}
            <div className="confirmation-dialog__actions">
              <button
                type="button"
                className="secondary-action"
                onClick={closeCareerWorkspaceReset}
                disabled={careerResetBusy}
                ref={careerResetCancelRef}
              >
                Cancel
              </button>
              <button
                type="button"
                className="danger-action"
                onClick={() => void resetCareerWorkspace()}
                disabled={
                  careerResetBusy || careerResetConfirmation !== "RESET" ||
                  (careerResetError !== null && !careerResetRetryable)
                }
              >
                {careerResetBusy
                  ? "Resetting…"
                  : careerResetError && careerResetRetryable
                    ? "Verify or retry reset"
                    : "Reset career workspace"}
              </button>
            </div>
          </section>
        </div>
      )}

      <footer className="status-bar" aria-label="Workspace status">
        <span><span className={`status-dot ${status ? "status-dot--ready" : ""}`} aria-hidden="true" />{checking ? "Checking local engine" : status ? "Local engine ready" : "Local engine unavailable"}</span>
        <span>{status ? `Vault schema ${status.schema_version} · Engine ${status.engine_version}` : "All work remains on this computer"}</span>
        <span title="Interface zoom · Ctrl/⌘ +/− · Ctrl/⌘ 0 to reset">{uiZoomPercent(uiZoom)}</span>
        <span>{roleTailoringBusy ? "Model-assisted resume operation running" : activeView === "profile" ? !status ? workspaceState === "checking" ? "Profile · Opening" : "Profile · Unavailable" : profileSection === "materials" ? "Profile · Imported files" : profileSection === "memories" ? "Profile · Memories" : profileSection === "inbox" ? "Profile · Review inbox" : profileSection === "review" ? "Profile · Review" : hasProfile ? "Profile · Update" : "Profile · Start" : activeView === "roles" ? "Ctrl/⌘+2 Roles" : "Ctrl/⌘+, Models · Optional"}</span>
      </footer>
    </main>
    </DesktopCloseGuard>
  );
}

export default App;
