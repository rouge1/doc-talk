import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import Search from "./Search";

// Keep the index out of it — these tests are about the redirect guard, not retrieval.
vi.mock("../api", () => ({
  api: { search: vi.fn(async () => ({ query: "", hits: [] })) },
  sourcePath: () => "/",
}));

function renderSearchAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/search" element={<Search />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("Search restore-last redirect guard", () => {
  // Regression: a poisoned __last ("mode:query", no q=) used to redirect /search → /search?<that>,
  // which is still q-less → redirect again → infinite loop → blank page. The guard must render the
  // form instead. (On the old code this test hangs / blows the update-depth limit.)
  it("renders the form (not a blank loop) when __last is a poisoned cache key", async () => {
    localStorage.setItem("doctalk:search:__last", "hybrid:cats");
    renderSearchAt("/search");
    expect(
      await screen.findByRole("heading", { name: /search the stacks/i }),
    ).toBeInTheDocument();
  });

  // The guard must still allow a legitimate restore: a real querystring redirects and the box
  // re-fills from the URL query.
  it("restores the last view when __last is a real querystring", async () => {
    localStorage.setItem("doctalk:search:__last", "q=cats&mode=hybrid");
    renderSearchAt("/search");
    const box = await screen.findByRole("textbox");
    await waitFor(() => expect(box).toHaveValue("cats"));
  });

  // The exact URL the user hit: a q-less /search?<garbage> renders the form, never a blank page.
  it("renders the form for a malformed /search?<garbage> URL", async () => {
    renderSearchAt("/search?hybrid:cats");
    expect(
      await screen.findByRole("heading", { name: /search the stacks/i }),
    ).toBeInTheDocument();
  });
});
