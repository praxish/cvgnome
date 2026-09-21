// SPDX-License-Identifier: MPL-2.0
import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { useDesktopCloseProtection } from "./DesktopCloseGuard";
import {
  isCloudProvider,
  isProviderVerified,
  providerLabel,
  ProviderSettings,
  safeProviderError,
  TailoredResume,
  TailoredResumeDraft,
  TailoredResumeRevision,
  TailoredResumeRevisionList,
} from "./localProvider";
import {
  formatBytes,
  ResumeExportResult,
  ResumeFormat,
  safeResumeExportError,
} from "./resumeExport";
import {
  formatOpportunityDate,
  formatOpportunityTimestamp,
  OPPORTUNITY_LIMITS,
  OPPORTUNITY_SORT_LABELS,
  OpportunityDetail,
  OpportunityInput,
  OpportunityListItem,
  OpportunityListResult,
  OpportunitySort,
  OpportunityStageFilter,
  OpportunityTrackerStage,
  safeOpportunityError,
  SIGNAL_LABELS,
  STAGE_FILTER_LABELS,
  TRACKER_STAGE_LABELS,
  TRACKER_STAGES,
} from "./opportunities";
import {
  applyTailoredRevisionEdits,
  classifyTailoredRevisionError,
  createTailoredRevisionEditor,
  diffTailoredResumes,
  isTailoredRevisionEditorDirty,
  latestTailoredRevision,
  safeTailoredRevisionGetError,
  safeTailoredRevisionListError,
  safeTailoredRevisionSaveError,
  sortedTailoredRevisions,
  TAILORED_REVISION_LIMITS,
  TailoredRevisionEditorState,
  validateTailoredRevisionEditor,
} from "./tailoredResumeRevisions";

type OpportunitiesPanelProps = {
  hasProfile: boolean;
  workspaceState: "checking" | "ready" | "unavailable";
  currentProfileVersion: number | null;
  initialOpportunityId: string | null;
  onCountChange: (count: number) => void;
  onDirtyChange: (dirty: boolean) => void;
  onSelectedOpportunityChange: (opportunityId: string | null) => void;
  onOpenProfile: () => void;
  onOpenModelSettings: () => void;
  onEngineStatusRefresh: () => Promise<void>;
  onTailoringBusyChange: (busy: boolean) => void;
};

type TailoredExportOperation = {
  actionId: number;
  versionKey: string;
  format: ResumeFormat;
};

type TailoredExportNotice = {
  versionKey: string;
  kind: "success" | "info" | "error";
  message: string;
};

type TailoredRevisionNotice = {
  kind: "success" | "info" | "error";
  message: string;
};

type PostingForm = {
  title: string;
  company: string;
  location: string;
  sourceUrl: string;
  applyUrl: string;
  description: string;
};

const EMPTY_FORM: PostingForm = {
  title: "",
  company: "",
  location: "",
  sourceUrl: "",
  applyUrl: "",
  description: "",
};

const STAGE_FILTERS = Object.keys(STAGE_FILTER_LABELS) as OpportunityStageFilter[];
const SORT_OPTIONS = Object.keys(OPPORTUNITY_SORT_LABELS) as OpportunitySort[];

function optional(value: string): string | undefined {
  const trimmed = value.trim();
  return trimmed || undefined;
}

function jsonStringPayloadBytes(value: string): number {
  return Math.max(0, new TextEncoder().encode(JSON.stringify(value)).length - 2);
}

type ProfileConnection = {
  key: string;
  snippet: string;
  terms: string[];
};

const PROFILE_CONNECTION_SUMMARIES: Record<OpportunityDetail["match"]["signal"], string> = {
  strong: "Your profile includes several examples you could emphasize for this role.",
  mixed: "Your profile includes some relevant examples, with a few parts of the role worth a closer look.",
  limited: "CVGnome found a few clear connections; you may have more relevant experience that is not written down yet.",
};

function groupProfileConnections(
  evidence: OpportunityDetail["match"]["evidence"],
): ProfileConnection[] {
  const groups = new Map<string, ProfileConnection>();

  for (const item of evidence) {
    const key = JSON.stringify([item.profile_path, item.snippet]);
    const existing = groups.get(key);
    if (existing) {
      if (!existing.terms.includes(item.term)) existing.terms.push(item.term);
      continue;
    }
    groups.set(key, {
      key,
      snippet: item.snippet,
      terms: [item.term],
    });
  }

  return Array.from(groups.values());
}

function mergeListItem(current: OpportunityListItem, detail: OpportunityDetail): OpportunityListItem {
  return {
    ...current,
    title: detail.opportunity.title,
    company: detail.opportunity.company,
    location: detail.opportunity.location,
    status: detail.opportunity.status,
    tracker_stage: detail.opportunity.tracker_stage,
    tracker_updated_at_ms: detail.opportunity.tracker_updated_at_ms,
    created_at_ms: detail.opportunity.created_at_ms,
    updated_at_ms: detail.opportunity.updated_at_ms,
    snapshot: {
      id: detail.snapshot.id,
      checksum_sha256: detail.snapshot.checksum_sha256,
      captured_at_ms: detail.snapshot.captured_at_ms,
    },
    match: {
      id: detail.match.id,
      contract: detail.match.contract,
      score: detail.match.score,
      signal: detail.match.signal,
      created_at_ms: detail.match.created_at_ms,
    },
    profile_version: detail.profile_version,
  };
}

function tailoringQualityMessage(code: string): string | null {
  if (code === "embedding_query_compacted") {
    return "Evidence ranking used a bounded head-and-tail view of this long posting. The full saved posting was still supplied to the chat model; review the result closely.";
  }
  if (code === "unsupported_model_copy_removed") {
    return "CVGnome omitted generated wording that it could not ground in your imported evidence. Review the remaining tailored copy before export.";
  }
  return null;
}

