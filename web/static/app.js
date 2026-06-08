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
    organizing: {label: '需求整理', color: '#6366f1', dot: '#818cf8'},
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
    const rolePattern = /\*\*\[([^\]]+?)\]\s*[：:]\s*\*\*/;
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

        if (i > 0 && i % 2 === 1) {
            html += `<div class="chat-section-title">${esc(part)}</div>`;
            continue;
        }

        const lines = part.split('\n');
        let bubbles = [];
        let currentRole = null;
        let currentTarget = null;
        let currentLines = [];

        for (const line of lines) {
            const m = line.match(/\*\*\[([^\]]+?)(?:\s*→\s*([^\]]+?))?\]\s*[：:]\s*\*\*(.*)/);
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
                const asksQ = b.text.includes('[需要补充]') || b.text.includes('[需补充]');
                html += `<div class="chat-bubble${asksQ ? ' asks' : ''}">
                    <div class="chat-avatar-wrap">
                        ${avatarUrl
                            ? `<img class="chat-avatar" src="${avatarUrl}" alt="${esc(b.role)}">`
                            : `<div class="chat-avatar" style="background:${color}">${esc(initial)}</div>`}
                    </div>
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
    document.getElementById('chat-fab').style.display = '';
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
    hideDoneDrawer();
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
        document.getElementById('chat-fab').style.display = 'none';
        document.getElementById('chat-panel').style.display = 'none';
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
    loadProductizationTarget();
}

async function loadProductizationTarget() {
    if (!currentProject) return;
    const badge = document.getElementById('prod-target-badge');
    try {
        const data = await api('/api/projects/' + currentProject.id + '/product-memory');
        const match = (data.content || '').match(/productization_target:\s*(L\d)/);
        if (match) {
            badge.textContent = '🎯 ' + match[1];
            badge.style.display = 'inline';
        } else {
            badge.style.display = 'none';
        }
    } catch(e) {
        badge.style.display = 'none';
    }
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
    if (!currentVersion) {
        hideDoneDrawer();
        renderProjectOverview();
    }
}

function hideDoneDrawer() {
    const drawer = document.getElementById('done-drawer');
    const tab = document.getElementById('done-tab');
    if (drawer) { drawer.classList.remove('open'); drawer.remove(); }
    if (tab) { tab.classList.remove('active'); tab.remove(); }
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
    if (!currentVersion) {
        hideDoneDrawer();
        renderProjectOverview();
    }
}

// ==================== Requirements ====================

async function loadRequirements(vid) {
    requirements = await api('/api/versions/' + vid + '/requirements');
}

const COL_ROLE_MAP = {
    research: {role: 'Industry', avatar: '/static/avatars/industry_avatar.png', agentKey: 'industry'},
    organizing: {role: 'PM', avatar: '/static/avatars/pm_avatar.png', agentKey: 'pm'},
    dev: {role: 'Coach-Dev', avatar: '/static/avatars/coach_dev_avatar.png', agentKey: 'coach_dev'},
    testing: {role: 'Coach-Review', avatar: '/static/avatars/coach_review_avatar.png', agentKey: 'coach_review'},
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
            const avatarWrap = document.createElement('div');
            avatarWrap.className = 'col-avatar-wrap';
            const avatar = document.createElement('img');
            avatar.className = 'col-avatar';
            avatar.src = roleInfo.avatar;
            avatar.alt = roleInfo.role;
            avatar.title = roleInfo.role;
            avatarWrap.appendChild(avatar);

            const runningSessions = (lastSchedulerState && lastSchedulerState.running) || [];
            const session = runningSessions.find(s => s.agent_role === roleInfo.agentKey);
            if (session) {
                const silent = session.silent_seconds;
                const stallTimeout = session.stall_timeout || 120;
                const ratio = silent / stallTimeout;
                const hbClass = ratio < 0.5 ? 'heartbeat-ok' : ratio < 0.75 ? 'heartbeat-warn' : 'heartbeat-danger';
                const badge = document.createElement('span');
                badge.className = `col-heartbeat ${hbClass}`;
                badge.textContent = `${silent}s`;
                badge.title = `心跳 ${silent}s 前 · ${session.card_code || ''}`;
                avatarWrap.appendChild(badge);
            }
            wrapper.appendChild(avatarWrap);
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
            waitingCards.forEach(r => {
                const cardEl = createCardEl(r);
                cardEl.title = getQueueReason(r);
                cardsEl.appendChild(cardEl);
            });
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

function getQueueReason(r) {
    if (r.queue_reason) return r.queue_reason;
    const defaults = {
        research: '等待行业顾问调研分析',
        organizing:  '等待 PM 需求整理',
        dev:      schedulerMode === 'paused' ? '调度器已暂停，待恢复后自动分配' : '等待 Coach-Dev 开发实现',
        testing:  '等待 Coach-Review 测试验收',
    };
    return defaults[r.status] || '排队等待中';
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
    const queueReasonHtml = r.queue_reason ? '<span class="queue-reason-badge" title="排队原因">' + esc(r.queue_reason) + '</span>' : '';

    const reviewBadge = r.reviewed === false ? '<span class="review-badge unreviewed">未审议</span>' : '';
    const typeBadge = r.type === 'research' ? '<span class="type-badge research">调研</span>' : '';

    el.innerHTML = `
        <div class="card-top">
            <span class="priority-badge ${r.priority}">${r.priority}</span>
            ${r.code ? '<span class="card-code copyable" onclick="event.stopPropagation();copyCode(\'' + esc(r.code) + '\')" title="点击复制">' + esc(r.code) + '</span>' : ''}
            ${typeBadge}
            ${reviewBadge}
            ${tagsHtml}
        </div>
        <div class="card-title">${esc(r.title)}</div>
        ${r.description ? '<div class="card-desc md-content">' + renderMd(r.description) + '</div>' : ''}
        <div class="card-bottom">
            <div class="card-meta">${assigneeHtml}${deadlineHtml}${hoursHtml}${attHtml}${queueReasonHtml}</div>
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
    document.getElementById('req-modal-title').textContent = editId ? '编辑需求' : '新的需求想法';

    if (editId) {
        // 编辑模式：显示完整表单，隐藏快速输入
        document.getElementById('req-quick-input').style.display = 'none';
        document.getElementById('req-full-form').style.display = '';
        switchReqTab('desc');
        const r = requirements.find(x => x.id === editId);
        document.getElementById('req-title').value = r.title;
        document.getElementById('req-desc').value = r.description || '';
        document.getElementById('req-priority').value = r.priority;
        document.getElementById('req-type').value = r.type || 'dev';
        document.getElementById('req-status').value = r.status;
        document.getElementById('req-assignee').value = r.assignee || '';
        // 编辑模式显示状态和负责人（只读参考）
        document.getElementById('req-status-group').style.display = '';
        document.getElementById('req-assignee').style.display = '';
        document.getElementById('req-deadline').value = r.deadline || '';
        document.getElementById('req-hours').value = r.estimated_hours || 0;
        document.getElementById('req-actual').value = r.actual_hours || 0;
        let tags = [];
        try { tags = JSON.parse(r.tags || '[]'); } catch(e) {}
        if (typeof tags === 'string') tags = tags.split(',').map(t => t.trim()).filter(Boolean);
        document.getElementById('req-tags').value = tags.join(', ');
        document.getElementById('req-notes').value = r.notes || '';
        document.getElementById('req-queue-reason').value = r.queue_reason || '';
        const preset = document.getElementById('req-queue-reason-preset');
        const qr = r.queue_reason || '';
        const presetLabels = {waiting_reply: '等待回复', waiting_dependency: '等待依赖完成', waiting_turn: '排队等待中', need_info: '缺信息，需补充', deferred: '推迟处理'};
        const matched = Object.entries(presetLabels).find(([k, v]) => v === qr);
        preset.value = matched ? matched[0] : '';
        renderAttachments(r.attachments || []);
        loadComments(editId);
        loadCommits(editId);
        loadCardLogs(editId);
        // Default to preview mode when opening existing card
        if (r.description) {
            const preview = document.getElementById('req-desc-preview');
            preview.innerHTML = renderMd(r.description);
            preview.classList.remove('hidden');
            document.getElementById('req-desc').style.display = 'none';
            document.getElementById('btn-desc-preview').textContent = '编辑';
        }
    } else {
        // 新建模式：显示快速输入，隐藏完整表单
        document.getElementById('req-quick-input').style.display = '';
        document.getElementById('req-full-form').style.display = 'none';
        document.getElementById('req-idea-input').value = '';
    }
    document.getElementById('req-modal').classList.remove('hidden');
    if (editId) {
        document.getElementById('req-title').focus();
    } else {
        document.getElementById('req-idea-input').focus();
    }
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
    // 新建模式：快速输入
    if (!editingReqId) {
        const idea = document.getElementById('req-idea-input').value.trim();
        if (!idea) { document.getElementById('req-idea-input').focus(); return; }
        const body = {
            version_id: currentVersion.id,
            title: '待 PM 整理',
            priority: document.getElementById('req-quick-priority').value,
            initial_comment: idea,
        };
        await api('/api/requirements', {method: 'POST', body});
        hideModal('req-modal');
        await loadRequirements(currentVersion.id);
        renderRequirements();
        await refreshVersions();
        return;
    }

    // 编辑模式：完整表单
    const title = document.getElementById('req-title').value.trim();
    if (!title) { document.getElementById('req-title').focus(); return; }

    const tagsRaw = document.getElementById('req-tags').value.trim();
    const tags = JSON.stringify(tagsRaw ? tagsRaw.split(',').map(t => t.trim()).filter(Boolean) : []);

    const body = {
        title,
        description: document.getElementById('req-desc').value.trim(),
        priority: document.getElementById('req-priority').value,
        type: document.getElementById('req-type').value,
        status: document.getElementById('req-status').value,
        assignee: document.getElementById('req-assignee').value.trim(),
        deadline: document.getElementById('req-deadline').value,
        estimated_hours: parseFloat(document.getElementById('req-hours').value) || 0,
        actual_hours: parseFloat(document.getElementById('req-actual').value) || 0,
        tags,
        notes: document.getElementById('req-notes').value.trim(),
        queue_reason: document.getElementById('req-queue-reason').value.trim()
    };

    await api('/api/requirements/' + editingReqId, {method: 'PUT', body});

    for (const f of pendingFiles) {
        const fd = new FormData();
        fd.append('file', f);
        await api('/api/requirements/' + editingReqId + '/attachments', {method: 'POST', body: fd});
    }
    pendingFiles = [];

    hideModal('req-modal');
    await loadRequirements(currentVersion.id);
    renderRequirements();
    await refreshVersions();
}

