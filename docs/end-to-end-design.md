# CapacityPilot end-to-end design

## 1. Executive summary

CapacityPilot helps capacity planners identify customers likely to need storage expansion,
understand why, compare demand with available regional supply, and complete the appropriate
handoff. It replaces repeated manual analysis across storage history, utilization, demand,
news, and prior decisions with a durable evidence and recommendation workflow.

The implementation is deliberately **bounded autonomous**:

- The backend independently queues, executes, retries, and audits investigations.
- Specialist stages use governed tools and persisted evidence rather than unconstrained SQL.
- Nebius produces a structured recommendation within an allowlisted contract.
- Poor data, degraded news, conflicting signals, or low confidence route to planner review.
- A human must approve reservations and Jira handoffs.
- Slack initiates review but cannot approve or provision capacity.
- PostgreSQL is the system of record; Mem0 is advisory memory.

The complete system diagram is in [architecture.md](architecture.md).

## 2. Goals and success criteria

### Product goal

Identify and prioritize customers most likely to require storage expansion, with
evidence-based timing and capacity estimates.

### Primary users

- Capacity planners reviewing demand and supply pressure.
- Capacity managers monitoring regional pool health.
- Model and operations owners examining quality, reliability, and prediction outcomes.

### Success criteria

| Measure | Target |
| --- | ---: |
| Time to produce a usable expansion shortlist | Under 10 minutes |
| Prediction precision after outcomes are labeled | At least 80% |
| Technical data-quality pass rate | At least 95% |
| Specialist coverage | At least 95% |
| Orchestration terminal success | At least 99% |
| Deterministic safety and link contracts | 100% |
| Memory delivery success | At least 95% |

Precision remains `NOT EVALUATED` until actual expansion outcomes are loaded. Synthetic
signals can demonstrate workflow behavior but cannot establish production model accuracy.

## 3. Scope

### In scope

- One-time portfolio baseline and ad hoc customer investigations.
- Data-quality, storage-history, demand, news, evaluation, and memory evidence.
- LLM-generated likelihood, confidence, timing, growth, and action recommendations.
- Ranked planner inbox and read-only portfolio exploration.
- Regional capacity availability and planning reservations.
- CAP Jira handoff for existing-capacity reservations.
- HUB Jira handoff for regional infrastructure orders.
- Slack review digests.
- Planner decisions, overrides, prediction outcomes, and Mem0 feedback.
- Operational metrics and benchmarked evals.

### Out of scope

- Direct purchase, physical provisioning, or deletion of storage.
- Autonomous approval of a reservation or infrastructure order.
- Modification or deletion of source history and demand records.
- Treating Mem0 or an LLM response as the authoritative audit record.
- Claiming production prediction precision from demonstration data.

## 4. Technology choices

| Layer | Technology | Responsibility |
| --- | --- | --- |
| Planner experience | Streamlit | Review inbox, portfolio, capacity, approvals, Jira, Slack, memory, and evals |
| Control plane | FastAPI | Authenticated read/write API and validation boundary |
| System of record | PostgreSQL 16 | Evidence, queues, audit events, reservations, outboxes, and outcomes |
| Agent orchestration | LangGraph | Ordered, stateful specialist execution |
| Recommendation LLM | Nebius Token Factory | Structured synthesis and portfolio-query interpretation |
| Public evidence | SEC EDGAR | Authoritative filings and company identities |
| Optional news | Licensed News API | Publisher headlines and snippets when legally configured |
| Advisory memory | Mem0 | Retrieval of prior planner disposition patterns |
| Workflow handoff | Jira Cloud | CAP reservation and HUB infrastructure requests |
| Notification | Slack | Review digest and link back to CapacityPilot |
| Packaging | uv and Docker | Reproducible dependencies and runtime image |
| Continuous validation | pytest, Ruff, GitHub Actions | Unit, contract, orchestration, integration-boundary, and lint checks |

## 5. Component design

### 5.1 Streamlit planner application

The UI is a thin authenticated client of FastAPI. It does not invoke the LangGraph directly.
Its principal screens are:

1. **Planner inbox** — unresolved recommendations, evidence, regional availability, and
   explicit planner actions.
2. **Customer portfolio** — current signals and recommendation status across all companies.
3. **Ask CapacityPilot** — read-only grounded questions over PostgreSQL data.
4. **Capacity supply** — inventory by region, QFAB, service, and tier.
5. **Reservations** — append-only local planning holds.
6. **Jira handoffs** — CAP/HUB delivery status with mandatory ticket links.
7. **Slack delivery** — current digest contents and delivery audit.
8. **Investigate customer** — priority ad hoc reruns.
9. **Quality & evals** — data-quality results, benchmarks, orchestration, memory, and
   observability.
