import React, { useEffect, useRef, useState } from 'react';
import Icon from './Icons.jsx';
import { request } from './api.js';

export default function QueryPanel({ models, selectedModel, onSelect, loading, unavailable }) {
  const model = models.find(item => item.id === selectedModel);
  const [input, setInput] = useState('');
  const [inputError, setInputError] = useState('');
  const [prediction, setPrediction] = useState(null);
  const [predictionError, setPredictionError] = useState('');
  const [health, setHealth] = useState(null);
  const [predicting, setPredicting] = useState(false);
  const [checking, setChecking] = useState(false);
  const [copied, setCopied] = useState('');
  const predictionRequest = useRef(null);
  const healthRequest = useRef(null);

  useEffect(() => {
    setInput(JSON.stringify(model?.input_example ?? {}, null, 2));
    setInputError(''); setPrediction(null); setPredictionError('');
    setHealth(null); setPredicting(false); setChecking(false); setCopied('');
    return () => { predictionRequest.current?.abort(); healthRequest.current?.abort(); };
  }, [model?.id, model?.version]);

  const handleHealth = async () => {
    if (!model || checking) return;
    const controller = new AbortController();
    healthRequest.current = controller;
    setChecking(true); setHealth(null);
    try {
      const result = await request(`/v1/models/${encodeURIComponent(model.id)}/health`, { signal: controller.signal, timeout: 10000 });
      if (!controller.signal.aborted) setHealth(result);
    } catch (error) {
      if (!controller.signal.aborted) setHealth({ status: 'unhealthy', message: error.message });
    } finally {
      if (!controller.signal.aborted) setChecking(false);
    }
  };

  const handlePredict = async event => {
    event.preventDefault();
    if (!model || predicting) return;
    let parsed;
    try { parsed = JSON.parse(input); } catch {
      setInputError('Некорректный JSON. Проверьте кавычки, запятые и скобки.');
      document.getElementById('prediction-input')?.focus();
      return;
    }
    const controller = new AbortController();
    predictionRequest.current = controller;
    setInputError(''); setPredictionError(''); setPrediction(null); setCopied(''); setPredicting(true);
    const started = performance.now();
    try {
      const result = await request('/v1/responses', {
        method: 'POST', signal: controller.signal, timeout: 65000,
        body: JSON.stringify({ model: model.id, input: parsed }),
      });
      if (!controller.signal.aborted) setPrediction({ result, milliseconds: Math.round(performance.now() - started) });
    } catch (error) {
      if (!controller.signal.aborted) setPredictionError(error.message);
    } finally {
      if (!controller.signal.aborted) setPredicting(false);
    }
  };

  const output = prediction?.result.output?.length === 1
    ? prediction.result.output[0].content : prediction?.result.output;
  const outputText = JSON.stringify(output, null, 2);
  const handleCopy = async () => {
    try { await navigator.clipboard.writeText(outputText); setCopied('Скопировано'); }
    catch { setCopied('Не удалось скопировать. Выделите текст результата.'); }
  };

  return (
    <section className="panel query-panel" id="try-model" aria-labelledby="query-title">
      <div className="panel-heading"><div><h2 id="query-title">Проверить модель</h2><p>Пример входа берётся из опубликованной модели.</p></div><Icon name="play" className="panel-symbol" /></div>
      {!models.length ? (
        <div className="compact-empty"><Icon name="play" size={26} /><strong>{loading ? 'Загрузка моделей…' : 'Сначала запустите модель'}</strong><p>{unavailable ? 'Список моделей недоступен. Обновите подключение к сервису.' : 'Здесь появятся входной пример, проверка здоровья и результат предсказания.'}</p></div>
      ) : <>
        <label className="field-label" htmlFor="query-model">Модель</label>
        <div className="model-choice"><select id="query-model" value={selectedModel} onChange={event => onSelect(event.target.value)}><option value="">Выберите модель</option>{models.map(item => <option key={item.id} value={item.id}>{item.id} · v{item.version}</option>)}</select><button type="button" className="button button-quiet" disabled={!model || checking || unavailable} onClick={() => void handleHealth()}><Icon name="activity" />{checking ? 'Проверка…' : 'Проверить здоровье'}</button></div>
        {health && <div className={`inline-notice ${health.status === 'healthy' ? 'notice-success' : 'notice-error'}`} role="status"><Icon name={health.status === 'healthy' ? 'check' : 'alert'} /><span>{health.status === 'healthy' ? `Runtime отвечает · версия ${health.version}` : health.message || 'Runtime не отвечает. Попробуйте повторить проверку.'}</span></div>}
        {model ? <>
          <form onSubmit={handlePredict}>
            <div className="editor-heading"><label className="field-label" htmlFor="prediction-input">Входные данные · JSON</label><button type="button" className="text-button" disabled={predicting} onClick={() => { setInput(JSON.stringify(model.input_example, null, 2)); setInputError(''); setPrediction(null); setPredictionError(''); setCopied(''); }}>Восстановить пример</button></div>
            <textarea id="prediction-input" className="json-editor" rows={7} value={input} onChange={event => { setInput(event.target.value); setInputError(''); setPrediction(null); setPredictionError(''); setCopied(''); }} spellCheck={false} aria-invalid={Boolean(inputError)} aria-describedby={inputError ? 'input-error' : undefined} disabled={predicting} />
            {inputError && <p id="input-error" className="field-error" role="alert">{inputError}</p>}
            <div className="form-actions"><button className="button button-primary" type="submit" disabled={predicting || unavailable}>{predicting ? <><Icon name="refresh" className="spinning" />Выполняется…</> : <><Icon name="play" />Выполнить запрос</>}</button><span className="muted">{model.id} · v{model.version}</span></div>
          </form>
          {predictionError && <div className="inline-notice notice-error" role="alert"><Icon name="alert" /><span>{predictionError}</span></div>}
          {prediction && <div className="prediction-result" aria-live="polite"><div className="result-heading"><strong><Icon name="check" />Результат</strong><span>{prediction.milliseconds} мс · v{prediction.result.model_version}</span>{navigator.clipboard?.writeText && <button type="button" className="text-button" onClick={() => void handleCopy()} aria-label="Скопировать результат"><Icon name="copy" />Копировать</button>}</div><pre className="response-output">{outputText}</pre>{copied && <p className="muted" role="status">{copied}</p>}<details className="response-details"><summary>Полный ответ API</summary><pre className="response-output">{JSON.stringify(prediction.result, null, 2)}</pre></details></div>}
        </> : <p className="selection-hint">Выберите модель в списке выше, чтобы загрузить пример запроса.</p>}
      </>}
    </section>
  );
}
