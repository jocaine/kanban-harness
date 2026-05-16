const API = window.location.pathname.replace(/\/+$/, '');
let projects = [];
let versions = [];
let requirements = [];
let currentProject = null;
let currentVersion = null;
let editingReqId = null;
let editingProjectId = null;
let editingVersionId = null;
let selectedColor = '#4f46e5';
let pendingFiles = [];
let pendingMove = null;
let currentView = 'board';
let tagData = [];
let currentTag = null;

function compareVersions(a, b) {
    const parse = s => {
        const m = s.match(/v?(\d[\d.]*\d*)(.*)/i);
        if (!m) return {parts: [], suffix: s};
        return {parts: m[1].split('.').map(Number), suffix: m[2] || ''};
    };
    const pa = parse(a.name), pb = parse(b.name);
    for (let i = 0; i < Math.max(pa.parts.length, pb.parts.length); i++) {
        const na = pa.parts[i] || 0, nb = pb.parts[i] || 0;
        if (na !== nb) return na - nb;
    }
    return pa.suffix.localeCompare(pb.suffix);
}

function updateHash() {
    const parts = [];
    if (currentProject) parts.push('p=' + currentProject.id);
    if (currentVersion) parts.push('v=' + currentVersion.id);
    if (currentView && currentView !== 'board') parts.push('view=' + currentView);
    if (editingReqId) parts.push('r=' + editingReqId);
    const newHash = parts.length ? '#' + parts.join('&') : '';
    if (window.location.hash !== newHash) {
        history.replaceState(null, '', newHash || window.location.pathname);
    }
}

function parseHash() {
    const hash = window.location.hash.slice(1);
    const params = {};
    hash.split('&').forEach(s => {
        const [k, v] = s.split('=');
        if (k && v) params[k] = v;
    });
    return params;
}

const STATUS_MAP = {
    research: {label: '调研中', color: '#a855f7', dot: '#c084fc'},
    pending: {label: '需求整理', color: '#6366f1', dot: '#818cf8'},
    dev: {label: '开发中', color: '#f59e0b', dot: '#fbbf24'},
    testing: {label: '测试中', color: '#06b6d4', dot: '#22d3ee'},
    done: {label: '已完成', color: '#22c55e', dot: '#4ade80'}
};

const VER_STATUS = {
    planning: '规划中', active: '进行中', testing: '测试中', released: '已发布'
};

async function api(path, opts = {}) {
    const res = await fetch(API + path, {
        headers: opts.body instanceof FormData ? {} : {'Content-Type': 'application/json'},
        ...opts,
        body: opts.body instanceof FormData ? opts.body : (opts.body ? JSON.stringify(opts.body) : undefined),
    });
    if (!res.ok) throw new Error(await res.text());
    return res.json();
}

function esc(str) {
    if (!str) return '';
    const d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
}

function renderMd(str) {
    if (!str) return '';
    str = str.replace(/\\n/g, '\n');
    const html = marked.parse(str, {breaks: true, gfm: true});
    const clean = DOMPurify.sanitize(html, {ADD_TAGS: ['input'], ADD_ATTR: ['type', 'checked', 'disabled']});
    return clean.replace(/\b([A-Z]{2,5})-(\d{3,})\b/g, '<a class="code-link" href="javascript:void(0)" onclick="event.stopPropagation();jumpToCode(\'$1-$2\')">$1-$2</a>');
}

const ROLE_COLORS = ['#6366f1','#06b6d4','#f59e0b','#22c55e','#ec4899','#8b5cf6'];

const ROLE_AVATARS = {
    'PM': '/static/avatars/pm_avatar.png',
    '产品经理': '/static/avatars/pm_avatar.png',
    'Industry': '/static/avatars/industry_avatar.png',
    '行业顾问': '/static/avatars/industry_avatar.png',
    'Coach-Dev': '/static/avatars/coach_dev_avatar.png',
    'Coach': '/static/avatars/coach_dev_avatar.png',
    'Coach-Review': '/static/avatars/coach_review_avatar.png',
};

function renderRoleChat(content) {
    if (!content) return '';
    const rolePattern = /\*\*\[?([^\]*:→]+?)\]?\s*(?:→\s*([^\]*:]+?))?\s*[：:]\s*\*\*/;
    if (!rolePattern.test(content)) return renderMd(content);
    const colorMap = {};
    let colorIdx = 0;
    function getColor(role) {
        if (!colorMap[role]) colorMap[role] = ROLE_COLORS[colorIdx++ % ROLE_COLORS.length];
        return colorMap[role];
    }
    function getInitial(role) {
        const clean = role.replace(/[\[\]]/g, '').trim();
        return clean.length <= 3 ? clean : clean.slice(0, 2);
    }

    const sections = content.split(/^###\s+(.+)$/m);
    let html = '';

    for (let i = 0; i < sections.length; i++) {
        const part = sections[i].trim();
        if (!part) continue;

        if (i > 0 && sections[i - 1] !== undefined) {
            const prevRaw = sections[i - 1];
            if (/^###\s+/.test('### ' + prevRaw)) {
                // This check is tricky with split; let's use a different approach
            }
        }

        if (i % 2 === 1) {
            html += `<div class="chat-section-title">${esc(part)}</div>`;
            continue;
        }

        const lines = part.split('\n');
        let bubbles = [];
        let currentRole = null;
        let currentTarget = null;
        let currentLines = [];

        for (const line of lines) {
            const m = line.match(/\*\*\[?([^\]*:→]+?)\]?\s*(?:→\s*([^\]*:]+?))?\s*[：:]\s*\*\*(.*)/);
            if (m) {
                if (currentRole) {
                    bubbles.push({role: currentRole, target: currentTarget, text: currentLines.join('\n')});
                }
                currentRole = m[1].trim();
                currentTarget = m[2] ? m[2].trim() : null;
                currentLines = m[3] ? [m[3]] : [];
            } else {
                currentLines.push(line);
            }
        }
        if (currentRole) {
            bubbles.push({role: currentRole, target: currentTarget, text: currentLines.join('\n')});
        }

        if (bubbles.length === 0) {
            html += `<div class="md-content">${renderMd(part)}</div>`;
        } else {
            html += '<div class="chat-bubbles">';
            for (const b of bubbles) {
                const color = getColor(b.role);
                const initial = getInitial(b.role);
                const targetHtml = b.target ? `<span class="chat-target">→ ${esc(b.target)}</span>` : '';
                const avatarUrl = ROLE_AVATARS[b.role];
                html += `<div class="chat-bubble">
                    ${avatarUrl
                        ? `<img class="chat-avatar" src="${avatarUrl}" alt="${esc(b.role)}">`
                        : `<div class="chat-avatar" style="background:${color}">${esc(initial)}</div>`}
                    <div class="chat-content">
                        <div class="chat-meta"><span class="chat-role">${esc(b.role)}</span>${targetHtml}</div>
                        <div class="md-content">${renderMd(b.text.trim())}</div>
                    </div>
                </div>`;
            }
            html += '</div>';
        }
    }
    return html;
}

function formatSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1048576).toFixed(1) + ' MB';
}

// ==================== Projects ====================

async function loadProjects() {
    projects = await api('/api/projects');
    renderProjects();
}

function renderProjects() {
    const list = document.getElementById('project-list');
    if (projects.length === 0) {
        list.innerHTML = '<div style="padding:8px 10px;color:var(--text3);font-size:12px">暂无项目</div>';
        return;
    }
    list.innerHTML = projects.map(p => `
        <div class="nav-item ${currentProject && currentProject.id === p.id ? 'active' : ''}" onclick="selectProject(${p.id})">
            <div class="dot" style="background:${esc(p.color)}"></div>
            <span class="nav-label">${esc(p.name)}</span>
            <span class="nav-count">${p.req_count || 0}</span>
            <div class="nav-actions">
                <button onclick="event.stopPropagation();editProject(${p.id})" title="编辑">&#9998;</button>
                <button onclick="event.stopPropagation();deleteProject(${p.id})" title="删除">&#10005;</button>
            </div>
        </div>
    `).join('');
}

async function selectProject(pid) {
    currentProject = projects.find(p => p.id === pid);
    currentVersion = null;
    requirements = [];
    renderProjects();
    await loadVersions(pid);
    document.getElementById('version-section').style.display = '';
    document.getElementById('version-section-label').textContent = currentProject.name + ' - 版本';
    document.getElementById('board-view').style.display = 'none';
    const archView = document.getElementById('arch-view');
    if (archView) archView.style.display = 'none';
    renderProjectOverview();
    updateStats();
    if (currentView === 'tag') loadTags();
    // 切换项目时立即清空对话，确保隔离
    document.getElementById('chat-messages').innerHTML = '';
    document.getElementById('chat-messages').dataset.loaded = '';
    if (document.getElementById('chat-panel').style.display !== 'none') {
        updateChatHeader();
        loadChatHistory();
    }
    updateHash();
}

function renderGitSection(project) {
    const remoteUrl = project.git_remote_url || '';
    const lastSynced = project.git_last_synced_at || '';
    if (remoteUrl) {
        const syncInfo = lastSynced
            ? `<span style="color:var(--text3)">最近同步：</span><span>${new Date(lastSynced).toLocaleString('zh-CN', {month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'})}</span>`
            : `<span style="color:var(--text3)">尚未同步</span>`;
        return `
        <div class="git-section" style="margin-bottom:16px;padding:12px 14px;border-radius:8px;background:var(--bg2);border:1px solid var(--border)">
            <div style="display:flex;align-items:center;gap:6px;margin-bottom:8px">
                <span style="font-size:14px">&#128279;</span>
                <strong style="font-size:13px">Git 集成</strong>
                <span style="font-size:11px;color:var(--green);background:rgba(34,197,94,0.1);padding:2px 6px;border-radius:4px">已配置</span>
            </div>
            <div style="font-size:12px;color:var(--text2);display:flex;flex-direction:column;gap:4px">
                <div><span style="color:var(--text3)">仓库：</span><code style="background:var(--bg3);padding:1px 4px;border-radius:3px">${esc(remoteUrl)}</code></div>
                <div>${syncInfo}</div>
            </div>
            <div style="margin-top:8px;font-size:11px;color:var(--text3)">
                KH 自动管理 workspace，卡片进入「开发中」后 Coach-Dev 会自动在独立 worktree 中编码并关联 commit
            </div>
        </div>`;
    }
    return `
    <div class="git-section" style="margin-bottom:16px;padding:12px 14px;border-radius:8px;background:var(--bg2);border:1px dashed var(--border)">
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:8px">
            <span style="font-size:14px">&#128279;</span>
            <strong style="font-size:13px">Git 集成</strong>
            <span style="font-size:11px;color:var(--text3);background:var(--bg3);padding:2px 6px;border-radius:4px">未配置</span>
        </div>
        <div style="font-size:12px;color:var(--text2);margin-bottom:8px">
            告诉 AI 你的仓库地址，KH 会自动 clone 并管理独立 workspace，多项目互不干扰。
        </div>
        <details style="font-size:12px;color:var(--text2)">
            <summary style="cursor:pointer;color:var(--primary);font-weight:500">如何配置？</summary>
            <div style="margin-top:8px;padding:10px 12px;background:var(--bg3);border-radius:6px;font-size:11px;line-height:1.7">

                <div style="font-weight:600;margin-bottom:4px">配置方式</div>
                <div style="color:var(--text3);margin-bottom:4px">打开右下角 AI 对话，直接说：</div>
                <pre style="margin:0 0 4px;padding:6px 8px;background:var(--bg1);border-radius:4px;overflow-x:auto;white-space:pre">git 仓库是 git@github.com:user/repo.git</pre>
                <div style="color:var(--text3);margin-bottom:12px">KH 会自动 clone 到 <code>~/.kh/workspaces/project_${project.id}/</code>，每个项目独立目录，互不冲突。</div>

                <div style="font-weight:600;margin-bottom:4px">工作流程</div>
                <div style="color:var(--text3)">
                    卡片移到「开发中」→ Scheduler 自动触发 Coach-Dev → 在 git worktree 中编码 → commit 自动关联卡片 → 卡片移到「测试中」
                </div>
            </div>
        </details>
    </div>`;
}

