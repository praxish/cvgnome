// SPDX-License-Identifier: MPL-2.0
import { useCallback, useEffect, useMemo, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { useDesktopCloseProtection } from "./DesktopCloseGuard";
import {
  ProviderCostActivity,
  ProviderCostControls,
  ProviderPriceCard,
} from "./localProvider";

type ProviderSpendControlProps = {
  generationModelId: string;
  embeddingModelId: string;
  disabled?: boolean;
  refreshKey?: number;
  onDirtyChange?: (dirty: boolean) => void;
};

type PriceDraft = {
  modelId: string;
  input: string;
  output: string;
};

const MICROS_PER_USD = 1_000_000;
const MONEY_INPUT_PATTERN = /^\d+(?:\.\d{0,6})?$/;

function microsToInput(value: number | null): string {
  if (value === null) return "";
  return (value / MICROS_PER_USD).toFixed(6).replace(/\.?0+$/, "");
}

function inputToMicros(value: string, allowEmpty: true): number | null;
function inputToMicros(value: string, allowEmpty?: false): number;
function inputToMicros(value: string, allowEmpty = false): number | null {
  const trimmed = value.trim();
  if (!trimmed && allowEmpty) return null;
  if (!MONEY_INPUT_PATTERN.test(trimmed)) return Number.NaN;
  const micros = Math.round(Number(trimmed) * MICROS_PER_USD);
  return Number.isSafeInteger(micros) ? micros : Number.NaN;
}

function formatUsdMicros(value: number): string {
  const usd = Math.max(0, value) / MICROS_PER_USD;
  const maximumFractionDigits = usd > 0 && usd < 0.01 ? 6 : 2;
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits,
  }).format(usd);
}

function formatMonth(monthKey: string): string {
  const match = /^(\d{4})-(\d{2})$/.exec(monthKey);
  if (!match) return "This month";
  const date = new Date(Number(match[1]), Number(match[2]) - 1, 1);
  return new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric" }).format(date);
}

function formatActivityTime(timestamp: number): string {
  if (!Number.isFinite(timestamp)) return "Unknown time";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(timestamp));
}

function rateDraft(card: ProviderPriceCard): PriceDraft {
  return {
    modelId: card.model_id,
    input: microsToInput(card.input_microusd_per_million_tokens),
    output: microsToInput(card.output_microusd_per_million_tokens),
  };
}

function sortedDrafts(drafts: PriceDraft[]): PriceDraft[] {
  return [...drafts].sort((left, right) => left.modelId.localeCompare(right.modelId));
}

function fingerprint(
  enabled: boolean,
  monthlyLimit: string,
  perRequestLimit: string,
  drafts: PriceDraft[],
): string {
  return JSON.stringify({
    enabled,
    monthlyLimit: monthlyLimit.trim(),
    perRequestLimit: perRequestLimit.trim(),
    drafts: sortedDrafts(drafts).map((draft) => ({
      modelId: draft.modelId,
      input: draft.input.trim(),
      output: draft.output.trim(),
    })),
  });
}

function controlsFingerprint(controls: ProviderCostControls): string {
  return fingerprint(
    controls.enabled,
    microsToInput(controls.monthly_limit_microusd),
    microsToInput(controls.per_request_limit_microusd),
    controls.model_rates.map(rateDraft),
  );
}

function safeSpendError(caught: unknown): string {
  const raw = caught instanceof Error ? caught.message : String(caught);
  const value = raw.toLowerCase();
  if (value.includes("revision") || value.includes("changed")) {
    return "Spend controls changed on disk. Reload Models and try again.";
  }
  if (value.includes("model_rate") || value.includes("price") || value.includes("invalid")) {
    return "Check the limits and model prices. Use USD amounts with no more than six decimal places.";
  }
  return "CVGnome could not read or save the local spend controls. No provider request was made.";
}

function activityStatus(activity: ProviderCostActivity): string {
  if (activity.status === "uncertain") return "Outcome uncertain";
  if (activity.status === "reserved") return "Reserved";
  return "Completed";
}

function activityUsage(activity: ProviderCostActivity): string {
  if (activity.input_tokens === null && activity.output_tokens === null) {
    return "Usage not confirmed";
  }
  const parts: string[] = [];
  if (activity.input_tokens !== null) parts.push(`${activity.input_tokens.toLocaleString()} in`);
  if (activity.output_tokens !== null) parts.push(`${activity.output_tokens.toLocaleString()} out`);
  return parts.join(" · ");
}

