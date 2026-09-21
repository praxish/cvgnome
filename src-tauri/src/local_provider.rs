// SPDX-License-Identifier: MPL-2.0
use crate::provider_spend::{
    ProviderSpendKind, ProviderSpendReservation, ProviderSpendStage, ProviderTokenUsage,
    cancel_provider_spend, complete_provider_spend, reconcile_interrupted_provider_spend,
    require_priced_models_if_guarded, reserve_provider_spend,
};
use crate::{EngineRuntime, prepare_data_dir, run_engine_request};
use keyring_core::{CredentialStore, Error as KeyringError};
use reqwest::header::{ACCEPT, AUTHORIZATION, CONTENT_TYPE, HeaderValue};
use reqwest::{Client, RequestBuilder, StatusCode, Url, redirect};
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use std::collections::{BTreeMap, HashSet};
use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tauri::Manager;
use tempfile::NamedTempFile;
use tokio::sync::Mutex;
use uuid::Uuid;
use zeroize::Zeroizing;

const CONFIG_FILENAME: &str = "local-provider.json";
const LEGACY_CONFIG_SCHEMA_VERSION: u32 = 1;
const CONFIG_SCHEMA_VERSION: u32 = 2;
const CONFIG_FILE_LIMIT_BYTES: u64 = 16 * 1024;
const JAVASCRIPT_MAX_SAFE_INTEGER: u64 = 9_007_199_254_740_991;
const DEFAULT_BASE_URL: &str = "http://127.0.0.1:1234/v1";
const OPENAI_BASE_URL: &str = "https://api.openai.com/v1";
const OPENAI_CREDENTIAL_SERVICE: &str = "com.cvgnome.provider-credentials.v1";
const OPENAI_CREDENTIAL_ACCOUNT: &str = "openai-api-key";
const CREDENTIAL_ENVELOPE_VERSION: u32 = 1;
const API_KEY_MIN_BYTES: usize = 16;
const API_KEY_MAX_BYTES: usize = 512;
const MODEL_ID_LIMIT_BYTES: usize = 240;
const MODEL_DISPLAY_NAME_LIMIT_CHARS: usize = 240;
const MODEL_DISPLAY_NAME_LIMIT_BYTES: usize = 960;
const MODEL_LIST_LIMIT: usize = 512;
const MODEL_DISCOVERY_RESPONSE_LIMIT: usize = 1024 * 1024;
const EMBEDDING_REQUEST_LIMIT: usize = 256 * 1024;
const CHAT_RESPONSE_LIMIT: usize = 96 * 1024;
const EMBEDDING_RESPONSE_LIMIT: usize = 8 * 1024 * 1024;
const DISCOVERY_TIMEOUT: Duration = Duration::from_secs(15);
const MODEL_TEST_TIMEOUT: Duration = Duration::from_secs(180);
const TAILORING_TIMEOUT: Duration = Duration::from_secs(300);
const CONNECT_TIMEOUT: Duration = Duration::from_secs(3);
const CLOUD_CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
const MAX_EMBEDDING_DIMENSION: usize = 8_192;
const MAX_TAILORING_EMBEDDING_INPUTS: usize = 33;
const MAX_TAILORING_EMBEDDING_INPUT_BYTES: usize = 40_000;
const MAX_TAILORING_QUERY_BYTES: usize = 8_000;
const MAX_TAILORING_CHUNK_BYTES: usize = 1_200;
const MAX_ENGINE_ARM_PARAMS_BYTES: usize = 8 * 1024 * 1024;
const MAX_CHAT_MESSAGES: usize = 8;
const MAX_CHAT_MESSAGE_BYTES: usize = 128 * 1024;
const MAX_CHAT_MESSAGES_TOTAL_BYTES: usize = 128 * 1024;
const MAX_RESPONSE_FORMAT_BYTES: usize = 16 * 1024;
const MAX_RESPONSE_FORMAT_DEPTH: usize = 20;
const MAX_ENGINE_ARM_RESULT_BYTES: usize = 160 * 1024;
const MAX_CHAT_REQUEST_BYTES: usize = MAX_ENGINE_ARM_RESULT_BYTES + 16 * 1024;
const MAX_CHAT_OUTPUT_TOKENS: u64 = 8_000;
const MAX_DRAFT_EVIDENCE_ITEMS: usize = 32;
const MAX_DRAFT_QUALITY_ISSUES: usize = 32;
const MAX_DRAFT_RESUME_BYTES: usize = 56 * 1024;
const MAX_DRAFT_BYTES: usize = 60 * 1024;

const TEST_EMBEDDING_INPUT: &str = "CVGnome synthetic embedding capability test.";
const TEST_CHAT_SYSTEM: &str =
    "Return only JSON matching the supplied schema. Do not include prose or markdown.";
const TEST_CHAT_USER: &str = "Return exactly this synthetic object: {\"basics\":{\"label\":\"local-check\",\"summary\":\"structured-output-ok\"},\"work\":[{\"source_index\":0,\"summary\":\"summary-branch-ok\",\"highlights\":[\"highlights-branch-ok\"]}]}";
const TEST_CHAT_LABEL: &str = "local-check";
const TEST_CHAT_SUMMARY: &str = "structured-output-ok";
const TEST_CHAT_WORK_SUMMARY: &str = "summary-branch-ok";
const TEST_CHAT_HIGHLIGHT: &str = "highlights-branch-ok";

#[derive(Default)]
pub(crate) struct LocalProviderRuntime {
    gate: Mutex<()>,
}

impl LocalProviderRuntime {
    pub(crate) async fn lock_operations(&self) -> tokio::sync::MutexGuard<'_, ()> {
        self.gate.lock().await
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq, Hash)]
#[serde(rename_all = "snake_case")]
pub(crate) enum LocalProviderKind {
    OpenaiCompatibleLocal,
    Openai,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProviderConfiguration {
    revision: u64,
    base_url: String,
    chat_model_id: Option<String>,
    embedding_model_id: Option<String>,
    verified_revision: Option<u64>,
    verified_at_ms: Option<u64>,
    embedding_dimension: Option<u64>,
}

impl ProviderConfiguration {
    fn local_default() -> Self {
        Self {
            revision: 1,
            base_url: DEFAULT_BASE_URL.to_string(),
            chat_model_id: None,
            embedding_model_id: None,
            verified_revision: None,
            verified_at_ms: None,
            embedding_dimension: None,
        }
    }

    fn openai_default() -> Self {
        Self {
            revision: 1,
            base_url: OPENAI_BASE_URL.to_string(),
            chat_model_id: None,
            embedding_model_id: None,
            verified_revision: None,
            verified_at_ms: None,
            embedding_dimension: None,
        }
    }

    fn validated(mut self, provider_kind: LocalProviderKind) -> Result<Self, LocalProviderError> {
        if self.revision == 0 || self.revision > JAVASCRIPT_MAX_SAFE_INTEGER {
            return Err(LocalProviderError::config_invalid());
        }
        self.base_url = normalized_base_url_for_kind(provider_kind, &self.base_url)?;
        self.chat_model_id = validated_optional_model_id(self.chat_model_id)?;
        self.embedding_model_id = validated_optional_model_id(self.embedding_model_id)?;
        match (
            self.verified_revision,
            self.verified_at_ms,
            self.embedding_dimension,
        ) {
            (None, None, None) => {}
            (Some(revision), Some(verified_at_ms), Some(dimension))
                if revision == self.revision
                    && verified_at_ms > 0
                    && verified_at_ms <= JAVASCRIPT_MAX_SAFE_INTEGER
                    && (1..=MAX_EMBEDDING_DIMENSION as u64).contains(&dimension)
                    && self.chat_model_id.is_some()
                    && self.embedding_model_id.is_some()
                    && self.chat_model_id != self.embedding_model_id => {}
            _ => return Err(LocalProviderError::config_invalid()),
        }
        Ok(self)
    }

    fn clear_verification(&mut self) {
        self.verified_revision = None;
        self.verified_at_ms = None;
        self.embedding_dimension = None;
    }
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct StoredProviderSettings {
    schema_version: u32,
    revision: u64,
    provider_kind: LocalProviderKind,
    local: ProviderConfiguration,
    openai: ProviderConfiguration,
    openai_credential_generation: Option<String>,
}

impl Default for StoredProviderSettings {
    fn default() -> Self {
        Self {
            schema_version: CONFIG_SCHEMA_VERSION,
            revision: 1,
            provider_kind: LocalProviderKind::OpenaiCompatibleLocal,
            local: ProviderConfiguration::local_default(),
            openai: ProviderConfiguration::openai_default(),
            openai_credential_generation: None,
        }
    }
}

impl StoredProviderSettings {
    fn validated(mut self) -> Result<Self, LocalProviderError> {
        if self.schema_version != CONFIG_SCHEMA_VERSION
            || self.revision == 0
            || self.revision > JAVASCRIPT_MAX_SAFE_INTEGER
        {
            return Err(LocalProviderError::config_invalid());
        }
        self.local = self
            .local
            .validated(LocalProviderKind::OpenaiCompatibleLocal)?;
        self.openai = self.openai.validated(LocalProviderKind::Openai)?;
        self.openai_credential_generation = self
            .openai_credential_generation
            .map(|value| validated_credential_generation(&value))
            .transpose()?;
        Ok(self)
    }

    fn active(&self) -> &ProviderConfiguration {
        match self.provider_kind {
            LocalProviderKind::OpenaiCompatibleLocal => &self.local,
            LocalProviderKind::Openai => &self.openai,
        }
    }

    fn active_mut(&mut self) -> &mut ProviderConfiguration {
        match self.provider_kind {
            LocalProviderKind::OpenaiCompatibleLocal => &mut self.local,
            LocalProviderKind::Openai => &mut self.openai,
        }
    }

    fn selected_mut(&mut self, kind: LocalProviderKind) -> &mut ProviderConfiguration {
        match kind {
            LocalProviderKind::OpenaiCompatibleLocal => &mut self.local,
            LocalProviderKind::Openai => &mut self.openai,
        }
    }

