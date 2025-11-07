const $ = (selector, scope = document) => scope.querySelector(selector);
const $all = (selector, scope = document) => Array.from(scope.querySelectorAll(selector));

const config = window.MILSTONE_CONFIG || { apiBase: window.location.origin };
const DEFAULT_EXPECTED_HOURS = 1;
const params = new URLSearchParams(window.location.search);
const state = {
  projects: [],
  currentProject: params.get('project'),
  milestones: [],
  snapshots: [],
  progress: null,
  expanded: new Set(),
  statusFilter: 'all',
};

const sidebarEl = document.getElementById('sidebar');
const mainEl = document.getElementById('main');
const modalsEl = document.getElementById('modals');
const toastEl = document.getElementById('toast');
const STATUS_VALUES = ['active', 'blocked', 'on_hold', 'done'];
const STATUS_COLOR_CLASS = {
  active: 'status-green',
  done: 'status-green',
  blocked: 'status-red',
  on_hold: 'status-yellow',
  deleted: 'status-gray',
};

const canonicalStatusValue = (value) => {
  if (!value) return 'active';
  const lower = value.toLowerCase();
  return lower === 'planned' ? 'active' : lower;
};

const formatStatusLabel = (value) => {
  if (!value) return '';
  const label = value.replace(/_/g, ' ');
  return label.charAt(0).toUpperCase() + label.slice(1);
};

const STATUS_FILTERS = [
  { value: 'all', label: 'All statuses' },
  ...STATUS_VALUES.map((value) => ({ value, label: formatStatusLabel(value) })),
  { value: 'deleted', label: 'Deleted' },
];

const escapeHtml = (value = '') =>
  String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');

const normalizeDateInput = (value) => {
  if (!value) return '';
  if (value.includes('T')) return value.split('T')[0];
  if (value.includes(' ')) return value.split(' ')[0];
  return value;
};

const formatHours = (val) => {
  const num = Number(val) || 0;
  return Number.isInteger(num) ? num.toFixed(0) : num.toFixed(2);
};

const formatDateTime = (value) => {
  if (!value) return '';
  return new Date(value).toLocaleString();
};

const findMilestoneBySlug = (slug, nodes = state.milestones) => {
  for (const node of nodes) {
    if (node.slug === slug) return node;
    if (node.children?.length) {
      const found = findMilestoneBySlug(slug, node.children);
      if (found) return found;
    }
  }
  return null;
};

const buildNodeIndex = (nodes, map = new Map()) => {
  nodes.forEach((node) => {
    map.set(node.id, node);
    if (node.children?.length) {
      buildNodeIndex(node.children, map);
    }
  });
  return map;
};

const flattenMilestones = (nodes, excludeSlug = null, depth = 0, acc = []) => {
  nodes.forEach((node) => {
    if (node.slug === excludeSlug) {
      return;
    }
    if (!node.deleted) {
      acc.push({ slug: node.slug, title: node.title, depth });
    }
    if (node.children?.length) {
      flattenMilestones(node.children, excludeSlug, depth + 1, acc);
    }
  });
  return acc;
};

const renderStatusSelect = (name, selected = '', includeEmpty = false) => {
  const options = [];
  if (includeEmpty) {
    options.push('<option value="">(leave unchanged)</option>');
  }
  STATUS_VALUES.forEach((value) => {
    const label = formatStatusLabel(value);
    const selectedAttr = selected === value ? 'selected' : '';
    options.push(`<option value="${value}" ${selectedAttr}>${label}</option>`);
  });
  return `<select name="${name}">${options.join('')}</select>`;
};

const renderParentSelect = (name, excludeSlug = null, selectedSlug = '') => {
  const choices = flattenMilestones(state.milestones, excludeSlug);
  const options = ['<option value="">None (no parent)</option>'];
  choices.forEach((choice) => {
    const indent = '&nbsp;&nbsp;'.repeat(choice.depth);
    const selected = choice.slug === selectedSlug ? 'selected' : '';
    options.push(`<option value="${choice.slug}" ${selected}>${indent}${choice.title} (${choice.slug})</option>`);
  });
  return `<select name="${name}">${options.join('')}</select>`;
};

