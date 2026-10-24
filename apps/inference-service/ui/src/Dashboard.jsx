import React, { useEffect, useRef, useState } from 'react';
import DeploymentPanel from './DeploymentPanel.jsx';
import Icon from './Icons.jsx';
import MonitoringPanel from './MonitoringPanel.jsx';
import QueryPanel from './QueryPanel.jsx';
import RuntimeGroups from './RuntimeGroups.jsx';
import useServiceSnapshot from './useServiceSnapshot.js';
import { number } from './api.js';
import { runtimeTotals } from './runtimeData.js';

const sections = [
  { id: 'overview', label: 'Обзор', title: 'Inference Service', icon: 'grid', description: 'Контейнеры, состав моделей и фактические запросы к ним.' },
  { id: 'models', label: 'Контейнеры и модели', title: 'Контейнеры и модели', icon: 'model', description: 'Каждая группа — отдельный runtime-контейнер с одной или несколькими моделями.' },
  { id: 'try-model', label: 'Запрос к модели', title: 'Запрос к модели', icon: 'play', description: 'Проверка здоровья и предсказания выбранной модели.' },
  { id: 'monitoring', label: 'Мониторинг', title: 'Мониторинг', icon: 'activity', description: 'Состояние сервиса, проверки runtime и метрики каждой модели.' },
  { id: 'deploy', label: 'Развёртывание', title: 'Развёртывание', icon: 'upload', description: 'Запуск точной версии модели из MLflow Model Registry.' },
];
const currentSection = () => sections.some(section => section.id === window.location.hash.slice(1)) ? window.location.hash.slice(1) : 'overview';

