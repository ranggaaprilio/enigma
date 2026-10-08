"""Epic 05: connection pooling, PgBouncer compatibility, Redis timeouts, WebSocket DB usage."""

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool

from app.core.config import Settings
from app.db.session import engine_options
from app.services.redis import redis_client_options

API_DIR = Path(__file__).resolve().parents[1] / "app" / "api"


# --- engine / pool -----------------------------------------------------------------------


def test_engine_options_size_the_pool_from_settings():
    settings = Settings(db_pool_size=7, db_max_overflow=3, db_pool_timeout=4.5, db_pool_recycle=600)

    options = engine_options(settings)

    assert options["pool_size"] == 7
    assert options["max_overflow"] == 3
    assert options["pool_timeout"] == 4.5
    assert options["pool_recycle"] == 600
    assert options["pool_pre_ping"] is True
    assert "poolclass" not in options


def test_engine_built_from_options_uses_a_bounded_queue_pool():
    settings = Settings(db_pool_size=2, db_max_overflow=1, db_pgbouncer=False)

    engine = create_async_engine(settings.database_url, **engine_options(settings))

    assert isinstance(engine.pool, AsyncAdaptedQueuePool)
    assert engine.pool.size() == 2
    assert engine.pool._max_overflow == 1


def test_pgbouncer_mode_hands_pooling_to_pgbouncer_and_disables_prepared_statement_caches():
    settings = Settings(db_pgbouncer=True)

    options = engine_options(settings)

    # Transaction pooling: PgBouncer owns the pool, so a second app-side pool would only pin
    # server connections (SQLAlchemy's documented setup for asyncpg behind PgBouncer).
    assert options["poolclass"] is NullPool
    for key in ("pool_size", "max_overflow", "pool_timeout", "pool_recycle"):
        assert key not in options
    connect_args = options["connect_args"]
    assert connect_args["statement_cache_size"] == 0
    assert connect_args["prepared_statement_cache_size"] == 0
    # Statements prepared on one backend must never collide with another client's names when
    # PgBouncer hands the same server connection to someone else.
    name_func = connect_args["prepared_statement_name_func"]
    assert name_func() != name_func()

    engine = create_async_engine(settings.database_url, **options)
    assert isinstance(engine.pool, NullPool)


@pytest.mark.parametrize(
    "field,value",
    [("db_pool_size", 0), ("db_max_overflow", -1), ("db_pool_timeout", 0)],
)
def test_pool_settings_reject_nonsense(field, value):
    with pytest.raises(ValidationError):
        Settings(**{field: value})


# --- redis -------------------------------------------------------------------------------


def test_command_clients_get_connect_and_read_timeouts():
    settings = Settings(
        redis_socket_timeout_seconds=2.5,
        redis_connect_timeout_seconds=1.5,
        redis_health_check_interval_seconds=20,
    )

    options = redis_client_options(settings)

    assert options["socket_timeout"] == 2.5
    assert options["socket_connect_timeout"] == 1.5
    assert options["health_check_interval"] == 20
    assert options["socket_keepalive"] is True


def test_subscriber_clients_have_no_read_timeout():
    # A blocking ``pubsub.listen()`` raises TimeoutError after ``socket_timeout`` of silence,
    # so a long-lived subscription relies on keepalive instead of a read timeout.
    settings = Settings(redis_socket_timeout_seconds=2.5, redis_connect_timeout_seconds=1.5)

    options = redis_client_options(settings, subscriber=True)

    assert options["socket_timeout"] is None
    assert options["socket_connect_timeout"] == 1.5
    assert options["socket_keepalive"] is True


def test_get_redis_client_applies_the_options(monkeypatch):
    from app.services import redis as redis_module

    settings = Settings(redis_socket_timeout_seconds=2.5, redis_connect_timeout_seconds=1.5)
    monkeypatch.setattr(redis_module, "get_settings", lambda: settings)

    kwargs = redis_module.get_redis_client().connection_pool.connection_kwargs
    assert kwargs["socket_timeout"] == 2.5
    assert kwargs["socket_connect_timeout"] == 1.5

    kwargs = redis_module.get_redis_client(subscriber=True).connection_pool.connection_kwargs
    assert kwargs["socket_timeout"] is None


async def test_presence_listens_on_the_subscriber_client_and_commands_keep_their_timeout():
    """Join/heartbeat/leave must fail fast on a dead Redis; only ``listen()`` may block."""
    from app.services.presence import PresenceService

    class Asked(Exception):
        pass

    class FakeClient:
        def __init__(self) -> None:
            self.pubsubs = 0

        def pubsub(self):
            self.pubsubs += 1
            raise Asked

    commands, subscriber = FakeClient(), FakeClient()
    presence = PresenceService(commands, subscriber=subscriber)

    with pytest.raises(Asked):
        await anext(presence.subscribe("workspace:x"))

    assert (commands.pubsubs, subscriber.pubsubs) == (0, 1)
    assert presence.redis is commands


def test_arq_connections_use_the_connect_timeout():
    from app.services.jobs import arq_redis_settings

    settings = Settings(redis_url="redis://cache:6380/2", redis_connect_timeout_seconds=1.5)

    redis_settings = arq_redis_settings(settings)

    assert (redis_settings.host, redis_settings.port, redis_settings.database) == ("cache", 6380, 2)
    assert redis_settings.conn_timeout == 1.5


# --- websockets --------------------------------------------------------------------------


def _calls(node: ast.AST, attr: str) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == attr
    ]


def _opens_db_session(item: ast.withitem) -> bool:
    call = item.context_expr
    return isinstance(call, ast.Call) and getattr(call.func, "id", None) == "async_session_maker"


def _websocket_handlers() -> list[tuple[str, ast.AsyncFunctionDef]]:
    handlers = []
    for path in sorted(API_DIR.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.AsyncFunctionDef) and any(
                isinstance(d, ast.Call) and getattr(d.func, "attr", None) == "websocket"
                for d in node.decorator_list
            ):
                handlers.append((f"{path.name}:{node.name}", node))
    return handlers


def test_websocket_handlers_are_found():
    names = {name for name, _ in _websocket_handlers()}
    assert {
        "agents.py:stream_agent_run",
        "commands.py:stream_command_run",
        "notifications.py:stream_notifications",
        "terminal.py:connect_terminal_session",
        "presence.py:stream_presence",
        "workspace_services.py:stream_workspace_service_logs",
    } <= names


@pytest.mark.parametrize(
    "name,handler", _websocket_handlers(), ids=[name for name, _ in _websocket_handlers()]
)
def test_websocket_never_holds_a_db_session_while_streaming(name, handler):
    """A socket lives for minutes or hours; a session held across it pins a pooled connection.

    Auth and lookups run in a short ``async with async_session_maker()`` block that must close
    before ``websocket.accept()``; streaming code opens its own short sessions per read.
    """
    for node in ast.walk(handler):
        if isinstance(node, ast.AsyncWith) and any(_opens_db_session(i) for i in node.items):
            assert not _calls(node, "accept"), f"{name} accepts the socket inside a DB session"
    assert "Depends" not in ast.unparse(handler.args), f"{name} injects a request-scoped session"