const parentSlugFor = (slug) => {
  const node = findMilestoneBySlug(slug);
  if (!node || !node.parentId) return '';
  const index = buildNodeIndex(state.milestones, new Map());
  const parent = index.get(node.parentId);
  return parent?.slug || '';
};

const findLog = (slug, logId) => {
  const node = findMilestoneBySlug(slug);
  if (!node) return null;
  return (node.logs || []).find((log) => log.id === logId) || null;
};

const templates = {
  projectButton: (project) => {
    const lastOpened = project.lastOpened ? new Date(project.lastOpened).toLocaleString() : '';
    return `
      <button class="project-btn ${state.currentProject === project.key ? 'active' : ''}" data-project="${project.key}">
        <div class="project-btn__header">
          <div>
            <strong>${project.name || project.key}</strong>
            <small>${project.key}</small>
          </div>
          ${project.path ? `<span class="project-path">${project.path}</span>` : ''}
        </div>
        ${lastOpened ? `<div class="project-last">Last opened ${lastOpened}</div>` : ''}
      </button>
    `;
  },
};

const showToast = (message) => {
  toastEl.textContent = message;
  toastEl.classList.remove('hidden');
  setTimeout(() => toastEl.classList.add('hidden'), 2800);
};

const showConfirm = ({ title, message, confirmLabel = 'Confirm', cancelLabel = 'Cancel' }) =>
  new Promise((resolve) => {
    const modalContent = `
      <button class="modal-close" data-action="close-modal">×</button>
      <h3>${title}</h3>
      <p class="confirm-message">${message}</p>
      <div class="form-actions">
        <button type="button" class="button secondary" data-action="cancel-confirm">${cancelLabel}</button>
        <button type="button" class="button danger" data-action="confirm-modal">${confirmLabel}</button>
      </div>
    `;
    openModal(modalContent);
    let handled = false;
    const cleanup = (result) => {
      if (handled) return;
      handled = true;
      modalsEl.removeEventListener('click', handler);
      closeModal();
      resolve(result);
    };
    const handler = (event) => {
      if (event.target.dataset.action === 'confirm-modal') {
        cleanup(true);
        return;
      }
      if (
        event.target.dataset.action === 'cancel-confirm' ||
        event.target.dataset.action === 'close-modal' ||
        event.target.classList.contains('modal-backdrop')
      ) {
        cleanup(false);
      }
    };
    modalsEl.addEventListener('click', handler);
  });

const fetchJSON = async (path, options = {}) => {
  const url = `${config.apiBase}${path}`;
  const res = await fetch(url, options);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || 'Request failed');
  }
  if (res.headers.get('Content-Type')?.includes('application/json')) {
    return res.json();
  }
  return res.text();
};

const loadProjects = async () => {
  const data = await fetchJSON('/api/projects');
  state.projects = data.projects || [];
  if (!state.currentProject || !state.projects.find((p) => p.key === state.currentProject)) {
    state.currentProject = data.current_project || state.projects[0]?.key || null;
  }
  renderSidebar();
};

const loadSnapshots = async () => {
  if (!state.currentProject) {
    state.snapshots = [];
    return state.snapshots;
  }
  const data = await fetchJSON(`/api/progress/history?project=${encodeURIComponent(state.currentProject)}`);
  state.snapshots = data || [];
  return state.snapshots;
};

const loadMilestones = async () => {
  if (!state.currentProject) {
    state.milestones = [];
    state.progress = null;
    return;
  }
  const params = new URLSearchParams({ project: state.currentProject });
  if (state.statusFilter === 'deleted') params.append('include_deleted', 'true');
  const data = await fetchJSON(`/api/milestones?${params.toString()}`);
  state.milestones = data.milestones || [];
  annotateTotals(state.milestones);
  pruneExpanded();
  state.progress = data.progress || null;
};

const annotateTotals = (nodes) => {
  const calc = (node) => {
    const children = node.children || [];
    const childSum = children.reduce((sum, child) => sum + calc(child), 0);
    const own = Number(node.expectedHours) || 0;
    node.totalHours = own + childSum;
    return node.totalHours;
  };
  nodes.forEach((node) => calc(node));
};

