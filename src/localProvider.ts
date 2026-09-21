// SPDX-License-Identifier: MPL-2.0
export type ProviderModelCapability = "chat" | "embedding";

export type ProviderModelCapabilitySource = "provider" | "cvgnome_catalog" | "unknown";

export type ProviderKind = "openai_compatible_local" | "openai";

export type ProviderCredentialState = "not_required" | "missing" | "stored" | "unavailable";

export type ProviderSettings = {
  schema_version: number;
  revision: number;
  configuration_revision: number;
  provider_kind: ProviderKind;
  base_url: string;
  chat_model_id: string | null;
  embedding_model_id: string | null;
  verified_revision: number | null;
  verified_at_ms: number | null;
  embedding_dimension: number | null;
  credential_state: ProviderCredentialState;
};

export type LocalProviderSettings = ProviderSettings;

export type ProviderModel = {
  id: string;
  display_name: string;
  capabilities: ProviderModelCapability[];
  capability_source: ProviderModelCapabilitySource;
  loaded: boolean | null;
};

export type ProviderModelDiscovery = {
  provider_kind: ProviderKind;
  models: ProviderModel[];
  truncated: boolean;
};

export type ProviderPriceCard = {
  provider_kind: "openai";
  model_id: string;
  input_microusd_per_million_tokens: number;
  output_microusd_per_million_tokens: number;
};

export type ProviderCostActivity = {
  id: string;
  operation_id: string;
  kind: "test" | "tailoring";
  stage: "embedding" | "chat";
  provider_kind: "openai";
  model_id: string;
  month_key: string;
  maximum_input_tokens: number;
  maximum_output_tokens: number;
  input_tokens: number | null;
  output_tokens: number | null;
  estimated_cost_microusd: number | null;
  reserved_cost_microusd: number | null;
  occurred_at_ms: number;
  status: "completed" | "reserved" | "uncertain";
};

export type ProviderCostControls = {
  schema_version: number;
  revision: number;
  enabled: boolean;
  monthly_limit_microusd: number | null;
  per_request_limit_microusd: number | null;
  block_unpriced: boolean;
  model_rates: ProviderPriceCard[];
  summary: {
    month_key: string;
    timezone: "UTC";
    estimated_cost_microusd: number;
    reserved_cost_microusd: number;
    request_count: number;
    unpriced_request_count: number;
  };
  activity: ProviderCostActivity[];
};

export type TailoredResumeBasics = {
  name?: string;
  label?: string;
  summary?: string;
  email?: string;
  phone?: string;
  url?: string;
};

export type TailoredResumeWork = {
  name?: string;
  position?: string;
  location?: string;
  startDate?: string;
  endDate?: string;
  summary?: string;
  highlights?: string[];
};

export type TailoredResume = {
  basics?: TailoredResumeBasics;
  work?: TailoredResumeWork[];
  [key: string]: unknown;
};

export type TailoredResumeDraft = {
  id: string;
  attempt_id: string;
  created_at_ms: number;
  opportunity: {
    id: string;
    title: string;
    company: string;
  };
  profile_version: {
    id: string;
    version_number: number;
    checksum_sha256: string;
  };
  snapshot: {
    id: string;
    checksum_sha256: string;
  };
  models: {
    provider_kind: ProviderKind;
    chat_model_id: string;
    embedding_model_id: string;
  };
  input_fingerprint: string;
  selected_evidence: Array<{
    chunk_id: string;
    kind: string;
    path: string;
    content: string;
  }>;
  quality_issue_codes: string[];
  resume: TailoredResume;
};

export type TailoredResumeRevision = {
  id: string;
  draft_id: string;
  parent_revision_id: string | null;
  revision_number: number;
  author_kind: "user";
  created_at_ms: number;
  parent_resume_checksum_sha256: string;
  resume_checksum_sha256: string;
  changed_fields: string[];
  resume: TailoredResume;
};

export type TailoredResumeRevisionSummary = Omit<
  TailoredResumeRevision,
  "changed_fields" | "resume"
> & {
  changed_field_count: number;
};

