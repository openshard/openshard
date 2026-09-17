import type { OpenShardApi } from "./client";
import { RECEIPTS, TASKS, toSummary } from "./fixtures";

/** In-memory client over the fixture set. A short delay keeps loading states honest. */
export function createFixtureClient(delayMs = 120): OpenShardApi {
  const wait = () => new Promise<void>((resolve) => setTimeout(resolve, delayMs));
  return {
    async listTasks() {
      await wait();
      return [...TASKS]
        .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
        .map(toSummary);
    },
    async getTask(taskId) {
      await wait();
      return TASKS.find((t) => t.task_id === taskId) ?? null;
    },
    async getReceipt(receiptId) {
      await wait();
      return RECEIPTS.find((r) => r.receipt_id === receiptId) ?? null;
    },
  };
}