function renderProjectOverview() {
    const el = document.getElementById('welcome-view');
    const total = versions.reduce((s, v) => s + (v.req_count || 0), 0);
    const done = versions.reduce((s, v) => s + (v.done_count || 0), 0);
    const pct = total > 0 ? Math.round((done / total) * 100) : 0;
    const versionList = versions.length > 0 ? versions.map(v => {
        const vPct = v.req_count > 0 ? Math.round((v.done_count / v.req_count) * 100) : 0;
        return `<details class="version-card-collapse">
            <summary class="project-overview-version" onclick="event.preventDefault();this.parentElement.toggleAttribute('open')">
                <div style="display:flex;align-items:center;gap:8px">
                    <span class="ver-status ${v.status}">${VER_STATUS[v.status] || v.status}</span>
                    <strong>${esc(v.name)}</strong>
                    <span style="color:var(--text3);font-size:12px">${v.done_count || 0}/${v.req_count || 0} 需求</span>
                </div>
                <div class="progress-bar" style="margin-top:6px"><div class="progress-fill" style="width:${vPct}%"></div></div>
            </summary>
            <div class="version-card-body" onclick="selectVersion(${v.id})">
                ${v.description ? '<div class="md-content" style="color:var(--text2);font-size:13px;margin-bottom:8px">' + renderMd(v.description) + '</div>' : ''}
                <button class="btn-primary btn-sm" style="font-size:12px">进入版本</button>
            </div>
        </details>`;
    }).join('') : '<div style="color:var(--text3);font-size:13px">暂无版本，点击左侧「+」创建第一个版本</div>';

    el.innerHTML = `
        <div style="width:100%;max-width:600px;text-align:left">
            <div style="display:flex;align-items:center;gap:10px;margin-bottom:4px">
                <div class="dot" style="background:${esc(currentProject.color)};width:14px;height:14px;border-radius:50%"></div>
                <h2 style="margin:0">${esc(currentProject.name)}</h2>
            </div>
            ${currentProject.description ? '<p style="color:var(--text2);margin:8px 0 16px">' + esc(currentProject.description) + '</p>' : '<div style="margin-bottom:16px"></div>'}
            <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px;font-size:13px;color:var(--text3)">
                <span>版本: ${versions.length}</span>
                <span>需求: ${done}/${total}</span>
                <span>完成度: ${pct}%</span>
            </div>
            <div style="display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap">
                <button class="btn-arch" onclick="showDocView('arch')">架构文档</button>
                <button class="btn-arch" onclick="showDocView('team')">AI 团队</button>
                <button class="btn-arch" onclick="showDocView('memory')">产品记忆</button>
            </div>
            ${renderGitSection(currentProject)}
            <h3 style="margin:0 0 10px;font-size:15px">版本列表</h3>
            <div style="display:flex;flex-direction:column;gap:10px">${versionList}</div>
        </div>
    `;
    el.style.display = 'flex';
}

function renderWelcomeDefault() {
    const el = document.getElementById('welcome-view');
    el.innerHTML = `
        <div class="welcome-icon">&#128203;</div>
        <h2>需求管理系统</h2>
        <p>选择左侧项目和版本，或创建一个新项目开始</p>
        <button class="btn-primary" onclick="showProjectModal()">创建第一个项目</button>
    `;
    el.style.display = 'flex';
}

function showProjectModal(editId) {
    editingProjectId = editId || null;
    const modal = document.getElementById('project-modal');
    document.getElementById('project-modal-title').textContent = editId ? '编辑项目' : '新建项目';
    if (editId) {
        const p = projects.find(x => x.id === editId);
        document.getElementById('proj-name').value = p.name;
        document.getElementById('proj-desc').value = p.description || '';
        selectedColor = p.color || '#4f46e5';
    } else {
        document.getElementById('proj-name').value = '';
        document.getElementById('proj-desc').value = '';
        selectedColor = '#4f46e5';
    }
    document.querySelectorAll('.color-dot').forEach(d => {
        d.classList.toggle('selected', d.dataset.color === selectedColor);
    });
    modal.classList.remove('hidden');
    document.getElementById('proj-name').focus();
}

function editProject(pid) { showProjectModal(pid); }

async function saveProject() {
    const name = document.getElementById('proj-name').value.trim();
    if (!name) return;
    const body = {
        name,
        description: document.getElementById('proj-desc').value.trim(),
        color: selectedColor
    };
    if (editingProjectId) {
        await api('/api/projects/' + editingProjectId, {method: 'PUT', body});
    } else {
        await api('/api/projects', {method: 'POST', body});
    }
    hideModal('project-modal');
    await loadProjects();
    if (editingProjectId && currentProject && currentProject.id === editingProjectId) {
        currentProject = projects.find(p => p.id === editingProjectId);
    }
}

async function deleteProject(pid) {
    const p = projects.find(x => x.id === pid);
    if (!confirm('确定删除项目「' + p.name + '」？所有版本和需求都会被归档。')) return;
    await api('/api/projects/' + pid, {method: 'DELETE'});
    if (currentProject && currentProject.id === pid) {
        currentProject = null;
        currentVersion = null;
        document.getElementById('version-section').style.display = 'none';
        document.getElementById('board-view').style.display = 'none';
        renderWelcomeDefault();
    }
    await loadProjects();
}

function pickColor(el) {
    selectedColor = el.dataset.color;
    document.querySelectorAll('.color-dot').forEach(d => d.classList.remove('selected'));
    el.classList.add('selected');
}

// ==================== Versions ====================

async function loadVersions(pid) {
    versions = await api('/api/projects/' + pid + '/versions');
    versions.sort(compareVersions);
    renderVersions();
}

function renderVersions() {
    const list = document.getElementById('version-list');
    if (versions.length === 0) {
        list.innerHTML = '<div style="padding:8px 10px;color:var(--text3);font-size:12px">暂无版本</div>';
        return;
    }
    list.innerHTML = versions.map(v => {
        const pct = v.req_count > 0 ? Math.round((v.done_count / v.req_count) * 100) : 0;
        const allDone = v.req_count > 0 && v.done_count === v.req_count && v.status !== 'released';
        return `
        <div class="nav-item ${currentVersion && currentVersion.id === v.id ? 'active' : ''}" onclick="selectVersion(${v.id})">
            <span class="ver-status ${v.status}">${VER_STATUS[v.status] || v.status}</span>
            <span class="nav-label">${esc(v.name)}</span>
            ${allDone ? '<span class="ver-all-done" title="所有需求已完成，点击标记为已发布" onclick="event.stopPropagation();markReleased(' + v.id + ')">&#10003; 可发布</span>' : ''}
            <span class="nav-count">${v.done_count || 0}/${v.req_count || 0}</span>
            <div class="nav-actions">
                <button onclick="event.stopPropagation();editVersion(${v.id})" title="编辑">&#9998;</button>
                <button onclick="event.stopPropagation();deleteVersion(${v.id})" title="删除">&#10005;</button>
            </div>
                         <div style="padding:0 10px 4px"><div class="progress-bar"><div class="progress-fill" style="width:${pct}%"></div></div></div>
        </div>
        `;
    }).join('');
}

async function selectVersion(vid) {
    currentVersion = versions.find(v => v.id === vid);
    renderVersions();
    await loadRequirements(vid);
    document.getElementById('welcome-view').style.display = 'none';
    document.getElementById('board-view').style.display = 'flex';
    document.getElementById('board-title-text').textContent = currentProject.name;
    document.getElementById('board-version-badge').textContent = currentVersion.name;
    updateDescCollapsible('version');
    renderRequirements();
    updateHash();
}

function showVersionModal(editId) {
    editingVersionId = editId || null;
    document.getElementById('version-modal-title').textContent = editId ? '编辑版本' : '新建版本';
    if (editId) {
        const v = versions.find(x => x.id === editId);
        document.getElementById('ver-name').value = v.name;
        document.getElementById('ver-desc').value = v.description || '';
    } else {
        document.getElementById('ver-name').value = '';
        document.getElementById('ver-desc').value = '';
    }
    document.getElementById('version-modal').classList.remove('hidden');
    document.getElementById('ver-name').focus();
}

function editVersion(vid) { showVersionModal(vid); }

async function saveVersion() {
    const name = document.getElementById('ver-name').value.trim();
    if (!name) return;
    const desc = document.getElementById('ver-desc').value.trim();
    if (editingVersionId) {
        await api('/api/versions/' + editingVersionId, {method: 'PUT', body: {name, description: desc}});
    } else {
        await api('/api/versions', {method: 'POST', body: {project_id: currentProject.id, name, description: desc}});
    }
    hideModal('version-modal');
    await loadVersions(currentProject.id);
    if (currentVersion && editingVersionId) {
        currentVersion = versions.find(v => v.id === editingVersionId);
    }
    if (!currentVersion) renderProjectOverview();
}

async function deleteVersion(vid) {
    const v = versions.find(x => x.id === vid);
    if (!confirm('确定删除版本「' + v.name + '」？其中的需求也会被删除。')) return;
    await api('/api/versions/' + vid, {method: 'DELETE'});
    if (currentVersion && currentVersion.id === vid) {
        currentVersion = null;
        document.getElementById('board-view').style.display = 'none';
    }
    await loadVersions(currentProject.id);
    if (!currentVersion) renderProjectOverview();
}

// ==================== Requirements ====================

async function loadRequirements(vid) {
    requirements = await api('/api/versions/' + vid + '/requirements');
}

const COL_ROLE_MAP = {
    research: {role: 'Industry', avatar: '/static/avatars/industry_avatar.png'},
    pending: {role: 'PM', avatar: '/static/avatars/pm_avatar.png'},
    dev: {role: 'Coach-Dev', avatar: '/static/avatars/coach_dev_avatar.png'},
    testing: {role: 'Coach-Review', avatar: '/static/avatars/coach_review_avatar.png'},
};

