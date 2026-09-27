// Единый компонент выбора навыков -- используется onboarding.js (шаги 2-3) и
// profile.js (Профиль → Настройки → Навыки). Единственный источник данных --
// GET /api/work-types (backend/work_types.py), никаких статичных копий каталога
// здесь или где-либо ещё во frontend.
//
// Два режима работы:
//  - createSkillPicker(container, {onChange}) -- выбор набора skill_id (шаг 2 onboarding,
//    открытие редактирования навыков в профиле). featured-карточки сверху + поиск +
//    группы-аккордеоны, один тап переключает выбор, featured и группа -- одно состояние.
//  - createSkillLevelPicker(container, selectedIds, {onChange}) -- уровень для каждого
//    уже выбранного навыка (шаг 3 onboarding).

let _skillCatalogCache = null;

async function _loadSkillCatalog() {
  if (_skillCatalogCache) return _skillCatalogCache;
  _skillCatalogCache = await api('/api/work-types');
  return _skillCatalogCache;
}

function _allCatalogItems(catalog) {
  const items = [];
  for (const g of catalog.groups) items.push(...g.items);
  return items;
}

// 27.09 (owner UX request): replaces the old separate "frequently used" grid
// (a fixed editorial `featured` flag per work type, not real usage) with a
// real per-device usage count, so items a worker actually picks rise toward the
// top of their own section over time. Local-only (per device), no backend change.
const SKILL_USAGE_STORAGE_KEY = 'grandmont-group-skill-usage-v1';

