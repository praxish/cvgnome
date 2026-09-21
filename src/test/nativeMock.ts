// SPDX-License-Identifier: MPL-2.0
import { vi } from "vitest";

const nativeMock = vi.hoisted(() => ({
  invoke: vi.fn<(command: string, args?: Record<string, unknown>) => Promise<unknown>>(),
  destroy: vi.fn<() => Promise<void>>(),
  unlisten: vi.fn(),
  onCloseRequested: vi.fn(),
  closeHandler: null as null | ((event: { preventDefault: () => void }) => void),
}));

export { nativeMock };

vi.mock("@tauri-apps/api/core", () => ({
  invoke: nativeMock.invoke,
  isTauri: () => true,
}));
vi.mock("@tauri-apps/api/window", () => ({
  getCurrentWindow: () => ({
    onCloseRequested: nativeMock.onCloseRequested,
    destroy: nativeMock.destroy,
  }),
}));
vi.mock("@tauri-apps/api/webview", () => ({
  getCurrentWebview: () => ({ setZoom: vi.fn().mockResolvedValue(undefined) }),
}));

export function resetNativeMock() {
  nativeMock.invoke.mockReset();
  nativeMock.destroy.mockReset().mockResolvedValue(undefined);
  nativeMock.unlisten.mockReset();
  nativeMock.closeHandler = null;
  nativeMock.onCloseRequested.mockReset().mockImplementation((handler) => {
    nativeMock.closeHandler = handler;
    return Promise.resolve(nativeMock.unlisten);
  });
}
