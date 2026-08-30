import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row

from .config import get_settings


@contextmanager
def connection() -> Iterator[Connection]:
    with Connection.connect(get_settings().postgres_dsn, row_factory=dict_row) as conn:
        yield conn


def migrate() -> None:
    migration_directory = Path(__file__).parents[2] / "migrations"
    migrations = sorted(migration_directory.glob("*.sql"))
    with connection() as conn:
        conn.execute(
            """create table if not exists public.capacity_planner_schema_migration (
               migration_name text primary key,
               applied_at timestamptz not null default now()
               )"""
        )
        applied = {
            row["migration_name"]
            for row in conn.execute(
                "select migration_name from public.capacity_planner_schema_migration"
            ).fetchall()
        }
        existing_application = conn.execute(
            "select to_regclass('capacity_planner.company') application_table"
        ).fetchone()["application_table"]
        if not applied and existing_application:
            for migration in migrations:
                if migration.name >= "013_generic_capacity_taxonomy.sql":
                    continue
                conn.execute(
                    """insert into public.capacity_planner_schema_migration(migration_name)
                       values (%s) on conflict do nothing""",
                    (migration.name,),
                )
                applied.add(migration.name)
        for migration in migrations:
            if migration.name in applied:
                continue
            conn.execute(migration.read_text())
            conn.execute(
                """insert into public.capacity_planner_schema_migration(migration_name)
                   values (%s)""",
                (migration.name,),
            )


def event(case_id: str, event_type: str, payload: dict[str, Any]) -> None:
    with connection() as conn:
        conn.execute(
            "insert into capacity_planner.case_event(case_id,event_type,payload) values (%s,%s,%s)",
            (case_id, event_type, json.dumps(payload, default=str)),
        )
