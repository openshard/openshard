import { describe, expect, it } from "vitest";
import { changedFilesDisplay, cost, duration, gapsDisplay, relativeTime, tokens } from "./format";

describe("relativeTime", () => {
  const now = Date.parse("2026-09-17T12:00:00Z");
  it("reads like the product brief", () => {
    expect(relativeTime("2026-09-17T11:58:00Z", now)).toBe("2m ago");
    expect(relativeTime("2026-09-17T11:39:00Z", now)).toBe("21m ago");
    expect(relativeTime("2026-09-17T10:59:00Z", now)).toBe("1h ago");
    expect(relativeTime("2026-09-16T09:00:00Z", now)).toBe("yesterday");
    expect(relativeTime("2026-09-14T09:00:00Z", now)).toBe("3d ago");
  });
  it("falls back to a date after a month", () => {
    expect(relativeTime("2026-07-01T09:00:00Z", now)).toBe("2026-07-01");
  });
});

describe("duration", () => {
  it("matches fmt_duration", () => {
    expect(duration(12.34)).toBe("12.3s");
    expect(duration(214)).toBe("3m 34s");
    expect(duration(3905)).toBe("1h 05m");
    expect(duration(null)).toBe("not recorded");
  });
});

describe("cost and tokens never invent a figure", () => {
  it("marks every cost as an estimate", () => {
    expect(cost(0.42)).toBe("$0.42 est.");
    expect(cost(null)).toBe("not recorded");
  });
  it("formats tokens like the CLI", () => {
    expect(tokens({ tokens_input: 48_210, tokens_output: 3_904, tokens_cache_read: 21_000 })).toBe(
      "48.2k input / 3.9k output (+21.0k cache read)",
    );
    expect(tokens({ tokens_input: null, tokens_output: null, tokens_cache_read: null })).toBe("not recorded");
  });
});

describe("changedFilesDisplay", () => {
  it("names the attribution split", () => {
    expect(
      changedFilesDisplay({
        files_changed: 3,
        changes: { agent_reported: 2, git_observed: 1, pre_existing_excluded: 0, other_session_excluded: 0 },
      }),
    ).toBe("3 files (2 agent-reported; 1 git-observed, actor not established)");
    expect(changedFilesDisplay({ files_changed: 1, changes: null })).toBe("1 file");
  });
});

describe("gapsDisplay", () => {
  it("matches capture_completeness.gaps_display", () => {
    expect(gapsDisplay({ depth: "full", status: "complete", reasons: [], derived: false })).toBe("None known");
    expect(gapsDisplay({ depth: "full", status: "unknown", reasons: [], derived: true })).toBe(
      "Unknown (record predates loss tracking)",
    );
    expect(
      gapsDisplay({
        depth: "partial",
        status: "incomplete",
        reasons: [{ kind: "hook_gap", count: 3, detail: "3 edits observed by git only" }],
        derived: false,
      }),
    ).toBe("3 edits observed by git only");
  });
});