function renderRequirements() {
    const search = document.getElementById('search-input').value.toLowerCase();
    const priFilter = document.getElementById('filter-priority').value;
    const container = document.getElementById('board-columns');

    let filtered = requirements;
    if (search) filtered = filtered.filter(r => r.title.toLowerCase().includes(search) || (r.description || '').toLowerCase().includes(search) || (r.assignee || '').toLowerCase().includes(search) || (r.code || '').toLowerCase().includes(search));
    if (priFilter) filtered = filtered.filter(r => r.priority === priFilter);

    container.innerHTML = '';
    Object.entries(STATUS_MAP).forEach(([status, info]) => {
        if (status === 'done') return;
        const cards = filtered.filter(r => r.status === status);
        const roleInfo = COL_ROLE_MAP[status];

        // Wrap column with avatar in a col-wrapper
        const wrapper = document.createElement('div');
        wrapper.className = 'col-wrapper';

        if (roleInfo) {
            const avatar = document.createElement('img');
            avatar.className = 'col-avatar';
            avatar.src = roleInfo.avatar;
            avatar.alt = roleInfo.role;
            avatar.title = roleInfo.role;
            wrapper.appendChild(avatar);
        }

        const col = document.createElement('div');
        col.className = 'board-col';
        col.dataset.status = status;
        col.innerHTML = `
            <div class="col-header">
                <div class="col-dot" style="background:${info.dot}"></div>
                <span class="col-name">${info.label}</span>
            <span class="col-count">${cards.length}</span>
            </div>
            <div class="col-cards" data-status="${status}"></div>
        `;
        const cardsEl = col.querySelector('.col-cards');
        setupDropZone(cardsEl, status);

        const activeCards = roleInfo ? cards.filter(r => r.assignee === roleInfo.role) : [];
        const waitingCards = roleInfo ? cards.filter(r => r.assignee !== roleInfo.role) : cards;

        if (activeCards.length > 0) {
            activeCards.forEach(r => cardsEl.appendChild(createCardEl(r)));
        } else {
            const emptyActive = document.createElement('div');
            emptyActive.className = 'empty-section';
            emptyActive.textContent = '当前无进行中任务';
            cardsEl.appendChild(emptyActive);
        }

        const divider = document.createElement('div');
        divider.className = 'col-divider';
        divider.innerHTML = '<span>排队中</span>';
        cardsEl.appendChild(divider);

        if (waitingCards.length > 0) {
            waitingCards.forEach(r => cardsEl.appendChild(createCardEl(r)));
        } else {
            const emptyWaiting = document.createElement('div');
            emptyWaiting.className = 'empty-section';
            emptyWaiting.textContent = '队列为空';
            cardsEl.appendChild(emptyWaiting);
        }
        wrapper.appendChild(col);
        container.appendChild(wrapper);
    });

    // Done panel — fixed right-side drawer
    const doneCards = filtered.filter(r => r.status === 'done');
    const doneInfo = STATUS_MAP.done;

    let doneDrawer = document.getElementById('done-drawer');
    let doneTab = document.getElementById('done-tab');
    if (!doneDrawer) {
        doneTab = document.createElement('div');
        doneTab.id = 'done-tab';
        doneTab.addEventListener('click', () => {
            doneDrawer.classList.toggle('open');
            doneTab.classList.toggle('active');
        });
        document.body.appendChild(doneTab);

        doneDrawer = document.createElement('div');
        doneDrawer.id = 'done-drawer';
        document.body.appendChild(doneDrawer);
    }
    doneTab.innerHTML = `<span class="done-tab-dot" style="background:${doneInfo.dot}"></span>已完成<span class="done-tab-count">${doneCards.length}</span>`;

    doneDrawer.innerHTML = `
        <div class="done-drawer-header">
            <div class="col-dot" style="background:${doneInfo.dot}"></div>
            <span>已完成</span>
            <span class="col-count">${doneCards.length}</span>
            <button class="done-drawer-close" title="关闭">&times;</button>
        </div>
        <div class="done-drawer-cards" data-status="done"></div>
    `;
    doneDrawer.querySelector('.done-drawer-close').addEventListener('click', () => {
        doneDrawer.classList.remove('open');
        doneTab.classList.remove('active');
    });
    const doneCardsEl = doneDrawer.querySelector('.done-drawer-cards');
    setupDropZone(doneCardsEl, 'done');
    if (doneCards.length === 0) {
        doneCardsEl.innerHTML = '<div class="empty-col">拖拽需求到这里</div>';
    } else {
        doneCards.forEach(r => doneCardsEl.appendChild(createCardEl(r)));
    }
}

function createCardEl(r) {
    const el = document.createElement('div');
    el.className = 'card';
    el.draggable = true;
    el.dataset.id = r.id;

    let tags = [];
    try { tags = JSON.parse(r.tags || '[]'); } catch(e) {}
    if (typeof tags === 'string') tags = tags.split(',').map(t => t.trim()).filter(Boolean);

    const tagsHtml = tags.map(t => '<span class="card-tag">' + esc(t) + '</span>').join('');
    const deadlineHtml = r.deadline ? '<span title="截止日期">' + esc(r.deadline) + '</span>' : '';
    const assigneeHtml = r.assignee ? '<span title="负责人">' + esc(r.assignee) + '</span>' : '';
    const hoursHtml = r.estimated_hours > 0 ? '<span title="工时">' + r.actual_hours + '/' + r.estimated_hours + 'h</span>' : '';
    const attHtml = (r.attachments && r.attachments.length > 0) ? '<span title="附件">' + r.attachments.length + '个附件</span>' : '';

    const reviewBadge = r.reviewed === false ? '<span class="review-badge unreviewed">未审议</span>' : '';

    el.innerHTML = `
        <div class="card-top">
            <span class="priority-badge ${r.priority}">${r.priority}</span>
            ${r.code ? '<span class="card-code copyable" onclick="event.stopPropagation();copyCode(\'' + esc(r.code) + '\')" title="点击复制">' + esc(r.code) + '</span>' : ''}
            ${reviewBadge}
            ${tagsHtml}
        </div>
        <div class="card-title">${esc(r.title)}</div>
        ${r.description ? '<div class="card-desc md-content">' + renderMd(r.description) + '</div>' : ''}
        <div class="card-bottom">
            <div class="card-meta">${assigneeHtml}${deadlineHtml}${hoursHtml}${attHtml}</div>
            <div class="card-actions">
                <button onclick="event.stopPropagation();showReqModal(${r.id})" title="编辑">&#9998;</button>
                <button onclick="event.stopPropagation();archiveReq(${r.id})" title="归档">&#128230;</button>
                <button class="danger" onclick="event.stopPropagation();deleteReq(${r.id})" title="删除">&#10005;</button>
            </div>
        </div>
    `;

    el.addEventListener('dragstart', e => {
        el.classList.add('dragging');
        e.dataTransfer.setData('text/plain', r.id);
        e.dataTransfer.effectAllowed = 'move';
    });
    el.addEventListener('dragend', () => {
        el.classList.remove('dragging');
        document.querySelectorAll('.drop-placeholder').forEach(p => p.remove());
        document.querySelectorAll('.board-col.drag-over').forEach(c => c.classList.remove('drag-over'));
    });
    el.addEventListener('click', () => showReqModal(r.id));
    return el;
}

function setupDropZone(container, status) {
    container.addEventListener('dragover', e => {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        container.closest('.board-col').classList.add('drag-over');
        let ph = container.querySelector('.drop-placeholder');
        if (!ph) {
            ph = document.createElement('div');
            ph.className = 'drop-placeholder';
            container.appendChild(ph);
        }
        const afterEl = getDragAfter(container, e.clientY);
        if (afterEl) container.insertBefore(ph, afterEl);
        else container.appendChild(ph);
    });
    container.addEventListener('dragleave', e => {
        if (!container.contains(e.relatedTarget)) {
            container.closest('.board-col').classList.remove('drag-over');
            const ph = container.querySelector('.drop-placeholder');
            if (ph) ph.remove();
        }
    });
    container.addEventListener('drop', async e => {
        e.preventDefault();
        container.closest('.board-col').classList.remove('drag-over');
        const ph = container.querySelector('.drop-placeholder');
        const rid = parseInt(e.dataTransfer.getData('text/plain'));
        if (!rid || !ph) return;
        const position = [...container.children].indexOf(ph);
        ph.remove();
        // 弹出确认弹窗，要求人类填写移动原因
        pendingMove = {rid, status, position};
        const card = requirements.find(x => x.id === rid);
        const cardTitle = card ? card.title : `#${rid}`;
        const desc = status === 'done'
            ? `将 [${cardTitle}] 标记为完成，请说明完成情况（验收结果、遗留问题等）：`
            : `将 [${cardTitle}] 移动到「${status}」，请说明原因：`;
        document.getElementById('move-confirm-desc').textContent = desc;
        document.getElementById('move-confirm-reason').value = '';
        document.getElementById('move-confirm-reason').style.borderColor = '';
        document.getElementById('move-confirm-modal').classList.remove('hidden');
        document.getElementById('move-confirm-reason').focus();
    });
}

function getDragAfter(container, y) {
    const els = [...container.querySelectorAll('.card:not(.dragging)')];
    return els.reduce((closest, child) => {
        const box = child.getBoundingClientRect();
        const offset = y - box.top - box.height / 2;
        if (offset < 0 && offset > closest.offset) return {offset, element: child};
        return closest;
    }, {offset: Number.NEGATIVE_INFINITY}).element;
}

// ================ Move Confirmation ====================

async function confirmMove() {
    const reason = document.getElementById('move-confirm-reason').value.trim();
    if (!reason) {
        document.getElementById('move-confirm-reason').style.borderColor = '#dc2626';
        document.getElementById('move-confirm-reason').setAttribute('placeholder', '必须填写移动原因！');
        document.getElementById('move-confirm-reason').focus();
        return;
    }
    if (!pendingMove) return;
    const {rid, status, position} = pendingMove;
    document.getElementById('move-confirm-modal').classList.add('hidden');
    pendingMove = null;
    await api('/api/requirements/' + rid + '/move', {method: 'PUT', body: {status, position, reason}});
    await loadRequirements(currentVersion.id);
    renderRequirements();
    await refreshVersions();
}

function cancelMove() {
    document.getElementById('move-confirm-modal').classList.add('hidden');
    pendingMove = null;
    // 恢复看板显示（卡片已从原位移除，需要重新渲染）
    renderRequirements();
}

// ================ Requirement Modal ====================

function showReqModal(editId) {
    editingReqId = editId || null;
    pendingFiles = [];
    document.getElementById('req-modal-title').textContent = editId ? '编辑需求' : '新建需求';

    // Reset to first tab
    switchReqTab('desc');

    if (editId) {
        const r = requirements.find(x => x.id === editId);
        document.getElementById('req-title').value = r.title;
        document.getElementById('req-desc').value = r.description || '';
        document.getElementById('req-priority').value = r.priority;
        document.getElementById('req-status').value = r.status;
        document.getElementById('req-assignee').value = r.assignee || '';
        document.getElementById('req-deadline').value = r.deadline || '';
        document.getElementById('req-hours').value = r.estimated_hours || 0;
        document.getElementById('req-actual').value = r.actual_hours || 0;
        let tags = [];
        try { tags = JSON.parse(r.tags || '[]'); } catch(e) {}
        if (typeof tags === 'string') tags = tags.split(',').map(t => t.trim()).filter(Boolean);
        document.getElementById('req-tags').value = tags.join(', ');
        document.getElementById('req-notes').value = r.notes || '';
        renderAttachments(r.attachments || []);
        loadComments(editId);
        loadCommits(editId);
        // Default to preview mode when opening existing card
        if (r.description) {
            const preview = document.getElementById('req-desc-preview');
            preview.innerHTML = renderMd(r.description);
            preview.classList.remove('hidden');
            document.getElementById('req-desc').style.display = 'none';
            document.getElementById('btn-desc-preview').textContent = '编辑';
        }
    } else {
        document.getElementById('req-title').value = '';
        document.getElementById('req-desc').value = '';
        document.getElementById('req-priority').value = 'P2';
        document.getElementById('req-status').value = 'pending';
        document.getElementById('req-assignee').value = '';
        document.getElementById('req-deadline').value = '';
        document.getElementById('req-hours').value = 0;
        document.getElementById('req-actual').value = 0;
        document.getElementById('req-tags').value = '';
        document.getElementById('req-notes').value = '';
        document.getElementById('attachment-list').innerHTML = '';
        document.getElementById('commits-section').style.display = 'none';
    }
    document.getElementById('req-modal').classList.remove('hidden');
    document.getElementById('req-title').focus();
    updateHash();
}

function renderAttachments(atts) {
    const list = document.getElementById('attachment-list');
    list.innerHTML = atts.map(a => `
        <div class="att-item">
            <span class="att-name" onclick="event.stopPropagation();previewAtt(${a.id})">${esc(a.filename)}</span>
            <span class="att-size">${formatSize(a.filesize)}</span>
            <button onclick="event.stopPropagation();downloadAtt(${a.id})" title="下载">&#8595;</button>
            <button onclick="event.stopPropagation();deleteAtt(${a.id})" title="删除">&times;</button>
        </div>
    `).join('');
}

