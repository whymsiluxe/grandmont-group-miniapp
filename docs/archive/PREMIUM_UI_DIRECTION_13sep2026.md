> **SUPERSEDED 2026-09-18** — historical snapshot, kept for context/git-blame only, not maintained. See [BACKLOG.md](BACKLOG.md) for current state.

# Premium UI Direction - 2026-09-13

Status: refined after live Telegram screenshot batch from 2026-09-13 23:20-23:21 and static code audit. This is the current visual direction for the next autonomous UI pass.

## Product Intent

Grandmont Group miniapp must feel like a premium iPhone work app for a construction company: calm, fast, tactile, and obvious for workers on a site. The app is not a marketing landing page. It is an operational tool, so polish must improve clarity and reduce taps.

Target references:

- iOS system apps for typography, spacing, sheets, forms, segmented controls, and motion.
- Instagram for feed photo cards, media viewer, comments, reactions, and save/share action layout.
- Apple Notes/Reminders style for task lists, object details, daily summary, and manager cockpit.

Avoid continuing the current mixed direction where screens combine Old Money, Telegram, Instagram, Connecteam, dark chat, emoji controls, and inline one-off styles.

## Live Screenshot Audit - 2026-09-13

Owner feedback: colors differ everywhere, fonts look inconsistent, and the app feels raw instead of premium.

Root cause:

- The app has multiple visual systems active at once:
  - cinematic black/gold splash,
  - beige "Old Money" cards,
  - heavy dark glass bottom navigation,
  - Instagram dark comments,
  - green chat bubbles,
  - dark calendar,
  - emoji weather/media,
  - large bold dashboard typography,
  - mixed pill/tab/button styles.
- The font family is mostly tokenized, but the perceived font inconsistency comes from uncontrolled weights, uppercase labels, letter spacing, inconsistent font sizes, and many one-off component rules.
- The palette is not actually one palette in use. It is forest green + cream + brass + black + red + dark grey + emoji colors + multiple translucent overlays.
- The UI uses too many "big" elements at the same time: huge titles, huge cards, huge pills, huge nav, huge buttons. Premium iOS UI needs hierarchy, not everything shouting.

### Screen Findings

Splash:

- Looks like a separate luxury poster, not the same app as the light screens.
- Black/gold atmosphere is polished alone, but it creates a hard visual jump into the beige dashboard.
- Recommendation: either simplify splash into the same premium light/green brand system, or make the main app adopt a subtler version of the splash language. Do not keep two unrelated identities.

Dashboard/Home:

- KPI tiles use very large numbers and all-caps labels; they feel heavy and more like a mockup than an iOS dashboard.
- Radio card is visually from another app: dark block, chips, glass controls, strong contrast.
- Rows like Messages/Calendar/Tools/Documents/AI/Contracts look like identical oversized cards with too little information density.
- Team schedule and object blocks use yet another style with big uppercase titles and nested card feeling.
- Recommendation: replace the home stack with one owner "Today" cockpit card, compact KPI row, clean action list, and a lighter audio widget or remove radio from core operational dashboard.

Worker Card:

- Header has good structure, but avatar, tabs, pills, and cards do not share one component language.
- Tabs are cramped and use labels like "Производит." that feel technical/truncated.
- Productivity circles look decorative but not informative when all values are zero.
- Empty profile/skills screen wastes a full viewport with one card.
- Recommendation: worker profile should be a native iOS profile sheet: avatar/name/status, three primary actions, segmented tabs, compact stat cards only when data exists, graceful empty states.

Feed Weather:

- Weather list is cleaner than old weather posts, but emoji weather icons make it less premium.
- Warning section color is brass/gold while active tabs are green and saved chips are black/cream: too many states.
- Recommendation: use a consistent list-row component with SF Symbol/lucide weather icons, one severity accent strip, and uniform chip style.

Feed Photo:

- Photo card is closer to Instagram but still trapped inside beige app-card styling.
- Add photo button is a full-width heavy green bar; on Instagram-like feed it feels like an admin form action, not a feed composer.
- Media is too tall/large with bottom nav covering the action/caption area.
- Recommendation: media-first card with less outer chrome, composer action as a compact iOS row/button, action row always visible above nav spacing.

