// SPDX-License-Identifier: MPL-2.0
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import App from "./App";
import { nativeMock } from "./test/nativeMock";

const emptyStatus = {
  engine_version: "0.5.0", schema_version: 19, profile_versions: 0,
  opportunities: 0, artifacts: 0, pending_jobs: 0, source_snapshots: 0,
  source_imports: 0, source_previews: 0, latest_profile_name: null,
  latest_profile_renderable: null, source_retention_generation: 1,
  retained_source_files: 0, retained_source_bytes: 0, retained_source_imports: 0,
  historical_source_files: 0, historical_source_imports: 0,
  last_source_import_at_ms: null, source_retention_reset_at_ms: null,
  review_inbox_items: 0, review_deferred_items: 0, review_history_items: 0,
  source_review_inbox_items: 0, source_review_deferred_items: 0,
  source_review_history_items: 0, memory_count: 0,
};

async function closeWindow() {
  await waitFor(() => expect(nativeMock.closeHandler).not.toBeNull());
  const event = { preventDefault: vi.fn() };
  act(() => nativeMock.closeHandler?.(event));
  expect(event.preventDefault).toHaveBeenCalledOnce();
}

describe("first profile interactions", () => {
  it("waits for vault status before showing first-run actions and allows retry on failure", async () => {
    let rejectStatus!: (error: Error) => void;
    nativeMock.invoke.mockImplementation(() => new Promise((_resolve, reject) => { rejectStatus = reject; }));
    render(<App />);
    expect(screen.getByRole("heading", { name: "Opening your local workspace" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start manually" })).not.toBeInTheDocument();
    await act(async () => rejectStatus(new Error("engine unavailable")));
    expect(screen.getByRole("heading", { name: "Local workspace needs attention" })).toBeInTheDocument();
    nativeMock.invoke.mockResolvedValue(emptyStatus);
    await userEvent.click(screen.getByRole("button", { name: "Check again" }));
    expect(await screen.findByRole("button", { name: "Start manually" })).toBeEnabled();
  });

  it("protects an unsaved manual profile from native close without calling the engine to save", async () => {
    nativeMock.invoke.mockResolvedValue(emptyStatus);
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Start manually" }));
    await user.type(screen.getByRole("textbox", { name: /^Name/ }), "Ada Example");
    await closeWindow();
    expect(screen.getByRole("alertdialog")).toHaveAccessibleName("Discard unsaved changes and close?");
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    await user.keyboard("{Control>}2{/Control}");
    expect(confirm).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Keep open" }));
    expect(screen.getByRole("textbox", { name: /^Name/ })).toHaveValue("Ada Example");
    expect(nativeMock.invoke.mock.calls.map(([command]) => command)).toEqual(["engine_status"]);
    expect(nativeMock.destroy).not.toHaveBeenCalled();
  });

  it("blocks closing an uncertain manual save and retries the exact original request", async () => {
    nativeMock.invoke.mockImplementation(async (command) => {
      if (command === "engine_status") return emptyStatus;
      if (command === "start_manual_profile") throw new Error("The local engine timed out");
      throw new Error(`Unexpected command: ${command}`);
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Start manually" }));
    await user.type(screen.getByRole("textbox", { name: /^Name/ }), "Ada Example");
    await user.click(screen.getByRole("button", { name: "Create profile" }));
    const retry = await screen.findByRole("button", { name: "Retry exact request" });
    expect(screen.getByRole("textbox", { name: /^Name/ })).toBeDisabled();
    await closeWindow();
    expect(screen.getByRole("alertdialog")).toHaveAccessibleName("Finish the current action before closing");
    expect(screen.queryByRole("button", { name: "Discard and close" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Keep open" }));
    await user.click(retry);
    await waitFor(() => expect(nativeMock.invoke.mock.calls.filter(([command]) => command === "start_manual_profile")).toHaveLength(2));
    const requests = nativeMock.invoke.mock.calls.filter(([command]) => command === "start_manual_profile");
    expect(requests[1][1]).toEqual(requests[0][1]);
    expect(nativeMock.destroy).not.toHaveBeenCalled();
  });
});
