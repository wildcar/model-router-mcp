# Environment

Host facts, tools, credentials, and command cheat-sheet for this project.
Update whenever a new tool, credential, or host-specific quirk is learned.

## Host

If the project runs in more than one place (e.g. local dev + a server), split per environment.

- **Dev**: <OS / shell / user / repo path>
- **Prod**: <OS / shell / service user / deploy path / config path>

## Tools

- CLI tools, runtimes (pin versions where they matter), MCP servers, invocation quirks.

## Credentials & secrets

- Where they live and how to read them — **pointers only, never store values here**.
- Note which files are gitignored (e.g. local env files) and must not be committed.

## Environments

For projects with several targets (dev / test / prod), one row per environment.

| Env | Host | Identifier | Role / account | Where used |
|-----|------|------------|----------------|------------|
| dev  | <...> | <...> | <...> | <...> |
| test | <...> | <...> | <...> | <...> |
| prod | <...> | <...> | <...> | <...> |

## Commands cheat-sheet

Split by environment when shells differ (e.g. PowerShell on dev, bash on prod).

### Dev

```
<frequently used commands>
```

### Prod

```
<deploy, migrate, logs, restart, ...>
```

## Host-specific quirks

A running log of gotchas — the things that cost an hour the first time. Split by environment.

### Dev

- —

### Prod

- —
