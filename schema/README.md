# AgentKB Schema

Defines the compilation rules, wiki conventions, and operational workflows for AgentKB.

## Architecture

AgentKB follows Karpathy's LLM Knowledge Base pattern:

```
Layer 1: daily/          — Immutable session logs (raw data)
Layer 2: knowledge/      — LLM-compiled articles (concepts, connections, qa)
Layer 3: schema/         — Compilation rules and conventions (this directory)
```

## Conventions

- **File naming**: lowercase, hyphens for spaces (e.g., `claude-code-hooks.md`)
- **Frontmatter**: Required YAML with title, sources, created, updated
- **Wikilinks**: `[[path/to/article]]` without `.md` extension
- **Style**: Encyclopedia-style, factual, self-contained
- **Dates**: ISO 8601 (YYYY-MM-DD)
- **Sources**: Every article links back to contributing daily logs

## Scope

AgentKB is a **cross-project** knowledge base. It stores knowledge that is:
- Not bound to a single project
- Valuable across multiple repositories
- Durable (not ephemeral session state)

Project-specific knowledge stays in project-level KBs (e.g., Mercury_KB).

## Relationship to Project KBs

```
AgentKB (cross-project, NAS-backed)
├── knowledge from Mercury
├── knowledge from SoT
├── knowledge from future projects
└── cross-cutting patterns and decisions

Mercury_KB (project-specific, local)
├── Mercury research reports
├── Mercury decisions
├── Mercury task/issue records
└── Mercury-specific context
```

AgentKB receives compiled knowledge from all project KBs. Project KBs remain
the authoritative source for project-specific detail; AgentKB holds the
cross-cutting, durable subset.

## Compile Pipeline

```
Source                    →  daily/YYYY-MM-DD.md     →  knowledge/
─────────────────────────────────────────────────────────────────
Claude Code session logs     Raw extraction             Compiled articles
Mercury auto memory          (flush.py or skill)        (compile.py or skill)
Research reports
Decision records
```

## NAS Sync

Local working copy: `$AGENTKB_DIR` (e.g. `D:/Mercury/AgentKB`)
NAS backup: `/share/CACHEDEV1_DATA/AgentKB` (file sync via `scripts/rsync-to-nas.ps1`, no git on NAS)
Sync frequency: hourly via Windows Task Scheduler