const pruneExpanded = () => {
  const present = new Set();
  const track = (node) => {
    present.add(node.slug);
    node.children?.forEach(track);
  };
  state.milestones.forEach(track);
  for (const slug of Array.from(state.expanded)) {
    if (!present.has(slug)) {
      state.expanded.delete(slug);
    }
  }
};

const getStatusKey = (node) => {
  if (node.deleted) return 'deleted';
  return canonicalStatusValue(node.status);
};

const filterMilestones = (nodes, statusFilter) => {
  const cloneAll = (list) => list.map((node) => ({ ...node, children: cloneAll(node.children || []) }));
  const recurse = (list) =>
    list.reduce((acc, node) => {
      const filteredChildren = recurse(node.children || []);
      const matches = getStatusKey(node) === statusFilter;
      if (matches || filteredChildren.length) {
        acc.push({ ...node, children: filteredChildren });
      }
      return acc;
    }, []);
  return statusFilter === 'all' ? cloneAll(nodes) : recurse(nodes);
};

const renderSidebar = () => {
  const listHtml = state.projects.length
    ? state.projects.map((p) => templates.projectButton(p)).join('')
    : '<p class="empty-state">No projects registered yet. Run <code>milstone project ui</code> inside a project to add one.</p>';
  sidebarEl.innerHTML = `
    <h2>Recently opened</h2>
    <div class="project-list">${listHtml}</div>
    <button class="button secondary" data-action="refresh-projects">Refresh</button>
  `;
};

const renderMain = () => {
  const project = state.projects.find((p) => p.key === state.currentProject);
  if (!project) {
    mainEl.innerHTML = `
      <section class="empty-panel">
        <h2>Select a project</h2>
        <p>Use the sidebar to pick a project that has been registered via <code>milstone project ui</code>.</p>
      </section>
    `;
    return;
  }
  const percent = Math.round((state.progress?.stats?.ratio || 0) * 100);
  const filtered = filterMilestones(state.milestones, state.statusFilter);
  const tree = renderTree(filtered);

  mainEl.innerHTML = `
    <section class="project-overview">
      <div class="project-info">
        <h1>${project?.name || 'Project'}</h1>
        <p>${project?.description || 'Use the buttons on the right to manage milestones.'}</p>
      </div>
      <div class="project-actions">
        <button class="button" data-action="open-create">+ Create Milestone</button>
        <button class="button danger" data-action="reset-project">Reset Project</button>
      </div>
    </section>
    ${renderProgressSection(percent)}
    ${renderMilestoneSection(tree)}
  `;
};

function renderProgressSection(percent) {
  const stats = state.progress?.stats || {};
  const totalHours = stats.totalHours || 0;
  const completedHours = stats.completedHours || 0;
  const remainingHours = Math.max(totalHours - completedHours, 0);
  const totalCount = stats.totalCount || 0;
  const completedCount = stats.completedCount || 0;
  const sinceLabel = state.progress?.since ? new Date(state.progress.since).toLocaleString() : 'Project start';
  return `
    <section class="progressCard">
      <div class="progressHeader">
        <div>
          <div class="progressLabel">Tracking Since</div>
          <div>${sinceLabel}</div>
        </div>
        <div class="progressStats">
          <strong>${percent}%</strong>
          <span>${completedHours.toFixed(2)}h / ${totalHours.toFixed(2)}h</span>
        </div>
      </div>
      <div class="progressBar"><span style="width:${percent}%;"></span></div>
      <div class="progress-metrics">
        <div class="metric">
          <span class="metric-label">Completed Hours</span>
          <strong>${completedHours.toFixed(2)}h</strong>
        </div>
        <div class="metric">
          <span class="metric-label">Remaining Hours</span>
          <strong>${remainingHours.toFixed(2)}h</strong>
        </div>
        <div class="metric">
          <span class="metric-label">Milestones</span>
          <strong>${completedCount}/${totalCount}</strong>
        </div>
      </div>
      <div class="progress-actions">
        <button class="button secondary" data-action="view-history">View history</button>
        <button class="button secondary" data-action="open-reset">Reset progress</button>
      </div>
    </section>
  `;
}

