// Indicator instances (key + params) and the popover that edits them.
// Instances are remembered per session in localStorage (a per-viewer convenience only),
// and only after the server accepted them (the app calls save()).

import { SERIES_COLORS } from './chart.js';
import { buildParamInputs, readParamInputs } from './params.js';

function storageKey(sessionId) { return `replay.indicators.${sessionId}`; }

export class IndicatorPanel {
  constructor(els, catalog, onChange) {
    this.els = els; // { popover, key, params, add, list, count, open, close, error }
    this.catalog = catalog; // { key: spec }
    this.onChange = onChange; // (instances, previousInstances) => void
    this.instances = [];
    this.sessionId = null;
    this.nextId = 1;

    for (const spec of Object.values(catalog)) {
      const opt = document.createElement('option');
      opt.value = spec.key;
      opt.textContent = spec.name;
      this.els.key.appendChild(opt);
    }
    this.els.key.addEventListener('change', () => this.renderParams());
    this.els.add.addEventListener('click', () => this.addFromForm());
    this.els.open.addEventListener('click', () => this.toggle());
    this.els.close.addEventListener('click', () => this.toggle(false));
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') this.toggle(false); });
    this.renderParams();
  }

  toggle(force) {
    const show = force === undefined ? this.els.popover.classList.contains('hidden') : force;
    this.els.popover.classList.toggle('hidden', !show);
    this.showError('');
  }

  showError(msg) {
    if (!this.els.error) return;
    this.els.error.textContent = msg;
    this.els.error.classList.toggle('hidden', !msg);
  }

  load(sessionId, defaults) {
    this.sessionId = sessionId;
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(storageKey(sessionId)) || 'null'); } catch { saved = null; }
    const list = Array.isArray(saved) ? saved : (defaults || []);
    this.setInstances(list.filter(x => x && this.catalog[x.key]).map(x => ({ key: x.key, params: { ...x.params } })));
    this.onChange(this.instances, this.instances);
  }

  // Replace the list without notifying (used to roll back a change the server rejected).
  setInstances(list) {
    this.instances = list.map(x => ({ id: x.id ?? this.nextId++, key: x.key, params: { ...x.params } }));
    this.renderList();
  }

  save() {
    if (!this.sessionId) return;
    try { localStorage.setItem(storageKey(this.sessionId), JSON.stringify(this.instances.map(({ key, params }) => ({ key, params })))); } catch { /* storage unavailable */ }
  }

  requestList() {
    return this.instances.map(({ key, params }) => ({ key, params }));
  }

  renderParams() {
    const spec = this.catalog[this.els.key.value];
    if (spec) buildParamInputs(this.els.params, spec.params);
  }

  addFromForm() {
    const key = this.els.key.value;
    const spec = this.catalog[key];
    if (!spec) return;
    const { params, error, message } = readParamInputs(this.els.params);
    if (error) { this.showError(message); error.focus(); return; }
    this.showError('');
    const previous = this.instances;
    this.setInstances([...this.instances, { key, params }]);
    this.onChange(this.instances, previous);
  }

  // Add an instance of `key` with `params`, or remove the first instance of `key` if one exists.
  // Returns true when the instance was added.
  toggleKey(key, params) {
    if (!this.catalog[key]) return false;
    const previous = this.instances;
    const existing = this.instances.find(x => x.key === key);
    this.setInstances(existing ? this.instances.filter(x => x.id !== existing.id) : [...this.instances, { key, params }]);
    this.onChange(this.instances, previous);
    return !existing;
  }

  remove(id) {
    const previous = this.instances;
    this.setInstances(this.instances.filter(x => x.id !== id));
    this.onChange(this.instances, previous);
  }

  renderList() {
    this.els.list.innerHTML = '';
    this.instances.forEach((inst, i) => {
      const spec = this.catalog[inst.key];
      const li = document.createElement('li');
      const sw = document.createElement('span');
      sw.className = 'sw';
      sw.style.background = SERIES_COLORS[Math.min(i, SERIES_COLORS.length - 1)];
      const name = document.createElement('span');
      name.className = 'name';
      const args = Object.values(inst.params);
      name.textContent = `${spec.name}${args.length ? ` (${args.join(', ')})` : ''}`;
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.title = 'Remove';
      btn.setAttribute('aria-label', `Remove ${spec.name}`);
      btn.innerHTML = '&times;';
      btn.addEventListener('click', () => this.remove(inst.id));
      li.append(sw, name, btn);
      this.els.list.appendChild(li);
    });
    this.els.count.textContent = String(this.instances.length);
  }
}