export function OpportunitiesPanel({
  hasProfile,
  workspaceState,
  currentProfileVersion,
  initialOpportunityId,
  onCountChange,
  onDirtyChange,
  onSelectedOpportunityChange,
  onOpenProfile,
  onOpenModelSettings,
  onEngineStatusRefresh,
  onTailoringBusyChange,
}: OpportunitiesPanelProps) {
  const [form, setForm] = useState<PostingForm>(EMPTY_FORM);
  const [query, setQuery] = useState("");
  const [stageFilter, setStageFilter] = useState<OpportunityStageFilter>("all");
  const [sort, setSort] = useState<OpportunitySort>("last_action");
  const [items, setItems] = useState<OpportunityListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(initialOpportunityId);
  const [detail, setDetail] = useState<OpportunityDetail | null>(null);
  const [editorMode, setEditorMode] = useState<"detail" | "new">("detail");
  const [trackerStage, setTrackerStage] = useState<OpportunityTrackerStage>("new");
  const [notes, setNotes] = useState("");
  const [loadingList, setLoadingList] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [nextOffset, setNextOffset] = useState(0);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [saving, setSaving] = useState(false);
  const [savingTracker, setSavingTracker] = useState(false);
  const [rematching, setRematching] = useState(false);
  const [providerSettings, setProviderSettings] = useState<ProviderSettings | null>(null);
  const [tailoredDraft, setTailoredDraft] = useState<TailoredResumeDraft | null>(null);
  const [tailoredRevisionList, setTailoredRevisionList] = useState<TailoredResumeRevisionList | null>(null);
  const [tailoredRevisionCache, setTailoredRevisionCache] = useState<Record<string, TailoredResumeRevision>>({});
  const [selectedTailoredRevisionId, setSelectedTailoredRevisionId] = useState<string | null>(null);
  const [loadingTailoredRevisions, setLoadingTailoredRevisions] = useState(false);
  const [loadingSelectedTailoredRevision, setLoadingSelectedTailoredRevision] = useState(false);
  const [tailoredRevisionEditor, setTailoredRevisionEditor] = useState<TailoredRevisionEditorState | null>(null);
  const [savingTailoredRevision, setSavingTailoredRevision] = useState(false);
  const [tailoredRevisionNotice, setTailoredRevisionNotice] = useState<TailoredRevisionNotice | null>(null);
  const [loadingTailoring, setLoadingTailoring] = useState(false);
  const [generatingTailoring, setGeneratingTailoring] = useState(false);
  const [tailoringError, setTailoringError] = useState<string | null>(null);
  const [tailoredExport, setTailoredExport] = useState<TailoredExportOperation | null>(null);
  const [tailoredExportNotice, setTailoredExportNotice] = useState<TailoredExportNotice | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const detailRef = useRef<HTMLElement>(null);
  const titleRef = useRef<HTMLInputElement>(null);
  const listRequestRef = useRef(0);
  const detailRequestRef = useRef(0);
  const tailoringLoadRequestRef = useRef(0);
  const tailoringRevisionLoadRequestRef = useRef(0);
  const tailoringRevisionDetailRequestRef = useRef(0);
  const tailoringGenerationRequestRef = useRef(0);
  const tailoringViewEpochRef = useRef(0);
  const tailoringActionSequenceRef = useRef(0);
  const tailoringActionInFlightRef = useRef<{ id: number; kind: "generate" | "export" | "save-revision" } | null>(null);
  const currentTailoredDraftIdRef = useRef<string | null>(null);
  const currentTailoredVersionKeyRef = useRef<string | null>(null);
  const currentSelectedTailoredRevisionIdRef = useRef<string | null>(null);
  const currentLatestTailoredRevisionIdRef = useRef<string | null>(null);
  const revisionHeadlineRef = useRef<HTMLInputElement>(null);
  const tailoredHistorySelectRef = useRef<HTMLSelectElement>(null);
  const docxExportButtonRef = useRef<HTMLButtonElement>(null);
  const pdfExportButtonRef = useRef<HTMLButtonElement>(null);
  const restoredSelectionRef = useRef(false);

  currentTailoredDraftIdRef.current = tailoredDraft?.id ?? null;
  currentSelectedTailoredRevisionIdRef.current = selectedTailoredRevisionId;

  const trackerDirty = Boolean(
    editorMode === "detail" &&
    detail &&
    (trackerStage !== detail.opportunity.tracker_stage || notes !== detail.opportunity.notes),
  );
  const postingDirty = editorMode === "new" && Object.values(form).some((value) => value.length > 0);
  const tailoredRevisionValidation = useMemo(
    () => tailoredRevisionEditor ? validateTailoredRevisionEditor(tailoredRevisionEditor) : null,
    [tailoredRevisionEditor],
  );
  const tailoredRevisionDirty = Boolean(
    tailoredRevisionEditor && isTailoredRevisionEditorDirty(tailoredRevisionEditor),
  );
  const tailoredRevisionNormalizationOnly = Boolean(
    tailoredRevisionDirty &&
    !tailoredRevisionValidation?.hasChanges &&
    !tailoredRevisionValidation?.errors.length,
  );
  const editorDirty = trackerDirty || postingDirty || tailoredRevisionDirty;
  useDesktopCloseProtection({
    dirty: editorDirty,
    blocked: saving || savingTracker || rematching || generatingTailoring || savingTailoredRevision
      || tailoredExport !== null || Boolean(tailoredRevisionEditor?.requestId),
  });
  const notesBytes = new TextEncoder().encode(notes).length;
  const notesTooLarge = notes.length > OPPORTUNITY_LIMITS.notes || notesBytes > OPPORTUNITY_LIMITS.notes;

  useEffect(() => {
    onDirtyChange(editorDirty);
    return () => onDirtyChange(false);
  }, [editorDirty, onDirtyChange]);

  useEffect(() => {
    if (!editorDirty) return;
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => window.removeEventListener("beforeunload", handleBeforeUnload);
  }, [editorDirty]);

  useEffect(() => () => {
    listRequestRef.current += 1;
    detailRequestRef.current += 1;
    tailoringLoadRequestRef.current += 1;
    tailoringRevisionLoadRequestRef.current += 1;
    tailoringRevisionDetailRequestRef.current += 1;
    tailoringGenerationRequestRef.current += 1;
    tailoringViewEpochRef.current += 1;
    tailoringActionSequenceRef.current += 1;
    tailoringActionInFlightRef.current = null;
    onTailoringBusyChange(false);
  }, [onTailoringBusyChange]);

  const loadTailoredRevision = useCallback(async (
    draftId: string,
    revisionId: string,
  ): Promise<TailoredResumeRevision | null> => {
    const requestId = ++tailoringRevisionDetailRequestRef.current;
    setLoadingSelectedTailoredRevision(true);
    try {
      const result = await invoke<TailoredResumeRevision>("get_tailored_resume_revision", {
        revisionId,
      });
      if (
        requestId !== tailoringRevisionDetailRequestRef.current ||
        currentTailoredDraftIdRef.current !== draftId ||
        currentSelectedTailoredRevisionIdRef.current !== revisionId
      ) return null;
      if (result.id !== revisionId || result.draft_id !== draftId) {
        throw new Error("tailored revision detail mismatch");
      }
      setTailoredRevisionCache((current) => {
        const next: Record<string, TailoredResumeRevision> = { [result.id]: result };
        const latestId = currentLatestTailoredRevisionIdRef.current;
        if (latestId && latestId !== result.id && current[latestId]) {
          next[latestId] = current[latestId];
        }
        return next;
      });
      return result;
    } catch (caught) {
      if (
        requestId === tailoringRevisionDetailRequestRef.current &&
        currentTailoredDraftIdRef.current === draftId &&
        currentSelectedTailoredRevisionIdRef.current === revisionId
      ) {
        currentSelectedTailoredRevisionIdRef.current = null;
        setSelectedTailoredRevisionId(null);
        setTailoredRevisionNotice({
          kind: "error",
          message: `${safeTailoredRevisionGetError(caught)} Showing the generated original instead.`,
        });
      }
      return null;
    } finally {
      if (requestId === tailoringRevisionDetailRequestRef.current) {
        setLoadingSelectedTailoredRevision(false);
      }
    }
  }, []);

  const loadTailoredRevisionHistory = useCallback(async (
    draftId: string,
  ): Promise<{ list: TailoredResumeRevisionList; latest: TailoredResumeRevision | null } | null> => {
    const requestId = ++tailoringRevisionLoadRequestRef.current;
    setLoadingTailoredRevisions(true);
    setTailoredRevisionNotice(null);
    try {
      const result = await invoke<TailoredResumeRevisionList>("list_tailored_resume_revisions", {
        draftId,
      });
      if (
        requestId !== tailoringRevisionLoadRequestRef.current ||
        currentTailoredDraftIdRef.current !== draftId
      ) return null;
      if (
        result.draft_id !== draftId ||
        result.revisions.some((revision) => revision.draft_id !== draftId)
      ) throw new Error("tailored revision history draft mismatch");
      const normalized = {
        ...result,
        revisions: sortedTailoredRevisions(result.revisions),
      };
      setTailoredRevisionList(normalized);
      const latest = latestTailoredRevision(normalized.revisions);
      currentSelectedTailoredRevisionIdRef.current = latest?.id ?? null;
      setSelectedTailoredRevisionId(latest?.id ?? null);
      const latestFull = latest ? await loadTailoredRevision(draftId, latest.id) : null;
      return { list: normalized, latest: latestFull };
    } catch (caught) {
      if (
        requestId === tailoringRevisionLoadRequestRef.current &&
        currentTailoredDraftIdRef.current === draftId
      ) {
        setTailoredRevisionNotice({
          kind: "error",
          message: safeTailoredRevisionListError(caught),
        });
      }
      return null;
    } finally {
      if (requestId === tailoringRevisionLoadRequestRef.current) {
        setLoadingTailoredRevisions(false);
      }
    }
  }, [loadTailoredRevision]);

  useEffect(() => {
    const opportunityId = detail?.opportunity.id;
    const requestId = ++tailoringLoadRequestRef.current;
    tailoringGenerationRequestRef.current += 1;
    tailoringViewEpochRef.current += 1;
    setTailoringError(null);
    setTailoredDraft(null);
    setProviderSettings(null);
    setTailoredExportNotice(null);
    if (!opportunityId || editorMode !== "detail") {
      setLoadingTailoring(false);
      return;
    }
    setLoadingTailoring(true);
    void (async () => {
      const [settingsResult, draftResult] = await Promise.allSettled([
        invoke<ProviderSettings>("get_local_provider_settings"),
        invoke<TailoredResumeDraft | null>("get_latest_tailored_resume", { opportunityId }),
      ]);
      if (requestId !== tailoringLoadRequestRef.current) return;
      if (settingsResult.status === "fulfilled") setProviderSettings(settingsResult.value);
      if (
        draftResult.status === "fulfilled" &&
        (!draftResult.value || draftResult.value.opportunity.id === opportunityId)
      ) {
        setTailoredDraft(draftResult.value);
      }
      const failure = settingsResult.status === "rejected"
        ? settingsResult.reason
        : draftResult.status === "rejected"
          ? draftResult.reason
          : draftResult.status === "fulfilled" && draftResult.value?.opportunity.id !== opportunityId
            ? new Error("tailoring draft opportunity changed")
            : null;
      if (failure) setTailoringError(safeProviderError(failure, settingsResult.status === "fulfilled" ? settingsResult.value.provider_kind : undefined));
      setLoadingTailoring(false);
    })();
  }, [detail?.opportunity.id, editorMode]);

  useEffect(() => {
    const draftId = tailoredDraft?.id;
    tailoringRevisionLoadRequestRef.current += 1;
    tailoringRevisionDetailRequestRef.current += 1;
    setTailoredRevisionList(null);
    setTailoredRevisionCache({});
    currentSelectedTailoredRevisionIdRef.current = null;
    setSelectedTailoredRevisionId(null);
    setTailoredRevisionEditor(null);
    setTailoredRevisionNotice(null);
    setLoadingTailoredRevisions(false);
    setLoadingSelectedTailoredRevision(false);
    if (!draftId) return;
    void loadTailoredRevisionHistory(draftId);
  }, [loadTailoredRevisionHistory, tailoredDraft?.id]);

  const latestTailoredRevisionSummary = useMemo(
    () => latestTailoredRevision(tailoredRevisionList?.revisions ?? []),
    [tailoredRevisionList],
  );
  currentLatestTailoredRevisionIdRef.current = latestTailoredRevisionSummary?.id ?? null;
  const selectedTailoredRevision = selectedTailoredRevisionId
    ? tailoredRevisionCache[selectedTailoredRevisionId] ?? null
    : null;
  const selectedTailoredResume: TailoredResume | null = selectedTailoredRevisionId
    ? selectedTailoredRevision?.resume ?? null
    : tailoredDraft?.resume ?? null;
  const selectedTailoredVersionKey = tailoredDraft
    ? selectedTailoredRevisionId
      ? `revision:${selectedTailoredRevisionId}`
      : `draft:${tailoredDraft.id}`
    : null;
  currentTailoredVersionKeyRef.current = selectedTailoredVersionKey;
  const selectedRevisionIsLatest = selectedTailoredRevisionId !== null
    && selectedTailoredRevisionId === latestTailoredRevisionSummary?.id;
  const generatedOriginalIsLatest = selectedTailoredRevisionId === null
    && latestTailoredRevisionSummary === null;
  const selectedTailoredDiff = useMemo(
    () => tailoredDraft && selectedTailoredRevision
      ? diffTailoredResumes(tailoredDraft.resume, selectedTailoredRevision.resume)
      : [],
    [selectedTailoredRevision, tailoredDraft],
  );

  const loadList = useCallback(async (
    search: string,
    filter: OpportunityStageFilter,
    ordering: OpportunitySort,
    expectedRequestId?: number,
  ) => {
    if (!hasProfile) return;
    const requestId = expectedRequestId ?? ++listRequestRef.current;
    setLoadingList(true);
    setLoadingMore(false);
    try {
      const result = await invoke<OpportunityListResult>("list_opportunities", {
        query: search.trim(),
        offset: 0,
        stage: filter,
        sort: ordering,
      });
      if (requestId !== listRequestRef.current) return;
      setItems(result.items);
      setTotal(result.total);
      setNextOffset(result.items.length);
      if (!search.trim() && filter === "all") onCountChange(result.total);
      setError(null);
    } catch (caught) {
      if (requestId === listRequestRef.current) setError(safeOpportunityError(caught));
    } finally {
      if (requestId === listRequestRef.current) setLoadingList(false);
    }
  }, [hasProfile, onCountChange]);

  useEffect(() => {
    const requestId = ++listRequestRef.current;
    setLoadingMore(false);
    setNextOffset(0);
    if (!hasProfile) {
      setItems([]);
      setTotal(0);
      setLoadingList(false);
      return;
    }
    setItems([]);
    setTotal(0);
    setLoadingList(true);
    const handle = window.setTimeout(
      () => void loadList(query, stageFilter, sort, requestId),
      180,
    );
    return () => {
      window.clearTimeout(handle);
      if (listRequestRef.current === requestId) listRequestRef.current += 1;
    };
  }, [hasProfile, loadList, query, sort, stageFilter]);

  const loadMore = useCallback(async () => {
    if (!hasProfile || loadingList || loadingMore || nextOffset >= total || nextOffset > OPPORTUNITY_LIMITS.listOffset) return;
    const requestId = ++listRequestRef.current;
    const offset = nextOffset;
    setLoadingMore(true);
    setError(null);
    try {
      const result = await invoke<OpportunityListResult>("list_opportunities", {
        query: query.trim(),
        offset,
        stage: stageFilter,
        sort,
      });
      if (requestId !== listRequestRef.current) return;
      setItems((current) => {
        const seen = new Set(current.map((item) => item.id));
        return [...current, ...result.items.filter((item) => !seen.has(item.id))];
      });
      setTotal(result.total);
      setNextOffset(result.items.length > 0 ? offset + result.items.length : result.total);
      if (!query.trim() && stageFilter === "all") onCountChange(result.total);
      setAnnouncement(result.items.length ? `${result.items.length} more roles loaded.` : "All roles are loaded.");
    } catch (caught) {
      if (requestId === listRequestRef.current) setError(safeOpportunityError(caught));
    } finally {
      if (requestId === listRequestRef.current) setLoadingMore(false);
    }
  }, [hasProfile, loadingList, loadingMore, nextOffset, onCountChange, query, sort, stageFilter, total]);

  const openDetail = useCallback((result: OpportunityDetail) => {
    setDetail(result);
    setSelectedId(result.opportunity.id);
    onSelectedOpportunityChange(result.opportunity.id);
    setTrackerStage(result.opportunity.tracker_stage);
    setNotes(result.opportunity.notes);
    setEditorMode("detail");
    window.requestAnimationFrame(() => detailRef.current?.focus());
  }, [onSelectedOpportunityChange]);

  const confirmDiscardEditorChanges = useCallback(() => {
    if (!editorDirty) {
      setTailoredRevisionEditor(null);
      return true;
    }
    const confirmed = window.confirm(editorMode === "new"
      ? "Discard this unsaved role draft?"
      : tailoredRevisionDirty && trackerDirty
        ? "Discard the unsaved tracker and resume revision changes?"
        : tailoredRevisionDirty
          ? "Discard this unsaved resume revision?"
          : "Discard unsaved tracker changes?");
    if (!confirmed) return false;
    setTailoredRevisionEditor(null);
    setTailoredRevisionNotice(null);
    if (editorMode === "new") {
      setForm(EMPTY_FORM);
    } else if (detail) {
      setTrackerStage(detail.opportunity.tracker_stage);
      setNotes(detail.opportunity.notes);
    }
    return true;
  }, [detail, editorDirty, editorMode, tailoredRevisionDirty, trackerDirty]);

  const beginNewOpportunity = useCallback(() => {
    if (saving || savingTracker || tailoringActionInFlightRef.current) return;
    if (editorMode === "new") {
      window.requestAnimationFrame(() => titleRef.current?.focus());
      return;
    }
    if (!confirmDiscardEditorChanges()) return;
    detailRequestRef.current += 1;
    setLoadingDetail(false);
    setEditorMode("new");
    setSelectedId(null);
    setError(null);
    window.requestAnimationFrame(() => titleRef.current?.focus());
  }, [confirmDiscardEditorChanges, editorMode, saving, savingTracker]);

  const cancelNewOpportunity = useCallback(() => {
    if (saving) return;
    if (!confirmDiscardEditorChanges()) return;
    detailRequestRef.current += 1;
    setLoadingDetail(false);
    setEditorMode("detail");
    setSelectedId(detail?.opportunity.id ?? null);
    setError(null);
    window.requestAnimationFrame(() => detailRef.current?.focus());
  }, [confirmDiscardEditorChanges, detail?.opportunity.id, saving]);

  const selectOpportunity = useCallback(async (opportunityId: string) => {
    if (saving || savingTracker || tailoringActionInFlightRef.current) return;
    if (editorMode === "detail" && selectedId === opportunityId && detail?.opportunity.id === opportunityId) return;
    if (!confirmDiscardEditorChanges()) return;
    const requestId = ++detailRequestRef.current;
    setSelectedId(opportunityId);
    setEditorMode("detail");
    setLoadingDetail(true);
    setError(null);
    try {
      const result = await invoke<OpportunityDetail>("get_opportunity", { opportunityId });
      if (requestId !== detailRequestRef.current) return;
      openDetail(result);
      setAnnouncement(`${result.opportunity.title} opened.`);
    } catch (caught) {
      if (requestId !== detailRequestRef.current) return;
      setDetail(null);
      setSelectedId(null);
      onSelectedOpportunityChange(null);
      setError(safeOpportunityError(caught));
    } finally {
      if (requestId === detailRequestRef.current) setLoadingDetail(false);
    }
  }, [confirmDiscardEditorChanges, detail?.opportunity.id, editorMode, onSelectedOpportunityChange, openDetail, saving, savingTracker, selectedId]);

  useEffect(() => {
    if (restoredSelectionRef.current || !hasProfile || !initialOpportunityId) return;
    restoredSelectionRef.current = true;
    void selectOpportunity(initialOpportunityId);
  }, [hasProfile, initialOpportunityId, selectOpportunity]);

  const savePosting = useCallback(async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!hasProfile || saving) return;
    const description = form.description.trim();
    if (
      new TextEncoder().encode(description).length > OPPORTUNITY_LIMITS.descriptionBytes ||
      jsonStringPayloadBytes(description) > OPPORTUNITY_LIMITS.descriptionBytes
    ) {
      setError("The posting text exceeds the 32 KiB local matching boundary. Shorten it and try again.");
      return;
    }
    const input: OpportunityInput = {
      title: form.title.trim(),
      description,
      company: optional(form.company),
      location: optional(form.location),
      source_url: optional(form.sourceUrl),
      apply_url: optional(form.applyUrl),
    };
    setSaving(true);
    setError(null);
    try {
      const result = await invoke<OpportunityDetail>("save_and_match_opportunity", { input });
      setForm(EMPTY_FORM);
      setQuery("");
      setStageFilter("all");
      setSort("last_action");
      openDetail(result);
      setAnnouncement(`${result.opportunity.title} was saved and checked locally.`);
      await loadList("", "all", "last_action");
    } catch (caught) {
      setError(safeOpportunityError(caught));
      window.requestAnimationFrame(() => titleRef.current?.focus());
    } finally {
      setSaving(false);
    }
  }, [form, hasProfile, loadList, openDetail, saving]);

  const rematch = useCallback(async () => {
    if (!detail || rematching) return;
    const requestId = ++detailRequestRef.current;
    setRematching(true);
    setError(null);
    try {
      const result = await invoke<OpportunityDetail>("rematch_opportunity", { opportunityId: detail.opportunity.id });
      if (requestId !== detailRequestRef.current) return;
      openDetail(result);
      setItems((current) => current.map((item) => item.id === result.opportunity.id ? mergeListItem(item, result) : item));
      setAnnouncement(`${result.opportunity.title} now uses profile version ${result.profile_version.version_number}.`);
    } catch (caught) {
      if (requestId === detailRequestRef.current) setError(safeOpportunityError(caught));
    } finally {
      setRematching(false);
    }
  }, [detail, openDetail, rematching]);

  const saveTracker = useCallback(async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!detail || savingTracker || rematching) return;
    if (notesTooLarge) {
      setError("Keep tracker notes within 4,000 UTF-8 bytes.");
      return;
    }
    const requestId = detailRequestRef.current;
    setSavingTracker(true);
    setError(null);
    try {
      const result = await invoke<OpportunityDetail>("update_opportunity_tracker", {
        opportunityId: detail.opportunity.id,
        expectedTrackerUpdatedAtMs: detail.opportunity.tracker_updated_at_ms,
        trackerStage,
        notes,
      });
      if (requestId !== detailRequestRef.current) return;
      openDetail(result);
      setItems((current) => current.map((item) => item.id === result.opportunity.id ? mergeListItem(item, result) : item));
      setAnnouncement(`Tracker updated for ${result.opportunity.title}.`);
      void loadList(query, stageFilter, sort);
    } catch (caught) {
      if (requestId === detailRequestRef.current) {
        setError(safeOpportunityError(caught));
        setAnnouncement("The tracker update was not saved.");
      }
    } finally {
      setSavingTracker(false);
    }
  }, [detail, loadList, notes, notesTooLarge, openDetail, query, rematching, savingTracker, sort, stageFilter, trackerStage]);

  const selectTailoredVersion = useCallback(async (revisionId: string | null) => {
    const draft = tailoredDraft;
    if (!draft || tailoringActionInFlightRef.current || tailoredRevisionEditor) return;
    tailoringRevisionDetailRequestRef.current += 1;
    currentSelectedTailoredRevisionIdRef.current = revisionId;
    setSelectedTailoredRevisionId(revisionId);
    setLoadingSelectedTailoredRevision(false);
    setTailoredRevisionNotice(null);
    setTailoredExportNotice(null);
    if (revisionId === null) {
      setTailoredRevisionCache((current) => {
        const latestId = currentLatestTailoredRevisionIdRef.current;
        return latestId && current[latestId] ? { [latestId]: current[latestId] } : {};
      });
      return;
    }
    if (tailoredRevisionCache[revisionId]) {
      setTailoredRevisionCache((current) => {
        const next: Record<string, TailoredResumeRevision> = {
          [revisionId]: current[revisionId],
        };
        const latestId = currentLatestTailoredRevisionIdRef.current;
        if (latestId && latestId !== revisionId && current[latestId]) {
          next[latestId] = current[latestId];
        }
        return next;
      });
      return;
    }
    await loadTailoredRevision(draft.id, revisionId);
  }, [loadTailoredRevision, tailoredDraft, tailoredRevisionCache, tailoredRevisionEditor]);

  const beginTailoredRevisionEdit = useCallback(() => {
    const draft = tailoredDraft;
    const selectedResume = selectedTailoredResume;
    if (
      !draft ||
      !selectedResume ||
      !tailoredRevisionList ||
      loadingTailoredRevisions ||
      loadingSelectedTailoredRevision ||
      tailoringActionInFlightRef.current ||
      (!selectedRevisionIsLatest && !generatedOriginalIsLatest)
    ) return;
    const parentRevision = selectedRevisionIsLatest ? selectedTailoredRevision : null;
    const parentChecksum = parentRevision?.resume_checksum_sha256
      ?? (generatedOriginalIsLatest ? tailoredRevisionList.base_resume_checksum_sha256 : null);
    if (!parentChecksum) return;
    setTailoredRevisionEditor(createTailoredRevisionEditor(
      selectedResume,
      parentRevision?.id ?? null,
      parentChecksum,
    ));
    setTailoredRevisionNotice(null);
    setTailoredExportNotice(null);
    window.requestAnimationFrame(() => revisionHeadlineRef.current?.focus());
  }, [
    generatedOriginalIsLatest,
    loadingSelectedTailoredRevision,
    loadingTailoredRevisions,
    selectedRevisionIsLatest,
    selectedTailoredResume,
    selectedTailoredRevision,
    tailoredDraft,
    tailoredRevisionList,
  ]);

  const cancelTailoredRevisionEdit = useCallback(() => {
    if (!tailoredRevisionEditor || savingTailoredRevision) return;
    if (
      tailoredRevisionDirty &&
      !window.confirm("Discard this unsaved resume revision?")
    ) return;
    setTailoredRevisionEditor(null);
    setTailoredRevisionNotice(null);
    setAnnouncement("Resume revision editing cancelled. The selected version is unchanged.");
    window.requestAnimationFrame(() => tailoredHistorySelectRef.current?.focus());
  }, [savingTailoredRevision, tailoredRevisionDirty, tailoredRevisionEditor]);

  const updateTailoredRevisionEditor = useCallback((
    update: (current: TailoredRevisionEditorState) => TailoredRevisionEditorState,
  ) => {
    setTailoredRevisionEditor((current) => current
      ? { ...update(current), requestId: null }
      : current);
    setTailoredRevisionNotice(null);
  }, []);

  const saveTailoredRevision = useCallback(async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const draft = tailoredDraft;
    const editor = tailoredRevisionEditor;
    if (!draft || !editor || savingTailoredRevision || tailoringActionInFlightRef.current) return;
    const validation = validateTailoredRevisionEditor(editor);
    if (!validation.edits || validation.errors.length > 0) return;

    const edits = validation.edits;
    const requestId = editor.requestId ?? crypto.randomUUID();
    if (editor.requestId === null) {
      setTailoredRevisionEditor((current) => current === editor
        ? { ...current, requestId }
        : current);
    }
    const actionId = ++tailoringActionSequenceRef.current;
    const viewEpoch = tailoringViewEpochRef.current;
    tailoringActionInFlightRef.current = { id: actionId, kind: "save-revision" };
    onTailoringBusyChange(true);
    setSavingTailoredRevision(true);
    setTailoredRevisionNotice(null);
    setTailoredExportNotice(null);
    setAnnouncement("Saving this user-authored revision locally…");

    try {
      const result = await invoke<TailoredResumeRevision>("save_tailored_resume_revision", {
        requestId,
        draftId: draft.id,
        expectedParentRevisionId: editor.expectedParentRevisionId,
        expectedParentResumeChecksumSha256: editor.expectedParentResumeChecksumSha256,
        edits,
      });
      if (
        tailoringActionInFlightRef.current?.id !== actionId ||
        tailoringViewEpochRef.current !== viewEpoch ||
        currentTailoredDraftIdRef.current !== draft.id
      ) return;
      if (
        result.draft_id !== draft.id ||
        result.parent_revision_id !== editor.expectedParentRevisionId ||
        result.parent_resume_checksum_sha256 !== editor.expectedParentResumeChecksumSha256
      ) throw new Error("tailored revision save result mismatch");
      const { changed_fields: changedFields, resume: _resume, ...summaryFields } = result;
      const summary = {
        ...summaryFields,
        changed_field_count: changedFields.length,
      };
      setTailoredRevisionList((current) => {
        if (!current) return current;
        const revisions = sortedTailoredRevisions([
          ...current.revisions.filter((revision) => revision.id !== result.id),
          summary,
        ]);
        const truncated = current.truncated
          || revisions.length > TAILORED_REVISION_LIMITS.history;
        return {
          ...current,
          revisions: revisions.slice(-TAILORED_REVISION_LIMITS.history),
          truncated,
        };
      });
      currentLatestTailoredRevisionIdRef.current = result.id;
      currentSelectedTailoredRevisionIdRef.current = result.id;
      setTailoredRevisionCache({ [result.id]: result });
      setSelectedTailoredRevisionId(result.id);
      setTailoredRevisionEditor(null);
      setTailoredRevisionNotice({
        kind: "success",
        message: `User revision ${result.revision_number} was saved locally. Its wording has not been re-verified against profile evidence.`,
      });
      setAnnouncement(`User revision ${result.revision_number} was saved and selected.`);
      window.requestAnimationFrame(() => tailoredHistorySelectRef.current?.focus());
    } catch (caught) {
      if (
        tailoringActionInFlightRef.current?.id !== actionId ||
        tailoringViewEpochRef.current !== viewEpoch ||
        currentTailoredDraftIdRef.current !== draft.id
      ) return;
      const kind = classifyTailoredRevisionError(caught);
      if (kind === "conflict") {
        setTailoredRevisionEditor((current) => current ? { ...current, requestId: null } : current);
        const reloaded = await loadTailoredRevisionHistory(draft.id);
        if (
          tailoringActionInFlightRef.current?.id !== actionId ||
          tailoringViewEpochRef.current !== viewEpoch ||
          currentTailoredDraftIdRef.current !== draft.id
        ) return;
        const latest = reloaded?.latest ?? null;
        const canUseGeneratedOriginal = Boolean(reloaded && reloaded.list.revisions.length === 0);
        const parentResume = latest?.resume ?? (canUseGeneratedOriginal ? draft.resume : null);
        const parentChecksum = latest?.resume_checksum_sha256
          ?? (canUseGeneratedOriginal ? reloaded?.list.base_resume_checksum_sha256 ?? null : null);
        if (parentResume && parentChecksum) {
          const rebasedResume = applyTailoredRevisionEdits(parentResume, edits);
          const rebasedEditor = createTailoredRevisionEditor(
            rebasedResume,
            latest?.id ?? null,
            parentChecksum,
          );
          setTailoredRevisionEditor({ ...rebasedEditor, parentResume });
          setTailoredRevisionNotice({
            kind: "info",
            message: "A newer local revision was found. Your edits were carried onto it; review them before saving again.",
          });
          window.requestAnimationFrame(() => revisionHeadlineRef.current?.focus());
        } else {
          setTailoredRevisionNotice({
            kind: "error",
            message: "A newer local revision exists, but CVGnome could not reopen it. Your edits remain in the editor; reload history before retrying.",
          });
        }
      } else {
        if (kind !== "ambiguous") {
          setTailoredRevisionEditor((current) => current ? { ...current, requestId: null } : current);
        }
        setTailoredRevisionNotice({ kind: "error", message: safeTailoredRevisionSaveError(caught) });
      }
      setAnnouncement("");
    } finally {
      if (tailoringActionInFlightRef.current?.id === actionId) {
        tailoringActionInFlightRef.current = null;
        setSavingTailoredRevision(false);
        onTailoringBusyChange(false);
      }
    }
  }, [
    loadTailoredRevisionHistory,
    onTailoringBusyChange,
    savingTailoredRevision,
    tailoredDraft,
    tailoredRevisionEditor,
  ]);

  const generateTailoredDraft = useCallback(async () => {
    if (
      !detail ||
      generatingTailoring ||
      trackerDirty ||
      rematching ||
      !providerSettings ||
      !isProviderVerified(providerSettings) ||
      tailoredRevisionEditor ||
      tailoringActionInFlightRef.current
    ) return;
    const cloud = isCloudProvider(providerSettings.provider_kind);
    if (cloud && !window.confirm(
      "Send this saved job posting and bounded public resume evidence to OpenAI? CVGnome removes name, email, phone, location, URL, and profile links from baseline basics before sending. The requests may charge your OpenAI API account; no fallback provider will be used.",
    )) {
      setAnnouncement("OpenAI draft cancelled. Nothing was sent.");
      return;
    }
    const actionId = ++tailoringActionSequenceRef.current;
    tailoringActionInFlightRef.current = { id: actionId, kind: "generate" };
    const requestId = ++tailoringGenerationRequestRef.current;
    const opportunityId = detail.opportunity.id;
    onTailoringBusyChange(true);
    setGeneratingTailoring(true);
    setTailoringError(null);
    setTailoredExportNotice(null);
    setAnnouncement(
      cloud
        ? "Sending the bounded embedding batch, then the draft request directly to OpenAI."
        : "Using the embedding model, then the chat model. LM Studio may load each model in turn.",
    );
    try {
      const result = await invoke<TailoredResumeDraft>("generate_tailored_resume", { opportunityId });
      if (requestId !== tailoringGenerationRequestRef.current) return;
      setTailoredDraft(result);
      setAnnouncement(`A tailored draft for ${result.opportunity.title} is stored locally and ready to review.`);
    } catch (caught) {
      if (requestId === tailoringGenerationRequestRef.current) {
        setTailoringError(safeProviderError(caught, providerSettings.provider_kind));
        setAnnouncement("The tailored draft was not created. No automatic retry or fallback was attempted.");
      }
    } finally {
      if (tailoringActionInFlightRef.current?.id === actionId) {
        tailoringActionInFlightRef.current = null;
        setGeneratingTailoring(false);
        onTailoringBusyChange(false);
      }
    }
  }, [detail, generatingTailoring, onTailoringBusyChange, providerSettings, rematching, tailoredRevisionEditor, trackerDirty]);

  const exportTailoredDraft = useCallback(async (format: ResumeFormat) => {
    const draft = tailoredDraft;
    const revision = selectedTailoredRevision;
    const versionKey = selectedTailoredVersionKey;
    if (
      !draft ||
      !versionKey ||
      loadingTailoring ||
      loadingSelectedTailoredRevision ||
      (selectedTailoredRevisionId !== null && !revision) ||
      tailoringActionInFlightRef.current
    ) return;

    const actionId = ++tailoringActionSequenceRef.current;
    const viewEpoch = tailoringViewEpochRef.current;
    const operation: TailoredExportOperation = {
      actionId,
      versionKey,
      format,
    };
    tailoringActionInFlightRef.current = { id: actionId, kind: "export" };
    onTailoringBusyChange(true);
    setTailoredExport(operation);
    setTailoredExportNotice(null);
    const versionLabel = revision ? `user revision ${revision.revision_number}` : "generated original";
    setAnnouncement(`Creating a ${format.toUpperCase()} copy of the exact ${versionLabel}…`);

    try {
      const result = revision
        ? await invoke<ResumeExportResult | null>("export_tailored_resume_revision", {
            revisionId: revision.id,
            format,
          })
        : await invoke<ResumeExportResult | null>("export_tailored_resume", {
            draftId: draft.id,
            format,
          });
      if (
        tailoringActionInFlightRef.current?.id !== actionId ||
        tailoringViewEpochRef.current !== viewEpoch ||
        currentTailoredVersionKeyRef.current !== versionKey
      ) return;
      if (result === null) {
        const message = `Save cancelled. No copy of ${versionLabel} was saved outside CVGnome; the local version is unchanged.`;
        setTailoredExportNotice({ versionKey, kind: "info", message });
        setAnnouncement(message);
      } else {
        if (result.format !== format) throw new Error("tailored export format mismatch");
        const filename = result.saved_filename ?? result.suggested_filename;
        const message = `Saved ${filename} (${formatBytes(result.byte_size)}) from ${versionLabel}.`;
        setTailoredExportNotice({ versionKey, kind: "success", message });
        setAnnouncement(message);
      }
    } catch (caught) {
      if (
        tailoringActionInFlightRef.current?.id === actionId &&
        tailoringViewEpochRef.current === viewEpoch &&
        currentTailoredVersionKeyRef.current === versionKey
      ) {
        setTailoredExportNotice({
          versionKey,
          kind: "error",
          message: safeResumeExportError(caught, format, revision ? "tailored-revision" : "tailored-draft"),
        });
        setAnnouncement("");
      }
    } finally {
      await onEngineStatusRefresh().catch(() => undefined);
      if (tailoringActionInFlightRef.current?.id === actionId) {
        tailoringActionInFlightRef.current = null;
        setTailoredExport((current) => current?.actionId === actionId ? null : current);
        onTailoringBusyChange(false);
        window.requestAnimationFrame(() => {
          if (
            tailoringViewEpochRef.current !== viewEpoch ||
            currentTailoredDraftIdRef.current !== draft.id ||
            currentTailoredVersionKeyRef.current !== versionKey
          ) return;
          (format === "docx" ? docxExportButtonRef : pdfExportButtonRef).current?.focus();
        });
      }
    }
  }, [
    loadingSelectedTailoredRevision,
    loadingTailoring,
    onEngineStatusRefresh,
    onTailoringBusyChange,
    selectedTailoredRevision,
    selectedTailoredRevisionId,
    selectedTailoredVersionKey,
    tailoredDraft,
  ]);

  if (workspaceState !== "ready") {
    const checkingWorkspace = workspaceState === "checking";
    return (
      <section
        className="empty-document empty-document--roles"
        aria-labelledby="roles-workspace-title"
        aria-busy={checkingWorkspace}
      >
        <div className="empty-document__glyph" aria-hidden="true">{checkingWorkspace ? "…" : "!"}</div>
        <h2 id="roles-workspace-title">
          {checkingWorkspace ? "Checking your local workspace" : "Local workspace needs attention"}
        </h2>
        <p>
          {checkingWorkspace
            ? "CVGnome is checking the private vault on this computer."
            : "CVGnome could not confirm your profile or saved roles from the local vault."}
        </p>
        {!checkingWorkspace && (
          <button
            type="button"
            className="secondary-action"
            onClick={() => void onEngineStatusRefresh()}
          >
            Check again
          </button>
        )}
      </section>
    );
  }

  if (!hasProfile) {
    return (
      <section className="empty-document empty-document--roles" aria-labelledby="roles-empty-title">
        <div className="empty-document__glyph" aria-hidden="true">⌁</div>
        <h2 id="roles-empty-title">Create a profile first</h2>
        <p>Roles are checked against explicit evidence in your current local profile.</p>
        <button type="button" className="primary-action" onClick={onOpenProfile}>
          Build your profile
        </button>
      </section>
    );
  }

  const isStale = Boolean(detail && currentProfileVersion !== null && detail.profile_version.version_number < currentProfileVersion);
  const profileConnections = detail ? groupProfileConnections(detail.match.evidence) : [];
  const providerVerified = isProviderVerified(providerSettings);
  const cloudProvider = isCloudProvider(providerSettings?.provider_kind);
  const configuredProviderLabel = providerLabel(providerSettings?.provider_kind);
  const generationProgressLabel = cloudProvider ? "Sending to OpenAI…" : "Embedding, then drafting…";
  const tailoredDraftProfileIsStale = Boolean(
    tailoredDraft && currentProfileVersion !== null && tailoredDraft.profile_version.version_number < currentProfileVersion,
  );
  const tailoredDraftPostingIsStale = Boolean(
    tailoredDraft && detail && (
      tailoredDraft.snapshot.id !== detail.snapshot.id ||
      tailoredDraft.snapshot.checksum_sha256 !== detail.snapshot.checksum_sha256
    ),
  );
  const tailoringActionBusy = generatingTailoring || savingTailoredRevision || tailoredExport !== null;
  const visibleTailoredExportNotice = tailoredExportNotice?.versionKey === selectedTailoredVersionKey
    ? tailoredExportNotice
    : null;
  const selectedTailoredVersionLabel = selectedTailoredRevision
    ? `user revision ${selectedTailoredRevision.revision_number}`
    : "generated original";
  const canEditSelectedTailoredVersion = Boolean(
    tailoredRevisionList &&
    selectedTailoredResume &&
    !loadingTailoredRevisions &&
    !loadingSelectedTailoredRevision &&
    (selectedRevisionIsLatest || generatedOriginalIsLatest),
  );
  const tailoredExportDescriptionIds = [
    "tailored-export-context",
    tailoredDraftProfileIsStale ? "tailored-profile-stale-warning" : null,
    tailoredDraftPostingIsStale ? "tailored-posting-stale-warning" : null,
  ].filter((value): value is string => Boolean(value)).join(" ");
  const normalizedDescription = form.description.trim();
  const descriptionBytes = new TextEncoder().encode(normalizedDescription).length;
  const descriptionPayloadBytes = jsonStringPayloadBytes(normalizedDescription);
  const descriptionTooLarge = descriptionBytes > OPPORTUNITY_LIMITS.descriptionBytes || descriptionPayloadBytes > OPPORTUNITY_LIMITS.descriptionBytes;
  const canLoadMore = nextOffset < total && nextOffset <= OPPORTUNITY_LIMITS.listOffset;

  return (
    <section className="roles-workspace" aria-label="Roles workspace">
      <p className="sr-only" role="status" aria-live="polite">{announcement}</p>
      <p id="tailoring-navigation-lock" className="sr-only">
        Role navigation is unavailable until the current resume operation finishes.
      </p>

      <aside className="roles-list-pane" aria-label="Saved roles">
        <header className="pane-header">
          <div><p className="section-kicker">Application workflow</p><h2>Roles</h2></div>
          <button type="button" className="icon-action" title="Add role" aria-label="Add role" aria-describedby={tailoringActionBusy ? "tailoring-navigation-lock" : undefined} onClick={beginNewOpportunity} disabled={saving || savingTracker || tailoringActionBusy}><span aria-hidden="true">＋</span></button>
        </header>

        <div className="roles-controls">
          <label className="search-field">
            <span className="sr-only">Search saved roles</span>
            <svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="8.5" cy="8.5" r="5.5"/><path d="m12.5 12.5 4 4"/></svg>
            <input type="search" value={query} maxLength={OPPORTUNITY_LIMITS.search} onChange={(event) => setQuery(event.target.value)} placeholder="Search roles" />
          </label>
          <div className="compact-selects">
            <label><span className="sr-only">Filter by workflow stage</span><select value={stageFilter} onChange={(event) => setStageFilter(event.target.value as OpportunityStageFilter)}>{STAGE_FILTERS.map((value) => <option key={value} value={value}>{STAGE_FILTER_LABELS[value]}</option>)}</select></label>
            <label><span className="sr-only">Sort roles</span><select value={sort} onChange={(event) => setSort(event.target.value as OpportunitySort)}>{SORT_OPTIONS.map((value) => <option key={value} value={value}>{OPPORTUNITY_SORT_LABELS[value]}</option>)}</select></label>
          </div>
        </div>

        <div className="pane-list-meta" role="status"><span>{total} {total === 1 ? "role" : "roles"}</span>{loadingList && <span>Updating…</span>}</div>
        {error && <p className="pane-error pane-error--compact" role="alert">{error}</p>}

        <div className="role-list" aria-busy={loadingList || loadingMore}>
          {!loadingList && items.length === 0 && <div className="pane-placeholder"><strong>No matching roles</strong><span>Adjust the filters or add a job posting.</span></div>}
          {items.map((item) => {
            const stale = currentProfileVersion !== null && item.profile_version.version_number < currentProfileVersion;
            return (
              <button key={item.id} type="button" className={`role-row${selectedId === item.id ? " role-row--selected" : ""}`} onClick={() => void selectOpportunity(item.id)} aria-pressed={selectedId === item.id} aria-describedby={tailoringActionBusy ? "tailoring-navigation-lock" : undefined} disabled={saving || savingTracker || tailoringActionBusy}>
                <span className="role-row__topline"><strong>{item.title}</strong><span className={`signal-dot signal-dot--${item.match.signal}`} title={SIGNAL_LABELS[item.match.signal]} aria-hidden="true" /><span className="sr-only">, {SIGNAL_LABELS[item.match.signal]}</span></span>
                <span>{[item.company, item.location].filter(Boolean).join(" · ") || "Details not supplied"}</span>
                <span className="role-row__meta"><span className={`stage-pill stage-pill--${item.tracker_stage}`}>{TRACKER_STAGE_LABELS[item.tracker_stage]}</span><small>{formatOpportunityDate(item.tracker_updated_at_ms)}{stale ? " · Profile changed" : ""}</small></span>
              </button>
            );
          })}
          {!loadingList && canLoadMore && <button type="button" className="load-more-row" onClick={() => void loadMore()} disabled={loadingMore}>{loadingMore ? "Loading…" : `Load more (${total - nextOffset})`}</button>}
        </div>
      </aside>

      <div className="role-work-pane">
        {editorMode === "new" && (
          <form className="document-form" onSubmit={savePosting} aria-busy={saving}>
            <header className="document-header document-header--compact"><div><p className="section-kicker">New local snapshot</p><h2>Add a role</h2><p>Paste the posting once. CVGnome keeps the snapshot and checks it locally.</p></div></header>
            <div className="form-grid">
              <label className="field field--wide"><span>Role title *</span><input ref={titleRef} required maxLength={OPPORTUNITY_LIMITS.title} value={form.title} onChange={(event) => setForm((current) => ({ ...current, title: event.target.value }))} placeholder="Senior Data Analyst" /></label>
              <label className="field"><span>Company</span><input maxLength={OPPORTUNITY_LIMITS.company} value={form.company} onChange={(event) => setForm((current) => ({ ...current, company: event.target.value }))} /></label>
              <label className="field"><span>Location</span><input maxLength={OPPORTUNITY_LIMITS.location} value={form.location} onChange={(event) => setForm((current) => ({ ...current, location: event.target.value }))} /></label>
              <label className="field"><span>Posting URL</span><input type="url" maxLength={OPPORTUNITY_LIMITS.url} value={form.sourceUrl} onChange={(event) => setForm((current) => ({ ...current, sourceUrl: event.target.value }))} placeholder="https://…" /></label>
              <label className="field"><span>Apply URL</span><input type="url" maxLength={OPPORTUNITY_LIMITS.url} value={form.applyUrl} onChange={(event) => setForm((current) => ({ ...current, applyUrl: event.target.value }))} placeholder="https://…" /></label>
              <label className="field field--wide"><span>Posting text *</span><textarea required maxLength={OPPORTUNITY_LIMITS.description} rows={14} value={form.description} onChange={(event) => setForm((current) => ({ ...current, description: event.target.value }))} placeholder="Paste responsibilities and requirements…" /><small>{descriptionBytes.toLocaleString()} / {OPPORTUNITY_LIMITS.descriptionBytes.toLocaleString()} UTF-8 bytes · URLs are never fetched.</small></label>
            </div>
            <div className="document-actions"><button type="button" className="secondary-action" onClick={cancelNewOpportunity} disabled={saving}>Cancel</button><button type="submit" className="primary-action" disabled={saving || descriptionTooLarge}>{saving ? "Saving & checking…" : "Save & find connections"}</button></div>
          </form>
        )}

        {editorMode === "detail" && loadingDetail && <div className="empty-document"><p>Opening local snapshot…</p></div>}
        {editorMode === "detail" && !loadingDetail && !detail && <div className="empty-document"><div className="empty-document__glyph" aria-hidden="true">⌁</div><h2>Select a role</h2><p>Review profile connections, workflow stage, notes, and the saved posting snapshot.</p><button type="button" className="primary-action" onClick={beginNewOpportunity} aria-describedby={tailoringActionBusy ? "tailoring-navigation-lock" : undefined} disabled={tailoringActionBusy}>Add your first role</button></div>}

        {editorMode === "detail" && !loadingDetail && detail && (
          <article className="role-document" ref={detailRef} tabIndex={-1} aria-labelledby="role-document-title">
            <header className="document-header"><div><p className="section-kicker">Saved role · Last action {formatOpportunityTimestamp(detail.opportunity.tracker_updated_at_ms)}</p><h2 id="role-document-title">{detail.opportunity.title}</h2><p>{[detail.opportunity.company, detail.opportunity.location].filter(Boolean).join(" · ") || "Company and location not supplied"}</p></div><span className={`stage-pill stage-pill--${detail.opportunity.tracker_stage}`}>{TRACKER_STAGE_LABELS[detail.opportunity.tracker_stage]}</span></header>

            <form className="tracker-editor" onSubmit={saveTracker} aria-busy={savingTracker || rematching}>
              <label className="field"><span>Workflow stage</span><select value={trackerStage} onChange={(event) => setTrackerStage(event.target.value as OpportunityTrackerStage)} disabled={savingTracker || rematching}>{TRACKER_STAGES.map((value) => <option key={value} value={value}>{TRACKER_STAGE_LABELS[value]}</option>)}</select></label>
              <label className="field field--wide"><span>Private notes</span><textarea rows={5} maxLength={OPPORTUNITY_LIMITS.notes} value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="Follow-ups, contacts, interview notes…" disabled={savingTracker || rematching} /><small>{notesBytes.toLocaleString()} / {OPPORTUNITY_LIMITS.notes.toLocaleString()} UTF-8 bytes</small></label>
              <div className="tracker-actions"><span>{trackerDirty ? "Unsaved tracker changes" : "Tracker up to date"}</span><button type="submit" className="primary-action" disabled={!trackerDirty || savingTracker || rematching || notesTooLarge}>{savingTracker ? "Saving…" : "Save tracker"}</button></div>
            </form>

            <section className="inspector-section profile-connections" aria-labelledby="profile-connections-title">
              <div className="section-heading-line">
                <div>
                  <p className="section-kicker">Profile version {detail.profile_version.version_number} · Captured {formatOpportunityDate(detail.snapshot.captured_at_ms)}</p>
                  <h3 id="profile-connections-title">Profile connections</h3>
                </div>
                <span className={`signal-badge signal-badge--${detail.match.signal}`}>{SIGNAL_LABELS[detail.match.signal]}</span>
              </div>
              <p className="profile-connections__summary">
                {profileConnections.length > 0
                  ? PROFILE_CONNECTION_SUMMARIES[detail.match.signal]
                  : "CVGnome did not find a clear connection in the profile wording it checked."}
              </p>
              <p className="inspector-note">A reading aid based on your saved profile—not a qualification score or hiring prediction.</p>
              {isStale && <div className="inline-callout"><span>This comparison predates profile version {currentProfileVersion}.</span><button type="button" onClick={() => void rematch()} disabled={rematching || savingTracker || trackerDirty}>{rematching ? "Checking…" : "Check latest profile"}</button></div>}
              <details className="match-details">
                <summary className="match-details__summary">
                  <span>See what CVGnome connected</span>
                  <span className="match-details__summary-count">{profileConnections.length} found · {detail.match.terms_to_review.length} to check</span>
                </summary>
                <div className="match-details__body evidence-columns">
                  <section aria-labelledby="connected-profile-material-title">
                    <h4 id="connected-profile-material-title">Examples from your profile</h4>
                    {profileConnections.length ? (
                      <ul className="evidence-list connection-evidence-list">
                        {profileConnections.map((connection) => (
                          <li key={connection.key}>
                            <ul className="connection-term-list" aria-label="Connected posting wording">
                              {connection.terms.map((term) => <li key={term}>{term}</li>)}
                            </ul>
                            <p>{connection.snippet}</p>
                          </li>
                        ))}
                      </ul>
                    ) : <p className="quiet-empty">No clear profile wording was connected to this posting.</p>}
                  </section>
                  <section className="wording-review" aria-labelledby="wording-review-title">
                    <h4 id="wording-review-title">Wording to check</h4>
                    <p className="wording-review__note">These phrases look important in the posting but were not found clearly in your saved profile. Missing wording is not proof of an experience gap.</p>
                    {detail.match.terms_to_review.length ? <ul className="term-list">{detail.match.terms_to_review.map((term) => <li key={term}>{term}</li>)}</ul> : <p className="quiet-empty">No additional wording was flagged.</p>}
                  </section>
                </div>
              </details>
            </section>

            <section className="inspector-section tailoring-section" aria-labelledby="tailoring-title" aria-busy={loadingTailoring || tailoringActionBusy}>
              <div className="section-heading-line">
                <div><p className="section-kicker">Model-assisted workspace</p><h3 id="tailoring-title">Tailored resume draft</h3></div>
                {tailoredDraft && <span className="review-badge review-badge--ready">Versioned locally</span>}
              </div>

              {loadingTailoring ? (
                <p className="inspector-note">Checking for a locally stored draft and verified model configuration…</p>
              ) : tailoredDraft ? (
                <div className="tailored-preview">
                  <div className="tailored-history-bar">
                    <label className="field tailored-history-select">
                      <span>Version history</span>
                      <select
                        ref={tailoredHistorySelectRef}
                        value={selectedTailoredRevisionId ?? "original"}
                        onChange={(event) => void selectTailoredVersion(event.target.value === "original" ? null : event.target.value)}
                        disabled={loadingTailoredRevisions || tailoringActionBusy || tailoredRevisionEditor !== null}
                      >
                        <option value="original">Revision 0 · Generated original · {formatOpportunityTimestamp(tailoredDraft.created_at_ms)}</option>
                        {(tailoredRevisionList?.revisions ?? []).map((revision) => (
                          <option key={revision.id} value={revision.id}>
                            User revision {revision.revision_number} · {revision.changed_field_count} {revision.changed_field_count === 1 ? "field" : "fields"} · {formatOpportunityTimestamp(revision.created_at_ms)}
                          </option>
                        ))}
                      </select>
                    </label>
                    <div className="tailored-history-meta">
                      {loadingTailoredRevisions
                        ? <span>Loading local history…</span>
                        : tailoredRevisionList
                          ? <span>{tailoredRevisionList.revisions.length} {tailoredRevisionList.truncated ? "newest user revisions shown" : `saved user ${tailoredRevisionList.revisions.length === 1 ? "revision" : "revisions"}`}</span>
                          : <button type="button" className="text-action" onClick={() => void loadTailoredRevisionHistory(tailoredDraft.id)} disabled={tailoringActionBusy || tailoredRevisionEditor !== null}>Retry history</button>}
                      {tailoredRevisionList?.truncated && <span>Only the newest {tailoredRevisionList.revisions.length} revisions are shown.</span>}
                    </div>
                  </div>

                  {tailoredRevisionNotice && (
                    <p
                      className={`action-notice action-notice--${tailoredRevisionNotice.kind}`}
                      role={tailoredRevisionNotice.kind === "error" ? "alert" : "status"}
                    >
                      {tailoredRevisionNotice.message}
                    </p>
                  )}

                  {tailoredRevisionEditor ? (
                    <form className="tailored-revision-editor" onSubmit={saveTailoredRevision} aria-busy={savingTailoredRevision}>
                      <header className="tailored-revision-editor__header">
                        <div>
                          <p className="section-kicker">Local user revision</p>
                          <h4>Edit the latest tailored resume</h4>
                          <p>Only wording is editable. Employer, role, location, and dates stay anchored to the generated resume.</p>
                        </div>
                      </header>
                      <label className="field">
                        <span>Headline</span>
                        <input
                          ref={revisionHeadlineRef}
                          maxLength={TAILORED_REVISION_LIMITS.headline}
                          value={tailoredRevisionEditor.label}
                          onChange={(event) => updateTailoredRevisionEditor((current) => ({ ...current, label: event.target.value }))}
                          disabled={savingTailoredRevision}
                        />
                      </label>
                      <label className="field">
                        <span>Professional summary</span>
                        <textarea
                          rows={6}
                          maxLength={TAILORED_REVISION_LIMITS.summary}
                          value={tailoredRevisionEditor.summary}
                          onChange={(event) => updateTailoredRevisionEditor((current) => ({ ...current, summary: event.target.value }))}
                          disabled={savingTailoredRevision}
                        />
                        <small>Leave blank to remove it · {new TextEncoder().encode(tailoredRevisionEditor.summary).length} / {TAILORED_REVISION_LIMITS.summary} UTF-8 bytes</small>
                      </label>
                      <div className="tailored-revision-work">
                        {tailoredRevisionEditor.work.map((editedWork, workEditorIndex) => {
                          const immutableWork = tailoredRevisionEditor.parentResume.work?.[editedWork.sourceIndex];
                          const highlightCount = editedWork.highlightsText.split(/\r?\n/u).filter((line) => line.trim()).length;
                          return (
                            <fieldset key={editedWork.sourceIndex}>
                              <legend>{immutableWork?.position || "Role"}</legend>
                              <p className="tailored-revision-work__identity">
                                {[immutableWork?.name || "Organization", immutableWork?.location, [immutableWork?.startDate, immutableWork?.endDate || "Present"].filter(Boolean).join(" – ")].filter(Boolean).join(" · ")}
                              </p>
                              <label className="field">
                                <span>Role summary</span>
                                <textarea
                                  rows={3}
                                  maxLength={TAILORED_REVISION_LIMITS.workSummary}
                                  value={editedWork.summary}
                                  onChange={(event) => updateTailoredRevisionEditor((current) => ({
                                    ...current,
                                    work: current.work.map((item, index) => index === workEditorIndex ? { ...item, summary: event.target.value } : item),
                                  }))}
                                  disabled={savingTailoredRevision}
                                />
                              </label>
                              <label className="field">
                                <span>Highlights</span>
                                <textarea
                                  rows={5}
                                  maxLength={(TAILORED_REVISION_LIMITS.highlight * TAILORED_REVISION_LIMITS.highlightsPerRole) + TAILORED_REVISION_LIMITS.highlightsPerRole - 1}
                                  value={editedWork.highlightsText}
                                  onChange={(event) => updateTailoredRevisionEditor((current) => ({
                                    ...current,
                                    work: current.work.map((item, index) => index === workEditorIndex ? { ...item, highlightsText: event.target.value } : item),
                                  }))}
                                  disabled={savingTailoredRevision}
                                />
                                <small>One highlight per line · {highlightCount} / {TAILORED_REVISION_LIMITS.highlightsPerRole}</small>
                              </label>
                            </fieldset>
                          );
                        })}
                      </div>
                      {tailoredRevisionValidation?.errors.length ? (
                        <ul className="tailored-revision-errors" role="alert">
                          {tailoredRevisionValidation.errors.map((message) => <li key={message}>{message}</li>)}
                        </ul>
                      ) : null}
                      <div className="tailored-revision-editor__actions">
                        <span>
                          {tailoredRevisionNormalizationOnly
                            ? "Only spacing changed; CVGnome normalizes spacing, so there is no revision to save"
                            : tailoredRevisionDirty
                              ? "Unsaved user input"
                              : "Make a wording change to save a revision"}
                        </span>
                        <div>
                          <button type="button" className="secondary-action" onClick={cancelTailoredRevisionEdit} disabled={savingTailoredRevision}>Cancel</button>
                          <button type="submit" className="primary-action" disabled={savingTailoredRevision || !tailoredRevisionValidation?.edits || Boolean(tailoredRevisionValidation.errors.length)}>
                            {savingTailoredRevision ? "Saving revision…" : "Save user revision"}
                          </button>
                        </div>
                      </div>
                    </form>
                  ) : loadingSelectedTailoredRevision || !selectedTailoredResume ? (
                    <div className="tailored-version-loading" role="status">Opening the selected local revision…</div>
                  ) : (
                    <>
                      <div className="tailored-preview__heading">
                        <div>
                          <span className={`tailored-version-kind${selectedTailoredRevision ? " tailored-version-kind--user" : ""}`}>
                            {selectedTailoredRevision ? `User revision ${selectedTailoredRevision.revision_number}` : "Revision 0 · Generated original"}
                          </span>
                          <strong>{selectedTailoredResume.basics?.label || tailoredDraft.opportunity.title}</strong>
                          <span>Profile version {tailoredDraft.profile_version.version_number} · {formatOpportunityTimestamp(selectedTailoredRevision?.created_at_ms ?? tailoredDraft.created_at_ms)}</span>
                        </div>
                        <div className="tailored-preview__badges">
                          {tailoredDraftProfileIsStale && <span className="semantic-pill">Profile changed</span>}
                          {tailoredDraftPostingIsStale && <span className="semantic-pill">Posting changed</span>}
                        </div>
                      </div>
                      {selectedTailoredResume.basics?.summary && <p className="tailored-summary">{selectedTailoredResume.basics.summary}</p>}
                      <div className="tailored-work-list">
                        {(selectedTailoredResume.work ?? []).map((work, index) => (
                          <article key={`${work.name ?? "work"}-${work.position ?? index}-${index}`}>
                            <header><div><strong>{work.position || "Role"}</strong><span>{work.name || "Organization"}</span></div><small>{[work.startDate, work.endDate || "Present"].filter(Boolean).join(" – ")}</small></header>
                            {work.summary && <p>{work.summary}</p>}
                            {work.highlights?.length ? <ul>{work.highlights.map((highlight, highlightIndex) => <li key={`${highlightIndex}-${highlight}`}>{highlight}</li>)}</ul> : null}
                          </article>
                        ))}
                      </div>
                      {selectedTailoredRevision && (
                        <section className="tailored-revision-review" aria-labelledby="tailored-revision-review-title">
                          <div>
                            <p className="section-kicker">Generated original → user revision {selectedTailoredRevision.revision_number}</p>
                            <h4 id="tailored-revision-review-title">What changed</h4>
                            <p>User-authored wording is stored locally and was not re-verified against imported evidence.</p>
                          </div>
                          {selectedTailoredDiff.length > 0 ? (
                            <dl className="tailored-diff-list">
                              {selectedTailoredDiff.map((item) => (
                                <div key={item.key}>
                                  <dt>{item.label}</dt>
                                  <dd><span>Generated</span><p>{item.before ?? "Removed / not present"}</p></dd>
                                  <dd><span>Selected</span><p>{item.after ?? "Removed / not present"}</p></dd>
                                </div>
                              ))}
                            </dl>
                          ) : <p className="quiet-empty">No visible wording difference from the generated original.</p>}
                        </section>
                      )}
                    </>
                  )}

                  <details className="tailoring-receipt"><summary>Generated-original evidence and model receipt</summary><dl className="property-grid compact-properties"><div><dt>Provider</dt><dd>{providerLabel(tailoredDraft.models.provider_kind)}</dd></div><div><dt>Chat model</dt><dd>{tailoredDraft.models.chat_model_id}</dd></div><div><dt>Embedding model</dt><dd>{tailoredDraft.models.embedding_model_id}</dd></div><div><dt>Evidence chunks</dt><dd>{tailoredDraft.selected_evidence.length}</dd></div><div><dt>Input fingerprint</dt><dd>{tailoredDraft.input_fingerprint.slice(0, 16)}…</dd></div></dl>{tailoredDraft.selected_evidence.length > 0 && <ul className="tailoring-evidence-list">{tailoredDraft.selected_evidence.map((evidence) => <li key={evidence.chunk_id}><strong>{evidence.path}</strong><p>{evidence.content}</p></li>)}</ul>}</details>
                  <p className="tailoring-receipt-note">This receipt belongs to the generated original. It does not verify wording added in a user revision. Generation received the bounded public baseline resume plus the ranked evidence shown here.</p>
                  {tailoredDraft.quality_issue_codes.map(tailoringQualityMessage).filter((message): message is string => Boolean(message)).map((message) => <p key={message} className="tailoring-warning">{message}</p>)}
                  {tailoredDraftProfileIsStale && <p id="tailored-profile-stale-warning" className="tailoring-warning">This resume lineage uses profile version {tailoredDraft.profile_version.version_number}; the current profile is version {currentProfileVersion}. Export preserves the selected version exactly.</p>}
                  {tailoredDraftPostingIsStale && <p id="tailored-posting-stale-warning" className="tailoring-warning">This resume lineage was tailored to an earlier saved posting snapshot. Export preserves the selected version exactly and will not substitute the current posting.</p>}
                  <p className="inspector-note">Every version is stored locally. Editing or exporting does not call a model, alter the canonical profile, verify qualifications, or publish anything.</p>
                  <div className="tailoring-output" aria-busy={tailoredExport !== null}>
                    <div className="action-copy">
                      <strong>Export exact {selectedTailoredVersionLabel}</strong>
                      <span id="tailored-export-context">DOCX and PDF are rendered from the selected immutable version. The canonical profile stays unchanged.</span>
                    </div>
                    <div className="export-buttons" role="group" aria-label={`Export ${selectedTailoredVersionLabel}`}>
                      <button
                        ref={docxExportButtonRef}
                        type="button"
                        className="secondary-action"
                        onClick={() => void exportTailoredDraft("docx")}
                        disabled={tailoringActionBusy || tailoredRevisionEditor !== null || !selectedTailoredResume}
                        aria-describedby={tailoredExportDescriptionIds}
                      >
                        {tailoredExport?.format === "docx" ? "Creating DOCX…" : "Export DOCX"}
                      </button>
                      <button
                        ref={pdfExportButtonRef}
                        type="button"
                        className="secondary-action"
                        onClick={() => void exportTailoredDraft("pdf")}
                        disabled={tailoringActionBusy || tailoredRevisionEditor !== null || !selectedTailoredResume}
                        aria-describedby={tailoredExportDescriptionIds}
                      >
                        {tailoredExport?.format === "pdf" ? "Creating PDF…" : "Export PDF"}
                      </button>
                    </div>
                  </div>
                  {visibleTailoredExportNotice && (
                    <p
                      className={`action-notice action-notice--${visibleTailoredExportNotice.kind}`}
                      role={visibleTailoredExportNotice.kind === "error" ? "alert" : undefined}
                      aria-live={visibleTailoredExportNotice.kind === "error" ? "assertive" : undefined}
                      aria-atomic={visibleTailoredExportNotice.kind === "error" ? "true" : undefined}
                    >
                      {visibleTailoredExportNotice.message}
                    </p>
                  )}
                  <div className="tailoring-preview__actions">
                    <div className="tailored-local-actions">
                      {canEditSelectedTailoredVersion ? (
                        <button type="button" className="secondary-action" onClick={beginTailoredRevisionEdit} disabled={tailoringActionBusy || tailoredRevisionEditor !== null}>Edit latest version</button>
                      ) : latestTailoredRevisionSummary && selectedTailoredRevisionId !== latestTailoredRevisionSummary.id ? (
                        <button type="button" className="secondary-action" onClick={() => void selectTailoredVersion(latestTailoredRevisionSummary.id)} disabled={tailoringActionBusy || tailoredRevisionEditor !== null}>View latest to edit</button>
                      ) : null}
                    </div>
                    <div className="tailored-regenerate-action">
                      {!providerVerified ? (
                        <button type="button" className="secondary-action" onClick={onOpenModelSettings} disabled={tailoringActionBusy || tailoredRevisionEditor !== null}>Configure models to create another…</button>
                      ) : (
                        <button type="button" className="secondary-action" onClick={() => void generateTailoredDraft()} disabled={isStale || trackerDirty || rematching || tailoringActionBusy || tailoredRevisionEditor !== null}>
                          {generatingTailoring ? generationProgressLabel : isStale ? "Check latest profile first" : cloudProvider ? "Send to OpenAI & create another" : "Create another generated draft"}
                        </button>
                      )}
                    </div>
                  </div>
                </div>
              ) : (
                <div className="tailoring-empty">
                  <p>{cloudProvider ? `Create with ${configuredProviderLabel} using your API account. CVGnome sends this saved posting and bounded public resume evidence for embedding, then the full posting, a tailoring-relevant public baseline, and selected evidence for generation. Name, email, phone, location, URL, and profile links are removed from baseline basics before sending. Raw imports and tracker notes are not sent. Provider charges and policies apply; no other provider is tried.` : "Use canonical evidence to create a role-focused JSON Resume draft. CVGnome ranks a bounded evidence batch first, then sends the public baseline and selected evidence to the local chat model; the two models are never used concurrently."}</p>
                  {!providerVerified ? <button type="button" className="secondary-action" onClick={onOpenModelSettings} disabled={tailoringActionBusy}>Configure models…</button> : <button type="button" className="primary-action" onClick={() => void generateTailoredDraft()} disabled={isStale || trackerDirty || rematching || tailoringActionBusy}>{generatingTailoring ? generationProgressLabel : isStale ? "Check latest profile first" : cloudProvider ? "Send to OpenAI & create draft" : "Create tailored draft"}</button>}
                </div>
              )}
              {tailoringError && <p className="tailoring-error" role="alert">{tailoringError}</p>}
            </section>

            {(detail.opportunity.source_url || detail.opportunity.apply_url) && <dl className="property-grid compact-properties">{detail.opportunity.source_url && <div><dt>Posting reference</dt><dd>{detail.opportunity.source_url}</dd></div>}{detail.opportunity.apply_url && <div><dt>Apply reference</dt><dd>{detail.opportunity.apply_url}</dd></div>}</dl>}
            <details className="posting-snapshot"><summary>Saved posting text</summary><p>{detail.snapshot.description}</p></details>
          </article>
        )}
      </div>
    </section>
  );
}