function renderMilestoneSection(tree) {
  const options = STATUS_FILTERS.map(
    (opt) => `<option value="${opt.value}" ${state.statusFilter === opt.value ? 'selected' : ''}>${opt.label}</option>`
  ).join('');
  return `
    <section class="milestone-section">
      <div class="section-header">
        <div>
          <h3>Milestones</h3>
          <p>Filter milestones by status to focus on what matters.</p>
        </div>
        <label class="status-filter">
          <span>Status</span>
          <select id="status-filter">${options}</select>
        </label>
      </div>
      ${tree || '<div class="node node-empty">Use <strong>+ Create Milestone</strong> to add your first item.</div>'}
    </section>
  `;
}

const renderTree = (nodes) => {
  if (!nodes || !nodes.length) return '';
  return `
    <ul class="tree">
      ${nodes
        .map(
          (node) => `
            <li>
              <div class="node ${node.deleted ? 'deleted' : ''}">
                <div class="node-header">
                  <div class="node-title">
                    ${renderStatusDot(node)}
                    <div>
                      <strong>${renderDoneLabel(node)}${node.title}</strong>
                      <small>${node.slug}</small>
                    </div>
                  </div>
                  <div class="node-actions">
                    <button data-action="toggle-details" data-slug="${node.slug}">${state.expanded.has(node.slug) ? 'Hide details' : 'Show details'}</button>
                    <button data-action="edit" data-slug="${node.slug}">Edit</button>
                    <button data-action="delete" data-slug="${node.slug}">Delete</button>
                  </div>
                </div>
                ${renderNodeDetails(node)}
              </div>
              ${renderTree(node.children)}
            </li>
          `
        )
        .join('')}
    </ul>
  `;
};

const renderNodeDetails = (node) => {
  if (!state.expanded.has(node.slug)) {
    return '';
  }
  const statusLabel = formatStatusLabel(getStatusKey(node));
  return `
    <div class="node-details">
      <div class="meta-row">
        ${statusLabel ? `<span>Status: ${statusLabel}</span>` : ''}
        ${node.owner ? `<span>Owner: ${node.owner}</span>` : ''}
        ${node.dueDate ? `<span>Due: ${node.dueDate}</span>` : ''}
        <span>Own: ${formatHours(node.expectedHours)}h</span>
        <span>Total: ${formatHours(node.totalHours ?? node.expectedHours)}h</span>
      </div>
      ${node.description ? `<p class="node-description">${escapeHtml(node.description)}</p>` : ''}
      ${renderLogsSection(node)}
    </div>
  `;
};

const renderLogsSection = (node) => {
  const logs = node.logs || [];
  const items = logs.length
    ? logs
        .map(
          (log) => `
            <li class="log-entry">
              <div class="log-entry__header">
                <strong>Log #${log.sequence}</strong>
                <small>${formatDateTime(log.createdAt)}</small>
              </div>
              <p>${escapeHtml(log.summary || '').replace(/\n/g, '<br />')}</p>
              <div class="log-actions">
                <button data-action="edit-log" data-log-id="${log.id}" data-slug="${node.slug}">Edit log</button>
              </div>
            </li>
          `
        )
        .join('')
    : '<li class="log-entry log-empty">No logs yet.</li>';
  return `
    <div class="logs-block">
      <div class="logs-header">
        <span>Logs</span>
        <button class="button tiny" data-action="add-log" data-slug="${node.slug}">Add log</button>
      </div>
      <ul class="log-list">${items}</ul>
    </div>
  `;
};

const openModal = (content) => {
  modalsEl.innerHTML = `
    <div class="modal-backdrop">
      <div class="modal">
        ${content}
      </div>
    </div>
  `;
};

const closeModal = () => {
  modalsEl.innerHTML = '';
};

