// Профиль (Фаза 8 → Раунд1): чёткое разделение.
//   * Owner Profile = ЛИЧНЫЙ + административный (3 вкладки: Профиль | Доступ | Настройки).
//     Никаких worker-часов/колец/навыков/размеров/отпуска/выбора чужого сотрудника.
//   * Worker self-profile (когда Worker смотрит СЕБЯ) = полный (кольца часов, отпуск,
//     доступность, объекты, навыки, размеры) — не сломан, ветка по currentRole.
//   * Worker Card = ОТДЕЛЬНЫЙ режим-overlay (openWorkerCard), НЕ подмена профиля Owner:
//     часы/смена/объекты/навыки/доступность/размеры/табель для конкретного uid.
//     Открывается из Команды/объекта/доступа/аватара; Back возвращает туда, откуда открыт.

const WEEKDAY_LETTERS = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
const PROFILE_DAY_NORM_HOURS = 10; // 100% кольца = 10ч в день
const PROFILE_ICONS = {
  camera: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"></path><circle cx="12" cy="13" r="4"></circle></svg>',
  calendar: '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"></rect><path d="M16 2v4M8 2v4M3 10h18"></path></svg>',
  object: '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 21h18"></path><path d="M5 21V7l8-4v18"></path><path d="M19 21V11l-6-4"></path><path d="M9 9h1M9 13h1M9 17h1"></path></svg>',
  edit: '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"></path><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"></path></svg>',
  birthday: '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v4"></path><path d="M8 6h8"></path><path d="M4 14h16"></path><path d="M5 14v6h14v-6"></path><path d="M6 14c0-3.3 2.7-6 6-6s6 2.7 6 6"></path></svg>',
  skills: '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.1-3.1a6 6 0 0 1-7.9 7.9l-5.6 5.6a2.1 2.1 0 0 1-3-3l5.6-5.6a6 6 0 0 1 7.9-7.9z"></path></svg>',
  sizes: '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 3l5 3-2 5-3-1v11H8V10l-3 1-2-5 5-3a4 4 0 0 0 8 0z"></path></svg>',
};

let _profileStatsUserId = ''; // пусто = я сам (worker-self). Owner-self НЕ смотрит чужих тут.
let _profilePeriod = 'week'; // period-pills worker-self — week/month/3months/year
const PROFILE_PERIOD_LABEL = { week: 'Неделя', month: 'Месяц', '3months': '3 месяца', year: 'Год' };

// Раунд1 Задача 2.3: явный режим + точка возврата для Worker Card.
let _profileMode = 'owner-self'; // 'owner-self' | 'worker-card'
let _profileReturnView = null;   // куда вернуться из Worker Card (Back)

function initProfileView() {
  const slot = document.getElementById('profile-content');
  if (!slot) return;
  // Bottom-nav "Профиль" ВСЕГДА показывает СВОЙ профиль. Worker Card — отдельный overlay.
  _profileStatsUserId = '';
  _profileMode = 'owner-self';
  if (currentRole === 'owner') _renderOwnerSelfProfile(slot);
  else _renderWorkerSelfProfile(slot);
}

// ─────────────────────────── OWNER: личный + административный ───────────────────────────

function _renderOwnerSelfProfile(slot) {
  slot.innerHTML = `
    <div class="profile-header-card">
      <div class="profile-avatar-wrap" id="profile-avatar-wrap" title="Сменить фото">
        <img id="profile-avatar-img" alt="" style="display:none">
        <span id="profile-avatar-fallback"><svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="4"></circle><path d="M4 20c0-4 3.6-7 8-7s8 3 8 7"></path></svg></span>
        <span class="profile-avatar-edit">${PROFILE_ICONS.camera}</span>
      </div>
      <input type="file" id="profile-avatar-input" accept="image/*" style="display:none">
      <div class="profile-header-info">
        <div class="profile-name" id="profile-name">Загрузка…</div>
        <div class="profile-role-badge" id="profile-role-badge">Владелец</div>
        <div class="profile-secondary-id" id="profile-telegram-id"></div>
      </div>
    </div>

    <div class="profile-tabs" id="profile-tabs">
      <div class="profile-tab active" data-tab="me">Профиль</div>
      <div class="profile-tab" data-tab="team">Доступ</div>
    </div>

    <div class="profile-tab-panel" data-panel="me">
      <div class="card ios-list profile-settings-list profile-actions-list">
        <div class="profile-owner-actions">
          <button class="profile-owner-action-btn" id="profile-owner-edit-name" type="button">
            <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"></path><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"></path></svg>
            Изменить имя
          </button>
          <button class="profile-owner-action-btn" id="profile-owner-edit-photo" type="button">
            <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"></path><circle cx="12" cy="13" r="4"></circle></svg>
            Изменить фото
          </button>
        </div>
        <div id="profile-name-edit-inline" style="display:none;margin-top:0.75rem;">
          <input type="text" id="profile-name-input" class="mangel-select" placeholder="Например: Иван" maxlength="100">
          <button class="submit-btn profile-inline-btn" id="profile-name-save-btn" type="button" style="margin-top:0.5rem">Сохранить имя</button>
          <div id="profile-name-status" style="font-size:0.8rem;color:var(--accent);margin-top:0.4rem"></div>
        </div>
      </div>
      <div class="card ios-list profile-settings-list profile-system-list">
        <div class="home-section-header" style="padding:0 0 0.5rem;">
          <span class="home-section-title">Приложение</span>
        </div>
        <div id="profile-app-status" class="profile-app-status">
          <div class="profile-app-status-row"><span>Версия</span><span id="profile-app-version">—</span></div>
          <div class="profile-app-status-row"><span>AI-ассистент</span><span>GLM-режим</span></div>
        </div>
        <button class="submit-btn profile-inline-btn" id="profile-system-status-btn" type="button" style="margin-top:0.75rem">Статус системы →</button>
      </div>
    </div>

    <div class="profile-tab-panel" data-panel="team" style="display:none">
      <div class="accordion-section ios-list profile-settings-list profile-access-list" style="display:block">
        <div id="profile-team-list" style="font-size:0.85rem;color:var(--text-light);padding:0.75rem 0">Загрузка…</div>
      </div>
    </div>
  `;

  _bindProfileTabs();
  _bindOwnerSelfHandlers();
  _loadOwnerSelfProfile();
  _loadProfileTeam();
}

function _bindOwnerSelfHandlers() {
  const wrap = document.getElementById('profile-avatar-wrap');
  const input = document.getElementById('profile-avatar-input');
  const triggerPhoto = () => input && input.click();
  wrap?.addEventListener('click', triggerPhoto);
  document.getElementById('profile-owner-edit-photo')?.addEventListener('click', triggerPhoto);
  document.getElementById('profile-system-status-btn')?.addEventListener('click', () => {
    if (typeof switchView === 'function') switchView('diagnostics');
  });
  document.getElementById('profile-owner-edit-name')?.addEventListener('click', () => {
    // Item 4 fix: name editing used to jump to a separate Настройки tab that
    // duplicated the same input/button already visible on this tab -- now
    // toggles an inline field in-place instead of removing that duplication
    // rather than hiding it behind a tab switch.
    const wrap = document.getElementById('profile-name-edit-inline');
    if (!wrap) return;
    const opening = wrap.style.display === 'none';
    wrap.style.display = opening ? 'block' : 'none';
    if (opening) setTimeout(() => document.getElementById('profile-name-input')?.focus(), 60);
  });
  input?.addEventListener('change', async () => {
    if (!input.files || !input.files[0]) return;
    const fd = new FormData();
    fd.append('file', input.files[0]);
    try {
      const res = await fetch(`${API_BASE}/api/profile/me/avatar`, { method: 'POST', headers: { ..._authHeaders() }, body: fd });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
      hapticImpact('light');
      _loadAvatar(String(window._profileMyUserId || ''));
    } catch (e) {
      showToast('Ошибка загрузки аватара: ' + e.message, 'error');
    }
    input.value = '';
  });
  document.getElementById('profile-name-save-btn')?.addEventListener('click', _saveName);
}

async function _loadOwnerSelfProfile() {
  let stats;
  try {
    stats = await api('/api/profile/stats');
  } catch (e) {
    document.getElementById('profile-name').textContent = 'Ошибка загрузки профиля';
    return;
  }
  window._profileMyUserId = stats.user_id;
  document.getElementById('profile-name').textContent = stats.name || 'Владелец';
  const idEl = document.getElementById('profile-telegram-id');
  if (idEl) idEl.textContent = 'Telegram ID: ' + stats.user_id;
  const nameInput = document.getElementById('profile-name-input');
  if (nameInput) nameInput.value = (stats.name && stats.name !== String(stats.user_id)) ? stats.name : '';

  if (stats.has_avatar) _loadAvatar(stats.user_id);
  else {
    const img = document.getElementById('profile-avatar-img');
    const fb = document.getElementById('profile-avatar-fallback');
    if (img) img.style.display = 'none';
    if (fb) fb.style.display = 'block';
  }

  // Компактный статус приложения (версия/SHA из существующего /api/health — новых
  // health-эндпоинтов не добавляем, ТЗ 2.2).
  try {
    const h = await api('/api/health');
    // 22.09 (iPhone screenshot audit): this repo has no separate "version"
    // concept from the deploy commit -- h.version IS already the short SHA
    // (see backend/system_status.py::read_app_version), so `ver` and
    // `commit` used to always be the SAME value, rendering as a visibly
    // duplicated "c28ff97 · c28ff97" on a real device. Show the commit once;
    // only append a second value if h.version is genuinely a DIFFERENT
    // string from it.
    const commit = h.commit ? String(h.commit).slice(0, 7) : '';
    const versionDiffersFromCommit = h.version && h.version !== 'unknown' && h.version !== h.commit && h.version !== commit;
    const label = [commit, versionDiffersFromCommit ? h.version : ''].filter(Boolean).join(' · ') || '—';
    const vEl = document.getElementById('profile-app-version');
    if (vEl) vEl.textContent = label;
  } catch (e) {}
}

