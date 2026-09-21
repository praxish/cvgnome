// SPDX-License-Identifier: MPL-2.0
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect, useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { DesktopCloseGuard, useDesktopCloseProtection } from "./DesktopCloseGuard";
import { nativeMock } from "./test/nativeMock";

async function requestClose() {
  await waitFor(() => expect(nativeMock.closeHandler).not.toBeNull());
  const event = { preventDefault: vi.fn() };
  act(() => nativeMock.closeHandler?.(event));
  expect(event.preventDefault).toHaveBeenCalledOnce();
}

function Surface({ blocked = false }: { blocked?: boolean }) {
  useDesktopCloseProtection({ dirty: !blocked, blocked });
  return <input aria-label="Draft" defaultValue="Unsaved words" />;
}

describe("native close protection", () => {
  it("closes a clean window once, without recursing through another close request", async () => {
    render(<DesktopCloseGuard dirty={false} blocked={false}><p>Saved workspace</p></DesktopCloseGuard>);
    await requestClose();
    await requestClose();
    expect(nativeMock.destroy).toHaveBeenCalledOnce();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("keeps a child draft, focuses Keep open, traps Tab, and restores focus on Escape", async () => {
    const user = userEvent.setup();
    render(<DesktopCloseGuard dirty={false} blocked={false}><Surface /></DesktopCloseGuard>);
    const draft = screen.getByLabelText("Draft");
    draft.focus();
    await requestClose();
    expect(screen.getByRole("button", { name: "Keep open" })).toHaveFocus();
    expect(draft.closest("[inert]")).not.toBeNull();
    await user.tab({ shift: true });
    expect(screen.getByRole("button", { name: "Discard and close" })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("button", { name: "Keep open" })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    await waitFor(() => expect(draft).toHaveFocus());
    expect(draft).toHaveValue("Unsaved words");
    expect(nativeMock.destroy).not.toHaveBeenCalled();
  });

  it("destroys only after the user explicitly discards the draft", async () => {
    const user = userEvent.setup();
    render(<DesktopCloseGuard dirty={true} blocked={false}><p>Draft</p></DesktopCloseGuard>);
    await requestClose();
    expect(nativeMock.destroy).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Discard and close" }));
    expect(nativeMock.destroy).toHaveBeenCalledOnce();
  });

  it("gives Escape to the close confirmation before an existing window-capture dialog", async () => {
    const lowerEscape = vi.fn();
    function Workspace() {
      const [confirming, setConfirming] = useState(false);
      useEffect(() => {
        if (!confirming) return;
        const handleEscape = (event: KeyboardEvent) => {
          if (event.key !== "Escape") return;
          event.preventDefault();
          event.stopImmediatePropagation();
          lowerEscape();
          setConfirming(false);
        };
        window.addEventListener("keydown", handleEscape, true);
        return () => window.removeEventListener("keydown", handleEscape, true);
      }, [confirming]);
      return <>
        <button onClick={() => setConfirming(true)}>Review draft</button>
        {confirming && <section role="dialog" aria-label="Apply draft changes">
          <button>Keep editing</button>
        </section>}
      </>;
    }
    const user = userEvent.setup();
    render(<DesktopCloseGuard dirty={true} blocked={false}><Workspace /></DesktopCloseGuard>);
    await user.click(screen.getByRole("button", { name: "Review draft" }));
    const keepEditing = screen.getByRole("button", { name: "Keep editing" });
    keepEditing.focus();
    await requestClose();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Apply draft changes" })).toBeInTheDocument();
    expect(lowerEscape).not.toHaveBeenCalled();
    await waitFor(() => expect(keepEditing).toHaveFocus());
    await user.keyboard("{Escape}");
    expect(lowerEscape).toHaveBeenCalledOnce();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(nativeMock.destroy).not.toHaveBeenCalled();
  });

  it("blocks close for a pending or uncertain mutation, including one begun after the prompt", async () => {
    const { rerender } = render(<DesktopCloseGuard dirty={true} blocked={false}><p>Draft</p></DesktopCloseGuard>);
    await requestClose();
    rerender(<DesktopCloseGuard dirty={true} blocked={true}><p>Saving</p></DesktopCloseGuard>);
    expect(screen.getByRole("alertdialog")).toHaveAccessibleName("Finish the current action before closing");
    expect(screen.queryByRole("button", { name: "Discard and close" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Keep open" })).toHaveFocus();
    expect(nativeMock.destroy).not.toHaveBeenCalled();
  });

  it("removes a departed surface's close lock", async () => {
    const { rerender } = render(<DesktopCloseGuard dirty={false} blocked={false}><Surface blocked /></DesktopCloseGuard>);
    await requestClose();
    expect(screen.getByRole("alertdialog")).toHaveAccessibleName("Finish the current action before closing");
    await userEvent.click(screen.getByRole("button", { name: "Keep open" }));
    rerender(<DesktopCloseGuard dirty={false} blocked={false}><p>Saved workspace</p></DesktopCloseGuard>);
    await requestClose();
    expect(nativeMock.destroy).toHaveBeenCalledOnce();
  });

  it("keeps the workspace open and allows retry if native destruction fails", async () => {
    nativeMock.destroy.mockRejectedValueOnce(new Error("native unavailable"));
    render(<DesktopCloseGuard dirty={false} blocked={false}><p>Saved workspace</p></DesktopCloseGuard>);
    await requestClose();
    expect(await screen.findByRole("alert")).toHaveTextContent("could not close the window");
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(nativeMock.destroy).toHaveBeenCalledTimes(2);
  });

  it("cleans up a listener whose async registration completes after unmount", async () => {
    let completeRegistration!: (stop: () => void) => void;
    nativeMock.onCloseRequested.mockReturnValue(new Promise<() => void>((resolve) => { completeRegistration = resolve; }));
    const { unmount } = render(<DesktopCloseGuard dirty={false} blocked={false}><p>Workspace</p></DesktopCloseGuard>);
    unmount();
    await act(async () => completeRegistration(nativeMock.unlisten));
    expect(nativeMock.unlisten).toHaveBeenCalledOnce();
  });

  it("makes a failed listener registration visible and retries it", async () => {
    nativeMock.onCloseRequested.mockRejectedValueOnce(new Error("event unavailable"));
    render(<DesktopCloseGuard dirty={true} blocked={false}><p>Draft</p></DesktopCloseGuard>);
    expect(await screen.findByRole("alert")).toHaveTextContent("Close protection is unavailable");
    await userEvent.click(screen.getByRole("button", { name: "Retry close protection" }));
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    await requestClose();
    expect(screen.getByRole("alertdialog")).toHaveAccessibleName("Discard unsaved changes and close?");
    expect(nativeMock.destroy).not.toHaveBeenCalled();
  });
});