function handleFileSelect(input) {
    pendingFiles = [...pendingFiles, ...input.files];
    const list = document.getElementById('attachment-list');
    for (const f of input.files) {
        const div = document.createElement('div');
        div.className = 'att-item';
        div.innerHTML = '<span class="att-name">' + esc(f.name) + '</span><span class="att-size">' + formatSize(f.size) + '</span><span style="color:var(--warning)">待上传</span>';
        list.appendChild(div);
    }
    input.value = '';
}

async function saveRequirement() {
    const title = document.getElementById('req-title').value.trim();
    if (!title) { document.getElementById('req-title').focus(); return; }

    const tagsRaw = document.getElementById('req-tags').value.trim();
    const tags = JSON.stringify(tagsRaw ? tagsRaw.split(',').map(t => t.trim()).filter(Boolean) : []);

    const body = {
        title,
        description: document.getElementById('req-desc').value.trim(),
        priority: document.getElementById('req-priority').value,
        status: document.getElementById('req-status').value,
        assignee: document.getElementById('req-assignee').value.trim(),
        deadline: document.getElementById('req-deadline').value,
        estimated_hours: parseFloat(document.getElementById('req-hours').value) || 0,
        actual_hours: parseFloat(document.getElementById('req-actual').value) || 0,
        tags,
        notes: document.getElementById('req-notes').value.trim()
    };

    let rid = editingReqId;
    if (editingReqId) {
        await api('/api/requirements/' + editingReqId, {method: 'PUT', body});
    } else {
        body.version_id = currentVersion.id;
        const result = await api('/api/requirements', {method: 'POST', body});
        rid = result.id;
    }

    for (const f of pendingFiles) {
        const fd = new FormData();
        fd.append('file', f);
        await api('/api/requirements/' + rid + '/attachments', {method: 'POST', body: fd});
    }
    pendingFiles = [];

    hideModal('req-modal');
    await loadRequirements(currentVersion.id);
    renderRequirements();
    await refreshVersions();
}

async function deleteReq(rid) {
    if (!confirm('确定删除这个需求？')) return;
    await api('/api/requirements/' + rid, {method: 'DELETE'});
    await loadRequirements(currentVersion.id);
    renderRequirements();
    await refreshVersions();
}

async function archiveReq(rid) {
    await api('/api/requirements/' + rid + '/archive', {method: 'PUT'});
    await loadRequirements(currentVersion.id);
    renderRequirements();
    await refreshVersions();
}

function previewAtt(aid) {
    window.open(API + '/api/attachments/' + aid + '/preview', '_blank');
}

async function downloadAtt(aid) {
    window.open(API + '/api/attachments/' + aid + '/download', '_blank');
}

async function deleteAtt(aid) {
    if (!confirm('确定删除此附件？')) return;
    await api('/api/attachments/' + aid, {method: 'DELETE'});
    if (editingReqId) {
        await loadRequirements(currentVersion.id);
        const r = requirements.find(x => x.id === editingReqId);
        if (r) renderAttachments(r.attachments || []);
    }
}

// ==================== View Toggle ====================

function updateDescCollapsible(mode, content) {
    const el = document.getElementById('desc-collapsible');
    const label = document.getElementById('desc-toggle-label');
    const body = document.getElementById('desc-body');
    if (mode === 'hide') {
        el.style.display = 'none';
        return;
    }
    if (mode === 'version') {
        const desc = currentVersion && currentVersion.description;
        if (!desc) { el.style.display = 'none'; return; }
        label.textContent = '版本介绍';
        body.innerHTML = renderMd(desc);
        el.style.display = '';
    } else if (mode === 'tag') {
        if (!content) { el.style.display = 'none'; return; }
        label.textContent = '标签介绍';
        body.innerHTML = renderMd(content);
        el.style.display = '';
    }
}

// ==================== Tab Switching ====================

function switchReqTab(tab) {
    document.querySelectorAll('.req-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
    document.querySelectorAll('.req-tab-content').forEach(c => c.classList.toggle('active', c.id === 'tab-' + tab));
}

function switchView(view) {
    currentView = view;
    document.getElementById('btn-board-mode').classList.toggle('active', view === 'board');
    document.getElementById('btn-tag-mode').classList.toggle('active', view === 'tag');
    document.getElementById('btn-arch-mode').classList.toggle('active', view === 'arch');
    updateHash();
    if (view === 'board') {
        document.getElementById('board-columns').style.display = 'flex';
        document.getElementById('tag-view').style.display = 'none';
        document.getElementById('arch-view').style.display = 'none';
        updateDescCollapsible('version');
        renderRequirements();
    } else if (view === 'tag') {
        document.getElementById('board-columns').style.display = 'none';
        document.getElementById('tag-view').style.display = 'flex';
        document.getElementById('arch-view').style.display = 'none';
        updateDescCollapsible('hide');
        loadTags();
    } else {
        document.getElementById('board-columns').style.display = 'none';
        document.getElementById('tag-view').style.display = 'none';
        document.getElementById('board-view').style.display = 'none';
        updateDescCollapsible('hide');
        showArchView();
    }
}

function onSearchInput() {
    if (currentView === 'board') renderRequirements();
    else renderTagList();
}

function onSearchEnter() {
    const val = document.getElementById('search-input').value.trim();
    if (!val) return;
    if (/^[A-Z]{2,5}-\d{3,}$/i.test(val) || /^\d+$/.test(val)) {
        jumpToCode(val);
    }
}

function onFilterChange() {
    if (currentView === 'board') renderRequirements();
    else renderTagList();
}

// ==================== Tag View ====================

async function loadTags() {
    if (!currentProject) return;
    tagData = await api('/api/tags?project_id=' + currentProject.id);
    currentTag = null;
    document.getElementById('tag-detail').style.display = 'none';
    renderTagList();
}

function renderTagList() {
    const search = document.getElementById('search-input').value.toLowerCase();
    const list = document.getElementById('tag-list');
    let filtered = tagData;
    if (search) filtered = filtered.filter(t => t.tag.toLowerCase().includes(search));

    if (filtered.length === 0) {
        list.innerHTML = '<div class="tag-empty">暂无标签，创建需求时添加标签即可</div>';
        return;
    }

    list.innerHTML = filtered.map(t => {
        const pct = t.total > 0 ? Math.round((t.done / t.total) * 100) : 0;
        const isActive = currentTag === t.tag;
        return `
        <div class="tag-card ${isActive ? 'active' : ''}" onclick="showTagDetail('${esc(t.tag).replace(/'/g, "\\'")}')">
            <div class="tag-card-header">
                <span class="tag-card-name">${esc(t.tag)}</span>
                <span class="tag-card-total">${t.total} 条需求</span>
            </div>
            ${t.description ? '<div class="tag-card-desc md-content">' + renderMd(t.description) + '</div>' : ''}
            <div class="tag-progress-row">
                <div class="tag-progress-bar"><div class="tag-progress-fill" style="width:${pct}%"></div></div>
                <span class="tag-card-pct">${pct}%</span>
            </div>
            <div class="tag-card-stats">
                ${t.research > 0 ? '<span class="tag-stat research">' + t.research + ' 调研中</span>' : ''}
                ${t.pending > 0 ? '<span class="tag-stat pending">' + t.pending + ' 待开发</span>' : ''}
                ${t.dev > 0 ? '<span class="tag-stat dev">' + t.dev + ' 开发中</span>' : ''}
                ${t.testing > 0 ? '<span class="tag-stat testing">' + t.testing + ' 测试中</span>' : ''}
                ${t.done > 0 ? '<span class="tag-stat done">' + t.done + ' 已完成</span>' : ''}
            </div>
        </div>`;
    }).join('');
}

async function showTagDetail(tag) {
    currentTag = tag;
    renderTagList();
    const detail = document.getElementById('tag-detail');
    detail.style.display = '';
    detail.innerHTML = '<div class="tag-loading">加载中...</div>';

    const data = await api('/api/tags/' + encodeURIComponent(tag) + '/requirements?project_id=' + currentProject.id);
    const priFilter = document.getElementById('filter-priority').value;

    let html = '<div class="tag-detail-header"><h3>' + esc(tag) + '</h3><span class="tag-detail-summary">'
        + data.summary.total + ' 条需求，' + data.summary.done + ' 已完成</span>'
        + '</div>';

    updateDescCollapsible('tag', data.description || '');

    const statusOrder = ['research', 'pending', 'dev', 'testing', 'done'];
    for (const status of statusOrder) {
        let items = data.grouped[status] || [];
        if (priFilter) items = items.filter(r => r.priority === priFilter);
        if (items.length === 0) continue;
        const info = STATUS_MAP[status];
        html += '<div class="tag-status-group">';
        html += '<div class="tag-status-header"><div class="col-dot" style="background:' + info.dot + '"></div>'
            + '<span>' + info.label + '</span><span class="tag-status-count">' + items.length + '</span></div>';
        html += '<div class="tag-req-list">';
        for (const r of items) {
            let tags = [];
            try { tags = JSON.parse(r.tags || '[]'); } catch(e) {}
            const tagsHtml = tags.filter(t => t !== tag).map(t => '<span class="card-tag">' + esc(t) + '</span>').join('');
            html += `
            <div class="tag-req-card" onclick="showReqModalFromTag(${r.id}, '${esc(tag).replace(/'/g, "\\'")}')">
                <div class="card-top">
                    <span class="priority-badge ${r.priority}">${r.priority}</span>
                    ${r.code ? '<span class="card-code copyable" onclick="event.stopPropagation();copyCode(\'' + esc(r.code) + '\')" title="点击复制">' + esc(r.code) + '</span>' : ''}
                    ${tagsHtml}
                </div>
                <div class="card-title">${esc(r.title)}</div>
                ${r.description ? '<div class="card-desc md-content">' + renderMd(r.description) + '</div>' : ''}
                <div class="card-meta">
                    ${r.assignee ? '<span>' + esc(r.assignee) + '</span>' : ''}
                    ${r.deadline ? '<span>' + esc(r.deadline) + '</span>' : ''}
                    ${r.version_name ? '<span class="tag-version-label">' + esc(r.version_name) + '</span>' : ''}
                </div>
            </div>`;
        }
        html += '</div></div>';
    }
    detail.innerHTML = html;
}

async function showReqModalFromTag(rid, tag) {
    const data = await api('/api/requirements/' + rid);
    const idx = requirements.findIndex(x => x.id === rid);
    if (idx >= 0) requirements[idx] = data;
    else requirements.push(data);
    showReqModal(rid);
}

// ==================== Document View (Architecture / Advisor Skill / Product Memory) ====================

let docContent = '';
let currentDocTab = 'team';

const DOC_CONFIG = {
    arch: {label: '架构文档', apiPath: '/architecture', emptyMsg: '暂无架构文档，点击「编辑」添加项目架构与技术栈说明'},
    memory: {label: '产品记忆', apiPath: '/product-memory', emptyMsg: '暂无产品记忆文档，点击「编辑」开始记录产品决策历史'},
};

async function loadDoc() {
    if (!currentProject) return;
    const cfg = DOC_CONFIG[currentDocTab];
    const data = await api('/api/projects/' + currentProject.id + cfg.apiPath);
    docContent = data.content || '';
    renderDoc();
}

function renderDoc() {
    const el = document.getElementById('arch-content');
    const cfg = DOC_CONFIG[currentDocTab];

    if (!docContent) {
        el.innerHTML = '<div class="arch-empty">' + cfg.emptyMsg + '</div>';
    } else if (currentDocTab === 'arch') {
        let html = renderMd(docContent);
        const wrapper = document.createElement('div');
        wrapper.innerHTML = html;
        const result = document.createElement('div');
        let currentDetails = null;
        for (const node of [...wrapper.childNodes]) {
            if (node.nodeType === 1 && /^H[12]$/.test(node.tagName)) {
                if (currentDetails) { result.appendChild(currentDetails); currentDetails = null; }
                result.appendChild(node.cloneNode(true));
            } else if (node.nodeType === 1 && node.tagName === 'H3') {
                if (currentDetails) result.appendChild(currentDetails);
                currentDetails = document.createElement('details');
                currentDetails.className = 'arch-collapse';
                const summary = document.createElement('summary');
                summary.innerHTML = node.innerHTML;
                currentDetails.appendChild(summary);
            } else if (currentDetails) {
                currentDetails.appendChild(node.cloneNode(true));
            } else {
                result.appendChild(node.cloneNode(true));
            }
        }
        if (currentDetails) result.appendChild(currentDetails);
        el.innerHTML = result.innerHTML;
    } else {
        el.innerHTML = renderMd(docContent);
    }
}

function showDocView(tab) {
    currentDocTab = tab || 'team';
    document.getElementById('welcome-view').style.display = 'none';
    document.getElementById('board-view').style.display = 'none';
    document.getElementById('arch-view').style.display = 'block';
    document.getElementById('arch-title').textContent = currentProject.name + ' - 项目文档';
    updateDocTabs();
    if (tab === 'team') {
        document.getElementById('team-view').style.display = 'block';
        document.getElementById('arch-content').style.display = 'none';
        document.getElementById('arch-actions').style.display = 'none';
        loadTeamView();
    } else {
        document.getElementById('team-view').style.display = 'none';
        document.getElementById('arch-content').style.display = '';
        document.getElementById('arch-actions').style.display = '';
        loadDoc();
    }
}

function showArchView() { showDocView('team'); }

function switchDocTab(tab) {
    currentDocTab = tab;
    updateDocTabs();
    cancelArchEdit();
    const teamView = document.getElementById('team-view');
    const archContent = document.getElementById('arch-content');
    const archActions = document.getElementById('arch-actions');
    if (tab === 'team') {
        teamView.style.display = 'block';
        archContent.style.display = 'none';
        archActions.style.display = 'none';
        loadTeamView();
    } else {
        teamView.style.display = 'none';
        archContent.style.display = '';
        archActions.style.display = '';
        loadDoc();
    }
}

function updateDocTabs() {
    document.querySelectorAll('.doc-tab').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tab === currentDocTab);
    });
}

