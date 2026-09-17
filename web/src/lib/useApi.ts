import { createContext, useContext, useEffect, useState } from "react";
import type { OpenShardApi } from "../api";

export const ApiContext = createContext<OpenShardApi | null>(null);

export function useApi(): OpenShardApi {
  const api = useContext(ApiContext);
  if (!api) throw new Error("ApiContext is not provided");
  return api;
}

export type Loadable<T> = { state: "loading" } | { state: "error"; message: string } | { state: "ready"; data: T };

/** Runs one api call per `key`; pages branch on `state` and nothing else. */
export function useLoad<T>(key: string, load: (api: OpenShardApi) => Promise<T>): Loadable<T> {
  const api = useApi();
  const [result, setResult] = useState<Loadable<T>>({ state: "loading" });
  useEffect(() => {
    let cancelled = false;
    setResult({ state: "loading" });
    load(api).then(
      (data) => !cancelled && setResult({ state: "ready", data }),
      (err: unknown) => !cancelled && setResult({ state: "error", message: err instanceof Error ? err.message : String(err) }),
    );
    return () => {
      cancelled = true;
    };
    // `load` is expected to be stable for a given key.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, key]);
  return result;
}
