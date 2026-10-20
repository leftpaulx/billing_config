from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def make_pool(dsn: str) -> ConnectionPool:
    return ConnectionPool(
        dsn,
        min_size=1,
        max_size=10,
        open=True,
        kwargs={
            # Reads run autocommit; every write path opens an explicit conn.transaction().
            "autocommit": True,
            "row_factory": dict_row,
            "options": "-c timezone=UTC -c search_path=billing_config,public",
        },
    )
