import type { Receipt, Task, WorkItem } from "./types";

/**
 * The only seam between the dashboard and its data.
 *
 * Pages call these three methods and nothing else, so the fixture client
 * (`fixtureClient.ts`) can be swapped for the HTTP client (`httpClient.ts`)
 * without touching a page.
 */
export interface OpenShardApi {
  /** Recent work, newest first: explicit Tasks and standalone Receipts (no `task_id`). */
  listWork(): Promise<WorkItem[]>;
  /** Resolves `null` when the task does not exist. */
  getTask(taskId: string): Promise<Task | null>;
  /** Resolves `null` when the receipt does not exist. */
  getReceipt(receiptId: string): Promise<Receipt | null>;
}

export class ApiError extends Error {
  constructor(message: string, readonly status?: number) {
    super(message);
    this.name = "ApiError";
  }
}
