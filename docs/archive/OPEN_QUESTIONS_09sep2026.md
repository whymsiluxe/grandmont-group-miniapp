> **SUPERSEDED 2026-09-18** — historical snapshot, kept for context/git-blame only, not maintained. See [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md) for current state.

# Open Questions — 09.09.2026 evening

## Item 6: Worker profile — what exactly is "the duplicate" to remove?

**Owner's words**: "у сотрудника два раза календарь нахуя?" (screenshot: tabs Сегодня / Производит. / Календарь / Профиль)

**What we know**:
- There is a "Календарь" tab in the worker-profile screen
- There is also a "Открыть полный календарь" button somewhere in the same screen
- Owner considers one of these redundant

**What we do NOT know** (need owner to clarify before touching anything):
- Is it the tab OR the button that should be removed?
- Is the "Производит." tab actually "Производительность" and is THAT the duplicate of something else?
- Are the two "calendars" showing the same data, or different things (e.g., tab = personal absence calendar, button = full team calendar)?

**Where to look**: `profile.js` — worker-card section, rendering of tabs and the "Открыть полный календарь" action.

**Action needed from owner**: look at the worker-profile screen and confirm which element to remove.
