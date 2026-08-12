# TeslaMate MCP

[简体中文](README.zh-CN.md) · [AI deployment prompt](AI_SETUP_PROMPT.zh-CN.md)

Two authenticated MCP services for an existing [TeslaMate](https://github.com/teslamate-org/teslamate) deployment:

- a read-only vehicle, drive, route, charging, battery and lifetime-statistics service;
- a narrowly scoped charging-cost writer with an audit trail and one-time human approval.

The services query TeslaMate's PostgreSQL/MQTT data. They do not call Tesla APIs.

## Read-only tools

- vehicle health and state
- drives, routes and JSON drive export
- charging sessions and summaries
- battery/range trends
- lifetime statistics

The database role has `default_transaction_read_only=on`, a five-second statement timeout and access only to curated views that exclude Tesla credentials, VIN and internal identifiers.

## Charging-cost workflow

The public cost-writer surface intentionally has only three tools:

1. `find_charging_sessions_for_cost` finds safe candidates without writing.
2. `request_charging_cost_changes` requests one host approval and then atomically writes 1–20 matched costs.
3. `get_charging_cost_history` reads the audit trail.

There is no typed “yes, confirm” step. A compatible Hermes host intercepts the write request before MCP execution and displays a card containing only **Allow Once** and **Deny**. The included plugin lives in [`plugins/teslamate_cost_approval`](plugins/teslamate_cost_approval).

Batch writes use one PostgreSQL transaction. If any row is stale or invalid, the entire batch rolls back. The writer role has no direct table-write grant; it can only call the audited `SECURITY DEFINER` function.

## Setup

Requirements:

- Python 3.12+
- Docker Compose, or a Python environment managed by `uv`
- an existing TeslaMate PostgreSQL database (and MQTT broker for live vehicle state)

Create the runtime secret files referenced by [`compose.yaml`](compose.yaml):

```text
secrets/db_password
secrets/mcp_token
secrets/cost_db_password
secrets/cost_token_huunter
secrets/cost_token_guoguo
secrets/cost_signing_secret
```

Generate bearer/signing values with a cryptographically secure generator, for example `openssl rand -hex 32`. The entire `secrets/` directory is gitignored and excluded from the Docker build context.

Edit the host bindings, external TeslaMate network name and allowed hosts in `compose.yaml` for your deployment. Then apply the idempotent database setup scripts as the TeslaMate database owner:

- [`db/setup.sql`](db/setup.sql) for read-only views and role;
- [`db/setup_cost.sql`](db/setup_cost.sql) for the cost writer, audit table and function.

Start the services:

```bash
docker compose up -d --build
docker compose ps
```

Default container ports are `8766` for the read-only service and `8767` for the cost writer. Both MCP endpoints use Streamable HTTP at `/mcp`; health checks are exposed at `/healthz`.

## Hermes approval plugin

Copy `plugins/teslamate_cost_approval` into the active Hermes profile's `plugins/` directory, enable `teslamate_cost_approval` in that profile, and configure the cost-writer MCP URL and actor-specific bearer token.

The plugin requires Hermes' `pre_tool_call` approval escalation with `once_only` support. It validates the batch shown on the card and fails closed on malformed requests, denial or timeout.

## Development

```bash
uv sync --extra test
uv run --extra test pytest -q
```

Before committing, verify that no runtime material is staged:

```bash
git status --short
git grep -nE '(BEGIN (RSA|OPENSSH) PRIVATE KEY|Bearer [A-Za-z0-9._-]{20,})'
```

## Security notes

- Never commit `secrets/`, `.env`, database dumps, exports or rollback archives.
- Bind the service ports to localhost, a private interface or a trusted overlay network.
- Route and charging tools may return exact coordinates; treat access as sensitive.
- Original billing screenshots are not stored. Only the supplied source summary is written to the cost audit row.
