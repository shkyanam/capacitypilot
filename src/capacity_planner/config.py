from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    postgres_dsn: str = "postgresql://localhost/capacity_planner_dev"
    nebius_api_key: str = ""
    nebius_base_url: str = "https://api.tokenfactory.nebius.com/v1"
    nebius_chat_model: str = "meta-llama/Llama-3.3-70B-Instruct"
    portfolio_chat_timeout_seconds: float = Field(default=10, ge=5, le=60)
    api_base_url: str = "http://localhost:8000"
    api_auth_token: str = "local-development-only"
    sec_user_agent: str = "CapacityPlanner/1.0 admin@example.com"
    worker_poll_seconds: float = Field(default=2, ge=0.2, le=60)
    max_agent_attempts: int = Field(default=3, ge=1, le=10)
    max_signal_age_hours: int = Field(default=48, ge=1, le=720)
    news_api_key: str = ""
    news_lookback_days: int = Field(default=180, ge=1, le=1825)
    news_cache_hours: int = Field(default=24, ge=1, le=168)
    sec_max_filings_per_case: int = Field(default=3, ge=1, le=10)
    stale_case_minutes: int = Field(default=15, ge=1, le=1440)
    sec_min_request_interval_seconds: float = Field(default=0.15, ge=0.1, le=10)
    news_bulk_company_interval_seconds: float = Field(default=1, ge=0.1, le=300)
    news_bulk_max_attempts: int = Field(default=3, ge=1, le=10)
    news_bulk_refresh_hours: int = Field(default=24, ge=1, le=720)
    mem0_enabled: bool = False
    mem0_api_key: str = ""
    mem0_agent_id: str = "capacity-planner"
    mem0_search_top_k: int = Field(default=5, ge=1, le=20)
    mem0_max_attempts: int = Field(default=3, ge=1, le=10)
    capacity_inventory_max_age_hours: int = Field(default=24, ge=1, le=720)
    jira_enabled: bool = False
    jira_base_url: str = ""
    jira_auth_mode: str = "basic"
    jira_user_email: str = ""
    jira_api_token: str = ""
    jira_bearer_token: str = ""
    jira_capacity_project_key: str = "CAP"
    jira_capacity_issue_type: str = "Task"
    jira_hub_project_key: str = "HUB"
    jira_hub_issue_type: str = "Task"
    jira_default_labels: str = "capacity-planner,storage-expansion"
    jira_timeout_seconds: float = Field(default=30, ge=1, le=120)
    jira_verify_ssl: bool = True
    jira_max_attempts: int = Field(default=5, ge=1, le=10)
    slack_enabled: bool = False
    slack_auth_mode: str = "webhook"
    slack_webhook_url: str = ""
    slack_bot_token: str = ""
    slack_channel_id: str = ""
    slack_planner_url: str = "http://localhost:8501"
    slack_digest_interval_minutes: int = Field(default=15, ge=1, le=1440)
    slack_max_attempts: int = Field(default=5, ge=1, le=10)
    slack_timeout_seconds: float = Field(default=30, ge=1, le=120)
    slack_verify_ssl: bool = True
    slack_require_production_eligible: bool = True
    slack_include_test_scenarios: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
