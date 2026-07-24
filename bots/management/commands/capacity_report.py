import json
import math
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from bots.models import BotResourceSnapshot


def nearest_rank_percentile(values, percentile):
    """Return the nearest-rank percentile for a non-empty numeric sequence."""
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil((percentile / 100) * len(ordered)) - 1)
    return ordered[index]


def summarize_metric(rows, key, percentiles):
    values = []
    for _, data in rows:
        if not isinstance(data, dict):
            continue
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            values.append(value)

    summary = {"sample_count": len(values)}
    for percentile in percentiles:
        summary[f"p{percentile}"] = nearest_rank_percentile(values, percentile)
    summary["max"] = max(values) if values else None
    return summary


def iter_snapshot_rows(cutoff):
    return BotResourceSnapshot.objects.filter(created_at__gte=cutoff).values_list("bot_id", "data").iterator(chunk_size=2_000)


def build_capacity_report(*, days, now=None):
    now = now or timezone.now()
    rows = list(iter_snapshot_rows(now - timedelta(days=days)))
    bot_ids = {bot_id for bot_id, _ in rows}

    return {
        "window_days": days,
        "snapshot_count": len(rows),
        "sampled_bot_count": len(bot_ids),
        "metrics": {
            "ram_usage_megabytes": summarize_metric(
                rows,
                "ram_usage_megabytes",
                (50, 95),
            ),
            "cpu_usage_millicores": summarize_metric(
                rows,
                "cpu_usage_millicores",
                (50, 95),
            ),
            "db_connection_count": summarize_metric(
                rows,
                "db_connection_count",
                (95,),
            ),
            "redis_connection_count": summarize_metric(
                rows,
                "redis_connection_count",
                (95,),
            ),
        },
    }


def human_value(value):
    return "n/a" if value is None else str(value)


class Command(BaseCommand):
    help = "Report read-only capacity percentiles from existing bot resource snapshots."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=30,
            help="Lookback window in days (default: 30).",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            dest="as_json",
            help="Emit machine-readable JSON.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        if days <= 0:
            raise CommandError("--days must be greater than zero.")

        report = build_capacity_report(days=days)
        if options["as_json"]:
            self.stdout.write(json.dumps(report, sort_keys=True))
            return

        self.stdout.write(f"Attendee capacity report (last {days} day(s))")
        self.stdout.write(f"snapshots={report['snapshot_count']}")
        self.stdout.write(f"sampled_bots={report['sampled_bot_count']}")

        ram = report["metrics"]["ram_usage_megabytes"]
        self.stdout.write(f"ram_usage_megabytes: samples={ram['sample_count']} p50={human_value(ram['p50'])} p95={human_value(ram['p95'])} max={human_value(ram['max'])}")

        cpu = report["metrics"]["cpu_usage_millicores"]
        self.stdout.write(f"cpu_usage_millicores: samples={cpu['sample_count']} p50={human_value(cpu['p50'])} p95={human_value(cpu['p95'])} max={human_value(cpu['max'])}")

        db_connections = report["metrics"]["db_connection_count"]
        self.stdout.write(f"db_connection_count: samples={db_connections['sample_count']} p95={human_value(db_connections['p95'])} max={human_value(db_connections['max'])}")

        redis_connections = report["metrics"]["redis_connection_count"]
        self.stdout.write(f"redis_connection_count: samples={redis_connections['sample_count']} p95={human_value(redis_connections['p95'])} max={human_value(redis_connections['max'])}")
