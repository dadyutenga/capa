import { useEffect, useMemo, useRef, useState } from 'react';

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...(options.headers || {}) },
  });
  const text = await response.text();
  let payload = {};
  try { payload = text ? JSON.parse(text) : {}; } catch { payload = { detail: text }; }
  if (!response.ok) throw new Error(payload.detail || `Request failed (${response.status})`);
  return payload;
}

const NAV = [
  { id: 'overview', label: 'Overview', mark: '◫' },
  { id: 'journeys', label: 'Journeys', mark: '↗' },
  { id: 'targets', label: 'Targets', mark: '◎' },
  { id: 'history', label: 'Test history', mark: '◷' },
  { id: 'safety', label: 'Safety', mark: '◇' },
];

const DEFAULT_CONFIG = {
  concurrency: 5,
  ramp_seconds: 10,
  duration_seconds: 60,
  max_requests: 1000,
  rate_limit_rps: 5,
  threshold_p95_ms: 1000,
  threshold_error_percent: 2,
};

function IconMark({ children }) {
  return <span className="icon-mark" aria-hidden="true">{children}</span>;
}

function MetricCard({ label, value, unit, note, accent = 'blue', mark }) {
  return (
    <section className={`metric-card metric-${accent}`}>
      <div className="metric-top"><span>{label}</span><IconMark>{mark}</IconMark></div>
      <div className="metric-value">{value}<small>{unit}</small></div>
      <div className="metric-note">{note}</div>
    </section>
  );
}

function TrendChart({ values = [] }) {
  const width = 600;
  const height = 180;
  const series = values.length ? values.slice(-36) : [0, 0];
  const max = Math.max(1, ...series);
  const min = Math.min(0, ...series);
  const range = Math.max(1, max - min);
  const coords = series.map((value, index) => {
    const x = 8 + (index / Math.max(1, series.length - 1)) * (width - 16);
    const y = height - 12 - ((value - min) / range) * (height - 32);
    return `${x},${y}`;
  }).join(' ');
  return (
    <svg className="trend-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Live p95 latency trend">
      {[36, 80, 124, 168].map((y) => <line key={y} x1="0" x2={width} y1={y} y2={y} className="chart-grid" />)}
      <polyline points={coords} className="chart-line" />
      {series.length > 0 && <circle cx={coords.split(' ').at(-1)?.split(',')[0]} cy={coords.split(' ').at(-1)?.split(',')[1]} r="4" className="chart-dot" />}
    </svg>
  );
}

function StatusPill({ status }) {
  const value = status || 'not run';
  const className = ['running', 'queued', 'stopping'].includes(value) ? 'live' : ['completed', 'verified'].includes(value) ? 'ok' : value === 'stopped' ? 'muted' : 'warn';
  return <span className={`status-pill ${className}`}><i />{value.replaceAll('_', ' ')}</span>;
}

function EmptyState({ mark = '·', title, copy, action }) {
  return <div className="empty-state"><div className="empty-mark">{mark}</div><h3>{title}</h3><p>{copy}</p>{action}</div>;
}

