// SPDX-License-Identifier: MPL-2.0
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";

export type RetainedSourceScope = "active" | "historical" | "all";

type RetainedSourceItem = {
  import_id: string;
  ordinal: number;
  display_name: string;
  source_format: string;
  source_kind: string;
  byte_size: number;
  extraction_status: string;
  issue_code: string | null;
  retention_generation: number;
  retention_status: string;
  committed_at_ms: number;
  profile_version_number: number;
  import_mode: string;
  import_file_count: number;
  content_reference_count: number;
};

type RetainedSourceList = {
  scope: RetainedSourceScope;
  current_generation: number;
  offset: number;
  limit: number;
  total_entries: number;
  total_unique_files: number;
  total_imports: number;
  items: RetainedSourceItem[];
};

type SourcesPanelProps = {
  reloadKey: string;
};

const SCOPE_LABELS: Record<RetainedSourceScope, string> = {
  active: "Active",
  historical: "History",
  all: "All",
};

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value < 0) return "Unknown";
  if (value < 1_024) return `${value} B`;
  if (value < 1_048_576) return `${(value / 1_024).toFixed(1)} KB`;
  return `${(value / 1_048_576).toFixed(1)} MB`;
}

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

function safeListError(caught: unknown): string {
  const value = caught instanceof Error ? caught.message : String(caught);
  if (value.toLowerCase().includes("vault_busy")) {
    return "The local vault is busy. Wait a moment and try again.";
  }
  return "CVGnome could not read the retained import entries from the local vault.";
}

