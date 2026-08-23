# Contrarian interface system

## Product experience

Contrarian is an investor decision desk, not a collection of market modules. Every primary surface follows the same order: portfolio state, risk, required action, research opportunity, evidence. HK, CN, and US are filters and provenance labels rather than separate product destinations.

Funded A-share, HK-share, and US-share positions belong to the live portfolio. BTC remains a watch asset until a data and position provider is configured. Watch assets never enter portfolio value, P&L, or formal trade notifications.

## Navigation

- 今日: decision summary and prioritized work.
- 组合: all broker accounts in one table.
- 研究: security discovery and analysis across markets.
- 验证: historical and forward strategy evidence.

Legacy specialist pages remain reachable from contextual links instead of occupying primary navigation.

## Visual system

- Paper: `#f3f5f7`; panels: white; primary ink: `#172033`.
- Navigation and decision surface: `#13243a`.
- Opportunity/action blue: `#1769aa`; risk red: `#bf3038`; loss/negative green follows the existing Chinese-market convention.
- Tables carry dense numeric information; tabular numerals are used only for measurements.
- Panels use 14px corners. Small state tags and controls use restrained 5-9px corners.
- Borders establish structure; shadow is reserved for the single leading decision surface.

## Interaction rules

- Show action and risk before evidence.
- A failed data source must not block the other markets.
- Error copy states what is unavailable and what the user can check; raw provider errors stay hidden.
- Empty states never manufacture opportunities.
- All account and strategy features remain read-only; the user confirms every real trade.
- Mobile keeps the same decision order and permits horizontal scrolling only inside wide position tables.
