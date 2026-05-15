from functools import lru_cache
from typing import Any, Optional

from pyairtable import Api
from pyairtable.formulas import match

from config import get_settings


@lru_cache
def _api() -> Api:
    return Api(get_settings().airtable_token)


def _table(name: str):
    s = get_settings()
    if not s.airtable_token or not s.airtable_base_id:
        raise RuntimeError("AIRTABLE_TOKEN и AIRTABLE_BASE_ID должны быть заданы")
    return _api().table(s.airtable_base_id, name)


def leads_table():
    return _table(get_settings().airtable_leads_table)


def listings_table():
    return _table(get_settings().airtable_listings_table)


def want_to_buy_table():
    return _table(get_settings().airtable_want_to_buy_table)


def create_record(table, fields: dict[str, Any]) -> dict:
    clean = {k: v for k, v in fields.items() if v is not None}
    return table.create(clean)


def list_records(
    table,
    *,
    filters: Optional[dict[str, Any]] = None,
    formula: Optional[str] = None,
    max_records: int = 100,
    sort: Optional[list[str]] = None,
) -> list[dict]:
    kwargs: dict[str, Any] = {"max_records": max_records}
    if formula:
        kwargs["formula"] = formula
    elif filters:
        kwargs["formula"] = match(filters)
    if sort:
        kwargs["sort"] = sort
    return table.all(**kwargs)


def get_record(table, record_id: str) -> dict:
    return table.get(record_id)
