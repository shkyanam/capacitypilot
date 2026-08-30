# CapacityPilot

**AI-guided storage planning for capacity teams.**

## Design documentation

- [End-to-end architecture diagrams](docs/architecture.md)
- [End-to-end system design](docs/end-to-end-design.md)

This application uses FastAPI, a PostgreSQL-backed worker queue, LangGraph, Nebius Token Factory, and Streamlit. It can create audited local planning reservations after explicit planner confirmation. It never provisions physical capacity or sends an external Capacity Manager request.

## Optional Mem0 memory

Mem0 can recall prior planner dispositions for the same customer. PostgreSQL remains the authoritative audit record. The application sends only a minimal derived decision summary to Mem0; it excludes planner notes, raw capacity data, news extracts, and credentials. Memory is advisory and can never bypass data-quality checks, enable an alert, or approve capacity.

Add your managed Mem0 key to `.env` (never commit it):

```bash
MEM0_ENABLED=true
MEM0_API_KEY=your-key
MEM0_AGENT_ID=capacity-planner
```

Then run the durable synchronization worker in a fourth terminal:

```bash
uv run capacity-memory-worker
```

If Mem0 is unavailable, the PostgreSQL decision still commits, the outbox retries, and investigations continue with memory marked `DEGRADED`.

## Data truth

The first 1,000 company identities (name, SEC CIK, ticker, exchange) are downloaded from the official SEC EDGAR company-ticker dataset. Storage, utilization, expansion, and demand values are deterministic **synthetic demonstration data**, because customer operational capacity data is not public. Every row carries `data_classification = SYNTHETIC_DEMO`.

## Production news evidence

The News Agent retrieves recent authoritative SEC filings for the selected company, extracts short signal-focused passages, classifies acquisitions, growth plans, data-center activity, capacity investments, and geographic expansion, and stores the citation and audit metadata in `capacity_planner.news_evidence`. Results are cached for 24 hours and deduplicated by provider identity and URL.

Set `NEWS_API_KEY` only when the organization has an appropriate production News API license. When configured, licensed publisher headlines and snippets are combined with SEC evidence. Provider failures degrade confidence, suppress alerts, and remain visible to the planner.

## Local setup

```bash
cp .env.example .env
# Add NEBIUS_API_KEY, replace API_AUTH_TOKEN, and set SEC_USER_AGENT to a monitored team email.
uv sync --extra dev
uv run capacity-seed
```

Run these in three terminals:

```bash
uv run capacity-api
uv run capacity-worker
uv run streamlit run src/capacity_planner/ui.py
```

Or start the same local stack with one command:

```bash
uv run capacity-start
```

This starts the API, Streamlit, and the capacity worker. It also starts the Jira, Slack,
and Mem0 workers when their corresponding `*_ENABLED` setting is `true`. Press `Ctrl-C`
to stop every service started by the command.

## Bulk news ingestion

Set `SEC_USER_AGENT` to your organization and a monitored contact email; placeholder identities are rejected. Then enqueue all companies and run the separate rate-limited worker:

```bash
uv run capacity-news-enqueue
uv run capacity-news-worker
```

The worker processes one company at a time, uses PostgreSQL `SKIP LOCKED`, retries transient failures, recovers stale jobs, caches evidence, and refreshes completed companies after 24 hours. Progress is visible in Streamlit under **Bulk news ingestion**.

Open http://localhost:8501. API documentation is at http://localhost:8000/docs.

## One-time portfolio baseline and ad hoc reruns

FastAPI queues each company exactly once for its initial portfolio investigation when the backend starts. PostgreSQL makes this idempotent, and the separate capacity worker processes the durable queue. Streamlit only displays progress and results; it does not orchestrate the agent. The dashboard displays completed, remaining, and active counts plus the last successful investigation date and time. No daily schedule is created.

After the baseline, planners can use **Customer investigation** to rerun any individual customer on demand.
Ad hoc planner reruns use priority `10`; one-time portfolio baseline cases use priority `100`.
Workers therefore claim planner-requested investigations before remaining baseline work while
retaining FIFO order within each priority.

## Audited planner forecast overrides

The shortlist supports bulk editing of likelihood, confidence, timing, capacity growth, and action. Customer identity, calculated quality, and production eligibility are read-only. Saving requires a planner identity and creates append-only rows in `capacity_planner.planner_forecast_override` plus case audit events. The original AI recommendation is never overwritten.

## Local capacity reservations

Each customer has a capacity region. The **Regional capacity** page shows usable, physically allocated, locally held, and available capacity across region, QFAB, storage tier, and capacity-model pools.

From **Review queue**, select a customer and complete **Create local reservation**. The application derives the customer region, lets the planner select a compatible QFAB/service/vault pool, and displays capacity before and after the request. Reservation is disabled when inventory is missing, stale, or insufficient. The PostgreSQL transaction locks and rechecks the pool before inserting, so concurrent planners cannot over-reserve it.

A successful submission creates one idempotent `LOCAL_RESERVED` row per investigation in `capacity_planner.local_capacity_reservation`, stores the inventory snapshot, records an `APPROVE_REVIEW` decision, writes a case audit event, and queues the derived Mem0 decision when memory is enabled. If post-reservation allocation reaches 70%, the application recommends ordering additional regional infrastructure. A shortage blocks reservation and reports the TiB shortfall. When Jira is enabled, the same approval automatically queues a `CAP_RESERVATION` request and, when the 70% threshold is reached, a `HUB_INFRASTRUCTURE` request. Jira delivery remains asynchronous and retryable, so Jira downtime does not roll back the committed reservation.

