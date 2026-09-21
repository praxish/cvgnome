// SPDX-License-Identifier: MPL-2.0
//! A bounded local-only bridge for original career memories and review drafts.
//! Narratives are intentionally returned only by the explicit detail command.

use crate::{
    ENGINE_PROFILE_EDIT_TIMEOUT, ENGINE_PROFILE_REVIEW_TIMEOUT, EngineRuntime,
    deserialize_required_nullable, invalidate_status, is_profile_format_character,
    run_engine_request, validate_javascript_integer, validate_profile_basics_text,
    validate_profile_review_serialized_size, validate_uuid_receipt,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::HashSet;

const PAGE_LIMIT: usize = 10;
const OFFSET_LIMIT: usize = 10_000;
const TITLE_CHARS: usize = 160;
const NARRATIVE_CHARS: usize = 12_000;
const NARRATIVE_BYTES: usize = 48_000;
const EXCERPT_CHARS: usize = 240;
const PAGE_BYTES: usize = 32 * 1024;
const DETAIL_BYTES: usize = 80 * 1024;
const RECEIPT_BYTES: usize = 4 * 1024;

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub(crate) enum MemoryScope {
    Current,
    History,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct MemorySummary {
    id: String,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    title: Option<String>,
    excerpt: String,
    character_count: u64,
    created_at_ms: u64,
    is_previous_import_set: bool,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    review_item_id: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct MemoryPage {
    scope: MemoryScope,
    offset: u64,
    limit: u64,
    total_items: u64,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    next_offset: Option<u64>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    current_profile_version_id: Option<String>,
    retention_generation: u64,
    items: Vec<MemorySummary>,
}

// Deliberately repeat the small summary allowlist: flattening a nested record
// would prevent serde's deny_unknown_fields from guarding this privacy boundary.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct MemoryDetail {
    id: String,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    title: Option<String>,
    excerpt: String,
    character_count: u64,
    created_at_ms: u64,
    is_previous_import_set: bool,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    review_item_id: Option<String>,
    narrative: String,
    profile_version_id: String,
    retention_generation: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct MemorySaveReceipt {
    request_id: String,
    memory_id: String,
    created: bool,
    created_at_ms: u64,
    profile_version_id: String,
    retention_generation: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct MemoryProposalReceipt {
    request_id: String,
    memory_id: String,
    review_item_id: String,
    created: bool,
    created_at_ms: u64,
}

fn positive_integer(value: u64, label: &str) -> Result<(), String> {
    validate_javascript_integer(value, label)?;
    if value == 0 {
        return Err(format!("The local engine returned an invalid {label}"));
    }
    Ok(())
}

fn validate_narrative(value: &str) -> Result<(), String> {
    if value.trim().is_empty()
        || value.chars().count() > NARRATIVE_CHARS
        || value.len() > NARRATIVE_BYTES
        || value.chars().any(|character| {
            (character.is_control() && !matches!(character, '\r' | '\n' | '\t'))
                || is_profile_format_character(character)
        })
    {
        return Err("invalid_params: A memory needs text of at most 12,000 characters without unsupported control characters.".to_string());
    }
    Ok(())
}

fn normalize_optional_text(
    value: Option<&str>,
    label: &str,
    maximum: usize,
    multiline: bool,
) -> Result<Option<String>, String> {
    let value = value.map(str::trim).filter(|value| !value.is_empty());
    if let Some(value) = value {
        validate_profile_basics_text(value, label, maximum, multiline)
            .map_err(|_| format!("invalid_params: Check the memory {label} and try again."))?;
    }
    Ok(value.map(str::to_string))
}

fn validate_request_identity(
    request_id: &str,
    parent_id: &str,
    generation: u64,
) -> Result<(), String> {
    for value in [request_id, parent_id] {
        validate_uuid_receipt(value)
            .map_err(|_| "invalid_params: The memory request identifier is invalid.".to_string())?;
    }
    positive_integer(generation, "memory retention generation").map_err(|_| {
        "invalid_params: The memory import set is invalid. Reload the workspace.".to_string()
    })
}

fn list_params(scope: MemoryScope, offset: usize) -> Result<Value, String> {
    if offset > OFFSET_LIMIT {
        return Err("invalid_params: The memory page exceeds the local limit.".to_string());
    }
    Ok(json!({"scope": scope, "limit": PAGE_LIMIT, "offset": offset}))
}

fn get_params(memory_id: &str) -> Result<Value, String> {
    validate_uuid_receipt(memory_id)
        .map_err(|_| "invalid_params: The memory identifier is invalid.".to_string())?;
    Ok(json!({"memory_id": memory_id}))
}

fn save_params(
    request_id: &str,
    parent_id: &str,
    generation: u64,
    title: Option<&str>,
    narrative: &str,
) -> Result<Value, String> {
    validate_request_identity(request_id, parent_id, generation)?;
    validate_narrative(narrative)?;
    let title = normalize_optional_text(title, "title", TITLE_CHARS, false)?;
    let params = json!({
        "request_id": request_id,
        "expected_parent_profile_version_id": parent_id,
        "expected_retention_generation": generation,
        "title": title,
        "narrative": narrative,
    });
    validate_profile_review_serialized_size(&params, DETAIL_BYTES, "memory save request")?;
    Ok(params)
}

fn proposal_params(
    request_id: &str,
    memory_id: &str,
    parent_id: &str,
    generation: u64,
    name: &str,
    description: Option<&str>,
) -> Result<Value, String> {
    validate_request_identity(request_id, parent_id, generation)?;
    get_params(memory_id)?;
    let name = normalize_optional_text(Some(name), "project name", 200, false)?
        .ok_or("invalid_params: Give the project suggestion a name.")?;
    let description = normalize_optional_text(description, "project description", 600, true)?;
    let params = json!({
        "request_id": request_id,
        "memory_id": memory_id,
        "expected_parent_profile_version_id": parent_id,
        "expected_retention_generation": generation,
        "name": name,
        "description": description,
    });
    validate_profile_review_serialized_size(&params, RECEIPT_BYTES, "memory project request")?;
    Ok(params)
}

impl MemorySummary {
    fn validate(&self) -> Result<(), String> {
        validate_uuid_receipt(&self.id)?;
        if let Some(id) = &self.review_item_id {
            validate_uuid_receipt(id)?;
        }
        if let Some(title) = &self.title {
            validate_profile_basics_text(title, "memory title", TITLE_CHARS, false)?;
        }
        validate_profile_basics_text(&self.excerpt, "memory excerpt", EXCERPT_CHARS, false)?;
        positive_integer(self.created_at_ms, "memory timestamp")?;
        if !(1..=NARRATIVE_CHARS as u64).contains(&self.character_count) {
            return Err("The local engine returned an invalid memory character count".to_string());
        }
        Ok(())
    }
}

impl MemoryPage {
    fn validated(self, scope: MemoryScope, offset: usize) -> Result<Self, String> {
        positive_integer(self.retention_generation, "memory retention generation")?;
        validate_javascript_integer(self.total_items, "memory count")?;
        if let Some(id) = &self.current_profile_version_id {
            validate_uuid_receipt(id)?;
        } else if self.total_items != 0 {
            return Err("The local engine returned memories without a profile".to_string());
        }
        if self.scope != scope
            || self.offset != offset as u64
            || self.limit != PAGE_LIMIT as u64
            || self.items.len() > PAGE_LIMIT
        {
            return Err("The local engine returned a mismatched memory page".to_string());
        }
        let end = self
            .offset
            .checked_add(self.items.len() as u64)
            .ok_or("The local engine returned invalid memory pagination")?;
        if (!self.items.is_empty() && (self.offset >= self.total_items || end > self.total_items))
            || (self.items.is_empty() && self.offset < self.total_items)
            || self.next_offset
                != (end < self.total_items && end <= OFFSET_LIMIT as u64).then_some(end)
        {
            return Err("The local engine returned invalid memory pagination".to_string());
        }
        let mut ids = HashSet::new();
        for item in &self.items {
            item.validate()?;
            if !ids.insert(item.id.as_str())
                || item.is_previous_import_set != (scope == MemoryScope::History)
            {
                return Err("The local engine returned inconsistent memory items".to_string());
            }
        }
        validate_profile_review_serialized_size(&self, PAGE_BYTES, "memory list")?;
        Ok(self)
    }
}

impl MemoryDetail {
    fn validated(self, memory_id: &str) -> Result<Self, String> {
        MemorySummary {
            id: self.id.clone(),
            title: self.title.clone(),
            excerpt: self.excerpt.clone(),
            character_count: self.character_count,
            created_at_ms: self.created_at_ms,
            is_previous_import_set: self.is_previous_import_set,
            review_item_id: self.review_item_id.clone(),
        }
        .validate()?;
        validate_narrative(&self.narrative)?;
        validate_uuid_receipt(&self.profile_version_id)?;
        positive_integer(self.retention_generation, "memory retention generation")?;
        if self.id != memory_id || self.character_count != self.narrative.chars().count() as u64 {
            return Err("The local engine returned a mismatched memory detail".to_string());
        }
        validate_profile_review_serialized_size(&self, DETAIL_BYTES, "memory detail")?;
        Ok(self)
    }
}

impl MemorySaveReceipt {
    fn validated(self, request_id: &str, parent_id: &str, generation: u64) -> Result<Self, String> {
        for id in [&self.request_id, &self.memory_id, &self.profile_version_id] {
            validate_uuid_receipt(id)?;
        }
        positive_integer(self.created_at_ms, "memory save timestamp")?;
        positive_integer(self.retention_generation, "memory retention generation")?;
        if self.request_id != request_id
            || self.memory_id != request_id
            || self.profile_version_id != parent_id
            || self.retention_generation != generation
        {
            return Err("The local engine returned a mismatched memory save receipt".to_string());
        }
        validate_profile_review_serialized_size(&self, RECEIPT_BYTES, "memory save receipt")?;
        Ok(self)
    }
}

impl MemoryProposalReceipt {
    fn validated(self, request_id: &str, memory_id: &str) -> Result<Self, String> {
        for id in [&self.request_id, &self.memory_id, &self.review_item_id] {
            validate_uuid_receipt(id)?;
        }
        positive_integer(self.created_at_ms, "memory proposal timestamp")?;
        if self.request_id != request_id || self.memory_id != memory_id {
            return Err(
                "The local engine returned a mismatched memory proposal receipt".to_string(),
            );
        }
        validate_profile_review_serialized_size(&self, RECEIPT_BYTES, "memory proposal receipt")?;
        Ok(self)
    }
}

fn safe_error(error: String, writing: bool) -> String {
    let message = match error.split_once(':').map(|(code, _)| code.trim()) {
        Some("memory_request_conflict") => {
            "memory_request_conflict: This request ID was already used for a different memory action."
        }
        Some("memory_profile_conflict") | Some("profile_conflict") => {
            "memory_profile_conflict: Your profile changed. Reload memories before continuing."
        }
        Some("memory_stale_generation") => {
            "memory_stale_generation: The import set changed. Reload memories before continuing."
        }
        Some("memory_not_found") => {
            "memory_not_found: This memory is no longer available. Reload the list."
        }
        Some("memory_project_exists") => {
            "memory_project_exists: This memory already has a project suggestion. Open the review inbox."
        }
        Some("memory_evidence_missing") => {
            "memory_evidence_missing: CVGnome could not verify this memory's original evidence."
        }
        Some("memory_limit") => "memory_limit: The workspace has reached its local memory limit.",
        Some("profile_missing") => {
            "profile_missing: Create a profile before saving a career memory."
        }
        Some("invalid_params") | Some("memory_invalid") => {
            "invalid_params: Check the memory text and project details and try again."
        }
        Some("vault_busy") => "vault_busy: The local vault is busy; wait a moment and retry.",
        _ if writing => {
            "memory_uncertain: CVGnome could not confirm the result. Keep this entry and retry the same request."
        }
        _ => "memory_error: CVGnome could not load your memories. Try again.",
    };
    message.to_string()
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn list_memories(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    scope: MemoryScope,
    offset: usize,
) -> Result<MemoryPage, String> {
    let params = list_params(scope, offset)?;
    let _operation_guard = runtime.operation_gate.lock().await;
    run_engine_request::<MemoryPage>(&app, "memory.list", params, ENGINE_PROFILE_REVIEW_TIMEOUT)
        .await
        .and_then(|page| page.validated(scope, offset))
        .map_err(|error| safe_error(error, false))
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn get_memory(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    memory_id: String,
) -> Result<MemoryDetail, String> {
    let params = get_params(&memory_id)?;
    let _operation_guard = runtime.operation_gate.lock().await;
    run_engine_request::<MemoryDetail>(&app, "memory.get", params, ENGINE_PROFILE_REVIEW_TIMEOUT)
        .await
        .and_then(|detail| detail.validated(&memory_id))
        .map_err(|error| safe_error(error, false))
}

async fn finalize_save_attempt(
    runtime: &EngineRuntime,
    result: Result<MemorySaveReceipt, String>,
    request_id: &str,
    parent_id: &str,
    generation: u64,
) -> Result<MemorySaveReceipt, String> {
    // A malformed or lost reply can follow a successful transaction. Preserve
    // the exact request for retries and never retain a pre-write status cache.
    invalidate_status(runtime).await;
    result
        .and_then(|receipt| receipt.validated(request_id, parent_id, generation))
        .map_err(|error| safe_error(error, true))
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn save_memory(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    request_id: String,
    expected_parent_profile_version_id: String,
    expected_retention_generation: u64,
    title: Option<String>,
    narrative: String,
) -> Result<MemorySaveReceipt, String> {
    let params = save_params(
        &request_id,
        &expected_parent_profile_version_id,
        expected_retention_generation,
        title.as_deref(),
        &narrative,
    )?;
    let _operation_guard = runtime.operation_gate.lock().await;
    let result = run_engine_request::<MemorySaveReceipt>(
        &app,
        "memory.save",
        params,
        ENGINE_PROFILE_EDIT_TIMEOUT,
    )
    .await;
    finalize_save_attempt(
        &runtime,
        result,
        &request_id,
        &expected_parent_profile_version_id,
        expected_retention_generation,
    )
    .await
}

async fn finalize_proposal_attempt(
    runtime: &EngineRuntime,
    result: Result<MemoryProposalReceipt, String>,
    request_id: &str,
    memory_id: &str,
) -> Result<MemoryProposalReceipt, String> {
    invalidate_status(runtime).await;
    result
        .and_then(|receipt| receipt.validated(request_id, memory_id))
        .map_err(|error| safe_error(error, true))
}

#[tauri::command(rename_all = "camelCase")]
#[allow(clippy::too_many_arguments)]
pub(crate) async fn queue_memory_project(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    request_id: String,
    memory_id: String,
    expected_parent_profile_version_id: String,
    expected_retention_generation: u64,
    name: String,
    description: Option<String>,
) -> Result<MemoryProposalReceipt, String> {
    let params = proposal_params(
        &request_id,
        &memory_id,
        &expected_parent_profile_version_id,
        expected_retention_generation,
        &name,
        description.as_deref(),
    )?;
    let _operation_guard = runtime.operation_gate.lock().await;
    let result = run_engine_request::<MemoryProposalReceipt>(
        &app,
        "memory.propose_project",
        params,
        ENGINE_PROFILE_EDIT_TIMEOUT,
    )
    .await;
    finalize_proposal_attempt(&runtime, result, &request_id, &memory_id).await
}

#[cfg(test)]
mod tests {
    use super::*;
    const MEMORY: &str = "565ed1b4-3876-4560-8c46-a7a3b4fc0600";
    const PARENT: &str = "6ac523a8-cfad-4bbf-a6bb-2b7e0163a266";
    const REVIEW: &str = "e76ac26e-375c-4bd7-be2f-28915e4f5f06";
    const REQUEST: &str = "a25e0b67-120c-4a75-ae70-4871bf2f5dfb";

    fn summary_fixture() -> Value {
        json!({"id": MEMORY, "title": "Team project", "excerpt": "A story.",
            "character_count": 8, "created_at_ms": 1_800_000_000_000_u64,
            "is_previous_import_set": false, "review_item_id": null})
    }

    fn page_fixture() -> Value {
        json!({"scope": "current", "offset": 0, "limit": 10, "total_items": 1,
            "next_offset": null, "current_profile_version_id": PARENT,
            "retention_generation": 1, "items": [summary_fixture()]})
    }

    fn detail_fixture() -> Value {
        let mut value = summary_fixture();
        value["narrative"] = json!("A story.");
        value["profile_version_id"] = json!(PARENT);
        value["retention_generation"] = json!(1);
        value
    }

    fn save_fixture() -> Value {
        json!({"request_id": REQUEST, "memory_id": REQUEST, "created": true,
            "created_at_ms": 1_800_000_000_000_u64, "profile_version_id": PARENT,
            "retention_generation": 1})
    }

    fn proposal_fixture() -> Value {
        json!({"request_id": REQUEST, "memory_id": MEMORY, "review_item_id": REVIEW,
            "created": true, "created_at_ms": 1_800_000_000_000_u64})
    }

    #[test]
    fn memory_requests_pin_profile_and_generation_but_preserve_original_text() {
        let original = "  A story.\r\n\tWith detail.  ";
        let result = save_params(REQUEST, PARENT, 3, Some("  Title  "), original).unwrap();
        assert_eq!(
            result,
            json!({"request_id": REQUEST,
            "expected_parent_profile_version_id": PARENT, "expected_retention_generation": 3,
            "title": "Title", "narrative": original})
        );
        assert!(save_params("bad", PARENT, 3, None, original).is_err());
        assert!(save_params(REQUEST, "bad", 3, None, original).is_err());
        assert!(save_params(REQUEST, PARENT, 0, None, original).is_err());
        assert!(
            save_params(
                REQUEST,
                PARENT,
                crate::JAVASCRIPT_MAX_SAFE_INTEGER + 1,
                None,
                original
            )
            .is_err()
        );
        assert_eq!(
            save_params(REQUEST, PARENT, 1, Some("  "), original).unwrap()["title"],
            Value::Null
        );
        assert!(
            save_params(
                REQUEST,
                PARENT,
                1,
                Some(&"t".repeat(TITLE_CHARS + 1)),
                original
            )
            .is_err()
        );
    }

    #[test]
    fn memory_narrative_bounds_accept_unicode_and_reject_control_formats() {
        assert!(validate_narrative(&"🧙".repeat(NARRATIVE_CHARS)).is_ok());
        assert!(validate_narrative(&"x".repeat(NARRATIVE_CHARS + 1)).is_err());
        for invalid in [
            "",
            " \r\n\t ",
            "text\0",
            "text\u{7f}",
            "text\u{85}",
            "text\u{200b}",
            "text\u{202e}",
            "text\u{2066}",
        ] {
            assert!(validate_narrative(invalid).is_err());
        }
        assert!(validate_narrative("  text\r\n\t ").is_ok());
    }

    #[test]
    fn memory_projection_request_is_bounded_and_does_not_accept_extra_fact_fields() {
        let result = proposal_params(
            REQUEST,
            MEMORY,
            PARENT,
            1,
            " Project ",
            Some(" Description "),
        )
        .unwrap();
        assert_eq!(
            result,
            json!({"request_id": REQUEST, "memory_id": MEMORY,
            "expected_parent_profile_version_id": PARENT, "expected_retention_generation": 1,
            "name": "Project", "description": "Description"})
        );
        assert!(proposal_params(REQUEST, MEMORY, PARENT, 1, "", None).is_err());
        assert!(proposal_params(REQUEST, MEMORY, PARENT, 1, &"n".repeat(201), None).is_err());
        assert!(
            proposal_params(REQUEST, MEMORY, PARENT, 1, "Name", Some(&"d".repeat(601))).is_err()
        );
        assert!(proposal_params(REQUEST, "invalid", PARENT, 1, "Name", None).is_err());
    }

    #[test]
    fn memory_list_requests_and_pages_are_bounded_and_scoped() {
        assert_eq!(
            list_params(MemoryScope::History, 30).unwrap(),
            json!({"scope":"history", "offset":30,"limit":10})
        );
        assert!(list_params(MemoryScope::Current, OFFSET_LIMIT + 1).is_err());
        let fixture = page_fixture();
        let page: MemoryPage = serde_json::from_value(fixture.clone()).unwrap();
        assert!(page.validated(MemoryScope::Current, 0).is_ok());
        for (field, value) in [
            ("scope", json!("history")),
            ("offset", json!(1)),
            ("limit", json!(11)),
            ("total_items", json!(0)),
            ("next_offset", json!(1)),
            ("current_profile_version_id", Value::Null),
            ("retention_generation", json!(0)),
        ] {
            let mut invalid = fixture.clone();
            invalid[field] = value;
            let page: MemoryPage = serde_json::from_value(invalid).unwrap();
            assert!(page.validated(MemoryScope::Current, 0).is_err(), "{field}");
        }
        let mut duplicated = fixture.clone();
        duplicated["items"] = json!([summary_fixture(), summary_fixture()]);
        duplicated["total_items"] = json!(2);
        let page: MemoryPage = serde_json::from_value(duplicated).unwrap();
        assert!(page.validated(MemoryScope::Current, 0).is_err());
        let mut wrong_scope = fixture;
        wrong_scope["items"][0]["is_previous_import_set"] = json!(true);
        let page: MemoryPage = serde_json::from_value(wrong_scope).unwrap();
        assert!(page.validated(MemoryScope::Current, 0).is_err());
    }

    #[test]
    fn memory_contract_denies_internal_fields_and_requires_nullable_fields() {
        for extra in ["narrative", "checksum_sha256", "blob_path", "canonical"] {
            let mut fixture = page_fixture();
            fixture["items"][0][extra] = json!("private");
            assert!(serde_json::from_value::<MemoryPage>(fixture).is_err());
        }
        for missing in ["title", "review_item_id"] {
            let mut fixture = detail_fixture();
            fixture.as_object_mut().unwrap().remove(missing);
            assert!(serde_json::from_value::<MemoryDetail>(fixture).is_err());
        }
        let mut detail = detail_fixture();
        detail["database_path"] = json!("/private/vault");
        assert!(serde_json::from_value::<MemoryDetail>(detail).is_err());
        let mut receipt = save_fixture();
        receipt["narrative"] = json!("private");
        assert!(serde_json::from_value::<MemorySaveReceipt>(receipt).is_err());
    }

    #[test]
    fn memory_detail_validates_identity_original_length_and_normalized_summary() {
        let fixture = detail_fixture();
        let detail: MemoryDetail = serde_json::from_value(fixture.clone()).unwrap();
        assert!(detail.validated(MEMORY).is_ok());
        for (field, value) in [
            ("id", json!(REVIEW)),
            ("character_count", json!(9)),
            ("title", json!(" Title ")),
            ("excerpt", json!("\tHidden")),
            ("narrative", json!("A\0story.")),
            ("profile_version_id", json!("bad")),
            ("retention_generation", json!(0)),
            ("created_at_ms", json!(0)),
        ] {
            let mut invalid = fixture.clone();
            invalid[field] = value;
            let detail: MemoryDetail = serde_json::from_value(invalid).unwrap();
            assert!(detail.validated(MEMORY).is_err(), "{field}");
        }
        let original = "  A story.\r\n\t  ";
        let mut preserved = fixture;
        preserved["narrative"] = json!(original);
        preserved["character_count"] = json!(original.chars().count());
        let detail: MemoryDetail = serde_json::from_value(preserved).unwrap();
        assert_eq!(detail.validated(MEMORY).unwrap().narrative, original);
    }

    #[test]
    fn memory_receipts_match_original_request_and_accept_exact_retry() {
        for created in [true, false] {
            let mut fixture = save_fixture();
            fixture["created"] = json!(created);
            let receipt: MemorySaveReceipt = serde_json::from_value(fixture).unwrap();
            assert_eq!(
                receipt.validated(REQUEST, PARENT, 1).unwrap().created,
                created
            );
        }
        for (field, value) in [
            ("request_id", json!(REVIEW)),
            ("profile_version_id", json!(REVIEW)),
            ("memory_id", json!(MEMORY)),
            ("retention_generation", json!(2)),
            ("created_at_ms", json!(0)),
        ] {
            let mut fixture = save_fixture();
            fixture[field] = value;
            let receipt: MemorySaveReceipt = serde_json::from_value(fixture).unwrap();
            assert!(receipt.validated(REQUEST, PARENT, 1).is_err(), "{field}");
        }
        let receipt: MemoryProposalReceipt = serde_json::from_value(proposal_fixture()).unwrap();
        assert!(receipt.validated(REQUEST, MEMORY).is_ok());
        for (field, value) in [
            ("request_id", json!(REVIEW)),
            ("memory_id", json!(REVIEW)),
            ("review_item_id", json!("bad")),
            ("created_at_ms", json!(0)),
        ] {
            let mut fixture = proposal_fixture();
            fixture[field] = value;
            let receipt: MemoryProposalReceipt = serde_json::from_value(fixture).unwrap();
            assert!(receipt.validated(REQUEST, MEMORY).is_err(), "{field}");
        }
    }

    #[test]
    fn memory_safe_errors_never_echo_private_engine_content() {
        for writing in [true, false] {
            for raw in [
                "vault_integrity_error: /private/narrative",
                "Unexpected story contents",
                "invalid_params: SECRET",
                "memory_not_found: /private/local/path",
            ] {
                let error = safe_error(raw.to_string(), writing);
                assert!(!error.contains("/private"));
                assert!(!error.contains("SECRET"));
                assert!(!error.contains("contents"));
            }
        }
        assert!(safe_error("timeout".to_string(), true).starts_with("memory_uncertain:"));
        assert!(safe_error("timeout".to_string(), false).starts_with("memory_error:"));
        for code in [
            "memory_request_conflict",
            "memory_profile_conflict",
            "memory_stale_generation",
            "memory_not_found",
            "memory_project_exists",
            "profile_missing",
            "invalid_params",
            "vault_busy",
        ] {
            let error = safe_error(format!("{code}: /private/original"), true);
            assert!(error.starts_with(&format!("{code}:")), "{code}");
            assert!(!error.contains("/private"));
        }
    }

    #[test]
    fn every_memory_write_attempt_invalidates_cached_status_even_if_reply_is_invalid() {
        tauri::async_runtime::block_on(async {
            let runtime = EngineRuntime::default();
            for result in [
                Err("timeout /private/text".to_string()),
                Ok(serde_json::from_value::<MemorySaveReceipt>({
                    let mut value = save_fixture();
                    value["request_id"] = json!(REVIEW);
                    value
                })
                .unwrap()),
                Ok(serde_json::from_value::<MemorySaveReceipt>(save_fixture()).unwrap()),
            ] {
                *runtime.cached_status.lock().await = Some(crate::CachedEngineStatus {
                    checked_at: std::time::Instant::now(),
                    result: Err("cached".to_string()),
                });
                let result = finalize_save_attempt(&runtime, result, REQUEST, PARENT, 1).await;
                if let Err(error) = result {
                    assert!(error.starts_with("memory_uncertain:"));
                }
                assert!(runtime.cached_status.lock().await.is_none());
            }
            *runtime.cached_status.lock().await = Some(crate::CachedEngineStatus {
                checked_at: std::time::Instant::now(),
                result: Err("cached".to_string()),
            });
            let error =
                finalize_proposal_attempt(&runtime, Err("timeout".to_string()), REQUEST, MEMORY)
                    .await
                    .unwrap_err();
            assert!(error.starts_with("memory_uncertain:"));
            assert!(runtime.cached_status.lock().await.is_none());
        });
    }
}
