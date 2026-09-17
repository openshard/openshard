import { describe, expect, it } from "vitest";
import { createFixtureClient } from "./fixtureClient";
import { createHttpClient } from "./httpClient";
import { RECEIPTS, TASKS } from "./fixtures";

const api = createFixtureClient(0);

describe("fixture client", () => {
  it("lists tasks newest first", async () => {
    const tasks = await api.listTasks();
    expect(tasks.length).toBe(TASKS.length);
    for (let i = 1; i < tasks.length; i++) {
      expect(tasks[i - 1].updated_at >= tasks[i].updated_at).toBe(true);
    }
    expect(tasks[0].title).toBe("Fix refresh-token reuse bug");
  });

  it("resolves a task with its attempts and latest receipt", async () => {
    const task = await api.getTask("task_01j8refreshtokenreuse");
    expect(task?.attempt_count).toBe(2);
    expect(task?.attempts.map((a) => a.status)).toEqual(["failed", "completed"]);
    expect(task?.latest_receipt.files_changed).toBe(1);
    expect(task?.latest_receipt.checks).toBe("10 checks passed");
    expect(task?.latest_receipt.integrity).toBe("Matches (content hash)");
  });

  it("returns null for unknown ids", async () => {
    expect(await api.getTask("task_nope")).toBeNull();
    expect(await api.getReceipt("rcpt_nope")).toBeNull();
  });

  it("uses well-formed identifiers", () => {
    for (const r of RECEIPTS) {
      expect(r.receipt_id).toMatch(/^rcpt_[0-9a-f]{32}$/);
      expect(r.shard_id).toMatch(/^shard-\d{8}-\d{4}$/);
      expect(TASKS.some((t) => t.task_id === r.task_id)).toBe(true);
    }
    const ids = new Set(RECEIPTS.map((r) => r.receipt_id));
    expect(ids.size).toBe(RECEIPTS.length);
  });

  it("never fabricates cost or tokens", () => {
    for (const r of RECEIPTS) {
      if (r.cost_usd !== null) expect(r.cost_provenance).not.toBeNull();
      if (r.tokens_input !== null) expect(r.tokens_provenance).not.toBeNull();
    }
  });
});

describe("http client", () => {
  it("hits the same three routes and maps 404 to null", async () => {
    const calls: string[] = [];
    const fetchImpl = (async (input: RequestInfo | URL) => {
      const url = String(input);
      calls.push(url);
      if (url.endsWith("/v1/tasks")) return new Response(JSON.stringify([]), { status: 200 });
      return new Response("", { status: 404 });
    }) as typeof fetch;
    const http = createHttpClient("https://api.example.test/", fetchImpl);
    expect(await http.listTasks()).toEqual([]);
    expect(await http.getTask("t")).toBeNull();
    expect(await http.getReceipt("r")).toBeNull();
    expect(calls).toEqual([
      "https://api.example.test/v1/tasks",
      "https://api.example.test/v1/tasks/t",
      "https://api.example.test/v1/receipts/r",
    ]);
  });
});
