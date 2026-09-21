// SPDX-License-Identifier: MPL-2.0
import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { useDesktopCloseProtection } from "./DesktopCloseGuard";
import {
  formatProviderTimestamp,
  isCloudProvider,
  isProviderVerified,
  ProviderModel,
  ProviderModelCapability,
  ProviderModelDiscovery,
  ProviderCredentialState,
  ProviderKind,
  ProviderSettings,
  safeProviderDiscoveryError,
  safeProviderError,
} from "./localProvider";
import { ProviderSpendControl } from "./ProviderSpendControl";

type ProviderSettingsPanelProps = {
  onSettingsChange?: (settings: ProviderSettings) => void;
  onDirtyChange?: (dirty: boolean) => void;
  careerWorkspaceHasData?: boolean;
  careerWorkspaceResetDisabled?: boolean;
  onRequestCareerWorkspaceReset?: () => void;
};

const LOCAL_BASE_URL = "http://127.0.0.1:1234/v1";
const OPENAI_BASE_URL = "https://api.openai.com/v1";
const DEFAULT_OPENAI_CHAT_MODEL_ID = "gpt-5.4-mini";
const DEFAULT_OPENAI_EMBEDDING_MODEL_ID = "text-embedding-3-small";
const EXACT_MODEL_ID = "__cvgnome_exact_model_id__";
const DEFAULT_OPENAI_MODEL_SUGGESTIONS: ProviderModel[] = [
  {
    id: DEFAULT_OPENAI_CHAT_MODEL_ID,
    display_name: "Suggested default",
    capabilities: ["chat"],
    capability_source: "cvgnome_catalog",
    loaded: null,
  },
  {
    id: DEFAULT_OPENAI_EMBEDDING_MODEL_ID,
    display_name: "Suggested default",
    capabilities: ["embedding"],
    capability_source: "cvgnome_catalog",
    loaded: null,
  },
];

function modelOptions(models: ProviderModel[], capability: ProviderModelCapability) {
  return models.filter(
    (model) => model.capability_source !== "unknown" && model.capabilities.includes(capability),
  );
}

function modelLabel(model: ProviderModel): string {
  const name = model.display_name && model.display_name !== model.id
    ? `${model.display_name} · ${model.id}`
    : model.id;
  return `${name}${model.loaded === true ? " · loaded" : ""}`;
}

function discoverySummary(discovery: ProviderModelDiscovery): string {
  const total = discovery.models.length;
  const generation = modelOptions(discovery.models, "chat").length;
  const embedding = modelOptions(discovery.models, "embedding").length;
  const providerClassified = discovery.models.filter(
    (model) => model.capability_source === "provider",
  ).length;
  const catalogClassified = discovery.models.filter(
    (model) => model.capability_source === "cvgnome_catalog",
  ).length;
  const unclassified = discovery.models.filter(
    (model) => model.capability_source === "unknown",
  ).length;
  const sources = [
    providerClassified ? `${providerClassified} provider-described` : "",
    catalogClassified ? `${catalogClassified} CVGnome catalog-classified` : "",
    unclassified ? `${unclassified} unclassified` : "",
  ].filter(Boolean).join(", ") || "no classified entries";
  const truncated = discovery.truncated
    ? " The bounded result was truncated, so additional model IDs may exist."
    : " The result was not truncated.";

  if (discovery.provider_kind === "openai") {
    return `OpenAI returned ${total} account-visible model ID${total === 1 ? "" : "s"}: ${generation} generation choice${generation === 1 ? "" : "s"} and ${embedding} embedding choice${embedding === 1 ? "" : "s"}. Sources: ${sources}.${truncated} Discovery sends no career data; the bounded synthetic test remains authoritative.`;
  }
  const loaded = discovery.models.filter((model) => model.loaded === true).length;
  return `LM Studio returned ${total} model ID${total === 1 ? "" : "s"}: ${generation} generation choice${generation === 1 ? "" : "s"}, ${embedding} embedding choice${embedding === 1 ? "" : "s"}, and ${loaded} currently loaded. Sources: ${sources}.${truncated} Discovery does not load models or send career data; the bounded synthetic test remains authoritative.`;
}

