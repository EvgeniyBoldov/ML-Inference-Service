import { useCallback, useEffect, useRef, useState } from 'react';
import { request } from './api.js';

export default function useServiceSnapshot() {
  const [snapshot, setSnapshot] = useState({ models: null, status: null, runtimes: null, modelsError: '', statusError: '', runtimesError: '', updatedAt: null, refreshing: true });
  const inFlight = useRef(null);
  const refresh = useCallback(async () => {
    if (inFlight.current) return;
    const controller = new AbortController();
    inFlight.current = controller;
    setSnapshot(previous => ({ ...previous, refreshing: true }));
    try {
      const [models, status, runtimes] = await Promise.allSettled([
        request('/v1/models', { signal: controller.signal, timeout: 8000 }),
        request('/internal/v1/status', { signal: controller.signal, timeout: 8000 }),
        request('/internal/v1/runtimes', { signal: controller.signal, timeout: 12000 }),
      ]);
      if (controller.signal.aborted) return;
      setSnapshot(previous => ({
        models: models.status === 'fulfilled' ? models.value.data : previous.models,
        status: status.status === 'fulfilled' ? status.value : previous.status,
        runtimes: runtimes.status === 'fulfilled' ? runtimes.value.data : previous.runtimes,
        modelsError: models.status === 'rejected' ? models.reason.message : '',
        statusError: status.status === 'rejected' ? status.reason.message : '',
        runtimesError: runtimes.status === 'rejected' ? runtimes.reason.message : '',
        updatedAt: status.status === 'fulfilled' ? new Date() : previous.updatedAt,
        refreshing: false,
      }));
    } finally {
      if (inFlight.current === controller) inFlight.current = null;
    }
  }, []);
  useEffect(() => {
    void refresh();
    const updateVisiblePage = () => { if (!document.hidden) void refresh(); };
    const timer = setInterval(updateVisiblePage, 5000);
    document.addEventListener('visibilitychange', updateVisiblePage);
    return () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', updateVisiblePage);
      inFlight.current?.abort();
      inFlight.current = null;
    };
  }, [refresh]);
  return { ...snapshot, refresh };
}
