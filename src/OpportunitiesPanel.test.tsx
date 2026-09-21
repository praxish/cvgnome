// SPDX-License-Identifier: MPL-2.0
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { OpportunitiesPanel } from "./OpportunitiesPanel";
import { nativeMock } from "./test/nativeMock";

function renderRoles() {
  return render(<OpportunitiesPanel hasProfile workspaceState="ready" currentProfileVersion={1}
    initialOpportunityId={null} onCountChange={vi.fn()} onDirtyChange={vi.fn()}
    onSelectedOpportunityChange={vi.fn()} onOpenProfile={vi.fn()} onOpenModelSettings={vi.fn()}
    onEngineStatusRefresh={vi.fn().mockResolvedValue(undefined)} onTailoringBusyChange={vi.fn()} />);
}

describe("Roles list interactions", () => {
  it("loads all roles with a blank query on entry and reloads after clearing search", async () => {
    nativeMock.invoke.mockResolvedValue({ total: 0, items: [] });
    const user = userEvent.setup();
    renderRoles();
    await screen.findByText("No matching roles");
    expect(nativeMock.invoke).toHaveBeenCalledWith("list_opportunities", {
      query: "", offset: 0, stage: "all", sort: "last_action",
    });
    const search = screen.getByRole("searchbox");
    await user.type(search, "Python");
    await waitFor(() => expect(nativeMock.invoke).toHaveBeenLastCalledWith("list_opportunities", {
      query: "Python", offset: 0, stage: "all", sort: "last_action",
    }));
    await user.clear(search);
    await waitFor(() => expect(nativeMock.invoke).toHaveBeenLastCalledWith("list_opportunities", {
      query: "", offset: 0, stage: "all", sort: "last_action",
    }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows an IPC failure and recovers when the user changes the filter", async () => {
    nativeMock.invoke.mockRejectedValueOnce(new Error("vault_busy: locked"));
    nativeMock.invoke.mockResolvedValue({ total: 0, items: [] });
    renderRoles();
    expect(await screen.findByRole("alert")).toHaveTextContent("local vault is busy");
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Filter by workflow stage" }), "applied");
    await waitFor(() => expect(nativeMock.invoke).toHaveBeenLastCalledWith("list_opportunities", {
      query: "", offset: 0, stage: "applied", sort: "last_action",
    }));
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    expect(screen.getByText("No matching roles")).toBeInTheDocument();
  });
});
