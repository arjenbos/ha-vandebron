"""Import dated supplier day totals into the Energy Dashboard.

Day totals are assigned to the final hour of their Dutch delivery day. This
preserves daily/monthly totals without inventing an hourly consumption profile.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import slugify

from .const import DOMAIN
from .models import (
    AMSTERDAM,
    UTC,
    Source,
    day_bounds,
    items,
    legacy_usage,
    number,
    timestamp,
)


def daily_values(source: Source, block: str, field: str, fuel: str) -> list[tuple[datetime, float]]:
    data = source.blocks.get(block)
    if not isinstance(data, dict) or block in source.errors:
        return []
    if source.kind == "connection":
        rows = legacy_usage(data)["intervals"]
    else:
        group = data.get(fuel, {})
        rows = (
            items(group, "intervals")
            if isinstance(group, dict) and group.get("kind") != "error"
            else []
        )
    result = {}
    for row in rows:
        if row.get("status") not in (None, "AVAILABLE"):
            continue
        when = timestamp(row.get("timestamp"))
        value = number(row.get(field))
        if when is None or value is None or value < 0:
            continue
        _, end = day_bounds(when.astimezone(AMSTERDAM).date())
        # Exclude today's partial total; it will be imported once the day closes.
        if end > datetime.now(UTC):
            continue
        result[end - timedelta(hours=1)] = value
    return sorted(result.items())


def accumulated(values: list[tuple[datetime, float]], baseline: float) -> list[StatisticData]:
    total = baseline
    records: list[StatisticData] = []
    for start, value in values:
        total += value
        records.append(StatisticData(start=start, state=total, sum=total))
    return records


async def async_import_usage(hass: HomeAssistant, sources: dict[str, Source]) -> None:
    now = datetime.now(UTC)
    year = now.astimezone(AMSTERDAM).date().replace(month=1, day=1)
    block = f"usage_year_{year}"
    for source in sources.values():
        if source.kind not in ("account", "connection") or "discovery" in source.errors:
            continue
        fuels = (
            ("electricity", "gas")
            if source.kind == "account"
            else (("gas",) if source.segment == "Gas" else ("electricity",))
        )
        for fuel in fuels:
            for field in ("usage",) if fuel == "gas" else ("usage", "redelivery"):
                values = daily_values(source, block, field, fuel)
                if not values:
                    continue
                stat_id = f"{DOMAIN}:{slugify(source.key)}_{fuel}_{field}"
                # Rebuild the entire fetched year so supplier corrections replace,
                # rather than double-count, historical daily values. Carry the sum
                # from BEFORE this year to survive restarts and year transitions.
                start, _ = day_bounds(year)
                previous = await get_instance(hass).async_add_executor_job(
                    statistics_during_period,
                    hass,
                    datetime(1970, 1, 1, tzinfo=UTC),
                    start,
                    {stat_id},
                    "hour",
                    None,
                    {"sum"},
                )
                old = previous.get(stat_id, [])
                baseline = old[-1]["sum"] if old and old[-1].get("sum") is not None else 0.0
                metadata = StatisticMetaData(
                    mean_type=StatisticMeanType.NONE,
                    has_sum=True,
                    name=(
                        f"Vandebron {source.name} "
                        + (
                            "Gas consumption"
                            if fuel == "gas"
                            else "Electricity consumption"
                            if field == "usage"
                            else "Electricity returned to grid"
                        )
                        + f" ({source.kind})"
                    ),
                    source=DOMAIN,
                    statistic_id=stat_id,
                    unit_class="volume" if fuel == "gas" else "energy",
                    unit_of_measurement="m³" if fuel == "gas" else "kWh",
                )
                # An explicit preceding baseline ensures the first imported day
                # contributes its full amount to statistics differences.
                records = [
                    StatisticData(start=start - timedelta(hours=1), state=baseline, sum=baseline)
                ]
                records.extend(accumulated(values, baseline))
                async_add_external_statistics(hass, metadata, records)
