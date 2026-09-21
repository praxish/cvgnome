// SPDX-License-Identifier: MPL-2.0
import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { useDesktopCloseProtection } from "./DesktopCloseGuard";
import "./MemoryPanel.css";

type MemoryScope = "current" | "history";
type MemoryItem = {
  id: string;
  title: string | null;
  excerpt: string;
  character_count: number;
  created_at_ms: number;
  is_previous_import_set: boolean;
  review_item_id: string | null;
};
type MemoryDetail = MemoryItem & {
  narrative: string;
  profile_version_id: string;
  retention_generation: number;
};
type MemoryPage = {
  scope: MemoryScope;
  offset: number;
  limit: number;
  total_items: number;
  next_offset: number | null;
  current_profile_version_id: string | null;
  retention_generation: number;
  items: MemoryItem[];
};
type MemoryDraft = { title: string; narrative: string };
type ProjectDraft = { name: string; description: string };
type PendingMemorySave = {
  kind: "memory";
  requestId: string;
  expectedParentProfileVersionId: string;
  expectedRetentionGeneration: number;
  title: string | null;
  narrative: string;
};
type PendingProjectSave = {
  kind: "project";
  requestId: string;
  memoryId: string;
  expectedParentProfileVersionId: string;
  expectedRetentionGeneration: number;
  name: string;
  description: string | null;
};
type PendingSave = PendingMemorySave | PendingProjectSave;
type MemorySaveReceipt = {
  request_id: string;
  memory_id: string;
  profile_version_id: string;
  retention_generation: number;
  created_at_ms: number;
  created: boolean;
};
type ProjectSaveReceipt = {
  request_id: string;
  memory_id: string;
  review_item_id: string;
  created_at_ms: number;
  created: boolean;
};

function timestamp(value: number): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Unknown date" : new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium", timeStyle: "short",
  }).format(date);
}

function errorCode(caught: unknown): string {
  const message = typeof caught === "string" ? caught : caught instanceof Error ? caught.message : "";
  return message.match(/^([a-z_]+):/)?.[1] ?? "";
}

function saveFailure(caught: unknown): { definitive: boolean; message: string } {
  switch (errorCode(caught)) {
    case "vault_busy":
      return { definitive: true, message: "The local vault is busy. Nothing was saved by this request. Wait a moment and try again; your draft is still here." };
    case "invalid_params":
    case "memory_invalid":
    case "memory_input_invalid":
      return { definitive: true, message: "That draft could not be validated. Your text is still here; check the field lengths and try again." };
    case "profile_missing":
    case "profile_parent_conflict":
    case "profile_version_conflict":
    case "memory_profile_conflict":
      return { definitive: true, message: "The current profile changed. Refresh the local context, then save again. Your draft has been kept." };
    case "memory_stale_generation":
    case "source_retention_conflict":
      return { definitive: true, message: "The active import set changed. Refresh the local context before saving again. Earlier memories stay in History." };
    case "memory_not_found":
    case "memory_evidence_missing":
    case "memory_evidence_invalid":
      return { definitive: true, message: "The original memory could not be verified. No new suggestion was queued; your draft is still here." };
    case "memory_request_conflict":
    case "request_conflict":
      return { definitive: true, message: "That save request was already used for different content. No new item was saved; review your draft and try again." };
    case "memory_project_exists":
    case "memory_proposal_exists":
      return { definitive: true, message: "This memory already has a project suggestion. Open Profile suggestions to review it; your draft has been kept here." };
    case "memory_limit":
    case "memory_limit_reached":
      return { definitive: true, message: "The local memory collection has reached its safe limit. Your draft has not been discarded." };
    default:
      return { definitive: false, message: "CVGnome could not confirm whether this was saved. Retry this exact save to recover safely. Your text is protected until the outcome is confirmed." };
  }
}

