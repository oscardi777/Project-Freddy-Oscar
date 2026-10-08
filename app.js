const state = {
  scripts: [],
  selectedScript: '',
  parameters: [],
  datasets: [],
  outputs: [],
  lastResult: null,
};

const scriptsListEl = document.getElementById('scriptsList');
const parametersPanelEl = document.getElementById('parametersPanel');
const dataInputSelectEl = document.getElementById('dataInputSelect');
const datasetPreviewEl = document.getElementById('datasetPreview');
const outputFilesListEl = document.getElementById('outputFilesList');
const resultsContainerEl = document.getElementById('resultsContainer');
const logsContainerEl = document.getElementById('logsContainer');

function logMessage(msg) {
  const stamp = new Date().toLocaleTimeString();
  logsContainerEl.textContent += `[${stamp}] ${msg}\n`;
  logsContainerEl.scrollTop = logsContainerEl.scrollHeight;
}

function clearLogs() {
  logsContainerEl.textContent = 'Logs reiniciados.\n';
}

function setResultBox(content, isJson = false) {
  resultsContainerEl.classList.remove('empty');
  if (isJson) {
    resultsContainerEl.textContent = JSON.stringify(content, null, 2);
  } else {
    resultsContainerEl.textContent = content;
  }
}

async function apiFetch(url, options = {}) {
  const response = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });

  const text = await response.text();
  if (!text) return null;

  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

async function refreshScripts() {
  const scripts = await apiFetch('/api/scripts');
  state.scripts = Array.isArray(scripts) ? scripts : [];

  if (!state.selectedScript && state.scripts.length) {
    state.selectedScript = state.scripts[0];
  }

  scriptsListEl.innerHTML = '';
  state.scripts.forEach((script) => {
    const button = document.createElement('button');
    button.className = `script-item ${script === state.selectedScript ? 'active' : ''}`;
    button.textContent = script;
    button.addEventListener('click', async () => {
      state.selectedScript = script;
      await loadScriptParameters();
      refreshScripts();
    });
    scriptsListEl.appendChild(button);
  });
}

async function refreshDatasets() {
  const payload = await apiFetch('/api/files?folder=data-input');
  const files = payload?.files || [];
  state.datasets = files.filter((file) => file.name !== '.gitkeep');

  dataInputSelectEl.innerHTML = '';
  if (!state.datasets.length) {
    const option = new Option('Sin datasets disponibles', '');
    dataInputSelectEl.appendChild(option);
    datasetPreviewEl.textContent = 'No hay archivos en data-input.';
    return;
  }

  state.datasets.forEach((file) => {
    dataInputSelectEl.appendChild(new Option(file.name, file.path));
  });

  const selected = state.datasets[0];
  dataInputSelectEl.value = selected.path;
  datasetPreviewEl.textContent = `Dataset activo: ${selected.name}`;
}

async function refreshOutputs() {
  const payload = await apiFetch('/api/files?folder=data-output');
  const files = payload?.files || [];
  state.outputs = files.filter((file) => file.name !== '.gitkeep');

  outputFilesListEl.innerHTML = '';
  if (!state.outputs.length) {
    outputFilesListEl.innerHTML = '<p class="empty-state">No hay archivos en data-output.</p>';
    return;
  }

  state.outputs.forEach((file) => {
    const item = document.createElement('div');
    item.className = 'file-item';
    item.innerHTML = `
      <a href="/files/${encodeURIComponent(file.path)}" target="_blank" rel="noreferrer">${file.name}</a>
      <span>${file.type.toUpperCase()}</span>
    `;
    outputFilesListEl.appendChild(item);
  });
}