10. **System health** — queue, worker, news, and API state.

High-impact buttons are guarded by explicit confirmation, planner identity, fresh capacity,
source classification, and server-side revalidation. A disabled action always has a reason.

### 5.2 FastAPI control plane

FastAPI is the only supported UI write boundary. An `X-API-Key` header protects application
routes in the local implementation. Major API groups include:

- `/companies`, `/shortlist`, and `/portfolio/chat`
- `/cases` and `/portfolio-investigation/*`
- `/capacity-inventory` and `/capacity-availability`
- `/reservations` and `/shortlist/overrides`
- `/jira-requests` and `/slack-alerts/*`
- `/news-ingestion/*`, `/memories`, `/evaluation`, and `/quality-evals`

Pydantic request models enforce allowed actions, required customer association, explicit
confirmation, positive capacity, valid target dates, and permitted service/tier values.

### 5.3 PostgreSQL data foundation

PostgreSQL provides transactionality, concurrency control, and auditability.

| Domain | Primary tables |
| --- | --- |
| Customer and evidence | `company`, `capacity_signal`, `news_evidence` |
| Investigation state | `case_run`, `case_event`, `capacity_signal_scenario` |
| Planner feedback | `planner_decision`, `planner_forecast_override`, `prediction_outcome` |
| Supply and reservations | `capacity_inventory`, `local_capacity_reservation` |
| Durable delivery | `news_ingestion_job`, `memory_outbox`, `jira_request`, `slack_alert_outbox` |

Schema migrations are ordered, idempotent, and recorded in
`public.capacity_planner_schema_migration`. Queue consumers use atomic claims and
`SKIP LOCKED` semantics so multiple workers can run without processing the same item.

### 5.4 Autonomous investigation worker

The capacity worker polls the durable case queue, recovers stale work at startup, claims one
case, invokes the graph, and persists the terminal recommendation. Ad hoc cases use higher
priority than the remaining baseline while preserving FIFO order within each priority.

Case states are:

```text
QUEUED -> RUNNING -> COMPLETE or REVIEW_REQUIRED
                   -> RETRY -> RUNNING
                   -> FAILED after retry exhaustion
```

Each graph node writes an immutable `case_event`. An exception writes an `error` event before
the case is retried or failed.

## 6. Agent design

The business design has five principal roles—Data Quality, Storage History, News,
Integration/Orchestration, and Evaluation. The implementation expands the workflow into
seven explicit LangGraph stages so demand and memory have isolated contracts.

| Stage | Inputs | Tools and processing | Persisted output |
| --- | --- | --- | --- |
| Data Quality | Customer and current capacity signal | Governed PostgreSQL query plus 16 deterministic checks | Scores, failed checks, freshness, production eligibility |
| Storage History | Installed, consumed, prior expansions, 12-month growth | Read-only SQL and deterministic calculations | Utilization, growth, expansion pattern |
| Demand | Open demand and stage | Read-only SQL | Demand TiB and stage |
| News | Company identity and cached evidence | SEC/news collectors with cache and deduplication | Status, citations, extracted signal content |
| Evaluation | Prior specialist outputs | Coverage calculation and benchmark metadata | Missing specialists and evidence coverage |
| Memory | Customer identity | Mem0 bounded search | Prior decision memories or degraded status |
| Recommendation | All evidence | Nebius structured-output contract plus deterministic guardrails | Likelihood, confidence, timing, growth, action, alert permission |

The graph order is deterministic. This prevents the LLM from skipping mandatory checks or
inventing new tools. Autonomy exists in queue progression, evidence collection, retries,
structured synthesis, and handoff selection—not in bypassing controls.

## 7. Data-quality controls

The Data Quality Agent evaluates 16 rules:

1. Complete company identity.
2. No nulls in required fields.
3. Valid company name.
4. Valid ticker format.
5. No control or unsafe characters.
6. No duplicate SEC CIK.
7. No duplicate ticker on an exchange.
8. Source marked fresh.
9. Snapshot within the configured age limit.
10. Timestamp not in the future.
11. Installed capacity is positive.
12. Consumption is nonnegative.
13. Consumption does not exceed installed capacity.
14. Demand is nonnegative.
15. Historical values are nonnegative.
16. Data is classified as production rather than demonstration or test.