    fn view(&self, credential_state: ProviderCredentialState) -> LocalProviderSettings {
        let active = self.active();
        LocalProviderSettings {
            schema_version: self.schema_version,
            revision: self.revision,
            configuration_revision: active.revision,
            provider_kind: self.provider_kind,
            base_url: active.base_url.clone(),
            chat_model_id: active.chat_model_id.clone(),
            embedding_model_id: active.embedding_model_id.clone(),
            verified_revision: active.verified_revision,
            verified_at_ms: active.verified_at_ms,
            embedding_dimension: active.embedding_dimension,
            credential_state,
        }
    }
}

#[derive(Clone, Copy, Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub(crate) enum ProviderCredentialState {
    NotRequired,
    Missing,
    Stored,
    Unavailable,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct LocalProviderSettings {
    schema_version: u32,
    revision: u64,
    configuration_revision: u64,
    provider_kind: LocalProviderKind,
    base_url: String,
    chat_model_id: Option<String>,
    embedding_model_id: Option<String>,
    verified_revision: Option<u64>,
    verified_at_ms: Option<u64>,
    embedding_dimension: Option<u64>,
    credential_state: ProviderCredentialState,
}

impl Default for LocalProviderSettings {
    fn default() -> Self {
        StoredProviderSettings::default().view(ProviderCredentialState::NotRequired)
    }
}

impl LocalProviderSettings {
    fn is_verified(&self) -> bool {
        self.verified_revision == Some(self.configuration_revision)
            && self.verified_at_ms.is_some()
            && self.embedding_dimension.is_some()
            && self.chat_model_id.is_some()
            && self.embedding_model_id.is_some()
            && matches!(
                (self.provider_kind, self.credential_state),
                (
                    LocalProviderKind::OpenaiCompatibleLocal,
                    ProviderCredentialState::NotRequired
                ) | (LocalProviderKind::Openai, ProviderCredentialState::Stored)
            )
    }
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct LegacyProviderSettingsV1 {
    schema_version: u32,
    revision: u64,
    provider_kind: LegacyProviderKindV1,
    base_url: String,
    chat_model_id: Option<String>,
    embedding_model_id: Option<String>,
    verified_revision: Option<u64>,
    verified_at_ms: Option<u64>,
    embedding_dimension: Option<u64>,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum LegacyProviderKindV1 {
    OpenaiCompatibleLocal,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "lowercase")]
pub(crate) enum ProviderModelCapability {
    Chat,
    Embedding,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub(crate) enum ProviderModelCapabilitySource {
    Provider,
    CvgnomeCatalog,
    Unknown,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct ProviderModel {
    id: String,
    display_name: String,
    capabilities: Vec<ProviderModelCapability>,
    capability_source: ProviderModelCapabilitySource,
    loaded: Option<bool>,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct ProviderModelDiscovery {
    provider_kind: LocalProviderKind,
    models: Vec<ProviderModel>,
    truncated: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum TransportErrorKind {
    Unavailable,
    Authentication,
    RateLimited,
    Timeout,
    ModelUnavailable,
    Http,
    TooLarge,
    Incomplete,
    Refusal,
    UsageReceipt,
    Invalid,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct TransportError {
    kind: TransportErrorKind,
    usage: Option<ProviderTokenUsage>,
}

impl TransportError {
    fn new(kind: TransportErrorKind) -> Self {
        Self { kind, usage: None }
    }

    fn with_usage(mut self, usage: Option<ProviderTokenUsage>) -> Self {
        self.usage = usage;
        self
    }

    fn public(self) -> LocalProviderError {
        match self.kind {
            TransportErrorKind::Unavailable => LocalProviderError::new(
                "provider_unavailable",
                "CVGnome could not reach the selected model provider.",
            ),
            TransportErrorKind::Authentication => LocalProviderError::new(
                "provider_authentication_failed",
                "The selected provider rejected or could not access its credential.",
            ),
            TransportErrorKind::RateLimited => LocalProviderError::new(
                "provider_rate_limited",
                "The provider rate limit or account quota was reached. No request was retried.",
            ),
            TransportErrorKind::Timeout => LocalProviderError::new(
                "provider_timeout",
                "The model operation timed out and was not retried.",
            ),
            TransportErrorKind::ModelUnavailable => LocalProviderError::new(
                "provider_model_unavailable",
                "The requested model is not available to the selected provider account.",
            ),
            TransportErrorKind::Http => LocalProviderError::new(
                "provider_request_failed",
                "The selected provider refused the bounded CVGnome request.",
            ),
            TransportErrorKind::TooLarge => LocalProviderError::new(
                "provider_response_too_large",
                "The selected provider returned more data than CVGnome allows.",
            ),
            TransportErrorKind::Incomplete => LocalProviderError::new(
                "provider_response_incomplete",
                "The selected provider stopped before completing the requested structured result.",
            ),
            TransportErrorKind::Refusal => LocalProviderError::new(
                "provider_response_refused",
                "The selected provider declined the structured draft request.",
            ),
            TransportErrorKind::UsageReceipt => LocalProviderError::new(
                "provider_spend_usage_missing",
                "The cloud provider did not return the valid usage receipt CVGnome requires.",
            ),
            TransportErrorKind::Invalid => LocalProviderError::new(
                "provider_response_invalid",
                "The selected provider returned data that CVGnome could not validate.",
            ),
        }
    }

    fn public_for_stage(self, stage: ProviderStage) -> LocalProviderError {
        match (self.kind, stage) {
            (TransportErrorKind::Invalid, ProviderStage::Embedding) => LocalProviderError::new(
                "provider_embedding_response_invalid",
                "The embedding model returned vectors that CVGnome could not validate.",
            ),
            (TransportErrorKind::TooLarge, ProviderStage::Embedding) => LocalProviderError::new(
                "provider_embedding_response_too_large",
                "The embedding model returned more vector data than CVGnome allows.",
            ),
            (TransportErrorKind::Invalid, ProviderStage::Chat) => LocalProviderError::new(
                "provider_chat_response_invalid",
                "The chat model did not return the strict structured result CVGnome requires.",
            ),
            (TransportErrorKind::TooLarge, ProviderStage::Chat) => LocalProviderError::new(
                "provider_chat_response_too_large",
                "The chat model returned more structured data than CVGnome allows.",
            ),
            _ => self.public(),
        }
    }

    fn tailoring_fail_code(self, stage: ProviderStage) -> &'static str {
        match self.kind {
            TransportErrorKind::Authentication => "provider_auth_failed",
            TransportErrorKind::RateLimited => match stage {
                ProviderStage::Embedding => "embedding_request_failed",
                ProviderStage::Chat => "chat_request_failed",
            },
            TransportErrorKind::Timeout => "provider_timeout",
            TransportErrorKind::Unavailable => "provider_unavailable",
            TransportErrorKind::ModelUnavailable => match stage {
                ProviderStage::Embedding => "embedding_model_unavailable",
                ProviderStage::Chat => "chat_model_unavailable",
            },
            TransportErrorKind::Invalid
            | TransportErrorKind::TooLarge
            | TransportErrorKind::Incomplete
            | TransportErrorKind::Refusal
            | TransportErrorKind::UsageReceipt => "provider_response_invalid",
            TransportErrorKind::Http => match stage {
                ProviderStage::Embedding => "embedding_request_failed",
                ProviderStage::Chat => "chat_request_failed",
            },
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum ProviderStage {
    Embedding,
    Chat,
}

impl ProviderStage {
    const fn spend_stage(self) -> ProviderSpendStage {
        match self {
            Self::Embedding => ProviderSpendStage::Embedding,
            Self::Chat => ProviderSpendStage::Chat,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum ProviderRoute {
    LmStudioModels,
    Models,
    Embeddings,
    ChatCompletions,
    Responses,
}

impl ProviderRoute {
    const fn path(self) -> &'static str {
        match self {
            Self::LmStudioModels => "/api/v1/models",
            Self::Models => "/v1/models",
            Self::Embeddings => "/v1/embeddings",
            Self::ChatCompletions => "/v1/chat/completions",
            Self::Responses => "/v1/responses",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct LocalProviderError {
    code: &'static str,
    message: &'static str,
}

impl LocalProviderError {
    const fn new(code: &'static str, message: &'static str) -> Self {
        Self { code, message }
    }

    const fn config_invalid() -> Self {
        Self::new(
            "local_provider_config_invalid",
            "The saved local model settings are invalid.",
        )
    }

    fn command_message(self) -> String {
        format!("{}: {}", self.code, self.message)
    }
}

fn normalized_base_url(value: &str) -> Result<String, LocalProviderError> {
    let normalized = value.trim();
    let invalid = || {
        LocalProviderError::new(
            "local_provider_url_invalid",
            "Use a numeric loopback URL such as http://127.0.0.1:1234/v1.",
        )
    };
    let authority = normalized
        .strip_prefix("http://")
        .and_then(|remainder| remainder.strip_suffix("/v1"))
        .ok_or_else(invalid)?;
    let (host, port_text) = if let Some(port) = authority.strip_prefix("127.0.0.1:") {
        ("127.0.0.1", port)
    } else if let Some(port) = authority.strip_prefix("[::1]:") {
        ("[::1]", port)
    } else {
        return Err(invalid());
    };
    let port = port_text
        .parse::<u16>()
        .ok()
        .filter(|port| *port != 0)
        .filter(|port| port.to_string() == port_text)
        .ok_or_else(invalid)?;
    Ok(format!("http://{host}:{port}/v1"))
}

fn validated_base_url(value: &str) -> Result<Url, LocalProviderError> {
    let normalized = normalized_base_url(value)?;
    Url::parse(&normalized).map_err(|_| LocalProviderError::config_invalid())
}

fn normalized_base_url_for_kind(
    provider_kind: LocalProviderKind,
    value: &str,
) -> Result<String, LocalProviderError> {
    match provider_kind {
        LocalProviderKind::OpenaiCompatibleLocal => normalized_base_url(value),
        LocalProviderKind::Openai if value.trim() == OPENAI_BASE_URL => {
            Ok(OPENAI_BASE_URL.to_string())
        }
        LocalProviderKind::Openai => Err(LocalProviderError::new(
            "provider_url_invalid",
            "OpenAI requests use only CVGnome's fixed official API endpoint.",
        )),
    }
}

fn validated_base_url_for_kind(
    provider_kind: LocalProviderKind,
    value: &str,
) -> Result<Url, LocalProviderError> {
    let normalized = normalized_base_url_for_kind(provider_kind, value)?;
    Url::parse(&normalized).map_err(|_| LocalProviderError::config_invalid())
}

fn validated_credential_generation(value: &str) -> Result<String, LocalProviderError> {
    let parsed = Uuid::parse_str(value).map_err(|_| LocalProviderError::config_invalid())?;
    if parsed.to_string() != value {
        return Err(LocalProviderError::config_invalid());
    }
    Ok(value.to_string())
}

fn validated_model_id(value: &str) -> Result<String, LocalProviderError> {
    if value.is_empty()
        || value.len() > MODEL_ID_LIMIT_BYTES
        || !value.bytes().all(|byte| (0x21..=0x7e).contains(&byte))
    {
        return Err(LocalProviderError::new(
            "provider_model_invalid",
            "Enter an exact printable model identifier supported by the selected provider.",
        ));
    }
    Ok(value.to_string())
}

fn validated_optional_model_id(
    value: Option<String>,
) -> Result<Option<String>, LocalProviderError> {
    value
        .map(|candidate| validated_model_id(&candidate))
        .transpose()
}

fn validated_display_name(value: Option<&str>, fallback: &str) -> String {
    let candidate = value.unwrap_or_default().trim();
    if candidate.is_empty()
        || candidate.chars().count() > MODEL_DISPLAY_NAME_LIMIT_CHARS
        || candidate.len() > MODEL_DISPLAY_NAME_LIMIT_BYTES
        || candidate.chars().any(char::is_control)
    {
        fallback.to_string()
    } else {
        candidate.to_string()
    }
}

fn config_path(data_dir: &Path) -> PathBuf {
    data_dir.join(CONFIG_FILENAME)
}

fn secure_regular_config_file(path: &Path) -> Result<(), LocalProviderError> {
    let metadata = fs::symlink_metadata(path).map_err(|_| LocalProviderError::config_invalid())?;
    if metadata.file_type().is_symlink()
        || !metadata.file_type().is_file()
        || metadata.len() == 0
        || metadata.len() > CONFIG_FILE_LIMIT_BYTES
    {
        return Err(LocalProviderError::config_invalid());
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o600))
            .map_err(|_| LocalProviderError::config_invalid())?;
    }
    Ok(())
}

fn migrate_legacy_settings(
    legacy: LegacyProviderSettingsV1,
) -> Result<StoredProviderSettings, LocalProviderError> {
    if legacy.schema_version != LEGACY_CONFIG_SCHEMA_VERSION
        || legacy.provider_kind != LegacyProviderKindV1::OpenaiCompatibleLocal
    {
        return Err(LocalProviderError::config_invalid());
    }
    StoredProviderSettings {
        schema_version: CONFIG_SCHEMA_VERSION,
        revision: legacy.revision,
        provider_kind: LocalProviderKind::OpenaiCompatibleLocal,
        local: ProviderConfiguration {
            revision: legacy.revision,
            base_url: legacy.base_url,
            chat_model_id: legacy.chat_model_id,
            embedding_model_id: legacy.embedding_model_id,
            verified_revision: legacy.verified_revision,
            verified_at_ms: legacy.verified_at_ms,
            embedding_dimension: legacy.embedding_dimension,
        },
        openai: ProviderConfiguration::openai_default(),
        openai_credential_generation: None,
    }
    .validated()
}

fn load_settings(data_dir: &Path) -> Result<StoredProviderSettings, LocalProviderError> {
    prepare_data_dir(data_dir).map_err(|_| LocalProviderError::config_invalid())?;
    let path = config_path(data_dir);
    match fs::symlink_metadata(&path) {
        Ok(_) => secure_regular_config_file(&path)?,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(StoredProviderSettings::default());
        }
        Err(_) => return Err(LocalProviderError::config_invalid()),
    }
    let file = File::open(&path).map_err(|_| LocalProviderError::config_invalid())?;
    let mut bytes = Vec::new();
    file.take(CONFIG_FILE_LIMIT_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| LocalProviderError::config_invalid())?;
    if bytes.is_empty() || bytes.len() as u64 > CONFIG_FILE_LIMIT_BYTES {
        return Err(LocalProviderError::config_invalid());
    }
    let value: Value =
        serde_json::from_slice(&bytes).map_err(|_| LocalProviderError::config_invalid())?;
    let schema_version = value
        .as_object()
        .and_then(|object| object.get("schema_version"))
        .and_then(Value::as_u64)
        .and_then(|value| u32::try_from(value).ok())
        .ok_or_else(LocalProviderError::config_invalid)?;
    match schema_version {
        LEGACY_CONFIG_SCHEMA_VERSION => {
            let legacy: LegacyProviderSettingsV1 =
                serde_json::from_value(value).map_err(|_| LocalProviderError::config_invalid())?;
            let migrated = migrate_legacy_settings(legacy)?;
            write_settings(data_dir, &migrated)?;
            Ok(migrated)
        }
        CONFIG_SCHEMA_VERSION => serde_json::from_value::<StoredProviderSettings>(value)
            .map_err(|_| LocalProviderError::config_invalid())?
            .validated(),
        _ => Err(LocalProviderError::config_invalid()),
    }
}

fn write_settings(
    data_dir: &Path,
    settings: &StoredProviderSettings,
) -> Result<(), LocalProviderError> {
    prepare_data_dir(data_dir).map_err(|_| LocalProviderError::config_invalid())?;
    let settings = settings.clone().validated()?;
    let destination = config_path(data_dir);
    match fs::symlink_metadata(&destination) {
        Ok(_) => secure_regular_config_file(&destination)?,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(_) => return Err(LocalProviderError::config_invalid()),
    }
    let bytes = serde_json::to_vec(&settings).map_err(|_| LocalProviderError::config_invalid())?;
    if bytes.is_empty() || bytes.len() as u64 > CONFIG_FILE_LIMIT_BYTES {
        return Err(LocalProviderError::config_invalid());
    }
    let mut temporary =
        NamedTempFile::new_in(data_dir).map_err(|_| LocalProviderError::config_invalid())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        temporary
            .as_file()
            .set_permissions(fs::Permissions::from_mode(0o600))
            .map_err(|_| LocalProviderError::config_invalid())?;
    }
    temporary
        .write_all(&bytes)
        .and_then(|_| temporary.flush())
        .map_err(|_| LocalProviderError::config_invalid())?;
    temporary
        .as_file()
        .sync_all()
        .map_err(|_| LocalProviderError::config_invalid())?;
    temporary
        .persist(&destination)
        .map_err(|_| LocalProviderError::config_invalid())?;
    #[cfg(unix)]
    {
        let _ = File::open(data_dir).and_then(|directory| directory.sync_all());
    }
    Ok(())
}

fn unix_timestamp_ms() -> Result<u64, LocalProviderError> {
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| LocalProviderError::config_invalid())?
        .as_millis();
    u64::try_from(millis)
        .ok()
        .filter(|value| *value > 0 && *value <= JAVASCRIPT_MAX_SAFE_INTEGER)
        .ok_or_else(LocalProviderError::config_invalid)
}

fn update_settings(
    current: StoredProviderSettings,
    expected_revision: u64,
    provider_kind: LocalProviderKind,
    base_url: String,
    chat_model_id: Option<String>,
    embedding_model_id: Option<String>,
) -> Result<(StoredProviderSettings, bool), LocalProviderError> {
    if expected_revision != current.revision {
        return Err(LocalProviderError::new(
            "provider_settings_conflict",
            "The model-provider settings changed. Reload them before saving.",
        ));
    }
    let normalized_base_url = normalized_base_url_for_kind(provider_kind, &base_url)?;
    let chat_model_id = validated_optional_model_id(chat_model_id)?;
    let embedding_model_id = validated_optional_model_id(embedding_model_id)?;
    let selected = match provider_kind {
        LocalProviderKind::OpenaiCompatibleLocal => &current.local,
        LocalProviderKind::Openai => &current.openai,
    };
    let configuration_changed = normalized_base_url != selected.base_url
        || chat_model_id != selected.chat_model_id
        || embedding_model_id != selected.embedding_model_id;
    let provider_changed = provider_kind != current.provider_kind;
    if !configuration_changed && !provider_changed {
        return Ok((current, false));
    }
    if current.revision >= JAVASCRIPT_MAX_SAFE_INTEGER
        || (configuration_changed && selected.revision >= JAVASCRIPT_MAX_SAFE_INTEGER)
    {
        return Err(LocalProviderError::config_invalid());
    }
    let mut updated = current;
    updated.revision += 1;
    updated.provider_kind = provider_kind;
    if configuration_changed {
        let selected = updated.selected_mut(provider_kind);
        selected.revision += 1;
        selected.base_url = normalized_base_url;
        selected.chat_model_id = chat_model_id;
        selected.embedding_model_id = embedding_model_id;
        selected.clear_verification();
    }
    Ok((updated, true))
}

fn select_provider(
    current: StoredProviderSettings,
    expected_revision: u64,
    provider_kind: LocalProviderKind,
) -> Result<(StoredProviderSettings, bool), LocalProviderError> {
    if expected_revision != current.revision {
        return Err(LocalProviderError::new(
            "provider_settings_conflict",
            "The model-provider settings changed. Reload them before switching providers.",
        ));
    }
    if current.provider_kind == provider_kind {
        return Ok((current, false));
    }
    if current.revision >= JAVASCRIPT_MAX_SAFE_INTEGER {
        return Err(LocalProviderError::config_invalid());
    }
    let mut updated = current;
    updated.revision += 1;
    updated.provider_kind = provider_kind;
    Ok((updated, true))
}

fn app_data_dir(app: &tauri::AppHandle) -> Result<PathBuf, LocalProviderError> {
    app.path()
        .app_data_dir()
        .map_err(|_| LocalProviderError::config_invalid())
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum CredentialFailure {
    Missing,
    Invalid,
    Unavailable,
}

impl CredentialFailure {
    fn public(self) -> LocalProviderError {
        match self {
            Self::Missing => LocalProviderError::new(
                "provider_credential_missing",
                "Store an OpenAI API key in the operating system credential manager first.",
            ),
            Self::Invalid => LocalProviderError::new(
                "provider_credential_invalid",
                "The OpenAI API key must be a bounded printable token without whitespace.",
            ),
            Self::Unavailable => LocalProviderError::new(
                "provider_credential_unavailable",
                "The operating system credential manager is unavailable or locked.",
            ),
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct CredentialEnvelope {
    version: u32,
    generation: String,
    secret: String,
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct CredentialEnvelopeRef<'a> {
    version: u32,
    generation: &'a str,
    secret: &'a str,
}

fn api_key_is_valid(value: &str) -> bool {
    (API_KEY_MIN_BYTES..=API_KEY_MAX_BYTES).contains(&value.len())
        && value.bytes().all(|byte| (0x21..=0x7e).contains(&byte))
}

fn validated_api_key(value: String) -> Result<Zeroizing<String>, CredentialFailure> {
    let value = Zeroizing::new(value);
    if !api_key_is_valid(&value) {
        return Err(CredentialFailure::Invalid);
    }
    Ok(value)
}

fn map_keyring_error(error: KeyringError) -> CredentialFailure {
    match error {
        KeyringError::NoEntry => CredentialFailure::Missing,
        _ => CredentialFailure::Unavailable,
    }
}

#[cfg(target_os = "macos")]
fn system_credential_store() -> Result<Arc<CredentialStore>, CredentialFailure> {
    let store = apple_native_keyring_store::keychain::Store::new().map_err(map_keyring_error)?;
    Ok(store)
}

#[cfg(target_os = "windows")]
fn system_credential_store() -> Result<Arc<CredentialStore>, CredentialFailure> {
    let store = windows_native_keyring_store::Store::new().map_err(map_keyring_error)?;
    Ok(store)
}

#[cfg(target_os = "linux")]
fn system_credential_store() -> Result<Arc<CredentialStore>, CredentialFailure> {
    let store = zbus_secret_service_keyring_store::Store::new().map_err(map_keyring_error)?;
    Ok(store)
}

#[cfg(not(any(target_os = "macos", target_os = "windows", target_os = "linux")))]
fn system_credential_store() -> Result<Arc<CredentialStore>, CredentialFailure> {
    Err(CredentialFailure::Unavailable)
}

fn read_openai_secret_with_store(
    store: &CredentialStore,
    expected_generation: Option<&str>,
) -> Result<Zeroizing<String>, CredentialFailure> {
    let expected_generation = expected_generation.ok_or(CredentialFailure::Missing)?;
    let entry = store
        .build(OPENAI_CREDENTIAL_SERVICE, OPENAI_CREDENTIAL_ACCOUNT, None)
        .map_err(map_keyring_error)?;
    let encoded = Zeroizing::new(entry.get_password().map_err(map_keyring_error)?);
    let envelope: CredentialEnvelope =
        serde_json::from_str(&encoded).map_err(|_| CredentialFailure::Unavailable)?;
    let secret = Zeroizing::new(envelope.secret);
    if envelope.version != CREDENTIAL_ENVELOPE_VERSION
        || validated_credential_generation(&envelope.generation).is_err()
        || envelope.generation != expected_generation
    {
        return Err(CredentialFailure::Missing);
    }
    if !api_key_is_valid(&secret) {
        return Err(CredentialFailure::Invalid);
    }
    Ok(secret)
}

fn write_openai_secret_with_store(
    store: &CredentialStore,
    generation: &str,
    secret: &str,
) -> Result<(), CredentialFailure> {
    validated_credential_generation(generation).map_err(|_| CredentialFailure::Invalid)?;
    let entry = store
        .build(OPENAI_CREDENTIAL_SERVICE, OPENAI_CREDENTIAL_ACCOUNT, None)
        .map_err(map_keyring_error)?;
    let encoded = Zeroizing::new(
        serde_json::to_string(&CredentialEnvelopeRef {
            version: CREDENTIAL_ENVELOPE_VERSION,
            generation,
            secret,
        })
        .map_err(|_| CredentialFailure::Unavailable)?,
    );
    entry.set_password(&encoded).map_err(map_keyring_error)
}

fn delete_openai_secret_with_store(store: &CredentialStore) -> Result<(), CredentialFailure> {
    let entry = store
        .build(OPENAI_CREDENTIAL_SERVICE, OPENAI_CREDENTIAL_ACCOUNT, None)
        .map_err(map_keyring_error)?;
    match entry.delete_credential() {
        Ok(()) | Err(KeyringError::NoEntry) => Ok(()),
        Err(error) => Err(map_keyring_error(error)),
    }
}

async fn read_openai_secret(
    expected_generation: Option<String>,
) -> Result<Zeroizing<String>, CredentialFailure> {
    tauri::async_runtime::spawn_blocking(move || {
        let store = system_credential_store()?;
        read_openai_secret_with_store(store.as_ref(), expected_generation.as_deref())
    })
    .await
    .map_err(|_| CredentialFailure::Unavailable)?
}

async fn store_openai_secret(
    generation: String,
    secret: Zeroizing<String>,
) -> Result<(), CredentialFailure> {
    tauri::async_runtime::spawn_blocking(move || {
        let store = system_credential_store()?;
        write_openai_secret_with_store(store.as_ref(), &generation, &secret)
    })
    .await
    .map_err(|_| CredentialFailure::Unavailable)?
}

async fn delete_openai_secret() -> Result<(), CredentialFailure> {
    tauri::async_runtime::spawn_blocking(move || {
        let store = system_credential_store()?;
        delete_openai_secret_with_store(store.as_ref())
    })
    .await
    .map_err(|_| CredentialFailure::Unavailable)?
}

async fn openai_credential_state(expected_generation: Option<String>) -> ProviderCredentialState {
    if expected_generation.is_none() {
        return ProviderCredentialState::Missing;
    }
    match read_openai_secret(expected_generation).await {
        Ok(_) => ProviderCredentialState::Stored,
        Err(CredentialFailure::Missing | CredentialFailure::Invalid) => {
            ProviderCredentialState::Missing
        }
        Err(CredentialFailure::Unavailable) => ProviderCredentialState::Unavailable,
    }
}

async fn credential_state_for_settings(
    settings: &StoredProviderSettings,
) -> ProviderCredentialState {
    match settings.provider_kind {
        LocalProviderKind::OpenaiCompatibleLocal => ProviderCredentialState::NotRequired,
        LocalProviderKind::Openai => {
            openai_credential_state(settings.openai_credential_generation.clone()).await
        }
    }
}

async fn settings_view(settings: &StoredProviderSettings) -> LocalProviderSettings {
    let credential_state = credential_state_for_settings(settings).await;
    settings.view(credential_state)
}

fn set_credential_generation(
    mut settings: StoredProviderSettings,
    generation: Option<String>,
) -> Result<StoredProviderSettings, LocalProviderError> {
    if settings.revision >= JAVASCRIPT_MAX_SAFE_INTEGER
        || settings.openai.revision >= JAVASCRIPT_MAX_SAFE_INTEGER
    {
        return Err(LocalProviderError::config_invalid());
    }
    settings.revision += 1;
    settings.openai.revision += 1;
    settings.openai_credential_generation = generation;
    settings.openai.clear_verification();
    settings.validated()
}

fn provider_client(
    provider_kind: LocalProviderKind,
    timeout: Duration,
) -> Result<Client, TransportError> {
    let builder = Client::builder()
        .no_proxy()
        .redirect(redirect::Policy::none())
        .retry(reqwest::retry::never())
        .connect_timeout(match provider_kind {
            LocalProviderKind::OpenaiCompatibleLocal => CONNECT_TIMEOUT,
            LocalProviderKind::Openai => CLOUD_CONNECT_TIMEOUT,
        })
        .timeout(timeout);
    let builder = match provider_kind {
        LocalProviderKind::OpenaiCompatibleLocal => builder,
        LocalProviderKind::Openai => builder.https_only(true),
    };
    builder
        .build()
        .map_err(|_| TransportError::new(TransportErrorKind::Unavailable))
}

fn route_url(base_url: &Url, route: ProviderRoute) -> Url {
    let mut result = base_url.clone();
    result.set_path(route.path());
    result.set_query(None);
    result.set_fragment(None);
    result
}

fn classify_reqwest_error(error: &reqwest::Error) -> TransportError {
    if error.is_timeout() {
        TransportError::new(TransportErrorKind::Timeout)
    } else {
        TransportError::new(TransportErrorKind::Unavailable)
    }
}

fn classify_status(status: StatusCode) -> Result<(), TransportError> {
    if status.is_success() {
        Ok(())
    } else if matches!(status, StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN) {
        Err(TransportError::new(TransportErrorKind::Authentication))
    } else if status == StatusCode::TOO_MANY_REQUESTS {
        Err(TransportError::new(TransportErrorKind::RateLimited))
    } else if status == StatusCode::NOT_FOUND {
        Err(TransportError::new(TransportErrorKind::ModelUnavailable))
    } else {
        Err(TransportError::new(TransportErrorKind::Http))
    }
}

async fn bounded_json_response(
    mut response: reqwest::Response,
    maximum_bytes: usize,
) -> Result<Value, TransportError> {
    classify_status(response.status())?;
    if response
        .content_length()
        .is_some_and(|length| length > maximum_bytes as u64)
    {
        return Err(TransportError::new(TransportErrorKind::TooLarge));
    }
    let mut bytes = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(|error| {
        if error.is_timeout() {
            TransportError::new(TransportErrorKind::Timeout)
        } else {
            TransportError::new(TransportErrorKind::Unavailable)
        }
    })? {
        if bytes.len().saturating_add(chunk.len()) > maximum_bytes {
            return Err(TransportError::new(TransportErrorKind::TooLarge));
        }
        bytes.extend_from_slice(&chunk);
    }
    if bytes.is_empty() {
        return Err(TransportError::new(TransportErrorKind::Invalid));
    }
    serde_json::from_slice(&bytes).map_err(|_| TransportError::new(TransportErrorKind::Invalid))
}

async fn get_json(
    provider_kind: LocalProviderKind,
    api_key: Option<&str>,
    url: Url,
    response_limit: usize,
    timeout: Duration,
) -> Result<Value, TransportError> {
    let client = provider_client(provider_kind, timeout)?;
    let response = authenticated_get_request(&client, provider_kind, api_key, url)?
        .send()
        .await
        .map_err(|error| classify_reqwest_error(&error))?;
    bounded_json_response(response, response_limit).await
}

fn authenticated_get_request(
    client: &Client,
    provider_kind: LocalProviderKind,
    api_key: Option<&str>,
    url: Url,
) -> Result<RequestBuilder, TransportError> {
    let request = client.get(url).header(ACCEPT, "application/json");
    match (provider_kind, api_key) {
        (LocalProviderKind::OpenaiCompatibleLocal, None) => Ok(request),
        (LocalProviderKind::Openai, Some(secret)) => {
            Ok(request.header(AUTHORIZATION, authorization_header(secret)?))
        }
        _ => Err(TransportError::new(TransportErrorKind::Authentication)),
    }
}

fn authorization_header(secret: &str) -> Result<HeaderValue, TransportError> {
    let bearer = Zeroizing::new(format!("Bearer {secret}"));
    let mut header = HeaderValue::from_str(&bearer)
        .map_err(|_| TransportError::new(TransportErrorKind::Authentication))?;
    header.set_sensitive(true);
    Ok(header)
}

async fn post_json(
    provider_kind: LocalProviderKind,
    api_key: Option<&str>,
    url: Url,
    body: &Value,
    request_limit: usize,
    response_limit: usize,
    timeout: Duration,
) -> Result<Value, TransportError> {
    let encoded =
        serde_json::to_vec(body).map_err(|_| TransportError::new(TransportErrorKind::Invalid))?;
    if encoded.is_empty() || encoded.len() > request_limit {
        return Err(TransportError::new(TransportErrorKind::TooLarge));
    }
    let client = provider_client(provider_kind, timeout)?;
    let request = client
        .post(url)
        .header(ACCEPT, "application/json")
        .header(CONTENT_TYPE, "application/json")
        .body(encoded);
    let request = match (provider_kind, api_key) {
        (LocalProviderKind::OpenaiCompatibleLocal, None) => request,
        (LocalProviderKind::Openai, Some(secret)) => {
            request.header(AUTHORIZATION, authorization_header(secret)?)
        }
        _ => return Err(TransportError::new(TransportErrorKind::Authentication)),
    };
    let response = request
        .send()
        .await
        .map_err(|error| classify_reqwest_error(&error))?;
    bounded_json_response(response, response_limit).await
}

fn lm_studio_model_capabilities(value: Option<&str>) -> Vec<ProviderModelCapability> {
    match value
        .unwrap_or_default()
        .trim()
        .to_ascii_lowercase()
        .as_str()
    {
        "llm" | "chat" => vec![ProviderModelCapability::Chat],
        "embedding" | "embeddings" => vec![ProviderModelCapability::Embedding],
        _ => Vec::new(),
    }
}

fn model_is_loaded(model: &Map<String, Value>) -> bool {
    if model.get("loaded").and_then(Value::as_bool) == Some(true) {
        return true;
    }
    if model
        .get("loaded_instances")
        .and_then(Value::as_array)
        .is_some_and(|instances| !instances.is_empty())
    {
        return true;
    }
    ["state", "status"]
        .iter()
        .filter_map(|key| model.get(*key).and_then(Value::as_str))
        .any(|value| value.eq_ignore_ascii_case("loaded"))
}

fn model_sort_rank(model: &ProviderModel) -> u8 {
    match model.capabilities.as_slice() {
        [ProviderModelCapability::Chat] => 0,
        [ProviderModelCapability::Embedding] => 1,
        _ => 2,
    }
}

fn sorted_bounded_models(
    models_by_id: BTreeMap<String, ProviderModel>,
) -> (Vec<ProviderModel>, bool) {
    let truncated = models_by_id.len() > MODEL_LIST_LIMIT;
    let mut models: Vec<_> = models_by_id.into_values().collect();
    models.sort_by(|left, right| {
        model_sort_rank(left)
            .cmp(&model_sort_rank(right))
            .then_with(|| left.display_name.cmp(&right.display_name))
            .then_with(|| left.id.cmp(&right.id))
    });
    models.truncate(MODEL_LIST_LIMIT);
    (models, truncated)
}

fn parse_lm_studio_models(value: Value) -> Result<ProviderModelDiscovery, TransportError> {
    let raw_models = value
        .as_object()
        .and_then(|object| object.get("models"))
        .and_then(Value::as_array)
        .ok_or_else(|| TransportError::new(TransportErrorKind::Invalid))?;
    let mut models_by_id: BTreeMap<String, ProviderModel> = BTreeMap::new();
    for raw in raw_models {
        let object = raw
            .as_object()
            .ok_or_else(|| TransportError::new(TransportErrorKind::Invalid))?;
        let raw_id = object
            .get("key")
            .and_then(Value::as_str)
            .ok_or_else(|| TransportError::new(TransportErrorKind::Invalid))?;
        let id = validated_model_id(raw_id)
            .map_err(|_| TransportError::new(TransportErrorKind::Invalid))?;
        let capabilities = lm_studio_model_capabilities(object.get("type").and_then(Value::as_str));
        let model = ProviderModel {
            display_name: validated_display_name(
                object.get("display_name").and_then(Value::as_str),
                &id,
            ),
            capability_source: if capabilities.is_empty() {
                ProviderModelCapabilitySource::Unknown
            } else {
                ProviderModelCapabilitySource::Provider
            },
            capabilities,
            loaded: Some(model_is_loaded(object)),
            id: id.clone(),
        };
        if let Some(existing) = models_by_id.get(&id) {
            if existing != &model {
                return Err(TransportError::new(TransportErrorKind::Invalid));
            }
        } else {
            models_by_id.insert(id, model);
        }
    }
    let (models, truncated) = sorted_bounded_models(models_by_id);
    Ok(ProviderModelDiscovery {
        provider_kind: LocalProviderKind::OpenaiCompatibleLocal,
        models,
        truncated,
    })
}

fn is_dated_snapshot(model_id: &str, base_id: &str) -> bool {
    let Some(suffix) = model_id
        .strip_prefix(base_id)
        .and_then(|value| value.strip_prefix('-'))
    else {
        return false;
    };
    let bytes = suffix.as_bytes();
    if bytes.len() != 10
        || bytes[4] != b'-'
        || bytes[7] != b'-'
        || bytes
            .iter()
            .enumerate()
            .any(|(index, byte)| index != 4 && index != 7 && !byte.is_ascii_digit())
    {
        return false;
    }
    let month = suffix[5..7].parse::<u8>().ok();
    let day = suffix[8..10].parse::<u8>().ok();
    matches!(month, Some(1..=12)) && matches!(day, Some(1..=31))
}

fn openai_model_capabilities(model_id: &str) -> Vec<ProviderModelCapability> {
    if matches!(
        model_id,
        "text-embedding-3-small" | "text-embedding-3-large" | "text-embedding-ada-002"
    ) {
        vec![ProviderModelCapability::Embedding]
    } else if model_id == "gpt-5.4-mini" || is_dated_snapshot(model_id, "gpt-5.4-mini") {
        vec![ProviderModelCapability::Chat]
    } else {
        Vec::new()
    }
}

fn parse_openai_models(value: Value) -> Result<ProviderModelDiscovery, TransportError> {
    let envelope = value
        .as_object()
        .ok_or_else(|| TransportError::new(TransportErrorKind::Invalid))?;
    if envelope.get("object").and_then(Value::as_str) != Some("list") {
        return Err(TransportError::new(TransportErrorKind::Invalid));
    }
    let raw_models = envelope
        .get("data")
        .and_then(Value::as_array)
        .ok_or_else(|| TransportError::new(TransportErrorKind::Invalid))?;
    let mut models_by_id: BTreeMap<String, ProviderModel> = BTreeMap::new();
    for raw in raw_models {
        let object = raw
            .as_object()
            .ok_or_else(|| TransportError::new(TransportErrorKind::Invalid))?;
        if object.get("object").and_then(Value::as_str) != Some("model")
            || object
                .get("created")
                .and_then(Value::as_u64)
                .filter(|created| *created > 0 && *created <= JAVASCRIPT_MAX_SAFE_INTEGER)
                .is_none()
            || object
                .get("owned_by")
                .and_then(Value::as_str)
                .filter(|owner| {
                    !owner.is_empty()
                        && owner.len() <= MODEL_ID_LIMIT_BYTES
                        && owner.bytes().all(|byte| (0x21..=0x7e).contains(&byte))
                })
                .is_none()
        {
            return Err(TransportError::new(TransportErrorKind::Invalid));
        }
        let raw_id = object
            .get("id")
            .and_then(Value::as_str)
            .ok_or_else(|| TransportError::new(TransportErrorKind::Invalid))?;
        let id = validated_model_id(raw_id)
            .map_err(|_| TransportError::new(TransportErrorKind::Invalid))?;
        let capabilities = openai_model_capabilities(&id);
        models_by_id
            .entry(id.clone())
            .or_insert_with(|| ProviderModel {
                display_name: id.clone(),
                capability_source: if capabilities.is_empty() {
                    ProviderModelCapabilitySource::Unknown
                } else {
                    ProviderModelCapabilitySource::CvgnomeCatalog
                },
                capabilities,
                loaded: None,
                id,
            });
    }
    let (models, truncated) = sorted_bounded_models(models_by_id);
    Ok(ProviderModelDiscovery {
        provider_kind: LocalProviderKind::Openai,
        models,
        truncated,
    })
}

async fn discover_lm_studio_models_at(
    base_url: &Url,
) -> Result<ProviderModelDiscovery, TransportError> {
    let value = get_json(
        LocalProviderKind::OpenaiCompatibleLocal,
        None,
        route_url(base_url, ProviderRoute::LmStudioModels),
        MODEL_DISCOVERY_RESPONSE_LIMIT,
        DISCOVERY_TIMEOUT,
    )
    .await?;
    parse_lm_studio_models(value)
}

async fn discover_openai_models_at(
    base_url: &Url,
    api_key: &str,
) -> Result<ProviderModelDiscovery, TransportError> {
    let value = get_json(
        LocalProviderKind::Openai,
        Some(api_key),
        route_url(base_url, ProviderRoute::Models),
        MODEL_DISCOVERY_RESPONSE_LIMIT,
        DISCOVERY_TIMEOUT,
    )
    .await?;
    parse_openai_models(value)
}

fn require_selected_models(
    settings: &LocalProviderSettings,
) -> Result<(String, String), LocalProviderError> {
    let chat_model_id = settings.chat_model_id.clone().ok_or_else(|| {
        LocalProviderError::new(
            "provider_chat_model_required",
            "Choose a chat model before testing the provider.",
        )
    })?;
    let embedding_model_id = settings.embedding_model_id.clone().ok_or_else(|| {
        LocalProviderError::new(
            "provider_embedding_model_required",
            "Choose an embedding model before testing the provider.",
        )
    })?;
    if chat_model_id == embedding_model_id {
        return Err(LocalProviderError::new(
            "provider_models_must_differ",
            "Choose separate chat and embedding models.",
        ));
    }
    Ok((chat_model_id, embedding_model_id))
}

fn require_selected_typed_models(
    settings: &LocalProviderSettings,
    discovery: &ProviderModelDiscovery,
) -> Result<(String, String), LocalProviderError> {
    let (chat_model_id, embedding_model_id) = require_selected_models(settings)?;
    let chat = discovery
        .models
        .iter()
        .find(|model| model.id == chat_model_id);
    let embedding = discovery
        .models
        .iter()
        .find(|model| model.id == embedding_model_id);
    if !chat.is_some_and(|model| model.capabilities.contains(&ProviderModelCapability::Chat)) {
        return Err(LocalProviderError::new(
            "local_provider_chat_model_unavailable",
            "The selected chat model is not available as an LM Studio chat model.",
        ));
    }
    if !embedding.is_some_and(|model| {
        model
            .capabilities
            .contains(&ProviderModelCapability::Embedding)
    }) {
        return Err(LocalProviderError::new(
            "local_provider_embedding_model_unavailable",
            "The selected embedding model is not available as an LM Studio embedding model.",
        ));
    }
    Ok((chat_model_id, embedding_model_id))
}

fn test_response_format() -> Value {
    json!({
        "type": "json_schema",
        "json_schema": {
            "name": "cvgnome_local_provider_schema_test",
            "strict": true,
            "schema": {
                "type": "object",
                "additionalProperties": false,
                "required": ["basics", "work"],
                "properties": {
                    "basics": {
                        "type": "object",
                        "additionalProperties": false,
                        "required": ["label", "summary"],
                        "properties": {
                            "label": {"type": "string", "enum": [TEST_CHAT_LABEL]},
                            "summary": {"type": "string", "enum": [TEST_CHAT_SUMMARY]}
                        }
                    },
                    "work": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 1,
                        "items": {
                            "type": "object",
                            "additionalProperties": false,
                            "required": ["source_index", "summary", "highlights"],
                            "properties": {
                                "source_index": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "maximum": 0
                                },
                                "summary": {
                                    "type": "string",
                                    "enum": [TEST_CHAT_WORK_SUMMARY]
                                },
                                "highlights": {
                                    "type": "array",
                                    "minItems": 1,
                                    "maxItems": 1,
                                    "items": {
                                        "type": "string",
                                        "enum": [TEST_CHAT_HIGHLIGHT]
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    })
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct TestChatContent {
    basics: TestChatBasics,
    work: Vec<TestChatWorkItem>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct TestChatBasics {
    label: String,
    summary: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct TestChatWorkItem {
    source_index: u64,
    summary: String,
    highlights: Vec<String>,
}

fn parse_chat_response_text(value: &Value) -> Result<String, TransportError> {
    let choices = value
        .as_object()
        .and_then(|object| object.get("choices"))
        .and_then(Value::as_array)
        .ok_or_else(|| TransportError::new(TransportErrorKind::Invalid))?;
    if choices.len() != 1 {
        return Err(TransportError::new(TransportErrorKind::Invalid));
    }
    let choice = choices[0]
        .as_object()
        .ok_or_else(|| TransportError::new(TransportErrorKind::Invalid))?;
    if choice.get("finish_reason").and_then(Value::as_str) != Some("stop") {
        return Err(TransportError::new(TransportErrorKind::Invalid));
    }
    let message = choice
        .get("message")
        .and_then(Value::as_object)
        .ok_or_else(|| TransportError::new(TransportErrorKind::Invalid))?;
    // LM Studio includes `tool_calls: []` on ordinary completions, so presence
    // alone is not evidence that a tool was invoked.
    let made_tool_call = match message.get("tool_calls") {
        None | Some(Value::Null) => false,
        Some(Value::Array(calls)) => !calls.is_empty(),
        Some(_) => true,
    };
    let refused = match message.get("refusal") {
        None | Some(Value::Null) => false,
        Some(Value::String(refusal)) => !refusal.trim().is_empty(),
        Some(_) => true,
    };
    if made_tool_call || refused {
        return Err(TransportError::new(TransportErrorKind::Invalid));
    }
    let content = message
        .get("content")
        .and_then(Value::as_str)
        .ok_or_else(|| TransportError::new(TransportErrorKind::Invalid))?;
    if content.is_empty() || content.len() > CHAT_RESPONSE_LIMIT {
        return Err(TransportError::new(TransportErrorKind::Invalid));
    }
    Ok(content.to_string())
}

fn validate_test_chat_content(content: &str) -> Result<(), TransportError> {
    let parsed: TestChatContent = serde_json::from_str(content)
        .map_err(|_| TransportError::new(TransportErrorKind::Invalid))?;
    if parsed.basics.label != TEST_CHAT_LABEL
        || parsed.basics.summary != TEST_CHAT_SUMMARY
        || parsed.work.len() != 1
        || parsed.work[0].source_index != 0
        || parsed.work[0].summary != TEST_CHAT_WORK_SUMMARY
        || parsed.work[0].highlights != [TEST_CHAT_HIGHLIGHT]
    {
        return Err(TransportError::new(TransportErrorKind::Invalid));
    }
    Ok(())
}

fn parse_embedding_vectors(
    value: &Value,
    expected_count: usize,
    expected_dimension: Option<usize>,
) -> Result<Vec<Vec<f64>>, TransportError> {
    if !(1..=MAX_TAILORING_EMBEDDING_INPUTS).contains(&expected_count) {
        return Err(TransportError::new(TransportErrorKind::Invalid));
    }
    let data = value
        .as_object()
        .and_then(|object| object.get("data"))
        .and_then(Value::as_array)
        .ok_or_else(|| TransportError::new(TransportErrorKind::Invalid))?;
    if data.len() != expected_count {
        return Err(TransportError::new(TransportErrorKind::Invalid));
    }
    let mut ordered: Vec<Option<Vec<f64>>> = vec![None; expected_count];
    let mut common_dimension = expected_dimension;
    for item in data {
        let object = item
            .as_object()
            .ok_or_else(|| TransportError::new(TransportErrorKind::Invalid))?;
        let index = object
            .get("index")
            .and_then(Value::as_u64)
            .and_then(|index| usize::try_from(index).ok())
            .filter(|index| *index < expected_count)
            .ok_or_else(|| TransportError::new(TransportErrorKind::Invalid))?;
        if ordered[index].is_some() {
            return Err(TransportError::new(TransportErrorKind::Invalid));
        }
        let raw_vector = object
            .get("embedding")
            .and_then(Value::as_array)
            .ok_or_else(|| TransportError::new(TransportErrorKind::Invalid))?;
        let dimension = raw_vector.len();
        if !(1..=MAX_EMBEDDING_DIMENSION).contains(&dimension)
            || common_dimension.is_some_and(|expected| expected != dimension)
        {
            return Err(TransportError::new(TransportErrorKind::Invalid));
        }
        common_dimension = Some(dimension);
        let mut vector = Vec::with_capacity(dimension);
        for raw in raw_vector {
            let number = raw
                .as_f64()
                .filter(|number| number.is_finite())
                .ok_or_else(|| TransportError::new(TransportErrorKind::Invalid))?;
            vector.push(number);
        }
        ordered[index] = Some(vector);
    }
    ordered
        .into_iter()
        .map(|item| item.ok_or_else(|| TransportError::new(TransportErrorKind::Invalid)))
        .collect()
}

#[derive(Debug)]
struct EmbeddingProviderResponse {
    vectors: Vec<Vec<f64>>,
    usage: Option<ProviderTokenUsage>,
}

#[derive(Debug)]
struct TextProviderResponse {
    text: String,
    usage: Option<ProviderTokenUsage>,
}

fn parse_openai_embedding_usage(value: &Value) -> Result<ProviderTokenUsage, TransportError> {
    let invalid = || TransportError::new(TransportErrorKind::UsageReceipt);
    let usage = value
        .as_object()
        .and_then(|object| object.get("usage"))
        .and_then(Value::as_object)
        .ok_or_else(invalid)?;
    let input_tokens = usage
        .get("prompt_tokens")
        .and_then(Value::as_u64)
        .filter(|value| *value <= JAVASCRIPT_MAX_SAFE_INTEGER)
        .ok_or_else(invalid)?;
    let total_tokens = usage
        .get("total_tokens")
        .and_then(Value::as_u64)
        .filter(|value| *value <= JAVASCRIPT_MAX_SAFE_INTEGER)
        .ok_or_else(invalid)?;
    if input_tokens == 0 || total_tokens != input_tokens {
        return Err(invalid());
    }
    Ok(ProviderTokenUsage {
        input_tokens,
        output_tokens: 0,
    })
}

fn parse_openai_response_usage(value: &Value) -> Result<ProviderTokenUsage, TransportError> {
    let invalid = || TransportError::new(TransportErrorKind::UsageReceipt);
    let usage = value
        .as_object()
        .and_then(|object| object.get("usage"))
        .and_then(Value::as_object)
        .ok_or_else(invalid)?;
    let input_tokens = usage
        .get("input_tokens")
        .and_then(Value::as_u64)
        .filter(|value| *value <= JAVASCRIPT_MAX_SAFE_INTEGER)
        .ok_or_else(invalid)?;
    let output_tokens = usage
        .get("output_tokens")
        .and_then(Value::as_u64)
        .filter(|value| *value <= JAVASCRIPT_MAX_SAFE_INTEGER)
        .ok_or_else(invalid)?;
    let total_tokens = usage
        .get("total_tokens")
        .and_then(Value::as_u64)
        .filter(|value| *value <= JAVASCRIPT_MAX_SAFE_INTEGER)
        .ok_or_else(invalid)?;
    if input_tokens == 0
        || output_tokens == 0
        || input_tokens.checked_add(output_tokens) != Some(total_tokens)
    {
        return Err(invalid());
    }
    Ok(ProviderTokenUsage {
        input_tokens,
        output_tokens,
    })
}

fn conservative_input_token_bound(body: &Value) -> Result<u64, LocalProviderError> {
    serde_json::to_vec(body)
        .ok()
        .and_then(|encoded| u64::try_from(encoded.len()).ok())
        .filter(|value| (1..=JAVASCRIPT_MAX_SAFE_INTEGER).contains(value))
        .ok_or_else(|| {
            LocalProviderError::new(
                "provider_spend_estimate_invalid",
                "CVGnome could not safely estimate this cloud request.",
            )
        })
}

fn finish_provider_spend(
    data_dir: &Path,
    reservation: Option<&ProviderSpendReservation>,
    usage: Option<ProviderTokenUsage>,
) -> Result<(), String> {
    if let Err(error) = complete_provider_spend(data_dir, reservation, usage) {
        // A missing/invalid provider receipt or local persistence failure must
        // never turn the durable upper-bound reservation into a zero-cost call.
        let _ = cancel_provider_spend(data_dir, reservation, true);
        return Err(error.command_message());
    }
    Ok(())
}

fn leave_provider_spend_uncertain(data_dir: &Path, reservation: Option<&ProviderSpendReservation>) {
    // Any failure after the durable reservation may have crossed the provider
    // boundary. Count its upper bound when possible; if persistence itself is
    // unavailable, the existing on-disk reservation remains fail-closed.
    let _ = cancel_provider_spend(data_dir, reservation, true);
}

fn settle_failed_provider_spend(
    data_dir: &Path,
    reservation: Option<&ProviderSpendReservation>,
    error: TransportError,
) -> Result<(), String> {
    if let Some(usage) = error.usage {
        finish_provider_spend(data_dir, reservation, Some(usage))
    } else {
        leave_provider_spend_uncertain(data_dir, reservation);
        Ok(())
    }
}

fn embedding_request_body(model_id: &str, inputs: &[String]) -> Value {
    json!({
        "model": model_id,
        "input": inputs,
        "encoding_format": "float",
    })
}

fn parse_embedding_provider_response(
    provider_kind: LocalProviderKind,
    value: &Value,
    expected_count: usize,
    expected_dimension: Option<usize>,
) -> Result<EmbeddingProviderResponse, TransportError> {
    let usage = match provider_kind {
        LocalProviderKind::OpenaiCompatibleLocal => None,
        LocalProviderKind::Openai => Some(parse_openai_embedding_usage(value)?),
    };
    let vectors = parse_embedding_vectors(value, expected_count, expected_dimension)
        .map_err(|error| error.with_usage(usage))?;
    Ok(EmbeddingProviderResponse { vectors, usage })
}

async fn request_embeddings(
    provider_kind: LocalProviderKind,
    api_key: Option<&str>,
    base_url: &Url,
    model_id: &str,
    inputs: &[String],
    expected_dimension: Option<usize>,
    timeout: Duration,
) -> Result<EmbeddingProviderResponse, TransportError> {
    let body = embedding_request_body(model_id, inputs);
    let value = post_json(
        provider_kind,
        api_key,
        route_url(base_url, ProviderRoute::Embeddings),
        &body,
        EMBEDDING_REQUEST_LIMIT,
        EMBEDDING_RESPONSE_LIMIT,
        timeout,
    )
    .await?;
    parse_embedding_provider_response(provider_kind, &value, inputs.len(), expected_dimension)
}

#[cfg(test)]
async fn test_embedding_model(
    provider_kind: LocalProviderKind,
    api_key: Option<&str>,
    base_url: &Url,
    model_id: &str,
) -> Result<(usize, Option<ProviderTokenUsage>), TransportError> {
    let response = request_embeddings(
        provider_kind,
        api_key,
        base_url,
        model_id,
        &[TEST_EMBEDDING_INPUT.to_string()],
        None,
        MODEL_TEST_TIMEOUT,
    )
    .await?;
    Ok((response.vectors[0].len(), response.usage))
}

async fn test_chat_model(base_url: &Url, model_id: &str) -> Result<(), TransportError> {
    let body = json!({
        "model": model_id,
        "messages": [
            {"role": "system", "content": TEST_CHAT_SYSTEM},
            {"role": "user", "content": TEST_CHAT_USER}
        ],
        "response_format": test_response_format(),
        "temperature": 0,
        "max_tokens": 1_024,
        "stream": false
    });
    let value = post_json(
        LocalProviderKind::OpenaiCompatibleLocal,
        None,
        route_url(base_url, ProviderRoute::ChatCompletions),
        &body,
        8 * 1024,
        CHAT_RESPONSE_LIMIT,
        MODEL_TEST_TIMEOUT,
    )
    .await?;
    let content = parse_chat_response_text(&value)?;
    validate_test_chat_content(&content)
}

fn validate_openai_strict_schema(value: &Value) -> Result<(), TransportError> {
    match value {
        Value::Object(object) => {
            if object.get("type").and_then(Value::as_str) == Some("object") {
                let properties = object
                    .get("properties")
                    .and_then(Value::as_object)
                    .ok_or_else(|| TransportError::new(TransportErrorKind::Invalid))?;
                let required = object
                    .get("required")
                    .and_then(Value::as_array)
                    .ok_or_else(|| TransportError::new(TransportErrorKind::Invalid))?;
                if object.get("additionalProperties").and_then(Value::as_bool) != Some(false)
                    || required.len() != properties.len()
                    || required.iter().any(|name| name.as_str().is_none())
                    || properties.keys().any(|property| {
                        required
                            .iter()
                            .filter(|name| name.as_str() == Some(property.as_str()))
                            .count()
                            != 1
                    })
                {
                    return Err(TransportError::new(TransportErrorKind::Invalid));
                }
            }
            for nested in object.values() {
                validate_openai_strict_schema(nested)?;
            }
        }
        Value::Array(values) => {
            for nested in values {
                validate_openai_strict_schema(nested)?;
            }
        }
        _ => {}
    }
    Ok(())
}

fn responses_text_format(response_format: &Value) -> Result<Value, TransportError> {
    let object = response_format
        .as_object()
        .filter(|object| object.get("type").and_then(Value::as_str) == Some("json_schema"))
        .ok_or_else(|| TransportError::new(TransportErrorKind::Invalid))?;
    let schema = object
        .get("json_schema")
        .and_then(Value::as_object)
        .ok_or_else(|| TransportError::new(TransportErrorKind::Invalid))?;
    let name = schema
        .get("name")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| TransportError::new(TransportErrorKind::Invalid))?;
    let strict = schema
        .get("strict")
        .and_then(Value::as_bool)
        .filter(|value| *value)
        .ok_or_else(|| TransportError::new(TransportErrorKind::Invalid))?;
    let json_schema = schema
        .get("schema")
        .filter(|value| value.is_object())
        .ok_or_else(|| TransportError::new(TransportErrorKind::Invalid))?;
    validate_openai_strict_schema(json_schema)?;
    Ok(json!({
        "type": "json_schema",
        "name": name,
        "strict": strict,
        "schema": json_schema,
    }))
}

fn openai_response_body(
    model_id: &str,
    instructions: &str,
    input: &str,
    response_format: &Value,
    maximum_output_tokens: u64,
) -> Result<Value, TransportError> {
    if instructions.trim().is_empty()
        || input.trim().is_empty()
        || !(1..=MAX_CHAT_OUTPUT_TOKENS).contains(&maximum_output_tokens)
    {
        return Err(TransportError::new(TransportErrorKind::Invalid));
    }
    Ok(json!({
        "model": model_id,
        "instructions": instructions,
        "input": input,
        "text": {"format": responses_text_format(response_format)?},
        "max_output_tokens": maximum_output_tokens,
        "store": false,
        "stream": false,
    }))
}

fn parse_openai_response_text(value: &Value) -> Result<String, TransportError> {
    let object = value
        .as_object()
        .ok_or_else(|| TransportError::new(TransportErrorKind::Invalid))?;
    match object.get("status").and_then(Value::as_str) {
        Some("completed") => {}
        Some("incomplete") => {
            return Err(TransportError::new(TransportErrorKind::Incomplete));
        }
        Some("failed") => return Err(TransportError::new(TransportErrorKind::Http)),
        _ => return Err(TransportError::new(TransportErrorKind::Invalid)),
    }
    let output = object
        .get("output")
        .and_then(Value::as_array)
        .ok_or_else(|| TransportError::new(TransportErrorKind::Invalid))?;
    let mut response_text: Option<String> = None;
    let mut assistant_messages = 0_usize;
    for item in output {
        let item = item
            .as_object()
            .ok_or_else(|| TransportError::new(TransportErrorKind::Invalid))?;
        match item.get("type").and_then(Value::as_str) {
            Some("reasoning") => continue,
            Some("message")
                if item.get("status").and_then(Value::as_str) == Some("completed")
                    && item.get("role").and_then(Value::as_str) == Some("assistant") =>
            {
                assistant_messages += 1;
                let content = item
                    .get("content")
                    .and_then(Value::as_array)
                    .ok_or_else(|| TransportError::new(TransportErrorKind::Invalid))?;
                for part in content {
                    let part = part
                        .as_object()
                        .ok_or_else(|| TransportError::new(TransportErrorKind::Invalid))?;
                    match part.get("type").and_then(Value::as_str) {
                        Some("refusal") => {
                            return Err(TransportError::new(TransportErrorKind::Refusal));
                        }
                        Some("output_text") if response_text.is_none() => {}
                        _ => return Err(TransportError::new(TransportErrorKind::Invalid)),
                    }
                    let text = part
                        .get("text")
                        .and_then(Value::as_str)
                        .filter(|text| !text.is_empty() && text.len() <= CHAT_RESPONSE_LIMIT)
                        .ok_or_else(|| TransportError::new(TransportErrorKind::Invalid))?;
                    response_text = Some(text.to_string());
                }
            }
            _ => return Err(TransportError::new(TransportErrorKind::Invalid)),
        }
    }
    if assistant_messages != 1 {
        return Err(TransportError::new(TransportErrorKind::Invalid));
    }
    response_text.ok_or_else(|| TransportError::new(TransportErrorKind::Invalid))
}

async fn request_openai_response(
    base_url: &Url,
    api_key: &str,
    body: &Value,
    timeout: Duration,
) -> Result<TextProviderResponse, TransportError> {
    let value = post_json(
        LocalProviderKind::Openai,
        Some(api_key),
        route_url(base_url, ProviderRoute::Responses),
        body,
        MAX_CHAT_REQUEST_BYTES,
        CHAT_RESPONSE_LIMIT,
        timeout,
    )
    .await?;
    parse_openai_provider_response(&value)
}

fn parse_openai_provider_response(value: &Value) -> Result<TextProviderResponse, TransportError> {
    let usage = parse_openai_response_usage(value)?;
    let text = parse_openai_response_text(value).map_err(|error| error.with_usage(Some(usage)))?;
    Ok(TextProviderResponse {
        text,
        usage: Some(usage),
    })
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum ProviderModelDiscoveryTarget {
    LmStudio {
        base_url: Url,
    },
    Openai {
        base_url: Url,
        credential_generation: Option<String>,
    },
}

fn provider_model_discovery_target(
    settings: &StoredProviderSettings,
    expected_revision: u64,
    provider_kind: LocalProviderKind,
    base_url: Option<String>,
) -> Result<ProviderModelDiscoveryTarget, LocalProviderError> {
    if expected_revision != settings.revision {
        return Err(LocalProviderError::new(
            "provider_settings_conflict",
            "The model-provider settings changed. Reload them before discovering models.",
        ));
    }
    if provider_kind != settings.provider_kind {
        return Err(LocalProviderError::new(
            "provider_selection_conflict",
            "The active model provider changed. Reload it before discovering models.",
        ));
    }
    match provider_kind {
        LocalProviderKind::OpenaiCompatibleLocal => {
            let base_url = base_url.ok_or_else(|| {
                LocalProviderError::new(
                    "local_provider_url_invalid",
                    "Use a numeric loopback URL such as http://127.0.0.1:1234/v1.",
                )
            })?;
            Ok(ProviderModelDiscoveryTarget::LmStudio {
                base_url: validated_base_url(&base_url)?,
            })
        }
        LocalProviderKind::Openai => {
            if base_url.is_some() {
                return Err(LocalProviderError::new(
                    "provider_url_not_allowed",
                    "OpenAI model discovery does not accept a caller-supplied endpoint.",
                ));
            }
            Ok(ProviderModelDiscoveryTarget::Openai {
                base_url: validated_base_url_for_kind(
                    LocalProviderKind::Openai,
                    &settings.openai.base_url,
                )?,
                credential_generation: settings.openai_credential_generation.clone(),
            })
        }
    }
}

#[tauri::command]
pub(crate) async fn get_local_provider_settings(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, LocalProviderRuntime>,
) -> Result<LocalProviderSettings, String> {
    let _guard = runtime.gate.lock().await;
    let data_dir = app_data_dir(&app).map_err(LocalProviderError::command_message)?;
    let settings = load_settings(&data_dir).map_err(LocalProviderError::command_message)?;
    Ok(settings_view(&settings).await)
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn discover_model_provider_models(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, LocalProviderRuntime>,
    expected_revision: u64,
    provider_kind: LocalProviderKind,
    base_url: Option<String>,
) -> Result<ProviderModelDiscovery, String> {
    let _guard = runtime.gate.lock().await;
    let data_dir = app_data_dir(&app).map_err(LocalProviderError::command_message)?;
    let settings = load_settings(&data_dir).map_err(LocalProviderError::command_message)?;
    let target =
        provider_model_discovery_target(&settings, expected_revision, provider_kind, base_url)
            .map_err(LocalProviderError::command_message)?;
    match target {
        ProviderModelDiscoveryTarget::LmStudio { base_url } => {
            discover_lm_studio_models_at(&base_url)
                .await
                .map_err(|error| error.public().command_message())
        }
        ProviderModelDiscoveryTarget::Openai {
            base_url,
            credential_generation,
        } => {
            let api_key = read_openai_secret(credential_generation)
                .await
                .map_err(|error| error.public().command_message())?;
            discover_openai_models_at(&base_url, &api_key)
                .await
                .map_err(|error| error.public().command_message())
        }
    }
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn select_model_provider(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, LocalProviderRuntime>,
    expected_revision: u64,
    provider_kind: LocalProviderKind,
) -> Result<LocalProviderSettings, String> {
    let _guard = runtime.gate.lock().await;
    let data_dir = app_data_dir(&app).map_err(LocalProviderError::command_message)?;
    let current = load_settings(&data_dir).map_err(LocalProviderError::command_message)?;
    let (updated, changed) = select_provider(current, expected_revision, provider_kind)
        .map_err(LocalProviderError::command_message)?;
    if changed {
        write_settings(&data_dir, &updated).map_err(LocalProviderError::command_message)?;
    }
    Ok(settings_view(&updated).await)
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn save_local_provider_settings(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, LocalProviderRuntime>,
    expected_revision: u64,
    provider_kind: LocalProviderKind,
    base_url: String,
    chat_model_id: Option<String>,
    embedding_model_id: Option<String>,
) -> Result<LocalProviderSettings, String> {
    let _guard = runtime.gate.lock().await;
    let data_dir = app_data_dir(&app).map_err(LocalProviderError::command_message)?;
    let current = load_settings(&data_dir).map_err(LocalProviderError::command_message)?;
    let (updated, changed) = update_settings(
        current,
        expected_revision,
        provider_kind,
        base_url,
        chat_model_id,
        embedding_model_id,
    )
    .map_err(LocalProviderError::command_message)?;
    if changed {
        write_settings(&data_dir, &updated).map_err(LocalProviderError::command_message)?;
    }
    Ok(settings_view(&updated).await)
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn save_openai_api_key(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, LocalProviderRuntime>,
    api_key: String,
) -> Result<LocalProviderSettings, String> {
    let _guard = runtime.gate.lock().await;
    let data_dir = app_data_dir(&app).map_err(LocalProviderError::command_message)?;
    let current = load_settings(&data_dir).map_err(LocalProviderError::command_message)?;
    let secret = validated_api_key(api_key).map_err(|error| error.public().command_message())?;
    let generation = Uuid::new_v4().to_string();

    // Write the new secret first. If the settings update fails, the generation
    // mismatch makes both the old and new credential unusable until replaced.
    store_openai_secret(generation.clone(), secret)
        .await
        .map_err(|error| error.public().command_message())?;
    let updated = set_credential_generation(current, Some(generation))
        .map_err(LocalProviderError::command_message)?;
    write_settings(&data_dir, &updated).map_err(LocalProviderError::command_message)?;
    Ok(settings_view(&updated).await)
}

#[tauri::command]
pub(crate) async fn delete_openai_api_key(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, LocalProviderRuntime>,
) -> Result<LocalProviderSettings, String> {
    let _guard = runtime.gate.lock().await;
    let data_dir = app_data_dir(&app).map_err(LocalProviderError::command_message)?;
    let current = load_settings(&data_dir).map_err(LocalProviderError::command_message)?;

    // Delete first so a failed settings write leaves a generation that cannot
    // resolve to a credential and therefore fails closed.
    delete_openai_secret()
        .await
        .map_err(|error| error.public().command_message())?;
    let updated = if current.openai_credential_generation.is_some() {
        set_credential_generation(current, None).map_err(LocalProviderError::command_message)?
    } else {
        current
    };
    write_settings(&data_dir, &updated).map_err(LocalProviderError::command_message)?;
    Ok(settings_view(&updated).await)
}

#[tauri::command]
pub(crate) async fn test_local_provider_models(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, LocalProviderRuntime>,
) -> Result<LocalProviderSettings, String> {
    let _guard = runtime.gate.lock().await;
    let data_dir = app_data_dir(&app).map_err(LocalProviderError::command_message)?;
    let mut settings = load_settings(&data_dir).map_err(LocalProviderError::command_message)?;
    let mut view = settings_view(&settings).await;
    if view.provider_kind == LocalProviderKind::Openai {
        reconcile_interrupted_provider_spend(&data_dir).map_err(|error| error.command_message())?;
    }
    let base_url = validated_base_url_for_kind(view.provider_kind, &view.base_url)
        .map_err(LocalProviderError::command_message)?;
    let (chat_model_id, embedding_model_id) = match view.provider_kind {
        LocalProviderKind::OpenaiCompatibleLocal => {
            let discovery = discover_lm_studio_models_at(&base_url)
                .await
                .map_err(|error| error.public().command_message())?;
            require_selected_typed_models(&view, &discovery)
                .map_err(LocalProviderError::command_message)?
        }
        LocalProviderKind::Openai => {
            require_selected_models(&view).map_err(LocalProviderError::command_message)?
        }
    };
    let api_key = match view.provider_kind {
        LocalProviderKind::OpenaiCompatibleLocal => None,
        LocalProviderKind::Openai => Some(
            read_openai_secret(settings.openai_credential_generation.clone())
                .await
                .map_err(|error| error.public().command_message())?,
        ),
    };

    let operation_id = Uuid::new_v4().to_string();
    require_priced_models_if_guarded(
        &data_dir,
        view.provider_kind,
        &[&embedding_model_id, &chat_model_id],
    )
    .map_err(|error| error.command_message())?;

    // Keep the two potentially memory-intensive probes strictly sequential.
    // Each cloud reservation is durable before network dispatch. CVGnome
    // performs no model lifecycle calls.
    let embedding_input = vec![TEST_EMBEDDING_INPUT.to_string()];
    let embedding_body = embedding_request_body(&embedding_model_id, &embedding_input);
    let embedding_reservation = reserve_provider_spend(
        &data_dir,
        &operation_id,
        ProviderSpendKind::Test,
        ProviderStage::Embedding.spend_stage(),
        view.provider_kind,
        &embedding_model_id,
        conservative_input_token_bound(&embedding_body)
            .map_err(LocalProviderError::command_message)?,
        0,
    )
    .map_err(|error| error.command_message())?;
    let embedding_response = match request_embeddings(
        view.provider_kind,
        api_key.as_ref().map(|value| value.as_str()),
        &base_url,
        &embedding_model_id,
        &embedding_input,
        None,
        MODEL_TEST_TIMEOUT,
    )
    .await
    {
        Ok(response) => response,
        Err(error) => {
            settle_failed_provider_spend(&data_dir, embedding_reservation.as_ref(), error)?;
            return Err(error
                .public_for_stage(ProviderStage::Embedding)
                .command_message());
        }
    };
    finish_provider_spend(
        &data_dir,
        embedding_reservation.as_ref(),
        embedding_response.usage,
    )?;
    let embedding_dimension = embedding_response.vectors[0].len();

    match view.provider_kind {
        LocalProviderKind::OpenaiCompatibleLocal => test_chat_model(&base_url, &chat_model_id)
            .await
            .map_err(|error| {
                error
                    .public_for_stage(ProviderStage::Chat)
                    .command_message()
            })?,
        LocalProviderKind::Openai => {
            let body = openai_response_body(
                &chat_model_id,
                TEST_CHAT_SYSTEM,
                TEST_CHAT_USER,
                &test_response_format(),
                1_024,
            )
            .map_err(|error| error.public().command_message())?;
            let chat_reservation = reserve_provider_spend(
                &data_dir,
                &operation_id,
                ProviderSpendKind::Test,
                ProviderStage::Chat.spend_stage(),
                view.provider_kind,
                &chat_model_id,
                conservative_input_token_bound(&body)
                    .map_err(LocalProviderError::command_message)?,
                1_024,
            )
            .map_err(|error| error.command_message())?;
            let response = match request_openai_response(
                &base_url,
                api_key
                    .as_ref()
                    .map(|value| value.as_str())
                    .ok_or_else(|| CredentialFailure::Missing.public().command_message())?,
                &body,
                MODEL_TEST_TIMEOUT,
            )
            .await
            {
                Ok(response) => response,
                Err(error) => {
                    settle_failed_provider_spend(&data_dir, chat_reservation.as_ref(), error)?;
                    return Err(error
                        .public_for_stage(ProviderStage::Chat)
                        .command_message());
                }
            };
            finish_provider_spend(&data_dir, chat_reservation.as_ref(), response.usage)?;
            validate_test_chat_content(&response.text).map_err(|error| {
                error
                    .public_for_stage(ProviderStage::Chat)
                    .command_message()
            })?;
        }
    }
    drop(api_key);

    let unchanged = load_settings(&data_dir).map_err(LocalProviderError::command_message)?;
    if unchanged != settings {
        return Err(LocalProviderError::new(
            "provider_settings_conflict",
            "The model-provider settings changed during testing. Test them again.",
        )
        .command_message());
    }
    let active = settings.active_mut();
    active.verified_revision = Some(active.revision);
    active.verified_at_ms = Some(unix_timestamp_ms().map_err(LocalProviderError::command_message)?);
    active.embedding_dimension = Some(embedding_dimension as u64);
    write_settings(&data_dir, &settings).map_err(LocalProviderError::command_message)?;
    view = settings_view(&settings).await;
    Ok(view)
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct TailoringPrepareResult {
    attempt_id: String,
    embedding_model_id: String,
    embedding_input: Vec<String>,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
enum ProviderMessageRole {
    System,
    User,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct ProviderMessage {
    role: ProviderMessageRole,
    content: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct TailoringArmResult {
    attempt_id: String,
    chat_model_id: String,
    messages: Vec<ProviderMessage>,
    response_format: Value,
    max_tokens: u64,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct TailoringFailResult {
    attempt_id: String,
    status: TailoringFailedStatus,
    error_code: String,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
enum TailoringFailedStatus {
    Failed,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct TailoredResumeDraft {
    id: String,
    attempt_id: String,
    created_at_ms: u64,
    opportunity: TailoredOpportunityReference,
    snapshot: TailoredSnapshotReference,
    profile_version: TailoredProfileReference,
    models: TailoredModelReference,
    input_fingerprint: String,
    selected_evidence: Vec<TailoredEvidence>,
    quality_issue_codes: Vec<String>,
    resume: Value,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct TailoredOpportunityReference {
    id: String,
    title: String,
    company: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct TailoredSnapshotReference {
    id: String,
    checksum_sha256: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct TailoredProfileReference {
    id: String,
    version_number: u64,
    checksum_sha256: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct TailoredModelReference {
    provider_kind: LocalProviderKind,
    chat_model_id: String,
    embedding_model_id: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct TailoredEvidence {
    chunk_id: String,
    kind: String,
    path: String,
    content: String,
}

fn validate_uuid(value: &str) -> Result<(), String> {
    let parsed = Uuid::parse_str(value).map_err(|_| "invalid UUID".to_string())?;
    if parsed.to_string() != value {
        return Err("invalid UUID".to_string());
    }
    Ok(())
}

fn validate_sha256(value: &str) -> Result<(), String> {
    if value.len() != 64
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err("invalid fingerprint".to_string());
    }
    Ok(())
}

fn validate_draft_text(
    value: &str,
    maximum_chars: usize,
    maximum_bytes: usize,
    allow_empty: bool,
    allow_multiline: bool,
) -> Result<(), String> {
    if (!allow_empty && value.trim().is_empty())
        || value.chars().count() > maximum_chars
        || value.len() > maximum_bytes
        || value.chars().any(|character| {
            character.is_control() && !(allow_multiline && matches!(character, '\n' | '\r' | '\t'))
        })
    {
        return Err("invalid bounded text".to_string());
    }
    Ok(())
}

fn validate_contract_code(value: &str) -> Result<(), String> {
    if value.is_empty()
        || value.len() > 80
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-' | b'.'))
    {
        return Err("invalid contract code".to_string());
    }
    Ok(())
}

fn validate_bounded_json(
    value: &Value,
    maximum_bytes: usize,
    maximum_depth: usize,
    maximum_nodes: usize,
) -> Result<(), String> {
    let encoded = serde_json::to_vec(value).map_err(|_| "invalid JSON".to_string())?;
    if encoded.len() > maximum_bytes {
        return Err("invalid JSON size".to_string());
    }
    let mut stack = vec![(value, 1_usize)];
    let mut nodes = 0_usize;
    while let Some((node, depth)) = stack.pop() {
        nodes = nodes.saturating_add(1);
        if nodes > maximum_nodes || depth > maximum_depth {
            return Err("invalid JSON shape".to_string());
        }
        match node {
            Value::Object(object) => {
                for (key, child) in object {
                    if key.is_empty()
                        || key.chars().count() > 128
                        || key.chars().any(char::is_control)
                    {
                        return Err("invalid JSON key".to_string());
                    }
                    stack.push((child, depth + 1));
                }
            }
            Value::Array(array) => {
                for child in array {
                    stack.push((child, depth + 1));
                }
            }
            Value::String(string) => {
                if string.len() > 16 * 1024
                    || string.chars().any(|character| {
                        character.is_control() && !matches!(character, '\n' | '\r' | '\t')
                    })
                {
                    return Err("invalid JSON string".to_string());
                }
            }
            Value::Null | Value::Bool(_) | Value::Number(_) => {}
        }
    }
    Ok(())
}

impl TailoringPrepareResult {
    fn validated(self, settings: &LocalProviderSettings) -> Result<Self, LocalProviderError> {
        if validate_uuid(&self.attempt_id).is_err()
            || settings.embedding_model_id.as_deref() != Some(&self.embedding_model_id)
            || validated_model_id(&self.embedding_model_id).is_err()
            || !(2..=MAX_TAILORING_EMBEDDING_INPUTS).contains(&self.embedding_input.len())
        {
            return Err(LocalProviderError::new(
                "tailoring_contract_invalid",
                "The local tailoring engine returned an invalid provider request.",
            ));
        }
        let total_bytes = self
            .embedding_input
            .iter()
            .map(|item| item.len())
            .sum::<usize>();
        if total_bytes > MAX_TAILORING_EMBEDDING_INPUT_BYTES {
            return Err(LocalProviderError::new(
                "tailoring_contract_invalid",
                "The local tailoring engine returned an invalid provider request.",
            ));
        }
        for (index, item) in self.embedding_input.iter().enumerate() {
            let maximum = if index == 0 {
                MAX_TAILORING_QUERY_BYTES
            } else {
                MAX_TAILORING_CHUNK_BYTES
            };
            if item.trim().is_empty() || item.len() > maximum {
                return Err(LocalProviderError::new(
                    "tailoring_contract_invalid",
                    "The local tailoring engine returned an invalid provider request.",
                ));
            }
        }
        Ok(self)
    }
}

impl TailoringArmResult {
    fn validated(
        self,
        expected_attempt_id: &str,
        settings: &LocalProviderSettings,
    ) -> Result<Self, LocalProviderError> {
        if serde_json::to_vec(&self)
            .map(|encoded| encoded.len() > MAX_ENGINE_ARM_RESULT_BYTES)
            .unwrap_or(true)
            || self.attempt_id != expected_attempt_id
            || settings.chat_model_id.as_deref() != Some(&self.chat_model_id)
            || validated_model_id(&self.chat_model_id).is_err()
            || !(2..=MAX_CHAT_MESSAGES).contains(&self.messages.len())
            || !(1..=MAX_CHAT_OUTPUT_TOKENS).contains(&self.max_tokens)
            || !self.response_format.is_object()
            || validate_bounded_json(
                &self.response_format,
                MAX_RESPONSE_FORMAT_BYTES,
                MAX_RESPONSE_FORMAT_DEPTH,
                1_024,
            )
            .is_err()
        {
            return Err(LocalProviderError::new(
                "tailoring_contract_invalid",
                "The local tailoring engine returned an invalid chat request.",
            ));
        }
        let mut total = 0_usize;
        let mut has_system = false;
        let mut has_user = false;
        for message in &self.messages {
            if message.content.trim().is_empty() || message.content.len() > MAX_CHAT_MESSAGE_BYTES {
                return Err(LocalProviderError::new(
                    "tailoring_contract_invalid",
                    "The local tailoring engine returned an invalid chat request.",
                ));
            }
            total = total.saturating_add(message.content.len());
            match message.role {
                ProviderMessageRole::System => has_system = true,
                ProviderMessageRole::User => has_user = true,
            }
        }
        if total > MAX_CHAT_MESSAGES_TOTAL_BYTES || !has_system || !has_user {
            return Err(LocalProviderError::new(
                "tailoring_contract_invalid",
                "The local tailoring engine returned an invalid chat request.",
            ));
        }
        Ok(self)
    }
}

impl TailoredResumeDraft {
    fn validated(
        self,
        expected_opportunity_id: &str,
        expected_attempt_id: Option<&str>,
        expected_settings: Option<&LocalProviderSettings>,
    ) -> Result<Self, String> {
        if serde_json::to_vec(&self)
            .map(|encoded| encoded.len() > MAX_DRAFT_BYTES)
            .unwrap_or(true)
        {
            return Err("The local engine returned too much tailoring data".to_string());
        }
        validate_uuid(&self.id)?;
        validate_uuid(&self.attempt_id)?;
        if expected_attempt_id.is_some_and(|expected| expected != self.attempt_id)
            || self.opportunity.id != expected_opportunity_id
        {
            return Err("The local engine returned a mismatched tailoring draft".to_string());
        }
        validate_uuid(&self.opportunity.id)?;
        validate_draft_text(&self.opportunity.title, 240, 960, false, false)?;
        validate_draft_text(&self.opportunity.company, 160, 640, true, false)?;
        validate_uuid(&self.snapshot.id)?;
        validate_sha256(&self.snapshot.checksum_sha256)?;
        validate_uuid(&self.profile_version.id)?;
        if self.profile_version.version_number == 0
            || self.profile_version.version_number > JAVASCRIPT_MAX_SAFE_INTEGER
            || self.created_at_ms == 0
            || self.created_at_ms > JAVASCRIPT_MAX_SAFE_INTEGER
        {
            return Err("The local engine returned invalid tailoring metadata".to_string());
        }
        validate_sha256(&self.profile_version.checksum_sha256)?;
        validate_sha256(&self.input_fingerprint)?;
        validated_model_id(&self.models.chat_model_id)
            .map_err(|_| "The local engine returned an invalid chat model".to_string())?;
        validated_model_id(&self.models.embedding_model_id)
            .map_err(|_| "The local engine returned an invalid embedding model".to_string())?;
        if self.models.chat_model_id == self.models.embedding_model_id {
            return Err("The local engine returned invalid tailoring models".to_string());
        }
        if let Some(settings) = expected_settings
            && (settings.provider_kind != self.models.provider_kind
                || settings.chat_model_id.as_deref() != Some(&self.models.chat_model_id)
                || settings.embedding_model_id.as_deref() != Some(&self.models.embedding_model_id))
        {
            return Err("The local engine returned mismatched tailoring models".to_string());
        }
        if self.selected_evidence.len() > MAX_DRAFT_EVIDENCE_ITEMS
            || self.quality_issue_codes.len() > MAX_DRAFT_QUALITY_ISSUES
        {
            return Err("The local engine returned too much tailoring detail".to_string());
        }
        let mut evidence_ids = HashSet::new();
        for evidence in &self.selected_evidence {
            validate_draft_text(&evidence.chunk_id, 160, 640, false, false)?;
            validate_draft_text(&evidence.kind, 64, 256, false, false)?;
            validate_draft_text(&evidence.path, 512, 2_048, false, false)?;
            validate_draft_text(
                &evidence.content,
                MAX_TAILORING_CHUNK_BYTES,
                MAX_TAILORING_CHUNK_BYTES,
                false,
                true,
            )?;
            if !evidence_ids.insert(&evidence.chunk_id) {
                return Err("The local engine returned duplicate tailoring evidence".to_string());
            }
        }
        let mut issue_codes = HashSet::new();
        for code in &self.quality_issue_codes {
            validate_contract_code(code)?;
            if !issue_codes.insert(code) {
                return Err("The local engine returned duplicate tailoring issues".to_string());
            }
        }
        if !self.resume.is_object()
            || validate_bounded_json(&self.resume, MAX_DRAFT_RESUME_BYTES, 20, 8_000).is_err()
        {
            return Err("The local engine returned an invalid tailored resume".to_string());
        }
        Ok(self)
    }
}

async fn engine_request<T: DeserializeOwned>(
    app: &tauri::AppHandle,
    runtime: &EngineRuntime,
    method: &str,
    params: Value,
    timeout: Duration,
) -> Result<T, String> {
    let _guard = runtime.operation_gate.lock().await;
    run_engine_request(app, method, params, timeout).await
}

async fn fail_tailoring_attempt(
    app: &tauri::AppHandle,
    runtime: &EngineRuntime,
    attempt_id: &str,
    error_code: &'static str,
) {
    let result = engine_request::<TailoringFailResult>(
        app,
        runtime,
        "tailoring.fail",
        json!({"attempt_id": attempt_id, "error_code": error_code}),
        Duration::from_secs(15),
    )
    .await;
    if let Ok(receipt) = result {
        let _ = receipt.status;
        let _ = receipt.attempt_id == attempt_id && receipt.error_code == error_code;
    }
}

async fn tailoring_embedding_failure(
    app: &tauri::AppHandle,
    runtime: &EngineRuntime,
    attempt_id: &str,
    error: TransportError,
) -> String {
    fail_tailoring_attempt(
        app,
        runtime,
        attempt_id,
        error.tailoring_fail_code(ProviderStage::Embedding),
    )
    .await;
    error
        .public_for_stage(ProviderStage::Embedding)
        .command_message()
}

async fn tailoring_chat_failure(
    app: &tauri::AppHandle,
    runtime: &EngineRuntime,
    attempt_id: &str,
    error: TransportError,
) -> String {
    fail_tailoring_attempt(
        app,
        runtime,
        attempt_id,
        error.tailoring_fail_code(ProviderStage::Chat),
    )
    .await;
    error
        .public_for_stage(ProviderStage::Chat)
        .command_message()
}

fn tailoring_chat_body(
    provider_kind: LocalProviderKind,
    arm: &TailoringArmResult,
) -> Result<Value, TransportError> {
    match provider_kind {
        LocalProviderKind::OpenaiCompatibleLocal => Ok(json!({
            "model": arm.chat_model_id,
            "messages": arm.messages,
            "response_format": arm.response_format,
            "temperature": 0,
            "max_tokens": arm.max_tokens,
            "stream": false
        })),
        LocalProviderKind::Openai => {
            let system_messages: Vec<&str> = arm
                .messages
                .iter()
                .filter(|message| message.role == ProviderMessageRole::System)
                .map(|message| message.content.as_str())
                .collect();
            let user_messages: Vec<&str> = arm
                .messages
                .iter()
                .filter(|message| message.role == ProviderMessageRole::User)
                .map(|message| message.content.as_str())
                .collect();
            if system_messages.len() != 1 || user_messages.len() != 1 {
                return Err(TransportError::new(TransportErrorKind::Invalid));
            }
            openai_response_body(
                &arm.chat_model_id,
                system_messages[0],
                user_messages[0],
                &arm.response_format,
                arm.max_tokens,
            )
        }
    }
}

async fn request_tailoring_chat(
    provider_kind: LocalProviderKind,
    api_key: Option<&str>,
    base_url: &Url,
    body: &Value,
) -> Result<TextProviderResponse, TransportError> {
    match provider_kind {
        LocalProviderKind::OpenaiCompatibleLocal => {
            let value = post_json(
                LocalProviderKind::OpenaiCompatibleLocal,
                None,
                route_url(base_url, ProviderRoute::ChatCompletions),
                body,
                MAX_CHAT_REQUEST_BYTES,
                CHAT_RESPONSE_LIMIT,
                TAILORING_TIMEOUT,
            )
            .await?;
            Ok(TextProviderResponse {
                text: parse_chat_response_text(&value)?,
                usage: None,
            })
        }
        LocalProviderKind::Openai => {
            let api_key =
                api_key.ok_or_else(|| TransportError::new(TransportErrorKind::Authentication))?;
            request_openai_response(base_url, api_key, body, TAILORING_TIMEOUT).await
        }
    }
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn generate_tailored_resume(
    app: tauri::AppHandle,
    provider_runtime: tauri::State<'_, LocalProviderRuntime>,
    engine_runtime: tauri::State<'_, EngineRuntime>,
    opportunity_id: String,
) -> Result<TailoredResumeDraft, String> {
    validate_uuid(&opportunity_id)
        .map_err(|_| "opportunity_id_invalid: The selected role is invalid.".to_string())?;
    let _provider_guard = provider_runtime.gate.lock().await;
    let data_dir = app_data_dir(&app).map_err(LocalProviderError::command_message)?;
    let stored_settings = load_settings(&data_dir).map_err(LocalProviderError::command_message)?;
    let settings = settings_view(&stored_settings).await;
    if !settings.is_verified() {
        return Err(LocalProviderError::new(
            "provider_not_verified",
            "Test the selected chat and embedding models before tailoring.",
        )
        .command_message());
    }
    if settings.provider_kind == LocalProviderKind::Openai {
        reconcile_interrupted_provider_spend(&data_dir).map_err(|error| error.command_message())?;
    }
    let base_url = validated_base_url_for_kind(settings.provider_kind, &settings.base_url)
        .map_err(LocalProviderError::command_message)?;
    let api_key = match settings.provider_kind {
        LocalProviderKind::OpenaiCompatibleLocal => None,
        LocalProviderKind::Openai => Some(
            read_openai_secret(stored_settings.openai_credential_generation.clone())
                .await
                .map_err(|error| error.public().command_message())?,
        ),
    };
    let chat_model_id = settings
        .chat_model_id
        .clone()
        .ok_or_else(|| LocalProviderError::config_invalid().command_message())?;
    let embedding_model_id = settings
        .embedding_model_id
        .clone()
        .ok_or_else(|| LocalProviderError::config_invalid().command_message())?;
    let embedding_dimension = settings
        .embedding_dimension
        .ok_or_else(|| LocalProviderError::config_invalid().command_message())?
        as usize;
    require_priced_models_if_guarded(
        &data_dir,
        settings.provider_kind,
        &[&embedding_model_id, &chat_model_id],
    )
    .map_err(|error| error.command_message())?;

    let raw_prepare = engine_request::<TailoringPrepareResult>(
        &app,
        &engine_runtime,
        "tailoring.prepare",
        json!({
            "opportunity_id": opportunity_id,
            "provider_kind": settings.provider_kind,
            "chat_model_id": chat_model_id,
            "embedding_model_id": embedding_model_id
        }),
        Duration::from_secs(30),
    )
    .await?;
    let prepare_attempt_id = raw_prepare.attempt_id.clone();
    let prepare = match raw_prepare.validated(&settings) {
        Ok(prepare) => prepare,
        Err(error) => {
            if validate_uuid(&prepare_attempt_id).is_ok() {
                fail_tailoring_attempt(
                    &app,
                    &engine_runtime,
                    &prepare_attempt_id,
                    "provider_response_invalid",
                )
                .await;
            }
            return Err(error.command_message());
        }
    };
    let attempt_id = prepare.attempt_id.clone();

    // Embedding and chat are deliberately sequential. Every cloud call is
    // reserved durably before dispatch. No model load, unload, download, CLI,
    // lifecycle, or fallback operation is performed by CVGnome.
    let embedding_body = embedding_request_body(&embedding_model_id, &prepare.embedding_input);
    let embedding_reservation = match reserve_provider_spend(
        &data_dir,
        &attempt_id,
        ProviderSpendKind::Tailoring,
        ProviderStage::Embedding.spend_stage(),
        settings.provider_kind,
        &embedding_model_id,
        conservative_input_token_bound(&embedding_body)
            .map_err(LocalProviderError::command_message)?,
        0,
    ) {
        Ok(reservation) => reservation,
        Err(error) => {
            fail_tailoring_attempt(&app, &engine_runtime, &attempt_id, "provider_unavailable")
                .await;
            return Err(error.command_message());
        }
    };
    let embedding_response = match request_embeddings(
        settings.provider_kind,
        api_key.as_ref().map(|value| value.as_str()),
        &base_url,
        &embedding_model_id,
        &prepare.embedding_input,
        Some(embedding_dimension),
        TAILORING_TIMEOUT,
    )
    .await
    {
        Ok(response) => response,
        Err(error) => {
            let spend_result =
                settle_failed_provider_spend(&data_dir, embedding_reservation.as_ref(), error);
            let provider_error =
                tailoring_embedding_failure(&app, &engine_runtime, &attempt_id, error).await;
            return Err(spend_result.err().unwrap_or(provider_error));
        }
    };
    if let Err(error) = finish_provider_spend(
        &data_dir,
        embedding_reservation.as_ref(),
        embedding_response.usage,
    ) {
        fail_tailoring_attempt(&app, &engine_runtime, &attempt_id, "provider_unavailable").await;
        return Err(error);
    }
    let embeddings = embedding_response.vectors;

    let current_settings = load_settings(&data_dir).map_err(LocalProviderError::command_message)?;
    if current_settings != stored_settings {
        fail_tailoring_attempt(&app, &engine_runtime, &attempt_id, "provider_unavailable").await;
        return Err(LocalProviderError::new(
            "provider_settings_conflict",
            "The model-provider settings changed during tailoring. Start a new draft.",
        )
        .command_message());
    }

    let arm_params = json!({"attempt_id": attempt_id, "embeddings": embeddings});
    if serde_json::to_vec(&arm_params)
        .map(|encoded| encoded.len() > MAX_ENGINE_ARM_PARAMS_BYTES)
        .unwrap_or(true)
    {
        fail_tailoring_attempt(
            &app,
            &engine_runtime,
            &prepare.attempt_id,
            "provider_response_invalid",
        )
        .await;
        return Err(LocalProviderError::new(
            "local_provider_response_invalid",
            "The embedding response exceeded CVGnome's local tailoring boundary.",
        )
        .command_message());
    }
    let arm = match engine_request::<TailoringArmResult>(
        &app,
        &engine_runtime,
        "tailoring.arm",
        arm_params,
        Duration::from_secs(30),
    )
    .await
    {
        Ok(arm) => arm,
        Err(error) => {
            fail_tailoring_attempt(
                &app,
                &engine_runtime,
                &prepare.attempt_id,
                "provider_response_invalid",
            )
            .await;
            return Err(error);
        }
    };
    let arm = match arm.validated(&prepare.attempt_id, &settings) {
        Ok(arm) => arm,
        Err(error) => {
            fail_tailoring_attempt(
                &app,
                &engine_runtime,
                &prepare.attempt_id,
                "provider_response_invalid",
            )
            .await;
            return Err(error.command_message());
        }
    };

    let chat_body = match tailoring_chat_body(settings.provider_kind, &arm) {
        Ok(body) => body,
        Err(error) => {
            return Err(
                tailoring_chat_failure(&app, &engine_runtime, &prepare.attempt_id, error).await,
            );
        }
    };
    let chat_reservation = match reserve_provider_spend(
        &data_dir,
        &attempt_id,
        ProviderSpendKind::Tailoring,
        ProviderStage::Chat.spend_stage(),
        settings.provider_kind,
        &chat_model_id,
        conservative_input_token_bound(&chat_body).map_err(LocalProviderError::command_message)?,
        arm.max_tokens,
    ) {
        Ok(reservation) => reservation,
        Err(error) => {
            fail_tailoring_attempt(&app, &engine_runtime, &attempt_id, "provider_unavailable")
                .await;
            return Err(error.command_message());
        }
    };
    let response = match request_tailoring_chat(
        settings.provider_kind,
        api_key.as_ref().map(|value| value.as_str()),
        &base_url,
        &chat_body,
    )
    .await
    {
        Ok(response) => response,
        Err(error) => {
            let spend_result =
                settle_failed_provider_spend(&data_dir, chat_reservation.as_ref(), error);
            let provider_error =
                tailoring_chat_failure(&app, &engine_runtime, &prepare.attempt_id, error).await;
            return Err(spend_result.err().unwrap_or(provider_error));
        }
    };
    if let Err(error) = finish_provider_spend(&data_dir, chat_reservation.as_ref(), response.usage)
    {
        fail_tailoring_attempt(&app, &engine_runtime, &attempt_id, "provider_unavailable").await;
        return Err(error);
    }
    let response_text = response.text;
    drop(api_key);
    let draft = match engine_request::<TailoredResumeDraft>(
        &app,
        &engine_runtime,
        "tailoring.finalize",
        json!({"attempt_id": prepare.attempt_id, "response_text": response_text}),
        Duration::from_secs(60),
    )
    .await
    {
        Ok(draft) => draft,
        Err(error) => {
            let provider_response_invalid = error.starts_with("tailoring_response_invalid:");
            fail_tailoring_attempt(
                &app,
                &engine_runtime,
                &attempt_id,
                if provider_response_invalid {
                    "provider_response_invalid"
                } else {
                    "cancelled"
                },
            )
            .await;
            return Err(if provider_response_invalid {
                LocalProviderError::new(
                    "tailoring_draft_rejected",
                    "CVGnome rejected the generated draft because it could not be safely applied to the imported evidence.",
                )
                .command_message()
            } else {
                error
            });
        }
    };
    match draft.validated(&opportunity_id, Some(&attempt_id), Some(&settings)) {
        Ok(draft) => Ok(draft),
        Err(_) => {
            fail_tailoring_attempt(
                &app,
                &engine_runtime,
                &attempt_id,
                "provider_response_invalid",
            )
            .await;
            Err(LocalProviderError::new(
                "tailoring_contract_invalid",
                "The local tailoring engine returned an invalid draft.",
            )
            .command_message())
        }
    }
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn get_latest_tailored_resume(
    app: tauri::AppHandle,
    engine_runtime: tauri::State<'_, EngineRuntime>,
    opportunity_id: String,
) -> Result<Option<TailoredResumeDraft>, String> {
    validate_uuid(&opportunity_id)
        .map_err(|_| "opportunity_id_invalid: The selected role is invalid.".to_string())?;
    let draft = engine_request::<Option<TailoredResumeDraft>>(
        &app,
        &engine_runtime,
        "tailoring.latest",
        json!({"opportunity_id": opportunity_id}),
        Duration::from_secs(15),
    )
    .await?;
    draft
        .map(|draft| draft.validated(&opportunity_id, None, None))
        .transpose()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::{Ipv4Addr, TcpListener, TcpStream};
    use std::sync::{Arc, Mutex as StdMutex};
    use std::thread;

    #[derive(Clone)]
    struct StubResponse {
        status: u16,
        headers: Vec<(String, String)>,
        body: Vec<u8>,
    }

    #[derive(Clone, Debug)]
    struct RecordedRequest {
        method: String,
        path: String,
        body: Vec<u8>,
    }

    fn header_end(buffer: &[u8]) -> Option<usize> {
        buffer.windows(4).position(|window| window == b"\r\n\r\n")
    }

    fn read_request(stream: &mut TcpStream) -> RecordedRequest {
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        let mut bytes = Vec::new();
        let mut chunk = [0_u8; 8 * 1024];
        let headers_end = loop {
            let count = stream.read(&mut chunk).unwrap();
            assert!(count > 0);
            bytes.extend_from_slice(&chunk[..count]);
            assert!(bytes.len() <= 1024 * 1024);
            if let Some(position) = header_end(&bytes) {
                break position;
            }
        };
        let header_text = std::str::from_utf8(&bytes[..headers_end]).unwrap();
        let mut lines = header_text.split("\r\n");
        let mut request_line = lines.next().unwrap().split_whitespace();
        let method = request_line.next().unwrap().to_string();
        let path = request_line.next().unwrap().to_string();
        let content_length = lines
            .filter_map(|line| line.split_once(':'))
            .find(|(name, _)| name.eq_ignore_ascii_case("content-length"))
            .map(|(_, value)| value.trim().parse::<usize>().unwrap())
            .unwrap_or(0);
        let body_start = headers_end + 4;
        while bytes.len() < body_start + content_length {
            let count = stream.read(&mut chunk).unwrap();
            assert!(count > 0);
            bytes.extend_from_slice(&chunk[..count]);
        }
        RecordedRequest {
            method,
            path,
            body: bytes[body_start..body_start + content_length].to_vec(),
        }
    }

    fn reason(status: u16) -> &'static str {
        match status {
            200 => "OK",
            302 => "Found",
            401 => "Unauthorized",
            404 => "Not Found",
            500 => "Internal Server Error",
            _ => "Response",
        }
    }

    fn spawn_stub_server(
        responses: Vec<StubResponse>,
    ) -> (
        String,
        Arc<StdMutex<Vec<RecordedRequest>>>,
        thread::JoinHandle<()>,
    ) {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        let requests = Arc::new(StdMutex::new(Vec::new()));
        let recorded = Arc::clone(&requests);
        let handle = thread::spawn(move || {
            for response in responses {
                let (mut stream, _) = listener.accept().unwrap();
                let request = read_request(&mut stream);
                recorded.lock().unwrap().push(request);
                let has_length = response
                    .headers
                    .iter()
                    .any(|(name, _)| name.eq_ignore_ascii_case("content-length"));
                let mut header = format!(
                    "HTTP/1.1 {} {}\r\nConnection: close\r\n",
                    response.status,
                    reason(response.status)
                );
                for (name, value) in response.headers {
                    header.push_str(&format!("{name}: {value}\r\n"));
                }
                if !has_length {
                    header.push_str(&format!("Content-Length: {}\r\n", response.body.len()));
                }
                header.push_str("\r\n");
                stream.write_all(header.as_bytes()).unwrap();
                stream.write_all(&response.body).unwrap();
                stream.flush().unwrap();
            }
        });
        (format!("http://127.0.0.1:{port}/v1"), requests, handle)
    }

    fn json_response(value: Value) -> StubResponse {
        StubResponse {
            status: 200,
            headers: vec![("Content-Type".to_string(), "application/json".to_string())],
            body: serde_json::to_vec(&value).unwrap(),
        }
    }

    fn test_models_payload() -> Value {
        json!({
            "models": [
                {
                    "key": "org/chat-model",
                    "display_name": "Chat Model",
                    "type": "llm",
                    "loaded_instances": [{"id": "chat-1"}]
                },
                {
                    "key": "org/embed-model",
                    "display_name": "Embedding Model",
                    "type": "embedding",
                    "loaded_instances": []
                },
                {
                    "key": "org/other-model",
                    "display_name": "Other Model",
                    "type": "vision"
                }
            ]
        })
    }

    fn configured_settings() -> LocalProviderSettings {
        configured_stored_settings().view(ProviderCredentialState::NotRequired)
    }

    fn configured_stored_settings() -> StoredProviderSettings {
        let mut settings = StoredProviderSettings::default();
        settings.local.chat_model_id = Some("org/chat-model".to_string());
        settings.local.embedding_model_id = Some("org/embed-model".to_string());
        settings
    }

    #[test]
    fn loopback_base_url_is_strict_and_canonical() {
        assert_eq!(
            normalized_base_url(" http://127.0.0.1:80/v1 ").unwrap(),
            "http://127.0.0.1:80/v1"
        );
        assert_eq!(
            validated_base_url("http://127.0.0.1:1234/v1")
                .unwrap()
                .as_str(),
            "http://127.0.0.1:1234/v1"
        );
        assert_eq!(
            validated_base_url("http://[::1]:1066/v1").unwrap().as_str(),
            "http://[::1]:1066/v1"
        );
        for invalid in [
            "http://localhost:1234/v1",
            "http://127.0.0.2:1234/v1",
            "https://127.0.0.1:1234/v1",
            "http://127.0.0.1/v1",
            "http://127.0.0.1:1234/v1/",
            "http://127.0.0.1:1234/v1/models",
            "http://user@127.0.0.1:1234/v1",
            "http://127.0.0.1:1234/v1?q=1",
            "http://127.0.0.1:1234/v1#fragment",
            "http://127.0.0.1:0/v1",
            "http://127.0.0.1:01234/v1",
            "http://2130706433:1234/v1",
            "http://127.0.0.1:1234/extra/../v1",
        ] {
            assert!(validated_base_url(invalid).is_err(), "accepted {invalid}");
        }
    }

    #[test]
    fn model_ids_are_exact_bounded_printable_ascii_tokens() {
        assert_eq!(
            validated_model_id("google/gemma-4-26b-a4b@q4_0:v1+local.test").unwrap(),
            "google/gemma-4-26b-a4b@q4_0:v1+local.test"
        );
        assert!(validated_model_id("").is_err());
        assert!(validated_model_id(" org/model@q4").is_err());
        assert!(validated_model_id("org/model @q4").is_err());
        assert!(validated_model_id("bad\nmodel").is_err());
        assert!(validated_model_id("mödél").is_err());
        assert!(validated_model_id(&"a".repeat(MODEL_ID_LIMIT_BYTES + 1)).is_err());
    }

    #[test]
    fn config_round_trip_is_owner_only_and_strict() {
        let directory = tempfile::tempdir().unwrap();
        assert_eq!(
            load_settings(directory.path()).unwrap(),
            StoredProviderSettings::default()
        );
        let mut settings = configured_stored_settings();
        settings.local.verified_revision = Some(settings.local.revision);
        settings.local.verified_at_ms = Some(1_800_000_000_000);
        settings.local.embedding_dimension = Some(2_560);
        write_settings(directory.path(), &settings).unwrap();
        assert_eq!(load_settings(directory.path()).unwrap(), settings);
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                fs::metadata(config_path(directory.path()))
                    .unwrap()
                    .permissions()
                    .mode()
                    & 0o777,
                0o600
            );
        }

        let mut invalid = serde_json::to_value(&settings).unwrap();
        invalid
            .as_object_mut()
            .unwrap()
            .insert("unexpected".to_string(), Value::Bool(true));
        fs::write(
            config_path(directory.path()),
            serde_json::to_vec(&invalid).unwrap(),
        )
        .unwrap();
        assert!(load_settings(directory.path()).is_err());
    }

    #[test]
    fn schema_one_migrates_without_losing_local_verification() {
        let directory = tempfile::tempdir().unwrap();
        let legacy = json!({
            "schema_version": 1,
            "revision": 7,
            "provider_kind": "openai_compatible_local",
            "base_url": "http://127.0.0.1:1066/v1",
            "chat_model_id": "org/chat-model",
            "embedding_model_id": "org/embed-model",
            "verified_revision": 7,
            "verified_at_ms": 1_800_000_000_000_u64,
            "embedding_dimension": 2_560,
        });
        fs::write(
            config_path(directory.path()),
            serde_json::to_vec(&legacy).unwrap(),
        )
        .unwrap();

        let migrated = load_settings(directory.path()).unwrap();
        assert_eq!(migrated.schema_version, CONFIG_SCHEMA_VERSION);
        assert_eq!(migrated.revision, 7);
        assert_eq!(
            migrated.provider_kind,
            LocalProviderKind::OpenaiCompatibleLocal
        );
        assert_eq!(migrated.local.revision, 7);
        assert_eq!(migrated.local.verified_revision, Some(7));
        assert_eq!(migrated.local.embedding_dimension, Some(2_560));
        assert_eq!(migrated.openai, ProviderConfiguration::openai_default());
        let persisted: Value =
            serde_json::from_slice(&fs::read(config_path(directory.path())).unwrap()).unwrap();
        assert_eq!(persisted["schema_version"], CONFIG_SCHEMA_VERSION);
        assert!(persisted.get("local").is_some());
        assert!(persisted.get("openai").is_some());
    }

    #[test]
    fn provider_switch_preserves_each_configuration_and_verification() {
        let mut settings = configured_stored_settings();
        settings.local.verified_revision = Some(settings.local.revision);
        settings.local.verified_at_ms = Some(1_800_000_000_000);
        settings.local.embedding_dimension = Some(3);
        settings.openai.chat_model_id = Some("gpt-test".to_string());
        settings.openai.embedding_model_id = Some("embed-test".to_string());

        let (cloud, changed) =
            select_provider(settings.clone(), 1, LocalProviderKind::Openai).unwrap();
        assert!(changed);
        assert_eq!(cloud.local, settings.local);
        assert_eq!(cloud.openai, settings.openai);
        let (local, changed) = select_provider(
            cloud.clone(),
            cloud.revision,
            LocalProviderKind::OpenaiCompatibleLocal,
        )
        .unwrap();
        assert!(changed);
        assert_eq!(local.local, settings.local);
        assert!(
            local
                .view(ProviderCredentialState::NotRequired)
                .is_verified()
        );
    }

    #[test]
    fn credential_store_envelope_is_generation_bound_and_secret_free_in_config() {
        let store: Arc<CredentialStore> = keyring_core::mock::Store::new().unwrap();
        let generation = Uuid::new_v4().to_string();
        let secret_marker = "sk-test-secret-marker-123456789";
        write_openai_secret_with_store(store.as_ref(), &generation, secret_marker).unwrap();
        assert_eq!(
            read_openai_secret_with_store(store.as_ref(), Some(&generation))
                .unwrap()
                .as_str(),
            secret_marker
        );
        assert_eq!(
            read_openai_secret_with_store(store.as_ref(), Some(&Uuid::new_v4().to_string()))
                .unwrap_err(),
            CredentialFailure::Missing
        );

        let settings = StoredProviderSettings {
            openai_credential_generation: Some(generation),
            ..StoredProviderSettings::default()
        };
        let encoded = serde_json::to_string(&settings).unwrap();
        assert!(!encoded.contains(secret_marker));
        assert!(!encoded.contains("api_key"));

        delete_openai_secret_with_store(store.as_ref()).unwrap();
        assert_eq!(
            read_openai_secret_with_store(
                store.as_ref(),
                settings.openai_credential_generation.as_deref(),
            )
            .unwrap_err(),
            CredentialFailure::Missing
        );
        delete_openai_secret_with_store(store.as_ref()).unwrap();
    }

    #[test]
    fn openai_origin_routes_and_authorization_are_fixed_and_sensitive() {
        assert_eq!(
            validated_base_url_for_kind(LocalProviderKind::Openai, OPENAI_BASE_URL)
                .unwrap()
                .as_str(),
            OPENAI_BASE_URL
        );
        for invalid in [
            "https://api.openai.com/v1/",
            "https://api.openai.com/v1?x=1",
            "https://example.com/v1",
            "http://api.openai.com/v1",
        ] {
            assert!(
                validated_base_url_for_kind(LocalProviderKind::Openai, invalid).is_err(),
                "accepted {invalid}"
            );
        }
        let base = Url::parse(OPENAI_BASE_URL).unwrap();
        assert_eq!(
            route_url(&base, ProviderRoute::Embeddings).as_str(),
            "https://api.openai.com/v1/embeddings"
        );
        assert_eq!(
            route_url(&base, ProviderRoute::Responses).as_str(),
            "https://api.openai.com/v1/responses"
        );
        assert_eq!(
            route_url(&base, ProviderRoute::Models).as_str(),
            "https://api.openai.com/v1/models"
        );
        let header = authorization_header("sk-test-secret-marker-123456789").unwrap();
        assert!(header.is_sensitive());
        assert_eq!(
            header.to_str().unwrap(),
            "Bearer sk-test-secret-marker-123456789"
        );
        assert!(authorization_header("bad\nkey").is_err());

        let secret = "sk-test-discovery-secret-123456789";
        let client = provider_client(LocalProviderKind::Openai, DISCOVERY_TIMEOUT).unwrap();
        let request = authenticated_get_request(
            &client,
            LocalProviderKind::Openai,
            Some(secret),
            route_url(&base, ProviderRoute::Models),
        )
        .unwrap()
        .build()
        .unwrap();
        assert_eq!(request.method().as_str(), "GET");
        assert_eq!(request.url().as_str(), "https://api.openai.com/v1/models");
        let request_authorization = request.headers().get(AUTHORIZATION).unwrap();
        assert!(request_authorization.is_sensitive());
        assert_eq!(
            request_authorization.to_str().unwrap(),
            "Bearer sk-test-discovery-secret-123456789"
        );
        assert!(!format!("{request:?}").contains(secret));
        assert!(
            authenticated_get_request(
                &client,
                LocalProviderKind::Openai,
                None,
                route_url(&base, ProviderRoute::Models),
            )
            .is_err()
        );
        assert!(
            authenticated_get_request(
                &client,
                LocalProviderKind::OpenaiCompatibleLocal,
                Some(secret),
                route_url(&base, ProviderRoute::Models),
            )
            .is_err()
        );
    }

    #[test]
    fn discovery_target_requires_current_active_provider_and_scoped_url() {
        let local = StoredProviderSettings::default();
        let target = provider_model_discovery_target(
            &local,
            local.revision,
            LocalProviderKind::OpenaiCompatibleLocal,
            Some("http://127.0.0.1:1066/v1".to_string()),
        )
        .unwrap();
        assert_eq!(
            target,
            ProviderModelDiscoveryTarget::LmStudio {
                base_url: Url::parse("http://127.0.0.1:1066/v1").unwrap()
            }
        );
        assert_eq!(
            provider_model_discovery_target(
                &local,
                local.revision + 1,
                LocalProviderKind::OpenaiCompatibleLocal,
                Some(DEFAULT_BASE_URL.to_string()),
            )
            .unwrap_err()
            .code,
            "provider_settings_conflict"
        );
        assert_eq!(
            provider_model_discovery_target(
                &local,
                local.revision,
                LocalProviderKind::Openai,
                None,
            )
            .unwrap_err()
            .code,
            "provider_selection_conflict"
        );
        assert!(
            provider_model_discovery_target(
                &local,
                local.revision,
                LocalProviderKind::OpenaiCompatibleLocal,
                None,
            )
            .is_err()
        );

        let generation = Uuid::new_v4().to_string();
        let mut openai = StoredProviderSettings {
            provider_kind: LocalProviderKind::Openai,
            openai_credential_generation: Some(generation.clone()),
            ..StoredProviderSettings::default()
        };
        assert_eq!(
            provider_model_discovery_target(
                &openai,
                openai.revision,
                LocalProviderKind::Openai,
                None,
            )
            .unwrap(),
            ProviderModelDiscoveryTarget::Openai {
                base_url: Url::parse(OPENAI_BASE_URL).unwrap(),
                credential_generation: Some(generation),
            }
        );
        assert_eq!(
            provider_model_discovery_target(
                &openai,
                openai.revision,
                LocalProviderKind::Openai,
                Some(OPENAI_BASE_URL.to_string()),
            )
            .unwrap_err()
            .code,
            "provider_url_not_allowed"
        );
        openai.openai.base_url = "https://example.com/v1".to_string();
        assert!(
            provider_model_discovery_target(
                &openai,
                openai.revision,
                LocalProviderKind::Openai,
                None,
            )
            .is_err()
        );
    }

    #[cfg(unix)]
    #[test]
    fn config_rejects_regular_and_dangling_symlinks() {
        use std::os::unix::fs::symlink;
        let directory = tempfile::tempdir().unwrap();
        let target = directory.path().join("target.json");
        fs::write(&target, b"{}").unwrap();
        symlink(&target, config_path(directory.path())).unwrap();
        assert!(load_settings(directory.path()).is_err());

        fs::remove_file(config_path(directory.path())).unwrap();
        fs::remove_file(target).unwrap();
        symlink(
            directory.path().join("missing.json"),
            config_path(directory.path()),
        )
        .unwrap();
        assert!(load_settings(directory.path()).is_err());
        assert!(write_settings(directory.path(), &configured_stored_settings()).is_err());
    }

    #[test]
    fn optimistic_revision_is_stable_on_noop_and_clears_verification_on_change() {
        let mut current = configured_stored_settings();
        current.local.verified_revision = Some(1);
        current.local.verified_at_ms = Some(1_800_000_000_000);
        current.local.embedding_dimension = Some(2_560);
        let (unchanged, changed) = update_settings(
            current.clone(),
            1,
            LocalProviderKind::OpenaiCompatibleLocal,
            DEFAULT_BASE_URL.to_string(),
            current.local.chat_model_id.clone(),
            current.local.embedding_model_id.clone(),
        )
        .unwrap();
        assert!(!changed);
        assert_eq!(unchanged, current);

        let (updated, changed) = update_settings(
            current,
            1,
            LocalProviderKind::OpenaiCompatibleLocal,
            "http://127.0.0.1:1066/v1".to_string(),
            Some("new/chat".to_string()),
            Some("new/embedding".to_string()),
        )
        .unwrap();
        assert!(changed);
        assert_eq!(updated.revision, 2);
        assert_eq!(updated.local.revision, 2);
        assert_eq!(updated.local.verified_revision, None);
        assert_eq!(updated.local.verified_at_ms, None);
        assert_eq!(updated.local.embedding_dimension, None);
        assert!(
            update_settings(
                updated.clone(),
                1,
                LocalProviderKind::OpenaiCompatibleLocal,
                updated.local.base_url.clone(),
                updated.local.chat_model_id.clone(),
                updated.local.embedding_model_id.clone(),
            )
            .is_err()
        );

        let mut forged_verification = configured_stored_settings();
        forged_verification.local.embedding_model_id =
            forged_verification.local.chat_model_id.clone();
        forged_verification.local.verified_revision = Some(forged_verification.local.revision);
        forged_verification.local.verified_at_ms = Some(1_800_000_000_000);
        forged_verification.local.embedding_dimension = Some(2_560);
        assert!(forged_verification.validated().is_err());
        assert!(
            update_settings(
                configured_stored_settings(),
                1,
                LocalProviderKind::OpenaiCompatibleLocal,
                DEFAULT_BASE_URL.to_string(),
                Some("org/chat model".to_string()),
                Some("org/embed-model".to_string()),
            )
            .is_err()
        );
    }

    #[test]
    fn native_model_parser_is_typed_bounded_and_deterministic() {
        let list = parse_lm_studio_models(test_models_payload()).unwrap();
        assert_eq!(list.provider_kind, LocalProviderKind::OpenaiCompatibleLocal);
        assert!(!list.truncated);
        assert_eq!(list.models.len(), 3);
        assert_eq!(
            list.models[0].capabilities,
            vec![ProviderModelCapability::Chat]
        );
        assert_eq!(
            list.models[0].capability_source,
            ProviderModelCapabilitySource::Provider
        );
        assert_eq!(list.models[0].loaded, Some(true));
        assert_eq!(
            list.models[1].capabilities,
            vec![ProviderModelCapability::Embedding]
        );
        assert_eq!(list.models[1].loaded, Some(false));
        assert!(list.models[2].capabilities.is_empty());
        assert_eq!(
            list.models[2].capability_source,
            ProviderModelCapabilitySource::Unknown
        );
        assert_eq!(list.models[2].loaded, Some(false));
        assert_eq!(
            require_selected_typed_models(&configured_settings(), &list).unwrap(),
            ("org/chat-model".to_string(), "org/embed-model".to_string())
        );

        let mut wrong_type = configured_settings();
        wrong_type.chat_model_id = Some("org/other-model".to_string());
        assert!(require_selected_typed_models(&wrong_type, &list).is_err());
        let mut missing = configured_settings();
        missing.embedding_model_id = Some("org/not-returned".to_string());
        assert!(require_selected_typed_models(&missing, &list).is_err());

        let mut duplicate = test_models_payload();
        let models = duplicate
            .as_object_mut()
            .unwrap()
            .get_mut("models")
            .unwrap()
            .as_array_mut()
            .unwrap();
        let mut conflicting = models[0].clone();
        conflicting.as_object_mut().unwrap().insert(
            "display_name".to_string(),
            Value::String("Conflict".to_string()),
        );
        models.push(conflicting);
        assert!(parse_lm_studio_models(duplicate).is_err());

        let oversized = json!({
            "models": (0..MODEL_LIST_LIMIT + 10)
                .map(|index| json!({
                    "key": format!("org/model-{index:04}"),
                    "display_name": format!("Model {index:04}"),
                    "type": "llm"
                }))
                .collect::<Vec<_>>()
        });
        let oversized = parse_lm_studio_models(oversized).unwrap();
        assert_eq!(oversized.models.len(), MODEL_LIST_LIMIT);
        assert!(oversized.truncated);

        let invalid_identifier = json!({
            "models": [{
                "key": "org/mödél",
                "display_name": "Unicode display names remain fine",
                "type": "llm"
            }]
        });
        assert!(parse_lm_studio_models(invalid_identifier).is_err());
    }

    #[test]
    fn openai_model_parser_is_strict_deduplicated_classified_and_bounded() {
        let model = |id: &str| {
            json!({
                "id": id,
                "object": "model",
                "created": 1_800_000_000_u64,
                "owned_by": "openai"
            })
        };
        let list = parse_openai_models(json!({
            "object": "list",
            "data": [
                model("gpt-5.4-mini"),
                model("gpt-5.4-mini"),
                model("gpt-5.4-mini-2026-08-07"),
                model("text-embedding-3-small"),
                model("text-embedding-3-large"),
                model("text-embedding-ada-002"),
                model("gpt-image-1"),
                model("gpt-5.4-mini-transcribe"),
                model("omni-moderation-latest")
            ]
        }))
        .unwrap();
        assert_eq!(list.provider_kind, LocalProviderKind::Openai);
        assert!(!list.truncated);
        assert_eq!(list.models.len(), 8);
        for id in ["gpt-5.4-mini", "gpt-5.4-mini-2026-08-07"] {
            let discovered = list.models.iter().find(|item| item.id == id).unwrap();
            assert_eq!(discovered.capabilities, vec![ProviderModelCapability::Chat]);
            assert_eq!(
                discovered.capability_source,
                ProviderModelCapabilitySource::CvgnomeCatalog
            );
            assert_eq!(discovered.loaded, None);
        }
        for id in [
            "text-embedding-3-small",
            "text-embedding-3-large",
            "text-embedding-ada-002",
        ] {
            let discovered = list.models.iter().find(|item| item.id == id).unwrap();
            assert_eq!(
                discovered.capabilities,
                vec![ProviderModelCapability::Embedding]
            );
            assert_eq!(
                discovered.capability_source,
                ProviderModelCapabilitySource::CvgnomeCatalog
            );
        }
        for id in [
            "gpt-image-1",
            "gpt-5.4-mini-transcribe",
            "omni-moderation-latest",
        ] {
            let discovered = list.models.iter().find(|item| item.id == id).unwrap();
            assert!(discovered.capabilities.is_empty());
            assert_eq!(
                discovered.capability_source,
                ProviderModelCapabilitySource::Unknown
            );
        }
        let encoded = serde_json::to_value(&list).unwrap();
        assert_eq!(encoded["provider_kind"], "openai");
        assert_eq!(encoded["models"][0]["capabilities"], json!(["chat"]));
        assert_eq!(encoded["models"][0]["capability_source"], "cvgnome_catalog");
        assert_eq!(encoded["models"][0]["loaded"], Value::Null);
        assert_eq!(encoded["truncated"], false);

        for invalid in [
            json!({"object": "models", "data": []}),
            json!({"object": "list", "data": {}}),
            json!({
                "object": "list",
                "data": [{
                    "id": "gpt-5.4-mini",
                    "object": "not-a-model",
                    "created": 1_800_000_000_u64,
                    "owned_by": "openai"
                }]
            }),
            json!({
                "object": "list",
                "data": [{
                    "id": "gpt-5.4-mini bad",
                    "object": "model",
                    "created": 1_800_000_000_u64,
                    "owned_by": "openai"
                }]
            }),
            json!({
                "object": "list",
                "data": [{
                    "id": "gpt-5.4-mini",
                    "object": "model",
                    "created": "recently",
                    "owned_by": "openai"
                }]
            }),
            json!({
                "object": "list",
                "data": [{
                    "id": "gpt-5.4-mini",
                    "object": "model",
                    "created": 1_800_000_000_u64,
                    "owned_by": "open ai"
                }]
            }),
        ] {
            assert!(parse_openai_models(invalid).is_err());
        }

        let mut oversized_models = (0..MODEL_LIST_LIMIT + 20)
            .map(|index| model(&format!("unknown/model-{index:04}")))
            .collect::<Vec<_>>();
        oversized_models.push(model("gpt-5.4-mini"));
        let oversized = parse_openai_models(json!({
            "object": "list",
            "data": oversized_models
        }))
        .unwrap();
        assert!(oversized.truncated);
        assert_eq!(oversized.models.len(), MODEL_LIST_LIMIT);
        assert!(
            oversized
                .models
                .iter()
                .any(|item| item.id == "gpt-5.4-mini")
        );
    }

    #[test]
    fn chat_and_embedding_shapes_are_strict() {
        let expected_chat_content = serde_json::to_string(&json!({
            "basics": {
                "label": TEST_CHAT_LABEL,
                "summary": TEST_CHAT_SUMMARY
            },
            "work": [{
                "source_index": 0,
                "summary": TEST_CHAT_WORK_SUMMARY,
                "highlights": [TEST_CHAT_HIGHLIGHT]
            }]
        }))
        .unwrap();
        let chat = json!({
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": expected_chat_content}
            }]
        });
        let content = parse_chat_response_text(&chat).unwrap();
        validate_test_chat_content(&content).unwrap();
        let response_format = test_response_format();
        let schema = &response_format["json_schema"]["schema"];
        assert_eq!(schema["required"], json!(["basics", "work"]));
        assert_eq!(schema["properties"]["basics"]["type"], "object");
        assert_eq!(schema["properties"]["work"]["type"], "array");
        let work_item = &schema["properties"]["work"]["items"];
        assert_eq!(work_item["properties"]["source_index"]["type"], "integer");
        assert_eq!(work_item["properties"]["source_index"]["minimum"], 0);
        assert_eq!(work_item["properties"]["source_index"]["maximum"], 0);
        assert_eq!(
            work_item["required"],
            json!(["source_index", "summary", "highlights"])
        );
        assert!(
            parse_chat_response_text(&json!({
                "choices": [{
                    "finish_reason": "length",
                    "message": {"content": ""}
                }]
            }))
            .is_err()
        );
        assert_eq!(
            parse_chat_response_text(&json!({
                "choices": [{
                    "finish_reason": "stop",
                    "message": {
                        "content": "{}",
                        "reasoning": "Provider-specific reasoning metadata is ignored.",
                        "tool_calls": [],
                        "refusal": ""
                    }
                }]
            }))
            .unwrap(),
            "{}"
        );
        for rejected_message in [
            json!({"content": "{}", "tool_calls": [{"id": "call-1"}]}),
            json!({"content": "{}", "tool_calls": {}}),
            json!({"content": "{}", "refusal": "I cannot comply."}),
            json!({"content": "{}", "refusal": false}),
        ] {
            assert!(
                parse_chat_response_text(&json!({
                    "choices": [{
                        "finish_reason": "stop",
                        "message": rejected_message
                    }]
                }))
                .is_err()
            );
        }
        assert!(
            validate_test_chat_content(
                "{\"basics\":{\"label\":\"local-check\",\"summary\":\"structured-output-ok\"},\"work\":[{\"source_index\":1,\"summary\":\"summary-branch-ok\",\"highlights\":[\"highlights-branch-ok\"]}]}"
            )
            .is_err()
        );
        assert!(
            validate_test_chat_content(
                "{\"basics\":{\"label\":\"local-check\",\"summary\":\"structured-output-ok\"},\"work\":[{\"source_index\":0,\"summary\":\"summary-branch-ok\",\"highlights\":[\"highlights-branch-ok\"],\"extra\":true}]}"
            )
            .is_err()
        );
        assert!(
            validate_test_chat_content(
                "{\"basics\":{\"label\":\"local-check\",\"label\":\"local-check\",\"summary\":\"structured-output-ok\"},\"work\":[{\"source_index\":0,\"summary\":\"summary-branch-ok\",\"highlights\":[\"highlights-branch-ok\"]}]}"
            )
            .is_err()
        );

        let embeddings = json!({
            "data": [
                {"index": 1, "embedding": [0.3, 0.4]},
                {"index": 0, "embedding": [0.1, 0.2]}
            ]
        });
        assert_eq!(
            parse_embedding_vectors(&embeddings, 2, Some(2)).unwrap(),
            vec![vec![0.1, 0.2], vec![0.3, 0.4]]
        );
        assert!(parse_embedding_vectors(&embeddings, 2, Some(3)).is_err());
        assert!(
            parse_embedding_vectors(
                &json!({"data": [{"index": 0, "embedding": ["NaN"]}]}),
                1,
                None
            )
            .is_err()
        );
    }

    #[test]
    fn responses_request_and_parser_are_strict_nonstored_and_model_compatible() {
        assert_eq!(
            TransportError::new(TransportErrorKind::Invalid)
                .public_for_stage(ProviderStage::Embedding)
                .code,
            "provider_embedding_response_invalid"
        );
        assert_eq!(
            TransportError::new(TransportErrorKind::Invalid)
                .public_for_stage(ProviderStage::Chat)
                .code,
            "provider_chat_response_invalid"
        );
        let body = openai_response_body(
            "gpt-test",
            TEST_CHAT_SYSTEM,
            TEST_CHAT_USER,
            &test_response_format(),
            1_024,
        )
        .unwrap();
        assert_eq!(body["model"], "gpt-test");
        assert_eq!(body["store"], false);
        assert_eq!(body["stream"], false);
        assert_eq!(body["max_output_tokens"], 1_024);
        assert!(body.get("temperature").is_none());
        assert_eq!(body["text"]["format"]["type"], "json_schema");
        assert_eq!(body["text"]["format"]["strict"], true);
        assert_eq!(
            body["text"]["format"]["schema"],
            test_response_format()["json_schema"]["schema"]
        );
        let optional_property_schema = json!({
            "type": "json_schema",
            "json_schema": {
                "name": "invalid_optional_property",
                "strict": true,
                "schema": {
                    "type": "object",
                    "additionalProperties": false,
                    "required": ["present"],
                    "properties": {
                        "present": {"type": "string"},
                        "optional": {"type": "string"},
                    },
                },
            },
        });
        assert!(
            openai_response_body(
                "gpt-test",
                TEST_CHAT_SYSTEM,
                TEST_CHAT_USER,
                &optional_property_schema,
                1_024,
            )
            .is_err()
        );

        let content = serde_json::to_string(&json!({
            "basics": {
                "label": TEST_CHAT_LABEL,
                "summary": TEST_CHAT_SUMMARY,
            },
            "work": [{
                "source_index": 0,
                "summary": TEST_CHAT_WORK_SUMMARY,
                "highlights": [TEST_CHAT_HIGHLIGHT],
            }],
        }))
        .unwrap();
        let completed = json!({
            "status": "completed",
            "output": [
                {"type": "reasoning", "summary": []},
                {
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": content}],
                },
            ],
        });
        let parsed = parse_openai_response_text(&completed).unwrap();
        validate_test_chat_content(&parsed).unwrap();

        assert_eq!(
            parse_openai_response_text(&json!({
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [],
            }))
            .unwrap_err()
            .kind,
            TransportErrorKind::Incomplete
        );
        assert_eq!(
            parse_openai_response_text(&json!({
                "status": "completed",
                "output": [{
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "refusal", "refusal": "declined"}],
                }],
            }))
            .unwrap_err()
            .kind,
            TransportErrorKind::Refusal
        );

        for rejected in [
            json!({
                "status": "completed",
                "output": [{"type": "function_call", "name": "tool"}],
            }),
            json!({
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "{}"}],
                    },
                    {
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "{}"}],
                    },
                ],
            }),
        ] {
            assert!(parse_openai_response_text(&rejected).is_err());
        }
    }

    #[test]
    fn openai_usage_receipts_are_exact_bounded_and_required() {
        assert_eq!(
            parse_openai_embedding_usage(&json!({
                "usage": {"prompt_tokens": 27, "total_tokens": 27}
            }))
            .unwrap(),
            ProviderTokenUsage {
                input_tokens: 27,
                output_tokens: 0,
            }
        );
        assert_eq!(
            parse_openai_response_usage(&json!({
                "usage": {
                    "input_tokens": 101,
                    "input_tokens_details": {"cached_tokens": 80},
                    "output_tokens": 39,
                    "output_tokens_details": {"reasoning_tokens": 20},
                    "total_tokens": 140
                }
            }))
            .unwrap(),
            ProviderTokenUsage {
                input_tokens: 101,
                output_tokens: 39,
            }
        );
        for invalid in [
            json!({}),
            json!({"usage": null}),
            json!({"usage": {"prompt_tokens": 2, "total_tokens": 3}}),
            json!({"usage": {"prompt_tokens": -1, "total_tokens": -1}}),
        ] {
            assert_eq!(
                parse_openai_embedding_usage(&invalid).unwrap_err().kind,
                TransportErrorKind::UsageReceipt
            );
        }
        for invalid in [
            json!({}),
            json!({"usage": {"input_tokens": 1, "output_tokens": 1}}),
            json!({"usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 3}}),
            json!({"usage": {"input_tokens": 0, "output_tokens": 1, "total_tokens": 1}}),
            json!({"usage": {"input_tokens": 1, "output_tokens": 0, "total_tokens": 1}}),
        ] {
            let error = parse_openai_response_usage(&invalid).unwrap_err();
            assert_eq!(error.kind, TransportErrorKind::UsageReceipt);
            assert_eq!(error.public().code, "provider_spend_usage_missing");
        }

        let embedding_error = parse_embedding_provider_response(
            LocalProviderKind::Openai,
            &json!({
                "usage": {"prompt_tokens": 8, "total_tokens": 8},
                "data": [{"index": 0, "embedding": ["invalid"]}]
            }),
            1,
            None,
        )
        .unwrap_err();
        assert_eq!(embedding_error.kind, TransportErrorKind::Invalid);
        assert_eq!(
            embedding_error.usage,
            Some(ProviderTokenUsage {
                input_tokens: 8,
                output_tokens: 0,
            })
        );

        let response_error = parse_openai_provider_response(&json!({
            "status": "incomplete",
            "output": [],
            "usage": {"input_tokens": 40, "output_tokens": 10, "total_tokens": 50}
        }))
        .unwrap_err();
        assert_eq!(response_error.kind, TransportErrorKind::Incomplete);
        assert_eq!(
            response_error.usage,
            Some(ProviderTokenUsage {
                input_tokens: 40,
                output_tokens: 10,
            })
        );
    }

    #[test]
    fn loopback_transport_uses_only_allowlisted_routes_and_distinct_models() {
        let chat_content = serde_json::to_string(&json!({
            "basics": {
                "label": TEST_CHAT_LABEL,
                "summary": TEST_CHAT_SUMMARY
            },
            "work": [{
                "source_index": 0,
                "summary": TEST_CHAT_WORK_SUMMARY,
                "highlights": [TEST_CHAT_HIGHLIGHT]
            }]
        }))
        .unwrap();
        let (base, requests, server) = spawn_stub_server(vec![
            json_response(test_models_payload()),
            json_response(json!({
                "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]
            })),
            json_response(json!({
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": chat_content}
                }]
            })),
        ]);
        tauri::async_runtime::block_on(async {
            let url = validated_base_url(&base).unwrap();
            let discovery = discover_lm_studio_models_at(&url).await.unwrap();
            let (chat, embedding) =
                require_selected_typed_models(&configured_settings(), &discovery).unwrap();
            assert_eq!(
                test_embedding_model(
                    LocalProviderKind::OpenaiCompatibleLocal,
                    None,
                    &url,
                    &embedding,
                )
                .await
                .unwrap(),
                (3, None)
            );
            test_chat_model(&url, &chat).await.unwrap();
        });
        server.join().unwrap();
        let requests = requests.lock().unwrap();
        assert_eq!(
            requests
                .iter()
                .map(|request| (request.method.as_str(), request.path.as_str()))
                .collect::<Vec<_>>(),
            vec![
                ("GET", "/api/v1/models"),
                ("POST", "/v1/embeddings"),
                ("POST", "/v1/chat/completions")
            ]
        );
        let embedding_body: Value = serde_json::from_slice(&requests[1].body).unwrap();
        let chat_body: Value = serde_json::from_slice(&requests[2].body).unwrap();
        assert_eq!(
            embedding_body.get("model").and_then(Value::as_str),
            Some("org/embed-model")
        );
        assert_eq!(
            chat_body.get("model").and_then(Value::as_str),
            Some("org/chat-model")
        );
        assert_eq!(chat_body.get("stream"), Some(&Value::Bool(false)));
        assert_eq!(
            chat_body.get("max_tokens").and_then(Value::as_u64),
            Some(1_024)
        );
        assert_eq!(
            chat_body.get("response_format"),
            Some(&test_response_format())
        );
        let encoded = String::from_utf8(requests[1].body.clone()).unwrap()
            + &String::from_utf8(requests[2].body.clone()).unwrap();
        assert!(!encoded.contains("career"));
        assert!(!encoded.contains("resume"));
    }

    #[test]
    fn redirects_are_refused_and_not_followed() {
        let (base, requests, server) = spawn_stub_server(vec![StubResponse {
            status: 302,
            headers: vec![(
                "Location".to_string(),
                "http://127.0.0.1:9/escaped".to_string(),
            )],
            body: Vec::new(),
        }]);
        let error = tauri::async_runtime::block_on(async {
            discover_lm_studio_models_at(&validated_base_url(&base).unwrap())
                .await
                .unwrap_err()
        });
        assert_eq!(error.kind, TransportErrorKind::Http);
        server.join().unwrap();
        let requests = requests.lock().unwrap();
        assert_eq!(requests.len(), 1);
        assert_eq!(requests[0].path, "/api/v1/models");
    }

    #[test]
    fn response_caps_and_public_errors_do_not_leak_bodies() {
        let secret_marker = "provider-secret-marker";
        let (base, _, server) = spawn_stub_server(vec![StubResponse {
            status: 500,
            headers: vec![("Content-Type".to_string(), "text/plain".to_string())],
            body: secret_marker.as_bytes().to_vec(),
        }]);
        let error = tauri::async_runtime::block_on(async {
            get_json(
                LocalProviderKind::OpenaiCompatibleLocal,
                None,
                route_url(
                    &validated_base_url(&base).unwrap(),
                    ProviderRoute::LmStudioModels,
                ),
                1_024,
                DISCOVERY_TIMEOUT,
            )
            .await
            .unwrap_err()
        });
        server.join().unwrap();
        let public = error.public().command_message();
        assert!(!public.contains(secret_marker));
        assert_eq!(error.kind, TransportErrorKind::Http);

        let (base, _, server) = spawn_stub_server(vec![StubResponse {
            status: 200,
            headers: vec![("Content-Length".to_string(), "2000000".to_string())],
            body: Vec::new(),
        }]);
        let error = tauri::async_runtime::block_on(async {
            get_json(
                LocalProviderKind::OpenaiCompatibleLocal,
                None,
                route_url(
                    &validated_base_url(&base).unwrap(),
                    ProviderRoute::LmStudioModels,
                ),
                1_024,
                DISCOVERY_TIMEOUT,
            )
            .await
            .unwrap_err()
        });
        server.join().unwrap();
        assert_eq!(error.kind, TransportErrorKind::TooLarge);
    }

    #[test]
    fn maximum_valid_embedding_shape_fits_provider_arm_and_sidecar_caps() {
        let provider_response = json!({
            "data": (0..MAX_TAILORING_EMBEDDING_INPUTS)
                .map(|index| json!({
                    "index": index,
                    "embedding": vec![-f64::MAX; MAX_EMBEDDING_DIMENSION]
                }))
                .collect::<Vec<_>>()
        });
        let provider_bytes = serde_json::to_vec(&provider_response).unwrap();
        assert!(provider_bytes.len() <= EMBEDDING_RESPONSE_LIMIT);
        let embeddings = parse_embedding_vectors(
            &provider_response,
            MAX_TAILORING_EMBEDDING_INPUTS,
            Some(MAX_EMBEDDING_DIMENSION),
        )
        .unwrap();
        drop(provider_response);

        let arm_params = json!({
            "attempt_id": "11111111-1111-4111-8111-111111111111",
            "embeddings": embeddings
        });
        let arm_bytes = serde_json::to_vec(&arm_params).unwrap();
        assert!(arm_bytes.len() > 2_500_000);
        assert!(arm_bytes.len() <= MAX_ENGINE_ARM_PARAMS_BYTES);

        let sidecar_request = serde_json::to_vec(&json!({
            "protocol_version": crate::ENGINE_PROTOCOL_VERSION,
            "id": "22222222-2222-4222-8222-222222222222",
            "method": "tailoring.arm",
            "params": arm_params
        }))
        .unwrap();
        assert!(sidecar_request.len() < crate::ENGINE_REQUEST_LIMIT_BYTES);
    }

    #[test]
    fn provisional_engine_contracts_are_strict_and_consistent() {
        let settings = configured_settings();
        let prepare: TailoringPrepareResult = serde_json::from_value(json!({
            "attempt_id": "11111111-1111-4111-8111-111111111111",
            "embedding_model_id": "org/embed-model",
            "embedding_input": ["query", "chunk"]
        }))
        .unwrap();
        assert!(prepare.validated(&settings).is_ok());
        assert!(
            serde_json::from_value::<TailoringPrepareResult>(json!({
                "attempt_id": "11111111-1111-4111-8111-111111111111",
                "embedding_model_id": "org/embed-model",
                "embedding_input": ["query", "chunk"],
                "unexpected": true
            }))
            .is_err()
        );

        let arm: TailoringArmResult = serde_json::from_value(json!({
            "attempt_id": "11111111-1111-4111-8111-111111111111",
            "chat_model_id": "org/chat-model",
            "messages": [
                {"role": "system", "content": "Return JSON."},
                {"role": "user", "content": "Draft locally."}
            ],
            "response_format": test_response_format(),
            "max_tokens": 4_000
        }))
        .unwrap();
        assert!(
            arm.validated("11111111-1111-4111-8111-111111111111", &settings)
                .is_ok()
        );

        let draft_value = json!({
            "id": "22222222-2222-4222-8222-222222222222",
            "attempt_id": "11111111-1111-4111-8111-111111111111",
            "created_at_ms": 1_800_000_000_000_u64,
            "opportunity": {
                "id": "33333333-3333-4333-8333-333333333333",
                "title": "Product Lead",
                "company": "Northstar"
            },
            "snapshot": {
                "id": "55555555-5555-4555-8555-555555555555",
                "checksum_sha256": "c".repeat(64)
            },
            "profile_version": {
                "id": "44444444-4444-4444-8444-444444444444",
                "version_number": 3,
                "checksum_sha256": "a".repeat(64)
            },
            "models": {
                "provider_kind": "openai_compatible_local",
                "chat_model_id": "org/chat-model",
                "embedding_model_id": "org/embed-model"
            },
            "input_fingerprint": "b".repeat(64),
            "selected_evidence": [{
                "chunk_id": "chunk-1",
                "kind": "work",
                "path": "/work/0/highlights/0",
                "content": "Shipped a grounded local workflow."
            }],
            "quality_issue_codes": [],
            "resume": {"basics": {"name": "Ada"}, "work": []}
        });
        let draft: TailoredResumeDraft = serde_json::from_value(draft_value.clone()).unwrap();
        assert!(
            draft
                .validated(
                    "33333333-3333-4333-8333-333333333333",
                    Some("11111111-1111-4111-8111-111111111111"),
                    Some(&settings)
                )
                .is_ok()
        );

        let mut invalid_snapshot = draft_value;
        invalid_snapshot["snapshot"]["checksum_sha256"] = json!("not-a-checksum");
        let invalid_snapshot: TailoredResumeDraft =
            serde_json::from_value(invalid_snapshot).unwrap();
        assert!(
            invalid_snapshot
                .validated(
                    "33333333-3333-4333-8333-333333333333",
                    Some("11111111-1111-4111-8111-111111111111"),
                    Some(&settings)
                )
                .is_err()
        );
    }
}
