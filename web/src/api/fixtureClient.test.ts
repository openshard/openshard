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
    const task = await api.getTask("task_019965a1-4d2e-7c3a-8f11-2b6e9d4a0c51");
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
    }
    const ids = new Set(RECEIPTS.map((r) => r.receipt_id));
    expect(ids.size).toBe(RECEIPTS.length);
  });

  it("groups only by an explicitly carried task_id and never invents one", () => {
    for (const r of RECEIPTS) {
      const owners = TASKS.filter((t) => t.attempts.some((a) => a.receipt_id === r.receipt_id));
      if (r.task_id === null) {
        expect(owners).toEqual([]);
      } else {
        expect(owners.map((t) => t.task_id)).toEqual([r.task_id]);
      }
    }
    for (const t of TASKS) {
      expect(t.latest_receipt.task_id).toBe(t.task_id);
    }
  });

  it("keeps a legacy receipt without task_id valid and reachable", async () => {
    const legacy = await api.getReceipt("rcpt_1a3c5e7a9b1d3f5a7c9e1b3d5f7a9c16");
    expect(legacy?.task_id).toBeNull();
    expect(legacy?.attempt_number).toBeNull();
    expect(legacy?.integrity).toBe("Not recorded");
    expect((await api.listTasks()).some((t) => t.latest_receipt_id === legacy?.receipt_id)).toBe(false);
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