Technical quality excludes only the production-classification check. This allows planners to
exercise demonstration flows while preventing those rows from being mistaken for production
evidence. Failed technical checks force low confidence, suppress Slack alerts, and require
planner review.

## 8. Recommendation and decision policy

Nebius returns a strict recommendation object containing likelihood, confidence, expected
timing, estimated growth, action, severity, evidence summary, and assumptions. Unknown keys
and invalid enum values are rejected at the model boundary.

Deterministic post-processing applies the safety policy:

- Technical-quality failure or unavailable news sets confidence to `LOW`.
- Unsafe cases receive `PLANNER_REVIEW` and `alert_allowed=false`.
- Slack eligibility requires `MEDIUM` or `HIGH` confidence and at least 80% likelihood.
- Every recommendation sets `requires_human_approval=true`.
- Planning simulations cannot reserve capacity or create Jira work.
- A high likelihood is not the same as high evidence confidence.

Planner dispositions are `APPROVE_REVIEW`, `MONITOR`, and `REJECT_INVESTIGATE`. Forecast
overrides append a new audit row; they never overwrite the original AI recommendation.

## 9. Regional capacity and reservation design

Each customer is assigned a capacity region. Inventory is keyed by region, QFAB, service,
and storage tier and records usable, allocated, held, available, freshness, and source
classification values.

Before showing an action, CapacityPilot calculates:

```text
available now = usable - allocated - active planning holds
available after = available now - requested capacity
post-reservation allocation % = (allocated + holds + request) / usable * 100
```

The planner chooses the compatible pool and confirms the request. The reservation transaction
then locks and rechecks the inventory row to prevent concurrent over-reservation.

- If capacity is sufficient, create a `LOCAL_RESERVED` planning hold and queue a CAP Jira
  request.
- If capacity is insufficient, block the reservation and offer a HUB infrastructure order.
- If the reservation reaches the configured 70% allocation threshold, queue CAP and recommend
  a HUB replenishment request.
- Regional HUB requests can be created without a customer when a supply planner is ordering
  capacity for a verified pool.

No code path provisions physical storage.

## 10. Jira and Slack integrations

### Jira

Jira requests use a transactional outbox. The API commits the planner action and outbox row in
PostgreSQL; the Jira worker later creates or finds an issue using an idempotency label.

- `CAP_RESERVATION` routes to the CAP project and includes the customer.
- `HUB_INFRASTRUCTURE` routes to the HUB project and emphasizes region/QFAB supply.
- Retries use `QUEUED`, `RUNNING`, `RETRY`, `COMPLETE`, and `FAILED` states.
- A completed request is valid only when its HTTPS URL matches the project and issue key.

Jira downtime does not roll back an approved local reservation.

### Slack

The Slack digest contains three synchronized counts:

1. Demand signals awaiting planner review.
2. Recommendations that may use existing regional capacity.
3. Recommendations likely to require additional storage.

The outbox deduplicates unchanged snapshots. Slack links to CapacityPilot and does not expose
an approval action. Ad hoc sends require an explicit UI confirmation.

## 11. Memory design

PostgreSQL is authoritative; Mem0 is a derived advisory store. The memory outbox delivers only
a minimal decision or prediction summary and excludes planner notes, raw capacity history,
news extracts, and credentials.

During an investigation, the Memory stage retrieves a small number of customer-specific
memories. The LLM may use them as context, but memory cannot:

- Change data-quality results.
- Enable an otherwise suppressed alert.
- Approve a reservation.
- Replace PostgreSQL audit evidence.

If Mem0 is unavailable, the graph continues with `DEGRADED` memory status and the outbox
retries independently.

## 12. Evaluation and observability

The Evaluation results screen presents measured accuracy, benchmark, sample size, and status
for 11 eval suites:

| Evaluation | Benchmark |
| --- | ---: |
| API and PostgreSQL connectivity | 100% |
| Technical data-quality pass rate | 95% |
| Average specialist evidence coverage | 95% |
| Full specialist coverage rate | 95% |
| Expansion prediction precision | 80% |
| Chatbot grounding contract | 100% |
| Navigation and action contract | 100% |
| Safe-link contract | 100% |
| Completed Jira link integrity | 100% |
| Orchestration terminal success | 99% |
| Memory delivery success | 95% |

