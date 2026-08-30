import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pandas as pd
import streamlit as st

from capacity_planner.config import get_settings
from capacity_planner.eval_benchmarks import build_eval_scorecard, detailed_contract_checks
from capacity_planner.runtime_health import evaluate_api_connectivity
from capacity_planner.ui_contracts import NAVIGATION_PAGES, reservation_action_state
from capacity_planner.web import safe_jira_ticket_url, safe_source_url

API = get_settings().api_base_url.rstrip("/")
HEADERS = {"X-API-Key": get_settings().api_auth_token}
LOGO_PATH = Path(__file__).parents[2] / "assets" / "capacitypilot-logo.png"

st.set_page_config(page_title="CapacityPilot", page_icon=str(LOGO_PATH), layout="wide")
st.logo(str(LOGO_PATH), icon_image=str(LOGO_PATH), size="large")

st.markdown(
    """
    <style>
    .block-container {max-width: 1480px; padding-top: 2rem; padding-bottom: 4rem;}
    [data-testid="stMetric"] {
        background: color-mix(in srgb, var(--secondary-background-color) 82%, transparent);
        border: 1px solid color-mix(in srgb, var(--text-color) 14%, transparent);
        border-radius: 14px;
        padding: 1rem 1.1rem;
    }
    [data-testid="stMetricLabel"] {font-weight: 600;}
    [data-testid="stSidebar"] {border-right: 1px solid rgba(128, 128, 128, 0.18);}
    div[data-testid="stExpander"] {border-radius: 12px;}
    </style>
    """,
    unsafe_allow_html=True,
)


def api(method: str, path: str, **kwargs):
    response = httpx.request(method, f"{API}{path}", headers=HEADERS, timeout=60, **kwargs)
    response.raise_for_status()
    return response.json()


def refresh_label(value) -> str:
    if not value:
        return "No completed investigations"
    return pd.to_datetime(value).strftime("%d %b %Y, %I:%M:%S %p %Z")


def jira_ticket_link(issue_key: str, issue_url: str | None) -> str:
    """Always render a Jira key as a link, or avoid exposing an unlinked key."""
    url = safe_jira_ticket_url(issue_key, issue_url, get_settings().jira_base_url)
    return f"[{issue_key}]({url})" if url else "Ticket link unavailable"


def load(path: str):
    try:
        return api("GET", path)
    except httpx.HTTPError as exc:
        st.error(f"Capacity agent API unavailable: {exc}")
        st.stop()


def require_healthy_api() -> dict:
    result = evaluate_api_connectivity(API)
    if result["status"] == "PASS":
        return result
    st.title("CapacityPilot backend is unavailable")
    st.error(
        f"API connectivity eval: FAIL · {result['failure_type']}. "
        "Streamlit cannot retrieve or update capacity-planning data."
    )
    st.write(f"Health endpoint: `{result['health_url']}`")
    st.code(result["recovery_command"], language="bash")
    st.caption(
        "Run the command in a separate terminal and leave it running. Then select Retry. "
        "PostgreSQL data and queued worker state are not lost."
    )
    if st.button("Retry API connection", type="primary"):
        st.rerun()
    st.stop()


