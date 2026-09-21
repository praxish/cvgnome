// SPDX-License-Identifier: MPL-2.0
import { useCallback, useState } from "react";
import { ReviewInboxPanel, type ReviewInboxCounts, type ReviewItemApplyReceipt } from "./ReviewInboxPanel";
import { SourceReviewPanel, type SourceReviewReceipt } from "./SourceReviewPanel";

export function ReviewWorkspace({ suggestionCounts, sourceCounts, initialTab, backLabel, onBack, onStatusRefresh, onSuggestionApplied, onSourceChoicesApplied, onMutationBusyChange }: {
  suggestionCounts: ReviewInboxCounts;
  sourceCounts: ReviewInboxCounts;
  initialTab?: "suggestions" | "sources";
  backLabel: string;
  onBack: () => void;
  onStatusRefresh: () => Promise<void>;
  onSuggestionApplied: (receipt: ReviewItemApplyReceipt) => Promise<void>;
  onSourceChoicesApplied: (receipt: SourceReviewReceipt) => Promise<void>;
  onMutationBusyChange: (busy: boolean) => void;
}) {
  const [activeTab, setActiveTab] = useState<"suggestions" | "sources">(() => initialTab ?? (sourceCounts.inbox > 0 || (suggestionCounts.inbox === 0 && sourceCounts.deferred > 0) ? "sources" : "suggestions"));
  const [locked, setLocked] = useState(false);
  const mutationChanged = useCallback((busy: boolean) => {
    setLocked(busy);
    onMutationBusyChange(busy);
  }, [onMutationBusyChange]);
  return <div className="review-workspace">
    <div className="review-workspace-tabs" aria-label="Review area">
      <button type="button" className={activeTab === "suggestions" ? "is-selected" : ""} aria-pressed={activeTab === "suggestions"} disabled={locked} onClick={() => setActiveTab("suggestions")}>Profile suggestions <span>{suggestionCounts.inbox + suggestionCounts.deferred}</span></button>
      <button type="button" className={activeTab === "sources" ? "is-selected" : ""} aria-pressed={activeTab === "sources"} disabled={locked} onClick={() => setActiveTab("sources")}>Source checks <span>{sourceCounts.inbox + sourceCounts.deferred}</span></button>
    </div>
    {activeTab === "sources" ? <SourceReviewPanel initialCounts={sourceCounts} backLabel={backLabel} onBack={onBack} onApplied={onSourceChoicesApplied} onMutationBusyChange={mutationChanged} />
      : <ReviewInboxPanel initialCounts={suggestionCounts} backLabel={backLabel} onBack={onBack} onStatusRefresh={onStatusRefresh} onApplied={onSuggestionApplied} onMutationBusyChange={mutationChanged} />}
  </div>;
}