const createFormTemplate = () => {
  const parentSelect = renderParentSelect('parentSlug');
  return `
    <button class="modal-close" data-action="close-modal">×</button>
    <h3>Create Milestone</h3>
    <form id="create-form">
      ${formField('Title', '<input name="title" required />')}
      ${formField('Description', '<textarea name="description"></textarea>')}
      ${formField('Status', renderStatusSelect('status', 'active'))}
      ${formField('Owner', '<input name="owner" />')}
      ${formField('Due Date', '<input type="date" name="dueDate" />')}
      ${formField('Expected Hours', '<input type="number" step="0.25" min="0.25" name="expectedHours" value="1" />')}
      ${formField('Parent Milestone', parentSelect)}
      <div class="form-actions">
        <button type="button" class="button secondary" data-action="close-modal">Cancel</button>
        <button class="button" type="submit">Create</button>
      </div>
    </form>
  `;
};

const updateFormTemplate = (slug = '') => {
  const milestone = findMilestoneBySlug(slug) || {};
  const selectedParent = parentSlugFor(slug);
  const parentSelect = renderParentSelect('parentSlug', slug, selectedParent);
  const currentStatus = milestone.status ? canonicalStatusValue(milestone.status) : '';
  const hiddenSlug = `<input type="hidden" name="slug" value="${escapeHtml(slug)}" />`;
  return `
    <button class="modal-close" data-action="close-modal">×</button>
    <h3>Update Milestone</h3>
    <form id="update-form">
      ${hiddenSlug}
      ${formField('Title', `<input name="title" value="${escapeHtml(milestone.title || '')}" />`)}
      ${formField('Description', `<textarea name="description">${escapeHtml(milestone.description || '')}</textarea>`)}
      ${formField('Status', renderStatusSelect('status', currentStatus || '', true))}
      ${formField('Owner', `<input name="owner" value="${escapeHtml(milestone.owner || '')}" />`)}
      ${formField('Due Date', `<input type="date" name="dueDate" value="${escapeHtml(normalizeDateInput(milestone.dueDate))}" />`)}
      ${formField('Expected Hours', `<input type="number" step="0.25" min="0.25" name="expectedHours" value="${escapeHtml(milestone.expectedHours ?? '')}" />`)}
      ${formField('Parent Milestone', parentSelect)}
      ${formField('', '<label><input type="checkbox" name="clearParent" /> Remove parent</label>')}
      <div class="form-actions">
        <button type="button" class="button secondary" data-action="close-modal">Cancel</button>
        <button class="button" type="submit">Apply Updates</button>
      </div>
    </form>
  `;
};

const resetFormTemplate = () => `
  <button class="modal-close" data-action="close-modal">×</button>
  <h3>Reset Progress</h3>
  <form id="reset-form">
    ${formField('Snapshot Label', '<input name="label" placeholder="e.g., Sprint 8" />')}
    <div class="form-actions">
      <button type="button" class="button secondary" data-action="close-modal">Cancel</button>
      <button class="button" type="submit">Save Snapshot & Reset</button>
    </div>
  </form>
`;

const logFormTemplate = (slug, log = null) => {
  const heading = log ? 'Edit Log' : 'Add Log';
  return `
    <button class="modal-close" data-action="close-modal">×</button>
    <h3>${heading}</h3>
    <form id="log-form">
      <input type="hidden" name="slug" value="${escapeHtml(slug)}" />
      ${log ? `<input type="hidden" name="logId" value="${log.id}" />` : ''}
      ${formField('Summary', `<textarea name="summary" required>${escapeHtml(log?.summary || '')}</textarea>`)}
      <div class="form-actions">
        <button type="button" class="button secondary" data-action="close-modal">Cancel</button>
        <button class="button" type="submit">${log ? 'Save Changes' : 'Add Log'}</button>
      </div>
    </form>
  `;
};