Feed News:

- News cards are readable, but typography is too heavy and large for every card.
- Category pill, source, title, body, actions all compete.
- Recommendation: Apple News-like hierarchy: smaller source/category row, strong but not oversized title, lighter summary, same action icons as photos/weather.

Comments:

- This is the most coherent screen visually. The Instagram-like dark sheet works.
- Still needs polish: top back button circle is too large/bright, avatars use gradient circles that feel unrelated, emoji row is bright/noisy.
- Recommendation: keep dark comments, but tune spacing, avatar style, and action sheet to one reusable component.

Chat List:

- Large empty whitespace and oversized worker/search circles make the chat list feel unfinished.
- Category chips differ from feed chips and profile tabs.
- Recommendation: Apple Messages-like compact list with search inline, horizontal category filter if needed, no oversized empty top area.

Object Chat:

- Chat composer has useful actions (attach, mic, location), but white/cream composer contrasts with green message bubbles and beige page in a way that feels patched together.
- Empty chat message is floating in the middle with no object context.
- Recommendation: use the same composer geometry as comments, with light iOS Messages style for object chats or full dark style for all conversation views, not both.

Object Detail:

- The object detail has useful blocks, but it looks like a form/admin panel rather than a premium object workspace.
- "Center management" stat grid is too flat; object photo/status/team/description/works are stacked as independent blocks without a clear story.
- Empty states ("not added") dominate the page.
- Recommendation: first screen should be an object cockpit:
  - hero/photo or compact header,
  - status/stage/risk summary,
  - team and shift state,
  - next actions,
  - tabs below.
  Move long editable admin sections deeper or collapse them.

Plan Work:

- Stage card is closer to a proper operational card.
- Drag handle, status dot, stage card and red outline button do not align visually.
- Recommendation: make stages a clear timeline/list with one card style, status pill, due/risk info, and compact problem button.

Profile:

- Profile header is decent but oversized and sparse.
- Access list is functional, but buttons/cards differ from the rest of the app.
- Version/system info looks like developer UI, not owner-facing premium settings.
- Recommendation: iOS Settings style list groups; app/system info in small rows; primary actions lower visual weight.

Tools:

- Empty state is too bare; KPI cards at top are another component style.
- Search field is clipped/truncated.
- Recommendation: use iOS list/search pattern; empty state with clear "add tool" owner action if role allows.

Calendar:

- Dark calendar is one of the strongest mismatches with the light app.
- On one screenshot half the screen is dark and bottom remains light, creating a broken/overlay feeling.
- Recommendation: choose either fully light calendar to match app, or make it a true fullscreen dark mode with matching nav and no split background. For consistency, prefer light.

Team:

- Team/Control/Needs pages are functional but feel like wireframes: big headings, raw separators, empty sections, inconsistent chips.
- Recommendation: make these manager cockpit screens use the same summary-card + list-row system.

Alerts:

- Alert bottom sheet is useful and understandable.
- Emoji bell and colored circles reduce premium feel.
- Recommendation: use icon buttons and severity symbols from the shared icon system; keep the sheet pattern.

## Global Design Rules

- One design language across all tabs: iOS-like surfaces, consistent typography, consistent icon buttons, consistent bottom sheets.
- Use Apple/SF system font first, Manrope fallback. Keep the current font token direction.
- Use restrained neutral surfaces:
  - App background: `#F7F4ED` or similar light warm neutral.
  - Primary surface: `#FFFFFF`.
  - Secondary grouped surface: `#EFEAE1`.
  - Text primary: near-black `#1C1C1E`, not pure black.
  - Text secondary: iOS-like muted gray/taupe.
  - Dark surfaces only for fullscreen chat/comment/media contexts where Instagram-like immersion is intentional.