export default function Dashboard() {
  const snapshot = useServiceSnapshot();
  const { status, modelsError, statusError, runtimesError, updatedAt, refreshing, refresh, runtimes } = snapshot;
  const models = snapshot.models ?? [];
  const [selectedModel, setSelectedModel] = useState('');
  const [activeSection, setActiveSection] = useState(currentSection);
  const main = useRef(null);
  const section = sections.find(item => item.id === activeSection);
  const totals = runtimeTotals(runtimes);
  const loading = snapshot.models === null && !modelsError;
  const disconnected = Boolean(statusError);
  const ready = status?.status === 'ready' && !disconnected;
  const statusLabel = disconnected ? 'Нет связи' : !status ? 'Подключение…' : ready ? 'Сервис готов' : 'Требует внимания';
  const groupView = ['overview', 'models', 'monitoring'].includes(activeSection);

  useEffect(() => {
    if (snapshot.models && selectedModel && !snapshot.models.some(item => item.id === selectedModel)) setSelectedModel('');
  }, [snapshot.models, selectedModel]);
  useEffect(() => {
    const handleLocation = () => setActiveSection(currentSection());
    window.addEventListener('hashchange', handleLocation);
    window.addEventListener('popstate', handleLocation);
    return () => { window.removeEventListener('hashchange', handleLocation); window.removeEventListener('popstate', handleLocation); };
  }, []);
  useEffect(() => { main.current?.scrollTo({ top: 0, behavior: 'instant' }); }, [activeSection]);
  const navigate = id => {
    if (window.location.hash !== `#${id}`) window.history.pushState(null, '', `#${id}`);
    setActiveSection(id);
    main.current?.scrollTo({ top: 0, behavior: 'instant' });
  };
  const handleSelect = id => { setSelectedModel(id); navigate('try-model'); };

  return <div className="app-shell">
    <header className="topbar">
      <a className="brand" href="#overview" onClick={event => { event.preventDefault(); navigate('overview'); }} aria-label="ML Inference — обзор"><span className="brand-mark"><Icon name="model" size={21} /></span><span>ML Inference</span></a>
      <div className="topbar-right"><span className={`service-pill ${ready ? 'is-ready' : disconnected || !status ? 'is-offline' : 'is-degraded'}`} role="status"><i />{statusLabel}</span><button type="button" className="button button-quiet" disabled={refreshing} onClick={() => void refresh()}><Icon name="refresh" className={refreshing ? 'spinning' : ''} />{refreshing ? 'Обновление…' : 'Обновить'}</button></div>
    </header>
    <div className="workspace">
      <aside className="sidebar">
        <div className="sidebar-heading">Управление сервисом</div>
        <nav aria-label="Разделы панели">{sections.map(item => <a href={`#${item.id}`} key={item.id} onClick={event => { event.preventDefault(); navigate(item.id); }} className={`side-link ${activeSection === item.id ? 'active' : ''}`} aria-current={activeSection === item.id ? 'page' : undefined} aria-label={item.label} title={item.label}><Icon name={item.icon} className="nav-icon" /><span className="nav-label">{item.label}</span>{item.id === 'models' && <span className="nav-count">{runtimes === null ? '—' : runtimes.length}</span>}</a>)}</nav>
        <div className="sidebar-note"><span className={`live-dot ${statusError || modelsError || runtimesError ? 'dot-warning' : ''}`} />{statusError || modelsError || runtimesError ? 'Повторяем подключение' : 'Автообновление включено'}<small>каждые 5 секунд</small></div>
      </aside>
      <main className="main-content" ref={main} id="main-content">
        <div className="page-layout">
          <div className="page-heading"><div><div className="eyebrow">УПРАВЛЕНИЕ МОДЕЛЯМИ</div><h1>{section.title}</h1><p>{section.description}</p></div><div className="updated">{updatedAt ? <>Данные на {updatedAt.toLocaleTimeString('ru-RU')} {(statusError || runtimesError) && <span className="stale-label">· устарели</span>}</> : 'Ожидание данных'}</div></div>
          {(statusError || modelsError) && <div className="alert" role="alert"><Icon name="alert" /><div><strong>{statusError ? 'Не удалось обновить состояние сервиса' : 'Не удалось обновить список моделей'}</strong><span>{statusError || modelsError} Повторяем автоматически.</span></div></div>}
          {status?.status === 'degraded' && !statusError && <div className="inline-notice notice-warning"><Icon name="alert" /><span>{status.recovery_required ? 'Сервис восстанавливает состояние моделей.' : 'Один из runtime не готов. Проверьте его в разделе мониторинга.'}</span></div>}
          <div className="view-section" hidden={activeSection !== 'overview'}>
            <section className="stat-grid" aria-label="Статистика внутри активных runtime">
              <Stat icon="model" color="purple" label="Runtime-контейнеры" value={number(runtimes?.length)} detail={status ? `${number(status.pending_deployments)} развёртываний в работе` : 'Ожидание данных'} />
              <Stat icon="grid" color="green" label="Активные модели" value={number(runtimes === null ? undefined : totals.model_count)} detail="Внутри runtime-контейнеров" />
              <Stat icon="activity" color="blue" label="Запросы к моделям" value={number(totals.complete ? totals.total : undefined)} detail={totals.complete ? `${number(totals.in_progress)} выполняются сейчас` : 'Нет данных от части runtime'} />
              <Stat icon="alert" color="orange" label="Ошибки моделей" value={number(totals.complete ? totals.failed : undefined)} detail="По счётчикам контейнеров" />
            </section>
          </div>
          <div className="view-section" hidden={activeSection !== 'monitoring'}><MonitoringPanel status={statusError ? null : status} runtimes={runtimes} refreshing={refreshing} onRefresh={refresh} /></div>
          <div className="view-section" hidden={!groupView}><RuntimeGroups runtimes={runtimes} error={runtimesError} selectedModel={selectedModel} onSelect={handleSelect} onRefresh={refresh} refreshing={refreshing} /></div>
          <div className="view-section" hidden={activeSection !== 'try-model'}><QueryPanel models={models} selectedModel={selectedModel} onSelect={setSelectedModel} loading={loading} unavailable={Boolean(modelsError)} /></div>
          <div className="view-section" hidden={activeSection !== 'deploy'}><DeploymentPanel capability={status?.capabilities?.deployments} connectionError={statusError} refreshing={refreshing} onRefresh={refresh} /></div>
          <footer className="page-footer"><Icon name="clock" size={14} />Запросы считаются отдельно в каждой модели runtime. Healthcheck и прогрев не учитываются. Счётчики сбрасываются при перезапуске контейнера.</footer>
        </div>
      </main>
    </div>
    <footer className="app-footer">
      <div className="app-footer-content">
        <a href="https://boldov.dev" target="_blank" rel="noopener noreferrer" className="app-footer-brand">
          <Icon name="model" size={15} />
          <span>Boldov Development Ltd</span>
        </a>
        <span className="app-footer-divider" aria-hidden="true">•</span>
        <span className="app-footer-year">© {new Date().getFullYear()}</span>
      </div>
    </footer>
  </div>;
}

function Stat({ icon, color, label, value, detail }) {
  return <article className="stat-card"><div className={`stat-icon ${color}`}><Icon name={icon} /></div><div className="stat-label">{label}</div><strong>{value}</strong><small>{detail}</small></article>;
}