const renderHistoryModal = () => {
  const snapshots = state.snapshots || [];
  const items = snapshots.length
    ? snapshots
        .map((snap, index) => {
          const next = snapshots[index + 1];
          const windowStart = next ? formatDateTime(next.createdAt) : 'Project start';
          const windowEnd = formatDateTime(snap.createdAt);
          return `
            <li class="history-item">
              <div class="history-range">${windowStart} → ${windowEnd}</div>
              <div class="history-label">${escapeHtml(snap.label)}</div>
              <div class="history-metrics">
                <span>${snap.completedHours.toFixed(2)}h / ${snap.totalHours.toFixed(2)}h</span>
                <span>${snap.completedCount}/${snap.totalCount} milestones</span>
              </div>
            </li>
          `;
        })
        .join('')
    : '<li class="history-empty">No historical snapshots yet.</li>';
  return `
    <button class="modal-close" data-action="close-modal">×</button>
    <h3>Progress history</h3>
    <ul class="history-list">${items}</ul>
  `;
};

const formField = (label, control) => `
  <div class="form-field">
    ${label ? `<label>${label}</label>` : ''}
    ${control}
  </div>
`;

const handleCreate = async (event) => {
  event.preventDefault();
  if (!state.currentProject) {
    showToast('Select a project first.');
    return;
  }
  const formData = new FormData(event.currentTarget);
  const payload = Object.fromEntries(formData.entries());
  try {
    const res = await fetchJSON(`/api/milestones/create?projectKey=${encodeURIComponent(state.currentProject)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    showToast(`Milestone created (slug: ${res.slug}).`);
    closeModal();
    await loadMilestones();
    state.snapshots = [];
    renderMain();
  } catch (error) {
    showToast(error.message);
  }
};

const handleUpdate = async (event) => {
  event.preventDefault();
  if (!state.currentProject) {
    showToast('Select a project first.');
    return;
  }
  const formData = new FormData(event.currentTarget);
  const payload = Object.fromEntries(formData.entries());
  const parentSelection = formData.get('parentSlug');
  const explicitClear = formData.get('clearParent') === 'on';
  if (parentSelection) {
    payload.parentSlug = parentSelection;
    payload.clearParent = false;
  } else if (explicitClear || parentSelection === '') {
    payload.clearParent = true;
    delete payload.parentSlug;
  }
  try {
    await fetchJSON(`/api/milestones/update?projectKey=${encodeURIComponent(state.currentProject)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    showToast('Milestone updated.');
    closeModal();
    await loadMilestones();
    renderMain();
  } catch (error) {
    showToast(error.message);
  }
};

const handleReset = async (event) => {
  event.preventDefault();
  if (!state.currentProject) {
    showToast('Select a project first.');
    return;
  }
  const formData = new FormData(event.currentTarget);
  try {
    await fetchJSON(`/api/progress/reset?projectKey=${encodeURIComponent(state.currentProject)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label: formData.get('label') || undefined }),
    });
    showToast('Snapshot saved.');
    closeModal();
    await loadMilestones();
    state.snapshots = [];
    renderMain();
  } catch (error) {
    showToast(error.message);
  }
};

const handleDelete = async (slug) => {
  if (!state.currentProject) {
    showToast('Select a project first.');
    return;
  }
  const confirmed = await showConfirm({
    title: 'Delete milestone',
    message: `This will mark ${slug} as deleted. You can restore it later from the Deleted filter.`,
    confirmLabel: 'Delete milestone',
  });
  if (!confirmed) return;
  try {
    await fetchJSON(`/api/milestones/delete?projectKey=${encodeURIComponent(state.currentProject)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slug }),
    });
    showToast('Milestone deleted.');
    await loadMilestones();
    state.snapshots = [];
    renderMain();
  } catch (error) {
    showToast(error.message);
  }
};

const handleProjectReset = async () => {
  if (!state.currentProject) {
    showToast('Select a project first.');
    return;
  }
  const confirmed = await showConfirm({
    title: 'Reset project data',
    message: 'This removes all milestones, logs, and progress snapshots for this project. Project metadata will be kept.',
    confirmLabel: 'Reset project',
  });
  if (!confirmed) return;
  try {
    await fetchJSON(`/api/projects/reset`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ projectKey: state.currentProject }),
    });
    state.expanded.clear();
    state.snapshots = [];
    await loadMilestones();
    renderMain();
    showToast('Project data reset.');
  } catch (error) {
    showToast(error.message);
  }
};

