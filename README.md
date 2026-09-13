# TeslaMate MCP

[简体中文](README.zh-CN.md) · [AI deployment prompt](AI_SETUP_PROMPT.zh-CN.md)

Two authenticated MCP services for an existing [TeslaMate](https://github.com/teslamate-org/teslamate) deployment:

- a read-only vehicle, drive, route, charging, battery and lifetime-statistics service;
- a narrowly scoped charging/toll expense writer with audit trails and one-time human approval.

The services query TeslaMate's PostgreSQL/MQTT data. They do not call Tesla APIs.

## Read-only tools

- vehicle health and state
- drives, routes and JSON drive export
- charging sessions and summaries
- battery/range trends
- lifetime statistics

The database role has `default_transaction_read_only=on`, a five-second statement timeout and access only to curated views that exclude Tesla credentials, VIN and internal identifiers.

## Expense workflow

The cost writer keeps the existing three charging tools and adds four toll tools:

1. `find_charging_sessions_for_cost` finds safe candidates without writing.
2. `request_charging_cost_changes` requests one host approval and then atomically writes 1–20 matched costs.
3. `get_charging_cost_history` reads the audit trail.

4. `find_toll_journey_candidates` groups 1–20 continuous drives into toll journey candidates.
5. `request_toll_expense_changes` creates, corrects, links or voids 1–20 toll expenses atomically.
6. `get_toll_expense_history` returns current toll expenses and their append-only audit.
7. `get_road_trip_cost_summary` combines a journey's known toll and charging costs.

There is no typed “yes, confirm” step. A compatible Hermes host intercepts either write request before MCP execution and displays a card containing only **Approve** and **Reject**. The included plugin lives in [`plugins/teslamate_cost_approval`](plugins/teslamate_cost_approval). Toll receipts may link to multiple drives or remain `pending_match`; originals are never stored.

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

Copy [`.env.example`](.env.example) to `.env`, then set the host binding, external TeslaMate network name and allowed hosts for your deployment. The defaults bind both services to localhost. Apply the idempotent database setup scripts as the TeslaMate database owner:

- [`db/setup.sql`](db/setup.sql) for read-only views and role;
- [`db/setup_cost.sql`](db/setup_cost.sql) for the cost writer, audit table and function.

Start the services:

```bash
docker compose up -d --build
docker compose ps
```

Default container ports are `8766` for the read-only service and `8767` for the cost writer. Both MCP endpoints use Streamable HTTP at `/mcp`; health checks are exposed at `/healthz`.

## Location privacy

Both services apply the same server-side location policy before returning data to an MCP client:

- `TESLAMATE_LOCATION_PRIVACY=coarse` is the default. Coordinates are rounded to two decimal places, fallback addresses retain only their city component, and exact standalone place labels are hidden unless aliased.
- `hidden` keeps response keys stable but returns `null` for coordinates and place fields.
- `precise` returns the original addresses and coordinates and must be enabled explicitly in the server environment.

The client cannot override this policy in a tool call. Every response includes `location_privacy` metadata. To expose useful labels without publishing private addresses, copy [`config/location-aliases.example.json`](config/location-aliases.example.json) to the gitignored `config/location-aliases.json` and set `TESLAMATE_LOCATION_ALIASES_FILE=/config/location-aliases.json`.

## Hermes approval plugin

Copy `plugins/teslamate_cost_approval` into the active Hermes profile's `plugins/` directory, enable `teslamate_cost_approval` in that profile, and configure the cost-writer MCP URL and actor-specific bearer token.

The plugin uses Hermes `pre_tool_call` escalation plus a per-call rule key, installs the two-button Feishu card on gateway dispatch, and fails closed on malformed requests, denial or timeout.

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
- `precise` mode exposes exact routes and recurring locations; enable it only on a trusted private deployment.
- Original billing screenshots are not stored. Only the supplied source summary is written to the cost audit row.