// ─────────────────────────── WORKER: полный собственный профиль ───────────────────────────

function _renderWorkerSelfProfile(slot) {
  slot.innerHTML = `
    <div class="profile-header-card">
      <div class="profile-avatar-wrap" id="profile-avatar-wrap" title="Сменить фото">
        <img id="profile-avatar-img" alt="" style="display:none">
        <span id="profile-avatar-fallback"><svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="4"></circle><path d="M4 20c0-4 3.6-7 8-7s8 3 8 7"></path></svg></span>
        <span class="profile-avatar-edit">${PROFILE_ICONS.camera}</span>
      </div>
      <input type="file" id="profile-avatar-input" accept="image/*" style="display:none">
      <div class="profile-header-info">
        <div class="profile-name" id="profile-name">Загрузка…</div>
        <div class="profile-role-badge" id="profile-role-badge"></div>
      </div>
    </div>

    <div class="profile-tabs" id="profile-tabs">
      <div class="profile-tab active" data-tab="me">Мой профиль</div>
      <div class="profile-tab" data-tab="settings">Настройки</div>
    </div>

    <div class="profile-tab-panel" data-panel="me">
      <div class="card ios-card profile-week-card">
        <div class="profile-period-pills" id="profile-period-pills">
          ${Object.keys(PROFILE_PERIOD_LABEL).map(p =>
            `<div class="profile-period-pill${p === _profilePeriod ? ' active' : ''}" data-period="${p}">${PROFILE_PERIOD_LABEL[p]}</div>`
          ).join('')}
        </div>
        <div class="home-section-header" style="padding:0 0 0.5rem;">
          <span class="home-section-title" id="profile-period-title">Часы за 7 дней</span>
          <span class="profile-week-total" id="profile-week-total">—</span>
        </div>
        <div class="profile-week-rings" id="profile-week-rings">
          <div style="color:var(--text-light);font-size:0.85rem">Загрузка…</div>
        </div>
        <a class="profile-csv-link-secondary" id="profile-export-stundenzettel-btn">Скачать табель (CSV)</a>
      </div>

      <div class="card ios-card profile-urlaub-card" id="profile-urlaub-card" style="display:none;">
        <div class="home-section-header" style="padding:0 0 0.5rem;">
          <span class="home-section-title">Отпуск</span>
          <span class="profile-week-total" id="profile-urlaub-remaining">—</span>
        </div>
        <div class="profile-urlaub-bar"><div class="profile-urlaub-bar-fill" id="profile-urlaub-bar-fill"></div></div>
        <div class="profile-urlaub-caption" id="profile-urlaub-caption"></div>
      </div>

      <div class="card ios-card profile-speed-card" id="profile-speed-card" style="display:none"></div>

      <div class="accordion-section profile-settings-list" id="profile-availability-section" style="display:none">
        <div class="accordion-header"><span class="accordion-icon profile-icon-calendar" style="background:var(--icon-bg-2)">${PROFILE_ICONS.calendar}</span><span class="accordion-title">Доступность</span><span class="accordion-chevron">▾</span></div>
        <div class="accordion-body collapsed"><div class="accordion-body-inner">
          <div id="profile-availability-summary" style="font-size:0.85rem;color:var(--text-light)">—</div>
          <button class="submit-btn profile-inline-btn" id="profile-availability-link-btn" type="button" onclick="switchView('abwesenheit')">Открыть календарь →</button>
        </div></div>
      </div>

      <div class="accordion-section profile-settings-list">
        <div class="accordion-header"><span class="accordion-icon profile-icon-object" style="background:var(--icon-bg-5)">${PROFILE_ICONS.object}</span><span class="accordion-title">Объекты</span><span class="accordion-chevron">▾</span></div>
        <div class="accordion-body collapsed"><div class="accordion-body-inner"><div id="profile-objects-list"></div></div></div>
      </div>
    </div>

    <div class="profile-tab-panel" data-panel="settings" style="display:none">
      <div class="accordion-section profile-settings-list">
        <div class="accordion-header"><span class="accordion-icon profile-icon-edit" style="background:var(--icon-bg-4)">${PROFILE_ICONS.edit}</span><span class="accordion-title">Имя</span><span class="accordion-chevron">▾</span></div>
        <div class="accordion-body collapsed"><div class="accordion-body-inner">
          <div style="font-size:0.8rem;color:var(--text-light);margin-bottom:0.5rem">Имя будет видно в чате, задачах и отчётах.</div>
          <input type="text" id="profile-name-input" class="mangel-select" placeholder="Например: Иван" maxlength="100">
          <button class="submit-btn profile-inline-btn" id="profile-name-save-btn" type="button" style="margin-top:0.5rem">Сохранить имя</button>
          <div id="profile-name-status" style="font-size:0.8rem;color:var(--accent);margin-top:0.4rem"></div>
        </div></div>
      </div>

      <div class="accordion-section profile-settings-list">
        <div class="accordion-header"><span class="accordion-icon profile-icon-birthday" style="background:var(--icon-bg-3)">${PROFILE_ICONS.birthday}</span><span class="accordion-title">Дата рождения</span><span class="accordion-chevron">▾</span></div>
        <div class="accordion-body collapsed"><div class="accordion-body-inner">
          <div style="font-size:0.8rem;color:var(--text-light);margin-bottom:0.5rem">Нужна для календаря и напоминаний руководителю. Видна только тебе и руководителю.</div>
          <input type="date" id="profile-birthday-input" class="mangel-select" max="${_profileTodayIso()}">
          <button class="submit-btn profile-inline-btn" id="profile-birthday-save-btn" type="button" style="margin-top:0.5rem">Сохранить дату</button>
          <div id="profile-birthday-status" style="font-size:0.8rem;color:var(--accent);margin-top:0.4rem"></div>
        </div></div>
      </div>

      <div class="accordion-section profile-settings-list">
        <div class="accordion-header"><span class="accordion-icon profile-icon-skills" style="background:var(--icon-bg-1)">${PROFILE_ICONS.skills}</span><span class="accordion-title">Навыки</span><span class="accordion-chevron">▾</span></div>
        <div class="accordion-body collapsed"><div class="accordion-body-inner">
          <div id="profile-skills-chips" class="profile-skills-chips"></div>
          <div id="profile-skills-edit" style="display:none"></div>
          <button class="submit-btn profile-inline-btn" id="profile-skills-edit-btn" type="button">Изменить навыки</button>
        </div></div>
      </div>

      <div class="accordion-section profile-settings-list">
        <div class="accordion-header"><span class="accordion-icon profile-icon-sizes" style="background:var(--icon-bg-2)">${PROFILE_ICONS.sizes}</span><span class="accordion-title">Размеры одежды</span><span class="accordion-chevron">▾</span></div>
        <div class="accordion-body collapsed"><div class="accordion-body-inner">
          <div class="profile-sizes-grid">
            <label>Штаны<select id="profile-size-pants" class="mangel-select"></select></label>
            <label>Футболка<select id="profile-size-shirt" class="mangel-select"></select></label>
            <label>Обувь<input id="profile-size-shoe" class="mangel-select" placeholder="напр. 44"></label>
          </div>
          <button class="submit-btn profile-inline-btn" id="profile-sizes-save-btn" type="button">Сохранить размеры</button>
          <div id="profile-sizes-status" style="font-size:0.8rem;color:var(--accent);margin-top:0.4rem"></div>
        </div></div>
      </div>
    </div>
  `;

  initAccordions(document.getElementById('view-profile'));
  _bindProfileTabs();
  _bindProfileHandlers();
  _loadProfileStats();
  _loadProfileAvailabilitySummary();
}

function _bindProfileTabs() {
  const tabs = document.getElementById('profile-tabs');
  if (!tabs) return;
  tabs.addEventListener('click', e => {
    const tab = e.target.closest('.profile-tab');
    if (!tab) return;
    tabs.querySelectorAll('.profile-tab').forEach(t => t.classList.toggle('active', t === tab));
    document.querySelectorAll('#view-profile .profile-tab-panel').forEach(panel => {
      panel.style.display = panel.dataset.panel === tab.dataset.tab ? '' : 'none';
    });
    hapticImpact('light');
  });
}

