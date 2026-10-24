export class ApiError extends Error {
  constructor(message, status, code) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export async function request(path, { timeout = 15000, signal, ...options } = {}) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (signal?.aborted) abort();
  signal?.addEventListener('abort', abort, { once: true });
  const timer = setTimeout(abort, timeout);
  try {
    const response = await fetch(path, {
      ...options,
      signal: controller.signal,
      credentials: 'same-origin',
      headers: { Accept: 'application/json', ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers },
    });
    const body = await response.json().catch(() => null);
    if (!response.ok) throw new ApiError(body?.error?.message || `Сервис вернул ошибку ${response.status}`, response.status, body?.error?.code);
    if (body === null) throw new Error('Сервис вернул некорректный ответ. Обновите страницу или проверьте подключение.');
    return body;
  } catch (error) {
    if (signal?.aborted) throw error;
    if (controller.signal.aborted) throw new Error('Сервис не ответил вовремя. Попробуйте ещё раз.');
    if (error instanceof TypeError) throw new Error('Нет связи с сервисом. Проверьте подключение и повторите запрос.');
    throw error;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', abort);
  }
}

export function parseModelUri(value) {
  const uri = value.trim();
  const match = /^models:\/([A-Za-z0-9][A-Za-z0-9_.-]*)\/([0-9]+)$/.exec(uri);
  if (!match || match[1].length > 255 || match[2].length > 128 || uri.length > 1024) return null;
  return { uri, model: match[1], version: match[2] };
}

export const number = value => value == null ? '—' : new Intl.NumberFormat('ru-RU').format(value);
export const terminalDeployments = new Set(['active', 'failed', 'removed', 'standby', 'draining']);
export const deploymentLabels = {
  checking: 'Получение статуса', created: 'В очереди', downloading: 'Скачивание модели',
  loading: 'Запуск runtime', warming_up: 'Проверка предсказания', ready: 'Подготовка маршрута',
  active: 'Модель запущена', failed: 'Не удалось развернуть модель',
  standby: 'Версия заменена', draining: 'Версия заменена', removed: 'Runtime удалён',
};
