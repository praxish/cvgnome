// SPDX-License-Identifier: MPL-2.0
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach } from "vitest";
import { resetNativeMock } from "./nativeMock";

beforeEach(() => {
  resetNativeMock();
  window.localStorage.clear();
});
afterEach(cleanup);
