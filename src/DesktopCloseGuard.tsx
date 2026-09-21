// SPDX-License-Identifier: MPL-2.0
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { isTauri } from "@tauri-apps/api/core";
import { getCurrentWindow } from "@tauri-apps/api/window";

type CloseState = { dirty: boolean; blocked: boolean };
type RegisterCloseState = (id: string, state: CloseState | null) => void;
const CloseStateContext = createContext<RegisterCloseState | null>(null);

/** Register local drafts and unresolved mutations separately from navigation locks. */
export function useDesktopCloseProtection({ dirty, blocked }: CloseState) {
  const register = useContext(CloseStateContext);
  const id = useId();
  useLayoutEffect(() => {
    register?.(id, { dirty, blocked });
    return () => register?.(id, null);
  }, [blocked, dirty, id, register]);
}

export function DesktopCloseGuard({ children, dirty, blocked }: CloseState & { children: ReactNode }) {
  const [surfaces, setSurfaces] = useState<Record<string, CloseState>>({});
  const [open, setOpen] = useState(false);
  const [closing, setClosing] = useState(false);
  const [closeError, setCloseError] = useState<string | null>(null);
  const [registrationError, setRegistrationError] = useState(false);
  const [registrationAttempt, setRegistrationAttempt] = useState(0);
  const openRef = useRef(open);
  openRef.current = open;
  const dialogRef = useRef<HTMLElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const closingRef = useRef(false);
  const register = useCallback<RegisterCloseState>((id, state) => {
    setSurfaces((current) => {
      if (state === null) {
        if (!(id in current)) return current;
        const next = { ...current };
        delete next[id];
        return next;
      }
      if (current[id]?.dirty === state.dirty && current[id]?.blocked === state.blocked) return current;
      return { ...current, [id]: state };
    });
  }, []);
  const state = useMemo(() => ({
    dirty: dirty || Object.values(surfaces).some((surface) => surface.dirty),
    blocked: blocked || Object.values(surfaces).some((surface) => surface.blocked),
  }), [blocked, dirty, surfaces]);
  const stateRef = useRef(state);
  stateRef.current = state;

  const showConfirmation = useCallback(() => {
    setOpen((current) => {
      if (!current) returnFocusRef.current = document.activeElement as HTMLElement | null;
      return true;
    });
  }, []);

  const finishClose = useCallback(async () => {
    // A mutation can start after the initial close request; check again at the
    // actual destructive boundary instead of capturing the earlier dialog state.
    if (closingRef.current || stateRef.current.blocked) return;
    closingRef.current = true;
    setClosing(true);
    setCloseError(null);
    try {
      await getCurrentWindow().destroy();
    } catch {
      closingRef.current = false;
      setClosing(false);
      setCloseError("CVGnome could not close the window. Your workspace is still open; try again.");
      showConfirmation();
    }
  }, [showConfirmation]);

  useEffect(() => {
    if (!isTauri()) return;
    let active = true;
    let unlisten: (() => void) | undefined;
    setRegistrationError(false);
    void getCurrentWindow().onCloseRequested((event) => {
      // Prevent synchronously: opening a React dialog must never leave the
      // native close event free to destroy the window while the user decides.
      event.preventDefault();
      if (!active || closingRef.current) return;
      if (stateRef.current.dirty || stateRef.current.blocked) showConfirmation();
      else void finishClose();
    }).then((stop) => {
      if (active) unlisten = stop;
      else stop();
    }).catch(() => {
      if (active) setRegistrationError(true);
    });
    return () => { active = false; unlisten?.(); };
  }, [finishClose, registrationAttempt, showConfirmation]);

  useEffect(() => {
    const preventUnload = (event: BeforeUnloadEvent) => {
      if (!stateRef.current.dirty && !stateRef.current.blocked) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", preventUnload);
    return () => window.removeEventListener("beforeunload", preventUnload);
  }, []);

  const cancel = useCallback(() => {
    if (closingRef.current) return;
    setOpen(false);
    setCloseError(null);
    const target = returnFocusRef.current;
    window.requestAnimationFrame(() => { if (target?.isConnected) target.focus(); });
  }, []);

  useEffect(() => {
    if (!open) return;
    cancelRef.current?.focus();
  }, [open, state.blocked]);

  useLayoutEffect(() => {
    // Register before descendant dialogs install their window-capture effects,
    // and retain that order when the close confirmation opens over a dialog.
    const handleKey = (event: KeyboardEvent) => {
      if (!openRef.current) return;
      // Workspace shortcuts must not navigate or start another action behind
      // the modal. Native button activation still uses its default behavior.
      event.stopImmediatePropagation();
      if (event.key === "Escape") {
        event.preventDefault();
        cancel();
      } else if (event.key === "Tab") {
        const buttons = Array.from(dialogRef.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") ?? []);
        const first = buttons[0];
        const last = buttons[buttons.length - 1];
        if (!first) { event.preventDefault(); return; }
        if (event.shiftKey && (document.activeElement === first || !dialogRef.current?.contains(document.activeElement))) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && (document.activeElement === last || !dialogRef.current?.contains(document.activeElement))) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    window.addEventListener("keydown", handleKey, true);
    return () => window.removeEventListener("keydown", handleKey, true);
  }, [cancel]);

  return <CloseStateContext.Provider value={register}>
    <div style={{ height: "100%" }} inert={open || undefined}>{children}</div>
    {registrationError && <div className="settings-message settings-message--error" role="alert"
      style={{ position: "fixed", left: 64, right: 16, bottom: 32, zIndex: 150 }}>
      Close protection is unavailable. Save your changes before closing CVGnome.
      <button type="button" onClick={() => setRegistrationAttempt((current) => current + 1)}>Retry close protection</button>
    </div>}
    {open && <div className="modal-backdrop" role="presentation">
      <section className="confirmation-dialog" ref={dialogRef} role="alertdialog" aria-modal="true"
        aria-labelledby="desktop-close-title" aria-describedby="desktop-close-description" aria-busy={closing}>
        <div className="confirmation-dialog__heading"><h2 id="desktop-close-title">
          {state.blocked ? "Finish the current action before closing" : state.dirty ? "Discard unsaved changes and close?" : "Close CVGnome?"}
        </h2></div>
        <p id="desktop-close-description">{state.blocked
          ? "An action is still running or its result needs to be confirmed. Return to the workspace and finish or retry it before closing."
          : state.dirty
            ? "Unsaved drafts and queued choices will be lost. Your saved profile, evidence, and history stay on this computer."
            : "Your saved work will be available when you reopen the app."}</p>
        {closeError && <p className="settings-message settings-message--error" role="alert">{closeError}</p>}
        <div className="confirmation-dialog__actions">
          <button type="button" className="secondary-action" ref={cancelRef} onClick={cancel} disabled={closing}>Keep open</button>
          {!state.blocked && <button type="button" className={state.dirty ? "danger-action" : "primary-action"}
            onClick={() => void finishClose()} disabled={closing}>{closing ? "Closing…" : state.dirty ? "Discard and close" : "Close"}</button>}
        </div>
      </section>
    </div>}
  </CloseStateContext.Provider>;
}
