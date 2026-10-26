export function runtimeTotals(runtimes) {
  const result = { total: 0, completed: 0, failed: 0, in_progress: 0, model_count: 0, complete: runtimes !== null };
  for (const runtime of runtimes || []) {
    for (const model of runtime.models) {
      result.model_count += 1;
      if (!model.requests) { result.complete = false; continue; }
      for (const key of ['total', 'completed', 'failed', 'in_progress']) result[key] += model.requests[key];
    }
  }
  return result;
}

export const runtimeStateLabel = state => ({ stopped: 'Выключен', healthy: 'Отвечает', unhealthy: 'Не отвечает', unavailable: 'Недоступен', checking: 'Проверка…' })[state] || 'Нет проверки';
export const checkedAt = timestamp => timestamp ? new Date(timestamp * 1000).toLocaleTimeString('ru-RU') : '—';

export function countLabel(value, one, few, many) {
  const lastHundred = Math.abs(value) % 100;
  const lastDigit = lastHundred % 10;
  const word = lastHundred >= 11 && lastHundred <= 14 ? many : lastDigit === 1 ? one : lastDigit >= 2 && lastDigit <= 4 ? few : many;
  return `${value} ${word}`;
}