- Accent color should be Grandmont Group forest green; brass/gold only for small status/accent details.
- Red/yellow/green must be semantic, not decorative. Avoid brass/gold for generic section titles.
- Do not introduce decorative blobs, random gradients, fake 3D emoji icons, or novelty effects.
- Prefer thin SVG/lucide-style icons over emoji labels in controls.
- Text in compact controls must be short and never wrap awkwardly.
- Keep cards at 8-14px radius unless the component is a true modal/sheet or floating nav.
- Avoid nested cards. A card can contain rows/sections, but not multiple floating cards inside another floating card.
- Inline style patches should be removed over time in favor of component classes.
- Motion should be subtle: press scale, sheet slide, small FAB spring. Respect `prefers-reduced-motion`.

## Typography Fix

Use one scale, not one-off weights:

- Page title: 34-38px, 800, only top-level screens.
- Screen/subtitle title: 24-28px, 750.
- Card title: 17-20px, 700.
- Body: 16-17px, 400-500.
- Meta: 13-14px, 500.
- Button label: 16-17px, 650.
- Avoid all-caps except very small section labels. Current large uppercase section labels make screens feel loud and less Apple-like.
- Remove letter spacing from normal Russian text. Use it only for tiny meta labels if needed.

## Component System To Build First

Before restyling individual screens, define/reuse these classes:

- `.ios-page`
- `.ios-section`
- `.ios-card`
- `.ios-list`
- `.ios-list-row`
- `.ios-segmented`
- `.ios-chip`
- `.ios-action-button`
- `.ios-icon-button`
- `.ios-bottom-sheet`
- `.ios-empty-state`
- `.ios-status-pill`
- `.ios-stat-tile`

Acceptance: feed tabs, object tabs, profile tabs, team tabs, saved filters, and date filters should all look like variants of one segmented/chip system.

## App Shell

Current issue:

- Header, Telegram safe-area handling, bottom nav, chat fullscreen, and comments all use separate visual rules.
- The bottom nav is visually heavy and can make screens feel toy-like.

Direction:

- Keep the floating nav, but make it visually lighter: lower shadow, cleaner active state, consistent icon stroke.
- Header should feel native iOS: large centered title on top-level screens, smaller sticky title after scroll, no visual conflict with Telegram Close/Menu buttons.
- Use one safe-area/sheet/keyboard strategy for chat, AI, comments, object creation, and finish wizard.

## Feed

Current issue:

- Photo feed is structurally close to Instagram, but card containment and media sizing still feel like a generic card list.
- News cards use a Telegram-channel style, weather cards use another visual language.
- Saved filters are per tab now, but presentation should feel like a native segmented/filter bar.

Direction:

- Photo tab:
  - Media-first Instagram card.
  - Header: avatar, author, object/subtitle, timestamp. No oversized beige blocks around media.
  - Media: full-width fixed aspect ratio, swipeable carousel, dots/count overlay, no black empty blocks unless media truly failed.
  - Actions: heart, comment, share left; bookmark right; identical icon sizing to weather/news.
  - Caption: compact, author bold, one or two lines before expansion if long.
- News tab:
  - Make news cards calmer and closer to Apple News/Notes: white surface, readable title, short body, source/date, same action row icons as photo.
  - Comment sheet must be the same shared sheet as photo.
- Weather tab:
  - Rename/preserve as `Погода`, not `Инфо`.
  - Weather posts can keep richer visual media, but action row must be the same Instagram-like icon system.
  - Saved weather must never mix with saved photos/news.
- Fresh counters:
  - Small iOS badge on each feed subtab only when non-zero.
  - Counts should not change tab height or shift layout.

## Comments

Current issue:

- Photo/news comments now share logic, but the UI is still implemented as separate DOM blocks.
- The dark sheet is correct for Instagram-like comments, but it must be polished and stable.

Direction:

- Comments are always a bottom popup/sheet, never a separate route.
- Use one visual system for photo, news, weather, and future object/feed comments.
- Backdrop tap closes reliably when tapping above the sheet.
- Input composer never jumps after send; newly sent comment scrolls into view.
- Quick emoji row must respond with haptic/visual feedback and append/send consistently.
- `...` actions open a small dark action sheet above comments.
- Worker can delete only own comments; owner/admin can moderate according to backend role rules.
- When keyboard opens, only composer/sheet content adjusts; background feed should not flash through.

## Chat

Current issue:

- Chat has its own dark fullscreen layout and recently borrowed comment bottom sheets, but thread switching can still look like a stale-content flash if not guarded.

