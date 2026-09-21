// SPDX-License-Identifier: MPL-2.0
export type OpportunitySignal = "strong" | "mixed" | "limited";

export type OpportunityTrackerStage =
  | "tracked"
  | "new"
  | "matched"
  | "to_apply"
  | "applied"
  | "interviewing"
  | "offer"
  | "rejected"
  | "closed"
  | "ignored";

export type OpportunityStageFilter = "all" | OpportunityTrackerStage;
export type OpportunitySort = "last_action" | "strength" | "stage";

export type OpportunityInput = {
  title: string;
  description: string;
  company?: string;
  location?: string;
  source_url?: string;
  apply_url?: string;
};

export type OpportunityTracker = {
  id: string;
  title: string;
  company: string;
  location: string;
  source_url: string | null;
  apply_url: string | null;
  status: string;
  tracker_stage: OpportunityTrackerStage;
  tracker_updated_at_ms: number;
  notes: string;
  created_at_ms: number;
  updated_at_ms: number;
};

export type OpportunitySnapshotSummary = {
  id: string;
  checksum_sha256: string;
  captured_at_ms: number;
};

export type OpportunitySnapshot = OpportunitySnapshotSummary & {
  description: string;
};

export type OpportunityEvidence = {
  term: string;
  profile_path: string;
  snippet: string;
};

export type OpportunityMatchSummary = {
  id: string;
  contract: string;
  score: number;
  signal: OpportunitySignal;
  created_at_ms: number;
};

export type OpportunityMatch = OpportunityMatchSummary & {
  matched_terms: string[];
  terms_to_review: string[];
  evidence: OpportunityEvidence[];
};

export type MatchedProfileVersion = {
  id: string;
  version_number: number;
  checksum_sha256: string;
};

export type OpportunityListItem = Omit<
  OpportunityTracker,
  "source_url" | "apply_url" | "notes"
> & {
  snapshot: OpportunitySnapshotSummary;
  match: OpportunityMatchSummary;
  profile_version: MatchedProfileVersion;
};

export type OpportunityListResult = {
  items: OpportunityListItem[];
  total: number;
};

export type OpportunityDetail = {
  opportunity: OpportunityTracker;
  snapshot: OpportunitySnapshot;
  match: OpportunityMatch;
  profile_version: MatchedProfileVersion;
};

export const OPPORTUNITY_LIMITS = {
  title: 240,
  company: 160,
  location: 160,
  description: 32_000,
  descriptionBytes: 32_000,
  url: 2_048,
  search: 200,
  notes: 4_000,
  listOffset: 10_000,
} as const;

export const SIGNAL_LABELS: Record<OpportunitySignal, string> = {
  strong: "Many clear connections",
  mixed: "Some clear connections",
  limited: "A few clear connections",
};

export const TRACKER_STAGE_LABELS: Record<OpportunityTrackerStage, string> = {
  tracked: "Tracked",
  new: "New",
  matched: "Matched",
  to_apply: "To apply",
  applied: "Applied",
  interviewing: "Interviewing",
  offer: "Offer",
  rejected: "Rejected",
  closed: "Closed",
  ignored: "Ignored",
};

export const TRACKER_STAGES = Object.keys(TRACKER_STAGE_LABELS) as OpportunityTrackerStage[];

export const STAGE_FILTER_LABELS: Record<OpportunityStageFilter, string> = {
  all: "All roles",
  ...TRACKER_STAGE_LABELS,
};

export const OPPORTUNITY_SORT_LABELS: Record<OpportunitySort, string> = {
  last_action: "Last action",
  strength: "Profile connections",
  stage: "Workflow stage",
};

function errorText(caught: unknown): string {
  return caught instanceof Error ? caught.message : String(caught);
}

export function safeOpportunityError(caught: unknown): string {
  const value = errorText(caught).trim().toLowerCase();
  if (value.includes("profile_missing")) {
    return "Create a local profile before checking a role.";
  }
  if (value.includes("opportunity_not_found")) {
    return "That saved role is no longer available. Refresh the role list.";
  }
  if (value.includes("tracker_conflict") || value.includes("updated") && value.includes("conflict")) {
    return "This role changed in another operation. Reopen it before saving your tracker update.";
  }
  if (value.includes("notes") && (value.includes("limit") || value.includes("long"))) {
    return "Keep tracker notes within 4,000 UTF-8 bytes.";
  }
  if (value.includes("url") && (value.includes("https") || value.includes("credential"))) {
    return "Reference links must be HTTPS URLs without a username or password.";
  }
  if (value.includes("description") && (value.includes("limit") || value.includes("long"))) {
    return "The posting text is too large for the local matching boundary. Shorten it and try again.";
  }
  if (value.includes("title") && (value.includes("required") || value.includes("limit"))) {
    return "Enter a role title within the displayed limit.";
  }
  if (value.includes("vault_busy")) {
    return "The local vault is busy. Wait a moment and try again.";
  }
  if (value.includes("timed out")) {
    return "The local evidence check took too long and was stopped. Nothing was sent anywhere.";
  }
  return "CVGnome could not complete that local role operation. Your existing profile and saved roles were not changed.";
}

export function formatOpportunityDate(timestamp: number): string {
  if (!Number.isFinite(timestamp)) return "Unknown date";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  }).format(new Date(timestamp));
}

export function formatOpportunityTimestamp(timestamp: number): string {
  if (!Number.isFinite(timestamp)) return "Unknown";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(timestamp));
}
