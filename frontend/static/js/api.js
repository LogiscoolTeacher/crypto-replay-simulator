// Thin fetch wrapper for the backend API. Every error becomes an Error with the server's message.

async function request(method, path, body) {
  const init = { method, headers: {} };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const res = await fetch(path, init);
  if (res.status === 204) return null;
  let data = null;
  const text = await res.text();
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (!res.ok) {
    const detail = data && data.detail;
    const msg = typeof detail === 'string' ? detail
      : Array.isArray(detail) ? detail.map(d => `${(d.loc || []).join('.')}: ${d.msg}`).join('; ')
      : `${res.status} ${res.statusText}`;
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  return data;
}

export const api = {
  coins: () => request('GET', '/api/coins'),
  intervals: () => request('GET', '/api/intervals'),
  indicators: () => request('GET', '/api/indicators'),
  strategies: () => request('GET', '/api/strategies'),
  sessions: () => request('GET', '/api/sessions'),
  createSession: (body) => request('POST', '/api/sessions', body),
  session: (id) => request('GET', `/api/sessions/${id}`),
  prepare: (id) => request('POST', `/api/sessions/${id}/prepare`),
  deleteSession: (id) => request('DELETE', `/api/sessions/${id}`),
  step: (id, body) => request('POST', `/api/sessions/${id}/step`, body),
  reset: (id) => request('POST', `/api/sessions/${id}/reset`),
  snapshots: (id) => request('GET', `/api/sessions/${id}/snapshots`),
  snapshotImage: (id, bucketId, side, body) => request('POST', `/api/sessions/${id}/snapshots/${bucketId}/${side}/image`, body),
  snapshotsCsvUrl: (id) => `/api/sessions/${id}/snapshots.csv`,
  snapshotImageUrl: (id, snapId) => `/api/sessions/${id}/snapshots/${snapId}/image`,
  fibs: (id) => request('GET', `/api/sessions/${id}/fibs`),
  addFib: (id, body) => request('POST', `/api/sessions/${id}/fibs`, body),
  deleteFib: (id, fibId) => request('DELETE', `/api/sessions/${id}/fibs/${fibId}`),
  buy: (id, amount, interval, whole = false, coins = null, sr_tol = null, reasons = [], note = null, tp_pct = null, sl_pct = null) => request('POST', `/api/sessions/${id}/buy`, { amount, interval, whole, coins, sr_tol, reasons, note, tp_pct, sl_pct }),
  arm: (id, bucketId, armed) => request('POST', `/api/sessions/${id}/buckets/${bucketId}/arm`, { armed }),
  sell: (id, bucket_id, interval, sr_tol = null, part = 1, whole = false, reasons = [], note = null) => request('POST', `/api/sessions/${id}/sell`, { bucket_id, interval, sr_tol, part, whole, reasons, note }),
  reasons: () => request('GET', '/api/reasons'),
  performance: (id) => request('GET', `/api/sessions/${id}/performance`),
  performanceAll: () => request('GET', '/api/stats/performance'),
  reasonStats: (symbol = '') => request('GET', `/api/stats/reasons${symbol ? `?symbol=${encodeURIComponent(symbol)}` : ''}`),
  trades: (id) => request('GET', `/api/sessions/${id}/trades`),
  view: (id, body) => request('POST', `/api/sessions/${id}/view`, body),
  context: (id, body) => request('POST', `/api/sessions/${id}/context`, body),
  // Day-trading mode (sessions with mode "day")
  createDaySession: (body) => request('POST', '/api/day/sessions', body),
  daySession: (id) => request('GET', `/api/day/sessions/${id}`),
  dayPrepare: (id) => request('POST', `/api/day/sessions/${id}/prepare`),
  dayZones: (id) => request('GET', `/api/day/sessions/${id}/zones`),
  dayAddZone: (id, body) => request('POST', `/api/day/sessions/${id}/zones`, body),
  dayUpdateZone: (id, zoneId, body) => request('PUT', `/api/day/sessions/${id}/zones/${zoneId}`, body),
  dayDeleteZone: (id, zoneId) => request('DELETE', `/api/day/sessions/${id}/zones/${zoneId}`),
  dayPlan: (id, body) => request('POST', `/api/day/sessions/${id}/plan`, body),
  dayBuy: (id, body) => request('POST', `/api/day/sessions/${id}/buy`, body),
  daySell: (id, body) => request('POST', `/api/day/sessions/${id}/sell`, body),
  dayMoveTrade: (id, body) => request('PATCH', `/api/day/sessions/${id}/trade`, body),
  dayStep: (id, body) => request('POST', `/api/day/sessions/${id}/step`, body),
  dayStepBack: (id, body) => request('POST', `/api/day/sessions/${id}/step-back`, body),
  dayEndDay: (id) => request('POST', `/api/day/sessions/${id}/end-day`),
  dayKeepTrading: (id) => request('POST', `/api/day/sessions/${id}/keep-trading`),
  daySkip: (id) => request('POST', `/api/day/sessions/${id}/skip`),
  dayNextEma: (id) => request('POST', `/api/day/sessions/${id}/next-ema-day`),
  dayStrategies: () => request('GET', '/api/day/strategies'),
  dayNextSignal: (id, body) => request('POST', `/api/day/sessions/${id}/next-signal`, body),
  dayGoto: (id, date) => request('POST', `/api/day/sessions/${id}/goto`, { date }),
  dayReset: (id) => request('POST', `/api/day/sessions/${id}/reset`),
  dayTrades: (id) => request('GET', `/api/day/sessions/${id}/trades`),
  dayAutoTrades: (id) => request('GET', `/api/day/sessions/${id}/auto-trades`),
  dayReview: (id, tradeId, body) => request('POST', `/api/day/sessions/${id}/trades/${tradeId}/review`, body),
  dayTradeImage: (id, tradeId, image) => request('POST', `/api/day/sessions/${id}/trades/${tradeId}/image`, { image }),
  dayTradeImageUrl: (id, tradeId) => `/api/day/sessions/${id}/trades/${tradeId}/image`,
  dayDays: (id) => request('GET', `/api/day/sessions/${id}/days`),
  dayDaysCsvUrl: (id) => `/api/day/sessions/${id}/days.csv`,
  dayConditions: (id, scope) => request('GET', `/api/day/sessions/${id}/conditions?scope=${encodeURIComponent(scope)}`),
  dayConditionsCsvUrl: (id, scope) => `/api/day/sessions/${id}/conditions.csv?scope=${encodeURIComponent(scope)}`,
  dayNotes: (id, date, notes) => request('PUT', `/api/day/sessions/${id}/days/${date}/notes`, { notes }),
  dayImage: (id, date, image) => request('POST', `/api/day/sessions/${id}/days/${date}/image`, { image }),
  dayImageUrl: (id, date) => `/api/day/sessions/${id}/days/${date}/image`,
};
