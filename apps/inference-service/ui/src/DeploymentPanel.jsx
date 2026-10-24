import React, { useEffect, useRef, useState } from 'react';
import Icon from './Icons.jsx';
import { deploymentLabels, parseModelUri, request, terminalDeployments } from './api.js';

const STORAGE_KEY = 'ml-inference:last-deployment';
const stages = ['created', 'downloading', 'loading', 'warming_up', 'active'];

function restoredDeployment() {
  try {
    const id = localStorage.getItem(STORAGE_KEY);
    return id && /^deploy_[A-Za-z0-9_-]+$/.test(id) ? { id, status: 'checking' } : null;
  } catch { return null; }
}

export default function DeploymentPanel({ capability, connectionError, refreshing, onRefresh }) {
  const [uri, setUri] = useState('');
  const [fieldError, setFieldError] = useState('');
  const [submissionError, setSubmissionError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [deployment, setDeployment] = useState(restoredDeployment);
  const [pollError, setPollError] = useState('');
  const [retry, setRetry] = useState(0);
  const retryRequest = useRef(null);
  const submitController = useRef(null);
  const available = capability?.available === true && !connectionError;
  const parsed = parseModelUri(uri);
  const isRunning = deployment && !terminalDeployments.has(deployment.status);

  useEffect(() => () => submitController.current?.abort(), []);
  useEffect(() => {
    if (!deployment?.id) return undefined;
    const controller = new AbortController();
    let timer;
    const poll = async () => {
      let continuePolling = true;
      try {
        const next = await request(`/internal/v1/deployments/${encodeURIComponent(deployment.id)}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setDeployment(next); setPollError('');
        continuePolling = !terminalDeployments.has(next.status);
        if (next.status === 'active') void onRefresh();
      } catch (error) {
        if (controller.signal.aborted) return;
        setPollError(error.status === 404 ? 'Это развёртывание больше не найдено на сервисе.' : `${error.message} Проверка повторится автоматически.`);
        if (error.status === 404) { continuePolling = false; setDeployment(previous => ({ ...previous, status: 'not_found' })); }
      }
      if (!controller.signal.aborted && continuePolling) timer = setTimeout(poll, 3000);
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [deployment?.id, retry, onRefresh]);

  const handleSubmit = async event => {
    event.preventDefault();
    if (!available || submitting || (isRunning && deployment.status !== 'not_found')) return;
    if (!parsed) {
      setFieldError('Укажите URI вида models:/имя-модели/номер-версии. Alias и stages не поддерживаются.');
      return;
    }
    const controller = new AbortController();
    submitController.current = controller;
    if (retryRequest.current?.uri !== parsed.uri) retryRequest.current = { uri: parsed.uri, key: globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}` };
    setSubmitting(true); setSubmissionError(''); setFieldError('');
    try {
      const result = await request('/internal/v1/deployments', {
        method: 'POST', signal: controller.signal,
        headers: { 'Idempotency-Key': retryRequest.current.key },
        body: JSON.stringify({ model: parsed.model, source: { type: 'mlflow', uri: parsed.uri } }),
      });
      if (controller.signal.aborted) return;
      retryRequest.current = null;
      setDeployment(result); setPollError('');
      try { localStorage.setItem(STORAGE_KEY, result.id); } catch { /* Storage is optional. */ }
    } catch (error) {
      if (!controller.signal.aborted) setSubmissionError(`${error.message}${!error.status || error.status >= 500 ? ' Повторите отправку с тем же URI: повторный запрос не создаст дубликат.' : ''}`);
    } finally {
      if (!controller.signal.aborted) setSubmitting(false);
    }
  };

  const handleDismiss = () => {
    setDeployment(null); setPollError('');
    try { localStorage.removeItem(STORAGE_KEY); } catch { /* Storage is optional. */ }
  };
  const running = isRunning && deployment.status !== 'not_found';
  const stage = deployment?.status === 'ready' ? 3 : stages.indexOf(deployment?.status);

  return (
    <section className="panel deploy-panel" id="deploy" aria-labelledby="deploy-title">
      <div className="panel-heading"><div><h2 id="deploy-title">Развернуть модель</h2><p>Опубликуйте точную версию из MLflow Model Registry.</p></div><Icon name="upload" className="panel-symbol" /></div>
      {!available && <div className="inline-notice notice-warning"><Icon name="alert" /><div><strong>{connectionError ? 'Не удалось получить настройки' : capability ? 'MLflow не настроен' : 'Получаем настройки сервиса…'}</strong><p>{connectionError ? 'Обновите состояние сервиса перед развёртыванием.' : capability ? 'Укажите адрес MLflow в окружении backend и перезапустите сервис. После этого здесь появится форма развёртывания.' : 'Форма появится после проверки доступных действий.'}</p>{connectionError && <button type="button" className="text-button" disabled={refreshing} onClick={() => void onRefresh()}>{refreshing ? 'Обновляем…' : 'Повторить проверку'}</button>}</div></div>}
      {available && <form onSubmit={handleSubmit}>
        <label htmlFor="deploy-uri" className="field-label">URI модели в MLflow</label>
        <input id="deploy-uri" className="uri-input" value={uri} onChange={event => { setUri(event.target.value); setFieldError(''); }} placeholder="models:/credit-scoring/18" autoComplete="off" disabled={!available || submitting || running} aria-invalid={Boolean(fieldError)} aria-describedby={fieldError ? 'uri-error' : 'uri-help'} required />
        <p id="uri-help" className="field-help">Имя модели и версия определяются из URI.</p>
        {fieldError && <p id="uri-error" className="field-error" role="alert">{fieldError}</p>}
        {parsed && <div className="deployment-preview"><span>Модель <strong>{parsed.model}</strong></span><span>Версия <strong>{parsed.version}</strong></span></div>}
        <button className="button button-primary" type="submit" disabled={!available || submitting || running || !uri.trim()}>{submitting ? <><Icon name="refresh" className="spinning" />Отправка…</> : <><Icon name="upload" />{running ? 'Развёртывание выполняется' : 'Развернуть модель'}</>}</button>
      </form>}
      {submissionError && <div className="inline-notice notice-error" role="alert"><Icon name="alert" /><span>{submissionError}</span></div>}
      {deployment && <div className={`deployment-state ${deployment.status === 'failed' ? 'deployment-failed' : ''}`} aria-live="polite">
        <div className="deployment-state-top"><strong>{deploymentLabels[deployment.status] || 'Проверка недоступна'}</strong>{!running && <button type="button" className="text-button" onClick={handleDismiss}>Скрыть</button>}</div>
        {deployment.model && <p className="deployment-model">{deployment.model} · версия {deployment.version || 'уточняется'}</p>}
        {running && stage >= 0 && <ol className="deployment-progress" aria-label="Этапы развёртывания">{stages.map((item, index) => <li key={item} className={index <= stage ? 'reached' : ''} aria-current={index === stage ? 'step' : undefined}><span />{['Очередь', 'Загрузка', 'Runtime', 'Проверка', 'Готово'][index]}</li>)}</ol>}
        <code className="deployment-id">{deployment.id}</code>
        {deployment.status === 'active' && <p className="success-text">Модель доступна для запросов. Список моделей обновлён.</p>}
        {deployment.error && <p className="field-error">{deployment.error.message}</p>}
        {pollError && <div className="inline-notice notice-warning"><span>{pollError}</span><button type="button" className="text-button" onClick={() => setRetry(value => value + 1)}>Повторить проверку</button></div>}
      </div>}
    </section>
  );
}
