import { ApiError, type OpenShardApi } from "./client";
import type { Receipt, Task, TaskSummary } from "./types";

/**
 * Hosted API client. Same three calls as the fixture client, against
 * `${baseUrl}/v1/...`. Exists so the swap is a config change, not a rewrite;
 * the routes are the proposal for the sync service, not a live contract.
 */
export function createHttpClient(baseUrl: string, fetchImpl: typeof fetch = fetch): OpenShardApi {
  const root = baseUrl.replace(/\/+$/, "");

  async function get<T>(path: string): Promise<T | null> {
    const res = await fetchImpl(`${root}${path}`, { headers: { Accept: "application/json" } });
    if (res.status === 404) return null;
    if (!res.ok) throw new ApiError(`OpenShard API ${res.status} for ${path}`, res.status);
    return (await res.json()) as T;
  }

  return {
    async listTasks() {
      return (await get<TaskSummary[]>("/v1/tasks")) ?? [];
    },
    getTask(taskId) {
      return get<Task>(`/v1/tasks/${encodeURIComponent(taskId)}`);
    },
    getReceipt(receiptId) {
      return get<Receipt>(`/v1/receipts/${encodeURIComponent(receiptId)}`);
    },
  };
}