// 21.07: worker — summary-строка Urlaub-баланс + deep-link в Календарь.
async function _loadProfileAvailabilitySummary() {
  const section = document.getElementById('profile-availability-section');
  const summaryEl = document.getElementById('profile-availability-summary');
  if (!section || !summaryEl) return;
  try {
    const stats = await api('/api/profile/stats');
    section.style.display = 'block';
    if (stats.urlaub) {
      summaryEl.textContent = `Urlaub: ${stats.urlaub.remaining}/${stats.urlaub.total} осталось`;
    }
  } catch (e) {}
}

// Owner "Доступ": управление whitelist (backend /api/roles). Чисто административная вкладка.
async function _loadProfileTeam() {
  const listEl = document.getElementById('profile-team-list');
  if (!listEl) return;
  try {
    const data = await api('/api/roles');
    const teamAvatar = (uid, name) => `<span class="profile-team-avatar" style="background:hsl(${_chatAvatarHue(uid)} 45% 42%)" onclick="openUserCard('${esc(uid)}')">${(name || '?')[0].toUpperCase()}</span>`;

    const rows = (data.roles || []).map(r => `
      <div class="profile-team-row">
        ${teamAvatar(r.user_id, r.name)}
        <span class="profile-team-name">${esc(r.name)} <span style="color:var(--text-light);font-size:0.75rem">(${r.role === 'owner' ? 'владелец' : 'работник'})</span></span>
        ${r.role !== 'owner' ? `<button class="profile-team-revoke-btn" data-uid="${esc(r.user_id)}">Убрать</button>` : ''}
      </div>`).join('');
    const pendingRows = (data.pending || []).map(p => `
      <div class="profile-team-row">
        ${teamAvatar(p.user_id, p.name)}
        <span class="profile-team-name">${esc(p.name)} <span style="color:var(--warning);font-size:0.75rem">(ожидает доступа)</span></span>
        <button class="profile-team-grant-btn" data-uid="${esc(p.user_id)}">Дать доступ</button>
      </div>`).join('');
    listEl.innerHTML = (rows || '') + (pendingRows || '') || 'Пока никого нет.';
    listEl.querySelectorAll('.profile-team-revoke-btn').forEach(btn => {
      btn.addEventListener('click', async () => {
        if (!await appConfirm('Убрать доступ этому работнику?', { danger: true })) return;
        try {
          await api(`/api/roles/${btn.dataset.uid}`, { method: 'DELETE' });
          hapticImpact('medium');
          _loadProfileTeam();
        } catch (e) { showToast('Ошибка: ' + e.message, 'error'); }
      });
    });
    listEl.querySelectorAll('.profile-team-grant-btn').forEach(btn => {
      btn.addEventListener('click', async () => {
        if (btn.disabled) return;
        btn.disabled = true;
        try {
          await api('/api/roles', { method: 'POST', body: JSON.stringify({ user_id: btn.dataset.uid, role: 'worker' }) });
          hapticImpact('light');
          _loadProfileTeam();
        } catch (e) {
          showToast('Ошибка: ' + e.message, 'error');
          btn.disabled = false;
        }
      });
    });
  } catch (e) {
    listEl.textContent = 'Ошибка загрузки команды: ' + e.message;
  }
}

// Worker-self handlers.
function _bindProfileHandlers() {
  const wrap = document.getElementById('profile-avatar-wrap');
  const input = document.getElementById('profile-avatar-input');
  wrap.addEventListener('click', () => { input.click(); });
  input.addEventListener('change', async () => {
    if (!input.files || !input.files[0]) return;
    const fd = new FormData();
    fd.append('file', input.files[0]);
    try {
      const res = await fetch(`${API_BASE}/api/profile/me/avatar`, { method: 'POST', headers: { ..._authHeaders() }, body: fd });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
      hapticImpact('light');
      _loadAvatar(String(window._profileMyUserId || ''));
    } catch (e) {
      showToast('Ошибка загрузки аватара: ' + e.message, 'error');
    }
    input.value = '';
  });

  document.getElementById('profile-skills-edit-btn').addEventListener('click', _toggleSkillsEdit);
  document.getElementById('profile-sizes-save-btn').addEventListener('click', _saveSizes);
  document.getElementById('profile-name-save-btn').addEventListener('click', _saveName);
  document.getElementById('profile-birthday-save-btn')?.addEventListener('click', _saveBirthday);
  document.getElementById('profile-export-stundenzettel-btn').addEventListener('click', _downloadStundenzettel);

  document.getElementById('profile-period-pills').addEventListener('click', e => {
    const pill = e.target.closest('.profile-period-pill');
    if (!pill || pill.dataset.period === _profilePeriod) return;
    _profilePeriod = pill.dataset.period;
    document.querySelectorAll('#view-profile .profile-period-pill').forEach(p => p.classList.toggle('active', p.dataset.period === _profilePeriod));
    hapticImpact('light');
    _loadProfileStats();
  });
}

