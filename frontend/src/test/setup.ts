// Runs before every test file. Registers @testing-library/jest-dom matchers (toBeInTheDocument,
// toHaveValue, …); unmounts each render and clears localStorage between tests. cleanup() is explicit
// because RTL only auto-registers it under Vitest's `globals: true`, and we keep globals off (tests
// import describe/it/expect from "vitest" directly).
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
  localStorage.clear();
});
