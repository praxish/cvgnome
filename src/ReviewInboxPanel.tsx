// SPDX-License-Identifier: MPL-2.0
import { type FormEvent, type KeyboardEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { useDesktopCloseProtection } from "./DesktopCloseGuard";
import {
  collectionInputFromForm,
  ProfileCollectionFields,
  type ProfileCollectionForm,
  validateCollectionForm,
} from "./ProfileCollections";

export type ReviewInboxScope = "inbox" | "deferred" | "history";

export type ReviewInboxCounts = {
  inbox: number;
  deferred: number;
  history: number;
};

type ReviewItemState = "inbox" | "deferred" | "rejected" | "applied";
type OrganizableReviewState = Exclude<ReviewItemState, "applied">;
type ReviewCandidateKind = "education" | "projects" | "skills";
type ReviewOperationKind = "add" | "update";
type ReviewTransitionAction = "defer" | "reject" | "reopen";
type ReviewChangedField =
  | "name"
  | "description"
  | "url"
  | "start_date"
  | "end_date"
  | "highlights"
  | "keywords"
  | "institution"
  | "study_type"
  | "area"
  | "score"
  | "courses"
  | "level";
type ReviewEvidenceFormat = "pdf" | "docx" | "json" | "txt" | "md" | "csv";
type ReviewEvidenceKind = "resume" | "evidence" | "preferences" | "unclassified";

type ReviewItemDetail = {
  label: string;
  value: string;
};

type ReviewItemEvidence = {
  display_name: string;
  source_format: ReviewEvidenceFormat;
  source_kind: ReviewEvidenceKind;
  location_label: string | null;
  excerpt: string;
};

type ReviewProjectCandidate = {
  name: string;
  description: string | null;
  url: string | null;
  start_date: string | null;
  end_date: string | null;
  highlights: string[];
  keywords: string[];
};

type ReviewEducationCandidate = {
  institution: string;
  study_type: string | null;
  area: string | null;
  url: string | null;
  start_date: string | null;
  end_date: string | null;
  score: string | null;
  courses: string[];
};

type ReviewSkillCandidate = {
  name: string;
  level: string | null;
  keywords: string[];
};

type ReviewCandidate = ReviewProjectCandidate | ReviewEducationCandidate | ReviewSkillCandidate;

type ReviewInboxItem = {
  id: string;
  state: ReviewItemState;
  state_revision: number;
  candidate_kind: ReviewCandidateKind;
  operation_kind: ReviewOperationKind;
  title: string;
  subtitle: string | null;
  date_range: string | null;
  summary: string | null;
  highlights: string[];
  tags: string[];
  details: ReviewItemDetail[];
  changed_fields: ReviewChangedField[];
  candidate: ReviewCandidate;
  evidence: ReviewItemEvidence[];
  created_at_ms: number;
  is_previous_import_set: boolean;
};

type ReviewInboxPage = {
  scope: ReviewInboxScope;
  offset: number;
  limit: number;
  total_items: number;
  next_offset: number | null;
  counts: ReviewInboxCounts;
  current_profile_version_id: string | null;
  items: ReviewInboxItem[];
};

type ReviewItemTransitionReceipt = {
  request_id: string;
  review_item_id: string;
  action: ReviewTransitionAction;
  previous_state: OrganizableReviewState;
  state: OrganizableReviewState;
  state_revision: number;
  created_at_ms: number;
  created: boolean;
};

type PendingTransition = {
  reviewItemId: string;
  requestId: string;
  action: ReviewTransitionAction;
  expectedState: OrganizableReviewState;
  expectedRevision: number;
  itemTitle: string;
};

export type ReviewItemApplyReceipt = {
  request_id: string;
  review_item_id: string;
  previous_state: "inbox";
  state: "applied";
  state_revision: number;
  profile_version_id: string;
  parent_profile_version_id: string;
  version_number: number;
  candidate_kind: ReviewCandidateKind;
  operation_kind: ReviewOperationKind;
  entry_index: number;
  changed_fields: ReviewChangedField[];
  created_at_ms: number;
  created: boolean;
};

type PendingApply = {
  reviewItemId: string;
  requestId: string;
  expectedState: "inbox";
  expectedRevision: number;
  expectedParentProfileVersionId: string;
  candidateKind: ReviewCandidateKind;
  operationKind: ReviewOperationKind;
  candidate: ReviewCandidate;
  itemTitle: string;
};

type ReviewInboxPanelProps = {
  initialCounts: ReviewInboxCounts;
  backLabel: string;
  onBack: () => void;
  onStatusRefresh: () => Promise<void>;
  onApplied: (receipt: ReviewItemApplyReceipt) => Promise<void>;
  onMutationBusyChange: (busy: boolean) => void;
};

const SCOPE_LABELS: Record<ReviewInboxScope, string> = {
  inbox: "Inbox",
  deferred: "Deferred",
  history: "History",
};

const CANDIDATE_LABELS: Record<ReviewCandidateKind, string> = {
  education: "Education",
  projects: "Project",
  skills: "Skill group",
};

const CANDIDATE_GLYPHS: Record<ReviewCandidateKind, string> = {
  education: "EDU",
  projects: "PRJ",
  skills: "SKL",
};

const TARGET_STATE: Record<ReviewTransitionAction, OrganizableReviewState> = {
  defer: "deferred",
  reject: "rejected",
  reopen: "inbox",
};

function formatTimestamp(value: number): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Unknown";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function humanize(value: string): string {
  return value
    .replace(/_/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function stateLabel(state: ReviewItemState): string {
  if (state === "applied") return "History · Applied";
  if (state === "rejected") return "History · Rejected";
  return humanize(state);
}

function reviewErrorCode(caught: unknown): string | null {
  const message = caught instanceof Error ? caught.message : String(caught);
  const code = message.split(":", 1)[0]?.trim().toLowerCase();
  return code && /^[a-z][a-z0-9_]*$/.test(code) ? code : null;
}

function safeListError(caught: unknown): string {
  if (reviewErrorCode(caught) === "vault_busy") {
    return "The local vault is busy. Wait a moment and try opening the review items again.";
  }
  return "CVGnome could not read the local review items.";
}

function transitionFailure(caught: unknown): { definitive: boolean; message: string } {
  switch (reviewErrorCode(caught)) {
    case "review_item_not_found":
      return { definitive: true, message: "That review item is no longer available. The list has been refreshed." };
    case "state_conflict":
    case "review_item_state_conflict":
      return { definitive: true, message: "That review item changed locally. Review its latest state before trying again." };
    case "invalid_transition":
    case "review_item_invalid_transition":
      return { definitive: true, message: "That action is no longer available for this review item." };
    case "review_item_stale_generation":
      return { definitive: true, message: "That item belongs to an earlier import set and is available as read-only history." };
    case "request_conflict":
    case "review_item_request_conflict":
      return { definitive: true, message: "That request ID was already used for a different review action. The list has been refreshed." };
    case "invalid_params":
      return { definitive: true, message: "CVGnome could not validate that review action. The item was not changed." };
    default:
      return {
        definitive: false,
        message: "CVGnome could not confirm the review action. Retry the exact action to recover safely; your profile was not changed.",
      };
  }
}

function applyFailure(caught: unknown): { definitive: boolean; reloadRequired: boolean; message: string } {
  switch (reviewErrorCode(caught)) {
    case "review_item_not_found":
      return { definitive: true, reloadRequired: true, message: "That suggestion is no longer available. Nothing was applied; reload the review item when you are ready to leave this draft." };
    case "review_item_state_conflict":
    case "review_item_apply_conflict":
    case "state_conflict":
      return { definitive: true, reloadRequired: true, message: "That suggestion changed locally. Nothing was applied; your edited draft is still visible until you reload the item." };
    case "review_item_stale_generation":
      return { definitive: true, reloadRequired: true, message: "That suggestion belongs to an earlier import set and can no longer be applied. Your edited draft is still visible until you reload." };
    case "review_item_candidate_conflict":
    case "review_item_candidate_invalid":
    case "invalid_candidate":
    case "invalid_params":
      return { definitive: true, reloadRequired: false, message: "CVGnome could not validate the proposed profile entry. Nothing was applied; edit the suggestion and try again." };
    case "profile_parent_conflict":
    case "profile_version_conflict":
    case "review_item_profile_conflict":
      return { definitive: true, reloadRequired: true, message: "The current profile changed after this suggestion was opened. Nothing was applied; your edited draft is still visible until you reload against the latest profile." };
    case "review_item_request_conflict":
    case "review_item_apply_request_conflict":
    case "request_conflict":
      return { definitive: true, reloadRequired: true, message: "That request ID was already used for a different apply. Nothing new was saved; reload the review item before trying again." };
    case "review_item_apply_noop":
      return { definitive: true, reloadRequired: false, message: "That proposal does not change the current profile, so CVGnome did not create a duplicate version. Edit the proposal, or defer or reject it." };
    case "review_item_evidence_invalid":
      return { definitive: true, reloadRequired: false, message: "CVGnome could not verify this suggestion against its retained evidence, so nothing was applied. Defer or reject the item instead." };
    case "profile_missing":
      return { definitive: true, reloadRequired: true, message: "The current profile is no longer available. Nothing was applied; reload the review item when you are ready to leave this draft." };
    case "profile_version_limit":
      return { definitive: true, reloadRequired: false, message: "The local profile has reached its safe version limit, so CVGnome cannot apply another suggestion." };
    default:
      return {
        definitive: false,
        reloadRequired: false,
        message: "CVGnome could not confirm whether the profile version was saved. Retry this exact apply to recover safely.",
      };
  }
}

function reviewFormFromCandidate(
  kind: ReviewCandidateKind,
  candidate: ReviewCandidate,
): ProfileCollectionForm {
  if (kind === "projects") {
    const project = candidate as ReviewProjectCandidate;
    return {
      section: kind,
      name: project.name,
      description: project.description ?? "",
      url: project.url ?? "",
      startDate: project.start_date ?? "",
      endDate: project.end_date ?? "",
      highlights: project.highlights.join("\n"),
      keywords: project.keywords.join("\n"),
    };
  }
  if (kind === "education") {
    const education = candidate as ReviewEducationCandidate;
    return {
      section: kind,
      institution: education.institution,
      studyType: education.study_type ?? "",
      area: education.area ?? "",
      url: education.url ?? "",
      startDate: education.start_date ?? "",
      endDate: education.end_date ?? "",
      score: education.score ?? "",
      courses: education.courses.join("\n"),
    };
  }
  const skill = candidate as ReviewSkillCandidate;
  return {
    section: kind,
    name: skill.name,
    level: skill.level ?? "",
    keywords: skill.keywords.join("\n"),
  };
}

function comparableCandidate(candidate: ReviewCandidate): string {
  return JSON.stringify(Object.fromEntries(
    Object.entries(candidate).sort(([left], [right]) => left.localeCompare(right)),
  ));
}

function validTransitionReceipt(
  receipt: ReviewItemTransitionReceipt,
  pending: PendingTransition,
): boolean {
  return receipt.request_id === pending.requestId
    && receipt.review_item_id === pending.reviewItemId
    && receipt.action === pending.action
    && receipt.previous_state === pending.expectedState
    && receipt.state === TARGET_STATE[pending.action]
    && receipt.state_revision === pending.expectedRevision + 1
    && Number.isSafeInteger(receipt.created_at_ms)
    && receipt.created_at_ms > 0
    && typeof receipt.created === "boolean";
}

function validApplyReceipt(receipt: ReviewItemApplyReceipt, pending: PendingApply): boolean {
  return receipt.request_id === pending.requestId
    && receipt.review_item_id === pending.reviewItemId
    && receipt.previous_state === pending.expectedState
    && receipt.state === "applied"
    && receipt.state_revision === pending.expectedRevision + 1
    && receipt.parent_profile_version_id === pending.expectedParentProfileVersionId
    && receipt.profile_version_id.length > 0
    && receipt.profile_version_id !== pending.expectedParentProfileVersionId
    && Number.isSafeInteger(receipt.version_number)
    && receipt.version_number > 0
    && receipt.candidate_kind === pending.candidateKind
    && receipt.operation_kind === pending.operationKind
    && Number.isSafeInteger(receipt.entry_index)
    && receipt.entry_index >= 0
    && Array.isArray(receipt.changed_fields)
    && receipt.changed_fields.length > 0
    && Number.isSafeInteger(receipt.created_at_ms)
    && receipt.created_at_ms > 0
    && typeof receipt.created === "boolean";
}

function initialScopeFor(counts: ReviewInboxCounts): ReviewInboxScope {
  if (counts.inbox > 0) return "inbox";
  if (counts.deferred > 0) return "deferred";
  return "history";
}

export function ReviewInboxPanel({
  initialCounts,
  backLabel,
  onBack,
  onStatusRefresh,
  onApplied,
  onMutationBusyChange,
}: ReviewInboxPanelProps) {
  const [scope, setScope] = useState<ReviewInboxScope>(() => initialScopeFor(initialCounts));
  const [page, setPage] = useState<ReviewInboxPage | null>(null);
  const [items, setItems] = useState<ReviewInboxItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [transitionBusy, setTransitionBusy] = useState(false);
  const [transitionUncertain, setTransitionUncertain] = useState(false);
  const [transitionError, setTransitionError] = useState<string | null>(null);
  const [pendingTransition, setPendingTransition] = useState<PendingTransition | null>(null);
  const [editForm, setEditForm] = useState<ProfileCollectionForm | null>(null);
  const [editingItemId, setEditingItemId] = useState<string | null>(null);
  const [applyBusy, setApplyBusy] = useState(false);
  const [applyUncertain, setApplyUncertain] = useState(false);
  const [applyNeedsReload, setApplyNeedsReload] = useState(false);
  const [applyError, setApplyError] = useState<string | null>(null);
  const [pendingApply, setPendingApply] = useState<PendingApply | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const listRequestRef = useRef(0);
  const transitionInFlightRef = useRef(false);
  const pendingTransitionRef = useRef<PendingTransition | null>(null);
  const applyInFlightRef = useRef(false);
  const pendingApplyRef = useRef<PendingApply | null>(null);
  const rowRefs = useRef(new Map<string, HTMLButtonElement>());
  const scopeRefs = useRef(new Map<ReviewInboxScope, HTMLButtonElement>());
  const headingRef = useRef<HTMLHeadingElement>(null);
  const applyEditFirstInputRef = useRef<HTMLInputElement>(null);
  const editApplyTriggerRef = useRef<HTMLButtonElement>(null);
  const focusAfterRefreshRef = useRef(false);
  const focusItemAfterRefreshRef = useRef<string | null>(null);

  const counts = page?.counts ?? initialCounts;
  const editorOpen = editForm !== null && editingItemId !== null;
  const operationBusy = transitionBusy || applyBusy;
  const interactionLocked = transitionBusy || transitionUncertain || applyBusy || applyUncertain || applyNeedsReload || editorOpen;
  useDesktopCloseProtection({ dirty: editorOpen, blocked: transitionBusy || transitionUncertain || applyBusy || applyUncertain });

  useEffect(() => {
    window.requestAnimationFrame(() => headingRef.current?.focus());
  }, []);

  useEffect(() => {
    onMutationBusyChange(interactionLocked);
    return () => onMutationBusyChange(false);
  }, [interactionLocked, onMutationBusyChange]);

  useEffect(() => {
    if (!interactionLocked) return;
    const preventAccidentalClose = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", preventAccidentalClose);
    return () => window.removeEventListener("beforeunload", preventAccidentalClose);
  }, [interactionLocked]);

  const loadPage = useCallback(async (
    nextScope: ReviewInboxScope,
    offset: number,
    append: boolean,
  ): Promise<boolean> => {
    const requestId = ++listRequestRef.current;
    append ? setLoadingMore(true) : setLoading(true);
    setListError(null);
    try {
      const next = await invoke<ReviewInboxPage>("list_profile_review_items", {
        scope: nextScope,
        offset,
      });
      if (requestId !== listRequestRef.current) return false;
      setPage(next);
      setItems((current) => {
        if (!append) return next.items;
        const seen = new Set(current.map((item) => item.id));
        return [...current, ...next.items.filter((item) => !seen.has(item.id))];
      });
      if (!append) {
        const desiredId = focusItemAfterRefreshRef.current;
        const nextSelectedId = desiredId && next.items.some((item) => item.id === desiredId)
          ? desiredId
          : next.items[0]?.id ?? null;
        setSelectedId((current) => current && next.items.some((item) => item.id === current)
          ? current
          : nextSelectedId);
        if (focusAfterRefreshRef.current) {
          focusAfterRefreshRef.current = false;
          focusItemAfterRefreshRef.current = null;
          window.requestAnimationFrame(() => {
            if (nextSelectedId) rowRefs.current.get(nextSelectedId)?.focus();
            else scopeRefs.current.get(nextScope)?.focus();
          });
        }
      }
      return true;
    } catch (caught) {
      if (requestId === listRequestRef.current) setListError(safeListError(caught));
      return false;
    } finally {
      if (requestId === listRequestRef.current) {
        setLoading(false);
        setLoadingMore(false);
      }
    }
  }, []);

  useEffect(() => {
    setItems([]);
    setPage(null);
    setSelectedId(null);
    void loadPage(scope, 0, false);
    return () => {
      listRequestRef.current += 1;
    };
  }, [loadPage, scope]);

  const selected = useMemo(
    () => items.find((item) => item.id === selectedId) ?? null,
    [items, selectedId],
  );
  const editedCandidate = useMemo(
    () => editForm ? collectionInputFromForm(editForm) as ReviewCandidate : null,
    [editForm],
  );
  const editedCandidateChanged = Boolean(
    selected
    && editedCandidate
    && comparableCandidate(editedCandidate) !== comparableCandidate(selected.candidate),
  );

  const selectScope = useCallback((nextScope: ReviewInboxScope) => {
    if (nextScope === scope || interactionLocked) return;
    setItems([]);
    setPage(null);
    setSelectedId(null);
    setListError(null);
    setTransitionError(null);
    setApplyError(null);
    setApplyNeedsReload(false);
    setLoading(true);
    setScope(nextScope);
  }, [interactionLocked, scope]);

  const selectByArrow = useCallback((event: KeyboardEvent<HTMLElement>) => {
    if (!selectedId || interactionLocked) return;
    if (!(event.target instanceof HTMLButtonElement) || !event.target.classList.contains("review-item-row")) return;
    const keys = items.map((item) => item.id);
    const currentIndex = keys.indexOf(selectedId);
    if (currentIndex < 0) return;
    let nextIndex = currentIndex;
    if (event.key === "ArrowDown" || event.key === "ArrowRight") nextIndex += 1;
    else if (event.key === "ArrowUp" || event.key === "ArrowLeft") nextIndex -= 1;
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = keys.length - 1;
    else return;
    event.preventDefault();
    nextIndex = Math.max(0, Math.min(keys.length - 1, nextIndex));
    setSelectedId(keys[nextIndex]);
    setApplyError(null);
    window.requestAnimationFrame(() => rowRefs.current.get(keys[nextIndex])?.focus());
  }, [interactionLocked, items, selectedId]);

  const selectScopeByArrow = useCallback((
    event: KeyboardEvent<HTMLButtonElement>,
    currentScope: ReviewInboxScope,
  ) => {
    if (interactionLocked) return;
    const scopes = Object.keys(SCOPE_LABELS) as ReviewInboxScope[];
    const currentIndex = scopes.indexOf(currentScope);
    let nextIndex = currentIndex;
    if (event.key === "ArrowRight" || event.key === "ArrowDown") nextIndex += 1;
    else if (event.key === "ArrowLeft" || event.key === "ArrowUp") nextIndex -= 1;
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = scopes.length - 1;
    else return;
    event.preventDefault();
    const nextScope = scopes[(nextIndex + scopes.length) % scopes.length];
    selectScope(nextScope);
    window.requestAnimationFrame(() => scopeRefs.current.get(nextScope)?.focus());
  }, [interactionLocked, selectScope]);

  const refreshCurrentScope = useCallback(async (confirmedAction: boolean) => {
    if (confirmedAction) {
      // A confirmed transition makes the currently rendered row authoritative no
      // longer. Clear it before refreshing so a read failure cannot expose stale
      // action buttons that would only produce a state conflict.
      setItems([]);
      setPage(null);
      setSelectedId(null);
    }
    const [listRefresh, statusRefresh] = await Promise.allSettled([
      loadPage(scope, 0, false),
      onStatusRefresh(),
    ]);
    const listRefreshed = listRefresh.status === "fulfilled" && listRefresh.value;
    if (confirmedAction && !listRefreshed) {
      setListError(statusRefresh.status === "rejected"
        ? "The review action was saved, but neither the updated review list nor workspace totals could be read. Reload this view to try again."
        : "The review action was saved, but the updated review list could not be read. Reload this view to try again.");
    } else if (statusRefresh.status === "rejected") {
      setListError(confirmedAction
        ? "The review action was saved, but the updated workspace totals could not be read. Reload this view to try again."
        : "The review items were refreshed, but the updated workspace totals could not be read.");
    }
  }, [loadPage, onStatusRefresh, scope]);

  const performTransition = useCallback(async (pending: PendingTransition) => {
    if (transitionInFlightRef.current) return;
    transitionInFlightRef.current = true;
    pendingTransitionRef.current = pending;
    setPendingTransition(pending);
    setTransitionBusy(true);
    setTransitionUncertain(false);
    setTransitionError(null);
    setApplyError(null);
    setAnnouncement(`Updating ${pending.itemTitle} in the local review inbox.`);
    try {
      try {
        const receipt = await invoke<ReviewItemTransitionReceipt>("transition_profile_review_item", {
          reviewItemId: pending.reviewItemId,
          requestId: pending.requestId,
          action: pending.action,
          expectedState: pending.expectedState,
          expectedRevision: pending.expectedRevision,
        });
        if (!validTransitionReceipt(receipt, pending)) {
          throw new Error("review_item_receipt_invalid: The local engine returned an invalid review action receipt.");
        }
      } catch (caught) {
        const failure = transitionFailure(caught);
        setTransitionError(failure.message);
        if (failure.definitive) {
          pendingTransitionRef.current = null;
          setPendingTransition(null);
          setTransitionUncertain(false);
          setAnnouncement("The review action was not completed. The local list is being refreshed.");
          focusAfterRefreshRef.current = true;
          await refreshCurrentScope(false);
        } else {
          setTransitionUncertain(true);
          setAnnouncement("The review action outcome is unconfirmed. Retry the exact action to recover safely.");
        }
        return;
      }

      const destination = TARGET_STATE[pending.action] === "rejected"
        ? "History"
        : SCOPE_LABELS[TARGET_STATE[pending.action] as ReviewInboxScope];
      pendingTransitionRef.current = null;
      setPendingTransition(null);
      setTransitionUncertain(false);
      setTransitionError(null);
      setAnnouncement(`${pending.itemTitle} moved to ${destination}. Your saved profile and imported files were unchanged.`);
      focusAfterRefreshRef.current = true;
      await refreshCurrentScope(true);
    } finally {
      transitionInFlightRef.current = false;
      setTransitionBusy(false);
    }
  }, [refreshCurrentScope]);

  const requestTransition = useCallback((item: ReviewInboxItem, action: ReviewTransitionAction) => {
    if (interactionLocked || transitionInFlightRef.current) return;
    if (item.state === "applied") return;
    void performTransition({
      reviewItemId: item.id,
      requestId: crypto.randomUUID(),
      action,
      expectedState: item.state,
      expectedRevision: item.state_revision,
      itemTitle: item.title,
    });
  }, [interactionLocked, performTransition]);

  const reloadAfterFailedApply = useCallback(async () => {
    if (applyBusy || applyUncertain || applyInFlightRef.current) return;
    setEditForm(null);
    setEditingItemId(null);
    setApplyNeedsReload(false);
    setApplyError(null);
    pendingApplyRef.current = null;
    setPendingApply(null);
    focusAfterRefreshRef.current = true;
    setAnnouncement("Reloading the suggestion against the latest local profile and inbox state.");
    await refreshCurrentScope(false);
  }, [applyBusy, applyUncertain, refreshCurrentScope]);

  const closeEditor = useCallback(() => {
    if (applyBusy || applyUncertain || applyInFlightRef.current) return;
    if (applyNeedsReload) {
      void reloadAfterFailedApply();
      return;
    }
    setEditForm(null);
    setEditingItemId(null);
    setApplyError(null);
    setAnnouncement("Edited proposal discarded. The original local suggestion is unchanged.");
    window.requestAnimationFrame(() => editApplyTriggerRef.current?.focus());
  }, [applyBusy, applyNeedsReload, applyUncertain, reloadAfterFailedApply]);

  const startEditor = useCallback((item: ReviewInboxItem) => {
    if (
      interactionLocked
      || item.is_previous_import_set
      || item.state !== "inbox"
      || !page?.current_profile_version_id
    ) return;
    setEditingItemId(item.id);
    setEditForm(reviewFormFromCandidate(item.candidate_kind, item.candidate));
    setApplyError(null);
    setApplyNeedsReload(false);
    setAnnouncement(`Editing ${item.title} before applying it to the current profile.`);
    window.requestAnimationFrame(() => applyEditFirstInputRef.current?.focus());
  }, [interactionLocked, page?.current_profile_version_id]);

  const performApply = useCallback(async (pending: PendingApply) => {
    if (applyInFlightRef.current) return;
    applyInFlightRef.current = true;
    pendingApplyRef.current = pending;
    setPendingApply(pending);
    setApplyBusy(true);
    setApplyUncertain(false);
    setApplyNeedsReload(false);
    setApplyError(null);
    setTransitionError(null);
    setAnnouncement(`Applying ${pending.itemTitle} to one new local profile version.`);
    try {
      let receipt: ReviewItemApplyReceipt;
      try {
        receipt = await invoke<ReviewItemApplyReceipt>("apply_profile_review_item", {
          reviewItemId: pending.reviewItemId,
          expectedState: pending.expectedState,
          expectedRevision: pending.expectedRevision,
          expectedParentProfileVersionId: pending.expectedParentProfileVersionId,
          expectedOperationKind: pending.operationKind,
          requestId: pending.requestId,
          candidate: pending.candidate,
        });
        if (!validApplyReceipt(receipt, pending)) {
          throw new Error("review_item_apply_receipt_invalid: The local engine returned an invalid apply receipt.");
        }
      } catch (caught) {
        const failure = applyFailure(caught);
        setApplyError(failure.message);
        if (failure.definitive) {
          pendingApplyRef.current = null;
          setPendingApply(null);
          setApplyUncertain(false);
          setApplyNeedsReload(failure.reloadRequired);
          setAnnouncement(failure.reloadRequired
            ? "The suggestion was not applied. Your current draft remains visible until you reload it."
            : "The suggestion was not applied. Review the proposal fields and try again.");
        } else {
          setApplyUncertain(true);
          setAnnouncement("The apply outcome is unconfirmed. Retry the exact apply to recover safely.");
        }
        return;
      }

      pendingApplyRef.current = null;
      setPendingApply(null);
      setApplyUncertain(false);
      setApplyNeedsReload(false);
      setEditForm(null);
      setEditingItemId(null);
      setApplyError(null);
      setItems([]);
      setPage(null);
      setSelectedId(null);
      setLoading(true);
      focusAfterRefreshRef.current = true;
      focusItemAfterRefreshRef.current = pending.reviewItemId;
      setAnnouncement(`${pending.itemTitle} was applied as profile version ${receipt.version_number} and moved to read-only History.`);
      try {
        await onApplied(receipt);
      } catch {
        setApplyError(`Profile version ${receipt.version_number} is saved, but the workspace totals could not be refreshed. Reload the view; retrying Apply is not necessary.`);
      }
      // Changing scope starts a fresh fixed-page History read through the scope
      // effect. Do this only after the parent has accepted/refreshed the profile
      // receipt so the page and global counts advance together.
      setScope("history");
    } finally {
      applyInFlightRef.current = false;
      setApplyBusy(false);
    }
  }, [onApplied, refreshCurrentScope]);

  const requestApply = useCallback((item: ReviewInboxItem, candidate: ReviewCandidate) => {
    const expectedParentProfileVersionId = page?.current_profile_version_id;
    if (
      applyInFlightRef.current
      || applyBusy
      || applyUncertain
      || applyNeedsReload
      || item.is_previous_import_set
      || item.state !== "inbox"
      || !expectedParentProfileVersionId
    ) return;
    void performApply({
      reviewItemId: item.id,
      requestId: crypto.randomUUID(),
      expectedState: "inbox",
      expectedRevision: item.state_revision,
      expectedParentProfileVersionId,
      candidateKind: item.candidate_kind,
      operationKind: item.operation_kind,
      candidate,
      itemTitle: item.title,
    });
  }, [applyBusy, applyNeedsReload, applyUncertain, page?.current_profile_version_id, performApply]);

  const submitEditedApply = useCallback((event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!editForm || !selected || editingItemId !== selected.id || applyBusy || applyUncertain) return;
    const validation = validateCollectionForm(editForm);
    if (validation) {
      setApplyError(validation.message);
      setAnnouncement("Review the highlighted proposal field before applying.");
      window.requestAnimationFrame(() => {
        document.querySelector<HTMLElement>(`[data-profile-collection-field="${validation.field}"]`)?.focus();
      });
      return;
    }
    requestApply(selected, collectionInputFromForm(editForm) as ReviewCandidate);
  }, [applyBusy, applyUncertain, editForm, editingItemId, requestApply, selected]);

  const canLoadMore = Boolean(
    page
    && page.next_offset !== null
    && page.next_offset > page.offset
    && items.length < page.total_items,
  );

  const firstItem = items[0] ?? null;
  const laterItems = scope === "inbox" && firstItem ? items.slice(1) : items;

  const renderRow = (item: ReviewInboxItem, prominent: boolean) => (
    <button
      key={item.id}
      ref={(node) => {
        if (node) rowRefs.current.set(item.id, node);
        else rowRefs.current.delete(item.id);
      }}
      type="button"
      className={`review-item-row${selectedId === item.id ? " is-selected" : ""}${prominent ? " review-item-row--start" : ""}`}
      aria-current={selectedId === item.id ? "true" : undefined}
      aria-controls="review-inbox-detail"
      onClick={() => {
        setSelectedId(item.id);
        setApplyError(null);
      }}
      disabled={interactionLocked}
    >
      <span className="review-item-glyph" aria-hidden="true">{CANDIDATE_GLYPHS[item.candidate_kind]}</span>
      <span className="review-item-row__copy">
        <strong>{item.title}</strong>
        <small>{item.subtitle ?? `${CANDIDATE_LABELS[item.candidate_kind]} · ${item.state === "applied" ? "Applied" : "Suggested"} ${item.operation_kind}`}</small>
      </span>
      <span className="review-item-row__state">
        {item.is_previous_import_set ? `Previous set · ${humanize(item.state)}` : stateLabel(item.state)}
      </span>
    </button>
  );

  return (
    <section className="materials-workspace review-inbox-workspace" aria-labelledby="review-inbox-title">
      <p className="sr-only" role="status" aria-live="polite" aria-atomic="true">{announcement}</p>

      <header className="materials-commandbar review-inbox-commandbar">
        <div className="materials-commandbar__title">
          <button type="button" className="profile-back-button" onClick={onBack} disabled={interactionLocked}>
            <span aria-hidden="true">←</span> {backLabel}
          </button>
          <div>
            <p className="section-kicker">Local profile suggestions</p>
            <h2 id="review-inbox-title" ref={headingRef} tabIndex={-1}>Profile suggestions</h2>
            <span>{counts.inbox} in Inbox · Nothing joins your profile until you explicitly apply it</span>
          </div>
        </div>
      </header>

      <div className="review-inbox-browser">
        <aside className="materials-list-pane review-inbox-list-pane" aria-label="Review inbox items">
          <header className="pane-header">
            <div>
              <p className="section-kicker">Review queue</p>
              <h3>{SCOPE_LABELS[scope]}</h3>
            </div>
            <span className="count-label">{counts[scope]}</span>
          </header>

          <div className="segmented-control" role="tablist" aria-label="Review item state">
            {(Object.keys(SCOPE_LABELS) as ReviewInboxScope[]).map((value) => (
              <button
                key={value}
                ref={(node) => {
                  if (node) scopeRefs.current.set(value, node);
                  else scopeRefs.current.delete(value);
                }}
                type="button"
                role="tab"
                className={scope === value ? "is-selected" : ""}
                id={`review-scope-tab-${value}`}
                aria-selected={scope === value}
                aria-controls="review-inbox-item-list"
                tabIndex={scope === value ? 0 : -1}
                onClick={() => selectScope(value)}
                onKeyDown={(event) => selectScopeByArrow(event, value)}
                disabled={interactionLocked}
              >
                {SCOPE_LABELS[value]} <span className="review-scope-count">{counts[value]}</span>
              </button>
            ))}
          </div>

          {transitionError && (
            <div className="pane-error review-transition-error" role="alert">
              <p>{transitionError}</p>
              {transitionUncertain && pendingTransition && (
                <button
                  type="button"
                  className="secondary-action"
                  onClick={() => void performTransition(pendingTransitionRef.current ?? pendingTransition)}
                  disabled={transitionBusy}
                >
                  {transitionBusy ? "Checking…" : `Retry exact ${pendingTransition.action}`}
                </button>
              )}
            </div>
          )}

          <nav
            id="review-inbox-item-list"
            className="review-inbox-list"
            role="tabpanel"
            aria-labelledby={`review-scope-tab-${scope}`}
            aria-label={`${SCOPE_LABELS[scope]} review items`}
            aria-busy={loading || loadingMore}
            onKeyDown={selectByArrow}
          >
            {loading && items.length === 0 && <p className="pane-placeholder">Reading local review items…</p>}
            {!loading && listError && items.length === 0 && (
              <div className="pane-error" role="alert">
                <p>{listError}</p>
                <button type="button" onClick={() => void refreshCurrentScope(false)}>Try again</button>
              </div>
            )}
            {!loading && !listError && items.length === 0 && (
              <div className="pane-placeholder">
                <strong>No {SCOPE_LABELS[scope].toLowerCase()} items</strong>
                <span>
                  {scope === "inbox"
                    ? "Suggestions from imported sources and memories will appear here."
                    : scope === "deferred"
                      ? "Items you set aside will remain available here."
                      : "Applied items, rejected items, and suggestions from earlier import sets remain here."}
                </span>
              </div>
            )}

            {scope === "inbox" && firstItem && (
              <section className="review-item-group" aria-labelledby="review-start-here-label">
                <p id="review-start-here-label" className="review-item-group__label">Start here</p>
                {renderRow(firstItem, true)}
              </section>
            )}

            {laterItems.length > 0 && (
              <section className="review-item-group" aria-labelledby="review-up-next-label">
                <p id="review-up-next-label" className="review-item-group__label">
                  {scope === "inbox" ? "Up next" : SCOPE_LABELS[scope]}
                </p>
                {laterItems.map((item) => renderRow(item, false))}
              </section>
            )}

            {listError && items.length > 0 && (
              <div className="pane-error pane-error--compact" role="alert">
                <p>{listError}</p>
                <button type="button" onClick={() => void refreshCurrentScope(false)}>Reload view</button>
              </div>
            )}

            {canLoadMore && !listError && (
              <button
                type="button"
                className="load-more-row"
                onClick={() => void loadPage(scope, page!.next_offset as number, true)}
                disabled={loadingMore || interactionLocked}
              >
                {loadingMore ? "Loading…" : `Load more (${page!.total_items - items.length})`}
              </button>
            )}
          </nav>
        </aside>

        <main id="review-inbox-detail" className="review-inbox-inspector" aria-busy={operationBusy}>
          {selected ? (
            <article className="review-inbox-document" aria-labelledby="review-item-title">
              <header className="document-header review-item-header">
                <div className="document-icon" aria-hidden="true">{CANDIDATE_GLYPHS[selected.candidate_kind]}</div>
                <div>
                  <p className="section-kicker">{selected.state === "applied" ? "Applied" : "Suggested"} {CANDIDATE_LABELS[selected.candidate_kind]}</p>
                  <h2 id="review-item-title">{selected.title}</h2>
                  <p>
                    {[selected.subtitle, selected.date_range].filter(Boolean).join(" · ")
                      || `${selected.state === "applied" ? "Applied" : humanize(selected.operation_kind)} candidate linked to saved local evidence`}
                  </p>
                </div>
                <span className={`semantic-pill review-state-pill review-state-pill--${selected.state}`}>
                  {selected.is_previous_import_set ? "Previous import set" : stateLabel(selected.state)}
                </span>
              </header>

              <div className="review-item-honesty" role="note">
                <span aria-hidden="true">◇</span>
                <p>
                  <strong>
                    {selected.state === "applied"
                      ? "This suggestion has been applied."
                      : "This suggestion is not part of your profile."}
                  </strong>
                  {selected.state === "applied"
                    ? "It is retained here as read-only history; applying it created a separate immutable profile version."
                    : "Apply creates one immutable profile version. Defer, Reject, and Return to Inbox only organize the suggestion."}
                </p>
              </div>
              {selected.state === "applied" && applyError && (
                <div className="profile-review-inline-error review-applied-refresh-error" role="alert">
                  <p>{applyError}</p>
                  <button
                    type="button"
                    className="secondary-action"
                    onClick={() => {
                      setApplyError(null);
                      void refreshCurrentScope(false);
                    }}
                  >
                    Reload review view
                  </button>
                </div>
              )}

              <div className="review-inbox-detail-grid">
                <div className="review-item-proposal">
                  {editorOpen && editingItemId === selected.id && editForm ? (
                    <section className="review-apply-editor" aria-labelledby="review-apply-editor-title">
                      <header>
                        <p className="section-kicker">Unsaved proposal</p>
                        <h3 id="review-apply-editor-title" tabIndex={-1}>Edit before applying</h3>
                        <p>Adjust only what should enter the profile. The source suggestion and evidence remain unchanged.</p>
                      </header>
                      <form onSubmit={submitEditedApply} aria-describedby={applyError ? "review-apply-error" : undefined}>
                        <fieldset disabled={applyBusy || applyUncertain || applyNeedsReload}>
                          <ProfileCollectionFields
                            form={editForm}
                            setForm={setEditForm}
                            firstInputRef={applyEditFirstInputRef}
                            errorId={applyError ? "review-apply-error" : undefined}
                          />
                        </fieldset>
                        {applyError && (
                          <div id="review-apply-error" className="profile-review-inline-error review-apply-error" role="alert">
                            <p>{applyError}</p>
                            {applyUncertain && pendingApply && (
                              <button
                                type="button"
                                className="secondary-action"
                                onClick={() => void performApply(pendingApplyRef.current ?? pendingApply)}
                                disabled={applyBusy}
                              >
                                {applyBusy ? "Checking…" : "Retry exact apply"}
                              </button>
                            )}
                            {applyNeedsReload && (
                              <button type="button" className="secondary-action" onClick={() => void reloadAfterFailedApply()} disabled={applyBusy}>
                                Reload latest item
                              </button>
                            )}
                          </div>
                        )}
                        <footer className="review-apply-editor__actions">
                          <span>
                            {editedCandidateChanged
                              ? "Edited locally · not yet in profile"
                              : "Original proposal · not yet in profile"}
                          </span>
                          <div>
                            <button
                              type="button"
                              className="quiet-action"
                              onClick={closeEditor}
                              aria-disabled={applyBusy || applyUncertain}
                            >
                              {applyNeedsReload ? "Reload and close draft" : "Cancel"}
                            </button>
                            <button type="submit" className="primary-action" disabled={applyBusy || applyUncertain || applyNeedsReload}>
                              {applyBusy ? "Applying…" : editedCandidateChanged ? "Apply edited proposal" : "Apply proposal"}
                            </button>
                          </div>
                        </footer>
                      </form>
                    </section>
                  ) : (
                    <section className="inspector-section" aria-labelledby="review-proposal-title">
                      <h3 id="review-proposal-title">{selected.state === "applied" ? "Applied" : "Proposed"} {selected.operation_kind === "add" ? "addition" : "update"}</h3>
                      {selected.summary && <p className="review-item-summary">{selected.summary}</p>}
                      {selected.details.length > 0 && (
                        <dl className="property-grid review-item-properties">
                          {selected.details.map((detail, index) => (
                            <div key={`${detail.label}:${index}`}><dt>{detail.label}</dt><dd>{detail.value}</dd></div>
                          ))}
                        </dl>
                      )}
                      {selected.highlights.length > 0 && (
                        <div className="review-item-copy-list">
                          <h4>Highlights</h4>
                          <ul>{selected.highlights.map((highlight, index) => <li key={index}>{highlight}</li>)}</ul>
                        </div>
                      )}
                      {selected.tags.length > 0 && (
                        <ul className="profile-review-tags review-item-tags" aria-label="Suggested tags">
                          {selected.tags.map((tag, index) => <li key={`${tag}:${index}`}>{tag}</li>)}
                        </ul>
                      )}
                      {selected.changed_fields.length > 0 && (
                        <p className="review-item-fields">
                          {selected.state === "applied" ? "Applied" : "Suggested"} fields: {selected.changed_fields.map(humanize).join(", ")}.
                        </p>
                      )}
                    </section>
                  )}

                  {!editorOpen && (selected.is_previous_import_set || selected.state === "applied") ? (
                    <section className="review-item-actions review-item-actions--historical" aria-labelledby="review-actions-title">
                      <div>
                        <h3 id="review-actions-title">{selected.state === "applied" ? "Applied suggestion" : "Previous import set"}</h3>
                        <p>
                          {selected.state === "applied"
                            ? "This record is read-only. The resulting profile version remains available in Profile review and version history."
                            : "This item remains as read-only history. Newer imports cannot change, reopen, or apply it."}
                        </p>
                      </div>
                    </section>
                  ) : !editorOpen ? (
                    <section className="review-item-actions" aria-labelledby="review-actions-title">
                      <div>
                        <h3 id="review-actions-title">{selected.state === "inbox" ? "Apply or organize" : "Organize this item"}</h3>
                        <p>
                          {selected.state === "inbox"
                            ? "Apply the exact proposal or edit it first. Either choice creates one new profile version."
                            : "Return this item to Inbox before applying it. Organizing actions never edit the profile or retained evidence."}
                        </p>
                      </div>
                      <div className="review-item-actions__buttons">
                        {selected.state === "inbox" && (
                          <>
                            <button
                              type="button"
                              className="primary-action"
                              onClick={() => requestApply(selected, selected.candidate)}
                              disabled={interactionLocked || !page?.current_profile_version_id}
                            >
                              {applyBusy ? "Applying…" : "Apply to profile"}
                            </button>
                            <button
                              ref={editApplyTriggerRef}
                              type="button"
                              className="secondary-action"
                              onClick={() => startEditor(selected)}
                              disabled={interactionLocked || !page?.current_profile_version_id}
                            >
                              Edit first
                            </button>
                            <span className="review-item-actions__separator" aria-hidden="true" />
                            <button type="button" className="quiet-action" onClick={() => requestTransition(selected, "defer")} disabled={interactionLocked}>
                              Defer
                            </button>
                          </>
                        )}
                        {selected.state === "deferred" && (
                          <button type="button" className="secondary-action" onClick={() => requestTransition(selected, "reopen")} disabled={interactionLocked}>
                            Return to Inbox
                          </button>
                        )}
                        {(selected.state === "inbox" || selected.state === "deferred") && (
                          <button type="button" className="quiet-action" onClick={() => requestTransition(selected, "reject")} disabled={interactionLocked}>
                            Reject
                          </button>
                        )}
                        {selected.state === "rejected" && (
                          <button type="button" className="secondary-action" onClick={() => requestTransition(selected, "reopen")} disabled={interactionLocked}>
                            Reopen in Inbox
                          </button>
                        )}
                      </div>
                      {applyError && (
                        <div id="review-apply-error" className="profile-review-inline-error review-apply-error" role="alert">
                          <p>{applyError}</p>
                          {applyUncertain && pendingApply && (
                            <button
                              type="button"
                              className="secondary-action"
                              onClick={() => void performApply(pendingApplyRef.current ?? pendingApply)}
                              disabled={applyBusy}
                            >
                              {applyBusy ? "Checking…" : "Retry exact apply"}
                            </button>
                          )}
                          {applyNeedsReload && (
                            <button type="button" className="secondary-action" onClick={() => void reloadAfterFailedApply()} disabled={applyBusy}>
                              Reload latest item
                            </button>
                          )}
                        </div>
                      )}
                    </section>
                  ) : null}
                </div>

                <aside className="review-item-evidence" aria-labelledby="review-evidence-title">
                  <header>
                    <p className="section-kicker">Local evidence</p>
                    <h3 id="review-evidence-title">Why this appeared</h3>
                    <span>{selected.evidence.length} {selected.evidence.length === 1 ? "excerpt" : "excerpts"}</span>
                    {selected.state === "applied" && (
                      <p>Evidence preserves the source of this suggestion; the applied fields shown at left include any edits made before applying.</p>
                    )}
                  </header>
                  {selected.evidence.map((evidence, index) => (
                    <article key={`${evidence.display_name}:${index}`}>
                      <header>
                        <strong>{evidence.display_name}</strong>
                        <small>{humanize(evidence.source_kind)} · {evidence.source_format.toUpperCase()}</small>
                      </header>
                      {evidence.location_label && <span>{evidence.location_label}</span>}
                      <blockquote>{evidence.excerpt}</blockquote>
                    </article>
                  ))}
                  <p className="review-evidence-privacy">
                    Only relevant excerpts are shown here. Full original memories remain in Memories;
                    imported documents remain in Imported files. Neither is changed when you edit a suggestion.
                  </p>
                  <dl className="review-item-lineage">
                    <div><dt>Suggestion saved</dt><dd>{formatTimestamp(selected.created_at_ms)}</dd></div>
                    <div><dt>Candidate</dt><dd>{CANDIDATE_LABELS[selected.candidate_kind]}</dd></div>
                    <div><dt>Operation</dt><dd>{humanize(selected.operation_kind)}</dd></div>
                  </dl>
                </aside>
              </div>
            </article>
          ) : (
            <div className="empty-document">
              <div className="empty-document__glyph" aria-hidden="true">◇</div>
              <h2>{loading ? "Opening local review items" : `No ${SCOPE_LABELS[scope].toLowerCase()} item selected`}</h2>
              <p>
                {loading
                  ? "CVGnome is reading bounded suggestions and evidence from the local vault."
                  : "Choose another review state or return to Profile review."}
              </p>
            </div>
          )}
        </main>
      </div>
    </section>
  );
}