async function _downloadStundenzettel() {
  const btn = document.getElementById('profile-export-stundenzettel-btn');
  btn.disabled = true;
  try {
    const targetId = _profileStatsUserId || '';
    const res = await fetch(`${API_BASE}/api/checkin/stundenzettel?user_id=${targetId}`, { headers: { ..._authHeaders() } });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = res.headers.get('Content-Disposition')?.match(/filename="(.+)"/)?.[1] || 'Stundenzettel.csv';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    hapticImpact('light');
  } catch (e) {
    showToast('Ошибка экспорта: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
  }
}

// img src не умеет слать X-Telegram-Init-Data — тянем blob через fetch.
async function _loadAvatar(uid) {
  const img = document.getElementById('profile-avatar-img');
  const fallback = document.getElementById('profile-avatar-fallback');
  if (!img || !uid) return;
  try {
    const res = await fetch(`${API_BASE}/api/profile/${uid}/avatar`, { headers: { ..._authHeaders() } });
    if (!res.ok) return;
    img.src = URL.createObjectURL(await res.blob());
    img.style.display = 'block';
    fallback.style.display = 'none';
  } catch (e) {}
}

async function _loadProfileStats() {
  const params = new URLSearchParams();
  if (_profileStatsUserId) params.set('user_id', _profileStatsUserId);
  params.set('period', _profilePeriod);
  let stats;
  try {
    stats = await api('/api/profile/stats?' + params.toString());
  } catch (e) {
    document.getElementById('profile-name').textContent = 'Ошибка загрузки профиля';
    return;
  }
  if (!_profileStatsUserId) window._profileMyUserId = stats.user_id;

  document.getElementById('profile-name').textContent = stats.name || stats.user_id;
  const roleBadge = document.getElementById('profile-role-badge');
  roleBadge.textContent = stats.role === 'owner' ? 'Владелец' : 'Работник';

  if (stats.has_avatar) _loadAvatar(stats.user_id);
  else {
    const img = document.getElementById('profile-avatar-img');
    const fb = document.getElementById('profile-avatar-fallback');
    if (img) { img.style.display = 'none'; }
    if (fb) { fb.style.display = 'block'; }
  }

  const rings = document.getElementById('profile-week-rings');
  const periodTitle = document.getElementById('profile-period-title');
  const pd = stats.period_data;
  if (stats.period === 'week' || !pd) {
    periodTitle.textContent = 'Часы за 7 дней';
    rings.className = 'profile-week-rings';
    rings.innerHTML = (stats.week || []).map(d => {
      const pct = Math.min(100, d.hours / PROFILE_DAY_NORM_HOURS * 100);
      const hoursLabel = d.hours > 0 ? (Math.round(d.hours * 10) / 10) + '' : '·';
      return `
        <div class="profile-day-ring">
          ${renderRingProgress(pct, 40, 4, hoursLabel)}
          <div class="profile-day-letter">${WEEKDAY_LETTERS[d.weekday]}</div>
        </div>`;
    }).join('');
    document.getElementById('profile-week-total').textContent = (stats.week_total_hours || 0) + ' ч';
  } else if (pd.kind === 'heatmap') {
    periodTitle.textContent = 'Часы за месяц';
    rings.className = 'profile-month-heatmap';
    const maxH = Math.max(1, ...pd.days.map(d => d.hours));
    rings.innerHTML = pd.days.map(d => {
      const intensity = d.hours > 0 ? Math.min(1, d.hours / maxH) : 0;
      return `<div class="profile-heatmap-cell" style="background:color-mix(in srgb, var(--accent) ${Math.round(intensity * 100)}%, var(--bg-card-raised))" title="${d.date}: ${d.hours}ч"></div>`;
    }).join('');
    document.getElementById('profile-week-total').textContent = pd.total_hours + ' ч';
  } else if (pd.kind === 'bar') {
    periodTitle.textContent = stats.period === '3months' ? 'Часы за 3 месяца' : 'Часы за год';
    rings.className = 'profile-period-bar-chart';
    const maxH = Math.max(1, ...pd.buckets.map(b => b.hours));
    rings.innerHTML = pd.buckets.map(b =>
      `<div class="profile-bar-col" title="${b.label}: ${b.hours}ч"><div class="profile-bar-fill" style="height:${Math.round(b.hours / maxH * 100)}%"></div></div>`
    ).join('');
    document.getElementById('profile-week-total').textContent = pd.total_hours + ' ч';
  }

  if (stats.role === 'worker' && stats.urlaub) {
    const card = document.getElementById('profile-urlaub-card');
    card.style.display = 'block';
    const pct = Math.min(100, (stats.urlaub.used / stats.urlaub.total) * 100);
    document.getElementById('profile-urlaub-bar-fill').style.width = pct + '%';
    document.getElementById('profile-urlaub-remaining').textContent = `${stats.urlaub.remaining} дн. осталось`;
    document.getElementById('profile-urlaub-caption').textContent =
      `Использовано ${stats.urlaub.used} из ${stats.urlaub.total} дней в этом году`;
  }

  const speedCard = document.getElementById('profile-speed-card');
  if (stats.work_speed || stats.avg_session_hours) {
    const ws = stats.work_speed;
    const avgPct = stats.avg_session_hours ? Math.min(100, stats.avg_session_hours / 8 * 100) : 0;
    speedCard.style.display = 'block';
    speedCard.innerHTML = `
      <div class="profile-speed-row">
        ${renderRingProgress(avgPct, 56, 5, (stats.avg_session_hours || 0) + 'ч')}
        <div class="profile-speed-text">
          <div style="font-weight:600">Работа по фото</div>
          <div style="font-size:0.8rem;color:var(--text-light)">
            ${ws
              ? `AI-анализов смен: ${ws.analyzed_sessions}. ${ws.last_summary}`
              : 'Средняя длина смены по check-in фото. AI-заметки по прогрессу появятся после первого анализа смены.'}
          </div>
        </div>
      </div>`;
  } else {
    speedCard.style.display = 'block';
    speedCard.innerHTML = `<div style="font-size:0.85rem;color:var(--text-light);display:flex;align-items:center;gap:0.4rem">
      <svg viewBox="0 0 24 24" width="16" height="16" style="flex-shrink:0"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/></svg>
      Работа по фото: пока нет данных — сделайте первый check-in смены с фото на объекте.</div>`;
  }

  const objList = document.getElementById('profile-objects-list');
  const objects = stats.objects || [];
  objList.innerHTML = objects.length
    ? objects.map(o => `
        <div class="profile-object-row">
          <div class="profile-object-name">${esc(o.object_name)}</div>
          <div class="profile-object-meta">
            ${o.sessions ? `${o.sessions} смен · ${o.total_hours} ч` : ''}
            ${o.assigned_stages && o.assigned_stages.length ? ` · назначен: ${esc(o.assigned_stages.join(', '))}` : ''}
            ${o.last_date ? ` · ${o.last_date}` : ''}
          </div>
        </div>`).join('')
    : '<div style="font-size:0.85rem;color:var(--text-light)">Пока нет ни смен, ни назначений на объекты.</div>';

  await _ensureProfileSkillCatalogCache();
  const chips = document.getElementById('profile-skills-chips');
  const skillsV2 = stats.skills_v2 || [];
  chips.innerHTML = skillsV2.length
    ? skillsV2.map(s => {
        const name = _skillLevelDisplayName(s.skill_id);
        const levelLabel = { helper: 'Помощник', independent: 'Самостоятельно', master: 'Мастер' }[s.level] || s.level;
        const verifiedBadge = s.verified ? '<div class="profile-skill-verified">✓ Подтверждено компанией</div>' : '';
        return `<div class="profile-skill-chip-v2">
          <div class="profile-skill-chip-name">${esc(name)}</div>
          <div class="profile-skill-chip-level">${esc(levelLabel)}</div>
          ${verifiedBadge}
        </div>`;
      }).join('')
    : '<div style="font-size:0.85rem;color:var(--text-light)">Навыки не указаны.</div>';

  const nameInput = document.getElementById('profile-name-input');
  if (nameInput) nameInput.value = (stats.name && stats.name !== String(stats.user_id)) ? stats.name : '';

  // Раунд 6 §3.3: worker-self редактирует свою дату рождения (input есть только в
  // worker-self settings; stats.birthday приходит с owner-gated endpoint для себя).
  const bdayInput = document.getElementById('profile-birthday-input');
  if (bdayInput) bdayInput.value = stats.birthday || '';

  _fillSizeSelect('profile-size-pants', PROFILE_PANTS_SIZES, stats.sizes?.pants || '');
  _fillSizeSelect('profile-size-shirt', PROFILE_SHIRT_SIZES, stats.sizes?.shirt || '');
  document.getElementById('profile-size-shoe').value = stats.sizes?.shoe || '';
}

// 27.09 (owner UX request): clothing size no longer requires typing -- a tap
// opens the browser/OS's own compact picker (native <select>, no keyboard),
// per standard upper-body/workwear letter sizes and numeric trouser sizes.
// A legacy/custom stored value that isn't one of these is kept as its own
// selected option (never silently dropped/overwritten) so it stays visible
// and the worker can replace it with a supported size whenever they want.
const PROFILE_SHIRT_SIZES = ['XS', 'S', 'M', 'L', 'XL', 'XXL', '3XL', '4XL', '5XL'];
const PROFILE_PANTS_SIZES = ['44', '46', '48', '50', '52', '54', '56', '58', '60'];

function _fillSizeSelect(elId, options, currentValue) {
  const el = document.getElementById(elId);
  if (!el) return;
  const opts = [...options];
  if (currentValue && !opts.includes(currentValue)) opts.unshift(currentValue);
  el.innerHTML = `<option value="">Не выбрано</option>` +
    opts.map(v => `<option value="${esc(v)}"${v === currentValue ? ' selected' : ''}>${esc(v)}</option>`).join('');
  el.value = currentValue || '';
}

let _profileSkillCatalogByIdCache = null;

async function _ensureProfileSkillCatalogCache() {
  if (_profileSkillCatalogByIdCache) return;
  try {
    const catalog = await api('/api/work-types');
    _profileSkillCatalogByIdCache = {};
    for (const g of catalog.groups) for (const w of g.items) _profileSkillCatalogByIdCache[w.id] = w.name;
    for (const w of catalog.featured) _profileSkillCatalogByIdCache[w.id] = w.name;
  } catch (e) {
    _profileSkillCatalogByIdCache = {};
  }
}

function _skillLevelDisplayName(skillId) {
  return (_profileSkillCatalogByIdCache && _profileSkillCatalogByIdCache[skillId]) || skillId;
}

// Редактирование навыков worker-self через общий skill-picker.js.
let _profileSkillsSheetEl = null;

async function _toggleSkillsEdit() {
  if (_profileStatsUserId) return; // defensive guard
  if (_profileSkillsSheetEl) return;

  const me = await api('/api/profile/me');
  const currentSkillsV2 = me.skills_v2 || [];
  const initialSelected = new Set(currentSkillsV2.map(s => s.skill_id));
  const initialLevels = new Map(currentSkillsV2.map(s => [s.skill_id, s.level]));

  const overlay = document.createElement('div');
  overlay.className = 'bottom-sheet-overlay open';
  overlay.innerHTML = `
    <div class="bottom-sheet-panel profile-skills-sheet">
      <div class="bottom-sheet-handle"></div>
      <div class="form-header">
        <span>Навыки</span>
        <button type="button" class="obj-stage-add-sheet-close" id="profile-skills-sheet-close">✕</button>
      </div>
      <div class="profile-skills-sheet-body" id="profile-skills-sheet-step-picker"></div>
      <div class="profile-skills-sheet-body" id="profile-skills-sheet-step-levels" style="display:none"></div>
      <div class="form-submit-bar">
        <button type="button" class="submit-btn" id="profile-skills-sheet-continue">Продолжить</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);
  document.body.style.overflow = 'hidden';
  _profileSkillsSheetEl = overlay;

  const closeSheet = () => {
    overlay.remove();
    document.body.style.overflow = '';
    _profileSkillsSheetEl = null;
    unregisterOverlay?.();
  };
  const unregisterOverlay = typeof NavigationManager !== 'undefined' ? NavigationManager.registerOverlay(closeSheet) : null;
  document.getElementById('profile-skills-sheet-close').addEventListener('click', closeSheet);

  const pickerContainer = document.getElementById('profile-skills-sheet-step-picker');
  const levelsContainer = document.getElementById('profile-skills-sheet-step-levels');
  const continueBtn = document.getElementById('profile-skills-sheet-continue');

  let stage = 'picker';
  const picker = await createSkillPicker(pickerContainer, {
    initialSelected,
    onChange: (selected) => { continueBtn.disabled = selected.size === 0; },
  });
  continueBtn.disabled = initialSelected.size === 0;

  let levelPicker = null;
  continueBtn.addEventListener('click', async () => {
    if (stage === 'picker') {
      const selectedIds = Array.from(picker.getSelected());
      if (!selectedIds.length) return;
      pickerContainer.style.display = 'none';
      levelsContainer.style.display = 'block';
      continueBtn.textContent = 'Сохранить';
      continueBtn.disabled = true;
      levelPicker = await createSkillLevelPicker(levelsContainer, selectedIds, {
        initialLevels,
        onChange: () => { continueBtn.disabled = !levelPicker.isComplete(); },
      });
      continueBtn.disabled = !levelPicker.isComplete();
      stage = 'levels';
      return;
    }
    const skillsV2 = Array.from(levelPicker.getLevels().entries()).map(([skill_id, level]) => ({ skill_id, level, verified: false }));
    continueBtn.disabled = true;
    continueBtn.textContent = 'Сохраняю...';
    try {
      await api('/api/profile/me', { method: 'PATCH', body: JSON.stringify({ skills_v2: skillsV2 }) });
      hapticImpact('light');
      closeSheet();
      _loadProfileStats();
    } catch (e) {
      showToast('Ошибка сохранения: ' + e.message, 'error');
      continueBtn.disabled = false;
      continueBtn.textContent = 'Сохранить';
    }
  });
}

async function _saveName() {
  // owner-self: _profileStatsUserId пусто -> сохраняем СВОЁ имя. Worker-self: тоже своё.
  // Гард остаётся: чужой uid никогда не редактируется через /api/profile/me.
  if (_profileStatsUserId) return;
  const input = document.getElementById('profile-name-input');
  const statusEl = document.getElementById('profile-name-status');
  const name = input.value.trim();
  if (!name) {
    statusEl.textContent = 'Введи имя';
    statusEl.style.color = 'var(--red)';
    return;
  }
  try {
    await api('/api/profile/me', { method: 'PATCH', body: JSON.stringify({ name }) });
    hapticImpact('light');
    statusEl.style.color = 'var(--accent)';
    statusEl.textContent = '✓ Сохранено';
    const nameEl = document.getElementById('profile-name');
    if (nameEl) nameEl.textContent = name;
    setTimeout(() => { statusEl.textContent = ''; }, 2500);
  } catch (e) {
    statusEl.style.color = 'var(--red)';
    statusEl.textContent = 'Ошибка: ' + e.message;
  }
}

async function _saveSizes() {
  if (_profileStatsUserId) return;
  const statusEl = document.getElementById('profile-sizes-status');
  try {
    await api('/api/profile/me', {
      method: 'PATCH',
      body: JSON.stringify({
        pants_size: document.getElementById('profile-size-pants').value.trim(),
        shirt_size: document.getElementById('profile-size-shirt').value.trim(),
        shoe_size: document.getElementById('profile-size-shoe').value.trim(),
      }),
    });
    hapticImpact('light');
    statusEl.textContent = '✓ Сохранено';
    setTimeout(() => { statusEl.textContent = ''; }, 2500);
  } catch (e) {
    statusEl.textContent = 'Ошибка: ' + e.message;
  }
}

// Раунд 6 §3.3: worker-self сохраняет СВОЮ дату рождения. Тот же гард (_profileStatsUserId
// пусто = свой профиль), та же валидация «не в будущем» (Europe/Berlin), что в onboarding
// и backend; при пустом значении не шлём PATCH (backend валидирует только непустое поле).
async function _saveBirthday() {
  if (_profileStatsUserId) return;
  const input = document.getElementById('profile-birthday-input');
  const statusEl = document.getElementById('profile-birthday-status');
  if (!input || !statusEl) return;
  const bday = input.value.trim();
  if (!bday) {
    statusEl.style.color = 'var(--red)';
    statusEl.textContent = 'Укажи дату рождения';
    return;
  }
  if (bday > _profileTodayIso()) {
    statusEl.style.color = 'var(--red)';
    statusEl.textContent = 'Дата рождения не может быть в будущем';
    return;
  }
  try {
    await api('/api/profile/me', { method: 'PATCH', body: JSON.stringify({ birthday: bday }) });
    hapticImpact('light');
    statusEl.style.color = 'var(--accent)';
    statusEl.textContent = '✓ Сохранено';
    setTimeout(() => { statusEl.textContent = ''; }, 2500);
  } catch (e) {
    statusEl.style.color = 'var(--red)';
    statusEl.textContent = 'Ошибка: ' + e.message;
  }
}

// ─────────────────────────── WORKER CARD — отдельный режим (overlay) ───────────────────────────
// ТЗ 2.3/3: НЕ подмена профиля Owner. Собственные scoped-ID (wc-*), собственный стейт,
// не трогает _profileMyUserId/имя/avatar Owner. Back через NavigationManager.registerOverlay.

let _workerCardEl = null;          // overlay элемент; null = закрыт (двойной тап -> один overlay)
let _workerCardUserId = '';
let _workerCardPeriod = 'week';
let _wcActiveTab = 'today';        // Round 4: активная вкладка Worker Card
let _wcLoadedTabs = new Set();     // Round 4: уже загруженные вкладки (lazy load)

// Раунд 5 §13: диапазон дат для выбранного периода worker card (Неделя/Месяц/3 месяца/Год).
// Локальные компоненты даты (не UTC toISOString) — время фирмы = Europe/Berlin.
function _wcPeriodRange(period) {
  const pad = n => String(n).padStart(2, '0');
  const iso = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const today = new Date();
  let from;
  if (period === 'month') {
    from = new Date(today.getFullYear(), today.getMonth(), 1);
  } else if (period === '3months') {
    from = new Date(today.getFullYear(), today.getMonth() - 2, 1);
  } else if (period === 'year') {
    from = new Date(today.getFullYear(), 0, 1);
  } else { // week: текущая неделя Пн-сегодня
    const dow = (today.getDay() + 6) % 7; // Пн=0
    from = new Date(today.getFullYear(), today.getMonth(), today.getDate() - dow);
  }
  return { date_from: iso(from), date_to: iso(today) };
}
let _workerCardUnreg = null;

// Совместимость: shared.js/openUserCard раньше звал openWorkerFullProfile.
function openWorkerFullProfile(uid) { openWorkerCard(uid); }

function openWorkerCard(uid, returnCtx) {
  if (currentRole !== 'owner') {
    if (typeof openUserCard === 'function') openUserCard(uid);
    return;
  }
  if (_workerCardEl) return; // guard: двойной тап не открывает два overlay
  _workerCardUserId = String(uid);
  _workerCardPeriod = 'week';
  _wcActiveTab = (returnCtx && returnCtx.initialTab) || 'today';
  _wcLoadedTabs = new Set();
  _profileMode = 'worker-card';
  _profileReturnView = returnCtx || null;

  const overlay = document.createElement('div');
  overlay.className = 'worker-card-overlay';
  overlay.id = 'worker-card-overlay';
  overlay.innerHTML = `
    <div class="worker-card-panel">
      <div class="worker-card-header">
        <button type="button" class="worker-card-back" id="wc-back" aria-label="Назад">
          <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"></path></svg>
          <span>Сотрудник</span>
        </button>
      </div>
      <div class="worker-card-identity">
        <div class="worker-card-avatar-wrap">
          <img class="worker-card-avatar" id="wc-avatar-img" alt="" style="display:none">
          <div class="worker-card-avatar-fallback" id="wc-avatar-fallback">?</div>
        </div>
        <div class="worker-card-name" id="wc-name">Загрузка…</div>
        <div class="worker-card-role" id="wc-role">Работник</div>
        <div class="worker-card-shift" id="wc-shift"></div>
        <div class="worker-card-actions" id="wc-actions"></div>
      </div>
      <!-- Round 4: 4-tab system -->
      <div class="wc-tabs" id="wc-tabs">
        <button class="wc-tab${_wcActiveTab === 'today' ? ' active' : ''}" data-wc-tab="today">Сегодня</button>
        <button class="wc-tab${_wcActiveTab === 'productivity' ? ' active' : ''}" data-wc-tab="productivity">Производит.</button>
        <button class="wc-tab${_wcActiveTab === 'calendar' ? ' active' : ''}" data-wc-tab="calendar">Ближайшие</button>
        <button class="wc-tab${_wcActiveTab === 'profile' ? ' active' : ''}" data-wc-tab="profile">Профиль</button>
      </div>
      <div id="wc-panel-today" class="wc-tab-panel${_wcActiveTab !== 'today' ? ' wc-tab-panel-hidden' : ''}">
        <div style="color:var(--text-light);font-size:0.85rem;padding:1rem 0.75rem">Загрузка…</div>
      </div>
      <div id="wc-panel-productivity" class="wc-tab-panel${_wcActiveTab !== 'productivity' ? ' wc-tab-panel-hidden' : ''}">
        <div class="worker-card-ops" id="wc-ops"></div>
        <div class="profile-period-pills" id="wc-period-pills">
          ${Object.keys(PROFILE_PERIOD_LABEL).map(p =>
            `<div class="profile-period-pill${p === _workerCardPeriod ? ' active' : ''}" data-period="${p}">${PROFILE_PERIOD_LABEL[p]}</div>`
          ).join('')}
        </div>
        <div class="worker-card-body" id="wc-body">
          <div style="color:var(--text-light);font-size:0.85rem;padding:1rem 0">Загрузка…</div>
        </div>
      </div>
      <div id="wc-panel-calendar" class="wc-tab-panel${_wcActiveTab !== 'calendar' ? ' wc-tab-panel-hidden' : ''}">
        <div id="wc-calendar-content" style="padding:0.75rem">
          <div style="color:var(--text-light);font-size:0.85rem">Загрузка…</div>
        </div>
      </div>
      <div id="wc-panel-profile" class="wc-tab-panel${_wcActiveTab !== 'profile' ? ' wc-tab-panel-hidden' : ''}">
        <div id="wc-profile-content" style="padding:0.75rem">
          <div style="color:var(--text-light);font-size:0.85rem">Загрузка…</div>
        </div>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);
  document.body.style.overflow = 'hidden';
  _workerCardEl = overlay;

  _workerCardUnreg = (typeof NavigationManager !== 'undefined') ? NavigationManager.registerOverlay(closeWorkerCard) : null;
  overlay.querySelector('#wc-back').addEventListener('click', closeWorkerCard);

  // Tab switching
  overlay.querySelector('#wc-tabs').addEventListener('click', e => {
    const tab = e.target.closest('.wc-tab');
    if (!tab || tab.dataset.wcTab === _wcActiveTab) return;
    _wcSwitchTab(tab.dataset.wcTab);
    hapticImpact('light');
  });

  // Period pills (inside Производительность tab)
  overlay.querySelector('#wc-period-pills').addEventListener('click', e => {
    const pill = e.target.closest('.profile-period-pill');
    if (!pill || pill.dataset.period === _workerCardPeriod) return;
    _workerCardPeriod = pill.dataset.period;
    overlay.querySelectorAll('#wc-period-pills .profile-period-pill').forEach(p => p.classList.toggle('active', p.dataset.period === _workerCardPeriod));
    _wcLoadedTabs.delete('productivity'); // force reload for new period
    hapticImpact('light');
    _loadWorkerCardTab('productivity');
  });

  hapticImpact('light');
  _loadWorkerCardIdentity();
  _loadWorkerCardTab(_wcActiveTab);
}