Operational views expose queue depth, stale work, retries, terminal latency, error types,
worker throughput, Mem0 searches and deliveries, news ingestion, Jira status, and Slack
delivery. Case events provide a customer-level execution trace.

## 13. Error recovery

| Failure | Response |
| --- | --- |
| Transient database or tool error | Mark retryable work `RETRY` with backoff |
| Worker dies after claim | Recover stale locks after the configured threshold |
| Required evidence missing | Lower confidence, suppress alert, route to planner |
| News or Mem0 unavailable | Continue in degraded mode where safe and expose status |
| Nebius timeout or invalid output | Use constrained fallback where implemented or retry/fail visibly |
| Concurrent reservation | Lock and recheck inventory; return conflict when capacity changed |
| Jira or Slack unavailable | Preserve committed planner action; retry outbox independently |
| Invalid Jira URL | Fail mandatory link eval and show the request for remediation |
| API unavailable | Stop the Streamlit workflow and show the recovery command |

## 14. Security and governance

Current controls include:

- Secrets loaded from `.env`, which is excluded from Git.
- API-key protection on application endpoints.
- Read-only evidence queries inside specialist agents.
- Pydantic allowlists for actions and structured LLM output.
- Explicit planner confirmation for all write actions.
- Append-only decisions, overrides, events, and reservations.
- HTTPS validation for evidence and Jira links.
- No credentials, raw news, or planner notes sent to Mem0.
- Test and synthetic classifications visible and action-restricted.

Production deployment additionally requires enterprise SSO and authorization, managed secrets,
TLS, network segmentation, encrypted PostgreSQL, backups, audit retention, vulnerability
management, centralized logs/metrics, and approved data/news licenses.

## 15. Deployment topology

The local topology runs separate processes:

```text
Streamlit UI -> FastAPI -> PostgreSQL
                    |
                    +-> capacity worker -> LangGraph -> Nebius/SEC/Mem0 search
                    +-> news worker -> SEC and optional News API
                    +-> Jira worker -> Jira Cloud
                    +-> Slack worker -> Slack
                    +-> memory worker -> Mem0
```

For production, package the processes as independently scalable services. Keep all queues and
outboxes in managed PostgreSQL initially; scale workers horizontally using their existing
atomic claim behavior. Place the UI and API behind the enterprise ingress and identity layer.

## 16. Testing strategy

The automated suite covers:

- Specialist-node outputs and graph order.
- All data-quality rules and degradation behavior.
- Nebius schema boundaries and fallback behavior.
- News caching, extraction, ingestion, and worker recovery.
- Queue priority, idempotency, retries, and stale-claim recovery.
- Reservation concurrency and human approval.
- Jira, Slack, and Mem0 outbox behavior.
- Chatbot transactional grounding and follow-up context.
- Mandatory Jira/evidence link construction.
- Streamlit navigation and action-state contracts.
- Evaluation benchmark calculation.
- FastAPI authentication and orchestration endpoints.

GitHub Actions runs all tests and Ruff on every push and pull request. Live browser evaluation
is used for end-to-end Streamlit navigation and rendered control-state verification without
submitting external actions.

## 17. Production rollout plan

1. **Data contract** — replace demonstration capacity signals with approved history,
   consumption, CIMS, and inventory views while preserving source timestamps and
   classifications.
2. **Shadow mode** — run recommendations without alerts or write actions; label actual
   expansions and measure precision by forecast window and region.
3. **Planner pilot** — enable Streamlit review for a small planner group and collect overrides,
   rejects, time-to-decision, and evidence-quality feedback.
4. **Controlled integrations** — enable Slack first, then Jira outboxes, with alert volume and
   failure-rate limits.
5. **Reservation workflow** — enable approved planning holds after concurrency, authorization,
   and reconciliation tests pass.
6. **Scale and calibrate** — tune thresholds using labeled outcomes while maintaining the 80%
   precision floor and monitoring segment-level performance.

## 18. Known limitations and next decisions

- The checked-in capacity and demand values are demonstration signals, not customer production
  telemetry.
- Prediction precision cannot be asserted until actual outcomes are labeled.
- The current graph uses a fixed safe sequence rather than LLM-selected dynamic routing.
- PostgreSQL queues are appropriate for the current scale; very high throughput may justify a
  dedicated message broker later.
- Streamlit is suitable for the planner pilot; a larger enterprise rollout may require a
  dedicated frontend and fine-grained identity-aware authorization.
- Cost estimates and physical provisioning APIs are intentionally absent.

These are explicit production gates rather than hidden assumptions.
