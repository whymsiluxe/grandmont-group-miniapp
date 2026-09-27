// Свайп-навигация между вкладками: звук + вибрация + slide-анимация (Фаза 1, must-have).
// Экран "выезжает" в направлении жеста и новый "въезжает" с противоположной стороны.

const SWIPE_VIEWS = ['feed', 'home', 'chat', 'objects', 'profile'];
const SWIPE_THRESHOLD = 50;
const SWIPE_ANIM_MS = 220;

let touchStartX = 0;
let touchStartY = 0;
let currentViewIndex = 0;
let _touchStartOnExcludedEl = false;

// Системный фикс (не растущий список): любой контейнер с data-no-swipe исключён из
// глобального свайп-жеста — новые горизонтально-интерактивные элементы (drag, месяц-grid,
// period-pills) помечаются этим атрибутом сразу при создании, а не патчатся здесь централизованно.
function _isExcludedSwipeTarget(target) {
  const el = target instanceof Element ? target : target?.parentElement;
  // 27.09 (owner report: skill-add sheet "closing" from a stray tap/scroll):
  // .bottom-sheet-overlay is a FULL-SCREEN fixed layer -- any tap above the
  // panel (empty backdrop area) or a horizontal-ish drag while scrolling the
  // panel's content is still, structurally, a touch on this document-level
  // listener. It was never excluded, so a swipe-shaped gesture anywhere on an
  // open bottom sheet (skills, assignment, edit-period/work-type, etc.) could
  // fire a real view switch underneath it -- looking, to the user, exactly
  // like the sheet "closing" itself. Excluding the shared overlay class once
  // here covers every current and future sheet built on it, not just this one.
  return !!el?.closest?.('[data-no-swipe], .bottom-sheet-overlay, .mangel-card, .mangel-kanban, .doc-type-switch, .chat-category-tabs, .nav-item-start, #radio-fab, .checkin-status-modal, .feed-photo-img-wrap, #view-object-detail, .obj-filter-row, .filters, .profile-tabs, .profile-period-pills, .wx-city-tabs, .wx-object-tabs, .home-radio-stations-viewport, .chat-bubble');
}

function animateSwipeTransition(fromViewName, toViewName, direction) {
  const fromEl = document.getElementById(`view-${fromViewName}`);
  const toEl = document.getElementById(`view-${toViewName}`);
  if (!fromEl || !toEl) { switchView(toViewName); return; }

  const outDir = direction === 'left' ? '-100%' : '100%';
  const inDir = direction === 'left' ? '100%' : '-100%';

  // fromEl держим видимым поверх потока (swipe-exiting), пока switchView() скрывает его через .active
  fromEl.classList.add('swipe-exiting');
  fromEl.style.transition = 'none';
  fromEl.style.transform = 'translateX(0)';

  switchView(toViewName);

  toEl.style.transition = 'none';
  toEl.style.transform = `translateX(${inDir})`;

  requestAnimationFrame(() => {
    fromEl.style.transition = `transform ${SWIPE_ANIM_MS}ms ease-out`;
    toEl.style.transition = `transform ${SWIPE_ANIM_MS}ms ease-out`;
    fromEl.style.transform = `translateX(${outDir})`;
    toEl.style.transform = 'translateX(0)';
  });

  setTimeout(() => {
    fromEl.classList.remove('swipe-exiting');
    fromEl.style.transition = '';
    fromEl.style.transform = '';
    toEl.style.transition = '';
    toEl.style.transform = '';
  }, SWIPE_ANIM_MS + 20);
}

document.addEventListener('touchstart', e => {
  touchStartX = e.changedTouches[0].screenX;
  touchStartY = e.changedTouches[0].screenY;
  _touchStartOnExcludedEl = _isExcludedSwipeTarget(e.target);
}, false);

document.addEventListener('touchend', e => {
  if (_touchStartOnExcludedEl) return;
  const touchEndX = e.changedTouches[0].screenX;
  const touchEndY = e.changedTouches[0].screenY;
  const diff = touchEndX - touchStartX;
  const diffY = touchEndY - touchStartY;
  if (Math.abs(diff) < SWIPE_THRESHOLD) return;
  // Вертикальный скролл (список сообщений в чате/ИИ и т.п.) не должен триггерить смену таба —
  // считаем жест свайпом между вкладками только если горизонтальное движение доминирует над вертикальным.
  if (Math.abs(diff) < Math.abs(diffY)) return;

  const direction = diff > 0 ? 'right' : 'left';
  let newIdx = currentViewIndex;

  if (direction === 'right' && currentViewIndex > 0) {
    newIdx = currentViewIndex - 1;
  } else if (direction === 'left' && currentViewIndex < SWIPE_VIEWS.length - 1) {
    newIdx = currentViewIndex + 1;
  } else {
    return;
  }

  const fromView = SWIPE_VIEWS[currentViewIndex];
  const toView = SWIPE_VIEWS[newIdx];
  currentViewIndex = newIdx;

  animateSwipeTransition(fromView, toView, direction);
  playSwipeSound(direction);
  hapticImpact('light');
}, false);

document.addEventListener('click', e => {
  const navItem = e.target.closest('.nav-item');
  if (navItem) {
    const viewName = navItem.dataset.view;
    const idx = SWIPE_VIEWS.indexOf(viewName);
    if (idx >= 0) currentViewIndex = idx;
  }
}, true);