function onQueueReasonPresetChange(select) {
    const val = select.value;
    const input = document.getElementById('req-queue-reason');
    if (val) {
        const labels = {waiting_reply: '等待回复', waiting_dependency: '等待依赖完成', waiting_turn: '排队等待中', need_info: '缺信息，需补充', deferred: '推迟处理'};
        input.value = labels[val] || '';
    }
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
                ${t.organizing > 0 ? '<span class="tag-stat organizing">' + t.organizing + ' 需求整理</span>' : ''}
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

    const statusOrder = ['research', 'organizing', 'dev', 'testing', 'done'];
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
    if (window._teamPollInterval) {
        clearInterval(window._teamPollInterval);
        window._teamPollInterval = null;
    }
    switchDocTab(currentDocTab);
    if (currentDocTab === 'team' && !window._teamPollInterval) {
        window._teamPollInterval = setInterval(loadTeamView, 5000);
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
    const wikiView = document.getElementById('wiki-view');
    if (tab === 'team') {
        teamView.style.display = 'block';
        archContent.style.display = 'none';
        archActions.style.display = 'none';
        if (wikiView) wikiView.style.display = 'none';
        loadTeamView();
    } else if (tab === 'wiki') {
        teamView.style.display = 'none';
        archContent.style.display = 'none';
        archActions.style.display = 'none';
        if (wikiView) wikiView.style.display = 'block';
        loadWikiView();
    } else {
        teamView.style.display = 'none';
        archContent.style.display = '';
        archActions.style.display = '';
        if (wikiView) wikiView.style.display = 'none';
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
    currentView = 'board';
    document.getElementById('btn-board-mode').classList.add('active');
    document.getElementById('btn-arch-mode').classList.remove('active');
    if (currentVersion) {
        document.getElementById('board-view').style.display = '';
        document.getElementById('board-columns').style.display = 'flex';
    } else {
        document.getElementById('welcome-view').style.display = 'flex';
    }
}

function cancelArchEdit() {}

// ==================== Wiki ====================

async function loadWikiView() {
    if (!currentProject) return;
    const listEl = document.getElementById('wiki-page-list');
    const contentEl = document.getElementById('wiki-page-content');
    contentEl.innerHTML = '<div class="arch-empty">选择左侧页面查看内容</div>';

    const data = await api('/api/projects/' + currentProject.id + '/wiki');
    const pages = data.pages || [];

    if (!pages.length) {
        listEl.innerHTML = '<div class="wiki-empty">暂无 Wiki 页面<br><small>调研卡完成后会自动生成</small></div>';
        return;
    }

    let html = '';
    const grouped = {research: [], product: [], arch: []};
    pages.forEach(p => {
        if (grouped[p.subdir]) grouped[p.subdir].push(p);
    });

    const labels = {research: '📊 调研', product: '🎯 产品', arch: '🏗️ 架构'};
    for (const [dir, items] of Object.entries(grouped)) {
        if (!items.length) continue;
        html += '<div class="wiki-group"><div class="wiki-group-title">' + labels[dir] + '</div>';
        items.forEach(p => {
            const tags = p.tags && p.tags.length ? '<span class="wiki-tags">' + p.tags.join(', ') + '</span>' : '';
            html += '<div class="wiki-item" onclick="loadWikiPage(\'' + p.page + '\')">' +
                '<span class="wiki-item-title">' + p.title + '</span>' + tags + '</div>';
        });
        html += '</div>';
    }
    listEl.innerHTML = html;
}

async function loadWikiPage(pagePath) {
    if (!currentProject) return;
    const contentEl = document.getElementById('wiki-page-content');
    contentEl.innerHTML = '<div class="loading">加载中...</div>';

    try {
        const data = await api('/api/projects/' + currentProject.id + '/wiki/' + pagePath);
        contentEl.innerHTML = renderMd(data.content || '');
    } catch (e) {
        contentEl.innerHTML = '<div class="arch-empty">页面加载失败</div>';
    }

    // Highlight active item
    document.querySelectorAll('.wiki-item').forEach(el => el.classList.remove('active'));
    const items = document.querySelectorAll('.wiki-item');
    items.forEach(el => {
        if (el.getAttribute('onclick').includes(pagePath)) el.classList.add('active');
    });
}

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

async function loadCardLogs(rid) {
    const el = document.getElementById('card-log-list');
    if (!el) return;
    el.innerHTML = '<div style="color:#888;padding:8px">加载中...</div>';
    try {
        const resp = await fetch(`/api/requirements/${rid}/logs?limit=200`);
        const data = await resp.json();
        if (!data.logs || data.logs.length === 0) {
            el.innerHTML = '<div style="color:#888;padding:8px">暂无日志</div>';
            return;
        }
        const levelColors = {info: '#6b7280', warning: '#f59e0b', error: '#ef4444'};
        el.innerHTML = data.logs.map(log => {
            const color = levelColors[log.level] || '#6b7280';
            const src = log.source ? `<span style="color:#8b5cf6">[${log.source}]</span> ` : '';
            const ts = log.created_at ? log.created_at.slice(5, 19) : '';
            return `<div style="padding:3px 8px;border-bottom:1px solid #f3f4f6"><span style="color:#9ca3af">${ts}</span> <span style="color:${color};font-weight:500">${log.level.toUpperCase()}</span> ${src}${log.message}</div>`;
        }).join('');
    } catch(e) {
        el.innerHTML = `<div style="color:#ef4444;padding:8px">加载失败: ${e.message}</div>`;
    }
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
                ${c.detail ? `<button class="btn-detail" onclick="event.stopPropagation();toggleDetail(this,${c.id})">详情</button>` : ''}
                <button class="btn-download-md" onclick="event.stopPropagation();downloadCommentMd(${c.id})" title="下载 Markdown">⬇</button>
                <button onclick="event.stopPropagation();deleteComment(${c.id})" title="删除">&times;</button>
            </div>
            <div class="comment-body md-content">${renderRoleChat(c.content)}</div>
        </div>
    `).join('');
    list.innerHTML = html;
    list._commentsData = comments;
}

async function toggleDetail(btn, cid) {
    const el = document.getElementById('detail-overlay');
    if (!el) return;
    if (el.style.display !== 'none' && el.dataset.cid === String(cid)) {
        el.style.display = 'none';
        btn.classList.remove('active');
        return;
    }
    el.dataset.cid = String(cid);
    el.innerHTML = '<div class="detail-overlay-header"><span>详细数据</span><button onclick="closeDetailOverlay()">&times;</button></div><div class="detail-overlay-body"><em>加载中...</em></div>';
    el.style.display = 'flex';
    btn.classList.add('active');
    try {
        const resp = await api('/api/comments/' + cid + '/detail');
        const body = el.querySelector('.detail-overlay-body');
        body.innerHTML = resp.detail
            ? `<div class="detail-content md-content">${renderRoleChat(resp.detail)}</div>`
            : '<em>无详细数据</em>';
    } catch (e) {
        const body = el.querySelector('.detail-overlay-body');
        body.innerHTML = '<em>加载失败: ' + esc(e.message) + '</em>';
    }
}

function closeDetailOverlay() {
    const el = document.getElementById('detail-overlay');
    if (el) el.style.display = 'none';
    document.querySelectorAll('.btn-detail.active').forEach(b => b.classList.remove('active'));
}

async function downloadCommentMd(cid) {
    const list = document.getElementById('comment-list');
    const comments = (list && list._commentsData) || window._decisionComments || [];
    const c = comments.find(x => x.id === cid);
    if (!c) return;
    let md = c.content || '';
    if (c.detail) {
        const resp = await api('/api/comments/' + cid + '/detail');
        if (resp.detail) md += '\n\n---\n\n## 详细数据\n\n' + resp.detail;
    }
    const blob = new Blob([md], {type: 'text/markdown;charset=utf-8'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `comment-${cid}-${(c.author || 'system').replace(/\s+/g, '_')}.md`;
    a.click();
    URL.revokeObjectURL(url);
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
    // Ctrl+Shift+L: toggle dev log viewer
    if (e.ctrlKey && e.shiftKey && e.key === 'L') {
        e.preventDefault();
        toggleLogViewer();
    }
});

let _logFilters = new Set(['poll', 'api', 'dim', 'info', 'highlight', 'chat', 'err']); // all visible by default
let _logViewMode = 'type'; // 'type' | 'layer'
let _layerData = null;
let _lastLogLines = [];
let _logCardFilter = ''; // card code filter (e.g. "KH-086")

async function toggleLogViewer() {
    const overlay = document.getElementById('log-overlay');
    if (!overlay.classList.contains('hidden')) {
        closeLogViewer();
        return;
    }
    _logFilters = new Set(['poll', 'api', 'dim', 'info', 'highlight', 'chat', 'err']);
    _logViewMode = 'type';
    document.querySelectorAll('.log-view-tab').forEach(t => {
        t.classList.toggle('active', t.dataset.mode === 'type');
    });
    document.getElementById('log-filters').style.display = '';
    overlay.classList.remove('hidden');
    await fetchLogs();
}

function toggleLogFilter(key) {
    if (_logFilters.has(key)) _logFilters.delete(key);
    else _logFilters.add(key);
    renderLogLines();
    // Update button active state
    document.querySelectorAll('#log-filters .filter-btn').forEach(btn => {
        const k = btn.dataset.filter;
        btn.classList.toggle('active', _logFilters.has(k));
    });
}

async function fetchLogs() {
    const el = document.getElementById('log-content');
    el.innerHTML = '<div class="log-line"><span class="log-ts">加载中...</span></div>';
    try {
        const res = await fetch('/api/dev/logs?lines=300');
        const data = await res.json();
        const lines = data.logs || [];
        const pollRe = /"(?:GET|POST) \/api\/(scheduler\/status|agents\/sessions|decisions\/pending)/;

        // Parse & collapse consecutive polling
        const grouped = [];
        for (let i = 0; i < lines.length; i++) {
            const raw = lines[i];
            const pm = raw.match(/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.\d+Z\s+(.*)/);
            const ts = pm ? pm[1].replace('T', ' ') : '';
            const msg = pm ? pm[2] : raw;
            const pollKey = pollRe.test(msg) ? msg.match(/(scheduler\/status|agents\/sessions|decisions\/pending)/)[1] : null;
            if (pollKey) {
                const last = grouped[grouped.length - 1];
                if (last && last.pollKey === pollKey && last.pollCount > 0) {
                    last.pollCount++;
                    last.ts = ts;
                    continue;
                }
                grouped.push({ts, msg, pollKey, pollCount: 1, raw, filterKey: 'poll'});
            } else {
                const a = annotateLog(msg);
                let filterKey = 'info';
                if (a.cls === 'log-err') filterKey = 'err';
                else if (a.cls === 'log-chat') filterKey = 'chat';
                else if (a.cls === 'log-highlight') filterKey = 'highlight';
                else if (a.cls === 'log-dim') filterKey = 'dim';
                else if (a.label === 'API 查询') filterKey = 'api';
                grouped.push({ts, msg, pollKey: null, pollCount: 0, raw, annotation: a, filterKey});
            }
        }
        _lastLogLines = grouped;
        buildFilterBar(grouped);
        renderLogLines();
    } catch (err) {
        el.innerHTML = '<div class="log-line log-err"><span class="log-ts">-</span><span class="log-icon">❌</span><span class="log-msg">获取日志失败: ' + escapeHtml(err.message) + '</span></div>';
    }
}

function buildFilterBar(grouped) {
    const counts = {};
    grouped.forEach(g => { counts[g.filterKey] = (counts[g.filterKey] || 0) + 1; });
    const defs = [
        {key: 'err', label: '错误', icon: '❌', color: '#ef4444'},
        {key: 'chat', label: '用户', icon: '💬', color: '#22c55e'},
        {key: 'highlight', label: '事件', icon: '📌', color: '#6366f1'},
        {key: 'info', label: '信息', icon: 'ℹ️', color: '#94a3b8'},
        {key: 'api', label: 'API', icon: '📡', color: '#94a3b8'},
        {key: 'dim', label: '调试', icon: '🔧', color: '#64748b'},
        {key: 'poll', label: '轮询', icon: '🔄', color: '#64748b'},
    ];
    const bar = document.getElementById('log-filters');
    bar.innerHTML = defs.map(d => {
        const active = _logFilters.has(d.key) ? 'active' : '';
        const cnt = counts[d.key] || 0;
        return `<button class="filter-btn ${active}" data-filter="${d.key}" style="--fc:${d.color}" onclick="toggleLogFilter('${d.key}')">${d.icon} ${d.label} <span class="filter-cnt">${cnt}</span></button>`;
    }).join('');
}

function renderLogLines() {
    const el = document.getElementById('log-content');
    el.innerHTML = _lastLogLines.map(g => {
        if (!_logFilters.has(g.filterKey)) return '';
        if (_logCardFilter && !g.raw.includes(_logCardFilter)) return '';
        if (g.pollKey) {
            const label = { 'scheduler/status': '调度器状态', 'agents/sessions': 'Agent 会话', 'decisions/pending': '待决策' }[g.pollKey] || g.pollKey;
            return `<div class="log-line log-poll">`
                + `<span class="log-ts">${g.ts}</span>`
                + `<span class="log-icon">🔄</span>`
                + `<span class="log-msg">轮询 <strong>${label}</strong>${g.pollCount > 1 ? ` <span class="log-badge">×${g.pollCount}</span>` : ''}</span></div>`;
        }
        const a = g.annotation || {cls:'',icon:'',label:'',display:''};
        return `<div class="log-line ${a.cls}">`
            + `<span class="log-ts">${g.ts}</span>`
            + (a.icon ? `<span class="log-icon">${a.icon}</span>` : '')
            + `<span class="log-label">${a.label}</span>`
            + `<span class="log-msg">${a.display || escapeHtml(g.msg)}</span></div>`;
    }).join('');
    if (!el.innerHTML.trim()) {
        const hint = _logCardFilter ? `没有找到包含 "${escapeHtml(_logCardFilter)}" 的日志` : '所有过滤器已关闭，点击上方按钮显示日志';
        el.innerHTML = '<div class="log-line" style="justify-content:center;padding:40px;color:var(--text3)">' + hint + '</div>';
    }
}

function filterLogByCard(value) {
    _logCardFilter = value.trim().toUpperCase();
    renderLogLines();
}

function clearCardFilter() {
    _logCardFilter = '';
    document.getElementById('log-card-input').value = '';
    renderLogLines();
}

function annotateLog(msg) {
    let icon = '', label = '', cls = 'log-info', display = '';

    // Errors
    if (/ERROR|CRITICAL|Traceback|UNIQUE constraint|failed|Error/i.test(msg)) {
        cls = 'log-err'; icon = '❌'; label = '错误';
        // Try to extract the actual error
        const em = msg.match(/(?:ERROR|error)\s*(?:[-:]\s*)?(.+?)(?:\s*\(|$)/);
        display = em ? escapeHtml(em[1].trim()) : escapeHtml(msg);
        return {icon, label, cls, display};
    }
    if (/WARNING|WARN/i.test(msg)) {
        cls = 'log-warn'; icon = '⚠️'; label = '警告';
        return {icon, label, cls, display: escapeHtml(msg)};
    }

    // App lifecycle
    if (/Application startup complete/.test(msg)) {
        icon = '🚀'; label = '服务就绪';
        display = '<span class="log-dim">应用启动完成</span>';
        return {icon, label, cls: 'log-highlight', display};
    }
    if (/Scheduler started/.test(msg)) {
        icon = '⏱'; label = '调度器';
        display = '<span class="log-dim">AI 调度器已启动</span>';
        return {icon, label, cls, display};
    }
    if (/Started server process/.test(msg)) {
        icon = '🔌'; label = 'HTTP 服务';
        display = '<span class="log-dim">Uvicorn 服务进程已启动</span>';
        return {icon, label, cls: 'log-highlight', display};
    }
    if (/Loaded agent role/.test(msg)) {
        icon = '🤖'; label = 'Agent';
        const role = msg.match(/Loaded agent role: (\S+)/);
        const roleName = role ? role[1] : '';
        display = `Agent 角色已加载: <strong>${roleName}</strong>`;
        return {icon, label, cls, display};
    }

    // Chat / User interaction
    if (/\[kh\.web\.chat\]/.test(msg) || /\[CHAT\]/.test(msg)) {
        icon = '💬'; label = '用户';
        const chatM = msg.match(/user message:\s*"([^"]+)"/);
        display = chatM ? '用户说: "' + escapeHtml(chatM[1]) + '"' : escapeHtml(msg);
        cls = 'log-chat';
        return {icon, label, cls, display};
    }

    // PM Agent actions
    if (/\[kh\.agent\.pm\]/.test(msg) || /\[PM\]/.test(msg)) {
        icon = '📋'; label = 'PM';
        if (/tool_exec/.test(msg)) {
            const toolM = msg.match(/tool_exec:\s*(\w+)/);
            display = toolM ? '调用工具: ' + escapeHtml(toolM[1]) : escapeHtml(msg);
        } else if (/tool_call/.test(msg)) {
            const roundM = msg.match(/round=(\d+)/);
            display = '工具调用返回' + (roundM ? ` (第${roundM[1]}轮)` : '');
            cls = 'log-dim';
        } else if (/auto-created version/.test(msg)) {
            display = '👉 自动创建版本 v0.1 MVP';
            cls = 'log-highlight';
        } else if (/done, \d+ tool rounds/.test(msg)) {
            display = '✅ PM 决策完成: ' + escapeHtml(msg.replace(/.*done, /, ''));
            cls = 'log-highlight';
        } else {
            display = escapeHtml(msg);
        }
        return {icon, label, cls, display};
    }

    // HTTP API calls (non-polling)
    const httpM = msg.match(/"([A-Z]+) (\/(?:api\/)?[^\s]*) HTTP\/1\.1" (\d+)/);
    if (httpM) {
        const method = httpM[1], path = httpM[2], status = httpM[3];
        const isErr = status >= '400';
        if (isErr) { cls = 'log-err'; icon = '❌'; }
        else { icon = '📡'; }
        label = method === 'POST' ? 'API 写入' : 'API 查询';
        const pathShort = path.length > 50 ? path.slice(0, 47) + '...' : path;
        display = `${method} ${escapeHtml(pathShort)} → <span class="log-status-${status}">${status}</span>`;
        if (isErr) cls = 'log-err';
        return {icon, label, cls, display};
    }

    // HTTP Request to AI (DeepSeek/OpenAI)
    if (/HTTP Request: POST/.test(msg) && /chat\/completions/.test(msg)) {
        icon = '🤖'; label = 'AI 调用';
        const ms = (new Date().getTime() - Date.parse(msg.match(/\d{4}[-/]\d{2}[-/]\d{2}/)?.[0] || '') || 0) / 1000;
        display = '请求 AI 模型 (DeepSeek)';
        cls = 'log-dim';
        return {icon, label, cls, display};
    }

    // DB operations
    if (/DB|database|INSERT|SELECT|UPDATE|DELETE|requirements\.code/.test(msg)) {
        icon = '🗄'; label = '数据库';
        cls = 'log-dim';
        display = escapeHtml(msg);
        return {icon, label, cls, display};
    }

    // Default
    return {icon: '', label: '', cls, display: escapeHtml(msg)};
}

function escapeHtml(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
}

function closeLogViewer(event) {
    if (event && event.target !== event.currentTarget) return;
    document.getElementById('log-overlay').classList.add('hidden');
}

// ==================== Log Layer View (by architecture layer) ====================


function switchLogView(mode) {
    _logViewMode = mode;
    document.querySelectorAll('.log-view-tab').forEach(t => {
        t.classList.toggle('active', t.dataset.mode === mode);
    });
    if (mode === 'type') {
        document.getElementById('log-filters').style.display = '';
        renderLogLines();
    } else {
        document.getElementById('log-filters').style.display = 'none';
        fetchLogsByLayer();
    }
}

function refreshLogs() {
    if (_logViewMode === 'type') {
        fetchLogs();
    } else {
        fetchLogsByLayer();
    }
}

async function fetchLogsByLayer() {
    const el = document.getElementById('log-content');
    el.innerHTML = '<div class="log-line"><span class="log-ts">加载中...</span></div>';
    try {
        const res = await fetch('/api/dev/logs/layers?lines=300');
        const data = await res.json();
        _layerData = data;
        renderLayerView();
    } catch (err) {
        el.innerHTML = '<div class="log-line log-err"><span class="log-ts">-</span><span class="log-icon">❌</span><span class="log-msg">获取分层日志失败: ' + escapeHtml(err.message) + '</span></div>';
    }
}

const LAYER_DISPLAY = {
    core:  { label: 'Core 核心层',   icon: '⚙️', color: '#6366f1', desc: '数据库 · 配置 · 会话管理' },
    web:   { label: 'Web 服务层',    icon: '🌐', color: '#06b6d4', desc: 'API · Chat · Hermes · 中间件' },
    agent: { label: 'Agent 智能体层', icon: '🤖', color: '#f59e0b', desc: 'Coach-Dev · CommentAgent · Registry' },
    sched: { label: 'Scheduler 调度层', icon: '⏱', color: '#22c55e', desc: '定时任务 · 工作流引擎' },
    mcp:   { label: 'MCP 协议层',    icon: '🔌', color: '#ec4899', desc: 'MCP Server · KH Client' },
};

function renderLayerView() {
    const el = document.getElementById('log-content');
    if (!_layerData || !_layerData.layers) {
        el.innerHTML = '<div class="log-line" style="justify-content:center;padding:40px;color:var(--text3)">无分层数据</div>';
        return;
    }

    const order = ['core', 'web', 'agent', 'sched', 'mcp'];
    let html = '';
    let firstNonEmpty = null;

    for (const key of order) {
        const layer = _layerData.layers[key];
        if (!layer || !layer.lines || layer.lines.length === 0) continue;
        if (!firstNonEmpty) firstNonEmpty = key;

        const disp = LAYER_DISPLAY[key] || { label: key, icon: '📋', color: '#64748b', desc: '' };
        const isFirst = (key === firstNonEmpty);
        const linesHtml = layer.lines.map(l => {
            // Use annotateLog for styling
            const a = annotateLog(l.msg);
            return `<div class="log-line ${a.cls}">`
                + `<span class="log-ts">${l.ts}</span>`
                + (a.icon ? `<span class="log-icon">${a.icon}</span>` : '')
                + `<span class="log-label">${a.label}</span>`
                + `<span class="log-msg">${a.display || escapeHtml(l.msg)}</span></div>`;
        }).join('');

        html += `<div class="layer-section" data-layer="${key}">`
            + `<div class="layer-header" style="--layer-color:${disp.color}" onclick="toggleLayerSection(this)">`
            + `<span class="layer-arrow">${isFirst ? '▼' : '▶'}</span>`
            + `<span class="layer-icon">${disp.icon}</span>`
            + `<span class="layer-name">${disp.label}</span>`
            + `<span class="layer-count">${layer.count} 条</span>`
            + `<span class="layer-desc">${disp.desc}</span>`
            + `</div>`
            + `<div class="layer-body" style="display:${isFirst ? 'block' : 'none'}">${linesHtml}</div>`
            + `</div>`;
    }

    el.innerHTML = html || '<div class="log-line" style="justify-content:center;padding:40px;color:var(--text3)">无日志数据</div>';
}

function toggleLayerSection(header) {
    const body = header.nextElementSibling;
    const arrow = header.querySelector('.layer-arrow');
    if (body.style.display === 'none') {
        body.style.display = 'block';
        arrow.textContent = '▼';
    } else {
        body.style.display = 'none';
        arrow.textContent = '▶';
    }
}

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
    // Load app version
    try {
        const ver = await api('/api/version');
        document.getElementById('app-version').textContent = ver.version || '';
    } catch(e) {
        document.getElementById('app-version').textContent = '';
    }
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
let lastSchedulerState = null;

async function pollActivity() {
    try {
        const [status, schedState] = await Promise.all([
            api('/api/scheduler/status'),
            api('/api/scheduler/state'),
        ]);
        lastSchedulerState = schedState;
        updateSchedulerToggle(status.mode);
        updateActivityInfo(schedState, status);
        updateBoardHeartbeats();
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

function updateActivityInfo(schedState, status) {
    const info = document.getElementById('activity-info');
    const runningSessions = (schedState && schedState.running) || [];
    const running = runningSessions[0];

    if (running) {
        const startTime = new Date(running.started_at.replace(' ', 'T'));
        activitySessionStart = startTime;
        const stallTimeout = running.stall_timeout || 120;
        const taskTitle = running.card_code || running.agent_role;
        const ROLE_LABELS = {coach_dev: 'Coach-Dev', coach_review: 'Coach-Review', pm: 'PM', industry: '行业顾问'};
        const role = ROLE_LABELS[running.agent_role] || running.agent_role;

        const silent = running.silent_seconds;
        const ratio = silent / stallTimeout;
        const hbClass = ratio < 0.5 ? 'heartbeat-ok' : ratio < 0.75 ? 'heartbeat-warn' : 'heartbeat-danger';

        info.innerHTML = `
            <div class="activity-running">
                <span class="activity-role">${esc(role)}</span>
                <span class="activity-task">${esc(taskTitle)}</span>
                <span class="activity-timer" id="activity-timer"></span>
                <span class="activity-heartbeat ${hbClass}">心跳 ${silent}s</span>
                <div class="activity-progress"><div class="activity-progress-fill" id="activity-progress-fill"></div></div>
            </div>`;
        const timeout = schedState.scheduler?.poll_interval ? running.elapsed_seconds + stallTimeout : 600;
        updateTimer(timeout);
        if (!timerInterval) {
            timerInterval = setInterval(() => updateTimer(timeout), 1000);
        }
    } else {
        activitySessionStart = null;
        if (timerInterval) { clearInterval(timerInterval); timerInterval = null; }
        // Show morecent completed/failed from stale or blocked
        const stale = (schedState && schedState.stale) || [];
        const blocked = (schedState && schedState.blocked) || [];
        const recent = blocked[0] || stale[0];
        if (recent) {
            const label = recent.card_code || recent.agent_role;
            const ago = timeAgo(recent.failed_at || recent.started_at);
            const icon = recent.error ? '✗' : '✓';
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

function updateBoardHeartbeats() {
    const runningSessions = (lastSchedulerState && lastSchedulerState.running) || [];
    document.querySelectorAll('.col-avatar-wrap').forEach(wrap => {
        const avatar = wrap.querySelector('.col-avatar');
        if (!avatar) return;
        const role = avatar.alt;
        const ROLE_TO_AGENT = {'Industry': 'industry', 'PM': 'pm', 'Coach-Dev': 'coach_dev', 'Coach-Review': 'coach_review'};
        const agentKey = ROLE_TO_AGENT[role];
        const session = agentKey ? runningSessions.find(s => s.agent_role === agentKey) : null;

        let ecgEl = wrap.querySelector('.col-ecg');
        let badge = wrap.querySelector('.col-heartbeat');

        if (session) {
            const silent = session.silent_seconds;
            const stallTimeout = session.stall_timeout || 120;
            const ratio = silent / stallTimeout;
            const colorClass = ratio < 0.5 ? '' : ratio < 0.75 ? 'warn' : 'danger';
            const avatarClass = ratio < 0.5 ? 'heartbeat-active' : ratio < 0.75 ? 'heartbeat-warn' : 'heartbeat-danger';

            avatar.classList.remove('heartbeat-active', 'heartbeat-warn', 'heartbeat-danger');
            avatar.classList.add(avatarClass);

            if (!ecgEl) {
                ecgEl = document.createElement('div');
                ecgEl.className = 'col-ecg active';
                ecgEl.innerHTML = '<svg viewBox="0 0 48 16" preserveAspectRatio="none"><polyline class="col-ecg-line" points="0,8 6,8 9,8 12,2 15,14 18,8 21,8 24,8 30,8 33,8 36,2 39,14 42,8 45,8 48,8" stroke-dasharray="48" stroke-dashoffset="0"/></svg>';
                wrap.appendChild(ecgEl);
            }
            ecgEl.className = 'col-ecg active';
            const line = ecgEl.querySelector('.col-ecg-line');
            if (line) {
                line.classList.remove('warn', 'danger');
                if (colorClass) line.classList.add(colorClass);
            }

            if (session.card_code) {
                if (!badge) {
                    badge = document.createElement('span');
                    badge.className = 'col-heartbeat';
                    wrap.appendChild(badge);
                }
                badge.className = 'col-heartbeat heartbeat-ok';
                badge.textContent = session.card_code;
                badge.title = '正在处理 ' + session.card_code + ' · 已运行 ' + session.elapsed_seconds + 's';
            } else if (badge) {
                badge.remove();
            }
        } else {
            avatar.classList.remove('heartbeat-active', 'heartbeat-warn', 'heartbeat-danger');
            if (ecgEl) ecgEl.remove();
            if (badge) badge.remove();
        }
    });
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

// ==================== Board SSE (real-time refresh) ====================
(function initBoardSSE() {
    let es = null;
    let retryDelay = 1000;

    function connect() {
        es = new EventSource('/api/board/events');
        es.onopen = () => { retryDelay = 1000; };
        es.addEventListener('card_moved', () => refreshBoardQuiet());
        es.addEventListener('card_created', () => refreshBoardQuiet());
        es.addEventListener('card_updated', () => refreshBoardQuiet());
        es.onerror = () => {
            es.close();
            setTimeout(connect, retryDelay);
            retryDelay = Math.min(retryDelay * 2, 30000);
        };
    }

    async function refreshBoardQuiet() {
        try {
            if (currentVersion) {
                await loadRequirements(currentVersion.id);
                renderRequirements();
            }
            await refreshVersions();
        } catch(e) {}
    }

    connect();
})();

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
    if (!currentProject) return;
    document.getElementById('chat-fab').style.display = '';
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
            messages.scrollTop = messages.scrollHeight;
        } else {
            messages.innerHTML = '';
        }
        messages.dataset.loaded = String(currentProject.id);
    } catch(e) {}
    reconnectActiveTask();
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

    try {
        const taskResp = await api('/api/chat/tasks', {
            method: 'POST',
            body: {message: msg, project_id: currentProject?.id || 0},
        });
        const taskId = taskResp.task_id;
        sessionStorage.setItem('kh_active_task', taskId);

        await streamTask(taskId, assistantDiv, 0);
    } catch(e) {
        assistantDiv.innerHTML = `<span style="color:var(--danger)">连接失败: ${escapeHtml(e.message)}</span>`;
    } finally {
        sessionStorage.removeItem('kh_active_task');
        refreshBoardAfterChat();
    }
}

async function streamTask(taskId, assistantDiv, fromIndex) {
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

    const messages = document.getElementById('chat-messages');
    let fullText = '';
    let gotText = false;

    try {
        const resp = await fetch(API + `/api/chat/tasks/${taskId}/stream?last_event_id=${fromIndex}`);
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();

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
    } finally {
        if (thinkingTimer) clearInterval(thinkingTimer);
    }
}

async function reconnectActiveTask() {
    if (!currentProject) return;
    try {
        const data = await api('/api/chat/tasks/active?project_id=' + currentProject.id);
        if (data.task && data.task.status === 'running') {
            const messages = document.getElementById('chat-messages');
            const assistantDiv = document.createElement('div');
            assistantDiv.className = 'chat-msg assistant';
            assistantDiv.innerHTML = '<div class="chat-thinking"><div class="thinking-dots"><span></span><span></span><span></span></div><span class="thinking-label">重新连接中...</span></div>';
            messages.appendChild(assistantDiv);
            messages.scrollTop = messages.scrollHeight;

            sessionStorage.setItem('kh_active_task', data.task.id);
            try {
                await streamTask(data.task.id, assistantDiv, 0);
            } finally {
                sessionStorage.removeItem('kh_active_task');
                refreshBoardAfterChat();
            }
        }
    } catch(e) {}
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
        const [data, schedState] = await Promise.all([
            api('/api/agents/status'),
            api('/api/scheduler/state'),
        ]);
        teamDataCache = data.agents;
        renderTeamWorkflow();
        renderTokenCostCard();
        renderTeamGrid(data.agents, schedState);
        loadProductMemorySummary();
    } catch(e) {
        document.getElementById('team-grid').innerHTML = '<div class="arch-empty">无法加载团队状态</div>';
    }
}

async function renderTokenCostCard() {
    const el = document.getElementById('token-cost-card');
    if (!el) return;
    try {
        const params = currentProject ? `?project_id=${currentProject.id}` : '';
        const data = await api('/api/stats/tokens' + params);
        const today = data.today || {};
        const week = data.this_week || {};
        const byRole = data.by_role || [];

        const todayTotal = today.total_tokens || 0;
        const weekTotal = week.total_tokens || 0;
        const totalSessions = byRole.reduce((s, r) => s + (r.session_count || 0), 0);

        const ROLE_LABELS = {pm: '产品经理', industry: '行业顾问', coach_dev: 'Coach-Dev', coach_review: 'Coach-Review'};
        const ROLE_COLORS = {pm: '#6366f1', industry: '#f59e0b', coach_dev: '#22c55e', coach_review: '#06b6d4'};

        // If no token data yet, show session counts instead
        const hasTokenData = todayTotal > 0 || weekTotal > 0;

        const maxTokens = Math.max(...byRole.map(r => r.total_tokens || 0), 1);
        const maxSessions = Math.max(...byRole.map(r => r.session_count || 0), 1);
        const barsHtml = byRole.filter(r => (r.total_tokens > 0) || (r.session_count > 0)).map(r => {
            const pct = hasTokenData
                ? Math.round((r.total_tokens / maxTokens) * 100)
                : Math.round((r.session_count / maxSessions) * 100);
            const label = ROLE_LABELS[r.agent_role] || r.agent_role;
            const color = ROLE_COLORS[r.agent_role] || '#94a3b8';
            const value = hasTokenData ? formatTokens(r.total_tokens) : `${r.session_count} 次`;
            return `<div class="token-bar-row">
                <span class="token-bar-label">${esc(label)}</span>
                <div class="token-bar-track"><div class="token-bar-fill" style="width:${pct}%;background:${color}"></div></div>
                <span class="token-bar-value">${value}</span>
            </div>`;
        }).join('');

        el.innerHTML = `
            <div class="token-card-header">
                <span class="token-card-title">💰 ${hasTokenData ? 'Token 消耗' : 'AI 调用统计'}</span>
            </div>
            <div class="token-card-stats">
                <div class="token-stat">
                    <span class="token-stat-value">${hasTokenData ? formatTokens(todayTotal) : totalSessions}</span>
                    <span class="token-stat-label">${hasTokenData ? '今日' : '总调用'}</span>
                </div>
                <div class="token-stat">
                    <span class="token-stat-value">${hasTokenData ? formatTokens(weekTotal) : byRole.length}</span>
                    <span class="token-stat-label">${hasTokenData ? '本周' : '活跃角色'}</span>
                </div>
            </div>
            ${barsHtml ? '<div class="token-bars">' + barsHtml + '</div>' : ''}
        `;
    } catch(e) {
        el.innerHTML = '';
    }
}

function formatTokens(n) {
    if (!n) return '0';
    if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
    if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
    return String(n);
}

async function loadProductMemorySummary() {
    const el = document.getElementById('product-memory-summary');
    if (!el || !currentProject) return;
    try {
        const data = await api('/api/projects/' + currentProject.id + '/product-memory');
        const content = data.content || '';
        if (!content) {
            el.innerHTML = '<div class="arch-empty">暂无产品记忆</div>';
            return;
        }
        // Extract key sections
        const sections = [];
        const miMatch = content.match(/## 一、市场分析.*?(?=## |\Z)/s);
        if (miMatch) {
            const lines = miMatch[0].split('\n').filter(l => l.trim()).slice(0, 8);
            sections.push('<details class="arch-collapse"><summary>📊 市场分析</summary><div class="memory-preview">' +
                renderMd(lines.join('\n')) + '</div></details>');
        }
        const dcMatch = content.match(/## 二、方向把控.*?(?=## |\Z)/s);
        if (dcMatch) {
            const lines = dcMatch[0].split('\n').filter(l => l.trim()).slice(0, 12);
            sections.push('<details class="arch-collapse"><summary>🎯 方向把控</summary><div class="memory-preview">' +
                renderMd(lines.join('\n')) + '</div></details>');
        }
        el.innerHTML = sections.join('') || '<div class="arch-empty">产品记忆为空</div>';
    } catch(e) {
        el.innerHTML = '<div class="arch-empty">加载失败</div>';
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
            <div class="wf-node wf-pm">PM 审核<span class="wf-sub">research→organizing</span></div>
        </div>
        <div class="workflow-title">开发链（需求交付）</div>
        <div class="workflow-main">
            <div class="wf-node wf-pm">PM 分配<span class="wf-sub">organizing→dev</span></div>
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
                <span class="wf-esc-step">退回 PM<span class="wf-esc-sub">dev→organizing</span></span>
                <span class="wf-esc-arrow">→</span>
                <span class="wf-esc-step">PM 修改重推<span class="wf-esc-sub">organizing→dev</span></span>
                <span class="wf-esc-arrow">→</span>
                <span class="wf-esc-step wf-esc-blocked">仍有分歧→ask_ceo<span class="wf-esc-sub">CEO 裁决</span></span>
                <span class="wf-esc-arrow">→</span>
                <span class="wf-esc-step wf-esc-ceo">CEO 裁决</span>
            </div>
        </div>
    </div>`;
}

function renderTeamGrid(agents, schedulerState) {
    const grid = document.getElementById('team-grid');
    const AVATAR_MAP = {
        pm: '/static/avatars/pm_avatar.png',
        industry: '/static/avatars/industry_avatar.png',
        coach_dev: '/static/avatars/coach_dev_avatar.png',
        coach_review: '/static/avatars/coach_review_avatar.png',
    };
    const runningSessions = (schedulerState && schedulerState.running) || [];
    let html = '';
    for (const [role, info] of Object.entries(agents)) {
        const runningSession = runningSessions.find(s => s.agent_role === role);
        const statusClass = runningSession ? 'working' : 'idle';
        const avatarSrc = AVATAR_MAP[role] || info.avatar || '';
        const lastActivity = info.last_run ? timeAgo(info.last_run) : '暂无活动';

        let activityHtml;
        if (runningSession) {
            const silent = runningSession.silent_seconds;
            const stallTimeout = runningSession.stall_timeout || 120;
            const ratio = silent / stallTimeout;
            const hbClass = ratio < 0.5 ? 'heartbeat-ok' : ratio < 0.75 ? 'heartbeat-warn' : 'heartbeat-danger';
            const cardLabel = runningSession.card_code ? ` · ${esc(runningSession.card_code)}` : '';
            activityHtml = `<span class="${hbClass}">工作中 · 心跳 ${silent}s 前${cardLabel}</span>`;
        } else {
            activityHtml = `空闲 · ${lastActivity}`;
        }
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
        const moves = (info.permissions?.can_move || []).map(m => m.replace('research', '调研').replace('organizing', '需求整理').replace('dev', '开发').replace('testing', '测试').replace('done', '完成').replace('->', ' → ')).join(' · ') || '—';
        const TRIGGER_LABELS = {
            'requirement_created': '新需求触发',
            'status_changed': '状态变更触发',
            'scheduled': '定期巡查',
        };
        const triggersHtml = (info.triggers || []).map(t =>
            `<span class="tool-tag common">${esc(TRIGGER_LABELS[t] || t)}</span>`
        ).join('');

        const MOVE_EXPLAIN = {
            'organizing->dev': '分配任务给开发',
            'dev->testing': '提交代码送测',
            'dev->organizing': '退回需求给PM',
            'testing->done': '验收通过完成',
            'testing->dev': '打回修改',
        };
        const STATUS_ROLE = {
            'organizing': 'pm',
            'dev': 'coach_dev',
            'testing': 'coach_review',
            'done': '',
        };
        const movesHtml = (info.permissions?.can_move || []).map(m => {
            const parts = m.split('->');
            const fromStatus = parts[0];
            const toStatus = parts[1];
            const fromLabel = fromStatus.replace('research', '调研').replace('organizing', '需求整理').replace('dev', '开发').replace('testing', '测试').replace('done', '完成');
            const toLabel = toStatus.replace('research', '调研').replace('organizing', '需求整理').replace('dev', '开发').replace('testing', '测试').replace('done', '完成');
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
                <span class="agent-persona-activity">${activityHtml}</span>
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
        const params = currentProject ? `?project_id=${currentProject.id}` : '';
        const resp = await fetch('/api/decisions/pending' + params);
        if (!resp.ok) return;
        const data = await resp.json();
        _pendingDecisions = data.decisions || [];
        updateDecisionBadges();
    } catch (e) { /* silent */ }
}

function updateDecisionBadges() {
    // Remove existing badges
    document.querySelectorAll('.decision-badge').forEach(b => b.remove());

    const total = _pendingDecisions.length;
    if (total === 0) {
        const fab = document.getElementById('chat-fab');
        if (fab) fab.removeAttribute('data-decisions');
        return;
    }

    // Map asking_role to the kanban column status
    const roleToStatus = { pm: 'organizing', industry: 'research', coach_dev: 'dev', coach_review: 'testing' };
    const roleLabels = { pm: 'PM', industry: '行业顾问', coach_dev: 'Coach-Dev', coach_review: 'Coach-QA' };

    // Group by card's actual status so badge appears on the right column
    const byStatus = {};
    for (const d of _pendingDecisions) {
        const s = d.status || 'organizing';
        if (!byStatus[s]) byStatus[s] = [];
        byStatus[s].push(d);
    }

    for (const [status, decisions] of Object.entries(byStatus)) {
        const wrappers = document.querySelectorAll('.col-wrapper');
        for (const wrapper of wrappers) {
            const col = wrapper.querySelector(`.board-col[data-status="${status}"]`);
            const avatar = wrapper.querySelector('.col-avatar');
            if (col && avatar) {
                wrapper.style.position = 'relative';
                const badge = document.createElement('span');
                badge.className = 'decision-badge';
                // Show role labels for all roles with decisions in this column
                const roles = [...new Set(decisions.map(d => roleLabels[d.asking_role] || d.asking_role || 'PM'))];
                badge.textContent = '?';
                badge.title = `${roles.join('/')}: ${decisions.length} 项待CEO决策`;
                badge.onclick = (e) => { e.stopPropagation(); openDecision(decisions[0]); };
                wrapper.appendChild(badge);
                break;
            }
        }
    }

    // Also show count on chat FAB
    const fab = document.getElementById('chat-fab');
    if (fab) {
        fab.setAttribute('data-decisions', total);
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
    document.getElementById('decision-avatar-q').classList.add('visible');

    // Build plain-language speech from PM summary
    if (isWizardMode(decision)) {
        _wizardQuestions = decision.questions;
        _wizardAnswers = new Array(decision.questions.length).fill('');
        _wizardIndex = 0;
        renderWizard();
    } else {
        const speech = buildSpeech(decision);
        document.getElementById('decision-speech').innerHTML = speech;

        // Build action buttons
        const actions = document.getElementById('decision-actions');
        actions.innerHTML = buildActionButtons(decision);

        // Show the custom input area
        document.querySelector('.decision-custom').style.display = '';
    }

    // Load card detail on the right
    await loadDecisionCard(decision);

    // Clear input
    document.getElementById('decision-input').value = '';

    // Show overlay
    overlay.classList.add('active');
}

function buildSpeech(decision) {
    // For agent-deliberated decisions (PM/Industry actively asking), show message directly
    if (decision.reason === 'agent_d' && decision.message) {
        let chat = decision.message;
        if (typeof marked !== 'undefined') {
            return DOMPurify.sanitize(marked.parse(chat));
        }
        return escapeHtml(chat).replace(/\n/g, '<br>');
    }

    // For system-escalated decisions (stuck/timeout), show the message directly
    if (decision.reason === 'stuck_timeout' && decision.message) {
        let chat = decision.message + '\n\n';
        if (decision.pm_summary && decision.pm_summary !== decision.message) {
            chat += '最近的上下文：\n\n' + decision.pm_summary.slice(0, 300);
        }
        if (typeof marked !== 'undefined') {
            return DOMPurify.sanitize(marked.parse(chat));
        }
        return chat.replace(/\n/g, '<br>');
    }

    let summary = decision.pm_summary || '';
    // Strip any role prefix
    summary = summary.replace(/^\*\*\[?[^\]]*\]?\s*[：:]\s*\*\*/m, '');
    summary = summary.replace(/^\[?[^\]]*\]?\s*[：:]\s*/m, '');
    // Strip decision markers
    summary = summary.replace(/^\[需要补充\]\s*/m, '').replace(/^\[调研充分\]\s*/m, '');

    // Remove numbered pipe-separated lines (LLM artifact: "55 发出视觉提示 | 无 |")
    summary = summary.replace(/^\d+\s*[^|\n]+\|[^|\n]+(\|.*)?$/gm, '');
    // Collapse excess blank lines left by removal
    summary = summary.replace(/\n{3,}/g, '\n\n');

    const role = decision.asking_role || 'pm';
    const isPM = role === 'pm';

    // Extract key facts (bullet points starting with -)
    const bulletLines = summary.split('\n').filter(l => /^\s*-\s/.test(l)).map(l => l.replace(/^\s*-\s*/, '').replace(/\*\*/g, '').trim());

    // Extract the "risk" or "decision needed" part
    const riskMatch = summary.match(/(?:遗留风险|需.*?决策|需要.*?确认|需要补充|信号冲突)[^]*?(?=\n\*\*|\n\d+\.|$)/s);
    const riskText = riskMatch ? riskMatch[0].replace(/\*\*/g, '').replace(/[（(]已知[^)）]*[)）]/g, '').trim() : '';

    // Extract options (路线/方向)
    const optLines = summary.split('\n').filter(l => /^\s*\d+\.\s*(路线|方向|方案)/.test(l)).map(l => l.replace(/\*\*/g, '').trim());

    // Build conversational output
    let chat = '';

    if (bulletLines.length > 0) {
        chat += '几个关键发现：\n\n';
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
    const role = decision.asking_role || 'pm';
    const roleNames = { pm: '产品经理', industry: '行业顾问', coach_dev: 'Coach-Dev', coach_review: 'Coach-QA' };
    const roleName = roleNames[role] || role;
    const actions = decision.actions || ['reply_to_role'];

    const ACTION_DEFS = {
        reply_to_role: { icon: '💬', label: `回复${roleName}`, hint: '回答问题，卡片留在原列', cls: 'primary' },
        retry: { icon: '🔄', label: '重试', hint: '让 AI 重新尝试处理', cls: '' },
    };

    let html = '';
    for (const action of actions) {
        const def = ACTION_DEFS[action];
        if (!def) continue;
        html += `<button class="decision-btn ${def.cls}" onclick="submitDecision('${action}')">
            <span class="decision-btn-icon">${def.icon}</span>
            <div class="decision-btn-text">${def.label}<div class="decision-btn-hint">${def.hint}</div></div>
        </button>`;
    }

    return html;
}

async function loadDecisionCard(decision) {
    const cardEl = document.getElementById('decision-card');
    cardEl.innerHTML = '<div style="color:var(--text3);padding:20px">加载中...</div>';

    try {
        if (!decision.code) throw new Error('no code');
        const resp = await fetch(`/api/requirements/by-code/${encodeURIComponent(decision.code)}`);
        if (!resp.ok) throw new Error('load failed');
        const req = await resp.json();

        const cResp = await fetch(`/api/requirements/${decision.id}/comments`);
        const comments = cResp.ok ? await cResp.json() : [];

        const priorityColors = { P0: 'var(--danger)', P1: 'var(--warning)', P2: 'var(--info)', P3: 'var(--text3)' };
        const priorityBg = { P0: 'var(--danger-bg)', P1: 'var(--warning-bg)', P2: 'var(--info-bg)', P3: 'var(--bg4)' };

        const descHtml = renderMd(req.description || '');

        const recentComments = comments.slice(-8);
        window._decisionComments = recentComments;
        let commentsHtml = recentComments.map(c => `
            <div class="comment-item">
                <div class="comment-header">
                    <span class="comment-author">${esc(c.author || '系统')}</span>
                    <span>${esc(c.created_at)}</span>
                    ${c.detail ? `<button class="btn-detail" onclick="event.stopPropagation();toggleDetail(this,${c.id})">详情</button>` : ''}
                    <button class="btn-download-md" onclick="event.stopPropagation();downloadCommentMd(${c.id})" title="下载 Markdown">⬇</button>
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
            body: JSON.stringify({ decision, comment, asking_role: _currentDecision.asking_role || 'pm' }),
        });
        if (!resp.ok) throw new Error('submit failed');
        const result = await resp.json();

        closeDecision();

        // Navigate to the card's version so user can see it
        if (result.version_id && (!currentVersion || currentVersion.id !== result.version_id)) {
            await selectVersion(result.version_id);
        } else {
            if (typeof loadRequirements === 'function' && currentVersion) await loadRequirements(currentVersion.id);
            renderRequirements();
        }

        const statusLabel = STATUS_MAP[result.new_status]?.label || result.new_status;
        if (result.new_status === 'archived') {
            showToast('卡片已归档');
        } else {
            showToast(`卡片已移至「${statusLabel}」`);
        }

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
            body: JSON.stringify({ decision: 'custom', comment, asking_role: _currentDecision.asking_role || 'pm' }),
        });
        if (!resp.ok) throw new Error('submit failed');
        const result = await resp.json();

        closeDecision();

        if (result.version_id && (!currentVersion || currentVersion.id !== result.version_id)) {
            await selectVersion(result.version_id);
        } else {
            if (typeof loadRequirements === 'function' && currentVersion) await loadRequirements(currentVersion.id);
            renderRequirements();
        }

        showToast('决策已提交');
        setTimeout(pollDecisions, 1000);
    } catch (e) {
        alert('提交失败: ' + e.message);
    }
}

function closeDecision() {
    document.getElementById('decision-overlay').classList.remove('active');
    document.getElementById('decision-avatar-q').classList.remove('visible');
    document.querySelector('.decision-custom').style.display = '';
    _currentDecision = null;
    _wizardQuestions = [];
    _wizardAnswers = [];
    _wizardIndex = 0;
}

// ==================== Multi-question Wizard ====================

let _wizardQuestions = [];
let _wizardAnswers = [];
let _wizardIndex = 0;

function isWizardMode(decision) {
    return decision.questions && decision.questions.length > 1;
}

function renderWizard() {
    const total = _wizardQuestions.length;
    const idx = _wizardIndex;
    const question = _wizardQuestions[idx];

    // Progress + question
    const speechEl = document.getElementById('decision-speech');
    const progress = `<div class="wizard-progress">${idx + 1} / ${total}</div>`;
    const qHtml = typeof marked !== 'undefined'
        ? DOMPurify.sanitize(marked.parse(question))
        : escapeHtml(question).replace(/\n/g, '<br>');
    speechEl.innerHTML = progress + qHtml;

    // Replace actions with wizard input
    const actionsEl = document.getElementById('decision-actions');
    const isLast = idx === total - 1;
    const btnLabel = isLast ? '提交全部' : '下一题 →';
    const btnCls = isLast ? 'primary' : '';
    actionsEl.innerHTML = `
        <div class="wizard-input-wrap">
            <textarea id="wizard-answer" placeholder="输入你的回答...">${_wizardAnswers[idx] || ''}</textarea>
            <div class="wizard-nav">
                ${idx > 0 ? '<button class="decision-btn" onclick="wizardPrev()">← 上一题</button>' : ''}
                <button class="decision-btn ${btnCls}" onclick="wizardNext()">${btnLabel}</button>
            </div>
        </div>`;

    // Hide the original custom input area
    document.querySelector('.decision-custom').style.display = 'none';

    setTimeout(() => { const ta = document.getElementById('wizard-answer'); if (ta) ta.focus(); }, 50);
}

function wizardPrev() {
    // Save current answer
    const ta = document.getElementById('wizard-answer');
    if (ta) _wizardAnswers[_wizardIndex] = ta.value.trim();
    _wizardIndex--;
    renderWizard();
}

function wizardNext() {
    const ta = document.getElementById('wizard-answer');
    const answer = ta ? ta.value.trim() : '';
    if (!answer) {
        ta && ta.focus();
        ta && (ta.style.borderColor = 'var(--danger, #ef4444)');
        setTimeout(() => { if (ta) ta.style.borderColor = ''; }, 1500);
        return;
    }
    _wizardAnswers[_wizardIndex] = answer;

    if (_wizardIndex < _wizardQuestions.length - 1) {
        _wizardIndex++;
        renderWizard();
    } else {
        wizardSubmit();
    }
}

async function wizardSubmit() {
    // Build structured comment from Q&A pairs
    let comment = '';
    _wizardQuestions.forEach((q, i) => {
        comment += `**Q${i + 1}:** ${q}\n**A${i + 1}:** ${_wizardAnswers[i]}\n\n`;
    });

    try {
        const resp = await fetch(`/api/decisions/${_currentDecision.id}/submit`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ decision: 'reply_to_role', comment: comment.trim(), asking_role: _currentDecision.asking_role || 'pm' }),
        });
        if (!resp.ok) throw new Error('submit failed');
        const result = await resp.json();

        closeDecision();

        if (result.version_id && (!currentVersion || currentVersion.id !== result.version_id)) {
            await selectVersion(result.version_id);
        } else {
            if (typeof loadRequirements === 'function' && currentVersion) await loadRequirements(currentVersion.id);
            renderRequirements();
        }

        showToast('所有问题已回复');
        setTimeout(pollDecisions, 1000);
    } catch (e) {
        alert('提交失败: ' + e.message);
    }
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