function App() {
  const [workspace, setWorkspace] = useState(null);
  const [projectId, setProjectId] = useState(null);
  const [view, setView] = useState('overview');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [showProjectModal, setShowProjectModal] = useState(false);
  const [projectName, setProjectName] = useState('');
  const [projectEnvironment, setProjectEnvironment] = useState('staging');
  const [targetId, setTargetId] = useState('');
  const [journeyId, setJourneyId] = useState('');
  const [config, setConfig] = useState(DEFAULT_CONFIG);
  const [runAuthorizationConfirmed, setRunAuthorizationConfirmed] = useState(false);
  const [liveRun, setLiveRun] = useState(null);
  const [trend, setTrend] = useState([]);
  const eventSourceRef = useRef(null);

  const [newTargetKind, setNewTargetKind] = useState('simulator');
  const [newTargetName, setNewTargetName] = useState('Staging API');
  const [newTargetOrigin, setNewTargetOrigin] = useState('');
  const [newTargetAuthorized, setNewTargetAuthorized] = useState(false);
  const [journeyName, setJourneyName] = useState('New user journey');
  const [journeySteps, setJourneySteps] = useState([{ method: 'GET', path: '/' }]);
  const [editingJourneyId, setEditingJourneyId] = useState(null);

  const refresh = async (requestedProjectId = projectId) => {
    try {
      const path = requestedProjectId ? `/api/workspace?project_id=${requestedProjectId}` : '/api/workspace';
      const data = await api(path);
      setWorkspace(data);
      setProjectId(data.selected_project_id);
      if (data.targets.length && !data.targets.some((item) => String(item.id) === String(targetId))) {
        const preferred = data.targets.find((item) => item.kind === 'simulator') || data.targets[0];
        setTargetId(String(preferred.id));
      }
      if (data.journeys.length && !data.journeys.some((item) => String(item.id) === String(journeyId))) {
        setJourneyId(String(data.journeys[0].id));
      }
      setError('');
      setLoading(false);
      return data;
    } catch (err) {
      setError(err.message);
      setLoading(false);
      return null;
    }
  };

  useEffect(() => { refresh(null); }, []);
  useEffect(() => {
    const timer = setTimeout(() => setNotice(''), 3800);
    return () => clearTimeout(timer);
  }, [notice]);
  useEffect(() => {
    const stats = liveRun?.stats;
    if (!stats) return;
    setTrend((current) => {
      const point = Number(stats.p95_ms || 0);
      const last = current.at(-1);
      if (last && last.completed === stats.completed) return current;
      return [...current, { completed: stats.completed, p95: point }].slice(-36);
    });
  }, [liveRun?.stats?.completed, liveRun?.stats?.p95_ms]);
  useEffect(() => {
    if (!liveRun?.id) return undefined;
    eventSourceRef.current?.close();
    const source = new EventSource(`/api/tests/${liveRun.id}/events`);
    eventSourceRef.current = source;
    source.onmessage = (event) => {
      try {
        const snapshot = JSON.parse(event.data);
        setLiveRun({ id: snapshot.test_id || liveRun.id, status: snapshot.status, stats: snapshot });
        if (['completed', 'stopped', 'failed'].includes(snapshot.status)) {
          source.close();
          refresh(projectId);
        }
      } catch { /* Ignore an incomplete event; the stream will send another snapshot. */ }
    };
    return () => source.close();
  }, [liveRun?.id]);

  const projects = workspace?.projects || [];
  const targets = workspace?.targets || [];
  const journeys = workspace?.journeys || [];
  const tests = workspace?.tests || [];
  const safety = workspace?.safety || { caps: {}, kill_switch_active: false };
  const activeProject = projects.find((item) => item.id === projectId);
  const selectedTarget = targets.find((item) => String(item.id) === String(targetId));
  const selectedJourney = journeys.find((item) => String(item.id) === String(journeyId));
  const displayedStats = liveRun?.stats || tests[0]?.stats || {};
  const runActive = liveRun && ['queued', 'running', 'stopping'].includes(liveRun.status);
  const chartValues = trend.map((item) => item.p95);

  const notify = (message) => { setNotice(message); setError(''); };
  const fail = (message) => { setError(message); setNotice(''); };

  async function createProject(event) {
    event.preventDefault();
    setBusy(true);
    try {
      const created = await api('/api/projects', { method: 'POST', body: JSON.stringify({ name: projectName, environment: projectEnvironment }) });
      setShowProjectModal(false);
      setProjectName('');
      await refresh(created.id);
      setView('overview');
      notify(`${created.name} is ready.`);
    } catch (err) { fail(err.message); }
    finally { setBusy(false); }
  }

  async function createTarget(event) {
    event.preventDefault();
    if (!projectId) return;
    setBusy(true);
    try {
      const body = newTargetKind === 'simulator'
        ? { name: newTargetName, kind: 'simulator', authorization_confirmed: true }
        : { name: newTargetName, kind: 'http', base_url: newTargetOrigin, authorization_confirmed: newTargetAuthorized };
      const created = await api(`/api/projects/${projectId}/targets`, { method: 'POST', body: JSON.stringify(body) });
      await refresh(projectId);
      setTargetId(String(created.id));
      setNewTargetOrigin('');
      setNewTargetAuthorized(false);
      notify(created.kind === 'http' ? 'Target added. Publish its DNS TXT challenge, then verify it.' : 'Local simulator added. It does not send network traffic.');
    } catch (err) { fail(err.message); }
    finally { setBusy(false); }
  }

  async function verifyTarget(id) {
    setBusy(true);
    try {
      const result = await api(`/api/targets/${id}/verify`, { method: 'POST', body: '{}' });
      await refresh(projectId);
      notify(result.message || 'Target ownership verified.');
    } catch (err) { fail(err.message); }
    finally { setBusy(false); }
  }

  async function removeTarget(id) {
    if (!window.confirm('Remove this target? Targets referenced by history are retained.')) return;
    try {
      await api(`/api/targets/${id}`, { method: 'DELETE' });
      await refresh(projectId);
      notify('Target removed.');
    } catch (err) { fail(err.message); }
  }

  function updateStep(index, key, value) {
    setJourneySteps((current) => current.map((step, i) => i === index ? { ...step, [key]: value } : step));
  }

  async function createJourney(event) {
    event.preventDefault();
    setBusy(true);
    try {
      const steps = journeySteps.map((step) => {
        const value = { method: step.method, path: step.path };
        if (step.method === 'POST' && step.body?.trim()) value.body = step.body;
        return value;
      });
      const endpoint = editingJourneyId ? `/api/journeys/${editingJourneyId}` : `/api/projects/${projectId}/journeys`;
      const created = await api(endpoint, { method: editingJourneyId ? 'PUT' : 'POST', body: JSON.stringify({ name: journeyName, steps }) });
      await refresh(projectId);
      setJourneyId(String(created.id));
      setEditingJourneyId(null);
      setJourneyName('New user journey');
      setJourneySteps([{ method: 'GET', path: '/' }]);
      notify(editingJourneyId ? 'Journey updates saved.' : 'Journey saved. It contains declarative HTTP steps only.');
    } catch (err) { fail(err.message); }
    finally { setBusy(false); }
  }

  async function removeJourney(id) {
    if (!window.confirm('Remove this reusable journey? Journeys referenced by history are retained.')) return;
    try {
      await api(`/api/journeys/${id}`, { method: 'DELETE' });
      await refresh(projectId);
      notify('Journey removed.');
    } catch (err) { fail(err.message); }
  }

  function editJourney(item) {
    setEditingJourneyId(item.id);
    setJourneyName(item.name);
    setJourneySteps(item.steps.map((step) => ({ ...step })));
    setView('journeys');
  }

  function updateConfig(key, value) {
    setConfig((current) => ({ ...current, [key]: Number(value) }));
  }

  async function startTest(event) {
    event.preventDefault();
    if (!selectedTarget || !selectedJourney) return;
    setBusy(true);
    setError('');
    try {
      const result = await api('/api/tests/start', {
        method: 'POST',
        body: JSON.stringify({
          target_id: Number(targetId),
          journey_id: Number(journeyId),
          authorization_confirmed: selectedTarget.kind === 'simulator' || runAuthorizationConfirmed,
          ...config,
        }),
      });
      setTrend([]);
      setLiveRun({ id: result.id, status: 'running', stats: { test_id: result.id, status: 'running', completed: 0, requests_started: 0, p95_ms: 0, p50_ms: 0, p99_ms: 0, throughput_rps: 0, error_rate_percent: 0, active_users: 0, alerts: [] } });
      setRunAuthorizationConfirmed(false);
      notify('Test started. Live metrics are streaming.');
    } catch (err) { fail(err.message); }
    finally { setBusy(false); }
  }

  async function stopTest() {
    if (!liveRun?.id) return;
    try {
      await api(`/api/tests/${liveRun.id}/stop`, { method: 'POST', body: '{}' });
      notify('Stop requested. Active workers are being cancelled.');
    } catch (err) { fail(err.message); }
  }

  async function toggleKillSwitch() {
    const active = !safety.kill_switch_active;
    try {
      await api('/api/kill-switch', { method: 'POST', body: JSON.stringify({ active }) });
      await refresh(projectId);
      if (active) {
        if (liveRun?.id) setLiveRun((current) => current ? { ...current, status: 'stopped' } : current);
        notify('Global kill switch engaged. All active workers were cancelled.');
      } else notify('Runner re-enabled. Target and authorization checks still apply.');
    } catch (err) { fail(err.message); }
  }

  async function exportCsv(id) {
    try {
      const response = await fetch(`/api/tests/${id}/export.csv`);
      if (!response.ok) throw new Error('Could not export this test.');
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = `capacity-lab-test-${id}.csv`;
      anchor.click();
      URL.revokeObjectURL(url);
      notify('CSV export downloaded.');
    } catch (err) { fail(err.message); }
  }

  function copyText(value) {
    if (!navigator.clipboard) { fail('Clipboard access is not available in this browser.'); return; }
    navigator.clipboard.writeText(value).then(() => notify('Copied to clipboard.')).catch(() => fail('Clipboard access is not available in this browser.'));
  }

  if (loading) return <div className="loading-screen"><div className="brand-symbol"><span /></div><p>Opening Capacity Lab…</p></div>;
  if (!workspace) return <div className="loading-screen"><div className="error-panel"><h2>Capacity Lab could not connect</h2><p>{error || 'Start the local backend, then reload.'}</p><button className="button primary" onClick={() => refresh(null)}>Retry connection</button></div></div>;

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-lockup">
          <div className="brand-symbol"><span /></div>
          <div><strong>capacity<span>lab</span></strong><small>STAGING FIRST</small></div>
        </div>
        <div className="sidebar-project-label">WORKSPACE</div>
        <button className="project-switcher" onClick={() => setShowProjectModal(true)}>
          <span className="project-avatar">{activeProject?.name?.slice(0, 1).toUpperCase() || 'C'}</span>
          <span className="project-switch-text"><b>{activeProject?.name || 'Choose project'}</b><small>{activeProject?.environment || 'local'}</small></span>
          <span className="chevron">⌄</span>
        </button>
        <nav className="main-nav" aria-label="Main navigation">
          {NAV.map((item) => (
            <button key={item.id} className={`nav-item ${view === item.id ? 'selected' : ''}`} onClick={() => setView(item.id)}>
              <IconMark>{item.mark}</IconMark><span>{item.label}</span>{item.id === 'history' && tests.length > 0 && <em>{tests.length}</em>}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className={`side-safety ${safety.kill_switch_active ? 'is-locked' : ''}`}>
            <span className="safety-dot" /><div><b>{safety.kill_switch_active ? 'Runner locked' : 'Guardrails active'}</b><small>{safety.kill_switch_active ? 'Kill switch is on' : 'External targets require proof'}</small></div>
          </div>
          <button className="sidebar-help" onClick={() => setView('safety')}><span>?</span> Safety controls</button>
          <div className="local-only"><span className="local-dot" />Local operator mode</div>
        </div>
      </aside>

      <main className="main-panel">
        <header className="topbar">
          <div className="breadcrumbs"><span>WORKSPACE</span><b>/</b><strong>{NAV.find((item) => item.id === view)?.label || 'Overview'}</strong></div>
          <div className="top-actions">
            {projects.length > 1 && <select className="header-project-select" aria-label="Switch project" value={projectId || ''} onChange={(event) => refresh(Number(event.target.value))}>{projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select>}
            <span className="environment-badge"><i />{activeProject?.environment === 'simulation' ? 'SIMULATION' : 'STAGING ONLY'}</span>
            <button className={`kill-button ${safety.kill_switch_active ? 'locked' : ''}`} onClick={toggleKillSwitch}>
              <span className="kill-icon">{safety.kill_switch_active ? '▶' : '■'}</span>{safety.kill_switch_active ? 'Resume runner' : 'Emergency stop'}
            </button>
          </div>
        </header>

        {(error || notice) && <div className={`flash-message ${error ? 'flash-error' : 'flash-success'}`} role="status"><span>{error ? '!' : '✓'}</span><p>{error || notice}</p><button onClick={() => { setError(''); setNotice(''); }} aria-label="Dismiss">×</button></div>}

        {view === 'overview' && (
          <div className="page-content">
            <section className="page-heading overview-heading">
              <div>
                <div className="eyebrow"><span className="eyebrow-line" />CAPACITY TESTING, WITH GUARDRAILS</div>
                <h1>Know what your app can handle.</h1>
                <p>Replay a real user journey against an authorized staging target, then watch capacity unfold in real time.</p>
              </div>
              <button className="button subtle" onClick={() => setShowProjectModal(true)}><span>＋</span> New project</button>
            </section>

            <div className="metrics-grid">
              <MetricCard label="Virtual users" value={displayedStats.active_users ?? 0} unit="" note={runActive ? 'Currently active' : 'No active run'} accent="blue" mark="♧" />
              <MetricCard label="p95 latency" value={displayedStats.p95_ms ?? '—'} unit={displayedStats.p95_ms ? 'ms' : ''} note={displayedStats.p50_ms ? `p50 ${displayedStats.p50_ms} ms · p99 ${displayedStats.p99_ms} ms` : 'Measured per run'} accent="violet" mark="◌" />
              <MetricCard label="Throughput" value={displayedStats.throughput_rps ?? '—'} unit={displayedStats.throughput_rps ? 'req/s' : ''} note={`${displayedStats.completed ?? 0} requests completed`} accent="green" mark="↗" />
              <MetricCard label="Error rate" value={displayedStats.error_rate_percent ?? '—'} unit={displayedStats.error_rate_percent !== undefined ? '%' : ''} note={displayedStats.failed ? `${displayedStats.failed} failed requests` : 'No failures recorded'} accent="orange" mark="!" />
            </div>

            <div className="overview-grid">
              <section className="panel run-panel">
                <div className="panel-heading">
                  <div><div className="section-kicker">RUN CONFIGURATION</div><h2>Shape your test</h2></div>
                  <span className="guarded-label"><span className="mini-shield">◇</span> Hard-capped</span>
                </div>
                {!targets.length || !journeys.length ? <EmptyState mark="＋" title="Add a target and a journey" copy="Create a staging target or use the built-in local simulator, then save the user journey you want to measure." action={<button className="button primary" onClick={() => setView(!targets.length ? 'targets' : 'journeys')}>Set up {targets.length ? 'journeys' : 'a target'}</button>} /> : (
                  <form onSubmit={startTest} className="run-form">
                    <div className="form-two-col">
                      <label className="field"><span>Target</span><select value={targetId} onChange={(event) => { setTargetId(event.target.value); setRunAuthorizationConfirmed(false); }}>
                        {targets.map((item) => <option key={item.id} value={item.id} disabled={item.kind === 'http' && !item.verified}>{item.name}{item.kind === 'http' && !item.verified ? ' · verify first' : ''}</option>)}
                      </select><small>{selectedTarget?.kind === 'simulator' ? 'Runs locally; no network traffic is sent.' : selectedTarget?.verified ? selectedTarget.base_url : 'Unverified targets cannot be selected for traffic.'}</small></label>
                      <label className="field"><span>User journey</span><select value={journeyId} onChange={(event) => setJourneyId(event.target.value)}>{journeys.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.steps.length} steps</option>)}</select><small>Sequential, reusable request steps</small></label>
                    </div>
                    <div className="config-section-label"><span>LOAD PROFILE</span><i>Server limits: {safety.caps.concurrency || 50} users · {safety.caps.requests_per_second || 25} req/s · {safety.caps.duration_seconds || 300}s</i></div>
                    <div className="load-grid">
                      <label className="number-field"><span>Virtual users</span><div><input type="number" min="1" max={safety.caps.concurrency || 50} value={config.concurrency} onChange={(event) => updateConfig('concurrency', event.target.value)} /><small>users</small></div></label>
                      <label className="number-field"><span>Ramp-up</span><div><input type="number" min="0" max={Math.min(config.duration_seconds, safety.caps.duration_seconds || 300)} value={config.ramp_seconds} onChange={(event) => updateConfig('ramp_seconds', event.target.value)} /><small>seconds</small></div></label>
                      <label className="number-field"><span>Duration</span><div><input type="number" min="1" max={safety.caps.duration_seconds || 300} value={config.duration_seconds} onChange={(event) => updateConfig('duration_seconds', event.target.value)} /><small>seconds</small></div></label>
                      <label className="number-field"><span>Rate limit</span><div><input type="number" min="1" max={safety.caps.requests_per_second || 25} value={config.rate_limit_rps} onChange={(event) => updateConfig('rate_limit_rps', event.target.value)} /><small>req/s</small></div></label>
                    </div>
                    <label className="number-field request-budget"><span>Total request budget</span><div><input type="number" min="1" max={safety.caps.requests_per_test || 5000} value={config.max_requests} onChange={(event) => updateConfig('max_requests', event.target.value)} /><small>requests max</small></div></label>
                    <div className="threshold-row">
                      <label className="field compact"><span>p95 alert threshold</span><div className="input-with-unit"><input type="number" min="50" max="30000" value={config.threshold_p95_ms} onChange={(event) => updateConfig('threshold_p95_ms', event.target.value)} /><small>ms</small></div></label>
                      <label className="field compact"><span>Error alert threshold</span><div className="input-with-unit"><input type="number" min="0" max="100" step="0.1" value={config.threshold_error_percent} onChange={(event) => updateConfig('threshold_error_percent', event.target.value)} /><small>%</small></div></label>
                    </div>
                    {selectedTarget?.kind === 'http' && <label className="authorization-check"><input type="checkbox" checked={runAuthorizationConfirmed} onChange={(event) => setRunAuthorizationConfirmed(event.target.checked)} /><span><b>I have explicit written permission to test this exact scheme, host, and port.</b><small>No third-party target traffic without authorization. This confirmation is required for every run.</small></span></label>}
                    <div className="run-submit-row"><div className="run-guard-note"><span className="mini-shield">◇</span><span>Requests, duration, concurrency, and egress are enforced by the server.</span></div>
                      {runActive ? <button type="button" className="button danger" onClick={stopTest}>Stop this test</button> : <button className="button primary run-button" disabled={busy || safety.kill_switch_active || !selectedTarget || !selectedJourney || (selectedTarget.kind === 'http' && (!selectedTarget.verified || !runAuthorizationConfirmed))}><span>{busy ? 'Starting…' : '▶'}</span>{busy ? 'Starting…' : 'Start load test'}</button>}
                    </div>
                    {safety.kill_switch_active && <div className="inline-warning">The global kill switch is engaged. Resume the runner before starting.</div>}
                  </form>
                )}
              </section>

              <section className="panel live-panel">
                <div className="panel-heading live-heading"><div><div className="section-kicker">LIVE TELEMETRY</div><h2>{runActive ? 'Test in progress' : 'Performance trend'}</h2></div><StatusPill status={liveRun?.status || tests[0]?.status} /></div>
                {runActive ? (
                  <div className="live-summary"><div className="live-run-row"><span className="live-pulse" />RUN #{liveRun.id}<span className="live-time">{displayedStats.elapsed_seconds || 0}s elapsed</span></div><div className="live-chart-wrap"><TrendChart values={chartValues} /><div className="chart-labels"><span>p95 latency</span><b>{displayedStats.p95_ms || 0} ms</b></div></div><div className="live-stat-row"><div><b>{displayedStats.requests_started || 0}</b><small>started</small></div><div><b>{displayedStats.completed || 0}</b><small>completed</small></div><div><b>{displayedStats.active_users || 0}</b><small>active users</small></div></div>
                    {displayedStats.alerts?.length > 0 && <div className="alert-list">{displayedStats.alerts.map((alert, index) => <div key={index} className="alert-item"><span>!</span>{alert.message}</div>)}</div>}
                  </div>
                ) : tests.length ? (
                  <div className="live-summary"><div className="live-chart-wrap"><TrendChart values={tests.slice(0, 12).reverse().map((item) => item.stats?.p95_ms || 0)} /><div className="chart-labels"><span>Latest p95 latency</span><b>{tests[0]?.stats?.p95_ms || 0} ms</b></div></div><div className="last-run-card"><div className="last-run-icon">↗</div><div><b>Latest run · {tests[0].journey_name}</b><small>{tests[0].target_name} · {tests[0].stats?.completed || 0} requests</small></div><StatusPill status={tests[0].status} /></div><button className="text-button" onClick={() => setView('history')}>View full test history <span>→</span></button></div>
                ) : <EmptyState mark="⌁" title="Your run will appear here" copy="Start with the local simulator to preview live latency, throughput, error-rate alerts, and stop controls." />}
              </section>
            </div>
            <section className="safety-banner"><div className="banner-mark">◇</div><div><b>Safe by default, not by convention.</b><p>External staging targets need DNS ownership proof and a fresh authorization check. All resolved IPs are screened and pinned; redirects cannot leave the authorized origin.</p></div><button className="text-button" onClick={() => setView('safety')}>Review safeguards <span>→</span></button></section>
          </div>
        )}

        {view === 'targets' && (
          <div className="page-content">
            <section className="page-heading"><div><div className="eyebrow"><span className="eyebrow-line" />AUTHORIZED EGRESS ONLY</div><h1>Targets</h1><p>Use the local simulator or prove control of an exact staging origin before any external traffic.</p></div><div className="heading-count">{targets.length} {targets.length === 1 ? 'target' : 'targets'}</div></section>
            <div className="target-layout">
              <section className="panel target-list-panel"><div className="panel-heading"><div><div className="section-kicker">PROJECT TARGETS</div><h2>Registered targets</h2></div><span className="count-chip">{targets.length}</span></div>
                {targets.length ? <div className="target-list">{targets.map((item) => <article className="target-card" key={item.id}>
                  <div className="target-card-top"><div className={`target-icon ${item.kind === 'simulator' ? 'sim-icon' : ''}`}>{item.kind === 'simulator' ? '⌁' : '◎'}</div><div className="target-title"><h3>{item.name}</h3><p>{item.base_url}</p></div><StatusPill status={item.kind === 'simulator' ? 'completed' : item.verified ? 'verified' : 'pending'} /></div>
                  <div className="target-card-meta"><span><b>Environment</b>{item.kind === 'simulator' ? 'Local simulator' : 'Staging'}</span><span><b>Scope</b>{item.kind === 'http' ? item.origin_key : 'No external egress'}</span></div>
                  {item.kind === 'http' && !item.verified && <div className="challenge-box"><div className="challenge-title"><span>OWNERSHIP CHALLENGE</span><small>DNS TXT · required before load</small></div><p>Add this TXT record to the target hostname's DNS zone:</p><div className="copy-row"><code>{item.challenge_record_name}</code><button onClick={() => copyText(item.challenge_record_name)}>Copy name</button></div><div className="copy-row"><code>{item.challenge_record_value}</code><button onClick={() => copyText(item.challenge_record_value)}>Copy value</button></div><div className="challenge-actions"><button className="button primary small-button" disabled={busy} onClick={() => verifyTarget(item.id)}>Verify ownership</button><span>Only DNS is queried; the target receives no verification request.</span></div></div>}
                  {item.kind === 'http' && item.verified && <div className="verified-note"><span>✓</span> DNS ownership verified · every test re-checks the proof and public IPs.</div>}
                  <div className="target-card-actions">{item.kind === 'simulator' && <span className="local-safe"><i />No network traffic</span>}<button className="text-button danger-text" onClick={() => removeTarget(item.id)}>Remove target</button></div>
                </article>)}</div> : <EmptyState mark="◎" title="No targets yet" copy="Add your staging origin or create a local simulator target." />}
              </section>
              <section className="panel add-target-panel"><div className="panel-heading"><div><div className="section-kicker">SET UP A TARGET</div><h2>Add target</h2></div><span className="setup-number">01</span></div>
                <form onSubmit={createTarget} className="stacked-form">
                  <label className="field"><span>Target type</span><select value={newTargetKind} onChange={(event) => setNewTargetKind(event.target.value)}><option value="http" disabled={activeProject?.environment === 'simulation'}>External staging origin</option><option value="simulator">Local simulator</option></select></label>
                  <label className="field"><span>Target name</span><input value={newTargetName} onChange={(event) => setNewTargetName(event.target.value)} required minLength="2" maxLength="80" placeholder="Payments staging" /></label>
                  {newTargetKind === 'http' && <>
                    <label className="field"><span>Exact origin</span><input type="url" value={newTargetOrigin} onChange={(event) => setNewTargetOrigin(event.target.value)} required placeholder="https://staging.example.com:8443" /><small>Only scheme, hostname, and optional port. No paths, IP literals, or production targets.</small></label>
                    <label className="authorization-check"><input type="checkbox" checked={newTargetAuthorized} onChange={(event) => setNewTargetAuthorized(event.target.checked)} /><span><b>I own this staging origin or have written authorization to test it.</b><small>The DNS challenge is scoped to this exact scheme, host, and port.</small></span></label>
                  </>}
                  {newTargetKind === 'simulator' && <div className="simulator-info"><span>⌁</span><div><b>Safe local preview</b><p>Generates synthetic responses within this app. No external requests are made.</p></div></div>}
                  <button className="button primary full-button" disabled={busy || (newTargetKind === 'http' && !newTargetAuthorized)}>{busy ? 'Saving…' : newTargetKind === 'simulator' ? 'Add simulator' : 'Create DNS challenge'}</button>
                </form>
              </section>
            </div>
          </div>
        )}

        {view === 'journeys' && (
          <div className="page-content">
            <section className="page-heading"><div><div className="eyebrow"><span className="eyebrow-line" />REUSABLE, DECLARATIVE FLOWS</div><h1>User journeys</h1><p>Define a sequence of ordinary HTTP requests once, then reuse it across bounded test runs.</p></div><span className="heading-count">{journeys.length} saved</span></section>
            <div className="journey-layout">
              <section className="panel journey-list-panel"><div className="panel-heading"><div><div className="section-kicker">SAVED JOURNEYS</div><h2>Journey library</h2></div></div>
                {journeys.length ? <div className="journey-list">{journeys.map((item, index) => <article className="journey-card" key={item.id}><div className="journey-card-head"><div className="journey-index">{String(index + 1).padStart(2, '0')}</div><div className="journey-name"><h3>{item.name}</h3><small>{item.steps.length} steps · updated {new Date(item.updated_at).toLocaleDateString()}</small></div><button className="edit-journey" onClick={() => editJourney(item)}>Edit</button><button className="icon-button" onClick={() => removeJourney(item.id)} aria-label={`Remove ${item.name}`}>×</button></div><div className="journey-steps">{item.steps.map((step, stepIndex) => <div className="journey-step" key={`${item.id}-${stepIndex}`}><span className={`method-tag method-${step.method.toLowerCase()}`}>{step.method}</span><code>{step.path}</code><span className="step-arrow">↘</span></div>)}</div></article>)}</div> : <EmptyState mark="↗" title="No journeys saved" copy="Add request steps in order. Code and arbitrary scripts are intentionally not supported." />}
              </section>
              <section className="panel create-journey-panel"><div className="panel-heading"><div><div className="section-kicker">JOURNEY BUILDER</div><h2>Compose a flow</h2></div><span className="setup-number">02</span></div>
                <form onSubmit={createJourney} className="stacked-form"><label className="field"><span>{editingJourneyId ? 'Editing journey' : 'Journey name'}</span><input value={journeyName} onChange={(event) => setJourneyName(event.target.value)} required minLength="2" maxLength="80" /></label>
                  <div className="steps-builder-label"><span>REQUEST STEPS</span><small>{journeySteps.length}/30</small></div>
                  <div className="editable-steps">{journeySteps.map((step, index) => <div className="editable-step" key={index}><div className="step-number">{String(index + 1).padStart(2, '0')}</div><div className="step-fields"><select aria-label="Method" value={step.method} onChange={(event) => updateStep(index, 'method', event.target.value)}><option>GET</option><option>HEAD</option><option>POST</option></select><input aria-label="Relative path" value={step.path} onChange={(event) => updateStep(index, 'path', event.target.value)} required placeholder="/account" /></div>{journeySteps.length > 1 && <button type="button" className="remove-step" onClick={() => setJourneySteps((current) => current.filter((_, i) => i !== index))} aria-label="Remove step">×</button>}{step.method === 'POST' && <textarea className="step-body" value={step.body || ''} onChange={(event) => updateStep(index, 'body', event.target.value)} placeholder={'Optional JSON body (max 2 KB)'} rows="2" />}</div>)}</div>
                  <button type="button" className="add-step-button" disabled={journeySteps.length >= 30} onClick={() => setJourneySteps((current) => [...current, { method: 'GET', path: '/' }])}><span>＋</span> Add request step</button>
                  <div className="declarative-note"><span>◇</span><p>Only relative paths and GET, HEAD, or POST are allowed. Credentials, cookies, arbitrary headers, and user-supplied code are blocked. POST bodies are stored locally as text; never enter passwords or secrets.</p></div>
                  <button className="button primary full-button" disabled={busy}>{busy ? 'Saving…' : editingJourneyId ? 'Save journey updates' : 'Save reusable journey'}</button>
                  {editingJourneyId && <button type="button" className="text-button" onClick={() => { setEditingJourneyId(null); setJourneyName('New user journey'); setJourneySteps([{ method: 'GET', path: '/' }]); }}>Cancel editing</button>}
                </form>
              </section>
            </div>
          </div>
        )}

        {view === 'history' && (
          <div className="page-content">
            <section className="page-heading"><div><div className="eyebrow"><span className="eyebrow-line" />MEASUREMENTS THAT PERSIST</div><h1>Test history</h1><p>Review run outcomes, compare latency and export a portable CSV summary.</p></div><span className="heading-count">{tests.length} recent</span></section>
            <section className="panel history-panel"><div className="panel-heading"><div><div className="section-kicker">RECENT TESTS</div><h2>Run archive</h2></div><span className="history-retention">Most recent 100 runs</span></div>
              {tests.length ? <div className="history-table-wrap"><table className="history-table"><thead><tr><th>TEST</th><th>STATUS</th><th>REQUESTS</th><th>p95 LATENCY</th><th>THROUGHPUT</th><th>ERROR RATE</th><th /></tr></thead><tbody>{tests.map((item) => <tr key={item.id}><td><div className="test-title-cell"><b>{item.journey_name}</b><small>{item.target_name} · {new Date(item.created_at).toLocaleString()}</small></div></td><td><StatusPill status={item.status} /></td><td><b>{item.stats?.completed || 0}</b><small className="sub-cell"> / {item.stats?.requests_started || 0} started</small></td><td><b>{item.stats?.p95_ms || 0}</b><small className="sub-cell"> ms</small></td><td><b>{item.stats?.throughput_rps || 0}</b><small className="sub-cell"> req/s</small></td><td><b>{item.stats?.error_rate_percent || 0}</b><small className="sub-cell">%</small></td><td><button className="export-button" onClick={() => exportCsv(item.id)} title="Export CSV">↓ CSV</button></td></tr>)}</tbody></table></div> : <EmptyState mark="◷" title="No runs yet" copy="Start a test to create your first capacity report." action={<button className="button primary" onClick={() => setView('overview')}>Configure a test</button>} />}
            </section>
          </div>
        )}

        {view === 'safety' && (
          <div className="page-content">
            <section className="page-heading"><div><div className="eyebrow"><span className="eyebrow-line" />BUILT-IN BOUNDARIES</div><h1>Safety controls</h1><p>Traffic is deliberately constrained so a capacity test cannot silently become an open-ended scanner.</p></div></section>
            <div className="safety-grid">
              <section className="panel safety-overview-card"><div className="safety-hero-icon">◇</div><div className="section-kicker">GLOBAL RUNNER STATE</div><h2>{safety.kill_switch_active ? 'All tests are locked' : 'Runner is ready'}</h2><p>{safety.kill_switch_active ? 'The global stop is active and blocks new test starts.' : 'No external traffic starts until target ownership and per-run authorization both pass.'}</p><button className={`button ${safety.kill_switch_active ? 'primary' : 'danger'}`} onClick={toggleKillSwitch}>{safety.kill_switch_active ? 'Resume runner' : 'Engage emergency stop'}</button></section>
              <section className="panel safeguards-card"><div className="panel-heading"><div><div className="section-kicker">SERVER-ENFORCED</div><h2>Hard caps per test</h2></div><span className="guarded-label"><span className="mini-shield">◇</span> Not client-only</span></div><div className="cap-list"><div><span>Concurrent virtual users</span><b>{safety.caps.concurrency || 50}</b></div><div><span>Duration</span><b>{safety.caps.duration_seconds || 300}s max</b></div><div><span>Total requests</span><b>{(safety.caps.requests_per_test || 5000).toLocaleString()}</b></div><div><span>Request rate</span><b>{safety.caps.requests_per_second || 25} req/s</b></div><div><span>Response bytes</span><b>{((safety.caps.response_bytes_per_test || 20971520) / (1024 * 1024)).toFixed(0)} MB max</b></div></div></section>
              <section className="panel guard-detail-card"><div className="section-kicker">TARGET AUTHORIZATION</div><h2>One exact origin at a time.</h2><ul className="guard-list"><li><span>01</span><div><b>Staging-only setup</b><p>External targets can only be registered in a staging project; IP literals and production projects are rejected.</p></div></li><li><span>02</span><div><b>DNS ownership proof</b><p>Publish a unique TXT challenge bound to the normalized scheme, hostname, and port. Re-check it before each run.</p></div></li><li><span>03</span><div><b>Explicit permission per run</b><p>Each external run requires a fresh confirmation of written authorization for that exact origin.</p></div></li></ul></section>
              <section className="panel guard-detail-card"><div className="section-kicker">EGRESS AND JOURNEYS</div><h2>No pivot paths.</h2><ul className="guard-list"><li><span>04</span><div><b>DNS answers screened and pinned</b><p>Every A/AAAA answer is checked before a request. Any private, loopback, link-local, metadata, reserved, or special-use IP blocks the run.</p></div></li><li><span>05</span><div><b>Redirects stay in scope</b><p>Each hop must match the exact scheme, host, and port; its DNS answers are re-validated and pinned again.</p></div></li><li><span>06</span><div><b>Declarative steps only</b><p>GET, HEAD, and POST with relative paths; no scripts, credentials, cookies, arbitrary headers, or unbounded response capture.</p></div></li></ul></section>
              <section className="panel kill-card"><div className="kill-card-copy"><div className="section-kicker">IMMEDIATE STOP</div><h2>Global kill switch</h2><p>Cancels active async workers and closes their sockets, then blocks new runs until manually resumed.</p></div><button className={`button ${safety.kill_switch_active ? 'primary' : 'danger'}`} onClick={toggleKillSwitch}>{safety.kill_switch_active ? 'Resume runner' : 'Stop all tests'}</button></section>
            </div>
            <div className="local-mode-note"><span>i</span><p><b>Local single-operator build.</b> The service binds to loopback and has no multi-user authentication or production deployment hardening. Keep it on this computer; do not expose the API to a network.</p></div>
          </div>
        )}

        <footer className="app-footer"><span>CAPACITY LAB <b>·</b> LOCAL BUILD</span><span>Staging-first · {safety.caps.requests_per_second || 25} req/s hard cap</span></footer>
      </main>

      {showProjectModal && <div className="modal-scrim" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setShowProjectModal(false); }}><form className="modal-card" onSubmit={createProject}><div className="modal-head"><div><div className="section-kicker">WORKSPACE</div><h2>Create a project</h2></div><button type="button" className="icon-button" onClick={() => setShowProjectModal(false)}>×</button></div><p>Keep targets, reusable journeys, and test history grouped by environment.</p><label className="field"><span>Project name</span><input autoFocus value={projectName} onChange={(event) => setProjectName(event.target.value)} required minLength="2" maxLength="80" placeholder="Checkout staging" /></label><label className="field"><span>Environment</span><select value={projectEnvironment} onChange={(event) => setProjectEnvironment(event.target.value)}><option value="staging">Staging</option><option value="simulation">Simulation</option></select><small>Production targets are not supported.</small></label><div className="modal-actions"><button type="button" className="button subtle" onClick={() => setShowProjectModal(false)}>Cancel</button><button className="button primary" disabled={busy}>{busy ? 'Creating…' : 'Create project'}</button></div></form></div>}
    </div>
  );
}

export default App;