function _wcSwitchTab(tab) {
  if (!_workerCardEl) return;
  _workerCardEl.querySelectorAll('.wc-tab').forEach(b => b.classList.toggle('active', b.dataset.wcTab === tab));
  _workerCardEl.querySelectorAll('.wc-tab-panel').forEach(p => p.classList.toggle('wc-tab-panel-hidden', p.id !== `wc-panel-${tab}`));
  _wcActiveTab = tab;
  if (!_wcLoadedTabs.has(tab)) _loadWorkerCardTab(tab);
}

function closeWorkerCard() {
  if (_workerCardEl) { _workerCardEl.remove(); _workerCardEl = null; }
  document.body.style.overflow = '';
  _workerCardUserId = '';
  _profileMode = 'owner-self';
  _wcActiveTab = 'today';
  _wcLoadedTabs = new Set();
  if (_workerCardUnreg) { _workerCardUnreg(); _workerCardUnreg = null; }
}

// Round 4: split into identity loader (always) + per-tab loaders (lazy)
async function _loadWorkerCardIdentity() {
  const uid = _workerCardUserId;
  let stats, card;
  try {
    const params = new URLSearchParams({ user_id: uid, period: _workerCardPeriod });
    [stats, card] = await Promise.all([
      api('/api/profile/stats?' + params.toString()),
      api(`/api/users/${encodeURIComponent(uid)}/card`).catch(() => null),
    ]);
  } catch (e) {
    const nameEl = document.getElementById('wc-name');
    if (nameEl) nameEl.textContent = 'Ошибка загрузки';
    return;
  }
  if (!document.getElementById('wc-name')) return; // закрыли пока грузилось

  const nameEl = document.getElementById('wc-name');
  if (nameEl) nameEl.textContent = resolveDisplayName({ profileName: stats.name, userId: stats.user_id });
  const roleEl = document.getElementById('wc-role');
  if (roleEl) roleEl.textContent = stats.role === 'owner' ? 'Владелец' : 'Работник';
  const fb = document.getElementById('wc-avatar-fallback');
  const img = document.getElementById('wc-avatar-img');
  const initials = (stats.name || '?').split(' ').map(p => p[0]).join('').slice(0, 2).toUpperCase();
  if (fb) fb.textContent = initials;
  if (stats.has_avatar && img) {
    img.onload = () => { img.style.display = 'block'; if (fb) fb.style.display = 'none'; };
    if (typeof authImg === 'function') authImg(img, `/api/profile/${uid}/avatar`);
  }

  const shiftEl = document.getElementById('wc-shift');
  if (shiftEl) {
    if (card && card.shift_status === 'working') {
      const mins = card.start_at ? Math.round((Date.now() / 1000 - card.start_at) / 60) : 0;
      const dur = mins >= 60 ? `${Math.floor(mins / 60)} ч ${mins % 60} мин` : `${mins} мин`;
      shiftEl.className = 'worker-card-shift worker-card-shift-active';
      shiftEl.textContent = `● На смене · ${card.object_name || ''}${card.stage_name ? ' · ' + card.stage_name : ''} · ${dur}`;
    } else if (card && card.shift_status === 'idle') {
      shiftEl.className = 'worker-card-shift';
      shiftEl.textContent = 'Сейчас не на смене';
    } else {
      shiftEl.textContent = '';
    }
  }

  const actionsEl = document.getElementById('wc-actions');
  if (actionsEl) {
    const nm = resolveDisplayName({ profileName: stats.name, userId: stats.user_id });
    actionsEl.innerHTML = `
      <button type="button" class="wc-action-btn" id="wc-act-message">Написать</button>
      <button type="button" class="wc-action-btn" id="wc-act-calendar">Календарь</button>
      <button type="button" class="wc-action-btn" id="wc-act-assign">Назначить</button>`;
    document.getElementById('wc-act-message')?.addEventListener('click', () => {
      closeWorkerCard();
      if (typeof openDirectChatWithReturn === 'function') openDirectChatWithReturn(uid, nm, { view: 'worker-card', userId: uid });
    });
    document.getElementById('wc-act-calendar')?.addEventListener('click', () => {
      closeWorkerCard();
      if (typeof switchView === 'function') switchView('abwesenheit');
    });
    document.getElementById('wc-act-assign')?.addEventListener('click', () => {
      if (typeof openAssignmentSheet === 'function') openAssignmentSheet({ userId: uid, userName: nm });
      else showToast('Форма назначения недоступна', 'error');
    });
  }

  // stash card/stats for tab loaders (avoid re-fetching)
  if (_workerCardEl) {
    _workerCardEl._wcStats = stats;
    _workerCardEl._wcCard = card;
  }

  // If productivity tab already visible, fill ops immediately
  if (_wcActiveTab === 'productivity') _fillProductivityTab(stats, card); // async, fire-and-forget
}

