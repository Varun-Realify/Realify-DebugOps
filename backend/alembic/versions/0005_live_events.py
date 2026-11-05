"""live events: NOTIFY triggers feeding the WebSocket stream

Revision ID: 0005
Revises: 0004
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

FUNCS = {
    "debugops_notify_event": """json_build_object('kind', 'event', 'id', NEW.id, 'run_id', NEW.run_id, 'level', NEW.level,
        'type', NEW.type, 'source', NEW.source, 'message', left(NEW.message, 1500), 'ts', NEW.ts,
        'scan_id', NEW.data->>'scan_id')""",
    "debugops_notify_run": """json_build_object('kind', 'run', 'id', NEW.id, 'bug_id', NEW.bug_id, 'status', NEW.status,
        'current_node', NEW.current_node, 'attempt', NEW.attempt)""",
    "debugops_notify_scan": """json_build_object('kind', 'scan', 'id', NEW.id, 'repository_id', NEW.repository_id,
        'status', NEW.status)""",
}
TRIGGERS = [
    ("activity_events_notify", "activity_events", "AFTER INSERT", "debugops_notify_event"),
    ("workflow_runs_notify", "workflow_runs", "AFTER INSERT OR UPDATE", "debugops_notify_run"),
    ("scan_runs_notify", "scan_runs", "AFTER INSERT OR UPDATE", "debugops_notify_scan"),
]


def upgrade() -> None:
    for fn, payload in FUNCS.items():
        op.execute(f"""
            CREATE FUNCTION {fn}() RETURNS trigger AS $$
            BEGIN
                PERFORM pg_notify('debugops_events', {payload}::text);
                RETURN NEW;
            END; $$ LANGUAGE plpgsql
        """)
    for name, table, when, fn in TRIGGERS:
        op.execute(f"CREATE TRIGGER {name} {when} ON {table} FOR EACH ROW EXECUTE FUNCTION {fn}()")


def downgrade() -> None:
    for name, table, _, _ in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name} ON {table}")
    for fn in FUNCS:
        op.execute(f"DROP FUNCTION IF EXISTS {fn}()")
