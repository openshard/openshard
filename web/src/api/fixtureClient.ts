import type { OpenShardApi } from "./client";
import { RECEIPTS, TASKS, recentWork } from "./fixtures";

/** In-memory client over the fixture set. A short delay keeps loading states honest. */
export function createFixtureClient(delayMs = 120): OpenShardApi {
  const wait = () => new Promise<void>((resolve) => setTimeout(resolve, delayMs));
  return {
    async listWork() {
      await wait();
      return recentWork();
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