function hideArchView() {
    document.getElementById('arch-view').style.display = 'none';
    if (currentVersion) {
        document.getElementById('board-view').style.display = 'flex';
    } else {
        document.getElementById('welcome-view').style.display = 'flex';
    }
}

function cancelArchEdit() {}

// ==================== Commits ====================

async function loadCommits(rid) {
    const commits = await api('/api/requirements/' + rid + '/commits');
    const section = document.getElementById('commits-section');
    const list = document.getElementById('commits-list');
    section.style.display = '';

    if (!commits || commits.length === 0) {
        list.innerHTML = `
        <div style="border-left:3px solid var(--border);background:var(--bg2);border-radius:0 6px 6px 0;padding:8px 12px;font-size:11px;color:var(--text3)">
            暂无关联提交 — 此需求尚未纳入版本管理
        </div>`;
        return;
    }

    list.innerHTML = `
    <div class="commits-container" style="border-left:3px solid var(--primary);background:var(--bg2);border-radius:0 6px 6px 0;padding:8px 12px">
        <div style="font-size:11px;color:var(--text3);margin-bottom:6px">${commits.length} 次提交（点击查看 diff）</div>
        ${commits.map(c => {
            const hash = c.commit_hash.slice(0, 7);
            const date = c.committed_at ? c.committed_at.split('T')[0] : '';
            return `<div class="commit-row" style="cursor:pointer" onclick="toggleDiff('${c.commit_hash}', this)">
                <div style="display:flex;align-items:center;gap:8px;padding:5px 0;font-size:11px;border-top:1px solid var(--border)">
                    <code style="background:var(--bg3);padding:1px 4px;border-radius:3px;color:var(--primary);font-size:10px;flex-shrink:0">${hash}</code>
                    <span style="flex:1;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(c.message)}</span>
                    <span style="color:var(--success);flex-shrink:0">+${c.total_additions || 0}</span>
                    <span style="color:#f87171;flex-shrink:0">-${c.total_deletions || 0}</span>
                    <span style="color:var(--text3);flex-shrink:0;font-size:10px">${date}</span>
                    <span style="color:var(--text3);font-size:10px">▾</span>
                </div>
                <div class="diff-panel" style="display:none"></div>
            </div>`;
        }).join('')}
    </div>`;
}

async function toggleDiff(hash, el) {
    const panel = el.querySelector('.diff-panel');
    if (panel.style.display !== 'none') {
        panel.style.display = 'none';
        return;
    }
    if (panel.dataset.loaded) {
        panel.style.display = '';
        return;
    }
    panel.innerHTML = '<div style="padding:8px;color:var(--text3);font-size:11px">加载中...</div>';
    panel.style.display = '';
    try {
        const data = await api('/api/commits/' + hash + '/diff');
        panel.innerHTML = renderDiff(data.diff);
        panel.dataset.loaded = '1';
    } catch(e) {
        panel.innerHTML = '<div style="padding:8px;color:var(--danger);font-size:11px">加载失败: ' + escapeHtml(e.message) + '</div>';
    }
}

function renderDiff(diffText) {
    const lines = diffText.split('\n');
    let html = '<div class="diff-view" style="margin-top:6px;font-family:monospace;font-size:11px;line-height:1.6;max-height:400px;overflow:auto;background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:8px">';
    for (const line of lines) {
        if (line.startsWith('+++') || line.startsWith('---')) {
            html += `<div style="color:var(--text3);font-weight:600">${escapeHtml(line)}</div>`;
        } else if (line.startsWith('+')) {
            html += `<div style="background:rgba(34,197,94,.1);color:#4ade80">${escapeHtml(line)}</div>`;
        } else if (line.startsWith('-')) {
            html += `<div style="background:rgba(239,68,68,.1);color:#f87171">${escapeHtml(line)}</div>`;
        } else if (line.startsWith('@@')) {
            html += `<div style="color:var(--text3);background:var(--bg3);padding:2px 4px;margin:4px 0;border-radius:3px">${escapeHtml(line)}</div>`;
        } else if (line.startsWith('diff --git')) {
            html += `<div style="color:var(--primary);font-weight:600;margin-top:8px;padding-top:6px;border-top:1px solid var(--border)">${escapeHtml(line)}</div>`;
        } else {
            html += `<div style="color:var(--text2)">${escapeHtml(line)}</div>`;
        }
    }
    html += '</div>';
    return html;
}

// ==================== Comments ====================

async function loadComments(rid) {
    const comments = await api('/api/requirements/' + rid + '/comments');
    renderComments(comments);
}

function renderComments(comments) {
    const list = document.getElementById('comment-list');
    const html = (!comments || comments.length === 0)
        ? '<div class="comment-empty">暂无评论</div>'
        : comments.map(c => `
        <div class="comment-item">
            <div class="comment-header">
                <span class="comment-author">${esc(c.author) || '系统'}</span>
                <span>${esc(c.created_at)}</span>
                <button onclick="event.stopPropagation();deleteComment(${c.id})" title="删除">&times;</button>
            </div>
            <div class="comment-body md-content">${renderRoleChat(c.content)}</div>
        </div>
    `).join('');
    list.innerHTML = html;
}

async function addComment() {
    const input = document.getElementById('comment-input');
    const content = input.value.trim();
    if (!content || !editingReqId) return;
    await api('/api/requirements/' + editingReqId + '/comments', {
        method: 'POST',
        body: {content, author: ''}
    });
    input.value = '';
    await loadComments(editingReqId);
}

async function deleteComment(cid) {
    if (!confirm('确定删除此评论？')) return;
    await api('/api/comments/' + cid, {method: 'DELETE'});
    if (editingReqId) await loadComments(editingReqId);
}

// ==================== Helpers ====================

async function refreshVersions() {
    if (currentProject) {
        await loadVersions(currentProject.id);
        if (currentVersion) {
            currentVersion = versions.find(v => v.id === currentVersion.id);
        }
    }
    await loadProjects();
    updateStats();
}

function updateStats() {
    const el = document.getElementById('sidebar-stats');
    if (!currentProject) { el.innerHTML = ''; return; }
    const total = versions.reduce((s, v) => s + (v.req_count || 0), 0);
    const done = versions.reduce((s, v) => s + (v.done_count || 0), 0);
    el.innerHTML = '需求: ' + done + '/' + total + ' 完成 | 版本: ' + versions.length;
}

async function markReleased(vid) {
    await api('/api/versions/' + vid, {method: 'PUT', body: {status: 'released'}});
    await loadVersions(currentProject.id);
    if (currentVersion && currentVersion.id === vid) {
        currentVersion = versions.find(v => v.id === vid);
    }
    renderProjectOverview();
    showToast('已标记为已发布');
}

function hideModal(id) {
    document.getElementById(id).classList.add('hidden');
    if (id === 'req-modal') {
        document.getElementById('req-desc-preview').classList.add('hidden');
        document.getElementById('req-desc').style.display = '';
        document.getElementById('btn-desc-preview').textContent = '预览';
        const modal = document.querySelector('#req-modal .modal');
        if (modal.classList.contains('fullscreen')) {
            modal.classList.remove('fullscreen');
            document.getElementById('btn-fullscreen').innerHTML = '&#x26F6;';
        }
        editingReqId = null;
        updateHash();
    }
}

// Fullscreen toggle — expands modal to full viewport with same tab layout
function toggleFullscreen() {
    const modal = document.querySelector('#req-modal .modal');
    const btn = document.getElementById('btn-fullscreen');
    modal.classList.toggle('fullscreen');
    const isFs = modal.classList.contains('fullscreen');
    btn.innerHTML = isFs ? '&#x21A9;' : '&#x26F6;';
}

function toggleDescPreview() {
    const textarea = document.getElementById('req-desc');
    const preview = document.getElementById('req-desc-preview');
    const btn = document.getElementById('btn-desc-preview');
    const showing = !preview.classList.contains('hidden');
    if (showing) {
        preview.classList.add('hidden');
        textarea.style.display = '';
        btn.textContent = '预览';
    } else {
        preview.innerHTML = renderMd(textarea.value) || '<span style="color:var(--text3)">无内容</span>';
        preview.classList.remove('hidden');
        textarea.style.display = 'none';
        btn.textContent = '编辑';
    }
}

function updateDescPreview() {
    const preview = document.getElementById('req-desc-preview');
    if (!preview.classList.contains('hidden')) {
        preview.innerHTML = renderMd(document.getElementById('req-desc').value) || '<span style="color:var(--text3)">无内容</span>';
    }
}

function closeModal(id, e) {
    if (e.target === document.getElementById(id)) hideModal(id);
}

document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
        const modal = document.querySelector('#req-modal .modal');
        if (modal && modal.classList.contains('fullscreen')) {
            toggleFullscreen();
            return;
        }
        ['project-modal', 'version-modal', 'req-modal'].forEach(id => hideModal(id));
    }
});

function copyCode(code) {
    if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(code).then(() => showToast(code + ' 已复制')).catch(() => copyFallback(code));
    } else {
        copyFallback(code);
    }
}