export function SourcesPanel({ reloadKey }: SourcesPanelProps) {
  const [scope, setScope] = useState<RetainedSourceScope>("active");
  const [result, setResult] = useState<RetainedSourceList | null>(null);
  const [items, setItems] = useState<RetainedSourceItem[]>([]);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestRef = useRef(0);
  const rowRefs = useRef(new Map<string, HTMLButtonElement>());

  const keyFor = useCallback(
    (item: RetainedSourceItem) => `${item.import_id}:${item.ordinal}`,
    [],
  );

  const loadPage = useCallback(async (
    nextScope: RetainedSourceScope,
    offset: number,
    append: boolean,
  ) => {
    const requestId = ++requestRef.current;
    append ? setLoadingMore(true) : setLoading(true);
    setError(null);
    try {
      const next = await invoke<RetainedSourceList>("list_retained_profile_sources", {
        scope: nextScope,
        offset,
      });
      if (requestId !== requestRef.current) return;
      setResult(next);
      setItems((current) => {
        if (!append) return next.items;
        const seen = new Set(current.map(keyFor));
        return [...current, ...next.items.filter((item) => !seen.has(keyFor(item)))];
      });
      setSelectedKey((current) => {
        const available = append ? [...items, ...next.items] : next.items;
        return current && available.some((item) => keyFor(item) === current)
          ? current
          : available[0]
            ? keyFor(available[0])
            : null;
      });
    } catch (caught) {
      if (requestId === requestRef.current) setError(safeListError(caught));
    } finally {
      if (requestId === requestRef.current) {
        setLoading(false);
        setLoadingMore(false);
      }
    }
  }, [items, keyFor]);

  useEffect(() => {
    setItems([]);
    setResult(null);
    setSelectedKey(null);
    void loadPage(scope, 0, false);
    return () => {
      requestRef.current += 1;
    };
    // reloadKey intentionally invalidates the vault-derived list after imports or resets.
  }, [scope, reloadKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const selected = useMemo(
    () => items.find((item) => keyFor(item) === selectedKey) ?? null,
    [items, keyFor, selectedKey],
  );

  const selectByArrow = useCallback((direction: -1 | 1) => {
    if (items.length === 0) return;
    const currentIndex = Math.max(0, items.findIndex((item) => keyFor(item) === selectedKey));
    const nextIndex = Math.min(items.length - 1, Math.max(0, currentIndex + direction));
    const nextKey = keyFor(items[nextIndex]);
    setSelectedKey(nextKey);
    window.requestAnimationFrame(() => rowRefs.current.get(nextKey)?.focus());
  }, [items, keyFor, selectedKey]);

  const loadedCount = items.length;
  const canLoadMore = Boolean(result && loadedCount < result.total_entries);

  return (
    <section className="materials-browser" aria-labelledby="materials-browser-title">
      <aside className="materials-list-pane" aria-label="Retained imported files">
        <header className="pane-header">
          <div>
            <p className="section-kicker">Evidence archive</p>
            <h2 id="materials-browser-title">Imported files</h2>
          </div>
          {result && <span className="count-label">{result.total_entries}</span>}
        </header>

        <div className="segmented-control" aria-label="Imported file retention scope">
          {(Object.keys(SCOPE_LABELS) as RetainedSourceScope[]).map((value) => (
            <button
              key={value}
              type="button"
              className={scope === value ? "is-selected" : ""}
              onClick={() => setScope(value)}
              aria-pressed={scope === value}
            >
              {SCOPE_LABELS[value]}
            </button>
          ))}
        </div>

        {result && (
          <p className="materials-totals" role="status">
            {result.total_unique_files} unique {result.total_unique_files === 1 ? "content" : "contents"}
            <span aria-hidden="true"> · </span>
            {result.total_imports} {result.total_imports === 1 ? "import" : "imports"}
          </p>
        )}

        {error && (
          <div className="pane-error" role="alert">
            <p>{error}</p>
            <button type="button" onClick={() => void loadPage(scope, 0, false)}>Try again</button>
          </div>
        )}

        <div
          className="materials-list"
          role="listbox"
          aria-label={`${SCOPE_LABELS[scope]} import entries`}
          aria-busy={loading || loadingMore}
          onKeyDown={(event) => {
            if (event.key === "ArrowDown" || event.key === "ArrowUp") {
              event.preventDefault();
              selectByArrow(event.key === "ArrowDown" ? 1 : -1);
            }
          }}
        >
          {loading && items.length === 0 && <p className="pane-placeholder">Reading local archive…</p>}
          {!loading && !error && items.length === 0 && (
            <div className="pane-placeholder">
              <strong>No {scope === "all" ? "retained" : scope} import entries</strong>
              <span>Import source files to build an evidence trail.</span>
            </div>
          )}
          {items.map((item) => {
            const itemKey = keyFor(item);
            const selectedRow = itemKey === selectedKey;
            return (
              <button
                key={itemKey}
                ref={(node) => {
                  if (node) rowRefs.current.set(itemKey, node);
                  else rowRefs.current.delete(itemKey);
                }}
                type="button"
                role="option"
                aria-selected={selectedRow}
                className={`material-row${selectedRow ? " material-row--selected" : ""}`}
                onClick={() => setSelectedKey(itemKey)}
              >
                <span className="file-glyph" aria-hidden="true">{item.source_format.slice(0, 3).toUpperCase()}</span>
                <span className="material-row__copy">
                  <strong title={item.display_name}>{item.display_name}</strong>
                  <small>
                    {humanize(item.source_kind)} · {formatBytes(item.byte_size)}
                  </small>
                </span>
                <span className={`status-dot status-dot--${item.retention_status}`} aria-hidden="true" />
              </button>
            );
          })}

          {canLoadMore && (
            <button
              type="button"
              className="load-more-row"
              onClick={() => void loadPage(scope, loadedCount, true)}
              disabled={loadingMore}
            >
              {loadingMore ? "Loading…" : `Load more (${result!.total_entries - loadedCount})`}
            </button>
          )}
        </div>
      </aside>

      <div className="material-inspector">
        {selected ? (
          <article className="inspector-document" aria-labelledby="material-title">
            <header className="document-header">
              <div className="document-icon" aria-hidden="true">{selected.source_format.slice(0, 3).toUpperCase()}</div>
              <div>
                <p className="section-kicker">Retained import entry</p>
                <h2 id="material-title">{selected.display_name}</h2>
                <p>{humanize(selected.source_kind)} · {formatBytes(selected.byte_size)}</p>
              </div>
              <span className={`semantic-pill semantic-pill--${selected.retention_status}`}>
                {humanize(selected.retention_status)}
              </span>
            </header>

            <section className="inspector-section" aria-labelledby="material-file-title">
              <h3 id="material-file-title">Material</h3>
              <dl className="property-grid">
                <div><dt>Format</dt><dd>{selected.source_format.toUpperCase()}</dd></div>
                <div><dt>Classification</dt><dd>{humanize(selected.source_kind)}</dd></div>
                <div><dt>Size</dt><dd>{formatBytes(selected.byte_size)}</dd></div>
                <div><dt>Extraction</dt><dd>{humanize(selected.extraction_status)}</dd></div>
                {selected.issue_code && <div><dt>Issue</dt><dd>{humanize(selected.issue_code)}</dd></div>}
              </dl>
            </section>

            <section className="inspector-section" aria-labelledby="material-lineage-title">
              <h3 id="material-lineage-title">Lineage</h3>
              <dl className="property-grid">
                <div><dt>Imported</dt><dd>{formatTimestamp(selected.committed_at_ms)}</dd></div>
                <div><dt>Import route</dt><dd>{humanize(selected.import_mode)}</dd></div>
                <div><dt>Import position</dt><dd>{selected.ordinal + 1} of {selected.import_file_count}</dd></div>
                <div><dt>Retained set</dt><dd>{selected.retention_generation}</dd></div>
                <div><dt>Profile version</dt><dd>{selected.profile_version_number}</dd></div>
                <div>
                  <dt>Content references</dt>
                  <dd>{selected.content_reference_count} import {selected.content_reference_count === 1 ? "entry" : "entries"}</dd>
                </div>
              </dl>
              <p className="inspector-note">
                This view intentionally exposes file metadata and lineage only. Managed paths,
                content identifiers, and extracted text remain private to the local engine.
              </p>
            </section>
          </article>
        ) : (
          <div className="empty-document">
            <div className="empty-document__glyph" aria-hidden="true">⌁</div>
            <h2>Select an import entry</h2>
            <p>Choose a retained material from the list to inspect its local evidence lineage.</p>
          </div>
        )}
      </div>
    </section>
  );
}