async function _loadWorkerCardTab(tab) {
  if (!_workerCardEl) return;
  _wcLoadedTabs.add(tab);

  if (tab === 'today') {
    await _loadWcTodayTab();
  } else if (tab === 'productivity') {
    const stats = _workerCardEl._wcStats;
    const card = _workerCardEl._wcCard;
    if (stats) {
      await _fillProductivityTab(stats, card);
    } else {
      // identity not yet loaded — wait for it then fill.
      // 20.09 (merged from upstream c23894d): раньше этот интервал не имел ни
      // таймаута, ни отмены при закрытии карточки. closeWorkerCard() ставит
      // _workerCardEl = null, после чего условие внутри НИКОГДА не станет
      // истинным -- clearInterval недостижим, таймер тикает 5 раз в секунду до
      // конца жизни мини-аппа, и каждое повторное открытие карточки добавляет
      // ещё один. Сосед ниже (wait() внутри _loadWcProfileTab, строка ~1194)
      // уже делает это правильно -- с 3-секундным предохранителем.
      const wait = setInterval(async () => {
        if (!_workerCardEl) { clearInterval(wait); return; } // карточку закрыли
        if (_workerCardEl._wcStats) {
          clearInterval(wait);
          await _fillProductivityTab(_workerCardEl._wcStats, _workerCardEl._wcCard);
        }
      }, 200);
      setTimeout(() => clearInterval(wait), 3000);
    }
  } else if (tab === 'calendar') {
    _loadWcCalendarTab();
  } else if (tab === 'profile') {
    _loadWcProfileTab();
  }
}

async function _loadWcTodayTab() {
  const uid = _workerCardUserId;
  const panel = document.getElementById('wc-panel-today');
  if (!panel) return;
  try {
    const data = await api(`/api/daily-plan/today?worker_id=${encodeURIComponent(uid)}`);
    if (!document.getElementById('wc-panel-today')) return;
    panel.innerHTML = _wcTodayTabHtml(data);
  } catch (e) {
    if (panel) panel.innerHTML = `<div style="color:var(--red);padding:1rem 0.75rem">Ошибка: ${esc(e.message)}
      <button type="button" class="wo-retry-btn" onclick="_loadWcTodayTab()">Повторить</button></div>`;
  }
}

