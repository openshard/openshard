import type { OpenShardApi } from "./client";
import { createFixtureClient } from "./fixtureClient";
import { createHttpClient } from "./httpClient";

export type { OpenShardApi } from "./client";
export { ApiError } from "./client";

/**
 * Picks the data source once at startup.
 *
 * Set `VITE_OPENSHARD_API_URL` to point the dashboard at a real sync
 * service; leave it unset and the app runs on fixtures.
 */
export function createApi(): OpenShardApi {
  const url = import.meta.env.VITE_OPENSHARD_API_URL as string | undefined;
  return url ? createHttpClient(url) : createFixtureClient();
}