function _loadSkillUsage() {
  try {
    const raw = localStorage.getItem(SKILL_USAGE_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    return (parsed && typeof parsed === 'object') ? parsed : {};
  } catch (e) {
    return {};
  }
}

function _bumpSkillUsage(id) {
  try {
    const usage = _loadSkillUsage();
    usage[id] = (usage[id] || 0) + 1;
    localStorage.setItem(SKILL_USAGE_STORAGE_KEY, JSON.stringify(usage));
  } catch (e) {}
}

/**
 * createSkillPicker(container, opts)
 *   opts.initialSelected -- Set<string> skill_id, предвыбранные навыки
 *   opts.onChange(selectedSet) -- вызывается при каждом изменении выбора
 * Возвращает { getSelected(): Set<string>, destroy() }.
 */
async function createSkillPicker(container, opts = {}) {
  const selected = new Set(opts.initialSelected || []);
  const onChange = opts.onChange || (() => {});
  // 03.08: singleSelect -- Assignment Sheet и "Изменить вид работ" выбирают ОДИН
  // work_type_id на назначение, но пикер по умолчанию multi-select (нужен для
  // onboarding/профиля). Раньше вызывающий код имитировал single-select вручную --
  // ловил onChange, если Set вырос до 2 элементов, чистил его и делал
  // picker.destroy()+пересоздание, что физически уничтожало и заново создавало DOM
  // (интерфейс на миг исчезал, а внешний selectedId мог не успеть обновиться до
  // следующего рендера). Теперь single-select -- встроенный режим: toggle() сам
  // снимает предыдущий выбор перед добавлением нового, picker никогда не уничтожается.
  const singleSelect = !!opts.singleSelect;
  // 09.09: hideFeatured used to hide the old separate frequently-used grid
  // for Assignment Sheet specifically. 27.09: that whole grid was removed
  // for every caller (see the redesign above) -- kept as an accepted, now
  // no-op option so existing call sites passing it don't need to change.
  const hideFeatured = !!opts.hideFeatured; // eslint-disable-line no-unused-vars
  // 09.09: allLabel -- owner попросил "Все навыки" -> "Виды работ" в контексте
  // Assignment Sheet (это ровно то же понятие, что owner уже переименовал в других
  // местах интерфейса по исходному 15-пунктному плану) -- опция вместо жёсткой
  // замены текста, чтобы онбординг (широкий "навыки работника") не поменялся заодно.
  const allLabel = opts.allLabel || 'Все навыки';
  let catalog;
  try {
    catalog = await _loadSkillCatalog();
  } catch (e) {
    container.innerHTML = `<div class="skill-picker-error">Не удалось загрузить список работ. <button type="button" class="skill-picker-retry-btn">Повторить</button></div>`;
    container.querySelector('.skill-picker-retry-btn')?.addEventListener('click', () => {
      _skillCatalogCache = null;
      createSkillPicker(container, opts);
    });
    return { getSelected: () => selected, destroy: () => {} };
  }

  let searchQuery = '';
  // 27.09: frozen once per picker instance (i.e. once per open) -- toggling a
  // skill bumps the persisted usage count immediately (see toggle()), but the
  // ORDER used by this render() never changes mid-session from that, so the
  // list can never jump under the user's finger while they're selecting.
  // Reopening the picker (a fresh createSkillPicker() call) reads a new
  // snapshot and re-sorts.
  const usageSnapshot = _loadSkillUsage();
  const sortedGroups = catalog.groups.map(g => ({
    ...g,
    items: [...g.items].sort((a, b) => (usageSnapshot[b.id] || 0) - (usageSnapshot[a.id] || 0)),
  }));

  function isSelected(id) { return selected.has(id); }

  function toggle(id) {
    if (selected.has(id)) {
      selected.delete(id);
    } else if (singleSelect) {
      selected.clear();
      selected.add(id);
      _bumpSkillUsage(id);
    } else {
      selected.add(id);
      _bumpSkillUsage(id);
    }
    render();
    onChange(selected);
  }

  // 27.09: whole-section select. State is a simple tri-state derived from
  // how many of the group's own items are currently selected -- no separate
  // stored flag to drift out of sync with individual toggles.
  function _groupState(g) {
    const count = g.items.filter(w => isSelected(w.id)).length;
    if (count === 0) return 'none';
    if (count === g.items.length) return 'all';
    return 'partial';
  }

  function toggleGroup(g) {
    const state = _groupState(g);
    if (state === 'all') {
      g.items.forEach(w => selected.delete(w.id));
    } else {
      g.items.forEach(w => { if (!selected.has(w.id)) _bumpSkillUsage(w.id); selected.add(w.id); });
    }
    render();
    onChange(selected);
  }

  function render() {
    const q = searchQuery.trim().toLowerCase();

    let groupsHtml;
    if (q) {
      const matches = _allCatalogItems(catalog).filter(w =>
        w.name.toLowerCase().includes(q) || (w.keywords || []).some(k => k.toLowerCase().includes(q))
      );
      groupsHtml = `<div class="skill-picker-search-results">${matches.map(w => _skillRowHtml(w, isSelected(w.id))).join('') || '<div class="skill-picker-empty">Ничего не найдено</div>'}</div>`;
    } else {
      groupsHtml = sortedGroups.map(g => {
        const state = _groupState(g);
        const stateGlyph = state === 'all' ? '☑' : (state === 'partial' ? '◐' : '☐');
        const selectedCount = g.items.filter(w => isSelected(w.id)).length;
        return `
          <div class="skill-picker-group">
            <div class="skill-picker-group-header">
              <button type="button" class="skill-picker-group-select-all" data-group-select="${g.id}"
                      title="${state === 'all' ? 'Снять весь раздел' : 'Выбрать весь раздел'}"
                      aria-label="${state === 'all' ? 'Снять весь раздел' : 'Выбрать весь раздел'}">${stateGlyph}</button>
              <span class="skill-picker-group-name">${_escSkill(g.name)}</span>
              ${selectedCount ? `<span class="skill-picker-group-badge">${selectedCount}</span>` : ''}
            </div>
            <div class="skill-picker-group-body">${g.items.map(w => _skillRowHtml(w, isSelected(w.id))).join('')}</div>
          </div>
        `;
      }).join('');
    }

    // 01.08 (доп.раунд П7, реальный найденный баг): проверка `document.activeElement
    // === searchInput` ниже сравнивала СТАРЫЙ фокусированный элемент с НОВЫМ (после
    // innerHTML пересоздания DOM) -- always false, .focus() никогда не вызывался,
    // фокус слетал после каждой буквы поиска. Снимаем состояние фокуса/курсора ДО
    // замены DOM, восстанавливаем ПОСЛЕ на новом элементе.
    const prevSearchInput = container.querySelector('.skill-picker-search-input');
    const wasSearchFocused = document.activeElement === prevSearchInput;
    const prevSelectionStart = prevSearchInput ? prevSearchInput.selectionStart : null;
    const prevSelectionEnd = prevSearchInput ? prevSearchInput.selectionEnd : null;

    container.innerHTML = `
      <div class="skill-picker-all-label">${_escSkill(allLabel)}</div>
      <input type="search" class="skill-picker-search-input" placeholder="Поиск..." value="${_escSkill(searchQuery)}">
      <div class="skill-picker-groups">${groupsHtml}</div>
    `;

    container.querySelectorAll('[data-skill-id]').forEach(el => {
      el.addEventListener('click', () => toggle(el.dataset.skillId));
    });
    container.querySelectorAll('[data-group-select]').forEach(el => {
      el.addEventListener('click', (e) => {
        e.stopPropagation();
        const gid = el.dataset.groupSelect;
        const g = sortedGroups.find(x => x.id === gid);
        if (g) toggleGroup(g);
      });
    });
    const searchInput = container.querySelector('.skill-picker-search-input');
    searchInput?.addEventListener('input', (e) => {
      searchQuery = e.target.value;
      render();
    });
    if (wasSearchFocused && searchInput) {
      searchInput.focus();
      if (prevSelectionStart !== null) {
        searchInput.setSelectionRange(prevSelectionStart, prevSelectionEnd);
      }
    }
  }

  render();
  return { getSelected: () => selected, destroy: () => { container.innerHTML = ''; } };
}

function _skillRowHtml(w, selected) {
  return `
    <button type="button" class="skill-picker-row${selected ? ' selected' : ''}" data-skill-id="${w.id}">
      <span class="skill-picker-row-name">${_escSkill(w.name)}</span>
      ${selected ? '<span class="skill-picker-check">✓</span>' : ''}
    </button>
  `;
}

/**
 * createSkillLevelPicker(container, skillIds, opts)
 *   skillIds -- array<string> уже выбранных skill_id
 *   opts.initialLevels -- Map<skill_id, level> предзаполненные уровни (редактирование)
 *   opts.onChange(levelsMap) -- вызывается при каждом изменении
 * Возвращает { getLevels(): Map<string,string>, isComplete(): bool, destroy() }.
 */
async function createSkillLevelPicker(container, skillIds, opts = {}) {
  const levels = new Map(opts.initialLevels || []);
  const onChange = opts.onChange || (() => {});
  const catalog = await _loadSkillCatalog();
  const byId = {};
  _allCatalogItems(catalog).forEach(w => { byId[w.id] = w; });
  for (const w of catalog.featured) byId[w.id] = w; // featured items тоже должны резолвиться

  const LEVEL_LABELS = { helper: 'Помощник', independent: 'Самостоятельно', master: 'Мастер' };

  function setLevel(skillId, level) {
    levels.set(skillId, level);
    render();
    onChange(levels);
  }

  function isComplete() {
    return skillIds.every(id => levels.has(id));
  }

  function render() {
    const doneCount = skillIds.filter(id => levels.has(id)).length;
    container.innerHTML = `
      <div class="skill-level-progress">Уровень указан: ${doneCount} из ${skillIds.length}</div>
      <div class="skill-level-explain">
        <div><b>Помощник</b> — работаю под руководством</div>
        <div><b>Самостоятельно</b> — выполняю работу сам</div>
        <div><b>Мастер</b> — отвечаю за результат и могу руководить</div>
      </div>
      <div class="skill-level-list">
        ${skillIds.map(id => {
          const name = byId[id] ? byId[id].name : id;
          const current = levels.get(id);
          return `
            <div class="skill-level-row">
              <div class="skill-level-row-name">${_escSkill(name)}</div>
              <div class="skill-level-buttons">
                ${['helper', 'independent', 'master'].map(lv => `
                  <button type="button" class="skill-level-btn${current === lv ? ' selected' : ''}" data-skill-id="${id}" data-level="${lv}">${LEVEL_LABELS[lv]}</button>
                `).join('')}
              </div>
            </div>
          `;
        }).join('')}
      </div>
    `;
    container.querySelectorAll('.skill-level-btn').forEach(btn => {
      btn.addEventListener('click', () => setLevel(btn.dataset.skillId, btn.dataset.level));
    });
  }

  render();
  return { getLevels: () => levels, isComplete, destroy: () => { container.innerHTML = ''; } };
}

function _escSkill(s) {
  const div = document.createElement('div');
  div.textContent = s == null ? '' : String(s);
  return div.innerHTML;
}
