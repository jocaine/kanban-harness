# Wiki Schema

## Domain

Kanban Harness (KH) — 本地部署的 AI 团队编排引擎。本 wiki 管理项目的架构知识和产品决策记忆。

## Conventions

- File names: lowercase, hyphens, no spaces (e.g., `layer2-infra.md`)
- Every wiki page starts with YAML frontmatter (see below)
- Use `[[wikilinks]]` to link between pages (minimum 2 outbound links per page)
- When updating a page, always bump the `updated` date
- Every new page must be added to `index.md` under the correct section
- Every action must be appended to `log.md`

## Frontmatter

```yaml
---
type: arch | product
updated: YYYY-MM-DD
tags: [from taxonomy below]
---
```

## Tag Taxonomy

### Architecture tags
- runtime, infra, orchestration, roles, frontend
- database, scheduler, session, task, buffer
- api, mcp, sse, docker

### Product tags
- positioning, decision, workflow, principle
- rejected, user-profile, roadmap

## Page Thresholds

- **Create a page** when a module/concept is referenced from 2+ other pages
- **Split a page** when it exceeds ~200 lines
- **Don't create a page** for minor implementation details derivable from code

## Update Policy

- Architecture pages: update when code structure changes (new modules, moved files, changed responsibilities)
- Product pages: update when CEO makes a decision, rejects a direction, or confirms a workflow
- When new info conflicts with existing content, note both with dates and flag for CEO review
