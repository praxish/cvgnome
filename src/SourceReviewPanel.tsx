// SPDX-License-Identifier: MPL-2.0
import { useCallback, useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { useDesktopCloseProtection } from "./DesktopCloseGuard";
import type { ReviewInboxCounts, ReviewInboxScope } from "./ReviewInboxPanel";

type SourceReviewState = "inbox" | "deferred" | "resolved";
type SourceReviewAction = "keep_profile" | "use_source" | "acknowledge_duplicate" | "defer" | "reopen";
type SourceReviewItem = {
  id: string;
  kind: "basic_conflict" | "duplicate_document";
  state: SourceReviewState;
  state_revision: number;
  title: string;
  field: "name" | "headline" | "summary" | "email" | "phone" | "url" | null;
  previous_value: string | null;
  proposed_value: string | null;
  resolution: "keep_profile" | "use_source" | "acknowledge_duplicate" | null;
  evidence: {
    display_name: string;
    source_format: "pdf" | "docx" | "json" | "txt" | "md" | "csv";
    source_kind: "resume" | "evidence" | "preferences" | "unclassified";
    location_label: string | null;
    excerpt: string;
  }[];
  is_previous_import_set: boolean;
  created_at_ms: number;
};
type SourceReviewPage = {
  current_profile_version_id: string | null;
  scope: ReviewInboxScope;
  offset: number;
  limit: number;
  total_items: number;
  next_offset: number | null;
  counts: ReviewInboxCounts;
  items: SourceReviewItem[];
};
type SourceReviewDecision = {
  item_id: string;
  expected_state: "inbox" | "deferred";
  expected_revision: number;
  action: SourceReviewAction;
};
type SourceReviewBatch = {
  requestId: string;
  expectedParentProfileVersionId: string;
  decisions: SourceReviewDecision[];
};
export type SourceReviewReceipt = {
  request_id: string;
  created: boolean;
  created_at_ms: number;
  parent_profile_version_id: string;
  profile_version_id: string;
  version_number: number;
  profile_changed: boolean;
  decisions: { item_id: string; action: SourceReviewAction; state: SourceReviewState; state_revision: number }[];
  counts: ReviewInboxCounts;
};
type QueuedChoice = { item: SourceReviewItem; action: SourceReviewAction };

const SCOPE_LABELS: Record<ReviewInboxScope, string> = { inbox: "Inbox", deferred: "Deferred", history: "History" };
const ACTION_LABELS: Record<SourceReviewAction, string> = {
  keep_profile: "Keep profile value",
  use_source: "Use source value",
  acknowledge_duplicate: "Mark reviewed",
  defer: "Defer",
  reopen: "Return to Inbox",
};

function failureMessage(caught: unknown): { uncertain: boolean; stale: boolean; message: string } {
  const code = (caught instanceof Error ? caught.message : String(caught)).split(":", 1)[0].trim();
  if (["source_review_not_found", "source_review_state_conflict", "source_review_stale_generation", "source_review_generation_conflict", "source_review_profile_conflict", "source_review_field_conflict", "profile_parent_conflict", "profile_version_conflict", "profile_missing"].includes(code)) {
    return { uncertain: false, stale: true, message: "The profile or a source check changed since you opened it. Nothing was applied. Your choices are still here; review the latest checks before applying again." };
  }
  if (["source_review_invalid_transition", "source_review_request_conflict", "source_review_evidence_invalid", "source_review_evidence_missing", "source_review_limit", "invalid_params", "profile_version_limit"].includes(code)) {
    return { uncertain: false, stale: true, message: "These choices could not be applied. Your queue is still here. Reload the latest checks to review what is available." };
  }
  if (code === "vault_busy") return { uncertain: false, stale: false, message: "Your local workspace is busy. Your choices are kept; try Apply again in a moment." };
  return { uncertain: true, stale: false, message: "CVGnome could not confirm whether your choices were saved. Retry this exact apply to recover the result safely." };
}

function validReceipt(receipt: SourceReviewReceipt, pending: SourceReviewBatch): boolean {
  if (!receipt || receipt.request_id !== pending.requestId
    || receipt.parent_profile_version_id !== pending.expectedParentProfileVersionId
    || typeof receipt.created !== "boolean" || typeof receipt.profile_changed !== "boolean"
    || !Number.isSafeInteger(receipt.created_at_ms) || receipt.created_at_ms <= 0
    || !Number.isSafeInteger(receipt.version_number) || receipt.version_number < 1
    || typeof receipt.profile_version_id !== "string" || receipt.profile_version_id.length === 0
    || receipt.profile_changed !== (receipt.profile_version_id !== receipt.parent_profile_version_id)
    || !Array.isArray(receipt.decisions) || receipt.decisions.length !== pending.decisions.length
    || !receipt.counts || [receipt.counts.inbox, receipt.counts.deferred, receipt.counts.history].some((count) => !Number.isSafeInteger(count) || count < 0)) return false;
  const seen = new Set<string>();
  return receipt.decisions.every((result) => {
    const expected = pending.decisions.find((decision) => decision.item_id === result.item_id);
    if (!expected || seen.has(result.item_id)) return false;
    seen.add(result.item_id);
    const state = expected.action === "defer" ? "deferred" : expected.action === "reopen" ? "inbox" : "resolved";
    return result.action === expected.action && result.state === state && result.state_revision === expected.expected_revision + 1;
  });
}

export function SourceReviewPanel({ initialCounts, backLabel, onBack, onApplied, onMutationBusyChange }: {
  initialCounts: ReviewInboxCounts;
  backLabel: string;
  onBack: () => void;
  onApplied: (receipt: SourceReviewReceipt) => Promise<void>;
  onMutationBusyChange: (busy: boolean) => void;
}) {
  const [scope, setScope] = useState<ReviewInboxScope>(() => initialCounts.inbox ? "inbox" : initialCounts.deferred ? "deferred" : "history");
  const [page, setPage] = useState<SourceReviewPage | null>(null);
  const [items, setItems] = useState<SourceReviewItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [choices, setChoices] = useState<QueuedChoice[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);
  const [applyError, setApplyError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [stale, setStale] = useState(false);
  const [announcement, setAnnouncement] = useState("");
  const listRequest = useRef(0);
  const pendingBatch = useRef<SourceReviewBatch | null>(null);
  const queueParent = useRef<string | null>(null);
  const inFlight = useRef(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const locked = busy || uncertain;
  const navigationLocked = locked || choices.length > 0;
  useDesktopCloseProtection({ dirty: choices.length > 0, blocked: locked });
  const counts = page?.counts ?? initialCounts;
  const selected = items.find((item) => item.id === selectedId) ?? null;
  const selectedChoice = choices.find((choice) => choice.item.id === selectedId);
  const selectedReadOnly = !selected || selected.state === "resolved" || selected.is_previous_import_set || scope === "history";

  useEffect(() => { heading.current?.focus(); }, []);
  useEffect(() => {
    onMutationBusyChange(navigationLocked);
    return () => onMutationBusyChange(false);
  }, [navigationLocked, onMutationBusyChange]);
  useEffect(() => {
    if (!navigationLocked) return;
    const preventClose = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", preventClose);
    return () => window.removeEventListener("beforeunload", preventClose);
  }, [navigationLocked]);

  const load = useCallback(async (nextScope: ReviewInboxScope, offset = 0, append = false) => {
    const request = ++listRequest.current;
    setLoading(true);
    setListError(null);
    try {
      const next = await invoke<SourceReviewPage>("list_source_review_items", { scope: nextScope, offset, limit: 10 });
      if (request !== listRequest.current) return;
      if (queueParent.current && next.current_profile_version_id !== queueParent.current) {
        setStale(true);
        setApplyError("Your profile changed while these choices were queued. The queue is kept; reload and review the latest checks before applying.");
      }
      setScope(nextScope);
      setPage(next);
      setItems((current) => append ? [...current, ...next.items.filter((item) => !current.some((existing) => existing.id === item.id))] : next.items);
      if (!append) setSelectedId((current) => next.items.some((item) => item.id === current) ? current : next.items[0]?.id ?? null);
    } catch {
      if (request === listRequest.current) setListError("CVGnome could not read the source checks. Your current view and queued choices are kept.");
    } finally {
      if (request === listRequest.current) setLoading(false);
    }
  }, []);
  useEffect(() => {
    void load(initialCounts.inbox ? "inbox" : initialCounts.deferred ? "deferred" : "history");
    return () => { listRequest.current += 1; };
    // Open once; receipts and explicit refreshes update this view without erasing choices.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load]);

  function queue(item: SourceReviewItem, action: SourceReviewAction) {
    if (locked || stale || item.is_previous_import_set || item.state === "resolved" || !page?.current_profile_version_id) return;
    const otherChoices = choices.filter((choice) => choice.item.id !== item.id);
    if (otherChoices.length >= 50) { setApplyError("Apply or remove some of the 50 queued choices before adding another."); return; }
    if (action === "use_source" && otherChoices.some((choice) => choice.action === "use_source" && choice.item.field === item.field)) {
      setApplyError("Another source value for this field is already queued. Undo that choice before choosing a different value.");
      return;
    }
    queueParent.current ??= page.current_profile_version_id;
    pendingBatch.current = null;
    setChoices([...otherChoices, { item, action }]);
    setApplyError(null);
    setAnnouncement(`${ACTION_LABELS[action]} queued for ${item.title}.`);
  }

  function undo(itemId: string) {
    if (locked) return;
    const remaining = choices.filter((choice) => choice.item.id !== itemId);
    setChoices(remaining);
    pendingBatch.current = null;
    if (!remaining.length) queueParent.current = null;
    setAnnouncement("Choice removed from the queue.");
  }

  async function apply() {
    if (inFlight.current || !choices.length || stale || !queueParent.current) return;
    const batch = pendingBatch.current ?? {
      requestId: crypto.randomUUID(),
      expectedParentProfileVersionId: queueParent.current,
      decisions: choices.map(({ item, action }) => ({ item_id: item.id, expected_state: item.state as "inbox" | "deferred", expected_revision: item.state_revision, action })),
    };
    pendingBatch.current = batch;
    inFlight.current = true;
    setBusy(true);
    setApplyError(null);
    try {
      let receipt: SourceReviewReceipt;
      try {
        receipt = await invoke<SourceReviewReceipt>("apply_source_review_decisions", batch);
        if (!validReceipt(receipt, batch)) throw new Error("source_review_uncertain");
      } catch (caught) {
        const failure = failureMessage(caught);
        setUncertain(failure.uncertain);
        setStale(failure.stale);
        setApplyError(failure.message);
        if (!failure.uncertain) pendingBatch.current = null;
        return;
      }
      pendingBatch.current = null;
      queueParent.current = null;
      setUncertain(false);
      setChoices([]);
      setPage((current) => current ? { ...current, counts: receipt.counts, current_profile_version_id: receipt.profile_version_id } : current);
      const updates = new Map(receipt.decisions.map((decision) => [decision.item_id, decision]));
      setItems((current) => current.map((item) => {
        const update = updates.get(item.id);
        return update ? { ...item, state: update.state, state_revision: update.state_revision, resolution: update.state === "resolved" ? update.action as SourceReviewItem["resolution"] : item.resolution } : item;
      }));
      setAnnouncement(receipt.profile_changed
        ? `${receipt.decisions.length} ${receipt.decisions.length === 1 ? "choice" : "choices"} applied together. Profile version ${receipt.version_number} is saved.`
        : `${receipt.decisions.length} ${receipt.decisions.length === 1 ? "choice" : "choices"} saved. Your profile is unchanged.`);
      try { await onApplied(receipt); }
      catch { setApplyError("Your choices are saved, but workspace totals could not refresh. Reload the view; you do not need to apply again."); }
      await load(scope);
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  function discardAndReload() {
    if (locked) return;
    setChoices([]);
    queueParent.current = null;
    pendingBatch.current = null;
    setStale(false);
    setApplyError(null);
    void load(scope);
  }

  return (
    <section className="materials-workspace review-inbox-workspace source-review-workspace" aria-labelledby="source-checks-title">
      <p className="sr-only" role="status" aria-live="polite">{announcement}</p>
      <header className="materials-commandbar review-inbox-commandbar">
        <div className="materials-commandbar__title">
          <button type="button" className="profile-back-button" onClick={onBack} disabled={navigationLocked}><span aria-hidden="true">←</span> {backLabel}</button>
          <div>
            <p className="section-kicker">Imported material</p>
            <h2 id="source-checks-title" ref={heading} tabIndex={-1}>Source checks</h2>
            <span>Compare source details and queue your choices, then Apply when ready.</span>
          </div>
        </div>
      </header>
      <div className="review-inbox-browser">
        <aside className="materials-list-pane review-inbox-list-pane">
          <div className="pane-header"><h3>Source checks</h3><button type="button" className="quiet-action" onClick={() => void load(scope)} disabled={loading || locked || choices.length > 0}>Refresh</button></div>
          <div className="segmented-control" aria-label="Source check state">
            {(Object.keys(SCOPE_LABELS) as ReviewInboxScope[]).map((value) => <button key={value} type="button" className={scope === value ? "is-selected" : ""} aria-pressed={scope === value} onClick={() => void load(value)} disabled={loading || locked}>{SCOPE_LABELS[value]} <span className="review-scope-count">{counts[value]}</span></button>)}
          </div>
          <nav className="review-inbox-list" aria-label={`${SCOPE_LABELS[scope]} source checks`} aria-busy={loading}>
            {loading && !items.length && <p className="pane-placeholder">Reading source checks…</p>}
            {listError && <div className="pane-error" role="alert"><p>{listError}</p><button type="button" onClick={() => void load(scope)} disabled={locked}>Try again</button></div>}
            {!loading && !listError && !items.length && <div className="pane-placeholder"><strong>No {scope} source checks</strong><span>{scope === "inbox" ? "Conflicting details and identical documents from future imports will appear here." : scope === "deferred" ? "Checks you set aside stay here until you return them to Inbox." : "Completed checks and earlier import sets appear here."}</span></div>}
            {items.map((item) => <button key={item.id} type="button" className={`review-item-row${item.id === selectedId ? " is-selected" : ""}`} aria-current={item.id === selectedId ? "true" : undefined} onClick={() => setSelectedId(item.id)} disabled={busy}>
              <span className="review-item-glyph" aria-hidden="true">{item.kind === "basic_conflict" ? "ABC" : "DOC"}</span>
              <span className="review-item-row__copy"><strong>{item.title}</strong><small>{item.kind === "basic_conflict" ? "Different source detail" : "Identical documents"}</small></span>
              <span className="review-item-row__state">{choices.some((choice) => choice.item.id === item.id) ? "Choice queued" : item.is_previous_import_set ? "Previous import set" : item.state === "resolved" ? "Reviewed" : SCOPE_LABELS[item.state]}</span>
            </button>)}
            {page?.next_offset != null && <button type="button" className="load-more-row" onClick={() => void load(scope, page.next_offset!, true)} disabled={loading || locked}>{loading ? "Loading…" : "Load more"}</button>}
          </nav>
        </aside>
        <main className="review-inbox-inspector" aria-busy={busy}>
          {selected ? <article className="review-inbox-document" aria-labelledby="source-check-title">
            <header className="document-header">
              <div><p className="section-kicker">{selected.kind === "basic_conflict" ? "Basic Details" : "Imported files"}</p><h2 id="source-check-title">{selected.title}</h2><p>{selected.is_previous_import_set ? "Previous import set · Read-only history" : selected.state === "resolved" ? "Reviewed · Read-only history" : selected.state === "deferred" ? "Deferred" : "Ready for your review"}</p></div>
            </header>
            {selected.kind === "basic_conflict" ? <>
              <p className="source-check-intro">The imported source gives a different {selected.field === "url" ? "website" : selected.field}. Compare the values recorded when this check was created.</p>
              <div className="source-value-comparison">
                <section><h3>Profile at import</h3><p>{selected.previous_value || "No value"}</p></section>
                <section><h3>Source value</h3><p>{selected.proposed_value || "No value"}</p></section>
              </div>
            </> : <div className="review-item-honesty" role="note"><span aria-hidden="true">≡</span><p><strong>Identical file contents; both files retained.</strong>Duplicate copies do not add profile facts. Marking this check reviewed keeps both originals in your evidence trail.</p></div>}
            {selectedReadOnly ? <p className="source-check-history">{selected.resolution ? `${ACTION_LABELS[selected.resolution]} · ` : ""}This check is kept as read-only history.</p> : <section className="review-item-actions source-check-actions">
              <div><h3>{selectedChoice ? `${ACTION_LABELS[selectedChoice.action]} queued` : "Choose what to do"}</h3><p>Nothing changes until you Apply the queued choices.</p></div>
              <div className="review-item-actions__buttons">
                {selected.state === "inbox" && (selected.kind === "basic_conflict" ? <>
                  <button type="button" className="secondary-action" onClick={() => queue(selected, "keep_profile")} disabled={locked || stale}>Keep profile value</button>
                  <button type="button" className="secondary-action" onClick={() => queue(selected, "use_source")} disabled={locked || stale}>Use source value</button>
                </> : <button type="button" className="secondary-action" onClick={() => queue(selected, "acknowledge_duplicate")} disabled={locked || stale}>Mark reviewed</button>)}
                <button type="button" className="quiet-action" onClick={() => queue(selected, selected.state === "deferred" ? "reopen" : "defer")} disabled={locked || stale}>{selected.state === "deferred" ? "Return to Inbox" : "Defer"}</button>
                {selectedChoice && <button type="button" className="quiet-action" onClick={() => undo(selected.id)} disabled={locked}>Undo choice</button>}
              </div>
            </section>}
            <section className="review-item-evidence source-check-evidence" aria-labelledby="source-check-evidence-title">
              <header><h3 id="source-check-evidence-title">{selected.kind === "basic_conflict" ? "Where this came from" : "Retained documents"}</h3></header>
              {selected.evidence.map((evidence, index) => <article key={index}><header><strong title={evidence.display_name}>{evidence.display_name}</strong><small>{evidence.source_format.toUpperCase()} · {evidence.source_kind}</small></header>{evidence.location_label && <span>{evidence.location_label}</span>}{evidence.excerpt && <blockquote>{evidence.excerpt}</blockquote>}</article>)}
            </section>
          </article> : <div className="empty-document"><div className="empty-document__glyph" aria-hidden="true">◇</div><h2>{loading ? "Opening source checks" : "No source check selected"}</h2><p>Choose a check to compare the details and review its source.</p></div>}
        </main>
      </div>
      {(choices.length > 0 || applyError) && <footer className="source-choice-tray" aria-label="Queued source choices">
        {applyError && <div className="profile-review-inline-error" role="alert"><p>{applyError}</p>{stale && <button type="button" className="secondary-action" onClick={discardAndReload} disabled={locked}>{choices.length ? "Discard choices and reload" : "Reload latest checks"}</button>}</div>}
        {choices.length > 0 && <>
          <details><summary>{choices.length} {choices.length === 1 ? "choice" : "choices"} queued</summary><ul>{choices.map(({ item, action }) => <li key={item.id}><span><strong>{item.title}</strong> · {ACTION_LABELS[action]}</span><button type="button" className="quiet-action" onClick={() => undo(item.id)} disabled={locked} aria-label={`Undo choice for ${item.title}`}>Undo</button></li>)}</ul></details>
          <div className="source-choice-tray__actions"><p>{uncertain ? "Resolve this apply before leaving the review." : "Apply saves all choices together. Source values create one profile version."}</p><button type="button" className="quiet-action" onClick={() => { if (!locked) { setChoices([]); queueParent.current = null; pendingBatch.current = null; } }} disabled={locked}>Clear choices</button><button type="button" className="primary-action" onClick={() => void apply()} disabled={busy || stale}>{busy ? "Applying…" : uncertain ? "Retry exact apply" : `Apply ${choices.length} ${choices.length === 1 ? "choice" : "choices"}`}</button></div>
        </>}
      </footer>}
    </section>
  );
}