function copyFallback(code) {
    const ta = document.createElement('textarea');
    ta.value = code;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    showToast(code + ' 已复制');
}

function showToast(msg) {
    let t = document.getElementById('copy-toast');
    if (!t) {
        t = document.createElement('div');
        t.id = 'copy-toast';
        document.body.appendChild(t);
    }
    t.textContent = msg;
    t.classList.add('show');
    setTimeout(() => t.classList.remove('show'), 1500);
}

async function jumpToCode(code) {
    const input = document.getElementById('search-input');
    let val = (code || input.value).trim();
    if (!val) return;
    if (/^\d+$/.test(val)) {
        const prefix = currentProject ? currentProject.prefix : null;
        if (prefix) val = prefix + '-' + val.padStart(3, '0');
        else { showToast('请先选择项目'); return; }
    }
    try {
        const req = await api('/api/requirements/by-code/' + encodeURIComponent(val));
        input.value = '';
        await navigateToRequirement(req);
    } catch(e) {
        showToast('未找到 ' + val);
    }
}

async function navigateToRequirement(req) {
    const pid = req.project_id;
    const vid = req.version_id;
    if (!currentProject || currentProject.id !== pid) {
        await selectProject(pid);
    }
    if (!currentVersion || currentVersion.id !== vid) {
        await selectVersion(vid);
    }
    if (!requirements.find(r => r.id === req.id)) {
        requirements.push(req);
    }
    showReqModal(req.id);
}

async function initApp() {
    await loadProjects();
    const urlParams = new URLSearchParams(window.location.search);
    const codeParam = urlParams.get('code');
    if (codeParam) {
        try {
            const req = await api('/api/requirements/by-code/' + encodeURIComponent(codeParam));
            await navigateToRequirement(req);
            return;
        } catch(e) {
            showToast('未找到 ' + codeParam);
        }
    }
    restoreChatPanel();
    const params = parseHash();
    if (params.p) {
        const pid = parseInt(params.p);
        if (projects.find(p => p.id === pid)) {
            await selectProject(pid);
            if (params.v) {
                const vid = parseInt(params.v);
                if (versions.find(v => v.id === vid)) {
                    await selectVersion(vid);
                    if (params.view && params.view !== 'board') {
                        switchView(params.view);
                    }
                    if (params.r) {
                        const rid = parseInt(params.r);
                        if (requirements.find(x => x.id === rid)) {
                            showReqModal(rid);
                        } else {
                            try {
                                const data = await api('/api/requirements/' + rid);
                                requirements.push(data);
                                showReqModal(rid);
                            } catch(e) {}
                        }
                    }
                }
            }
        }
    }
}

initApp();

// ==================== Activity Bar & Scheduler Control ====================

let schedulerMode = 'running';
let activityInterval = null;
let timerInterval = null;
let activitySessionStart = null;

async function pollActivity() {
    try {
        const [status, sessions] = await Promise.all([
            api('/api/scheduler/status'),
            api('/api/agents/sessions'),
        ]);
        updateSchedulerToggle(status.mode);
        updateActivityInfo(sessions, status);
    } catch(e) {}
}

function updateSchedulerToggle(mode) {
    schedulerMode = mode;
    const btn = document.getElementById('scheduler-toggle');
    const bar = document.getElementById('activity-bar');
    if (mode === 'paused') {
        btn.className = 'scheduler-toggle paused';
        btn.querySelector('.toggle-label').textContent = '已暂停';
        bar.classList.add('paused');
    } else {
        btn.className = 'scheduler-toggle running';
        btn.querySelector('.toggle-label').textContent = 'AI 运行中';
        bar.classList.remove('paused');
    }
}

function updateActivityInfo(sessions, status) {
    const info = document.getElementById('activity-info');
    const running = sessions.find(s => s.status === 'running');

    if (running) {
        const startTime = new Date(running.started_at.replace(' ', 'T'));
        activitySessionStart = startTime;
        const timeout = running.timeout_seconds || 600;
        let ctx = {};
        try { ctx = JSON.parse(running.input_context || '{}'); } catch(e) {}
        const taskTitle = ctx.title || ctx.code || running.agent_role;
        const role = running.agent_role === 'coach_dev' ? 'Coach-Dev' : running.agent_role;

        info.innerHTML = `
            <div class="activity-running">
                <span class="activity-role">${esc(role)}</span>
                <span class="activity-task">${esc(taskTitle)}</span>
                <span class="activity-timer" id="activity-timer"></span>
                <div class="activity-progress"><div class="activity-progress-fill" id="activity-progress-fill"></div></div>
            </div>`;
        updateTimer(timeout);
        if (!timerInterval) {
            timerInterval = setInterval(() => updateTimer(timeout), 1000);
        }
    } else {
        activitySessionStart = null;
        if (timerInterval) { clearInterval(timerInterval); timerInterval = null; }
        const recent = sessions.find(s => s.status === 'completed' || s.status === 'failed');
        if (recent) {
            let ctx = {};
            try { ctx = JSON.parse(recent.input_context || '{}'); } catch(e) {}
            const label = ctx.code || recent.agent_role;
            const ago = timeAgo(recent.completed_at);
            const icon = recent.status === 'completed' ? '✓' : '✗';
            info.innerHTML = `<span class="activity-idle">AI 团队空闲</span><span class="activity-history">${icon} ${esc(label)} ${ago}</span>`;
        } else {
            info.innerHTML = '<span class="activity-idle">AI 团队空闲</span>';
        }
    }
}

function updateTimer(timeout) {
    if (!activitySessionStart) return;
    const elapsed = Math.floor((Date.now() - activitySessionStart.getTime()) / 1000);
    const mins = Math.floor(elapsed / 60);
    const secs = elapsed % 60;
    const timerEl = document.getElementById('activity-timer');
    if (timerEl) timerEl.textContent = `${mins}:${String(secs).padStart(2, '0')}`;
    const progressEl = document.getElementById('activity-progress-fill');
    if (progressEl) {
        const pct = Math.min((elapsed / timeout) * 100, 100);
        progressEl.style.width = pct + '%';
        progressEl.classList.toggle('warning', pct > 80);
    }
}

function timeAgo(dateStr) {
    if (!dateStr) return '';
    const d = new Date(dateStr.replace(' ', 'T'));
    const diff = Math.floor((Date.now() - d.getTime()) / 1000);
    if (diff < 60) return '刚刚';
    if (diff < 3600) return Math.floor(diff / 60) + '分钟前';
    if (diff < 86400) return Math.floor(diff / 3600) + '小时前';
    return Math.floor(diff / 86400) + '天前';
}

async function toggleScheduler() {
    try {
        if (schedulerMode === 'paused') {
            await api('/api/scheduler/resume', {method: 'POST'});
        } else {
            await api('/api/scheduler/pause', {method: 'POST'});
        }
        await pollActivity();
    } catch(e) {}
}

pollActivity();
activityInterval = setInterval(pollActivity, 5000);

// ==================== Chat Panel ====================
function toggleChat() {
    const panel = document.getElementById('chat-panel');
    const isHidden = panel.style.display === 'none';
    panel.style.display = isHidden ? 'flex' : 'none';
    localStorage.setItem('kh_chat_open', isHidden ? '1' : '0');
    if (isHidden) {
        updateChatHeader();
        loadChatHistory();
    }
}

function restoreChatPanel() {
    if (localStorage.getItem('kh_chat_open') === '1') {
        const panel = document.getElementById('chat-panel');
        panel.style.display = 'flex';
        updateChatHeader();
        loadChatHistory();
    }
}

function updateChatHeader() {
    const label = document.getElementById('chat-project-label');
    if (currentProject) {
        label.textContent = currentProject.name;
    } else {
        label.textContent = '未选择项目';
    }
}

async function loadChatHistory() {
    if (!currentProject) return;
    const messages = document.getElementById('chat-messages');
    if (messages.dataset.loaded === String(currentProject.id)) return;
    try {
        const data = await api('/api/chat/history?project_id=' + currentProject.id + '&limit=30');
        if (data.messages && data.messages.length > 0) {
            messages.innerHTML = data.messages.map(m => {
                if (m.role === 'user') {
                    return `<div class="chat-msg user">${escapeHtml(m.content)}</div>`;
                } else {
                    return `<div class="chat-msg assistant">${renderMarkdown(m.content)}</div>`;
                }
            }).join('');
            // 如果最后一条是 user 消息（AI 可能还在后台处理），显示提示
            const last = data.messages[data.messages.length - 1];
            if (last.role === 'user') {
                messages.innerHTML += `<div class="chat-msg assistant"><div class="chat-thinking-hint">AI 可能仍在后台处理中，稍后刷新可查看回复</div></div>`;
            }
            messages.scrollTop = messages.scrollHeight;
        } else {
            messages.innerHTML = '';
        }
        messages.dataset.loaded = String(currentProject.id);
    } catch(e) {}
}

async function clearChatHistory() {
    if (!currentProject) return;
    if (!confirm('清除当前项目的所有对话历史？')) return;
    await api('/api/chat/history?project_id=' + currentProject.id, {method: 'DELETE'});
    document.getElementById('chat-messages').innerHTML = '';
    document.getElementById('chat-messages').dataset.loaded = '';
}

function autoGrowInput(el) {
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 120) + 'px';
}

async function sendChat() {
    const input = document.getElementById('chat-input');
    const msg = input.value.trim();
    if (!msg) return;
    input.value = '';

    const messages = document.getElementById('chat-messages');
    messages.innerHTML += `<div class="chat-msg user">${escapeHtml(msg)}</div>`;
    const assistantDiv = document.createElement('div');
    assistantDiv.className = 'chat-msg assistant';
    assistantDiv.innerHTML = '<div class="chat-thinking"><div class="thinking-dots"><span></span><span></span><span></span></div><span class="thinking-label">连接中...</span></div>';
    messages.appendChild(assistantDiv);
    messages.scrollTop = messages.scrollHeight;

    const ROLE_AVATARS_CHAT = {
        pm: '/static/avatars/pm_avatar.png',
        coach_dev: '/static/avatars/coach_dev_avatar.png',
        coach_review: '/static/avatars/coach_review_avatar.png',
        industry: '/static/avatars/industry_avatar.png',
    };
    const ROLE_LABELS_CHAT = {pm: '产品经理', coach_dev: 'Coach-Dev', coach_review: 'Coach-Review', industry: '行业顾问'};

    let currentRole = 'pm';
    let statusText = '等待回复';
    let thinkingStart = Date.now();
    let thinkingTimer = setInterval(() => {
        const el = assistantDiv.querySelector('.thinking-elapsed');
        if (el) {
            const sec = Math.floor((Date.now() - thinkingStart) / 1000);
            el.textContent = sec + 's';
        }
    }, 1000);

    function renderWaiting() {
        const avatar = ROLE_AVATARS_CHAT[currentRole] || ROLE_AVATARS_CHAT.pm;
        const roleName = ROLE_LABELS_CHAT[currentRole] || 'PM';
        assistantDiv.innerHTML = `<div class="chat-thinking-persona">
            <img class="thinking-avatar" src="${avatar}" alt="${escapeHtml(roleName)}">
            <div class="thinking-body">
                <div class="thinking-header"><span class="thinking-role">${escapeHtml(roleName)}</span><div class="thinking-dots"><span></span><span></span><span></span></div><span class="thinking-elapsed"></span></div>
                <div class="thinking-status">${escapeHtml(statusText)}</div>
            </div>
        </div>`;
    }

    try {
        const resp = await fetch(API + '/api/chat/stream', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({message: msg, project_id: currentProject?.id || 0}),
        });
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let fullText = '';
        let gotText = false;

        while (true) {
            const {done, value} = await reader.read();
            if (done) break;
            const chunk = decoder.decode(value, {stream: true});
            for (const line of chunk.split('\n')) {
                if (!line.startsWith('data: ')) continue;
                try {
                    const data = JSON.parse(line.slice(6));
                    if (data.type === 'text') {
                        if (!gotText) {
                            gotText = true;
                            clearInterval(thinkingTimer);
                            thinkingTimer = null;
                            assistantDiv.innerHTML = '';
                        }
                        fullText += data.content;
                        assistantDiv.innerHTML = renderMarkdown(fullText);
                    } else if (data.type === 'route') {
                        currentRole = data.role || 'pm';
                        statusText = '已接管';
                        renderWaiting();
                    } else if (data.type === 'status') {
                        if (data.state === 'waiting') {
                            const ctx = data.context || {};
                            const parts = [];
                            if (ctx.project) parts.push(ctx.project);
                            if (ctx.cards) parts.push(ctx.cards + '张卡片');
                            statusText = parts.length ? '已加载 ' + parts.join(' · ') + '，等待回复' : '等待回复';
                            renderWaiting();
                        }
                    } else if (data.type === 'thinking') {
                        statusText = '等待回复';
                        renderWaiting();
                    } else if (data.type === 'tool_start') {
                        if (!gotText) {
                            statusText = '⚙ ' + data.name;
                            renderWaiting();
                        } else {
                            assistantDiv.innerHTML = renderMarkdown(fullText) + `<div class="chat-tool-indicator">⚙ 执行 ${escapeHtml(data.name)}...</div>`;
                        }
                    } else if (data.type === 'tool_done') {
                        if (gotText) {
                            assistantDiv.innerHTML = renderMarkdown(fullText);
                        }
                    } else if (data.type === 'error') {
                        clearInterval(thinkingTimer);
                        thinkingTimer = null;
                        assistantDiv.innerHTML = `<span style="color:var(--danger)">${escapeHtml(data.content)}</span>`;
                    }
                } catch(e) {}
            }
            messages.scrollTop = messages.scrollHeight;
        }
    } catch(e) {
        assistantDiv.innerHTML = `<span style="color:var(--danger)">连接失败: ${escapeHtml(e.message)}</span>`;
    } finally {
        if (thinkingTimer) clearInterval(thinkingTimer);
        // AI 可能通过 MCP 工具修改了看板数据，自动刷新
        refreshBoardAfterChat();
    }
}

