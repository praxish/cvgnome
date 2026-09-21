// SPDX-License-Identifier: MPL-2.0
mod local_provider;
mod memory;
mod provider_spend;
mod source_import;
mod source_review;

use crate::local_provider::{
    LocalProviderRuntime, delete_openai_api_key, discover_model_provider_models,
    generate_tailored_resume, get_latest_tailored_resume, get_local_provider_settings,
    save_local_provider_settings, save_openai_api_key, select_model_provider,
    test_local_provider_models,
};
use crate::memory::{get_memory, list_memories, queue_memory_project, save_memory};
use crate::provider_spend::{get_provider_spend_control, save_provider_spend_control};
use crate::source_review::{apply_source_review_decisions, list_source_review_items};

use crate::source_import::{
    StagedCanonicalProfile, StagedSourceFolder, cleanup_staging, stage_canonical_profile,
    stage_source_files, stage_source_folder,
};
use serde::de::DeserializeOwned;
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::fs::{self, File};
use std::io::{Read, Write};
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};
use std::path::{Component, Path, PathBuf};
use std::time::{Duration, Instant};
use tauri::Manager;
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_shell::ShellExt;
use tauri_plugin_shell::process::CommandEvent;
use tempfile::NamedTempFile;
use tokio::sync::Mutex;
use url::Url;
use uuid::Uuid;

const ENGINE_PROTOCOL_VERSION: u32 = 1;
const ENGINE_SCHEMA_VERSION: u32 = 19;
const ENGINE_REQUEST_LIMIT_BYTES: usize = 9 * 1024 * 1024;
const ENGINE_OUTPUT_LIMIT_BYTES: usize = 192 * 1024;
const MANAGED_ARTIFACT_LIMIT_BYTES: u64 = 20 * 1024 * 1024;
const ENGINE_STATUS_TIMEOUT: Duration = Duration::from_secs(15);
const ENGINE_IMPORT_TIMEOUT: Duration = Duration::from_secs(15);
const ENGINE_DOCX_TIMEOUT: Duration = Duration::from_secs(30);
const ENGINE_PDF_TIMEOUT: Duration = Duration::from_secs(60);
const ENGINE_SOURCE_SCAN_TIMEOUT: Duration = Duration::from_secs(120);
const ENGINE_SOURCE_BUILD_TIMEOUT: Duration = Duration::from_secs(60);
const ENGINE_SOURCE_RESET_TIMEOUT: Duration = Duration::from_secs(15);
const ENGINE_WORKSPACE_RESET_TIMEOUT: Duration = Duration::from_secs(300);
const ENGINE_SOURCE_LIST_TIMEOUT: Duration = Duration::from_secs(15);
const ENGINE_PROFILE_REVIEW_TIMEOUT: Duration = Duration::from_secs(15);
const ENGINE_PROFILE_EDIT_TIMEOUT: Duration = Duration::from_secs(30);
const ENGINE_OPPORTUNITY_TIMEOUT: Duration = Duration::from_secs(30);
const ENGINE_STATUS_CACHE_TTL: Duration = Duration::from_millis(750);
const OPPORTUNITY_TITLE_LIMIT_CHARS: usize = 240;
const OPPORTUNITY_DESCRIPTION_LIMIT_CHARS: usize = 32_000;
const OPPORTUNITY_SHORT_FIELD_LIMIT_CHARS: usize = 160;
const OPPORTUNITY_URL_LIMIT_CHARS: usize = 2_048;
const OPPORTUNITY_QUERY_LIMIT_CHARS: usize = 200;
const OPPORTUNITY_NOTES_LIMIT_CHARS: usize = 4_000;
const OPPORTUNITY_LIST_LIMIT: usize = 20;
const OPPORTUNITY_LIST_OFFSET_LIMIT: usize = 10_000;
const OPPORTUNITY_MATCHED_TERMS_LIMIT: usize = 24;
const OPPORTUNITY_REVIEW_TERMS_LIMIT: usize = 16;
const OPPORTUNITY_EVIDENCE_LIMIT: usize = 12;
const OPPORTUNITY_TERM_LIMIT_CHARS: usize = 64;
const OPPORTUNITY_PROFILE_PATH_LIMIT_CHARS: usize = 256;
const OPPORTUNITY_EVIDENCE_SNIPPET_LIMIT_CHARS: usize = 240;
const PROFILE_NAME_LIMIT_CHARS: usize = 160;
const PROFILE_REVIEW_PAGE_LIMIT: usize = 10;
const PROFILE_REVIEW_OFFSET_LIMIT: usize = 10_000;
const PROFILE_REVIEW_SECTION_COUNT: usize = 11;
const PROFILE_REVIEW_SECTION_ITEM_LIMIT: u64 = 5_000;
const PROFILE_REVIEW_CONTACT_LIMIT: usize = 12;
const PROFILE_REVIEW_HIGHLIGHT_LIMIT: usize = 8;
const PROFILE_REVIEW_TAG_LIMIT: usize = 18;
const PROFILE_REVIEW_DETAIL_LIMIT: usize = 8;
const PROFILE_REVIEW_SUMMARY_LIMIT_BYTES: usize = 32 * 1024;
const PROFILE_REVIEW_PAGE_LIMIT_BYTES: usize = 128 * 1024;
const PROFILE_REVIEW_INBOX_PAGE_LIMIT: usize = 10;
const PROFILE_REVIEW_INBOX_OFFSET_LIMIT: usize = 10_000;
const PROFILE_REVIEW_INBOX_RESPONSE_LIMIT_BYTES: usize = 112 * 1024;
const PROFILE_REVIEW_INBOX_TRANSITION_LIMIT_BYTES: usize = 8 * 1024;
const PROFILE_REVIEW_INBOX_APPLY_REQUEST_LIMIT_BYTES: usize = 48 * 1024;
const PROFILE_REVIEW_INBOX_APPLY_RECEIPT_LIMIT_BYTES: usize = 8 * 1024;
const PROFILE_REVIEW_INBOX_EVIDENCE_LIMIT: usize = 3;
const PROFILE_REVIEW_INBOX_CHANGED_FIELD_LIMIT: usize = 8;
const PROFILE_REVIEW_INBOX_DETAIL_LABEL_LIMIT_CHARS: usize = 32;
const PROFILE_REVIEW_INBOX_EVIDENCE_LOCATION_LIMIT_CHARS: usize = 80;
const PROFILE_REVIEW_INBOX_EVIDENCE_EXCERPT_LIMIT_CHARS: usize = 360;
const PROFILE_SOURCE_REVIEW_CANDIDATE_LIMIT: u64 = 240;
const PROFILE_VERSION_LIST_LIMIT: usize = 20;
const PROFILE_VERSION_LIST_OFFSET_LIMIT: usize = 10_000;
const PROFILE_VERSION_LIST_LIMIT_BYTES: usize = 64 * 1024;
const PROFILE_RESTORE_RECEIPT_LIMIT_BYTES: usize = 4 * 1024;
const PROFILE_DIFF_BASIC_CHANGE_LIMIT: usize = 9;
const PROFILE_DIFF_SECTION_CHANGE_LIMIT: usize = 11;
const PROFILE_DIFF_LIMIT_BYTES: usize = 64 * 1024;
const PROFILE_REVIEW_NAME_LIMIT_CHARS: usize = 160;
const PROFILE_REVIEW_HEADLINE_LIMIT_CHARS: usize = 200;
const PROFILE_REVIEW_SUMMARY_TEXT_LIMIT_CHARS: usize = 700;
const PROFILE_REVIEW_CONTACT_LABEL_LIMIT_CHARS: usize = 80;
const PROFILE_REVIEW_CONTACT_VALUE_LIMIT_CHARS: usize = 500;
const PROFILE_REVIEW_ITEM_TITLE_LIMIT_CHARS: usize = 240;
const PROFILE_REVIEW_ITEM_SUBTITLE_LIMIT_CHARS: usize = 240;
const PROFILE_REVIEW_ITEM_DATE_LIMIT_CHARS: usize = 80;
const PROFILE_REVIEW_ITEM_LOCATION_LIMIT_CHARS: usize = 200;
const PROFILE_REVIEW_HIGHLIGHT_LIMIT_CHARS: usize = 360;
const PROFILE_REVIEW_TAG_LIMIT_CHARS: usize = 100;
const PROFILE_REVIEW_DETAIL_LABEL_LIMIT_CHARS: usize = 80;
const PROFILE_REVIEW_DETAIL_VALUE_LIMIT_CHARS: usize = 240;
const PROFILE_BASICS_SUMMARY_LIMIT_CHARS: usize = 4_000;
const PROFILE_BASICS_EMAIL_LIMIT_CHARS: usize = 320;
const PROFILE_BASICS_PHONE_LIMIT_CHARS: usize = 80;
const PROFILE_BASICS_URL_LIMIT_CHARS: usize = 2_048;
const PROFILE_BASICS_LOCATION_LIMIT_CHARS: usize = 160;
const PROFILE_BASICS_COUNTRY_CODE_LIMIT_CHARS: usize = 2;
const PROFILE_WORK_PAGE_LIMIT: usize = 20;
const PROFILE_WORK_OFFSET_LIMIT: usize = 10_000;
const PROFILE_WORK_ENTRY_LIMIT: u64 = 5_000;
const PROFILE_WORK_NAME_LIMIT_CHARS: usize = 240;
const PROFILE_WORK_POSITION_LIMIT_CHARS: usize = 240;
const PROFILE_WORK_DATE_LIMIT_CHARS: usize = 80;
const PROFILE_WORK_SUMMARY_LIMIT_CHARS: usize = 1_000;
const PROFILE_WORK_LOCATION_LIMIT_CHARS: usize = 200;
const PROFILE_WORK_URL_LIMIT_CHARS: usize = 2_048;
const PROFILE_WORK_HIGHLIGHT_LIMIT: usize = 8;
const PROFILE_WORK_HIGHLIGHT_LIMIT_CHARS: usize = 360;
const PROFILE_WORK_HIGHLIGHT_TOTAL_LIMIT_CHARS: usize = 1_800;
const PROFILE_WORK_PAGE_LIMIT_BYTES: usize = 128 * 1024;
const PROFILE_SECTION_PAGE_LIMIT: usize = 20;
const PROFILE_SECTION_OFFSET_LIMIT: usize = 10_000;
const PROFILE_SECTION_ENTRY_LIMIT: u64 = 5_000;
const PROFILE_SECTION_PAGE_LIMIT_BYTES: usize = 128 * 1024;
const PROFILE_SECTION_RECEIPT_LIMIT_BYTES: usize = 4 * 1024;
const PROFILE_PROJECT_NAME_LIMIT_CHARS: usize = 200;
const PROFILE_PROJECT_DESCRIPTION_LIMIT_CHARS: usize = 600;
const PROFILE_PROJECT_URL_LIMIT_CHARS: usize = 2_048;
const PROFILE_PROJECT_DATE_LIMIT_CHARS: usize = 80;
const PROFILE_PROJECT_HIGHLIGHT_LIMIT: usize = 8;
const PROFILE_PROJECT_HIGHLIGHT_LIMIT_CHARS: usize = 360;
const PROFILE_PROJECT_HIGHLIGHT_TOTAL_LIMIT_CHARS: usize = 1_800;
const PROFILE_PROJECT_KEYWORD_LIMIT: usize = 16;
const PROFILE_PROJECT_KEYWORD_LIMIT_CHARS: usize = 100;
const PROFILE_PROJECT_KEYWORD_TOTAL_LIMIT_CHARS: usize = 1_200;
const PROFILE_EDUCATION_INSTITUTION_LIMIT_CHARS: usize = 240;
const PROFILE_EDUCATION_STUDY_TYPE_LIMIT_CHARS: usize = 160;
const PROFILE_EDUCATION_AREA_LIMIT_CHARS: usize = 200;
const PROFILE_EDUCATION_URL_LIMIT_CHARS: usize = 2_048;
const PROFILE_EDUCATION_DATE_LIMIT_CHARS: usize = 80;
const PROFILE_EDUCATION_SCORE_LIMIT_CHARS: usize = 80;
const PROFILE_EDUCATION_COURSE_LIMIT: usize = 12;
const PROFILE_EDUCATION_COURSE_LIMIT_CHARS: usize = 160;
const PROFILE_EDUCATION_COURSE_TOTAL_LIMIT_CHARS: usize = 1_200;
const PROFILE_SKILL_NAME_LIMIT_CHARS: usize = 120;
const PROFILE_SKILL_LEVEL_LIMIT_CHARS: usize = 80;
const PROFILE_SKILL_KEYWORD_LIMIT: usize = 18;
const PROFILE_SKILL_KEYWORD_LIMIT_CHARS: usize = 80;
const PROFILE_SKILL_KEYWORD_TOTAL_LIMIT_CHARS: usize = 1_200;
const PROFILE_CHANGE_OPERATION_LIMIT: usize = 64;
const PROFILE_CHANGE_SUGGESTION_LIMIT: usize = 64;
const PROFILE_CHANGE_REQUEST_LIMIT_BYTES: usize = 256 * 1024;
const PROFILE_CHANGE_PREVIEW_LIMIT_BYTES: usize = 128 * 1024;
const PROFILE_CHANGE_RECEIPT_LIMIT_BYTES: usize = 8 * 1024;
const MANUAL_PROFILE_START_REQUEST_LIMIT_BYTES: usize = 64 * 1024;
const MANUAL_PROFILE_START_RECEIPT_LIMIT_BYTES: usize = 4 * 1024;
const RETAINED_SOURCE_DISPLAY_NAME_LIMIT_CHARS: usize = 240;
const RETAINED_SOURCE_DISPLAY_NAME_LIMIT_BYTES: usize = 960;
const RETAINED_SOURCE_ISSUE_CODE_LIMIT_CHARS: usize = 80;
const RETAINED_SOURCE_LIST_LIMIT: usize = 25;
const RETAINED_SOURCE_LIST_OFFSET_LIMIT: usize = 100_000;
const PROFILE_COMPACTION_REPORT_LIMIT_BYTES: usize = 32 * 1024;
const PROFILE_COMPACTION_REPORT_MAX_DEPTH: usize = 8;
const PROFILE_COMPACTION_REPORT_MAX_NODES: usize = 512;
const PROFILE_COMPACTION_REPORT_MAX_KEY_CHARS: usize = 96;
const PROFILE_COMPACTION_REPORT_MAX_STRING_CHARS: usize = 1_024;
const TAILORED_REVISION_LIST_LIMIT: usize = 50;
const TAILORED_REVISION_CHANGED_FIELDS_LIMIT: usize = 66;
const TAILORED_REVISION_LABEL_LIMIT_BYTES: usize = 200;
const TAILORED_REVISION_BASICS_SUMMARY_LIMIT_BYTES: usize = 700;
const TAILORED_REVISION_WORK_SUMMARY_LIMIT_BYTES: usize = 420;
const TAILORED_REVISION_WORK_EDITS_LIMIT: usize = 32;
const TAILORED_REVISION_HIGHLIGHTS_LIMIT: usize = 8;
const TAILORED_REVISION_HIGHLIGHT_LIMIT_BYTES: usize = 360;
const TAILORED_REVISION_EDITS_LIMIT_BYTES: usize = 128 * 1024;
const TAILORED_REVISION_RESUME_LIMIT_BYTES: usize = 56 * 1024;
const TAILORED_REVISION_RESUME_MAX_DEPTH: usize = 20;
const TAILORED_REVISION_RESUME_MAX_NODES: usize = 8_000;
const TAILORED_REVISION_RESUME_MAX_STRING_BYTES: usize = 16 * 1024;
const TAILORED_REVISION_CHANGED_FIELD_LIMIT_BYTES: usize = 512;
const ENGINE_TAILORED_REVISION_TIMEOUT: Duration = Duration::from_secs(30);
const JAVASCRIPT_MAX_SAFE_INTEGER: u64 = 9_007_199_254_740_991;

#[derive(Clone, Debug, Serialize)]
struct EngineStatus {
    engine_version: String,
    schema_version: u32,
    profile_versions: u64,
    opportunities: u64,
    artifacts: u64,
    pending_jobs: u64,
    source_snapshots: u64,
    source_imports: u64,
    source_previews: u64,
    review_inbox_items: u64,
    review_deferred_items: u64,
    review_history_items: u64,
    source_review_inbox_items: u64,
    source_review_deferred_items: u64,
    source_review_history_items: u64,
    memory_count: u64,
    source_retention_generation: u64,
    retained_source_files: u64,
    retained_source_bytes: u64,
    retained_source_imports: u64,
    historical_source_files: u64,
    historical_source_imports: u64,
    last_source_import_at_ms: Option<u64>,
    source_retention_reset_at_ms: Option<u64>,
    latest_profile_name: Option<String>,
    latest_profile_renderable: Option<bool>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct EngineStatusWire {
    engine_version: String,
    schema_version: u32,
    database_path: String,
    profile_versions: u64,
    opportunities: u64,
    artifacts: u64,
    pending_jobs: u64,
    source_snapshots: u64,
    source_imports: u64,
    source_previews: u64,
    review_inbox_items: u64,
    review_deferred_items: u64,
    review_history_items: u64,
    source_review_inbox_items: u64,
    source_review_deferred_items: u64,
    source_review_history_items: u64,
    memory_count: u64,
    source_retention_generation: u64,
    retained_source_files: u64,
    retained_source_bytes: u64,
    retained_source_imports: u64,
    historical_source_files: u64,
    historical_source_imports: u64,
    last_source_import_at_ms: Option<u64>,
    source_retention_reset_at_ms: Option<u64>,
    latest_profile_name: Option<String>,
    latest_profile_renderable: Option<bool>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct CareerWorkspaceResetExpected {
    profile_versions: u64,
    opportunities: u64,
    artifacts: u64,
    source_imports: u64,
    source_previews: u64,
    source_retention_generation: u64,
    review_inbox_items: u64,
    review_deferred_items: u64,
    review_history_items: u64,
    source_review_inbox_items: u64,
    source_review_deferred_items: u64,
    source_review_history_items: u64,
    memory_count: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct CareerWorkspaceResetReceipt {
    request_id: String,
    reset_at_ms: u64,
    schema_version: u32,
    source_retention_generation: u64,
    profile_versions: u64,
    opportunities: u64,
    artifacts: u64,
    source_imports: u64,
    source_previews: u64,
    review_inbox_items: u64,
    review_deferred_items: u64,
    review_history_items: u64,
    source_review_inbox_items: u64,
    source_review_deferred_items: u64,
    source_review_history_items: u64,
    memory_count: u64,
    created: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct RetainedSourceStatus {
    source_retention_generation: u64,
    retained_source_files: u64,
    retained_source_bytes: u64,
    retained_source_imports: u64,
    historical_source_files: u64,
    historical_source_imports: u64,
    last_source_import_at_ms: Option<u64>,
    source_retention_reset_at_ms: Option<u64>,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum RetainedSourceScope {
    Active,
    Historical,
    All,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
enum RetainedSourceFormat {
    Pdf,
    Docx,
    Txt,
    Md,
    Json,
    Csv,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
enum RetainedSourceKind {
    Resume,
    Evidence,
    Preferences,
    Unclassified,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
enum RetainedSourceExtractionStatus {
    Parsed,
    Failed,
    Duplicate,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
enum RetainedSourceRetentionStatus {
    Active,
    Historical,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum RetainedSourceImportMode {
    Canonical,
    SourceSet,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct RetainedSourceListItem {
    import_id: String,
    ordinal: u64,
    display_name: String,
    source_format: RetainedSourceFormat,
    source_kind: RetainedSourceKind,
    byte_size: u64,
    extraction_status: RetainedSourceExtractionStatus,
    issue_code: Option<String>,
    retention_generation: u64,
    retention_status: RetainedSourceRetentionStatus,
    committed_at_ms: u64,
    profile_version_number: u64,
    import_mode: RetainedSourceImportMode,
    import_file_count: u64,
    content_reference_count: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct RetainedSourceListResult {
    scope: RetainedSourceScope,
    current_generation: u64,
    offset: u64,
    limit: u64,
    total_entries: u64,
    total_unique_files: u64,
    total_imports: u64,
    items: Vec<RetainedSourceListItem>,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum ProfileReviewInboxScope {
    Inbox,
    Deferred,
    History,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum ProfileReviewInboxState {
    Inbox,
    Deferred,
    Rejected,
    Applied,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum ProfileReviewInboxAction {
    Defer,
    Reject,
    Reopen,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum ProfileReviewInboxCandidateKind {
    Education,
    Projects,
    Skills,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum ProfileReviewInboxOperationKind {
    Add,
    Update,
}

impl ProfileReviewInboxOperationKind {
    fn parse(value: &str) -> Result<Self, String> {
        match value {
            "add" => Ok(Self::Add),
            "update" => Ok(Self::Update),
            _ => Err("The review item operation is invalid".to_string()),
        }
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq, Hash)]
#[serde(rename_all = "snake_case")]
enum ProfileReviewInboxChangedField {
    Name,
    Description,
    Url,
    StartDate,
    EndDate,
    Highlights,
    Keywords,
    Institution,
    StudyType,
    Area,
    Score,
    Courses,
    Level,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileReviewInboxCounts {
    inbox: u64,
    deferred: u64,
    history: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileReviewInboxDetail {
    label: String,
    value: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileReviewInboxEvidence {
    display_name: String,
    source_format: RetainedSourceFormat,
    source_kind: RetainedSourceKind,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    location_label: Option<String>,
    excerpt: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileReviewInboxItem {
    id: String,
    state: ProfileReviewInboxState,
    state_revision: u64,
    candidate_kind: ProfileReviewInboxCandidateKind,
    operation_kind: ProfileReviewInboxOperationKind,
    title: String,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    subtitle: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    date_range: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    summary: Option<String>,
    highlights: Vec<String>,
    tags: Vec<String>,
    details: Vec<ProfileReviewInboxDetail>,
    changed_fields: Vec<ProfileReviewInboxChangedField>,
    candidate: ProfileReviewInboxCandidate,
    evidence: Vec<ProfileReviewInboxEvidence>,
    is_previous_import_set: bool,
    created_at_ms: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileReviewInboxPage {
    #[serde(deserialize_with = "deserialize_required_nullable")]
    current_profile_version_id: Option<String>,
    scope: ProfileReviewInboxScope,
    offset: u64,
    limit: u64,
    total_items: u64,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    next_offset: Option<u64>,
    counts: ProfileReviewInboxCounts,
    items: Vec<ProfileReviewInboxItem>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileReviewInboxTransitionReceipt {
    request_id: String,
    review_item_id: String,
    action: ProfileReviewInboxAction,
    previous_state: ProfileReviewInboxState,
    state: ProfileReviewInboxState,
    state_revision: u64,
    created_at_ms: u64,
    created: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(untagged)]
enum ProfileReviewInboxCandidate {
    Projects(ProfileProjectEntryInput),
    Education(ProfileEducationEntryInput),
    Skills(ProfileSkillEntryInput),
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileReviewInboxApplyReceipt {
    request_id: String,
    review_item_id: String,
    previous_state: ProfileReviewInboxState,
    state: ProfileReviewInboxState,
    state_revision: u64,
    profile_version_id: String,
    parent_profile_version_id: String,
    version_number: u64,
    candidate_kind: ProfileReviewInboxCandidateKind,
    operation_kind: ProfileReviewInboxOperationKind,
    entry_index: u64,
    changed_fields: Vec<ProfileReviewInboxChangedField>,
    created_at_ms: u64,
    created: bool,
}

struct ValidatedProfileReviewInboxApplyRequest {
    review_item_id: String,
    expected_state_revision: u64,
    expected_parent_profile_version_id: String,
    request_id: String,
    expected_operation_kind: ProfileReviewInboxOperationKind,
    candidate: ProfileReviewInboxCandidate,
    params: Value,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq, Hash)]
#[serde(rename_all = "snake_case")]
enum ProfileReviewSourceKind {
    SourceDocuments,
    StructuredFile,
    ManualStart,
    LocalEdit,
    LocalRestore,
    Other,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq, Hash)]
#[serde(rename_all = "snake_case")]
enum ProfileReviewContactKind {
    Email,
    Phone,
    Location,
    Website,
    Profile,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq, Hash)]
#[serde(rename_all = "snake_case")]
enum ProfileReviewSectionKey {
    Work,
    Education,
    Skills,
    Projects,
    Volunteer,
    Certificates,
    Awards,
    Publications,
    Languages,
    Interests,
    References,
}

const PROFILE_REVIEW_SECTION_SPECS: [(ProfileReviewSectionKey, &str); 11] = [
    (ProfileReviewSectionKey::Work, "Work history"),
    (ProfileReviewSectionKey::Education, "Education"),
    (ProfileReviewSectionKey::Skills, "Skills"),
    (ProfileReviewSectionKey::Projects, "Projects & experiences"),
    (ProfileReviewSectionKey::Volunteer, "Volunteer work"),
    (ProfileReviewSectionKey::Certificates, "Certificates"),
    (ProfileReviewSectionKey::Awards, "Awards"),
    (ProfileReviewSectionKey::Publications, "Publications"),
    (ProfileReviewSectionKey::Languages, "Languages"),
    (ProfileReviewSectionKey::Interests, "Interests"),
    (ProfileReviewSectionKey::References, "References"),
];

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileReviewContact {
    kind: ProfileReviewContactKind,
    label: String,
    value: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileReviewSectionSummary {
    key: ProfileReviewSectionKey,
    label: String,
    count: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileReviewSummary {
    profile_version_id: String,
    version_number: u64,
    created_at_ms: u64,
    source_kind: ProfileReviewSourceKind,
    renderable: bool,
    name: String,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    headline: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    summary: Option<String>,
    contacts: Vec<ProfileReviewContact>,
    sections: Vec<ProfileReviewSectionSummary>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileReviewDetail {
    label: String,
    value: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileReviewItem {
    ordinal: u64,
    title: String,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    subtitle: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    date_range: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    location: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    summary: Option<String>,
    highlights: Vec<String>,
    tags: Vec<String>,
    details: Vec<ProfileReviewDetail>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileReviewSectionPage {
    profile_version_id: String,
    key: ProfileReviewSectionKey,
    label: String,
    total_items: u64,
    offset: u64,
    limit: u64,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    next_offset: Option<u64>,
    items: Vec<ProfileReviewItem>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileVersionSummary {
    profile_version_id: String,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    parent_profile_version_id: Option<String>,
    version_number: u64,
    created_at_ms: u64,
    source_kind: ProfileReviewSourceKind,
    name: String,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    headline: Option<String>,
    renderable: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileVersionList {
    #[serde(deserialize_with = "deserialize_required_nullable")]
    current_profile_version_id: Option<String>,
    total_items: u64,
    offset: u64,
    limit: u64,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    next_offset: Option<u64>,
    items: Vec<ProfileVersionSummary>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileRestoreReceipt {
    request_id: String,
    profile_version_id: String,
    parent_profile_version_id: String,
    restored_from_profile_version_id: String,
    restored_from_version_number: u64,
    version_number: u64,
    created_at_ms: u64,
    created: bool,
    profile_name: String,
    renderable: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileBasicsLocation {
    #[serde(deserialize_with = "deserialize_required_nullable")]
    city: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    region: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    country_code: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileBasics {
    profile_version_id: String,
    version_number: u64,
    created_at_ms: u64,
    renderable: bool,
    name: String,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    headline: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    summary: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    email: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    phone: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    url: Option<String>,
    location: ProfileBasicsLocation,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq, Hash)]
enum ProfileBasicField {
    #[serde(rename = "name")]
    Name,
    #[serde(rename = "headline")]
    Headline,
    #[serde(rename = "summary")]
    Summary,
    #[serde(rename = "email")]
    Email,
    #[serde(rename = "phone")]
    Phone,
    #[serde(rename = "url")]
    Url,
    #[serde(rename = "location.city")]
    LocationCity,
    #[serde(rename = "location.region")]
    LocationRegion,
    #[serde(rename = "location.country_code")]
    LocationCountryCode,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileBasicChange {
    field: ProfileBasicField,
    label: String,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    before: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    after: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileSectionChange {
    key: ProfileReviewSectionKey,
    label: String,
    before_count: u64,
    after_count: u64,
    content_changed: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileVersionDiff {
    from_version: ProfileVersionSummary,
    to_version: ProfileVersionSummary,
    basic_changes: Vec<ProfileBasicChange>,
    section_changes: Vec<ProfileSectionChange>,
    total_changes: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileBasicsLocationPatch {
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    city: RevisionPatchField<String>,
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    region: RevisionPatchField<String>,
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    country_code: RevisionPatchField<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileBasicsPatch {
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    name: RevisionPatchField<String>,
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    headline: RevisionPatchField<String>,
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    summary: RevisionPatchField<String>,
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    email: RevisionPatchField<String>,
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    phone: RevisionPatchField<String>,
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    url: RevisionPatchField<String>,
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    location: RevisionPatchField<ProfileBasicsLocationPatch>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileBasicsUpdateReceipt {
    request_id: String,
    profile_version_id: String,
    parent_profile_version_id: String,
    version_number: u64,
    created_at_ms: u64,
    created: bool,
    changed_fields: Vec<ProfileBasicField>,
    profile_name: String,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    headline: Option<String>,
    renderable: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ManualProfileStartReceipt {
    request_id: String,
    profile_version_id: String,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    parent_profile_version_id: Option<String>,
    version_number: u64,
    created_at_ms: u64,
    created: bool,
    changed_fields: Vec<ProfileBasicField>,
    profile_name: String,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    headline: Option<String>,
    renderable: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileWorkEntry {
    entry_index: u64,
    name: String,
    position: String,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    url: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    start_date: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    end_date: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    summary: Option<String>,
    highlights: Vec<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    location: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileWorkPage {
    profile_version_id: String,
    version_number: u64,
    total_items: u64,
    offset: u64,
    limit: u64,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    next_offset: Option<u64>,
    items: Vec<ProfileWorkEntry>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileWorkEntryInput {
    name: String,
    position: String,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    url: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    start_date: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    end_date: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    summary: Option<String>,
    highlights: Vec<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    location: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileWorkEntryPatch {
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    name: RevisionPatchField<String>,
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    position: RevisionPatchField<String>,
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    url: RevisionPatchField<String>,
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    start_date: RevisionPatchField<String>,
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    end_date: RevisionPatchField<String>,
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    summary: RevisionPatchField<String>,
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    highlights: RevisionPatchField<Vec<String>>,
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    location: RevisionPatchField<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum ProfileWorkOperation {
    Add {
        entry: ProfileWorkEntryInput,
    },
    Update {
        entry_index: u64,
        patch: ProfileWorkEntryPatch,
    },
    Remove {
        entry_index: u64,
    },
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq, Hash)]
#[serde(rename_all = "snake_case")]
enum ProfileWorkOperationKind {
    Add,
    Update,
    Remove,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq, Hash)]
#[serde(rename_all = "snake_case")]
enum ProfileWorkField {
    Name,
    Position,
    Url,
    StartDate,
    EndDate,
    Summary,
    Highlights,
    Location,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileWorkUpdateReceipt {
    request_id: String,
    profile_version_id: String,
    parent_profile_version_id: String,
    version_number: u64,
    created_at_ms: u64,
    created: bool,
    operation: ProfileWorkOperationKind,
    entry_index: u64,
    changed_fields: Vec<ProfileWorkField>,
    profile_name: String,
    work_entries: u64,
    renderable: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileProjectEntry {
    entry_index: u64,
    name: String,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    description: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    url: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    start_date: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    end_date: Option<String>,
    highlights: Vec<String>,
    keywords: Vec<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileProjectsPage {
    profile_version_id: String,
    section: ProfileEditableSection,
    version_number: u64,
    total_items: u64,
    offset: u64,
    limit: u64,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    next_offset: Option<u64>,
    items: Vec<ProfileProjectEntry>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileProjectEntryInput {
    name: String,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    description: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    url: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    start_date: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    end_date: Option<String>,
    highlights: Vec<String>,
    keywords: Vec<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileProjectEntryPatch {
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    name: RevisionPatchField<String>,
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    description: RevisionPatchField<String>,
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    url: RevisionPatchField<String>,
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    start_date: RevisionPatchField<String>,
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    end_date: RevisionPatchField<String>,
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    highlights: RevisionPatchField<Vec<String>>,
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    keywords: RevisionPatchField<Vec<String>>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum ProfileProjectsOperation {
    Add {
        entry: ProfileProjectEntryInput,
    },
    Update {
        entry_index: u64,
        patch: ProfileProjectEntryPatch,
    },
    Remove {
        entry_index: u64,
    },
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileEducationEntry {
    entry_index: u64,
    institution: String,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    study_type: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    area: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    url: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    start_date: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    end_date: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    score: Option<String>,
    courses: Vec<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileEducationPage {
    profile_version_id: String,
    section: ProfileEditableSection,
    version_number: u64,
    total_items: u64,
    offset: u64,
    limit: u64,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    next_offset: Option<u64>,
    items: Vec<ProfileEducationEntry>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileEducationEntryInput {
    institution: String,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    study_type: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    area: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    url: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    start_date: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    end_date: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    score: Option<String>,
    courses: Vec<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileEducationEntryPatch {
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    institution: RevisionPatchField<String>,
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    study_type: RevisionPatchField<String>,
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    area: RevisionPatchField<String>,
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    url: RevisionPatchField<String>,
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    start_date: RevisionPatchField<String>,
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    end_date: RevisionPatchField<String>,
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    score: RevisionPatchField<String>,
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    courses: RevisionPatchField<Vec<String>>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum ProfileEducationOperation {
    Add {
        entry: ProfileEducationEntryInput,
    },
    Update {
        entry_index: u64,
        patch: ProfileEducationEntryPatch,
    },
    Remove {
        entry_index: u64,
    },
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileSkillEntry {
    entry_index: u64,
    name: String,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    level: Option<String>,
    keywords: Vec<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileSkillsPage {
    profile_version_id: String,
    section: ProfileEditableSection,
    version_number: u64,
    total_items: u64,
    offset: u64,
    limit: u64,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    next_offset: Option<u64>,
    items: Vec<ProfileSkillEntry>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileSkillEntryInput {
    name: String,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    level: Option<String>,
    keywords: Vec<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileSkillEntryPatch {
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    name: RevisionPatchField<String>,
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    level: RevisionPatchField<String>,
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    keywords: RevisionPatchField<Vec<String>>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum ProfileSkillsOperation {
    Add {
        entry: ProfileSkillEntryInput,
    },
    Update {
        entry_index: u64,
        patch: ProfileSkillEntryPatch,
    },
    Remove {
        entry_index: u64,
    },
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq, Hash)]
#[serde(rename_all = "snake_case")]
enum ProfileEditableSection {
    Projects,
    Education,
    Skills,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq, Hash)]
#[serde(rename_all = "snake_case")]
enum ProfileSectionOperationKind {
    Add,
    Update,
    Remove,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq, Hash)]
#[serde(rename_all = "snake_case")]
enum ProfileSectionField {
    Name,
    Description,
    Url,
    StartDate,
    EndDate,
    Highlights,
    Keywords,
    Institution,
    StudyType,
    Area,
    Score,
    Courses,
    Level,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileSectionUpdateReceipt {
    request_id: String,
    profile_version_id: String,
    parent_profile_version_id: String,
    version_number: u64,
    created_at_ms: u64,
    created: bool,
    section: ProfileEditableSection,
    operation: ProfileSectionOperationKind,
    entry_index: u64,
    changed_fields: Vec<ProfileSectionField>,
    profile_name: String,
    section_entries: u64,
    renderable: bool,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq, Hash)]
#[serde(rename_all = "snake_case")]
enum ProfileChangeSection {
    Work,
    Projects,
    Education,
    Skills,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
enum ProfileChangeReasonCode {
    #[serde(rename = "matching_employer_role_and_dates")]
    EmployerRoleAndDates,
    #[serde(rename = "matching_project_name_and_dates")]
    ProjectNameAndDates,
    #[serde(rename = "matching_institution_program_and_dates")]
    InstitutionProgramAndDates,
    #[serde(rename = "matching_skill_group")]
    SkillGroup,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(tag = "section", rename_all = "snake_case", deny_unknown_fields)]
enum ProfileChangeSuggestion {
    Work {
        suggestion_id: String,
        keep_entry_index: u64,
        remove_entry_index: u64,
        reason_codes: Vec<ProfileChangeReasonCode>,
        merged_entry: ProfileWorkEntryInput,
        changed_fields: Vec<ProfileWorkField>,
    },
    Projects {
        suggestion_id: String,
        keep_entry_index: u64,
        remove_entry_index: u64,
        reason_codes: Vec<ProfileChangeReasonCode>,
        merged_entry: ProfileProjectEntryInput,
        changed_fields: Vec<ProfileSectionField>,
    },
    Education {
        suggestion_id: String,
        keep_entry_index: u64,
        remove_entry_index: u64,
        reason_codes: Vec<ProfileChangeReasonCode>,
        merged_entry: ProfileEducationEntryInput,
        changed_fields: Vec<ProfileSectionField>,
    },
    Skills {
        suggestion_id: String,
        keep_entry_index: u64,
        remove_entry_index: u64,
        reason_codes: Vec<ProfileChangeReasonCode>,
        merged_entry: ProfileSkillEntryInput,
        changed_fields: Vec<ProfileSectionField>,
    },
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileChangesPreview {
    profile_version_id: String,
    version_number: u64,
    algorithm_version: u64,
    suggestions: Vec<ProfileChangeSuggestion>,
    truncated: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum ProfileChangeOperation {
    Remove {
        section: ProfileChangeSection,
        entry_index: u64,
    },
    Update {
        section: ProfileChangeSection,
        entry_index: u64,
        patch: Value,
    },
    Merge {
        section: ProfileChangeSection,
        keep_entry_index: u64,
        remove_entry_index: u64,
        suggestion_id: String,
    },
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileChangeSectionCounts {
    work: u64,
    projects: u64,
    education: u64,
    skills: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProfileChangesApplyReceipt {
    request_id: String,
    profile_version_id: String,
    parent_profile_version_id: String,
    version_number: u64,
    created_at_ms: u64,
    created: bool,
    operation_count: u64,
    changed_sections: Vec<ProfileChangeSection>,
    section_counts: ProfileChangeSectionCounts,
    profile_name: String,
    renderable: bool,
}

#[derive(Clone, Debug, Serialize)]
struct ProfileImportResult {
    profile_version_id: String,
    version_number: u64,
    created: bool,
    checksum_sha256: String,
    profile_name: String,
    work_entries: u64,
    education_entries: u64,
    skill_groups: u64,
    renderable: bool,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ProfileImportResultWire {
    profile_version_id: String,
    version_number: u64,
    created: bool,
    checksum_sha256: String,
    profile_name: String,
    work_entries: u64,
    education_entries: u64,
    skill_groups: u64,
    renderable: bool,
    compaction_report: Value,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct ProfileReference {
    profile_version_id: String,
    version_number: u64,
    profile_name: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct SourceFileCounts {
    discovered: u64,
    staged: u64,
    parsed: u64,
    duplicates: u64,
    skipped: u64,
    failed: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct SourceKindCounts {
    resume: u64,
    evidence: u64,
    preferences: u64,
    unclassified: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct AggregateSourceWarning {
    code: String,
    stage: String,
    severity: String,
    count: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct ProfileSourceScanResult {
    scan_id: String,
    expires_at_ms: u64,
    base_profile: Option<ProfileReference>,
    file_counts: SourceFileCounts,
    source_counts: SourceKindCounts,
    format_counts: std::collections::BTreeMap<String, u64>,
    warnings: Vec<AggregateSourceWarning>,
    can_build: bool,
    review_candidate_count: u64,
    source_review_count: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct BuiltSourceCounts {
    parsed: u64,
    used: u64,
    unused: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct BuiltProfileCounts {
    work_entries: u64,
    education_entries: u64,
    skill_groups: u64,
    evidence_claims: u64,
    preference_items: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct ProfileSourceBuildResult {
    scan_id: String,
    profile_version_id: String,
    version_number: u64,
    created: bool,
    checksum_sha256: String,
    profile_name: String,
    renderable: bool,
    source_counts: BuiltSourceCounts,
    profile_counts: BuiltProfileCounts,
    warnings: Vec<AggregateSourceWarning>,
    review_item_count: u64,
    source_review_count: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct DiscardSourceScanResult {
    discarded: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct DiscardUnfinishedSourceReviewsResult {
    discarded_previews: u64,
}

#[derive(Clone, Debug, Deserialize)]
struct EngineResumeArtifact {
    artifact_id: String,
    format: String,
    relative_path: String,
    checksum_sha256: String,
    byte_size: u64,
    suggested_filename: String,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct EngineBaselineResumeArtifact {
    profile_version_id: String,
    artifact_id: String,
    format: String,
    media_type: String,
    relative_path: String,
    checksum_sha256: String,
    byte_size: u64,
    suggested_filename: String,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct EngineTailoredResumeArtifact {
    draft_id: String,
    opportunity_id: String,
    profile_version_id: String,
    artifact_id: String,
    format: String,
    media_type: String,
    relative_path: String,
    checksum_sha256: String,
    byte_size: u64,
    suggested_filename: String,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct EngineTailoredResumeRevisionArtifact {
    revision_id: String,
    draft_id: String,
    opportunity_id: String,
    profile_version_id: String,
    artifact_id: String,
    format: String,
    media_type: String,
    relative_path: String,
    checksum_sha256: String,
    byte_size: u64,
    suggested_filename: String,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
enum RevisionPatchField<T> {
    #[default]
    Missing,
    Null,
    Value(T),
}

impl<T> RevisionPatchField<T> {
    fn is_missing(&self) -> bool {
        matches!(self, Self::Missing)
    }
}

impl<T: Serialize> Serialize for RevisionPatchField<T> {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        match self {
            Self::Missing => serializer.serialize_unit(),
            Self::Null => serializer.serialize_none(),
            Self::Value(value) => value.serialize(serializer),
        }
    }
}

impl<'de, T: Deserialize<'de>> Deserialize<'de> for RevisionPatchField<T> {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        Ok(match Option::<T>::deserialize(deserializer)? {
            Some(value) => Self::Value(value),
            None => Self::Null,
        })
    }
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct TailoredRevisionBasicsEdits {
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    label: RevisionPatchField<String>,
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    summary: RevisionPatchField<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct TailoredRevisionWorkEdit {
    source_index: u64,
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    summary: RevisionPatchField<String>,
    #[serde(default, skip_serializing_if = "RevisionPatchField::is_missing")]
    highlights: RevisionPatchField<Vec<String>>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct TailoredResumeRevisionEdits {
    basics: TailoredRevisionBasicsEdits,
    work: Vec<TailoredRevisionWorkEdit>,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
enum TailoredRevisionAuthorKind {
    User,
}

fn deserialize_required_nullable<'de, D, T>(deserializer: D) -> Result<Option<T>, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de>,
{
    Option::<T>::deserialize(deserializer)
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct TailoredResumeRevisionSummary {
    id: String,
    draft_id: String,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    parent_revision_id: Option<String>,
    revision_number: u64,
    author_kind: TailoredRevisionAuthorKind,
    created_at_ms: u64,
    parent_resume_checksum_sha256: String,
    resume_checksum_sha256: String,
    changed_field_count: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct TailoredResumeRevision {
    id: String,
    draft_id: String,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    parent_revision_id: Option<String>,
    revision_number: u64,
    author_kind: TailoredRevisionAuthorKind,
    created_at_ms: u64,
    parent_resume_checksum_sha256: String,
    resume_checksum_sha256: String,
    changed_fields: Vec<String>,
    resume: Value,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct TailoredResumeRevisionList {
    draft_id: String,
    base_resume_checksum_sha256: String,
    revisions: Vec<TailoredResumeRevisionSummary>,
    truncated: bool,
}

#[derive(Clone, Debug, Serialize)]
struct ResumeExportResult {
    artifact_id: String,
    format: String,
    byte_size: u64,
    checksum_sha256: String,
    suggested_filename: String,
    saved_filename: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SaveOpportunityInput {
    title: String,
    description: String,
    #[serde(default)]
    company: Option<String>,
    #[serde(default)]
    location: Option<String>,
    #[serde(default)]
    source_url: Option<String>,
    #[serde(default)]
    apply_url: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum OpportunityStatus {
    Saved,
    Analyzing,
    Ready,
    Applying,
    Applied,
    Archived,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum OpportunityTrackerStage {
    Tracked,
    New,
    Matched,
    ToApply,
    Applied,
    Interviewing,
    Offer,
    Rejected,
    Closed,
    Ignored,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum OpportunityListStage {
    All,
    Tracked,
    New,
    Matched,
    ToApply,
    Applied,
    Interviewing,
    Offer,
    Rejected,
    Closed,
    Ignored,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum OpportunityListSort {
    LastAction,
    Strength,
    Stage,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
enum OpportunityMatchSignal {
    Strong,
    Mixed,
    Limited,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
enum OpportunityMatchContract {
    #[serde(rename = "deterministic-local-v1")]
    DeterministicLocalV1,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct OpportunitySnapshotSummary {
    id: String,
    checksum_sha256: String,
    captured_at_ms: u64,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct OpportunityMatchSummary {
    id: String,
    contract: OpportunityMatchContract,
    score: u8,
    signal: OpportunityMatchSignal,
    created_at_ms: u64,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct OpportunityProfileVersion {
    id: String,
    version_number: u64,
    checksum_sha256: String,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct OpportunitySummary {
    id: String,
    title: String,
    company: String,
    location: String,
    status: OpportunityStatus,
    tracker_stage: OpportunityTrackerStage,
    tracker_updated_at_ms: u64,
    created_at_ms: u64,
    updated_at_ms: u64,
    snapshot: OpportunitySnapshotSummary,
    #[serde(rename = "match")]
    match_result: OpportunityMatchSummary,
    profile_version: OpportunityProfileVersion,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct OpportunityListResult {
    items: Vec<OpportunitySummary>,
    total: u64,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct OpportunityRecord {
    id: String,
    title: String,
    company: String,
    location: String,
    source_url: Option<String>,
    apply_url: Option<String>,
    status: OpportunityStatus,
    notes: String,
    tracker_stage: OpportunityTrackerStage,
    tracker_updated_at_ms: u64,
    created_at_ms: u64,
    updated_at_ms: u64,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct OpportunitySnapshotDetail {
    id: String,
    checksum_sha256: String,
    captured_at_ms: u64,
    description: String,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct OpportunityEvidence {
    term: String,
    profile_path: String,
    snippet: String,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct OpportunityMatchDetail {
    id: String,
    contract: OpportunityMatchContract,
    score: u8,
    signal: OpportunityMatchSignal,
    matched_terms: Vec<String>,
    terms_to_review: Vec<String>,
    evidence: Vec<OpportunityEvidence>,
    created_at_ms: u64,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct OpportunityDetail {
    opportunity: OpportunityRecord,
    snapshot: OpportunitySnapshotDetail,
    #[serde(rename = "match")]
    match_result: OpportunityMatchDetail,
    profile_version: OpportunityProfileVersion,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct EngineError {
    code: String,
    message: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct EngineEnvelope<T> {
    protocol_version: u32,
    id: Option<String>,
    ok: bool,
    result: Option<T>,
    error: Option<EngineError>,
}

#[derive(Clone)]
struct CachedEngineStatus {
    checked_at: Instant,
    result: Result<EngineStatus, String>,
}

#[derive(Default)]
pub(crate) struct EngineRuntime {
    cached_status: Mutex<Option<CachedEngineStatus>>,
    pub(crate) operation_gate: Mutex<()>,
}

pub(crate) fn prepare_data_dir(data_dir: &Path) -> Result<(), String> {
    fs::create_dir_all(data_dir)
        .map_err(|_| "Unable to create the local data directory".to_string())?;
    let metadata = fs::symlink_metadata(data_dir)
        .map_err(|_| "Unable to inspect the local data directory".to_string())?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return Err("The local data directory is unsafe".to_string());
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(data_dir, fs::Permissions::from_mode(0o700))
            .map_err(|_| "Unable to secure the local data directory".to_string())?;
    }
    Ok(())
}

fn append_bounded(
    buffer: &mut Vec<u8>,
    chunk: &[u8],
    total_length: &mut usize,
) -> Result<(), String> {
    *total_length = total_length.saturating_add(chunk.len());
    if *total_length > ENGINE_OUTPUT_LIMIT_BYTES {
        return Err("The local engine produced more than the 192 KiB output limit".to_string());
    }
    buffer.extend_from_slice(chunk);
    Ok(())
}

fn safe_engine_error(error: EngineError) -> String {
    let code: String = error
        .code
        .chars()
        .filter(|character| {
            character.is_ascii_alphanumeric() || matches!(character, '_' | '-' | '.')
        })
        .take(64)
        .collect();
    let message: String = error
        .message
        .chars()
        .filter(|character| !character.is_control())
        .take(300)
        .collect();
    format!(
        "{}: {}",
        if code.is_empty() {
            "engine_error"
        } else {
            &code
        },
        if message.is_empty() {
            "The local engine could not complete the request"
        } else {
            &message
        }
    )
}

fn normalized_required_opportunity_text(
    value: String,
    field_name: &str,
    max_chars: usize,
    allow_line_breaks: bool,
) -> Result<String, String> {
    let normalized = value.trim().to_string();
    if normalized.is_empty() {
        return Err(format!("{field_name} is required"));
    }
    if normalized.chars().count() > max_chars || normalized.len() > max_chars {
        return Err(format!("{field_name} exceeds the local limit"));
    }
    if normalized.chars().any(|character| {
        character.is_control() && !(allow_line_breaks && matches!(character, '\n' | '\r' | '\t'))
    }) {
        return Err(format!(
            "{field_name} contains unsupported control characters"
        ));
    }
    Ok(normalized)
}

fn normalized_optional_opportunity_text(
    value: Option<String>,
    field_name: &str,
    max_chars: usize,
) -> Result<Option<String>, String> {
    let Some(value) = value else {
        return Ok(None);
    };
    let normalized = value.trim().to_string();
    if normalized.is_empty() {
        return Ok(None);
    }
    if normalized.chars().count() > max_chars || normalized.len() > max_chars {
        return Err(format!("{field_name} exceeds the local limit"));
    }
    if normalized.chars().any(char::is_control) {
        return Err(format!(
            "{field_name} contains unsupported control characters"
        ));
    }
    Ok(Some(normalized))
}

fn validate_https_url(value: &str, field_name: &str) -> Result<(), String> {
    if value.is_empty() {
        return Ok(());
    }
    if value.chars().count() > OPPORTUNITY_URL_LIMIT_CHARS
        || value.len() > OPPORTUNITY_URL_LIMIT_CHARS
    {
        return Err(format!("{field_name} exceeds the local limit"));
    }
    if value.trim() != value
        || value
            .chars()
            .any(|character| character.is_whitespace() || character.is_control())
        || value.contains('\\')
    {
        return Err(format!("{field_name} must be a valid HTTPS URL"));
    }
    let parsed =
        Url::parse(value).map_err(|_| format!("{field_name} must be a valid HTTPS URL"))?;
    let authority = value
        .strip_prefix("https://")
        .and_then(|remainder| remainder.split(['/', '?', '#']).next())
        .unwrap_or_default();
    if parsed.scheme() != "https"
        || parsed.host_str().is_none()
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || authority.contains('@')
    {
        return Err(format!("{field_name} must be a credential-free HTTPS URL"));
    }
    Ok(())
}

fn normalized_optional_https_url(
    value: Option<String>,
    field_name: &str,
) -> Result<Option<String>, String> {
    let Some(value) = value else {
        return Ok(None);
    };
    if value.is_empty() {
        return Ok(None);
    }
    validate_https_url(&value, field_name)?;
    Ok(Some(value))
}

fn normalized_opportunity_query(value: Option<String>) -> Result<String, String> {
    // The engine list contract requires text; an empty search is not JSON null.
    normalized_optional_opportunity_text(value, "Search query", OPPORTUNITY_QUERY_LIMIT_CHARS)
        .map(Option::unwrap_or_default)
}

fn normalized_opportunity_notes(value: String) -> Result<String, String> {
    let normalized = value.trim().to_string();
    if normalized.chars().count() > OPPORTUNITY_NOTES_LIMIT_CHARS
        || normalized.len() > OPPORTUNITY_NOTES_LIMIT_CHARS
    {
        return Err("Notes exceed the local limit".to_string());
    }
    if normalized
        .chars()
        .any(|character| character.is_control() && !matches!(character, '\n' | '\r' | '\t'))
    {
        return Err("Notes contain unsupported control characters".to_string());
    }
    Ok(normalized)
}

fn validated_opportunity_offset(offset: usize) -> Result<usize, String> {
    if offset > OPPORTUNITY_LIST_OFFSET_LIMIT {
        return Err("The opportunity list offset exceeds the local limit".to_string());
    }
    Ok(offset)
}

impl OpportunityListStage {
    fn parse(value: &str) -> Result<Self, String> {
        match value {
            "all" => Ok(Self::All),
            "tracked" => Ok(Self::Tracked),
            "new" => Ok(Self::New),
            "matched" => Ok(Self::Matched),
            "to_apply" => Ok(Self::ToApply),
            "applied" => Ok(Self::Applied),
            "interviewing" => Ok(Self::Interviewing),
            "offer" => Ok(Self::Offer),
            "rejected" => Ok(Self::Rejected),
            "closed" => Ok(Self::Closed),
            "ignored" => Ok(Self::Ignored),
            _ => Err("The opportunity stage filter is invalid".to_string()),
        }
    }
}

impl OpportunityListSort {
    fn parse(value: &str) -> Result<Self, String> {
        match value {
            "last_action" => Ok(Self::LastAction),
            "strength" => Ok(Self::Strength),
            "stage" => Ok(Self::Stage),
            _ => Err("The opportunity sort is invalid".to_string()),
        }
    }
}

impl OpportunityTrackerStage {
    fn parse(value: &str) -> Result<Self, String> {
        match value {
            "tracked" => Ok(Self::Tracked),
            "new" => Ok(Self::New),
            "matched" => Ok(Self::Matched),
            "to_apply" => Ok(Self::ToApply),
            "applied" => Ok(Self::Applied),
            "interviewing" => Ok(Self::Interviewing),
            "offer" => Ok(Self::Offer),
            "rejected" => Ok(Self::Rejected),
            "closed" => Ok(Self::Closed),
            "ignored" => Ok(Self::Ignored),
            _ => Err("The opportunity tracker stage is invalid".to_string()),
        }
    }
}

fn opportunity_list_params(
    query: Option<String>,
    offset: usize,
    stage: &str,
    sort: &str,
) -> Result<Value, String> {
    let query = normalized_opportunity_query(query)?;
    let offset = validated_opportunity_offset(offset)?;
    let stage = OpportunityListStage::parse(stage)?;
    let sort = OpportunityListSort::parse(sort)?;
    Ok(json!({
        "query": query,
        "offset": offset,
        "stage": stage,
        "sort": sort,
    }))
}

fn opportunity_tracker_update_params(
    opportunity_id: &str,
    expected_tracker_updated_at_ms: u64,
    tracker_stage: Option<String>,
    notes: Option<String>,
) -> Result<Value, String> {
    let opportunity_id = validated_opportunity_id(opportunity_id)?;
    if expected_tracker_updated_at_ms == 0 {
        return Err("The opportunity tracker version is invalid".to_string());
    }
    validate_javascript_integer(
        expected_tracker_updated_at_ms,
        "opportunity tracker version",
    )?;
    if tracker_stage.is_none() && notes.is_none() {
        return Err("Choose a tracker stage or notes to update".to_string());
    }

    let mut params = serde_json::Map::new();
    params.insert("opportunity_id".to_string(), json!(opportunity_id));
    params.insert(
        "expected_tracker_updated_at_ms".to_string(),
        json!(expected_tracker_updated_at_ms),
    );
    if let Some(tracker_stage) = tracker_stage {
        params.insert(
            "tracker_stage".to_string(),
            serde_json::to_value(OpportunityTrackerStage::parse(&tracker_stage)?)
                .map_err(|_| "The opportunity tracker stage is invalid".to_string())?,
        );
    }
    if let Some(notes) = notes {
        params.insert(
            "notes".to_string(),
            json!(normalized_opportunity_notes(notes)?),
        );
    }
    Ok(Value::Object(params))
}

fn validated_opportunity_id(value: &str) -> Result<String, String> {
    let parsed = Uuid::parse_str(value).map_err(|_| "The opportunity ID is invalid".to_string())?;
    let normalized = parsed.to_string();
    if normalized != value {
        return Err("The opportunity ID is invalid".to_string());
    }
    Ok(normalized)
}

fn validate_sha256(value: &str) -> Result<(), String> {
    if value.len() != 64
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err("The local engine returned an invalid checksum".to_string());
    }
    Ok(())
}

fn validate_engine_text(
    value: &str,
    field_name: &str,
    max_chars: usize,
    allow_empty: bool,
    allow_line_breaks: bool,
) -> Result<(), String> {
    if (!allow_empty && value.trim().is_empty())
        || value.chars().count() > max_chars
        || value.len() > max_chars
    {
        return Err(format!("The local engine returned an invalid {field_name}"));
    }
    if value.chars().any(|character| {
        character.is_control() && !(allow_line_breaks && matches!(character, '\n' | '\r' | '\t'))
    }) {
        return Err(format!("The local engine returned an invalid {field_name}"));
    }
    Ok(())
}

fn validate_uuid_receipt(value: &str) -> Result<(), String> {
    Uuid::parse_str(value)
        .ok()
        .filter(|parsed| parsed.to_string() == value)
        .map(|_| ())
        .ok_or_else(|| "The local engine returned an invalid identifier".to_string())
}

fn validate_javascript_integer(value: u64, field_name: &str) -> Result<(), String> {
    if value > JAVASCRIPT_MAX_SAFE_INTEGER {
        return Err(format!("The local engine returned an invalid {field_name}"));
    }
    Ok(())
}

impl RetainedSourceScope {
    fn parse(value: &str) -> Result<Self, String> {
        match value {
            "active" => Ok(Self::Active),
            "historical" => Ok(Self::Historical),
            "all" => Ok(Self::All),
            _ => Err("The retained material scope is invalid".to_string()),
        }
    }
}

fn retained_source_list_params(scope: &str, offset: usize) -> Result<Value, String> {
    let scope = RetainedSourceScope::parse(scope)?;
    if offset > RETAINED_SOURCE_LIST_OFFSET_LIMIT {
        return Err("The retained material list offset exceeds the local limit".to_string());
    }
    Ok(json!({
        "scope": scope,
        "limit": RETAINED_SOURCE_LIST_LIMIT,
        "offset": offset,
    }))
}

fn validate_retained_source_display_name(value: &str) -> Result<(), String> {
    if value.trim().is_empty()
        || value.chars().count() > RETAINED_SOURCE_DISPLAY_NAME_LIMIT_CHARS
        || value.len() > RETAINED_SOURCE_DISPLAY_NAME_LIMIT_BYTES
        || value.chars().any(char::is_control)
    {
        return Err("The local engine returned an invalid retained material name".to_string());
    }
    Ok(())
}

impl RetainedSourceListItem {
    fn validate(&self, current_generation: u64) -> Result<(), String> {
        validate_uuid_receipt(&self.import_id)?;
        validate_retained_source_display_name(&self.display_name)?;
        for (value, field_name) in [
            (self.ordinal, "retained material ordinal"),
            (self.byte_size, "retained material byte size"),
            (self.retention_generation, "retained material generation"),
            (self.committed_at_ms, "retained material timestamp"),
            (
                self.profile_version_number,
                "retained material profile version",
            ),
            (
                self.import_file_count,
                "retained material import file count",
            ),
            (
                self.content_reference_count,
                "retained material reference count",
            ),
        ] {
            validate_javascript_integer(value, field_name)?;
        }
        if self.byte_size == 0
            || self.retention_generation == 0
            || self.retention_generation > current_generation
            || self.committed_at_ms == 0
            || self.profile_version_number == 0
            || self.import_file_count == 0
            || self.content_reference_count == 0
            || self.ordinal >= self.import_file_count
        {
            return Err("The local engine returned invalid retained material metadata".to_string());
        }
        let expected_status = if self.retention_generation == current_generation {
            RetainedSourceRetentionStatus::Active
        } else {
            RetainedSourceRetentionStatus::Historical
        };
        if self.retention_status != expected_status {
            return Err("The local engine returned an inconsistent retention status".to_string());
        }
        if let Some(issue_code) = self.issue_code.as_deref()
            && (issue_code.is_empty()
                || issue_code.len() > RETAINED_SOURCE_ISSUE_CODE_LIMIT_CHARS
                || issue_code.chars().count() > RETAINED_SOURCE_ISSUE_CODE_LIMIT_CHARS
                || !issue_code.chars().enumerate().all(|(index, character)| {
                    if index == 0 {
                        character.is_ascii_alphanumeric()
                    } else {
                        character.is_ascii_alphanumeric() || matches!(character, '.' | '_' | '-')
                    }
                }))
        {
            return Err("The local engine returned an invalid retained material issue".to_string());
        }
        Ok(())
    }
}

impl RetainedSourceListResult {
    fn validated(
        self,
        expected_scope: RetainedSourceScope,
        expected_offset: usize,
    ) -> Result<Self, String> {
        if self.scope != expected_scope
            || self.current_generation == 0
            || self.offset != expected_offset as u64
            || self.limit != RETAINED_SOURCE_LIST_LIMIT as u64
        {
            return Err(
                "The local engine returned a mismatched retained material list".to_string(),
            );
        }
        for (value, field_name) in [
            (self.current_generation, "retained material generation"),
            (self.offset, "retained material offset"),
            (self.limit, "retained material limit"),
            (self.total_entries, "retained material entry count"),
            (
                self.total_unique_files,
                "retained material unique file count",
            ),
            (self.total_imports, "retained material import count"),
        ] {
            validate_javascript_integer(value, field_name)?;
        }
        let item_count = self.items.len() as u64;
        let page_end = self.offset.checked_add(item_count).ok_or_else(|| {
            "The local engine returned an invalid retained material page".to_string()
        })?;
        if self.items.len() > RETAINED_SOURCE_LIST_LIMIT
            || self.total_unique_files > self.total_entries
            || self.total_imports > self.total_entries
            || (self.total_entries == 0
                && (self.total_unique_files != 0
                    || self.total_imports != 0
                    || !self.items.is_empty()))
            || (self.total_entries > 0 && (self.total_unique_files == 0 || self.total_imports == 0))
            || (!self.items.is_empty()
                && (self.offset >= self.total_entries || page_end > self.total_entries))
            || (self.items.is_empty() && self.offset < self.total_entries)
            || (item_count < self.limit && page_end < self.total_entries)
        {
            return Err("The local engine returned an invalid retained material page".to_string());
        }
        for item in &self.items {
            item.validate(self.current_generation)?;
            match self.scope {
                RetainedSourceScope::Active
                    if item.retention_status != RetainedSourceRetentionStatus::Active =>
                {
                    return Err(
                        "The local engine returned material outside the requested scope"
                            .to_string(),
                    );
                }
                RetainedSourceScope::Historical
                    if item.retention_status != RetainedSourceRetentionStatus::Historical =>
                {
                    return Err(
                        "The local engine returned material outside the requested scope"
                            .to_string(),
                    );
                }
                _ => {}
            }
        }
        Ok(self)
    }
}

impl ProfileReviewInboxScope {
    fn parse(value: &str) -> Result<Self, String> {
        match value {
            "inbox" => Ok(Self::Inbox),
            "deferred" => Ok(Self::Deferred),
            "history" => Ok(Self::History),
            _ => Err("The review inbox scope is invalid".to_string()),
        }
    }

    fn count(self, counts: &ProfileReviewInboxCounts) -> u64 {
        match self {
            Self::Inbox => counts.inbox,
            Self::Deferred => counts.deferred,
            Self::History => counts.history,
        }
    }
}

impl ProfileReviewInboxState {
    fn parse(value: &str) -> Result<Self, String> {
        match value {
            "inbox" => Ok(Self::Inbox),
            "deferred" => Ok(Self::Deferred),
            "rejected" => Ok(Self::Rejected),
            "applied" => Ok(Self::Applied),
            _ => Err("The review item state is invalid".to_string()),
        }
    }
}

impl ProfileReviewInboxAction {
    fn parse(value: &str) -> Result<Self, String> {
        match value {
            "defer" => Ok(Self::Defer),
            "reject" => Ok(Self::Reject),
            "reopen" => Ok(Self::Reopen),
            _ => Err("The review item action is invalid".to_string()),
        }
    }

    fn resulting_state(
        self,
        previous: ProfileReviewInboxState,
    ) -> Result<ProfileReviewInboxState, String> {
        match (previous, self) {
            (ProfileReviewInboxState::Inbox, Self::Defer) => Ok(ProfileReviewInboxState::Deferred),
            (ProfileReviewInboxState::Inbox | ProfileReviewInboxState::Deferred, Self::Reject) => {
                Ok(ProfileReviewInboxState::Rejected)
            }
            (
                ProfileReviewInboxState::Deferred | ProfileReviewInboxState::Rejected,
                Self::Reopen,
            ) => Ok(ProfileReviewInboxState::Inbox),
            _ => Err("That action is not available for this review item".to_string()),
        }
    }
}

impl ProfileReviewInboxCandidateKind {
    fn changed_field_order(self, field: ProfileReviewInboxChangedField) -> Option<usize> {
        let fields: &[ProfileReviewInboxChangedField] = match self {
            Self::Projects => &[
                ProfileReviewInboxChangedField::Name,
                ProfileReviewInboxChangedField::Description,
                ProfileReviewInboxChangedField::Url,
                ProfileReviewInboxChangedField::StartDate,
                ProfileReviewInboxChangedField::EndDate,
                ProfileReviewInboxChangedField::Highlights,
                ProfileReviewInboxChangedField::Keywords,
            ],
            Self::Education => &[
                ProfileReviewInboxChangedField::Institution,
                ProfileReviewInboxChangedField::StudyType,
                ProfileReviewInboxChangedField::Area,
                ProfileReviewInboxChangedField::Url,
                ProfileReviewInboxChangedField::StartDate,
                ProfileReviewInboxChangedField::EndDate,
                ProfileReviewInboxChangedField::Score,
                ProfileReviewInboxChangedField::Courses,
            ],
            Self::Skills => &[
                ProfileReviewInboxChangedField::Name,
                ProfileReviewInboxChangedField::Level,
                ProfileReviewInboxChangedField::Keywords,
            ],
        };
        fields.iter().position(|candidate| *candidate == field)
    }
}

impl ProfileReviewInboxCandidate {
    fn kind(&self) -> ProfileReviewInboxCandidateKind {
        match self {
            Self::Projects(_) => ProfileReviewInboxCandidateKind::Projects,
            Self::Education(_) => ProfileReviewInboxCandidateKind::Education,
            Self::Skills(_) => ProfileReviewInboxCandidateKind::Skills,
        }
    }

    fn validated_request(self) -> Result<Self, String> {
        let candidate = match self {
            Self::Projects(entry) => Self::Projects(entry.validated()?),
            Self::Education(entry) => Self::Education(entry.validated()?),
            Self::Skills(entry) => Self::Skills(entry.validated()?),
        };
        validate_profile_review_serialized_size(
            &candidate,
            PROFILE_REVIEW_INBOX_APPLY_REQUEST_LIMIT_BYTES,
            "review item candidate",
        )?;
        Ok(candidate)
    }

    fn validate_engine_output(&self) -> Result<(), String> {
        let normalized = self.clone().validated_request().map_err(|_| {
            "The local engine returned an invalid review item candidate".to_string()
        })?;
        if &normalized != self {
            return Err(
                "The local engine returned a non-normalized review item candidate".to_string(),
            );
        }
        Ok(())
    }
}

impl ProfileReviewInboxDetail {
    fn validate(&self) -> Result<(), String> {
        validate_profile_review_text(
            &self.label,
            "inbox detail label",
            PROFILE_REVIEW_INBOX_DETAIL_LABEL_LIMIT_CHARS,
            PROFILE_REVIEW_INBOX_DETAIL_LABEL_LIMIT_CHARS * 4,
        )?;
        validate_profile_review_text(
            &self.value,
            "inbox detail value",
            PROFILE_REVIEW_DETAIL_VALUE_LIMIT_CHARS,
            PROFILE_REVIEW_DETAIL_VALUE_LIMIT_CHARS * 4,
        )
    }
}

impl ProfileReviewInboxEvidence {
    fn validate(&self) -> Result<(), String> {
        validate_retained_source_display_name(&self.display_name)?;
        validate_optional_profile_review_text(
            self.location_label.as_deref(),
            "inbox evidence location",
            PROFILE_REVIEW_INBOX_EVIDENCE_LOCATION_LIMIT_CHARS,
            PROFILE_REVIEW_INBOX_EVIDENCE_LOCATION_LIMIT_CHARS * 4,
        )?;
        validate_profile_review_text(
            &self.excerpt,
            "inbox evidence excerpt",
            PROFILE_REVIEW_INBOX_EVIDENCE_EXCERPT_LIMIT_CHARS,
            PROFILE_REVIEW_INBOX_EVIDENCE_EXCERPT_LIMIT_CHARS * 4,
        )
    }
}

impl ProfileReviewInboxItem {
    fn validate(&self, scope: ProfileReviewInboxScope) -> Result<(), String> {
        validate_uuid_receipt(&self.id)?;
        validate_javascript_integer(self.state_revision, "review item state revision")?;
        if self.created_at_ms == 0 {
            return Err("The local engine returned an invalid review item timestamp".to_string());
        }
        validate_javascript_integer(self.created_at_ms, "review item timestamp")?;
        validate_profile_review_text(
            &self.title,
            "inbox item title",
            PROFILE_REVIEW_ITEM_TITLE_LIMIT_CHARS,
            PROFILE_REVIEW_ITEM_TITLE_LIMIT_CHARS * 4,
        )?;
        validate_optional_profile_review_text(
            self.subtitle.as_deref(),
            "inbox item subtitle",
            PROFILE_REVIEW_ITEM_SUBTITLE_LIMIT_CHARS,
            PROFILE_REVIEW_ITEM_SUBTITLE_LIMIT_CHARS * 4,
        )?;
        validate_optional_profile_review_text(
            self.date_range.as_deref(),
            "inbox item date range",
            PROFILE_REVIEW_ITEM_DATE_LIMIT_CHARS,
            PROFILE_REVIEW_ITEM_DATE_LIMIT_CHARS * 4,
        )?;
        validate_optional_profile_review_text(
            self.summary.as_deref(),
            "inbox item summary",
            PROFILE_REVIEW_SUMMARY_TEXT_LIMIT_CHARS,
            PROFILE_REVIEW_SUMMARY_TEXT_LIMIT_CHARS * 4,
        )?;
        if self.highlights.len() > PROFILE_REVIEW_HIGHLIGHT_LIMIT
            || self.tags.len() > PROFILE_REVIEW_TAG_LIMIT
            || self.details.len() > PROFILE_REVIEW_DETAIL_LIMIT
            || self.changed_fields.is_empty()
            || self.changed_fields.len() > PROFILE_REVIEW_INBOX_CHANGED_FIELD_LIMIT
            || self.evidence.is_empty()
            || self.evidence.len() > PROFILE_REVIEW_INBOX_EVIDENCE_LIMIT
        {
            return Err("The local engine returned an oversized review inbox item".to_string());
        }
        for highlight in &self.highlights {
            validate_profile_review_text(
                highlight,
                "inbox item highlight",
                PROFILE_REVIEW_HIGHLIGHT_LIMIT_CHARS,
                PROFILE_REVIEW_HIGHLIGHT_LIMIT_CHARS * 4,
            )?;
        }
        for tag in &self.tags {
            validate_profile_review_text(
                tag,
                "inbox item tag",
                PROFILE_REVIEW_TAG_LIMIT_CHARS,
                PROFILE_REVIEW_TAG_LIMIT_CHARS * 4,
            )?;
        }
        for detail in &self.details {
            detail.validate()?;
        }
        self.candidate.validate_engine_output()?;
        if self.candidate.kind() != self.candidate_kind {
            return Err(
                "The local engine returned an inconsistent review item candidate".to_string(),
            );
        }
        for evidence in &self.evidence {
            evidence.validate()?;
        }

        let mut previous_changed_field_order = None;
        for field in &self.changed_fields {
            let order = self
                .candidate_kind
                .changed_field_order(*field)
                .ok_or_else(|| {
                    "The local engine returned an inconsistent review item field".to_string()
                })?;
            if previous_changed_field_order.is_some_and(|previous| order <= previous) {
                return Err("The local engine returned review item fields out of order".to_string());
            }
            previous_changed_field_order = Some(order);
        }

        let in_scope = match scope {
            ProfileReviewInboxScope::Inbox => {
                self.state == ProfileReviewInboxState::Inbox && !self.is_previous_import_set
            }
            ProfileReviewInboxScope::Deferred => {
                self.state == ProfileReviewInboxState::Deferred && !self.is_previous_import_set
            }
            ProfileReviewInboxScope::History => {
                self.is_previous_import_set
                    || matches!(
                        self.state,
                        ProfileReviewInboxState::Rejected | ProfileReviewInboxState::Applied
                    )
            }
        };
        if !in_scope {
            return Err(
                "The local engine returned a review item outside the requested scope".to_string(),
            );
        }
        Ok(())
    }
}

impl ProfileReviewInboxPage {
    fn validated(
        self,
        expected_scope: ProfileReviewInboxScope,
        expected_offset: usize,
    ) -> Result<Self, String> {
        if let Some(profile_version_id) = &self.current_profile_version_id {
            validate_uuid_receipt(profile_version_id)?;
        } else if self.counts.inbox != 0 || self.counts.deferred != 0 || self.counts.history != 0 {
            return Err(
                "The local engine returned review items without a current profile".to_string(),
            );
        }
        if self.scope != expected_scope
            || self.offset != expected_offset as u64
            || self.limit != PROFILE_REVIEW_INBOX_PAGE_LIMIT as u64
        {
            return Err("The local engine returned a mismatched review inbox page".to_string());
        }
        for (value, field_name) in [
            (self.offset, "review inbox offset"),
            (self.limit, "review inbox limit"),
            (self.total_items, "review inbox item count"),
            (self.counts.inbox, "review inbox count"),
            (self.counts.deferred, "deferred review item count"),
            (self.counts.history, "review history count"),
        ] {
            validate_javascript_integer(value, field_name)?;
        }
        if self.total_items != expected_scope.count(&self.counts)
            || self.items.len() > PROFILE_REVIEW_INBOX_PAGE_LIMIT
        {
            return Err("The local engine returned inconsistent review inbox counts".to_string());
        }
        let item_count = self.items.len() as u64;
        let page_end = self
            .offset
            .checked_add(item_count)
            .ok_or_else(|| "The local engine returned an invalid review inbox page".to_string())?;
        if (!self.items.is_empty()
            && (self.offset >= self.total_items || page_end > self.total_items))
            || (self.items.is_empty() && self.offset < self.total_items)
        {
            return Err("The local engine returned an invalid review inbox page".to_string());
        }
        let expected_next_offset = (page_end < self.total_items
            && page_end <= PROFILE_REVIEW_INBOX_OFFSET_LIMIT as u64)
            .then_some(page_end);
        if self.next_offset != expected_next_offset {
            return Err(
                "The local engine returned inconsistent review inbox pagination".to_string(),
            );
        }
        if let Some(next_offset) = self.next_offset {
            validate_javascript_integer(next_offset, "review inbox next offset")?;
            if next_offset > PROFILE_REVIEW_INBOX_OFFSET_LIMIT as u64 {
                return Err(
                    "The local engine returned an invalid review inbox next offset".to_string(),
                );
            }
        }
        let mut seen_ids = HashSet::new();
        for item in &self.items {
            item.validate(expected_scope)?;
            if !seen_ids.insert(item.id.as_str()) {
                return Err("The local engine returned duplicate review inbox items".to_string());
            }
        }
        validate_profile_review_serialized_size(
            &self,
            PROFILE_REVIEW_INBOX_RESPONSE_LIMIT_BYTES,
            "review inbox page",
        )?;
        Ok(self)
    }
}

impl ProfileReviewInboxApplyReceipt {
    fn validated(
        self,
        expected_item_id: &str,
        expected_state_revision: u64,
        expected_parent_profile_version_id: &str,
        expected_request_id: &str,
        expected_candidate: &ProfileReviewInboxCandidate,
        expected_operation_kind: ProfileReviewInboxOperationKind,
    ) -> Result<Self, String> {
        validate_uuid_receipt(&self.request_id)?;
        validate_uuid_receipt(&self.review_item_id)?;
        validate_uuid_receipt(&self.profile_version_id)?;
        validate_uuid_receipt(&self.parent_profile_version_id)?;
        let expected_result_revision = expected_state_revision
            .checked_add(1)
            .ok_or_else(|| "The review item state revision exceeds the local limit".to_string())?;
        if self.request_id != expected_request_id
            || self.review_item_id != expected_item_id
            || self.previous_state != ProfileReviewInboxState::Inbox
            || self.state != ProfileReviewInboxState::Applied
            || self.state_revision != expected_result_revision
            || self.parent_profile_version_id != expected_parent_profile_version_id
            || self.profile_version_id == self.parent_profile_version_id
            || self.version_number < 2
            || self.candidate_kind != expected_candidate.kind()
            || self.operation_kind != expected_operation_kind
            || self.entry_index >= PROFILE_SECTION_ENTRY_LIMIT
            || self.changed_fields.is_empty()
            || self.changed_fields.len() > PROFILE_REVIEW_INBOX_CHANGED_FIELD_LIMIT
            || self.created_at_ms == 0
        {
            return Err(
                "The local engine returned a mismatched review item apply receipt".to_string(),
            );
        }
        for (value, field_name) in [
            (self.state_revision, "review item state revision"),
            (self.version_number, "profile version"),
            (self.entry_index, "profile section entry index"),
            (self.created_at_ms, "review item apply timestamp"),
        ] {
            validate_javascript_integer(value, field_name)?;
        }
        let mut previous_field_order = None;
        for field in &self.changed_fields {
            let field_order = self
                .candidate_kind
                .changed_field_order(*field)
                .ok_or_else(|| {
                    "The local engine returned invalid review item apply fields".to_string()
                })?;
            if previous_field_order.is_some_and(|previous| field_order <= previous) {
                return Err(
                    "The local engine returned invalid review item apply fields".to_string()
                );
            }
            previous_field_order = Some(field_order);
        }
        validate_profile_review_serialized_size(
            &self,
            PROFILE_REVIEW_INBOX_APPLY_RECEIPT_LIMIT_BYTES,
            "review item apply receipt",
        )?;
        Ok(self)
    }
}

impl ProfileReviewInboxTransitionReceipt {
    fn validated(
        self,
        expected_item_id: &str,
        expected_state: ProfileReviewInboxState,
        expected_state_revision: u64,
        expected_request_id: &str,
        expected_action: ProfileReviewInboxAction,
    ) -> Result<Self, String> {
        validate_uuid_receipt(&self.request_id)?;
        validate_uuid_receipt(&self.review_item_id)?;
        validate_javascript_integer(self.state_revision, "review item state revision")?;
        validate_javascript_integer(self.created_at_ms, "review item transition timestamp")?;
        let expected_result_state = expected_action.resulting_state(expected_state)?;
        let expected_result_revision = expected_state_revision
            .checked_add(1)
            .ok_or_else(|| "The review item state revision exceeds the local limit".to_string())?;
        if self.request_id != expected_request_id
            || self.review_item_id != expected_item_id
            || self.action != expected_action
            || self.previous_state != expected_state
            || self.state != expected_result_state
            || self.state_revision != expected_result_revision
            || self.created_at_ms == 0
        {
            return Err("The local engine returned a mismatched review item receipt".to_string());
        }
        validate_profile_review_serialized_size(
            &self,
            PROFILE_REVIEW_INBOX_TRANSITION_LIMIT_BYTES,
            "review item transition receipt",
        )?;
        Ok(self)
    }
}

fn profile_review_inbox_list_params(
    scope: &str,
    offset: usize,
) -> Result<(ProfileReviewInboxScope, Value), String> {
    let scope = ProfileReviewInboxScope::parse(scope)?;
    if offset > PROFILE_REVIEW_INBOX_OFFSET_LIMIT {
        return Err("The review inbox offset exceeds the local limit".to_string());
    }
    let params = json!({
        "scope": scope,
        "offset": offset,
    });
    Ok((scope, params))
}

fn validated_profile_review_item_id(value: &str) -> Result<String, String> {
    let parsed = Uuid::parse_str(value).map_err(|_| "The review item ID is invalid".to_string())?;
    let normalized = parsed.to_string();
    if normalized != value {
        return Err("The review item ID is invalid".to_string());
    }
    Ok(normalized)
}

fn validated_profile_review_transition_request_id(value: &str) -> Result<String, String> {
    let parsed =
        Uuid::parse_str(value).map_err(|_| "The review item request ID is invalid".to_string())?;
    let normalized = parsed.to_string();
    if normalized != value {
        return Err("The review item request ID is invalid".to_string());
    }
    Ok(normalized)
}

fn profile_review_inbox_transition_params(
    review_item_id: &str,
    expected_state: &str,
    expected_state_revision: u64,
    request_id: &str,
    action: &str,
) -> Result<
    (
        String,
        ProfileReviewInboxState,
        u64,
        String,
        ProfileReviewInboxAction,
        Value,
    ),
    String,
> {
    let review_item_id = validated_profile_review_item_id(review_item_id)?;
    let expected_state = ProfileReviewInboxState::parse(expected_state)?;
    if expected_state_revision >= JAVASCRIPT_MAX_SAFE_INTEGER {
        return Err("The review item state revision is invalid".to_string());
    }
    let request_id = validated_profile_review_transition_request_id(request_id)?;
    let action = ProfileReviewInboxAction::parse(action)?;
    action.resulting_state(expected_state)?;
    let params = json!({
        "review_item_id": review_item_id,
        "expected_state": expected_state,
        "expected_state_revision": expected_state_revision,
        "request_id": request_id,
        "action": action,
    });
    Ok((
        review_item_id,
        expected_state,
        expected_state_revision,
        request_id,
        action,
        params,
    ))
}

fn profile_review_inbox_apply_params(
    review_item_id: &str,
    expected_state: &str,
    expected_state_revision: u64,
    expected_parent_profile_version_id: &str,
    request_id: &str,
    expected_operation_kind: &str,
    candidate: ProfileReviewInboxCandidate,
) -> Result<ValidatedProfileReviewInboxApplyRequest, String> {
    let review_item_id = validated_profile_review_item_id(review_item_id)?;
    if ProfileReviewInboxState::parse(expected_state)? != ProfileReviewInboxState::Inbox {
        return Err("Only an inbox review item can be applied".to_string());
    }
    if expected_state_revision >= JAVASCRIPT_MAX_SAFE_INTEGER {
        return Err("The review item state revision is invalid".to_string());
    }
    let expected_parent_profile_version_id =
        validated_profile_review_version_id(expected_parent_profile_version_id)?;
    let request_id = validated_profile_review_transition_request_id(request_id)?;
    let expected_operation_kind = ProfileReviewInboxOperationKind::parse(expected_operation_kind)?;
    let candidate = candidate.validated_request()?;
    let params = json!({
        "review_item_id": review_item_id,
        "request_id": request_id,
        "expected_state": ProfileReviewInboxState::Inbox,
        "expected_state_revision": expected_state_revision,
        "expected_parent_profile_version_id": expected_parent_profile_version_id,
        "candidate": candidate,
    });
    validate_profile_review_serialized_size(
        &params,
        PROFILE_REVIEW_INBOX_APPLY_REQUEST_LIMIT_BYTES,
        "review item apply request",
    )?;
    Ok(ValidatedProfileReviewInboxApplyRequest {
        review_item_id,
        expected_state_revision,
        expected_parent_profile_version_id,
        request_id,
        expected_operation_kind,
        candidate,
        params,
    })
}

impl ProfileReviewSectionKey {
    fn parse(value: &str) -> Result<Self, String> {
        match value {
            "work" => Ok(Self::Work),
            "education" => Ok(Self::Education),
            "skills" => Ok(Self::Skills),
            "projects" => Ok(Self::Projects),
            "volunteer" => Ok(Self::Volunteer),
            "certificates" => Ok(Self::Certificates),
            "awards" => Ok(Self::Awards),
            "publications" => Ok(Self::Publications),
            "languages" => Ok(Self::Languages),
            "interests" => Ok(Self::Interests),
            "references" => Ok(Self::References),
            _ => Err("The profile review section is invalid".to_string()),
        }
    }

    fn label(self) -> &'static str {
        match self {
            Self::Work => "Work history",
            Self::Education => "Education",
            Self::Skills => "Skills",
            Self::Projects => "Projects & experiences",
            Self::Volunteer => "Volunteer work",
            Self::Certificates => "Certificates",
            Self::Awards => "Awards",
            Self::Publications => "Publications",
            Self::Languages => "Languages",
            Self::Interests => "Interests",
            Self::References => "References",
        }
    }

    fn order(self) -> u8 {
        match self {
            Self::Work => 0,
            Self::Education => 1,
            Self::Skills => 2,
            Self::Projects => 3,
            Self::Volunteer => 4,
            Self::Certificates => 5,
            Self::Awards => 6,
            Self::Publications => 7,
            Self::Languages => 8,
            Self::Interests => 9,
            Self::References => 10,
        }
    }
}

impl ProfileReviewContactKind {
    fn order(self) -> u8 {
        match self {
            Self::Email => 0,
            Self::Phone => 1,
            Self::Location => 2,
            Self::Website => 3,
            Self::Profile => 4,
        }
    }

    fn fixed_label(self) -> Option<&'static str> {
        match self {
            Self::Email => Some("Email"),
            Self::Phone => Some("Phone"),
            Self::Location => Some("Location"),
            Self::Website => Some("Website"),
            Self::Profile => None,
        }
    }
}

fn validate_profile_review_text(
    value: &str,
    field_name: &str,
    maximum_chars: usize,
    maximum_bytes: usize,
) -> Result<(), String> {
    if value.trim().is_empty()
        || value.chars().count() > maximum_chars
        || value.len() > maximum_bytes
        || value.chars().any(char::is_control)
    {
        return Err(format!(
            "The local engine returned an invalid profile review {field_name}"
        ));
    }
    Ok(())
}

fn validate_optional_profile_review_text(
    value: Option<&str>,
    field_name: &str,
    maximum_chars: usize,
    maximum_bytes: usize,
) -> Result<(), String> {
    if let Some(value) = value {
        validate_profile_review_text(value, field_name, maximum_chars, maximum_bytes)?;
    }
    Ok(())
}

fn validate_profile_review_serialized_size<T: Serialize>(
    value: &T,
    maximum_bytes: usize,
    payload_name: &str,
) -> Result<(), String> {
    let encoded = serde_json::to_vec(value)
        .map_err(|_| format!("The local engine returned an invalid {payload_name}"))?;
    if encoded.len() > maximum_bytes {
        return Err(format!(
            "The local engine returned an oversized {payload_name}"
        ));
    }
    Ok(())
}

impl ProfileReviewContact {
    fn validate(&self) -> Result<(), String> {
        validate_profile_review_text(
            &self.label,
            "contact label",
            PROFILE_REVIEW_CONTACT_LABEL_LIMIT_CHARS,
            PROFILE_REVIEW_CONTACT_LABEL_LIMIT_CHARS * 4,
        )?;
        validate_profile_review_text(
            &self.value,
            "contact value",
            PROFILE_REVIEW_CONTACT_VALUE_LIMIT_CHARS,
            PROFILE_REVIEW_CONTACT_VALUE_LIMIT_CHARS * 4,
        )?;
        if let Some(expected_label) = self.kind.fixed_label()
            && self.label != expected_label
        {
            return Err(
                "The local engine returned an inconsistent profile review contact".to_string(),
            );
        }
        Ok(())
    }
}

impl ProfileReviewSummary {
    fn validated(self) -> Result<Self, String> {
        validate_uuid_receipt(&self.profile_version_id)?;
        for (value, field_name) in [
            (self.version_number, "profile review version"),
            (self.created_at_ms, "profile review timestamp"),
        ] {
            validate_javascript_integer(value, field_name)?;
            if value == 0 {
                return Err(format!("The local engine returned an invalid {field_name}"));
            }
        }
        validate_profile_review_text(
            &self.name,
            "name",
            PROFILE_REVIEW_NAME_LIMIT_CHARS,
            PROFILE_REVIEW_NAME_LIMIT_CHARS * 4,
        )?;
        validate_optional_profile_review_text(
            self.headline.as_deref(),
            "headline",
            PROFILE_REVIEW_HEADLINE_LIMIT_CHARS,
            PROFILE_REVIEW_HEADLINE_LIMIT_CHARS * 4,
        )?;
        validate_optional_profile_review_text(
            self.summary.as_deref(),
            "summary",
            PROFILE_REVIEW_SUMMARY_TEXT_LIMIT_CHARS,
            PROFILE_REVIEW_SUMMARY_TEXT_LIMIT_CHARS * 4,
        )?;

        if self.contacts.len() > PROFILE_REVIEW_CONTACT_LIMIT {
            return Err("The local engine returned too many profile review contacts".to_string());
        }
        let mut seen_singleton_kinds = HashSet::new();
        let mut previous_contact_order = None;
        for contact in &self.contacts {
            contact.validate()?;
            let order = contact.kind.order();
            if previous_contact_order.is_some_and(|previous| order < previous) {
                return Err(
                    "The local engine returned profile review contacts out of order".to_string(),
                );
            }
            previous_contact_order = Some(order);
            if contact.kind != ProfileReviewContactKind::Profile
                && !seen_singleton_kinds.insert(contact.kind)
            {
                return Err(
                    "The local engine returned duplicate profile review contacts".to_string(),
                );
            }
        }

        if self.sections.len() != PROFILE_REVIEW_SECTION_COUNT {
            return Err(
                "The local engine returned an incomplete profile review outline".to_string(),
            );
        }
        for (section, (expected_key, expected_label)) in
            self.sections.iter().zip(PROFILE_REVIEW_SECTION_SPECS)
        {
            validate_javascript_integer(section.count, "profile review section count")?;
            if section.key != expected_key
                || section.label != expected_label
                || section.count > PROFILE_REVIEW_SECTION_ITEM_LIMIT
            {
                return Err(
                    "The local engine returned an inconsistent profile review outline".to_string(),
                );
            }
        }
        validate_profile_review_serialized_size(
            &self,
            PROFILE_REVIEW_SUMMARY_LIMIT_BYTES,
            "profile review summary",
        )?;
        Ok(self)
    }
}

impl ProfileReviewDetail {
    fn validate(&self) -> Result<(), String> {
        validate_profile_review_text(
            &self.label,
            "detail label",
            PROFILE_REVIEW_DETAIL_LABEL_LIMIT_CHARS,
            PROFILE_REVIEW_DETAIL_LABEL_LIMIT_CHARS * 4,
        )?;
        validate_profile_review_text(
            &self.value,
            "detail value",
            PROFILE_REVIEW_DETAIL_VALUE_LIMIT_CHARS,
            PROFILE_REVIEW_DETAIL_VALUE_LIMIT_CHARS * 4,
        )
    }
}

impl ProfileReviewItem {
    fn validate(&self, expected_ordinal: u64) -> Result<(), String> {
        validate_javascript_integer(self.ordinal, "profile review item ordinal")?;
        if self.ordinal != expected_ordinal {
            return Err(
                "The local engine returned an inconsistent profile review item".to_string(),
            );
        }
        validate_profile_review_text(
            &self.title,
            "item title",
            PROFILE_REVIEW_ITEM_TITLE_LIMIT_CHARS,
            PROFILE_REVIEW_ITEM_TITLE_LIMIT_CHARS * 4,
        )?;
        validate_optional_profile_review_text(
            self.subtitle.as_deref(),
            "item subtitle",
            PROFILE_REVIEW_ITEM_SUBTITLE_LIMIT_CHARS,
            PROFILE_REVIEW_ITEM_SUBTITLE_LIMIT_CHARS * 4,
        )?;
        validate_optional_profile_review_text(
            self.date_range.as_deref(),
            "item date range",
            PROFILE_REVIEW_ITEM_DATE_LIMIT_CHARS,
            PROFILE_REVIEW_ITEM_DATE_LIMIT_CHARS * 4,
        )?;
        validate_optional_profile_review_text(
            self.location.as_deref(),
            "item location",
            PROFILE_REVIEW_ITEM_LOCATION_LIMIT_CHARS,
            PROFILE_REVIEW_ITEM_LOCATION_LIMIT_CHARS * 4,
        )?;
        validate_optional_profile_review_text(
            self.summary.as_deref(),
            "item summary",
            PROFILE_REVIEW_SUMMARY_TEXT_LIMIT_CHARS,
            PROFILE_REVIEW_SUMMARY_TEXT_LIMIT_CHARS * 4,
        )?;
        if self.highlights.len() > PROFILE_REVIEW_HIGHLIGHT_LIMIT
            || self.tags.len() > PROFILE_REVIEW_TAG_LIMIT
            || self.details.len() > PROFILE_REVIEW_DETAIL_LIMIT
        {
            return Err("The local engine returned an oversized profile review item".to_string());
        }
        for highlight in &self.highlights {
            validate_profile_review_text(
                highlight,
                "item highlight",
                PROFILE_REVIEW_HIGHLIGHT_LIMIT_CHARS,
                PROFILE_REVIEW_HIGHLIGHT_LIMIT_CHARS * 4,
            )?;
        }
        for tag in &self.tags {
            validate_profile_review_text(
                tag,
                "item tag",
                PROFILE_REVIEW_TAG_LIMIT_CHARS,
                PROFILE_REVIEW_TAG_LIMIT_CHARS * 4,
            )?;
        }
        for detail in &self.details {
            detail.validate()?;
        }
        Ok(())
    }
}

impl ProfileReviewSectionPage {
    fn validated(
        self,
        expected_profile_version_id: &str,
        expected_key: ProfileReviewSectionKey,
        expected_offset: usize,
    ) -> Result<Self, String> {
        validate_uuid_receipt(&self.profile_version_id)?;
        if self.profile_version_id != expected_profile_version_id
            || self.key != expected_key
            || self.label != expected_key.label()
            || self.offset != expected_offset as u64
            || self.limit != PROFILE_REVIEW_PAGE_LIMIT as u64
        {
            return Err("The local engine returned a mismatched profile review page".to_string());
        }
        for (value, field_name) in [
            (self.total_items, "profile review item count"),
            (self.offset, "profile review offset"),
            (self.limit, "profile review limit"),
        ] {
            validate_javascript_integer(value, field_name)?;
        }
        if self.total_items > PROFILE_REVIEW_SECTION_ITEM_LIMIT
            || self.items.len() > PROFILE_REVIEW_PAGE_LIMIT
        {
            return Err("The local engine returned an oversized profile review page".to_string());
        }
        let item_count = self.items.len() as u64;
        let page_end = self.offset.checked_add(item_count).ok_or_else(|| {
            "The local engine returned an invalid profile review page".to_string()
        })?;
        if (!self.items.is_empty()
            && (self.offset >= self.total_items || page_end > self.total_items))
            || (self.items.is_empty() && self.offset < self.total_items)
        {
            return Err("The local engine returned an invalid profile review page".to_string());
        }
        let expected_next_offset = (page_end < self.total_items).then_some(page_end);
        if self.next_offset != expected_next_offset {
            return Err(
                "The local engine returned inconsistent profile review pagination".to_string(),
            );
        }
        if let Some(next_offset) = self.next_offset {
            validate_javascript_integer(next_offset, "profile review next offset")?;
            if next_offset > PROFILE_REVIEW_OFFSET_LIMIT as u64 {
                return Err(
                    "The local engine returned an invalid profile review next offset".to_string(),
                );
            }
        }
        for (index, item) in self.items.iter().enumerate() {
            let expected_ordinal = self.offset.checked_add(index as u64).ok_or_else(|| {
                "The local engine returned an invalid profile review item ordinal".to_string()
            })?;
            item.validate(expected_ordinal)?;
        }
        validate_profile_review_serialized_size(
            &self,
            PROFILE_REVIEW_PAGE_LIMIT_BYTES,
            "profile review page",
        )?;
        Ok(self)
    }
}

fn validated_profile_review_version_id(value: &str) -> Result<String, String> {
    let parsed =
        Uuid::parse_str(value).map_err(|_| "The profile version ID is invalid".to_string())?;
    let normalized = parsed.to_string();
    if normalized != value {
        return Err("The profile version ID is invalid".to_string());
    }
    Ok(normalized)
}

fn validated_profile_review_offset(offset: usize) -> Result<usize, String> {
    if offset > PROFILE_REVIEW_OFFSET_LIMIT {
        return Err("The profile review offset exceeds the local limit".to_string());
    }
    Ok(offset)
}

fn profile_review_section_params(
    profile_version_id: &str,
    section: ProfileReviewSectionKey,
    offset: usize,
) -> Value {
    json!({
        "profile_version_id": profile_version_id,
        "section": section,
        "offset": offset,
    })
}

fn is_profile_format_character(character: char) -> bool {
    matches!(
        character,
        '\u{00ad}'
            | '\u{0600}'..='\u{0605}'
            | '\u{061c}'
            | '\u{06dd}'
            | '\u{070f}'
            | '\u{0890}'..='\u{0891}'
            | '\u{08e2}'
            | '\u{180e}'
            | '\u{200b}'..='\u{200f}'
            | '\u{202a}'..='\u{202e}'
            | '\u{2060}'..='\u{206f}'
            | '\u{feff}'
            | '\u{fff9}'..='\u{fffb}'
            | '\u{110bd}'
            | '\u{110cd}'
            | '\u{13430}'..='\u{1343f}'
            | '\u{1bca0}'..='\u{1bca3}'
            | '\u{1d173}'..='\u{1d17a}'
            | '\u{e0001}'
            | '\u{e0020}'..='\u{e007f}'
    )
}

fn profile_character_is_unsupported(character: char, allow_line_breaks: bool) -> bool {
    (character.is_control() && !(allow_line_breaks && character == '\n'))
        || is_profile_format_character(character)
}

fn validate_profile_basics_text(
    value: &str,
    field_name: &str,
    maximum_chars: usize,
    allow_line_breaks: bool,
) -> Result<(), String> {
    if value.trim().is_empty()
        || value.trim() != value
        || value.chars().count() > maximum_chars
        || value.len() > maximum_chars.saturating_mul(4)
        || value
            .chars()
            .any(|character| profile_character_is_unsupported(character, allow_line_breaks))
    {
        return Err(format!(
            "The local engine returned an invalid profile {field_name}"
        ));
    }
    Ok(())
}

fn validate_optional_profile_basics_text(
    value: Option<&str>,
    field_name: &str,
    maximum_chars: usize,
    allow_line_breaks: bool,
) -> Result<(), String> {
    if let Some(value) = value {
        validate_profile_basics_text(value, field_name, maximum_chars, allow_line_breaks)?;
    }
    Ok(())
}

fn normalized_profile_basics_text(
    value: String,
    field_name: &str,
    maximum_chars: usize,
    allow_line_breaks: bool,
) -> Result<String, String> {
    let normalized = if allow_line_breaks {
        value
            .replace('\u{00a0}', " ")
            .replace("\r\n", "\n")
            .replace('\r', "\n")
            .replace('\t', " ")
            .trim()
            .to_string()
    } else {
        value.replace('\u{00a0}', " ").trim().to_string()
    };
    if normalized.is_empty()
        || normalized.chars().count() > maximum_chars
        || normalized.len() > maximum_chars.saturating_mul(4)
        || normalized
            .chars()
            .any(|character| profile_character_is_unsupported(character, allow_line_breaks))
    {
        return Err(format!(
            "{field_name} is invalid or exceeds the local limit"
        ));
    }
    Ok(normalized)
}

fn normalized_nullable_profile_patch_field(
    field: RevisionPatchField<String>,
    field_name: &str,
    maximum_chars: usize,
    allow_line_breaks: bool,
) -> Result<RevisionPatchField<String>, String> {
    match field {
        RevisionPatchField::Missing => Ok(RevisionPatchField::Missing),
        RevisionPatchField::Null => Ok(RevisionPatchField::Null),
        RevisionPatchField::Value(value) => {
            normalized_profile_basics_text(value, field_name, maximum_chars, allow_line_breaks)
                .map(RevisionPatchField::Value)
        }
    }
}

fn ipv4_is_public(address: Ipv4Addr) -> bool {
    let [first, second, third, _] = address.octets();
    !(address.is_private()
        || address.is_loopback()
        || address.is_link_local()
        || address.is_unspecified()
        || address.is_broadcast()
        || address.is_multicast()
        || first == 0
        || (first == 100 && (64..=127).contains(&second))
        || (first == 192 && second == 0 && third == 0)
        || (first == 192 && second == 0 && third == 2)
        || (first == 198 && (second == 18 || second == 19))
        || (first == 198 && second == 51 && third == 100)
        || (first == 203 && second == 0 && third == 113)
        || first >= 240)
}

fn ipv6_is_in_prefix(address: Ipv6Addr, network: Ipv6Addr, prefix_length: u32) -> bool {
    debug_assert!((1..=128).contains(&prefix_length));
    let shift = 128_u32 - prefix_length;
    u128::from(address) >> shift == u128::from(network) >> shift
}

fn ipv6_is_public(address: Ipv6Addr) -> bool {
    if let Some(mapped) = address.to_ipv4() {
        return ipv4_is_public(mapped);
    }
    let segments = address.segments();
    let special_2001_exception = address == Ipv6Addr::new(0x2001, 0x0001, 0, 0, 0, 0, 0, 1)
        || address == Ipv6Addr::new(0x2001, 0x0001, 0, 0, 0, 0, 0, 2)
        || ipv6_is_in_prefix(address, Ipv6Addr::new(0x2001, 0x0003, 0, 0, 0, 0, 0, 0), 32)
        || ipv6_is_in_prefix(
            address,
            Ipv6Addr::new(0x2001, 0x0004, 0x0112, 0, 0, 0, 0, 0),
            48,
        )
        || ipv6_is_in_prefix(address, Ipv6Addr::new(0x2001, 0x0020, 0, 0, 0, 0, 0, 0), 28)
        || ipv6_is_in_prefix(address, Ipv6Addr::new(0x2001, 0x0030, 0, 0, 0, 0, 0, 0), 28);
    !(address.is_loopback()
        || address.is_unspecified()
        || address.is_multicast()
        || ipv6_is_in_prefix(
            address,
            Ipv6Addr::new(0x0064, 0xff9b, 0x0001, 0, 0, 0, 0, 0),
            48,
        )
        || ipv6_is_in_prefix(address, Ipv6Addr::new(0x0100, 0, 0, 0, 0, 0, 0, 0), 64)
        || (ipv6_is_in_prefix(address, Ipv6Addr::new(0x2001, 0, 0, 0, 0, 0, 0, 0), 23)
            && !special_2001_exception)
        || ipv6_is_in_prefix(address, Ipv6Addr::new(0x2001, 0x0db8, 0, 0, 0, 0, 0, 0), 32)
        || ipv6_is_in_prefix(address, Ipv6Addr::new(0x2002, 0, 0, 0, 0, 0, 0, 0), 16)
        || ipv6_is_in_prefix(address, Ipv6Addr::new(0x3fff, 0, 0, 0, 0, 0, 0, 0), 20)
        || segments[0] & 0xfe00 == 0xfc00
        || segments[0] & 0xffc0 == 0xfe80)
}

fn validate_public_profile_url(value: &str, field_name: &str) -> Result<(), String> {
    if value.trim() != value
        || value.is_empty()
        || value.chars().count() > PROFILE_BASICS_URL_LIMIT_CHARS
        || value.len() > PROFILE_BASICS_URL_LIMIT_CHARS.saturating_mul(4)
        || value.chars().any(|character| {
            character.is_whitespace() || profile_character_is_unsupported(character, false)
        })
        || value.contains('\\')
    {
        return Err(format!("{field_name} must be a public HTTP or HTTPS URL"));
    }
    let parsed = Url::parse(value)
        .map_err(|_| format!("{field_name} must be a public HTTP or HTTPS URL"))?;
    let authority = value
        .split_once("://")
        .map(|(_, remainder)| remainder)
        .and_then(|remainder| remainder.split(['/', '?', '#']).next())
        .unwrap_or_default();
    if !matches!(parsed.scheme(), "http" | "https")
        || parsed.host_str().is_none()
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || authority.contains('@')
    {
        return Err(format!(
            "{field_name} must be a credential-free public HTTP or HTTPS URL"
        ));
    }
    let host = parsed.host_str().unwrap_or_default().trim_end_matches('.');
    let normalized_host = host.to_ascii_lowercase();
    if normalized_host.is_empty()
        || [
            "localhost",
            "localhost.localdomain",
            "local",
            "internal",
            "home",
            "lan",
            "invalid",
            "test",
            "example",
        ]
        .iter()
        .any(|suffix| {
            normalized_host == *suffix || normalized_host.ends_with(&format!(".{suffix}"))
        })
    {
        return Err(format!("{field_name} must use a public network host"));
    }
    let ip_host = normalized_host
        .strip_prefix('[')
        .and_then(|host| host.strip_suffix(']'))
        .unwrap_or(&normalized_host);
    if let Ok(address) = ip_host.parse::<IpAddr>() {
        let public = match address {
            IpAddr::V4(address) => ipv4_is_public(address),
            IpAddr::V6(address) => ipv6_is_public(address),
        };
        if !public {
            return Err(format!("{field_name} must use a public network host"));
        }
    } else {
        let labels = normalized_host.split('.').collect::<Vec<_>>();
        if normalized_host.len() > 253
            || labels.len() < 2
            || normalized_host
                .bytes()
                .all(|byte| byte.is_ascii_digit() || byte == b'.')
            || labels.iter().any(|label| {
                label.is_empty()
                    || label.len() > 63
                    || !label
                        .bytes()
                        .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
                    || !label
                        .as_bytes()
                        .first()
                        .is_some_and(u8::is_ascii_alphanumeric)
                    || !label
                        .as_bytes()
                        .last()
                        .is_some_and(u8::is_ascii_alphanumeric)
            })
        {
            return Err(format!("{field_name} must use a public network host"));
        }
    }
    Ok(())
}

fn validate_profile_update_email(value: &str) -> Result<(), String> {
    let Some((local, domain)) = value.rsplit_once('@') else {
        return Err("Email must be a valid address".to_string());
    };
    if local.is_empty()
        || local.contains('@')
        || value.chars().any(char::is_whitespace)
        || local.starts_with('.')
        || local.ends_with('.')
        || local.contains("..")
        || domain.is_empty()
    {
        return Err("Email must be a valid address".to_string());
    }
    let domain = domain.strip_suffix('.').unwrap_or(domain);
    if domain.is_empty()
        || domain.ends_with('.')
        || domain.chars().any(|character| {
            character.is_ascii()
                && !(character.is_ascii_alphanumeric() || matches!(character, '-' | '.'))
        })
    {
        return Err("Email must be a valid address".to_string());
    }
    let parsed = Url::parse(&format!("http://{domain}"))
        .map_err(|_| "Email must be a valid address".to_string())?;
    let host = parsed.host_str().unwrap_or_default();
    let labels = host.split('.').collect::<Vec<_>>();
    if host.is_empty()
        || host.len() > 253
        || labels.len() < 2
        || parsed.path() != "/"
        || parsed.query().is_some()
        || parsed.fragment().is_some()
        || parsed.port().is_some()
        || labels.iter().any(|label| {
            label.is_empty()
                || label.len() > 63
                || !label
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
                || !label
                    .as_bytes()
                    .first()
                    .is_some_and(u8::is_ascii_alphanumeric)
                || !label
                    .as_bytes()
                    .last()
                    .is_some_and(u8::is_ascii_alphanumeric)
        })
    {
        return Err("Email must be a valid address".to_string());
    }
    Ok(())
}

fn validate_profile_update_phone(value: &str) -> Result<(), String> {
    if value
        .chars()
        .filter(|character| character.is_numeric())
        .count()
        < 3
    {
        return Err("Phone must contain a usable number".to_string());
    }
    Ok(())
}

impl ProfileBasicField {
    fn order(self) -> u8 {
        match self {
            Self::Name => 0,
            Self::Headline => 1,
            Self::Summary => 2,
            Self::Email => 3,
            Self::Phone => 4,
            Self::Url => 5,
            Self::LocationCity => 6,
            Self::LocationRegion => 7,
            Self::LocationCountryCode => 8,
        }
    }

    fn label(self) -> &'static str {
        match self {
            Self::Name => "Name",
            Self::Headline => "Headline",
            Self::Summary => "Summary",
            Self::Email => "Email",
            Self::Phone => "Phone",
            Self::Url => "Website",
            Self::LocationCity => "City",
            Self::LocationRegion => "Region",
            Self::LocationCountryCode => "Country",
        }
    }
}

impl ProfileVersionSummary {
    fn validate(&self) -> Result<(), String> {
        validate_uuid_receipt(&self.profile_version_id)?;
        if let Some(parent_profile_version_id) = &self.parent_profile_version_id {
            validate_uuid_receipt(parent_profile_version_id)?;
            if parent_profile_version_id == &self.profile_version_id {
                return Err("The local engine returned an invalid profile parent".to_string());
            }
        }
        if self.version_number == 0
            || self.created_at_ms == 0
            || (self.version_number == 1) != self.parent_profile_version_id.is_none()
        {
            return Err("The local engine returned an invalid profile version summary".to_string());
        }
        validate_javascript_integer(self.version_number, "profile version")?;
        validate_javascript_integer(self.created_at_ms, "profile version timestamp")?;
        validate_profile_basics_text(&self.name, "name", PROFILE_REVIEW_NAME_LIMIT_CHARS, false)?;
        validate_optional_profile_basics_text(
            self.headline.as_deref(),
            "headline",
            PROFILE_REVIEW_HEADLINE_LIMIT_CHARS,
            false,
        )
    }
}

impl ProfileVersionList {
    fn validated(self, expected_offset: usize) -> Result<Self, String> {
        if self.offset != expected_offset as u64
            || self.limit != PROFILE_VERSION_LIST_LIMIT as u64
            || self.items.len() > PROFILE_VERSION_LIST_LIMIT
        {
            return Err("The local engine returned a mismatched profile version page".to_string());
        }
        for (value, field_name) in [
            (self.total_items, "profile version count"),
            (self.offset, "profile version offset"),
            (self.limit, "profile version limit"),
        ] {
            validate_javascript_integer(value, field_name)?;
        }
        match (&self.current_profile_version_id, self.total_items) {
            (None, 0) => {}
            (Some(current), total) if total > 0 => validate_uuid_receipt(current)?,
            _ => {
                return Err(
                    "The local engine returned an invalid current profile version".to_string(),
                );
            }
        }
        let item_count = self.items.len() as u64;
        let page_end = self.offset.checked_add(item_count).ok_or_else(|| {
            "The local engine returned an invalid profile version page".to_string()
        })?;
        if (!self.items.is_empty()
            && (self.offset >= self.total_items || page_end > self.total_items))
            || (self.items.is_empty() && self.offset < self.total_items)
            || (item_count < self.limit && page_end < self.total_items)
        {
            return Err("The local engine returned an invalid profile version page".to_string());
        }
        let expected_next_offset = (page_end < self.total_items
            && page_end <= PROFILE_VERSION_LIST_OFFSET_LIMIT as u64)
            .then_some(page_end);
        if self.next_offset != expected_next_offset {
            return Err(
                "The local engine returned inconsistent profile version pagination".to_string(),
            );
        }
        if let Some(next_offset) = self.next_offset {
            validate_javascript_integer(next_offset, "profile version next offset")?;
            if next_offset > PROFILE_VERSION_LIST_OFFSET_LIMIT as u64 {
                return Err(
                    "The local engine returned an invalid profile version next offset".to_string(),
                );
            }
        }
        let mut identifiers = HashSet::new();
        for (index, item) in self.items.iter().enumerate() {
            item.validate()?;
            let expected_version = self
                .total_items
                .checked_sub(self.offset + index as u64)
                .ok_or_else(|| {
                    "The local engine returned an invalid profile version order".to_string()
                })?;
            if item.version_number != expected_version
                || !identifiers.insert(item.profile_version_id.as_str())
            {
                return Err(
                    "The local engine returned an invalid profile version order".to_string()
                );
            }
        }
        if self.offset == 0
            && self
                .items
                .first()
                .map(|item| item.profile_version_id.as_str())
                != self.current_profile_version_id.as_deref()
        {
            return Err("The local engine returned an inconsistent current profile".to_string());
        }
        validate_profile_review_serialized_size(
            &self,
            PROFILE_VERSION_LIST_LIMIT_BYTES,
            "profile version page",
        )?;
        Ok(self)
    }
}

impl ProfileRestoreReceipt {
    fn validated(
        self,
        expected_source_profile_version_id: &str,
        expected_parent_profile_version_id: &str,
        expected_request_id: &str,
    ) -> Result<Self, String> {
        for identifier in [
            &self.request_id,
            &self.profile_version_id,
            &self.parent_profile_version_id,
            &self.restored_from_profile_version_id,
        ] {
            validate_uuid_receipt(identifier)?;
        }
        if self.request_id != expected_request_id
            || self.parent_profile_version_id != expected_parent_profile_version_id
            || self.restored_from_profile_version_id != expected_source_profile_version_id
            || self.parent_profile_version_id == self.restored_from_profile_version_id
            || self.profile_version_id == self.parent_profile_version_id
            || self.profile_version_id == self.restored_from_profile_version_id
            || self.version_number < 2
            || self.restored_from_version_number == 0
            || self.restored_from_version_number >= self.version_number
            || self.created_at_ms == 0
        {
            return Err(
                "The local engine returned a mismatched profile restore receipt".to_string(),
            );
        }
        for (value, field_name) in [
            (
                self.restored_from_version_number,
                "restored profile version",
            ),
            (self.version_number, "profile version"),
            (self.created_at_ms, "profile version timestamp"),
        ] {
            validate_javascript_integer(value, field_name)?;
        }
        validate_profile_basics_text(
            &self.profile_name,
            "name",
            PROFILE_REVIEW_NAME_LIMIT_CHARS,
            false,
        )?;
        validate_profile_review_serialized_size(
            &self,
            PROFILE_RESTORE_RECEIPT_LIMIT_BYTES,
            "profile restore receipt",
        )?;
        Ok(self)
    }
}

impl ProfileBasicsLocation {
    fn validate(&self) -> Result<(), String> {
        validate_optional_profile_basics_text(
            self.city.as_deref(),
            "city",
            PROFILE_BASICS_LOCATION_LIMIT_CHARS,
            false,
        )?;
        validate_optional_profile_basics_text(
            self.region.as_deref(),
            "region",
            PROFILE_BASICS_LOCATION_LIMIT_CHARS,
            false,
        )?;
        if let Some(country_code) = &self.country_code
            && (country_code.len() != PROFILE_BASICS_COUNTRY_CODE_LIMIT_CHARS
                || !country_code.bytes().all(|byte| byte.is_ascii_uppercase()))
        {
            return Err("The local engine returned an invalid profile country code".to_string());
        }
        Ok(())
    }
}

impl ProfileBasics {
    fn validated(self, expected_profile_version_id: &str) -> Result<Self, String> {
        validate_uuid_receipt(&self.profile_version_id)?;
        if self.profile_version_id != expected_profile_version_id
            || self.version_number == 0
            || self.created_at_ms == 0
        {
            return Err("The local engine returned mismatched profile basics".to_string());
        }
        validate_javascript_integer(self.version_number, "profile version")?;
        validate_javascript_integer(self.created_at_ms, "profile version timestamp")?;
        if !self.name.is_empty() {
            validate_profile_basics_text(
                &self.name,
                "name",
                PROFILE_REVIEW_NAME_LIMIT_CHARS,
                false,
            )?;
        }
        validate_optional_profile_basics_text(
            self.headline.as_deref(),
            "headline",
            PROFILE_REVIEW_HEADLINE_LIMIT_CHARS,
            false,
        )?;
        validate_optional_profile_basics_text(
            self.summary.as_deref(),
            "summary",
            PROFILE_BASICS_SUMMARY_LIMIT_CHARS,
            true,
        )?;
        validate_optional_profile_basics_text(
            self.email.as_deref(),
            "email",
            PROFILE_BASICS_EMAIL_LIMIT_CHARS,
            false,
        )?;
        validate_optional_profile_basics_text(
            self.phone.as_deref(),
            "phone",
            PROFILE_BASICS_PHONE_LIMIT_CHARS,
            false,
        )?;
        if let Some(url) = &self.url {
            validate_public_profile_url(url, "Profile website")?;
        }
        self.location.validate()?;
        validate_profile_review_serialized_size(
            &self,
            PROFILE_REVIEW_SUMMARY_LIMIT_BYTES,
            "profile basics",
        )?;
        Ok(self)
    }
}

fn validate_profile_basic_change_value(
    field: ProfileBasicField,
    value: Option<&str>,
) -> Result<(), String> {
    let Some(value) = value else {
        if field == ProfileBasicField::Name {
            return Err("The local engine returned an invalid profile name change".to_string());
        }
        return Ok(());
    };
    if field == ProfileBasicField::Name && value.is_empty() {
        return Ok(());
    }
    match field {
        ProfileBasicField::Name => validate_profile_basics_text(
            value,
            "name change",
            PROFILE_REVIEW_NAME_LIMIT_CHARS,
            false,
        ),
        ProfileBasicField::Headline => validate_profile_basics_text(
            value,
            "headline change",
            PROFILE_REVIEW_HEADLINE_LIMIT_CHARS,
            false,
        ),
        ProfileBasicField::Summary => validate_profile_basics_text(
            value,
            "summary change",
            PROFILE_BASICS_SUMMARY_LIMIT_CHARS,
            true,
        ),
        ProfileBasicField::Email => validate_profile_basics_text(
            value,
            "email change",
            PROFILE_BASICS_EMAIL_LIMIT_CHARS,
            false,
        ),
        ProfileBasicField::Phone => validate_profile_basics_text(
            value,
            "phone change",
            PROFILE_BASICS_PHONE_LIMIT_CHARS,
            false,
        ),
        ProfileBasicField::Url => validate_public_profile_url(value, "Profile website change"),
        ProfileBasicField::LocationCity | ProfileBasicField::LocationRegion => {
            validate_profile_basics_text(
                value,
                "location change",
                PROFILE_BASICS_LOCATION_LIMIT_CHARS,
                false,
            )
        }
        ProfileBasicField::LocationCountryCode => {
            if value.len() == PROFILE_BASICS_COUNTRY_CODE_LIMIT_CHARS
                && value.bytes().all(|byte| byte.is_ascii_uppercase())
            {
                Ok(())
            } else {
                Err("The local engine returned an invalid country code change".to_string())
            }
        }
    }
}

impl ProfileVersionDiff {
    fn validated(
        self,
        expected_from_profile_version_id: &str,
        expected_to_profile_version_id: &str,
    ) -> Result<Self, String> {
        self.from_version.validate()?;
        self.to_version.validate()?;
        if self.from_version.profile_version_id != expected_from_profile_version_id
            || self.to_version.profile_version_id != expected_to_profile_version_id
            || self.basic_changes.len() > PROFILE_DIFF_BASIC_CHANGE_LIMIT
            || self.section_changes.len() > PROFILE_DIFF_SECTION_CHANGE_LIMIT
        {
            return Err("The local engine returned a mismatched profile difference".to_string());
        }
        let mut previous_basic_order = None;
        for change in &self.basic_changes {
            let order = change.field.order();
            if previous_basic_order.is_some_and(|previous| order <= previous)
                || change.label != change.field.label()
                || change.before == change.after
            {
                return Err("The local engine returned an invalid profile basic change".to_string());
            }
            previous_basic_order = Some(order);
            validate_profile_basic_change_value(change.field, change.before.as_deref())?;
            validate_profile_basic_change_value(change.field, change.after.as_deref())?;
        }
        let mut previous_section_order = None;
        for change in &self.section_changes {
            for (value, field_name) in [
                (change.before_count, "profile difference section count"),
                (change.after_count, "profile difference section count"),
            ] {
                validate_javascript_integer(value, field_name)?;
                if value > PROFILE_REVIEW_SECTION_ITEM_LIMIT {
                    return Err(
                        "The local engine returned an invalid profile section change".to_string(),
                    );
                }
            }
            let order = change.key.order();
            if previous_section_order.is_some_and(|previous| order <= previous)
                || change.label != change.key.label()
                || !change.content_changed
            {
                return Err(
                    "The local engine returned an invalid profile section change".to_string(),
                );
            }
            previous_section_order = Some(order);
        }
        validate_javascript_integer(self.total_changes, "profile difference change count")?;
        if self.total_changes != self.basic_changes.len() as u64 + self.section_changes.len() as u64
            || (self.from_version.profile_version_id == self.to_version.profile_version_id
                && self.total_changes != 0)
        {
            return Err("The local engine returned an inconsistent profile difference".to_string());
        }
        validate_profile_review_serialized_size(
            &self,
            PROFILE_DIFF_LIMIT_BYTES,
            "profile difference",
        )?;
        Ok(self)
    }
}

impl ProfileBasicsLocationPatch {
    fn has_fields(&self) -> bool {
        !self.city.is_missing() || !self.region.is_missing() || !self.country_code.is_missing()
    }

    fn validated(self) -> Result<Self, String> {
        if !self.has_fields() {
            return Err("Location must include at least one field".to_string());
        }
        let city = normalized_nullable_profile_patch_field(
            self.city,
            "City",
            PROFILE_BASICS_LOCATION_LIMIT_CHARS,
            false,
        )?;
        let region = normalized_nullable_profile_patch_field(
            self.region,
            "Region",
            PROFILE_BASICS_LOCATION_LIMIT_CHARS,
            false,
        )?;
        let country_code = match self.country_code {
            RevisionPatchField::Missing => RevisionPatchField::Missing,
            RevisionPatchField::Null => RevisionPatchField::Null,
            RevisionPatchField::Value(value) => {
                let normalized = value.trim().to_ascii_uppercase();
                if normalized.len() != PROFILE_BASICS_COUNTRY_CODE_LIMIT_CHARS
                    || !normalized.bytes().all(|byte| byte.is_ascii_uppercase())
                {
                    return Err("Country code must contain two ASCII letters".to_string());
                }
                RevisionPatchField::Value(normalized)
            }
        };
        Ok(Self {
            city,
            region,
            country_code,
        })
    }
}

impl ProfileBasicsPatch {
    fn has_fields(&self) -> bool {
        !self.name.is_missing()
            || !self.headline.is_missing()
            || !self.summary.is_missing()
            || !self.email.is_missing()
            || !self.phone.is_missing()
            || !self.url.is_missing()
            || !self.location.is_missing()
    }

    fn present_fields(&self) -> HashSet<ProfileBasicField> {
        let mut fields = HashSet::new();
        for (present, field) in [
            (!self.name.is_missing(), ProfileBasicField::Name),
            (!self.headline.is_missing(), ProfileBasicField::Headline),
            (!self.summary.is_missing(), ProfileBasicField::Summary),
            (!self.email.is_missing(), ProfileBasicField::Email),
            (!self.phone.is_missing(), ProfileBasicField::Phone),
            (!self.url.is_missing(), ProfileBasicField::Url),
        ] {
            if present {
                fields.insert(field);
            }
        }
        if let RevisionPatchField::Value(location) = &self.location {
            for (present, field) in [
                (!location.city.is_missing(), ProfileBasicField::LocationCity),
                (
                    !location.region.is_missing(),
                    ProfileBasicField::LocationRegion,
                ),
                (
                    !location.country_code.is_missing(),
                    ProfileBasicField::LocationCountryCode,
                ),
            ] {
                if present {
                    fields.insert(field);
                }
            }
        }
        fields
    }

    fn validated(self) -> Result<Self, String> {
        if !self.has_fields() {
            return Err("The profile update patch must include at least one field".to_string());
        }
        let name = match self.name {
            RevisionPatchField::Missing => RevisionPatchField::Missing,
            RevisionPatchField::Null => return Err("Name cannot be cleared".to_string()),
            RevisionPatchField::Value(value) => {
                RevisionPatchField::Value(normalized_profile_basics_text(
                    value,
                    "Name",
                    PROFILE_REVIEW_NAME_LIMIT_CHARS,
                    false,
                )?)
            }
        };
        let headline = normalized_nullable_profile_patch_field(
            self.headline,
            "Headline",
            PROFILE_REVIEW_HEADLINE_LIMIT_CHARS,
            false,
        )?;
        let summary = normalized_nullable_profile_patch_field(
            self.summary,
            "Summary",
            PROFILE_BASICS_SUMMARY_LIMIT_CHARS,
            true,
        )?;
        let email = normalized_nullable_profile_patch_field(
            self.email,
            "Email",
            PROFILE_BASICS_EMAIL_LIMIT_CHARS,
            false,
        )?;
        if let RevisionPatchField::Value(email) = &email {
            validate_profile_update_email(email)?;
        }
        let phone = normalized_nullable_profile_patch_field(
            self.phone,
            "Phone",
            PROFILE_BASICS_PHONE_LIMIT_CHARS,
            false,
        )?;
        if let RevisionPatchField::Value(phone) = &phone {
            validate_profile_update_phone(phone)?;
        }
        let url = match self.url {
            RevisionPatchField::Missing => RevisionPatchField::Missing,
            RevisionPatchField::Null => RevisionPatchField::Null,
            RevisionPatchField::Value(value) => {
                let normalized = value.trim().to_string();
                validate_public_profile_url(&normalized, "Profile website")?;
                RevisionPatchField::Value(normalized)
            }
        };
        let location = match self.location {
            RevisionPatchField::Missing => RevisionPatchField::Missing,
            RevisionPatchField::Null => {
                return Err("Location must be changed through its individual fields".to_string());
            }
            RevisionPatchField::Value(location) => RevisionPatchField::Value(location.validated()?),
        };
        Ok(Self {
            name,
            headline,
            summary,
            email,
            phone,
            url,
            location,
        })
    }

    fn validated_for_manual_start(self) -> Result<Self, String> {
        let patch = self.validated()?;
        if !matches!(&patch.name, RevisionPatchField::Value(_)) {
            return Err("Name is required to create a profile".to_string());
        }
        Ok(patch)
    }
}

impl ManualProfileStartReceipt {
    fn validated(
        self,
        expected_request_id: &str,
        patch: &ProfileBasicsPatch,
    ) -> Result<Self, String> {
        validate_uuid_receipt(&self.request_id)?;
        validate_uuid_receipt(&self.profile_version_id)?;
        if self.request_id != expected_request_id
            || self.parent_profile_version_id.is_some()
            || self.version_number != 1
            || self.created_at_ms == 0
        {
            return Err(
                "The local engine returned a mismatched manual profile receipt".to_string(),
            );
        }
        validate_javascript_integer(self.version_number, "profile version")?;
        validate_javascript_integer(self.created_at_ms, "profile version timestamp")?;

        let expected_changed_fields = [
            (
                matches!(&patch.name, RevisionPatchField::Value(_)),
                ProfileBasicField::Name,
            ),
            (
                matches!(&patch.headline, RevisionPatchField::Value(_)),
                ProfileBasicField::Headline,
            ),
            (
                matches!(&patch.summary, RevisionPatchField::Value(_)),
                ProfileBasicField::Summary,
            ),
            (
                matches!(&patch.email, RevisionPatchField::Value(_)),
                ProfileBasicField::Email,
            ),
            (
                matches!(&patch.phone, RevisionPatchField::Value(_)),
                ProfileBasicField::Phone,
            ),
            (
                matches!(&patch.url, RevisionPatchField::Value(_)),
                ProfileBasicField::Url,
            ),
        ]
        .into_iter()
        .chain(match &patch.location {
            RevisionPatchField::Value(location) => [
                (
                    matches!(&location.city, RevisionPatchField::Value(_)),
                    ProfileBasicField::LocationCity,
                ),
                (
                    matches!(&location.region, RevisionPatchField::Value(_)),
                    ProfileBasicField::LocationRegion,
                ),
                (
                    matches!(&location.country_code, RevisionPatchField::Value(_)),
                    ProfileBasicField::LocationCountryCode,
                ),
            ],
            RevisionPatchField::Missing | RevisionPatchField::Null => [
                (false, ProfileBasicField::LocationCity),
                (false, ProfileBasicField::LocationRegion),
                (false, ProfileBasicField::LocationCountryCode),
            ],
        })
        .filter_map(|(changed, field)| changed.then_some(field))
        .collect::<Vec<_>>();
        if self.changed_fields != expected_changed_fields {
            return Err("The local engine returned invalid profile changed fields".to_string());
        }

        validate_profile_basics_text(
            &self.profile_name,
            "name",
            PROFILE_REVIEW_NAME_LIMIT_CHARS,
            false,
        )?;
        validate_optional_profile_basics_text(
            self.headline.as_deref(),
            "headline",
            PROFILE_REVIEW_HEADLINE_LIMIT_CHARS,
            false,
        )?;
        let RevisionPatchField::Value(name) = &patch.name else {
            return Err("The manual profile patch did not include a name".to_string());
        };
        if name.is_ascii() && name != &self.profile_name {
            return Err("The local engine returned an inconsistent profile name".to_string());
        }
        match &patch.headline {
            RevisionPatchField::Value(headline)
                if self.headline.is_none()
                    || (headline.is_ascii() && self.headline.as_ref() != Some(headline)) =>
            {
                return Err(
                    "The local engine returned an inconsistent profile headline".to_string()
                );
            }
            RevisionPatchField::Missing | RevisionPatchField::Null if self.headline.is_some() => {
                return Err(
                    "The local engine returned an inconsistent profile headline".to_string()
                );
            }
            _ => {}
        }
        let expected_renderable = matches!(&patch.summary, RevisionPatchField::Value(_));
        if self.renderable != expected_renderable {
            return Err(
                "The local engine returned an inconsistent profile renderability".to_string(),
            );
        }
        validate_profile_review_serialized_size(
            &self,
            MANUAL_PROFILE_START_RECEIPT_LIMIT_BYTES,
            "manual profile receipt",
        )?;
        Ok(self)
    }
}

impl ProfileBasicsUpdateReceipt {
    fn validated(
        self,
        expected_parent_profile_version_id: &str,
        expected_request_id: &str,
        patch: &ProfileBasicsPatch,
    ) -> Result<Self, String> {
        validate_uuid_receipt(&self.request_id)?;
        validate_uuid_receipt(&self.profile_version_id)?;
        validate_uuid_receipt(&self.parent_profile_version_id)?;
        if self.request_id != expected_request_id
            || self.parent_profile_version_id != expected_parent_profile_version_id
            || self.profile_version_id == self.parent_profile_version_id
            || self.version_number < 2
            || self.created_at_ms == 0
            || self.changed_fields.is_empty()
            || self.changed_fields.len() > PROFILE_DIFF_BASIC_CHANGE_LIMIT
        {
            return Err(
                "The local engine returned a mismatched profile update receipt".to_string(),
            );
        }
        validate_javascript_integer(self.version_number, "profile version")?;
        validate_javascript_integer(self.created_at_ms, "profile version timestamp")?;
        let present_fields = patch.present_fields();
        let mut previous_order = None;
        for field in &self.changed_fields {
            let order = field.order();
            if previous_order.is_some_and(|previous| order <= previous)
                || !present_fields.contains(field)
            {
                return Err("The local engine returned invalid profile changed fields".to_string());
            }
            previous_order = Some(order);
        }
        validate_profile_basics_text(
            &self.profile_name,
            "name",
            PROFILE_REVIEW_NAME_LIMIT_CHARS,
            false,
        )?;
        validate_optional_profile_basics_text(
            self.headline.as_deref(),
            "headline",
            PROFILE_REVIEW_HEADLINE_LIMIT_CHARS,
            false,
        )?;
        if let RevisionPatchField::Value(name) = &patch.name
            && name.is_ascii()
            && name != &self.profile_name
        {
            return Err("The local engine returned an inconsistent profile name".to_string());
        }
        match &patch.headline {
            RevisionPatchField::Value(headline)
                if self.headline.is_none()
                    || (headline.is_ascii() && self.headline.as_ref() != Some(headline)) =>
            {
                return Err(
                    "The local engine returned an inconsistent profile headline".to_string()
                );
            }
            RevisionPatchField::Null if self.headline.is_some() => {
                return Err(
                    "The local engine returned an inconsistent profile headline".to_string()
                );
            }
            _ => {}
        }
        Ok(self)
    }
}

impl ProfileWorkField {
    fn order(self) -> u8 {
        match self {
            Self::Name => 0,
            Self::Position => 1,
            Self::Url => 2,
            Self::StartDate => 3,
            Self::EndDate => 4,
            Self::Summary => 5,
            Self::Highlights => 6,
            Self::Location => 7,
        }
    }
}

fn validate_profile_work_highlights(
    highlights: &[String],
    engine_output: bool,
) -> Result<(), String> {
    if highlights.len() > PROFILE_WORK_HIGHLIGHT_LIMIT {
        return Err(if engine_output {
            "The local engine returned too many Work History highlights".to_string()
        } else {
            "Use at most eight Work History highlights".to_string()
        });
    }
    let mut total_chars = 0_usize;
    for highlight in highlights {
        let result = validate_profile_basics_text(
            highlight,
            "Work History highlight",
            PROFILE_WORK_HIGHLIGHT_LIMIT_CHARS,
            false,
        );
        if result.is_err() {
            return Err(if engine_output {
                "The local engine returned an invalid Work History highlight".to_string()
            } else {
                "Each Work History highlight must be nonblank and within the local limit"
                    .to_string()
            });
        }
        total_chars = total_chars
            .checked_add(highlight.chars().count())
            .ok_or_else(|| "Work History highlights exceed the local limit".to_string())?;
    }
    if total_chars > PROFILE_WORK_HIGHLIGHT_TOTAL_LIMIT_CHARS {
        return Err(if engine_output {
            "The local engine returned oversized Work History highlights".to_string()
        } else {
            "Work History highlights exceed the local total limit".to_string()
        });
    }
    Ok(())
}

fn normalize_profile_work_highlights(highlights: Vec<String>) -> Result<Vec<String>, String> {
    let normalized = highlights
        .into_iter()
        .map(|highlight| {
            normalized_profile_basics_text(
                highlight,
                "Work History highlight",
                PROFILE_WORK_HIGHLIGHT_LIMIT_CHARS,
                false,
            )
        })
        .collect::<Result<Vec<_>, _>>()?;
    validate_profile_work_highlights(&normalized, false)?;
    Ok(normalized)
}

fn normalize_optional_profile_work_text(
    value: Option<String>,
    field_name: &str,
    maximum_chars: usize,
    allow_line_breaks: bool,
) -> Result<Option<String>, String> {
    value
        .map(|value| {
            normalized_profile_basics_text(value, field_name, maximum_chars, allow_line_breaks)
        })
        .transpose()
}

impl ProfileWorkEntry {
    fn validate(&self) -> Result<(), String> {
        validate_javascript_integer(self.entry_index, "Work History entry index")?;
        if self.entry_index >= PROFILE_WORK_ENTRY_LIMIT {
            return Err(
                "The local engine returned an invalid Work History entry index".to_string(),
            );
        }
        if !self.name.is_empty() {
            validate_profile_basics_text(
                &self.name,
                "Work History organization",
                PROFILE_WORK_NAME_LIMIT_CHARS,
                false,
            )?;
        }
        if !self.position.is_empty() {
            validate_profile_basics_text(
                &self.position,
                "Work History position",
                PROFILE_WORK_POSITION_LIMIT_CHARS,
                false,
            )?;
        }
        if let Some(url) = &self.url {
            if url.chars().count() > PROFILE_WORK_URL_LIMIT_CHARS {
                return Err("The local engine returned an oversized Work History URL".to_string());
            }
            validate_public_profile_url(url, "Work History website")?;
        }
        validate_optional_profile_basics_text(
            self.start_date.as_deref(),
            "Work History start date",
            PROFILE_WORK_DATE_LIMIT_CHARS,
            false,
        )?;
        validate_optional_profile_basics_text(
            self.end_date.as_deref(),
            "Work History end date",
            PROFILE_WORK_DATE_LIMIT_CHARS,
            false,
        )?;
        validate_optional_profile_basics_text(
            self.summary.as_deref(),
            "Work History summary",
            PROFILE_WORK_SUMMARY_LIMIT_CHARS,
            true,
        )?;
        validate_profile_work_highlights(&self.highlights, true)?;
        validate_optional_profile_basics_text(
            self.location.as_deref(),
            "Work History location",
            PROFILE_WORK_LOCATION_LIMIT_CHARS,
            false,
        )
    }
}

impl ProfileWorkPage {
    fn validated(
        self,
        expected_profile_version_id: &str,
        expected_offset: usize,
    ) -> Result<Self, String> {
        validate_uuid_receipt(&self.profile_version_id)?;
        if self.profile_version_id != expected_profile_version_id
            || self.version_number == 0
            || self.offset != expected_offset as u64
            || self.limit != PROFILE_WORK_PAGE_LIMIT as u64
            || self.total_items > PROFILE_WORK_ENTRY_LIMIT
            || self.items.len() > PROFILE_WORK_PAGE_LIMIT
        {
            return Err("The local engine returned a mismatched Work History page".to_string());
        }
        for (value, field_name) in [
            (self.version_number, "profile version"),
            (self.total_items, "Work History entry count"),
            (self.offset, "Work History offset"),
            (self.limit, "Work History limit"),
        ] {
            validate_javascript_integer(value, field_name)?;
        }
        let item_count = self.items.len() as u64;
        let page_end = self
            .offset
            .checked_add(item_count)
            .ok_or_else(|| "The local engine returned an invalid Work History page".to_string())?;
        if (!self.items.is_empty()
            && (self.offset >= self.total_items || page_end > self.total_items))
            || (self.items.is_empty() && self.offset < self.total_items)
        {
            return Err("The local engine returned an invalid Work History page".to_string());
        }
        if self.next_offset != (page_end < self.total_items).then_some(page_end) {
            return Err(
                "The local engine returned inconsistent Work History pagination".to_string(),
            );
        }
        if let Some(next_offset) = self.next_offset {
            validate_javascript_integer(next_offset, "Work History next offset")?;
            if next_offset > PROFILE_WORK_OFFSET_LIMIT as u64 {
                return Err(
                    "The local engine returned an invalid Work History next offset".to_string(),
                );
            }
        }
        let mut previous_index = None;
        for entry in &self.items {
            entry.validate()?;
            if previous_index.is_some_and(|previous| entry.entry_index <= previous) {
                return Err("The local engine returned invalid Work History ordering".to_string());
            }
            previous_index = Some(entry.entry_index);
        }
        validate_profile_review_serialized_size(
            &self,
            PROFILE_WORK_PAGE_LIMIT_BYTES,
            "Work History page",
        )?;
        Ok(self)
    }
}

impl ProfileWorkEntryInput {
    fn validated(self) -> Result<Self, String> {
        let name = normalized_profile_basics_text(
            self.name,
            "Organization",
            PROFILE_WORK_NAME_LIMIT_CHARS,
            false,
        )?;
        let position = normalized_profile_basics_text(
            self.position,
            "Role or position",
            PROFILE_WORK_POSITION_LIMIT_CHARS,
            false,
        )?;
        let url = match self.url {
            Some(url) => {
                let normalized = normalized_profile_basics_text(
                    url,
                    "Organization website",
                    PROFILE_WORK_URL_LIMIT_CHARS,
                    false,
                )?;
                validate_public_profile_url(&normalized, "Organization website")?;
                Some(normalized)
            }
            None => None,
        };
        Ok(Self {
            name,
            position,
            url,
            start_date: normalize_optional_profile_work_text(
                self.start_date,
                "Start date",
                PROFILE_WORK_DATE_LIMIT_CHARS,
                false,
            )?,
            end_date: normalize_optional_profile_work_text(
                self.end_date,
                "End date",
                PROFILE_WORK_DATE_LIMIT_CHARS,
                false,
            )?,
            summary: normalize_optional_profile_work_text(
                self.summary,
                "Role summary",
                PROFILE_WORK_SUMMARY_LIMIT_CHARS,
                true,
            )?,
            highlights: normalize_profile_work_highlights(self.highlights)?,
            location: normalize_optional_profile_work_text(
                self.location,
                "Location",
                PROFILE_WORK_LOCATION_LIMIT_CHARS,
                false,
            )?,
        })
    }
}

impl ProfileWorkEntryPatch {
    fn has_fields(&self) -> bool {
        !self.name.is_missing()
            || !self.position.is_missing()
            || !self.url.is_missing()
            || !self.start_date.is_missing()
            || !self.end_date.is_missing()
            || !self.summary.is_missing()
            || !self.highlights.is_missing()
            || !self.location.is_missing()
    }

    fn present_fields(&self) -> HashSet<ProfileWorkField> {
        let mut fields = HashSet::new();
        for (present, field) in [
            (!self.name.is_missing(), ProfileWorkField::Name),
            (!self.position.is_missing(), ProfileWorkField::Position),
            (!self.url.is_missing(), ProfileWorkField::Url),
            (!self.start_date.is_missing(), ProfileWorkField::StartDate),
            (!self.end_date.is_missing(), ProfileWorkField::EndDate),
            (!self.summary.is_missing(), ProfileWorkField::Summary),
            (!self.highlights.is_missing(), ProfileWorkField::Highlights),
            (!self.location.is_missing(), ProfileWorkField::Location),
        ] {
            if present {
                fields.insert(field);
            }
        }
        fields
    }

    fn validated(self) -> Result<Self, String> {
        if !self.has_fields() {
            return Err("The Work History patch must include at least one field".to_string());
        }
        let name = match self.name {
            RevisionPatchField::Missing => RevisionPatchField::Missing,
            RevisionPatchField::Null => return Err("Organization cannot be cleared".to_string()),
            RevisionPatchField::Value(value) => {
                RevisionPatchField::Value(normalized_profile_basics_text(
                    value,
                    "Organization",
                    PROFILE_WORK_NAME_LIMIT_CHARS,
                    false,
                )?)
            }
        };
        let position = match self.position {
            RevisionPatchField::Missing => RevisionPatchField::Missing,
            RevisionPatchField::Null => {
                return Err("Role or position cannot be cleared".to_string());
            }
            RevisionPatchField::Value(value) => {
                RevisionPatchField::Value(normalized_profile_basics_text(
                    value,
                    "Role or position",
                    PROFILE_WORK_POSITION_LIMIT_CHARS,
                    false,
                )?)
            }
        };
        let url = match self.url {
            RevisionPatchField::Missing => RevisionPatchField::Missing,
            RevisionPatchField::Null => RevisionPatchField::Null,
            RevisionPatchField::Value(value) => {
                let normalized = normalized_profile_basics_text(
                    value,
                    "Organization website",
                    PROFILE_WORK_URL_LIMIT_CHARS,
                    false,
                )?;
                validate_public_profile_url(&normalized, "Organization website")?;
                RevisionPatchField::Value(normalized)
            }
        };
        let highlights = match self.highlights {
            RevisionPatchField::Missing => RevisionPatchField::Missing,
            RevisionPatchField::Null => RevisionPatchField::Null,
            RevisionPatchField::Value(values) => {
                RevisionPatchField::Value(normalize_profile_work_highlights(values)?)
            }
        };
        Ok(Self {
            name,
            position,
            url,
            start_date: normalized_nullable_profile_patch_field(
                self.start_date,
                "Start date",
                PROFILE_WORK_DATE_LIMIT_CHARS,
                false,
            )?,
            end_date: normalized_nullable_profile_patch_field(
                self.end_date,
                "End date",
                PROFILE_WORK_DATE_LIMIT_CHARS,
                false,
            )?,
            summary: normalized_nullable_profile_patch_field(
                self.summary,
                "Role summary",
                PROFILE_WORK_SUMMARY_LIMIT_CHARS,
                true,
            )?,
            highlights,
            location: normalized_nullable_profile_patch_field(
                self.location,
                "Location",
                PROFILE_WORK_LOCATION_LIMIT_CHARS,
                false,
            )?,
        })
    }
}

impl ProfileWorkOperation {
    fn kind(&self) -> ProfileWorkOperationKind {
        match self {
            Self::Add { .. } => ProfileWorkOperationKind::Add,
            Self::Update { .. } => ProfileWorkOperationKind::Update,
            Self::Remove { .. } => ProfileWorkOperationKind::Remove,
        }
    }

    fn present_fields(&self) -> HashSet<ProfileWorkField> {
        match self {
            Self::Add { entry } => {
                let mut fields =
                    HashSet::from([ProfileWorkField::Name, ProfileWorkField::Position]);
                for (present, field) in [
                    (entry.url.is_some(), ProfileWorkField::Url),
                    (entry.start_date.is_some(), ProfileWorkField::StartDate),
                    (entry.end_date.is_some(), ProfileWorkField::EndDate),
                    (entry.summary.is_some(), ProfileWorkField::Summary),
                    (!entry.highlights.is_empty(), ProfileWorkField::Highlights),
                    (entry.location.is_some(), ProfileWorkField::Location),
                ] {
                    if present {
                        fields.insert(field);
                    }
                }
                fields
            }
            Self::Update { patch, .. } => patch.present_fields(),
            Self::Remove { .. } => HashSet::from([
                ProfileWorkField::Name,
                ProfileWorkField::Position,
                ProfileWorkField::Url,
                ProfileWorkField::StartDate,
                ProfileWorkField::EndDate,
                ProfileWorkField::Summary,
                ProfileWorkField::Highlights,
                ProfileWorkField::Location,
            ]),
        }
    }

    fn validated(self) -> Result<Self, String> {
        match self {
            Self::Add { entry } => Ok(Self::Add {
                entry: entry.validated()?,
            }),
            Self::Update { entry_index, patch } => {
                validate_javascript_integer(entry_index, "Work History entry index")?;
                if entry_index >= PROFILE_WORK_ENTRY_LIMIT {
                    return Err("The Work History entry index exceeds the local limit".to_string());
                }
                Ok(Self::Update {
                    entry_index,
                    patch: patch.validated()?,
                })
            }
            Self::Remove { entry_index } => {
                validate_javascript_integer(entry_index, "Work History entry index")?;
                if entry_index >= PROFILE_WORK_ENTRY_LIMIT {
                    return Err("The Work History entry index exceeds the local limit".to_string());
                }
                Ok(Self::Remove { entry_index })
            }
        }
    }
}

impl ProfileWorkUpdateReceipt {
    fn validated(
        self,
        expected_parent_profile_version_id: &str,
        expected_request_id: &str,
        operation: &ProfileWorkOperation,
    ) -> Result<Self, String> {
        validate_uuid_receipt(&self.request_id)?;
        validate_uuid_receipt(&self.profile_version_id)?;
        validate_uuid_receipt(&self.parent_profile_version_id)?;
        if self.request_id != expected_request_id
            || self.parent_profile_version_id != expected_parent_profile_version_id
            || self.profile_version_id == self.parent_profile_version_id
            || self.operation != operation.kind()
            || self.version_number < 2
            || self.created_at_ms == 0
            || self.entry_index >= PROFILE_WORK_ENTRY_LIMIT
            || self.changed_fields.is_empty()
            || self.changed_fields.len() > 8
            || self.work_entries > PROFILE_WORK_ENTRY_LIMIT
        {
            return Err("The local engine returned a mismatched Work History receipt".to_string());
        }
        for (value, field_name) in [
            (self.version_number, "profile version"),
            (self.created_at_ms, "profile version timestamp"),
            (self.entry_index, "Work History entry index"),
            (self.work_entries, "Work History entry count"),
        ] {
            validate_javascript_integer(value, field_name)?;
        }
        if self.operation == ProfileWorkOperationKind::Add && self.work_entries == 0 {
            return Err("The local engine returned an invalid Work History count".to_string());
        }
        match operation {
            ProfileWorkOperation::Update { entry_index, .. }
            | ProfileWorkOperation::Remove { entry_index }
                if self.entry_index != *entry_index =>
            {
                return Err(
                    "The local engine returned a mismatched Work History entry index".to_string(),
                );
            }
            _ => {}
        }
        let present_fields = operation.present_fields();
        let mut previous_order = None;
        for field in &self.changed_fields {
            let order = field.order();
            if previous_order.is_some_and(|previous| order <= previous)
                || !present_fields.contains(field)
            {
                return Err(
                    "The local engine returned invalid Work History changed fields".to_string(),
                );
            }
            previous_order = Some(order);
        }
        if self.operation == ProfileWorkOperationKind::Add
            && (!self.changed_fields.contains(&ProfileWorkField::Name)
                || !self.changed_fields.contains(&ProfileWorkField::Position))
        {
            return Err("The local engine returned an incomplete Work History receipt".to_string());
        }
        validate_profile_basics_text(
            &self.profile_name,
            "name",
            PROFILE_REVIEW_NAME_LIMIT_CHARS,
            false,
        )?;
        Ok(self)
    }
}

fn validated_profile_work_offset(offset: usize) -> Result<usize, String> {
    if offset > PROFILE_WORK_OFFSET_LIMIT {
        return Err("The Work History offset exceeds the local limit".to_string());
    }
    Ok(offset)
}

fn profile_work_list_params(profile_version_id: &str, offset: usize) -> Value {
    json!({"profile_version_id": profile_version_id, "offset": offset})
}

fn profile_work_update_params(
    expected_parent_profile_version_id: &str,
    request_id: &str,
    operation: &ProfileWorkOperation,
) -> Value {
    json!({
        "expected_parent_profile_version_id": expected_parent_profile_version_id,
        "request_id": request_id,
        "operation": operation,
    })
}

fn validate_profile_section_string_list(
    values: &[String],
    field_name: &str,
    maximum_items: usize,
    maximum_item_chars: usize,
    maximum_total_chars: usize,
    require_nonempty: bool,
    engine_output: bool,
) -> Result<(), String> {
    if (require_nonempty && values.is_empty()) || values.len() > maximum_items {
        return Err(if engine_output {
            format!("The local engine returned an invalid {field_name}")
        } else {
            format!("{field_name} must contain between 1 and {maximum_items} values")
        });
    }
    let mut total_chars = 0_usize;
    for value in values {
        if validate_profile_basics_text(value, field_name, maximum_item_chars, false).is_err() {
            return Err(if engine_output {
                format!("The local engine returned an invalid {field_name}")
            } else {
                format!("Each {field_name} value must be nonblank and within the local limit")
            });
        }
        total_chars = total_chars
            .checked_add(value.chars().count())
            .ok_or_else(|| format!("{field_name} exceeds the local limit"))?;
    }
    if total_chars > maximum_total_chars {
        return Err(if engine_output {
            format!("The local engine returned an oversized {field_name}")
        } else {
            format!("{field_name} exceeds the local total limit")
        });
    }
    Ok(())
}

fn normalize_profile_section_string_list(
    values: Vec<String>,
    field_name: &str,
    maximum_items: usize,
    maximum_item_chars: usize,
    maximum_total_chars: usize,
    require_nonempty: bool,
) -> Result<Vec<String>, String> {
    let normalized = values
        .into_iter()
        .map(|value| normalized_profile_basics_text(value, field_name, maximum_item_chars, false))
        .collect::<Result<Vec<_>, _>>()?;
    validate_profile_section_string_list(
        &normalized,
        field_name,
        maximum_items,
        maximum_item_chars,
        maximum_total_chars,
        require_nonempty,
        false,
    )?;
    Ok(normalized)
}

fn validate_profile_section_entry_index(
    entry_index: u64,
    section_name: &str,
) -> Result<(), String> {
    validate_javascript_integer(entry_index, &format!("{section_name} entry index"))?;
    if entry_index >= PROFILE_SECTION_ENTRY_LIMIT {
        return Err(format!(
            "The local engine returned an invalid {section_name} entry index"
        ));
    }
    Ok(())
}

fn validated_profile_section_offset(offset: usize, section_name: &str) -> Result<usize, String> {
    if offset > PROFILE_SECTION_OFFSET_LIMIT {
        return Err(format!("The {section_name} offset exceeds the local limit"));
    }
    Ok(offset)
}

#[allow(clippy::too_many_arguments)]
fn validate_profile_section_page_header(
    profile_version_id: &str,
    expected_profile_version_id: &str,
    section: ProfileEditableSection,
    expected_section: ProfileEditableSection,
    version_number: u64,
    total_items: u64,
    offset: u64,
    expected_offset: usize,
    limit: u64,
    next_offset: Option<u64>,
    item_count: usize,
    section_name: &str,
) -> Result<(), String> {
    validate_uuid_receipt(profile_version_id)?;
    if profile_version_id != expected_profile_version_id
        || section != expected_section
        || version_number == 0
        || offset != expected_offset as u64
        || limit != PROFILE_SECTION_PAGE_LIMIT as u64
        || total_items > PROFILE_SECTION_ENTRY_LIMIT
        || item_count > PROFILE_SECTION_PAGE_LIMIT
    {
        return Err(format!(
            "The local engine returned a mismatched {section_name} page"
        ));
    }
    for (value, field_name) in [
        (version_number, "profile version"),
        (total_items, "profile section entry count"),
        (offset, "profile section offset"),
        (limit, "profile section limit"),
    ] {
        validate_javascript_integer(value, field_name)?;
    }
    let page_end = offset
        .checked_add(item_count as u64)
        .ok_or_else(|| format!("The local engine returned an invalid {section_name} page"))?;
    if (!item_count.eq(&0) && (offset >= total_items || page_end > total_items))
        || (item_count == 0 && offset < total_items)
    {
        return Err(format!(
            "The local engine returned an invalid {section_name} page"
        ));
    }
    if next_offset != (page_end < total_items).then_some(page_end) {
        return Err(format!(
            "The local engine returned inconsistent {section_name} pagination"
        ));
    }
    if let Some(next_offset) = next_offset {
        validate_javascript_integer(next_offset, "profile section next offset")?;
        if next_offset > PROFILE_SECTION_OFFSET_LIMIT as u64 {
            return Err(format!(
                "The local engine returned an invalid {section_name} next offset"
            ));
        }
    }
    Ok(())
}

impl ProfileProjectEntry {
    fn validate(&self) -> Result<(), String> {
        validate_profile_section_entry_index(self.entry_index, "Projects")?;
        if !self.name.is_empty() {
            validate_profile_basics_text(
                &self.name,
                "project name",
                PROFILE_PROJECT_NAME_LIMIT_CHARS,
                false,
            )?;
        }
        validate_optional_profile_basics_text(
            self.description.as_deref(),
            "project description",
            PROFILE_PROJECT_DESCRIPTION_LIMIT_CHARS,
            true,
        )?;
        if let Some(url) = &self.url {
            if url.chars().count() > PROFILE_PROJECT_URL_LIMIT_CHARS {
                return Err("The local engine returned an oversized project URL".to_string());
            }
            validate_public_profile_url(url, "Project website")?;
        }
        validate_optional_profile_basics_text(
            self.start_date.as_deref(),
            "project start date",
            PROFILE_PROJECT_DATE_LIMIT_CHARS,
            false,
        )?;
        validate_optional_profile_basics_text(
            self.end_date.as_deref(),
            "project end date",
            PROFILE_PROJECT_DATE_LIMIT_CHARS,
            false,
        )?;
        validate_profile_section_string_list(
            &self.highlights,
            "project highlights",
            PROFILE_PROJECT_HIGHLIGHT_LIMIT,
            PROFILE_PROJECT_HIGHLIGHT_LIMIT_CHARS,
            PROFILE_PROJECT_HIGHLIGHT_TOTAL_LIMIT_CHARS,
            false,
            true,
        )?;
        validate_profile_section_string_list(
            &self.keywords,
            "project keywords",
            PROFILE_PROJECT_KEYWORD_LIMIT,
            PROFILE_PROJECT_KEYWORD_LIMIT_CHARS,
            PROFILE_PROJECT_KEYWORD_TOTAL_LIMIT_CHARS,
            false,
            true,
        )
    }
}

impl ProfileProjectsPage {
    fn validated(
        self,
        expected_profile_version_id: &str,
        expected_offset: usize,
    ) -> Result<Self, String> {
        validate_profile_section_page_header(
            &self.profile_version_id,
            expected_profile_version_id,
            self.section,
            ProfileEditableSection::Projects,
            self.version_number,
            self.total_items,
            self.offset,
            expected_offset,
            self.limit,
            self.next_offset,
            self.items.len(),
            "Projects",
        )?;
        let mut previous_index = None;
        for entry in &self.items {
            entry.validate()?;
            if previous_index.is_some_and(|previous| entry.entry_index <= previous) {
                return Err("The local engine returned invalid Projects ordering".to_string());
            }
            previous_index = Some(entry.entry_index);
        }
        validate_profile_review_serialized_size(
            &self,
            PROFILE_SECTION_PAGE_LIMIT_BYTES,
            "Projects page",
        )?;
        Ok(self)
    }
}

impl ProfileProjectEntryInput {
    fn validated(self) -> Result<Self, String> {
        let name = normalized_profile_basics_text(
            self.name,
            "Project name",
            PROFILE_PROJECT_NAME_LIMIT_CHARS,
            false,
        )?;
        let description = normalize_optional_profile_work_text(
            self.description,
            "Project description",
            PROFILE_PROJECT_DESCRIPTION_LIMIT_CHARS,
            true,
        )?;
        let url = match self.url {
            Some(url) => {
                let normalized = normalized_profile_basics_text(
                    url,
                    "Project website",
                    PROFILE_PROJECT_URL_LIMIT_CHARS,
                    false,
                )?;
                validate_public_profile_url(&normalized, "Project website")?;
                Some(normalized)
            }
            None => None,
        };
        Ok(Self {
            name,
            description,
            url,
            start_date: normalize_optional_profile_work_text(
                self.start_date,
                "Project start date",
                PROFILE_PROJECT_DATE_LIMIT_CHARS,
                false,
            )?,
            end_date: normalize_optional_profile_work_text(
                self.end_date,
                "Project end date",
                PROFILE_PROJECT_DATE_LIMIT_CHARS,
                false,
            )?,
            highlights: normalize_profile_section_string_list(
                self.highlights,
                "project highlights",
                PROFILE_PROJECT_HIGHLIGHT_LIMIT,
                PROFILE_PROJECT_HIGHLIGHT_LIMIT_CHARS,
                PROFILE_PROJECT_HIGHLIGHT_TOTAL_LIMIT_CHARS,
                false,
            )?,
            keywords: normalize_profile_section_string_list(
                self.keywords,
                "project keywords",
                PROFILE_PROJECT_KEYWORD_LIMIT,
                PROFILE_PROJECT_KEYWORD_LIMIT_CHARS,
                PROFILE_PROJECT_KEYWORD_TOTAL_LIMIT_CHARS,
                false,
            )?,
        })
    }
}

impl ProfileProjectEntryPatch {
    fn has_fields(&self) -> bool {
        !self.name.is_missing()
            || !self.description.is_missing()
            || !self.url.is_missing()
            || !self.start_date.is_missing()
            || !self.end_date.is_missing()
            || !self.highlights.is_missing()
            || !self.keywords.is_missing()
    }

    fn present_fields(&self) -> HashSet<ProfileSectionField> {
        [
            (!self.name.is_missing(), ProfileSectionField::Name),
            (
                !self.description.is_missing(),
                ProfileSectionField::Description,
            ),
            (!self.url.is_missing(), ProfileSectionField::Url),
            (
                !self.start_date.is_missing(),
                ProfileSectionField::StartDate,
            ),
            (!self.end_date.is_missing(), ProfileSectionField::EndDate),
            (
                !self.highlights.is_missing(),
                ProfileSectionField::Highlights,
            ),
            (!self.keywords.is_missing(), ProfileSectionField::Keywords),
        ]
        .into_iter()
        .filter_map(|(present, field)| present.then_some(field))
        .collect()
    }

    fn validated(self) -> Result<Self, String> {
        if !self.has_fields() {
            return Err("The Projects patch must include at least one field".to_string());
        }
        let name = match self.name {
            RevisionPatchField::Missing => RevisionPatchField::Missing,
            RevisionPatchField::Null => return Err("Project name cannot be cleared".to_string()),
            RevisionPatchField::Value(value) => {
                RevisionPatchField::Value(normalized_profile_basics_text(
                    value,
                    "Project name",
                    PROFILE_PROJECT_NAME_LIMIT_CHARS,
                    false,
                )?)
            }
        };
        let url = match self.url {
            RevisionPatchField::Missing => RevisionPatchField::Missing,
            RevisionPatchField::Null => RevisionPatchField::Null,
            RevisionPatchField::Value(value) => {
                let normalized = normalized_profile_basics_text(
                    value,
                    "Project website",
                    PROFILE_PROJECT_URL_LIMIT_CHARS,
                    false,
                )?;
                validate_public_profile_url(&normalized, "Project website")?;
                RevisionPatchField::Value(normalized)
            }
        };
        let highlights = match self.highlights {
            RevisionPatchField::Missing => RevisionPatchField::Missing,
            RevisionPatchField::Null => RevisionPatchField::Null,
            RevisionPatchField::Value(values) => {
                RevisionPatchField::Value(normalize_profile_section_string_list(
                    values,
                    "project highlights",
                    PROFILE_PROJECT_HIGHLIGHT_LIMIT,
                    PROFILE_PROJECT_HIGHLIGHT_LIMIT_CHARS,
                    PROFILE_PROJECT_HIGHLIGHT_TOTAL_LIMIT_CHARS,
                    false,
                )?)
            }
        };
        let keywords = match self.keywords {
            RevisionPatchField::Missing => RevisionPatchField::Missing,
            RevisionPatchField::Null => RevisionPatchField::Null,
            RevisionPatchField::Value(values) => {
                RevisionPatchField::Value(normalize_profile_section_string_list(
                    values,
                    "project keywords",
                    PROFILE_PROJECT_KEYWORD_LIMIT,
                    PROFILE_PROJECT_KEYWORD_LIMIT_CHARS,
                    PROFILE_PROJECT_KEYWORD_TOTAL_LIMIT_CHARS,
                    false,
                )?)
            }
        };
        Ok(Self {
            name,
            description: normalized_nullable_profile_patch_field(
                self.description,
                "Project description",
                PROFILE_PROJECT_DESCRIPTION_LIMIT_CHARS,
                true,
            )?,
            url,
            start_date: normalized_nullable_profile_patch_field(
                self.start_date,
                "Project start date",
                PROFILE_PROJECT_DATE_LIMIT_CHARS,
                false,
            )?,
            end_date: normalized_nullable_profile_patch_field(
                self.end_date,
                "Project end date",
                PROFILE_PROJECT_DATE_LIMIT_CHARS,
                false,
            )?,
            highlights,
            keywords,
        })
    }
}

impl ProfileProjectsOperation {
    fn kind(&self) -> ProfileSectionOperationKind {
        match self {
            Self::Add { .. } => ProfileSectionOperationKind::Add,
            Self::Update { .. } => ProfileSectionOperationKind::Update,
            Self::Remove { .. } => ProfileSectionOperationKind::Remove,
        }
    }

    fn target_index(&self) -> Option<u64> {
        match self {
            Self::Add { .. } => None,
            Self::Update { entry_index, .. } | Self::Remove { entry_index } => Some(*entry_index),
        }
    }

    fn present_fields(&self) -> HashSet<ProfileSectionField> {
        match self {
            Self::Add { entry } => {
                let mut fields = HashSet::from([ProfileSectionField::Name]);
                for (present, field) in [
                    (
                        entry.description.is_some(),
                        ProfileSectionField::Description,
                    ),
                    (entry.url.is_some(), ProfileSectionField::Url),
                    (entry.start_date.is_some(), ProfileSectionField::StartDate),
                    (entry.end_date.is_some(), ProfileSectionField::EndDate),
                    (
                        !entry.highlights.is_empty(),
                        ProfileSectionField::Highlights,
                    ),
                    (!entry.keywords.is_empty(), ProfileSectionField::Keywords),
                ] {
                    if present {
                        fields.insert(field);
                    }
                }
                fields
            }
            Self::Update { patch, .. } => patch.present_fields(),
            Self::Remove { .. } => HashSet::from([
                ProfileSectionField::Name,
                ProfileSectionField::Description,
                ProfileSectionField::Url,
                ProfileSectionField::StartDate,
                ProfileSectionField::EndDate,
                ProfileSectionField::Highlights,
                ProfileSectionField::Keywords,
            ]),
        }
    }

    fn validated(self) -> Result<Self, String> {
        match self {
            Self::Add { entry } => Ok(Self::Add {
                entry: entry.validated()?,
            }),
            Self::Update { entry_index, patch } => {
                validate_profile_section_entry_index(entry_index, "Projects")?;
                Ok(Self::Update {
                    entry_index,
                    patch: patch.validated()?,
                })
            }
            Self::Remove { entry_index } => {
                validate_profile_section_entry_index(entry_index, "Projects")?;
                Ok(Self::Remove { entry_index })
            }
        }
    }
}

impl ProfileEducationEntry {
    fn validate(&self) -> Result<(), String> {
        validate_profile_section_entry_index(self.entry_index, "Education")?;
        if !self.institution.is_empty() {
            validate_profile_basics_text(
                &self.institution,
                "education institution",
                PROFILE_EDUCATION_INSTITUTION_LIMIT_CHARS,
                false,
            )?;
        }
        validate_optional_profile_basics_text(
            self.study_type.as_deref(),
            "education study type",
            PROFILE_EDUCATION_STUDY_TYPE_LIMIT_CHARS,
            false,
        )?;
        validate_optional_profile_basics_text(
            self.area.as_deref(),
            "education area",
            PROFILE_EDUCATION_AREA_LIMIT_CHARS,
            false,
        )?;
        if let Some(url) = &self.url {
            if url.chars().count() > PROFILE_EDUCATION_URL_LIMIT_CHARS {
                return Err("The local engine returned an oversized education URL".to_string());
            }
            validate_public_profile_url(url, "Education website")?;
        }
        validate_optional_profile_basics_text(
            self.start_date.as_deref(),
            "education start date",
            PROFILE_EDUCATION_DATE_LIMIT_CHARS,
            false,
        )?;
        validate_optional_profile_basics_text(
            self.end_date.as_deref(),
            "education end date",
            PROFILE_EDUCATION_DATE_LIMIT_CHARS,
            false,
        )?;
        validate_optional_profile_basics_text(
            self.score.as_deref(),
            "education score",
            PROFILE_EDUCATION_SCORE_LIMIT_CHARS,
            false,
        )?;
        validate_profile_section_string_list(
            &self.courses,
            "education courses",
            PROFILE_EDUCATION_COURSE_LIMIT,
            PROFILE_EDUCATION_COURSE_LIMIT_CHARS,
            PROFILE_EDUCATION_COURSE_TOTAL_LIMIT_CHARS,
            false,
            true,
        )
    }
}

impl ProfileEducationPage {
    fn validated(
        self,
        expected_profile_version_id: &str,
        expected_offset: usize,
    ) -> Result<Self, String> {
        validate_profile_section_page_header(
            &self.profile_version_id,
            expected_profile_version_id,
            self.section,
            ProfileEditableSection::Education,
            self.version_number,
            self.total_items,
            self.offset,
            expected_offset,
            self.limit,
            self.next_offset,
            self.items.len(),
            "Education",
        )?;
        let mut previous_index = None;
        for entry in &self.items {
            entry.validate()?;
            if previous_index.is_some_and(|previous| entry.entry_index <= previous) {
                return Err("The local engine returned invalid Education ordering".to_string());
            }
            previous_index = Some(entry.entry_index);
        }
        validate_profile_review_serialized_size(
            &self,
            PROFILE_SECTION_PAGE_LIMIT_BYTES,
            "Education page",
        )?;
        Ok(self)
    }
}

impl ProfileEducationEntryInput {
    fn validated(self) -> Result<Self, String> {
        let institution = normalized_profile_basics_text(
            self.institution,
            "Education institution",
            PROFILE_EDUCATION_INSTITUTION_LIMIT_CHARS,
            false,
        )?;
        let study_type = normalize_optional_profile_work_text(
            self.study_type,
            "Education study type",
            PROFILE_EDUCATION_STUDY_TYPE_LIMIT_CHARS,
            false,
        )?;
        let area = normalize_optional_profile_work_text(
            self.area,
            "Education area",
            PROFILE_EDUCATION_AREA_LIMIT_CHARS,
            false,
        )?;
        if study_type.is_none() && area.is_none() {
            return Err("Education must include a study type or area".to_string());
        }
        let url = match self.url {
            Some(url) => {
                let normalized = normalized_profile_basics_text(
                    url,
                    "Education website",
                    PROFILE_EDUCATION_URL_LIMIT_CHARS,
                    false,
                )?;
                validate_public_profile_url(&normalized, "Education website")?;
                Some(normalized)
            }
            None => None,
        };
        Ok(Self {
            institution,
            study_type,
            area,
            url,
            start_date: normalize_optional_profile_work_text(
                self.start_date,
                "Education start date",
                PROFILE_EDUCATION_DATE_LIMIT_CHARS,
                false,
            )?,
            end_date: normalize_optional_profile_work_text(
                self.end_date,
                "Education end date",
                PROFILE_EDUCATION_DATE_LIMIT_CHARS,
                false,
            )?,
            score: normalize_optional_profile_work_text(
                self.score,
                "Education score",
                PROFILE_EDUCATION_SCORE_LIMIT_CHARS,
                false,
            )?,
            courses: normalize_profile_section_string_list(
                self.courses,
                "education courses",
                PROFILE_EDUCATION_COURSE_LIMIT,
                PROFILE_EDUCATION_COURSE_LIMIT_CHARS,
                PROFILE_EDUCATION_COURSE_TOTAL_LIMIT_CHARS,
                false,
            )?,
        })
    }
}

impl ProfileEducationEntryPatch {
    fn has_fields(&self) -> bool {
        !self.institution.is_missing()
            || !self.study_type.is_missing()
            || !self.area.is_missing()
            || !self.url.is_missing()
            || !self.start_date.is_missing()
            || !self.end_date.is_missing()
            || !self.score.is_missing()
            || !self.courses.is_missing()
    }

    fn present_fields(&self) -> HashSet<ProfileSectionField> {
        [
            (
                !self.institution.is_missing(),
                ProfileSectionField::Institution,
            ),
            (
                !self.study_type.is_missing(),
                ProfileSectionField::StudyType,
            ),
            (!self.area.is_missing(), ProfileSectionField::Area),
            (!self.url.is_missing(), ProfileSectionField::Url),
            (
                !self.start_date.is_missing(),
                ProfileSectionField::StartDate,
            ),
            (!self.end_date.is_missing(), ProfileSectionField::EndDate),
            (!self.score.is_missing(), ProfileSectionField::Score),
            (!self.courses.is_missing(), ProfileSectionField::Courses),
        ]
        .into_iter()
        .filter_map(|(present, field)| present.then_some(field))
        .collect()
    }

    fn validated(self) -> Result<Self, String> {
        if !self.has_fields() {
            return Err("The Education patch must include at least one field".to_string());
        }
        let institution = match self.institution {
            RevisionPatchField::Missing => RevisionPatchField::Missing,
            RevisionPatchField::Null => {
                return Err("Education institution cannot be cleared".to_string());
            }
            RevisionPatchField::Value(value) => {
                RevisionPatchField::Value(normalized_profile_basics_text(
                    value,
                    "Education institution",
                    PROFILE_EDUCATION_INSTITUTION_LIMIT_CHARS,
                    false,
                )?)
            }
        };
        let url = match self.url {
            RevisionPatchField::Missing => RevisionPatchField::Missing,
            RevisionPatchField::Null => RevisionPatchField::Null,
            RevisionPatchField::Value(value) => {
                let normalized = normalized_profile_basics_text(
                    value,
                    "Education website",
                    PROFILE_EDUCATION_URL_LIMIT_CHARS,
                    false,
                )?;
                validate_public_profile_url(&normalized, "Education website")?;
                RevisionPatchField::Value(normalized)
            }
        };
        let courses = match self.courses {
            RevisionPatchField::Missing => RevisionPatchField::Missing,
            RevisionPatchField::Null => RevisionPatchField::Null,
            RevisionPatchField::Value(values) => {
                RevisionPatchField::Value(normalize_profile_section_string_list(
                    values,
                    "education courses",
                    PROFILE_EDUCATION_COURSE_LIMIT,
                    PROFILE_EDUCATION_COURSE_LIMIT_CHARS,
                    PROFILE_EDUCATION_COURSE_TOTAL_LIMIT_CHARS,
                    false,
                )?)
            }
        };
        Ok(Self {
            institution,
            study_type: normalized_nullable_profile_patch_field(
                self.study_type,
                "Education study type",
                PROFILE_EDUCATION_STUDY_TYPE_LIMIT_CHARS,
                false,
            )?,
            area: normalized_nullable_profile_patch_field(
                self.area,
                "Education area",
                PROFILE_EDUCATION_AREA_LIMIT_CHARS,
                false,
            )?,
            url,
            start_date: normalized_nullable_profile_patch_field(
                self.start_date,
                "Education start date",
                PROFILE_EDUCATION_DATE_LIMIT_CHARS,
                false,
            )?,
            end_date: normalized_nullable_profile_patch_field(
                self.end_date,
                "Education end date",
                PROFILE_EDUCATION_DATE_LIMIT_CHARS,
                false,
            )?,
            score: normalized_nullable_profile_patch_field(
                self.score,
                "Education score",
                PROFILE_EDUCATION_SCORE_LIMIT_CHARS,
                false,
            )?,
            courses,
        })
    }
}

impl ProfileEducationOperation {
    fn kind(&self) -> ProfileSectionOperationKind {
        match self {
            Self::Add { .. } => ProfileSectionOperationKind::Add,
            Self::Update { .. } => ProfileSectionOperationKind::Update,
            Self::Remove { .. } => ProfileSectionOperationKind::Remove,
        }
    }

    fn target_index(&self) -> Option<u64> {
        match self {
            Self::Add { .. } => None,
            Self::Update { entry_index, .. } | Self::Remove { entry_index } => Some(*entry_index),
        }
    }

    fn present_fields(&self) -> HashSet<ProfileSectionField> {
        match self {
            Self::Add { entry } => {
                let mut fields = HashSet::from([ProfileSectionField::Institution]);
                for (present, field) in [
                    (entry.study_type.is_some(), ProfileSectionField::StudyType),
                    (entry.area.is_some(), ProfileSectionField::Area),
                    (entry.url.is_some(), ProfileSectionField::Url),
                    (entry.start_date.is_some(), ProfileSectionField::StartDate),
                    (entry.end_date.is_some(), ProfileSectionField::EndDate),
                    (entry.score.is_some(), ProfileSectionField::Score),
                    (!entry.courses.is_empty(), ProfileSectionField::Courses),
                ] {
                    if present {
                        fields.insert(field);
                    }
                }
                fields
            }
            Self::Update { patch, .. } => patch.present_fields(),
            Self::Remove { .. } => HashSet::from([
                ProfileSectionField::Institution,
                ProfileSectionField::StudyType,
                ProfileSectionField::Area,
                ProfileSectionField::Url,
                ProfileSectionField::StartDate,
                ProfileSectionField::EndDate,
                ProfileSectionField::Score,
                ProfileSectionField::Courses,
            ]),
        }
    }

    fn validated(self) -> Result<Self, String> {
        match self {
            Self::Add { entry } => Ok(Self::Add {
                entry: entry.validated()?,
            }),
            Self::Update { entry_index, patch } => {
                validate_profile_section_entry_index(entry_index, "Education")?;
                Ok(Self::Update {
                    entry_index,
                    patch: patch.validated()?,
                })
            }
            Self::Remove { entry_index } => {
                validate_profile_section_entry_index(entry_index, "Education")?;
                Ok(Self::Remove { entry_index })
            }
        }
    }
}

impl ProfileSkillEntry {
    fn validate(&self) -> Result<(), String> {
        validate_profile_section_entry_index(self.entry_index, "Skills")?;
        if !self.name.is_empty() {
            validate_profile_basics_text(
                &self.name,
                "skill group name",
                PROFILE_SKILL_NAME_LIMIT_CHARS,
                false,
            )?;
        }
        validate_optional_profile_basics_text(
            self.level.as_deref(),
            "skill level",
            PROFILE_SKILL_LEVEL_LIMIT_CHARS,
            false,
        )?;
        validate_profile_section_string_list(
            &self.keywords,
            "skill keywords",
            PROFILE_SKILL_KEYWORD_LIMIT,
            PROFILE_SKILL_KEYWORD_LIMIT_CHARS,
            PROFILE_SKILL_KEYWORD_TOTAL_LIMIT_CHARS,
            false,
            true,
        )
    }
}

impl ProfileSkillsPage {
    fn validated(
        self,
        expected_profile_version_id: &str,
        expected_offset: usize,
    ) -> Result<Self, String> {
        validate_profile_section_page_header(
            &self.profile_version_id,
            expected_profile_version_id,
            self.section,
            ProfileEditableSection::Skills,
            self.version_number,
            self.total_items,
            self.offset,
            expected_offset,
            self.limit,
            self.next_offset,
            self.items.len(),
            "Skills",
        )?;
        let mut previous_index = None;
        for entry in &self.items {
            entry.validate()?;
            if previous_index.is_some_and(|previous| entry.entry_index <= previous) {
                return Err("The local engine returned invalid Skills ordering".to_string());
            }
            previous_index = Some(entry.entry_index);
        }
        validate_profile_review_serialized_size(
            &self,
            PROFILE_SECTION_PAGE_LIMIT_BYTES,
            "Skills page",
        )?;
        Ok(self)
    }
}

impl ProfileSkillEntryInput {
    fn validated(self) -> Result<Self, String> {
        Ok(Self {
            name: normalized_profile_basics_text(
                self.name,
                "Skill group name",
                PROFILE_SKILL_NAME_LIMIT_CHARS,
                false,
            )?,
            level: normalize_optional_profile_work_text(
                self.level,
                "Skill level",
                PROFILE_SKILL_LEVEL_LIMIT_CHARS,
                false,
            )?,
            keywords: normalize_profile_section_string_list(
                self.keywords,
                "skill keywords",
                PROFILE_SKILL_KEYWORD_LIMIT,
                PROFILE_SKILL_KEYWORD_LIMIT_CHARS,
                PROFILE_SKILL_KEYWORD_TOTAL_LIMIT_CHARS,
                true,
            )?,
        })
    }
}

impl ProfileSkillEntryPatch {
    fn has_fields(&self) -> bool {
        !self.name.is_missing() || !self.level.is_missing() || !self.keywords.is_missing()
    }

    fn present_fields(&self) -> HashSet<ProfileSectionField> {
        [
            (!self.name.is_missing(), ProfileSectionField::Name),
            (!self.level.is_missing(), ProfileSectionField::Level),
            (!self.keywords.is_missing(), ProfileSectionField::Keywords),
        ]
        .into_iter()
        .filter_map(|(present, field)| present.then_some(field))
        .collect()
    }

    fn validated(self) -> Result<Self, String> {
        if !self.has_fields() {
            return Err("The Skills patch must include at least one field".to_string());
        }
        let name = match self.name {
            RevisionPatchField::Missing => RevisionPatchField::Missing,
            RevisionPatchField::Null => {
                return Err("Skill group name cannot be cleared".to_string());
            }
            RevisionPatchField::Value(value) => {
                RevisionPatchField::Value(normalized_profile_basics_text(
                    value,
                    "Skill group name",
                    PROFILE_SKILL_NAME_LIMIT_CHARS,
                    false,
                )?)
            }
        };
        let keywords = match self.keywords {
            RevisionPatchField::Missing => RevisionPatchField::Missing,
            RevisionPatchField::Null => {
                return Err("Skill keywords cannot be cleared".to_string());
            }
            RevisionPatchField::Value(values) => {
                RevisionPatchField::Value(normalize_profile_section_string_list(
                    values,
                    "skill keywords",
                    PROFILE_SKILL_KEYWORD_LIMIT,
                    PROFILE_SKILL_KEYWORD_LIMIT_CHARS,
                    PROFILE_SKILL_KEYWORD_TOTAL_LIMIT_CHARS,
                    true,
                )?)
            }
        };
        Ok(Self {
            name,
            level: normalized_nullable_profile_patch_field(
                self.level,
                "Skill level",
                PROFILE_SKILL_LEVEL_LIMIT_CHARS,
                false,
            )?,
            keywords,
        })
    }
}

impl ProfileSkillsOperation {
    fn kind(&self) -> ProfileSectionOperationKind {
        match self {
            Self::Add { .. } => ProfileSectionOperationKind::Add,
            Self::Update { .. } => ProfileSectionOperationKind::Update,
            Self::Remove { .. } => ProfileSectionOperationKind::Remove,
        }
    }

    fn target_index(&self) -> Option<u64> {
        match self {
            Self::Add { .. } => None,
            Self::Update { entry_index, .. } | Self::Remove { entry_index } => Some(*entry_index),
        }
    }

    fn present_fields(&self) -> HashSet<ProfileSectionField> {
        match self {
            Self::Add { entry } => {
                let mut fields =
                    HashSet::from([ProfileSectionField::Name, ProfileSectionField::Keywords]);
                if entry.level.is_some() {
                    fields.insert(ProfileSectionField::Level);
                }
                fields
            }
            Self::Update { patch, .. } => patch.present_fields(),
            Self::Remove { .. } => HashSet::from([
                ProfileSectionField::Name,
                ProfileSectionField::Level,
                ProfileSectionField::Keywords,
            ]),
        }
    }

    fn validated(self) -> Result<Self, String> {
        match self {
            Self::Add { entry } => Ok(Self::Add {
                entry: entry.validated()?,
            }),
            Self::Update { entry_index, patch } => {
                validate_profile_section_entry_index(entry_index, "Skills")?;
                Ok(Self::Update {
                    entry_index,
                    patch: patch.validated()?,
                })
            }
            Self::Remove { entry_index } => {
                validate_profile_section_entry_index(entry_index, "Skills")?;
                Ok(Self::Remove { entry_index })
            }
        }
    }
}

impl ProfileEditableSection {
    fn field_order(self, field: ProfileSectionField) -> Option<u8> {
        match self {
            Self::Projects => match field {
                ProfileSectionField::Name => Some(0),
                ProfileSectionField::Description => Some(1),
                ProfileSectionField::Url => Some(2),
                ProfileSectionField::StartDate => Some(3),
                ProfileSectionField::EndDate => Some(4),
                ProfileSectionField::Highlights => Some(5),
                ProfileSectionField::Keywords => Some(6),
                _ => None,
            },
            Self::Education => match field {
                ProfileSectionField::Institution => Some(0),
                ProfileSectionField::StudyType => Some(1),
                ProfileSectionField::Area => Some(2),
                ProfileSectionField::Url => Some(3),
                ProfileSectionField::StartDate => Some(4),
                ProfileSectionField::EndDate => Some(5),
                ProfileSectionField::Score => Some(6),
                ProfileSectionField::Courses => Some(7),
                _ => None,
            },
            Self::Skills => match field {
                ProfileSectionField::Name => Some(0),
                ProfileSectionField::Level => Some(1),
                ProfileSectionField::Keywords => Some(2),
                _ => None,
            },
        }
    }

    fn maximum_fields(self) -> usize {
        match self {
            Self::Projects => 7,
            Self::Education => 8,
            Self::Skills => 3,
        }
    }

    fn required_add_fields(self) -> &'static [ProfileSectionField] {
        match self {
            Self::Projects => &[ProfileSectionField::Name],
            Self::Education => &[ProfileSectionField::Institution],
            Self::Skills => &[ProfileSectionField::Name, ProfileSectionField::Keywords],
        }
    }

    fn display_name(self) -> &'static str {
        match self {
            Self::Projects => "Projects",
            Self::Education => "Education",
            Self::Skills => "Skills",
        }
    }
}

impl ProfileSectionUpdateReceipt {
    #[allow(clippy::too_many_arguments)]
    fn validated(
        self,
        expected_section: ProfileEditableSection,
        expected_parent_profile_version_id: &str,
        expected_request_id: &str,
        expected_operation: ProfileSectionOperationKind,
        expected_entry_index: Option<u64>,
        present_fields: &HashSet<ProfileSectionField>,
    ) -> Result<Self, String> {
        validate_uuid_receipt(&self.request_id)?;
        validate_uuid_receipt(&self.profile_version_id)?;
        validate_uuid_receipt(&self.parent_profile_version_id)?;
        if self.request_id != expected_request_id
            || self.parent_profile_version_id != expected_parent_profile_version_id
            || self.profile_version_id == self.parent_profile_version_id
            || self.section != expected_section
            || self.operation != expected_operation
            || self.version_number < 2
            || self.created_at_ms == 0
            || self.entry_index >= PROFILE_SECTION_ENTRY_LIMIT
            || self.changed_fields.is_empty()
            || self.changed_fields.len() > expected_section.maximum_fields()
            || self.section_entries > PROFILE_SECTION_ENTRY_LIMIT
        {
            return Err(format!(
                "The local engine returned a mismatched {} receipt",
                expected_section.display_name()
            ));
        }
        for (value, field_name) in [
            (self.version_number, "profile version"),
            (self.created_at_ms, "profile version timestamp"),
            (self.entry_index, "profile section entry index"),
            (self.section_entries, "profile section entry count"),
        ] {
            validate_javascript_integer(value, field_name)?;
        }
        if self.operation == ProfileSectionOperationKind::Add && self.section_entries == 0 {
            return Err(format!(
                "The local engine returned an invalid {} count",
                expected_section.display_name()
            ));
        }
        if expected_entry_index.is_some_and(|expected| self.entry_index != expected) {
            return Err(format!(
                "The local engine returned a mismatched {} entry index",
                expected_section.display_name()
            ));
        }
        let mut previous_order = None;
        for field in &self.changed_fields {
            let order = expected_section.field_order(*field).ok_or_else(|| {
                format!(
                    "The local engine returned invalid {} changed fields",
                    expected_section.display_name()
                )
            })?;
            if previous_order.is_some_and(|previous| order <= previous)
                || !present_fields.contains(field)
            {
                return Err(format!(
                    "The local engine returned invalid {} changed fields",
                    expected_section.display_name()
                ));
            }
            previous_order = Some(order);
        }
        if self.operation == ProfileSectionOperationKind::Add
            && expected_section
                .required_add_fields()
                .iter()
                .any(|field| !self.changed_fields.contains(field))
        {
            return Err(format!(
                "The local engine returned an incomplete {} receipt",
                expected_section.display_name()
            ));
        }
        if self.operation == ProfileSectionOperationKind::Add
            && expected_section == ProfileEditableSection::Education
            && !self
                .changed_fields
                .contains(&ProfileSectionField::StudyType)
            && !self.changed_fields.contains(&ProfileSectionField::Area)
        {
            return Err("The local engine returned an incomplete Education receipt".to_string());
        }
        validate_profile_basics_text(
            &self.profile_name,
            "name",
            PROFILE_REVIEW_NAME_LIMIT_CHARS,
            false,
        )?;
        validate_profile_review_serialized_size(
            &self,
            PROFILE_SECTION_RECEIPT_LIMIT_BYTES,
            "profile section update receipt",
        )?;
        Ok(self)
    }
}

impl ProfileChangeSection {
    fn order(self) -> u8 {
        match self {
            Self::Work => 0,
            Self::Projects => 1,
            Self::Education => 2,
            Self::Skills => 3,
        }
    }

    fn display_name(self) -> &'static str {
        match self {
            Self::Work => "Work History",
            Self::Projects => "Projects",
            Self::Education => "Education",
            Self::Skills => "Skills",
        }
    }

    fn entry_limit(self) -> u64 {
        match self {
            Self::Work => PROFILE_WORK_ENTRY_LIMIT,
            Self::Projects | Self::Education | Self::Skills => PROFILE_SECTION_ENTRY_LIMIT,
        }
    }
}

fn validate_profile_change_suggestion_id(value: &str, engine_output: bool) -> Result<(), String> {
    if value.len() != 64
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err(if engine_output {
            "The local engine returned an invalid profile merge suggestion".to_string()
        } else {
            "The profile merge suggestion ID is invalid".to_string()
        });
    }
    Ok(())
}

fn validate_profile_change_entry_index(
    section: ProfileChangeSection,
    entry_index: u64,
    engine_output: bool,
) -> Result<(), String> {
    if entry_index >= section.entry_limit()
        || validate_javascript_integer(entry_index, "profile change entry index").is_err()
    {
        return Err(if engine_output {
            "The local engine returned an invalid profile change entry index".to_string()
        } else {
            format!(
                "The queued {} entry index is invalid",
                section.display_name()
            )
        });
    }
    Ok(())
}

fn validate_profile_work_changed_fields(fields: &[ProfileWorkField]) -> Result<(), String> {
    let mut previous_order = None;
    for field in fields {
        let order = field.order();
        if previous_order.is_some_and(|previous| order <= previous) {
            return Err(
                "The local engine returned invalid profile merge changed fields".to_string(),
            );
        }
        previous_order = Some(order);
    }
    Ok(())
}

fn validate_profile_section_changed_fields(
    section: ProfileEditableSection,
    fields: &[ProfileSectionField],
) -> Result<(), String> {
    let mut previous_order = None;
    for field in fields {
        let order = section.field_order(*field).ok_or_else(|| {
            "The local engine returned invalid profile merge changed fields".to_string()
        })?;
        if previous_order.is_some_and(|previous| order <= previous) {
            return Err(
                "The local engine returned invalid profile merge changed fields".to_string(),
            );
        }
        previous_order = Some(order);
    }
    Ok(())
}

fn validate_profile_change_merged_entry<T>(
    entry: &T,
    validator: impl FnOnce(T) -> Result<T, String>,
) -> Result<(), String>
where
    T: Clone + PartialEq,
{
    let normalized = validator(entry.clone())
        .map_err(|_| "The local engine returned an invalid profile merge preview".to_string())?;
    if &normalized != entry {
        return Err("The local engine returned a non-normalized profile merge preview".to_string());
    }
    Ok(())
}

impl ProfileChangeSuggestion {
    fn section(&self) -> ProfileChangeSection {
        match self {
            Self::Work { .. } => ProfileChangeSection::Work,
            Self::Projects { .. } => ProfileChangeSection::Projects,
            Self::Education { .. } => ProfileChangeSection::Education,
            Self::Skills { .. } => ProfileChangeSection::Skills,
        }
    }

    fn suggestion_id(&self) -> &str {
        match self {
            Self::Work { suggestion_id, .. }
            | Self::Projects { suggestion_id, .. }
            | Self::Education { suggestion_id, .. }
            | Self::Skills { suggestion_id, .. } => suggestion_id,
        }
    }

    fn entry_indexes(&self) -> (u64, u64) {
        match self {
            Self::Work {
                keep_entry_index,
                remove_entry_index,
                ..
            }
            | Self::Projects {
                keep_entry_index,
                remove_entry_index,
                ..
            }
            | Self::Education {
                keep_entry_index,
                remove_entry_index,
                ..
            }
            | Self::Skills {
                keep_entry_index,
                remove_entry_index,
                ..
            } => (*keep_entry_index, *remove_entry_index),
        }
    }

    fn validate(&self) -> Result<(), String> {
        validate_profile_change_suggestion_id(self.suggestion_id(), true)?;
        let section = self.section();
        let (keep_entry_index, remove_entry_index) = self.entry_indexes();
        validate_profile_change_entry_index(section, keep_entry_index, true)?;
        validate_profile_change_entry_index(section, remove_entry_index, true)?;
        if keep_entry_index >= remove_entry_index {
            return Err("The local engine returned an invalid profile merge pair".to_string());
        }

        match self {
            Self::Work {
                reason_codes,
                merged_entry,
                changed_fields,
                ..
            } => {
                if reason_codes != &[ProfileChangeReasonCode::EmployerRoleAndDates] {
                    return Err(
                        "The local engine returned an invalid profile merge reason".to_string()
                    );
                }
                validate_profile_change_merged_entry(merged_entry, |entry| entry.validated())?;
                validate_profile_work_changed_fields(changed_fields)?;
            }
            Self::Projects {
                reason_codes,
                merged_entry,
                changed_fields,
                ..
            } => {
                if reason_codes != &[ProfileChangeReasonCode::ProjectNameAndDates] {
                    return Err(
                        "The local engine returned an invalid profile merge reason".to_string()
                    );
                }
                validate_profile_change_merged_entry(merged_entry, |entry| entry.validated())?;
                validate_profile_section_changed_fields(
                    ProfileEditableSection::Projects,
                    changed_fields,
                )?;
            }
            Self::Education {
                reason_codes,
                merged_entry,
                changed_fields,
                ..
            } => {
                if reason_codes != &[ProfileChangeReasonCode::InstitutionProgramAndDates] {
                    return Err(
                        "The local engine returned an invalid profile merge reason".to_string()
                    );
                }
                validate_profile_change_merged_entry(merged_entry, |entry| entry.validated())?;
                validate_profile_section_changed_fields(
                    ProfileEditableSection::Education,
                    changed_fields,
                )?;
            }
            Self::Skills {
                reason_codes,
                merged_entry,
                changed_fields,
                ..
            } => {
                if reason_codes != &[ProfileChangeReasonCode::SkillGroup] {
                    return Err(
                        "The local engine returned an invalid profile merge reason".to_string()
                    );
                }
                validate_profile_change_merged_entry(merged_entry, |entry| entry.validated())?;
                validate_profile_section_changed_fields(
                    ProfileEditableSection::Skills,
                    changed_fields,
                )?;
            }
        }
        Ok(())
    }
}

impl ProfileChangesPreview {
    fn validated(self, expected_profile_version_id: &str) -> Result<Self, String> {
        validate_uuid_receipt(&self.profile_version_id)?;
        if self.profile_version_id != expected_profile_version_id
            || self.version_number == 0
            || self.algorithm_version != 1
            || self.suggestions.len() > PROFILE_CHANGE_SUGGESTION_LIMIT
        {
            return Err(
                "The local engine returned a mismatched profile changes preview".to_string(),
            );
        }
        validate_javascript_integer(self.version_number, "profile version")?;
        validate_javascript_integer(self.algorithm_version, "profile merge algorithm version")?;

        let mut suggestion_ids = HashSet::new();
        let mut suggestion_targets = HashSet::new();
        let mut previous_sort_key = None;
        for suggestion in &self.suggestions {
            suggestion.validate()?;
            if !suggestion_ids.insert(suggestion.suggestion_id()) {
                return Err(
                    "The local engine returned duplicate profile merge suggestions".to_string(),
                );
            }
            let section = suggestion.section();
            let (keep_entry_index, remove_entry_index) = suggestion.entry_indexes();
            let sort_key = (section.order(), keep_entry_index, remove_entry_index);
            if previous_sort_key.is_some_and(|previous| sort_key <= previous) {
                return Err(
                    "The local engine returned unordered profile merge suggestions".to_string(),
                );
            }
            previous_sort_key = Some(sort_key);
            if !suggestion_targets.insert((section, keep_entry_index))
                || !suggestion_targets.insert((section, remove_entry_index))
            {
                return Err(
                    "The local engine returned overlapping profile merge suggestions".to_string(),
                );
            }
        }
        validate_profile_review_serialized_size(
            &self,
            PROFILE_CHANGE_PREVIEW_LIMIT_BYTES,
            "profile changes preview",
        )?;
        Ok(self)
    }
}

impl ProfileChangeOperation {
    fn section(&self) -> ProfileChangeSection {
        match self {
            Self::Remove { section, .. }
            | Self::Update { section, .. }
            | Self::Merge { section, .. } => *section,
        }
    }

    fn target_indexes(&self) -> (u64, Option<u64>) {
        match self {
            Self::Remove { entry_index, .. } | Self::Update { entry_index, .. } => {
                (*entry_index, None)
            }
            Self::Merge {
                keep_entry_index,
                remove_entry_index,
                ..
            } => (*keep_entry_index, Some(*remove_entry_index)),
        }
    }

    fn validated(self) -> Result<Self, String> {
        match self {
            Self::Remove {
                section,
                entry_index,
            } => {
                validate_profile_change_entry_index(section, entry_index, false)?;
                Ok(Self::Remove {
                    section,
                    entry_index,
                })
            }
            Self::Update {
                section,
                entry_index,
                patch,
            } => {
                validate_profile_change_entry_index(section, entry_index, false)?;
                let patch = match section {
                    ProfileChangeSection::Work => serde_json::to_value(
                        serde_json::from_value::<ProfileWorkEntryPatch>(patch)
                            .map_err(|_| "The queued Work History patch is invalid".to_string())?
                            .validated()?,
                    ),
                    ProfileChangeSection::Projects => serde_json::to_value(
                        serde_json::from_value::<ProfileProjectEntryPatch>(patch)
                            .map_err(|_| "The queued Projects patch is invalid".to_string())?
                            .validated()?,
                    ),
                    ProfileChangeSection::Education => serde_json::to_value(
                        serde_json::from_value::<ProfileEducationEntryPatch>(patch)
                            .map_err(|_| "The queued Education patch is invalid".to_string())?
                            .validated()?,
                    ),
                    ProfileChangeSection::Skills => serde_json::to_value(
                        serde_json::from_value::<ProfileSkillEntryPatch>(patch)
                            .map_err(|_| "The queued Skills patch is invalid".to_string())?
                            .validated()?,
                    ),
                }
                .map_err(|_| "The desktop could not encode the queued profile patch".to_string())?;
                Ok(Self::Update {
                    section,
                    entry_index,
                    patch,
                })
            }
            Self::Merge {
                section,
                keep_entry_index,
                remove_entry_index,
                suggestion_id,
            } => {
                validate_profile_change_entry_index(section, keep_entry_index, false)?;
                validate_profile_change_entry_index(section, remove_entry_index, false)?;
                if keep_entry_index >= remove_entry_index {
                    return Err("A profile merge must keep the earlier suggested entry".to_string());
                }
                validate_profile_change_suggestion_id(&suggestion_id, false)?;
                Ok(Self::Merge {
                    section,
                    keep_entry_index,
                    remove_entry_index,
                    suggestion_id,
                })
            }
        }
    }
}

fn validated_profile_change_operations(
    operations: Vec<ProfileChangeOperation>,
) -> Result<Vec<ProfileChangeOperation>, String> {
    if operations.is_empty() || operations.len() > PROFILE_CHANGE_OPERATION_LIMIT {
        return Err("Queue between one and 64 profile changes before applying".to_string());
    }

    let mut validated = Vec::with_capacity(operations.len());
    let mut targets = HashSet::new();
    for operation in operations {
        let operation = operation.validated()?;
        let section = operation.section();
        let (first, second) = operation.target_indexes();
        if !targets.insert((section, first))
            || second.is_some_and(|entry_index| !targets.insert((section, entry_index)))
        {
            return Err("Each queued profile entry may be changed only once".to_string());
        }
        validated.push(operation);
    }

    let encoded = serde_json::to_vec(&validated)
        .map_err(|_| "The desktop could not encode the queued profile changes".to_string())?;
    if encoded.len() > PROFILE_CHANGE_REQUEST_LIMIT_BYTES {
        return Err("The queued profile changes exceed the local size limit".to_string());
    }
    Ok(validated)
}

impl ProfileChangesApplyReceipt {
    fn validated(
        self,
        expected_parent_profile_version_id: &str,
        expected_request_id: &str,
        operations: &[ProfileChangeOperation],
    ) -> Result<Self, String> {
        validate_uuid_receipt(&self.request_id)?;
        validate_uuid_receipt(&self.profile_version_id)?;
        validate_uuid_receipt(&self.parent_profile_version_id)?;
        if self.request_id != expected_request_id
            || self.parent_profile_version_id != expected_parent_profile_version_id
            || self.profile_version_id == self.parent_profile_version_id
            || self.version_number < 2
            || self.created_at_ms == 0
            || self.operation_count != operations.len() as u64
        {
            return Err(
                "The local engine returned a mismatched profile changes receipt".to_string(),
            );
        }
        for (value, field_name) in [
            (self.version_number, "profile version"),
            (self.created_at_ms, "profile version timestamp"),
            (self.operation_count, "profile change operation count"),
            (self.section_counts.work, "Work History entry count"),
            (self.section_counts.projects, "Projects entry count"),
            (self.section_counts.education, "Education entry count"),
            (self.section_counts.skills, "Skills entry count"),
        ] {
            validate_javascript_integer(value, field_name)?;
        }
        if self.section_counts.work > PROFILE_WORK_ENTRY_LIMIT
            || self.section_counts.projects > PROFILE_SECTION_ENTRY_LIMIT
            || self.section_counts.education > PROFILE_SECTION_ENTRY_LIMIT
            || self.section_counts.skills > PROFILE_SECTION_ENTRY_LIMIT
        {
            return Err("The local engine returned invalid profile section counts".to_string());
        }

        let expected_sections = [
            ProfileChangeSection::Work,
            ProfileChangeSection::Projects,
            ProfileChangeSection::Education,
            ProfileChangeSection::Skills,
        ]
        .into_iter()
        .filter(|section| {
            operations
                .iter()
                .any(|operation| operation.section() == *section)
        })
        .collect::<Vec<_>>();
        if self.changed_sections != expected_sections {
            return Err("The local engine returned invalid changed profile sections".to_string());
        }
        validate_profile_basics_text(
            &self.profile_name,
            "name",
            PROFILE_REVIEW_NAME_LIMIT_CHARS,
            false,
        )?;
        validate_profile_review_serialized_size(
            &self,
            PROFILE_CHANGE_RECEIPT_LIMIT_BYTES,
            "profile changes receipt",
        )?;
        Ok(self)
    }
}

fn profile_changes_preview_params(profile_version_id: &str) -> Value {
    json!({"profile_version_id": profile_version_id})
}

fn profile_changes_apply_params(
    expected_parent_profile_version_id: &str,
    request_id: &str,
    operations: &[ProfileChangeOperation],
) -> Value {
    json!({
        "expected_parent_profile_version_id": expected_parent_profile_version_id,
        "request_id": request_id,
        "operations": operations,
    })
}

fn validated_profile_changes_request_id(request_id: &str) -> Result<String, String> {
    Uuid::parse_str(request_id)
        .ok()
        .filter(|parsed| parsed.to_string() == request_id)
        .map(|parsed| parsed.to_string())
        .ok_or_else(|| "The profile changes request ID is invalid".to_string())
}

fn profile_section_list_params(profile_version_id: &str, offset: usize) -> Value {
    json!({"profile_version_id": profile_version_id, "offset": offset})
}

fn profile_section_update_params<T: Serialize>(
    expected_parent_profile_version_id: &str,
    request_id: &str,
    operation: &T,
) -> Value {
    json!({
        "expected_parent_profile_version_id": expected_parent_profile_version_id,
        "request_id": request_id,
        "operation": operation,
    })
}

fn validated_profile_section_request_id(
    request_id: &str,
    section_name: &str,
) -> Result<String, String> {
    Uuid::parse_str(request_id)
        .ok()
        .filter(|parsed| parsed.to_string() == request_id)
        .map(|parsed| parsed.to_string())
        .ok_or_else(|| format!("The {section_name} update request ID is invalid"))
}

fn validated_profile_version_offset(offset: usize) -> Result<usize, String> {
    if offset > PROFILE_VERSION_LIST_OFFSET_LIMIT {
        return Err("The profile version offset exceeds the local limit".to_string());
    }
    Ok(offset)
}

fn profile_version_list_params(offset: usize) -> Value {
    json!({"offset": offset})
}

fn profile_version_identity_params(profile_version_id: &str) -> Value {
    json!({"profile_version_id": profile_version_id})
}

fn profile_version_diff_params(
    from_profile_version_id: &str,
    to_profile_version_id: &str,
) -> Value {
    json!({
        "from_profile_version_id": from_profile_version_id,
        "to_profile_version_id": to_profile_version_id,
    })
}

fn validated_profile_restore_ids(
    source_profile_version_id: &str,
    expected_parent_profile_version_id: &str,
) -> Result<(String, String), String> {
    let source_profile_version_id = validated_profile_review_version_id(source_profile_version_id)?;
    let expected_parent_profile_version_id =
        validated_profile_review_version_id(expected_parent_profile_version_id)?;
    if source_profile_version_id == expected_parent_profile_version_id {
        return Err("Choose a historical profile version to restore".to_string());
    }
    Ok((
        source_profile_version_id,
        expected_parent_profile_version_id,
    ))
}

fn validated_profile_restore_request_id(value: &str) -> Result<String, String> {
    Uuid::parse_str(value)
        .ok()
        .filter(|parsed| parsed.to_string() == value)
        .map(|parsed| parsed.to_string())
        .ok_or_else(|| "The profile restore request ID is invalid".to_string())
}

fn profile_restore_params(
    source_profile_version_id: &str,
    expected_parent_profile_version_id: &str,
    request_id: &str,
) -> Value {
    json!({
        "source_profile_version_id": source_profile_version_id,
        "expected_parent_profile_version_id": expected_parent_profile_version_id,
        "request_id": request_id,
    })
}

fn profile_basics_update_params(
    expected_parent_profile_version_id: &str,
    request_id: &str,
    patch: &ProfileBasicsPatch,
) -> Value {
    json!({
        "expected_parent_profile_version_id": expected_parent_profile_version_id,
        "request_id": request_id,
        "patch": patch,
    })
}

fn validated_manual_profile_expected_parent(value: &Value) -> Result<(), String> {
    if value.is_null() {
        Ok(())
    } else {
        Err("A manual profile can only be created without a parent profile".to_string())
    }
}

fn validated_manual_profile_request_id(value: &str) -> Result<String, String> {
    Uuid::parse_str(value)
        .ok()
        .filter(|parsed| parsed.to_string() == value)
        .map(|parsed| parsed.to_string())
        .ok_or_else(|| "The manual profile request ID is invalid".to_string())
}

fn manual_profile_start_params(
    request_id: &str,
    patch: &ProfileBasicsPatch,
) -> Result<Value, String> {
    let params = json!({
        "expected_parent_profile_version_id": Value::Null,
        "request_id": request_id,
        "patch": patch,
    });
    let encoded = serde_json::to_vec(&params)
        .map_err(|_| "The manual profile request could not be encoded safely".to_string())?;
    if encoded.len() > MANUAL_PROFILE_START_REQUEST_LIMIT_BYTES {
        return Err("The manual profile request exceeds the local size limit".to_string());
    }
    Ok(params)
}

impl RetainedSourceStatus {
    fn validated(self) -> Result<Self, String> {
        if self.source_retention_generation == 0 {
            return Err("The local engine returned an invalid source generation".to_string());
        }
        for (value, field_name) in [
            (
                self.source_retention_generation,
                "source retention generation",
            ),
            (self.retained_source_files, "retained source file count"),
            (self.retained_source_bytes, "retained source byte count"),
            (self.retained_source_imports, "retained source import count"),
            (self.historical_source_files, "historical source file count"),
            (
                self.historical_source_imports,
                "historical source import count",
            ),
        ] {
            validate_javascript_integer(value, field_name)?;
        }
        for timestamp in [
            self.last_source_import_at_ms,
            self.source_retention_reset_at_ms,
        ]
        .into_iter()
        .flatten()
        {
            if timestamp == 0 {
                return Err("The local engine returned an invalid source timestamp".to_string());
            }
            validate_javascript_integer(timestamp, "source timestamp")?;
        }

        let current_is_empty = self.retained_source_imports == 0;
        if current_is_empty != (self.retained_source_files == 0)
            || current_is_empty != (self.retained_source_bytes == 0)
            || current_is_empty != self.last_source_import_at_ms.is_none()
        {
            return Err(
                "The local engine returned inconsistent retained source totals".to_string(),
            );
        }
        if (self.historical_source_imports == 0) != (self.historical_source_files == 0) {
            return Err(
                "The local engine returned inconsistent historical source totals".to_string(),
            );
        }
        if self.source_retention_generation == 1 {
            if self.source_retention_reset_at_ms.is_some()
                || self.historical_source_files != 0
                || self.historical_source_imports != 0
            {
                return Err(
                    "The local engine returned an invalid initial source generation".to_string(),
                );
            }
        } else if self.source_retention_reset_at_ms.is_none() {
            return Err("The local engine returned an incomplete source reset receipt".to_string());
        }
        Ok(self)
    }
}

impl EngineStatusWire {
    fn validated(self) -> Result<EngineStatus, String> {
        if self.engine_version != env!("CARGO_PKG_VERSION") {
            return Err(format!(
                "The local engine version ({}) is incompatible with this app ({})",
                self.engine_version,
                env!("CARGO_PKG_VERSION")
            ));
        }
        if self.schema_version != ENGINE_SCHEMA_VERSION {
            return Err(format!(
                "The local engine schema ({}) is incompatible with this app ({ENGINE_SCHEMA_VERSION})",
                self.schema_version
            ));
        }
        if self.database_path.is_empty()
            || self.database_path.len() > 32_768
            || self.database_path.chars().any(char::is_control)
        {
            return Err("The local engine returned an invalid vault location".to_string());
        }
        for (value, field_name) in [
            (self.profile_versions, "profile version count"),
            (self.opportunities, "opportunity count"),
            (self.artifacts, "artifact count"),
            (self.pending_jobs, "pending job count"),
            (self.source_snapshots, "source snapshot count"),
            (self.source_imports, "source import count"),
            (self.source_previews, "source preview count"),
            (self.review_inbox_items, "profile review inbox count"),
            (self.review_deferred_items, "profile review deferred count"),
            (self.review_history_items, "profile review history count"),
            (self.source_review_inbox_items, "source review inbox count"),
            (self.memory_count, "memory count"),
            (
                self.source_review_deferred_items,
                "source review deferred count",
            ),
            (
                self.source_review_history_items,
                "source review history count",
            ),
        ] {
            validate_javascript_integer(value, field_name)?;
        }
        if let Some(profile_name) = self.latest_profile_name.as_deref() {
            validate_engine_text(
                profile_name,
                "profile name",
                PROFILE_NAME_LIMIT_CHARS,
                false,
                false,
            )?;
        }

        let retention = RetainedSourceStatus {
            source_retention_generation: self.source_retention_generation,
            retained_source_files: self.retained_source_files,
            retained_source_bytes: self.retained_source_bytes,
            retained_source_imports: self.retained_source_imports,
            historical_source_files: self.historical_source_files,
            historical_source_imports: self.historical_source_imports,
            last_source_import_at_ms: self.last_source_import_at_ms,
            source_retention_reset_at_ms: self.source_retention_reset_at_ms,
        }
        .validated()?;
        if self.source_imports
            != retention
                .retained_source_imports
                .checked_add(retention.historical_source_imports)
                .ok_or_else(|| {
                    "The local engine returned inconsistent source import totals".to_string()
                })?
            || retention.retained_source_files > self.source_snapshots
            || retention.historical_source_files > self.source_snapshots
        {
            return Err("The local engine returned inconsistent source totals".to_string());
        }

        Ok(EngineStatus {
            engine_version: self.engine_version,
            schema_version: self.schema_version,
            profile_versions: self.profile_versions,
            opportunities: self.opportunities,
            artifacts: self.artifacts,
            pending_jobs: self.pending_jobs,
            source_snapshots: self.source_snapshots,
            source_imports: self.source_imports,
            source_previews: self.source_previews,
            review_inbox_items: self.review_inbox_items,
            review_deferred_items: self.review_deferred_items,
            review_history_items: self.review_history_items,
            source_review_inbox_items: self.source_review_inbox_items,
            source_review_deferred_items: self.source_review_deferred_items,
            source_review_history_items: self.source_review_history_items,
            memory_count: self.memory_count,
            source_retention_generation: retention.source_retention_generation,
            retained_source_files: retention.retained_source_files,
            retained_source_bytes: retention.retained_source_bytes,
            retained_source_imports: retention.retained_source_imports,
            historical_source_files: retention.historical_source_files,
            historical_source_imports: retention.historical_source_imports,
            last_source_import_at_ms: retention.last_source_import_at_ms,
            source_retention_reset_at_ms: retention.source_retention_reset_at_ms,
            latest_profile_name: self.latest_profile_name,
            latest_profile_renderable: self.latest_profile_renderable,
        })
    }
}

impl ProfileImportResult {
    fn validated(self) -> Result<Self, String> {
        validate_uuid_receipt(&self.profile_version_id)?;
        validate_sha256(&self.checksum_sha256)?;
        validate_engine_text(
            &self.profile_name,
            "profile name",
            PROFILE_NAME_LIMIT_CHARS,
            false,
            false,
        )?;
        if self.version_number == 0 {
            return Err("The local engine returned an invalid profile version".to_string());
        }
        for (value, field_name) in [
            (self.version_number, "profile version"),
            (self.work_entries, "work entry count"),
            (self.education_entries, "education entry count"),
            (self.skill_groups, "skill group count"),
        ] {
            validate_javascript_integer(value, field_name)?;
        }
        Ok(self)
    }
}

fn validate_profile_compaction_report(report: &Value) -> Result<(), String> {
    let root = report.as_object().ok_or_else(|| {
        "The local engine returned an invalid profile compaction report".to_string()
    })?;
    if root.get("version").and_then(Value::as_u64) != Some(1)
        || root.get("changed").and_then(Value::as_bool).is_none()
        || !root.get("metrics_before").is_some_and(Value::is_object)
        || !root.get("metrics_after").is_some_and(Value::is_object)
    {
        return Err("The local engine returned an invalid profile compaction report".to_string());
    }
    let encoded = serde_json::to_vec(report).map_err(|_| {
        "The local engine returned an invalid profile compaction report".to_string()
    })?;
    if encoded.len() > PROFILE_COMPACTION_REPORT_LIMIT_BYTES {
        return Err("The local engine returned an oversized profile compaction report".to_string());
    }

    let mut pending = vec![(report, 0_usize)];
    let mut nodes = 0_usize;
    while let Some((value, depth)) = pending.pop() {
        nodes = nodes.saturating_add(1);
        if nodes > PROFILE_COMPACTION_REPORT_MAX_NODES
            || depth > PROFILE_COMPACTION_REPORT_MAX_DEPTH
        {
            return Err(
                "The local engine returned an invalid profile compaction report".to_string(),
            );
        }
        match value {
            Value::Object(object) => {
                for (key, child) in object {
                    if key.is_empty()
                        || key.len() > PROFILE_COMPACTION_REPORT_MAX_KEY_CHARS
                        || key.chars().count() > PROFILE_COMPACTION_REPORT_MAX_KEY_CHARS
                        || key.chars().any(char::is_control)
                    {
                        return Err(
                            "The local engine returned an invalid profile compaction report"
                                .to_string(),
                        );
                    }
                    pending.push((child, depth + 1));
                }
            }
            Value::Array(items) => {
                pending.extend(items.iter().map(|item| (item, depth + 1)));
            }
            Value::String(text) => {
                if text.len() > PROFILE_COMPACTION_REPORT_MAX_STRING_CHARS
                    || text.chars().count() > PROFILE_COMPACTION_REPORT_MAX_STRING_CHARS
                    || text.chars().any(char::is_control)
                {
                    return Err(
                        "The local engine returned an invalid profile compaction report"
                            .to_string(),
                    );
                }
            }
            Value::Number(number) => {
                let value = number.as_u64().ok_or_else(|| {
                    "The local engine returned an invalid profile compaction report".to_string()
                })?;
                validate_javascript_integer(value, "profile compaction metric")?;
            }
            Value::Bool(_) | Value::Null => {}
        }
    }
    Ok(())
}

impl ProfileImportResultWire {
    fn validated(self) -> Result<ProfileImportResult, String> {
        validate_profile_compaction_report(&self.compaction_report)?;
        ProfileImportResult {
            profile_version_id: self.profile_version_id,
            version_number: self.version_number,
            created: self.created,
            checksum_sha256: self.checksum_sha256,
            profile_name: self.profile_name,
            work_entries: self.work_entries,
            education_entries: self.education_entries,
            skill_groups: self.skill_groups,
            renderable: self.renderable,
        }
        .validated()
    }
}

impl ProfileSourceScanResult {
    fn validated_review_candidate_count(self) -> Result<Self, String> {
        source_review::validate_source_review_count(self.source_review_count)?;
        validate_javascript_integer(self.review_candidate_count, "source review candidate count")?;
        if self.review_candidate_count > PROFILE_SOURCE_REVIEW_CANDIDATE_LIMIT {
            return Err("The local engine returned too many source review candidates".to_string());
        }
        validate_profile_review_serialized_size(
            &self,
            PROFILE_REVIEW_PAGE_LIMIT_BYTES,
            "source preview",
        )?;
        Ok(self)
    }
}

impl ProfileSourceBuildResult {
    fn validated_review_item_count(self) -> Result<Self, String> {
        source_review::validate_source_review_count(self.source_review_count)?;
        validate_javascript_integer(self.review_item_count, "created review item count")?;
        if self.review_item_count > PROFILE_SOURCE_REVIEW_CANDIDATE_LIMIT {
            return Err("The local engine returned too many created review items".to_string());
        }
        validate_profile_review_serialized_size(
            &self,
            PROFILE_REVIEW_PAGE_LIMIT_BYTES,
            "source build receipt",
        )?;
        Ok(self)
    }
}

impl DiscardUnfinishedSourceReviewsResult {
    fn validated(self) -> Result<Self, String> {
        validate_javascript_integer(self.discarded_previews, "discarded source preview count")?;
        Ok(self)
    }
}

fn canonical_import_params(source: StagedCanonicalProfile) -> Value {
    json!({"source": source})
}

fn discard_unfinished_source_reviews_params() -> Value {
    json!({})
}

fn cleanup_failed_canonical_import<T>(
    data_dir: &Path,
    scan_id: &str,
    result: Result<T, String>,
) -> Result<T, String> {
    if result.is_err() {
        // Cleanup is best effort so a filesystem issue never hides the engine
        // error that explains why the import did not produce a receipt.
        let _ = cleanup_staging(data_dir, scan_id);
    }
    result
}

fn source_reset_params(expected_generation: u64, request_id: &str) -> Result<Value, String> {
    if expected_generation == 0 || expected_generation >= JAVASCRIPT_MAX_SAFE_INTEGER {
        return Err("The source retention generation is invalid".to_string());
    }
    let parsed = Uuid::parse_str(request_id)
        .ok()
        .filter(|value| value.to_string() == request_id)
        .ok_or_else(|| "The source reset request ID is invalid".to_string())?;
    Ok(json!({
        "expected_generation": expected_generation,
        "request_id": parsed.to_string(),
    }))
}

impl CareerWorkspaceResetExpected {
    fn validated(&self) -> Result<(), String> {
        if self.source_retention_generation == 0 {
            return Err("The career workspace source generation is invalid".to_string());
        }
        for (value, field_name) in [
            (self.profile_versions, "profile version count"),
            (self.opportunities, "opportunity count"),
            (self.artifacts, "artifact count"),
            (self.source_imports, "source import count"),
            (self.source_previews, "source preview count"),
            (self.review_inbox_items, "review inbox item count"),
            (self.review_deferred_items, "deferred review item count"),
            (self.review_history_items, "review history item count"),
            (self.source_review_inbox_items, "source review inbox count"),
            (self.memory_count, "memory count"),
            (
                self.source_review_deferred_items,
                "source review deferred count",
            ),
            (
                self.source_review_history_items,
                "source review history count",
            ),
            (
                self.source_retention_generation,
                "source retention generation",
            ),
        ] {
            validate_javascript_integer(value, field_name)
                .map_err(|_| format!("The career workspace {field_name} is invalid"))?;
        }
        Ok(())
    }

    fn engine_value(&self) -> Value {
        json!({
            "profile_versions": self.profile_versions,
            "opportunities": self.opportunities,
            "artifacts": self.artifacts,
            "source_imports": self.source_imports,
            "source_previews": self.source_previews,
            "source_retention_generation": self.source_retention_generation,
            "review_inbox_items": self.review_inbox_items,
            "review_deferred_items": self.review_deferred_items,
            "review_history_items": self.review_history_items,
            "source_review_inbox_items": self.source_review_inbox_items,
            "source_review_deferred_items": self.source_review_deferred_items,
            "source_review_history_items": self.source_review_history_items,
            "memory_count": self.memory_count,
        })
    }
}

fn career_workspace_reset_params(
    expected: &CareerWorkspaceResetExpected,
    request_id: &str,
) -> Result<Value, String> {
    expected.validated()?;
    let parsed = Uuid::parse_str(request_id)
        .ok()
        .filter(|value| value.to_string() == request_id)
        .ok_or_else(|| "The career workspace reset request ID is invalid".to_string())?;
    Ok(json!({
        "request_id": parsed.to_string(),
        "expected": expected.engine_value(),
    }))
}

impl CareerWorkspaceResetReceipt {
    fn validated(self, expected_request_id: &str) -> Result<Self, String> {
        validate_uuid_receipt(&self.request_id)?;
        if self.request_id != expected_request_id {
            return Err("The local engine returned a mismatched reset receipt".to_string());
        }
        if self.reset_at_ms == 0 {
            return Err("The local engine returned an invalid reset timestamp".to_string());
        }
        validate_javascript_integer(self.reset_at_ms, "career workspace reset timestamp")?;
        if self.schema_version != ENGINE_SCHEMA_VERSION {
            return Err(format!(
                "The local engine reset schema ({}) is incompatible with this app ({ENGINE_SCHEMA_VERSION})",
                self.schema_version
            ));
        }
        if self.source_retention_generation != 1
            || self.profile_versions != 0
            || self.opportunities != 0
            || self.artifacts != 0
            || self.source_imports != 0
            || self.source_previews != 0
            || self.review_inbox_items != 0
            || self.review_deferred_items != 0
            || self.review_history_items != 0
            || self.source_review_inbox_items != 0
            || self.source_review_deferred_items != 0
            || self.source_review_history_items != 0
            || self.memory_count != 0
        {
            return Err("The local engine did not return a pristine career workspace".to_string());
        }
        Ok(self)
    }
}

fn validated_source_reset_result(
    expected_generation: u64,
    result: RetainedSourceStatus,
) -> Result<RetainedSourceStatus, String> {
    let result = result.validated()?;
    let expected_result_generation = expected_generation
        .checked_add(1)
        .ok_or_else(|| "The source retention generation is invalid".to_string())?;
    if result.source_retention_generation != expected_result_generation {
        return Err("The local engine returned a mismatched source generation".to_string());
    }
    Ok(result)
}

fn safe_opportunity_error(error: String) -> String {
    let code = error.split_once(':').map(|(code, _)| code.trim());
    match code {
        Some("profile_missing") => {
            "profile_missing: Build or import a career profile before assessing a role.".to_string()
        }
        Some("opportunity_not_found") => {
            "opportunity_not_found: That saved opportunity is no longer available.".to_string()
        }
        Some("opportunity_tracker_conflict") => concat!(
            "opportunity_tracker_conflict: This opportunity changed since you opened it. ",
            "Review the latest tracker state and try again."
        )
        .to_string(),
        Some("invalid_params") => {
            "invalid_params: Check the opportunity fields and try again.".to_string()
        }
        Some("vault_busy") => {
            "vault_busy: The local vault is busy; wait a moment and try again.".to_string()
        }
        Some(code) if code.starts_with("vault_") => {
            format!("{code}: The local vault could not complete the opportunity request.")
        }
        _ if error.starts_with("The local engine")
            || error.starts_with("Unable to start the local engine")
            || error.starts_with("The desktop could not send the request") =>
        {
            error
        }
        _ => "opportunity_error: The local engine could not complete the opportunity request."
            .to_string(),
    }
}

fn safe_profile_changes_error(error: String) -> String {
    let code = error.split_once(':').map(|(code, _)| code.trim());
    match code {
        Some("profile_changes_conflict") => concat!(
            "profile_changes_conflict: This profile changed since you began reviewing it. ",
            "Reload the latest profile and review your queued changes."
        )
        .to_string(),
        Some("request_conflict") | Some("profile_changes_request_conflict") => concat!(
            "profile_changes_request_conflict: That apply request was already used for different changes. ",
            "Discard the queued changes and try again."
        )
        .to_string(),
        Some("invalid_params") => {
            "invalid_params: Check the queued profile changes and try again.".to_string()
        }
        Some("vault_busy") => {
            "vault_busy: The local vault is busy; wait a moment and try again.".to_string()
        }
        Some(code) if code.starts_with("vault_") => {
            format!("{code}: The local vault could not complete the profile changes request.")
        }
        _ if error.starts_with("The local engine")
            || error.starts_with("Unable to start the local engine")
            || error.starts_with("The desktop could not send the request") =>
        {
            error
        }
        _ => "profile_changes_error: The local engine could not complete the profile changes request."
            .to_string(),
    }
}

fn safe_profile_review_inbox_error(error: String) -> String {
    let code = error.split_once(':').map(|(code, _)| code.trim());
    match code {
        Some("review_item_not_found") => {
            "review_item_not_found: That review suggestion is no longer available.".to_string()
        }
        Some("request_conflict")
        | Some("review_item_request_conflict")
        | Some("review_item_apply_request_conflict") => concat!(
            "review_item_request_conflict: That review request was already used for a different decision. ",
            "Reload the review inbox and try again."
        )
        .to_string(),
        Some("state_conflict") | Some("review_item_state_conflict") => concat!(
            "review_item_state_conflict: This suggestion changed since you opened it. ",
            "Reload the review inbox before deciding."
        )
        .to_string(),
        Some("invalid_transition") | Some("review_item_invalid_transition") => {
            "review_item_invalid_transition: That decision is not available for this suggestion."
                .to_string()
        }
        Some("review_item_stale_generation") | Some("stale_generation") => concat!(
            "review_item_stale_generation: The imported material was reset after this suggestion was opened. ",
            "Reload the review inbox to see its current history."
        )
        .to_string(),
        Some("profile_conflict") | Some("review_item_profile_conflict") => concat!(
            "review_item_profile_conflict: The profile changed since this suggestion was opened. ",
            "Reload the review inbox before applying it."
        )
        .to_string(),
        Some("review_item_evidence_invalid") | Some("evidence_invalid") => concat!(
            "review_item_evidence_invalid: CVGnome could not verify this suggestion against its retained source. ",
            "The profile was not changed."
        )
        .to_string(),
        Some("review_item_apply_conflict") | Some("candidate_conflict") => concat!(
            "review_item_apply_conflict: The proposed profile material no longer matches this review item. ",
            "Reload the review inbox before applying it."
        )
        .to_string(),
        Some("review_item_apply_noop") => concat!(
            "review_item_apply_noop: That material is already present in the current profile. ",
            "Nothing was changed."
        )
        .to_string(),
        Some("profile_missing") => concat!(
            "profile_missing: There is no current profile to update. ",
            "Reload the workspace before applying this suggestion."
        )
        .to_string(),
        Some("profile_version_limit") => concat!(
            "profile_version_limit: This local workspace has reached its profile version limit. ",
            "The suggestion was not applied."
        )
        .to_string(),
        Some("invalid_params") => {
            "invalid_params: Check the review item fields and try again.".to_string()
        }
        Some("vault_busy") => {
            "vault_busy: The local vault is busy; wait a moment and try again.".to_string()
        }
        Some(code) if code.starts_with("vault_") => {
            format!("{code}: The local vault could not complete the review request.")
        }
        _ if error.starts_with("The local engine")
            || error.starts_with("Unable to start the local engine")
            || error.starts_with("The desktop could not send the request") =>
        {
            error
        }
        _ => "review_inbox_error: The local engine could not complete the review request."
            .to_string(),
    }
}

fn safe_manual_profile_start_error(error: String) -> String {
    let code = error.split_once(':').map(|(code, _)| code.trim());
    match code {
        Some("profile_start_conflict") => concat!(
            "profile_start_conflict: A profile already exists in this local workspace. ",
            "Open the existing profile instead."
        )
        .to_string(),
        Some("profile_start_request_conflict") | Some("request_conflict") => concat!(
            "profile_start_request_conflict: That create request was already used for different profile details. ",
            "Cancel this draft and start again."
        )
        .to_string(),
        Some("profile_start_preview_conflict") => concat!(
            "profile_start_preview_conflict: An unfinished document import is still open. ",
            "Finish or discard that review before starting manually."
        )
        .to_string(),
        Some("invalid_params") => {
            "invalid_params: Check the profile details and try again.".to_string()
        }
        Some("vault_busy") => {
            "vault_busy: The local vault is busy; wait a moment and try again.".to_string()
        }
        Some(code) if code.starts_with("vault_") => {
            format!("{code}: The local vault could not create the profile.")
        }
        _ if error.starts_with("The local engine")
            || error.starts_with("Unable to start the local engine")
            || error.starts_with("The desktop could not send the request") =>
        {
            error
        }
        _ => "profile_start_error: The local engine could not create the profile.".to_string(),
    }
}

fn manual_profile_start_invalid_params(error: String) -> String {
    format!("invalid_params: {error}")
}

impl SaveOpportunityInput {
    fn into_engine_params(self) -> Result<Value, String> {
        let title = normalized_required_opportunity_text(
            self.title,
            "Role title",
            OPPORTUNITY_TITLE_LIMIT_CHARS,
            false,
        )?;
        let description = normalized_required_opportunity_text(
            self.description,
            "Job description",
            OPPORTUNITY_DESCRIPTION_LIMIT_CHARS,
            true,
        )?;
        let encoded_description = serde_json::to_vec(&description)
            .map_err(|_| "Job description could not be encoded safely".to_string())?;
        if encoded_description.len().saturating_sub(2) > OPPORTUNITY_DESCRIPTION_LIMIT_CHARS {
            return Err("Job description exceeds the local JSON limit".to_string());
        }
        let company = normalized_optional_opportunity_text(
            self.company,
            "Company",
            OPPORTUNITY_SHORT_FIELD_LIMIT_CHARS,
        )?;
        let location = normalized_optional_opportunity_text(
            self.location,
            "Location",
            OPPORTUNITY_SHORT_FIELD_LIMIT_CHARS,
        )?;
        let source_url = normalized_optional_https_url(self.source_url, "Source URL")?;
        let apply_url = normalized_optional_https_url(self.apply_url, "Apply URL")?;
        Ok(json!({
            "title": title,
            "description": description,
            "company": company,
            "location": location,
            "source_url": source_url,
            "apply_url": apply_url,
        }))
    }
}

impl OpportunitySnapshotSummary {
    fn validate(&self) -> Result<(), String> {
        validate_uuid_receipt(&self.id)?;
        validate_sha256(&self.checksum_sha256)?;
        if self.captured_at_ms == 0 {
            return Err("The local engine returned an invalid opportunity timestamp".to_string());
        }
        validate_javascript_integer(self.captured_at_ms, "opportunity timestamp")
    }
}

impl OpportunityMatchSummary {
    fn validate(&self) -> Result<(), String> {
        validate_uuid_receipt(&self.id)?;
        if self.score > 100 {
            return Err("The local engine returned an invalid opportunity score".to_string());
        }
        if self.created_at_ms == 0 {
            return Err("The local engine returned an invalid opportunity timestamp".to_string());
        }
        validate_javascript_integer(self.created_at_ms, "opportunity timestamp")
    }
}

impl OpportunityProfileVersion {
    fn validate(&self) -> Result<(), String> {
        validate_uuid_receipt(&self.id)?;
        validate_sha256(&self.checksum_sha256)?;
        if self.version_number == 0 {
            return Err("The local engine returned an invalid profile version".to_string());
        }
        validate_javascript_integer(self.version_number, "profile version")
    }
}

fn validate_engine_opportunity_notes(value: &str) -> Result<(), String> {
    validate_engine_text(
        value,
        "opportunity notes",
        OPPORTUNITY_NOTES_LIMIT_CHARS,
        true,
        true,
    )
}

fn validate_opportunity_timestamps(
    created_at_ms: u64,
    updated_at_ms: u64,
    tracker_updated_at_ms: u64,
) -> Result<(), String> {
    if created_at_ms == 0
        || updated_at_ms == 0
        || tracker_updated_at_ms == 0
        || created_at_ms > updated_at_ms
        || created_at_ms > tracker_updated_at_ms
    {
        return Err("The local engine returned invalid opportunity timestamps".to_string());
    }
    for value in [created_at_ms, updated_at_ms, tracker_updated_at_ms] {
        validate_javascript_integer(value, "opportunity timestamp")?;
    }
    Ok(())
}

impl OpportunitySummary {
    fn validate(&self) -> Result<(), String> {
        validate_uuid_receipt(&self.id)?;
        validate_engine_text(
            &self.title,
            "opportunity title",
            OPPORTUNITY_TITLE_LIMIT_CHARS,
            false,
            false,
        )?;
        validate_engine_text(
            &self.company,
            "opportunity company",
            OPPORTUNITY_SHORT_FIELD_LIMIT_CHARS,
            true,
            false,
        )?;
        validate_engine_text(
            &self.location,
            "opportunity location",
            OPPORTUNITY_SHORT_FIELD_LIMIT_CHARS,
            true,
            false,
        )?;
        validate_opportunity_timestamps(
            self.created_at_ms,
            self.updated_at_ms,
            self.tracker_updated_at_ms,
        )?;
        self.snapshot.validate()?;
        self.match_result.validate()?;
        self.profile_version.validate()
    }
}

impl OpportunityListResult {
    fn validated(self, offset: usize) -> Result<Self, String> {
        validate_javascript_integer(self.total, "opportunity count")?;
        let item_count = self.items.len() as u64;
        let page_end = (offset as u64)
            .checked_add(item_count)
            .ok_or_else(|| "The local engine returned an invalid opportunity list".to_string())?;
        if self.items.len() > OPPORTUNITY_LIST_LIMIT
            || (!self.items.is_empty() && (offset as u64 >= self.total || page_end > self.total))
            || (self.items.is_empty() && (offset as u64) < self.total)
            || (item_count < OPPORTUNITY_LIST_LIMIT as u64 && page_end < self.total)
        {
            return Err("The local engine returned an invalid opportunity list".to_string());
        }
        for item in &self.items {
            item.validate()?;
        }
        Ok(self)
    }
}

impl OpportunityDetail {
    fn validated(self) -> Result<Self, String> {
        validate_uuid_receipt(&self.opportunity.id)?;
        validate_engine_text(
            &self.opportunity.title,
            "opportunity title",
            OPPORTUNITY_TITLE_LIMIT_CHARS,
            false,
            false,
        )?;
        validate_engine_text(
            &self.opportunity.company,
            "opportunity company",
            OPPORTUNITY_SHORT_FIELD_LIMIT_CHARS,
            true,
            false,
        )?;
        validate_engine_text(
            &self.opportunity.location,
            "opportunity location",
            OPPORTUNITY_SHORT_FIELD_LIMIT_CHARS,
            true,
            false,
        )?;
        validate_engine_opportunity_notes(&self.opportunity.notes)?;
        validate_opportunity_timestamps(
            self.opportunity.created_at_ms,
            self.opportunity.updated_at_ms,
            self.opportunity.tracker_updated_at_ms,
        )?;
        if let Some(source_url) = self.opportunity.source_url.as_deref() {
            validate_https_url(source_url, "engine source URL")?;
        }
        if let Some(apply_url) = self.opportunity.apply_url.as_deref() {
            validate_https_url(apply_url, "engine apply URL")?;
        }
        validate_uuid_receipt(&self.snapshot.id)?;
        validate_sha256(&self.snapshot.checksum_sha256)?;
        if self.snapshot.captured_at_ms == 0 {
            return Err("The local engine returned an invalid opportunity timestamp".to_string());
        }
        validate_javascript_integer(self.snapshot.captured_at_ms, "opportunity timestamp")?;
        validate_engine_text(
            &self.snapshot.description,
            "opportunity description",
            OPPORTUNITY_DESCRIPTION_LIMIT_CHARS,
            false,
            true,
        )?;
        validate_uuid_receipt(&self.match_result.id)?;
        if self.match_result.score > 100
            || self.match_result.matched_terms.len() > OPPORTUNITY_MATCHED_TERMS_LIMIT
            || self.match_result.terms_to_review.len() > OPPORTUNITY_REVIEW_TERMS_LIMIT
            || self.match_result.evidence.len() > OPPORTUNITY_EVIDENCE_LIMIT
        {
            return Err("The local engine returned an invalid opportunity match".to_string());
        }
        if self.match_result.created_at_ms == 0 {
            return Err("The local engine returned an invalid opportunity timestamp".to_string());
        }
        validate_javascript_integer(self.match_result.created_at_ms, "opportunity timestamp")?;
        for term in self
            .match_result
            .matched_terms
            .iter()
            .chain(&self.match_result.terms_to_review)
        {
            validate_engine_text(
                term,
                "opportunity match term",
                OPPORTUNITY_TERM_LIMIT_CHARS,
                false,
                false,
            )?;
        }
        for evidence in &self.match_result.evidence {
            validate_engine_text(
                &evidence.term,
                "opportunity evidence term",
                OPPORTUNITY_TERM_LIMIT_CHARS,
                false,
                false,
            )?;
            validate_engine_text(
                &evidence.profile_path,
                "opportunity evidence path",
                OPPORTUNITY_PROFILE_PATH_LIMIT_CHARS,
                false,
                false,
            )?;
            validate_engine_text(
                &evidence.snippet,
                "opportunity evidence snippet",
                OPPORTUNITY_EVIDENCE_SNIPPET_LIMIT_CHARS,
                false,
                false,
            )?;
        }
        self.profile_version.validate()?;
        Ok(self)
    }
}

pub(crate) async fn run_engine_request<T: DeserializeOwned>(
    app: &tauri::AppHandle,
    method: &str,
    params: Value,
    timeout: Duration,
) -> Result<T, String> {
    let data_dir = app
        .path()
        .app_data_dir()
        .map_err(|_| "Unable to resolve the local data directory".to_string())?;
    prepare_data_dir(&data_dir)?;
    let data_dir_argument = data_dir
        .to_str()
        .ok_or_else(|| "The local data directory is not valid Unicode".to_string())?;
    let request_id = Uuid::new_v4().to_string();

    let mut request = serde_json::to_vec(&json!({
        "protocol_version": ENGINE_PROTOCOL_VERSION,
        "id": request_id,
        "method": method,
        "params": params,
    }))
    .map_err(|_| "The desktop could not encode the local engine request".to_string())?;
    request.push(b'\n');
    if request.len() > ENGINE_REQUEST_LIMIT_BYTES {
        return Err("The local engine request exceeds the 9 MiB limit".to_string());
    }

    let (mut events, mut child) = app
        .shell()
        .sidecar("cvgnome-engine")
        .map_err(|_| "Unable to resolve the local engine".to_string())?
        .args(["request", "--data-dir", data_dir_argument])
        .set_raw_out(true)
        .spawn()
        .map_err(|_| "Unable to start the local engine".to_string())?;
    if child.write(&request).is_err() {
        let _ = child.kill();
        return Err("The desktop could not send the request to the local engine".to_string());
    }

    let collected = tokio::time::timeout(timeout, async move {
        let mut exit_code = None;
        let mut stdout = Vec::new();
        let mut stderr = Vec::new();
        let mut total_length = 0;
        while let Some(event) = events.recv().await {
            match event {
                CommandEvent::Stdout(chunk) => {
                    append_bounded(&mut stdout, &chunk, &mut total_length)?
                }
                CommandEvent::Stderr(chunk) => {
                    append_bounded(&mut stderr, &chunk, &mut total_length)?
                }
                CommandEvent::Terminated(payload) => exit_code = payload.code,
                CommandEvent::Error(_) => {
                    return Err("The local engine process could not be monitored".to_string());
                }
                _ => {}
            }
        }
        Ok::<_, String>((exit_code, stdout))
    })
    .await;

    let (exit_code, stdout) = match collected {
        Ok(Ok(output)) => output,
        Ok(Err(error)) => {
            let _ = child.kill();
            return Err(error);
        }
        Err(_) => {
            let _ = child.kill();
            return Err("The local engine timed out and was stopped".to_string());
        }
    };

    let envelope: EngineEnvelope<T> = serde_json::from_slice(&stdout).map_err(|_| {
        if exit_code == Some(0) {
            "The local engine returned an invalid response".to_string()
        } else {
            "The local engine exited unexpectedly".to_string()
        }
    })?;
    if envelope.protocol_version != ENGINE_PROTOCOL_VERSION {
        return Err(format!(
            "The local engine protocol ({}) is incompatible with this app ({ENGINE_PROTOCOL_VERSION})",
            envelope.protocol_version
        ));
    }
    if envelope.id.as_deref() != Some(request_id.as_str()) {
        return Err("The local engine returned a mismatched response".to_string());
    }
    if (envelope.ok && envelope.error.is_some()) || (!envelope.ok && envelope.result.is_some()) {
        return Err("The local engine returned an inconsistent response".to_string());
    }
    if !envelope.ok {
        return Err(safe_engine_error(envelope.error.unwrap_or(EngineError {
            code: "engine_error".to_string(),
            message: "The local engine did not provide an error message".to_string(),
        })));
    }
    if exit_code != Some(0) {
        return Err("The local engine exited unexpectedly".to_string());
    }
    envelope
        .result
        .ok_or_else(|| "The local engine returned no result payload".to_string())
}

async fn invalidate_status(runtime: &EngineRuntime) {
    *runtime.cached_status.lock().await = None;
}

async fn finalize_profile_review_apply_attempt(
    runtime: &EngineRuntime,
    result: Result<ProfileReviewInboxApplyReceipt, String>,
    request: &ValidatedProfileReviewInboxApplyRequest,
) -> Result<ProfileReviewInboxApplyReceipt, String> {
    // Commit happens before the response crosses the process boundary. Clear
    // cached status for engine errors, transport failures, malformed receipts,
    // and successful receipts alike.
    invalidate_status(runtime).await;
    result?.validated(
        &request.review_item_id,
        request.expected_state_revision,
        &request.expected_parent_profile_version_id,
        &request.request_id,
        &request.candidate,
        request.expected_operation_kind,
    )
}

#[tauri::command]
async fn engine_status(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
) -> Result<EngineStatus, String> {
    {
        let cached_status = runtime.cached_status.lock().await;
        if let Some(cached) = cached_status.as_ref()
            && cached.checked_at.elapsed() <= ENGINE_STATUS_CACHE_TTL
        {
            return cached.result.clone();
        }
    }
    let _operation_guard = runtime.operation_gate.lock().await;
    {
        let cached_status = runtime.cached_status.lock().await;
        if let Some(cached) = cached_status.as_ref()
            && cached.checked_at.elapsed() <= ENGINE_STATUS_CACHE_TTL
        {
            return cached.result.clone();
        }
    }
    let result = run_engine_request::<EngineStatusWire>(
        &app,
        "system.status",
        json!({}),
        ENGINE_STATUS_TIMEOUT,
    )
    .await
    .and_then(EngineStatusWire::validated);
    let mut cached_status = runtime.cached_status.lock().await;
    *cached_status = Some(CachedEngineStatus {
        checked_at: Instant::now(),
        result: result.clone(),
    });
    result
}

#[tauri::command(rename_all = "camelCase")]
async fn save_and_match_opportunity(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    input: SaveOpportunityInput,
) -> Result<OpportunityDetail, String> {
    let params = input.into_engine_params()?;
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<OpportunityDetail>(
            &app,
            "opportunities.save_match",
            params,
            ENGINE_OPPORTUNITY_TIMEOUT,
        )
        .await
        .map_err(safe_opportunity_error)?
    };
    invalidate_status(&runtime).await;
    result.validated()
}

#[tauri::command(rename_all = "camelCase")]
async fn list_opportunities(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    query: Option<String>,
    offset: usize,
    stage: String,
    sort: String,
) -> Result<OpportunityListResult, String> {
    let params = opportunity_list_params(query, offset, &stage, &sort)?;
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<OpportunityListResult>(
            &app,
            "opportunities.list",
            params,
            ENGINE_OPPORTUNITY_TIMEOUT,
        )
        .await
        .map_err(safe_opportunity_error)?
    };
    result.validated(offset)
}

#[tauri::command(rename_all = "camelCase")]
async fn get_opportunity(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    opportunity_id: String,
) -> Result<OpportunityDetail, String> {
    let opportunity_id = validated_opportunity_id(&opportunity_id)?;
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<OpportunityDetail>(
            &app,
            "opportunities.get",
            json!({"opportunity_id": opportunity_id.clone()}),
            ENGINE_OPPORTUNITY_TIMEOUT,
        )
        .await
        .map_err(safe_opportunity_error)?
    }
    .validated()?;
    if result.opportunity.id != opportunity_id {
        return Err("The local engine returned a mismatched opportunity".to_string());
    }
    Ok(result)
}

#[tauri::command(rename_all = "camelCase")]
async fn rematch_opportunity(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    opportunity_id: String,
) -> Result<OpportunityDetail, String> {
    let opportunity_id = validated_opportunity_id(&opportunity_id)?;
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<OpportunityDetail>(
            &app,
            "opportunities.rematch",
            json!({"opportunity_id": opportunity_id.clone()}),
            ENGINE_OPPORTUNITY_TIMEOUT,
        )
        .await
        .map_err(safe_opportunity_error)?
    };
    invalidate_status(&runtime).await;
    let result = result.validated()?;
    if result.opportunity.id != opportunity_id {
        return Err("The local engine returned a mismatched opportunity".to_string());
    }
    Ok(result)
}

#[tauri::command(rename_all = "camelCase")]
async fn update_opportunity_tracker(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    opportunity_id: String,
    expected_tracker_updated_at_ms: u64,
    tracker_stage: Option<String>,
    notes: Option<String>,
) -> Result<OpportunityDetail, String> {
    let params = opportunity_tracker_update_params(
        &opportunity_id,
        expected_tracker_updated_at_ms,
        tracker_stage,
        notes,
    )?;
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<OpportunityDetail>(
            &app,
            "opportunities.update",
            params,
            ENGINE_OPPORTUNITY_TIMEOUT,
        )
        .await
        .map_err(safe_opportunity_error)?
    }
    .validated()?;
    if result.opportunity.id != opportunity_id {
        return Err("The local engine returned a mismatched opportunity".to_string());
    }
    Ok(result)
}

#[tauri::command(rename_all = "camelCase")]
async fn list_profile_versions(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    offset: usize,
) -> Result<ProfileVersionList, String> {
    let offset = validated_profile_version_offset(offset)?;
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<ProfileVersionList>(
            &app,
            "profile.versions.list",
            profile_version_list_params(offset),
            ENGINE_PROFILE_REVIEW_TIMEOUT,
        )
        .await?
    };
    result.validated(offset)
}

#[tauri::command(rename_all = "camelCase")]
async fn get_profile_review_version(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    profile_version_id: String,
) -> Result<ProfileReviewSummary, String> {
    let profile_version_id = validated_profile_review_version_id(&profile_version_id)?;
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<ProfileReviewSummary>(
            &app,
            "profile.review.version",
            profile_version_identity_params(&profile_version_id),
            ENGINE_PROFILE_REVIEW_TIMEOUT,
        )
        .await?
    }
    .validated()?;
    if result.profile_version_id != profile_version_id {
        return Err("The local engine returned a mismatched profile review".to_string());
    }
    Ok(result)
}

#[tauri::command(rename_all = "camelCase")]
async fn get_profile_basics(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    profile_version_id: String,
) -> Result<ProfileBasics, String> {
    let profile_version_id = validated_profile_review_version_id(&profile_version_id)?;
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<ProfileBasics>(
            &app,
            "profile.basics.get",
            profile_version_identity_params(&profile_version_id),
            ENGINE_PROFILE_REVIEW_TIMEOUT,
        )
        .await?
    };
    result.validated(&profile_version_id)
}

#[tauri::command(rename_all = "camelCase")]
async fn diff_profile_versions(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    from_profile_version_id: String,
    to_profile_version_id: String,
) -> Result<ProfileVersionDiff, String> {
    let from_profile_version_id = validated_profile_review_version_id(&from_profile_version_id)?;
    let to_profile_version_id = validated_profile_review_version_id(&to_profile_version_id)?;
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<ProfileVersionDiff>(
            &app,
            "profile.versions.diff",
            profile_version_diff_params(&from_profile_version_id, &to_profile_version_id),
            ENGINE_PROFILE_REVIEW_TIMEOUT,
        )
        .await?
    };
    result.validated(&from_profile_version_id, &to_profile_version_id)
}

#[tauri::command(rename_all = "camelCase")]
async fn restore_profile_version(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    source_profile_version_id: String,
    expected_parent_profile_version_id: String,
    request_id: String,
) -> Result<ProfileRestoreReceipt, String> {
    let (source_profile_version_id, expected_parent_profile_version_id) =
        validated_profile_restore_ids(
            &source_profile_version_id,
            &expected_parent_profile_version_id,
        )?;
    let request_id = validated_profile_restore_request_id(&request_id)?;
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<ProfileRestoreReceipt>(
            &app,
            "profile.versions.restore",
            profile_restore_params(
                &source_profile_version_id,
                &expected_parent_profile_version_id,
                &request_id,
            ),
            ENGINE_PROFILE_EDIT_TIMEOUT,
        )
        .await
    };
    invalidate_status(&runtime).await;
    result?.validated(
        &source_profile_version_id,
        &expected_parent_profile_version_id,
        &request_id,
    )
}

#[tauri::command(rename_all = "camelCase")]
async fn start_manual_profile(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    expected_parent_profile_version_id: Value,
    request_id: String,
    patch: ProfileBasicsPatch,
) -> Result<ManualProfileStartReceipt, String> {
    validated_manual_profile_expected_parent(&expected_parent_profile_version_id)
        .map_err(manual_profile_start_invalid_params)?;
    let request_id = validated_manual_profile_request_id(&request_id)
        .map_err(manual_profile_start_invalid_params)?;
    let patch = patch
        .validated_for_manual_start()
        .map_err(manual_profile_start_invalid_params)?;
    let params = manual_profile_start_params(&request_id, &patch)
        .map_err(manual_profile_start_invalid_params)?;
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<ManualProfileStartReceipt>(
            &app,
            "profile.start.manual",
            params,
            ENGINE_PROFILE_EDIT_TIMEOUT,
        )
        .await
        .map_err(safe_manual_profile_start_error)
    };
    // The engine may commit before a timeout or malformed response reaches the
    // desktop. Always clear a pre-create status so an exact retry sees reality.
    invalidate_status(&runtime).await;
    result?.validated(&request_id, &patch)
}

#[tauri::command(rename_all = "camelCase")]
async fn update_profile_basics(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    expected_parent_profile_version_id: String,
    request_id: String,
    patch: ProfileBasicsPatch,
) -> Result<ProfileBasicsUpdateReceipt, String> {
    let expected_parent_profile_version_id =
        validated_profile_review_version_id(&expected_parent_profile_version_id)?;
    let request_id = Uuid::parse_str(&request_id)
        .ok()
        .filter(|parsed| parsed.to_string() == request_id)
        .map(|parsed| parsed.to_string())
        .ok_or_else(|| "The profile update request ID is invalid".to_string())?;
    let patch = patch.validated()?;
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<ProfileBasicsUpdateReceipt>(
            &app,
            "profile.basics.update",
            profile_basics_update_params(&expected_parent_profile_version_id, &request_id, &patch),
            ENGINE_PROFILE_EDIT_TIMEOUT,
        )
        .await
    };
    invalidate_status(&runtime).await;
    result?.validated(&expected_parent_profile_version_id, &request_id, &patch)
}

#[tauri::command(rename_all = "camelCase")]
async fn list_profile_work_history(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    profile_version_id: String,
    offset: usize,
) -> Result<ProfileWorkPage, String> {
    let profile_version_id = validated_profile_review_version_id(&profile_version_id)?;
    let offset = validated_profile_work_offset(offset)?;
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<ProfileWorkPage>(
            &app,
            "profile.work.list",
            profile_work_list_params(&profile_version_id, offset),
            ENGINE_PROFILE_REVIEW_TIMEOUT,
        )
        .await?
    };
    result.validated(&profile_version_id, offset)
}

#[tauri::command(rename_all = "camelCase")]
async fn update_profile_work_history(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    expected_parent_profile_version_id: String,
    request_id: String,
    operation: ProfileWorkOperation,
) -> Result<ProfileWorkUpdateReceipt, String> {
    let expected_parent_profile_version_id =
        validated_profile_review_version_id(&expected_parent_profile_version_id)?;
    let request_id = Uuid::parse_str(&request_id)
        .ok()
        .filter(|parsed| parsed.to_string() == request_id)
        .map(|parsed| parsed.to_string())
        .ok_or_else(|| "The Work History update request ID is invalid".to_string())?;
    let operation = operation.validated()?;
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<ProfileWorkUpdateReceipt>(
            &app,
            "profile.work.update",
            profile_work_update_params(
                &expected_parent_profile_version_id,
                &request_id,
                &operation,
            ),
            ENGINE_PROFILE_EDIT_TIMEOUT,
        )
        .await
    };
    invalidate_status(&runtime).await;
    result?.validated(&expected_parent_profile_version_id, &request_id, &operation)
}

#[tauri::command(rename_all = "camelCase")]
async fn list_profile_projects(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    profile_version_id: String,
    offset: usize,
) -> Result<ProfileProjectsPage, String> {
    let profile_version_id = validated_profile_review_version_id(&profile_version_id)?;
    let offset = validated_profile_section_offset(offset, "Projects")?;
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<ProfileProjectsPage>(
            &app,
            "profile.projects.list",
            profile_section_list_params(&profile_version_id, offset),
            ENGINE_PROFILE_REVIEW_TIMEOUT,
        )
        .await?
    };
    result.validated(&profile_version_id, offset)
}

#[tauri::command(rename_all = "camelCase")]
async fn update_profile_projects(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    expected_parent_profile_version_id: String,
    request_id: String,
    operation: ProfileProjectsOperation,
) -> Result<ProfileSectionUpdateReceipt, String> {
    let expected_parent_profile_version_id =
        validated_profile_review_version_id(&expected_parent_profile_version_id)?;
    let request_id = validated_profile_section_request_id(&request_id, "Projects")?;
    let operation = operation.validated()?;
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<ProfileSectionUpdateReceipt>(
            &app,
            "profile.projects.update",
            profile_section_update_params(
                &expected_parent_profile_version_id,
                &request_id,
                &operation,
            ),
            ENGINE_PROFILE_EDIT_TIMEOUT,
        )
        .await
    };
    invalidate_status(&runtime).await;
    result?.validated(
        ProfileEditableSection::Projects,
        &expected_parent_profile_version_id,
        &request_id,
        operation.kind(),
        operation.target_index(),
        &operation.present_fields(),
    )
}

#[tauri::command(rename_all = "camelCase")]
async fn list_profile_education(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    profile_version_id: String,
    offset: usize,
) -> Result<ProfileEducationPage, String> {
    let profile_version_id = validated_profile_review_version_id(&profile_version_id)?;
    let offset = validated_profile_section_offset(offset, "Education")?;
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<ProfileEducationPage>(
            &app,
            "profile.education.list",
            profile_section_list_params(&profile_version_id, offset),
            ENGINE_PROFILE_REVIEW_TIMEOUT,
        )
        .await?
    };
    result.validated(&profile_version_id, offset)
}

#[tauri::command(rename_all = "camelCase")]
async fn update_profile_education(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    expected_parent_profile_version_id: String,
    request_id: String,
    operation: ProfileEducationOperation,
) -> Result<ProfileSectionUpdateReceipt, String> {
    let expected_parent_profile_version_id =
        validated_profile_review_version_id(&expected_parent_profile_version_id)?;
    let request_id = validated_profile_section_request_id(&request_id, "Education")?;
    let operation = operation.validated()?;
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<ProfileSectionUpdateReceipt>(
            &app,
            "profile.education.update",
            profile_section_update_params(
                &expected_parent_profile_version_id,
                &request_id,
                &operation,
            ),
            ENGINE_PROFILE_EDIT_TIMEOUT,
        )
        .await
    };
    invalidate_status(&runtime).await;
    result?.validated(
        ProfileEditableSection::Education,
        &expected_parent_profile_version_id,
        &request_id,
        operation.kind(),
        operation.target_index(),
        &operation.present_fields(),
    )
}

#[tauri::command(rename_all = "camelCase")]
async fn list_profile_skills(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    profile_version_id: String,
    offset: usize,
) -> Result<ProfileSkillsPage, String> {
    let profile_version_id = validated_profile_review_version_id(&profile_version_id)?;
    let offset = validated_profile_section_offset(offset, "Skills")?;
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<ProfileSkillsPage>(
            &app,
            "profile.skills.list",
            profile_section_list_params(&profile_version_id, offset),
            ENGINE_PROFILE_REVIEW_TIMEOUT,
        )
        .await?
    };
    result.validated(&profile_version_id, offset)
}

#[tauri::command(rename_all = "camelCase")]
async fn update_profile_skills(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    expected_parent_profile_version_id: String,
    request_id: String,
    operation: ProfileSkillsOperation,
) -> Result<ProfileSectionUpdateReceipt, String> {
    let expected_parent_profile_version_id =
        validated_profile_review_version_id(&expected_parent_profile_version_id)?;
    let request_id = validated_profile_section_request_id(&request_id, "Skills")?;
    let operation = operation.validated()?;
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<ProfileSectionUpdateReceipt>(
            &app,
            "profile.skills.update",
            profile_section_update_params(
                &expected_parent_profile_version_id,
                &request_id,
                &operation,
            ),
            ENGINE_PROFILE_EDIT_TIMEOUT,
        )
        .await
    };
    invalidate_status(&runtime).await;
    result?.validated(
        ProfileEditableSection::Skills,
        &expected_parent_profile_version_id,
        &request_id,
        operation.kind(),
        operation.target_index(),
        &operation.present_fields(),
    )
}

#[tauri::command(rename_all = "camelCase")]
async fn preview_profile_changes(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    profile_version_id: String,
) -> Result<ProfileChangesPreview, String> {
    let profile_version_id = validated_profile_review_version_id(&profile_version_id)?;
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<ProfileChangesPreview>(
            &app,
            "profile.changes.preview",
            profile_changes_preview_params(&profile_version_id),
            ENGINE_PROFILE_REVIEW_TIMEOUT,
        )
        .await
        .map_err(safe_profile_changes_error)?
    };
    result.validated(&profile_version_id)
}

#[tauri::command(rename_all = "camelCase")]
async fn apply_profile_changes(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    expected_parent_profile_version_id: String,
    request_id: String,
    operations: Vec<ProfileChangeOperation>,
) -> Result<ProfileChangesApplyReceipt, String> {
    let expected_parent_profile_version_id =
        validated_profile_review_version_id(&expected_parent_profile_version_id)?;
    let request_id = validated_profile_changes_request_id(&request_id)?;
    let operations = validated_profile_change_operations(operations)?;
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<ProfileChangesApplyReceipt>(
            &app,
            "profile.changes.apply",
            profile_changes_apply_params(
                &expected_parent_profile_version_id,
                &request_id,
                &operations,
            ),
            ENGINE_PROFILE_EDIT_TIMEOUT,
        )
        .await
        .map_err(safe_profile_changes_error)
    };
    // A timeout or malformed response can happen after the engine commits, and
    // an exact idempotent retry returns `created: false`. Clear status after
    // every apply attempt so a pre-apply cache entry is never retained.
    invalidate_status(&runtime).await;
    result?.validated(
        &expected_parent_profile_version_id,
        &request_id,
        &operations,
    )
}

#[tauri::command(rename_all = "camelCase")]
async fn list_profile_review_items(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    scope: String,
    offset: usize,
) -> Result<ProfileReviewInboxPage, String> {
    let (expected_scope, params) = profile_review_inbox_list_params(&scope, offset)?;
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<ProfileReviewInboxPage>(
            &app,
            "review.inbox.list",
            params,
            ENGINE_PROFILE_REVIEW_TIMEOUT,
        )
        .await
        .map_err(safe_profile_review_inbox_error)?
    };
    result.validated(expected_scope, offset)
}

#[tauri::command(rename_all = "camelCase")]
async fn transition_profile_review_item(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    review_item_id: String,
    expected_state: String,
    expected_revision: u64,
    request_id: String,
    action: String,
) -> Result<ProfileReviewInboxTransitionReceipt, String> {
    let (review_item_id, expected_state, expected_state_revision, request_id, action, params) =
        profile_review_inbox_transition_params(
            &review_item_id,
            &expected_state,
            expected_revision,
            &request_id,
            &action,
        )?;
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<ProfileReviewInboxTransitionReceipt>(
            &app,
            "review.inbox.transition",
            params,
            ENGINE_PROFILE_REVIEW_TIMEOUT,
        )
        .await
        .map_err(safe_profile_review_inbox_error)
    };
    // A timeout or malformed response can follow a committed state transition,
    // so a pre-transition status snapshot must never survive any engine attempt.
    invalidate_status(&runtime).await;
    result?.validated(
        &review_item_id,
        expected_state,
        expected_state_revision,
        &request_id,
        action,
    )
}

#[tauri::command(rename_all = "camelCase")]
#[allow(clippy::too_many_arguments)]
async fn apply_profile_review_item(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    review_item_id: String,
    expected_state: String,
    expected_revision: u64,
    expected_parent_profile_version_id: String,
    request_id: String,
    expected_operation_kind: String,
    candidate: ProfileReviewInboxCandidate,
) -> Result<ProfileReviewInboxApplyReceipt, String> {
    let request = profile_review_inbox_apply_params(
        &review_item_id,
        &expected_state,
        expected_revision,
        &expected_parent_profile_version_id,
        &request_id,
        &expected_operation_kind,
        candidate,
    )?;
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<ProfileReviewInboxApplyReceipt>(
            &app,
            "review.inbox.apply",
            request.params.clone(),
            ENGINE_PROFILE_EDIT_TIMEOUT,
        )
        .await
        .map_err(safe_profile_review_inbox_error)
    };
    finalize_profile_review_apply_attempt(&runtime, result, &request).await
}

#[tauri::command]
async fn get_profile_review(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
) -> Result<ProfileReviewSummary, String> {
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<ProfileReviewSummary>(
            &app,
            "profile.review",
            json!({}),
            ENGINE_PROFILE_REVIEW_TIMEOUT,
        )
        .await?
    };
    result.validated()
}

#[tauri::command(rename_all = "camelCase")]
async fn get_profile_review_section(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    profile_version_id: String,
    section: String,
    offset: usize,
) -> Result<ProfileReviewSectionPage, String> {
    let profile_version_id = validated_profile_review_version_id(&profile_version_id)?;
    let section = ProfileReviewSectionKey::parse(&section)?;
    let offset = validated_profile_review_offset(offset)?;
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<ProfileReviewSectionPage>(
            &app,
            "profile.review.section",
            profile_review_section_params(&profile_version_id, section, offset),
            ENGINE_PROFILE_REVIEW_TIMEOUT,
        )
        .await?
    };
    result.validated(&profile_version_id, section, offset)
}

#[tauri::command]
async fn import_canonical_profile(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
) -> Result<Option<ProfileImportResult>, String> {
    let mut dialog = app
        .dialog()
        .file()
        .set_title("Choose one canonical profile JSON file")
        .set_can_create_directories(false);
    if let Some(window) = app.get_webview_window("main") {
        dialog = dialog.set_parent(&window);
    }
    let Some(selected) = dialog.blocking_pick_file() else {
        return Ok(None);
    };
    let selected = selected
        .into_path()
        .map_err(|_| "The selected profile is not a local file".to_string())?;
    let data_dir = app
        .path()
        .app_data_dir()
        .map_err(|_| "Unable to resolve the local data directory".to_string())?;
    prepare_data_dir(&data_dir)?;
    let scan_id = Uuid::new_v4().to_string();

    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        let source = stage_canonical_profile(&selected, &data_dir, &scan_id)?;
        run_engine_request::<ProfileImportResultWire>(
            &app,
            "profile.import",
            canonical_import_params(source),
            ENGINE_IMPORT_TIMEOUT,
        )
        .await
    };
    // The child process has exited or has been stopped before an accepted
    // receipt. Remove only this opaque request's staging directory. An Ok
    // receipt is left to the engine's durable-commit cleanup lifecycle.
    let result = cleanup_failed_canonical_import(&data_dir, &scan_id, result);
    if result.is_ok() {
        invalidate_status(&runtime).await;
    }
    result
        .and_then(ProfileImportResultWire::validated)
        .map(Some)
}

fn validated_source_scan_id(value: &str) -> Result<String, String> {
    let parsed = Uuid::parse_str(value).map_err(|_| "The source scan ID is invalid".to_string())?;
    let normalized = parsed.to_string();
    if normalized != value {
        return Err("The source scan ID is invalid".to_string());
    }
    Ok(normalized)
}

async fn preview_staged_profile_sources(
    app: &tauri::AppHandle,
    data_dir: &Path,
    scan_id: &str,
    staged: StagedSourceFolder,
) -> Result<ProfileSourceScanResult, String> {
    let payload = match serde_json::to_value(&staged) {
        Ok(payload) => payload,
        Err(_) => {
            let _ = cleanup_staging(data_dir, scan_id);
            return Err("The desktop could not encode the source manifest".to_string());
        }
    };
    let request_result = run_engine_request::<ProfileSourceScanResult>(
        app,
        "profile.sources.preview",
        payload,
        ENGINE_SOURCE_SCAN_TIMEOUT,
    )
    .await;
    match request_result {
        Ok(preview) if preview.scan_id == scan_id => preview.validated_review_candidate_count(),
        Ok(_) => {
            let _ = cleanup_staging(data_dir, scan_id);
            Err("The local engine returned a mismatched source scan".to_string())
        }
        Err(error) => {
            let _ = cleanup_staging(data_dir, scan_id);
            Err(error)
        }
    }
}

#[tauri::command]
async fn scan_profile_source_files(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
) -> Result<Option<ProfileSourceScanResult>, String> {
    let mut dialog = app
        .dialog()
        .file()
        .set_title("Choose profile source documents")
        .set_can_create_directories(false)
        .add_filter(
            "Profile source documents",
            &["pdf", "docx", "json", "md", "markdown", "txt", "csv"],
        );
    if let Some(window) = app.get_webview_window("main") {
        dialog = dialog.set_parent(&window);
    }
    let Some(selected) = dialog.blocking_pick_files() else {
        return Ok(None);
    };
    let selected_files = selected
        .into_iter()
        .map(|selected| {
            selected
                .into_path()
                .map_err(|_| "A selected source is not a local file".to_string())
        })
        .collect::<Result<Vec<_>, _>>()?;
    let data_dir = app
        .path()
        .app_data_dir()
        .map_err(|_| "Unable to resolve the local data directory".to_string())?;
    prepare_data_dir(&data_dir)?;
    let scan_id = Uuid::new_v4().to_string();

    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        let staged = stage_source_files(&selected_files, &data_dir, &scan_id)?;
        preview_staged_profile_sources(&app, &data_dir, &scan_id, staged).await
    };
    if result.is_ok() {
        invalidate_status(&runtime).await;
    }
    result.map(Some)
}

#[tauri::command]
async fn scan_profile_source_folder(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
) -> Result<Option<ProfileSourceScanResult>, String> {
    let mut dialog = app
        .dialog()
        .file()
        .set_title("Choose a folder of profile source documents")
        .set_can_create_directories(false);
    if let Some(window) = app.get_webview_window("main") {
        dialog = dialog.set_parent(&window);
    }
    let Some(selected) = dialog.blocking_pick_folder() else {
        return Ok(None);
    };
    let selected = selected
        .into_path()
        .map_err(|_| "The selected source folder is not local".to_string())?;
    let data_dir = app
        .path()
        .app_data_dir()
        .map_err(|_| "Unable to resolve the local data directory".to_string())?;
    prepare_data_dir(&data_dir)?;
    let scan_id = Uuid::new_v4().to_string();

    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        let staged = stage_source_folder(&selected, &data_dir, &scan_id)?;
        preview_staged_profile_sources(&app, &data_dir, &scan_id, staged).await
    };
    if result.is_ok() {
        invalidate_status(&runtime).await;
    }
    result.map(Some)
}

#[tauri::command]
async fn build_profile_from_source_scan(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    scan_id: String,
) -> Result<ProfileSourceBuildResult, String> {
    let normalized_scan_id = validated_source_scan_id(&scan_id)?;
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<ProfileSourceBuildResult>(
            &app,
            "profile.sources.commit",
            json!({"scan_id": normalized_scan_id}),
            ENGINE_SOURCE_BUILD_TIMEOUT,
        )
        .await
    };
    // A malformed or interrupted response can still follow a completed local
    // commit, so never retain a pre-commit status cache entry.
    invalidate_status(&runtime).await;
    let result = result?;
    if result.scan_id != scan_id {
        return Err("The local engine returned a mismatched source scan".to_string());
    }
    result.validated_review_item_count()
}

#[tauri::command]
async fn discard_profile_source_scan(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    scan_id: String,
) -> Result<DiscardSourceScanResult, String> {
    let normalized_scan_id = validated_source_scan_id(&scan_id)?;
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<DiscardSourceScanResult>(
            &app,
            "profile.sources.discard",
            json!({"scan_id": normalized_scan_id}),
            ENGINE_IMPORT_TIMEOUT,
        )
        .await
    }?;
    invalidate_status(&runtime).await;
    Ok(result)
}

#[tauri::command]
async fn discard_unfinished_profile_source_reviews(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
) -> Result<DiscardUnfinishedSourceReviewsResult, String> {
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<DiscardUnfinishedSourceReviewsResult>(
            &app,
            "profile.sources.discard_all",
            discard_unfinished_source_reviews_params(),
            ENGINE_IMPORT_TIMEOUT,
        )
        .await
    };
    // A malformed or interrupted response can still follow a completed local
    // mutation, so never retain a pre-discard status cache entry.
    invalidate_status(&runtime).await;
    result.and_then(DiscardUnfinishedSourceReviewsResult::validated)
}

#[tauri::command(rename_all = "camelCase")]
async fn reset_career_workspace(
    app: tauri::AppHandle,
    provider_runtime: tauri::State<'_, LocalProviderRuntime>,
    runtime: tauri::State<'_, EngineRuntime>,
    request_id: String,
    expected: CareerWorkspaceResetExpected,
) -> Result<CareerWorkspaceResetReceipt, String> {
    let params = career_workspace_reset_params(&expected, &request_id)?;

    // Tailoring establishes this global order. Keeping it here prevents a
    // reset from racing either provider-backed work or a local vault request.
    let _provider_guard = provider_runtime.lock_operations().await;
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<CareerWorkspaceResetReceipt>(
            &app,
            "workspace.reset",
            params,
            ENGINE_WORKSPACE_RESET_TIMEOUT,
        )
        .await
    };

    // A malformed or interrupted response may follow a completed filesystem
    // cutover. Never serve a pre-reset cached status after any reset attempt.
    invalidate_status(&runtime).await;
    result.and_then(|receipt| receipt.validated(&request_id))
}

#[tauri::command(rename_all = "camelCase")]
async fn reset_profile_source_imports(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    expected_generation: u64,
) -> Result<RetainedSourceStatus, String> {
    let request_id = Uuid::new_v4().to_string();
    let params = source_reset_params(expected_generation, &request_id)?;
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<RetainedSourceStatus>(
            &app,
            "profile.sources.reset",
            params,
            ENGINE_SOURCE_RESET_TIMEOUT,
        )
        .await
    };
    // A malformed or interrupted response can still follow a completed local
    // reset, so never retain a pre-reset status cache entry.
    invalidate_status(&runtime).await;
    result.and_then(|result| validated_source_reset_result(expected_generation, result))
}

#[tauri::command(rename_all = "camelCase")]
async fn list_retained_profile_sources(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    scope: String,
    offset: usize,
) -> Result<RetainedSourceListResult, String> {
    let expected_scope = RetainedSourceScope::parse(&scope)?;
    let params = retained_source_list_params(&scope, offset)?;
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<RetainedSourceListResult>(
            &app,
            "profile.sources.list",
            params,
            ENGINE_SOURCE_LIST_TIMEOUT,
        )
        .await?
    };
    result.validated(expected_scope, offset)
}

fn validate_managed_relative_path(relative_path: &str) -> Result<PathBuf, String> {
    let relative = PathBuf::from(relative_path);
    if relative.is_absolute()
        || !relative
            .components()
            .all(|component| matches!(component, Component::Normal(_)))
    {
        return Err("The local engine returned an unsafe artifact path".to_string());
    }
    let mut components = relative.components();
    if components
        .next()
        .and_then(|value| value.as_os_str().to_str())
        != Some("artifacts")
        || components
            .next()
            .and_then(|value| value.as_os_str().to_str())
            != Some("resumes")
    {
        return Err("The local engine returned an unsafe artifact path".to_string());
    }
    Ok(relative)
}

fn normalized_resume_format(value: &str) -> Result<String, String> {
    let normalized = value.trim().to_ascii_lowercase();
    if matches!(normalized.as_str(), "docx" | "pdf") {
        Ok(normalized)
    } else {
        Err("Resume format must be docx or pdf".to_string())
    }
}

fn resume_export_timeout(format: &str) -> Duration {
    if format == "pdf" {
        ENGINE_PDF_TIMEOUT
    } else {
        ENGINE_DOCX_TIMEOUT
    }
}

fn resume_media_type(format: &str) -> Option<&'static str> {
    match format {
        "docx" => Some("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        "pdf" => Some("application/pdf"),
        _ => None,
    }
}

fn validated_tailored_draft_id(value: &str) -> Result<String, String> {
    let parsed = Uuid::parse_str(value)
        .map_err(|_| "The tailored resume draft ID is invalid".to_string())?;
    let normalized = parsed.to_string();
    if normalized != value {
        return Err("The tailored resume draft ID is invalid".to_string());
    }
    Ok(normalized)
}

fn validated_tailored_revision_id(value: &str) -> Result<String, String> {
    let parsed = Uuid::parse_str(value)
        .map_err(|_| "The tailored resume revision ID is invalid".to_string())?;
    let normalized = parsed.to_string();
    if normalized != value {
        return Err("The tailored resume revision ID is invalid".to_string());
    }
    Ok(normalized)
}

fn validated_tailored_revision_request_id(value: &str) -> Result<String, String> {
    let parsed = Uuid::parse_str(value)
        .map_err(|_| "The tailored resume revision request ID is invalid".to_string())?;
    let normalized = parsed.to_string();
    if normalized != value {
        return Err("The tailored resume revision request ID is invalid".to_string());
    }
    Ok(normalized)
}

fn validated_tailored_revision_checksum(value: &str) -> Result<String, String> {
    if value.len() != 64
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err("The tailored resume revision checksum is invalid".to_string());
    }
    Ok(value.to_string())
}

fn validate_revision_edit_text(
    value: &str,
    field_name: &str,
    maximum_bytes: usize,
    allow_line_breaks: bool,
) -> Result<(), String> {
    if value.trim().is_empty() || value.len() > maximum_bytes {
        return Err(format!("The tailored resume {field_name} is invalid"));
    }
    if value.chars().any(|character| {
        character.is_control() && !(allow_line_breaks && matches!(character, '\n' | '\r' | '\t'))
    }) {
        return Err(format!("The tailored resume {field_name} is invalid"));
    }
    Ok(())
}

impl TailoredRevisionBasicsEdits {
    fn has_change(&self) -> bool {
        !self.label.is_missing() || !self.summary.is_missing()
    }

    fn validate(&self) -> Result<(), String> {
        match &self.label {
            RevisionPatchField::Missing => {}
            RevisionPatchField::Null => {
                return Err("The tailored resume label cannot be null".to_string());
            }
            RevisionPatchField::Value(value) => validate_revision_edit_text(
                value,
                "label",
                TAILORED_REVISION_LABEL_LIMIT_BYTES,
                false,
            )?,
        }
        if let RevisionPatchField::Value(value) = &self.summary {
            validate_revision_edit_text(
                value,
                "basics summary",
                TAILORED_REVISION_BASICS_SUMMARY_LIMIT_BYTES,
                true,
            )?;
        }
        Ok(())
    }
}

impl TailoredRevisionWorkEdit {
    fn validate(&self) -> Result<(), String> {
        validate_javascript_integer(self.source_index, "tailored resume work source index")?;
        if self.summary.is_missing() && self.highlights.is_missing() {
            return Err("Choose a work summary or highlights field to revise".to_string());
        }
        if let RevisionPatchField::Value(value) = &self.summary {
            validate_revision_edit_text(
                value,
                "work summary",
                TAILORED_REVISION_WORK_SUMMARY_LIMIT_BYTES,
                true,
            )?;
        }
        match &self.highlights {
            RevisionPatchField::Missing => {}
            RevisionPatchField::Null => {
                return Err("Tailored resume highlights must be an array".to_string());
            }
            RevisionPatchField::Value(highlights) => {
                if highlights.len() > TAILORED_REVISION_HIGHLIGHTS_LIMIT {
                    return Err("Too many tailored resume highlights were supplied".to_string());
                }
                for highlight in highlights {
                    validate_revision_edit_text(
                        highlight,
                        "highlight",
                        TAILORED_REVISION_HIGHLIGHT_LIMIT_BYTES,
                        true,
                    )?;
                }
            }
        }
        Ok(())
    }
}

impl TailoredResumeRevisionEdits {
    fn validate(&self) -> Result<(), String> {
        if !self.basics.has_change() && self.work.is_empty() {
            return Err("Choose at least one tailored resume field to revise".to_string());
        }
        self.basics.validate()?;
        if self.work.len() > TAILORED_REVISION_WORK_EDITS_LIMIT {
            return Err("The tailored resume work edit count is invalid".to_string());
        }
        let mut source_indexes = HashSet::new();
        for edit in &self.work {
            edit.validate()?;
            if !source_indexes.insert(edit.source_index) {
                return Err(
                    "Tailored resume work edits contain a duplicate source index".to_string(),
                );
            }
        }
        let encoded = serde_json::to_vec(self)
            .map_err(|_| "The tailored resume edits could not be encoded safely".to_string())?;
        if encoded.len() > TAILORED_REVISION_EDITS_LIMIT_BYTES {
            return Err("The tailored resume edits exceed the local limit".to_string());
        }
        Ok(())
    }
}

fn validate_revision_resume_node(
    value: &Value,
    depth: usize,
    node_count: &mut usize,
) -> Result<(), String> {
    if depth > TAILORED_REVISION_RESUME_MAX_DEPTH {
        return Err(
            "The local engine returned an overly deep tailored resume revision".to_string(),
        );
    }
    *node_count = node_count.saturating_add(1);
    if *node_count > TAILORED_REVISION_RESUME_MAX_NODES {
        return Err("The local engine returned too much tailored resume revision data".to_string());
    }
    match value {
        Value::Null | Value::Bool(_) => Ok(()),
        Value::Number(number) if number.as_f64().is_some_and(f64::is_finite) => Ok(()),
        Value::Number(_) => {
            Err("The local engine returned an invalid tailored resume number".to_string())
        }
        Value::String(text) => {
            if text.len() > TAILORED_REVISION_RESUME_MAX_STRING_BYTES
                || text.chars().any(|character| {
                    character.is_control() && !matches!(character, '\n' | '\r' | '\t')
                })
            {
                return Err(
                    "The local engine returned invalid tailored resume revision text".to_string(),
                );
            }
            Ok(())
        }
        Value::Array(items) => {
            for item in items {
                validate_revision_resume_node(item, depth + 1, node_count)?;
            }
            Ok(())
        }
        Value::Object(object) => {
            for (key, nested) in object {
                if key.is_empty()
                    || key.len() > TAILORED_REVISION_CHANGED_FIELD_LIMIT_BYTES
                    || key.chars().any(char::is_control)
                {
                    return Err(
                        "The local engine returned an invalid tailored resume revision key"
                            .to_string(),
                    );
                }
                validate_revision_resume_node(nested, depth + 1, node_count)?;
            }
            Ok(())
        }
    }
}

fn validate_revision_resume(value: &Value) -> Result<(), String> {
    if !value.is_object() {
        return Err("The local engine returned an invalid tailored resume revision".to_string());
    }
    let encoded = serde_json::to_vec(value)
        .map_err(|_| "The local engine returned an invalid tailored resume revision".to_string())?;
    if encoded.len() > TAILORED_REVISION_RESUME_LIMIT_BYTES {
        return Err("The local engine returned too much tailored resume revision data".to_string());
    }
    let mut node_count = 0;
    validate_revision_resume_node(value, 0, &mut node_count)
}

struct TailoredRevisionMetadata<'a> {
    id: &'a str,
    draft_id: &'a str,
    parent_revision_id: Option<&'a str>,
    revision_number: u64,
    created_at_ms: u64,
    parent_resume_checksum_sha256: &'a str,
    resume_checksum_sha256: &'a str,
    changed_field_count: u64,
}

impl TailoredRevisionMetadata<'_> {
    fn validate(&self) -> Result<(), String> {
        validate_uuid_receipt(self.id)?;
        validate_uuid_receipt(self.draft_id)?;
        if let Some(parent_revision_id) = self.parent_revision_id {
            validate_uuid_receipt(parent_revision_id)?;
            if parent_revision_id == self.id {
                return Err("The local engine returned a self-parented resume revision".to_string());
            }
        }
        if !matches!(
            (self.parent_revision_id, self.revision_number),
            (None, 1) | (Some(_), 2..)
        ) || self.created_at_ms == 0
            || self.changed_field_count == 0
            || self.changed_field_count > TAILORED_REVISION_CHANGED_FIELDS_LIMIT as u64
        {
            return Err("The local engine returned invalid resume revision metadata".to_string());
        }
        validate_javascript_integer(self.revision_number, "resume revision number")?;
        validate_javascript_integer(self.created_at_ms, "resume revision timestamp")?;
        validate_javascript_integer(self.changed_field_count, "resume revision field count")?;
        validate_sha256(self.parent_resume_checksum_sha256)?;
        validate_sha256(self.resume_checksum_sha256)?;
        if self.parent_resume_checksum_sha256 == self.resume_checksum_sha256 {
            return Err("The local engine returned an unchanged resume revision".to_string());
        }
        Ok(())
    }
}

impl TailoredResumeRevisionSummary {
    fn validate(&self) -> Result<(), String> {
        TailoredRevisionMetadata {
            id: &self.id,
            draft_id: &self.draft_id,
            parent_revision_id: self.parent_revision_id.as_deref(),
            revision_number: self.revision_number,
            created_at_ms: self.created_at_ms,
            parent_resume_checksum_sha256: &self.parent_resume_checksum_sha256,
            resume_checksum_sha256: &self.resume_checksum_sha256,
            changed_field_count: self.changed_field_count,
        }
        .validate()
    }
}

impl TailoredResumeRevision {
    fn validate(&self) -> Result<(), String> {
        TailoredRevisionMetadata {
            id: &self.id,
            draft_id: &self.draft_id,
            parent_revision_id: self.parent_revision_id.as_deref(),
            revision_number: self.revision_number,
            created_at_ms: self.created_at_ms,
            parent_resume_checksum_sha256: &self.parent_resume_checksum_sha256,
            resume_checksum_sha256: &self.resume_checksum_sha256,
            changed_field_count: self.changed_fields.len() as u64,
        }
        .validate()?;
        let mut changed_fields = HashSet::new();
        for field in &self.changed_fields {
            if field.trim().is_empty()
                || field.len() > TAILORED_REVISION_CHANGED_FIELD_LIMIT_BYTES
                || field.chars().any(char::is_control)
                || !changed_fields.insert(field)
            {
                return Err("The local engine returned invalid resume revision fields".to_string());
            }
        }
        validate_revision_resume(&self.resume)
    }

    fn validated_requested(self, expected_revision_id: &str) -> Result<Self, String> {
        self.validate()?;
        if self.id != expected_revision_id {
            return Err(
                "The local engine returned a different tailored resume revision than requested"
                    .to_string(),
            );
        }
        Ok(self)
    }

    fn validated_created(
        self,
        expected_draft_id: &str,
        expected_parent_revision_id: Option<&str>,
        expected_parent_resume_checksum_sha256: &str,
    ) -> Result<Self, String> {
        self.validate()?;
        if self.draft_id != expected_draft_id
            || self.parent_revision_id.as_deref() != expected_parent_revision_id
            || self.parent_resume_checksum_sha256 != expected_parent_resume_checksum_sha256
            || (expected_parent_revision_id.is_none() && self.revision_number != 1)
        {
            return Err(
                "The local engine returned a mismatched tailored resume revision".to_string(),
            );
        }
        Ok(self)
    }
}

impl TailoredResumeRevisionList {
    fn validated(self, expected_draft_id: &str) -> Result<Self, String> {
        validate_uuid_receipt(&self.draft_id)?;
        validate_sha256(&self.base_resume_checksum_sha256)?;
        if self.draft_id != expected_draft_id
            || self.revisions.len() > TAILORED_REVISION_LIST_LIMIT
            || (self.truncated && self.revisions.len() != TAILORED_REVISION_LIST_LIMIT)
        {
            return Err(
                "The local engine returned an invalid tailored resume revision list".to_string(),
            );
        }
        if self.revisions.is_empty() {
            if self.truncated {
                return Err(
                    "The local engine returned an invalid tailored resume revision list"
                        .to_string(),
                );
            }
            return Ok(self);
        }
        let mut revision_ids: HashSet<&str> = HashSet::new();
        for revision in &self.revisions {
            revision.validate()?;
            if revision.draft_id != self.draft_id || !revision_ids.insert(revision.id.as_str()) {
                return Err(
                    "The local engine returned inconsistent tailored resume revisions".to_string(),
                );
            }
        }
        let first = &self.revisions[0];
        match first.parent_revision_id.as_deref() {
            None if !self.truncated
                && first.revision_number == 1
                && first.parent_resume_checksum_sha256 == self.base_resume_checksum_sha256 => {}
            Some(parent_revision_id)
                if self.truncated
                    && first.revision_number > 1
                    && !revision_ids.contains(parent_revision_id) => {}
            _ => {
                return Err(
                    "The local engine returned inconsistent tailored resume revision lineage"
                        .to_string(),
                );
            }
        }
        for adjacent in self.revisions.windows(2) {
            let parent = &adjacent[0];
            let revision = &adjacent[1];
            if parent.revision_number.checked_add(1) != Some(revision.revision_number)
                || revision.parent_revision_id.as_deref() != Some(parent.id.as_str())
                || revision.parent_resume_checksum_sha256 != parent.resume_checksum_sha256
                || revision.created_at_ms < parent.created_at_ms
            {
                return Err(
                    "The local engine returned inconsistent tailored resume revision lineage"
                        .to_string(),
                );
            }
        }
        Ok(self)
    }
}

fn tailored_revision_list_params(draft_id: &str) -> Value {
    json!({"draft_id": draft_id})
}

fn tailored_revision_get_params(revision_id: &str) -> Value {
    json!({"revision_id": revision_id})
}

fn tailored_revision_create_params(
    request_id: &str,
    draft_id: &str,
    expected_parent_revision_id: Option<&str>,
    expected_parent_resume_checksum_sha256: &str,
    edits: &TailoredResumeRevisionEdits,
) -> Value {
    json!({
        "request_id": request_id,
        "draft_id": draft_id,
        "expected_parent_revision_id": expected_parent_revision_id,
        "expected_parent_resume_checksum_sha256": expected_parent_resume_checksum_sha256,
        "edits": edits,
    })
}

fn tailored_revision_export_params(revision_id: &str, format: &str) -> Value {
    json!({"revision_id": revision_id, "format": format})
}

fn tailored_export_params(draft_id: &str, format: &str) -> Value {
    json!({"draft_id": draft_id, "format": format})
}

fn resume_export_params(format: &str, profile_version_id: Option<&str>) -> Value {
    let mut params = serde_json::Map::new();
    params.insert("format".to_string(), json!(format));
    if let Some(profile_version_id) = profile_version_id {
        params.insert("profile_version_id".to_string(), json!(profile_version_id));
    }
    Value::Object(params)
}

impl EngineBaselineResumeArtifact {
    fn validated(
        self,
        expected_profile_version_id: Option<&str>,
        expected_format: &str,
    ) -> Result<EngineResumeArtifact, String> {
        validate_uuid_receipt(&self.profile_version_id)?;
        validate_uuid_receipt(&self.artifact_id)?;
        validate_sha256(&self.checksum_sha256)?;
        if expected_profile_version_id.is_some_and(|expected| self.profile_version_id != expected) {
            return Err(
                "The local engine returned a different profile version than requested".to_string(),
            );
        }
        if self.format != expected_format {
            return Err(
                "The local engine returned a different artifact format than requested".to_string(),
            );
        }
        if resume_media_type(&self.format) != Some(self.media_type.as_str()) {
            return Err("The local engine returned a mismatched artifact media type".to_string());
        }
        if self.byte_size == 0 || self.byte_size > MANAGED_ARTIFACT_LIMIT_BYTES {
            return Err("The local engine returned an invalid artifact size".to_string());
        }
        validate_engine_text(
            &self.suggested_filename,
            "suggested filename",
            240,
            false,
            false,
        )?;
        if sanitized_suggested_filename(&self.suggested_filename, &self.format)
            != self.suggested_filename
        {
            return Err("The local engine returned an unsafe suggested filename".to_string());
        }
        let relative = validate_managed_relative_path(&self.relative_path)?;
        if relative.extension().and_then(|value| value.to_str()) != Some(self.format.as_str())
            || relative.file_stem().and_then(|value| value.to_str())
                != Some(self.artifact_id.as_str())
        {
            return Err("The local engine returned mismatched artifact identity".to_string());
        }
        Ok(EngineResumeArtifact {
            artifact_id: self.artifact_id,
            format: self.format,
            relative_path: self.relative_path,
            checksum_sha256: self.checksum_sha256,
            byte_size: self.byte_size,
            suggested_filename: self.suggested_filename,
        })
    }
}

impl EngineTailoredResumeArtifact {
    fn validated(
        self,
        expected_draft_id: &str,
        expected_format: &str,
    ) -> Result<EngineResumeArtifact, String> {
        validate_uuid_receipt(&self.draft_id)?;
        validate_uuid_receipt(&self.opportunity_id)?;
        validate_uuid_receipt(&self.profile_version_id)?;
        validate_uuid_receipt(&self.artifact_id)?;
        validate_sha256(&self.checksum_sha256)?;
        if self.draft_id != expected_draft_id {
            return Err(
                "The local engine returned a different tailored resume draft than requested"
                    .to_string(),
            );
        }
        if self.format != expected_format {
            return Err(
                "The local engine returned a different artifact format than requested".to_string(),
            );
        }
        if resume_media_type(&self.format) != Some(self.media_type.as_str()) {
            return Err("The local engine returned a mismatched artifact media type".to_string());
        }
        if self.byte_size == 0 || self.byte_size > MANAGED_ARTIFACT_LIMIT_BYTES {
            return Err("The local engine returned an invalid artifact size".to_string());
        }
        validate_engine_text(
            &self.suggested_filename,
            "suggested filename",
            240,
            false,
            false,
        )?;
        if sanitized_suggested_filename(&self.suggested_filename, &self.format)
            != self.suggested_filename
        {
            return Err("The local engine returned an unsafe suggested filename".to_string());
        }
        let relative = validate_managed_relative_path(&self.relative_path)?;
        if relative.extension().and_then(|value| value.to_str()) != Some(self.format.as_str())
            || relative.file_stem().and_then(|value| value.to_str())
                != Some(self.artifact_id.as_str())
        {
            return Err("The local engine returned mismatched artifact identity".to_string());
        }
        Ok(EngineResumeArtifact {
            artifact_id: self.artifact_id,
            format: self.format,
            relative_path: self.relative_path,
            checksum_sha256: self.checksum_sha256,
            byte_size: self.byte_size,
            suggested_filename: self.suggested_filename,
        })
    }
}

impl EngineTailoredResumeRevisionArtifact {
    fn validated(
        self,
        expected_revision_id: &str,
        expected_format: &str,
    ) -> Result<EngineResumeArtifact, String> {
        validate_uuid_receipt(&self.revision_id)?;
        validate_uuid_receipt(&self.draft_id)?;
        validate_uuid_receipt(&self.opportunity_id)?;
        validate_uuid_receipt(&self.profile_version_id)?;
        validate_uuid_receipt(&self.artifact_id)?;
        validate_sha256(&self.checksum_sha256)?;
        if self.revision_id != expected_revision_id {
            return Err(
                "The local engine returned a different tailored resume revision than requested"
                    .to_string(),
            );
        }
        if self.format != expected_format {
            return Err(
                "The local engine returned a different artifact format than requested".to_string(),
            );
        }
        if resume_media_type(&self.format) != Some(self.media_type.as_str()) {
            return Err("The local engine returned a mismatched artifact media type".to_string());
        }
        if self.byte_size == 0 || self.byte_size > MANAGED_ARTIFACT_LIMIT_BYTES {
            return Err("The local engine returned an invalid artifact size".to_string());
        }
        validate_engine_text(
            &self.suggested_filename,
            "suggested filename",
            240,
            false,
            false,
        )?;
        if sanitized_suggested_filename(&self.suggested_filename, &self.format)
            != self.suggested_filename
        {
            return Err("The local engine returned an unsafe suggested filename".to_string());
        }
        let relative = validate_managed_relative_path(&self.relative_path)?;
        if relative.extension().and_then(|value| value.to_str()) != Some(self.format.as_str())
            || relative.file_stem().and_then(|value| value.to_str())
                != Some(self.artifact_id.as_str())
        {
            return Err("The local engine returned mismatched artifact identity".to_string());
        }
        Ok(EngineResumeArtifact {
            artifact_id: self.artifact_id,
            format: self.format,
            relative_path: self.relative_path,
            checksum_sha256: self.checksum_sha256,
            byte_size: self.byte_size,
            suggested_filename: self.suggested_filename,
        })
    }
}

fn verify_managed_artifact(
    app: &tauri::AppHandle,
    artifact: &EngineResumeArtifact,
) -> Result<PathBuf, String> {
    let relative = validate_managed_relative_path(&artifact.relative_path)?;
    let expected_extension = match artifact.format.as_str() {
        "docx" => "docx",
        "pdf" => "pdf",
        _ => return Err("The local engine returned an unsupported artifact format".to_string()),
    };
    if relative.extension().and_then(|value| value.to_str()) != Some(expected_extension) {
        return Err("The local engine returned a mismatched artifact format".to_string());
    }
    let root = app
        .path()
        .app_data_dir()
        .map_err(|_| "Unable to resolve the local data directory".to_string())?;
    prepare_data_dir(&root)?;
    let canonical_root = fs::canonicalize(&root)
        .map_err(|_| "The local data directory could not be verified".to_string())?;
    let unresolved = canonical_root.join(relative);
    let link_metadata = fs::symlink_metadata(&unresolved)
        .map_err(|_| "The managed resume artifact is missing".to_string())?;
    if link_metadata.file_type().is_symlink() || !link_metadata.file_type().is_file() {
        return Err("The managed resume artifact is not a regular file".to_string());
    }
    let canonical = fs::canonicalize(&unresolved)
        .map_err(|_| "The managed resume artifact could not be verified".to_string())?;
    if !canonical.starts_with(&canonical_root) {
        return Err("The managed resume artifact escaped the local vault".to_string());
    }
    let metadata = fs::metadata(&canonical)
        .map_err(|_| "The managed resume artifact could not be inspected".to_string())?;
    if !metadata.is_file()
        || metadata.len() == 0
        || metadata.len() > MANAGED_ARTIFACT_LIMIT_BYTES
        || metadata.len() != artifact.byte_size
    {
        return Err("The managed resume artifact has an invalid size".to_string());
    }
    let mut file = File::open(&canonical)
        .map_err(|_| "The managed resume artifact could not be opened".to_string())?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    let mut total = 0_u64;
    loop {
        let read = file
            .read(&mut buffer)
            .map_err(|_| "The managed resume artifact could not be read".to_string())?;
        if read == 0 {
            break;
        }
        total = total.saturating_add(read as u64);
        if total > MANAGED_ARTIFACT_LIMIT_BYTES {
            return Err("The managed resume artifact exceeds the local limit".to_string());
        }
        hasher.update(&buffer[..read]);
    }
    if total != artifact.byte_size || format!("{:x}", hasher.finalize()) != artifact.checksum_sha256
    {
        return Err("The managed resume artifact failed its integrity check".to_string());
    }
    Ok(canonical)
}

fn sanitized_suggested_filename(value: &str, extension: &str) -> String {
    let leaf = value.replace('\\', "/");
    let leaf = leaf.rsplit('/').next().unwrap_or_default();
    let stem = Path::new(leaf)
        .file_stem()
        .and_then(|value| value.to_str())
        .unwrap_or("CVGnome-Resume");
    let stem: String = stem
        .chars()
        .filter(|character| !character.is_control() && !matches!(character, '/' | '\\' | ':'))
        .take(150)
        .collect();
    let stem = stem.trim().trim_matches('.');
    format!(
        "{}.{}",
        if stem.is_empty() {
            "CVGnome-Resume"
        } else {
            stem
        },
        extension
    )
}

fn destination_with_extension(path: PathBuf, extension: &str) -> Result<(PathBuf, bool), String> {
    match path.extension().and_then(|value| value.to_str()) {
        Some(current) if current.eq_ignore_ascii_case(extension) => Ok((path, false)),
        Some(_) => Err(format!("Choose a destination ending in .{extension}")),
        None => {
            let mut updated = path;
            updated.set_extension(extension);
            Ok((updated, true))
        }
    }
}

fn atomic_copy_artifact(
    source: &Path,
    destination: &Path,
    expected_size: u64,
    expected_sha256: &str,
    allow_replace: bool,
) -> Result<(), String> {
    let parent = destination
        .parent()
        .filter(|value| value.is_dir())
        .ok_or_else(|| "The selected destination directory is unavailable".to_string())?;
    if destination.is_dir() {
        return Err("The selected destination is a directory".to_string());
    }
    let mut source_file = File::open(source)
        .map_err(|_| "The managed resume artifact could not be opened".to_string())?;
    let mut temporary = NamedTempFile::new_in(parent)
        .map_err(|_| "A temporary export file could not be created".to_string())?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    let mut total = 0_u64;
    loop {
        let read = source_file
            .read(&mut buffer)
            .map_err(|_| "The managed resume artifact could not be read".to_string())?;
        if read == 0 {
            break;
        }
        total = total.saturating_add(read as u64);
        if total > MANAGED_ARTIFACT_LIMIT_BYTES {
            return Err("The managed resume artifact exceeds the local limit".to_string());
        }
        temporary
            .write_all(&buffer[..read])
            .map_err(|_| "The resume copy could not be written".to_string())?;
        hasher.update(&buffer[..read]);
    }
    if total != expected_size || format!("{:x}", hasher.finalize()) != expected_sha256 {
        return Err("The managed resume artifact changed during export".to_string());
    }
    temporary
        .as_file_mut()
        .flush()
        .map_err(|_| "The resume copy could not be flushed".to_string())?;
    temporary
        .as_file()
        .sync_all()
        .map_err(|_| "The resume copy could not be synchronized".to_string())?;
    if allow_replace {
        temporary
            .persist(destination)
            .map_err(|_| "The resume copy could not be saved atomically".to_string())?;
    } else {
        temporary.persist_noclobber(destination).map_err(|error| {
            if error.error.kind() == std::io::ErrorKind::AlreadyExists {
                "destination_exists: The destination changed; choose a new filename".to_string()
            } else {
                "The resume copy could not be saved atomically".to_string()
            }
        })?;
    }
    #[cfg(unix)]
    {
        // The atomic publish has already succeeded. Directory fsync is a
        // best-effort durability enhancement; reporting failure here would tell
        // the user an existing destination was untouched when it was replaced.
        let _ = File::open(parent).and_then(|directory| directory.sync_all());
    }
    Ok(())
}

fn save_resume_artifact_copy(
    app: &tauri::AppHandle,
    artifact: EngineResumeArtifact,
    dialog_title: &str,
) -> Result<Option<ResumeExportResult>, String> {
    let managed_path = verify_managed_artifact(app, &artifact)?;
    let suggested_filename =
        sanitized_suggested_filename(&artifact.suggested_filename, &artifact.format);

    let mut dialog = app
        .dialog()
        .file()
        .set_title(dialog_title)
        .set_file_name(&suggested_filename)
        .add_filter(
            if artifact.format == "pdf" {
                "PDF document"
            } else {
                "Word document"
            },
            &[artifact.format.as_str()],
        );
    if let Some(window) = app.get_webview_window("main") {
        dialog = dialog.set_parent(&window);
    }
    let Some(destination) = dialog.blocking_save_file() else {
        return Ok(None);
    };
    let destination = destination
        .into_path()
        .map_err(|_| "The selected destination is not a local file".to_string())?;
    let (destination, extension_added) = destination_with_extension(destination, &artifact.format)?;
    let existed_after_dialog = destination.exists();
    if extension_added && existed_after_dialog {
        return Err("destination_exists: Choose a different filename and try again".to_string());
    }
    atomic_copy_artifact(
        &managed_path,
        &destination,
        artifact.byte_size,
        &artifact.checksum_sha256,
        existed_after_dialog,
    )?;
    let saved_filename = destination
        .file_name()
        .and_then(|value| value.to_str())
        .map(str::to_owned);
    Ok(Some(ResumeExportResult {
        artifact_id: artifact.artifact_id,
        format: artifact.format,
        byte_size: artifact.byte_size,
        checksum_sha256: artifact.checksum_sha256,
        suggested_filename,
        saved_filename,
    }))
}

#[tauri::command(rename_all = "camelCase")]
async fn export_resume(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    format: String,
    profile_version_id: Option<String>,
) -> Result<Option<ResumeExportResult>, String> {
    let normalized_format = normalized_resume_format(&format)?;
    let normalized_profile_version_id = profile_version_id
        .as_deref()
        .map(validated_profile_review_version_id)
        .transpose()?;
    let artifact_result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<EngineBaselineResumeArtifact>(
            &app,
            "resume.export",
            resume_export_params(&normalized_format, normalized_profile_version_id.as_deref()),
            resume_export_timeout(&normalized_format),
        )
        .await
    };
    invalidate_status(&runtime).await;
    let artifact =
        artifact_result?.validated(normalized_profile_version_id.as_deref(), &normalized_format)?;
    save_resume_artifact_copy(&app, artifact, "Save a copy of your resume")
}

#[tauri::command(rename_all = "camelCase")]
async fn list_tailored_resume_revisions(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    draft_id: String,
) -> Result<TailoredResumeRevisionList, String> {
    let draft_id = validated_tailored_draft_id(&draft_id)?;
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<TailoredResumeRevisionList>(
            &app,
            "tailoring.revisions.list",
            tailored_revision_list_params(&draft_id),
            ENGINE_TAILORED_REVISION_TIMEOUT,
        )
        .await
    }?;
    result.validated(&draft_id)
}

#[tauri::command(rename_all = "camelCase")]
async fn get_tailored_resume_revision(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    revision_id: String,
) -> Result<TailoredResumeRevision, String> {
    let revision_id = validated_tailored_revision_id(&revision_id)?;
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<TailoredResumeRevision>(
            &app,
            "tailoring.revisions.get",
            tailored_revision_get_params(&revision_id),
            ENGINE_TAILORED_REVISION_TIMEOUT,
        )
        .await
    }?;
    result.validated_requested(&revision_id)
}

#[tauri::command(rename_all = "camelCase")]
async fn save_tailored_resume_revision(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    request_id: String,
    draft_id: String,
    expected_parent_revision_id: Option<String>,
    expected_parent_resume_checksum_sha256: String,
    edits: TailoredResumeRevisionEdits,
) -> Result<TailoredResumeRevision, String> {
    let request_id = validated_tailored_revision_request_id(&request_id)?;
    let draft_id = validated_tailored_draft_id(&draft_id)?;
    let expected_parent_revision_id = expected_parent_revision_id
        .as_deref()
        .map(validated_tailored_revision_id)
        .transpose()?;
    let expected_parent_resume_checksum_sha256 =
        validated_tailored_revision_checksum(&expected_parent_resume_checksum_sha256)?;
    edits.validate()?;
    let params = tailored_revision_create_params(
        &request_id,
        &draft_id,
        expected_parent_revision_id.as_deref(),
        &expected_parent_resume_checksum_sha256,
        &edits,
    );
    let result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<TailoredResumeRevision>(
            &app,
            "tailoring.revisions.create",
            params,
            ENGINE_TAILORED_REVISION_TIMEOUT,
        )
        .await
    }?;
    result.validated_created(
        &draft_id,
        expected_parent_revision_id.as_deref(),
        &expected_parent_resume_checksum_sha256,
    )
}

#[tauri::command(rename_all = "camelCase")]
async fn export_tailored_resume_revision(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    revision_id: String,
    format: String,
) -> Result<Option<ResumeExportResult>, String> {
    let revision_id = validated_tailored_revision_id(&revision_id)?;
    let format = normalized_resume_format(&format)?;
    let artifact_result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<EngineTailoredResumeRevisionArtifact>(
            &app,
            "tailoring.revisions.export",
            tailored_revision_export_params(&revision_id, &format),
            resume_export_timeout(&format),
        )
        .await
    };
    invalidate_status(&runtime).await;
    let artifact = artifact_result?.validated(&revision_id, &format)?;
    save_resume_artifact_copy(
        &app,
        artifact,
        "Save a copy of this tailored resume revision",
    )
}

#[tauri::command(rename_all = "camelCase")]
async fn export_tailored_resume(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    draft_id: String,
    format: String,
) -> Result<Option<ResumeExportResult>, String> {
    let normalized_draft_id = validated_tailored_draft_id(&draft_id)?;
    let normalized_format = normalized_resume_format(&format)?;
    let artifact_result = {
        let _operation_guard = runtime.operation_gate.lock().await;
        run_engine_request::<EngineTailoredResumeArtifact>(
            &app,
            "tailoring.export",
            tailored_export_params(&normalized_draft_id, &normalized_format),
            resume_export_timeout(&normalized_format),
        )
        .await
    };
    invalidate_status(&runtime).await;
    let artifact = artifact_result?.validated(&normalized_draft_id, &normalized_format)?;
    save_resume_artifact_copy(&app, artifact, "Save a copy of this tailored resume")
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _, _| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .manage(EngineRuntime::default())
        .manage(LocalProviderRuntime::default())
        .invoke_handler(tauri::generate_handler![
            engine_status,
            get_local_provider_settings,
            get_provider_spend_control,
            discover_model_provider_models,
            select_model_provider,
            save_local_provider_settings,
            save_provider_spend_control,
            save_openai_api_key,
            delete_openai_api_key,
            test_local_provider_models,
            generate_tailored_resume,
            get_latest_tailored_resume,
            list_tailored_resume_revisions,
            get_tailored_resume_revision,
            save_tailored_resume_revision,
            export_tailored_resume_revision,
            save_and_match_opportunity,
            list_opportunities,
            get_opportunity,
            rematch_opportunity,
            update_opportunity_tracker,
            list_profile_versions,
            get_profile_review_version,
            get_profile_basics,
            diff_profile_versions,
            restore_profile_version,
            start_manual_profile,
            update_profile_basics,
            list_profile_work_history,
            update_profile_work_history,
            list_profile_projects,
            update_profile_projects,
            list_profile_education,
            update_profile_education,
            list_profile_skills,
            update_profile_skills,
            preview_profile_changes,
            apply_profile_changes,
            list_profile_review_items,
            transition_profile_review_item,
            apply_profile_review_item,
            list_source_review_items,
            apply_source_review_decisions,
            list_memories,
            get_memory,
            save_memory,
            queue_memory_project,
            get_profile_review,
            get_profile_review_section,
            import_canonical_profile,
            scan_profile_source_files,
            scan_profile_source_folder,
            build_profile_from_source_scan,
            discard_profile_source_scan,
            discard_unfinished_profile_source_reviews,
            reset_career_workspace,
            reset_profile_source_imports,
            list_retained_profile_sources,
            export_tailored_resume,
            export_resume
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| {
            if let tauri::RunEvent::ExitRequested { api, .. } = event {
                // Quit (including Cmd+Q) must take the same guarded path as the
                // window close button. Destroying the final window removes it
                // before the next exit request, so that request can finish.
                if let Some(window) = app.get_webview_window("main") {
                    api.prevent_exit();
                    let _ = window.close();
                }
            }
        });
}

#[cfg(test)]
mod tests {
    use super::*;

    const TAILORED_DRAFT_ID: &str = "11111111-1111-4111-8111-111111111111";
    const TAILORED_OPPORTUNITY_ID: &str = "22222222-2222-4222-8222-222222222222";
    const TAILORED_PROFILE_VERSION_ID: &str = "33333333-3333-4333-8333-333333333333";
    const TAILORED_ARTIFACT_ID: &str = "44444444-4444-4444-8444-444444444444";
    const TAILORED_REVISION_REQUEST_ID: &str = "55555555-5555-4555-8555-555555555555";
    const TAILORED_REVISION_ONE_ID: &str = "66666666-6666-4666-8666-666666666666";
    const TAILORED_REVISION_TWO_ID: &str = "77777777-7777-4777-8777-777777777777";
    const TAILORED_REVISION_ARTIFACT_ID: &str = "88888888-8888-4888-8888-888888888888";
    const PROFILE_REVIEW_VERSION_ID: &str = "99999999-9999-4999-8999-999999999999";
    const BASELINE_ARTIFACT_ID: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    const PROFILE_VERSION_ONE_ID: &str = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
    const PROFILE_VERSION_TWO_ID: &str = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
    const PROFILE_UPDATE_REQUEST_ID: &str = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
    const PROFILE_VERSION_THREE_ID: &str = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";
    const PROFILE_RESTORE_REQUEST_ID: &str = "ffffffff-ffff-4fff-8fff-ffffffffffff";
    const REVIEW_INBOX_ITEM_ID: &str = "12345678-1234-4234-8234-123456789abc";
    const REVIEW_INBOX_REQUEST_ID: &str = "abcdefab-cdef-4def-8def-abcdefabcdef";

    fn profile_review_summary_fixture() -> Value {
        let sections = PROFILE_REVIEW_SECTION_SPECS
            .iter()
            .enumerate()
            .map(|(index, (key, label))| {
                json!({
                    "key": key,
                    "label": label,
                    "count": if index == 0 { 12 } else { 0 },
                })
            })
            .collect::<Vec<_>>();
        json!({
            "profile_version_id": PROFILE_REVIEW_VERSION_ID,
            "version_number": 4,
            "created_at_ms": 1_800_000_000_000_u64,
            "source_kind": "source_documents",
            "renderable": true,
            "name": "Ada Lovelace",
            "headline": "Systems engineer",
            "summary": "Builds dependable analytical systems.",
            "contacts": [
                {"kind": "email", "label": "Email", "value": "ada@example.test"},
                {"kind": "profile", "label": "GitHub", "value": "https://example.test/ada"}
            ],
            "sections": sections,
        })
    }

    fn profile_review_item_fixture(ordinal: u64) -> Value {
        json!({
            "ordinal": ordinal,
            "title": format!("Principal Engineer {ordinal}"),
            "subtitle": "Analytical Engines",
            "date_range": "2020-01 – Present",
            "location": "London, UK",
            "summary": "Led dependable local data systems.",
            "highlights": ["Reduced processing time by 40%."],
            "tags": ["Python", "SQLite"],
            "details": [{"label": "Scope", "value": "Platform engineering"}],
        })
    }

    fn profile_review_page_fixture(offset: u64, total_items: u64, item_count: usize) -> Value {
        let items = (0..item_count)
            .map(|index| profile_review_item_fixture(offset + index as u64))
            .collect::<Vec<_>>();
        let page_end = offset + item_count as u64;
        json!({
            "profile_version_id": PROFILE_REVIEW_VERSION_ID,
            "key": "work",
            "label": "Work history",
            "total_items": total_items,
            "offset": offset,
            "limit": PROFILE_REVIEW_PAGE_LIMIT,
            "next_offset": if page_end < total_items { Some(page_end) } else { None },
            "items": items,
        })
    }

    fn profile_review_inbox_item_fixture(
        id: &str,
        state: &str,
        is_previous_import_set: bool,
    ) -> Value {
        json!({
            "id": id,
            "state": state,
            "state_revision": 0,
            "candidate_kind": "education",
            "operation_kind": "add",
            "title": "University of London",
            "subtitle": "BSc · Mathematics",
            "date_range": "1830 – 1835",
            "summary": null,
            "highlights": [],
            "tags": [],
            "details": [{"label": "Area", "value": "Mathematics"}],
            "changed_fields": ["institution", "study_type", "area"],
            "candidate": {
                "institution": "University of London",
                "study_type": "BSc",
                "area": "Mathematics",
                "url": null,
                "start_date": "1830",
                "end_date": "1835",
                "score": null,
                "courses": []
            },
            "evidence": [{
                "display_name": "education-notes.docx",
                "source_format": "docx",
                "source_kind": "resume",
                "location_label": "Lines 12–16",
                "excerpt": "University of London — BSc, Mathematics"
            }],
            "is_previous_import_set": is_previous_import_set,
            "created_at_ms": 1_800_000_000_000_u64
        })
    }

    fn profile_review_inbox_page_fixture() -> Value {
        json!({
            "current_profile_version_id": PROFILE_REVIEW_VERSION_ID,
            "scope": "inbox",
            "offset": 0,
            "limit": PROFILE_REVIEW_INBOX_PAGE_LIMIT,
            "total_items": 1,
            "next_offset": null,
            "counts": {"inbox": 1, "deferred": 2, "history": 3},
            "items": [profile_review_inbox_item_fixture(
                REVIEW_INBOX_ITEM_ID,
                "inbox",
                false,
            )]
        })
    }

    fn profile_review_inbox_transition_fixture() -> Value {
        json!({
            "request_id": REVIEW_INBOX_REQUEST_ID,
            "review_item_id": REVIEW_INBOX_ITEM_ID,
            "action": "defer",
            "previous_state": "inbox",
            "state": "deferred",
            "state_revision": 1,
            "created_at_ms": 1_800_000_000_100_u64,
            "created": true
        })
    }

    fn profile_review_inbox_apply_fixture(created: bool) -> Value {
        json!({
            "request_id": REVIEW_INBOX_REQUEST_ID,
            "review_item_id": REVIEW_INBOX_ITEM_ID,
            "previous_state": "inbox",
            "state": "applied",
            "state_revision": 1,
            "profile_version_id": PROFILE_VERSION_TWO_ID,
            "parent_profile_version_id": PROFILE_REVIEW_VERSION_ID,
            "version_number": 5,
            "candidate_kind": "education",
            "operation_kind": "add",
            "entry_index": 1,
            "changed_fields": ["institution", "study_type", "area"],
            "created_at_ms": 1_800_000_000_100_u64,
            "created": created
        })
    }

    fn profile_review_inbox_apply_request_fixture() -> ValidatedProfileReviewInboxApplyRequest {
        let candidate = serde_json::from_value::<ProfileReviewInboxCandidate>(
            profile_review_inbox_item_fixture(REVIEW_INBOX_ITEM_ID, "inbox", false)["candidate"]
                .clone(),
        )
        .unwrap();
        profile_review_inbox_apply_params(
            REVIEW_INBOX_ITEM_ID,
            "inbox",
            0,
            PROFILE_REVIEW_VERSION_ID,
            REVIEW_INBOX_REQUEST_ID,
            "add",
            candidate,
        )
        .unwrap()
    }

    async fn seed_stale_status_cache(runtime: &EngineRuntime) {
        *runtime.cached_status.lock().await = Some(CachedEngineStatus {
            checked_at: Instant::now(),
            result: Err("stale cached status".to_string()),
        });
    }

    fn profile_version_summary_fixture(
        profile_version_id: &str,
        parent_profile_version_id: Option<&str>,
        version_number: u64,
    ) -> Value {
        json!({
            "profile_version_id": profile_version_id,
            "parent_profile_version_id": parent_profile_version_id,
            "version_number": version_number,
            "created_at_ms": 1_800_000_000_000_u64 + version_number,
            "source_kind": if version_number == 1 { "source_documents" } else { "local_edit" },
            "name": "Ada Lovelace",
            "headline": "Systems engineer",
            "renderable": true,
        })
    }

    fn profile_version_list_fixture() -> Value {
        json!({
            "current_profile_version_id": PROFILE_VERSION_TWO_ID,
            "total_items": 2,
            "offset": 0,
            "limit": PROFILE_VERSION_LIST_LIMIT,
            "next_offset": null,
            "items": [
                profile_version_summary_fixture(
                    PROFILE_VERSION_TWO_ID,
                    Some(PROFILE_VERSION_ONE_ID),
                    2,
                ),
                profile_version_summary_fixture(PROFILE_VERSION_ONE_ID, None, 1),
            ],
        })
    }

    fn profile_restore_receipt_fixture(created: bool) -> Value {
        json!({
            "request_id": PROFILE_RESTORE_REQUEST_ID,
            "profile_version_id": PROFILE_VERSION_THREE_ID,
            "parent_profile_version_id": PROFILE_VERSION_TWO_ID,
            "restored_from_profile_version_id": PROFILE_VERSION_ONE_ID,
            "restored_from_version_number": 1,
            "version_number": 3,
            "created_at_ms": 1_800_000_000_003_u64,
            "created": created,
            "profile_name": "Ada Lovelace",
            "renderable": true,
        })
    }

    fn profile_basics_fixture() -> Value {
        json!({
            "profile_version_id": PROFILE_VERSION_TWO_ID,
            "version_number": 2,
            "created_at_ms": 1_800_000_000_002_u64,
            "renderable": true,
            "name": "Ada Lovelace",
            "headline": "Systems engineer",
            "summary": "Builds dependable analytical systems.",
            "email": "ada@example.com",
            "phone": "+1 555 0100",
            "url": "https://example.com/ada",
            "location": {
                "city": "London",
                "region": null,
                "country_code": "GB",
            },
        })
    }

    fn profile_work_page_fixture() -> Value {
        json!({
            "profile_version_id": PROFILE_VERSION_TWO_ID,
            "version_number": 2,
            "total_items": 2,
            "offset": 0,
            "limit": PROFILE_WORK_PAGE_LIMIT,
            "next_offset": null,
            "items": [
                {
                    "entry_index": 0,
                    "name": "Analytical Engines",
                    "position": "Systems engineer",
                    "url": "https://example.com/work",
                    "start_date": "2021-01",
                    "end_date": null,
                    "summary": "Built dependable analytical systems.",
                    "highlights": ["Reduced processing time by 40%."],
                    "location": "London",
                },
                {
                    "entry_index": 2,
                    "name": "Royal Society",
                    "position": "Research fellow",
                    "url": null,
                    "start_date": null,
                    "end_date": null,
                    "summary": null,
                    "highlights": [],
                    "location": null,
                },
            ],
        })
    }

    fn profile_work_update_receipt_fixture(created: bool) -> Value {
        json!({
            "request_id": PROFILE_UPDATE_REQUEST_ID,
            "profile_version_id": PROFILE_VERSION_TWO_ID,
            "parent_profile_version_id": PROFILE_VERSION_ONE_ID,
            "version_number": 2,
            "created_at_ms": 1_800_000_000_002_u64,
            "created": created,
            "operation": "update",
            "entry_index": 2,
            "changed_fields": ["summary", "highlights"],
            "profile_name": "Ada Lovelace",
            "work_entries": 2,
            "renderable": true,
        })
    }

    fn profile_projects_page_fixture() -> Value {
        json!({
            "profile_version_id": PROFILE_VERSION_TWO_ID,
            "section": "projects",
            "version_number": 2,
            "total_items": 2,
            "offset": 0,
            "limit": PROFILE_SECTION_PAGE_LIMIT,
            "next_offset": null,
            "items": [
                {
                    "entry_index": 0,
                    "name": "Analytical Engine",
                    "description": "A programmable mechanical computer.",
                    "url": "https://example.com/projects/engine",
                    "start_date": "1842",
                    "end_date": null,
                    "highlights": ["Published the first algorithm."],
                    "keywords": ["Computing", "Mathematics"],
                },
                {
                    "entry_index": 2,
                    "name": "Bernoulli Notes",
                    "description": null,
                    "url": null,
                    "start_date": null,
                    "end_date": null,
                    "highlights": [],
                    "keywords": [],
                },
            ],
        })
    }

    fn profile_education_page_fixture() -> Value {
        json!({
            "profile_version_id": PROFILE_VERSION_TWO_ID,
            "section": "education",
            "version_number": 2,
            "total_items": 2,
            "offset": 0,
            "limit": PROFILE_SECTION_PAGE_LIMIT,
            "next_offset": null,
            "items": [
                {
                    "entry_index": 0,
                    "institution": "University of London",
                    "study_type": "Independent study",
                    "area": "Mathematics",
                    "url": "https://example.com/education",
                    "start_date": "1830",
                    "end_date": "1832",
                    "score": null,
                    "courses": ["Advanced mathematics"],
                },
                {
                    "entry_index": 3,
                    "institution": "",
                    "study_type": null,
                    "area": null,
                    "url": null,
                    "start_date": null,
                    "end_date": null,
                    "score": null,
                    "courses": [],
                },
            ],
        })
    }

    fn profile_skills_page_fixture() -> Value {
        json!({
            "profile_version_id": PROFILE_VERSION_TWO_ID,
            "section": "skills",
            "version_number": 2,
            "total_items": 2,
            "offset": 0,
            "limit": PROFILE_SECTION_PAGE_LIMIT,
            "next_offset": null,
            "items": [
                {
                    "entry_index": 1,
                    "name": "Computing",
                    "level": "Advanced",
                    "keywords": ["Algorithms", "Mathematics"],
                },
                {
                    "entry_index": 4,
                    "name": "",
                    "level": null,
                    "keywords": [],
                },
            ],
        })
    }

    fn profile_section_update_receipt_fixture(section: &str, changed_fields: &[&str]) -> Value {
        json!({
            "request_id": PROFILE_UPDATE_REQUEST_ID,
            "profile_version_id": PROFILE_VERSION_TWO_ID,
            "parent_profile_version_id": PROFILE_VERSION_ONE_ID,
            "version_number": 2,
            "created_at_ms": 1_800_000_000_002_u64,
            "created": true,
            "section": section,
            "operation": "update",
            "entry_index": 2,
            "changed_fields": changed_fields,
            "profile_name": "Ada Lovelace",
            "section_entries": 2,
            "renderable": true,
        })
    }

    fn profile_version_diff_fixture() -> Value {
        let section_changes = [json!({
            "key": "work",
            "label": "Work history",
            "before_count": 1,
            "after_count": 2,
            "content_changed": true,
        })];
        json!({
            "from_version": profile_version_summary_fixture(PROFILE_VERSION_ONE_ID, None, 1),
            "to_version": profile_version_summary_fixture(
                PROFILE_VERSION_TWO_ID,
                Some(PROFILE_VERSION_ONE_ID),
                2,
            ),
            "basic_changes": [{
                "field": "headline",
                "label": "Headline",
                "before": null,
                "after": "Systems engineer",
            }],
            "section_changes": section_changes,
            "total_changes": 2,
        })
    }

    fn profile_update_receipt_fixture(created: bool) -> Value {
        json!({
            "request_id": PROFILE_UPDATE_REQUEST_ID,
            "profile_version_id": PROFILE_VERSION_TWO_ID,
            "parent_profile_version_id": PROFILE_VERSION_ONE_ID,
            "version_number": 2,
            "created_at_ms": 1_800_000_000_002_u64,
            "created": created,
            "changed_fields": ["headline", "location.country_code"],
            "profile_name": "Ada Lovelace",
            "headline": "Systems engineer",
            "renderable": true,
        })
    }

    fn manual_profile_start_receipt_fixture(created: bool) -> Value {
        json!({
            "request_id": PROFILE_UPDATE_REQUEST_ID,
            "profile_version_id": PROFILE_VERSION_ONE_ID,
            "parent_profile_version_id": null,
            "version_number": 1,
            "created_at_ms": 1_800_000_000_001_u64,
            "created": created,
            "changed_fields": [
                "name",
                "headline",
                "summary",
                "location.city",
                "location.country_code",
            ],
            "profile_name": "Ada Lovelace",
            "headline": "Systems engineer",
            "renderable": true,
        })
    }

    fn tailored_revision_fixture() -> Value {
        json!({
            "id": TAILORED_REVISION_ONE_ID,
            "draft_id": TAILORED_DRAFT_ID,
            "parent_revision_id": null,
            "revision_number": 1,
            "author_kind": "user",
            "created_at_ms": 1_800_000_000_000_u64,
            "parent_resume_checksum_sha256": "a".repeat(64),
            "resume_checksum_sha256": "b".repeat(64),
            "changed_fields": ["/basics/label", "/work/0/summary"],
            "resume": {
                "basics": {"name": "Ada Lovelace", "label": "Product leader"},
                "work": [{"name": "Analytical Engines", "summary": "Led delivery."}]
            }
        })
    }

    fn tailored_revision_summary_fixture(
        revision_id: &str,
        parent_revision_id: Option<&str>,
        revision_number: u64,
        parent_checksum: &str,
        resume_checksum: &str,
    ) -> Value {
        json!({
            "id": revision_id,
            "draft_id": TAILORED_DRAFT_ID,
            "parent_revision_id": parent_revision_id,
            "revision_number": revision_number,
            "author_kind": "user",
            "created_at_ms": 1_800_000_000_000_u64 + revision_number,
            "parent_resume_checksum_sha256": parent_checksum,
            "resume_checksum_sha256": resume_checksum,
            "changed_field_count": 2
        })
    }

    fn tailored_revision_list_fixture() -> Value {
        json!({
            "draft_id": TAILORED_DRAFT_ID,
            "base_resume_checksum_sha256": "a".repeat(64),
            "revisions": [
                tailored_revision_summary_fixture(
                    TAILORED_REVISION_ONE_ID,
                    None,
                    1,
                    &"a".repeat(64),
                    &"b".repeat(64),
                ),
                tailored_revision_summary_fixture(
                    TAILORED_REVISION_TWO_ID,
                    Some(TAILORED_REVISION_ONE_ID),
                    2,
                    &"b".repeat(64),
                    &"c".repeat(64),
                )
            ],
            "truncated": false
        })
    }

    fn tailored_revision_artifact_fixture() -> Value {
        json!({
            "revision_id": TAILORED_REVISION_TWO_ID,
            "draft_id": TAILORED_DRAFT_ID,
            "opportunity_id": TAILORED_OPPORTUNITY_ID,
            "profile_version_id": TAILORED_PROFILE_VERSION_ID,
            "artifact_id": TAILORED_REVISION_ARTIFACT_ID,
            "format": "pdf",
            "media_type": "application/pdf",
            "relative_path": format!(
                "artifacts/resumes/{TAILORED_REVISION_ARTIFACT_ID}.pdf"
            ),
            "checksum_sha256": "d".repeat(64),
            "byte_size": 4_096,
            "suggested_filename": "Ada-Lovelace-Tailored-Resume-Revision-2.pdf"
        })
    }

    fn tailored_artifact_fixture() -> Value {
        json!({
            "draft_id": TAILORED_DRAFT_ID,
            "opportunity_id": TAILORED_OPPORTUNITY_ID,
            "profile_version_id": TAILORED_PROFILE_VERSION_ID,
            "artifact_id": TAILORED_ARTIFACT_ID,
            "format": "pdf",
            "media_type": "application/pdf",
            "relative_path": format!("artifacts/resumes/{TAILORED_ARTIFACT_ID}.pdf"),
            "checksum_sha256": "a".repeat(64),
            "byte_size": 4_096,
            "suggested_filename": "Ada-Lovelace-Tailored-Resume-v3.pdf"
        })
    }

    fn baseline_artifact_fixture() -> Value {
        json!({
            "profile_version_id": PROFILE_REVIEW_VERSION_ID,
            "artifact_id": BASELINE_ARTIFACT_ID,
            "format": "pdf",
            "media_type": "application/pdf",
            "relative_path": format!("artifacts/resumes/{BASELINE_ARTIFACT_ID}.pdf"),
            "checksum_sha256": "e".repeat(64),
            "byte_size": 4_096,
            "suggested_filename": "Ada-Lovelace-Resume-v1.pdf"
        })
    }

    #[test]
    fn engine_output_boundary_accepts_arm_envelope_and_rejects_overflow() {
        let mut buffer = Vec::new();
        let mut total = 0;
        append_bounded(&mut buffer, &vec![b'a'; 160 * 1024], &mut total).unwrap();
        append_bounded(&mut buffer, &vec![b'b'; 32 * 1024], &mut total).unwrap();
        assert_eq!(buffer.len(), ENGINE_OUTPUT_LIMIT_BYTES);
        assert!(append_bounded(&mut buffer, b"c", &mut total).is_err());
        assert_eq!(buffer.len(), ENGINE_OUTPUT_LIMIT_BYTES);
    }

    #[test]
    fn profile_review_command_arguments_and_engine_params_are_exact() {
        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase", deny_unknown_fields)]
        struct CommandArguments {
            profile_version_id: String,
            section: String,
            offset: usize,
        }

        let arguments: CommandArguments = serde_json::from_value(json!({
            "profileVersionId": PROFILE_REVIEW_VERSION_ID,
            "section": "work",
            "offset": 10,
        }))
        .unwrap();
        let profile_version_id =
            validated_profile_review_version_id(&arguments.profile_version_id).unwrap();
        let section = ProfileReviewSectionKey::parse(&arguments.section).unwrap();
        let offset = validated_profile_review_offset(arguments.offset).unwrap();
        assert_eq!(
            profile_review_section_params(&profile_version_id, section, offset),
            json!({
                "profile_version_id": PROFILE_REVIEW_VERSION_ID,
                "section": "work",
                "offset": 10,
            })
        );
        assert!(
            serde_json::from_value::<CommandArguments>(json!({
                "profile_version_id": PROFILE_REVIEW_VERSION_ID,
                "section": "work",
                "offset": 0,
            }))
            .is_err()
        );
        assert!(
            serde_json::from_value::<CommandArguments>(json!({
                "profileVersionId": PROFILE_REVIEW_VERSION_ID,
                "section": "work",
                "offset": 0,
                "limit": 10,
            }))
            .is_err()
        );
        assert!(validated_profile_review_version_id("NOT-A-UUID").is_err());
        assert!(
            validated_profile_review_version_id("AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA").is_err()
        );
        assert!(ProfileReviewSectionKey::parse("meta").is_err());
        assert!(validated_profile_review_offset(PROFILE_REVIEW_OFFSET_LIMIT).is_ok());
        assert!(validated_profile_review_offset(PROFILE_REVIEW_OFFSET_LIMIT + 1).is_err());
        let _summary_command = get_profile_review;
        let _section_command = get_profile_review_section;
    }

    #[test]
    fn review_inbox_commands_use_exact_camel_case_arguments_and_engine_params() {
        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase", deny_unknown_fields)]
        struct ListArguments {
            scope: String,
            offset: usize,
        }

        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase", deny_unknown_fields)]
        struct TransitionArguments {
            review_item_id: String,
            expected_state: String,
            expected_revision: u64,
            request_id: String,
            action: String,
        }

        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase", deny_unknown_fields)]
        struct ApplyArguments {
            review_item_id: String,
            expected_state: String,
            expected_revision: u64,
            expected_parent_profile_version_id: String,
            request_id: String,
            expected_operation_kind: String,
            candidate: ProfileReviewInboxCandidate,
        }

        let list: ListArguments = serde_json::from_value(json!({
            "scope": "deferred",
            "offset": 10,
        }))
        .unwrap();
        let (scope, params) = profile_review_inbox_list_params(&list.scope, list.offset).unwrap();
        assert_eq!(scope, ProfileReviewInboxScope::Deferred);
        assert_eq!(params, json!({"scope": "deferred", "offset": 10}));
        assert!(params.get("limit").is_none());

        let transition: TransitionArguments = serde_json::from_value(json!({
            "reviewItemId": REVIEW_INBOX_ITEM_ID,
            "expectedState": "inbox",
            "expectedRevision": 0,
            "requestId": REVIEW_INBOX_REQUEST_ID,
            "action": "defer",
        }))
        .unwrap();
        let (_, state, revision, _, action, params) = profile_review_inbox_transition_params(
            &transition.review_item_id,
            &transition.expected_state,
            transition.expected_revision,
            &transition.request_id,
            &transition.action,
        )
        .unwrap();
        assert_eq!(state, ProfileReviewInboxState::Inbox);
        assert_eq!(revision, 0);
        assert_eq!(action, ProfileReviewInboxAction::Defer);
        assert_eq!(
            params,
            json!({
                "review_item_id": REVIEW_INBOX_ITEM_ID,
                "expected_state": "inbox",
                "expected_state_revision": 0,
                "request_id": REVIEW_INBOX_REQUEST_ID,
                "action": "defer",
            })
        );
        let candidate =
            profile_review_inbox_item_fixture(REVIEW_INBOX_ITEM_ID, "inbox", false)["candidate"]
                .clone();
        for invalid in [
            json!({
                "reviewItemId": REVIEW_INBOX_ITEM_ID,
                "expectedState": "inbox",
                "expectedRevision": 0,
                "expectedParentProfileVersionId": PROFILE_REVIEW_VERSION_ID,
                "requestId": REVIEW_INBOX_REQUEST_ID,
                "candidate": candidate,
            }),
            json!({
                "reviewItemId": REVIEW_INBOX_ITEM_ID,
                "expectedState": "inbox",
                "expectedRevision": 0,
                "expectedParentProfileVersionId": PROFILE_REVIEW_VERSION_ID,
                "requestId": REVIEW_INBOX_REQUEST_ID,
                "expectedOperationKind": "add",
                "candidate": candidate,
                "sourcePath": "/private/resume.pdf",
            }),
        ] {
            assert!(serde_json::from_value::<ApplyArguments>(invalid).is_err());
        }

        let apply: ApplyArguments = serde_json::from_value(json!({
            "reviewItemId": REVIEW_INBOX_ITEM_ID,
            "expectedState": "inbox",
            "expectedRevision": 0,
            "expectedParentProfileVersionId": PROFILE_REVIEW_VERSION_ID,
            "requestId": REVIEW_INBOX_REQUEST_ID,
            "expectedOperationKind": "add",
            "candidate": candidate,
        }))
        .unwrap();
        let apply_request = profile_review_inbox_apply_params(
            &apply.review_item_id,
            &apply.expected_state,
            apply.expected_revision,
            &apply.expected_parent_profile_version_id,
            &apply.request_id,
            &apply.expected_operation_kind,
            apply.candidate,
        )
        .unwrap();
        assert_eq!(apply_request.expected_state_revision, 0);
        assert_eq!(
            apply_request.expected_parent_profile_version_id,
            PROFILE_REVIEW_VERSION_ID
        );
        assert_eq!(apply_request.request_id, REVIEW_INBOX_REQUEST_ID);
        assert_eq!(
            apply_request.expected_operation_kind,
            ProfileReviewInboxOperationKind::Add
        );
        assert_eq!(
            apply_request.candidate.kind(),
            ProfileReviewInboxCandidateKind::Education
        );
        assert_eq!(
            apply_request.params,
            json!({
                "review_item_id": REVIEW_INBOX_ITEM_ID,
                "request_id": REVIEW_INBOX_REQUEST_ID,
                "expected_state": "inbox",
                "expected_state_revision": 0,
                "expected_parent_profile_version_id": PROFILE_REVIEW_VERSION_ID,
                "candidate": {
                    "institution": "University of London",
                    "study_type": "BSc",
                    "area": "Mathematics",
                    "url": null,
                    "start_date": "1830",
                    "end_date": "1835",
                    "score": null,
                    "courses": []
                },
            })
        );

        for invalid in [
            json!({
                "review_item_id": REVIEW_INBOX_ITEM_ID,
                "expectedState": "inbox",
                "expectedRevision": 0,
                "requestId": REVIEW_INBOX_REQUEST_ID,
                "action": "defer",
            }),
            json!({
                "reviewItemId": REVIEW_INBOX_ITEM_ID,
                "expectedState": "inbox",
                "expectedStateRevision": 0,
                "requestId": REVIEW_INBOX_REQUEST_ID,
                "action": "defer",
            }),
            json!({
                "reviewItemId": REVIEW_INBOX_ITEM_ID,
                "expectedState": "inbox",
                "expectedRevision": 0,
                "requestId": REVIEW_INBOX_REQUEST_ID,
                "action": "defer",
                "candidate": {"private": true},
            }),
        ] {
            assert!(serde_json::from_value::<TransitionArguments>(invalid).is_err());
        }
        assert!(ProfileReviewInboxScope::parse("active").is_err());
        assert!(
            profile_review_inbox_list_params("inbox", PROFILE_REVIEW_INBOX_OFFSET_LIMIT + 1)
                .is_err()
        );
        assert!(
            profile_review_inbox_transition_params(
                &REVIEW_INBOX_ITEM_ID.to_uppercase(),
                "inbox",
                0,
                REVIEW_INBOX_REQUEST_ID,
                "defer",
            )
            .is_err()
        );
        assert!(
            profile_review_inbox_transition_params(
                REVIEW_INBOX_ITEM_ID,
                "inbox",
                JAVASCRIPT_MAX_SAFE_INTEGER,
                REVIEW_INBOX_REQUEST_ID,
                "defer",
            )
            .is_err()
        );
        assert!(
            profile_review_inbox_transition_params(
                REVIEW_INBOX_ITEM_ID,
                "rejected",
                1,
                REVIEW_INBOX_REQUEST_ID,
                "defer",
            )
            .is_err()
        );

        let education_candidate = || {
            serde_json::from_value::<ProfileReviewInboxCandidate>(json!({
                "institution": "University of London",
                "study_type": "BSc",
                "area": "Mathematics",
                "url": null,
                "start_date": "1830",
                "end_date": "1835",
                "score": null,
                "courses": []
            }))
            .unwrap()
        };
        assert!(
            profile_review_inbox_apply_params(
                REVIEW_INBOX_ITEM_ID,
                "deferred",
                0,
                PROFILE_REVIEW_VERSION_ID,
                REVIEW_INBOX_REQUEST_ID,
                "add",
                education_candidate(),
            )
            .is_err()
        );
        assert!(
            profile_review_inbox_apply_params(
                REVIEW_INBOX_ITEM_ID,
                "inbox",
                0,
                PROFILE_REVIEW_VERSION_ID,
                REVIEW_INBOX_REQUEST_ID,
                "remove",
                education_candidate(),
            )
            .is_err()
        );
        assert!(
            profile_review_inbox_apply_params(
                REVIEW_INBOX_ITEM_ID,
                "inbox",
                JAVASCRIPT_MAX_SAFE_INTEGER,
                PROFILE_REVIEW_VERSION_ID,
                REVIEW_INBOX_REQUEST_ID,
                "add",
                education_candidate(),
            )
            .is_err()
        );
        assert!(
            profile_review_inbox_apply_params(
                REVIEW_INBOX_ITEM_ID,
                "inbox",
                0,
                "not-a-profile-version",
                REVIEW_INBOX_REQUEST_ID,
                "add",
                education_candidate(),
            )
            .is_err()
        );
        assert!(
            serde_json::from_value::<ProfileReviewInboxCandidate>(json!({
                "institution": "University of London",
                "study_type": "BSc",
                "area": "Mathematics",
                "url": null,
                "start_date": "1830",
                "end_date": "1835",
                "score": null,
                "courses": [],
                "private_source_path": "/private/resume.pdf"
            }))
            .is_err()
        );
        let oversized_candidate =
            ProfileReviewInboxCandidate::Education(ProfileEducationEntryInput {
                institution: "x".repeat(PROFILE_REVIEW_INBOX_APPLY_REQUEST_LIMIT_BYTES),
                study_type: Some("BSc".to_string()),
                area: None,
                url: None,
                start_date: None,
                end_date: None,
                score: None,
                courses: vec![],
            });
        assert!(
            profile_review_inbox_apply_params(
                REVIEW_INBOX_ITEM_ID,
                "inbox",
                0,
                PROFILE_REVIEW_VERSION_ID,
                REVIEW_INBOX_REQUEST_ID,
                "add",
                oversized_candidate,
            )
            .is_err()
        );
        assert!(
            serde_json::from_value::<ProfileReviewInboxCandidate>(json!({
                "institution": "University of London",
                "study_type": "BSc",
                "area": "Mathematics",
                "start_date": "1830",
                "end_date": "1835",
                "score": null,
                "courses": []
            }))
            .is_err()
        );

        let _list_command = list_profile_review_items;
        let _transition_command = transition_profile_review_item;
        let _apply_command = apply_profile_review_item;
    }

    #[test]
    fn review_inbox_apply_candidates_are_exact_kind_specific_records() {
        let fixtures = [
            (
                ProfileReviewInboxCandidateKind::Projects,
                json!({
                    "name": "Analytical Engine",
                    "description": "A programmable mechanical computer.",
                    "url": "https://github.com/cvgnome/analytical-engine",
                    "start_date": "1834",
                    "end_date": null,
                    "highlights": ["Designed a general-purpose architecture."],
                    "keywords": ["Computing"]
                }),
            ),
            (
                ProfileReviewInboxCandidateKind::Education,
                json!({
                    "institution": "University of London",
                    "study_type": "BSc",
                    "area": "Mathematics",
                    "url": null,
                    "start_date": "1830",
                    "end_date": "1835",
                    "score": null,
                    "courses": []
                }),
            ),
            (
                ProfileReviewInboxCandidateKind::Skills,
                json!({
                    "name": "Programming",
                    "level": "Advanced",
                    "keywords": ["Python", "SQLite"]
                }),
            ),
        ];
        for (expected_kind, fixture) in fixtures {
            let candidate: ProfileReviewInboxCandidate =
                serde_json::from_value(fixture.clone()).unwrap();
            assert_eq!(candidate.kind(), expected_kind);
            assert_eq!(
                serde_json::to_value(candidate.validated_request().unwrap()).unwrap(),
                fixture
            );
        }

        for invalid in [
            json!({
                "name": "Programming",
                "level": "Advanced",
                "keywords": ["Python"],
                "evidence": "must not cross"
            }),
            json!({
                "name": "Project missing required nullable keys",
                "highlights": [],
                "keywords": []
            }),
            json!({
                "institution": "Education missing required nullable keys",
                "courses": []
            }),
        ] {
            assert!(serde_json::from_value::<ProfileReviewInboxCandidate>(invalid).is_err());
        }

        let candidate: ProfileReviewInboxCandidate = serde_json::from_value(json!({
            "name": "  Programming  ",
            "level": " Advanced ",
            "keywords": [" Python "]
        }))
        .unwrap();
        assert_eq!(
            serde_json::to_value(candidate.validated_request().unwrap()).unwrap(),
            json!({
                "name": "Programming",
                "level": "Advanced",
                "keywords": ["Python"]
            })
        );
    }

    #[test]
    fn review_inbox_page_is_strict_bounded_and_scope_consistent() {
        let fixture = profile_review_inbox_page_fixture();
        let page: ProfileReviewInboxPage = serde_json::from_value(fixture.clone()).unwrap();
        assert!(page.validated(ProfileReviewInboxScope::Inbox, 0).is_ok());

        let mut deferred = fixture.clone();
        deferred["scope"] = json!("deferred");
        deferred["total_items"] = json!(1);
        deferred["counts"] = json!({"inbox": 4, "deferred": 1, "history": 3});
        deferred["items"][0]["state"] = json!("deferred");
        let page: ProfileReviewInboxPage = serde_json::from_value(deferred).unwrap();
        assert!(page.validated(ProfileReviewInboxScope::Deferred, 0).is_ok());

        let mut previous_history = fixture.clone();
        previous_history["scope"] = json!("history");
        previous_history["counts"] = json!({"inbox": 4, "deferred": 2, "history": 1});
        previous_history["items"][0]["is_previous_import_set"] = json!(true);
        let page: ProfileReviewInboxPage = serde_json::from_value(previous_history).unwrap();
        assert!(page.validated(ProfileReviewInboxScope::History, 0).is_ok());

        let mut rejected_history = fixture.clone();
        rejected_history["scope"] = json!("history");
        rejected_history["counts"] = json!({"inbox": 4, "deferred": 2, "history": 1});
        rejected_history["items"][0]["state"] = json!("rejected");
        let page: ProfileReviewInboxPage = serde_json::from_value(rejected_history).unwrap();
        assert!(page.validated(ProfileReviewInboxScope::History, 0).is_ok());

        let mut applied_history = fixture.clone();
        applied_history["scope"] = json!("history");
        applied_history["counts"] = json!({"inbox": 4, "deferred": 2, "history": 1});
        applied_history["items"][0]["state"] = json!("applied");
        let page: ProfileReviewInboxPage = serde_json::from_value(applied_history).unwrap();
        assert!(page.validated(ProfileReviewInboxScope::History, 0).is_ok());

        let mut empty_without_profile = fixture.clone();
        empty_without_profile["current_profile_version_id"] = Value::Null;
        empty_without_profile["total_items"] = json!(0);
        empty_without_profile["counts"] = json!({"inbox": 0, "deferred": 0, "history": 0});
        empty_without_profile["items"] = json!([]);
        let page: ProfileReviewInboxPage = serde_json::from_value(empty_without_profile).unwrap();
        assert!(page.validated(ProfileReviewInboxScope::Inbox, 0).is_ok());

        let mut wrong_scope = fixture.clone();
        wrong_scope["items"][0]["state"] = json!("deferred");
        let page: ProfileReviewInboxPage = serde_json::from_value(wrong_scope).unwrap();
        assert!(page.validated(ProfileReviewInboxScope::Inbox, 0).is_err());

        let mut wrong_count = fixture.clone();
        wrong_count["counts"]["inbox"] = json!(2);
        let page: ProfileReviewInboxPage = serde_json::from_value(wrong_count).unwrap();
        assert!(page.validated(ProfileReviewInboxScope::Inbox, 0).is_err());

        let mut wrong_limit = fixture.clone();
        wrong_limit["limit"] = json!(9);
        let page: ProfileReviewInboxPage = serde_json::from_value(wrong_limit).unwrap();
        assert!(page.validated(ProfileReviewInboxScope::Inbox, 0).is_err());

        let mut wrong_next = fixture.clone();
        wrong_next["total_items"] = json!(2);
        wrong_next["counts"]["inbox"] = json!(2);
        wrong_next["next_offset"] = json!(2);
        let page: ProfileReviewInboxPage = serde_json::from_value(wrong_next).unwrap();
        assert!(page.validated(ProfileReviewInboxScope::Inbox, 0).is_err());

        let mut beyond_end = fixture.clone();
        beyond_end["offset"] = json!(20);
        beyond_end["items"] = json!([]);
        let page: ProfileReviewInboxPage = serde_json::from_value(beyond_end).unwrap();
        assert!(page.validated(ProfileReviewInboxScope::Inbox, 20).is_ok());

        let mut paging_boundary = fixture.clone();
        paging_boundary["offset"] = json!(PROFILE_REVIEW_INBOX_OFFSET_LIMIT);
        paging_boundary["total_items"] = json!(PROFILE_REVIEW_INBOX_OFFSET_LIMIT + 2);
        paging_boundary["counts"]["inbox"] = json!(PROFILE_REVIEW_INBOX_OFFSET_LIMIT + 2);
        let page: ProfileReviewInboxPage = serde_json::from_value(paging_boundary).unwrap();
        assert!(
            page.validated(
                ProfileReviewInboxScope::Inbox,
                PROFILE_REVIEW_INBOX_OFFSET_LIMIT,
            )
            .is_ok()
        );

        let mut duplicate_items = fixture;
        duplicate_items["total_items"] = json!(2);
        duplicate_items["counts"]["inbox"] = json!(2);
        duplicate_items["next_offset"] = Value::Null;
        let duplicate = duplicate_items["items"][0].clone();
        duplicate_items["items"]
            .as_array_mut()
            .unwrap()
            .push(duplicate);
        let page: ProfileReviewInboxPage = serde_json::from_value(duplicate_items).unwrap();
        assert!(page.validated(ProfileReviewInboxScope::Inbox, 0).is_err());
    }

    #[test]
    fn review_inbox_items_reject_private_unknown_and_inconsistent_fields() {
        let fixture = profile_review_inbox_page_fixture();

        let mut unknown_top_level = fixture.clone();
        unknown_top_level["retention_generation"] = json!(1);
        assert!(serde_json::from_value::<ProfileReviewInboxPage>(unknown_top_level).is_err());

        let mut invalid_current_profile = fixture.clone();
        invalid_current_profile["current_profile_version_id"] = json!("not-a-uuid");
        let page: ProfileReviewInboxPage = serde_json::from_value(invalid_current_profile).unwrap();
        assert!(page.validated(ProfileReviewInboxScope::Inbox, 0).is_err());

        let mut missing_current_profile = fixture.clone();
        missing_current_profile
            .as_object_mut()
            .unwrap()
            .remove("current_profile_version_id");
        assert!(serde_json::from_value::<ProfileReviewInboxPage>(missing_current_profile).is_err());

        let mut null_current_with_items = fixture.clone();
        null_current_with_items["current_profile_version_id"] = Value::Null;
        let page: ProfileReviewInboxPage = serde_json::from_value(null_current_with_items).unwrap();
        assert!(page.validated(ProfileReviewInboxScope::Inbox, 0).is_err());

        let mut unknown_evidence = fixture.clone();
        unknown_evidence["items"][0]["evidence"][0]["source_path"] =
            json!("/private/imports/resume.docx");
        assert!(serde_json::from_value::<ProfileReviewInboxPage>(unknown_evidence).is_err());

        let mut missing_nullable = fixture.clone();
        missing_nullable["items"][0]["evidence"][0]
            .as_object_mut()
            .unwrap()
            .remove("location_label");
        assert!(serde_json::from_value::<ProfileReviewInboxPage>(missing_nullable).is_err());

        let mut unknown_enum = fixture.clone();
        unknown_enum["items"][0]["candidate_kind"] = json!("work");
        assert!(serde_json::from_value::<ProfileReviewInboxPage>(unknown_enum).is_err());

        let mut wrong_kind_field = fixture.clone();
        wrong_kind_field["items"][0]["candidate_kind"] = json!("skills");
        let page: ProfileReviewInboxPage = serde_json::from_value(wrong_kind_field).unwrap();
        assert!(page.validated(ProfileReviewInboxScope::Inbox, 0).is_err());

        let mut private_candidate_field = fixture.clone();
        private_candidate_field["items"][0]["candidate"]["source_path"] =
            json!("/private/source.pdf");
        assert!(serde_json::from_value::<ProfileReviewInboxPage>(private_candidate_field).is_err());

        let mut non_normalized_candidate = fixture.clone();
        non_normalized_candidate["items"][0]["candidate"]["institution"] =
            json!(" University of London ");
        let page: ProfileReviewInboxPage =
            serde_json::from_value(non_normalized_candidate).unwrap();
        assert!(page.validated(ProfileReviewInboxScope::Inbox, 0).is_err());

        let mut wrong_field_order = fixture.clone();
        wrong_field_order["items"][0]["changed_fields"] = json!(["study_type", "institution"]);
        let page: ProfileReviewInboxPage = serde_json::from_value(wrong_field_order).unwrap();
        assert!(page.validated(ProfileReviewInboxScope::Inbox, 0).is_err());

        let mut invalid_id = fixture.clone();
        invalid_id["items"][0]["id"] = json!(REVIEW_INBOX_ITEM_ID.to_uppercase());
        let page: ProfileReviewInboxPage = serde_json::from_value(invalid_id).unwrap();
        assert!(page.validated(ProfileReviewInboxScope::Inbox, 0).is_err());

        let mut invalid_timestamp = fixture.clone();
        invalid_timestamp["items"][0]["created_at_ms"] = json!(0);
        let page: ProfileReviewInboxPage = serde_json::from_value(invalid_timestamp).unwrap();
        assert!(page.validated(ProfileReviewInboxScope::Inbox, 0).is_err());

        let mut unicode_title = fixture.clone();
        unicode_title["items"][0]["title"] =
            json!("📄".repeat(PROFILE_REVIEW_ITEM_TITLE_LIMIT_CHARS));
        let page: ProfileReviewInboxPage = serde_json::from_value(unicode_title).unwrap();
        assert!(page.validated(ProfileReviewInboxScope::Inbox, 0).is_ok());

        let mut long_excerpt = fixture;
        long_excerpt["items"][0]["evidence"][0]["excerpt"] =
            json!("x".repeat(PROFILE_REVIEW_INBOX_EVIDENCE_EXCERPT_LIMIT_CHARS + 1));
        let page: ProfileReviewInboxPage = serde_json::from_value(long_excerpt).unwrap();
        assert!(page.validated(ProfileReviewInboxScope::Inbox, 0).is_err());
    }

    #[test]
    fn review_inbox_transition_receipt_is_strict_and_fully_correlated() {
        let fixture = profile_review_inbox_transition_fixture();
        let receipt: ProfileReviewInboxTransitionReceipt =
            serde_json::from_value(fixture.clone()).unwrap();
        assert!(
            receipt
                .validated(
                    REVIEW_INBOX_ITEM_ID,
                    ProfileReviewInboxState::Inbox,
                    0,
                    REVIEW_INBOX_REQUEST_ID,
                    ProfileReviewInboxAction::Defer,
                )
                .is_ok()
        );

        let mut exact_retry = fixture.clone();
        exact_retry["created"] = json!(false);
        let receipt: ProfileReviewInboxTransitionReceipt =
            serde_json::from_value(exact_retry).unwrap();
        assert!(
            receipt
                .validated(
                    REVIEW_INBOX_ITEM_ID,
                    ProfileReviewInboxState::Inbox,
                    0,
                    REVIEW_INBOX_REQUEST_ID,
                    ProfileReviewInboxAction::Defer,
                )
                .is_ok()
        );

        for (field, value) in [
            ("request_id", json!(PROFILE_UPDATE_REQUEST_ID)),
            ("review_item_id", json!(PROFILE_VERSION_ONE_ID)),
            ("action", json!("reject")),
            ("previous_state", json!("deferred")),
            ("state", json!("rejected")),
            ("state_revision", json!(2)),
            ("created_at_ms", json!(0)),
        ] {
            let mut invalid = fixture.clone();
            invalid[field] = value;
            let receipt: ProfileReviewInboxTransitionReceipt =
                serde_json::from_value(invalid).unwrap();
            assert!(
                receipt
                    .validated(
                        REVIEW_INBOX_ITEM_ID,
                        ProfileReviewInboxState::Inbox,
                        0,
                        REVIEW_INBOX_REQUEST_ID,
                        ProfileReviewInboxAction::Defer,
                    )
                    .is_err(),
                "field {field} should fail correlation"
            );
        }

        let mut unknown = fixture;
        unknown["candidate"] = json!({"private": true});
        assert!(serde_json::from_value::<ProfileReviewInboxTransitionReceipt>(unknown).is_err());
    }

    #[test]
    fn review_inbox_apply_receipt_is_strict_bounded_and_fully_correlated() {
        let candidate = serde_json::from_value::<ProfileReviewInboxCandidate>(
            profile_review_inbox_item_fixture(REVIEW_INBOX_ITEM_ID, "inbox", false)["candidate"]
                .clone(),
        )
        .unwrap();
        for created in [true, false] {
            let receipt: ProfileReviewInboxApplyReceipt =
                serde_json::from_value(profile_review_inbox_apply_fixture(created)).unwrap();
            assert!(
                receipt
                    .validated(
                        REVIEW_INBOX_ITEM_ID,
                        0,
                        PROFILE_REVIEW_VERSION_ID,
                        REVIEW_INBOX_REQUEST_ID,
                        &candidate,
                        ProfileReviewInboxOperationKind::Add,
                    )
                    .is_ok()
            );
        }
        let receipt: ProfileReviewInboxApplyReceipt =
            serde_json::from_value(profile_review_inbox_apply_fixture(true)).unwrap();
        assert!(
            receipt
                .validated(
                    REVIEW_INBOX_ITEM_ID,
                    0,
                    PROFILE_REVIEW_VERSION_ID,
                    REVIEW_INBOX_REQUEST_ID,
                    &candidate,
                    ProfileReviewInboxOperationKind::Update,
                )
                .is_err()
        );

        for (field, value) in [
            ("request_id", json!(PROFILE_UPDATE_REQUEST_ID)),
            ("review_item_id", json!(PROFILE_VERSION_ONE_ID)),
            ("previous_state", json!("deferred")),
            ("state", json!("rejected")),
            ("state_revision", json!(2)),
            ("profile_version_id", json!(PROFILE_REVIEW_VERSION_ID)),
            ("parent_profile_version_id", json!(PROFILE_VERSION_ONE_ID)),
            ("version_number", json!(0)),
            ("version_number", json!(JAVASCRIPT_MAX_SAFE_INTEGER + 1)),
            ("candidate_kind", json!("projects")),
            ("entry_index", json!(PROFILE_SECTION_ENTRY_LIMIT)),
            ("changed_fields", json!([])),
            ("created_at_ms", json!(0)),
        ] {
            let mut invalid = profile_review_inbox_apply_fixture(true);
            invalid[field] = value;
            let receipt: ProfileReviewInboxApplyReceipt = serde_json::from_value(invalid).unwrap();
            assert!(
                receipt
                    .validated(
                        REVIEW_INBOX_ITEM_ID,
                        0,
                        PROFILE_REVIEW_VERSION_ID,
                        REVIEW_INBOX_REQUEST_ID,
                        &candidate,
                        ProfileReviewInboxOperationKind::Add,
                    )
                    .is_err(),
                "field {field} should fail correlation"
            );
        }

        let mut wrong_field_kind = profile_review_inbox_apply_fixture(true);
        wrong_field_kind["changed_fields"] = json!(["name"]);
        let receipt: ProfileReviewInboxApplyReceipt =
            serde_json::from_value(wrong_field_kind).unwrap();
        assert!(
            receipt
                .validated(
                    REVIEW_INBOX_ITEM_ID,
                    0,
                    PROFILE_REVIEW_VERSION_ID,
                    REVIEW_INBOX_REQUEST_ID,
                    &candidate,
                    ProfileReviewInboxOperationKind::Add,
                )
                .is_err()
        );

        let mut wrong_field_order = profile_review_inbox_apply_fixture(true);
        wrong_field_order["changed_fields"] = json!(["area", "institution"]);
        let receipt: ProfileReviewInboxApplyReceipt =
            serde_json::from_value(wrong_field_order).unwrap();
        assert!(
            receipt
                .validated(
                    REVIEW_INBOX_ITEM_ID,
                    0,
                    PROFILE_REVIEW_VERSION_ID,
                    REVIEW_INBOX_REQUEST_ID,
                    &candidate,
                    ProfileReviewInboxOperationKind::Add,
                )
                .is_err()
        );

        let mut unknown = profile_review_inbox_apply_fixture(true);
        unknown["profile_name"] = json!("must not cross the apply bridge");
        assert!(serde_json::from_value::<ProfileReviewInboxApplyReceipt>(unknown).is_err());

        let mut unknown_operation = profile_review_inbox_apply_fixture(true);
        unknown_operation["operation_kind"] = json!("remove");
        assert!(
            serde_json::from_value::<ProfileReviewInboxApplyReceipt>(unknown_operation).is_err()
        );
    }

    #[test]
    fn review_inbox_apply_invalidates_status_after_every_engine_outcome() {
        tauri::async_runtime::block_on(async {
            let runtime = EngineRuntime::default();
            let request = profile_review_inbox_apply_request_fixture();

            seed_stale_status_cache(&runtime).await;
            assert!(
                finalize_profile_review_apply_attempt(
                    &runtime,
                    Err("The desktop could not confirm the engine response".to_string()),
                    &request,
                )
                .await
                .is_err()
            );
            assert!(runtime.cached_status.lock().await.is_none());

            seed_stale_status_cache(&runtime).await;
            let mut malformed = profile_review_inbox_apply_fixture(true);
            malformed["state_revision"] = json!(2);
            let malformed: ProfileReviewInboxApplyReceipt =
                serde_json::from_value(malformed).unwrap();
            assert!(
                finalize_profile_review_apply_attempt(&runtime, Ok(malformed), &request)
                    .await
                    .is_err()
            );
            assert!(runtime.cached_status.lock().await.is_none());

            seed_stale_status_cache(&runtime).await;
            let valid: ProfileReviewInboxApplyReceipt =
                serde_json::from_value(profile_review_inbox_apply_fixture(true)).unwrap();
            assert!(
                finalize_profile_review_apply_attempt(&runtime, Ok(valid), &request)
                    .await
                    .is_ok()
            );
            assert!(runtime.cached_status.lock().await.is_none());
        });
    }

    #[test]
    fn review_inbox_transition_matrix_and_errors_are_safe() {
        for (state, action, result) in [
            ("inbox", "defer", ProfileReviewInboxState::Deferred),
            ("inbox", "reject", ProfileReviewInboxState::Rejected),
            ("deferred", "reject", ProfileReviewInboxState::Rejected),
            ("deferred", "reopen", ProfileReviewInboxState::Inbox),
            ("rejected", "reopen", ProfileReviewInboxState::Inbox),
        ] {
            assert_eq!(
                ProfileReviewInboxAction::parse(action)
                    .unwrap()
                    .resulting_state(ProfileReviewInboxState::parse(state).unwrap())
                    .unwrap(),
                result
            );
        }
        for (state, action) in [
            ("inbox", "reopen"),
            ("deferred", "defer"),
            ("rejected", "defer"),
            ("rejected", "reject"),
        ] {
            assert!(
                ProfileReviewInboxAction::parse(action)
                    .unwrap()
                    .resulting_state(ProfileReviewInboxState::parse(state).unwrap())
                    .is_err()
            );
        }

        let private_marker = "/private/imports/secret-resume.pdf";
        for (error, expected_code) in [
            (
                format!("review_item_not_found: {private_marker}"),
                "review_item_not_found",
            ),
            (
                format!("review_item_stale_generation: {private_marker}"),
                "review_item_stale_generation",
            ),
            (
                format!("state_conflict: {private_marker}"),
                "review_item_state_conflict",
            ),
            (
                format!("unexpected: {private_marker}"),
                "review_inbox_error",
            ),
            (
                format!("review_item_apply_request_conflict: {private_marker}"),
                "review_item_request_conflict",
            ),
            (
                format!("review_item_profile_conflict: {private_marker}"),
                "review_item_profile_conflict",
            ),
            (
                format!("review_item_evidence_invalid: {private_marker}"),
                "review_item_evidence_invalid",
            ),
            (
                format!("review_item_apply_conflict: {private_marker}"),
                "review_item_apply_conflict",
            ),
            (
                format!("review_item_apply_noop: {private_marker}"),
                "review_item_apply_noop",
            ),
            (
                format!("profile_missing: {private_marker}"),
                "profile_missing",
            ),
            (
                format!("profile_version_limit: {private_marker}"),
                "profile_version_limit",
            ),
        ] {
            let safe = safe_profile_review_inbox_error(error);
            assert!(safe.starts_with(expected_code));
            assert!(!safe.contains(private_marker));
        }
    }

    #[test]
    fn profile_review_summary_receipt_is_strict_and_consistent() {
        let fixture = profile_review_summary_fixture();
        let summary: ProfileReviewSummary = serde_json::from_value(fixture.clone()).unwrap();
        let validated = summary.validated().unwrap();
        assert_eq!(validated.sections.len(), PROFILE_REVIEW_SECTION_COUNT);
        assert_eq!(validated.sections[0].key, ProfileReviewSectionKey::Work);

        let mut unknown_top_level = fixture.clone();
        unknown_top_level["canonical_profile"] = json!({"private": "must not cross"});
        assert!(serde_json::from_value::<ProfileReviewSummary>(unknown_top_level).is_err());

        let mut unknown_nested = fixture.clone();
        unknown_nested["contacts"][0]["source_path"] = json!("/private/source.pdf");
        assert!(serde_json::from_value::<ProfileReviewSummary>(unknown_nested).is_err());

        let mut missing_nullable = fixture.clone();
        missing_nullable.as_object_mut().unwrap().remove("headline");
        assert!(serde_json::from_value::<ProfileReviewSummary>(missing_nullable).is_err());

        let mut invalid_source = fixture.clone();
        invalid_source["source_kind"] = json!("canonical_json");
        assert!(serde_json::from_value::<ProfileReviewSummary>(invalid_source).is_err());

        let mut invalid_contact_kind = fixture.clone();
        invalid_contact_kind["contacts"][0]["kind"] = json!("social");
        assert!(serde_json::from_value::<ProfileReviewSummary>(invalid_contact_kind).is_err());

        let mut invalid_section_key = fixture.clone();
        invalid_section_key["sections"][0]["key"] = json!("employment");
        assert!(serde_json::from_value::<ProfileReviewSummary>(invalid_section_key).is_err());

        for (field, value) in [
            ("profile_version_id", json!("not-a-uuid")),
            ("version_number", json!(0)),
            ("created_at_ms", json!(JAVASCRIPT_MAX_SAFE_INTEGER + 1)),
        ] {
            let mut invalid = fixture.clone();
            invalid[field] = value;
            let summary: ProfileReviewSummary = serde_json::from_value(invalid).unwrap();
            assert!(summary.validated().is_err(), "field {field} should fail");
        }

        let mut wrong_contact_label = fixture.clone();
        wrong_contact_label["contacts"][0]["label"] = json!("E-mail address");
        let summary: ProfileReviewSummary = serde_json::from_value(wrong_contact_label).unwrap();
        assert!(summary.validated().is_err());

        let mut wrong_section_order = fixture.clone();
        wrong_section_order["sections"]
            .as_array_mut()
            .unwrap()
            .swap(0, 1);
        let summary: ProfileReviewSummary = serde_json::from_value(wrong_section_order).unwrap();
        assert!(summary.validated().is_err());

        let mut wrong_section_label = fixture.clone();
        wrong_section_label["sections"][0]["label"] = json!("Employment");
        let summary: ProfileReviewSummary = serde_json::from_value(wrong_section_label).unwrap();
        assert!(summary.validated().is_err());

        let mut impossible_count = fixture;
        impossible_count["sections"][0]["count"] = json!(PROFILE_REVIEW_SECTION_ITEM_LIMIT + 1);
        let summary: ProfileReviewSummary = serde_json::from_value(impossible_count).unwrap();
        assert!(summary.validated().is_err());
    }

    #[test]
    fn profile_review_page_receipt_binds_identity_and_pagination() {
        let fixture = profile_review_page_fixture(0, 12, PROFILE_REVIEW_PAGE_LIMIT);
        let page: ProfileReviewSectionPage = serde_json::from_value(fixture.clone()).unwrap();
        assert!(
            page.validated(PROFILE_REVIEW_VERSION_ID, ProfileReviewSectionKey::Work, 0)
                .is_ok()
        );

        let last_page: ProfileReviewSectionPage =
            serde_json::from_value(profile_review_page_fixture(10, 12, 2)).unwrap();
        assert!(
            last_page
                .validated(PROFILE_REVIEW_VERSION_ID, ProfileReviewSectionKey::Work, 10)
                .is_ok()
        );
        let beyond_end: ProfileReviewSectionPage =
            serde_json::from_value(profile_review_page_fixture(20, 12, 0)).unwrap();
        assert!(
            beyond_end
                .validated(PROFILE_REVIEW_VERSION_ID, ProfileReviewSectionKey::Work, 20)
                .is_ok()
        );

        let valid_page =
            || serde_json::from_value::<ProfileReviewSectionPage>(fixture.clone()).unwrap();
        assert!(
            valid_page()
                .validated(
                    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    ProfileReviewSectionKey::Work,
                    0,
                )
                .is_err()
        );
        assert!(
            valid_page()
                .validated(
                    PROFILE_REVIEW_VERSION_ID,
                    ProfileReviewSectionKey::Education,
                    0,
                )
                .is_err()
        );
        assert!(
            valid_page()
                .validated(PROFILE_REVIEW_VERSION_ID, ProfileReviewSectionKey::Work, 10)
                .is_err()
        );

        for (field, value) in [
            ("label", json!("Employment")),
            ("limit", json!(9)),
            ("next_offset", json!(9)),
            ("total_items", json!(PROFILE_REVIEW_SECTION_ITEM_LIMIT + 1)),
        ] {
            let mut invalid = fixture.clone();
            invalid[field] = value;
            let page: ProfileReviewSectionPage = serde_json::from_value(invalid).unwrap();
            assert!(
                page.validated(PROFILE_REVIEW_VERSION_ID, ProfileReviewSectionKey::Work, 0)
                    .is_err(),
                "field {field} should fail"
            );
        }

        let mut wrong_ordinal = fixture.clone();
        wrong_ordinal["items"][0]["ordinal"] = json!(1);
        let page: ProfileReviewSectionPage = serde_json::from_value(wrong_ordinal).unwrap();
        assert!(
            page.validated(PROFILE_REVIEW_VERSION_ID, ProfileReviewSectionKey::Work, 0)
                .is_err()
        );

        let size_bounded_nonterminal: ProfileReviewSectionPage =
            serde_json::from_value(profile_review_page_fixture(0, 12, 9)).unwrap();
        assert!(
            size_bounded_nonterminal
                .validated(PROFILE_REVIEW_VERSION_ID, ProfileReviewSectionKey::Work, 0)
                .is_ok()
        );
        let empty_nonterminal: ProfileReviewSectionPage =
            serde_json::from_value(profile_review_page_fixture(0, 12, 0)).unwrap();
        assert!(
            empty_nonterminal
                .validated(PROFILE_REVIEW_VERSION_ID, ProfileReviewSectionKey::Work, 0)
                .is_err()
        );
        let too_many_items: ProfileReviewSectionPage = serde_json::from_value(
            profile_review_page_fixture(0, (PROFILE_REVIEW_PAGE_LIMIT + 1) as u64, 11),
        )
        .unwrap();
        assert!(
            too_many_items
                .validated(PROFILE_REVIEW_VERSION_ID, ProfileReviewSectionKey::Work, 0)
                .is_err()
        );

        let mut missing_nullable = fixture.clone();
        missing_nullable["items"][0]
            .as_object_mut()
            .unwrap()
            .remove("subtitle");
        assert!(serde_json::from_value::<ProfileReviewSectionPage>(missing_nullable).is_err());

        let mut unknown_nested = fixture;
        unknown_nested["items"][0]["details"][0]["private_note"] = json!("secret");
        assert!(serde_json::from_value::<ProfileReviewSectionPage>(unknown_nested).is_err());
    }

    #[test]
    fn profile_review_text_and_payload_caps_count_utf8_bytes() {
        let two_four_byte_characters = "📄📄";
        assert_eq!(two_four_byte_characters.chars().count(), 2);
        assert_eq!(two_four_byte_characters.len(), 8);
        assert!(validate_profile_review_text(two_four_byte_characters, "test", 2, 8).is_ok());
        assert!(validate_profile_review_text(two_four_byte_characters, "test", 2, 7).is_err());

        let mut unicode_summary = profile_review_summary_fixture();
        unicode_summary["name"] = json!("📄".repeat(PROFILE_REVIEW_NAME_LIMIT_CHARS));
        let summary: ProfileReviewSummary = serde_json::from_value(unicode_summary).unwrap();
        assert!(summary.validated().is_ok());

        let mut too_many_characters = profile_review_summary_fixture();
        too_many_characters["name"] = json!("📄".repeat(PROFILE_REVIEW_NAME_LIMIT_CHARS + 1));
        let summary: ProfileReviewSummary = serde_json::from_value(too_many_characters).unwrap();
        assert!(summary.validated().is_err());

        let mut engine_bounded_summary = profile_review_summary_fixture();
        engine_bounded_summary["name"] = json!("📄".repeat(PROFILE_REVIEW_NAME_LIMIT_CHARS));
        engine_bounded_summary["headline"] =
            json!("📄".repeat(PROFILE_REVIEW_HEADLINE_LIMIT_CHARS));
        engine_bounded_summary["summary"] =
            json!("📄".repeat(PROFILE_REVIEW_SUMMARY_TEXT_LIMIT_CHARS));
        let mut contacts = vec![
            json!({"kind": "email", "label": "Email", "value": "📄".repeat(PROFILE_REVIEW_CONTACT_VALUE_LIMIT_CHARS)}),
            json!({"kind": "phone", "label": "Phone", "value": "📄".repeat(PROFILE_REVIEW_CONTACT_VALUE_LIMIT_CHARS)}),
            json!({"kind": "location", "label": "Location", "value": "📄".repeat(PROFILE_REVIEW_CONTACT_VALUE_LIMIT_CHARS)}),
            json!({"kind": "website", "label": "Website", "value": "📄".repeat(PROFILE_REVIEW_CONTACT_VALUE_LIMIT_CHARS)}),
        ];
        contacts.extend((0..8).map(|_| {
            json!({
                "kind": "profile",
                "label": "📄".repeat(PROFILE_REVIEW_CONTACT_LABEL_LIMIT_CHARS),
                "value": "📄".repeat(PROFILE_REVIEW_CONTACT_VALUE_LIMIT_CHARS),
            })
        }));
        engine_bounded_summary["contacts"] = json!(contacts);
        while serde_json::to_vec(&engine_bounded_summary).unwrap().len() > 30 * 1024 {
            engine_bounded_summary["contacts"]
                .as_array_mut()
                .unwrap()
                .pop();
        }
        assert!(engine_bounded_summary["contacts"].as_array().unwrap().len() < 12);
        let summary: ProfileReviewSummary = serde_json::from_value(engine_bounded_summary).unwrap();
        assert!(summary.validated().is_ok());

        let mut maximum_highlight = profile_review_page_fixture(0, 1, 1);
        maximum_highlight["items"][0]["highlights"] =
            json!(["📄".repeat(PROFILE_REVIEW_HIGHLIGHT_LIMIT_CHARS)]);
        let page: ProfileReviewSectionPage = serde_json::from_value(maximum_highlight).unwrap();
        assert!(
            page.validated(PROFILE_REVIEW_VERSION_ID, ProfileReviewSectionKey::Work, 0)
                .is_ok()
        );

        let mut oversized_page = profile_review_page_fixture(0, 10, 10);
        for item in oversized_page["items"].as_array_mut().unwrap() {
            item["title"] = json!("📄".repeat(PROFILE_REVIEW_ITEM_TITLE_LIMIT_CHARS));
            item["subtitle"] = json!("📄".repeat(PROFILE_REVIEW_ITEM_SUBTITLE_LIMIT_CHARS));
            item["date_range"] = json!("📄".repeat(PROFILE_REVIEW_ITEM_DATE_LIMIT_CHARS));
            item["location"] = json!("📄".repeat(PROFILE_REVIEW_ITEM_LOCATION_LIMIT_CHARS));
            item["summary"] = json!("📄".repeat(PROFILE_REVIEW_SUMMARY_TEXT_LIMIT_CHARS));
            item["highlights"] = json!(vec![
                "📄".repeat(PROFILE_REVIEW_HIGHLIGHT_LIMIT_CHARS);
                PROFILE_REVIEW_HIGHLIGHT_LIMIT
            ]);
            item["tags"] = json!(vec![
                "📄".repeat(PROFILE_REVIEW_TAG_LIMIT_CHARS);
                PROFILE_REVIEW_TAG_LIMIT
            ]);
            item["details"] = json!(
                (0..PROFILE_REVIEW_DETAIL_LIMIT)
                    .map(|_| json!({
                        "label": "📄".repeat(PROFILE_REVIEW_DETAIL_LABEL_LIMIT_CHARS),
                        "value": "📄".repeat(PROFILE_REVIEW_DETAIL_VALUE_LIMIT_CHARS),
                    }))
                    .collect::<Vec<_>>()
            );
        }
        let page: ProfileReviewSectionPage = serde_json::from_value(oversized_page).unwrap();
        assert!(
            page.validated(PROFILE_REVIEW_VERSION_ID, ProfileReviewSectionKey::Work, 0)
                .is_err()
        );
    }

    fn opportunity_detail_fixture() -> Value {
        json!({
            "opportunity": {
                "id": "11111111-1111-4111-8111-111111111111",
                "title": "Senior Product Manager",
                "company": "Northstar",
                "location": "Remote",
                "source_url": "https://example.com/jobs/123",
                "apply_url": null,
                "status": "ready",
                "notes": "Follow up with the hiring manager.",
                "tracker_stage": "to_apply",
                "tracker_updated_at_ms": 1_800_000_000_003_u64,
                "created_at_ms": 1_800_000_000_000_u64,
                "updated_at_ms": 1_800_000_000_001_u64
            },
            "snapshot": {
                "id": "22222222-2222-4222-8222-222222222222",
                "checksum_sha256": "a".repeat(64),
                "captured_at_ms": 1_800_000_000_000_u64,
                "description": "Lead product discovery and cross-functional delivery."
            },
            "match": {
                "id": "33333333-3333-4333-8333-333333333333",
                "contract": "deterministic-local-v1",
                "score": 84,
                "signal": "strong",
                "matched_terms": ["product discovery", "cross-functional"],
                "terms_to_review": ["healthcare"],
                "evidence": [{
                    "term": "product discovery",
                    "profile_path": "/work/0/highlights/0",
                    "snippet": "Led product discovery across three launches."
                }],
                "created_at_ms": 1_800_000_000_002_u64
            },
            "profile_version": {
                "id": "44444444-4444-4444-8444-444444444444",
                "version_number": 2,
                "checksum_sha256": "b".repeat(64)
            }
        })
    }

    fn opportunity_summary_fixture() -> Value {
        let detail = opportunity_detail_fixture();
        json!({
            "id": detail["opportunity"]["id"],
            "title": detail["opportunity"]["title"],
            "company": detail["opportunity"]["company"],
            "location": detail["opportunity"]["location"],
            "status": detail["opportunity"]["status"],
            "tracker_stage": detail["opportunity"]["tracker_stage"],
            "tracker_updated_at_ms": detail["opportunity"]["tracker_updated_at_ms"],
            "created_at_ms": detail["opportunity"]["created_at_ms"],
            "updated_at_ms": detail["opportunity"]["updated_at_ms"],
            "snapshot": {
                "id": detail["snapshot"]["id"],
                "checksum_sha256": detail["snapshot"]["checksum_sha256"],
                "captured_at_ms": detail["snapshot"]["captured_at_ms"]
            },
            "match": {
                "id": detail["match"]["id"],
                "contract": detail["match"]["contract"],
                "score": detail["match"]["score"],
                "signal": detail["match"]["signal"],
                "created_at_ms": detail["match"]["created_at_ms"]
            },
            "profile_version": detail["profile_version"]
        })
    }

    fn retained_source_list_fixture() -> Value {
        json!({
            "scope": "active",
            "current_generation": 2,
            "offset": 0,
            "limit": RETAINED_SOURCE_LIST_LIMIT,
            "total_entries": 1,
            "total_unique_files": 1,
            "total_imports": 1,
            "items": [{
                "import_id": "55555555-5555-4555-8555-555555555555",
                "ordinal": 0,
                "display_name": "career evidence.pdf",
                "source_format": "pdf",
                "source_kind": "evidence",
                "byte_size": 4096,
                "extraction_status": "parsed",
                "issue_code": null,
                "retention_generation": 2,
                "retention_status": "active",
                "committed_at_ms": 1_800_000_000_000_u64,
                "profile_version_number": 3,
                "import_mode": "source_set",
                "import_file_count": 1,
                "content_reference_count": 2
            }]
        })
    }

    #[test]
    fn tauri_config_uses_cvgnome_identifier() {
        let config: Value = serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        assert_eq!(config["productName"], "CVGnome");
        assert_eq!(config["version"], env!("CARGO_PKG_VERSION"));
        assert_eq!(config["identifier"], "com.cvgnome.desktop");
    }

    #[test]
    fn prepare_data_dir_never_moves_a_sibling_identity_directory() {
        let root = tempfile::tempdir().unwrap();
        let data_dir = root.path().join("com.cvgnome.desktop");
        let sibling = root.path().join("com.cvgnome.desktop");
        fs::create_dir(&sibling).unwrap();
        fs::write(sibling.join("must-remain"), b"separate-vault").unwrap();

        prepare_data_dir(&data_dir).unwrap();

        assert!(data_dir.is_dir());
        assert_eq!(
            fs::read(sibling.join("must-remain")).unwrap(),
            b"separate-vault"
        );
    }

    #[cfg(unix)]
    #[test]
    fn prepare_data_dir_rejects_a_symlink_without_touching_its_target() {
        use std::os::unix::fs::symlink;

        let root = tempfile::tempdir().unwrap();
        let outside = tempfile::tempdir().unwrap();
        let marker = outside.path().join("must-remain");
        fs::write(&marker, b"outside").unwrap();
        let data_dir = root.path().join("com.cvgnome.desktop");
        symlink(outside.path(), &data_dir).unwrap();

        assert!(prepare_data_dir(&data_dir).is_err());
        assert!(
            fs::symlink_metadata(&data_dir)
                .unwrap()
                .file_type()
                .is_symlink()
        );
        assert_eq!(fs::read(&marker).unwrap(), b"outside");
    }

    #[test]
    fn managed_paths_are_strictly_scoped() {
        assert_eq!(
            validate_managed_relative_path("artifacts/resumes/example.pdf").unwrap(),
            PathBuf::from("artifacts/resumes/example.pdf")
        );
        for unsafe_path in [
            "../example.pdf",
            "/tmp/example.pdf",
            "artifacts/../example.pdf",
            "other/example.pdf",
        ] {
            assert!(validate_managed_relative_path(unsafe_path).is_err());
        }
    }

    #[test]
    fn baseline_export_contract_accepts_only_the_exact_profile_artifact() {
        let wire: EngineBaselineResumeArtifact =
            serde_json::from_value(baseline_artifact_fixture()).unwrap();
        let artifact = wire
            .validated(Some(PROFILE_REVIEW_VERSION_ID), "pdf")
            .unwrap();
        assert_eq!(artifact.artifact_id, BASELINE_ARTIFACT_ID);
        assert_eq!(artifact.format, "pdf");
        assert_eq!(artifact.byte_size, 4_096);

        let latest_wire: EngineBaselineResumeArtifact =
            serde_json::from_value(baseline_artifact_fixture()).unwrap();
        assert!(latest_wire.validated(None, "pdf").is_ok());

        let mut unexpected = baseline_artifact_fixture();
        unexpected["private_engine_detail"] = json!("must not cross the bridge");
        assert!(serde_json::from_value::<EngineBaselineResumeArtifact>(unexpected).is_err());

        for missing in ["profile_version_id", "media_type"] {
            let mut value = baseline_artifact_fixture();
            value.as_object_mut().unwrap().remove(missing);
            assert!(serde_json::from_value::<EngineBaselineResumeArtifact>(value).is_err());
        }

        for (field, value) in [
            ("profile_version_id", json!("not-a-uuid")),
            (
                "profile_version_id",
                json!("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
            ),
            ("artifact_id", json!("not-a-uuid")),
            ("format", json!("docx")),
            ("media_type", json!("text/plain")),
            ("checksum_sha256", json!("E".repeat(64))),
            ("byte_size", json!(0)),
            (
                "byte_size",
                json!(MANAGED_ARTIFACT_LIMIT_BYTES.saturating_add(1)),
            ),
            ("relative_path", json!("../outside.pdf")),
            (
                "relative_path",
                json!("artifacts/resumes/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb.pdf"),
            ),
            ("suggested_filename", json!("../../private.pdf")),
        ] {
            let mut invalid = baseline_artifact_fixture();
            invalid[field] = value;
            let wire: EngineBaselineResumeArtifact = serde_json::from_value(invalid).unwrap();
            assert!(
                wire.validated(Some(PROFILE_REVIEW_VERSION_ID), "pdf")
                    .is_err(),
                "{field} should be rejected"
            );
        }
    }

    #[test]
    fn baseline_export_command_args_preserve_optional_profile_pinning() {
        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase", deny_unknown_fields)]
        struct CommandArguments {
            format: String,
            #[serde(default)]
            profile_version_id: Option<String>,
        }

        let pinned: CommandArguments = serde_json::from_value(json!({
            "format": "PDF",
            "profileVersionId": PROFILE_REVIEW_VERSION_ID,
        }))
        .unwrap();
        let format = normalized_resume_format(&pinned.format).unwrap();
        let profile_version_id = pinned
            .profile_version_id
            .as_deref()
            .map(validated_profile_review_version_id)
            .transpose()
            .unwrap();
        assert_eq!(
            resume_export_params(&format, profile_version_id.as_deref()),
            json!({
                "format": "pdf",
                "profile_version_id": PROFILE_REVIEW_VERSION_ID,
            })
        );

        let latest: CommandArguments = serde_json::from_value(json!({"format": "docx"})).unwrap();
        assert_eq!(
            resume_export_params(
                &normalized_resume_format(&latest.format).unwrap(),
                latest.profile_version_id.as_deref(),
            ),
            json!({"format": "docx"})
        );
        assert!(
            serde_json::from_value::<CommandArguments>(json!({
                "format": "pdf",
                "profile_version_id": PROFILE_REVIEW_VERSION_ID,
            }))
            .is_err()
        );
        assert!(
            serde_json::from_value::<CommandArguments>(json!({
                "format": "pdf",
                "profileVersionId": PROFILE_REVIEW_VERSION_ID,
                "extra": true,
            }))
            .is_err()
        );
        assert!(validated_profile_review_version_id("not-a-uuid").is_err());
        let _command = export_resume;
    }

    #[test]
    fn tailored_export_contract_accepts_only_the_exact_pinned_artifact() {
        let wire: EngineTailoredResumeArtifact =
            serde_json::from_value(tailored_artifact_fixture()).unwrap();
        let artifact = wire.validated(TAILORED_DRAFT_ID, "pdf").unwrap();
        assert_eq!(artifact.artifact_id, TAILORED_ARTIFACT_ID);
        assert_eq!(artifact.format, "pdf");
        assert_eq!(artifact.byte_size, 4_096);

        let mut unexpected = tailored_artifact_fixture();
        unexpected["private_engine_detail"] = json!("must not cross the bridge");
        assert!(serde_json::from_value::<EngineTailoredResumeArtifact>(unexpected).is_err());

        let mismatched_draft: EngineTailoredResumeArtifact =
            serde_json::from_value(tailored_artifact_fixture()).unwrap();
        assert!(
            mismatched_draft
                .validated("55555555-5555-4555-8555-555555555555", "pdf")
                .is_err()
        );

        let mismatched_format: EngineTailoredResumeArtifact =
            serde_json::from_value(tailored_artifact_fixture()).unwrap();
        assert!(
            mismatched_format
                .validated(TAILORED_DRAFT_ID, "docx")
                .is_err()
        );
    }

    #[test]
    fn tailored_export_contract_rejects_invalid_lineage_media_and_file_identity() {
        for (field, value) in [
            ("opportunity_id", json!("not-a-uuid")),
            ("profile_version_id", json!("not-a-uuid")),
            ("artifact_id", json!("not-a-uuid")),
            ("media_type", json!("text/plain")),
            ("checksum_sha256", json!("A".repeat(64))),
            ("byte_size", json!(0)),
            (
                "byte_size",
                json!(MANAGED_ARTIFACT_LIMIT_BYTES.saturating_add(1)),
            ),
            ("relative_path", json!("../outside.pdf")),
            (
                "relative_path",
                json!("artifacts/resumes/55555555-5555-4555-8555-555555555555.pdf"),
            ),
            ("suggested_filename", json!("../../private.pdf")),
        ] {
            let mut invalid = tailored_artifact_fixture();
            invalid[field] = value;
            let wire: EngineTailoredResumeArtifact = serde_json::from_value(invalid).unwrap();
            assert!(
                wire.validated(TAILORED_DRAFT_ID, "pdf").is_err(),
                "{field} should be rejected"
            );
        }
    }

    #[test]
    fn tailored_export_params_normalize_camel_case_fixture_to_exact_engine_contract() {
        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase")]
        struct CommandArguments {
            draft_id: String,
            format: String,
        }

        let arguments: CommandArguments = serde_json::from_value(json!({
            "draftId": TAILORED_DRAFT_ID,
            "format": "PDF"
        }))
        .unwrap();
        let draft_id = validated_tailored_draft_id(&arguments.draft_id).unwrap();
        let format = normalized_resume_format(&arguments.format).unwrap();
        assert_eq!(
            tailored_export_params(&draft_id, &format),
            json!({"draft_id": TAILORED_DRAFT_ID, "format": "pdf"})
        );
        assert!(
            serde_json::from_value::<CommandArguments>(json!({
                "draft_id": TAILORED_DRAFT_ID,
                "format": "pdf"
            }))
            .is_err()
        );
        assert!(validated_tailored_draft_id("AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA").is_err());
        assert!(normalized_resume_format("html").is_err());
    }

    #[test]
    fn tailored_revision_command_arguments_are_camel_case_and_params_are_exact() {
        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase", deny_unknown_fields)]
        struct ListArguments {
            draft_id: String,
        }

        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase", deny_unknown_fields)]
        struct GetArguments {
            revision_id: String,
        }

        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase", deny_unknown_fields)]
        struct SaveArguments {
            request_id: String,
            draft_id: String,
            expected_parent_revision_id: Option<String>,
            expected_parent_resume_checksum_sha256: String,
            edits: TailoredResumeRevisionEdits,
        }

        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase", deny_unknown_fields)]
        struct ExportArguments {
            revision_id: String,
            format: String,
        }

        let list_arguments: ListArguments = serde_json::from_value(json!({
            "draftId": TAILORED_DRAFT_ID
        }))
        .unwrap();
        assert_eq!(
            tailored_revision_list_params(&list_arguments.draft_id),
            json!({"draft_id": TAILORED_DRAFT_ID})
        );

        let get_arguments: GetArguments = serde_json::from_value(json!({
            "revisionId": TAILORED_REVISION_ONE_ID
        }))
        .unwrap();
        assert_eq!(
            tailored_revision_get_params(&get_arguments.revision_id),
            json!({"revision_id": TAILORED_REVISION_ONE_ID})
        );

        let arguments: SaveArguments = serde_json::from_value(json!({
            "requestId": TAILORED_REVISION_REQUEST_ID,
            "draftId": TAILORED_DRAFT_ID,
            "expectedParentRevisionId": TAILORED_REVISION_ONE_ID,
            "expectedParentResumeChecksumSha256": "b".repeat(64),
            "edits": {
                "basics": {"label": "Product and platform leader"},
                "work": [{
                    "source_index": 0,
                    "summary": null,
                    "highlights": []
                }]
            }
        }))
        .unwrap();
        arguments.edits.validate().unwrap();
        let params = tailored_revision_create_params(
            &validated_tailored_revision_request_id(&arguments.request_id).unwrap(),
            &validated_tailored_draft_id(&arguments.draft_id).unwrap(),
            arguments.expected_parent_revision_id.as_deref(),
            &validated_tailored_revision_checksum(
                &arguments.expected_parent_resume_checksum_sha256,
            )
            .unwrap(),
            &arguments.edits,
        );
        assert_eq!(
            params,
            json!({
                "request_id": TAILORED_REVISION_REQUEST_ID,
                "draft_id": TAILORED_DRAFT_ID,
                "expected_parent_revision_id": TAILORED_REVISION_ONE_ID,
                "expected_parent_resume_checksum_sha256": "b".repeat(64),
                "edits": {
                    "basics": {"label": "Product and platform leader"},
                    "work": [{
                        "source_index": 0,
                        "summary": null,
                        "highlights": []
                    }]
                }
            })
        );
        assert!(params["edits"].get("resume").is_none());
        let export_arguments: ExportArguments = serde_json::from_value(json!({
            "revisionId": TAILORED_REVISION_ONE_ID,
            "format": "pdf"
        }))
        .unwrap();
        assert_eq!(
            tailored_revision_export_params(
                &export_arguments.revision_id,
                &export_arguments.format,
            ),
            json!({"revision_id": TAILORED_REVISION_ONE_ID, "format": "pdf"})
        );
        assert!(
            serde_json::from_value::<ListArguments>(json!({
                "draft_id": TAILORED_DRAFT_ID
            }))
            .is_err()
        );
        assert!(
            serde_json::from_value::<GetArguments>(json!({
                "revision_id": TAILORED_REVISION_ONE_ID
            }))
            .is_err()
        );
        assert!(
            serde_json::from_value::<ExportArguments>(json!({
                "revision_id": TAILORED_REVISION_ONE_ID,
                "format": "pdf"
            }))
            .is_err()
        );
        assert!(
            serde_json::from_value::<SaveArguments>(json!({
                "request_id": TAILORED_REVISION_REQUEST_ID,
                "draft_id": TAILORED_DRAFT_ID,
                "expected_parent_revision_id": null,
                "expected_parent_resume_checksum_sha256": "a".repeat(64),
                "edits": {"basics": {"summary": null}, "work": []}
            }))
            .is_err()
        );
        assert!(
            serde_json::from_value::<SaveArguments>(json!({
                "requestId": TAILORED_REVISION_REQUEST_ID,
                "draftId": TAILORED_DRAFT_ID,
                "expectedParentRevisionId": null,
                "expectedParentResumeChecksumSha256": "a".repeat(64),
                "edits": {"basics": {"summary": null}, "work": []},
                "resume": {}
            }))
            .is_err()
        );
        let _list_command = list_tailored_resume_revisions;
        let _get_command = get_tailored_resume_revision;
        let _save_command = save_tailored_resume_revision;
        let _export_command = export_tailored_resume_revision;
    }

    #[test]
    fn tailored_revision_edits_preserve_deletions_and_enforce_narrow_bounds() {
        let summary_deletion: TailoredResumeRevisionEdits = serde_json::from_value(json!({
            "basics": {"summary": null},
            "work": []
        }))
        .unwrap();
        summary_deletion.validate().unwrap();
        assert_eq!(
            serde_json::to_value(&summary_deletion).unwrap(),
            json!({"basics": {"summary": null}, "work": []})
        );

        let highlight_deletion: TailoredResumeRevisionEdits = serde_json::from_value(json!({
            "basics": {},
            "work": [{"source_index": 0, "highlights": []}]
        }))
        .unwrap();
        highlight_deletion.validate().unwrap();
        assert_eq!(
            serde_json::to_value(&highlight_deletion).unwrap(),
            json!({"basics": {}, "work": [{"source_index": 0, "highlights": []}]})
        );

        for invalid in [
            json!({"basics": {}, "work": []}),
            json!({"basics": {"label": null}, "work": []}),
            json!({"basics": {"label": "é".repeat(101)}, "work": []}),
            json!({"basics": {"summary": "s".repeat(701)}, "work": []}),
            json!({"basics": {}, "work": [{"source_index": 0}]}),
            json!({"basics": {}, "work": [
                {"source_index": 0, "summary": "First"},
                {"source_index": 0, "summary": "Duplicate"}
            ]}),
            json!({"basics": {}, "work": [{
                "source_index": 0,
                "highlights": (0..TAILORED_REVISION_HIGHLIGHTS_LIMIT + 1)
                    .map(|index| format!("Highlight {index}"))
                    .collect::<Vec<_>>()
            }]}),
            json!({"basics": {}, "work": [{
                "source_index": 0,
                "highlights": ["h".repeat(TAILORED_REVISION_HIGHLIGHT_LIMIT_BYTES + 1)]
            }]}),
            json!({"basics": {}, "work": (0..TAILORED_REVISION_WORK_EDITS_LIMIT + 1)
                .map(|source_index| json!({
                    "source_index": source_index,
                    "summary": "Bounded"
                }))
                .collect::<Vec<_>>()}),
        ] {
            let edits: TailoredResumeRevisionEdits = serde_json::from_value(invalid).unwrap();
            assert!(edits.validate().is_err());
        }

        for invalid_shape in [
            json!({}),
            json!({"basics": null, "work": []}),
            json!({"basics": {}, "work": null}),
            json!({"other": {"label": "No"}}),
            json!({"basics": {"headline": "No"}, "work": []}),
            json!({"basics": {}, "work": [{"source_index": 0, "summary": "Okay", "name": "No"}]}),
            json!({"basics": {}, "work": [{"sourceIndex": 0, "summary": "No"}]}),
        ] {
            assert!(serde_json::from_value::<TailoredResumeRevisionEdits>(invalid_shape).is_err());
        }
    }

    #[test]
    fn tailored_revision_receipts_are_strict_bounded_and_lineage_consistent() {
        let revision: TailoredResumeRevision =
            serde_json::from_value(tailored_revision_fixture()).unwrap();
        assert_eq!(
            serde_json::to_value(&revision).unwrap(),
            tailored_revision_fixture()
        );
        revision
            .clone()
            .validated_created(TAILORED_DRAFT_ID, None, &"a".repeat(64))
            .unwrap();
        revision
            .clone()
            .validated_requested(TAILORED_REVISION_ONE_ID)
            .unwrap();
        assert!(
            revision
                .clone()
                .validated_requested(TAILORED_REVISION_TWO_ID)
                .is_err()
        );
        assert!(
            revision
                .clone()
                .validated_created(TAILORED_REVISION_ONE_ID, None, &"a".repeat(64))
                .is_err()
        );
        assert!(
            revision
                .clone()
                .validated_created(TAILORED_DRAFT_ID, None, &"f".repeat(64))
                .is_err()
        );

        let mut missing_parent = tailored_revision_fixture();
        missing_parent
            .as_object_mut()
            .unwrap()
            .remove("parent_revision_id");
        assert!(serde_json::from_value::<TailoredResumeRevision>(missing_parent).is_err());
        let mut unexpected = tailored_revision_fixture();
        unexpected["engine_private"] = json!(true);
        assert!(serde_json::from_value::<TailoredResumeRevision>(unexpected).is_err());
        let mut invalid_author = tailored_revision_fixture();
        invalid_author["author_kind"] = json!("model");
        assert!(serde_json::from_value::<TailoredResumeRevision>(invalid_author).is_err());

        for (field, value) in [
            ("id", json!("not-a-uuid")),
            ("revision_number", json!(0)),
            ("revision_number", json!(2)),
            ("created_at_ms", json!(0)),
            ("resume_checksum_sha256", json!("a".repeat(64))),
            (
                "changed_fields",
                json!(
                    (0..TAILORED_REVISION_CHANGED_FIELDS_LIMIT + 1)
                        .map(|index| format!("/field/{index}"))
                        .collect::<Vec<_>>()
                ),
            ),
            (
                "resume",
                json!({"basics": {"summary": "x".repeat(TAILORED_REVISION_RESUME_LIMIT_BYTES)}}),
            ),
        ] {
            let mut invalid = tailored_revision_fixture();
            invalid[field] = value;
            let revision: TailoredResumeRevision = serde_json::from_value(invalid).unwrap();
            assert!(revision.validate().is_err(), "{field} should be rejected");
        }

        let list: TailoredResumeRevisionList =
            serde_json::from_value(tailored_revision_list_fixture()).unwrap();
        let list = list.validated(TAILORED_DRAFT_ID).unwrap();
        let encoded = serde_json::to_value(&list).unwrap();
        assert_eq!(encoded["revisions"][0]["changed_field_count"], 2);
        assert!(encoded["revisions"][0].get("changed_fields").is_none());
        assert!(encoded["revisions"][0].get("resume").is_none());

        for (path, value) in [
            (
                (1_usize, "parent_revision_id"),
                json!(TAILORED_REVISION_REQUEST_ID),
            ),
            ((1, "parent_resume_checksum_sha256"), json!("f".repeat(64))),
            ((1, "revision_number"), json!(4)),
            ((0, "changed_field_count"), json!(0)),
        ] {
            let mut invalid = tailored_revision_list_fixture();
            invalid["revisions"][path.0][path.1] = value;
            let list: TailoredResumeRevisionList = serde_json::from_value(invalid).unwrap();
            assert!(list.validated(TAILORED_DRAFT_ID).is_err());
        }
        let mut summary_with_resume = tailored_revision_list_fixture();
        summary_with_resume["revisions"][0]["resume"] = json!({});
        assert!(serde_json::from_value::<TailoredResumeRevisionList>(summary_with_resume).is_err());

        let mut parent_revision_id = TAILORED_REVISION_ONE_ID.to_string();
        let mut truncated_revisions = Vec::new();
        for revision_number in 2_u64..=51 {
            let revision_id = format!("00000000-0000-4000-8000-{revision_number:012x}");
            truncated_revisions.push(tailored_revision_summary_fixture(
                &revision_id,
                Some(&parent_revision_id),
                revision_number,
                &format!("{:064x}", revision_number - 1),
                &format!("{revision_number:064x}"),
            ));
            parent_revision_id = revision_id;
        }
        let truncated: TailoredResumeRevisionList = serde_json::from_value(json!({
            "draft_id": TAILORED_DRAFT_ID,
            "base_resume_checksum_sha256": "f".repeat(64),
            "revisions": truncated_revisions,
            "truncated": true
        }))
        .unwrap();
        truncated.validated(TAILORED_DRAFT_ID).unwrap();
    }

    #[test]
    fn tailored_revision_export_rejects_identity_format_and_unsafe_paths() {
        let wire: EngineTailoredResumeRevisionArtifact =
            serde_json::from_value(tailored_revision_artifact_fixture()).unwrap();
        let artifact = wire.validated(TAILORED_REVISION_TWO_ID, "pdf").unwrap();
        assert_eq!(artifact.artifact_id, TAILORED_REVISION_ARTIFACT_ID);
        assert_eq!(artifact.format, "pdf");

        let mut unexpected = tailored_revision_artifact_fixture();
        unexpected["private_path"] = json!("must not cross");
        assert!(
            serde_json::from_value::<EngineTailoredResumeRevisionArtifact>(unexpected).is_err()
        );
        for (field, value) in [
            ("revision_id", json!(TAILORED_REVISION_ONE_ID)),
            ("draft_id", json!("not-a-uuid")),
            ("opportunity_id", json!("not-a-uuid")),
            ("profile_version_id", json!("not-a-uuid")),
            ("artifact_id", json!("not-a-uuid")),
            ("format", json!("docx")),
            ("media_type", json!("text/plain")),
            ("relative_path", json!("../outside.pdf")),
            (
                "relative_path",
                json!(format!("artifacts/resumes/{TAILORED_ARTIFACT_ID}.pdf")),
            ),
            ("checksum_sha256", json!("D".repeat(64))),
            ("byte_size", json!(0)),
            ("suggested_filename", json!("../../private.pdf")),
        ] {
            let mut invalid = tailored_revision_artifact_fixture();
            invalid[field] = value;
            let wire: EngineTailoredResumeRevisionArtifact =
                serde_json::from_value(invalid).unwrap();
            assert!(
                wire.validated(TAILORED_REVISION_TWO_ID, "pdf").is_err(),
                "{field} should be rejected"
            );
        }
    }

    #[test]
    fn suggested_names_cannot_supply_directories() {
        assert_eq!(
            sanitized_suggested_filename("../../Ada:Resume.docx", "pdf"),
            "AdaResume.pdf"
        );
        assert_eq!(
            sanitized_suggested_filename("", "docx"),
            "CVGnome-Resume.docx"
        );
    }

    #[test]
    fn canonical_import_manifest_contains_only_opaque_source_metadata() {
        let scan_id = "4c3cf11d-98c5-4938-b761-9b7b6e1c7cb8";
        let params = canonical_import_params(StagedCanonicalProfile {
            scan_id: scan_id.to_string(),
            managed_relative_path: format!("imports/staging/{scan_id}/0000.json"),
            display_name: "canonical profile.json".to_string(),
            raw_sha256: "a".repeat(64),
            raw_bytes: 37,
        });

        assert_eq!(
            params,
            json!({
                "source": {
                    "scan_id": scan_id,
                    "managed_relative_path": format!("imports/staging/{scan_id}/0000.json"),
                    "display_name": "canonical profile.json",
                    "raw_sha256": "a".repeat(64),
                    "raw_bytes": 37
                }
            })
        );
        assert!(params.get("profile").is_none());
        assert!(params["source"].get("raw_content").is_none());
        assert!(params["source"].get("selected_path").is_none());
    }

    #[test]
    fn canonical_import_receipt_filters_the_engine_compaction_report() {
        let wire = json!({
            "profile_version_id": "64aa9547-6ff7-42a9-a03d-c29c3605ce80",
            "version_number": 1,
            "created": true,
            "checksum_sha256": "a".repeat(64),
            "profile_name": "Ada Lovelace",
            "work_entries": 1,
            "education_entries": 1,
            "skill_groups": 2,
            "renderable": true,
            "compaction_report": {
                "version": 1,
                "metrics_before": {"profile_json_chars": 1200, "work_entries": 1},
                "metrics_after": {"profile_json_chars": 1000, "work_entries": 1},
                "skill_groups_merged": 0,
                "changed": true
            }
        });
        let receipt: ProfileImportResultWire = serde_json::from_value(wire.clone()).unwrap();
        let receipt = receipt.validated().unwrap();
        let renderer_value = serde_json::to_value(receipt).unwrap();
        assert!(renderer_value.get("compaction_report").is_none());
        assert_eq!(renderer_value["profile_name"], "Ada Lovelace");

        let mut with_extra = wire.clone();
        with_extra["private_engine_detail"] = json!("must not cross the bridge");
        assert!(serde_json::from_value::<ProfileImportResultWire>(with_extra).is_err());

        let mut invalid_report = wire.clone();
        invalid_report["compaction_report"] = json!({
            "version": 1,
            "metrics_before": {},
            "metrics_after": {}
        });
        let receipt: ProfileImportResultWire = serde_json::from_value(invalid_report).unwrap();
        assert!(receipt.validated().is_err());

        let mut oversized_report = wire;
        oversized_report["compaction_report"]["private_detail"] =
            json!("x".repeat(PROFILE_COMPACTION_REPORT_LIMIT_BYTES));
        let receipt: ProfileImportResultWire = serde_json::from_value(oversized_report).unwrap();
        assert!(receipt.validated().is_err());
    }

    #[test]
    fn canonical_import_cleanup_preserves_engine_errors_and_never_erases_an_ok_request() {
        let selected = tempfile::tempdir().unwrap();
        let data = tempfile::tempdir().unwrap();
        let source = selected.path().join("canonical.json");
        fs::write(&source, b"{}").unwrap();

        let failed_scan_id = Uuid::new_v4().to_string();
        stage_canonical_profile(&source, data.path(), &failed_scan_id).unwrap();
        let engine_error = "canonical_profile.invalid: expected basics".to_string();
        let result = cleanup_failed_canonical_import::<()>(
            data.path(),
            &failed_scan_id,
            Err(engine_error.clone()),
        );
        assert_eq!(result.unwrap_err(), engine_error);
        assert!(
            !data
                .path()
                .join("imports/staging")
                .join(failed_scan_id)
                .exists()
        );

        let accepted_scan_id = Uuid::new_v4().to_string();
        stage_canonical_profile(&source, data.path(), &accepted_scan_id).unwrap();
        cleanup_failed_canonical_import(data.path(), &accepted_scan_id, Ok(())).unwrap();
        assert!(
            data.path()
                .join("imports/staging")
                .join(&accepted_scan_id)
                .exists()
        );
        cleanup_staging(data.path(), &accepted_scan_id).unwrap();
    }

    #[test]
    fn retained_source_receipts_are_strict_and_internally_consistent() {
        let receipt = json!({
            "source_retention_generation": 2,
            "retained_source_files": 2,
            "retained_source_bytes": 4096,
            "retained_source_imports": 1,
            "historical_source_files": 1,
            "historical_source_imports": 1,
            "last_source_import_at_ms": 1_800_000_000_100_u64,
            "source_retention_reset_at_ms": 1_800_000_000_000_u64
        });
        let parsed: RetainedSourceStatus = serde_json::from_value(receipt.clone()).unwrap();
        assert_eq!(parsed.validated().unwrap().retained_source_bytes, 4096);

        let mut with_extra = receipt.clone();
        with_extra["display_name"] = json!("must not cross the bridge");
        assert!(serde_json::from_value::<RetainedSourceStatus>(with_extra).is_err());

        let mut inconsistent = receipt;
        inconsistent["retained_source_imports"] = json!(0);
        let parsed: RetainedSourceStatus = serde_json::from_value(inconsistent).unwrap();
        assert!(parsed.validated().is_err());
    }

    #[test]
    fn discard_unfinished_source_reviews_contract_is_strict_and_parameterless() {
        assert_eq!(discard_unfinished_source_reviews_params(), json!({}));

        let receipt: DiscardUnfinishedSourceReviewsResult =
            serde_json::from_value(json!({"discarded_previews": 3})).unwrap();
        assert_eq!(receipt.validated().unwrap().discarded_previews, 3);

        assert!(
            serde_json::from_value::<DiscardUnfinishedSourceReviewsResult>(json!({
                "discarded_previews": 3,
                "scan_id": "must not cross the bridge"
            }))
            .is_err()
        );
        let unsafe_count: DiscardUnfinishedSourceReviewsResult = serde_json::from_value(json!({
            "discarded_previews": JAVASCRIPT_MAX_SAFE_INTEGER + 1
        }))
        .unwrap();
        assert!(unsafe_count.validated().is_err());

        // Referencing the command here and in `generate_handler!` keeps its
        // no-renderer-argument signature compile-checked.
        let _command = discard_unfinished_profile_source_reviews;
    }

    #[test]
    fn source_reset_payload_is_canonical_and_command_arguments_are_camel_case() {
        let request_id = "4c3cf11d-98c5-4938-b761-9b7b6e1c7cb8";
        assert_eq!(
            source_reset_params(2, request_id).unwrap(),
            json!({"expected_generation": 2, "request_id": request_id})
        );
        assert!(source_reset_params(0, request_id).is_err());
        assert!(source_reset_params(2, "not-a-uuid").is_err());

        let result: RetainedSourceStatus = serde_json::from_value(json!({
            "source_retention_generation": 3,
            "retained_source_files": 0,
            "retained_source_bytes": 0,
            "retained_source_imports": 0,
            "historical_source_files": 1,
            "historical_source_imports": 1,
            "last_source_import_at_ms": null,
            "source_retention_reset_at_ms": 1_800_000_000_000_u64
        }))
        .unwrap();
        assert!(validated_source_reset_result(2, result.clone()).is_ok());
        assert!(validated_source_reset_result(1, result).is_err());

        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase", deny_unknown_fields)]
        struct CommandArguments {
            expected_generation: u64,
        }
        let arguments: CommandArguments =
            serde_json::from_value(json!({"expectedGeneration": 2})).unwrap();
        assert_eq!(arguments.expected_generation, 2);
        assert!(
            serde_json::from_value::<CommandArguments>(json!({"expected_generation": 2})).is_err()
        );
    }

    #[test]
    fn career_workspace_reset_payload_is_canonical_and_strict() {
        let request_id = "4c3cf11d-98c5-4938-b761-9b7b6e1c7cb8";
        let expected: CareerWorkspaceResetExpected = serde_json::from_value(json!({
            "profileVersions": 9,
            "opportunities": 3,
            "artifacts": 4,
            "sourceImports": 2,
            "sourcePreviews": 1,
            "sourceRetentionGeneration": 2,
            "reviewInboxItems": 5,
            "reviewDeferredItems": 6,
            "reviewHistoryItems": 7,
            "sourceReviewInboxItems": 8,
            "sourceReviewDeferredItems": 9,
            "sourceReviewHistoryItems": 10,
            "memoryCount": 11
        }))
        .unwrap();
        assert_eq!(
            career_workspace_reset_params(&expected, request_id).unwrap(),
            json!({
                "request_id": request_id,
                "expected": {
                    "profile_versions": 9,
                    "opportunities": 3,
                    "artifacts": 4,
                    "source_imports": 2,
                    "source_previews": 1,
                    "source_retention_generation": 2,
                    "review_inbox_items": 5,
                    "review_deferred_items": 6,
                    "review_history_items": 7,
                    "source_review_inbox_items": 8,
                    "source_review_deferred_items": 9,
                    "source_review_history_items": 10,
                    "memory_count": 11
                }
            })
        );

        assert!(career_workspace_reset_params(&expected, "not-a-uuid").is_err());
        assert!(
            serde_json::from_value::<CareerWorkspaceResetExpected>(json!({
                "profileVersions": 9,
                "opportunities": 3,
                "artifacts": 4,
                "sourceImports": 2,
                "sourcePreviews": 1,
                "sourceRetentionGeneration": 2,
                "reviewInboxItems": 5,
                "reviewDeferredItems": 6,
                "reviewHistoryItems": 7,
                "sourceReviewInboxItems": 8,
                "sourceReviewDeferredItems": 9,
                "sourceReviewHistoryItems": 10,
                "memoryCount": 11,
                "databasePath": "/private/data"
            }))
            .is_err()
        );
        let invalid_generation: CareerWorkspaceResetExpected = serde_json::from_value(json!({
            "profileVersions": 0,
            "opportunities": 0,
            "artifacts": 0,
            "sourceImports": 0,
            "sourcePreviews": 0,
            "sourceRetentionGeneration": 0,
            "reviewInboxItems": 0,
            "reviewDeferredItems": 0,
            "reviewHistoryItems": 0,
            "sourceReviewInboxItems": 0,
            "sourceReviewDeferredItems": 0,
            "sourceReviewHistoryItems": 0,
            "memoryCount": 0
        }))
        .unwrap();
        assert!(invalid_generation.validated().is_err());
        let unsafe_count: CareerWorkspaceResetExpected = serde_json::from_value(json!({
            "profileVersions": JAVASCRIPT_MAX_SAFE_INTEGER + 1,
            "opportunities": 0,
            "artifacts": 0,
            "sourceImports": 0,
            "sourcePreviews": 0,
            "sourceRetentionGeneration": 1,
            "reviewInboxItems": 0,
            "reviewDeferredItems": 0,
            "reviewHistoryItems": 0,
            "sourceReviewInboxItems": 0,
            "sourceReviewDeferredItems": 0,
            "sourceReviewHistoryItems": 0,
            "memoryCount": 0
        }))
        .unwrap();
        assert!(unsafe_count.validated().is_err());

        let _command = reset_career_workspace;
    }

    #[test]
    fn career_workspace_reset_receipt_is_bounded_and_pristine() {
        let request_id = "4c3cf11d-98c5-4938-b761-9b7b6e1c7cb8";
        let fixture = json!({
            "request_id": request_id,
            "reset_at_ms": 1_800_000_000_000_u64,
            "schema_version": ENGINE_SCHEMA_VERSION,
            "source_retention_generation": 1,
            "profile_versions": 0,
            "opportunities": 0,
            "artifacts": 0,
            "source_imports": 0,
            "source_previews": 0,
            "review_inbox_items": 0,
            "review_deferred_items": 0,
            "review_history_items": 0,
            "created": true,
            "source_review_inbox_items": 0,
            "source_review_deferred_items": 0,
            "source_review_history_items": 0,
            "memory_count": 0
        });
        let receipt: CareerWorkspaceResetReceipt = serde_json::from_value(fixture.clone()).unwrap();
        assert!(receipt.validated(request_id).unwrap().created);

        let mut retry = fixture.clone();
        retry["created"] = json!(false);
        let receipt: CareerWorkspaceResetReceipt = serde_json::from_value(retry).unwrap();
        assert!(!receipt.validated(request_id).unwrap().created);

        let mut private_path = fixture.clone();
        private_path["database_path"] = json!("/private/data");
        assert!(serde_json::from_value::<CareerWorkspaceResetReceipt>(private_path).is_err());

        for (field, value) in [
            ("request_id", json!("not-a-uuid")),
            ("reset_at_ms", json!(0)),
            ("schema_version", json!(ENGINE_SCHEMA_VERSION + 1)),
            ("source_retention_generation", json!(2)),
            ("profile_versions", json!(1)),
            ("opportunities", json!(1)),
            ("artifacts", json!(1)),
            ("source_imports", json!(1)),
            ("source_previews", json!(1)),
            ("review_inbox_items", json!(1)),
            ("source_review_inbox_items", json!(1)),
            ("source_review_deferred_items", json!(1)),
            ("source_review_history_items", json!(1)),
            ("memory_count", json!(1)),
            ("review_deferred_items", json!(1)),
            ("review_history_items", json!(1)),
        ] {
            let mut invalid = fixture.clone();
            invalid[field] = value;
            let receipt: CareerWorkspaceResetReceipt = serde_json::from_value(invalid).unwrap();
            assert!(
                receipt.validated(request_id).is_err(),
                "{field} should fail"
            );
        }

        let receipt: CareerWorkspaceResetReceipt = serde_json::from_value(fixture).unwrap();
        assert!(
            receipt
                .validated("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
                .is_err()
        );
    }

    #[test]
    fn retained_source_list_params_are_canonical_and_bounded() {
        assert_eq!(
            retained_source_list_params("historical", 25).unwrap(),
            json!({
                "scope": "historical",
                "limit": RETAINED_SOURCE_LIST_LIMIT,
                "offset": 25
            })
        );
        assert!(retained_source_list_params("current", 0).is_err());
        assert!(
            retained_source_list_params("active", RETAINED_SOURCE_LIST_OFFSET_LIMIT + 1).is_err()
        );

        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase", deny_unknown_fields)]
        struct CommandArguments {
            scope: String,
            offset: usize,
        }
        let arguments: CommandArguments =
            serde_json::from_value(json!({"scope": "active", "offset": 0})).unwrap();
        assert_eq!(arguments.scope, "active");
        assert_eq!(arguments.offset, 0);
        let _command = list_retained_profile_sources;
    }

    #[test]
    fn retained_source_list_receipts_are_strict_and_scope_consistent() {
        let fixture = retained_source_list_fixture();
        let result: RetainedSourceListResult = serde_json::from_value(fixture.clone()).unwrap();
        assert_eq!(
            result
                .validated(RetainedSourceScope::Active, 0)
                .unwrap()
                .items[0]
                .display_name,
            "career evidence.pdf"
        );

        let multibyte_alias = format!("{}.json", "📄".repeat(60));
        assert!(multibyte_alias.len() > RETAINED_SOURCE_DISPLAY_NAME_LIMIT_CHARS);
        let mut multibyte = fixture.clone();
        multibyte["items"][0]["display_name"] = json!(multibyte_alias);
        let result: RetainedSourceListResult = serde_json::from_value(multibyte).unwrap();
        assert!(result.validated(RetainedSourceScope::Active, 0).is_ok());

        let maximum_multibyte_alias = format!("{}.json", "📄".repeat(235));
        assert_eq!(
            maximum_multibyte_alias.chars().count(),
            RETAINED_SOURCE_DISPLAY_NAME_LIMIT_CHARS
        );
        assert!(maximum_multibyte_alias.len() <= RETAINED_SOURCE_DISPLAY_NAME_LIMIT_BYTES);
        assert!(validate_retained_source_display_name(&maximum_multibyte_alias).is_ok());

        let mut unknown = fixture.clone();
        unknown["items"][0]["managed_path"] = json!("must not cross the bridge");
        assert!(serde_json::from_value::<RetainedSourceListResult>(unknown).is_err());

        let mut invalid_enum = fixture.clone();
        invalid_enum["items"][0]["source_format"] = json!("html");
        assert!(serde_json::from_value::<RetainedSourceListResult>(invalid_enum).is_err());

        let mismatched: RetainedSourceListResult = serde_json::from_value(fixture.clone()).unwrap();
        assert!(
            mismatched
                .validated(RetainedSourceScope::Historical, 0)
                .is_err()
        );

        let mut inconsistent_status = fixture.clone();
        inconsistent_status["items"][0]["retention_status"] = json!("historical");
        let result: RetainedSourceListResult = serde_json::from_value(inconsistent_status).unwrap();
        assert!(result.validated(RetainedSourceScope::Active, 0).is_err());

        let mut invalid_name = fixture.clone();
        invalid_name["items"][0]["display_name"] = json!("evidence\u{0000}.pdf");
        let result: RetainedSourceListResult = serde_json::from_value(invalid_name).unwrap();
        assert!(result.validated(RetainedSourceScope::Active, 0).is_err());

        let too_many_characters = "é".repeat(RETAINED_SOURCE_DISPLAY_NAME_LIMIT_CHARS + 1);
        assert!(too_many_characters.len() < RETAINED_SOURCE_DISPLAY_NAME_LIMIT_BYTES);
        assert!(validate_retained_source_display_name(&too_many_characters).is_err());

        let too_many_bytes = "📄".repeat(RETAINED_SOURCE_DISPLAY_NAME_LIMIT_CHARS + 1);
        assert!(too_many_bytes.len() > RETAINED_SOURCE_DISPLAY_NAME_LIMIT_BYTES);
        assert!(validate_retained_source_display_name(&too_many_bytes).is_err());

        let mut invalid_issue = fixture;
        invalid_issue["items"][0]["issue_code"] = json!("not safe");
        let result: RetainedSourceListResult = serde_json::from_value(invalid_issue).unwrap();
        assert!(result.validated(RetainedSourceScope::Active, 0).is_err());
    }

    #[test]
    fn retained_source_list_rejects_inconsistent_totals_and_pages() {
        let mut too_many_items = retained_source_list_fixture();
        too_many_items["items"] = Value::Array(
            (0..=RETAINED_SOURCE_LIST_LIMIT)
                .map(|index| {
                    let mut item = retained_source_list_fixture()["items"][0].clone();
                    item["import_id"] = json!(Uuid::new_v4().to_string());
                    item["ordinal"] = json!(index);
                    item["import_file_count"] = json!(RETAINED_SOURCE_LIST_LIMIT + 1);
                    item
                })
                .collect(),
        );
        too_many_items["total_entries"] = json!(RETAINED_SOURCE_LIST_LIMIT + 1);
        let result: RetainedSourceListResult = serde_json::from_value(too_many_items).unwrap();
        assert!(result.validated(RetainedSourceScope::Active, 0).is_err());

        let mut truncated_page = retained_source_list_fixture();
        truncated_page["total_entries"] = json!(2);
        let result: RetainedSourceListResult = serde_json::from_value(truncated_page).unwrap();
        assert!(result.validated(RetainedSourceScope::Active, 0).is_err());

        let mut impossible_totals = retained_source_list_fixture();
        impossible_totals["total_unique_files"] = json!(2);
        let result: RetainedSourceListResult = serde_json::from_value(impossible_totals).unwrap();
        assert!(result.validated(RetainedSourceScope::Active, 0).is_err());
    }

    #[test]
    fn engine_status_filters_the_vault_path_and_validates_retention_totals() {
        let fixture = json!({
            "engine_version": env!("CARGO_PKG_VERSION"),
            "schema_version": ENGINE_SCHEMA_VERSION,
            "database_path": "/private/local/vault.sqlite3",
            "profile_versions": 2,
            "opportunities": 1,
            "artifacts": 1,
            "pending_jobs": 0,
            "source_snapshots": 3,
            "source_imports": 2,
            "source_previews": 0,
            "review_inbox_items": 2,
            "review_deferred_items": 1,
            "review_history_items": 3,
            "source_review_inbox_items": 4,
            "source_review_deferred_items": 5,
            "source_review_history_items": 6,
            "memory_count": 2,
            "source_retention_generation": 2,
            "retained_source_files": 2,
            "retained_source_bytes": 4096,
            "retained_source_imports": 1,
            "historical_source_files": 1,
            "historical_source_imports": 1,
            "last_source_import_at_ms": 1_800_000_000_100_u64,
            "source_retention_reset_at_ms": 1_800_000_000_000_u64,
            "latest_profile_name": "Ada Lovelace",
            "latest_profile_renderable": true
        });
        let wire: EngineStatusWire = serde_json::from_value(fixture.clone()).unwrap();
        let status = wire.validated().unwrap();
        let renderer_value = serde_json::to_value(status).unwrap();
        assert!(renderer_value.get("database_path").is_none());
        assert_eq!(renderer_value["retained_source_files"], 2);
        assert_eq!(renderer_value["review_inbox_items"], 2);
        assert_eq!(renderer_value["source_review_inbox_items"], 4);

        let mut unknown = fixture.clone();
        unknown["private_source_path"] = json!("must not cross the bridge");
        assert!(serde_json::from_value::<EngineStatusWire>(unknown).is_err());

        let mut inconsistent = fixture;
        inconsistent["source_imports"] = json!(3);
        let wire: EngineStatusWire = serde_json::from_value(inconsistent).unwrap();
        assert!(wire.validated().is_err());

        let mut invalid_review_count = json!({
            "engine_version": env!("CARGO_PKG_VERSION"),
            "schema_version": ENGINE_SCHEMA_VERSION,
            "database_path": "/private/local/vault.sqlite3",
            "profile_versions": 0,
            "opportunities": 0,
            "artifacts": 0,
            "pending_jobs": 0,
            "source_snapshots": 0,
            "source_imports": 0,
            "source_previews": 0,
            "review_inbox_items": JAVASCRIPT_MAX_SAFE_INTEGER + 1,
            "review_deferred_items": 0,
            "review_history_items": 0,
            "source_review_inbox_items": 0,
            "source_review_deferred_items": 0,
            "source_review_history_items": 0,
            "memory_count": 0,
            "source_retention_generation": 1,
            "retained_source_files": 0,
            "retained_source_bytes": 0,
            "retained_source_imports": 0,
            "historical_source_files": 0,
            "historical_source_imports": 0,
            "last_source_import_at_ms": null,
            "source_retention_reset_at_ms": null,
            "latest_profile_name": null,
            "latest_profile_renderable": null
        });
        let wire: EngineStatusWire = serde_json::from_value(invalid_review_count.clone()).unwrap();
        assert!(wire.validated().is_err());
        invalid_review_count["review_inbox_items"] = json!(0);
        for field in [
            "source_review_inbox_items",
            "source_review_deferred_items",
            "source_review_history_items",
            "memory_count",
        ] {
            let mut invalid = invalid_review_count.clone();
            invalid[field] = json!(JAVASCRIPT_MAX_SAFE_INTEGER + 1);
            let wire: EngineStatusWire = serde_json::from_value(invalid).unwrap();
            assert!(wire.validated().is_err());
        }
        let wire: EngineStatusWire = serde_json::from_value(invalid_review_count).unwrap();
        assert!(wire.validated().is_ok());
    }

    #[test]
    fn profile_source_contract_dtos_accept_engine_receipts() {
        let scan: ProfileSourceScanResult = serde_json::from_value(json!({
            "scan_id": "4c3cf11d-98c5-4938-b761-9b7b6e1c7cb8",
            "expires_at_ms": 1_800_000_000_000_u64,
            "base_profile": null,
            "file_counts": {
                "discovered": 2,
                "staged": 1,
                "parsed": 1,
                "duplicates": 0,
                "skipped": 1,
                "failed": 0
            },
            "source_counts": {
                "resume": 1,
                "evidence": 0,
                "preferences": 0,
                "unclassified": 0
            },
            "format_counts": {"docx": 1},
            "warnings": [{
                "code": "unsupported_file_type",
                "stage": "scan",
                "severity": "info",
                "count": 1
            }],
            "can_build": true,
            "review_candidate_count": 2,
            "source_review_count": 1
        }))
        .unwrap();
        assert!(scan.can_build);
        assert_eq!(scan.file_counts.parsed, 1);
        assert_eq!(scan.review_candidate_count, 2);
        assert_eq!(scan.source_review_count, 1);
        let scan = scan.validated_review_candidate_count().unwrap();
        let mut maximum_scan = scan.clone();
        maximum_scan.review_candidate_count = PROFILE_SOURCE_REVIEW_CANDIDATE_LIMIT;
        assert!(maximum_scan.validated_review_candidate_count().is_ok());
        let mut oversized_scan = scan.clone();
        oversized_scan.review_candidate_count = PROFILE_SOURCE_REVIEW_CANDIDATE_LIMIT + 1;
        assert!(oversized_scan.validated_review_candidate_count().is_err());
        let mut maximum_source_checks = scan.clone();
        maximum_source_checks.source_review_count = 240;
        assert!(
            maximum_source_checks
                .clone()
                .validated_review_candidate_count()
                .is_ok()
        );
        maximum_source_checks.source_review_count = 241;
        assert!(
            maximum_source_checks
                .validated_review_candidate_count()
                .is_err()
        );

        let build: ProfileSourceBuildResult = serde_json::from_value(json!({
            "scan_id": scan.scan_id,
            "profile_version_id": "64aa9547-6ff7-42a9-a03d-c29c3605ce80",
            "version_number": 1,
            "created": true,
            "checksum_sha256": "a".repeat(64),
            "profile_name": "Ada Lovelace",
            "renderable": true,
            "source_counts": {"parsed": 1, "used": 1, "unused": 0},
            "profile_counts": {
                "work_entries": 1,
                "education_entries": 0,
                "skill_groups": 1,
                "evidence_claims": 0,
                "preference_items": 0
            },
            "warnings": [],
            "review_item_count": 2,
            "source_review_count": 1
        }))
        .unwrap();
        assert!(build.renderable);
        assert_eq!(build.source_counts.used, 1);
        assert_eq!(build.review_item_count, 2);
        assert_eq!(build.source_review_count, 1);
        let mut maximum_source_checks = build.clone();
        maximum_source_checks.source_review_count = 240;
        assert!(
            maximum_source_checks
                .clone()
                .validated_review_item_count()
                .is_ok()
        );
        maximum_source_checks.source_review_count = 241;
        assert!(maximum_source_checks.validated_review_item_count().is_err());
        assert!(build.clone().validated_review_item_count().is_ok());
        let mut maximum_build = build.clone();
        maximum_build.review_item_count = PROFILE_SOURCE_REVIEW_CANDIDATE_LIMIT;
        assert!(maximum_build.validated_review_item_count().is_ok());
        let mut oversized_build = build;
        oversized_build.review_item_count = PROFILE_SOURCE_REVIEW_CANDIDATE_LIMIT + 1;
        assert!(oversized_build.validated_review_item_count().is_err());

        let mut private_preview = serde_json::to_value(scan).unwrap();
        private_preview["candidate_payload"] = json!({"private": true});
        assert!(serde_json::from_value::<ProfileSourceScanResult>(private_preview).is_err());

        let mut invalid_count = json!({
            "scan_id": "4c3cf11d-98c5-4938-b761-9b7b6e1c7cb8",
            "expires_at_ms": 1_800_000_000_000_u64,
            "base_profile": null,
            "file_counts": {"discovered": 0, "staged": 0, "parsed": 0, "duplicates": 0, "skipped": 0, "failed": 0},
            "source_counts": {"resume": 0, "evidence": 0, "preferences": 0, "unclassified": 0},
            "format_counts": {},
            "warnings": [],
            "can_build": false,
            "review_candidate_count": JAVASCRIPT_MAX_SAFE_INTEGER + 1,
            "source_review_count": 0,
        });
        let scan: ProfileSourceScanResult = serde_json::from_value(invalid_count.clone()).unwrap();
        assert!(scan.validated_review_candidate_count().is_err());
        invalid_count["review_candidate_count"] = json!(0);
        let scan: ProfileSourceScanResult = serde_json::from_value(invalid_count).unwrap();
        assert!(scan.validated_review_candidate_count().is_ok());
    }

    #[test]
    fn opportunity_input_is_strict_normalized_and_character_bounded() {
        let input: SaveOpportunityInput = serde_json::from_value(json!({
            "title": "  Product Manager  ",
            "description": "\nOwn discovery and delivery.\n",
            "company": "  Northstar  ",
            "location": "Remote",
            "source_url": "https://example.com/jobs/123",
            "apply_url": ""
        }))
        .unwrap();
        let params = input.into_engine_params().unwrap();
        assert_eq!(params["title"], "Product Manager");
        assert_eq!(params["description"], "Own discovery and delivery.");
        assert_eq!(params["company"], "Northstar");
        assert!(params["apply_url"].is_null());

        assert!(
            serde_json::from_value::<SaveOpportunityInput>(json!({
                "title": "Product Manager",
                "description": "A role",
                "unexpected": "not accepted"
            }))
            .is_err()
        );

        let unicode_title = "é".repeat(OPPORTUNITY_TITLE_LIMIT_CHARS / 2);
        let at_limit: SaveOpportunityInput = serde_json::from_value(json!({
            "title": unicode_title,
            "description": "A role"
        }))
        .unwrap();
        assert!(at_limit.into_engine_params().is_ok());

        let over_byte_limit: SaveOpportunityInput = serde_json::from_value(json!({
            "title": "é".repeat((OPPORTUNITY_TITLE_LIMIT_CHARS / 2) + 1),
            "description": "A role"
        }))
        .unwrap();
        assert!(over_byte_limit.into_engine_params().is_err());

        let over_limit: SaveOpportunityInput = serde_json::from_value(json!({
            "title": "x".repeat(OPPORTUNITY_TITLE_LIMIT_CHARS + 1),
            "description": "A role"
        }))
        .unwrap();
        assert!(over_limit.into_engine_params().is_err());

        let over_description_limit: SaveOpportunityInput = serde_json::from_value(json!({
            "title": "Product Manager",
            "description": "x".repeat(OPPORTUNITY_DESCRIPTION_LIMIT_CHARS + 1)
        }))
        .unwrap();
        assert!(over_description_limit.into_engine_params().is_err());

        let over_json_limit: SaveOpportunityInput = serde_json::from_value(json!({
            "title": "Product Manager",
            "description": "\\".repeat((OPPORTUNITY_DESCRIPTION_LIMIT_CHARS / 2) + 1)
        }))
        .unwrap();
        assert!(over_json_limit.into_engine_params().is_err());

        assert!(
            normalized_opportunity_query(Some("x".repeat(OPPORTUNITY_QUERY_LIMIT_CHARS + 1)))
                .is_err()
        );
        assert!(normalized_opportunity_query(Some("role\nsecret".to_string())).is_err());

        assert_eq!(
            validated_opportunity_offset(OPPORTUNITY_LIST_OFFSET_LIMIT).unwrap(),
            OPPORTUNITY_LIST_OFFSET_LIMIT
        );
        assert!(validated_opportunity_offset(OPPORTUNITY_LIST_OFFSET_LIMIT + 1).is_err());
    }

    #[test]
    fn opportunity_urls_are_https_and_credential_free() {
        for invalid in [
            "http://example.com/jobs/123",
            "https://user@example.com/jobs/123",
            "https://example.com/jobs/has space",
            "https://example.com\\jobs\\123",
            " https://example.com/jobs/123",
        ] {
            assert!(validate_https_url(invalid, "Source URL").is_err());
        }
        assert!(
            validate_https_url("https://example.com/jobs/123?from=cvgnome", "Source URL").is_ok()
        );
    }

    #[test]
    fn opportunity_list_params_are_exact_and_enums_are_strict() {
        for query in [None, Some(String::new()), Some("   ".to_string())] {
            assert_eq!(
                opportunity_list_params(query, 0, "all", "last_action").unwrap(),
                json!({"query": "", "offset": 0, "stage": "all", "sort": "last_action"})
            );
        }
        let params = opportunity_list_params(
            Some("  product lead  ".to_string()),
            20,
            "to_apply",
            "strength",
        )
        .unwrap();
        assert_eq!(
            params,
            json!({
                "query": "product lead",
                "offset": 20,
                "stage": "to_apply",
                "sort": "strength"
            })
        );
        assert!(params.get("limit").is_none());
        assert!(opportunity_list_params(None, 0, "unknown", "last_action").is_err());
        assert!(opportunity_list_params(None, 0, "all", "newest").is_err());
        assert!(
            opportunity_list_params(
                None,
                OPPORTUNITY_LIST_OFFSET_LIMIT + 1,
                "all",
                "last_action"
            )
            .is_err()
        );

        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase", deny_unknown_fields)]
        struct CommandArguments {
            query: Option<String>,
            offset: usize,
            stage: String,
            sort: String,
        }
        let arguments: CommandArguments = serde_json::from_value(json!({
            "query": null,
            "offset": 0,
            "stage": "all",
            "sort": "last_action"
        }))
        .unwrap();
        assert!(arguments.query.is_none());
        assert_eq!(arguments.offset, 0);
        assert_eq!(arguments.stage, "all");
        assert_eq!(arguments.sort, "last_action");
    }

    #[test]
    #[ignore = "requires the Python engine; run scripts/check_native_engine_contract.py"]
    fn opportunity_list_native_payload_round_trips_engine_rpc() {
        use std::io::Write;
        use std::process::{Command, Stdio};

        // Exercise the production native serializer, not a separately maintained
        // JSON fixture. The Python harness checks these payloads through real RPC.
        let cases = [
            ("missing", None, 0, "all", "last_action"),
            ("blank", Some(""), 0, "all", "last_action"),
            ("whitespace", Some("   "), 0, "all", "last_action"),
            ("search", Some("  PYTHON  "), 0, "all", "strength"),
            (
                "no_match",
                Some("unlisted-keyword"),
                0,
                "all",
                "last_action",
            ),
            ("filtered", None, 0, "applied", "last_action"),
            ("next_page", Some(""), 1, "all", "stage"),
        ];
        let payloads: Vec<Value> = cases
            .into_iter()
            .map(|(id, query, offset, stage, sort)| {
                json!({
                    "id": id,
                    "params": opportunity_list_params(query.map(str::to_owned), offset, stage, sort)
                        .expect("native request must normalize successfully"),
                })
            })
            .collect();
        let python = std::env::var_os("CVGNOME_CONTRACT_PYTHON")
            .expect("run this integration test through scripts/check_native_engine_contract.py");
        let helper = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../engine/tests/native_opportunity_contract.py");
        let mut child = Command::new(python)
            .arg(helper)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .expect("could not start the Python RPC contract harness");
        child
            .stdin
            .take()
            .unwrap()
            .write_all(&serde_json::to_vec(&payloads).unwrap())
            .expect("could not send native-generated requests to the RPC harness");
        let output = child.wait_with_output().unwrap();
        assert!(
            output.status.success(),
            "native-to-engine contract failed:\n{}\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
    }

    #[test]
    fn opportunity_tracker_update_params_preserve_patch_semantics() {
        let opportunity_id = "11111111-1111-4111-8111-111111111111";
        let stage_only = opportunity_tracker_update_params(
            opportunity_id,
            1_800_000_000_003,
            Some("interviewing".to_string()),
            None,
        )
        .unwrap();
        assert_eq!(
            stage_only,
            json!({
                "opportunity_id": opportunity_id,
                "expected_tracker_updated_at_ms": 1_800_000_000_003_u64,
                "tracker_stage": "interviewing"
            })
        );
        assert!(stage_only.get("notes").is_none());

        let notes_only = opportunity_tracker_update_params(
            opportunity_id,
            1_800_000_000_003,
            None,
            Some("  First line\nSecond line  ".to_string()),
        )
        .unwrap();
        assert_eq!(notes_only["notes"], "First line\nSecond line");
        assert!(notes_only.get("tracker_stage").is_none());

        let clear_notes = opportunity_tracker_update_params(
            opportunity_id,
            1_800_000_000_003,
            None,
            Some("   ".to_string()),
        )
        .unwrap();
        assert_eq!(clear_notes["notes"], "");

        assert!(
            opportunity_tracker_update_params(opportunity_id, 1_800_000_000_003, None, None)
                .is_err()
        );
        assert!(
            opportunity_tracker_update_params(opportunity_id, 0, Some("applied".to_string()), None)
                .is_err()
        );
        assert!(
            opportunity_tracker_update_params(
                opportunity_id,
                1_800_000_000_003,
                Some("all".to_string()),
                None
            )
            .is_err()
        );
        assert!(
            opportunity_tracker_update_params(
                opportunity_id,
                1_800_000_000_003,
                None,
                Some("x".repeat(OPPORTUNITY_NOTES_LIMIT_CHARS + 1))
            )
            .is_err()
        );
        assert!(
            opportunity_tracker_update_params(
                opportunity_id,
                1_800_000_000_003,
                None,
                Some("private\u{0000}note".to_string())
            )
            .is_err()
        );
    }

    #[test]
    fn opportunity_tracker_command_arguments_are_camel_case() {
        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase", deny_unknown_fields)]
        struct CommandArguments {
            opportunity_id: String,
            expected_tracker_updated_at_ms: u64,
            tracker_stage: Option<String>,
            notes: Option<String>,
        }
        let arguments: CommandArguments = serde_json::from_value(json!({
            "opportunityId": "11111111-1111-4111-8111-111111111111",
            "expectedTrackerUpdatedAtMs": 1_800_000_000_003_u64,
            "trackerStage": "applied",
            "notes": null
        }))
        .unwrap();
        assert_eq!(arguments.expected_tracker_updated_at_ms, 1_800_000_000_003);
        assert_eq!(arguments.tracker_stage.as_deref(), Some("applied"));
        assert!(arguments.notes.is_none());
        assert_eq!(
            validated_opportunity_id(&arguments.opportunity_id).unwrap(),
            arguments.opportunity_id
        );
        assert!(
            serde_json::from_value::<CommandArguments>(json!({
                "opportunity_id": "11111111-1111-4111-8111-111111111111",
                "expected_tracker_updated_at_ms": 1_800_000_000_003_u64,
                "tracker_stage": "applied",
                "notes": null
            }))
            .is_err()
        );
        let _command = update_opportunity_tracker;
    }

    #[test]
    fn opportunity_detail_fixture_is_strict_and_serializes_snake_case() {
        let detail: OpportunityDetail =
            serde_json::from_value(opportunity_detail_fixture()).unwrap();
        let detail = detail.validated().unwrap();
        let serialized = serde_json::to_value(detail).unwrap();

        assert_eq!(serialized["match"]["contract"], "deterministic-local-v1");
        assert_eq!(
            serialized["opportunity"]["source_url"],
            "https://example.com/jobs/123"
        );
        assert!(serialized["opportunity"].get("sourceUrl").is_none());
        assert!(serialized.get("match_result").is_none());

        let mut with_extra = opportunity_detail_fixture();
        with_extra["opportunity"]["raw_job_text"] = json!("must not cross the bridge");
        assert!(serde_json::from_value::<OpportunityDetail>(with_extra).is_err());

        let mut invalid_stage = opportunity_detail_fixture();
        invalid_stage["opportunity"]["tracker_stage"] = json!("all");
        assert!(serde_json::from_value::<OpportunityDetail>(invalid_stage).is_err());
    }

    #[test]
    fn opportunity_receipts_enforce_scores_checksums_and_result_caps() {
        let mut invalid_score = opportunity_detail_fixture();
        invalid_score["match"]["score"] = json!(101);
        let detail: OpportunityDetail = serde_json::from_value(invalid_score).unwrap();
        assert!(detail.validated().is_err());

        let mut invalid_checksum = opportunity_detail_fixture();
        invalid_checksum["snapshot"]["checksum_sha256"] = json!("A".repeat(64));
        let detail: OpportunityDetail = serde_json::from_value(invalid_checksum).unwrap();
        assert!(detail.validated().is_err());

        let mut too_many_terms = opportunity_detail_fixture();
        too_many_terms["match"]["matched_terms"] =
            json!(vec!["product"; OPPORTUNITY_MATCHED_TERMS_LIMIT + 1]);
        let detail: OpportunityDetail = serde_json::from_value(too_many_terms).unwrap();
        assert!(detail.validated().is_err());

        let mut invalid_tracker_timestamp = opportunity_detail_fixture();
        invalid_tracker_timestamp["opportunity"]["tracker_updated_at_ms"] = json!(0);
        let detail: OpportunityDetail = serde_json::from_value(invalid_tracker_timestamp).unwrap();
        assert!(detail.validated().is_err());

        let mut invalid_notes = opportunity_detail_fixture();
        invalid_notes["opportunity"]["notes"] = json!("private\u{0000}note");
        let detail: OpportunityDetail = serde_json::from_value(invalid_notes).unwrap();
        assert!(detail.validated().is_err());

        let mut list_item_with_notes = opportunity_summary_fixture();
        list_item_with_notes["notes"] = json!("must remain detail-only");
        assert!(serde_json::from_value::<OpportunitySummary>(list_item_with_notes).is_err());

        let list: OpportunityListResult = serde_json::from_value(json!({
            "items": [opportunity_summary_fixture()],
            "total": 1
        }))
        .unwrap();
        assert_eq!(list.validated(0).unwrap().items.len(), 1);
    }

    #[test]
    fn opportunity_id_command_arguments_expect_camel_case() {
        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase", deny_unknown_fields)]
        struct CommandArguments {
            opportunity_id: String,
        }

        let arguments: CommandArguments = serde_json::from_value(json!({
            "opportunityId": "11111111-1111-4111-8111-111111111111"
        }))
        .unwrap();
        assert_eq!(
            validated_opportunity_id(&arguments.opportunity_id).unwrap(),
            arguments.opportunity_id
        );
        assert!(
            serde_json::from_value::<CommandArguments>(json!({
                "opportunity_id": "11111111-1111-4111-8111-111111111111"
            }))
            .is_err()
        );
    }

    #[test]
    fn opportunity_engine_errors_never_echo_job_text() {
        let sensitive = "secret role description phrase";
        for error in [
            format!("invalid_params: {sensitive}"),
            format!("opportunity_tracker_conflict: {sensitive}"),
            format!("some_new_engine_error: {sensitive}"),
        ] {
            let safe = safe_opportunity_error(error);
            assert!(!safe.contains(sensitive));
        }
        assert!(
            safe_opportunity_error("opportunity_tracker_conflict: stale".to_string())
                .starts_with("opportunity_tracker_conflict:")
        );
    }

    #[test]
    fn profile_history_command_arguments_and_engine_params_are_exact() {
        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase", deny_unknown_fields)]
        struct VersionArguments {
            profile_version_id: String,
        }
        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase", deny_unknown_fields)]
        struct DiffArguments {
            from_profile_version_id: String,
            to_profile_version_id: String,
        }
        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase", deny_unknown_fields)]
        struct RestoreArguments {
            source_profile_version_id: String,
            expected_parent_profile_version_id: String,
            request_id: String,
        }
        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase", deny_unknown_fields)]
        struct UpdateArguments {
            expected_parent_profile_version_id: String,
            request_id: String,
            patch: ProfileBasicsPatch,
        }

        let version_arguments: VersionArguments = serde_json::from_value(json!({
            "profileVersionId": PROFILE_VERSION_ONE_ID,
        }))
        .unwrap();
        assert_eq!(
            profile_version_identity_params(&version_arguments.profile_version_id),
            json!({"profile_version_id": PROFILE_VERSION_ONE_ID})
        );
        assert!(
            serde_json::from_value::<VersionArguments>(json!({
                "profile_version_id": PROFILE_VERSION_ONE_ID,
            }))
            .is_err()
        );

        let diff_arguments: DiffArguments = serde_json::from_value(json!({
            "fromProfileVersionId": PROFILE_VERSION_ONE_ID,
            "toProfileVersionId": PROFILE_VERSION_TWO_ID,
        }))
        .unwrap();
        assert_eq!(
            profile_version_diff_params(
                &diff_arguments.from_profile_version_id,
                &diff_arguments.to_profile_version_id,
            ),
            json!({
                "from_profile_version_id": PROFILE_VERSION_ONE_ID,
                "to_profile_version_id": PROFILE_VERSION_TWO_ID,
            })
        );

        let update_arguments: UpdateArguments = serde_json::from_value(json!({
            "expectedParentProfileVersionId": PROFILE_VERSION_ONE_ID,
            "requestId": PROFILE_UPDATE_REQUEST_ID,
            "patch": {"headline": null},
        }))
        .unwrap();
        assert_eq!(
            profile_basics_update_params(
                &update_arguments.expected_parent_profile_version_id,
                &update_arguments.request_id,
                &update_arguments.patch,
            ),
            json!({
                "expected_parent_profile_version_id": PROFILE_VERSION_ONE_ID,
                "request_id": PROFILE_UPDATE_REQUEST_ID,
                "patch": {"headline": null},
            })
        );
        assert!(
            serde_json::from_value::<UpdateArguments>(json!({
                "expected_parent_profile_version_id": PROFILE_VERSION_ONE_ID,
                "requestId": PROFILE_UPDATE_REQUEST_ID,
                "patch": {"headline": null},
            }))
            .is_err()
        );

        let restore_arguments: RestoreArguments = serde_json::from_value(json!({
            "sourceProfileVersionId": PROFILE_VERSION_ONE_ID,
            "expectedParentProfileVersionId": PROFILE_VERSION_TWO_ID,
            "requestId": PROFILE_RESTORE_REQUEST_ID,
        }))
        .unwrap();
        let (source_profile_version_id, expected_parent_profile_version_id) =
            validated_profile_restore_ids(
                &restore_arguments.source_profile_version_id,
                &restore_arguments.expected_parent_profile_version_id,
            )
            .unwrap();
        assert_eq!(
            profile_restore_params(
                &source_profile_version_id,
                &expected_parent_profile_version_id,
                &restore_arguments.request_id,
            ),
            json!({
                "source_profile_version_id": PROFILE_VERSION_ONE_ID,
                "expected_parent_profile_version_id": PROFILE_VERSION_TWO_ID,
                "request_id": PROFILE_RESTORE_REQUEST_ID,
            })
        );
        assert!(
            serde_json::from_value::<RestoreArguments>(json!({
                "source_profile_version_id": PROFILE_VERSION_ONE_ID,
                "expectedParentProfileVersionId": PROFILE_VERSION_TWO_ID,
                "requestId": PROFILE_RESTORE_REQUEST_ID,
            }))
            .is_err()
        );
        assert!(
            validated_profile_restore_ids(PROFILE_VERSION_TWO_ID, PROFILE_VERSION_TWO_ID).is_err()
        );
        assert_eq!(
            validated_profile_restore_request_id(PROFILE_RESTORE_REQUEST_ID).unwrap(),
            PROFILE_RESTORE_REQUEST_ID
        );
        assert!(
            validated_profile_restore_request_id(&PROFILE_RESTORE_REQUEST_ID.to_uppercase())
                .is_err()
        );
        assert!(validated_profile_restore_request_id("not-a-uuid").is_err());
        assert_eq!(profile_version_list_params(20), json!({"offset": 20}));
        assert!(validated_profile_version_offset(PROFILE_VERSION_LIST_OFFSET_LIMIT).is_ok());
        assert!(validated_profile_version_offset(PROFILE_VERSION_LIST_OFFSET_LIMIT + 1).is_err());
        assert!(validated_profile_review_version_id(PROFILE_VERSION_ONE_ID).is_ok());
        assert!(
            validated_profile_review_version_id(&PROFILE_VERSION_ONE_ID.to_uppercase()).is_err()
        );

        let _list_command = list_profile_versions;
        let _review_command = get_profile_review_version;
        let _basics_command = get_profile_basics;
        let _diff_command = diff_profile_versions;
        let _restore_command = restore_profile_version;
        let _update_command = update_profile_basics;
    }

    #[test]
    fn profile_work_command_arguments_and_engine_params_are_exact() {
        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase", deny_unknown_fields)]
        struct ListArguments {
            profile_version_id: String,
            offset: usize,
        }
        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase", deny_unknown_fields)]
        struct UpdateArguments {
            expected_parent_profile_version_id: String,
            request_id: String,
            operation: ProfileWorkOperation,
        }

        let list_arguments: ListArguments = serde_json::from_value(json!({
            "profileVersionId": PROFILE_VERSION_TWO_ID,
            "offset": 20,
        }))
        .unwrap();
        assert_eq!(
            profile_work_list_params(&list_arguments.profile_version_id, list_arguments.offset,),
            json!({
                "profile_version_id": PROFILE_VERSION_TWO_ID,
                "offset": 20,
            })
        );
        assert!(
            serde_json::from_value::<ListArguments>(json!({
                "profile_version_id": PROFILE_VERSION_TWO_ID,
                "offset": 0,
            }))
            .is_err()
        );

        let update_arguments: UpdateArguments = serde_json::from_value(json!({
            "expectedParentProfileVersionId": PROFILE_VERSION_ONE_ID,
            "requestId": PROFILE_UPDATE_REQUEST_ID,
            "operation": {
                "kind": "update",
                "entry_index": 2,
                "patch": {
                    "summary": null,
                    "highlights": ["Shipped entirely locally."],
                },
            },
        }))
        .unwrap();
        let operation = update_arguments.operation.validated().unwrap();
        assert_eq!(
            profile_work_update_params(
                &update_arguments.expected_parent_profile_version_id,
                &update_arguments.request_id,
                &operation,
            ),
            json!({
                "expected_parent_profile_version_id": PROFILE_VERSION_ONE_ID,
                "request_id": PROFILE_UPDATE_REQUEST_ID,
                "operation": {
                    "kind": "update",
                    "entry_index": 2,
                    "patch": {
                        "summary": null,
                        "highlights": ["Shipped entirely locally."],
                    },
                },
            })
        );
        assert!(
            serde_json::from_value::<UpdateArguments>(json!({
                "expected_parent_profile_version_id": PROFILE_VERSION_ONE_ID,
                "requestId": PROFILE_UPDATE_REQUEST_ID,
                "operation": {"kind": "remove", "entry_index": 0},
            }))
            .is_err()
        );
        assert!(
            serde_json::from_value::<ProfileWorkOperation>(json!({
                "kind": "remove",
                "entry_index": 0,
                "profile": {"must": "never cross the boundary"},
            }))
            .is_err()
        );
        assert!(validated_profile_work_offset(PROFILE_WORK_OFFSET_LIMIT).is_ok());
        assert!(validated_profile_work_offset(PROFILE_WORK_OFFSET_LIMIT + 1).is_err());

        let _list_command = list_profile_work_history;
        let _update_command = update_profile_work_history;
    }

    #[test]
    fn profile_work_page_is_strict_bounded_and_keeps_raw_indexes() {
        let fixture = profile_work_page_fixture();
        let page: ProfileWorkPage = serde_json::from_value(fixture.clone()).unwrap();
        let page = page.validated(PROFILE_VERSION_TWO_ID, 0).unwrap();
        assert_eq!(page.items.len(), 2);
        assert_eq!(page.items[0].entry_index, 0);
        assert_eq!(page.items[1].entry_index, 2);

        let mut incomplete = fixture.clone();
        incomplete["items"][0]["name"] = json!("");
        incomplete["items"][0]["position"] = json!("");
        let incomplete: ProfileWorkPage = serde_json::from_value(incomplete).unwrap();
        assert!(incomplete.validated(PROFILE_VERSION_TWO_ID, 0).is_ok());

        let mut whitespace_required = fixture.clone();
        whitespace_required["items"][0]["name"] = json!("   ");
        let whitespace_required: ProfileWorkPage =
            serde_json::from_value(whitespace_required).unwrap();
        assert!(
            whitespace_required
                .validated(PROFILE_VERSION_TWO_ID, 0)
                .is_err()
        );

        let mut missing_nullable = fixture.clone();
        missing_nullable["items"][0]
            .as_object_mut()
            .unwrap()
            .remove("end_date");
        assert!(serde_json::from_value::<ProfileWorkPage>(missing_nullable).is_err());

        let mut unknown = fixture.clone();
        unknown["items"][0]["private_notes"] = json!("must remain local to the engine");
        assert!(serde_json::from_value::<ProfileWorkPage>(unknown).is_err());

        for mutation in [
            |value: &mut Value| value["profile_version_id"] = json!(PROFILE_VERSION_ONE_ID),
            |value: &mut Value| value["version_number"] = json!(0),
            |value: &mut Value| value["limit"] = json!(19),
            |value: &mut Value| value["next_offset"] = json!(1),
            |value: &mut Value| value["items"][1]["entry_index"] = json!(0),
            |value: &mut Value| value["items"][0]["url"] = json!("http://127.0.0.1/work"),
            |value: &mut Value| value["items"][0]["highlights"] = json!(["   "]),
        ] {
            let mut invalid = fixture.clone();
            mutation(&mut invalid);
            let invalid: ProfileWorkPage = serde_json::from_value(invalid).unwrap();
            assert!(invalid.validated(PROFILE_VERSION_TWO_ID, 0).is_err());
        }

        let mut next_page = fixture;
        next_page["total_items"] = json!(3);
        next_page["next_offset"] = json!(2);
        let next_page: ProfileWorkPage = serde_json::from_value(next_page).unwrap();
        assert!(next_page.validated(PROFILE_VERSION_TWO_ID, 0).is_ok());
    }

    #[test]
    fn profile_version_list_receipt_is_strict_ordered_and_paginated() {
        let fixture = profile_version_list_fixture();
        let list: ProfileVersionList = serde_json::from_value(fixture.clone()).unwrap();
        let list = list.validated(0).unwrap();
        assert_eq!(list.items.len(), 2);
        assert_eq!(list.items[0].profile_version_id, PROFILE_VERSION_TWO_ID);

        let mut restored = fixture.clone();
        restored["items"][0]["source_kind"] = json!("local_restore");
        let restored: ProfileVersionList = serde_json::from_value(restored).unwrap();
        assert_eq!(
            restored.validated(0).unwrap().items[0].source_kind,
            ProfileReviewSourceKind::LocalRestore
        );

        let mut manual = fixture.clone();
        manual["items"][1]["source_kind"] = json!("manual_start");
        let manual: ProfileVersionList = serde_json::from_value(manual).unwrap();
        assert_eq!(
            manual.validated(0).unwrap().items[1].source_kind,
            ProfileReviewSourceKind::ManualStart
        );

        let empty: ProfileVersionList = serde_json::from_value(json!({
            "current_profile_version_id": null,
            "total_items": 0,
            "offset": 0,
            "limit": PROFILE_VERSION_LIST_LIMIT,
            "next_offset": null,
            "items": [],
        }))
        .unwrap();
        assert!(empty.validated(0).is_ok());

        let mut unknown = fixture.clone();
        unknown["canonical_profile"] = json!({"must": "never cross the boundary"});
        assert!(serde_json::from_value::<ProfileVersionList>(unknown).is_err());

        let mut missing_nullable = fixture.clone();
        missing_nullable
            .as_object_mut()
            .unwrap()
            .remove("next_offset");
        assert!(serde_json::from_value::<ProfileVersionList>(missing_nullable).is_err());

        for mutation in [
            |value: &mut Value| value["current_profile_version_id"] = Value::Null,
            |value: &mut Value| value["limit"] = json!(19),
            |value: &mut Value| value["next_offset"] = json!(2),
            |value: &mut Value| value["items"][0]["version_number"] = json!(1),
            |value: &mut Value| value["items"][0]["parent_profile_version_id"] = Value::Null,
        ] {
            let mut invalid = fixture.clone();
            mutation(&mut invalid);
            let invalid: ProfileVersionList = serde_json::from_value(invalid).unwrap();
            assert!(invalid.validated(0).is_err());
        }

        let mut corrected_clock = fixture;
        corrected_clock["items"][0]["created_at_ms"] = json!(1_700_000_000_000_u64);
        corrected_clock["items"][1]["created_at_ms"] = json!(1_800_000_000_000_u64);
        let corrected_clock: ProfileVersionList = serde_json::from_value(corrected_clock).unwrap();
        assert!(corrected_clock.validated(0).is_ok());

        let capped_items = (0..PROFILE_VERSION_LIST_LIMIT)
            .map(|index| ProfileVersionSummary {
                profile_version_id: Uuid::from_u128(100 + index as u128).to_string(),
                parent_profile_version_id: Some(PROFILE_VERSION_ONE_ID.to_string()),
                version_number: 21 - index as u64,
                created_at_ms: 1_800_000_000_000_u64 + index as u64,
                source_kind: ProfileReviewSourceKind::LocalEdit,
                name: "Ada Lovelace".to_string(),
                headline: None,
                renderable: true,
            })
            .collect::<Vec<_>>();
        let capped_page = ProfileVersionList {
            current_profile_version_id: Some(PROFILE_VERSION_TWO_ID.to_string()),
            total_items: 10_021,
            offset: PROFILE_VERSION_LIST_OFFSET_LIMIT as u64,
            limit: PROFILE_VERSION_LIST_LIMIT as u64,
            next_offset: None,
            items: capped_items,
        };
        assert!(
            capped_page
                .clone()
                .validated(PROFILE_VERSION_LIST_OFFSET_LIMIT)
                .is_ok()
        );
        let uncapped_engine_page = ProfileVersionList {
            next_offset: Some(10_020),
            ..capped_page
        };
        assert!(
            uncapped_engine_page
                .validated(PROFILE_VERSION_LIST_OFFSET_LIMIT)
                .is_err()
        );
    }

    #[test]
    fn profile_restore_receipt_binds_idempotency_source_and_lineage() {
        for created in [true, false] {
            let receipt: ProfileRestoreReceipt =
                serde_json::from_value(profile_restore_receipt_fixture(created)).unwrap();
            let receipt = receipt
                .validated(
                    PROFILE_VERSION_ONE_ID,
                    PROFILE_VERSION_TWO_ID,
                    PROFILE_RESTORE_REQUEST_ID,
                )
                .unwrap();
            assert_eq!(receipt.created, created);
            assert_eq!(receipt.profile_version_id, PROFILE_VERSION_THREE_ID);
            assert_eq!(receipt.restored_from_version_number, 1);
        }

        let fixture = profile_restore_receipt_fixture(true);
        let mut unknown = fixture.clone();
        unknown["canonical_profile"] = json!({"must": "remain inside the engine"});
        assert!(serde_json::from_value::<ProfileRestoreReceipt>(unknown).is_err());

        let mut missing = fixture.clone();
        missing
            .as_object_mut()
            .unwrap()
            .remove("restored_from_version_number");
        assert!(serde_json::from_value::<ProfileRestoreReceipt>(missing).is_err());

        for mutation in [
            |value: &mut Value| value["request_id"] = json!(PROFILE_UPDATE_REQUEST_ID),
            |value: &mut Value| value["parent_profile_version_id"] = json!(PROFILE_VERSION_ONE_ID),
            |value: &mut Value| {
                value["restored_from_profile_version_id"] = json!(PROFILE_VERSION_TWO_ID)
            },
            |value: &mut Value| value["profile_version_id"] = json!(PROFILE_VERSION_ONE_ID),
            |value: &mut Value| value["profile_version_id"] = json!(PROFILE_VERSION_TWO_ID),
            |value: &mut Value| value["restored_from_version_number"] = json!(0),
            |value: &mut Value| value["restored_from_version_number"] = json!(3),
            |value: &mut Value| value["version_number"] = json!(1),
            |value: &mut Value| value["created_at_ms"] = json!(0),
            |value: &mut Value| value["created_at_ms"] = json!(u64::MAX),
            |value: &mut Value| value["profile_name"] = json!(""),
            |value: &mut Value| value["profile_name"] = json!(" Ada Lovelace"),
        ] {
            let mut invalid = fixture.clone();
            mutation(&mut invalid);
            let invalid: ProfileRestoreReceipt = serde_json::from_value(invalid).unwrap();
            assert!(
                invalid
                    .validated(
                        PROFILE_VERSION_ONE_ID,
                        PROFILE_VERSION_TWO_ID,
                        PROFILE_RESTORE_REQUEST_ID,
                    )
                    .is_err()
            );
        }
    }

    #[test]
    fn profile_basics_receipt_is_allowlisted_and_nameless_profiles_are_repairable() {
        let fixture = profile_basics_fixture();
        let basics: ProfileBasics = serde_json::from_value(fixture.clone()).unwrap();
        assert!(basics.validated(PROFILE_VERSION_TWO_ID).is_ok());

        let mut nameless = fixture.clone();
        nameless["name"] = json!("");
        let nameless: ProfileBasics = serde_json::from_value(nameless).unwrap();
        assert!(nameless.validated(PROFILE_VERSION_TWO_ID).is_ok());

        let mut whitespace_name = fixture.clone();
        whitespace_name["name"] = json!("   ");
        let whitespace_name: ProfileBasics = serde_json::from_value(whitespace_name).unwrap();
        assert!(whitespace_name.validated(PROFILE_VERSION_TWO_ID).is_err());

        let mut private_url = fixture.clone();
        private_url["url"] = json!("http://127.0.0.1/profile");
        let private_url: ProfileBasics = serde_json::from_value(private_url).unwrap();
        assert!(private_url.validated(PROFILE_VERSION_TWO_ID).is_err());

        let mut missing_nullable = fixture.clone();
        missing_nullable.as_object_mut().unwrap().remove("phone");
        assert!(serde_json::from_value::<ProfileBasics>(missing_nullable).is_err());

        let mut unknown_location = fixture;
        unknown_location["location"]["postal_code"] = json!("SW1");
        assert!(serde_json::from_value::<ProfileBasics>(unknown_location).is_err());
    }

    #[test]
    fn profile_basics_patch_preserves_omitted_null_and_value_semantics() {
        let omitted: ProfileBasicsPatch = serde_json::from_value(json!({})).unwrap();
        assert_eq!(serde_json::to_value(&omitted).unwrap(), json!({}));
        assert!(omitted.validated().is_err());

        let clear: ProfileBasicsPatch = serde_json::from_value(json!({"headline": null})).unwrap();
        assert_eq!(
            serde_json::to_value(&clear).unwrap(),
            json!({"headline": null})
        );
        let clear = clear.validated().unwrap();
        assert_eq!(
            profile_basics_update_params(PROFILE_VERSION_ONE_ID, PROFILE_UPDATE_REQUEST_ID, &clear,)
                ["patch"],
            json!({"headline": null})
        );

        let normalized: ProfileBasicsPatch = serde_json::from_value(json!({
            "name": "  Ada Lovelace  ",
            "summary": " First\r\nSecond\tline\rThird\u{00a0}line ",
            "location": {"country_code": " gb "},
        }))
        .unwrap();
        let normalized = normalized.validated().unwrap();
        assert_eq!(
            serde_json::to_value(&normalized).unwrap(),
            json!({
                "name": "Ada Lovelace",
                "summary": "First\nSecond line\nThird line",
                "location": {"country_code": "GB"},
            })
        );

        for email in ["ada@example.com", "ada@bücher.example", "ada@example.com."] {
            let patch: ProfileBasicsPatch =
                serde_json::from_value(json!({"email": email})).unwrap();
            assert!(patch.validated().is_ok(), "rejected valid email: {email}");
        }

        for invalid in [
            json!({"name": null}),
            json!({"name": "   "}),
            json!({"location": null}),
            json!({"location": {}}),
            json!({"headline": ""}),
            json!({"email": "not-an-address"}),
            json!({"email": "a b@example.com"}),
            json!({"email": "a@example.com/path"}),
            json!({"email": "a@example.com:443"}),
            json!({"email": "a@-bad.example"}),
            json!({"email": "a@bad-.example"}),
            json!({"email": "a@exa_mple.com"}),
            json!({"email": "a@example.com.."}),
            json!({"phone": "+"}),
            json!({"url": ""}),
            json!({"summary": "safe\u{200b}hidden"}),
            json!({"headline": "unsafe\nline"}),
        ] {
            let patch: ProfileBasicsPatch = serde_json::from_value(invalid).unwrap();
            assert!(patch.validated().is_err());
        }

        assert!(
            serde_json::from_value::<ProfileBasicsPatch>(json!({
                "location": {"countryCode": "GB"}
            }))
            .is_err()
        );
        assert!(
            serde_json::from_value::<ProfileBasicsPatch>(json!({"canonical_profile": {}})).is_err()
        );
    }

    #[test]
    fn profile_basics_patch_urls_and_utf8_bounds_are_public_and_character_aware() {
        for url in [
            "http://example.com/profile",
            "https://例え.テスト.invalid/profile",
            "https://[2606:4700:4700::1111]/profile",
            "https://[64:ff9b::1]/profile",
            "https://[2001:1::1]/profile",
            "https://[2001:1::2]/profile",
            "https://[2001:3::1]/profile",
            "https://[2001:4:112::1]/profile",
            "https://[2001:20::1]/profile",
            "https://[2001:30::1]/profile",
        ] {
            let patch: ProfileBasicsPatch = serde_json::from_value(json!({"url": url})).unwrap();
            if url.contains("invalid") {
                assert!(patch.validated().is_err());
            } else {
                assert!(patch.validated().is_ok());
            }
        }
        for url in [
            "file:///tmp/profile",
            "https://user:secret@example.com/profile",
            "http://localhost/profile",
            "http://example/profile",
            "http://10.0.0.1/profile",
            "http://2130706433/profile",
            "http://127.1/profile",
            "http://169.254.1.2/profile",
            "http://[::1]/profile",
            "http://[::ffff:127.0.0.1]/profile",
            "http://[64:ff9b:1::1]/profile",
            "http://[100::1]/profile",
            "http://[2001::1]/profile",
            "http://[2001:2::1]/profile",
            "http://[2001:10::1]/profile",
            "http://[2001:db8::1]/profile",
            "http://[2002::1]/profile",
            "http://[3fff::1]/profile",
            "http://[fc00::1]/profile",
            "https://example.com\\@127.0.0.1/profile",
        ] {
            let patch: ProfileBasicsPatch = serde_json::from_value(json!({"url": url})).unwrap();
            assert!(patch.validated().is_err(), "accepted non-public URL: {url}");
        }

        let valid_unicode: ProfileBasicsPatch = serde_json::from_value(json!({
            "headline": "🦀".repeat(PROFILE_REVIEW_HEADLINE_LIMIT_CHARS),
        }))
        .unwrap();
        assert!(valid_unicode.validated().is_ok());
        let invalid_unicode: ProfileBasicsPatch = serde_json::from_value(json!({
            "headline": "🦀".repeat(PROFILE_REVIEW_HEADLINE_LIMIT_CHARS + 1),
        }))
        .unwrap();
        assert!(invalid_unicode.validated().is_err());
    }

    #[test]
    fn manual_profile_start_command_uses_camel_case_and_exact_engine_params() {
        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase", deny_unknown_fields)]
        struct StartArguments {
            expected_parent_profile_version_id: Value,
            request_id: String,
            patch: ProfileBasicsPatch,
        }

        let arguments: StartArguments = serde_json::from_value(json!({
            "expectedParentProfileVersionId": null,
            "requestId": PROFILE_UPDATE_REQUEST_ID,
            "patch": {
                "name": "  Ada Lovelace  ",
                "headline": " Systems engineer ",
                "summary": " Builds dependable analytical systems. ",
                "location": {"city": " London ", "country_code": " gb "},
            },
        }))
        .unwrap();
        validated_manual_profile_expected_parent(&arguments.expected_parent_profile_version_id)
            .unwrap();
        let request_id = validated_manual_profile_request_id(&arguments.request_id).unwrap();
        let patch = arguments.patch.validated_for_manual_start().unwrap();
        assert_eq!(
            manual_profile_start_params(&request_id, &patch).unwrap(),
            json!({
                "expected_parent_profile_version_id": null,
                "request_id": PROFILE_UPDATE_REQUEST_ID,
                "patch": {
                    "name": "Ada Lovelace",
                    "headline": "Systems engineer",
                    "summary": "Builds dependable analytical systems.",
                    "location": {"city": "London", "country_code": "GB"},
                },
            })
        );
        assert!(
            serde_json::from_value::<StartArguments>(json!({
                "expected_parent_profile_version_id": null,
                "requestId": PROFILE_UPDATE_REQUEST_ID,
                "patch": {"name": "Ada Lovelace"},
            }))
            .is_err()
        );
        assert!(
            serde_json::from_value::<StartArguments>(json!({
                "requestId": PROFILE_UPDATE_REQUEST_ID,
                "patch": {"name": "Ada Lovelace"},
            }))
            .is_err()
        );
        let _command = start_manual_profile;
    }

    #[test]
    fn manual_profile_start_requires_null_parent_name_canonical_uuid_and_bounded_request() {
        assert!(validated_manual_profile_expected_parent(&Value::Null).is_ok());
        for parent in [json!(PROFILE_VERSION_ONE_ID), json!({}), json!(false)] {
            assert!(validated_manual_profile_expected_parent(&parent).is_err());
        }
        assert_eq!(
            validated_manual_profile_request_id(PROFILE_UPDATE_REQUEST_ID).unwrap(),
            PROFILE_UPDATE_REQUEST_ID
        );
        for request_id in [
            "not-a-uuid",
            "DDDDDDDD-DDDD-4DDD-8DDD-DDDDDDDDDDDD",
            "dddddddddddd4ddd8ddddddddddddddd",
        ] {
            assert!(validated_manual_profile_request_id(request_id).is_err());
        }

        for invalid_patch in [
            json!({}),
            json!({"headline": "Systems engineer"}),
            json!({"name": null}),
            json!({"name": "   "}),
        ] {
            let patch: ProfileBasicsPatch = serde_json::from_value(invalid_patch).unwrap();
            assert!(patch.validated_for_manual_start().is_err());
        }
        let name_only: ProfileBasicsPatch =
            serde_json::from_value(json!({"name": "Ada Lovelace"})).unwrap();
        assert!(name_only.validated_for_manual_start().is_ok());

        let oversized: ProfileBasicsPatch = serde_json::from_value(json!({
            "name": "Ada Lovelace",
            "summary": "\\".repeat(MANUAL_PROFILE_START_REQUEST_LIMIT_BYTES),
        }))
        .unwrap();
        assert!(manual_profile_start_params(PROFILE_UPDATE_REQUEST_ID, &oversized).is_err());
    }

    #[test]
    fn manual_profile_start_receipt_is_strict_nullable_and_bound_to_patch() {
        let patch: ProfileBasicsPatch = serde_json::from_value(json!({
            "name": "Ada Lovelace",
            "headline": "Systems engineer",
            "summary": "Builds dependable analytical systems.",
            "location": {"city": "London", "country_code": "GB"},
        }))
        .unwrap();
        let patch = patch.validated_for_manual_start().unwrap();
        for created in [true, false] {
            let receipt: ManualProfileStartReceipt =
                serde_json::from_value(manual_profile_start_receipt_fixture(created)).unwrap();
            let receipt = receipt
                .validated(PROFILE_UPDATE_REQUEST_ID, &patch)
                .unwrap();
            assert_eq!(receipt.created, created);
            assert_eq!(receipt.parent_profile_version_id, None);
            assert_eq!(receipt.version_number, 1);
        }

        let no_op_null_patch: ProfileBasicsPatch = serde_json::from_value(json!({
            "name": "Ada Lovelace",
            "headline": null,
            "location": {"city": null},
        }))
        .unwrap();
        let no_op_null_patch = no_op_null_patch.validated_for_manual_start().unwrap();
        let mut no_op_null_receipt = manual_profile_start_receipt_fixture(true);
        no_op_null_receipt["changed_fields"] = json!(["name"]);
        no_op_null_receipt["headline"] = Value::Null;
        no_op_null_receipt["renderable"] = json!(false);
        let no_op_null_receipt: ManualProfileStartReceipt =
            serde_json::from_value(no_op_null_receipt).unwrap();
        assert!(
            no_op_null_receipt
                .validated(PROFILE_UPDATE_REQUEST_ID, &no_op_null_patch)
                .is_ok()
        );

        let fixture = manual_profile_start_receipt_fixture(true);
        let mut missing_parent = fixture.clone();
        missing_parent
            .as_object_mut()
            .unwrap()
            .remove("parent_profile_version_id");
        assert!(serde_json::from_value::<ManualProfileStartReceipt>(missing_parent).is_err());
        let mut missing_headline = fixture.clone();
        missing_headline.as_object_mut().unwrap().remove("headline");
        assert!(serde_json::from_value::<ManualProfileStartReceipt>(missing_headline).is_err());
        let mut unknown = fixture.clone();
        unknown["profile"] = json!({"must": "remain inside the engine"});
        assert!(serde_json::from_value::<ManualProfileStartReceipt>(unknown).is_err());

        for mutation in [
            |value: &mut Value| value["request_id"] = json!(PROFILE_RESTORE_REQUEST_ID),
            |value: &mut Value| value["profile_version_id"] = json!("not-a-uuid"),
            |value: &mut Value| value["parent_profile_version_id"] = json!(PROFILE_VERSION_TWO_ID),
            |value: &mut Value| value["version_number"] = json!(2),
            |value: &mut Value| value["created_at_ms"] = json!(JAVASCRIPT_MAX_SAFE_INTEGER + 1),
            |value: &mut Value| value["changed_fields"] = json!(["name", "headline", "summary"]),
            |value: &mut Value| {
                value["changed_fields"] = json!([
                    "headline",
                    "name",
                    "summary",
                    "location.city",
                    "location.country_code",
                ])
            },
            |value: &mut Value| value["profile_name"] = json!("Grace Hopper"),
            |value: &mut Value| value["headline"] = Value::Null,
            |value: &mut Value| value["renderable"] = json!(false),
        ] {
            let mut invalid = fixture.clone();
            mutation(&mut invalid);
            let invalid: ManualProfileStartReceipt = serde_json::from_value(invalid).unwrap();
            assert!(
                invalid
                    .validated(PROFILE_UPDATE_REQUEST_ID, &patch)
                    .is_err()
            );
        }
    }

    #[test]
    fn manual_profile_start_errors_are_safe_and_actionable() {
        assert_eq!(
            manual_profile_start_invalid_params("Phone must contain a usable number".to_string()),
            "invalid_params: Phone must contain a usable number"
        );
        assert_eq!(
            safe_manual_profile_start_error(
                "profile_start_conflict: sensitive engine detail".to_string()
            ),
            concat!(
                "profile_start_conflict: A profile already exists in this local workspace. ",
                "Open the existing profile instead."
            )
        );
        assert!(
            safe_manual_profile_start_error(
                "profile_start_request_conflict: sensitive engine detail".to_string()
            )
            .starts_with("profile_start_request_conflict:")
        );
        assert!(
            safe_manual_profile_start_error(
                "profile_start_preview_conflict: sensitive engine detail".to_string()
            )
            .starts_with("profile_start_preview_conflict:")
        );
        assert_eq!(
            safe_manual_profile_start_error(
                "vault_integrity_error: /private/sensitive/path".to_string()
            ),
            "vault_integrity_error: The local vault could not create the profile."
        );
        assert_eq!(
            safe_manual_profile_start_error("unexpected: secret".to_string()),
            "profile_start_error: The local engine could not create the profile."
        );
    }

    #[test]
    fn profile_work_add_and_patch_preserve_required_and_tri_state_fields() {
        let add: ProfileWorkOperation = serde_json::from_value(json!({
            "kind": "add",
            "entry": {
                "name": "  Analytical Engines  ",
                "position": "  Systems engineer  ",
                "url": null,
                "start_date": " 2021-01 ",
                "end_date": null,
                "summary": " First line\r\nSecond line ",
                "highlights": [" Built safe storage "],
                "location": null,
            },
        }))
        .unwrap();
        let add = add.validated().unwrap();
        assert_eq!(
            serde_json::to_value(&add).unwrap(),
            json!({
                "kind": "add",
                "entry": {
                    "name": "Analytical Engines",
                    "position": "Systems engineer",
                    "url": null,
                    "start_date": "2021-01",
                    "end_date": null,
                    "summary": "First line\nSecond line",
                    "highlights": ["Built safe storage"],
                    "location": null,
                },
            })
        );

        let update: ProfileWorkOperation = serde_json::from_value(json!({
            "kind": "update",
            "entry_index": 2,
            "patch": {
                "url": null,
                "summary": null,
                "highlights": [],
            },
        }))
        .unwrap();
        let update = update.validated().unwrap();
        assert_eq!(
            serde_json::to_value(&update).unwrap(),
            json!({
                "kind": "update",
                "entry_index": 2,
                "patch": {
                    "url": null,
                    "summary": null,
                    "highlights": [],
                },
            })
        );

        assert!(
            serde_json::from_value::<ProfileWorkOperation>(json!({
                "kind": "add",
                "entry": {
                    "name": "Analytical Engines",
                    "position": "Engineer",
                    "start_date": null,
                    "end_date": null,
                    "summary": null,
                    "highlights": [],
                    "location": null,
                },
            }))
            .is_err()
        );
        for invalid in [
            json!({"kind": "update", "entry_index": 0, "patch": {}}),
            json!({"kind": "update", "entry_index": 0, "patch": {"name": null}}),
            json!({"kind": "update", "entry_index": 0, "patch": {"position": "   "}}),
            json!({"kind": "update", "entry_index": 0, "patch": {"url": "http://localhost/work"}}),
            json!({"kind": "update", "entry_index": 0, "patch": {"start_date": "x".repeat(PROFILE_WORK_DATE_LIMIT_CHARS + 1)}}),
            json!({"kind": "update", "entry_index": 0, "patch": {"highlights": vec!["x"; PROFILE_WORK_HIGHLIGHT_LIMIT + 1]}}),
            json!({"kind": "remove", "entry_index": PROFILE_WORK_ENTRY_LIMIT}),
        ] {
            let operation: ProfileWorkOperation = serde_json::from_value(invalid).unwrap();
            assert!(operation.validated().is_err());
        }

        let oversized_highlights =
            vec!["x".repeat(PROFILE_WORK_HIGHLIGHT_LIMIT_CHARS); PROFILE_WORK_HIGHLIGHT_LIMIT];
        let operation: ProfileWorkOperation = serde_json::from_value(json!({
            "kind": "update",
            "entry_index": 0,
            "patch": {"highlights": oversized_highlights},
        }))
        .unwrap();
        assert!(operation.validated().is_err());
    }

    #[test]
    fn profile_version_diff_is_strict_bounded_and_internally_consistent() {
        let fixture = profile_version_diff_fixture();
        let difference: ProfileVersionDiff = serde_json::from_value(fixture.clone()).unwrap();
        let difference = difference
            .validated(PROFILE_VERSION_ONE_ID, PROFILE_VERSION_TWO_ID)
            .unwrap();
        assert_eq!(difference.total_changes, 2);
        assert_eq!(difference.section_changes.len(), 1);

        let mut repaired_name = fixture.clone();
        repaired_name["basic_changes"] = json!([{
            "field": "name",
            "label": "Name",
            "before": "",
            "after": "Ada Lovelace",
        }]);
        let repaired_name: ProfileVersionDiff = serde_json::from_value(repaired_name).unwrap();
        assert!(
            repaired_name
                .validated(PROFILE_VERSION_ONE_ID, PROFILE_VERSION_TWO_ID)
                .is_ok()
        );

        let mut unknown = fixture.clone();
        unknown["canonical_before"] = json!({});
        assert!(serde_json::from_value::<ProfileVersionDiff>(unknown).is_err());

        for mutation in [
            |value: &mut Value| value["total_changes"] = json!(1),
            |value: &mut Value| value["basic_changes"][0]["label"] = json!("Title"),
            |value: &mut Value| value["basic_changes"][0]["after"] = Value::Null,
            |value: &mut Value| value["section_changes"][0]["content_changed"] = json!(false),
            |value: &mut Value| value["section_changes"][0]["label"] = json!("Experience"),
        ] {
            let mut invalid = fixture.clone();
            mutation(&mut invalid);
            let invalid: ProfileVersionDiff = serde_json::from_value(invalid).unwrap();
            assert!(
                invalid
                    .validated(PROFILE_VERSION_ONE_ID, PROFILE_VERSION_TWO_ID)
                    .is_err()
            );
        }

        let same_version: ProfileVersionDiff = serde_json::from_value(json!({
            "from_version": profile_version_summary_fixture(PROFILE_VERSION_ONE_ID, None, 1),
            "to_version": profile_version_summary_fixture(PROFILE_VERSION_ONE_ID, None, 1),
            "basic_changes": [],
            "section_changes": [],
            "total_changes": 0,
        }))
        .unwrap();
        assert!(
            same_version
                .validated(PROFILE_VERSION_ONE_ID, PROFILE_VERSION_ONE_ID)
                .is_ok()
        );

        let mut invalid_field = fixture;
        invalid_field["basic_changes"][0]["field"] = json!("profiles");
        assert!(serde_json::from_value::<ProfileVersionDiff>(invalid_field).is_err());
    }

    #[test]
    fn profile_update_receipt_binds_idempotency_lineage_and_changed_fields() {
        let patch: ProfileBasicsPatch = serde_json::from_value(json!({
            "headline": "Systems engineer",
            "location": {"country_code": "GB"},
        }))
        .unwrap();
        let patch = patch.validated().unwrap();
        for created in [true, false] {
            let receipt: ProfileBasicsUpdateReceipt =
                serde_json::from_value(profile_update_receipt_fixture(created)).unwrap();
            let receipt = receipt
                .validated(PROFILE_VERSION_ONE_ID, PROFILE_UPDATE_REQUEST_ID, &patch)
                .unwrap();
            assert_eq!(receipt.created, created);
            assert_eq!(receipt.profile_version_id, PROFILE_VERSION_TWO_ID);
        }

        let unicode_patch: ProfileBasicsPatch =
            serde_json::from_value(json!({"headline": "Cafe\u{0301}"})).unwrap();
        let unicode_patch = unicode_patch.validated().unwrap();
        let mut normalized_receipt = profile_update_receipt_fixture(true);
        normalized_receipt["changed_fields"] = json!(["headline"]);
        normalized_receipt["headline"] = json!("Caf\u{00e9}");
        let normalized_receipt: ProfileBasicsUpdateReceipt =
            serde_json::from_value(normalized_receipt).unwrap();
        assert!(
            normalized_receipt
                .validated(
                    PROFILE_VERSION_ONE_ID,
                    PROFILE_UPDATE_REQUEST_ID,
                    &unicode_patch,
                )
                .is_ok()
        );

        let fixture = profile_update_receipt_fixture(true);
        let mut unknown = fixture.clone();
        unknown["profile"] = json!({"must": "remain inside engine"});
        assert!(serde_json::from_value::<ProfileBasicsUpdateReceipt>(unknown).is_err());

        for mutation in [
            |value: &mut Value| value["request_id"] = json!(PROFILE_VERSION_ONE_ID),
            |value: &mut Value| value["parent_profile_version_id"] = json!(PROFILE_VERSION_TWO_ID),
            |value: &mut Value| value["profile_version_id"] = json!(PROFILE_VERSION_ONE_ID),
            |value: &mut Value| value["changed_fields"] = json!([]),
            |value: &mut Value| value["changed_fields"] = json!(["email"]),
            |value: &mut Value| {
                value["changed_fields"] = json!(["location.country_code", "headline"])
            },
            |value: &mut Value| value["headline"] = Value::Null,
            |value: &mut Value| value["profile_name"] = json!(""),
        ] {
            let mut invalid = fixture.clone();
            mutation(&mut invalid);
            let invalid: ProfileBasicsUpdateReceipt = serde_json::from_value(invalid).unwrap();
            assert!(
                invalid
                    .validated(PROFILE_VERSION_ONE_ID, PROFILE_UPDATE_REQUEST_ID, &patch)
                    .is_err()
            );
        }
    }

    #[test]
    fn profile_work_receipt_binds_operation_lineage_and_changed_fields() {
        let operation: ProfileWorkOperation = serde_json::from_value(json!({
            "kind": "update",
            "entry_index": 2,
            "patch": {
                "summary": "Built dependable analytical systems.",
                "highlights": ["Reduced processing time by 40%."],
            },
        }))
        .unwrap();
        let operation = operation.validated().unwrap();
        for created in [true, false] {
            let receipt: ProfileWorkUpdateReceipt =
                serde_json::from_value(profile_work_update_receipt_fixture(created)).unwrap();
            let receipt = receipt
                .validated(
                    PROFILE_VERSION_ONE_ID,
                    PROFILE_UPDATE_REQUEST_ID,
                    &operation,
                )
                .unwrap();
            assert_eq!(receipt.created, created);
            assert_eq!(receipt.operation, ProfileWorkOperationKind::Update);
            assert_eq!(receipt.entry_index, 2);
        }

        let fixture = profile_work_update_receipt_fixture(true);
        let mut unknown = fixture.clone();
        unknown["canonical_profile"] = json!({"must": "remain inside the engine"});
        assert!(serde_json::from_value::<ProfileWorkUpdateReceipt>(unknown).is_err());

        for mutation in [
            |value: &mut Value| value["request_id"] = json!(PROFILE_VERSION_ONE_ID),
            |value: &mut Value| value["parent_profile_version_id"] = json!(PROFILE_VERSION_TWO_ID),
            |value: &mut Value| value["profile_version_id"] = json!(PROFILE_VERSION_ONE_ID),
            |value: &mut Value| value["operation"] = json!("remove"),
            |value: &mut Value| value["entry_index"] = json!(1),
            |value: &mut Value| value["changed_fields"] = json!([]),
            |value: &mut Value| value["changed_fields"] = json!(["url"]),
            |value: &mut Value| value["changed_fields"] = json!(["highlights", "summary"]),
            |value: &mut Value| value["work_entries"] = json!(PROFILE_WORK_ENTRY_LIMIT + 1),
            |value: &mut Value| value["profile_name"] = json!(""),
        ] {
            let mut invalid = fixture.clone();
            mutation(&mut invalid);
            let invalid: ProfileWorkUpdateReceipt = serde_json::from_value(invalid).unwrap();
            assert!(
                invalid
                    .validated(
                        PROFILE_VERSION_ONE_ID,
                        PROFILE_UPDATE_REQUEST_ID,
                        &operation
                    )
                    .is_err()
            );
        }

        let add_operation: ProfileWorkOperation = serde_json::from_value(json!({
            "kind": "add",
            "entry": {
                "name": "Analytical Engines",
                "position": "Systems engineer",
                "url": null,
                "start_date": null,
                "end_date": null,
                "summary": null,
                "highlights": [],
                "location": null,
            },
        }))
        .unwrap();
        let add_operation = add_operation.validated().unwrap();
        let mut add_receipt = fixture;
        add_receipt["operation"] = json!("add");
        add_receipt["entry_index"] = json!(1);
        add_receipt["changed_fields"] = json!(["name", "position"]);
        let add_receipt: ProfileWorkUpdateReceipt = serde_json::from_value(add_receipt).unwrap();
        assert!(
            add_receipt
                .validated(
                    PROFILE_VERSION_ONE_ID,
                    PROFILE_UPDATE_REQUEST_ID,
                    &add_operation,
                )
                .is_ok()
        );
    }

    #[test]
    fn profile_changes_commands_use_camel_case_and_exact_engine_params() {
        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase", deny_unknown_fields)]
        struct PreviewArguments {
            profile_version_id: String,
        }
        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase", deny_unknown_fields)]
        struct ApplyArguments {
            expected_parent_profile_version_id: String,
            request_id: String,
            operations: Vec<ProfileChangeOperation>,
        }

        let preview: PreviewArguments = serde_json::from_value(json!({
            "profileVersionId": PROFILE_VERSION_TWO_ID,
        }))
        .unwrap();
        assert_eq!(
            profile_changes_preview_params(&preview.profile_version_id),
            json!({"profile_version_id": PROFILE_VERSION_TWO_ID})
        );
        assert!(
            serde_json::from_value::<PreviewArguments>(json!({
                "profile_version_id": PROFILE_VERSION_TWO_ID,
            }))
            .is_err()
        );

        let suggestion_id = "a".repeat(64);
        let apply: ApplyArguments = serde_json::from_value(json!({
            "expectedParentProfileVersionId": PROFILE_VERSION_TWO_ID,
            "requestId": PROFILE_UPDATE_REQUEST_ID,
            "operations": [
                {
                    "kind": "update",
                    "section": "work",
                    "entry_index": 2,
                    "patch": {
                        "summary": null,
                        "highlights": ["Shipped entirely locally."],
                    },
                },
                {
                    "kind": "remove",
                    "section": "projects",
                    "entry_index": 4,
                },
                {
                    "kind": "merge",
                    "section": "skills",
                    "keep_entry_index": 6,
                    "remove_entry_index": 7,
                    "suggestion_id": suggestion_id,
                },
            ],
        }))
        .unwrap();
        let operations = validated_profile_change_operations(apply.operations).unwrap();
        assert_eq!(
            profile_changes_apply_params(
                &apply.expected_parent_profile_version_id,
                &apply.request_id,
                &operations,
            ),
            json!({
                "expected_parent_profile_version_id": PROFILE_VERSION_TWO_ID,
                "request_id": PROFILE_UPDATE_REQUEST_ID,
                "operations": [
                    {
                        "kind": "update",
                        "section": "work",
                        "entry_index": 2,
                        "patch": {
                            "summary": null,
                            "highlights": ["Shipped entirely locally."],
                        },
                    },
                    {
                        "kind": "remove",
                        "section": "projects",
                        "entry_index": 4,
                    },
                    {
                        "kind": "merge",
                        "section": "skills",
                        "keep_entry_index": 6,
                        "remove_entry_index": 7,
                        "suggestion_id": "a".repeat(64),
                    },
                ],
            })
        );
        assert!(
            serde_json::from_value::<ApplyArguments>(json!({
                "expected_parent_profile_version_id": PROFILE_VERSION_TWO_ID,
                "requestId": PROFILE_UPDATE_REQUEST_ID,
                "operations": [{
                    "kind": "remove",
                    "section": "work",
                    "entry_index": 0,
                }],
            }))
            .is_err()
        );
        assert!(
            serde_json::from_value::<ProfileChangeOperation>(json!({
                "kind": "remove",
                "section": "work",
                "entryIndex": 0,
            }))
            .is_err()
        );

        let _preview_command = preview_profile_changes;
        let _apply_command = apply_profile_changes;
    }

    #[test]
    fn profile_change_operations_are_strict_bounded_and_nonoverlapping() {
        assert!(validated_profile_change_operations(vec![]).is_err());

        let repeated = (0..=PROFILE_CHANGE_OPERATION_LIMIT)
            .map(|index| ProfileChangeOperation::Remove {
                section: ProfileChangeSection::Work,
                entry_index: index as u64,
            })
            .collect::<Vec<_>>();
        assert!(validated_profile_change_operations(repeated).is_err());

        let duplicate_target = serde_json::from_value::<Vec<ProfileChangeOperation>>(json!([
            {"kind": "remove", "section": "work", "entry_index": 2},
            {
                "kind": "update",
                "section": "work",
                "entry_index": 2,
                "patch": {"summary": "Changed"},
            },
        ]))
        .unwrap();
        assert!(validated_profile_change_operations(duplicate_target).is_err());

        for invalid in [
            json!({
                "kind": "merge",
                "section": "projects",
                "keep_entry_index": 2,
                "remove_entry_index": 2,
                "suggestion_id": "a".repeat(64),
            }),
            json!({
                "kind": "merge",
                "section": "projects",
                "keep_entry_index": 3,
                "remove_entry_index": 2,
                "suggestion_id": "a".repeat(64),
            }),
            json!({
                "kind": "merge",
                "section": "projects",
                "keep_entry_index": 2,
                "remove_entry_index": 3,
                "suggestion_id": "A".repeat(64),
            }),
            json!({
                "kind": "update",
                "section": "work",
                "entry_index": 2,
                "patch": {"keywords": ["Rust"]},
            }),
            json!({
                "kind": "update",
                "section": "skills",
                "entry_index": 2,
                "patch": {},
            }),
        ] {
            let operation: ProfileChangeOperation = serde_json::from_value(invalid).unwrap();
            assert!(validated_profile_change_operations(vec![operation]).is_err());
        }

        let normalized: ProfileChangeOperation = serde_json::from_value(json!({
            "kind": "update",
            "section": "education",
            "entry_index": 2,
            "patch": {
                "study_type": "  M.S.  ",
                "courses": [" Distributed Systems "],
            },
        }))
        .unwrap();
        assert_eq!(
            serde_json::to_value(validated_profile_change_operations(vec![normalized]).unwrap())
                .unwrap(),
            json!([{
                "kind": "update",
                "section": "education",
                "entry_index": 2,
                "patch": {
                    "study_type": "M.S.",
                    "courses": ["Distributed Systems"],
                },
            }])
        );

        let oversized = (0..PROFILE_CHANGE_OPERATION_LIMIT)
            .map(|entry_index| {
                serde_json::from_value::<ProfileChangeOperation>(json!({
                    "kind": "update",
                    "section": "projects",
                    "entry_index": entry_index,
                    "patch": {
                        "name": "n".repeat(PROFILE_PROJECT_NAME_LIMIT_CHARS),
                        "description": "d".repeat(PROFILE_PROJECT_DESCRIPTION_LIMIT_CHARS),
                        "url": format!(
                            "https://example.com/{}",
                            "u".repeat(PROFILE_PROJECT_URL_LIMIT_CHARS - 20)
                        ),
                        "start_date": "s".repeat(PROFILE_PROJECT_DATE_LIMIT_CHARS),
                        "end_date": "e".repeat(PROFILE_PROJECT_DATE_LIMIT_CHARS),
                        "highlights": vec![
                            "h".repeat(
                                PROFILE_PROJECT_HIGHLIGHT_TOTAL_LIMIT_CHARS
                                    / PROFILE_PROJECT_HIGHLIGHT_LIMIT
                            );
                            PROFILE_PROJECT_HIGHLIGHT_LIMIT
                        ],
                        "keywords": vec![
                            "k".repeat(
                                PROFILE_PROJECT_KEYWORD_TOTAL_LIMIT_CHARS
                                    / PROFILE_PROJECT_KEYWORD_LIMIT
                            );
                            PROFILE_PROJECT_KEYWORD_LIMIT
                        ],
                    },
                }))
                .unwrap()
            })
            .collect::<Vec<_>>();
        assert!(serde_json::to_vec(&oversized).unwrap().len() > PROFILE_CHANGE_REQUEST_LIMIT_BYTES);
        assert!(validated_profile_change_operations(oversized).is_err());
    }

    fn profile_changes_preview_fixture() -> Value {
        json!({
            "profile_version_id": PROFILE_VERSION_TWO_ID,
            "version_number": 2,
            "algorithm_version": 1,
            "suggestions": [
                {
                    "suggestion_id": "a".repeat(64),
                    "section": "work",
                    "keep_entry_index": 0,
                    "remove_entry_index": 1,
                    "reason_codes": ["matching_employer_role_and_dates"],
                    "merged_entry": {
                        "name": "Analytical Engines",
                        "position": "Systems engineer",
                        "url": "https://example.com/work",
                        "start_date": "2021-01",
                        "end_date": null,
                        "summary": "Built dependable analytical systems.",
                        "highlights": ["Reduced processing time by 40%."],
                        "location": "London",
                    },
                    "changed_fields": ["summary", "highlights"],
                },
                {
                    "suggestion_id": "b".repeat(64),
                    "section": "projects",
                    "keep_entry_index": 0,
                    "remove_entry_index": 1,
                    "reason_codes": ["matching_project_name_and_dates"],
                    "merged_entry": {
                        "name": "Local career workspace",
                        "description": "A private desktop career workspace.",
                        "url": "https://example.com/project",
                        "start_date": "2025-01",
                        "end_date": null,
                        "highlights": ["Kept customer data local."],
                        "keywords": ["Rust", "TypeScript"],
                    },
                    "changed_fields": ["description", "keywords"],
                },
                {
                    "suggestion_id": "c".repeat(64),
                    "section": "education",
                    "keep_entry_index": 0,
                    "remove_entry_index": 1,
                    "reason_codes": ["matching_institution_program_and_dates"],
                    "merged_entry": {
                        "institution": "University of London",
                        "study_type": "M.S.",
                        "area": "Computer Science",
                        "url": "https://example.com/education",
                        "start_date": "2018",
                        "end_date": "2020",
                        "score": null,
                        "courses": ["Distributed Systems"],
                    },
                    "changed_fields": ["area", "courses"],
                },
                {
                    "suggestion_id": "d".repeat(64),
                    "section": "skills",
                    "keep_entry_index": 0,
                    "remove_entry_index": 1,
                    "reason_codes": ["matching_skill_group"],
                    "merged_entry": {
                        "name": "Languages",
                        "level": null,
                        "keywords": ["Rust", "Python"],
                    },
                    "changed_fields": ["keywords"],
                },
            ],
            "truncated": false,
        })
    }

    #[test]
    fn profile_changes_preview_is_strict_typed_ordered_and_bounded() {
        let fixture = profile_changes_preview_fixture();
        let preview: ProfileChangesPreview = serde_json::from_value(fixture.clone()).unwrap();
        assert!(preview.validated(PROFILE_VERSION_TWO_ID).is_ok());

        let mut safety_truncated = fixture.clone();
        safety_truncated["truncated"] = json!(true);
        let safety_truncated: ProfileChangesPreview =
            serde_json::from_value(safety_truncated).unwrap();
        assert!(safety_truncated.validated(PROFILE_VERSION_TWO_ID).is_ok());

        let mut unknown = fixture.clone();
        unknown["suggestions"][0]["merged_entry"]["private_note"] =
            json!("must remain inside the engine");
        assert!(serde_json::from_value::<ProfileChangesPreview>(unknown).is_err());

        let mut missing_nullable = fixture.clone();
        missing_nullable["suggestions"][0]["merged_entry"]
            .as_object_mut()
            .unwrap()
            .remove("end_date");
        assert!(serde_json::from_value::<ProfileChangesPreview>(missing_nullable).is_err());

        for mutation in [
            |value: &mut Value| value["profile_version_id"] = json!(PROFILE_VERSION_ONE_ID),
            |value: &mut Value| value["algorithm_version"] = json!(2),
            |value: &mut Value| value["suggestions"][0]["suggestion_id"] = json!("A".repeat(64)),
            |value: &mut Value| {
                value["suggestions"][0]["reason_codes"] = json!(["matching_project_name_and_dates"])
            },
            |value: &mut Value| {
                value["suggestions"][0]["changed_fields"] = json!(["highlights", "summary"])
            },
            |value: &mut Value| {
                value["suggestions"][0]["keep_entry_index"] = json!(1);
                value["suggestions"][0]["remove_entry_index"] = json!(0);
            },
            |value: &mut Value| {
                let mut duplicate = value["suggestions"][0].clone();
                duplicate["suggestion_id"] = json!("b".repeat(64));
                value["suggestions"][1] = duplicate;
            },
            |value: &mut Value| value["suggestions"].as_array_mut().unwrap().reverse(),
        ] {
            let mut invalid = fixture.clone();
            mutation(&mut invalid);
            let invalid: ProfileChangesPreview = serde_json::from_value(invalid).unwrap();
            assert!(invalid.validated(PROFILE_VERSION_TWO_ID).is_err());
        }

        let mut too_many = fixture;
        let work = too_many["suggestions"][0].clone();
        too_many["suggestions"] = json!(
            (0..=PROFILE_CHANGE_SUGGESTION_LIMIT)
                .map(|index| {
                    let mut suggestion = work.clone();
                    suggestion["suggestion_id"] = json!(format!("{index:064x}"));
                    suggestion["keep_entry_index"] = json!((index * 2) as u64);
                    suggestion["remove_entry_index"] = json!((index * 2 + 1) as u64);
                    suggestion
                })
                .collect::<Vec<_>>()
        );
        let too_many: ProfileChangesPreview = serde_json::from_value(too_many).unwrap();
        assert!(too_many.validated(PROFILE_VERSION_TWO_ID).is_err());
    }

    #[test]
    fn profile_changes_receipt_is_strict_and_matches_the_batch() {
        let operations = validated_profile_change_operations(
            serde_json::from_value(json!([
                {"kind": "remove", "section": "work", "entry_index": 0},
                {
                    "kind": "merge",
                    "section": "projects",
                    "keep_entry_index": 1,
                    "remove_entry_index": 2,
                    "suggestion_id": "a".repeat(64),
                },
            ]))
            .unwrap(),
        )
        .unwrap();
        let fixture = json!({
            "request_id": PROFILE_UPDATE_REQUEST_ID,
            "profile_version_id": PROFILE_VERSION_THREE_ID,
            "parent_profile_version_id": PROFILE_VERSION_TWO_ID,
            "version_number": 3,
            "created_at_ms": 1_800_000_000_003_u64,
            "created": true,
            "operation_count": 2,
            "changed_sections": ["work", "projects"],
            "section_counts": {
                "work": 1,
                "projects": 2,
                "education": 3,
                "skills": 4,
            },
            "profile_name": "Ada Lovelace",
            "renderable": true,
        });

        for created in [true, false] {
            let mut receipt = fixture.clone();
            receipt["created"] = json!(created);
            let receipt: ProfileChangesApplyReceipt = serde_json::from_value(receipt).unwrap();
            assert!(
                receipt
                    .validated(
                        PROFILE_VERSION_TWO_ID,
                        PROFILE_UPDATE_REQUEST_ID,
                        &operations,
                    )
                    .is_ok()
            );
        }

        let mut missing_count = fixture.clone();
        missing_count["section_counts"]
            .as_object_mut()
            .unwrap()
            .remove("skills");
        assert!(serde_json::from_value::<ProfileChangesApplyReceipt>(missing_count).is_err());

        let mut unknown = fixture.clone();
        unknown["canonical_profile"] = json!({"must": "remain inside the engine"});
        assert!(serde_json::from_value::<ProfileChangesApplyReceipt>(unknown).is_err());

        for mutation in [
            |value: &mut Value| value["request_id"] = json!(PROFILE_VERSION_ONE_ID),
            |value: &mut Value| value["profile_version_id"] = json!(PROFILE_VERSION_TWO_ID),
            |value: &mut Value| value["parent_profile_version_id"] = json!(PROFILE_VERSION_ONE_ID),
            |value: &mut Value| value["operation_count"] = json!(1),
            |value: &mut Value| value["changed_sections"] = json!(["projects", "work"]),
            |value: &mut Value| {
                value["section_counts"]["work"] = json!(PROFILE_WORK_ENTRY_LIMIT + 1)
            },
            |value: &mut Value| value["profile_name"] = json!(""),
        ] {
            let mut invalid = fixture.clone();
            mutation(&mut invalid);
            let invalid: ProfileChangesApplyReceipt = serde_json::from_value(invalid).unwrap();
            assert!(
                invalid
                    .validated(
                        PROFILE_VERSION_TWO_ID,
                        PROFILE_UPDATE_REQUEST_ID,
                        &operations,
                    )
                    .is_err()
            );
        }

        assert_eq!(
            safe_profile_changes_error(
                "profile_changes_conflict: sensitive engine detail".to_string()
            ),
            concat!(
                "profile_changes_conflict: This profile changed since you began reviewing it. ",
                "Reload the latest profile and review your queued changes."
            )
        );
        assert_eq!(
            safe_profile_changes_error(
                "profile_changes_request_conflict: sensitive engine detail".to_string()
            ),
            concat!(
                "profile_changes_request_conflict: That apply request was already used for different changes. ",
                "Discard the queued changes and try again."
            )
        );
        assert_eq!(
            safe_profile_changes_error("vault_integrity_error: sensitive path".to_string()),
            "vault_integrity_error: The local vault could not complete the profile changes request."
        );
    }

    #[test]
    fn profile_section_commands_use_camel_case_and_exact_engine_params() {
        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase", deny_unknown_fields)]
        struct ListArguments {
            profile_version_id: String,
            offset: usize,
        }
        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase", deny_unknown_fields)]
        struct ProjectUpdateArguments {
            expected_parent_profile_version_id: String,
            request_id: String,
            operation: ProfileProjectsOperation,
        }
        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase", deny_unknown_fields)]
        struct EducationUpdateArguments {
            expected_parent_profile_version_id: String,
            request_id: String,
            operation: ProfileEducationOperation,
        }
        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase", deny_unknown_fields)]
        struct SkillsUpdateArguments {
            expected_parent_profile_version_id: String,
            request_id: String,
            operation: ProfileSkillsOperation,
        }

        let list: ListArguments = serde_json::from_value(json!({
            "profileVersionId": PROFILE_VERSION_TWO_ID,
            "offset": 20,
        }))
        .unwrap();
        assert_eq!(
            profile_section_list_params(&list.profile_version_id, list.offset),
            json!({"profile_version_id": PROFILE_VERSION_TWO_ID, "offset": 20})
        );
        assert!(
            serde_json::from_value::<ListArguments>(json!({
                "profile_version_id": PROFILE_VERSION_TWO_ID,
                "offset": 0,
            }))
            .is_err()
        );

        let projects: ProjectUpdateArguments = serde_json::from_value(json!({
            "expectedParentProfileVersionId": PROFILE_VERSION_ONE_ID,
            "requestId": PROFILE_UPDATE_REQUEST_ID,
            "operation": {
                "kind": "update",
                "entry_index": 2,
                "patch": {"description": null, "keywords": []},
            },
        }))
        .unwrap();
        let project_operation = projects.operation.validated().unwrap();
        assert_eq!(
            profile_section_update_params(
                &projects.expected_parent_profile_version_id,
                &projects.request_id,
                &project_operation,
            ),
            json!({
                "expected_parent_profile_version_id": PROFILE_VERSION_ONE_ID,
                "request_id": PROFILE_UPDATE_REQUEST_ID,
                "operation": {
                    "kind": "update",
                    "entry_index": 2,
                    "patch": {"description": null, "keywords": []},
                },
            })
        );

        let education: EducationUpdateArguments = serde_json::from_value(json!({
            "expectedParentProfileVersionId": PROFILE_VERSION_ONE_ID,
            "requestId": PROFILE_UPDATE_REQUEST_ID,
            "operation": {
                "kind": "update",
                "entry_index": 3,
                "patch": {"study_type": "M.S.", "courses": null},
            },
        }))
        .unwrap();
        let education_operation = education.operation.validated().unwrap();
        assert_eq!(
            profile_section_update_params(
                &education.expected_parent_profile_version_id,
                &education.request_id,
                &education_operation,
            )["operation"],
            json!({
                "kind": "update",
                "entry_index": 3,
                "patch": {"study_type": "M.S.", "courses": null},
            })
        );

        let skills: SkillsUpdateArguments = serde_json::from_value(json!({
            "expectedParentProfileVersionId": PROFILE_VERSION_ONE_ID,
            "requestId": PROFILE_UPDATE_REQUEST_ID,
            "operation": {
                "kind": "update",
                "entry_index": 4,
                "patch": {"level": null, "keywords": ["Rust"]},
            },
        }))
        .unwrap();
        let skills_operation = skills.operation.validated().unwrap();
        assert_eq!(
            profile_section_update_params(
                &skills.expected_parent_profile_version_id,
                &skills.request_id,
                &skills_operation,
            )["operation"],
            json!({
                "kind": "update",
                "entry_index": 4,
                "patch": {"level": null, "keywords": ["Rust"]},
            })
        );

        assert!(validated_profile_section_offset(PROFILE_SECTION_OFFSET_LIMIT, "Skills").is_ok());
        assert!(
            validated_profile_section_offset(PROFILE_SECTION_OFFSET_LIMIT + 1, "Skills").is_err()
        );
        assert_eq!(
            validated_profile_section_request_id(PROFILE_UPDATE_REQUEST_ID, "Skills").unwrap(),
            PROFILE_UPDATE_REQUEST_ID
        );
        assert!(
            validated_profile_section_request_id(
                &PROFILE_UPDATE_REQUEST_ID.to_uppercase(),
                "Skills"
            )
            .is_err()
        );

        let _list_projects_command = list_profile_projects;
        let _update_projects_command = update_profile_projects;
        let _list_education_command = list_profile_education;
        let _update_education_command = update_profile_education;
        let _list_skills_command = list_profile_skills;
        let _update_skills_command = update_profile_skills;
    }

    #[test]
    fn profile_section_pages_are_strict_bounded_and_keep_raw_indexes() {
        let projects: ProfileProjectsPage =
            serde_json::from_value(profile_projects_page_fixture()).unwrap();
        let projects = projects.validated(PROFILE_VERSION_TWO_ID, 0).unwrap();
        assert_eq!(projects.items[1].entry_index, 2);

        let education: ProfileEducationPage =
            serde_json::from_value(profile_education_page_fixture()).unwrap();
        let education = education.validated(PROFILE_VERSION_TWO_ID, 0).unwrap();
        assert_eq!(education.items[1].entry_index, 3);
        assert!(education.items[1].institution.is_empty());

        let skills: ProfileSkillsPage =
            serde_json::from_value(profile_skills_page_fixture()).unwrap();
        let skills = skills.validated(PROFILE_VERSION_TWO_ID, 0).unwrap();
        assert_eq!(skills.items[1].entry_index, 4);
        assert!(skills.items[1].name.is_empty());

        let mut missing_nullable = profile_projects_page_fixture();
        missing_nullable["items"][0]
            .as_object_mut()
            .unwrap()
            .remove("description");
        assert!(serde_json::from_value::<ProfileProjectsPage>(missing_nullable).is_err());

        let mut private = profile_education_page_fixture();
        private["items"][0]["private_note"] = json!("must remain in the engine");
        assert!(serde_json::from_value::<ProfileEducationPage>(private).is_err());

        let mut wrong_order = profile_skills_page_fixture();
        wrong_order["items"][1]["entry_index"] = json!(1);
        let wrong_order: ProfileSkillsPage = serde_json::from_value(wrong_order).unwrap();
        assert!(wrong_order.validated(PROFILE_VERSION_TWO_ID, 0).is_err());

        let mut wrong_section = profile_projects_page_fixture();
        wrong_section["section"] = json!("skills");
        let wrong_section: ProfileProjectsPage = serde_json::from_value(wrong_section).unwrap();
        assert!(wrong_section.validated(PROFILE_VERSION_TWO_ID, 0).is_err());

        let mut local_url = profile_projects_page_fixture();
        local_url["items"][0]["url"] = json!("http://127.0.0.1/project");
        let local_url: ProfileProjectsPage = serde_json::from_value(local_url).unwrap();
        assert!(local_url.validated(PROFILE_VERSION_TWO_ID, 0).is_err());

        let mut too_many_courses = profile_education_page_fixture();
        too_many_courses["items"][0]["courses"] =
            json!(vec!["Course"; PROFILE_EDUCATION_COURSE_LIMIT + 1]);
        let too_many_courses: ProfileEducationPage =
            serde_json::from_value(too_many_courses).unwrap();
        assert!(
            too_many_courses
                .validated(PROFILE_VERSION_TWO_ID, 0)
                .is_err()
        );

        let huge_item = json!({
            "entry_index": 0,
            "name": "🦀".repeat(PROFILE_PROJECT_NAME_LIMIT_CHARS),
            "description": "🦀".repeat(PROFILE_PROJECT_DESCRIPTION_LIMIT_CHARS),
            "url": null,
            "start_date": "🦀".repeat(PROFILE_PROJECT_DATE_LIMIT_CHARS),
            "end_date": "🦀".repeat(PROFILE_PROJECT_DATE_LIMIT_CHARS),
            "highlights": vec![
                "🦀".repeat(PROFILE_PROJECT_HIGHLIGHT_TOTAL_LIMIT_CHARS / PROFILE_PROJECT_HIGHLIGHT_LIMIT);
                PROFILE_PROJECT_HIGHLIGHT_LIMIT
            ],
            "keywords": vec![
                "🦀".repeat(PROFILE_PROJECT_KEYWORD_TOTAL_LIMIT_CHARS / PROFILE_PROJECT_KEYWORD_LIMIT);
                PROFILE_PROJECT_KEYWORD_LIMIT
            ],
        });
        let huge_items = (0..PROFILE_SECTION_PAGE_LIMIT)
            .map(|index| {
                let mut item = huge_item.clone();
                item["entry_index"] = json!(index);
                item
            })
            .collect::<Vec<_>>();
        let oversized: ProfileProjectsPage = serde_json::from_value(json!({
            "profile_version_id": PROFILE_VERSION_TWO_ID,
            "section": "projects",
            "version_number": 2,
            "total_items": PROFILE_SECTION_PAGE_LIMIT,
            "offset": 0,
            "limit": PROFILE_SECTION_PAGE_LIMIT,
            "next_offset": null,
            "items": huge_items,
        }))
        .unwrap();
        assert!(oversized.validated(PROFILE_VERSION_TWO_ID, 0).is_err());
    }

    #[test]
    fn profile_section_operations_are_strict_normalized_and_preserve_tri_state() {
        let project_add: ProfileProjectsOperation = serde_json::from_value(json!({
            "kind": "add",
            "entry": {
                "name": "  Analytical Engine  ",
                "description": " First line\r\nSecond line ",
                "url": null,
                "start_date": " 1842 ",
                "end_date": null,
                "highlights": [" First algorithm "],
                "keywords": [],
            },
        }))
        .unwrap();
        assert_eq!(
            serde_json::to_value(project_add.validated().unwrap()).unwrap(),
            json!({
                "kind": "add",
                "entry": {
                    "name": "Analytical Engine",
                    "description": "First line\nSecond line",
                    "url": null,
                    "start_date": "1842",
                    "end_date": null,
                    "highlights": ["First algorithm"],
                    "keywords": [],
                },
            })
        );

        let project_patch: ProfileProjectsOperation = serde_json::from_value(json!({
            "kind": "update",
            "entry_index": 2,
            "patch": {"description": null, "highlights": null, "keywords": []},
        }))
        .unwrap();
        assert_eq!(
            serde_json::to_value(project_patch.validated().unwrap()).unwrap()["patch"],
            json!({"description": null, "highlights": null, "keywords": []})
        );

        let education_add: ProfileEducationOperation = serde_json::from_value(json!({
            "kind": "add",
            "entry": {
                "institution": " University of London ",
                "study_type": null,
                "area": " Mathematics ",
                "url": null,
                "start_date": null,
                "end_date": null,
                "score": null,
                "courses": [],
            },
        }))
        .unwrap();
        assert_eq!(
            serde_json::to_value(education_add.validated().unwrap()).unwrap()["entry"]["institution"],
            "University of London"
        );

        let education_patch: ProfileEducationOperation = serde_json::from_value(json!({
            "kind": "update",
            "entry_index": 3,
            "patch": {"study_type": null, "courses": null},
        }))
        .unwrap();
        assert_eq!(
            serde_json::to_value(education_patch.validated().unwrap()).unwrap()["patch"],
            json!({"study_type": null, "courses": null})
        );

        let skill_add: ProfileSkillsOperation = serde_json::from_value(json!({
            "kind": "add",
            "entry": {
                "name": " Core ",
                "level": null,
                "keywords": [" Rust ", " SQLite "],
            },
        }))
        .unwrap();
        assert_eq!(
            serde_json::to_value(skill_add.validated().unwrap()).unwrap()["entry"]["keywords"],
            json!(["Rust", "SQLite"])
        );

        for invalid in [
            json!({"kind": "update", "entry_index": 0, "patch": {}}),
            json!({"kind": "update", "entry_index": 0, "patch": {"name": null}}),
            json!({"kind": "update", "entry_index": 0, "patch": {"url": "http://localhost/project"}}),
            json!({"kind": "update", "entry_index": PROFILE_SECTION_ENTRY_LIMIT, "patch": {"name": "Project"}}),
        ] {
            let operation: ProfileProjectsOperation = serde_json::from_value(invalid).unwrap();
            assert!(operation.validated().is_err());
        }
        assert!(
            serde_json::from_value::<ProfileProjectEntryInput>(json!({
                "name": "Project",
                "description": null,
                "url": null,
                "start_date": null,
                "end_date": null,
                "highlights": [],
            }))
            .is_err()
        );

        for invalid in [
            json!({
                "kind": "add",
                "entry": {
                    "institution": "University of London",
                    "study_type": null,
                    "area": "   ",
                    "url": null,
                    "start_date": null,
                    "end_date": null,
                    "score": null,
                    "courses": [],
                },
            }),
            json!({"kind": "update", "entry_index": 0, "patch": {"institution": null}}),
            json!({"kind": "update", "entry_index": 0, "patch": {"courses": vec!["x"; PROFILE_EDUCATION_COURSE_LIMIT + 1]}}),
            json!({"kind": "remove", "entry_index": PROFILE_SECTION_ENTRY_LIMIT}),
        ] {
            let operation: ProfileEducationOperation = serde_json::from_value(invalid).unwrap();
            assert!(operation.validated().is_err());
        }

        for invalid in [
            json!({"kind": "add", "entry": {"name": "Core", "level": null, "keywords": []}}),
            json!({"kind": "update", "entry_index": 0, "patch": {"keywords": null}}),
            json!({"kind": "update", "entry_index": 0, "patch": {"keywords": []}}),
            json!({"kind": "update", "entry_index": 0, "patch": {"keywords": vec!["x"; PROFILE_SKILL_KEYWORD_LIMIT + 1]}}),
        ] {
            let operation: ProfileSkillsOperation = serde_json::from_value(invalid).unwrap();
            assert!(operation.validated().is_err());
        }
        assert!(
            serde_json::from_value::<ProfileSkillsOperation>(json!({
                "kind": "remove",
                "entry_index": 0,
                "private": "must not cross the boundary",
            }))
            .is_err()
        );
    }

    #[test]
    fn profile_section_receipt_binds_section_operation_fields_and_bounds() {
        let operation: ProfileProjectsOperation = serde_json::from_value(json!({
            "kind": "update",
            "entry_index": 2,
            "patch": {
                "description": "A programmable mechanical computer.",
                "highlights": ["Published the first algorithm."],
            },
        }))
        .unwrap();
        let operation = operation.validated().unwrap();
        let fixture =
            profile_section_update_receipt_fixture("projects", &["description", "highlights"]);
        let receipt: ProfileSectionUpdateReceipt = serde_json::from_value(fixture.clone()).unwrap();
        let receipt = receipt
            .validated(
                ProfileEditableSection::Projects,
                PROFILE_VERSION_ONE_ID,
                PROFILE_UPDATE_REQUEST_ID,
                operation.kind(),
                operation.target_index(),
                &operation.present_fields(),
            )
            .unwrap();
        assert!(serde_json::to_vec(&receipt).unwrap().len() <= PROFILE_SECTION_RECEIPT_LIMIT_BYTES);

        let mut unknown = fixture.clone();
        unknown["canonical_profile"] = json!({"must": "remain inside the engine"});
        assert!(serde_json::from_value::<ProfileSectionUpdateReceipt>(unknown).is_err());

        for mutation in [
            |value: &mut Value| value["request_id"] = json!(PROFILE_VERSION_ONE_ID),
            |value: &mut Value| value["parent_profile_version_id"] = json!(PROFILE_VERSION_TWO_ID),
            |value: &mut Value| value["profile_version_id"] = json!(PROFILE_VERSION_ONE_ID),
            |value: &mut Value| value["section"] = json!("education"),
            |value: &mut Value| value["operation"] = json!("remove"),
            |value: &mut Value| value["entry_index"] = json!(1),
            |value: &mut Value| value["changed_fields"] = json!([]),
            |value: &mut Value| value["changed_fields"] = json!(["url"]),
            |value: &mut Value| value["changed_fields"] = json!(["highlights", "description"]),
            |value: &mut Value| value["changed_fields"] = json!(["description", "institution"]),
            |value: &mut Value| value["section_entries"] = json!(PROFILE_SECTION_ENTRY_LIMIT + 1),
            |value: &mut Value| value["profile_name"] = json!(""),
        ] {
            let mut invalid = fixture.clone();
            mutation(&mut invalid);
            let invalid: ProfileSectionUpdateReceipt = serde_json::from_value(invalid).unwrap();
            assert!(
                invalid
                    .validated(
                        ProfileEditableSection::Projects,
                        PROFILE_VERSION_ONE_ID,
                        PROFILE_UPDATE_REQUEST_ID,
                        operation.kind(),
                        operation.target_index(),
                        &operation.present_fields(),
                    )
                    .is_err()
            );
        }

        let add: ProfileSkillsOperation = serde_json::from_value(json!({
            "kind": "add",
            "entry": {"name": "Core", "level": null, "keywords": ["Rust"]},
        }))
        .unwrap();
        let add = add.validated().unwrap();
        let mut incomplete = profile_section_update_receipt_fixture("skills", &["name"]);
        incomplete["operation"] = json!("add");
        incomplete["entry_index"] = json!(4);
        let incomplete: ProfileSectionUpdateReceipt = serde_json::from_value(incomplete).unwrap();
        assert!(
            incomplete
                .validated(
                    ProfileEditableSection::Skills,
                    PROFILE_VERSION_ONE_ID,
                    PROFILE_UPDATE_REQUEST_ID,
                    add.kind(),
                    add.target_index(),
                    &add.present_fields(),
                )
                .is_err()
        );

        let education_add: ProfileEducationOperation = serde_json::from_value(json!({
            "kind": "add",
            "entry": {
                "institution": "University of London",
                "study_type": null,
                "area": "Mathematics",
                "url": null,
                "start_date": null,
                "end_date": null,
                "score": null,
                "courses": [],
            },
        }))
        .unwrap();
        let education_add = education_add.validated().unwrap();
        let mut incomplete_education =
            profile_section_update_receipt_fixture("education", &["institution"]);
        incomplete_education["operation"] = json!("add");
        let incomplete_education: ProfileSectionUpdateReceipt =
            serde_json::from_value(incomplete_education).unwrap();
        assert!(
            incomplete_education
                .validated(
                    ProfileEditableSection::Education,
                    PROFILE_VERSION_ONE_ID,
                    PROFILE_UPDATE_REQUEST_ID,
                    education_add.kind(),
                    education_add.target_index(),
                    &education_add.present_fields(),
                )
                .is_err()
        );

        let mut oversized = receipt;
        oversized.profile_name = "🦀".repeat(PROFILE_SECTION_RECEIPT_LIMIT_BYTES);
        assert!(
            oversized
                .validated(
                    ProfileEditableSection::Projects,
                    PROFILE_VERSION_ONE_ID,
                    PROFILE_UPDATE_REQUEST_ID,
                    operation.kind(),
                    operation.target_index(),
                    &operation.present_fields(),
                )
                .is_err()
        );
    }

    #[test]
    fn atomic_copy_preserves_unapproved_existing_file() {
        let directory = tempfile::tempdir().unwrap();
        let source = directory.path().join("source.pdf");
        let destination = directory.path().join("resume.pdf");
        fs::write(&source, b"new resume").unwrap();
        fs::write(&destination, b"existing resume").unwrap();
        let checksum = format!("{:x}", Sha256::digest(b"new resume"));

        assert!(atomic_copy_artifact(&source, &destination, 10, &checksum, false).is_err());
        assert_eq!(fs::read(destination).unwrap(), b"existing resume");
    }

    #[test]
    fn atomic_copy_never_publishes_an_artifact_with_a_mismatched_hash() {
        let directory = tempfile::tempdir().unwrap();
        let source = directory.path().join("managed.pdf");
        let destination = directory.path().join("tailored.pdf");
        fs::write(&source, b"tailored resume").unwrap();

        assert!(atomic_copy_artifact(&source, &destination, 15, &"0".repeat(64), false,).is_err());
        assert!(!destination.exists());
    }
}
