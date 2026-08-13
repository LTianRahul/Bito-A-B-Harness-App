import { useCallback, useEffect, useState } from "react";

export interface AsyncState<T> {
  loading: boolean;
  error?: string | null;
  data?: T;
}

// Run an async loader on mount (and when deps change). Returns state + reload().
// `reload()` shows the loading spinner; `refresh()` updates data silently in the
// background (no spinner/flicker) — used by polling.
export function useAsync<T>(loader: () => Promise<T>, deps: unknown[] = []) {
  const [state, setState] = useState<AsyncState<T>>({ loading: true });

  const run = useCallback((silent: boolean) => {
    let active = true;
    if (!silent) setState((s) => ({ ...s, loading: true, error: null }));
    loader()
      .then((data) => active && setState({ loading: false, data }))
      .catch((e) => active && setState((s) =>
        // On a silent refresh, keep the last good data; just note the error.
        silent ? { ...s, error: e?.message ?? String(e) } : { loading: false, error: e?.message ?? String(e) },
      ));
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  const reload = useCallback(() => run(false), [run]);
  const refresh = useCallback(() => run(true), [run]);

  useEffect(() => reload(), [reload]);

  return { ...state, reload, refresh, setData: (data: T) => setState({ loading: false, data }) };
}