async function refreshBoardAfterChat() {
    try {
        if (currentVersion) {
            await loadRequirements(currentVersion.id);
            renderRequirements();
        }
        await refreshVersions();
    } catch(e) {}
}

function escapeHtml(s) {
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function renderMarkdown(text) {
    if (typeof marked !== 'undefined' && typeof DOMPurify !== 'undefined') {
        return '<div class="md-preview">' + DOMPurify.sanitize(marked.parse(text), {ADD_TAGS: ['input'], ADD_ATTR: ['type', 'checked', 'disabled']}) + '</div>';
    }
    return '<pre>' + escapeHtml(text) + '</pre>';
}

// ==================== Team View (Agent Role Panels) ====================

let teamDataCache = null;

async function loadTeamView() {
    try {
        const data = await api('/api/agents/status');
        teamDataCache = data.agents;
        renderTeamWorkflow();
        renderTeamGrid(data.agents);
    } catch(e) {
        document.getElementById('team-grid').innerHTML = '<div class="arch-empty">无法加载团队状态</div>';
    }
}

function renderTeamWorkflow() {
    const el = document.getElementById('team-workflow');
    el.innerHTML = `
    <div class="workflow-diagram">
        <div class="workflow-principle">
            <span class="wf-principle-icon">⚡</span>
            <span class="wf-principle-text">核心原则：移动必评论，评论后要移动</span>
        </div>
        <div class="workflow-title">调研链（需求发现）</div>
        <div class="workflow-research">
            <div class="wf-node wf-user">用户原话<span class="wf-sub">首条评论，描述可为空</span></div>
            <div class="wf-arrow-comment">💬→</div>
            <div class="wf-node wf-pm">PM 评论<span class="wf-sub">指派调研方向</span></div>
            <div class="wf-arrow-move">📦→ research</div>
            <div class="wf-node wf-industry">Industry 调研<span class="wf-sub">联网搜索+分析</span></div>
            <div class="wf-arrow-comment">💬→</div>
            <div class="wf-node wf-pm">PM 审核<span class="wf-sub">research→pending</span></div>
        </div>
        <div class="workflow-title">开发链（需求交付）</div>
        <div class="workflow-main">
            <div class="wf-node wf-pm">PM 分配<span class="wf-sub">pending→dev</span></div>
            <div class="wf-arrow-move">📦→ dev</div>
            <div class="wf-node wf-dev">Dev 开发<span class="wf-sub">编码+commit</span></div>
            <div class="wf-arrow-move">📦→ testing</div>
            <div class="wf-node wf-qa">QA 验收<span class="wf-sub">testing→done</span></div>
            <div class="wf-arrow-move">📦→ done</div>
            <div class="wf-node wf-done">完成</div>
        </div>
        <div class="workflow-escalation">
            <div class="wf-esc-title">升级裁决链</div>
            <div class="wf-esc-flow">
                <span class="wf-esc-step">Dev 发现问题</span>
                <span class="wf-esc-arrow">→</span>
                <span class="wf-esc-step">退回 PM<span class="wf-esc-sub">dev→pending</span></span>
                <span class="wf-esc-arrow">→</span>
                <span class="wf-esc-step">PM 修改重推<span class="wf-esc-sub">pending→dev</span></span>
                <span class="wf-esc-arrow">→</span>
                <span class="wf-esc-step wf-esc-blocked">仍有分歧→阻塞<span class="wf-esc-sub">dev→blocked</span></span>
                <span class="wf-esc-arrow">→</span>
                <span class="wf-esc-step wf-esc-ceo">CEO 裁决</span>
            </div>
        </div>
    </div>`;
}

function renderTeamGrid(agents) {
    const grid = document.getElementById('team-grid');
    const AVATAR_MAP = {
        pm: '/static/avatars/pm_avatar.png',
        industry: '/static/avatars/industry_avatar.png',
        coach_dev: '/static/avatars/coach_dev_avatar.png',
        coach_review: '/static/avatars/coach_review_avatar.png',
    };
    let html = '';
    for (const [role, info] of Object.entries(agents)) {
        const statusClass = info.status === 'running' ? 'working' : 'idle';
        const statusLabel = info.status === 'running' ? '工作中' : '空闲';
        const avatarSrc = AVATAR_MAP[role] || info.avatar || '';
        const lastActivity = info.last_run ? timeAgo(info.last_run) : '暂无活动';
        const TOOL_LABELS = {
            'kanban_get_requirement': '读卡片',
            'kanban_list_requirements': '列需求',
            'kanban_list_comments': '看评论',
            'kanban_add_comment': '写评论',
            'kanban_create_requirements': '建卡片',
            'kanban_update_requirement': '改需求',
            'kanban_move_requirement': '移卡片',
            'kanban_list_commits': '看提交',
            'web_search': '联网搜索',
            'web_fetch': '抓取网页',
            'Bash': '终端',
            'Edit': '改文件',
            'Read': '读文件',
            'Write': '写文件',
        };
        const COMMON_TOOLS = ['kanban_get_requirement', 'kanban_list_comments', 'kanban_add_comment'];
        const uniqueTools = (info.allowed_tools || []).filter(t => !COMMON_TOOLS.includes(t));
        const commonTools = (info.allowed_tools || []).filter(t => COMMON_TOOLS.includes(t));
        const toolsHtml = [...uniqueTools, ...commonTools].map(t =>
            `<span class="tool-tag${COMMON_TOOLS.includes(t) ? ' common' : ''}">${esc(TOOL_LABELS[t] || t)}</span>`
        ).join('');
        const moves = (info.permissions?.can_move || []).map(m => m.replace('research', '调研').replace('pending', '待办').replace('dev', '开发').replace('testing', '测试').replace('done', '完成').replace('blocked', '阻塞').replace('->', ' → ')).join(' · ') || '—';
        const TRIGGER_LABELS = {
            'requirement_created': '新需求触发',
            'status_changed': '状态变更触发',
            'scheduled': '定期巡查',
        };
        const triggersHtml = (info.triggers || []).map(t =>
            `<span class="tool-tag common">${esc(TRIGGER_LABELS[t] || t)}</span>`
        ).join('');

        const MOVE_EXPLAIN = {
            'pending->dev': '分配任务给开发',
            'dev->testing': '提交代码送测',
            'dev->pending': '退回需求给PM',
            'dev->blocked': '请求CEO裁决',
            'testing->done': '验收通过完成',
            'testing->dev': '打回修改',
            'blocked->pending': '解除阻塞重排',
        };
        const STATUS_ROLE = {
            'pending': 'pm',
            'dev': 'coach_dev',
            'testing': 'coach_review',
            'done': '',
            'blocked': '',
        };
        const movesHtml = (info.permissions?.can_move || []).map(m => {
            const parts = m.split('->');
            const fromStatus = parts[0];
            const toStatus = parts[1];
            const fromLabel = fromStatus.replace('research', '调研').replace('pending', '待办').replace('dev', '开发').replace('testing', '测试').replace('done', '完成').replace('blocked', '阻塞');
            const toLabel = toStatus.replace('research', '调研').replace('pending', '待办').replace('dev', '开发').replace('testing', '测试').replace('done', '完成').replace('blocked', '阻塞');
            const explain = MOVE_EXPLAIN[m] || '';
            const fromRole = STATUS_ROLE[fromStatus] || '';
            const toRole = STATUS_ROLE[toStatus] || '';
            const fromAvatar = AVATAR_MAP[fromRole] || '';
            const toAvatar = AVATAR_MAP[toRole] || '';
            return `<div class="move-item">
                ${fromAvatar ? `<img class="move-avatar" src="${fromAvatar}">` : '<span class="move-avatar-placeholder"></span>'}
                <span class="move-label">${esc(fromLabel)}</span>
                <span class="move-arrow-icon">→</span>
                ${toAvatar ? `<img class="move-avatar" src="${toAvatar}">` : '<span class="move-avatar-placeholder"></span>'}
                <span class="move-label">${esc(toLabel)}</span>
                <span class="move-explain">${esc(explain)}</span>
            </div>`;
        }).join('') || '<div class="move-item"><span class="move-explain">仅评论，不流转卡片</span></div>';

        html += `
        <div class="agent-persona" style="--agent-color: ${esc(info.color)}">
            <div class="agent-persona-img" data-role="${esc(role)}">
                <img src="${esc(avatarSrc)}" alt="${esc(info.display_name)}" draggable="false">
                <span class="agent-status-dot ${statusClass}"></span>
            </div>
            <div class="agent-persona-info">
                <span class="agent-persona-name">${esc(info.display_name)}</span>
                <span class="agent-persona-desc">${esc(info.description)}</span>
                <div class="agent-tools">${toolsHtml}${triggersHtml}</div>
                <details class="agent-moves-details">
                    <summary class="agent-moves-summary">流转权限</summary>
                    <div class="agent-moves">${movesHtml}</div>
                </details>
                <span class="agent-persona-activity">${statusLabel} · ${lastActivity}</span>
            </div>
        </div>`;
    }
    grid.innerHTML = html;
}

function truncate(str, len) {
    if (!str) return '';
    return str.length > len ? str.substring(0, len) + '...' : str;
}

// ==================== Reigns-style CEO Decision System ====================

let _pendingDecisions = [];
let _currentDecision = null;

async function pollDecisions() {
    try {
        const resp = await fetch('/api/decisions/pending');
        if (!resp.ok) return;
        const data = await resp.json();
        _pendingDecisions = data.decisions || [];
        updateDecisionBadges();
    } catch (e) { /* silent */ }
}

function updateDecisionBadges() {
    // Remove existing badges
    document.querySelectorAll('.decision-badge').forEach(b => b.remove());

    // Map asking_role to the kanban column status where their avatar lives
    const roleToStatus = { pm: 'pending', industry: 'research', coach_dev: 'dev', coach_review: 'testing' };

    // Group decisions by asking_role
    const byRole = {};
    for (const d of _pendingDecisions) {
        const role = d.asking_role || 'pm';
        if (!byRole[role]) byRole[role] = [];
        byRole[role].push(d);
    }

    // Add badges to column avatars in the board view
    for (const [role, decisions] of Object.entries(byRole)) {
        const status = roleToStatus[role];
        if (!status) continue;

        // Find the col-wrapper for this status column, then its .col-avatar
        const wrappers = document.querySelectorAll('.col-wrapper');
        for (const wrapper of wrappers) {
            const col = wrapper.querySelector(`.board-col[data-status="${status}"]`);
            const avatar = wrapper.querySelector('.col-avatar');
            if (col && avatar) {
                wrapper.style.position = 'relative';
                const badge = document.createElement('span');
                badge.className = 'decision-badge';
                badge.textContent = '?';
                badge.title = `${decisions.length} 项待决策`;
                badge.onclick = (e) => { e.stopPropagation(); openDecision(decisions[0]); };
                wrapper.appendChild(badge);
                break;
            }
        }
    }

    // Also show in activity bar if there are pending decisions
    const fab = document.getElementById('chat-fab');
    if (_pendingDecisions.length > 0 && fab) {
        fab.setAttribute('data-decisions', _pendingDecisions.length);
    } else if (fab) {
        fab.removeAttribute('data-decisions');
    }
}

async function openDecision(decision) {
    _currentDecision = decision;
    const overlay = document.getElementById('decision-overlay');

    // Set avatar and role info
    const avatarMap = {
        pm: '/static/avatars/pm_avatar.png',
        industry: '/static/avatars/industry_avatar.png',
        coach_dev: '/static/avatars/coach_dev_avatar.png',
        coach_review: '/static/avatars/coach_review_avatar.png',
    };
    const roleNames = { pm: '产品经理', industry: '行业顾问', coach_dev: 'Coach-Dev', coach_review: 'Coach-QA' };
    const roleTitles = { pm: '需求拆解 · 优先级排序', industry: '行业分析 · 竞品调研', coach_dev: '代码实现 · 技术方案', coach_review: '质量保障 · 测试验证' };

    const role = decision.asking_role || 'pm';
    document.getElementById('decision-avatar').src = avatarMap[role] || avatarMap.pm;
    document.getElementById('decision-role-name').textContent = roleNames[role] || role;
    document.getElementById('decision-role-title').textContent = roleTitles[role] || '';

    // Build plain-language speech from PM summary
    const speech = buildSpeech(decision);
    document.getElementById('decision-speech').innerHTML = speech;

    // Build action buttons
    const actions = document.getElementById('decision-actions');
    actions.innerHTML = buildActionButtons(decision);

    // Load card detail on the right
    await loadDecisionCard(decision);

    // Clear input
    document.getElementById('decision-input').value = '';

    // Show overlay
    overlay.classList.add('active');
}

function buildSpeech(decision) {
    let summary = decision.pm_summary || '';
    summary = summary.replace(/^\*\*\[产品经理\]:\*\*\s*/m, '');
    summary = summary.replace(/^\[产品经理\]\s*/m, '');

    // Don't do regex replacement on the original text.
    // Instead, use LLM-style rewriting: extract the core message and present it like someone talking.
    // Since we can't call LLM here, we do structural extraction.

    // Extract key facts (bullet points starting with -)
    const bulletLines = summary.split('\n').filter(l => /^\s*-\s/.test(l)).map(l => l.replace(/^\s*-\s*/, '').replace(/\*\*/g, '').trim());

    // Extract the "risk" or "decision needed" part
    const riskMatch = summary.match(/(?:遗留风险|需.*?决策|需要.*?确认)[^]*?(?=\n\*\*|\n\d+\.|$)/s);
    const riskText = riskMatch ? riskMatch[0].replace(/\*\*/g, '').replace(/[（(]已知[^)）]*[)）]/g, '').trim() : '';

    // Extract options (路线/方向)
    const optLines = summary.split('\n').filter(l => /^\s*\d+\.\s*(路线|方向|方案)/.test(l)).map(l => l.replace(/\*\*/g, '').trim());

    // Build conversational output
    let chat = '';

    if (bulletLines.length > 0) {
        chat += '调研做完了，几个关键发现：\n\n';
        bulletLines.slice(0, 4).forEach(b => { chat += `• ${b}\n`; });
        chat += '\n';
    }

    if (riskText) {
        chat += '有个事得你来定——' + riskText.split('\n')[0].replace(/[：:：]$/, '') + '。';
        if (riskText.split('\n').length > 1) {
            chat += riskText.split('\n').slice(1).join('\n');
        }
        chat += '\n\n';
    }

    if (optLines.length > 0) {
        chat += '我这边整理了几个方向，你看走哪个：\n\n';
        optLines.forEach(l => { chat += l + '\n'; });
    }

    // Fallback: if extraction got nothing useful, just clean up the original
    if (!chat.trim()) {
        chat = summary
            .replace(/\*\*[^*]+\*\*/g, (m) => m.replace(/\*\*/g, ''))
            .replace(/#{1,3}\s*/g, '');
    }

    if (typeof marked !== 'undefined') {
        return DOMPurify.sanitize(marked.parse(chat));
    }
    return escapeHtml(chat).replace(/\n/g, '<br>');
}

function buildActionButtons(decision) {
    // Parse options from PM's summary — look for numbered routes/options
    const summary = decision.pm_summary || '';
    const optionPattern = /(\d+)\.\s*\*?\*?路线([A-Z])[^：:]*[：:]\*?\*?\s*(.+?)(?=\n\d+\.|\n\*\*|$)/gs;
    const options = [];
    let m;
    while ((m = optionPattern.exec(summary)) !== null) {
        options.push({ key: m[2], text: m[3].trim().replace(/\*\*/g, '') });
    }

    // If no route pattern, try generic numbered options
    if (options.length === 0) {
        const genericPattern = /(\d+)\.\s*(.+?)(?=\n\d+\.|\n\*\*|$)/gs;
        let gm;
        while ((gm = genericPattern.exec(summary)) !== null) {
            const text = gm[2].trim().replace(/\*\*/g, '');
            if (text.length > 10 && text.length < 200) {
                options.push({ key: gm[1], text });
            }
        }
    }

    let html = '';

    if (options.length > 0) {
        // Render PM's proposed options as buttons
        const icons = ['⚡', '📱', '🛡️', '🔄', '💡'];
        options.forEach((opt, i) => {
            const cls = i === 0 ? 'primary' : '';
            html += `<button class="decision-btn ${cls}" onclick="submitDecision('approve_dev', '选择方案${opt.key || (i+1)}：${escapeHtml(opt.text)}')">
                <span class="decision-btn-icon">${icons[i] || '▸'}</span>
                <div class="decision-btn-text">${escapeHtml(opt.text)}</div>
            </button>`;
        });
    }

    // Always add "continue research" as fallback
    html += `<button class="decision-btn warning" onclick="submitDecision('request_more_research')">
        <span class="decision-btn-icon">🔍</span>
        <div class="decision-btn-text">都不满意，继续调研<div class="decision-btn-hint">退回行业顾问补充材料</div></div>
    </button>`;
    return html;
}

async function loadDecisionCard(decision) {
    const cardEl = document.getElementById('decision-card');
    cardEl.innerHTML = '<div style="color:var(--text3);padding:20px">加载中...</div>';

    try {
        const resp = await fetch(`/api/requirements/by-code/${decision.code}`);
        if (!resp.ok) throw new Error('load failed');
        const req = await resp.json();

        const cResp = await fetch(`/api/requirements/${decision.id}/comments`);
        const comments = cResp.ok ? await cResp.json() : [];

        const priorityColors = { P0: 'var(--danger)', P1: 'var(--warning)', P2: 'var(--info)', P3: 'var(--text3)' };
        const priorityBg = { P0: 'var(--danger-bg)', P1: 'var(--warning-bg)', P2: 'var(--info-bg)', P3: 'var(--bg4)' };

        const descHtml = renderMd(req.description || '');

        const recentComments = comments.slice(-8);
        let commentsHtml = recentComments.map(c => `
            <div class="comment-item">
                <div class="comment-header">
                    <span class="comment-author">${esc(c.author || '系统')}</span>
                    <span>${esc(c.created_at)}</span>
                </div>
                <div class="comment-body md-content">${renderRoleChat(c.content)}</div>
            </div>
        `).join('');

        cardEl.innerHTML = `
            <div class="decision-card-header">
                <span class="decision-card-code">${escapeHtml(decision.code)}</span>
                <span class="decision-card-priority" style="background:${priorityBg[req.priority]};color:${priorityColors[req.priority]}">${req.priority}</span>
                <span style="font-size:11px;color:var(--text3)">调研 ${decision.research_rounds} 轮</span>
            </div>
            <div class="decision-card-title">${escapeHtml(req.title)}</div>
            <div class="decision-card-tabs">
                <button class="dc-tab active" onclick="switchDecisionTab('desc', this)">需求描述</button>
                <button class="dc-tab" onclick="switchDecisionTab('comments', this)">讨论记录 (${comments.length})</button>
            </div>
            <div class="decision-card-panel active" id="dc-panel-desc">
                <div class="md-content">${descHtml}</div>
            </div>
            <div class="decision-card-panel" id="dc-panel-comments">
                <div class="comment-list">${commentsHtml}</div>
            </div>
        `;
    } catch (e) {
        cardEl.innerHTML = `<div style="color:var(--danger);padding:20px">加载失败: ${e.message}</div>`;
    }
}

function switchDecisionTab(tab, btn) {
    document.querySelectorAll('.dc-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.decision-card-panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('dc-panel-' + tab).classList.add('active');
}

async function submitDecision(decision, optionComment) {
    if (!_currentDecision) return;
    const inputComment = document.getElementById('decision-input').value.trim();
    const comment = optionComment || inputComment;

    try {
        const resp = await fetch(`/api/decisions/${_currentDecision.id}/submit`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ decision, comment }),
        });
        if (!resp.ok) throw new Error('submit failed');

        closeDecision();
        // Refresh board
        if (typeof loadRequirements === 'function') loadRequirements();
        // Re-poll decisions
        setTimeout(pollDecisions, 1000);
    } catch (e) {
        alert('决策提交失败: ' + e.message);
    }
}

async function submitCustomDecision() {
    if (!_currentDecision) return;
    const comment = document.getElementById('decision-input').value.trim();
    if (!comment) return;

    try {
        const resp = await fetch(`/api/decisions/${_currentDecision.id}/submit`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ decision: 'custom', comment }),
        });
        if (!resp.ok) throw new Error('submit failed');

        closeDecision();
        if (typeof loadRequirements === 'function') loadRequirements();
        setTimeout(pollDecisions, 1000);
    } catch (e) {
        alert('提交失败: ' + e.message);
    }
}

function closeDecision() {
    document.getElementById('decision-overlay').classList.remove('active');
    _currentDecision = null;
}

// ESC to close decision overlay
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && _currentDecision) {
        closeDecision();
    }
});

// Poll decisions every 10 seconds
setInterval(pollDecisions, 10000);
setTimeout(pollDecisions, 2000);
