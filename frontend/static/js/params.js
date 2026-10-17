// Build and read parameter inputs from an indicator or strategy parameter schema.

export function buildParamInputs(container, params, values = {}) {
  container.innerHTML = '';
  for (const p of params) {
    const label = document.createElement('label');
    label.textContent = p.label;
    let input;
    const current = values[p.name] !== undefined ? values[p.name] : p.default;
    if (p.type === 'choice') {
      input = document.createElement('select');
      for (const c of p.choices) {
        const o = document.createElement('option');
        o.value = c; o.textContent = c; o.selected = c === current;
        input.appendChild(o);
      }
    } else {
      input = document.createElement('input');
      input.type = 'number';
      input.value = current;
      if (p.min !== null && p.min !== undefined) input.min = p.min;
      if (p.max !== null && p.max !== undefined) input.max = p.max;
      input.step = p.step ?? (p.type === 'int' ? 1 : 0.1);
      input.required = true;
    }
    input.name = p.name;
    input.dataset.type = p.type;
    input.dataset.label = p.label;
    label.appendChild(input);
    container.appendChild(label);
  }
}

// Returns { params } or { error: inputElement, message } when a value is missing, not finite,
// not an integer where one is required, or outside the schema's min/max.
export function readParamInputs(container) {
  const params = {};
  for (const input of container.querySelectorAll('[name]')) {
    const t = input.dataset.type;
    const label = input.dataset.label || input.name;
    if (t === 'choice') { params[input.name] = input.value; continue; }
    const v = t === 'int' ? Number(input.value) : parseFloat(input.value);
    if (input.value.trim() === '' || !Number.isFinite(v)) return { error: input, message: `${label} must be a number` };
    if (t === 'int' && !Number.isInteger(v)) return { error: input, message: `${label} must be a whole number` };
    if (input.min !== '' && v < Number(input.min)) return { error: input, message: `${label} must be at least ${input.min}` };
    if (input.max !== '' && v > Number(input.max)) return { error: input, message: `${label} must be at most ${input.max}` };
    params[input.name] = v;
  }
  return { params };
}
