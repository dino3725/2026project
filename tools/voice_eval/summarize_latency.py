#!/usr/bin/env python3
"""Aggregate explicitly measured metrics; missing trials never become zero latency."""

import argparse
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import statistics


def summarize(reports):
    groups = defaultdict(list)
    for report in reports:
        key = (report.get('action', 'unpublished'), report.get('detector', 'unknown'))
        groups[key].append(report)
    output = []
    for (action, detector), rows in sorted(groups.items()):
        metrics = defaultdict(list)
        metrics['physical_stop_seconds'] = []
        for row in rows:
            values = {**row.get('software', {}), **row.get('metrics', {}),
                      'physical_stop_seconds': row.get('physical_stop_seconds')}
            for name, value in values.items():
                if isinstance(value, (float, int)) and math.isfinite(value):
                    metrics[name].append(value)
        output.append({'action': action, 'detector': detector, 'trials': len(rows),
                       'trials_with_issues': sum(bool(r.get('issues')) for r in rows),
                       'metrics': {name: {
                           'measured': len(values), 'missing': len(rows) - len(values),
                           'median': statistics.median(values) if values else None,
                           'p95_nearest_rank': (sorted(values)[math.ceil(.95 * len(values)) - 1]
                                                if values else None),
                           'max': max(values) if values else None,
                       } for name, values in metrics.items()}})
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reports', type=Path, nargs='+', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    reports = [json.loads(p.read_text()) for p in args.reports]
    stamps = [r.get('command_stamp_ns') for r in reports if r.get('command_stamp_ns') is not None]
    if len(stamps) != len(set(stamps)):
        parser.error('duplicate command trials; do not count the same command twice')
    result = {'percentile_method': 'nearest_rank', 'groups': summarize(reports),
              'input_sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in args.reports}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as output:
        json.dump(result, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.write('\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