export type TailoredResumeRevisionList = {
  draft_id: string;
  base_resume_checksum_sha256: string;
  revisions: TailoredResumeRevisionSummary[];
  truncated: boolean;
};

export type TailoredResumeWorkRevisionEdit = {
  source_index: number;
  summary?: string | null;
  highlights?: string[];
};

export type TailoredResumeRevisionEdits = {
  basics: {
    label?: string;
    summary?: string | null;
  };
  work: TailoredResumeWorkRevisionEdit[];
};

export function providerLabel(providerKind: ProviderKind | undefined): string {
  return providerKind === "openai" ? "OpenAI" : "LM Studio";
}

export function isCloudProvider(providerKind: ProviderKind | undefined): boolean {
  return providerKind === "openai";
}

export function isProviderVerified(settings: ProviderSettings | null): boolean {
  return Boolean(
    settings &&
    settings.chat_model_id &&
    settings.embedding_model_id &&
    (settings.provider_kind !== "openai" || settings.credential_state === "stored") &&
    settings.verified_revision === settings.configuration_revision,
  );
}

export const isLocalProviderVerified = isProviderVerified;

export function formatProviderTimestamp(timestamp: number | null): string {
  if (timestamp === null || !Number.isFinite(timestamp)) return "Not tested";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(timestamp));
}

