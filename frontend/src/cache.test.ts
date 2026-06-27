import { describe, expect, it } from "vitest";
import { getCached, getLastKey, setCached, setLastKey } from "./cache";

const NS = "search";

describe("result cache __last slot", () => {
  it("setLastKey stores a querystring verbatim and getLastKey reads it back", () => {
    setLastKey(NS, "q=cats&mode=hybrid");
    expect(getLastKey(NS)).toBe("q=cats&mode=hybrid");
  });

  // Regression: the Search "restore last view" redirect reads __last as a URL querystring. An earlier
  // build had setCached clobber __last with the cache KEY ("mode:query"), so a later bare /search
  // redirected to /search?hybrid:cats — a q-less URL that looped forever and blanked the page.
  // setCached must leave __last alone; only setLastKey owns that slot.
  it("setCached does NOT overwrite __last with the cache key", () => {
    setLastKey(NS, "q=cats&mode=hybrid");
    setCached(NS, "hybrid:cats", { query: "cats", hits: [] });
    expect(getLastKey(NS)).toBe("q=cats&mode=hybrid"); // not the poisoned "hybrid:cats"
  });

  it("round-trips a cached value by key", () => {
    setCached(NS, "hybrid:cats", { query: "cats", hits: [1, 2] });
    expect(getCached(NS, "hybrid:cats")).toEqual({ query: "cats", hits: [1, 2] });
  });
});
