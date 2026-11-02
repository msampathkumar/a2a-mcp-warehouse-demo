"""Runtime settings, all from environment variables."""

import os
from dataclasses import dataclass, field
from pathlib import Path

# Tables in the order they are added, one per level.
TABLE_ORDER = ['orders', 'stores', 'customers', 'order_items', 'products']

APP_DIR = Path(__file__).resolve().parent.parent  # demo-app/


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'on'}


@dataclass(frozen=True)
class Settings:
    level: int = field(
        default_factory=lambda: int(os.environ.get('LEVEL', '1'))
    )
    data_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get('DATA_DIR', APP_DIR / 'data')
        )
    )
    runs_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get('RUNS_DIR', APP_DIR / 'runs')
        )
    )
    cache_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get('A2A_CACHE_DIR', '/tmp/a2a_cache')
        )
    )
    row_cap: int = field(
        default_factory=lambda: int(os.environ.get('ROW_CAP', '500'))
    )
    source_latency_ms: int = field(
        default_factory=lambda: int(os.environ.get('SOURCE_LATENCY_MS', '100'))
    )
    cache_enabled: bool = field(
        default_factory=lambda: _bool('CACHE_ENABLED', True)
    )
    cache_ttl_min: float = field(
        default_factory=lambda: float(os.environ.get('CACHE_TTL_MIN', '60'))
    )
    notes_enabled: bool = field(
        default_factory=lambda: _bool('A2A_NOTES_ENABLED', True)
    )
    model: str = field(
        default_factory=lambda: os.environ.get('MODEL', 'gemini-3.5-flash-lite')
    )
    public_url: str = field(
        default_factory=lambda: os.environ.get('PUBLIC_URL', '')
    )
    port: int = field(default_factory=lambda: int(os.environ.get('PORT', '0')))
    playbook_enabled: bool = field(default_factory=lambda: _bool('A2A_PLAYBOOK_ENABLED', True))
    max_agent_llm_calls: int = field(
        default_factory=lambda: int(os.environ.get('MAX_AGENT_LLM_CALLS', '25'))
    )
    admin_token: str = field(
        default_factory=lambda: os.environ.get('ADMIN_TOKEN', '')
    )
    public_demo: bool = field(
        default_factory=lambda: _bool('PUBLIC_DEMO', False)
    )
    max_input_chars: int = field(
        default_factory=lambda: int(os.environ.get('MAX_INPUT_CHARS', '600'))
    )
    a2a_rate_limit_per_min: int = field(
        default_factory=lambda: int(
            os.environ.get('A2A_RATE_LIMIT_PER_MIN', '10')
        )
    )
    a2a_daily_cap: int = field(
        default_factory=lambda: int(os.environ.get('A2A_DAILY_CAP', '500'))
    )
    a2a_enabled: bool = field(
        default_factory=lambda: _bool('A2A_ENABLED', True)
    )
    isolated_warehouses: bool = field(
        default_factory=lambda: _bool('ISOLATED_WAREHOUSES', True)
    )
    cloud_logging: bool = field(
        default_factory=lambda: _bool('CLOUD_LOGGING', False)
    )
    bq_telemetry_table: str = field(
        default_factory=lambda: os.environ.get('BQ_TELEMETRY_TABLE', '').strip()
    )
    bq_location: str = field(
        default_factory=lambda: os.environ.get('BQ_LOCATION', 'us-central1').strip()
    )

    def __post_init__(self) -> None:
        if not 1 <= self.level <= len(TABLE_ORDER):
            raise ValueError(
                f'LEVEL must be 1..{len(TABLE_ORDER)}, got {self.level}'
            )

    @property
    def tables(self) -> list[str]:
        return TABLE_ORDER[: self.level]

    @property
    def effective_port(self) -> int:
        return self.port or 8000 + self.level

    @property
    def base_url(self) -> str:
        return (
            self.public_url or f'http://localhost:{self.effective_port}'
        ).rstrip('/')

    @property
    def cache_active(self) -> bool:
        return self.cache_enabled and self.cache_ttl_min > 0

    def public_dict(self) -> dict:
        """Config values recorded with every run (no secrets)."""
        return {
            'level': self.level,
            'tables': self.tables,
            'row_cap': self.row_cap,
            'source_latency_ms': self.source_latency_ms,
            'cache_enabled': self.cache_enabled,
            'cache_ttl_min': self.cache_ttl_min,
            'notes_enabled': self.notes_enabled,
            'model': self.model,
            'max_agent_llm_calls': self.max_agent_llm_calls,
            'playbook_enabled': self.playbook_enabled,
            'public_demo': self.public_demo,
            'max_input_chars': self.max_input_chars,
            'a2a_rate_limit_per_min': self.a2a_rate_limit_per_min,
            'a2a_daily_cap': self.a2a_daily_cap,
            'a2a_enabled': self.a2a_enabled,
            'isolated_warehouses': self.isolated_warehouses,
            'bq_telemetry_table': self.bq_telemetry_table,
        }
