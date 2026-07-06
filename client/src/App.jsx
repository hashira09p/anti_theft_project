import { useEffect, useMemo, useState } from 'react';

const initialStats = {
  totalScans: 0,
  authorized: 0,
  denied: 0,
  anomalies: 0
};

function App() {
  const [scans, setScans] = useState([]);
  const [stats, setStats] = useState(initialStats);
  const [status, setStatus] = useState('Connecting to live feed...');

  useEffect(() => {
    const loadHistory = async () => {
      try {
        const res = await fetch('/api/history');
        const data = await res.json();
        setScans(data.slice(0, 15));
        setStats({
          totalScans: data.length,
          authorized: data.filter(item => item.authorized).length,
          denied: data.filter(item => !item.authorized).length,
          anomalies: data.filter(item => item.is_anomaly).length
        });
        setStatus('Live feed ready');
      } catch (error) {
        setStatus('Using demo data for preview');
        setScans([
          {
            id: 1,
            card_uid: 'AA BB CC DD',
            authorized: true,
            location: 'front_door',
            scanned_at: new Date().toISOString(),
            anomaly_score: 0.12,
            is_anomaly: false
          }
        ]);
      }
    };

    loadHistory();

    const eventSource = new EventSource('/events');
    eventSource.onmessage = (event) => {
      const newScan = JSON.parse(event.data);
      setScans(prev => [newScan, ...prev].slice(0, 15));
      setStats(prev => ({
        totalScans: prev.totalScans + 1,
        authorized: prev.authorized + (newScan.authorized ? 1 : 0),
        denied: prev.denied + (!newScan.authorized ? 1 : 0),
        anomalies: prev.anomalies + (newScan.is_anomaly ? 1 : 0)
      }));
      setStatus(`Live scan received from ${newScan.location || 'device'}`);
    };

    eventSource.onerror = () => {
      setStatus('Live feed disconnected');
    };

    return () => eventSource.close();
  }, []);

  const latestScan = scans[0];
  const summary = useMemo(() => ({
    accessRate: stats.totalScans ? Math.round((stats.authorized / stats.totalScans) * 100) : 0,
    anomalyRate: stats.totalScans ? Math.round((stats.anomalies / stats.totalScans) * 100) : 0
  }), [stats]);

  return (
    <div className="app-shell">
      <header className="hero">
        <div>
          <p className="eyebrow">RFID Smart Home Security</p>
          <h1>Live access dashboard</h1>
          <p className="subtitle">Monitor scans, authorization status, and anomaly alerts in real time.</p>
        </div>
        <div className="status-pill">{status}</div>
      </header>

      <section className="stats-grid">
        <article className="card stat-card">
          <span>Total scans</span>
          <strong>{stats.totalScans}</strong>
        </article>
        <article className="card stat-card">
          <span>Authorized</span>
          <strong>{stats.authorized}</strong>
        </article>
        <article className="card stat-card">
          <span>Denied</span>
          <strong>{stats.denied}</strong>
        </article>
        <article className="card stat-card">
          <span>Anomalies</span>
          <strong>{stats.anomalies}</strong>
        </article>
      </section>

      <section className="content-grid">
        <article className="card large-card">
          <div className="card-header">
            <h2>Latest scan</h2>
            <span className={latestScan?.authorized ? 'tag success' : 'tag danger'}>
              {latestScan?.authorized ? 'Authorized' : 'Denied'}
            </span>
          </div>
          {latestScan ? (
            <>
              <div className="metric-row">
                <div>
                  <p className="metric-label">Card UID</p>
                  <p className="metric-value">{latestScan.card_uid}</p>
                </div>
                <div>
                  <p className="metric-label">Location</p>
                  <p className="metric-value">{latestScan.location}</p>
                </div>
              </div>
              <div className="metric-row">
                <div>
                  <p className="metric-label">Time</p>
                  <p className="metric-value">{new Date(latestScan.scanned_at).toLocaleString()}</p>
                </div>
                <div>
                  <p className="metric-label">Anomaly score</p>
                  <p className="metric-value">{latestScan.anomaly_score?.toFixed(2) ?? '0.00'}</p>
                </div>
              </div>
              <div className="summary-box">
                <p>Access rate: {summary.accessRate}%</p>
                <p>Anomaly rate: {summary.anomalyRate}%</p>
              </div>
            </>
          ) : (
            <p className="empty-state">No scan activity yet.</p>
          )}
        </article>

        <article className="card">
          <div className="card-header">
            <h2>Recent events</h2>
            <span className="tag muted">Live</span>
          </div>
          <ul className="events-list">
            {scans.map(scan => (
              <li key={scan.id || scan.scanned_at} className="event-item">
                <div>
                  <strong>{scan.card_uid}</strong>
                  <p>{scan.authorized ? 'Access granted' : 'Access denied'}</p>
                </div>
                <span className={scan.is_anomaly ? 'tag danger' : 'tag success'}>
                  {scan.is_anomaly ? 'Alert' : 'OK'}
                </span>
              </li>
            ))}
          </ul>
        </article>
      </section>
    </div>
  );
}

export default App;
