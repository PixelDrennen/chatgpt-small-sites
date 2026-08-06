const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value).replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));

function actionClass(action = 'edit') {
  const normalized = action.toLowerCase();
  if (normalized.includes('delete') || normalized.includes('move/delete')) return 'delete';
  if (normalized.includes('add') || normalized.includes('move/add') || normalized.includes('branch')) return 'add';
  return 'modify';
}

function buildTree(files) {
  const root = { folders: {}, files: [] };
  files.forEach(file => {
    const parts = file.path.replace(/^\/\//, '').split('/'); let current = root;
    parts.slice(0, -1).forEach(part => current = current.folders[part] ||= { folders: {}, files: [] });
    current.files.push({ name: parts.at(-1), ...file });
  });
  const render = (node, level = 0) => {
    const folders = Object.entries(node.folders).sort(([a], [b]) => a.localeCompare(b)).map(([name, child]) => `<details class="tree-folder" ${level < 1 ? 'open' : ''}><summary>📁 ${escapeHtml(name)}</summary>${render(child, level + 1)}</details>`).join('');
    const files = node.files.sort((a,b) => a.name.localeCompare(b.name)).map(file => { const action = file.action || 'edit'; return `<div class="tree-file"><span class="action ${actionClass(action)}">${escapeHtml(action)}</span><span title="${escapeHtml(file.path)}">${escapeHtml(file.name)}</span></div>`; }).join('');
    return `<div class="tree-children">${folders}${files}</div>`;
  };
  return render(root);
}

function displayChanges(target, list, type) {
  target.innerHTML = '';
  if (!list.length) { target.innerHTML = `<p class="empty">No ${type} changes found.</p>`; return; }
  const template = $('#change');
  list.forEach(change => {
    const node = template.content.cloneNode(true); const fileCount = change.files.length;
    node.querySelector('.change-id').textContent = change.id === 'default' ? 'Default changelist' : `CL ${change.id}`;
    node.querySelector('.change-meta').textContent = change.user ? `${change.user}${change.client ? ` · ${change.client}` : ''}` : '';
    node.querySelector('.description').textContent = change.description || 'No description';
    node.querySelector('summary').textContent = `${fileCount.toLocaleString()} file${fileCount === 1 ? '' : 's'} · view by folder`;
    node.querySelector('.file-tree').innerHTML = buildTree(change.files);
    target.append(node);
  });
}

function notice(message, kind = 'secondary') {
  $('#notice').innerHTML = message ? `<div class="alert alert-${kind} py-2 px-3 small mb-0">${escapeHtml(message)}</div>` : '';
}

async function getSettings() {
  const response = await fetch('/api/settings'); const settings = await response.json();
  Object.entries(settings).forEach(([key, value]) => { const input = $(`[name="${key}"]`); if (input) input.value = value; });
}

async function refresh() {
  $('#refresh').disabled = true; notice('Checking Perforce…');
  try {
    const response = await fetch('/api/changes', {cache: 'no-store'}); const data = await response.json();
    if (data.error) { $('#connection-card').hidden = false; throw new Error(data.error); }
    $('#build-label').textContent = `Local dashboard · build ${data.build || 'unknown'}`;
    $('#connection-card').hidden = false; // keep settings available, but compact once connected
    $('#workspace').textContent = `${data.workspace.name} · ${data.workspace.stream || data.workspace.root || 'workspace connected'}`;
    $('#incoming-count').textContent = data.incoming.length; $('#outgoing-count').textContent = data.outgoing.length;
    displayChanges($('#incoming-list'), data.incoming, 'incoming'); displayChanges($('#outgoing-list'), data.outgoing, 'outgoing');
    notice(data.errors.length ? data.errors.join(' | ') : `Connected · updated ${new Date(data.checkedAt).toLocaleTimeString()}`, data.errors.length ? 'warning' : 'success');
  } catch (error) { notice(error.message, 'danger'); }
  finally { $('#refresh').disabled = false; }
}

$('#connection-form').addEventListener('submit', async event => {
  event.preventDefault(); const values = Object.fromEntries(new FormData(event.currentTarget));
  const response = await fetch('/api/settings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(values)});
  const data = await response.json(); if (data.error) return notice(data.error, 'danger'); refresh();
});
$('#refresh').addEventListener('click', refresh); getSettings().then(refresh); setInterval(refresh, 60000);
