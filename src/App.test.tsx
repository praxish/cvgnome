// SPDX-License-Identifier: MPL-2.0
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import App from "./App";
import { nativeMock } from "./test/nativeMock";

const emptyStatus = {
  engine_version: "0.5.1", schema_version: 19, profile_versions: 0,
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

const blockedScan = {
  scan_id: "00000000-0000-4000-8000-000000000041", expires_at_ms: 9_999_999_999_999,
  base_profile: null,
  file_counts: { discovered: 1, staged: 1, parsed: 1, duplicates: 0, skipped: 0, failed: 0 },
  source_counts: { resume: 1, evidence: 0, preferences: 0, unclassified: 0 },
  format_counts: { pdf: 1 },
  warnings: [
    { code: "missing_identity", stage: "synthesis", severity: "blocking", count: 1 },
    { code: "insufficient_resume_content", stage: "synthesis", severity: "blocking", count: 1 },
  ],
  review_candidate_count: 0, source_review_count: 0, can_build: false,
};

const recoveredProfile = {
  scan_id: blockedScan.scan_id, profile_version_id: "00000000-0000-4000-8000-000000000042",
  version_number: 1, created: true, checksum_sha256: "a".repeat(64),
  profile_name: "Ada Example", renderable: false,
  source_counts: { parsed: 1, used: 1, unused: 0 },
  profile_counts: { work_entries: 0, education_entries: 0, skill_groups: 0, evidence_claims: 0, preference_items: 0 },
  warnings: [], review_item_count: 0, source_review_count: 0,
};

const recoveredStatus = {
  ...emptyStatus, profile_versions: 1, latest_profile_name: "Ada Example",
  latest_profile_renderable: false, retained_source_files: 1, source_imports: 1,
};

async function openBlockedScan(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Choose files…" }));
  await user.click(await screen.findByRole("button", { name: "Complete details and keep files" }));
}

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

  it("recovers a blocked import as an incomplete profile without enabling export or discarding files", async () => {
    let saved = false;
    nativeMock.invoke.mockImplementation(async (command) => {
      if (command === "engine_status") return saved ? recoveredStatus : emptyStatus;
      if (command === "scan_profile_source_files") return blockedScan;
      if (command === "recover_profile_sources") { saved = true; return recoveredProfile; }
      throw new Error(`Unexpected command: ${command}`);
    });
    const user = userEvent.setup();
    render(<App />);
    await openBlockedScan(user);
    expect(screen.getByText(/Reading a document does not mean its name/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save profile and keep files" })).toBeDisabled();
    await user.type(screen.getByRole("textbox", { name: /^Name/ }), "Ada Example");
    await user.click(screen.getByRole("button", { name: "Save profile and keep files" }));
    expect(await screen.findByText(/This incomplete profile and its files are saved/)).toBeInTheDocument();
    expect(screen.queryByText("Resume ready")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Export PDF" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Export DOCX" })).toBeDisabled();
    expect(nativeMock.invoke).toHaveBeenCalledWith("recover_profile_sources", {
      scanId: blockedScan.scan_id, patch: { name: "Ada Example", summary: null },
    });
    expect(nativeMock.invoke.mock.calls.some(([command]) => command === "discard_profile_source_scan" || command === "start_manual_profile")).toBe(false);
  });

  it.each(["transport failure", "invalid receipt"])("locks an uncertain import recovery after %s and retries its exact corrections", async (failure) => {
    let attempts = 0;
    nativeMock.invoke.mockImplementation(async (command) => {
      if (command === "engine_status") return attempts > 1 ? recoveredStatus : emptyStatus;
      if (command === "scan_profile_source_files") return blockedScan;
      if (command === "recover_profile_sources") {
        if (++attempts === 1) {
          if (failure === "invalid receipt") return { ...recoveredProfile, scan_id: "unexpected-scan" };
          throw new Error("The local engine timed out");
        }
        return recoveredProfile;
      }
      throw new Error(`Unexpected command: ${command}`);
    });
    const user = userEvent.setup();
    render(<App />);
    await openBlockedScan(user);
    await user.type(screen.getByRole("textbox", { name: /^Name/ }), " Ada Example ");
    await user.type(screen.getByRole("textbox", { name: "Summary (optional)" }), "I design reliable systems.");
    await user.click(screen.getByRole("button", { name: "Save profile and keep files" }));
    const retry = await screen.findByRole("button", { name: "Retry exact request" });
    expect(screen.getByRole("textbox", { name: /^Name/ })).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "Summary (optional)" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Discard review" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Cancel corrections" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Roles" })).toBeDisabled();
    await closeWindow();
    expect(screen.getByRole("alertdialog")).toHaveAccessibleName("Finish the current action before closing");
    await user.click(screen.getByRole("button", { name: "Keep open" }));
    await user.click(retry);
    await screen.findByText(/This incomplete profile and its files are saved/);
    const requests = nativeMock.invoke.mock.calls.filter(([command]) => command === "recover_profile_sources");
    expect(requests).toHaveLength(2);
    expect(requests[0][1]).toEqual({ scanId: blockedScan.scan_id, patch: { name: "Ada Example", summary: "I design reliable systems." } });
    expect(requests[1][1]).toEqual(requests[0][1]);
    expect(nativeMock.destroy).not.toHaveBeenCalled();
  });

  it("resumes an unfinished first import without choosing or discarding the original files", async () => {
    nativeMock.invoke.mockImplementation(async (command) => {
      if (command === "engine_status") return { ...emptyStatus, source_previews: 1 };
      if (command === "resume_profile_source_scan") return { scan: blockedScan, recovery_patch: null };
      throw new Error(`Unexpected command: ${command}`);
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Resume import review" }));
    await user.click(await screen.findByRole("button", { name: "Complete details and keep files" }));
    expect(screen.getByRole("textbox", { name: /^Name/ })).toBeEnabled();
    expect(nativeMock.invoke.mock.calls.map(([command]) => command)).toEqual(["engine_status", "resume_profile_source_scan"]);
  });

  it("restores a pinned recovery after restart and sends only its exact saved patch", async () => {
    const patch = { name: "Ada Example", summary: "Confirmed before restart." };
    let saved = false;
    nativeMock.invoke.mockImplementation(async (command) => {
      if (command === "engine_status") return saved ? recoveredStatus : { ...emptyStatus, source_previews: 1 };
      if (command === "resume_profile_source_scan") return { scan: { ...blockedScan, can_build: true }, recovery_patch: patch };
      if (command === "recover_profile_sources") { saved = true; return recoveredProfile; }
      throw new Error(`Unexpected command: ${command}`);
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Resume import review" }));
    expect(await screen.findByRole("textbox", { name: /^Name/ })).toHaveValue(patch.name);
    expect(screen.getByRole("textbox", { name: "Summary (optional)" })).toHaveValue(patch.summary);
    expect(screen.getByRole("textbox", { name: /^Name/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Discard review" })).toBeDisabled();
    await closeWindow();
    expect(screen.getByRole("alertdialog")).toHaveAccessibleName("Finish the current action before closing");
    await user.click(screen.getByRole("button", { name: "Keep open" }));
    await user.click(screen.getByRole("button", { name: "Retry exact request" }));
    await screen.findByText(/This incomplete profile and its files are saved/);
    expect(nativeMock.invoke).toHaveBeenCalledWith("recover_profile_sources", { scanId: blockedScan.scan_id, patch });
  });

  it("allows corrections to be edited only after a known validation rejection", async () => {
    nativeMock.invoke.mockImplementation(async (command) => {
      if (command === "engine_status") return emptyStatus;
      if (command === "scan_profile_source_files") return blockedScan;
      if (command === "recover_profile_sources") throw new Error("invalid_params: invalid recovery details");
      throw new Error(`Unexpected command: ${command}`);
    });
    const user = userEvent.setup();
    render(<App />);
    await openBlockedScan(user);
    await user.type(screen.getByRole("textbox", { name: /^Name/ }), "Ada Example");
    await user.click(screen.getByRole("button", { name: "Save profile and keep files" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("These corrections were rejected");
    expect(screen.getByRole("textbox", { name: /^Name/ })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Discard review" })).toBeEnabled();
    await closeWindow();
    expect(screen.getByRole("alertdialog")).toHaveAccessibleName("Discard unsaved changes and close?");
  });

  it("resumes the original pinned corrections after a conflicting request", async () => {
    const originalPatch = { name: "Ada Example", summary: null };
    let attempts = 0;
    nativeMock.invoke.mockImplementation(async (command) => {
      if (command === "engine_status") return attempts > 1 ? recoveredStatus : emptyStatus;
      if (command === "scan_profile_source_files") return blockedScan;
      if (command === "resume_profile_source_scan") return { scan: blockedScan, recovery_patch: originalPatch };
      if (command === "recover_profile_sources") {
        if (++attempts === 1) throw new Error("profile_source_recovery_conflict: different pinned corrections");
        return recoveredProfile;
      }
      throw new Error(`Unexpected command: ${command}`);
    });
    const user = userEvent.setup();
    render(<App />);
    await openBlockedScan(user);
    await user.type(screen.getByRole("textbox", { name: /^Name/ }), "Different Example");
    await user.click(screen.getByRole("button", { name: "Save profile and keep files" }));
    const resume = await screen.findByRole("button", { name: "Resume original recovery" });
    expect(screen.getByRole("textbox", { name: /^Name/ })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Retry exact request" })).not.toBeInTheDocument();
    await user.click(resume);
    expect(await screen.findByRole("button", { name: "Retry exact request" })).toBeEnabled();
    expect(screen.getByRole("textbox", { name: /^Name/ })).toHaveValue(originalPatch.name);
    expect(screen.getByRole("textbox", { name: /^Name/ })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Retry exact request" }));
    await screen.findByText(/This incomplete profile and its files are saved/);
    expect(nativeMock.invoke).toHaveBeenLastCalledWith("engine_status");
    const requests = nativeMock.invoke.mock.calls.filter(([command]) => command === "recover_profile_sources");
    expect(requests).toHaveLength(2);
    expect(requests[1][1]).toEqual({ scanId: blockedScan.scan_id, patch: originalPatch });
  });
});
