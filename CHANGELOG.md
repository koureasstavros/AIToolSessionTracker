## 📦 Changelog

[](https://github.com/koureasstavros/AIToolSessionTracker/blob/main/changelog.md#-changelog)

This changelog provides a concise record of the updates made to AI Tool Session Tracker, including new providers, features, documentation, improvements, and fixes. Entries are summarized from the repository commit history.

### Version v0.0.38 — 2026-09-17

- Added xAI Cursor provider
- Added Cognition Devin provider
- Fixed OTEL provider support handling

### Version v0.0.37 — 2026-09-16

- Stabilized progressive lazy loading of sessions
- Added session process cache
- Added missing license file

### Version v0.0.36 — 2026-09-15

- Added session analysis and best practices pop up view

### Version v0.0.35 — 2026-09-15

- Added sorting into table columns on statistics view
- Added migration lifecycle from json to database for costs, mappings, routings
- Fixed issues with table row valuess modifications and row deletions on save

### Version v0.0.34 — 2026-09-14

- Added support for model / cost configuration
- Added support for otel with settings and service (experimental)

### Version v0.0.33 — 2026-09-13

- Added label live into currently active sessions
- Added label effort for model selected reasoning

### Version v0.0.32 — 2026-09-12

- Added support for model / deployment mapping

### Version v0.0.31 — 2026-09-11

- Added support for other operating systems

### Version v0.0.30 — 2026-09-11

- Incorporating progressive lazy loading for sessions

### Version v0.0.29 — 2026-09-11

- Reconciling token numbers and costs into different levels
- Added model analysis overview per operational session and handling

### Version v0.0.28 — 2026-09-10

- Added hierarchy handling for subagents including context content management

### Version v0.0.27 — 2026-09-10

- Added hierarchy handling for subagents including orchistration and agent cost

### Version v0.0.26 — 2026-09-10

- Added timestamp for each session-turn
- Added token warning flag system for missing token categories or missing tokens fields

### Version v0.0.25 — 2026-09-10

- Fixed immediate shutdown of the app
- Added logging mechanism into system file

### Version v0.0.24 — 2026-09-10

- Added shutdown command to close app
- Added handling of multiple instances

### Version v0.0.23 — 2026-09-10

- Fixed instructions file with proper version

### Version v0.0.22 — 2026-09-10

- Added compilation mechanism for platforms and packed releases

### Version v0.0.21 — 2026-09-10

- Improved the README
- Improved file names and project structure
- Added session examples for agent validation

### Version v0.0.20 — 2026-09-09

- Fixed provider surface identification when providers store data in the same or different locations

### Version v0.0.19 — 2026-09-09

- Added Claude Code internal instructions to input-context handling

### Version v0.0.18 — 2026-09-09

- Added loading handling between views and within statistics sub-pages

### Version v0.0.17 — 2026-09-08

- Fixed Claude Code internal-instruction identification.
- Fixed duplicated Google Antigravity sessions caused by chunked data

### Version v0.0.16 — 2026-09-08

- Added Google Antigravity provider support
- Added Google Antigravity session analysis and token-estimation mechanisms
- Reordered the provider list
- Added provider notes and test cases

### Version v0.0.15 — 2026-09-08

- Added copy-to-clipboard support for useful fields

### Version v0.0.14 — 2026-09-07

- Added documentation
- Added context-content visibility in the input view as separate sections

### Version v0.0.13 — 2026-09-07

- Added statistic-chart links to operational sessions
- Added selection of the current day as a statistics group

### Version v0.0.12 — 2026-09-07

- Added token-cost relationships
- Added the statistics view
- Added expand and collapse controls
- Improved subagent representation
- Added additional frontend and backend enhancements

### Version v0.0.11 — 2026-09-01

- Added and tested session import and export.
- Fixed import and export behavior across multiple follow-up commits

### Version v0.0.10 — 2026-08-28

- Added additional information to the main README

### Version v0.0.9 — 2026-08-28

- Restructured session, turn, and invocation logic

### Version v0.0.8 — 2026-08-28

- Fixed GitHub Copilot usage-summary handling
- Fixed OpenAI Codex usage-summary handling

### Version v0.0.7 — 2026-08-27

- Fixed tool-call handling
- Fixed turn-to-step handling

### Version v0.0.6 — 2026-08-27

- Aligned provider documentation
- Added source-deduplication fixes
- Aligned the source-tool interface

### Version v0.0.5 — 2026-08-27

- Restructured platform files.

### Version v0.0.4 — 2026-08-27

- Refactored frontend and backend operations
- Added main and provider-specific instructions

### Version v0.0.3 — 2026-08-27

- Split provider-specific logic from the main platform

### Version v0.0.2 — 2026-08-26

- Added frontend and backend enhancements

### Version v0.0.1 — 2026-08-26

- Created the initial version of AI Tool Session Explorer
