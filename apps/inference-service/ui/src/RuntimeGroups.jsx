import React, { useEffect, useRef, useState } from 'react';
import Icon from './Icons.jsx';
import { number, request } from './api.js';
import { checkedAt, countLabel, runtimeStateLabel, runtimeTotals } from './runtimeData.js';

export default function RuntimeGroups({ runtimes, error, selectedModel, onSelect, onRefresh, refreshing }) {
  const [checks, setChecks] = useState({});
  const controllers = useRef(new Map());
  useEffect(() => () => controllers.current.forEach(controller => controller.abort()), []);

  const check = async runtime => {
    if (controllers.current.has(runtime.id)) return;
    const controller = new AbortController();
    controllers.current.set(runtime.id, controller);
    setChecks(previous => ({ ...previous, [runtime.id]: { status: 'checking' } }));
    try {
      const result = await request(`/internal/v1/runtimes/${encodeURIComponent(runtime.id)}/health`, { signal: controller.signal, timeout: 10000 });
      if (!controller.signal.aborted) setChecks(previous => ({ ...previous, [runtime.id]: result }));
    } catch (cause) {
      if (!controller.signal.aborted) setChecks(previous => ({ ...previous, [runtime.id]: { status: 'unhealthy', checked_at: Date.now() / 1000, message: cause.message } }));
    } finally { controllers.current.delete(runtime.id); }
  };

  return <section className="panel runtime-panel" aria-labelledby="models-title">
    <div className="panel-heading"><div><h2 id="models-title">Контейнеры и модели</h2><p>Активные модели сгруппированы по runtime, который обслуживает запросы.</p></div><span className="count-badge">{runtimes === null ? '—' : countLabel(runtimes.length, 'группа', 'группы', 'групп')}</span></div>
    {error && <div className="inline-notice notice-warning" role="alert"><Icon name="alert" /><span>Не удалось обновить контейнеры. {error} Показаны последние доступные данные.</span></div>}
    {!runtimes?.length ? <div className="empty-state"><span className="empty-icon"><Icon name="model" size={28} /></span><strong>{error ? 'Список контейнеров недоступен' : runtimes === null ? 'Получаем контейнеры…' : 'Активных контейнеров пока нет'}</strong><p>{error ? 'Повторите обновление состояния сервиса.' : 'После развёртывания здесь появятся контейнеры, состав моделей и их собственные счётчики запросов.'}</p>{error ? <button type="button" className="button button-quiet" disabled={refreshing} onClick={() => void onRefresh()}>Повторить обновление</button> : runtimes !== null && <a className="button button-quiet" href="#deploy">Перейти к развёртыванию<Icon name="arrow" size={15} /></a>}</div> : <div className="runtime-groups">{runtimes.map(runtime => {
      const lastCheck = checks[runtime.id];
      const current = lastCheck?.status === 'checking' || (lastCheck?.checked_at || 0) > (runtime.checked_at || 0) ? lastCheck : runtime;
      const totals = runtimeTotals([runtime]);
      return <article className="runtime-card" key={runtime.id} data-runtime-id={runtime.id}>
        <div className="runtime-heading"><div className="runtime-identity"><span className="model-glyph"><Icon name="model" size={20} /></span><div><h3>{runtime.kind === 'docker' ? 'Контейнер' : 'Runtime'} <code>{runtime.container_name || runtime.id}</code></h3><p className="runtime-image" title={runtime.producer_image || runtime.image}>{runtime.producer_image || runtime.image || 'Встроенный runtime'}</p></div></div><div className="runtime-actions"><span className={`runtime-health health-${current.status}`} role="status"><i />{runtimeStateLabel(current.status)}</span><button type="button" className="button button-quiet" disabled={current.status === 'checking'} onClick={() => void check(runtime)} aria-label={`Проверить runtime ${runtime.container_name || runtime.id}`}><Icon name={current.status === 'checking' ? 'refresh' : 'activity'} className={current.status === 'checking' ? 'spinning' : ''} />{current.status === 'checking' ? 'Проверяем…' : 'Проверить'}</button></div></div>
        <div className="runtime-summary"><span>{countLabel(runtime.models.length, 'модель', 'модели', 'моделей')}</span><span>Запросов: <strong>{number(totals.complete ? totals.total : undefined)}</strong></span><span>Ошибок: <strong>{number(totals.complete ? totals.failed : undefined)}</strong></span><span>Проверка: {checkedAt(current.checked_at)}</span></div>
        {current.message && <p className="field-error" role="alert">{current.message}</p>}
        {runtime.statistics_status !== 'available' && <div className="inline-notice notice-warning"><Icon name="alert" /><span>Счётчики runtime недоступны. Для контейнеров со старой версией runner нужен повторный деплой; значения API не подставляются.</span></div>}
        <div className="runtime-table-head"><span>Модель / версия</span><span>Запросы</span><span>Успешно</span><span>Ошибки</span><span>В работе</span><span>Среднее время</span><span>Действие</span></div>
        {runtime.models.map(model => <div className={`runtime-model-row ${selectedModel === model.id ? 'row-selected' : ''}`} key={`${model.id}:${model.version}`}>
          <div className="runtime-model-name"><strong>{model.id}</strong><span className="mono">v{model.version}</span></div>
          <Metric label="Запросы" value={number(model.requests?.total)} />
          <Metric label="Успешно" value={number(model.requests?.completed)} />
          <Metric label="Ошибки" value={number(model.requests?.failed)} />
          <Metric label="В работе" value={number(model.requests?.in_progress)} />
          <Metric label="Среднее время" value={model.requests?.average_latency_ms == null ? '—' : `${number(model.requests.average_latency_ms)} мс`} />
          <button type="button" className="text-button runtime-model-action" onClick={() => onSelect(model.id)} aria-label={`Запрос к модели ${model.id}`}>Запрос<Icon name="arrow" size={15} /></button>
        </div>)}
      </article>;
    })}</div>}
  </section>;
}

function Metric({ label, value }) { return <span className="runtime-metric"><small>{label}</small><strong>{value}</strong></span>; }
