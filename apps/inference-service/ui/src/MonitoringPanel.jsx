import React, { useEffect, useRef, useState } from 'react';
import Icon from './Icons.jsx';
import { request } from './api.js';
import { checkedAt } from './runtimeData.js';

export default function MonitoringPanel({ status, runtimes, refreshing, onRefresh }) {
  const [checking, setChecking] = useState(false);
  const [result, setResult] = useState(null);
  const controller = useRef(null);
  useEffect(() => () => controller.current?.abort(), []);
  const checkService = async () => {
    if (checking) return;
    const current = new AbortController();
    controller.current = current;
    setChecking(true);
    const started = performance.now();
    try {
      await request('/health/ready', { signal: current.signal, timeout: 10000 });
      if (!current.signal.aborted) setResult({ healthy: true, checked_at: Date.now() / 1000, milliseconds: Math.round(performance.now() - started) });
    } catch (error) {
      if (!current.signal.aborted) setResult({ healthy: false, checked_at: Date.now() / 1000, message: error.message });
    } finally { if (!current.signal.aborted) setChecking(false); }
  };
  return <section className="panel monitoring-panel" aria-labelledby="monitoring-title"><div className="panel-heading"><div><h2 id="monitoring-title">Мониторинг</h2><p>Проверяйте готовность сервиса и состояние каждого контейнера.</p></div><button type="button" className="button button-quiet" disabled={refreshing} onClick={() => void onRefresh()}><Icon name="refresh" className={refreshing ? 'spinning' : ''} />{refreshing ? 'Обновляем…' : 'Обновить мониторинг'}</button></div><div className="monitoring-grid">
    <article className="monitor-card"><div><h3>Готовность сервиса</h3><span className={`runtime-health health-${status?.status === 'ready' ? 'healthy' : 'unavailable'}`}><i />{status?.status === 'ready' ? 'Сервис готов' : status ? 'Сервис не готов' : 'Нет актуальных данных'}</span></div><p>Проверяет восстановление маршрутов и доступность runtime.</p><button type="button" className="button button-quiet" disabled={checking} onClick={() => void checkService()}><Icon name="activity" />{checking ? 'Проверяем…' : 'Проверить сервис'}</button>{result && <p className={result.healthy ? 'success-text' : 'field-error'} role="status">{result.healthy ? `Проверка пройдена за ${result.milliseconds} мс` : result.message} · {checkedAt(result.checked_at)}</p>}</article>
    <article className="monitor-card"><h3>Runtime-контейнеры</h3><p className="monitor-count">{runtimes === null ? '—' : `${runtimes.filter(item => item.status === 'healthy').length} / ${runtimes.length}`}</p><p>{runtimes?.length === 0 ? 'Активных контейнеров нет. Они появятся после развёртывания модели.' : 'Отвечают на healthcheck. Каждую группу можно проверить отдельно ниже.'}</p></article>
    <article className="monitor-card"><h3>Источник статистики</h3><p>Счётчики находятся в процессах моделей внутри runtime. Проверки здоровья, валидация API и прогрев при деплое не увеличивают их.</p><p className="muted">При перезапуске контейнера его счётчики начинаются заново.</p></article>
  </div></section>;
}