After approval, the customer leaves the unresolved review inbox and the first Streamlit screen displays **Latest reservation and Jira handoff**, including CAP/HUB delivery status and clickable Jira ticket links. The separate **Jira requests** page retains the complete handoff history.

The default Streamlit screen and Slack use the same action-inbox contract: unresolved, `alert_allowed=true`, at least 80% likelihood, MEDIUM/HIGH confidence, positive estimated growth, and no prior planner decision or local reservation. Both route an item to **Reserve available capacity** when a fresh regional pool can absorb the estimated growth below 70% allocation, or **Order more storage** otherwise. The exact service, vault, and QFAB are always revalidated before a decision is committed.

Use **Quality & evals** in CapacityPilot to inspect persisted Data Quality Agent results for all 16 checks, recent per-customer failures, specialist evidence coverage, labeled-outcome precision against the 80% target, case status distribution, specialist execution counts, retries, and recent terminal orchestration failures.

The **Evaluation results** tab includes an explicit production benchmark scorecard. It reports
the measured percentage, benchmark, sample size, and PASS/FAIL/NOT EVALUATED status for API
connectivity, technical data quality, specialist coverage, prediction precision, chatbot
grounding, navigation and action guardrails, safe links, Jira integrity, orchestration
reliability, and memory delivery. Deterministic safety contracts require 100%, technical
quality and memory delivery require at least 95%, orchestration requires at least 99%, and
prediction precision uses the agreed 80% target.

## Jira capacity workflow

Jira creation follows the explicit planner approval in Streamlit:

- Available regional capacity: make the local reservation, then create a reservation ticket in the `CAP` project.
- Insufficient regional capacity: reservation is blocked and an infrastructure-order ticket can be created in the `HUB` project.
- Reservation leaves the pool at or above 70% allocation: create the `CAP` ticket and optionally create a `HUB` replenishment ticket.
- Regional supply planning: use **Create regional HUB request** on the Streamlit home page to order infrastructure for a verified region/QFAB/storage-tier pool without attaching a customer. The planner must supply an order quantity, required date, identity, justification, and explicit confirmation. Active duplicate orders for the same pool are suppressed.

Requests first enter the PostgreSQL `capacity_planner.jira_request` outbox. A separate worker creates the Jira issue with an idempotency label and retries transient failures:

```bash
uv run capacity-jira-worker
```

Configure the `JIRA_*` variables shown in `.env.example`. Keep the API token only in `.env`; never commit it.

## Slack capacity alerts

The Slack digest reports three live counts: demand signals awaiting review, cases that may fit in current regional supply (**Reserve capacity**), and cases whose estimated growth exceeds every fresh regional pool (**Order more storage**). Slack contains a link to Streamlit and never performs the decision itself. The reservation transaction revalidates the exact service, vault, and QFAB selection.

Alerts use the data available in PostgreSQL and require an orchestrator recommendation with `alert_allowed=true`. MEDIUM- and HIGH-confidence recommendations at 80% likelihood or above may alert; failed technical-quality checks, degraded news, and LOW confidence remain suppressed. `SLACK_REQUIRE_PRODUCTION_ELIGIBLE` can optionally add a separate source-classification gate; it is disabled by default.

Use either an incoming webhook or a bot token:

- Webhook: `SLACK_AUTH_MODE=webhook` and `SLACK_WEBHOOK_URL=...`
- Bot: `SLACK_AUTH_MODE=bot`, `SLACK_BOT_TOKEN=...`, and `SLACK_CHANNEL_ID=...` (requires Slack `chat:write` scope)

Then start the autonomous digest/outbox worker:

```bash
uv run capacity-slack-worker
```

It sends only when the eligible snapshot changes and observes `SLACK_DIGEST_INTERVAL_MINUTES`; delivery failures retry with backoff. The **Slack alerts** Streamlit page shows the current categorization, delivery history, and an explicitly confirmed ad hoc send option. Keep all Slack secrets only in `.env`.

Use **Local reservations** in the sidebar to review the complete local register. These records are internal planning holds; they do not represent physical storage provisioning or external Capacity Manager allocations.

## Tests

```bash
uv run pytest
uv run ruff check src tests
uv run pytest --cov=capacity_planner.agents --cov=capacity_planner.repository \
  --cov=capacity_planner.worker --cov=capacity_planner.nebius \
  --cov=capacity_planner.api --cov-report=term-missing
```

The orchestration suite covers graph order and state propagation, data-quality and news degradation routing, alert suppression, strict model-output allowlisting, worker success and failure behavior, retry exhaustion, stale-worker recovery, duplicate active-case prevention, concurrent queue locking, API authentication, and human-approval gating.

## Production controls still required

Deploy behind enterprise SSO/API authorization, use a managed secrets service, TLS, encrypted PostgreSQL, centralized logs and metrics, backups, vulnerability scanning, and an approved real-data connector. The included Docker Compose file is for local integration testing, not a production infrastructure definition.

## Public data source

SEC EDGAR: https://www.sec.gov/files/company_tickers_exchange.json
