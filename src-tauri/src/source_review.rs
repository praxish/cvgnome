// SPDX-License-Identifier: MPL-2.0
use crate::{
    ENGINE_PROFILE_EDIT_TIMEOUT, ENGINE_PROFILE_REVIEW_TIMEOUT, EngineRuntime,
    JAVASCRIPT_MAX_SAFE_INTEGER, PROFILE_BASICS_EMAIL_LIMIT_CHARS,
    PROFILE_BASICS_PHONE_LIMIT_CHARS, PROFILE_BASICS_SUMMARY_LIMIT_CHARS,
    PROFILE_REVIEW_HEADLINE_LIMIT_CHARS, PROFILE_REVIEW_NAME_LIMIT_CHARS, RetainedSourceFormat,
    RetainedSourceKind, deserialize_required_nullable, invalidate_status, run_engine_request,
    validate_javascript_integer, validate_optional_profile_review_text,
    validate_profile_basics_text, validate_profile_review_serialized_size,
    validate_profile_review_text, validate_public_profile_url,
    validate_retained_source_display_name, validate_uuid_receipt,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::HashSet;

const PAGE_LIMIT: usize = 10;
const OFFSET_LIMIT: usize = 10_000;
const DECISION_LIMIT: usize = 50;
const CHECK_LIMIT: u64 = 240;
const PAGE_BYTES: usize = 176 * 1024;
const APPLY_BYTES: usize = 16 * 1024;

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub(crate) enum SourceReviewScope {
    Inbox,
    Deferred,
    History,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum SourceReviewState {
    Inbox,
    Deferred,
    Resolved,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum SourceReviewKind {
    BasicConflict,
    DuplicateDocument,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum SourceReviewField {
    Name,
    Headline,
    Summary,
    Email,
    Phone,
    Url,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum SourceReviewResolution {
    KeepProfile,
    UseSource,
    AcknowledgeDuplicate,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum SourceReviewAction {
    KeepProfile,
    UseSource,
    AcknowledgeDuplicate,
    Defer,
    Reopen,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct SourceReviewCounts {
    inbox: u64,
    deferred: u64,
    history: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct SourceReviewEvidence {
    display_name: String,
    source_format: RetainedSourceFormat,
    source_kind: RetainedSourceKind,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    location_label: Option<String>,
    excerpt: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct SourceReviewItem {
    id: String,
    kind: SourceReviewKind,
    state: SourceReviewState,
    state_revision: u64,
    title: String,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    field: Option<SourceReviewField>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    previous_value: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    proposed_value: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    resolution: Option<SourceReviewResolution>,
    evidence: Vec<SourceReviewEvidence>,
    is_previous_import_set: bool,
    created_at_ms: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct SourceReviewPage {
    #[serde(deserialize_with = "deserialize_required_nullable")]
    current_profile_version_id: Option<String>,
    scope: SourceReviewScope,
    offset: u64,
    limit: u64,
    total_items: u64,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    next_offset: Option<u64>,
    counts: SourceReviewCounts,
    items: Vec<SourceReviewItem>,
}

// Top-level Tauri arguments are camelCase; these nested engine-shaped records
// deliberately stay snake_case to match the renderer's shared decision type.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct SourceReviewDecision {
    item_id: String,
    expected_state: SourceReviewState,
    expected_revision: u64,
    action: SourceReviewAction,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct SourceReviewDecisionReceipt {
    item_id: String,
    action: SourceReviewAction,
    state: SourceReviewState,
    state_revision: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct SourceReviewApplyReceipt {
    request_id: String,
    created: bool,
    created_at_ms: u64,
    parent_profile_version_id: String,
    profile_version_id: String,
    version_number: u64,
    profile_changed: bool,
    decisions: Vec<SourceReviewDecisionReceipt>,
    counts: SourceReviewCounts,
}

pub(crate) fn validate_source_review_count(count: u64) -> Result<(), String> {
    if count > CHECK_LIMIT {
        return Err("The local engine returned too many source checks".to_string());
    }
    Ok(())
}

impl SourceReviewCounts {
    fn validate(&self) -> Result<(), String> {
        for value in [self.inbox, self.deferred, self.history] {
            validate_javascript_integer(value, "source check count")?;
        }
        Ok(())
    }

    fn count(&self, scope: SourceReviewScope) -> u64 {
        match scope {
            SourceReviewScope::Inbox => self.inbox,
            SourceReviewScope::Deferred => self.deferred,
            SourceReviewScope::History => self.history,
        }
    }
}

impl SourceReviewAction {
    fn result_state(self, previous: SourceReviewState) -> Result<SourceReviewState, String> {
        match (previous, self) {
            (SourceReviewState::Inbox, Self::Defer) => Ok(SourceReviewState::Deferred),
            (SourceReviewState::Deferred, Self::Reopen) => Ok(SourceReviewState::Inbox),
            (
                SourceReviewState::Inbox,
                Self::KeepProfile | Self::UseSource | Self::AcknowledgeDuplicate,
            ) => Ok(SourceReviewState::Resolved),
            _ => Err("invalid_params: That source check decision is unavailable.".to_string()),
        }
    }
}

impl SourceReviewField {
    fn validate_value(self, value: &str) -> Result<(), String> {
        let (label, maximum, multiline) = match self {
            Self::Name => ("source name", PROFILE_REVIEW_NAME_LIMIT_CHARS, false),
            Self::Headline => (
                "source headline",
                PROFILE_REVIEW_HEADLINE_LIMIT_CHARS,
                false,
            ),
            Self::Summary => ("source summary", PROFILE_BASICS_SUMMARY_LIMIT_CHARS, true),
            Self::Email => ("source email", PROFILE_BASICS_EMAIL_LIMIT_CHARS, false),
            Self::Phone => ("source phone", PROFILE_BASICS_PHONE_LIMIT_CHARS, false),
            Self::Url => return validate_public_profile_url(value, "source website"),
        };
        validate_profile_basics_text(value, label, maximum, multiline)
    }
}

impl SourceReviewItem {
    fn validate(&self, scope: SourceReviewScope) -> Result<(), String> {
        validate_uuid_receipt(&self.id)?;
        validate_javascript_integer(self.state_revision, "source check revision")?;
        validate_javascript_integer(self.created_at_ms, "source check timestamp")?;
        if self.created_at_ms == 0 {
            return Err("The local engine returned an invalid source check timestamp".to_string());
        }
        validate_profile_review_text(&self.title, "source check title", 160, 640)?;
        let in_scope = match scope {
            SourceReviewScope::Inbox => {
                self.state == SourceReviewState::Inbox && !self.is_previous_import_set
            }
            SourceReviewScope::Deferred => {
                self.state == SourceReviewState::Deferred && !self.is_previous_import_set
            }
            SourceReviewScope::History => {
                self.state == SourceReviewState::Resolved || self.is_previous_import_set
            }
        };
        if !in_scope || (self.state == SourceReviewState::Resolved) != self.resolution.is_some() {
            return Err("The local engine returned an inconsistent source check state".to_string());
        }
        match self.kind {
            SourceReviewKind::BasicConflict => {
                let field = self
                    .field
                    .ok_or("The local engine omitted a source check field")?;
                let previous = self
                    .previous_value
                    .as_deref()
                    .ok_or("The local engine omitted a source check value")?;
                let proposed = self
                    .proposed_value
                    .as_deref()
                    .ok_or("The local engine omitted a source check value")?;
                field.validate_value(previous)?;
                field.validate_value(proposed)?;
                if previous == proposed
                    || self.evidence.len() != 1
                    || self.resolution == Some(SourceReviewResolution::AcknowledgeDuplicate)
                {
                    return Err(
                        "The local engine returned an inconsistent source conflict".to_string()
                    );
                }
            }
            SourceReviewKind::DuplicateDocument => {
                if self.field.is_some()
                    || self.previous_value.is_some()
                    || self.proposed_value.is_some()
                    || self.evidence.len() != 2
                    || matches!(
                        self.resolution,
                        Some(
                            SourceReviewResolution::KeepProfile | SourceReviewResolution::UseSource
                        )
                    )
                    || self
                        .evidence
                        .iter()
                        .any(|evidence| !evidence.excerpt.is_empty())
                {
                    return Err(
                        "The local engine returned an inconsistent duplicate source check"
                            .to_string(),
                    );
                }
            }
        }
        for evidence in &self.evidence {
            validate_retained_source_display_name(&evidence.display_name)?;
            validate_optional_profile_review_text(
                evidence.location_label.as_deref(),
                "source check location",
                120,
                480,
            )?;
            if !evidence.excerpt.is_empty() {
                validate_profile_review_text(&evidence.excerpt, "source check excerpt", 360, 1440)?;
            }
        }
        Ok(())
    }
}

impl SourceReviewPage {
    fn validated(
        self,
        scope: SourceReviewScope,
        limit: usize,
        offset: usize,
    ) -> Result<Self, String> {
        self.counts.validate()?;
        if let Some(id) = &self.current_profile_version_id {
            validate_uuid_receipt(id)?;
        } else if self.counts.inbox != 0 || self.counts.deferred != 0 || self.counts.history != 0 {
            return Err("The local engine returned source checks without a profile".to_string());
        }
        if self.scope != scope
            || self.limit != limit as u64
            || self.offset != offset as u64
            || self.total_items != self.counts.count(scope)
            || self.items.len() > limit
        {
            return Err("The local engine returned a mismatched source check page".to_string());
        }
        let end = self
            .offset
            .checked_add(self.items.len() as u64)
            .ok_or("The local engine returned invalid source check pagination")?;
        if (!self.items.is_empty() && (self.offset >= self.total_items || end > self.total_items))
            || (self.items.is_empty() && self.offset < self.total_items)
            || self.next_offset
                != (end < self.total_items && end <= OFFSET_LIMIT as u64).then_some(end)
        {
            return Err("The local engine returned invalid source check pagination".to_string());
        }
        let mut ids = HashSet::new();
        for item in &self.items {
            item.validate(scope)?;
            if !ids.insert(item.id.as_str()) {
                return Err("The local engine returned duplicate source checks".to_string());
            }
        }
        validate_profile_review_serialized_size(&self, PAGE_BYTES, "source check page")?;
        Ok(self)
    }
}

fn list_params(scope: SourceReviewScope, limit: usize, offset: usize) -> Result<Value, String> {
    if !(1..=PAGE_LIMIT).contains(&limit) || offset > OFFSET_LIMIT {
        return Err(
            "invalid_params: The source check page is outside the local limit.".to_string(),
        );
    }
    Ok(json!({"scope": scope, "limit": limit, "offset": offset}))
}

fn apply_params(
    request_id: &str,
    parent_id: &str,
    decisions: &[SourceReviewDecision],
) -> Result<Value, String> {
    for id in [request_id, parent_id] {
        validate_uuid_receipt(id).map_err(|_| {
            "invalid_params: A source check request identifier is invalid.".to_string()
        })?;
    }
    if decisions.is_empty() || decisions.len() > DECISION_LIMIT {
        return Err("invalid_params: Queue between 1 and 50 source check decisions.".to_string());
    }
    let mut ids = HashSet::new();
    for decision in decisions {
        validate_uuid_receipt(&decision.item_id)
            .map_err(|_| "invalid_params: A source check identifier is invalid.".to_string())?;
        if !ids.insert(decision.item_id.as_str())
            || decision.expected_revision >= JAVASCRIPT_MAX_SAFE_INTEGER
        {
            return Err(
                "invalid_params: The source check batch contains an invalid or repeated item."
                    .to_string(),
            );
        }
        decision.action.result_state(decision.expected_state)?;
    }
    let params = json!({"request_id": request_id, "expected_parent_profile_version_id": parent_id, "decisions": decisions});
    validate_profile_review_serialized_size(&params, APPLY_BYTES, "source check request")?;
    Ok(params)
}

impl SourceReviewApplyReceipt {
    fn validated(
        self,
        request_id: &str,
        parent_id: &str,
        decisions: &[SourceReviewDecision],
    ) -> Result<Self, String> {
        for id in [
            &self.request_id,
            &self.parent_profile_version_id,
            &self.profile_version_id,
        ] {
            validate_uuid_receipt(id)?;
        }
        self.counts.validate()?;
        validate_javascript_integer(self.created_at_ms, "source check apply timestamp")?;
        validate_javascript_integer(self.version_number, "source check profile version")?;
        if self.request_id != request_id
            || self.parent_profile_version_id != parent_id
            || self.created_at_ms == 0
            || self.version_number == 0
            || self.profile_changed != (self.profile_version_id != self.parent_profile_version_id)
            || self.profile_changed
                != decisions
                    .iter()
                    .any(|decision| decision.action == SourceReviewAction::UseSource)
            || self.decisions.len() != decisions.len()
        {
            return Err(
                "The local engine returned an inconsistent source check receipt".to_string(),
            );
        }
        let mut ids = HashSet::new();
        for result in &self.decisions {
            let expected = decisions
                .iter()
                .find(|decision| decision.item_id == result.item_id)
                .ok_or("The local engine returned a mismatched source check decision")?;
            if !ids.insert(result.item_id.as_str())
                || result.action != expected.action
                || result.state != expected.action.result_state(expected.expected_state)?
                || Some(result.state_revision) != expected.expected_revision.checked_add(1)
            {
                return Err(
                    "The local engine returned an inconsistent source check decision".to_string(),
                );
            }
        }
        validate_profile_review_serialized_size(&self, APPLY_BYTES, "source check receipt")?;
        Ok(self)
    }
}

fn safe_error(error: String, applying: bool) -> String {
    let code = error.split_once(':').map(|(code, _)| code.trim());
    let message = match code {
        Some("source_review_request_conflict") => {
            "source_review_request_conflict: This request ID was already used for different source check decisions."
        }
        Some("source_review_profile_conflict") | Some("profile_conflict") => {
            "source_review_profile_conflict: Your profile changed. Reload source checks before applying these decisions."
        }
        Some("source_review_state_conflict") | Some("source_review_not_found") => {
            "source_review_state_conflict: A source check changed or is no longer available. Reload the list."
        }
        Some("source_review_generation_conflict") => {
            "source_review_generation_conflict: The import set changed. Reload source checks to see their current history."
        }
        Some("source_review_field_conflict") => {
            "source_review_field_conflict: A profile detail changed. Reload source checks before choosing a source value."
        }
        Some("source_review_evidence_missing") => {
            "source_review_evidence_missing: CVGnome could not verify the retained evidence for a source check."
        }
        Some("source_review_invalid") | Some("invalid_params") => {
            "invalid_params: Check the queued source decisions and try again."
        }
        Some("source_review_limit") => {
            "source_review_limit: This request exceeds the local source check limit."
        }
        Some("profile_missing") => {
            "profile_missing: There is no current profile. Reload the workspace."
        }
        Some("profile_version_limit") => {
            "profile_version_limit: The workspace has reached its profile version limit."
        }
        Some("vault_busy") => "vault_busy: The local vault is busy; wait a moment and retry.",
        _ if applying => {
            "source_review_uncertain: CVGnome could not confirm the result. Keep these decisions and retry the same batch."
        }
        _ => "source_review_error: CVGnome could not load the source checks. Try again.",
    };
    message.to_string()
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn list_source_review_items(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    scope: SourceReviewScope,
    limit: usize,
    offset: usize,
) -> Result<SourceReviewPage, String> {
    let params = list_params(scope, limit, offset)?;
    let _operation_guard = runtime.operation_gate.lock().await;
    run_engine_request::<SourceReviewPage>(
        &app,
        "profile.source_review.list",
        params,
        ENGINE_PROFILE_REVIEW_TIMEOUT,
    )
    .await
    .and_then(|page| page.validated(scope, limit, offset))
    .map_err(|error| safe_error(error, false))
}

async fn finalize_apply_attempt(
    runtime: &EngineRuntime,
    result: Result<SourceReviewApplyReceipt, String>,
    request_id: &str,
    parent_id: &str,
    decisions: &[SourceReviewDecision],
) -> Result<SourceReviewApplyReceipt, String> {
    // The commit can succeed before any transport or validation failure. An
    // exact request remains retryable and status must never retain stale counts.
    invalidate_status(runtime).await;
    result
        .and_then(|receipt| receipt.validated(request_id, parent_id, decisions))
        .map_err(|error| safe_error(error, true))
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn apply_source_review_decisions(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, EngineRuntime>,
    request_id: String,
    expected_parent_profile_version_id: String,
    decisions: Vec<SourceReviewDecision>,
) -> Result<SourceReviewApplyReceipt, String> {
    let params = apply_params(&request_id, &expected_parent_profile_version_id, &decisions)?;
    let _operation_guard = runtime.operation_gate.lock().await;
    let result = run_engine_request::<SourceReviewApplyReceipt>(
        &app,
        "profile.source_review.apply",
        params,
        ENGINE_PROFILE_EDIT_TIMEOUT,
    )
    .await;
    finalize_apply_attempt(
        &runtime,
        result,
        &request_id,
        &expected_parent_profile_version_id,
        &decisions,
    )
    .await
}

#[cfg(test)]
mod tests {
    use super::*;

    const ITEM: &str = "565ed1b4-3876-4560-8c46-a7a3b4fc0600";
    const PARENT: &str = "6ac523a8-cfad-4bbf-a6bb-2b7e0163a266";
    const VERSION: &str = "e76ac26e-375c-4bd7-be2f-28915e4f5f06";
    const REQUEST: &str = "a25e0b67-120c-4a75-ae70-4871bf2f5dfb";

    fn decision() -> SourceReviewDecision {
        serde_json::from_value(json!({
            "item_id": ITEM, "expected_state": "inbox", "expected_revision": 0, "action": "use_source"
        })).unwrap()
    }

    fn page_fixture() -> Value {
        json!({
            "current_profile_version_id": PARENT, "scope": "inbox", "offset": 0,
            "limit": 10, "total_items": 1, "next_offset": null,
            "counts": {"inbox": 1, "deferred": 0, "history": 0},
            "items": [{
                "id": ITEM, "kind": "basic_conflict", "state": "inbox", "state_revision": 0,
                "title": "Headline differs", "field": "headline", "previous_value": "Engineer",
                "proposed_value": "Senior engineer", "resolution": null,
                "evidence": [{"display_name": "resume.docx", "source_format": "docx", "source_kind": "resume", "location_label": null, "excerpt": "Senior engineer"}],
                "is_previous_import_set": false, "created_at_ms": 1_800_000_000_000_u64
            }]
        })
    }

    fn receipt_fixture() -> Value {
        json!({
            "request_id": REQUEST, "created": true, "created_at_ms": 1_800_000_000_000_u64,
            "parent_profile_version_id": PARENT, "profile_version_id": VERSION,
            "version_number": 2, "profile_changed": true,
            "decisions": [{"item_id": ITEM, "action": "use_source", "state": "resolved", "state_revision": 1}],
            "counts": {"inbox": 0, "deferred": 0, "history": 1}
        })
    }

    #[test]
    fn source_review_request_contract_is_strict_and_state_bounded() {
        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase", deny_unknown_fields)]
        struct Arguments {
            request_id: String,
            expected_parent_profile_version_id: String,
            decisions: Vec<SourceReviewDecision>,
        }
        let arguments = json!({"requestId": REQUEST, "expectedParentProfileVersionId": PARENT, "decisions": [decision()]});
        let decoded: Arguments = serde_json::from_value(arguments.clone()).unwrap();
        let params = apply_params(
            &decoded.request_id,
            &decoded.expected_parent_profile_version_id,
            &decoded.decisions,
        )
        .unwrap();
        assert_eq!(
            params,
            json!({"request_id": REQUEST, "expected_parent_profile_version_id": PARENT, "decisions": [{"item_id": ITEM, "expected_state": "inbox", "expected_revision": 0, "action": "use_source"}]})
        );
        assert!(serde_json::from_value::<Arguments>(params).is_err());
        let mut wrong_nested = arguments.clone();
        wrong_nested["decisions"][0]["itemId"] = json!(ITEM);
        assert!(serde_json::from_value::<Arguments>(wrong_nested).is_err());
        assert!(apply_params(REQUEST, PARENT, &[]).is_err());
        assert!(apply_params(REQUEST, PARENT, &[decision(), decision()]).is_err());
        assert!(apply_params("bad", PARENT, &[decision()]).is_err());
        let mut maximum = Vec::new();
        for _ in 0..DECISION_LIMIT {
            let mut item = decision();
            item.item_id = uuid::Uuid::new_v4().to_string();
            maximum.push(item);
        }
        assert!(apply_params(REQUEST, PARENT, &maximum).is_ok());
        maximum.push(decision());
        assert!(apply_params(REQUEST, PARENT, &maximum).is_err());
        let mut invalid_revision = decision();
        invalid_revision.expected_revision = JAVASCRIPT_MAX_SAFE_INTEGER;
        assert!(apply_params(REQUEST, PARENT, &[invalid_revision]).is_err());
        for (state, action, valid) in [
            ("inbox", "defer", true),
            ("inbox", "reopen", false),
            ("deferred", "reopen", true),
            ("deferred", "keep_profile", false),
            ("resolved", "reopen", false),
            ("resolved", "use_source", false),
        ] {
            let input: SourceReviewDecision = serde_json::from_value(json!({"item_id": ITEM, "expected_state": state, "expected_revision": 2, "action": action})).unwrap();
            assert_eq!(apply_params(REQUEST, PARENT, &[input]).is_ok(), valid);
        }
        for (limit, offset, valid) in [
            (0, 0, false),
            (1, 0, true),
            (10, 10_000, true),
            (11, 0, false),
            (10, 10_001, false),
        ] {
            assert_eq!(
                list_params(SourceReviewScope::Inbox, limit, offset).is_ok(),
                valid
            );
        }
    }

    #[test]
    fn source_review_pages_reject_private_missing_and_inconsistent_shapes() {
        let fixture = page_fixture();
        let page: SourceReviewPage = serde_json::from_value(fixture.clone()).unwrap();
        assert!(page.validated(SourceReviewScope::Inbox, 10, 0).is_ok());
        for field in ["current_profile_version_id", "next_offset"] {
            let mut invalid = fixture.clone();
            invalid.as_object_mut().unwrap().remove(field);
            assert!(serde_json::from_value::<SourceReviewPage>(invalid).is_err());
        }
        for field in ["field", "previous_value", "proposed_value", "resolution"] {
            let mut invalid = fixture.clone();
            invalid["items"][0].as_object_mut().unwrap().remove(field);
            assert!(serde_json::from_value::<SourceReviewPage>(invalid).is_err());
        }
        let mut private = fixture.clone();
        private["items"][0]["evidence"][0]["source_path"] = json!("/private/evidence");
        assert!(serde_json::from_value::<SourceReviewPage>(private).is_err());
        for (field, value) in [
            ("scope", json!("history")),
            ("limit", json!(11)),
            ("offset", json!(1)),
            ("total_items", json!(2)),
            ("next_offset", json!(1)),
            ("current_profile_version_id", Value::Null),
        ] {
            let mut invalid = fixture.clone();
            invalid[field] = value;
            let page: SourceReviewPage = serde_json::from_value(invalid).unwrap();
            assert!(page.validated(SourceReviewScope::Inbox, 10, 0).is_err());
        }
        for (field, value) in [
            ("state_revision", json!(JAVASCRIPT_MAX_SAFE_INTEGER + 1)),
            ("created_at_ms", json!(0)),
            ("state", json!("resolved")),
            ("proposed_value", json!("Engineer")),
            ("resolution", json!("acknowledge_duplicate")),
            ("title", json!("x".repeat(161))),
            ("is_previous_import_set", json!(true)),
            ("field", Value::Null),
        ] {
            let mut invalid = fixture.clone();
            invalid["items"][0][field] = value;
            let page: SourceReviewPage = serde_json::from_value(invalid).unwrap();
            assert!(page.validated(SourceReviewScope::Inbox, 10, 0).is_err());
        }
        let mut duplicate_id = fixture.clone();
        duplicate_id["total_items"] = json!(2);
        duplicate_id["counts"]["inbox"] = json!(2);
        duplicate_id["items"]
            .as_array_mut()
            .unwrap()
            .push(fixture["items"][0].clone());
        let page: SourceReviewPage = serde_json::from_value(duplicate_id).unwrap();
        assert!(page.validated(SourceReviewScope::Inbox, 10, 0).is_err());
    }

    #[test]
    fn source_review_history_duplicate_and_unicode_scalar_contracts_are_valid() {
        let mut fixture = page_fixture();
        fixture["scope"] = json!("history");
        fixture["counts"] = json!({"inbox": 0, "deferred": 0, "history": 1});
        fixture["items"][0]["is_previous_import_set"] = json!(true);
        fixture["items"][0]["field"] = json!("summary");
        fixture["items"][0]["previous_value"] = json!("Previous\nsummary");
        fixture["items"][0]["proposed_value"] = json!("界".repeat(4000));
        let page: SourceReviewPage = serde_json::from_value(fixture.clone()).unwrap();
        assert!(page.validated(SourceReviewScope::History, 10, 0).is_ok());
        fixture["items"][0]["proposed_value"] = json!("界".repeat(4001));
        let page: SourceReviewPage = serde_json::from_value(fixture).unwrap();
        assert!(page.validated(SourceReviewScope::History, 10, 0).is_err());

        let mut duplicate = page_fixture();
        duplicate["items"][0]["kind"] = json!("duplicate_document");
        for field in ["field", "previous_value", "proposed_value"] {
            duplicate["items"][0][field] = Value::Null;
        }
        duplicate["items"][0]["evidence"][0]["excerpt"] = json!("");
        let evidence = duplicate["items"][0]["evidence"][0].clone();
        duplicate["items"][0]["evidence"]
            .as_array_mut()
            .unwrap()
            .push(evidence);
        let page: SourceReviewPage = serde_json::from_value(duplicate.clone()).unwrap();
        assert!(page.validated(SourceReviewScope::Inbox, 10, 0).is_ok());
        duplicate["items"][0]["evidence"][1]["excerpt"] = json!("Unexpected extracted text");
        let page: SourceReviewPage = serde_json::from_value(duplicate).unwrap();
        assert!(page.validated(SourceReviewScope::Inbox, 10, 0).is_err());
    }

    #[test]
    fn source_review_apply_receipts_are_fully_correlated_and_retry_safe() {
        let fixture = receipt_fixture();
        for created in [true, false] {
            let mut valid = fixture.clone();
            valid["created"] = json!(created);
            let receipt: SourceReviewApplyReceipt = serde_json::from_value(valid).unwrap();
            assert!(receipt.validated(REQUEST, PARENT, &[decision()]).is_ok());
        }
        for (field, value) in [
            ("request_id", json!(VERSION)),
            ("parent_profile_version_id", json!(VERSION)),
            ("profile_version_id", json!(PARENT)),
            ("profile_changed", json!(false)),
            ("created_at_ms", json!(0)),
            ("version_number", json!(JAVASCRIPT_MAX_SAFE_INTEGER + 1)),
            ("decisions", json!([])),
        ] {
            let mut invalid = fixture.clone();
            invalid[field] = value;
            let receipt: SourceReviewApplyReceipt = serde_json::from_value(invalid).unwrap();
            assert!(receipt.validated(REQUEST, PARENT, &[decision()]).is_err());
        }
        for (field, value) in [
            ("item_id", json!(VERSION)),
            ("action", json!("defer")),
            ("state", json!("inbox")),
            ("state_revision", json!(2)),
        ] {
            let mut invalid = fixture.clone();
            invalid["decisions"][0][field] = value;
            let receipt: SourceReviewApplyReceipt = serde_json::from_value(invalid).unwrap();
            assert!(receipt.validated(REQUEST, PARENT, &[decision()]).is_err());
        }
        let mut keep = decision();
        keep.action = SourceReviewAction::KeepProfile;
        let mut unchanged = fixture.clone();
        unchanged["profile_changed"] = json!(false);
        unchanged["profile_version_id"] = json!(PARENT);
        unchanged["decisions"][0]["action"] = json!("keep_profile");
        let receipt: SourceReviewApplyReceipt = serde_json::from_value(unchanged).unwrap();
        assert!(receipt.validated(REQUEST, PARENT, &[keep]).is_ok());
        let mut private = fixture;
        private["canonical"] = json!({});
        assert!(serde_json::from_value::<SourceReviewApplyReceipt>(private).is_err());
    }

    #[test]
    fn source_review_apply_failures_clear_status_and_preserve_uncertain_retry() {
        tauri::async_runtime::block_on(async {
            let runtime = EngineRuntime::default();
            for result in [
                Err("The local engine timed out /private/source".to_string()),
                Ok(serde_json::from_value::<SourceReviewApplyReceipt>({
                    let mut value = receipt_fixture();
                    value["request_id"] = json!(VERSION);
                    value
                })
                .unwrap()),
            ] {
                *runtime.cached_status.lock().await = Some(crate::CachedEngineStatus {
                    checked_at: std::time::Instant::now(),
                    result: Err("cached".to_string()),
                });
                let error =
                    finalize_apply_attempt(&runtime, result, REQUEST, PARENT, &[decision()])
                        .await
                        .unwrap_err();
                assert!(error.starts_with("source_review_uncertain:"));
                assert!(!error.contains("/private/"));
                assert!(runtime.cached_status.lock().await.is_none());
            }
            assert_eq!(
                safe_error(
                    "source_review_field_conflict: private data".to_string(),
                    true
                ),
                "source_review_field_conflict: A profile detail changed. Reload source checks before choosing a source value."
            );
            assert!(
                safe_error("vault_integrity_error: /private/source".to_string(), true)
                    .starts_with("source_review_uncertain:")
            );
        });
    }
}