Direction:

- Chat list: Apple Messages-like list, clean rows, avatar, name, preview, time, unread badge.
- Chat thread: dark or iOS Messages-like, but must use the same composer geometry and action sheet language as comments.
- When opening a new chat, clear old thread immediately and show skeleton/loading state before new messages render.
- Message actions should use sheets, not browser confirm.
- Location send should be a clear icon action in the composer/action tray.

## Objects

Current issue:

- Object cards can feel busy: big image, avatar overlaps, status pill, stage card, budget row, floating plus.
- Creation sheet is improved but still uses generic form blocks.

Direction:

- Object card should be a premium work card:
  - Real object photo or calm placeholder.
  - Clear object title, address, status.
  - Small team/avatar row.
  - Stage progress as compact strip, not a big card inside a card.
  - Weather/material/risk mini indicators inside the object context.
  - Owner-only object actions stay inside object detail/card context.
- Add-object FAB:
  - Fixed, aligned above nav, no collision with mini-player.
  - Press animation and reduced-motion fallback.
  - Should feel intentional, not “floating crooked”.
- New object sheet:
  - Native iOS form sections.
  - Inputs never zoom/jump on iPhone.
  - Submit bar stable above keyboard.

## Finish Shift / Check-In

Current state from code audit:

- Start shift already requests geolocation through `_uploadCheckinPhotos`.
- Finish wizard already enforces at least two photos, summary text, finish geolocation, and has voice buttons for text steps.

Direction:

- Start shift:
  - Must clearly communicate GPS capture and 2-4 start photos.
  - If GPS fails, worker sees practical retry text, not a generic error.
- Finish shift:
  - Sequential wizard remains: photos first, text/voice summary, plan/fact, extra work, issues/needs, geolocation, review.
  - The visual presentation should become iOS setup-flow style:
    - progress pill,
    - large step title,
    - one primary action,
    - secondary back action,
    - no crowded form stacks.
  - Additional work voice input should be obvious: microphone icon button next to text input, recording state, transcription status.
  - “Finish” must remain impossible without 2+ photos and finish GPS.

## Manager Cockpit

Direction:

- Home for owner should include an operational daily summary card:
  - greeting,
  - object count,
  - worker count,
  - who is working now,
  - who has not started,
  - overdue/unconfirmed tasks,
  - material/delay risks.
- Use Apple Reminders-like severity rows:
  - neutral info,
  - yellow warning after 2 hours no task confirmation,
  - red after 4 hours no confirmation.
- Central AI/voice action should create a draft first, not silently mutate important data.
- Broadcast controls should feel like a command center action sheet:
  - object audience,
  - whole company audience,
  - message preview,
  - send confirmation.

## Implementation Priorities

1. Freeze the design system: tokens, typography, button/icon sizes, sheets, cards, empty/error/loading states.
2. Normalize global shell: header, bottom nav, page padding, safe-area, tab/chip system.
3. Refactor feed cards visually without changing backend behavior.
4. Keep comments dark/Instagram-like but polish as a reusable component.
5. Refactor dashboard/home into a manager cockpit, not a stack of unrelated cards.
6. Refactor object detail into an object cockpit with collapsible admin sections.
7. Normalize chat list/thread/composer actions to one conversation system.
8. Polish finish wizard visual flow and voice affordances.
9. Add manager AI/voice/broadcast controls after the visual foundation is stable.

## Acceptance Criteria For Next UI Pass

- No screen uses a unique tab/chip style unless justified by platform convention.
- No screen has mixed light/dark halves unless it is a deliberate fullscreen mode.
- Bottom nav does not cover primary card actions or important content.
- All cards use one of the shared card/list row styles.
- Empty states are compact and actionable, not large blank pages.
- Emoji are not used as primary functional icons except inside user-generated reactions/comments.
- Weather icons should move away from emoji to a shared icon style.
- Page titles and section titles follow the typography scale.
- Inline styles are not added for new UI; use classes.
- Screenshot review after implementation should compare Home, Feed Photos, Feed News, Feed Weather, Comments, Chat List, Object Detail, Profile, Tools, Calendar, Team, Needs, and Alerts.