function _wcTodayTabHtml(data) {
  if (!data.has_plan) {
    return `<div class="wc-today-empty">
      <div style="font-size:1.5rem;margin-bottom:0.5rem">📋</div>
      <div style="font-weight:600;margin-bottom:0.25rem">План на сегодня не опубликован</div>
      <div style="font-size:0.82rem;color:var(--text-light)">${esc(data.date || '')}</div>
    </div>`;
  }
  const plan = data.plan;
  const acc = data.acceptance;
  const carryovers = data.carryovers || [];
  const statusLabel = {
    draft: 'Черновик',
    published: acc ? 'Принят' : 'Ожидает принятия',
    amendment_pending: 'Изменения не подтверждены',
    accepted: 'Принят',
    completed: 'Завершён',
  }[plan.status] || plan.status;

  const statusColor = {
    draft: 'var(--text-light)',
    published: acc ? 'var(--accent)' : 'var(--warning-text)',
    amendment_pending: 'var(--warning-text)',
    accepted: 'var(--accent)',
    completed: 'var(--text-light)',
  }[plan.status] || 'var(--text-main)';

  const itemsHtml = (plan.items || []).map((item, i) => `
    <div class="wc-today-item">
      <span class="wc-today-item-num">${i + 1}</span>
      <div class="wc-today-item-body">
        <div class="wc-today-item-title">${esc(item.title)}</div>
        ${item.planned_quantity != null ? `<div class="wc-today-item-meta">${item.planned_quantity} ${esc(item.unit || '')}${item.time_estimate_hours ? ' · ~' + item.time_estimate_hours + ' ч' : ''}</div>` : ''}
      </div>
    </div>`).join('');

  const carryoverHtml = carryovers.length ? `
    <div class="wc-today-carryover">
      <div style="font-weight:600;font-size:0.85rem;color:var(--warning-text);margin-bottom:0.4rem">⚠ Переносы с предыдущих дней (${carryovers.length})</div>
      ${carryovers.map(c => `<div class="wc-today-item-meta">↩ ${esc(c.item_title || c.item_id)} — ${c.remaining_quantity != null ? c.remaining_quantity + ' ' + esc(c.unit || '') : ''}</div>`).join('')}
    </div>` : '';

  return `
    <div style="padding:0.75rem">
      <div class="wc-today-status-row">
        <span class="wc-today-status-badge" style="color:${statusColor}">${esc(statusLabel)}</span>
        <span style="font-size:0.75rem;color:var(--text-light)">v${plan.version} · ${esc(plan.date)}</span>
      </div>
      <div style="font-size:0.8rem;color:var(--text-light);margin-bottom:0.75rem">${esc(plan.stage_key || '')}</div>
      ${carryoverHtml}
      <div class="wc-today-items">${itemsHtml}</div>
      ${acc ? `<div style="font-size:0.75rem;color:var(--text-light);margin-top:0.5rem">Принят в ${new Date(acc.accepted_at * 1000).toLocaleTimeString('ru', {hour:'2-digit', minute:'2-digit'})}</div>` : ''}
    </div>`;
}

async function _fillProductivityTab(stats, card) {
  const opsEl = document.getElementById('wc-ops');
  if (opsEl) opsEl.innerHTML = _workerCardOpsHtml(stats, card);
  const body = document.getElementById('wc-body');
  if (!body) return;

  body.innerHTML = _workerCardBodyHtml(stats, _workerCardUserId);
  document.getElementById('wc-export-csv')?.addEventListener('click', async (ev) => {
    const btn = ev.currentTarget;
    btn.disabled = true;
    try {
      const { date_from, date_to } = _wcPeriodRange(_workerCardPeriod);
      const res = await fetch(`${API_BASE}/api/checkin/stundenzettel?user_id=${encodeURIComponent(_workerCardUserId)}&date_from=${date_from}&date_to=${date_to}`, { headers: { ..._authHeaders() } });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = res.headers.get('Content-Disposition')?.match(/filename="(.+)"/)?.[1] || 'Stundenzettel.csv';
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
      hapticImpact('light');
    } catch (e) {
      showToast('Ошибка экспорта: ' + e.message, 'error');
    } finally {
      btn.disabled = false;
    }
  });

  // Async: append effective rate card
  const prodSlot = document.createElement('div');
  prodSlot.id = 'wc-productivity-rates';
  prodSlot.innerHTML = '<div class="card" style="margin-top:0.75rem"><div style="color:var(--text-light);font-size:0.85rem">Загрузка производительности…</div></div>';
  body.appendChild(prodSlot);
  try {
    const data = await api(`/api/productivity/workers/${encodeURIComponent(_workerCardUserId)}`);
    if (!document.getElementById('wc-productivity-rates')) return;
    prodSlot.innerHTML = _wcProductivityRatesHtml(data);
  } catch (e) {
    if (document.getElementById('wc-productivity-rates'))
      prodSlot.innerHTML = '';
  }
}

function _wcProductivityRatesHtml(data) {
  const aggs = Object.values(data.aggregates || {});
  if (!aggs.length) {
    return `<div class="card" style="margin-top:0.75rem">
      <div class="home-section-header" style="padding:0 0 0.5rem"><span class="home-section-title">Производительность</span></div>
      <div style="font-size:0.85rem;color:var(--text-light)">Нет данных о производительности. Появятся после завершения смен с планом.</div>
    </div>`;
  }
  const rows = aggs.map(a => {
    const rate = a.effective_rate != null ? a.effective_rate.toFixed(2) : '—';
    const unit = (data.observations || []).find(o => o.work_type_id === a.work_type_id)?.unit || '';
    const samples = a.observation_count || 0;
    const hours = a.total_person_hours != null ? a.total_person_hours.toFixed(1) : '—';
    return `<div class="wc-prod-rate-row">
      <div class="wc-prod-rate-type">${esc(a.work_type_id)}</div>
      <div class="wc-prod-rate-val">${rate}${unit ? ' ' + esc(unit) : ''}/ч</div>
      <div class="wc-prod-rate-meta">${samples} замер.&thinsp;·&thinsp;${hours} ч</div>
    </div>`;
  }).join('');
  return `<div class="card" style="margin-top:0.75rem">
    <div class="home-section-header" style="padding:0 0 0.5rem"><span class="home-section-title">Производительность</span></div>
    ${rows}
  </div>`;
}

async function _loadWcCalendarTab() {
  const uid = _workerCardUserId;
  const panel = document.getElementById('wc-calendar-content');
  if (!panel) return;
  try {
    const today = new Date();
    const pad = n => String(n).padStart(2, '0');
    const iso = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    const from = iso(new Date(today.getFullYear(), today.getMonth(), 1));
    const to = iso(new Date(today.getFullYear(), today.getMonth() + 2, 0));
    const data = await api(`/api/abwesenheit?user_id=${encodeURIComponent(uid)}&date_from=${from}&date_to=${to}`).catch(() => ({ entries: [] }));
    if (!document.getElementById('wc-calendar-content')) return;
    const entries = data.entries || [];
    if (!entries.length) {
      panel.innerHTML = `<div style="color:var(--text-light);font-size:0.85rem">Нет запланированных недоступностей</div>
        <button type="button" class="submit-btn" style="margin-top:0.75rem;width:100%" onclick="closeWorkerCard();switchView('abwesenheit')">Открыть полный календарь</button>`;
    } else {
      const typeLabel = { urlaub: 'Отпуск', krankheit: 'Больничный', sonstiges: 'Другое' };
      panel.innerHTML = entries.slice(0, 5).map(e =>
        `<div class="wc-today-item" style="margin-bottom:0.4rem">
          <span class="wc-today-item-num" style="background:var(--warning-bg,rgba(255,180,0,0.15));color:var(--warning-text)">!</span>
          <div class="wc-today-item-body">
            <div class="wc-today-item-title">${esc(typeLabel[e.type] || e.type)}</div>
            <div class="wc-today-item-meta">${esc(e.date_from)} – ${esc(e.date_to)}</div>
          </div>
        </div>`
      ).join('') + `<button type="button" class="submit-btn" style="margin-top:0.75rem;width:100%" onclick="closeWorkerCard();switchView('abwesenheit')">Открыть полный календарь</button>`;
    }
  } catch (e) {
    if (document.getElementById('wc-calendar-content'))
      document.getElementById('wc-calendar-content').innerHTML = `<div style="color:var(--red);font-size:0.85rem">Ошибка загрузки</div>`;
  }
}

async function _loadWcProfileTab() {
  const panel = document.getElementById('wc-profile-content');
  if (!panel) return;
  // Reuse stats already loaded by identity
  const wait = () => new Promise(r => {
    const id = setInterval(() => {
      if (_workerCardEl?._wcStats) { clearInterval(id); r(_workerCardEl._wcStats); }
    }, 100);
    setTimeout(() => { clearInterval(id); r(null); }, 3000);
  });
  const stats = _workerCardEl?._wcStats || await wait();
  if (!document.getElementById('wc-profile-content')) return;
  if (!stats) {
    panel.innerHTML = `<div style="color:var(--text-light)">Нет данных</div>`;
    return;
  }
  const skillsV2 = stats.skills_v2 || [];
  const sizes = stats.sizes || {};
  const hasSizes = sizes.pants || sizes.shirt || sizes.shoe;
  panel.innerHTML = `
    <div class="card" style="margin-bottom:0.75rem">
      <div class="home-section-header" style="padding:0 0 0.5rem"><span class="home-section-title">Навыки</span></div>
      <div class="profile-skills-chips">${skillsV2.length ? skillsV2.map(s => {
        const name = _skillLevelDisplayName(s.skill_id);
        const lvl = { helper: 'Помощник', independent: 'Самостоятельно', master: 'Мастер' }[s.level] || s.level;
        return `<div class="profile-skill-chip-v2"><div class="profile-skill-chip-name">${esc(name)}</div><div class="profile-skill-chip-level">${esc(lvl)}</div>${s.verified ? '<div class="profile-skill-verified">✓ Подтверждено</div>' : ''}</div>`;
      }).join('') : '<div style="font-size:0.85rem;color:var(--text-light)">Навыки не указаны.</div>'}</div>
    </div>
    ${hasSizes ? `<div class="card">
      <div class="home-section-header" style="padding:0 0 0.5rem"><span class="home-section-title">Размеры одежды</span></div>
      <div class="profile-object-meta" style="font-size:0.85rem">
        ${sizes.pants ? 'Штаны: ' + esc(sizes.pants) : ''}${sizes.shirt ? ' · Футболка: ' + esc(sizes.shirt) : ''}${sizes.shoe ? ' · Обувь: ' + esc(sizes.shoe) : ''}
      </div>
    </div>` : ''}
    ${stats.birthday ? `<div class="card" style="margin-top:0.75rem">
      <div class="home-section-header" style="padding:0 0 0.5rem"><span class="home-section-title">Дата рождения</span></div>
      <div style="font-size:0.9rem">${esc(_fmtBirthday(stats.birthday))}</div>
    </div>` : ''}
  `;
}

