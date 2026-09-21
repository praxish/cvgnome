// SPDX-License-Identifier: MPL-2.0
import { isTauri } from "@tauri-apps/api/core";
import { getCurrentWebview } from "@tauri-apps/api/webview";

const UI_ZOOM_STORAGE_KEY = "cvgnome.ui-zoom.v1";

export const UI_ZOOM_LEVELS = [0.75, 0.8, 0.9, 1, 1.1, 1.2, 1.3, 1.4, 1.5, 1.75, 2, 2.5, 3] as const;
export const DEFAULT_UI_ZOOM = 1;

function nearestLevel(value: number): number {
  if (!Number.isFinite(value)) return DEFAULT_UI_ZOOM;
  return UI_ZOOM_LEVELS.reduce((nearest, candidate) =>
    Math.abs(candidate - value) < Math.abs(nearest - value) ? candidate : nearest,
  DEFAULT_UI_ZOOM);
}

export function readUiZoom(): number {
  try {
    const stored = window.localStorage.getItem(UI_ZOOM_STORAGE_KEY);
    return stored === null ? DEFAULT_UI_ZOOM : nearestLevel(Number(stored));
  } catch {
    return DEFAULT_UI_ZOOM;
  }
}

export function steppedUiZoom(current: number, direction: -1 | 1): number {
  const normalized = nearestLevel(current);
  const index = UI_ZOOM_LEVELS.findIndex((level) => level === normalized);
  const nextIndex = Math.max(0, Math.min(UI_ZOOM_LEVELS.length - 1, index + direction));
  return UI_ZOOM_LEVELS[nextIndex];
}

export async function applyUiZoom(value: number): Promise<number> {
  const normalized = nearestLevel(value);
  try {
    window.localStorage.setItem(UI_ZOOM_STORAGE_KEY, String(normalized));
  } catch {
    // A zoom preference is useful but never required for the workspace to run.
  }

  document.documentElement.style.removeProperty("zoom");
  if (isTauri()) {
    try {
      await getCurrentWebview().setZoom(normalized);
      return normalized;
    } catch {
      // Preserve usable zoom in development or on an unsupported webview.
    }
  }
  document.documentElement.style.setProperty("zoom", String(normalized));
  return normalized;
}

export function uiZoomPercent(value: number): string {
  return `${Math.round(nearestLevel(value) * 100)}%`;
}