const handleViewHistory = async () => {
  if (!state.currentProject) {
    showToast('Select a project first.');
    return;
  }
  await loadSnapshots();
  openModal(renderHistoryModal());
};

const handleLogSubmit = async (event, isEdit) => {
  event.preventDefault();
  if (!state.currentProject) {
    showToast('Select a project first.');
    return;
  }
  const formData = new FormData(event.currentTarget);
  const payload = Object.fromEntries(formData.entries());
  if (!payload.summary) {
    showToast('Summary is required.');
    return;
  }
  if (!payload.slug) {
    showToast('Missing milestone slug.');
    return;
  }
  if (payload.logId === '') {
    delete payload.logId;
  } else if (payload.logId !== undefined) {
    payload.logId = Number(payload.logId);
  }
  const endpoint = isEdit ? '/api/milestones/logs/update' : '/api/milestones/logs/create';
  try {
    await fetchJSON(`${endpoint}?projectKey=${encodeURIComponent(state.currentProject)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    showToast(isEdit ? 'Log updated.' : 'Log added.');
    closeModal();
    await loadMilestones();
    renderMain();
  } catch (error) {
    showToast(error.message);
  }
};

sidebarEl.addEventListener('click', async (event) => {
  const btn = event.target.closest('[data-project]');
  if (btn) {
    state.currentProject = btn.dataset.project;
    await loadMilestones();
    state.snapshots = [];
    renderMain();
    return;
  }
  if (event.target.matches('[data-action="refresh-projects"]')) {
    loadProjects();
  }
});

mainEl.addEventListener('click', (event) => {
  const action = event.target.dataset.action;
  if (!action) return;
  if (action === 'open-create') {
    openModal(createFormTemplate());
    $('#create-form').addEventListener('submit', handleCreate);
  } else if (action === 'open-reset') {
    openModal(resetFormTemplate());
    $('#reset-form').addEventListener('submit', handleReset);
  } else if (action === 'reset-project') {
    handleProjectReset();
  } else if (action === 'edit') {
    const slug = event.target.dataset.slug;
    openModal(updateFormTemplate(slug));
    $('#update-form').addEventListener('submit', handleUpdate);
  } else if (action === 'delete') {
    handleDelete(event.target.dataset.slug);
  } else if (action === 'toggle-details') {
    const slug = event.target.dataset.slug;
    if (state.expanded.has(slug)) {
      state.expanded.delete(slug);
    } else {
      state.expanded.add(slug);
    }
    renderMain();
  } else if (action === 'view-history') {
    handleViewHistory();
  } else if (action === 'add-log') {
    const slug = event.target.dataset.slug;
    openModal(logFormTemplate(slug));
    $('#log-form').addEventListener('submit', (evt) => handleLogSubmit(evt, false));
  } else if (action === 'edit-log') {
    const slug = event.target.dataset.slug;
    const logId = Number(event.target.dataset.logId);
    const log = findLog(slug, logId);
    if (!log) {
      showToast('Log not found. Try refreshing.');
      return;
    }
    openModal(logFormTemplate(slug, log));
    $('#log-form').addEventListener('submit', (evt) => handleLogSubmit(evt, true));
  }
});

const renderStatusDot = (node) => {
  const statusKey = getStatusKey(node);
  const className = STATUS_COLOR_CLASS[statusKey] || 'status-gray';
  const label = formatStatusLabel(statusKey);
  return `<span class="status-dot ${className}" title="${label}"></span>`;
};

const renderDoneLabel = (node) => {
  return getStatusKey(node) === 'done' ? '<span class="status-chip">[DONE]</span> ' : '';
};

modalsEl.addEventListener('click', (event) => {
  if (event.target.classList.contains('modal-backdrop')) {
    closeModal();
    return;
  }
  if (event.target.closest('[data-action="close-modal"]')) {
    closeModal();
  }
});

mainEl.addEventListener('change', (event) => {
  if (event.target.id === 'status-filter') {
    state.statusFilter = event.target.value;
    loadMilestones().then(renderMain);
  }
});

(async () => {
  await loadProjects();
  if (state.currentProject) {
    await loadMilestones();
  }
  renderSidebar();
  renderMain();
})();
