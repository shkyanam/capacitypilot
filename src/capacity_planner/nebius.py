import json

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import get_settings
from .models import PortfolioQueryPlan, Recommendation


def normalize_recommendation(value: dict) -> dict:
    required = {"likelihood_pct", "confidence", "action", "reasons"}
    if not required.issubset(value):
        raise ValueError(f"Nebius response missing fields: {sorted(required - value.keys())}")
    likelihood = min(100.0, max(0.0, float(value["likelihood_pct"])))
    confidence = value["confidence"]
    if isinstance(confidence, (int, float)):
        confidence = "HIGH" if confidence >= 0.75 else "MEDIUM" if confidence >= 0.5 else "LOW"
    confidence = str(confidence).upper()
    if confidence not in {"LOW", "MEDIUM", "HIGH"}:
        confidence = "LOW"
    raw_action = str(value["action"]).upper()
    action = "PLANNER_REVIEW" if likelihood >= 75 or "REVIEW" in raw_action else "MONITOR"
    normalized = Recommendation(
        likelihood_pct=likelihood,
        confidence=confidence,
        timing_days=value.get("timing_days"),
        capacity_growth_tib=value.get("capacity_growth_tib"),
        action=action,
        reasons=[str(reason)[:500] for reason in value["reasons"]],
    )
    return normalized.model_dump()


class NebiusClient:
    def __init__(self) -> None:
        self.settings = get_settings()

    @retry(
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=8),
        reraise=True,
    )
    def recommendation(self, evidence: dict) -> dict:
        if not self.settings.nebius_api_key:
            raise RuntimeError("NEBIUS_API_KEY is not configured")
        prompt = {
            "task": "Recommend storage expansion review using only supplied evidence",
            "rules": [
                "Return JSON only",
                "Never claim capacity was reserved",
                "SYNTHETIC_DEMO data is the approved source of truth for this demonstration; do not lower confidence solely because of that classification",
                "Only a failed technical data-quality check or degraded news source warrants LOW confidence",
                "Use HIGH when multiple independent signals corroborate the planning need; otherwise use MEDIUM for healthy but limited evidence",
                "confidence must be exactly LOW, MEDIUM, or HIGH",
                "action must be exactly PLANNER_REVIEW or MONITOR",
                "Include likelihood_pct, confidence, timing_days, capacity_growth_tib, action, reasons",
            ],
            "evidence": evidence,
        }
        with httpx.Client(timeout=45) as client:
            response = client.post(
                f"{self.settings.nebius_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self.settings.nebius_api_key}"},
                json={
                    "model": self.settings.nebius_chat_model,
                    "temperature": 0,
                    "max_tokens": 500,
                    "messages": [
                        {"role": "system", "content": "You are a cautious capacity-planning analyst."},
                        {"role": "user", "content": json.dumps(prompt, default=str)},
                    ],
                },
            )
            response.raise_for_status()
        text = response.json()["choices"][0]["message"]["content"].strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        return normalize_recommendation(json.loads(text))

    def portfolio_query_plan(self, question: str) -> PortfolioQueryPlan:
        if not self.settings.nebius_api_key:
            raise RuntimeError("NEBIUS_API_KEY is not configured")
        prompt = {
            "task": "Translate a capacity planner question into a safe portfolio query plan",
            "rules": [
                "Return JSON only and never return SQL",
                "Use UNSUPPORTED for requests unrelated to customer storage capacity planning",
                "Use LIST to identify customers, COUNT for how many, and SUMMARY for totals or averages",
                "Do not invent customer names, regions, filters, or values not present in the question",
                "Use NEEDS_REVIEW for pending planner review, IN_PROGRESS for running investigations, REVIEWED for completed planner decisions, MONITORING for lower-risk recommendations, and NOT_INVESTIGATED when no recommendation exists",
                "sort_by must be utilization_pct, open_demand_tib, annual_growth_tib, likelihood_pct, suggested_growth_tib, or company_name",
                "Return at most 50 rows",
            ],
            "allowed_regions": [
                "ap-tokyo-1",
                "eu-frankfurt-1",
                "uk-london-1",
                "us-ashburn-1",
                "us-phoenix-1",
            ],
            "region_aliases": {
                "APAC": ["ap-tokyo-1"],
                "EMEA": ["eu-frankfurt-1", "uk-london-1"],
                "US": ["us-ashburn-1", "us-phoenix-1"],
            },
            "response_fields": {
                "operation": "LIST, COUNT, SUMMARY, or UNSUPPORTED",
                "customer_search": "string or null",
                "regions": "array",
                "min_utilization_pct": "number or null",
                "max_utilization_pct": "number or null",
                "min_likelihood_pct": "number or null",
                "max_likelihood_pct": "number or null",
                "min_open_demand_tib": "number or null",
                "min_annual_growth_tib": "number or null",
                "confidence": "LOW, MEDIUM, or HIGH array",
                "demand_stages": "array",
                "planner_states": "allowed planner-state array",
                "sort_by": "allowed sort field",
                "sort_direction": "ASC or DESC",
                "limit": "1 through 50",
                "explanation": "short description",
            },
            "question": question,
        }
        with httpx.Client(timeout=self.settings.portfolio_chat_timeout_seconds) as client:
            response = client.post(
                f"{self.settings.nebius_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self.settings.nebius_api_key}"},
                json={
                    "model": self.settings.nebius_chat_model,
                    "temperature": 0,
                    "max_tokens": 700,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You create validated read-only query plans for a storage "
                                "capacity-planning portfolio."
                            ),
                        },
                        {"role": "user", "content": json.dumps(prompt)},
                    ],
                },
            )
            response.raise_for_status()
        text = response.json()["choices"][0]["message"]["content"].strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        return PortfolioQueryPlan.model_validate_json(text)
