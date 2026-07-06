const express = require('express');
const cors = require('cors');
const path = require('path');

const app = express();
app.use(cors());
app.use(express.json());

const PORT = process.env.PORT || 5001;

const demoScans = [];

app.get('/api/history', (req, res) => {
  res.json(demoScans.slice(0, 20));
});

app.post('/api/scan', (req, res) => {
  const scan = {
    id: Date.now(),
    ...req.body,
    scanned_at: req.body.scanned_at || new Date().toISOString(),
    anomaly_score: req.body.anomaly_score ?? 0.0,
    is_anomaly: Boolean(req.body.is_anomaly)
  };
  demoScans.unshift(scan);
  if (demoScans.length > 50) demoScans.length = 50;
  res.json({ ok: true, scan });
});

app.get('/events', (req, res) => {
  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Connection', 'keep-alive');
  res.flushHeaders();

  const sendInitial = () => {
    res.write(`data: ${JSON.stringify({ type: 'connected' })}\n\n`);
  };
  sendInitial();

  const interval = setInterval(() => {
    if (demoScans.length) {
      res.write(`data: ${JSON.stringify(demoScans[0])}\n\n`);
    }
  }, 2000);

  req.on('close', () => clearInterval(interval));
});

if (process.env.NODE_ENV === 'production') {
  app.use(express.static(path.join(__dirname, '../client/dist')));
  app.get('*', (req, res) => {
    res.sendFile(path.join(__dirname, '../client/dist/index.html'));
  });
}

app.listen(PORT, () => {
  console.log(`Dashboard server running on http://localhost:${PORT}`);
});