function credentialLabel(state: ProviderCredentialState | undefined): string {
  if (state === "stored") return "Stored securely";
  if (state === "unavailable") return "Credential store unavailable";
  if (state === "missing") return "API key required";
  return "Not required";
}

export function ProviderSettingsPanel({
  onSettingsChange,
  onDirtyChange,
  careerWorkspaceHasData = false,
  careerWorkspaceResetDisabled = false,
  onRequestCareerWorkspaceReset,
}: ProviderSettingsPanelProps) {
  const [settings, setSettings] = useState<ProviderSettings | null>(null);
  const [providerKind, setProviderKind] = useState<ProviderKind>("openai_compatible_local");
  const [baseUrl, setBaseUrl] = useState(LOCAL_BASE_URL);
  const [chatModelId, setChatModelId] = useState("");
  const [embeddingModelId, setEmbeddingModelId] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [discovery, setDiscovery] = useState<ProviderModelDiscovery | null>(null);
  const [chatExactEntry, setChatExactEntry] = useState(false);
  const [embeddingExactEntry, setEmbeddingExactEntry] = useState(false);
  const [loading, setLoading] = useState(true);
  const [discovering, setDiscovering] = useState(false);
  const [switchingProvider, setSwitchingProvider] = useState(false);
  const [saving, setSaving] = useState(false);
  const [savingCredential, setSavingCredential] = useState(false);
  const [deletingCredential, setDeletingCredential] = useState(false);
  const [testing, setTesting] = useState(false);
  const [costDirty, setCostDirty] = useState(false);
  const [costRefreshKey, setCostRefreshKey] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const settingsRef = useRef<ProviderSettings | null>(null);
  const providerKindRef = useRef<ProviderKind>(providerKind);
  const discoveryRequestRef = useRef(0);
  const mountedRef = useRef(true);
  const busy = loading || discovering || switchingProvider || saving || savingCredential || deletingCredential || testing;
  const cloud = isCloudProvider(providerKind);

  const applySettings = useCallback((result: ProviderSettings, preserveModelDrafts = false) => {
    settingsRef.current = result;
    providerKindRef.current = result.provider_kind;
    setSettings(result);
    setProviderKind(result.provider_kind);
    setBaseUrl(result.base_url);
    if (!preserveModelDrafts) {
      setChatExactEntry(false);
      setEmbeddingExactEntry(false);
      setChatModelId(
        result.chat_model_id ?? (result.provider_kind === "openai" ? DEFAULT_OPENAI_CHAT_MODEL_ID : ""),
      );
      setEmbeddingModelId(
        result.embedding_model_id ?? (result.provider_kind === "openai" ? DEFAULT_OPENAI_EMBEDDING_MODEL_ID : ""),
      );
    }
    onSettingsChange?.(result);
  }, [onSettingsChange]);

  const clearDiscovery = useCallback(() => {
    discoveryRequestRef.current += 1;
    setDiscovery(null);
    setDiscovering(false);
  }, []);

  const runDiscovery = useCallback(async (
    snapshot: ProviderSettings,
    requestedBaseUrl: string | null,
    successNotice?: string,
    failureNotice?: string,
  ): Promise<"applied" | "failed" | "stale"> => {
    const requestedProvider = snapshot.provider_kind;
    const requestedRevision = snapshot.revision;
    const requestId = discoveryRequestRef.current + 1;
    discoveryRequestRef.current = requestId;
    setDiscovering(true);
    setError(null);
    setNotice(null);
    try {
      const result = await invoke<ProviderModelDiscovery>("discover_model_provider_models", {
        expectedRevision: requestedRevision,
        providerKind: requestedProvider,
        baseUrl: requestedProvider === "openai" ? null : requestedBaseUrl,
      });
      const current = settingsRef.current;
      const currentRequest = discoveryRequestRef.current === requestId;
      const currentSettings = current?.revision === requestedRevision
        && current.provider_kind === requestedProvider
        && providerKindRef.current === requestedProvider;
      if (!mountedRef.current || !currentRequest || !currentSettings) return "stale";
      if (result.provider_kind !== requestedProvider) {
        setError("The returned model list did not match the selected provider. Your current model IDs were not changed.");
        setNotice(failureNotice ?? null);
        return "failed";
      }
      setDiscovery(result);
      setNotice(successNotice ?? null);
      return "applied";
    } catch (caught) {
      const current = settingsRef.current;
      const currentRequest = discoveryRequestRef.current === requestId;
      const currentSettings = current?.revision === requestedRevision
        && current.provider_kind === requestedProvider
        && providerKindRef.current === requestedProvider;
      if (!mountedRef.current || !currentRequest || !currentSettings) return "stale";
      setError(safeProviderDiscoveryError(caught, requestedProvider));
      setNotice(failureNotice ?? null);
      return "failed";
    } finally {
      if (mountedRef.current && discoveryRequestRef.current === requestId) {
        setDiscovering(false);
      }
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      discoveryRequestRef.current += 1;
    };
  }, []);

  useEffect(() => {
    let active = true;
    setLoading(true);
    void (async () => {
      try {
        const result = await invoke<ProviderSettings>("get_local_provider_settings");
        if (!active) return;
        applySettings(result);
        setError(null);
        if (result.provider_kind === "openai" && result.credential_state === "stored") {
          await runDiscovery(result, null);
        }
      } catch (caught) {
        if (active) setError(safeProviderError(caught));
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => { active = false; };
  }, [applySettings, runDiscovery]);

  const chooseProvider = useCallback(async (next: ProviderKind) => {
    if (!settings || busy || next === providerKind) return;
    const currentBaseUrl = providerKind === "openai" ? OPENAI_BASE_URL : baseUrl;
    const hasUnsavedChanges = currentBaseUrl !== settings.base_url
      || chatModelId.trim() !== (settings.chat_model_id ?? "")
      || embeddingModelId.trim() !== (settings.embedding_model_id ?? "")
      || apiKey.trim().length > 0
      || costDirty;
    if (hasUnsavedChanges && !window.confirm("Switch providers and discard the unsaved changes on this screen?")) return;
    setSwitchingProvider(true);
    setError(null);
    setNotice(null);
    setApiKey("");
    try {
      const result = await invoke<ProviderSettings>("select_model_provider", {
        expectedRevision: settings.revision,
        providerKind: next,
      });
      clearDiscovery();
      applySettings(result);
      const switchedNotice = next === "openai"
        ? "OpenAI selected. Your saved LM Studio setup is preserved."
        : "LM Studio selected. Your saved OpenAI setup is preserved.";
      if (next === "openai" && result.credential_state === "stored") {
        await runDiscovery(
          result,
          null,
          `${switchedNotice} Available models refreshed.`,
          `${switchedNotice} Available models could not be refreshed.`,
        );
      } else {
        setNotice(switchedNotice);
      }
    } catch (caught) {
      setError(safeProviderError(caught, settings.provider_kind));
    } finally {
      setSwitchingProvider(false);
    }
  }, [apiKey, applySettings, baseUrl, busy, chatModelId, clearDiscovery, costDirty, embeddingModelId, providerKind, runDiscovery, settings]);

  const discover = useCallback(async () => {
    if (!settings || busy || settings.provider_kind !== providerKind) return;
    if (cloud && (settings.credential_state !== "stored" || apiKey.trim())) return;
    await runDiscovery(settings, cloud ? null : baseUrl);
  }, [apiKey, baseUrl, busy, cloud, providerKind, runDiscovery, settings]);

  const save = useCallback(async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!settings || busy) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const result = await invoke<ProviderSettings>("save_local_provider_settings", {
        expectedRevision: settings.revision,
        providerKind,
        baseUrl: cloud ? OPENAI_BASE_URL : baseUrl,
        chatModelId: chatModelId.trim() || null,
        embeddingModelId: embeddingModelId.trim() || null,
      });
      applySettings(result);
      setNotice(
        cloud
          ? result.credential_state === "stored"
            ? "OpenAI model IDs saved. Run the bounded provider test before tailoring."
            : "OpenAI model IDs saved. Store an API key, then run the bounded provider test."
          : "LM Studio model selections saved. Test them before creating a tailored draft.",
      );
    } catch (caught) {
      setError(safeProviderError(caught, providerKind));
    } finally {
      setSaving(false);
    }
  }, [applySettings, baseUrl, busy, chatModelId, cloud, embeddingModelId, providerKind, settings]);

  const saveCredential = useCallback(async () => {
    if (
      !settings ||
      settings.provider_kind !== "openai" ||
      providerKind !== "openai" ||
      busy ||
      !apiKey.trim()
    ) return;
    setSavingCredential(true);
    setError(null);
    setNotice(null);
    try {
      const result = await invoke<ProviderSettings>("save_openai_api_key", { apiKey: apiKey.trim() });
      setApiKey("");
      clearDiscovery();
      applySettings(result, true);
      if (result.credential_state === "stored") {
        await runDiscovery(
          result,
          null,
          "OpenAI API key stored securely and available models refreshed. CVGnome will never display the key again.",
          "OpenAI API key stored securely, but available models could not be refreshed. The stored key remains in place, and current model IDs were not changed.",
        );
      } else {
        setNotice("OpenAI API key was not reported as stored, so available models were not refreshed.");
      }
    } catch (caught) {
      setError(safeProviderError(caught, "openai"));
    } finally {
      setSavingCredential(false);
    }
  }, [apiKey, applySettings, busy, clearDiscovery, providerKind, runDiscovery, settings]);

  const deleteCredential = useCallback(async () => {
    if (
      !settings ||
      settings.provider_kind !== "openai" ||
      settings.credential_state !== "stored" ||
      busy
    ) return;
    if (!window.confirm("Remove the OpenAI API key from this computer’s credential store? CVGnome cannot recover it.")) return;
    setDeletingCredential(true);
    setError(null);
    setNotice(null);
    try {
      const result = await invoke<ProviderSettings>("delete_openai_api_key");
      setApiKey("");
      clearDiscovery();
      applySettings(result, true);
      setNotice("OpenAI API key removed. Cloud requests are disabled until another key is stored and tested.");
    } catch (caught) {
      setError(safeProviderError(caught, "openai"));
    } finally {
      setDeletingCredential(false);
    }
  }, [applySettings, busy, clearDiscovery, settings]);

  const testModels = useCallback(async () => {
    if (!settings || busy) return;
    const testingCloud = settings.provider_kind === "openai";
    setTesting(true);
    setError(null);
    setNotice(
      testingCloud
        ? "Sending a short synthetic embedding and structured generation request directly to OpenAI. Your API account may be charged; no career data is used."
        : "Testing the embedding model, then the chat model. LM Studio may load and evict them sequentially.",
    );
    try {
      const result = await invoke<ProviderSettings>("test_local_provider_models");
      applySettings(result);
      setNotice(
        testingCloud
          ? "Both OpenAI models passed their bounded synthetic checks. No career data was used."
          : "Both local models passed their bounded synthetic checks. No career data was used.",
      );
    } catch (caught) {
      setError(safeProviderError(caught, settings.provider_kind));
      setNotice(null);
    } finally {
      if (testingCloud) setCostRefreshKey((current) => current + 1);
      setTesting(false);
    }
  }, [applySettings, busy, settings]);

  const currentDiscovery = discovery?.provider_kind === providerKind ? discovery : null;
  const discoveredModels = currentDiscovery?.models ?? [];
  const selectableModels = cloud && !currentDiscovery
    ? DEFAULT_OPENAI_MODEL_SUGGESTIONS
    : discoveredModels;
  const chatModels = useMemo(() => modelOptions(selectableModels, "chat"), [selectableModels]);
  const embeddingModels = useMemo(
    () => modelOptions(selectableModels, "embedding"),
    [selectableModels],
  );
  const unknownModels = useMemo(
    () => discoveredModels.filter((model) => model.capability_source === "unknown"),
    [discoveredModels],
  );
  const chatUsesExactEntry = chatExactEntry || !chatModels.some((model) => model.id === chatModelId);
  const embeddingUsesExactEntry = embeddingExactEntry
    || !embeddingModels.some((model) => model.id === embeddingModelId);
  const normalizedBaseUrl = cloud ? OPENAI_BASE_URL : baseUrl;
  const dirty = Boolean(settings && (
    providerKind !== settings.provider_kind ||
    normalizedBaseUrl !== settings.base_url ||
    chatModelId.trim() !== (settings.chat_model_id ?? "") ||
    embeddingModelId.trim() !== (settings.embedding_model_id ?? "")
  ));
  const keyDraft = apiKey.trim().length > 0;
  const hasUnsavedInput = dirty || keyDraft || costDirty;
  useDesktopCloseProtection({
    dirty: hasUnsavedInput,
    blocked: switchingProvider || saving || savingCredential || deletingCredential || testing,
  });
  useEffect(() => {
    onDirtyChange?.(hasUnsavedInput);
    return () => onDirtyChange?.(false);
  }, [hasUnsavedInput, onDirtyChange]);
  const verified = isProviderVerified(settings) && !dirty && !keyDraft;
  const cloudConfigurationSaved = settings?.provider_kind === "openai" && providerKind === "openai" && !dirty;
  const credentialStored = providerKind === "openai"
    && settings?.provider_kind === "openai"
    && settings.credential_state === "stored";
  const statusLabel = loading
    ? "Loading"
    : keyDraft
      ? "Unsaved API key"
    : verified
      ? "Models verified"
      : cloudConfigurationSaved && !credentialStored
        ? settings?.credential_state === "unavailable" ? "Credential store unavailable" : "API key required"
        : "Setup required";

  return (
    <section className="settings-workspace" aria-labelledby="models-title" aria-busy={busy}>
      <header className="settings-header">
        <div>
          <p className="section-kicker">Optional · direct model connection</p>
          <h2 id="models-title">Models</h2>
          <p>Models are only needed for tailored drafts. Choose where inference runs; CVGnome has no hosted model relay and never switches providers automatically.</p>
        </div>
        <span className={`provider-state ${verified ? "provider-state--ready" : ""}`}>
          {statusLabel}
        </span>
      </header>

      <div className="settings-layout">
        <form className="settings-document" onSubmit={save}>
          <section className="settings-section" aria-labelledby="provider-heading">
            <div className="settings-section__heading">
              <div><p className="section-kicker">Provider</p><h3 id="provider-heading">Inference location</h3></div>
            </div>
            <fieldset className="provider-choice" aria-label="Model provider">
              <label
                className={providerKind === "openai_compatible_local" ? "provider-choice__option is-selected" : "provider-choice__option"}
              >
                <input type="radio" name="model-provider" value="openai_compatible_local" checked={providerKind === "openai_compatible_local"} disabled={busy} onChange={() => void chooseProvider("openai_compatible_local")} />
                <span className="provider-choice__copy"><strong>LM Studio</strong><span>On this computer · outside provider billing</span></span>
              </label>
              <label
                className={providerKind === "openai" ? "provider-choice__option is-selected" : "provider-choice__option"}
              >
                <input type="radio" name="model-provider" value="openai" checked={providerKind === "openai"} disabled={busy} onChange={() => void chooseProvider("openai")} />
                <span className="provider-choice__copy"><strong>OpenAI API</strong><span>Your key · your API charges</span></span>
              </label>
            </fieldset>
          </section>

          {cloud ? (
            <section className="settings-section" aria-labelledby="connection-heading">
              <div className="settings-section__heading">
                <div><p className="section-kicker">Connection</p><h3 id="connection-heading">OpenAI API</h3></div>
                <button
                  type="button"
                  className="secondary-action"
                  onClick={() => void discover()}
                  disabled={busy || !credentialStored || keyDraft}
                >
                  {discovering ? "Refreshing…" : "Refresh available models"}
                </button>
              </div>
              <label className="field">
                <span>API endpoint</span>
                <input readOnly value={OPENAI_BASE_URL} spellCheck={false} aria-describedby="openai-endpoint-note" />
                <small id="openai-endpoint-note">Fixed by CVGnome. Custom remote endpoints and redirects are not accepted.</small>
              </label>
              <div className="credential-card">
                <div className="credential-card__heading">
                  <div><strong>OpenAI API key</strong><span>{credentialLabel(settings?.provider_kind === "openai" ? settings.credential_state : "missing")}</span></div>
                  {credentialStored && (
                    <button type="button" className="text-action text-action--danger" onClick={() => void deleteCredential()} disabled={busy || keyDraft}>Remove key</button>
                  )}
                </div>
                <label className="field">
                  <span>{credentialStored ? "Replace stored key" : "Store API key"}</span>
                  <input
                    type="password"
                    value={apiKey}
                    onChange={(event) => setApiKey(event.target.value)}
                    autoComplete="off"
                    autoCapitalize="none"
                    spellCheck={false}
                    placeholder={credentialStored ? "Enter a replacement key" : "sk-…"}
                    disabled={busy}
                  />
                  <small>Write-only: the native app stores this in your operating system credential store, never in CVGnome’s vault or settings file, and never reads it back into this screen.</small>
                </label>
                <button
                  type="button"
                  className="secondary-action"
                  onClick={() => void saveCredential()}
                  disabled={busy || !apiKey.trim()}
                >
                  {savingCredential ? "Storing key…" : credentialStored ? "Replace key" : "Store key securely"}
                </button>
                <p className="credential-card__note">Store the key before or after choosing model IDs; the key and both IDs must be saved before testing.</p>
              </div>
              <p className="model-discovery-hint">Available-model discovery reads account-visible model IDs directly from OpenAI and sends no career data. CVGnome’s compatibility catalog supplies role suggestions; the bounded synthetic test remains authoritative.</p>
            </section>
          ) : (
            <section className="settings-section" aria-labelledby="connection-heading">
              <div className="settings-section__heading">
                <div><p className="section-kicker">Connection</p><h3 id="connection-heading">LM Studio server</h3></div>
                <button type="button" className="secondary-action" onClick={() => void discover()} disabled={busy}>
                  {discovering ? "Refreshing…" : "Connect & refresh models"}
                </button>
              </div>
              <label className="field"><span>OpenAI-compatible base URL</span><input required disabled={busy} value={baseUrl} onChange={(event) => { setBaseUrl(event.target.value); clearDiscovery(); }} spellCheck={false} autoCapitalize="none" placeholder={LOCAL_BASE_URL} /><small>Only explicit numeric loopback addresses and the exact <code>/v1</code> path are accepted. Discovery reads model metadata only and does not load a model.</small></label>
            </section>
          )}

          <section className="settings-section" aria-labelledby="model-roles-heading">
            <div className="settings-section__heading"><div><p className="section-kicker">Inference</p><h3 id="model-roles-heading">Separate model roles</h3></div></div>
            <div className="settings-fields-grid">
              {cloud ? (
                <>
                  <div className="field model-choice-field">
                    <span id="openai-generation-model-label">Generation model (Responses + strict schema)</span>
                    <select
                      required
                      disabled={busy}
                      value={chatUsesExactEntry ? EXACT_MODEL_ID : chatModelId}
                      aria-labelledby="openai-generation-model-label"
                      onChange={(event) => {
                        if (event.target.value === EXACT_MODEL_ID) {
                          setChatExactEntry(true);
                        } else {
                          setChatExactEntry(false);
                          setChatModelId(event.target.value);
                        }
                      }}
                    >
                      {chatModels.map((model) => <option key={model.id} value={model.id}>{modelLabel(model)}</option>)}
                      <option value={EXACT_MODEL_ID}>Enter exact model ID…</option>
                    </select>
                    {chatUsesExactEntry && (
                      <input
                        required
                        disabled={busy}
                        value={chatModelId}
                        onChange={(event) => setChatModelId(event.target.value)}
                        aria-label="Exact generation model ID"
                        list="openai-unclassified-model-suggestions"
                        autoCapitalize="none"
                        autoComplete="off"
                        spellCheck={false}
                        placeholder={DEFAULT_OPENAI_CHAT_MODEL_ID}
                      />
                    )}
                    <small>Dropdown choices are account-visible and classified for generation. Exact or unclassified IDs still require the bounded synthetic test.</small>
                  </div>
                  <div className="field model-choice-field">
                    <span id="openai-embedding-model-label">Embedding model</span>
                    <select
                      required
                      disabled={busy}
                      value={embeddingUsesExactEntry ? EXACT_MODEL_ID : embeddingModelId}
                      aria-labelledby="openai-embedding-model-label"
                      onChange={(event) => {
                        if (event.target.value === EXACT_MODEL_ID) {
                          setEmbeddingExactEntry(true);
                        } else {
                          setEmbeddingExactEntry(false);
                          setEmbeddingModelId(event.target.value);
                        }
                      }}
                    >
                      {embeddingModels.map((model) => <option key={model.id} value={model.id}>{modelLabel(model)}</option>)}
                      <option value={EXACT_MODEL_ID}>Enter exact model ID…</option>
                    </select>
                    {embeddingUsesExactEntry && (
                      <input
                        required
                        disabled={busy}
                        value={embeddingModelId}
                        onChange={(event) => setEmbeddingModelId(event.target.value)}
                        aria-label="Exact embedding model ID"
                        list="openai-unclassified-model-suggestions"
                        autoCapitalize="none"
                        autoComplete="off"
                        spellCheck={false}
                        placeholder={DEFAULT_OPENAI_EMBEDDING_MODEL_ID}
                      />
                    )}
                    <small>Dropdown choices are account-visible and classified for embeddings. Exact or unclassified IDs still require the bounded synthetic test.</small>
                  </div>
                </>
              ) : (
                <>
                  <label className="field"><span>Generation model</span><select required disabled={busy} value={chatModelId} onChange={(event) => setChatModelId(event.target.value)}><option value="">Choose a generation model…</option>{chatModels.map((model) => <option key={model.id} value={model.id}>{modelLabel(model)}</option>)}{chatModelId && !chatModels.some((model) => model.id === chatModelId) && <option value={chatModelId}>{chatModelId} · saved or exact ID</option>}</select><small>Shows models LM Studio identifies as chat-capable. Produces a strict, reviewable resume patch.</small></label>
                  <label className="field"><span>Embedding model</span><select required disabled={busy} value={embeddingModelId} onChange={(event) => setEmbeddingModelId(event.target.value)}><option value="">Choose an embedding model…</option>{embeddingModels.map((model) => <option key={model.id} value={model.id}>{modelLabel(model)}</option>)}{embeddingModelId && !embeddingModels.some((model) => model.id === embeddingModelId) && <option value={embeddingModelId}>{embeddingModelId} · saved</option>}</select><small>Ranks canonical evidence before the chat step.</small></label>
                </>
              )}
            </div>
            {cloud && (
              <datalist id="openai-unclassified-model-suggestions">
                {unknownModels.map((model) => <option key={model.id} value={model.id}>{model.display_name}</option>)}
              </datalist>
            )}
            {currentDiscovery && <p className="model-discovery-summary" role="status">{discoverySummary(currentDiscovery)}</p>}
            {cloud && !currentDiscovery && <p className="model-discovery-summary">The current CVGnome defaults are shown as pre-discovery suggestions. Store a key to refresh account-visible choices, or enter an exact model ID.</p>}
            <div className="settings-actions">
              <button type="submit" className="primary-action" disabled={!settings || !dirty || busy || !chatModelId.trim() || !embeddingModelId.trim()}>{saving ? "Saving…" : "Save configuration"}</button>
              <button type="button" className="secondary-action" onClick={() => void testModels()} disabled={!settings || dirty || keyDraft || costDirty || busy || !settings.chat_model_id || !settings.embedding_model_id || (settings.provider_kind === "openai" && settings.credential_state !== "stored")}>{testing ? "Testing sequentially…" : cloud ? "Test OpenAI models" : "Test both models"}</button>
            </div>
          </section>

          {cloud && (
            <ProviderSpendControl
              generationModelId={chatModelId.trim()}
              embeddingModelId={embeddingModelId.trim()}
              disabled={busy}
              refreshKey={costRefreshKey}
              onDirtyChange={setCostDirty}
            />
          )}

          {cloud && (
            <section className="settings-disclosure" aria-labelledby="cloud-data-heading">
              <strong id="cloud-data-heading">What leaves this computer</strong>
              <p>The provider test sends only short synthetic text. Tailoring sends a bounded posting query and public resume evidence for embedding, then the full saved posting, a tailoring-relevant public baseline, and selected evidence excerpts for generation. CVGnome removes name, email, phone, location, URL, and profile links from baseline basics before sending. Raw imported files, source file names, private profile fields, and tracker notes are not sent.</p>
              <p>The connection is direct from this computer to OpenAI, which bills the API account attached to your key. CVGnome sets <code>store: false</code> on generation, but OpenAI’s policies still govern processing and retention. No CVGnome service receives the key or request, and no other provider is tried.</p>
            </section>
          )}

          <section className="settings-section settings-danger-zone" aria-labelledby="career-data-heading">
            <div className="settings-section__heading">
              <div>
                <p className="section-kicker">Data & privacy</p>
                <h3 id="career-data-heading">Local career workspace</h3>
              </div>
            </div>
            <p>
              Reset CVGnome’s managed profiles, imported copies, review items and decision history,
              roles, drafts, and resume artifacts to return to the new-user flow. Model configuration,
              your OS-stored API key, interface zoom, original source files, and exports saved elsewhere
              are preserved. The local cloud-cost ledger and spend controls are also preserved.
            </p>
            <button
              type="button"
              className="secondary-action danger-action"
              onClick={onRequestCareerWorkspaceReset}
              disabled={
                busy || hasUnsavedInput || careerWorkspaceResetDisabled ||
                !careerWorkspaceHasData || !onRequestCareerWorkspaceReset
              }
            >
              Reset career workspace…
            </button>
            {hasUnsavedInput && (
              <small>Save or discard the model changes above before resetting the career workspace.</small>
            )}
            {!careerWorkspaceHasData && !careerWorkspaceResetDisabled && !hasUnsavedInput && (
              <small>The career workspace is already empty.</small>
            )}
          </section>

          {error && <p className="settings-message settings-message--error" role="alert">{error}</p>}
          {notice && <p className="settings-message" role="status">{notice}</p>}
        </form>

        <aside className="settings-inspector" aria-label="Model provider status">
          <p className="section-kicker">Provider status</p>
          <dl className="property-grid compact-properties">
            <div><dt>Provider</dt><dd>{cloud ? "OpenAI API" : "LM Studio"}</dd></div>
            <div><dt>Configuration</dt><dd>{settings ? `Revision ${settings.configuration_revision}` : "Unavailable"}</dd></div>
            {cloud && <div><dt>API credential</dt><dd>{credentialLabel(settings?.provider_kind === "openai" ? settings.credential_state : "missing")}</dd></div>}
            <div><dt>Last verified</dt><dd>{formatProviderTimestamp(settings?.verified_at_ms ?? null)}</dd></div>
            <div><dt>Embedding dimensions</dt><dd>{settings?.embedding_dimension?.toLocaleString() ?? "Not measured"}</dd></div>
            <div><dt>Execution</dt><dd>Embedding → chat, never concurrent</dd></div>
          </dl>
          {cloud ? (
            <>
              <div className="settings-note"><strong>Usage and cost</strong><p>Testing and tailoring use your OpenAI API account and may incur charges. Local estimates and optional hard stops are configured in Spend guard; the provider’s bill remains authoritative.</p></div>
              <div className="settings-note"><strong>Credential boundary</strong><p>At rest, the key exists only in the operating system credential store. It exists transiently in the password field while entered and in native memory while attached to the fixed OpenAI API request.</p></div>
            </>
          ) : (
            <>
              <div className="settings-note"><strong>Memory behavior</strong><p>Leave LM Studio’s JIT loading and auto-evict enabled. CVGnome batches embeddings first, then asks for chat generation, allowing LM Studio to keep only one JIT-loaded model resident. Models loaded manually in LM Studio are not auto-evicted; eject them first if memory is tight.</p></div>
              <div className="settings-note"><strong>Locality boundary</strong><p>CVGnome connects only to loopback. If strict same-machine inference matters, disable LM Link in LM Studio. Authentication and LAN endpoints are intentionally unsupported.</p></div>
            </>
          )}
        </aside>
      </div>
    </section>
  );
}