function validReceipt(receipt: MemorySaveReceipt | ProjectSaveReceipt, pending: PendingSave): boolean {
  if (receipt.request_id !== pending.requestId || typeof receipt.memory_id !== "string" || !receipt.memory_id
    || typeof receipt.created !== "boolean" || !Number.isSafeInteger(receipt.created_at_ms) || receipt.created_at_ms <= 0) return false;
  if (pending.kind === "memory") {
    const saved = receipt as MemorySaveReceipt;
    return saved.profile_version_id === pending.expectedParentProfileVersionId
      && saved.retention_generation === pending.expectedRetentionGeneration;
  }
  const saved = receipt as ProjectSaveReceipt;
  return saved.memory_id === pending.memoryId && typeof saved.review_item_id === "string" && saved.review_item_id.length > 0;
}

const invalidControl = (value: string, multiline = false) => Array.from(value).some(
  (character) => /[\p{Cc}\p{Cf}]/u.test(character) && !(multiline && "\r\n\t".includes(character)),
);
const characterCount = (value: string) => Array.from(value).length;

export function MemoryPanel({ onBack, onReviewSuggestions, onStatusRefresh, onDirtyChange, onMutationBusyChange }: {
  onBack: () => void;
  onReviewSuggestions: () => void;
  onStatusRefresh: () => Promise<void>;
  onDirtyChange: (dirty: boolean) => void;
  onMutationBusyChange: (busy: boolean) => void;
}) {
  const [scope, setScope] = useState<MemoryScope>("current");
  const [page, setPage] = useState<MemoryPage | null>(null);
  const [items, setItems] = useState<MemoryItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selected, setSelected] = useState<MemoryDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [memoryDraft, setMemoryDraft] = useState<MemoryDraft | null>(null);
  const [projectDraft, setProjectDraft] = useState<ProjectDraft | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [pending, setPending] = useState<PendingSave | null>(null);
  const [uncertain, setUncertain] = useState(false);
  const [refreshNeeded, setRefreshNeeded] = useState<"saved" | "context" | null>(null);
  const pageRequestRef = useRef(0);
  const detailRequestRef = useRef(0);
  const savingRef = useRef(false);
  const pendingRef = useRef<PendingSave | null>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const detailHeadingRef = useRef<HTMLHeadingElement>(null);
  const saveErrorRef = useRef<HTMLDivElement>(null);
  const draftFirstInputRef = useRef<HTMLInputElement>(null);
  const focusSavedDetailRef = useRef(false);
  const dirty = Boolean(memoryDraft && (memoryDraft.title || memoryDraft.narrative)
    || projectDraft && (projectDraft.name || projectDraft.description));
  const locked = saving || uncertain;
  useDesktopCloseProtection({ dirty, blocked: locked });

  useEffect(() => {
    headingRef.current?.focus();
    return () => { pageRequestRef.current += 1; detailRequestRef.current += 1; };
  }, []);
  useEffect(() => {
    if (saveError) window.requestAnimationFrame(() => saveErrorRef.current?.focus());
  }, [saveError]);
  useEffect(() => {
    onDirtyChange(dirty);
    return () => onDirtyChange(false);
  }, [dirty, onDirtyChange]);
  useEffect(() => {
    onMutationBusyChange(locked);
    return () => onMutationBusyChange(false);
  }, [locked, onMutationBusyChange]);
  useEffect(() => {
    if (!dirty && !locked) return;
    const preventClose = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", preventClose);
    return () => window.removeEventListener("beforeunload", preventClose);
  }, [dirty, locked]);

  const loadPage = useCallback(async (nextScope: MemoryScope, offset = 0, append = false, desiredId?: string): Promise<boolean> => {
    const request = ++pageRequestRef.current;
    append ? setLoadingMore(true) : setLoading(true);
    setListError(null);
    try {
      const next = await invoke<MemoryPage>("list_memories", { scope: nextScope, offset });
      if (request !== pageRequestRef.current) return false;
      setPage(next);
      setItems((current) => append ? [...current, ...next.items.filter((item) => !current.some((old) => old.id === item.id))] : next.items);
      if (!append) setSelectedId((current) => desiredId ?? (next.items.some((item) => item.id === current) ? current : next.items[0]?.id ?? null));
      return true;
    } catch (caught) {
      if (request === pageRequestRef.current) setListError(errorCode(caught) === "vault_busy"
        ? "The local vault is busy. Try opening your memories again in a moment."
        : "CVGnome could not read your local memories. Your saved originals have not been changed.");
      return false;
    } finally {
      if (request === pageRequestRef.current) { setLoading(false); setLoadingMore(false); }
    }
  }, []);

  const loadDetail = useCallback(async (memoryId: string): Promise<boolean> => {
    const request = ++detailRequestRef.current;
    setDetailLoading(true);
    setDetailError(null);
    try {
      const next = await invoke<MemoryDetail>("get_memory", { memoryId });
      if (request !== detailRequestRef.current) return false;
      if (next.id !== memoryId) throw new Error("memory_invalid: Unexpected memory returned");
      setSelected(next);
      if (focusSavedDetailRef.current) {
        focusSavedDetailRef.current = false;
        window.requestAnimationFrame(() => detailHeadingRef.current?.focus());
      }
      return true;
    } catch {
      if (request === detailRequestRef.current) setDetailError("CVGnome could not open this saved original. Try again before preparing a suggestion.");
      return false;
    } finally {
      if (request === detailRequestRef.current) setDetailLoading(false);
    }
  }, []);

  useEffect(() => { void loadPage(scope); }, [loadPage, scope]);
  useEffect(() => {
    setSelected(null);
    setDetailError(null);
    if (selectedId) void loadDetail(selectedId);
    else setDetailLoading(false);
    return () => { detailRequestRef.current += 1; };
  }, [loadDetail, selectedId]);

  const leaveDraft = useCallback((): boolean => {
    if (savingRef.current || pendingRef.current || locked) return false;
    if (dirty && !window.confirm("Discard this unsaved draft? Saved memories and suggestions will not be changed.")) return false;
    setMemoryDraft(null);
    setProjectDraft(null);
    setSaveError(null);
    return true;
  }, [dirty, locked]);

  const refreshContext = useCallback(async () => {
    const detailLoaded = selectedId ? await loadDetail(selectedId) : true;
    const loaded = await loadPage(scope);
    try {
      await onStatusRefresh();
      setRefreshNeeded((current) => !loaded || !detailLoaded ? current ?? "context" : null);
    } catch { setRefreshNeeded((current) => current ?? "context"); }
  }, [loadDetail, loadPage, onStatusRefresh, scope, selectedId]);

  const performSave = useCallback(async (request: PendingSave) => {
    if (savingRef.current) return;
    savingRef.current = true;
    pendingRef.current = request;
    onMutationBusyChange(true);
    setPending(request);
    setSaving(true);
    setSaveError(null);
    setNotice(null);
    let confirmed = false;
    try {
      const { kind, ...params } = request;
      const receipt = await invoke<MemorySaveReceipt | ProjectSaveReceipt>(kind === "memory" ? "save_memory" : "queue_memory_project", params);
      if (!validReceipt(receipt, request)) throw new Error("memory_uncertain: Invalid save receipt");
      confirmed = true;
      pendingRef.current = null;
      setPending(null);
      setUncertain(false);
      setMemoryDraft(null);
      setProjectDraft(null);
      setNotice(kind === "memory"
        ? "Memory saved on this computer. Your original words are preserved; your profile has not changed."
        : "Project suggestion queued for review. Your original memory and your profile have not changed.");
      focusSavedDetailRef.current = true;
      const listed = await loadPage(scope, 0, false, receipt.memory_id);
      // A new selection is opened by the selectedId effect. Only refresh here
      // when the selection stayed put (for example after queuing a project).
      // Competing detail reads would invalidate one another's request tokens.
      const opened = receipt.memory_id === selectedId ? await loadDetail(receipt.memory_id) : true;
      try {
        await onStatusRefresh();
        setRefreshNeeded(!listed || !opened ? "saved" : null);
      } catch { setRefreshNeeded("saved"); }
    } catch (caught) {
      if (confirmed) {
        setRefreshNeeded("saved");
      } else {
        const failure = saveFailure(caught);
        setSaveError(failure.message);
        setUncertain(!failure.definitive);
        if (failure.definitive) { pendingRef.current = null; setPending(null); }
      }
    } finally {
      savingRef.current = false;
      setSaving(false);
      onMutationBusyChange(pendingRef.current !== null);
    }
  }, [loadDetail, loadPage, onMutationBusyChange, onStatusRefresh, scope, selectedId]);

  const submitMemory = (event: FormEvent) => {
    event.preventDefault();
    if (!memoryDraft || savingRef.current || pendingRef.current) return;
    if (!memoryDraft.narrative.trim()) { setSaveError("Write a memory before saving."); return; }
    if (characterCount(memoryDraft.title.trim()) > 160 || characterCount(memoryDraft.narrative) > 12000
      || new TextEncoder().encode(memoryDraft.narrative).length > 48000 || invalidControl(memoryDraft.narrative, true)
      || invalidControl(memoryDraft.title)) {
      setSaveError("Use up to 160 characters for the title and 12,000 for your memory, without hidden control characters."); return;
    }
    if (!page?.current_profile_version_id || scope !== "current") {
      setSaveError("Open the current profile context before saving a memory."); return;
    }
    void performSave({ kind: "memory", requestId: crypto.randomUUID(),
      expectedParentProfileVersionId: page.current_profile_version_id,
      expectedRetentionGeneration: page.retention_generation,
      title: memoryDraft.title.trim() || null, narrative: memoryDraft.narrative });
  };

  const submitProject = (event: FormEvent) => {
    event.preventDefault();
    if (!projectDraft || !selected || savingRef.current || pendingRef.current) return;
    const name = projectDraft.name.trim();
    const description = projectDraft.description.trim();
    if (!name || characterCount(name) > 200 || characterCount(description) > 600
      || invalidControl(name) || invalidControl(description, true)) {
      setSaveError("Give the project a name of up to 200 characters and an optional description of up to 600 characters."); return;
    }
    if (!page?.current_profile_version_id || selected.is_previous_import_set || selected.retention_generation !== page.retention_generation) {
      setSaveError("This memory is not in the active import set. Earlier memories remain available in History."); return;
    }
    void performSave({ kind: "project", requestId: crypto.randomUUID(), memoryId: selected.id,
      expectedParentProfileVersionId: page.current_profile_version_id,
      expectedRetentionGeneration: page.retention_generation, name, description: description || null });
  };

  const beginMemory = () => {
    if (!leaveDraft()) return;
    setScope("current");
    setMemoryDraft({ title: "", narrative: "" });
    setNotice(null);
    window.requestAnimationFrame(() => draftFirstInputRef.current?.focus());
  };
  const beginProject = () => {
    if (!selected || !leaveDraft()) return;
    setProjectDraft({ name: selected.title ?? "", description: "" });
    setNotice(null);
    window.requestAnimationFrame(() => draftFirstInputRef.current?.focus());
  };
  const openSuggestions = () => { if (leaveDraft()) onReviewSuggestions(); };

  return <section className="materials-workspace memory-workspace" aria-labelledby="memory-title">
    <header className="materials-commandbar">
      <div className="materials-commandbar__title">
        <button type="button" className="profile-back-button" onClick={() => { if (leaveDraft()) onBack(); }} disabled={locked}><span aria-hidden="true">←</span> Profile review</button>
        <div><h2 id="memory-title" ref={headingRef} tabIndex={-1}>Memories</h2><span>Career stories, in your own words</span></div>
      </div>
      <div className="commandbar-actions">
        <button type="button" className="quiet-action" onClick={openSuggestions} disabled={locked}>Profile suggestions</button>
        <button type="button" className="secondary-action" onClick={beginMemory} disabled={locked || loading || !page?.current_profile_version_id}>Add memory</button>
      </div>
    </header>
    {notice && <p className="memory-notice" role="status">{notice}</p>}
    {refreshNeeded && <div className="memory-recovery" role="status"><p>{refreshNeeded === "saved" ? "The save is confirmed, but the workspace did not fully refresh." : "The local context did not fully refresh. Your draft has been kept."}</p><button type="button" className="quiet-action" onClick={() => void refreshContext()} disabled={locked || loading}>Refresh workspace</button></div>}
    <div className="memory-layout">
      <aside className="memory-list-pane" aria-label="Saved memories">
        <div className="memory-scopes" aria-label="Memory collection">
          {(["current", "history"] as const).map((value) => <button key={value} type="button" aria-pressed={scope === value} className={scope === value ? "is-selected" : ""}
            disabled={locked} onClick={() => { if (value !== scope && leaveDraft()) { setItems([]); setSelectedId(null); setScope(value); } }}>{value === "current" ? "Current" : "History"}</button>)}
        </div>
        <div className="memory-list-heading"><span>{scope === "current" ? "Saved originals" : "Earlier import sets"}</span><span>{page?.scope === scope ? page.total_items : ""}</span></div>
        {listError && <div className="memory-error" role="alert"><p>{listError}</p><button type="button" className="quiet-action" disabled={locked || loading} onClick={() => void loadPage(scope)}>Try again</button></div>}
        {loading && <p className="memory-list-message" role="status">Opening local memories…</p>}
        {!loading && !listError && items.length === 0 && <p className="memory-list-message">{scope === "current" ? "No memories saved yet." : "No memories in earlier import sets."}</p>}
        <ol className="memory-list">{items.map((item) => <li key={item.id}><button type="button" className={selectedId === item.id && !memoryDraft ? "is-selected" : ""} aria-current={selectedId === item.id && !memoryDraft ? "true" : undefined}
          disabled={locked} onClick={() => { if (leaveDraft()) { setSelectedId(item.id); setNotice(null); } }}>
          <strong>{item.title || "Untitled memory"}</strong><span>{item.excerpt}</span><small>{timestamp(item.created_at_ms)}{item.review_item_id ? " · Suggestion prepared" : ""}</small>
        </button></li>)}</ol>
        {page?.next_offset != null && <button type="button" className="quiet-action memory-load-more" disabled={locked || loading || loadingMore} onClick={() => void loadPage(scope, page.next_offset as number, true)}>{loadingMore ? "Loading…" : "Load older memories"}</button>}
      </aside>
      <div className="memory-document-pane" aria-busy={saving}>
        <div className="memory-document">
          {memoryDraft ? <form onSubmit={submitMemory} className="memory-editor">
            <header><p className="section-kicker">Private, local evidence</p><h3>Add a memory</h3><p>Capture a project, a difficult moment, something you learned, or a result you want to remember. Uncertain dates and details can stay uncertain.</p></header>
            <label htmlFor="memory-draft-title">Title <span>Optional</span></label>
            <input id="memory-draft-title" ref={draftFirstInputRef} value={memoryDraft.title} disabled={locked} onChange={(event) => setMemoryDraft({ ...memoryDraft, title: event.target.value })} placeholder="A name to help you find this later" aria-describedby="memory-title-limit" />
            <small id="memory-title-limit">Up to 160 characters</small>
            <label htmlFor="memory-draft-story">Your memory</label>
            <textarea id="memory-draft-story" value={memoryDraft.narrative} rows={14} disabled={locked} onChange={(event) => setMemoryDraft({ ...memoryDraft, narrative: event.target.value })} placeholder="What was happening? What did you do? What changed?" aria-describedby="memory-story-note memory-story-count" />
            <small id="memory-story-count" className={characterCount(memoryDraft.narrative) > 12000 ? "is-error" : ""}>{characterCount(memoryDraft.narrative).toLocaleString()} / 12,000 characters</small>
            <p id="memory-story-note" className="memory-local-note">Your original words will be saved exactly as entered. Nothing is sent to a model, and saving does not add anything to your public profile.</p>
            <div className="memory-editor-actions"><button type="button" className="quiet-action" onClick={leaveDraft} disabled={locked}>Cancel</button><button type="submit" className="primary-action" disabled={locked || !memoryDraft.narrative.trim() || !page?.current_profile_version_id}>{saving ? "Saving…" : "Save memory"}</button></div>
          </form> : detailLoading ? <div className="memory-empty" role="status"><p>Opening the original memory…</p></div>
            : detailError ? <div className="memory-error" role="alert"><p>{detailError}</p><button type="button" className="secondary-action" disabled={locked} onClick={() => selectedId && void loadDetail(selectedId)}>Try again</button></div>
            : selected ? <>
              <header className="memory-detail-header"><p className="section-kicker">Original memory · {selected.is_previous_import_set ? "History" : "Saved locally"}</p><h3 ref={detailHeadingRef} tabIndex={-1}>{selected.title || "Untitled memory"}</h3><p>{timestamp(selected.created_at_ms)} · {selected.character_count.toLocaleString()} characters</p></header>
              <div className="memory-original" aria-label="Original memory text">{selected.narrative}</div>
              <p className="memory-local-note">This is your saved original. It is not a public profile entry and is not sent to a model. Preparing or editing a suggestion never changes these words.</p>
              {projectDraft ? <form className="memory-editor memory-project-editor" onSubmit={submitProject}>
                <header><p className="section-kicker">You decide what becomes public</p><h3>Prepare a project suggestion</h3><p>Write only the details you want to propose for your profile. Nothing is inferred from the memory. You can refine dates, highlights, and skills in Profile suggestions before applying.</p></header>
                <label htmlFor="memory-project-name">Project name</label><input id="memory-project-name" ref={draftFirstInputRef} value={projectDraft.name} disabled={locked} onChange={(event) => setProjectDraft({ ...projectDraft, name: event.target.value })} aria-describedby="memory-project-name-limit" /><small id="memory-project-name-limit">Up to 200 characters</small>
                <label htmlFor="memory-project-description">Public description <span>Optional</span></label><textarea id="memory-project-description" value={projectDraft.description} rows={5} disabled={locked} onChange={(event) => setProjectDraft({ ...projectDraft, description: event.target.value })} aria-describedby="memory-project-description-count" /><small id="memory-project-description-count" className={characterCount(projectDraft.description) > 600 ? "is-error" : ""}>{characterCount(projectDraft.description).toLocaleString()} / 600 characters</small>
                <p className="memory-local-note">This only queues a review item, linked to the original above. Your profile changes only when you apply the suggestion.</p>
                <div className="memory-editor-actions"><button type="button" className="quiet-action" onClick={leaveDraft} disabled={locked}>Cancel</button><button type="submit" className="primary-action" disabled={locked || !projectDraft.name.trim()}>{saving ? "Queuing…" : "Queue for review"}</button></div>
              </form> : <section className="memory-next-step"><h4>{selected.review_item_id ? "Project suggestion prepared" : selected.is_previous_import_set ? "Kept as history" : "Turn this into a profile suggestion"}</h4><p>{selected.review_item_id ? "The linked suggestion remains in Profile suggestions. Check Inbox, Deferred, or History for its review status." : selected.is_previous_import_set ? "This original belongs to an earlier import set. It remains available here as read-only evidence." : "Choose the parts you want to use in a project entry. The original stays private; nothing enters your profile without review."}</p>
                {selected.review_item_id ? <button type="button" className="secondary-action" onClick={openSuggestions} disabled={locked}>Open Profile suggestions</button> : !selected.is_previous_import_set && <button type="button" className="secondary-action" onClick={beginProject} disabled={locked || !page?.current_profile_version_id}>Prepare project suggestion</button>}
              </section>}
            </> : <div className="memory-empty"><p className="section-kicker">More than a résumé</p><h3>Make room for the stories behind your work.</h3><p>A memory can hold the context that does not fit in a bullet point. Save it in your own words, then choose what belongs in your profile.</p>{scope === "current" && <button type="button" className="secondary-action" onClick={beginMemory} disabled={locked || loading || !page?.current_profile_version_id}>Add your first memory</button>}<small>Local only. No model or API key needed.</small></div>}
          {saveError && <div className="memory-error memory-save-error" ref={saveErrorRef} tabIndex={-1} role="alert"><p>{saveError}</p>{uncertain && pending ? <button type="button" className="secondary-action" disabled={saving} onClick={() => void performSave(pendingRef.current ?? pending)}>{saving ? "Confirming…" : "Retry exact save"}</button> : <button type="button" className="quiet-action" disabled={locked || loading} onClick={() => void refreshContext()}>Refresh local context</button>}</div>}
        </div>
      </div>
    </div>
  </section>;
}