// 04.08 (задача 7.2): блок оперативной информации — на смене/объект/работа/сегодня/неделя.
function _workerCardOpsHtml(stats, card) {
  const working = card && card.shift_status === 'working';
  // Сегодня — часы из недельного массива за текущий день недели (Пн=0), Europe/Berlin
  // считается на бэкенде; здесь сопоставляем локальный день только для выбора элемента.
  const todayIdx = (new Date().getDay() + 6) % 7;
  const todayEntry = (stats.week || []).find(d => d.weekday === todayIdx);
  const todayHours = todayEntry ? (Math.round(todayEntry.hours * 10) / 10) : 0;
  const weekHours = stats.week_total_hours || 0;
  const rows = [
    `<div class="wc-ops-row"><span>Сейчас</span><b>${working ? 'на смене' : 'не на смене'}</b></div>`,
  ];
  if (working && card.object_name) rows.push(`<div class="wc-ops-row"><span>Объект</span><b>${esc(card.object_name)}</b></div>`);
  if (working && card.stage_name) rows.push(`<div class="wc-ops-row"><span>Работа</span><b>${esc(card.stage_name)}</b></div>`);
  rows.push(`<div class="wc-ops-row"><span>Сегодня</span><b>${todayHours} ч</b></div>`);
  rows.push(`<div class="wc-ops-row"><span>Неделя</span><b>${weekHours} ч</b></div>`);
  // Раунд 6 §3.3: полную дату рождения видит только Owner/сам (stats.birthday приходит
  // лишь на owner-gated endpoint) — показываем в Worker Card как ДД.ММ.ГГГГ.
  if (stats.birthday) rows.push(`<div class="wc-ops-row"><span>Дата рождения</span><b>${esc(_fmtBirthday(stats.birthday))}</b></div>`);
  return `<div class="wc-ops-card">${rows.join('')}</div>`;
}

// Раунд 6 §3.3: YYYY-MM-DD -> ДД.ММ.ГГГГ (без вычисления возраста).
function _fmtBirthday(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || ''));
  return m ? `${m[3]}.${m[2]}.${m[1]}` : String(iso || '');
}

// Раунд 6 §3.3: граница «не в будущем» для input[type=date] max — Europe/Berlin
// (todayBerlin из shared.js), как в onboarding и backend _validate_birthday.
function _profileTodayIso() {
  return (typeof todayBerlin === 'function')
    ? todayBerlin()
    : new Intl.DateTimeFormat('en-CA', { timeZone: 'Europe/Berlin' }).format(new Date());
}

function _workerCardBodyHtml(stats, uid) {
  // Часы (period viz)
  const pd = stats.period_data;
  let hoursTitle, hoursHtml, hoursTotal;
  if (stats.period === 'week' || !pd) {
    hoursTitle = 'Часы за 7 дней';
    hoursHtml = `<div class="profile-week-rings">${(stats.week || []).map(d => {
      const pct = Math.min(100, d.hours / PROFILE_DAY_NORM_HOURS * 100);
      const lbl = d.hours > 0 ? (Math.round(d.hours * 10) / 10) + '' : '·';
      return `<div class="profile-day-ring">${renderRingProgress(pct, 40, 4, lbl)}<div class="profile-day-letter">${WEEKDAY_LETTERS[d.weekday]}</div></div>`;
    }).join('')}</div>`;
    hoursTotal = (stats.week_total_hours || 0) + ' ч';
  } else if (pd.kind === 'heatmap') {
    hoursTitle = 'Часы за месяц';
    const maxH = Math.max(1, ...pd.days.map(d => d.hours));
    hoursHtml = `<div class="profile-month-heatmap">${pd.days.map(d => {
      const intensity = d.hours > 0 ? Math.min(1, d.hours / maxH) : 0;
      return `<div class="profile-heatmap-cell" style="background:color-mix(in srgb, var(--accent) ${Math.round(intensity * 100)}%, var(--bg-card-raised))" title="${d.date}: ${d.hours}ч"></div>`;
    }).join('')}</div>`;
    hoursTotal = pd.total_hours + ' ч';
  } else {
    hoursTitle = stats.period === '3months' ? 'Часы за 3 месяца' : 'Часы за год';
    const maxH = Math.max(1, ...pd.buckets.map(b => b.hours));
    hoursHtml = `<div class="profile-period-bar-chart">${pd.buckets.map(b =>
      `<div class="profile-bar-col" title="${b.label}: ${b.hours}ч"><div class="profile-bar-fill" style="height:${Math.round(b.hours / maxH * 100)}%"></div></div>`
    ).join('')}</div>`;
    hoursTotal = pd.total_hours + ' ч';
  }

  const urlaubHtml = stats.urlaub ? `
    <div class="card">
      <div class="home-section-header" style="padding:0 0 0.5rem;">
        <span class="home-section-title">Отпуск</span>
        <span class="profile-week-total">${stats.urlaub.remaining} дн. осталось</span>
      </div>
      <div class="profile-urlaub-bar"><div class="profile-urlaub-bar-fill" style="width:${Math.min(100, (stats.urlaub.used / stats.urlaub.total) * 100)}%"></div></div>
      <div class="profile-urlaub-caption">Использовано ${stats.urlaub.used} из ${stats.urlaub.total} дней в этом году${stats.krankheit_days_this_year ? ` · больничных ${stats.krankheit_days_this_year}` : ''}</div>
    </div>` : '';

  const objects = stats.objects || [];
  const objectsHtml = `
    <div class="card">
      <div class="home-section-header" style="padding:0 0 0.5rem;"><span class="home-section-title">Объекты</span></div>
      ${objects.length ? objects.map(o => `
        <div class="profile-object-row">
          <div class="profile-object-name">${esc(o.object_name)}</div>
          <div class="profile-object-meta">
            ${o.sessions ? `${o.sessions} смен · ${o.total_hours} ч` : ''}
            ${o.assigned_stages && o.assigned_stages.length ? ` · назначен: ${esc(o.assigned_stages.join(', '))}` : ''}
            ${o.last_date ? ` · ${o.last_date}` : ''}
          </div>
        </div>`).join('') : '<div style="font-size:0.85rem;color:var(--text-light)">Пока нет ни смен, ни назначений.</div>'}
    </div>`;

  const skillsV2 = stats.skills_v2 || [];
  const skillsHtml = `
    <div class="card">
      <div class="home-section-header" style="padding:0 0 0.5rem;"><span class="home-section-title">Навыки</span></div>
      <div class="profile-skills-chips">${skillsV2.length ? skillsV2.map(s => {
        const name = _skillLevelDisplayName(s.skill_id);
        const levelLabel = { helper: 'Помощник', independent: 'Самостоятельно', master: 'Мастер' }[s.level] || s.level;
        return `<div class="profile-skill-chip-v2"><div class="profile-skill-chip-name">${esc(name)}</div><div class="profile-skill-chip-level">${esc(levelLabel)}</div>${s.verified ? '<div class="profile-skill-verified">✓ Подтверждено компанией</div>' : ''}</div>`;
      }).join('') : '<div style="font-size:0.85rem;color:var(--text-light)">Навыки не указаны.</div>'}</div>
    </div>`;

  const sizes = stats.sizes || {};
  const hasSizes = sizes.pants || sizes.shirt || sizes.shoe;
  const sizesHtml = hasSizes ? `
    <div class="card">
      <div class="home-section-header" style="padding:0 0 0.5rem;"><span class="home-section-title">Размеры одежды</span></div>
      <div class="profile-object-meta" style="font-size:0.85rem">
        ${sizes.pants ? `Штаны: ${esc(sizes.pants)}` : ''}${sizes.shirt ? ` · Футболка: ${esc(sizes.shirt)}` : ''}${sizes.shoe ? ` · Обувь: ${esc(sizes.shoe)}` : ''}
      </div>
    </div>` : '';

  return `
    <div class="card profile-week-card">
      <div class="home-section-header" style="padding:0 0 0.5rem;">
        <span class="home-section-title">${hoursTitle}</span>
        <span class="profile-week-total">${hoursTotal}</span>
      </div>
      ${hoursHtml}
    </div>
    ${urlaubHtml}
    ${objectsHtml}
    ${skillsHtml}
    ${sizesHtml}
    <button class="submit-btn profile-inline-btn" id="wc-export-csv" type="button">Скачать табель (CSV)</button>
  `;
}