def prepare_shortlist(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    frame = frame.rename(
        columns={
            "case_id": "Case ID",
            "company_name": "Customer",
            "ticker": "Ticker",
            "region": "Region",
            "likelihood_pct": "Expansion likelihood %",
            "confidence": "Evidence confidence",
            "timing_days": "Expected need (days)",
            "capacity_growth_tib": "Suggested growth (TiB)",
            "quality_score_pct": "Data quality %",
            "production_eligible": "Production ready",
            "action": "Recommended workflow",
            "planner_adjusted": "Planner adjusted",
            "installed_tib": "Installed capacity (TiB)",
            "consumed_tib": "Consumed capacity (TiB)",
            "utilization_pct": "Current utilization %",
            "trailing_12m_growth_tib": "12-month growth (TiB)",
            "open_demand_tib": "Open demand (TiB)",
            "demand_stage": "Demand stage",
            "test_scenario": "Planning simulation",
        }
    )
    frame["Production ready"] = (
        frame["Production ready"].map({True: "Yes", False: "No"}).fillna("Unknown")
    )
    frame["Forecast source"] = frame["Planner adjusted"].map(
        {True: "Planner adjusted", False: "AI recommendation"}
    )
    frame["Signal type"] = frame["Planning simulation"].map(
        {True: "Planning simulation", False: "Source data"}
    )
    return frame


def render_customer_recommendation(shortlist: pd.DataFrame) -> None:
    st.subheader("Plan capacity with confidence")
    st.caption("Signals are consolidated into one recommendation for the selected customer.")
    review_options = shortlist.sort_values(
        by=["Planning simulation", "Expansion likelihood %"],
        ascending=[True, False],
        kind="stable",
    )["Case ID"].tolist()
    selected_case = st.selectbox(
        "Customer to review",
        review_options,
        format_func=lambda case_id: (
            f"{shortlist.loc[shortlist['Case ID'] == case_id, 'Customer'].iloc[0]} "
            f"({shortlist.loc[shortlist['Case ID'] == case_id, 'Ticker'].iloc[0]}) · "
            f"{shortlist.loc[shortlist['Case ID'] == case_id, 'Signal type'].iloc[0]}"
        ),
        key="recommendation_customer",
    )
    row = shortlist[shortlist["Case ID"] == selected_case].iloc[0]
    case = load(f"/cases/{selected_case}")
    recommendation = case.get("recommendation") or {}
    reservations = load(f"/reservations?case_id={selected_case}")
    jira_requests = load(f"/jira-requests?case_id={selected_case}")
    jira_by_type = {item["request_type"]: item for item in jira_requests}
    likelihood = float(row["Expansion likelihood %"])
    utilization = float(row["Current utilization %"])
    installed = float(row["Installed capacity (TiB)"])
    consumed = float(row["Consumed capacity (TiB)"])
    annual_growth = float(row["12-month growth (TiB)"])
    open_demand = float(row["Open demand (TiB)"])
    timing_value = row["Expected need (days)"]
    growth_value = row["Suggested growth (TiB)"]
    quality_value = row["Data quality %"]
    timing_days = 180 if pd.isna(timing_value) else int(timing_value)
    growth = 0.0 if pd.isna(growth_value) else float(growth_value)
    quality = 0.0 if pd.isna(quality_value) else float(quality_value)
    test_scenario = bool(row["Planning simulation"])

    if test_scenario:
        st.warning(
            "Planning simulation: the source capacity and demand records were not modified. "
            "This recommendation may be reviewed, but reservation and Jira actions are disabled."
        )

    left, right = st.columns([1.55, 1], gap="large")
    with left, st.container(border=True):
        title_column, badge_column = st.columns([3, 1])
        title_column.caption("Customer")
        title_column.subheader(row["Customer"])
        title_column.caption(f"Capacity region: {row['Region']}")
        badge_column.info("Growth likely" if likelihood >= 75 else "Monitor")

        if utilization >= 85:
            signal_title = "Utilization is above the planning threshold"
            signal_detail = (
                f"Current utilization is {utilization:.1f}% with "
                f"{annual_growth:.1f} TiB growth over the past 12 months."
            )
        elif row["Demand stage"] == "COMMITTED":
            signal_title = "Committed demand is increasing capacity pressure"
            signal_detail = (
                f"The customer has {open_demand:.1f} TiB of committed demand and is "
                f"currently using {utilization:.1f}% of installed capacity."
            )
        else:
            signal_title = "Growth signals require capacity review"
            signal_detail = (
                f"Utilization is {utilization:.1f}% with {open_demand:.1f} TiB of open demand."
            )
        st.warning(f"**{signal_title}**\n\n{signal_detail}")

        horizon_months = max(3, min(12, round(timing_days / 30)))
        monthly_growth = annual_growth / 12
        projection = pd.DataFrame(
            {
                "Projected usage": [
                    max(0, consumed + monthly_growth * month)
                    for month in range(horizon_months + 1)
                ],
                "85% planning threshold": [installed * 0.85] * (horizon_months + 1),
            },
            index=[f"M+{month}" for month in range(horizon_months + 1)],
        )
        st.caption("Projected storage usage (TiB)")
        st.line_chart(projection, height=230)
        m1, m2 = st.columns(2)
        m1.metric("Projected utilization signal", f"{utilization:.1f}%")
        m2.metric("Suggested capacity growth", f"+{growth:,.1f} TiB")
        st.progress(min(1.0, max(0.0, utilization / 100)))
        st.caption(f"Expected capacity need in approximately {timing_days} days")

    with right, st.container(border=True):
        st.caption("Recommended decision")
        decision_title = (
            "Review capacity reservation"
            if row["Recommended workflow"] == "PLANNER_REVIEW"
            else "Continue monitoring"
        )
        st.header(decision_title)
        st.write(
            f"Review an estimated {growth:,.1f} TiB requirement within "
            f"{timing_days} days. Human approval is required before any reservation."
        )
        r1, r2 = st.columns(2)
        r1.metric("Expansion likelihood", f"{likelihood:.0f}%")
        r2.metric("Evidence confidence", row["Evidence confidence"].title())
        r3, r4 = st.columns(2)
        r3.metric("Data quality", f"{quality:.1f}%")
        r4.metric("Production ready", row["Production ready"])
        with st.expander("View supporting signals", expanded=False):
            for reason in recommendation.get("reasons", []):
                st.write(f"- {reason}")
            st.write(f"Demand stage: {row['Demand stage']}")
            st.write(f"Open demand: {open_demand:,.1f} TiB")
            st.write(f"Forecast source: {row['Forecast source']}")

    st.divider()
    st.subheader("Reservation and regional supply")
    st.caption(
        "Confirm the regional capacity pool before reserving existing supply or ordering "
        "additional storage infrastructure."
    )
    if reservations:
        reservation = reservations[0]
        st.success(
            f"Local planning reservation active: "
            f"{float(reservation['requested_tib']):,.1f} TiB"
        )
        st.write(
            f"**{reservation['service']} · {reservation['vault_type']} · "
            f"{reservation['tenancy_type']}**"
        )
        st.write(
            f"Target: {reservation['target_date']} · Region: {reservation['region']} · "
            f"Status: {reservation['status'].replace('_', ' ').title()}"
        )
        if reservation.get("inventory_id") is None:
            st.warning(
                "Legacy reservation: this hold predates regional capacity inventory and "
                "was not included in an availability calculation."
            )
        if reservation.get("available_before_tib") is not None:
            before, after = st.columns(2)
            before.metric(
                "Available before", f"{float(reservation['available_before_tib']):,.1f} TiB"
            )
            after.metric(
                "Available after", f"{float(reservation['available_after_tib']):,.1f} TiB"
            )
        if reservation.get("infrastructure_order_recommended"):
            st.warning(
                "This reservation crossed the 70% allocation threshold. Additional "
                "regional infrastructure should be ordered."
            )
        st.subheader("Jira handoff")
        cap_request = jira_by_type.get("CAP_RESERVATION")
        if cap_request:
            if cap_request["status"] == "COMPLETE":
                cap_url = safe_jira_ticket_url(
                    cap_request.get("jira_issue_key"),
                    cap_request.get("jira_issue_url"),
                    get_settings().jira_base_url,
                )
                st.success(
                    "CAP reservation ticket: "
                    + jira_ticket_link(
                        cap_request["jira_issue_key"], cap_request["jira_issue_url"]
                    )
                )
                if cap_url:
                    st.link_button("Open CAP ticket in Jira ↗", cap_url)
                else:
                    st.error("Mandatory Jira ticket link is unavailable.")
            elif cap_request["status"] == "FAILED":
                st.error(f"CAP ticket failed: {cap_request.get('last_error', '')}")
            else:
                st.info(f"CAP reservation ticket: {cap_request['status'].title()}")
        elif reservation.get("inventory_id") is not None and st.button(
            "Create CAP reservation ticket", type="primary", width="stretch"
        ):
            api(
                "POST",
                "/jira-requests",
                json={
                    "case_id": selected_case,
                    "request_type": "CAP_RESERVATION",
                    "planner_identity": reservation["planner_identity"],
                    "note": reservation.get("note", ""),
                    "confirm_create": True,
                },
            )
            st.success("CAP reservation ticket queued.")
            st.rerun()

        if reservation.get("infrastructure_order_recommended"):
            hub_request = jira_by_type.get("HUB_INFRASTRUCTURE")
            if hub_request:
                if hub_request["status"] == "COMPLETE":
                    hub_url = safe_jira_ticket_url(
                        hub_request.get("jira_issue_key"),
                        hub_request.get("jira_issue_url"),
                        get_settings().jira_base_url,
                    )
                    st.success(
                        "HUB infrastructure ticket: "
                        + jira_ticket_link(
                            hub_request["jira_issue_key"], hub_request["jira_issue_url"]
                        )
                    )
                    if hub_url:
                        st.link_button("Open HUB ticket in Jira ↗", hub_url)
                    else:
                        st.error("Mandatory Jira ticket link is unavailable.")
                elif hub_request["status"] == "FAILED":
                    st.error(f"HUB ticket failed: {hub_request.get('last_error', '')}")
                else:
                    st.info(f"HUB infrastructure ticket: {hub_request['status'].title()}")
            elif st.button("Order additional infrastructure in HUB", width="stretch"):
                api(
                    "POST",
                    "/jira-requests",
                    json={
                        "case_id": selected_case,
                        "request_type": "HUB_INFRASTRUCTURE",
                        "planner_identity": reservation["planner_identity"],
                        "note": reservation.get("note", ""),
                        "confirm_create": True,
                    },
                )
                st.success("HUB infrastructure ticket queued.")
                st.rerun()
        st.caption(
            f"Reservation ID: {reservation['reservation_id']}. This is an internal "
            "planning hold; no external Capacity Manager request was sent."
        )
    else:
        st.divider()
        st.subheader("Create local reservation")
        st.caption(
            "Records an audited planning hold in PostgreSQL. It does not provision "
            "physical storage or call an external Capacity Manager."
        )
        service = st.selectbox(
            "Service",
            ["Storage Capacity"],
            key=f"reservation_service_{selected_case}",
        )
        vaults = {
            "Storage Capacity": [
                "Standard",
                "High Performance",
                "Ultra Performance",
                "System Standard",
                "System Critical",
                "Replication",
                "General Purpose",
            ],
        }
        vault_type = st.selectbox(
            "Storage tier", vaults[service], key=f"reservation_vault_{selected_case}"
        )
        requested_tib = st.number_input(
            "Capacity to reserve (TiB)",
            min_value=0.01,
            max_value=10_000_000.0,
            value=max(0.01, round(growth, 2)),
            step=1.0,
            key=f"reservation_tib_{selected_case}",
        )
        query = str(
            httpx.QueryParams(
                {
                    "case_id": selected_case,
                    "service": service,
                    "vault_type": vault_type,
                    "requested_tib": requested_tib,
                }
            )
        )
        capacity_options = load(f"/capacity-availability?{query}")
        region = row["Region"]
        st.text_input(
            "Customer capacity region",
            value=region,
            disabled=True,
            key=f"reservation_region_{selected_case}",
        )
        if not capacity_options:
            st.error(
                "No capacity inventory exists for this region, service, and vault. "
                "Infrastructure ordering is required."
            )
            selected_capacity = None
        else:
            qfab = st.selectbox(
                "QFAB capacity pool",
                [option["qfab"] for option in capacity_options],
                format_func=lambda value: next(
                    f"{option['qfab']} · "
                    f"{float(option['available_capacity_tib']):,.1f} TiB available"
                    for option in capacity_options
                    if option["qfab"] == value
                ),
                key=f"reservation_qfab_{selected_case}",
            )
            selected_capacity = next(
                option for option in capacity_options if option["qfab"] == qfab
            )
            st.subheader("Regional capacity check")
            total_col, allocated_col, held_col = st.columns(3)
            total_col.metric(
                "Usable", f"{float(selected_capacity['usable_capacity_tib']):,.1f} TiB"
            )
            allocated_col.metric(
                "Allocated",
                f"{float(selected_capacity['allocated_capacity_tib']):,.1f} TiB",
            )
            held_col.metric(
                "Planning holds",
                f"{float(selected_capacity['planning_hold_tib']):,.1f} TiB",
            )
            available_col, request_col, after_col = st.columns(3)
            available_col.metric(
                "Available now",
                f"{float(selected_capacity['available_capacity_tib']):,.1f} TiB",
            )
            request_col.metric("Requested", f"{requested_tib:,.1f} TiB")
            after_col.metric(
                "Available after",
                f"{float(selected_capacity['available_after_tib']):,.1f} TiB",
            )
            post_pct = float(selected_capacity["post_reservation_allocation_pct"])
            st.progress(min(1.0, post_pct / 100), text=f"Post-reservation allocation: {post_pct:.1f}%")
            st.caption(
                f"Inventory: {selected_capacity['data_classification']} · "
                f"{selected_capacity['freshness_status']} · Refreshed "
                f"{refresh_label(selected_capacity['source_updated_at'])}"
            )
            if not selected_capacity["capacity_sufficient"]:
                st.error(
                    f"Insufficient capacity in {region}/{qfab}. Shortfall: "
                    f"{float(selected_capacity['shortfall_tib']):,.1f} TiB. "
                    "Do not reserve; order new regional infrastructure."
                )
            elif selected_capacity["infrastructure_order_required"]:
                st.warning(
                    "Capacity is sufficient for this reservation, but the resulting "
                    "allocation reaches the 70% infrastructure-order threshold."
                )
            else:
                st.success("Sufficient regional capacity is available for reservation.")
        today = datetime.now().astimezone().date()
        target_date = st.date_input(
            "Required by",
            value=today + timedelta(days=max(0, timing_days)),
            min_value=today,
            key=f"reservation_date_{selected_case}",
        )
        planner = st.text_input(
            "Planner identity", key=f"card_planner_{selected_case}"
        )
        note = st.text_area("Reservation note", key=f"card_note_{selected_case}")
        can_reserve = bool(
            selected_capacity and selected_capacity["capacity_sufficient"]
        )
        reservation_state = reservation_action_state(
            test_scenario=test_scenario,
            capacity_sufficient=can_reserve,
        )
        if can_reserve:
            confirmed = st.checkbox(
                "I confirm this local reservation against the available regional pool.",
                key=f"reservation_confirm_{selected_case}",
            )
            if not reservation_state["enabled"]:
                st.info(reservation_state["message"])
            if st.button(
                "Reserve available capacity",
                type="primary",
                width="stretch",
                disabled=not reservation_state["enabled"],
            ):
                if len(planner.strip()) < 2:
                    st.error("Enter the capacity planner's identity.")
                elif not confirmed:
                    st.error("Confirm the regional capacity reservation.")
                else:
                    reservation = api(
                        "POST",
                        "/reservations",
                        json={
                            "case_id": selected_case,
                            "requested_tib": requested_tib,
                            "target_date": target_date.isoformat(),
                            "service": service,
                            "vault_type": vault_type,
                            "region": region,
                            "qfab": qfab,
                            "planner_identity": planner.strip(),
                            "note": note.strip(),
                            "confirm_local_only": True,
                        },
                    )
                    st.success(
                        f"Reserved {float(reservation['requested_tib']):,.1f} TiB in "
                        "the local planning register. Jira handoff was queued."
                    )
                    if reservation.get("jira_errors"):
                        st.warning(
                            "The reservation succeeded, but a Jira handoff needs retry: "
                            + "; ".join(
                                item["error"] for item in reservation["jira_errors"]
                            )
                        )
                    st.rerun()
        elif selected_capacity:
            hub_request = jira_by_type.get("HUB_INFRASTRUCTURE")
            if hub_request:
                if hub_request["status"] == "COMPLETE":
                    hub_url = safe_jira_ticket_url(
                        hub_request.get("jira_issue_key"),
                        hub_request.get("jira_issue_url"),
                        get_settings().jira_base_url,
                    )
                    st.success(
                        "HUB ticket created: "
                        + jira_ticket_link(
                            hub_request["jira_issue_key"], hub_request["jira_issue_url"]
                        )
                    )
                    if hub_url:
                        st.link_button("Open HUB ticket in Jira ↗", hub_url)
                    else:
                        st.error("Mandatory Jira ticket link is unavailable.")
                elif hub_request["status"] == "FAILED":
                    st.error(f"HUB ticket failed: {hub_request.get('last_error', '')}")
                else:
                    st.info(f"HUB infrastructure order: {hub_request['status'].title()}")
            else:
                confirmed = st.checkbox(
                    "I confirm that the verified shortfall should be ordered through HUB.",
                    key=f"hub_confirm_{selected_case}",
                )
                if test_scenario:
                    st.info(
                        "Infrastructure ordering is disabled because this recommendation "
                        "is a planning simulation. Select a Source data recommendation "
                        "to create a HUB request."
                    )
                if st.button(
                    "Order new infrastructure in HUB",
                    type="primary",
                    width="stretch",
                    disabled=test_scenario,
                ):
                    if len(planner.strip()) < 2:
                        st.error("Enter the capacity planner's identity.")
                    elif not confirmed:
                        st.error("Confirm the HUB infrastructure order.")
                    else:
                        api(
                            "POST",
                            "/jira-requests",
                            json={
                                "case_id": selected_case,
                                "request_type": "HUB_INFRASTRUCTURE",
                                "service": service,
                                "vault_type": vault_type,
                                "qfab": qfab,
                                "requested_tib": requested_tib,
                                "target_date": target_date.isoformat(),
                                "planner_identity": planner.strip(),
                                "note": note.strip(),
                                "confirm_create": True,
                            },
                        )
                        st.success("HUB infrastructure order queued.")
                        st.rerun()

    st.divider()
    st.caption("Other planner dispositions")
    decision_planner = st.text_input(
        "Planner identity for disposition", key=f"disposition_planner_{selected_case}"
    )
    decision_note = st.text_area(
        "Disposition note", key=f"disposition_note_{selected_case}"
    )
    monitor, investigate = st.columns(2)
    decision = None
    if monitor.button("Monitor", width="stretch"):
        decision = "MONITOR"
    if investigate.button("Investigate", width="stretch"):
        decision = "REJECT_INVESTIGATE"
    if decision:
        if len(decision_planner.strip()) < 2:
            st.error("Enter the capacity planner's identity for the disposition.")
        else:
            api(
                "POST",
                f"/cases/{selected_case}/decisions",
                json={
                    "decision": decision,
                    "note": decision_note,
                    "decided_by": decision_planner.strip(),
                },
            )
            st.success("Planner disposition recorded.")


def render_local_reservations() -> None:
    st.title("Local capacity reservations")
    st.caption(
        "Audited internal planning holds. These records do not represent physical storage "
        "provisioning or external Capacity Manager allocations."
    )
    rows = load("/reservations")
    if not rows:
        st.info("No local capacity reservations have been created yet.")
        return
    frame = pd.DataFrame(rows).rename(
        columns={
            "company_name": "Customer",
            "ticker": "Ticker",
            "requested_tib": "Reserved (TiB)",
            "target_date": "Required by",
            "service": "Service",
            "vault_type": "Storage tier",
            "tenancy_type": "Capacity model",
            "region": "Region",
            "qfab": "QFAB",
            "status": "Status",
            "planner_identity": "Planner",
            "created_at": "Created",
            "reservation_id": "Reservation ID",
        }
    )
    total = pd.to_numeric(frame["Reserved (TiB)"], errors="coerce").sum()
    a, b, c = st.columns(3)
    a.metric("Active reservations", int((frame["Status"] == "LOCAL_RESERVED").sum()))
    b.metric("Capacity held locally", f"{total:,.1f} TiB")
    c.metric("Last reservation", refresh_label(frame["Created"].max()))
    display_columns = [
        "Customer",
        "Ticker",
        "Reserved (TiB)",
        "Required by",
        "Service",
        "Storage tier",
        "Capacity model",
        "Region",
        "QFAB",
        "Status",
        "Planner",
        "Created",
        "Reservation ID",
    ]
    st.dataframe(frame[display_columns], hide_index=True, width="stretch")


def render_capacity_inventory() -> None:
    st.title("Regional capacity inventory")
    st.caption(
        "Current allocatable pools by region, QFAB, service, vault, and tenancy."
    )
    frame = pd.DataFrame(load("/capacity-inventory"))
    if frame.empty:
        st.error("No regional capacity inventory is available.")
        return
    numeric = [
        "usable_capacity_tib",
        "allocated_capacity_tib",
        "planning_hold_tib",
        "available_capacity_tib",
        "current_allocation_pct",
    ]
    for column in numeric:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    region = st.selectbox("Region", sorted(frame["region"].unique()))
    visible = frame[frame["region"] == region].copy()
    a, b, c, d = st.columns(4)
    a.metric("Usable capacity", f"{visible['usable_capacity_tib'].sum():,.1f} TiB")
    b.metric("Physically allocated", f"{visible['allocated_capacity_tib'].sum():,.1f} TiB")
    c.metric("Planning holds", f"{visible['planning_hold_tib'].sum():,.1f} TiB")
    d.metric("Available", f"{visible['available_capacity_tib'].sum():,.1f} TiB")
    display = visible.rename(
        columns={
            "qfab": "QFAB",
            "service": "Service",
            "vault_type": "Storage tier",
            "tenancy_type": "Capacity model",
            "usable_capacity_tib": "Usable TiB",
            "allocated_capacity_tib": "Allocated TiB",
            "planning_hold_tib": "Planning holds TiB",
            "available_capacity_tib": "Available TiB",
            "current_allocation_pct": "Allocated %",
            "freshness_status": "Freshness",
            "source_updated_at": "Last refresh",
        }
    )
    st.dataframe(
        display[
            [
                "QFAB",
                "Service",
                "Storage tier",
                "Capacity model",
                "Usable TiB",
                "Allocated TiB",
                "Planning holds TiB",
                "Available TiB",
                "Allocated %",
                "Freshness",
                "Last refresh",
            ]
        ],
        hide_index=True,
        width="stretch",
    )


def render_jira_requests() -> None:
    st.title("Jira capacity requests")
    st.caption(
        "CAP handles reservations from existing supply; HUB handles regional infrastructure "
        "orders. Requests are created only after explicit planner action."
    )
    rows = load("/jira-requests")
    if not rows:
        st.info("No Jira capacity requests have been queued yet.")
        return
    completed_links = [
        {
            "key": row["jira_issue_key"],
            "url": safe_jira_ticket_url(
                row.get("jira_issue_key"),
                row.get("jira_issue_url"),
                get_settings().jira_base_url,
            ),
            "route": row["request_type"],
        }
        for row in rows
        if row.get("jira_issue_key")
    ]
    completed_links = [item for item in completed_links if item["url"]]
    if completed_links:
        st.subheader("Open completed Jira tickets")
        link_columns = st.columns(3)
        for index, item in enumerate(completed_links):
            label = item["route"].replace("_", " ").title()
            with link_columns[index % len(link_columns)], st.container(border=True):
                st.markdown(f"#### [{item['key']}]({item['url']})")
                st.caption(label)
                st.link_button(
                    f"Open {item['key']} in Jira ↗",
                    item["url"],
                    width="stretch",
                )
    else:
        st.info("Jira tickets are still being created. Links appear after delivery completes.")

    frame = pd.DataFrame(rows)
    frame["jira_ticket"] = frame.apply(
        lambda row: safe_jira_ticket_url(
            row.get("jira_issue_key"),
            row.get("jira_issue_url"),
            get_settings().jira_base_url,
        ),
        axis=1,
    )
    frame = frame.rename(
        columns={
            "company_name": "Customer",
            "ticker": "Ticker",
            "request_type": "Route",
            "project_key": "Project",
            "summary": "Summary",
            "status": "Status",
            "jira_ticket": "Jira ticket",
            "region": "Region",
            "qfab": "QFAB",
            "planner_identity": "Planner",
            "created_at": "Created",
            "last_error": "Last error",
        }
    )
    st.dataframe(
        frame[
            [
                "Customer",
                "Ticker",
                "Region",
                "QFAB",
                "Route",
                "Project",
                "Status",
                "Jira ticket",
                "Summary",
                "Planner",
                "Created",
                "Last error",
            ]
        ],
        hide_index=True,
        width="stretch",
        column_config={"Jira ticket": st.column_config.LinkColumn("Jira ticket")},
    )


def render_slack_alerts() -> None:
    st.title("Slack capacity alerts")
    st.caption(
        "The autonomous Slack worker sends a changed digest after the configured cooldown. "
        "Slack is informational; planners make decisions in this application."
    )
    summary = load("/slack-alerts/summary")
    a, b, c = st.columns(3)
    a.metric("Demand signals waiting", summary["demand_review_count"])
    b.metric("Reserve capacity", summary["reserve_capacity_count"])
    c.metric("Order more storage", summary["order_more_storage_count"])
    if summary["production_filter_enabled"]:
        st.info(
            "Production safety filter is enabled. Failed, degraded, and synthetic-only "
            "cases are excluded from Slack alerts."
        )
    if summary["reserve_capacity"]:
        with st.expander("Customers that may use available capacity"):
            st.dataframe(pd.DataFrame(summary["reserve_capacity"]), hide_index=True)
    if summary["order_more_storage"]:
        with st.expander("Customers that may require more infrastructure"):
            st.dataframe(pd.DataFrame(summary["order_more_storage"]), hide_index=True)
    confirm = st.checkbox("Send the current digest to the configured Slack channel now.")
    if st.button("Queue Slack digest", type="primary", disabled=not confirm):
        result = api(
            "POST",
            "/slack-alerts/enqueue",
            json={"force": True, "confirm_send": True},
        )
        if result["queued"]:
            st.success("Slack digest queued. The Slack worker will deliver it.")
        else:
            st.info("No eligible demand signals changed; no duplicate alert was queued.")
    rows = load("/slack-alerts")
    st.subheader("Delivery history")
    if not rows:
        st.info("No Slack alerts have been queued yet.")
        return
    frame = pd.DataFrame(rows)
    st.dataframe(
        frame[
            [
                "status",
                "attempt_count",
                "slack_channel",
                "slack_message_ts",
                "created_at",
                "completed_at",
                "last_error",
            ]
        ],
        hide_index=True,
        width="stretch",
    )


def render_regional_hub_request() -> None:
    with st.expander("Create regional HUB request", expanded=False):
        st.write(
            "Order shared infrastructure for a regional capacity pool without attaching "
            "the request to a customer. Jira creation requires explicit planner confirmation."
        )
        inventory = pd.DataFrame(load("/capacity-inventory"))
        if inventory.empty:
            st.error("No regional capacity inventory is available.")
            return
        inventory = inventory[inventory["freshness_status"] == "FRESH"].copy()
        if inventory.empty:
            st.error("No fresh regional capacity inventory is available.")
            return

        regions = sorted(inventory["region"].unique().tolist())
        region = st.selectbox("Region", regions, key="regional_hub_region")
        pools = inventory[inventory["region"] == region].copy()
        pool_ids = pools["inventory_id"].tolist()
        pool_labels = {
            row["inventory_id"]: (
                f"{row['qfab']} · {row['vault_type']} · {row['tenancy_type']} · "
                f"{float(row['available_capacity_tib']):,.1f} TiB available"
            )
            for _, row in pools.iterrows()
        }
        selected_inventory_id = st.selectbox(
            "Capacity pool",
            pool_ids,
            format_func=pool_labels.get,
            key="regional_hub_pool",
        )
        pool = pools[pools["inventory_id"] == selected_inventory_id].iloc[0]
        m1, m2, m3 = st.columns(3)
        m1.metric("Usable capacity", f"{float(pool['usable_capacity_tib']):,.1f} TiB")
        m2.metric("Available now", f"{float(pool['available_capacity_tib']):,.1f} TiB")
        m3.metric("Current allocation", f"{float(pool['current_allocation_pct']):.1f}%")

        with st.form("regional_hub_request_form"):
            requested_tib = st.number_input(
                "New infrastructure to order (TiB)",
                min_value=0.01,
                max_value=10_000_000.0,
                value=100.0,
                step=10.0,
            )
            today = datetime.now().astimezone().date()
            target_date = st.date_input(
                "Required by", value=today + timedelta(days=90), min_value=today
            )
            planner_identity = st.text_input("Planner identity")
            note = st.text_area("Business justification")
            confirmed = st.checkbox(
                "I confirm this regional infrastructure request should be sent to HUB."
            )
            submitted = st.form_submit_button(
                "Create HUB request", type="primary", width="stretch"
            )
        if submitted:
            if len(planner_identity.strip()) < 2:
                st.error("Enter the capacity planner's identity.")
            elif len(note.strip()) < 5:
                st.error("Enter a business justification for the infrastructure order.")
            elif not confirmed:
                st.error("Confirm the regional HUB request.")
            else:
                result = api(
                    "POST",
                    "/jira-requests",
                    json={
                        "request_type": "HUB_INFRASTRUCTURE",
                        "region": region,
                        "service": pool["service"],
                        "vault_type": pool["vault_type"],
                        "qfab": pool["qfab"],
                        "requested_tib": requested_tib,
                        "target_date": target_date.isoformat(),
                        "planner_identity": planner_identity.strip(),
                        "note": note.strip(),
                        "confirm_create": True,
                    },
                )
                if result.get("created"):
                    st.success("Regional HUB request queued for Jira delivery.")
                else:
                    st.info("An active HUB request already exists for this capacity pool.")
                st.rerun()


def render_latest_jira_handoff() -> None:
    """Keep the latest approved reservation visible after it leaves the review inbox."""
    rows = load("/jira-requests")
    if not rows:
        return

    rows = sorted(
        rows,
        key=lambda row: str(row.get("created_at") or ""),
        reverse=True,
    )
    latest = rows[0]
    latest_case_id = latest.get("case_id")
    handoffs = (
        [row for row in rows if row.get("case_id") == latest_case_id]
        if latest_case_id
        else [latest]
    )

    with st.container(border=True):
        st.subheader("Latest reservation and Jira handoff")
        if latest_case_id:
            st.caption(
                f"{latest['company_name']} ({latest['ticker']}) · "
                "The approved item has moved out of the unresolved review inbox."
            )
        else:
            st.caption(
                f"Regional capacity request · {latest['region']} / {latest['qfab']} · "
                "No customer is attached."
            )
        columns = st.columns(len(handoffs))
        for column, handoff in zip(columns, handoffs, strict=False):
            with column:
                route = (
                    "Reserve existing capacity"
                    if handoff["request_type"] == "CAP_RESERVATION"
                    else "Order more storage"
                )
                st.write(f"**{route}**")
                if handoff["request_type"] == "CAP_RESERVATION":
                    st.caption("Customer")
                    customer = handoff.get("company_name") or "Customer not available"
                    ticker = handoff.get("ticker")
                    st.write(f"**{customer} ({ticker})**" if ticker else f"**{customer}**")
                else:
                    st.caption("Storage region")
                    region = handoff.get("region") or "Region not available"
                    qfab = handoff.get("qfab")
                    st.write(f"**{region} · {qfab}**" if qfab else f"**{region}**")
                status = str(handoff["status"]).replace("_", " ").title()
                st.write(f"Status: **{status}**")
                handoff_url = safe_jira_ticket_url(
                    handoff.get("jira_issue_key"),
                    handoff.get("jira_issue_url"),
                    get_settings().jira_base_url,
                )
                if handoff.get("jira_issue_key") and handoff_url:
                    st.success(
                        "Jira ticket: "
                        + jira_ticket_link(
                            handoff["jira_issue_key"], handoff["jira_issue_url"]
                        )
                    )
                    st.link_button(
                        f"Open {handoff['jira_issue_key']}",
                        handoff_url,
                        width="stretch",
                    )
                elif handoff.get("jira_issue_key"):
                    st.error("Mandatory Jira ticket link is unavailable.")
                elif handoff["status"] == "FAILED":
                    st.error(handoff.get("last_error") or "Jira delivery failed")
                else:
                    st.info("The Jira worker is creating the ticket.")

        if st.button("Refresh Jira status", key="refresh_latest_jira"):
            st.rerun()


def render_home_follow_up() -> None:
    st.divider()
    st.subheader("Follow-up and supply actions")
    st.caption(
        "Track the latest Jira handoff or raise a regional infrastructure order that is not "
        "attached to a customer."
    )
    handoff_tab, regional_tab = st.tabs(
        ["Recent Jira handoff", "Order regional infrastructure"]
    )
    with handoff_tab:
        render_latest_jira_handoff()
    with regional_tab:
        render_regional_hub_request()


def render_review_queue(portfolio: dict) -> None:
    st.title("Planner inbox")
    st.caption(
        "Start with the highest-priority recommendation, verify the evidence and regional "
        "supply, then reserve capacity or order infrastructure."
    )
    shortlist = prepare_shortlist(
        load(
            "/shortlist?min_likelihood=80&pending_only=true&alert_eligible_only=true"
        )
    )
    if shortlist.empty:
        st.success("You are caught up — there are no recommendations awaiting review.")
        render_home_follow_up()
        return

    inventory = pd.DataFrame(load("/capacity-inventory"))
    if inventory.empty:
        available_by_region = {}
    else:
        inventory["available_capacity_tib"] = pd.to_numeric(
            inventory["available_capacity_tib"], errors="coerce"
        ).fillna(0)
        inventory["usable_capacity_tib"] = pd.to_numeric(
            inventory["usable_capacity_tib"], errors="coerce"
        ).fillna(0)
        inventory["allocated_capacity_tib"] = pd.to_numeric(
            inventory["allocated_capacity_tib"], errors="coerce"
        ).fillna(0)
        inventory["planning_hold_tib"] = pd.to_numeric(
            inventory["planning_hold_tib"], errors="coerce"
        ).fillna(0)
        inventory["capacity_before_order_threshold_tib"] = (
            inventory["usable_capacity_tib"] * 0.70
            - inventory["allocated_capacity_tib"]
            - inventory["planning_hold_tib"]
        ).clip(lower=0)
        usable = inventory[inventory["freshness_status"] == "FRESH"]
        available_by_region = (
            usable.groupby("region")["available_capacity_tib"].max().to_dict()
        )
        safe_capacity_by_region = (
            usable.groupby("region")["capacity_before_order_threshold_tib"]
            .max()
            .to_dict()
        )
    if inventory.empty:
        safe_capacity_by_region = {}
    shortlist["Best available regional pool (TiB)"] = (
        shortlist["Region"].map(available_by_region).fillna(0)
    )
    shortlist["Capacity before 70% threshold (TiB)"] = (
        shortlist["Region"].map(safe_capacity_by_region).fillna(0)
    )
    growth = pd.to_numeric(shortlist["Suggested growth (TiB)"], errors="coerce")
    shortlist["Capacity action"] = "Order more storage"
    shortlist.loc[
        growth.notna()
        & (growth <= shortlist["Capacity before 70% threshold (TiB)"]),
        "Capacity action",
    ] = "Reserve available capacity"

    reserve_count = int(
        (shortlist["Capacity action"] == "Reserve available capacity").sum()
    )
    order_count = int((shortlist["Capacity action"] == "Order more storage").sum())
    scenario_count = int(shortlist["Planning simulation"].sum())
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Needs your review", len(shortlist))
    c2.metric("Can use available supply", reserve_count)
    c3.metric("Needs new infrastructure", order_count)
    c4.metric("Planning simulations", scenario_count)

    st.subheader("Recommended actions")
    filter_one, filter_two = st.columns(2)
    route_filter = filter_one.selectbox(
        "Next step",
        ["All recommendations", "Reserve available capacity", "Order more storage"],
    )
    region_filter = filter_two.selectbox(
        "Region", ["All regions", *sorted(shortlist["Region"].unique().tolist())]
    )
    visible = shortlist
    if route_filter != "All recommendations":
        visible = shortlist[shortlist["Capacity action"] == route_filter]
    if region_filter != "All regions":
        visible = visible[visible["Region"] == region_filter]
    inbox = visible[
        [
            "Customer",
            "Region",
            "Expansion likelihood %",
            "Evidence confidence",
            "Expected need (days)",
            "Suggested growth (TiB)",
            "Best available regional pool (TiB)",
            "Capacity action",
            "Signal type",
        ]
    ].copy()
    inbox = inbox.rename(
        columns={
            "Expansion likelihood %": "Likelihood",
            "Evidence confidence": "Confidence",
            "Expected need (days)": "Need in days",
            "Suggested growth (TiB)": "Growth TiB",
            "Best available regional pool (TiB)": "Available TiB",
            "Capacity action": "Next step",
            "Signal type": "Signal",
        }
    )
    st.dataframe(
        inbox,
        hide_index=True,
        width="stretch",
        column_config={
            "Likelihood": st.column_config.ProgressColumn(
                "Likelihood", min_value=0, max_value=100, format="%.0f%%"
            ),
            "Growth TiB": st.column_config.NumberColumn(format="%.1f"),
            "Available TiB": st.column_config.NumberColumn(format="%.1f"),
        },
    )
    st.caption(
        "Reserve capacity means at least one fresh regional pool can absorb the estimated growth "
        "and remain below the 70% planning threshold. The selected pool is revalidated when "
        "the planner confirms the action."
    )

    if visible.empty:
        st.info("No pending items match this action filter.")
        return

    st.divider()
    st.subheader("Review a recommendation")
    render_customer_recommendation(visible)

    render_home_follow_up()

    results_tab, adjust_tab = st.tabs(["All recommendation details", "Adjust AI forecasts"])
    with results_tab:
        display = shortlist[
            [
                "Customer",
                "Ticker",
                "Expansion likelihood %",
                "Evidence confidence",
                "Expected need (days)",
                "Suggested growth (TiB)",
                "Data quality %",
                "Production ready",
                "Recommended workflow",
                "Forecast source",
            ]
        ].copy()
        display["Recommended workflow"] = display["Recommended workflow"].replace(
            {"PLANNER_REVIEW": "Planner review", "MONITOR": "Monitor"}
        )
        st.dataframe(display, hide_index=True, width="stretch")
        st.caption("Only customers with an effective likelihood of at least 75% appear here.")

    with adjust_tab:
        st.write(
            "Use this workspace only when planner evidence supports changing the AI forecast. "
            "Every saved change is append-only and audited."
        )
        edit_enabled = st.toggle("Enable planner edit mode", value=False)
        columns = [
            "Case ID",
            "Customer",
            "Ticker",
            "Expansion likelihood %",
            "Evidence confidence",
            "Expected need (days)",
            "Suggested growth (TiB)",
            "Data quality %",
            "Production ready",
            "Recommended workflow",
            "Forecast source",
        ]
        original = shortlist[columns].copy()
        edited = st.data_editor(
            original,
            hide_index=True,
            width="stretch",
            disabled=(
                True
                if not edit_enabled
                else [
                    "Customer",
                    "Ticker",
                    "Data quality %",
                    "Production ready",
                    "Forecast source",
                ]
            ),
            column_config={
                "Case ID": None,
                "Expansion likelihood %": st.column_config.NumberColumn(
                    min_value=0, max_value=100, step=1
                ),
                "Evidence confidence": st.column_config.SelectboxColumn(
                    options=["LOW", "MEDIUM", "HIGH"]
                ),
                "Expected need (days)": st.column_config.NumberColumn(
                    min_value=0, max_value=3650, step=1
                ),
                "Suggested growth (TiB)": st.column_config.NumberColumn(
                    min_value=0.0, step=1.0
                ),
                "Recommended workflow": st.column_config.SelectboxColumn(
                    options=["PLANNER_REVIEW", "MONITOR"]
                ),
            },
            key="planner_forecast_editor",
        )
        if not edit_enabled:
            st.caption("Turn on planner edit mode to change forecast fields.")
            return

        st.caption("Double-click a forecast cell to edit it, then press Enter.")
        planner_identity = st.text_input("Planner identity", key="bulk_planner_identity")
        reason = st.text_area(
            "Reason for adjustment", max_chars=2000, key="bulk_adjustment_reason"
        )
        if st.button("Save audited forecast changes", type="primary"):
            editable = {
                "Expansion likelihood %": "likelihood_pct",
                "Evidence confidence": "confidence",
                "Expected need (days)": "timing_days",
                "Suggested growth (TiB)": "capacity_growth_tib",
                "Recommended workflow": "action",
            }

            def same(left, right):
                return (pd.isna(left) and pd.isna(right)) or left == right

            originals = original.set_index("Case ID")
            overrides = []
            for _, row in edited.iterrows():
                case_id = row["Case ID"]
                before = originals.loc[case_id]
                if not any(not same(row[column], before[column]) for column in editable):
                    continue
                timing = row["Expected need (days)"]
                growth = row["Suggested growth (TiB)"]
                overrides.append(
                    {
                        "case_id": case_id,
                        "likelihood_pct": float(row["Expansion likelihood %"]),
                        "confidence": row["Evidence confidence"],
                        "timing_days": None if pd.isna(timing) else int(timing),
                        "capacity_growth_tib": None if pd.isna(growth) else float(growth),
                        "action": row["Recommended workflow"],
                    }
                )
            if len(planner_identity.strip()) < 2:
                st.error("Enter the capacity planner's identity.")
            elif not reason.strip():
                st.error("Explain why the AI forecast is being adjusted.")
            elif not overrides:
                st.info("No forecast changes were detected.")
            else:
                response = api(
                    "POST",
                    "/shortlist/overrides",
                    json={
                        "overrides": overrides,
                        "note": reason.strip(),
                        "modified_by": planner_identity.strip(),
                    },
                )
                st.success(f"Saved {response['updated']} audited planner adjustments.")
                st.rerun()


def render_case_result(case_id: str) -> None:
    current = api("GET", f"/cases/{case_id}")
    status = current["status"].replace("_", " ").title()
    st.subheader("Investigation result")
    st.write(f"Status: **{status}**")
    if current["status"] in ("QUEUED", "RUNNING", "RETRY"):
        st.info("The autonomous backend is processing this investigation.")
        if st.button("Refresh investigation status"):
            time.sleep(0.2)
            st.rerun()
        return
    if not current.get("recommendation"):
        return

    recommendation = current["recommendation"]
    a, b, c = st.columns(3)
    a.metric("Expansion likelihood", f"{recommendation.get('likelihood_pct', 0)}%")
    b.metric("Evidence confidence", recommendation.get("confidence", "UNKNOWN"))
    c.metric(
        "Recommended workflow", recommendation.get("action", "UNKNOWN").replace("_", " ")
    )
    st.write("Why the agent reached this result")
    for reason in recommendation.get("reasons", []):
        st.write(f"- {reason}")

    quality_event = next(
        (event for event in current["events"] if event["event_type"] == "data_quality"),
        None,
    )
    if quality_event:
        quality = quality_event["payload"]
        q1, q2, q3 = st.columns(3)
        q1.metric("Data quality", f"{quality.get('quality_score_pct', 0):.1f}%")
        q2.metric("Technical quality", f"{quality.get('technical_quality_score_pct', 0):.1f}%")
        q3.metric("Production ready", "Yes" if quality.get("production_eligible") else "No")
        if quality.get("failed_checks"):
            st.warning("Failed checks: " + ", ".join(quality["failed_checks"]))

    news_event = next(
        (event for event in current["events"] if event["event_type"] == "news"), None
    )
    if news_event:
        news = news_event["payload"]
        with st.expander("News and SEC evidence", expanded=False):
            if news.get("status") == "NO_RELEVANT_EVIDENCE":
                st.info(
                    f"No relevant announcements were found in the past "
                    f"{news.get('lookback_days', 180)} days."
                )
            for article in news.get("items", []):
                st.markdown(f"**{article['title']}**")
                st.write(article["excerpt"])
                source_url = safe_source_url(article.get("source_url", ""))
                if source_url:
                    st.link_button("Open source", source_url)
            if news.get("items"):
                st.download_button(
                    "Download evidence JSON",
                    json.dumps(news["items"], indent=2, default=str),
                    file_name=f"news-evidence-{case_id}.json",
                    mime="application/json",
                )

    memory_event = next(
        (event for event in current["events"] if event["event_type"] == "memory"), None
    )
    if memory_event and memory_event["payload"].get("items"):
        with st.expander("Prior planner decisions", expanded=False):
            for item in memory_event["payload"]["items"]:
                st.write(item.get("memory", ""))

    with st.expander("Technical evidence and audit trail", expanded=False):
        for event in current["events"]:
            st.write(event["event_type"].replace("_", " ").title())
            st.json(event["payload"])

    st.subheader("Planner decision")
    identity = st.text_input("Planner identity", key="decision_identity")
    choice = st.radio(
        "Disposition",
        ["APPROVE_REVIEW", "MONITOR", "REJECT_INVESTIGATE"],
        format_func=lambda value: value.replace("_", " ").title(),
        horizontal=True,
    )
    note = st.text_area("Decision note", key="decision_note")
    if st.button("Record planner decision"):
        if len(identity.strip()) < 2:
            st.error("Enter the capacity planner's identity.")
        else:
            api(
                "POST",
                f"/cases/{case_id}/decisions",
                json={"decision": choice, "note": note, "decided_by": identity.strip()},
            )
            st.success("Decision recorded. No capacity was reserved or provisioned.")


def render_customer_portfolio() -> None:
    st.title("Customer portfolio")
    st.caption(
        "Compare every customer using current capacity signals, the latest autonomous "
        "recommendation, and recorded planner actions."
    )
    portfolio = pd.DataFrame(load("/companies?limit=1000"))
    if portfolio.empty:
        st.info("No customer data is available.")
        return

    numeric_columns = [
        "installed_tib",
        "consumed_tib",
        "utilization_pct",
        "trailing_12m_growth_tib",
        "open_demand_tib",
        "likelihood_pct",
        "timing_days",
        "suggested_growth_tib",
        "reserved_tib",
    ]
    for column in numeric_columns:
        portfolio[column] = pd.to_numeric(portfolio[column], errors="coerce")

    portfolio["review_state"] = "Not investigated"
    has_recommendation = portfolio["likelihood_pct"].notna()
    portfolio.loc[has_recommendation, "review_state"] = "Monitoring"
    portfolio.loc[
        has_recommendation & (portfolio["likelihood_pct"] >= 75), "review_state"
    ] = "Needs planner review"
    portfolio.loc[
        portfolio["latest_case_status"].isin(["QUEUED", "RUNNING", "RETRY"]),
        "review_state",
    ] = "Investigation in progress"
    portfolio.loc[portfolio["planner_decision"].notna(), "review_state"] = "Reviewed"

    total = len(portfolio)
    high_utilization = int((portfolio["utilization_pct"] >= 85).sum())
    needs_review = int((portfolio["review_state"] == "Needs planner review").sum())
    not_investigated = int((portfolio["review_state"] == "Not investigated").sum())
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Customers", total)
    m2.metric("At or above 85% utilization", high_utilization)
    m3.metric("Needs planner review", needs_review)
    m4.metric("Not investigated", not_investigated)

    f1, f2, f3 = st.columns([1.4, 1, 1])
    search = f1.text_input(
        "Search customers",
        placeholder="Customer name or ticker",
        key="portfolio_customer_search",
    )
    regions = sorted(portfolio["region"].dropna().unique().tolist())
    selected_regions = f2.multiselect("Region", regions, placeholder="All regions")
    states = [
        "Needs planner review",
        "Investigation in progress",
        "Reviewed",
        "Monitoring",
        "Not investigated",
    ]
    selected_states = f3.multiselect("Planner state", states, placeholder="All states")

    visible = portfolio.copy()
    if search:
        visible = visible[
            visible["company_name"].str.contains(search, case=False, na=False)
            | visible["ticker"].str.contains(search, case=False, na=False)
        ]
    if selected_regions:
        visible = visible[visible["region"].isin(selected_regions)]
    if selected_states:
        visible = visible[visible["review_state"].isin(selected_states)]

    visible = visible.sort_values(
        ["likelihood_pct", "utilization_pct"], ascending=[False, False], na_position="last"
    )
    st.caption(f"Showing {len(visible):,} of {total:,} customers")
    planner_view = visible.rename(
        columns={
            "company_name": "Customer",
            "ticker": "Ticker",
            "region": "Region",
            "utilization_pct": "Utilization %",
            "installed_tib": "Installed TiB",
            "consumed_tib": "Consumed TiB",
            "trailing_12m_growth_tib": "12-month growth TiB",
            "open_demand_tib": "Open demand TiB",
            "demand_stage": "Demand stage",
            "likelihood_pct": "Expansion likelihood %",
            "confidence": "Evidence confidence",
            "timing_days": "Expected need days",
            "suggested_growth_tib": "Suggested growth TiB",
            "review_state": "Planner state",
            "reserved_tib": "Reserved TiB",
            "last_recommendation_at": "Last recommendation",
        }
    )
    display_columns = [
        "Customer",
        "Ticker",
        "Region",
        "Utilization %",
        "Installed TiB",
        "Consumed TiB",
        "12-month growth TiB",
        "Open demand TiB",
        "Demand stage",
        "Expansion likelihood %",
        "Evidence confidence",
        "Expected need days",
        "Suggested growth TiB",
        "Reserved TiB",
        "Planner state",
        "Last recommendation",
    ]
    st.dataframe(
        planner_view[display_columns],
        hide_index=True,
        width="stretch",
        height=620,
        column_config={
            "Utilization %": st.column_config.ProgressColumn(
                min_value=0, max_value=100, format="%.1f%%"
            ),
            "Expansion likelihood %": st.column_config.ProgressColumn(
                min_value=0, max_value=100, format="%.1f%%"
            ),
        },
    )
    st.download_button(
        "Download filtered customer data",
        visible.to_csv(index=False).encode("utf-8"),
        file_name="capacitypilot-customer-portfolio.csv",
        mime="text/csv",
    )


def render_portfolio_chat() -> None:
    st.title("Ask CapacityPilot")
    st.caption(
        "Ask read-only questions across the entire customer portfolio. Answers are grounded "
        "in current PostgreSQL capacity signals and the latest saved recommendations."
    )
    st.info(
        "Try: “Which 10 customers have the highest utilization?”, “How many customers need "
        "planner review?”, “How many reservations were approved in the last 24 hours?”, or "
        "“Summarize open demand for APAC customers above 80% utilization.”"
    )

    if "portfolio_chat_messages" not in st.session_state:
        st.session_state.portfolio_chat_messages = []

    def render_answer(result: dict) -> None:
        st.markdown(result["answer"])
        if result.get("interpretation_source") == "SAFE_FALLBACK":
            st.caption(
                "Nebius was unavailable, so CapacityPilot used its restricted built-in "
                "portfolio interpreter. The answer is still calculated from PostgreSQL."
            )
        elif result.get("interpretation_source") == "DETERMINISTIC_AUDIT":
            st.caption(
                "This transactional answer was calculated directly from PostgreSQL's "
                "audited local reservation records; the LLM was not used."
            )
        elif result.get("interpretation_source") == "DETERMINISTIC_PORTFOLIO":
            st.caption(
                "This standard portfolio count was calculated directly from PostgreSQL; "
                "the LLM was not used."
            )
        rows = pd.DataFrame(result.get("rows", []))
        if not rows.empty and "reservation_id" in rows.columns:
            rows = rows.rename(
                    columns={
                        "reservation_id": "Reservation ID",
                        "company_name": "Customer",
                        "ticker": "Ticker",
                        "region": "Region",
                        "qfab": "QFAB",
                        "requested_tib": "Reserved TiB",
                        "target_date": "Target date",
                        "status": "Status",
                        "planner_identity": "Planner",
                        "created_at": "Approved at",
                    }
            )
            st.dataframe(
                rows[
                        [
                            "Customer",
                            "Ticker",
                            "Region",
                            "QFAB",
                            "Reserved TiB",
                            "Target date",
                            "Status",
                            "Planner",
                            "Approved at",
                            "Reservation ID",
                        ]
                ],
                hide_index=True,
                width="stretch",
            )
            rows = pd.DataFrame()
        if not rows.empty:
            rows = rows.rename(
                columns={
                    "company_name": "Customer",
                    "ticker": "Ticker",
                    "region": "Region",
                    "utilization_pct": "Utilization %",
                    "annual_growth_tib": "12-month growth TiB",
                    "open_demand_tib": "Open demand TiB",
                    "likelihood_pct": "Expansion likelihood %",
                    "confidence": "Confidence",
                    "timing_days": "Expected need days",
                    "suggested_growth_tib": "Suggested growth TiB",
                    "planner_state": "Planner state",
                }
            )
            columns = [
                "Customer",
                "Ticker",
                "Region",
                "Utilization %",
                "12-month growth TiB",
                "Open demand TiB",
                "Expansion likelihood %",
                "Confidence",
                "Expected need days",
                "Suggested growth TiB",
                "Planner state",
            ]
            st.dataframe(rows[columns], hide_index=True, width="stretch")
        with st.expander("How this question was interpreted"):
            st.json(result.get("interpreted_as", {}))

    for message in st.session_state.portfolio_chat_messages:
        with st.chat_message(message["role"]):
            if message["role"] == "assistant":
                render_answer(message["result"])
            else:
                st.markdown(message["content"])

    question = st.chat_input("Ask about customers, demand, utilization, regions, or forecasts")
    if question:
        prior_question = next(
            (
                message["content"]
                for message in reversed(st.session_state.portfolio_chat_messages)
                if message["role"] == "user"
            ),
            None,
        )
        prior_result = next(
            (
                message["result"]
                for message in reversed(st.session_state.portfolio_chat_messages)
                if message["role"] == "assistant"
            ),
            None,
        )
        prior_interpretation = (prior_result or {}).get("interpreted_as", {})
        chat_context = None
        if str(prior_interpretation.get("intent", "")).startswith(
            "RESERVATION_AUDIT_"
        ):
            chat_context = {
                "previous_question": prior_question,
                "previous_intent": prior_interpretation.get("intent"),
                "previous_time_window_hours": prior_interpretation.get(
                    "time_window_hours"
                ),
            }
        st.session_state.portfolio_chat_messages.append(
            {"role": "user", "content": question}
        )
        with st.chat_message("user"):
            st.markdown(question)
        with st.chat_message("assistant"):
            with st.spinner("Reviewing the customer portfolio..."):
                try:
                    result = api(
                        "POST",
                        "/portfolio/chat",
                        json={"question": question, "context": chat_context},
                    )
                except httpx.HTTPError as exc:
                    st.error(f"CapacityPilot could not answer this question: {exc}")
                    return
            render_answer(result)
        st.session_state.portfolio_chat_messages.append(
            {"role": "assistant", "result": result}
        )

    if st.session_state.portfolio_chat_messages and st.button("Clear conversation"):
        st.session_state.portfolio_chat_messages = []
        st.rerun()


def render_customer_investigation() -> None:
    st.title("Customer investigation")
    st.caption(
        "Request an ad hoc rerun when a planner needs a fresh result for a specific customer."
    )
    companies = pd.DataFrame(load("/companies?limit=1000"))
    query = st.text_input("Search by customer name or ticker")
    visible = companies
    if query:
        visible = companies[
            companies.company_name.str.contains(query, case=False, na=False)
            | companies.ticker.str.contains(query, case=False, na=False)
        ]
    if visible.empty:
        st.info("No matching customers found.")
        return
    selected = st.selectbox(
        "Customer",
        visible.company_id.tolist(),
        format_func=lambda company_id: (
            f"{visible.loc[visible.company_id == company_id, 'company_name'].iloc[0]} "
            f"({visible.loc[visible.company_id == company_id, 'ticker'].iloc[0]})"
        ),
    )
    company = visible[visible.company_id == selected].iloc[0]
    c1, c2, c3 = st.columns(3)
    c1.metric("Current utilization", f"{company.utilization_pct}%")
    c2.metric("12-month growth", f"{company.trailing_12m_growth_tib} TiB")
    c3.metric("Open demand", f"{company.open_demand_tib} TiB")
    if st.button("Request fresh investigation", type="primary"):
        case = api("POST", "/cases", json={"company_id": int(company.company_id)})
        st.session_state.case_id = str(case["case_id"])
        st.success("Request queued for the autonomous backend.")
    if st.session_state.get("case_id"):
        render_case_result(st.session_state.case_id)


def render_quality_evals() -> None:
    st.title("Quality, evals & orchestration")
    st.caption(
        "Operational evidence from persisted agent runs: data-quality checks, evaluation "
        "coverage, workflow health, and Mem0 retrieval and delivery activity."
    )
    report = load("/quality-evals")
    quality_tab, eval_tab, orchestration_tab, observability_tab, memory_tab = st.tabs(
        [
            "Data quality agent",
            "Evaluation results",
            "Orchestration status",
            "Observability",
            "Mem0 memory",
        ]
    )

    with quality_tab:
        quality = report["data_quality"]
        q1, q2, q3, q4 = st.columns(4)
        q1.metric("Quality runs", quality["total_runs"])
        q2.metric(
            "Average quality",
            f"{float(quality.get('average_quality_pct') or 0):.1f}%",
        )
        q3.metric(
            "Technical quality",
            f"{float(quality.get('average_technical_quality_pct') or 0):.1f}%",
        )
        q4.metric("Technically passed", quality["technical_passed_runs"])
        st.caption(f"Last quality run: {refresh_label(quality.get('last_run_at'))}")

        checks = pd.DataFrame(quality["checks"])
        st.subheader("Check pass rates")
        if checks.empty:
            st.info("No data-quality checks have been recorded yet.")
        else:
            checks["check_name"] = checks["check_name"].str.replace("_", " ").str.title()
            checks = checks.rename(
                columns={
                    "check_name": "Check",
                    "run_count": "Runs",
                    "passed_count": "Passed",
                    "pass_rate_pct": "Pass rate %",
                }
            )
            st.dataframe(
                checks,
                hide_index=True,
                width="stretch",
                column_config={
                    "Pass rate %": st.column_config.ProgressColumn(
                        min_value=0, max_value=100, format="%.1f%%"
                    )
                },
            )

        st.subheader("Recent quality results")
        recent = pd.DataFrame(quality["recent_runs"])
        if recent.empty:
            st.info("No recent quality results are available.")
        else:
            recent["failed_checks"] = recent["failed_checks"].apply(
                lambda values: ", ".join(values) if values else "None"
            )
            recent["planning_simulation"] = recent["planning_simulation"].map(
                {True: "Planning simulation", False: "Source data"}
            )
            recent = recent.rename(
                columns={
                    "company_name": "Customer",
                    "ticker": "Ticker",
                    "status": "Case status",
                    "planning_simulation": "Signal",
                    "quality_score_pct": "Quality %",
                    "technical_quality_score_pct": "Technical %",
                    "production_eligible": "Production eligible",
                    "failed_checks": "Failed checks",
                    "created_at": "Run time",
                }
            )
            st.dataframe(
                recent[
                    [
                        "Customer",
                        "Ticker",
                        "Signal",
                        "Quality %",
                        "Technical %",
                        "Production eligible",
                        "Failed checks",
                        "Run time",
                    ]
                ],
                hide_index=True,
                width="stretch",
            )

    with eval_tab:
        evaluation = report["evaluation"]
        scorecard = pd.DataFrame(
            build_eval_scorecard(report, api_connectivity_passed=True)
        )
        st.subheader("Evaluation benchmark scorecard")
        st.caption(
            "Measured accuracy is compared with an explicit production benchmark. "
            "Safety contracts require 100%; predictive precision uses the agreed 80% target."
        )
        passed = int((scorecard["status"] == "PASS").sum())
        failed = int((scorecard["status"] == "FAIL").sum())
        pending = int(scorecard["status"].isin(["PENDING", "NOT EVALUATED"]).sum())
        s1, s2, s3, s4 = st.columns(4)
        s1.metric("Evals defined", len(scorecard))
        s2.metric("Benchmarks met", passed)
        s3.metric("Below benchmark", failed)
        s4.metric("Pending / not evaluated", pending)
        display_scorecard = scorecard.rename(
            columns={
                "eval": "Evaluation",
                "category": "Category",
                "measured_pct": "Measured accuracy %",
                "benchmark_pct": "Benchmark %",
                "samples": "Samples",
                "status": "Status",
                "definition": "Benchmark definition",
            }
        )
        st.dataframe(
            display_scorecard,
            hide_index=True,
            width="stretch",
            column_config={
                "Measured accuracy %": st.column_config.NumberColumn(format="%.2f%%"),
                "Benchmark %": st.column_config.NumberColumn(format="%.1f%%"),
            },
        )
        with st.expander("View every deterministic eval check"):
            detailed = pd.DataFrame(detailed_contract_checks(evaluation))
            if detailed.empty:
                st.info("No deterministic contract checks are available.")
            else:
                st.dataframe(
                    detailed.rename(
                        columns={"suite": "Suite", "check": "Check", "result": "Result"}
                    ),
                    hide_index=True,
                    width="stretch",
                )

        st.subheader("UI-to-API connectivity")
        st.success(
            "PASS · CapacityPilot API is reachable and its PostgreSQL health check passed."
        )
        precision = evaluation.get("precision_pct")
        e1, e2, e3, e4 = st.columns(4)
        e1.metric("Evaluation runs", evaluation["evaluation_runs"])
        e2.metric(
            "Average evidence coverage",
            f"{float(evaluation.get('average_evidence_coverage_pct') or 0):.1f}%",
        )
        e3.metric("Full-coverage runs", evaluation["full_coverage_runs"])
        e4.metric("Labeled outcomes", evaluation["labeled"])
        st.subheader("Prediction precision")
        if precision is None:
            st.info(
                "Precision is not available yet. Record actual expansion outcomes to compare "
                "predictions with observed results."
            )
        else:
            p1, p2, p3 = st.columns(3)
            p1.metric("Precision", f"{float(precision):.1f}%")
            p2.metric("Target", f"{evaluation['precision_target_pct']}%")
            p3.metric(
                "Target status",
                "Met" if evaluation["precision_target_met"] else "Below target",
            )
        missing = pd.DataFrame(evaluation["missing_specialists"])
        st.subheader("Missing specialist coverage")
        if missing.empty:
            st.success("All recorded evaluation runs contain full specialist coverage.")
        else:
            missing["missing_specialist"] = (
                missing["missing_specialist"].str.replace("_", " ").str.title()
            )
            st.dataframe(
                missing.rename(
                    columns={
                        "missing_specialist": "Missing specialist",
                        "missing_count": "Affected runs",
                    }
                ),
                hide_index=True,
                width="stretch",
            )

        chatbot_contract = evaluation.get("chatbot_contract")
        if chatbot_contract:
            st.subheader("Chatbot grounding checks")
            b1, b2, b3 = st.columns(3)
            b1.metric("Grounding status", chatbot_contract["status"])
            b2.metric("Checks passed", chatbot_contract["passed_checks"])
            b3.metric("Checks executed", chatbot_contract["total_checks"])
            if chatbot_contract["status"] == "PASS":
                st.success(
                    "PASS · Reservation questions route to audited reservation records and "
                    "cannot silently become an unrelated customer listing."
                )
            else:
                st.error(
                    "FAIL · Chatbot grounding checks failed: "
                    + ", ".join(chatbot_contract["failed_checks"])
                )

        ui_action_contract = evaluation.get("ui_action_contract")
        st.subheader("Navigation and action guardrails")
        if ui_action_contract:
            a1, a2, a3 = st.columns(3)
            a1.metric("UI action contract", ui_action_contract["status"])
            a2.metric("Checks passed", ui_action_contract["passed_checks"])
            a3.metric("Checks executed", ui_action_contract["total_checks"])
            if ui_action_contract["status"] == "PASS":
                st.success(
                    "PASS · All application pages are configured, source-data reservations "
                    "with sufficient capacity are enabled, and simulation or shortfall "
                    "reservations are intentionally blocked with an explanation."
                )
            else:
                st.error(
                    "FAIL · Navigation or action-state checks failed: "
                    + ", ".join(ui_action_contract["failed_checks"])
                )
        else:
            st.warning("UI action-contract eval is unavailable. Restart the CapacityPilot API.")

        st.subheader("Mandatory Jira ticket-link check")
        link_contract = evaluation.get("ui_link_contract")
        if link_contract:
            c1, c2, c3 = st.columns(3)
            c1.metric("UI link contract", link_contract["status"])
            c2.metric("Checks passed", link_contract["passed_checks"])
            c3.metric("Checks executed", link_contract["total_checks"])
            if link_contract["status"] == "PASS":
                st.success(
                    "PASS · Streamlit link helpers import correctly and safely construct "
                    "SEC/news and Jira URLs."
                )
            else:
                st.error(
                    "FAIL · Link helper checks failed: "
                    + ", ".join(link_contract["failed_checks"])
                )
        else:
            st.warning("UI link-contract eval is unavailable. Restart the CapacityPilot API.")

        jira_quality = evaluation.get("jira_handoff")
        if not jira_quality:
            st.info("Restart the CapacityPilot API to load Jira integration checks.")
        else:
            j1, j2, j3, j4 = st.columns(4)
            j1.metric("Jira requests", jira_quality["total_requests"])
            j2.metric("Completed", jira_quality["completed_requests"])
            j3.metric("Valid ticket links", jira_quality["completed_with_valid_link"])
            j4.metric(
                "Invalid or failed",
                jira_quality["invalid_completed_links"] + jira_quality["failed_requests"],
            )
            check_status = jira_quality["mandatory_check_status"]
            rule = (
                "Every completed Jira request must have a valid HTTPS ticket URL whose "
                "project and issue key match the request."
            )
            if check_status == "PASS":
                st.success(f"PASS · {rule}")
            elif check_status == "PENDING":
                st.warning(f"PENDING · {rule}")
            elif check_status == "FAIL":
                st.error(f"FAIL · {rule}")
            else:
                st.info(f"NOT EVALUATED · {rule}")

            invalid_jira = pd.DataFrame(jira_quality["invalid_requests"])
            if not invalid_jira.empty:
                invalid_jira = invalid_jira.rename(
                    columns={
                        "request_type": "Route",
                        "project_key": "Project",
                        "status": "Status",
                        "jira_issue_key": "Jira key",
                        "jira_issue_url": "Stored URL",
                        "last_error": "Last error",
                        "updated_at": "Last updated",
                    }
                )
                st.dataframe(
                    invalid_jira[
                        [
                            "Route",
                            "Project",
                            "Status",
                            "Jira key",
                            "Stored URL",
                            "Last error",
                            "Last updated",
                        ]
                    ],
                    hide_index=True,
                    width="stretch",
                )

    with orchestration_tab:
        orchestration = report["orchestration"]
        active = (
            orchestration["queued_cases"]
            + orchestration["running_cases"]
            + orchestration["retry_cases"]
        )
        o1, o2, o3, o4 = st.columns(4)
        o1.metric("Total cases", orchestration["total_cases"])
        o2.metric("Completed", orchestration["completed_cases"])
        o3.metric("Active", active)
        o4.metric("Failed", orchestration["failed_cases"])
        st.caption(
            f"Average attempts: {float(orchestration.get('average_attempts') or 0):.2f} · "
            f"Average duration: "
            f"{float(orchestration.get('average_duration_seconds') or 0):.1f} seconds · "
            f"Last activity: {refresh_label(orchestration.get('last_activity_at'))}"
        )
        status_frame = pd.DataFrame(
            {
                "Status": ["Completed", "Queued", "Running", "Retry", "Failed"],
                "Cases": [
                    orchestration["completed_cases"],
                    orchestration["queued_cases"],
                    orchestration["running_cases"],
                    orchestration["retry_cases"],
                    orchestration["failed_cases"],
                ],
            }
        )
        st.subheader("Case status distribution")
        st.bar_chart(status_frame.set_index("Status"), horizontal=True)

        nodes = pd.DataFrame(orchestration["node_counts"])
        st.subheader("Specialist execution coverage")
        if nodes.empty:
            st.info("No specialist execution events have been recorded.")
        else:
            nodes["event_type"] = nodes["event_type"].str.replace("_", " ").str.title()
            st.dataframe(
                nodes.rename(
                    columns={
                        "event_type": "Agent step",
                        "case_count": "Cases executed",
                        "last_event_at": "Last execution",
                    }
                ),
                hide_index=True,
                width="stretch",
            )

        failures = pd.DataFrame(orchestration["recent_failures"])
        st.subheader("Recent terminal failures")
        if failures.empty:
            st.success("No terminal orchestration failures are recorded.")
        else:
            failures = failures.rename(
                columns={
                    "company_name": "Customer",
                    "ticker": "Ticker",
                    "attempt_count": "Attempts",
                    "last_error": "Last error",
                    "updated_at": "Failed at",
                }
            )
            st.dataframe(
                failures[["Customer", "Ticker", "Attempts", "Last error", "Failed at"]],
                hide_index=True,
                width="stretch",
            )

    with observability_tab:
        observability = report["observability"]
        st.subheader("Workflow activity — last 24 hours")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Cases started", observability["cases_started_24h"])
        m2.metric("Cases completed", observability["cases_completed_24h"])
        m3.metric("Cases failed", observability["cases_failed_24h"])
        m4.metric("Retried cases — all time", observability["retried_cases"])

        latency1, latency2, queue1, queue2 = st.columns(4)
        latency1.metric(
            "Average case turnaround",
            f"{float(observability.get('average_terminal_latency_seconds') or 0):.1f}s",
        )
        latency2.metric(
            "95th percentile turnaround",
            f"{float(observability.get('p95_terminal_latency_seconds') or 0):.1f}s",
        )
        queue_age = observability.get("oldest_queue_age_seconds")
        queue1.metric(
            "Oldest queued case",
            f"{float(queue_age):.1f}s" if queue_age is not None else "No queue",
        )
        queue2.metric("Stale running cases", observability["stale_running_cases"])
        st.caption(
            "A running case is considered stale after "
            f"{observability['stale_threshold_minutes']} minutes without releasing its "
            "worker lock. Turnaround covers queueing and execution through terminal status."
        )

        throughput = pd.DataFrame(observability["hourly_throughput"])
        st.subheader("Hourly workflow throughput")
        if throughput.empty:
            st.info("No workflow activity was recorded in the last 24 hours.")
        else:
            throughput["activity_hour"] = pd.to_datetime(throughput["activity_hour"])
            throughput = throughput.rename(
                columns={
                    "activity_hour": "Hour",
                    "completed": "Completed",
                    "failed": "Failed",
                }
            ).set_index("Hour")
            st.line_chart(throughput[["Completed", "Failed"]])

        errors = pd.DataFrame(observability["error_types"])
        st.subheader("Recorded workflow errors")
        if errors.empty:
            st.success("No workflow errors are currently recorded.")
        else:
            st.dataframe(
                errors.rename(
                    columns={
                        "error_type": "Error type",
                        "occurrences": "Occurrences",
                        "last_seen_at": "Last seen",
                    }
                ),
                hide_index=True,
                width="stretch",
            )
        st.caption(f"Last observed activity: {refresh_label(observability.get('last_observed_at'))}")

    with memory_tab:
        memory = report["memory"]
        provider_memory = load("/memories")
        st.subheader("All memories stored in Mem0")
        provider_status = provider_memory.get("status", "UNKNOWN")
        stored_items = pd.DataFrame(provider_memory.get("items", []))
        m1, m2 = st.columns(2)
        m1.metric("Mem0 provider status", provider_status)
        m2.metric("CapacityPilot memories", provider_memory.get("count", 0))
        if provider_status == "DISABLED":
            st.info("Mem0 is disabled. Enable it to browse stored provider memories here.")
        elif provider_status == "DEGRADED":
            errors = provider_memory.get("errors", [])
            message = errors[0].get("message") if errors else "Mem0 could not be reached."
            st.error(f"Mem0 memory browser is unavailable: {message}")
        elif stored_items.empty:
            st.info("No CapacityPilot memories are currently stored in Mem0.")
        else:
            metadata = stored_items.pop("metadata").apply(pd.Series)
            stored_items = pd.concat([stored_items, metadata], axis=1)
            stored_items = stored_items.rename(
                columns={
                    "company_name": "Customer",
                    "ticker": "Ticker",
                    "memory": "Memory",
                    "event_type": "Memory type",
                    "audit_reference": "Case / audit reference",
                    "created_at": "Created",
                    "updated_at": "Updated",
                    "memory_id": "Memory ID",
                    "user_id": "Mem0 customer partition",
                }
            )
            visible_columns = [
                "Customer",
                "Ticker",
                "Memory type",
                "Memory",
                "Case / audit reference",
                "Created",
                "Updated",
                "Memory ID",
                "Mem0 customer partition",
            ]
            st.dataframe(
                stored_items[[column for column in visible_columns if column in stored_items]],
                hide_index=True,
                width="stretch",
                column_config={
                    "Memory": st.column_config.TextColumn("Memory", width="large"),
                    "Memory ID": st.column_config.TextColumn("Memory ID", width="medium"),
                },
            )
            if provider_memory.get("truncated"):
                st.warning(
                    "The Mem0 browser reached its 10,000-record safety limit. Narrow or "
                    "archive provider memories before reviewing the remainder."
                )
        st.caption(
            "Only memories written by CapacityPilot are displayed. Other Mem0 projects and "
            "users are excluded. This view is read-only."
        )

        st.subheader("Memory retrieved during investigations")
        r1, r2, r3, r4 = st.columns(4)
        r1.metric("Mem0 searches", memory["searches"])
        hit_rate = memory.get("search_hit_rate_pct")
        r2.metric(
            "Search hit rate",
            f"{float(hit_rate):.1f}%" if hit_rate is not None else "Not available",
        )
        r3.metric("Memories returned", memory["memories_returned"])
        r4.metric("Degraded searches", memory["degraded_searches"])
        st.caption(
            "A hit means Mem0 returned at least one prior planner decision or validated "
            f"outcome. Last search: {refresh_label(memory.get('last_search_at'))}"
        )

        search_status = pd.DataFrame(memory["search_statuses"])
        if not search_status.empty:
            st.dataframe(
                search_status.rename(
                    columns={
                        "status": "Provider status",
                        "search_count": "Searches",
                        "last_search_at": "Last search",
                    }
                ),
                hide_index=True,
                width="stretch",
            )

        st.subheader("Planner decisions stored in Mem0")
        d1, d2, d3, d4 = st.columns(4)
        d1.metric("Memory events", memory["delivery_events"])
        d2.metric("Delivered", memory["delivered"])
        d3.metric("Pending", memory["pending"])
        d4.metric("Failed", memory["failed"])
        delivery_rate = memory.get("delivery_success_rate_pct")
        st.caption(
            "Delivery success: "
            + (f"{float(delivery_rate):.1f}%" if delivery_rate is not None else "Not available")
            + f" · Retried events: {memory['retried']} · Delivery attempts: "
            f"{memory['delivery_attempts']} · Last delivered: "
            f"{refresh_label(memory.get('last_delivered_at'))}"
        )

        delivery_status = pd.DataFrame(memory["delivery_statuses"])
        if delivery_status.empty:
            st.info("No planner decisions have been queued for Mem0 yet.")
        else:
            st.dataframe(
                delivery_status.rename(
                    columns={
                        "status": "Delivery status",
                        "event_count": "Events",
                        "last_updated_at": "Last updated",
                    }
                ),
                hide_index=True,
                width="stretch",
            )

        delivery_errors = pd.DataFrame(memory["recent_delivery_errors"])
        st.subheader("Recent Mem0 delivery errors")
        if delivery_errors.empty:
            st.success("No Mem0 delivery errors are currently recorded.")
        else:
            delivery_errors = delivery_errors.rename(
                columns={
                    "company_name": "Customer",
                    "ticker": "Ticker",
                    "event_type": "Memory event",
                    "status": "Status",
                    "attempt_count": "Attempts",
                    "last_error": "Last error",
                    "updated_at": "Last updated",
                }
            )
            st.dataframe(
                delivery_errors[
                    [
                        "Customer",
                        "Ticker",
                        "Memory event",
                        "Status",
                        "Attempts",
                        "Last error",
                        "Last updated",
                    ]
                ],
                hide_index=True,
                width="stretch",
            )


def render_system_status(portfolio: dict) -> None:
    st.title("Agent status")
    st.caption("Read-only operational status for the autonomous backend.")
    total = int(portfolio.get("total_companies", 0))
    scored = int(portfolio.get("scored_companies", 0))
    active = int(portfolio.get("active_cases", 0))
    remaining = int(portfolio.get("remaining_companies", 0))
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Portfolio scored", f"{scored} / {total}")
    c2.metric("Remaining", remaining)
    c3.metric("Queued or processing", active)
    c4.metric("Last completed result", refresh_label(portfolio.get("last_refresh_at")))
    st.progress(scored / total if total else 0, text=f"Portfolio baseline: {scored}/{total}")
    if active:
        st.info(
            "Work is stored in PostgreSQL. The capacity worker processes it independently "
            "from Streamlit."
        )
    elif remaining:
        st.warning("Portfolio work is paused. Start capacity-worker to resume the durable queue.")
    else:
        st.success("The one-time portfolio baseline is complete.")

    st.subheader("News evidence ingestion")
    news = load("/news-ingestion/status")
    jobs = news.get("jobs", {})
    n1, n2, n3, n4 = st.columns(4)
    n1.metric("Queued", jobs.get("QUEUED", 0) + jobs.get("RETRY", 0))
    n2.metric("Processing", jobs.get("RUNNING", 0))
    n3.metric("Companies processed", news.get("processed_jobs", 0))
    n4.metric("Evidence records", news.get("evidence_records", 0))
    if st.button("Refresh status"):
        st.rerun()


api_health = require_healthy_api()
portfolio_status = load("/portfolio-investigation/status")
st.sidebar.title("CapacityPilot")
st.sidebar.caption("AI-guided storage planning")
page = st.sidebar.radio(
    "Navigate",
    list(NAVIGATION_PAGES),
)
st.sidebar.divider()
st.sidebar.caption(
    f"Portfolio: {portfolio_status.get('scored_companies', 0)} of "
    f"{portfolio_status.get('total_companies', 0)} scored"
)
st.sidebar.caption("API connectivity: PASS")
st.sidebar.caption("Autonomous agents run in the background. Planner approval controls actions.")

if page == "Planner review":
    render_review_queue(portfolio_status)
elif page == "Customer portfolio":
    render_customer_portfolio()
elif page == "Ask CapacityPilot":
    render_portfolio_chat()
elif page == "Capacity supply":
    render_capacity_inventory()
elif page == "Reservations":
    render_local_reservations()
elif page == "Jira handoffs":
    render_jira_requests()
elif page == "Slack delivery":
    render_slack_alerts()
elif page == "Investigate customer":
    render_customer_investigation()
elif page == "Quality & evals":
    render_quality_evals()
else:
    render_system_status(portfolio_status)