function validRateDraft(draft: PriceDraft): boolean {
  const input = inputToMicros(draft.input);
  const output = inputToMicros(draft.output);
  return draft.modelId.trim().length > 0
    && Number.isFinite(input) && input >= 0
    && Number.isFinite(output) && output >= 0
    && (input > 0 || output > 0);
}

function activityCost(activity: ProviderCostActivity): string {
  if (activity.estimated_cost_microusd !== null) {
    return formatUsdMicros(activity.estimated_cost_microusd);
  }
  if (activity.reserved_cost_microusd !== null) {
    return `${formatUsdMicros(activity.reserved_cost_microusd)} reserved`;
  }
  return "Price unknown";
}

export function ProviderSpendControl({
  generationModelId,
  embeddingModelId,
  disabled = false,
  refreshKey = 0,
  onDirtyChange,
}: ProviderSpendControlProps) {
  const [controls, setControls] = useState<ProviderCostControls | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [monthlyLimit, setMonthlyLimit] = useState("");
  const [perRequestLimit, setPerRequestLimit] = useState("");
  const [drafts, setDrafts] = useState<PriceDraft[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const applyControls = useCallback((result: ProviderCostControls) => {
    setControls(result);
    setEnabled(result.enabled);
    setMonthlyLimit(microsToInput(result.monthly_limit_microusd));
    setPerRequestLimit(microsToInput(result.per_request_limit_microusd));
    setDrafts(result.model_rates.map(rateDraft));
  }, []);

  useEffect(() => {
    let active = true;
    setLoading(true);
    void invoke<ProviderCostControls>("get_provider_spend_control")
      .then((result) => {
        if (!active) return;
        applyControls(result);
        setError(null);
      })
      .catch((caught) => {
        if (active) setError(safeSpendError(caught));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, [applyControls, refreshKey]);

  const currentFingerprint = useMemo(
    () => fingerprint(enabled, monthlyLimit, perRequestLimit, drafts),
    [drafts, enabled, monthlyLimit, perRequestLimit],
  );
  const dirty = controls !== null && currentFingerprint !== controlsFingerprint(controls);
  useDesktopCloseProtection({ dirty, blocked: saving });

  useEffect(() => {
    onDirtyChange?.(dirty);
    return () => onDirtyChange?.(false);
  }, [dirty, onDirtyChange]);

  const updateRate = useCallback((modelId: string, field: "input" | "output", value: string) => {
    setDrafts((current) => {
      const existing = current.find((draft) => draft.modelId === modelId);
      if (!existing) {
        return [...current, {
          modelId,
          input: field === "input" ? value : "",
          output: field === "output" ? value : modelId === embeddingModelId ? "0" : "",
        }];
      }
      return current.map((draft) => draft.modelId === modelId ? { ...draft, [field]: value } : draft);
    });
    setError(null);
    setNotice(null);
  }, [embeddingModelId]);

  const removeRate = useCallback((modelId: string) => {
    setDrafts((current) => current.filter((draft) => draft.modelId !== modelId));
    setError(null);
    setNotice(null);
  }, []);

  const rateFor = useCallback(
    (modelId: string) => drafts.find((draft) => draft.modelId === modelId),
    [drafts],
  );

  const monthlyMicros = inputToMicros(monthlyLimit, true);
  const perRequestMicros = inputToMicros(perRequestLimit, true);
  const limitsValid = (monthlyMicros === null || (Number.isFinite(monthlyMicros) && monthlyMicros > 0))
    && (perRequestMicros === null || (Number.isFinite(perRequestMicros) && perRequestMicros > 0));
  const ratesValid = drafts.every(validRateDraft);

  const save = useCallback(async () => {
    if (!controls || saving || disabled || !dirty || !limitsValid || !ratesValid) return;
    const modelRates = sortedDrafts(drafts).map((draft) => ({
      provider_kind: "openai" as const,
      model_id: draft.modelId,
      input_microusd_per_million_tokens: inputToMicros(draft.input),
      output_microusd_per_million_tokens: inputToMicros(draft.output),
    }));
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const result = await invoke<ProviderCostControls>("save_provider_spend_control", {
        expectedRevision: controls.revision,
        enabled,
        monthlyLimitMicrousd: monthlyMicros,
        perRequestLimitMicrousd: perRequestMicros,
        blockUnpriced: true,
        modelRates,
      });
      applyControls(result);
      setNotice(enabled
        ? "Spend guard saved. OpenAI requests will be checked against these local limits before they are sent."
        : "Cost tracking settings saved. The guard is off, so estimates will not block requests.");
    } catch (caught) {
      setError(safeSpendError(caught));
    } finally {
      setSaving(false);
    }
  }, [applyControls, controls, dirty, disabled, drafts, enabled, limitsValid, monthlyMicros, perRequestMicros, ratesValid, saving]);

  const generationRate = generationModelId ? rateFor(generationModelId) : undefined;
  const embeddingRate = embeddingModelId ? rateFor(embeddingModelId) : undefined;
  const otherRates = drafts.filter(
    (draft) => draft.modelId !== generationModelId && draft.modelId !== embeddingModelId,
  );
  const currentModelsPriced = Boolean(
    generationRate && validRateDraft(generationRate)
    && embeddingRate && validRateDraft(embeddingRate),
  );
  const summary = controls?.summary;
  const committed = summary?.estimated_cost_microusd ?? 0;
  const reserved = summary?.reserved_cost_microusd ?? 0;
  const countedSpend = committed + reserved;
  const budget = controls?.monthly_limit_microusd ?? null;
  const remaining = budget === null ? null : Math.max(0, budget - countedSpend);
  const budgetPercent = budget && budget > 0 ? Math.min(100, countedSpend / budget * 100) : 0;
  const unavailable = disabled || loading || saving;

  const renderRateEditor = (
    role: "Generation" | "Embedding" | "Saved model",
    modelId: string,
    rate: PriceDraft | undefined,
  ) => (
    <div className="spend-rate-card" key={`${role}:${modelId}`}>
      <div className="spend-rate-card__heading">
        <div>
          <span>{role}</span>
          <strong title={modelId}>{modelId || `Choose a ${role.toLowerCase()} model first`}</strong>
        </div>
        {rate ? (
          <button type="button" className="text-action" disabled={unavailable} onClick={() => removeRate(modelId)}>Remove price</button>
        ) : (
          <span className="spend-rate-state">Unpriced</span>
        )}
      </div>
      <div className={role === "Embedding" ? "spend-rate-fields spend-rate-fields--single" : "spend-rate-fields"}>
        <label className="field">
          <span>Input · USD per 1M tokens</span>
          <input
            type="number"
            min="0"
            step="0.000001"
            inputMode="decimal"
            value={rate?.input ?? ""}
            placeholder="Enter provider rate"
            disabled={unavailable || !modelId}
            onChange={(event) => updateRate(modelId, "input", event.target.value)}
          />
        </label>
        {role !== "Embedding" && (
          <label className="field">
            <span>Output · USD per 1M tokens</span>
            <input
              type="number"
              min="0"
              step="0.000001"
              inputMode="decimal"
              value={rate?.output ?? ""}
              placeholder="Enter provider rate"
              disabled={unavailable || !modelId}
              onChange={(event) => updateRate(modelId, "output", event.target.value)}
            />
          </label>
        )}
      </div>
    </div>
  );

  return (
    <section
      className="settings-section spend-control"
      aria-labelledby="spend-control-heading"
      aria-busy={loading || saving}
      onKeyDown={(event) => {
        if (event.key === "Enter" && event.target instanceof HTMLInputElement) {
          event.preventDefault();
          void save();
        }
      }}
    >
      <div className="settings-section__heading">
        <div>
          <p className="section-kicker">Local cost control</p>
          <h3 id="spend-control-heading">OpenAI spend guard</h3>
        </div>
        <span className={`provider-state ${enabled && !dirty ? "provider-state--ready" : ""}`}>
          {loading
            ? "Loading"
            : dirty
              ? "Unsaved changes"
              : enabled
                ? "Guard on"
                : currentModelsPriced
                  ? "Cost tracking"
                  : "Usage tracking"}
        </span>
      </div>

      {controls && summary && (
        <div className="spend-summary" aria-label={`${formatMonth(summary.month_key)} ${summary.timezone} OpenAI cost estimate`}>
          <div>
            <span>{summary.unpriced_request_count > 0 ? "Known estimate" : "Estimated"} · {formatMonth(summary.month_key)} {summary.timezone}</span>
            <strong>{formatUsdMicros(committed)}</strong>
            <small className={summary.unpriced_request_count > 0 ? "spend-summary__unpriced" : undefined}>
              {summary.request_count.toLocaleString()} provider step{summary.request_count === 1 ? "" : "s"} recorded
              {summary.unpriced_request_count > 0 ? ` · ${summary.unpriced_request_count.toLocaleString()} unpriced` : ""}
            </small>
          </div>
          <div><span>Reserved</span><strong>{formatUsdMicros(reserved)}</strong><small>{reserved > 0 ? "Held for calls not yet resolved" : "No open reservations"}</small></div>
          <div><span>Remaining</span><strong>{remaining === null ? "No cap" : formatUsdMicros(remaining)}</strong><small>{budget === null ? "Set a monthly limit below" : `of ${formatUsdMicros(budget)}`}</small></div>
          {budget !== null && (
            <div className="spend-meter" aria-label={`${Math.round(budgetPercent)} percent of monthly limit counted`}>
              <span style={{ width: `${budgetPercent}%` }} />
            </div>
          )}
        </div>
      )}

      <label className="spend-toggle">
        <input
          type="checkbox"
          checked={enabled}
          disabled={unavailable}
          onChange={(event) => { setEnabled(event.target.checked); setNotice(null); }}
        />
        <span><strong>Stop over-limit or unpriced requests</strong><small>When on, CVGnome checks a conservative upper-bound estimate before every synthetic test and tailoring request. Models without a saved price are blocked.</small></span>
      </label>

      <div className="settings-fields-grid spend-limit-fields">
        <label className="field">
          <span>Monthly limit · USD</span>
          <input type="number" min="0.000001" step="0.01" inputMode="decimal" value={monthlyLimit} placeholder="No monthly limit" disabled={unavailable} onChange={(event) => { setMonthlyLimit(event.target.value); setNotice(null); }} />
          <small>UTC calendar month. Completed estimates and active reservations count toward this limit.</small>
        </label>
        <label className="field">
          <span>Per-request limit · USD</span>
          <input type="number" min="0.000001" step="0.01" inputMode="decimal" value={perRequestLimit} placeholder="No per-request limit" disabled={unavailable} onChange={(event) => { setPerRequestLimit(event.target.value); setNotice(null); }} />
          <small>Checked separately for the embedding step and the generation step.</small>
        </label>
      </div>

      <div className="spend-rates-heading">
        <div><strong>Current model prices</strong><p>Enter the provider’s current prices; CVGnome does not guess or silently update them. Discovery does not run inference and is not entered here.</p></div>
        {enabled && !currentModelsPriced && <span className="spend-warning">Requests blocked until priced</span>}
      </div>
      <div className="spend-rate-list">
        {renderRateEditor("Generation", generationModelId, generationRate)}
        {renderRateEditor("Embedding", embeddingModelId, embeddingRate)}
      </div>
      {otherRates.length > 0 && (
        <details className="spend-saved-rates">
          <summary>{otherRates.length} other saved model price{otherRates.length === 1 ? "" : "s"}</summary>
          <div className="spend-rate-list">
            {otherRates.map((rate) => renderRateEditor("Saved model", rate.modelId, rate))}
          </div>
        </details>
      )}
      <p className="spend-estimate-note">These are local estimates based on token usage reported by OpenAI and the prices you enter. An unpriced entry has unknown cost, not zero cost. They are not an invoice; your provider account remains authoritative. The ledger and limits stay on this computer and survive a career workspace reset.</p>

      <div className="settings-actions">
        <button type="button" className="primary-action" disabled={unavailable || !dirty || !limitsValid || !ratesValid} onClick={() => void save()}>{saving ? "Saving…" : "Save spend controls"}</button>
        {!limitsValid && <span className="spend-form-error">Limits must be positive USD amounts or left blank.</span>}
        {limitsValid && !ratesValid && <span className="spend-form-error">Each saved model price needs valid input and output rates.</span>}
      </div>

      {controls && controls.activity.length > 0 && (
        <details className="spend-activity">
          <summary>Recent local cost activity</summary>
          <ul>
            {controls.activity.slice(0, 12).map((activity) => (
              <li key={activity.id}>
                <div><strong>{activity.kind === "test" ? "Model test" : "Tailored draft"} · {activity.stage === "embedding" ? "Embedding" : "Generation"}</strong><span>{activityStatus(activity)} · {formatActivityTime(activity.occurred_at_ms)}</span></div>
                <div><strong>{activityCost(activity)}</strong><span>{activityUsage(activity)}</span></div>
                <code title={activity.model_id}>{activity.model_id}</code>
              </li>
            ))}
          </ul>
          {controls.activity.length > 12 && <p>Showing the 12 most recent of {controls.activity.length.toLocaleString()} retained local entries.</p>}
          {summary && summary.unpriced_request_count > 0 && <p>{summary.unpriced_request_count.toLocaleString()} request{summary.unpriced_request_count === 1 ? " was" : "s were"} not priced because no saved rate matched the model at the time.</p>}
        </details>
      )}

      {error && <p className="settings-message settings-message--error" role="alert">{error}</p>}
      {notice && <p className="settings-message" role="status">{notice}</p>}
    </section>
  );
}