async function loadScriptParameters() {
  if (!state.selectedScript) return;

  const payload = await apiFetch(`/api/params?script=${encodeURIComponent(state.selectedScript)}`);
  state.parameters = payload?.params || [];

  parametersPanelEl.innerHTML = '';

  if (!state.parameters.length) {
    parametersPanelEl.innerHTML = '<p class="empty-state">Este script no expone parámetros por metadata. Puedes ejecutarlo con un dataset.</p>';
    return;
  }

  state.parameters.forEach((param) => {
    const group = document.createElement('div');
    group.className = 'param-group';

    const label = document.createElement('label');
    label.textContent = param.name;
    label.htmlFor = `param-${param.name}`;

    let input;
    if (param.options && param.options.length) {
      input = document.createElement('select');
      param.options.forEach((option) => {
        const opt = document.createElement('option');
        opt.value = option;
        opt.textContent = option;
        input.appendChild(opt);
      });
    } else if (param.type === 'number' || param.type === 'float' || param.type === 'int') {
      input = document.createElement('input');
      input.type = 'number';
      if (param.min !== undefined) input.min = param.min;
      if (param.max !== undefined) input.max = param.max;
    } else if (param.type === 'boolean') {
      input = document.createElement('select');
      ['true', 'false'].forEach((value) => {
        const option = document.createElement('option');
        option.value = value;
        option.textContent = value;
        input.appendChild(option);
      });
    } else {
      input = document.createElement('input');
      input.type = 'text';
    }

    input.id = `param-${param.name}`;
    input.name = param.name;
    input.dataset.paramName = param.name;
    input.value = param.default ?? '';

    group.appendChild(label);
    group.appendChild(input);
    parametersPanelEl.appendChild(group);
  });
}

function collectParams() {
  const result = {};
  document.querySelectorAll('[data-param-name]').forEach((input) => {
    const name = input.dataset.paramName;
    const value = input.value;

    if (input.type === 'number') {
      result[name] = Number(value);
      return;
    }

    if (input.tagName === 'SELECT' && value === 'true') {
      result[name] = true;
      return;
    }

    if (input.tagName === 'SELECT' && value === 'false') {
      result[name] = false;
      return;
    }

    result[name] = value;
  });
  return result;
}

async function uploadDataset() {
  const fileInput = document.getElementById('dataInputFile');
  const file = fileInput.files[0];
  if (!file) {
    logMessage('No se seleccionó un archivo para subir.');
    return;
  }

  const formData = new FormData();
  formData.append('file', file);

  const response = await fetch('/api/upload-dataset', { method: 'POST', body: formData });
  const data = await response.json();

  if (data.ok) {
    logMessage(`Dataset cargado correctamente: ${data.file}`);
    fileInput.value = '';
    await refreshDatasets();
  } else {
    logMessage(`Error al cargar dataset: ${data.error || 'desconocido'}`);
  }
}

async function executeSelectedScript() {
  if (!state.selectedScript) {
    logMessage('Primero debes seleccionar un script.');
    return;
  }

  const selectedDataset = dataInputSelectEl.value || '';
  const payload = await apiFetch('/api/run', {
    method: 'POST',
    body: JSON.stringify({
      script: state.selectedScript,
      params: collectParams(),
      dataset: selectedDataset,
    }),
  });

  if (!payload || payload.error) {
    logMessage(`Error al ejecutar: ${payload?.error || 'unkown error'}`);
    return;
  }

  state.lastResult = payload;
  logMessage(`Ejecución finalizada: ${state.selectedScript}`);

  if (payload.stdout) {
    setResultBox(payload.stdout, false);
  } else if (payload.json) {
    setResultBox(payload.json, true);
  } else {
    setResultBox('La ejecución no devolvió salida visible.');
  }

  if (payload.stderr) {
    logMessage(`stderr: ${payload.stderr}`);
  }

  await refreshOutputs();
}

async function exportResults() {
  if (!state.lastResult) {
    logMessage('No hay un resultado activo para exportar.');
    return;
  }

  const blob = new Blob([JSON.stringify(state.lastResult, null, 2)], { type: 'application/json' });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = 'resultado-ejecucion.json';
  link.click();
  URL.revokeObjectURL(link.href);
}

document.getElementById('loadDataBtn').addEventListener('click', uploadDataset);
document.getElementById('executeBtn').addEventListener('click', executeSelectedScript);
document.getElementById('exportResultsBtn').addEventListener('click', exportResults);
document.getElementById('clearLogsBtn').addEventListener('click', clearLogs);
document.getElementById('refreshBtn').addEventListener('click', async () => {
  clearLogs();
  await refreshScripts();
  await loadScriptParameters();
  await refreshDatasets();
  await refreshOutputs();
});

dataInputSelectEl.addEventListener('change', () => {
  const selected = state.datasets.find((file) => file.path === dataInputSelectEl.value);
  datasetPreviewEl.textContent = selected ? `Dataset activo: ${selected.name}` : 'Sin dataset seleccionado.';
});

async function initialize() {
  clearLogs();
  await refreshScripts();
  await loadScriptParameters();
  await refreshDatasets();
  await refreshOutputs();
  logMessage('Sistema listo.');
}

initialize();
