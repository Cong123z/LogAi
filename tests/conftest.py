import os
import uuid
from datetime import date

import pytest

from logai2.config import load_config
from logai2.db import connect, ensure_partitions, migrate

TEST_DSN = os.environ.get("LOGAI2_TEST_DSN", "postgresql://logai:logai@localhost:5433/logai_test")


@pytest.fixture
def schema():
    # Throwaway schema per test; later tasks build DSNs with options=-csearch_path=<schema>.
    return f"t_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def conn(schema):
    c = connect(TEST_DSN)
    c.execute(f"CREATE SCHEMA {schema}")
    c.execute(f"SET search_path TO {schema}")
    c.commit()
    migrate(c)
    ensure_partitions(c, date(2026, 10, 1), date(2026, 10, 31))
    yield c
    c.rollback()
    c.execute(f"DROP SCHEMA {schema} CASCADE")
    c.commit()
    c.close()


@pytest.fixture
def cfg():
    return load_config(None)
