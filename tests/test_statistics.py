from datetime import UTC, datetime

import pytest

from custom_components.vandebron.models import Source
from custom_components.vandebron.statistics import accumulated, daily_values


def test_supplier_daily_import_and_corrections(measurements):
    source = Source("a", "A", "account", blocks={"usage": measurements})
    values = daily_values(source, "usage", "usage", "electricity")
    assert [v for _, v in values] == [7.8, 8.1]
    assert values[0][0] == datetime(2026, 6, 1, 21, tzinfo=UTC)  # 23:00 Dutch
    records = accumulated(values, 100)
    assert records[0]["sum"] == 107.8
    assert records[1]["sum"] == pytest.approx(115.9)
    corrected = accumulated([(values[0][0], 5), values[1]], 100)
    assert corrected[-1]["sum"] == pytest.approx(113.1)  # replacement, not addition to old sum
    source.errors["usage"] = "offline"
    assert daily_values(source, "usage", "usage", "electricity") == []


def test_statistics_reject_missing_negative_and_partial(measurements):
    measurements["electricity"]["intervals"][0]["status"] = "MISSING"
    measurements["electricity"]["intervals"][1]["usage"]["value"] = "-1"
    source = Source("a", "A", "account", blocks={"usage": measurements})
    assert daily_values(source, "usage", "usage", "electricity") == []


async def test_recorder_import_metadata_and_year_baseline(hass, measurements, monkeypatch):
    from unittest.mock import AsyncMock, Mock

    import custom_components.vandebron.statistics as module
    from custom_components.vandebron.models import AMSTERDAM

    year = datetime.now(UTC).astimezone(AMSTERDAM).date().replace(month=1, day=1)
    source = Source("account_one", "One", "account", blocks={f"usage_year_{year}": measurements})
    recorder = Mock()

    async def query(func, *args):
        assert args[2] == module.day_bounds(year)[0]
        return {next(iter(args[3])): [{"sum": 100}]}

    recorder.async_add_executor_job = AsyncMock(side_effect=query)
    monkeypatch.setattr(module, "get_instance", lambda h: recorder)
    imported = []
    monkeypatch.setattr(
        module, "async_add_external_statistics", lambda h, m, records: imported.append((m, records))
    )
    await module.async_import_usage(hass, {source.key: source})
    assert len(imported) == 3
    for metadata, records in imported:
        assert metadata["unit_class"] in ("energy", "volume")
        assert metadata["source"] == "vandebron"
        assert records[0]["sum"] == 100
        assert records[-1]["sum"] > 100