export function safeProviderError(
  caught: unknown,
  providerKind?: ProviderKind,
): string {
  const raw = caught instanceof Error ? caught.message : String(caught);
  const value = raw.trim().toLowerCase();
  const cloud = isCloudProvider(providerKind);
  if (value.includes("provider_credential_invalid")) {
    return "Enter a bounded printable OpenAI API key without spaces or line breaks.";
  }
  if (
    value.includes("credential_missing") ||
    value.includes("api_key_missing") ||
    value.includes("openai_api_key_required")
  ) {
    return "Store an OpenAI API key in the operating system credential store before testing this configuration.";
  }
  if (value.includes("credential_unavailable") || value.includes("keychain") || value.includes("keyring")) {
    return "CVGnome could not access the operating system credential store. The API key was not saved or used.";
  }
  if (value.includes("provider_unavailable") || value.includes("connect")) {
    return cloud
      ? "CVGnome could not reach OpenAI. Check this computer’s connection and try again; no fallback provider was used."
      : "CVGnome could not reach LM Studio on the configured loopback address. Start its local server and try again.";
  }
  if (value.includes("provider_timeout") || value.includes("timed out")) {
    return cloud
      ? "OpenAI did not respond before the request timed out. No automatic retry or fallback was attempted."
      : "The local model operation timed out. LM Studio may still be loading a model; wait a moment and try again.";
  }
  if (
    value.includes("provider_auth_failed") ||
    value.includes("provider_authentication_failed") ||
    value.includes("authentication_required") ||
    value.includes("unauthorized") ||
    value.includes("invalid_api_key")
  ) {
    return cloud
      ? "OpenAI rejected the stored API key. Replace it in Models and test again."
      : "LM Studio requires authentication. Token support is not enabled for the local provider.";
  }
  if (
    value.includes("rate_limit") ||
    value.includes("insufficient_quota") ||
    value.includes("billing") ||
    value.includes("quota")
  ) {
    return "OpenAI declined the request because of an account limit, quota, or billing status. Check the API account before retrying.";
  }
  if (value.includes("provider_spend_rate_required")) {
    return "The local spend guard blocked this OpenAI request because the selected model has no saved price. Add its current price in Models → Spend guard, or turn the guard off.";
  }
  if (value.includes("provider_spend_request_limit")) {
    return "The local spend guard blocked this OpenAI request because its conservative upper-bound estimate exceeds your per-request limit.";
  }
  if (value.includes("provider_spend_monthly_limit")) {
    return "The local spend guard blocked this OpenAI request because it could exceed your remaining monthly limit.";
  }
  if (value.includes("provider_spend_activity_full")) {
    return "The local cost ledger cannot safely reserve another OpenAI request while all retained entries are still unresolved.";
  }
  if (value.includes("provider_spend_usage_missing")) {
    return "OpenAI did not return the token-usage receipt CVGnome requires for local cost tracking. The result was not accepted.";
  }
  if (value.includes("provider_spend_usage_exceeded_reservation")) {
    return "OpenAI reported more token usage than CVGnome’s bounded cost reservation allowed. The result was not accepted; review Spend guard before trying again.";
  }
  if (value.includes("provider_model_unavailable")) {
    return cloud
      ? "The selected OpenAI model is unavailable to this API account. Check both exact model IDs and account access."
      : "A selected LM Studio model is unavailable. Refresh models and check JIT loading settings.";
  }
  if (value.includes("embedding_model_unavailable")) {
    return cloud
      ? "The OpenAI embedding model is unavailable to this API account. Check the exact model ID and account access."
      : "The selected embedding model is unavailable. Refresh models and check LM Studio’s JIT loading settings.";
  }
  if (value.includes("chat_model_unavailable")) {
    return cloud
      ? "The OpenAI chat model is unavailable to this API account. Check the exact model ID and account access."
      : "The selected chat model is unavailable. Refresh models and check LM Studio’s JIT loading settings.";
  }
  if (
    value.includes("provider_embedding_response_invalid") ||
    value.includes("provider_embedding_response_too_large")
  ) {
    return `The selected ${providerLabel(providerKind)} embedding model returned vector data CVGnome could not validate.`;
  }
  if (value.includes("provider_chat_response_too_large")) {
    return `The selected ${providerLabel(providerKind)} chat model returned more structured data than CVGnome allows.`;
  }
  if (value.includes("provider_chat_response_invalid")) {
    return `The selected ${providerLabel(providerKind)} chat model did not return the strict structured result CVGnome requires.`;
  }
  if (value.includes("provider_response_incomplete")) {
    return `${providerLabel(providerKind)} stopped before completing the requested structured result. No result was saved, and CVGnome did not retry automatically.`;
  }
  if (value.includes("provider_response_refused")) {
    return `${providerLabel(providerKind)} declined this structured request. No result was saved, and no fallback provider was used.`;
  }
  if (value.includes("tailoring_draft_rejected")) {
    return `${providerLabel(providerKind)} returned a draft, but CVGnome rejected it because the result could not be safely grounded in your imported evidence. No draft was saved.`;
  }
  if (value.includes("provider_response_invalid") || value.includes("structured")) {
    return `The selected ${providerLabel(providerKind)} chat model did not return the strict structured result CVGnome requires.`;
  }
  if (value.includes("revision") || value.includes("changed")) {
    return "The model configuration changed. Reopen Models and test the current selections.";
  }
  if (value.includes("loopback") || value.includes("base url") || value.includes("base_url")) {
    return cloud
      ? "OpenAI connections use CVGnome’s fixed https://api.openai.com/v1 endpoint."
      : "Use an explicit loopback endpoint such as http://127.0.0.1:1234/v1.";
  }
  return `CVGnome could not complete the ${providerLabel(providerKind)} model operation. No automatic retry or fallback was attempted.`;
}

export function safeProviderDiscoveryError(
  caught: unknown,
  providerKind?: ProviderKind,
): string {
  const raw = caught instanceof Error ? caught.message : String(caught);
  const value = raw.trim().toLowerCase();
  if (
    value.includes("credential_missing") ||
    value.includes("api_key_missing") ||
    value.includes("openai_api_key_required")
  ) {
    return "Store an OpenAI API key in the operating system credential store before refreshing available models. Your current model IDs were not changed.";
  }
  if (value.includes("revision") || value.includes("changed")) {
    return "The model settings changed before discovery completed. Refresh the current provider again; your model IDs were not changed.";
  }
  if (
    value.includes("model_discovery_invalid") ||
    value.includes("model_list_invalid") ||
    value.includes("provider_response_invalid") ||
    value.includes("provider_response_too_large")
  ) {
    return `${providerLabel(providerKind)} returned a model list CVGnome could not safely read. Your current model IDs were not changed.`;
  }
  return `${safeProviderError(caught, providerKind)} Your current model IDs were not changed.`;
}

export const safeLocalProviderError = safeProviderError;
