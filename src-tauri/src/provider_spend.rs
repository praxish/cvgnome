// SPDX-License-Identifier: MPL-2.0
use crate::local_provider::{LocalProviderKind, LocalProviderRuntime};
use crate::prepare_data_dir;
use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use tempfile::NamedTempFile;
use uuid::Uuid;

const SPEND_FILENAME: &str = "provider-spend.json";
const SPEND_SCHEMA_VERSION: u32 = 1;
const SPEND_FILE_LIMIT_BYTES: u64 = 512 * 1024;
const JAVASCRIPT_MAX_SAFE_INTEGER: u64 = 9_007_199_254_740_991;
const MAX_MODEL_RATES: usize = 64;
const MAX_ACTIVITY: usize = 256;
const MAX_MONTH_ROLLUPS: usize = 36;
const MAX_RATE_MICROUSD_PER_MILLION_TOKENS: u64 = 1_000_000_000_000;

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub(crate) enum ProviderSpendStage {
    Embedding,
    Chat,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub(crate) enum ProviderSpendKind {
    Test,
    Tailoring,
    // Read compatibility for historical activity; no new requests use this kind.
    #[serde(rename = "matching")]
    LegacyMatching,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum ProviderSpendStatus {
    Reserved,
    Completed,
    Uncertain,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct ProviderTokenUsage {
    pub(crate) input_tokens: u64,
    pub(crate) output_tokens: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct ProviderModelRate {
    provider_kind: LocalProviderKind,
    model_id: String,
    input_microusd_per_million_tokens: u64,
    output_microusd_per_million_tokens: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProviderSpendActivity {
    id: String,
    operation_id: String,
    // Preserve attribution in existing ledgers without activating candidate scope.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    workspace_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    candidate_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    actor_id: Option<String>,
    kind: ProviderSpendKind,
    stage: ProviderSpendStage,
    provider_kind: LocalProviderKind,
    model_id: String,
    month_key: String,
    maximum_input_tokens: u64,
    maximum_output_tokens: u64,
    input_tokens: Option<u64>,
    output_tokens: Option<u64>,
    estimated_cost_microusd: Option<u64>,
    reserved_cost_microusd: Option<u64>,
    occurred_at_ms: u64,
    status: ProviderSpendStatus,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProviderSpendMonthRollup {
    month_key: String,
    estimated_cost_microusd: u64,
    request_count: u64,
    unpriced_request_count: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct StoredProviderSpend {
    schema_version: u32,
    revision: u64,
    enabled: bool,
    monthly_limit_microusd: Option<u64>,
    per_request_limit_microusd: Option<u64>,
    block_unpriced: bool,
    model_rates: Vec<ProviderModelRate>,
    month_rollups: Vec<ProviderSpendMonthRollup>,
    activity: Vec<ProviderSpendActivity>,
}

impl Default for StoredProviderSpend {
    fn default() -> Self {
        Self {
            schema_version: SPEND_SCHEMA_VERSION,
            revision: 1,
            enabled: false,
            monthly_limit_microusd: None,
            per_request_limit_microusd: None,
            block_unpriced: true,
            model_rates: Vec::new(),
            month_rollups: Vec::new(),
            activity: Vec::new(),
        }
    }
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ProviderSpendSummary {
    month_key: String,
    timezone: &'static str,
    estimated_cost_microusd: u64,
    reserved_cost_microusd: u64,
    request_count: u64,
    unpriced_request_count: u64,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct ProviderSpendControlView {
    schema_version: u32,
    revision: u64,
    enabled: bool,
    monthly_limit_microusd: Option<u64>,
    per_request_limit_microusd: Option<u64>,
    block_unpriced: bool,
    model_rates: Vec<ProviderModelRate>,
    summary: ProviderSpendSummary,
    activity: Vec<ProviderSpendActivity>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct ProviderSpendError {
    code: &'static str,
    message: &'static str,
}

impl ProviderSpendError {
    const fn new(code: &'static str, message: &'static str) -> Self {
        Self { code, message }
    }

    fn invalid() -> Self {
        Self::new(
            "provider_spend_settings_invalid",
            "The saved provider spend controls are invalid.",
        )
    }

    pub(crate) fn command_message(self) -> String {
        format!("{}: {}", self.code, self.message)
    }
}

#[derive(Clone, Debug)]
pub(crate) struct ProviderSpendReservation {
    id: String,
    model_rate: Option<ProviderModelRate>,
    maximum_input_tokens: u64,
    maximum_output_tokens: u64,
}

fn valid_uuid(value: &str) -> bool {
    Uuid::parse_str(value)
        .ok()
        .is_some_and(|parsed| parsed.to_string() == value)
}

fn valid_scope_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
}

fn valid_attribution(entry: &ProviderSpendActivity) -> bool {
    match (&entry.workspace_id, &entry.candidate_id, &entry.actor_id) {
        (None, None, None) => true,
        (Some(workspace), Some(candidate), Some(actor)) => {
            valid_scope_id(workspace) && valid_scope_id(candidate) && actor == "local-owner"
        }
        _ => false,
    }
}

fn valid_model_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 240
        && value.bytes().all(|byte| (0x21..=0x7e).contains(&byte))
}

fn valid_month_key(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() == 7
        && bytes[4] == b'-'
        && bytes[..4].iter().all(u8::is_ascii_digit)
        && bytes[5..].iter().all(u8::is_ascii_digit)
        && value[5..]
            .parse::<u8>()
            .is_ok_and(|month| (1..=12).contains(&month))
}

fn utc_month_key(timestamp_ms: u64) -> Result<String, ProviderSpendError> {
    let days_since_epoch =
        i64::try_from(timestamp_ms / 86_400_000).map_err(|_| ProviderSpendError::invalid())?;
    // Proleptic Gregorian civil date conversion for days since 1970-01-01.
    // The bounded timestamps accepted by this module are non-negative.
    let days = days_since_epoch
        .checked_add(719_468)
        .ok_or_else(ProviderSpendError::invalid)?;
    let era = days / 146_097;
    let day_of_era = days - era * 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let mut year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_prime = (5 * day_of_year + 2) / 153;
    let month = month_prime + if month_prime < 10 { 3 } else { -9 };
    year += i64::from(month <= 2);
    if !(0..=9_999).contains(&year) || !(1..=12).contains(&month) {
        return Err(ProviderSpendError::invalid());
    }
    Ok(format!("{year:04}-{month:02}"))
}

fn current_timestamp_ms() -> Result<u64, ProviderSpendError> {
    let timestamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|_| ProviderSpendError::invalid())?
        .as_millis();
    u64::try_from(timestamp)
        .ok()
        .filter(|value| *value > 0 && *value <= JAVASCRIPT_MAX_SAFE_INTEGER)
        .ok_or_else(ProviderSpendError::invalid)
}

fn valid_optional_positive(value: Option<u64>) -> bool {
    value.is_none_or(|amount| (1..=JAVASCRIPT_MAX_SAFE_INTEGER).contains(&amount))
}

impl StoredProviderSpend {
    fn validated(self) -> Result<Self, ProviderSpendError> {
        if self.schema_version != SPEND_SCHEMA_VERSION
            || !(1..=JAVASCRIPT_MAX_SAFE_INTEGER).contains(&self.revision)
            || !valid_optional_positive(self.monthly_limit_microusd)
            || !valid_optional_positive(self.per_request_limit_microusd)
            || (self.enabled && !self.block_unpriced)
            || self.model_rates.len() > MAX_MODEL_RATES
            || self.month_rollups.len() > MAX_MONTH_ROLLUPS
            || self.activity.len() > MAX_ACTIVITY
        {
            return Err(ProviderSpendError::invalid());
        }
        let mut rate_keys = HashSet::new();
        for rate in &self.model_rates {
            if rate.provider_kind != LocalProviderKind::Openai
                || !valid_model_id(&rate.model_id)
                || rate.input_microusd_per_million_tokens > MAX_RATE_MICROUSD_PER_MILLION_TOKENS
                || rate.output_microusd_per_million_tokens > MAX_RATE_MICROUSD_PER_MILLION_TOKENS
                || (rate.input_microusd_per_million_tokens == 0
                    && rate.output_microusd_per_million_tokens == 0)
                || !rate_keys.insert((rate.provider_kind, rate.model_id.as_str()))
            {
                return Err(ProviderSpendError::invalid());
            }
        }
        let mut month_keys = HashSet::new();
        for month in &self.month_rollups {
            if !valid_month_key(&month.month_key)
                || month.estimated_cost_microusd > JAVASCRIPT_MAX_SAFE_INTEGER
                || month.request_count > JAVASCRIPT_MAX_SAFE_INTEGER
                || month.unpriced_request_count > month.request_count
                || !month_keys.insert(month.month_key.as_str())
            {
                return Err(ProviderSpendError::invalid());
            }
        }
        let mut activity_ids = HashSet::new();
        for entry in &self.activity {
            if !valid_uuid(&entry.id)
                || !valid_uuid(&entry.operation_id)
                || !valid_attribution(entry)
                || entry.provider_kind != LocalProviderKind::Openai
                || !valid_model_id(&entry.model_id)
                || !valid_month_key(&entry.month_key)
                || entry.maximum_input_tokens == 0
                || entry.maximum_input_tokens > JAVASCRIPT_MAX_SAFE_INTEGER
                || entry.maximum_output_tokens > JAVASCRIPT_MAX_SAFE_INTEGER
                || entry
                    .input_tokens
                    .is_some_and(|value| value > JAVASCRIPT_MAX_SAFE_INTEGER)
                || entry
                    .output_tokens
                    .is_some_and(|value| value > JAVASCRIPT_MAX_SAFE_INTEGER)
                || entry
                    .estimated_cost_microusd
                    .is_some_and(|value| value > JAVASCRIPT_MAX_SAFE_INTEGER)
                || entry
                    .reserved_cost_microusd
                    .is_some_and(|value| value > JAVASCRIPT_MAX_SAFE_INTEGER)
                || !(1..=JAVASCRIPT_MAX_SAFE_INTEGER).contains(&entry.occurred_at_ms)
                || !activity_ids.insert(entry.id.as_str())
            {
                return Err(ProviderSpendError::invalid());
            }
            match entry.status {
                ProviderSpendStatus::Reserved
                    if entry.input_tokens.is_none()
                        && entry.output_tokens.is_none()
                        && entry.estimated_cost_microusd.is_none() => {}
                ProviderSpendStatus::Completed
                    if entry.input_tokens.is_some() && entry.output_tokens.is_some() => {}
                ProviderSpendStatus::Uncertain
                    if entry.input_tokens.is_some() == entry.output_tokens.is_some() => {}
                _ => return Err(ProviderSpendError::invalid()),
            }
        }
        Ok(self)
    }

    fn increment_revision(&mut self) -> Result<(), ProviderSpendError> {
        if self.revision >= JAVASCRIPT_MAX_SAFE_INTEGER {
            return Err(ProviderSpendError::invalid());
        }
        self.revision += 1;
        Ok(())
    }

    fn summary_for_month(
        &self,
        month_key: &str,
    ) -> Result<ProviderSpendSummary, ProviderSpendError> {
        if !valid_month_key(month_key) {
            return Err(ProviderSpendError::invalid());
        }
        let rollup = self
            .month_rollups
            .iter()
            .find(|rollup| rollup.month_key == month_key);
        let mut reserved_cost = 0_u64;
        let mut pending_count = 0_u64;
        let mut pending_unpriced = 0_u64;
        for entry in self.activity.iter().filter(|entry| {
            entry.month_key == month_key && entry.status == ProviderSpendStatus::Reserved
        }) {
            pending_count = pending_count
                .checked_add(1)
                .ok_or_else(ProviderSpendError::invalid)?;
            if let Some(cost) = entry.reserved_cost_microusd {
                reserved_cost = reserved_cost
                    .checked_add(cost)
                    .ok_or_else(ProviderSpendError::invalid)?;
            } else {
                pending_unpriced = pending_unpriced
                    .checked_add(1)
                    .ok_or_else(ProviderSpendError::invalid)?;
            }
        }
        let completed_count = rollup.map_or(0, |rollup| rollup.request_count);
        let completed_unpriced = rollup.map_or(0, |rollup| rollup.unpriced_request_count);
        Ok(ProviderSpendSummary {
            month_key: month_key.to_string(),
            timezone: "UTC",
            estimated_cost_microusd: rollup.map_or(0, |rollup| rollup.estimated_cost_microusd),
            reserved_cost_microusd: reserved_cost,
            request_count: completed_count
                .checked_add(pending_count)
                .ok_or_else(ProviderSpendError::invalid)?,
            unpriced_request_count: completed_unpriced
                .checked_add(pending_unpriced)
                .ok_or_else(ProviderSpendError::invalid)?,
        })
    }

    fn current_summary(&self) -> Result<ProviderSpendSummary, ProviderSpendError> {
        self.summary_for_month(&utc_month_key(current_timestamp_ms()?)?)
    }

    fn view(&self) -> Result<ProviderSpendControlView, ProviderSpendError> {
        // Historical candidate activity still counts toward the shared allowance,
        // but its operation identifiers do not belong in the personal history.
        let mut activity: Vec<_> = self
            .activity
            .iter()
            .filter(|entry| {
                entry.candidate_id.is_none() && entry.kind != ProviderSpendKind::LegacyMatching
            })
            .cloned()
            .collect();
        activity.reverse();
        Ok(ProviderSpendControlView {
            schema_version: self.schema_version,
            revision: self.revision,
            enabled: self.enabled,
            monthly_limit_microusd: self.monthly_limit_microusd,
            per_request_limit_microusd: self.per_request_limit_microusd,
            block_unpriced: self.block_unpriced,
            model_rates: self.model_rates.clone(),
            summary: self.current_summary()?,
            activity,
        })
    }

    fn record_completed_cost(
        &mut self,
        month_key: &str,
        estimated_cost_microusd: Option<u64>,
    ) -> Result<(), ProviderSpendError> {
        let position = self
            .month_rollups
            .iter()
            .position(|rollup| rollup.month_key == month_key);
        let rollup = if let Some(position) = position {
            &mut self.month_rollups[position]
        } else {
            if self.month_rollups.len() >= MAX_MONTH_ROLLUPS {
                self.month_rollups
                    .sort_by(|left, right| left.month_key.cmp(&right.month_key));
                self.month_rollups.remove(0);
            }
            self.month_rollups.push(ProviderSpendMonthRollup {
                month_key: month_key.to_string(),
                estimated_cost_microusd: 0,
                request_count: 0,
                unpriced_request_count: 0,
            });
            self.month_rollups.last_mut().unwrap()
        };
        rollup.request_count = rollup
            .request_count
            .checked_add(1)
            .filter(|value| *value <= JAVASCRIPT_MAX_SAFE_INTEGER)
            .ok_or_else(ProviderSpendError::invalid)?;
        if let Some(cost) = estimated_cost_microusd {
            rollup.estimated_cost_microusd = rollup
                .estimated_cost_microusd
                .checked_add(cost)
                .filter(|value| *value <= JAVASCRIPT_MAX_SAFE_INTEGER)
                .ok_or_else(ProviderSpendError::invalid)?;
        } else {
            rollup.unpriced_request_count = rollup
                .unpriced_request_count
                .checked_add(1)
                .filter(|value| *value <= JAVASCRIPT_MAX_SAFE_INTEGER)
                .ok_or_else(ProviderSpendError::invalid)?;
        }
        Ok(())
    }

    fn prune_activity(&mut self) {
        while self.activity.len() > MAX_ACTIVITY {
            let removable = self
                .activity
                .iter()
                .position(|entry| entry.status != ProviderSpendStatus::Reserved);
            if let Some(position) = removable {
                self.activity.remove(position);
            } else {
                break;
            }
        }
    }
}

pub(crate) fn require_priced_models_if_guarded(
    data_dir: &Path,
    provider_kind: LocalProviderKind,
    model_ids: &[&str],
) -> Result<(), ProviderSpendError> {
    if provider_kind == LocalProviderKind::OpenaiCompatibleLocal {
        return Ok(());
    }
    if model_ids.is_empty() || model_ids.iter().any(|model_id| !valid_model_id(model_id)) {
        return Err(ProviderSpendError::invalid());
    }
    let store = load_store(data_dir)?;
    if !store.enabled {
        return Ok(());
    }
    if !store.block_unpriced {
        return Err(ProviderSpendError::invalid());
    }
    if model_ids.iter().any(|model_id| {
        !store
            .model_rates
            .iter()
            .any(|rate| rate.provider_kind == provider_kind && rate.model_id == **model_id)
    }) {
        return Err(ProviderSpendError::new(
            "provider_spend_rate_required",
            "Add current input and output rates for both selected models before sending a cloud request.",
        ));
    }
    Ok(())
}

fn spend_path(data_dir: &Path) -> PathBuf {
    data_dir.join(SPEND_FILENAME)
}

fn secure_regular_file(path: &Path) -> Result<(), ProviderSpendError> {
    let metadata = fs::symlink_metadata(path).map_err(|_| ProviderSpendError::invalid())?;
    if metadata.file_type().is_symlink()
        || !metadata.file_type().is_file()
        || metadata.len() == 0
        || metadata.len() > SPEND_FILE_LIMIT_BYTES
    {
        return Err(ProviderSpendError::invalid());
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o600))
            .map_err(|_| ProviderSpendError::invalid())?;
    }
    Ok(())
}

pub(crate) fn load_provider_spend(
    data_dir: &Path,
) -> Result<ProviderSpendControlView, ProviderSpendError> {
    load_store(data_dir)?.view()
}

pub(crate) fn reconcile_interrupted_provider_spend(
    data_dir: &Path,
) -> Result<(), ProviderSpendError> {
    let mut store = load_store(data_dir)?;
    let reserved_positions = store
        .activity
        .iter()
        .enumerate()
        .filter_map(|(index, entry)| {
            (entry.status == ProviderSpendStatus::Reserved).then_some(index)
        })
        .collect::<Vec<_>>();
    if reserved_positions.is_empty() {
        return Ok(());
    }
    let reconciled_at_ms = current_timestamp_ms()?;
    for position in reserved_positions {
        let month_key = store.activity[position].month_key.clone();
        let estimated_cost = store.activity[position].reserved_cost_microusd;
        store.record_completed_cost(&month_key, estimated_cost)?;
        let entry = &mut store.activity[position];
        entry.estimated_cost_microusd = estimated_cost;
        entry.status = ProviderSpendStatus::Uncertain;
        entry.occurred_at_ms = reconciled_at_ms;
    }
    store.prune_activity();
    store.increment_revision()?;
    write_store(data_dir, &store)
}

fn load_store(data_dir: &Path) -> Result<StoredProviderSpend, ProviderSpendError> {
    prepare_data_dir(data_dir).map_err(|_| ProviderSpendError::invalid())?;
    let path = spend_path(data_dir);
    match fs::symlink_metadata(&path) {
        Ok(_) => secure_regular_file(&path)?,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(StoredProviderSpend::default());
        }
        Err(_) => return Err(ProviderSpendError::invalid()),
    }
    let file = File::open(path).map_err(|_| ProviderSpendError::invalid())?;
    let mut bytes = Vec::new();
    file.take(SPEND_FILE_LIMIT_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| ProviderSpendError::invalid())?;
    if bytes.is_empty() || bytes.len() as u64 > SPEND_FILE_LIMIT_BYTES {
        return Err(ProviderSpendError::invalid());
    }
    serde_json::from_slice::<StoredProviderSpend>(&bytes)
        .map_err(|_| ProviderSpendError::invalid())?
        .validated()
}

fn write_store(data_dir: &Path, store: &StoredProviderSpend) -> Result<(), ProviderSpendError> {
    prepare_data_dir(data_dir).map_err(|_| ProviderSpendError::invalid())?;
    let store = store.clone().validated()?;
    let destination = spend_path(data_dir);
    match fs::symlink_metadata(&destination) {
        Ok(_) => secure_regular_file(&destination)?,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(_) => return Err(ProviderSpendError::invalid()),
    }
    let bytes = serde_json::to_vec(&store).map_err(|_| ProviderSpendError::invalid())?;
    if bytes.is_empty() || bytes.len() as u64 > SPEND_FILE_LIMIT_BYTES {
        return Err(ProviderSpendError::invalid());
    }
    let mut temporary =
        NamedTempFile::new_in(data_dir).map_err(|_| ProviderSpendError::invalid())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        temporary
            .as_file()
            .set_permissions(fs::Permissions::from_mode(0o600))
            .map_err(|_| ProviderSpendError::invalid())?;
    }
    temporary
        .write_all(&bytes)
        .and_then(|_| temporary.flush())
        .map_err(|_| ProviderSpendError::invalid())?;
    temporary
        .as_file()
        .sync_all()
        .map_err(|_| ProviderSpendError::invalid())?;
    temporary
        .persist(destination)
        .map_err(|_| ProviderSpendError::invalid())?;
    #[cfg(unix)]
    {
        File::open(data_dir)
            .and_then(|directory| directory.sync_all())
            .map_err(|_| ProviderSpendError::invalid())?;
    }
    Ok(())
}

fn calculated_cost(
    input_tokens: u64,
    output_tokens: u64,
    rate: &ProviderModelRate,
) -> Result<u64, ProviderSpendError> {
    let numerator = u128::from(input_tokens)
        .checked_mul(u128::from(rate.input_microusd_per_million_tokens))
        .and_then(|input| {
            u128::from(output_tokens)
                .checked_mul(u128::from(rate.output_microusd_per_million_tokens))
                .and_then(|output| input.checked_add(output))
        })
        .ok_or_else(ProviderSpendError::invalid)?;
    let rounded_up = numerator
        .checked_add(999_999)
        .map(|value| value / 1_000_000)
        .ok_or_else(ProviderSpendError::invalid)?;
    u64::try_from(rounded_up)
        .ok()
        .filter(|value| *value <= JAVASCRIPT_MAX_SAFE_INTEGER)
        .ok_or_else(ProviderSpendError::invalid)
}

#[allow(clippy::too_many_arguments)]
pub(crate) fn reserve_provider_spend(
    data_dir: &Path,
    operation_id: &str,
    kind: ProviderSpendKind,
    stage: ProviderSpendStage,
    provider_kind: LocalProviderKind,
    model_id: &str,
    maximum_input_tokens: u64,
    maximum_output_tokens: u64,
) -> Result<Option<ProviderSpendReservation>, ProviderSpendError> {
    if kind == ProviderSpendKind::LegacyMatching {
        return Err(ProviderSpendError::invalid());
    }
    if provider_kind == LocalProviderKind::OpenaiCompatibleLocal {
        return Ok(None);
    }
    if !valid_uuid(operation_id)
        || !valid_model_id(model_id)
        || maximum_input_tokens == 0
        || maximum_input_tokens > JAVASCRIPT_MAX_SAFE_INTEGER
        || maximum_output_tokens > JAVASCRIPT_MAX_SAFE_INTEGER
    {
        return Err(ProviderSpendError::invalid());
    }
    let mut store = load_store(data_dir)?;
    let occurred_at_ms = current_timestamp_ms()?;
    let month_key = utc_month_key(occurred_at_ms)?;
    if store.activity.iter().any(|entry| {
        entry.operation_id == operation_id
            && entry.kind == kind
            && entry.stage == stage
            && entry.provider_kind == provider_kind
            && entry.model_id == model_id
    }) {
        return Err(ProviderSpendError::new(
            "provider_spend_duplicate_request",
            "CVGnome has already recorded this cloud request.",
        ));
    }
    let rate = store
        .model_rates
        .iter()
        .find(|rate| rate.provider_kind == provider_kind && rate.model_id == model_id)
        .cloned();
    if store.enabled && store.block_unpriced && rate.is_none() {
        return Err(ProviderSpendError::new(
            "provider_spend_rate_required",
            "Add current input and output rates for this model before sending a cloud request.",
        ));
    }
    let reserved_cost = rate
        .as_ref()
        .map(|rate| calculated_cost(maximum_input_tokens, maximum_output_tokens, rate))
        .transpose()?;
    if store.enabled {
        if let (Some(limit), Some(cost)) = (store.per_request_limit_microusd, reserved_cost)
            && cost > limit
        {
            return Err(ProviderSpendError::new(
                "provider_spend_request_limit",
                "This cloud request could exceed the per-request spend limit.",
            ));
        }
        if let (Some(limit), Some(cost)) = (store.monthly_limit_microusd, reserved_cost) {
            let summary = store.summary_for_month(&month_key)?;
            let projected = summary
                .estimated_cost_microusd
                .checked_add(summary.reserved_cost_microusd)
                .and_then(|value| value.checked_add(cost))
                .ok_or_else(ProviderSpendError::invalid)?;
            if projected > limit {
                return Err(ProviderSpendError::new(
                    "provider_spend_monthly_limit",
                    "This cloud request could exceed the local monthly spend limit.",
                ));
            }
        }
    }
    if store.activity.len() >= MAX_ACTIVITY
        && store
            .activity
            .iter()
            .all(|entry| entry.status == ProviderSpendStatus::Reserved)
    {
        return Err(ProviderSpendError::new(
            "provider_spend_activity_full",
            "CVGnome cannot safely reserve another cloud request.",
        ));
    }
    let id = Uuid::new_v4().to_string();
    store.activity.push(ProviderSpendActivity {
        id: id.clone(),
        operation_id: operation_id.to_string(),
        workspace_id: None,
        candidate_id: None,
        actor_id: None,
        kind,
        stage,
        provider_kind,
        model_id: model_id.to_string(),
        month_key,
        maximum_input_tokens,
        maximum_output_tokens,
        input_tokens: None,
        output_tokens: None,
        estimated_cost_microusd: None,
        reserved_cost_microusd: reserved_cost,
        occurred_at_ms,
        status: ProviderSpendStatus::Reserved,
    });
    store.prune_activity();
    store.increment_revision()?;
    write_store(data_dir, &store)?;
    Ok(Some(ProviderSpendReservation {
        id,
        model_rate: rate,
        maximum_input_tokens,
        maximum_output_tokens,
    }))
}

pub(crate) fn complete_provider_spend(
    data_dir: &Path,
    reservation: Option<&ProviderSpendReservation>,
    usage: Option<ProviderTokenUsage>,
) -> Result<(), ProviderSpendError> {
    let Some(reservation) = reservation else {
        return Ok(());
    };
    let usage = usage.ok_or_else(|| {
        ProviderSpendError::new(
            "provider_spend_usage_missing",
            "The cloud provider did not return the usage receipt CVGnome requires.",
        )
    })?;
    let exceeded_reservation = usage.input_tokens > reservation.maximum_input_tokens
        || usage.output_tokens > reservation.maximum_output_tokens;
    let mut store = load_store(data_dir)?;
    let position = store
        .activity
        .iter()
        .position(|entry| entry.id == reservation.id)
        .ok_or_else(ProviderSpendError::invalid)?;
    if store.activity[position].status != ProviderSpendStatus::Reserved {
        return Err(ProviderSpendError::invalid());
    }
    let estimated_cost = reservation
        .model_rate
        .as_ref()
        .map(|rate| calculated_cost(usage.input_tokens, usage.output_tokens, rate))
        .transpose()?;
    let month_key = store.activity[position].month_key.clone();
    store.record_completed_cost(&month_key, estimated_cost)?;
    let entry = &mut store.activity[position];
    entry.input_tokens = Some(usage.input_tokens);
    entry.output_tokens = Some(usage.output_tokens);
    entry.estimated_cost_microusd = estimated_cost;
    entry.status = ProviderSpendStatus::Completed;
    entry.occurred_at_ms = current_timestamp_ms()?;
    store.increment_revision()?;
    write_store(data_dir, &store)?;
    if exceeded_reservation {
        return Err(ProviderSpendError::new(
            "provider_spend_usage_exceeded_reservation",
            "The cloud provider reported usage beyond CVGnome's bounded reservation.",
        ));
    }
    Ok(())
}

pub(crate) fn cancel_provider_spend(
    data_dir: &Path,
    reservation: Option<&ProviderSpendReservation>,
    uncertain: bool,
) -> Result<(), ProviderSpendError> {
    let Some(reservation) = reservation else {
        return Ok(());
    };
    let mut store = load_store(data_dir)?;
    let position = store
        .activity
        .iter()
        .position(|entry| entry.id == reservation.id)
        .ok_or_else(ProviderSpendError::invalid)?;
    if store.activity[position].status != ProviderSpendStatus::Reserved {
        return Err(ProviderSpendError::invalid());
    }
    if uncertain {
        let estimated_cost = store.activity[position].reserved_cost_microusd;
        let month_key = store.activity[position].month_key.clone();
        store.record_completed_cost(&month_key, estimated_cost)?;
        let entry = &mut store.activity[position];
        entry.estimated_cost_microusd = estimated_cost;
        entry.status = ProviderSpendStatus::Uncertain;
        entry.occurred_at_ms = current_timestamp_ms()?;
    } else {
        store.activity.remove(position);
    }
    store.increment_revision()?;
    write_store(data_dir, &store)
}

fn app_data_dir(app: &tauri::AppHandle) -> Result<PathBuf, ProviderSpendError> {
    use tauri::Manager;
    app.path()
        .app_data_dir()
        .map_err(|_| ProviderSpendError::invalid())
}

#[tauri::command]
pub(crate) async fn get_provider_spend_control(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, LocalProviderRuntime>,
) -> Result<ProviderSpendControlView, String> {
    let _guard = runtime.lock_operations().await;
    let data_dir = app_data_dir(&app).map_err(ProviderSpendError::command_message)?;
    reconcile_interrupted_provider_spend(&data_dir).map_err(ProviderSpendError::command_message)?;
    load_provider_spend(&data_dir).map_err(ProviderSpendError::command_message)
}

#[tauri::command(rename_all = "camelCase")]
#[allow(clippy::too_many_arguments)]
pub(crate) async fn save_provider_spend_control(
    app: tauri::AppHandle,
    runtime: tauri::State<'_, LocalProviderRuntime>,
    expected_revision: u64,
    enabled: bool,
    monthly_limit_microusd: Option<u64>,
    per_request_limit_microusd: Option<u64>,
    block_unpriced: bool,
    model_rates: Vec<ProviderModelRate>,
) -> Result<ProviderSpendControlView, String> {
    let _guard = runtime.lock_operations().await;
    let data_dir = app_data_dir(&app).map_err(ProviderSpendError::command_message)?;
    reconcile_interrupted_provider_spend(&data_dir).map_err(ProviderSpendError::command_message)?;
    let mut store = load_store(&data_dir).map_err(ProviderSpendError::command_message)?;
    if expected_revision != store.revision {
        return Err(ProviderSpendError::new(
            "provider_spend_settings_conflict",
            "The provider spend controls changed. Reload them before saving.",
        )
        .command_message());
    }
    let unchanged = store.enabled == enabled
        && store.monthly_limit_microusd == monthly_limit_microusd
        && store.per_request_limit_microusd == per_request_limit_microusd
        && store.block_unpriced == block_unpriced
        && store.model_rates == model_rates;
    if !unchanged {
        store.enabled = enabled;
        store.monthly_limit_microusd = monthly_limit_microusd;
        store.per_request_limit_microusd = per_request_limit_microusd;
        store.block_unpriced = block_unpriced;
        store.model_rates = model_rates;
        store = store
            .validated()
            .map_err(ProviderSpendError::command_message)?;
        store
            .increment_revision()
            .map_err(ProviderSpendError::command_message)?;
        write_store(&data_dir, &store).map_err(ProviderSpendError::command_message)?;
    }
    store.view().map_err(ProviderSpendError::command_message)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rate(model_id: &str, input: u64, output: u64) -> ProviderModelRate {
        ProviderModelRate {
            provider_kind: LocalProviderKind::Openai,
            model_id: model_id.to_string(),
            input_microusd_per_million_tokens: input,
            output_microusd_per_million_tokens: output,
        }
    }

    fn historical_attributed_ledger() -> serde_json::Value {
        let timestamp = current_timestamp_ms().unwrap();
        let month = utc_month_key(timestamp).unwrap();
        let mut fixture = serde_json::to_value(StoredProviderSpend {
            enabled: true,
            monthly_limit_microusd: Some(150),
            model_rates: vec![rate("historical-model", 1_000_000, 1_000_000)],
            ..StoredProviderSpend::default()
        })
        .unwrap();
        let historical = serde_json::json!({
            "id": Uuid::new_v4().to_string(),
            "operation_id": Uuid::new_v4().to_string(),
            "workspace_id": "11111111-1111-4111-8111-111111111111",
            "candidate_id": "22222222-2222-4222-8222-222222222222",
            "actor_id": "local-owner",
            "kind": "matching",
            "stage": "chat",
            "provider_kind": "openai",
            "model_id": "historical-model",
            "month_key": month,
            "maximum_input_tokens": 100,
            "maximum_output_tokens": 0,
            "input_tokens": 100,
            "output_tokens": 0,
            "estimated_cost_microusd": 100,
            "reserved_cost_microusd": 100,
            "occurred_at_ms": timestamp,
            "status": "completed",
        });
        let mut pending = historical.clone();
        pending["id"] = serde_json::json!(Uuid::new_v4().to_string());
        pending["operation_id"] = serde_json::json!(Uuid::new_v4().to_string());
        pending["kind"] = serde_json::json!("tailoring");
        pending["status"] = serde_json::json!("reserved");
        pending["input_tokens"] = serde_json::Value::Null;
        pending["output_tokens"] = serde_json::Value::Null;
        pending["estimated_cost_microusd"] = serde_json::Value::Null;
        pending["reserved_cost_microusd"] = serde_json::json!(25);
        let mut personal = historical.clone();
        personal["id"] = serde_json::json!(Uuid::new_v4().to_string());
        personal["operation_id"] = serde_json::json!(Uuid::new_v4().to_string());
        personal["kind"] = serde_json::json!("test");
        personal["input_tokens"] = serde_json::json!(10);
        personal["estimated_cost_microusd"] = serde_json::json!(10);
        for field in ["workspace_id", "candidate_id", "actor_id"] {
            personal.as_object_mut().unwrap().remove(field);
        }
        fixture["month_rollups"] = serde_json::json!([{
            "month_key": month,
            "estimated_cost_microusd": 110,
            "request_count": 2,
            "unpriced_request_count": 0,
        }]);
        fixture["activity"] = serde_json::json!([historical, pending, personal]);
        fixture
    }

    #[test]
    fn historical_attribution_survives_saves_and_counts_without_exposing_activity() {
        let directory = tempfile::tempdir().unwrap();
        let fixture = historical_attributed_ledger();
        fs::write(
            spend_path(directory.path()),
            serde_json::to_vec(&fixture).unwrap(),
        )
        .unwrap();
        let mut store = load_store(directory.path()).unwrap();
        assert_eq!(store.activity[0].kind, ProviderSpendKind::LegacyMatching);
        let view = store.view().unwrap();
        assert_eq!(view.activity.len(), 1);
        assert_eq!(view.activity[0].kind, ProviderSpendKind::Test);
        assert_eq!(view.summary.estimated_cost_microusd, 110);
        assert_eq!(view.summary.reserved_cost_microusd, 25);
        assert_eq!(view.summary.request_count, 3);

        store.increment_revision().unwrap();
        write_store(directory.path(), &store).unwrap();
        let saved = serde_json::to_value(load_store(directory.path()).unwrap()).unwrap();
        assert_eq!(saved["activity"], fixture["activity"]);
        assert_eq!(saved["month_rollups"], fixture["month_rollups"]);

        reconcile_interrupted_provider_spend(directory.path()).unwrap();
        let reconciled = load_store(directory.path()).unwrap();
        assert_eq!(
            reconciled.activity[1].status,
            ProviderSpendStatus::Uncertain
        );
        assert_eq!(
            reconciled.activity[1].workspace_id,
            store.activity[1].workspace_id
        );
        assert_eq!(
            reconciled.activity[1].candidate_id,
            store.activity[1].candidate_id
        );
        assert_eq!(reconciled.activity[1].actor_id, store.activity[1].actor_id);
        assert_eq!(reconciled.view().unwrap().activity.len(), 1);
        assert_eq!(
            reconciled
                .current_summary()
                .unwrap()
                .estimated_cost_microusd,
            135
        );
        assert_eq!(
            reserve_provider_spend(
                directory.path(),
                &Uuid::new_v4().to_string(),
                ProviderSpendKind::Tailoring,
                ProviderSpendStage::Chat,
                LocalProviderKind::Openai,
                "historical-model",
                16,
                0,
            )
            .unwrap_err()
            .code,
            "provider_spend_monthly_limit"
        );
        reserve_provider_spend(
            directory.path(),
            &Uuid::new_v4().to_string(),
            ProviderSpendKind::Tailoring,
            ProviderSpendStage::Chat,
            LocalProviderKind::Openai,
            "historical-model",
            10,
            0,
        )
        .unwrap();
        let updated = load_store(directory.path()).unwrap();
        let new_entry = updated.activity.last().unwrap();
        assert_eq!(new_entry.workspace_id, None);
        assert_eq!(new_entry.candidate_id, None);
        assert_eq!(new_entry.actor_id, None);
        assert_eq!(
            updated.current_summary().unwrap().reserved_cost_microusd,
            10
        );
        assert_eq!(updated.current_summary().unwrap().request_count, 4);
        assert_eq!(updated.view().unwrap().activity.len(), 2);
        assert_eq!(updated.activity[0], store.activity[0]);
    }

    #[test]
    fn historical_attribution_remains_strict_and_matching_cannot_be_reserved() {
        for (field, value) in [
            ("workspace_id", serde_json::Value::Null),
            ("candidate_id", serde_json::json!("../candidate")),
            ("actor_id", serde_json::json!("other-actor")),
        ] {
            let mut fixture = historical_attributed_ledger();
            fixture["activity"][0][field] = value;
            let store: StoredProviderSpend = serde_json::from_value(fixture).unwrap();
            assert!(store.validated().is_err());
        }
        let mut unscoped = historical_attributed_ledger();
        for field in ["workspace_id", "candidate_id", "actor_id"] {
            unscoped["activity"][0]
                .as_object_mut()
                .unwrap()
                .remove(field);
        }
        let store: StoredProviderSpend = serde_json::from_value(unscoped).unwrap();
        let store = store.validated().unwrap();
        assert_eq!(store.view().unwrap().activity.len(), 1);
        assert_eq!(
            store.current_summary().unwrap().estimated_cost_microusd,
            110
        );
        assert_eq!(
            serde_json::to_value(&store).unwrap()["activity"][0]["kind"],
            "matching"
        );
        let directory = tempfile::tempdir().unwrap();
        for provider_kind in [
            LocalProviderKind::Openai,
            LocalProviderKind::OpenaiCompatibleLocal,
        ] {
            assert!(
                reserve_provider_spend(
                    directory.path(),
                    &Uuid::new_v4().to_string(),
                    ProviderSpendKind::LegacyMatching,
                    ProviderSpendStage::Chat,
                    provider_kind,
                    "historical-model",
                    10,
                    0,
                )
                .is_err()
            );
        }
        assert!(!spend_path(directory.path()).exists());
    }

    #[test]
    fn cost_calculation_is_checked_and_rounds_up() {
        let price = rate("gpt-test", 150_000, 600_000);
        assert_eq!(calculated_cost(1, 0, &price).unwrap(), 1);
        assert_eq!(
            calculated_cost(1_000_000, 1_000_000, &price).unwrap(),
            750_000
        );
    }

    #[test]
    fn month_keys_are_derived_in_utc() {
        assert_eq!(utc_month_key(1).unwrap(), "1970-01");
        assert_eq!(utc_month_key(951_782_400_000).unwrap(), "2000-02");
        assert_eq!(utc_month_key(1_704_067_199_999).unwrap(), "2023-12");
        assert_eq!(utc_month_key(1_704_067_200_000).unwrap(), "2024-01");
    }

    #[test]
    fn renderer_view_uses_the_exact_snake_case_contract() {
        fn sorted_keys(value: &serde_json::Value) -> Vec<&str> {
            let mut keys = value
                .as_object()
                .unwrap()
                .keys()
                .map(String::as_str)
                .collect::<Vec<_>>();
            keys.sort_unstable();
            keys
        }

        let directory = tempfile::tempdir().unwrap();
        let mut store = StoredProviderSpend {
            model_rates: vec![rate("gpt-test", 150_000, 600_000)],
            ..StoredProviderSpend::default()
        };
        store.increment_revision().unwrap();
        write_store(directory.path(), &store).unwrap();
        reserve_provider_spend(
            directory.path(),
            &Uuid::new_v4().to_string(),
            ProviderSpendKind::Test,
            ProviderSpendStage::Chat,
            LocalProviderKind::Openai,
            "gpt-test",
            100,
            50,
        )
        .unwrap();

        let view = serde_json::to_value(load_provider_spend(directory.path()).unwrap()).unwrap();
        assert_eq!(
            sorted_keys(&view),
            [
                "activity",
                "block_unpriced",
                "enabled",
                "model_rates",
                "monthly_limit_microusd",
                "per_request_limit_microusd",
                "revision",
                "schema_version",
                "summary",
            ]
        );
        assert_eq!(
            sorted_keys(&view["model_rates"][0]),
            [
                "input_microusd_per_million_tokens",
                "model_id",
                "output_microusd_per_million_tokens",
                "provider_kind",
            ]
        );
        assert_eq!(
            sorted_keys(&view["summary"]),
            [
                "estimated_cost_microusd",
                "month_key",
                "request_count",
                "reserved_cost_microusd",
                "timezone",
                "unpriced_request_count",
            ]
        );
        assert_eq!(
            sorted_keys(&view["activity"][0]),
            [
                "estimated_cost_microusd",
                "id",
                "input_tokens",
                "kind",
                "maximum_input_tokens",
                "maximum_output_tokens",
                "model_id",
                "month_key",
                "occurred_at_ms",
                "operation_id",
                "output_tokens",
                "provider_kind",
                "reserved_cost_microusd",
                "stage",
                "status",
            ]
        );
        assert_eq!(view["activity"][0]["provider_kind"], "openai");
        assert_eq!(view["activity"][0]["stage"], "chat");
        assert_eq!(view["activity"][0]["status"], "reserved");
    }

    #[test]
    fn default_is_monitoring_only_and_blocks_unpriced_if_enabled() {
        let directory = tempfile::tempdir().unwrap();
        let operation = Uuid::new_v4().to_string();
        let reservation = reserve_provider_spend(
            directory.path(),
            &operation,
            ProviderSpendKind::Test,
            ProviderSpendStage::Chat,
            LocalProviderKind::Openai,
            "gpt-test",
            100,
            50,
        )
        .unwrap();
        assert!(reservation.is_some());
        complete_provider_spend(
            directory.path(),
            reservation.as_ref(),
            Some(ProviderTokenUsage {
                input_tokens: 80,
                output_tokens: 40,
            }),
        )
        .unwrap();
        let view = load_provider_spend(directory.path()).unwrap();
        assert_eq!(view.summary.request_count, 1);
        assert_eq!(view.summary.unpriced_request_count, 1);
        assert_eq!(view.activity[0].estimated_cost_microusd, None);

        let mut store = load_store(directory.path()).unwrap();
        store.enabled = true;
        store.increment_revision().unwrap();
        write_store(directory.path(), &store).unwrap();
        assert_eq!(
            reserve_provider_spend(
                directory.path(),
                &Uuid::new_v4().to_string(),
                ProviderSpendKind::Test,
                ProviderSpendStage::Chat,
                LocalProviderKind::Openai,
                "gpt-test",
                100,
                50,
            )
            .unwrap_err()
            .code,
            "provider_spend_rate_required"
        );
    }

    #[test]
    fn reservation_enforces_request_and_month_limits_before_call() {
        let directory = tempfile::tempdir().unwrap();
        let mut store = StoredProviderSpend {
            enabled: true,
            per_request_limit_microusd: Some(100),
            monthly_limit_microusd: Some(150),
            model_rates: vec![rate("gpt-test", 1_000_000, 1_000_000)],
            ..StoredProviderSpend::default()
        };
        store.increment_revision().unwrap();
        write_store(directory.path(), &store).unwrap();
        assert_eq!(
            reserve_provider_spend(
                directory.path(),
                &Uuid::new_v4().to_string(),
                ProviderSpendKind::Tailoring,
                ProviderSpendStage::Chat,
                LocalProviderKind::Openai,
                "gpt-test",
                101,
                0,
            )
            .unwrap_err()
            .code,
            "provider_spend_request_limit"
        );
        let first = reserve_provider_spend(
            directory.path(),
            &Uuid::new_v4().to_string(),
            ProviderSpendKind::Tailoring,
            ProviderSpendStage::Chat,
            LocalProviderKind::Openai,
            "gpt-test",
            75,
            0,
        )
        .unwrap();
        assert_eq!(
            reserve_provider_spend(
                directory.path(),
                &Uuid::new_v4().to_string(),
                ProviderSpendKind::Tailoring,
                ProviderSpendStage::Chat,
                LocalProviderKind::Openai,
                "gpt-test",
                76,
                0,
            )
            .unwrap_err()
            .code,
            "provider_spend_monthly_limit"
        );
        complete_provider_spend(
            directory.path(),
            first.as_ref(),
            Some(ProviderTokenUsage {
                input_tokens: 70,
                output_tokens: 0,
            }),
        )
        .unwrap();
        assert_eq!(
            load_provider_spend(directory.path())
                .unwrap()
                .summary
                .estimated_cost_microusd,
            70
        );
    }

    #[test]
    fn guard_requires_both_selected_model_rates_before_first_call() {
        let directory = tempfile::tempdir().unwrap();
        let mut store = StoredProviderSpend {
            enabled: true,
            model_rates: vec![rate("embed-test", 20_000, 0)],
            ..StoredProviderSpend::default()
        };
        store.increment_revision().unwrap();
        write_store(directory.path(), &store).unwrap();
        assert_eq!(
            require_priced_models_if_guarded(
                directory.path(),
                LocalProviderKind::Openai,
                &["embed-test", "chat-test"],
            )
            .unwrap_err()
            .code,
            "provider_spend_rate_required"
        );
        store.model_rates.push(rate("chat-test", 100_000, 500_000));
        store.increment_revision().unwrap();
        write_store(directory.path(), &store).unwrap();
        require_priced_models_if_guarded(
            directory.path(),
            LocalProviderKind::Openai,
            &["embed-test", "chat-test"],
        )
        .unwrap();
    }

    #[test]
    fn uncertain_outcomes_count_the_reserved_upper_bound() {
        let directory = tempfile::tempdir().unwrap();
        let mut store = StoredProviderSpend {
            model_rates: vec![rate("gpt-test", 1_000_000, 1_000_000)],
            ..StoredProviderSpend::default()
        };
        store.increment_revision().unwrap();
        write_store(directory.path(), &store).unwrap();
        let reservation = reserve_provider_spend(
            directory.path(),
            &Uuid::new_v4().to_string(),
            ProviderSpendKind::Test,
            ProviderSpendStage::Chat,
            LocalProviderKind::Openai,
            "gpt-test",
            75,
            25,
        )
        .unwrap();
        cancel_provider_spend(directory.path(), reservation.as_ref(), true).unwrap();
        let view = load_provider_spend(directory.path()).unwrap();
        assert_eq!(view.summary.estimated_cost_microusd, 100);
        assert_eq!(view.summary.reserved_cost_microusd, 0);
        assert_eq!(view.summary.request_count, 1);
        assert_eq!(view.activity[0].status, ProviderSpendStatus::Uncertain);
        assert_eq!(view.activity[0].input_tokens, None);
    }

    #[test]
    fn usage_beyond_a_reservation_is_recorded_before_failing_closed() {
        let directory = tempfile::tempdir().unwrap();
        let mut store = StoredProviderSpend {
            model_rates: vec![rate("embed-test", 1_000_000, 0)],
            ..StoredProviderSpend::default()
        };
        store.increment_revision().unwrap();
        write_store(directory.path(), &store).unwrap();
        let reservation = reserve_provider_spend(
            directory.path(),
            &Uuid::new_v4().to_string(),
            ProviderSpendKind::Tailoring,
            ProviderSpendStage::Embedding,
            LocalProviderKind::Openai,
            "embed-test",
            10,
            0,
        )
        .unwrap();
        assert_eq!(
            complete_provider_spend(
                directory.path(),
                reservation.as_ref(),
                Some(ProviderTokenUsage {
                    input_tokens: 11,
                    output_tokens: 0,
                }),
            )
            .unwrap_err()
            .code,
            "provider_spend_usage_exceeded_reservation"
        );
        let view = load_provider_spend(directory.path()).unwrap();
        assert_eq!(view.summary.estimated_cost_microusd, 11);
        assert_eq!(view.activity[0].status, ProviderSpendStatus::Completed);
        assert_eq!(view.activity[0].input_tokens, Some(11));
    }

    #[test]
    fn interrupted_and_missing_usage_reservations_reconcile_as_uncertain() {
        let directory = tempfile::tempdir().unwrap();
        let mut store = StoredProviderSpend {
            model_rates: vec![rate("gpt-test", 1_000_000, 1_000_000)],
            ..StoredProviderSpend::default()
        };
        store.increment_revision().unwrap();
        write_store(directory.path(), &store).unwrap();

        let interrupted = reserve_provider_spend(
            directory.path(),
            &Uuid::new_v4().to_string(),
            ProviderSpendKind::Tailoring,
            ProviderSpendStage::Chat,
            LocalProviderKind::Openai,
            "gpt-test",
            40,
            10,
        )
        .unwrap();
        assert!(interrupted.is_some());
        reconcile_interrupted_provider_spend(directory.path()).unwrap();

        let missing_usage = reserve_provider_spend(
            directory.path(),
            &Uuid::new_v4().to_string(),
            ProviderSpendKind::Test,
            ProviderSpendStage::Chat,
            LocalProviderKind::Openai,
            "gpt-test",
            20,
            5,
        )
        .unwrap();
        assert_eq!(
            complete_provider_spend(directory.path(), missing_usage.as_ref(), None)
                .unwrap_err()
                .code,
            "provider_spend_usage_missing"
        );
        reconcile_interrupted_provider_spend(directory.path()).unwrap();

        let view = load_provider_spend(directory.path()).unwrap();
        assert_eq!(view.summary.estimated_cost_microusd, 75);
        assert_eq!(view.summary.reserved_cost_microusd, 0);
        assert_eq!(view.summary.request_count, 2);
        assert!(
            view.activity
                .iter()
                .all(|entry| entry.status == ProviderSpendStatus::Uncertain)
        );
    }

    #[test]
    fn pruning_recent_detail_never_reduces_month_rollup() {
        let directory = tempfile::tempdir().unwrap();
        let mut store = StoredProviderSpend {
            model_rates: vec![rate("embed-test", 1_000_000, 0)],
            ..StoredProviderSpend::default()
        };
        store.increment_revision().unwrap();
        write_store(directory.path(), &store).unwrap();
        for _ in 0..=MAX_ACTIVITY {
            let reservation = reserve_provider_spend(
                directory.path(),
                &Uuid::new_v4().to_string(),
                ProviderSpendKind::Tailoring,
                ProviderSpendStage::Embedding,
                LocalProviderKind::Openai,
                "embed-test",
                1,
                0,
            )
            .unwrap();
            complete_provider_spend(
                directory.path(),
                reservation.as_ref(),
                Some(ProviderTokenUsage {
                    input_tokens: 1,
                    output_tokens: 0,
                }),
            )
            .unwrap();
        }
        let view = load_provider_spend(directory.path()).unwrap();
        assert_eq!(view.activity.len(), MAX_ACTIVITY);
        assert_eq!(view.summary.request_count, MAX_ACTIVITY as u64 + 1);
        assert_eq!(
            view.summary.estimated_cost_microusd,
            MAX_ACTIVITY as u64 + 1
        );
    }

    #[test]
    fn local_provider_never_creates_a_metered_entry() {
        let directory = tempfile::tempdir().unwrap();
        let reservation = reserve_provider_spend(
            directory.path(),
            &Uuid::new_v4().to_string(),
            ProviderSpendKind::Tailoring,
            ProviderSpendStage::Embedding,
            LocalProviderKind::OpenaiCompatibleLocal,
            "local/embed",
            100,
            0,
        )
        .unwrap();
        assert!(reservation.is_none());
        assert!(!spend_path(directory.path()).exists());
    }

    #[test]
    fn operation_stage_is_idempotency_bound() {
        let directory = tempfile::tempdir().unwrap();
        let operation = Uuid::new_v4().to_string();
        let first = reserve_provider_spend(
            directory.path(),
            &operation,
            ProviderSpendKind::Tailoring,
            ProviderSpendStage::Embedding,
            LocalProviderKind::Openai,
            "embed-test",
            100,
            0,
        )
        .unwrap();
        assert!(first.is_some());
        assert_eq!(
            reserve_provider_spend(
                directory.path(),
                &operation,
                ProviderSpendKind::Tailoring,
                ProviderSpendStage::Embedding,
                LocalProviderKind::Openai,
                "embed-test",
                100,
                0,
            )
            .unwrap_err()
            .code,
            "provider_spend_duplicate_request"
        );
        assert!(
            reserve_provider_spend(
                directory.path(),
                &operation,
                ProviderSpendKind::Tailoring,
                ProviderSpendStage::Chat,
                LocalProviderKind::Openai,
                "chat-test",
                100,
                100,
            )
            .is_ok()
        );
    }

    #[test]
    fn activity_file_is_owner_only_strict_and_symlink_safe() {
        let directory = tempfile::tempdir().unwrap();
        let store = StoredProviderSpend::default();
        write_store(directory.path(), &store).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                fs::metadata(spend_path(directory.path()))
                    .unwrap()
                    .permissions()
                    .mode()
                    & 0o777,
                0o600
            );
        }
        let mut invalid = serde_json::to_value(&store).unwrap();
        invalid
            .as_object_mut()
            .unwrap()
            .insert("unknown".to_string(), serde_json::Value::Bool(true));
        fs::write(
            spend_path(directory.path()),
            serde_json::to_vec(&invalid).unwrap(),
        )
        .unwrap();
        assert!(load_store(directory.path()).is_err());
    }
}
