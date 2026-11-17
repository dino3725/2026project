"""Summarize equally weighted trial means without inventing missing stages."""

import argparse
import json
from pathlib import Path
import statistics


def summarize(events):
    """Separate starting HOME and return HOME, with counts for every stage."""
    labels = ['HOME_START', 'RECOGNITION', 'PREGRASP', 'PICK',
              'RETREAT', 'CARGO', 'HOME_END', 'HOME_RECOVERY']
    grouped = {key: [] for key in labels}
    homes = set()
    for row in events:
        if row['kind'] != 'pose':
            continue
        label = row['label']
        if label == 'HOME':
            label = 'HOME_END' if row['trial'] in homes else 'HOME_START'
            homes.add(row['trial'])
        grouped.setdefault(label, []).append(row)
    result = dict(
        attempted_trials=sorted({r['trial'] for r in events if r['kind'] == 'started'}),
        completed_trials=sorted({r['trial'] for r in events if r['kind'] == 'finished'}),
        failures=[r for r in events if r['kind'] in ('failed', 'cancelled')],
        poses={})
    for label, rows in grouped.items():
        result['poses'][label] = dict(count=len(rows), trials=[r['trial'] for r in rows])
        if not rows:
            continue
        result['poses'][label].update(
            mean_signed_error_deg=[statistics.mean(r['mean_error_deg'][i] for r in rows)
                                   for i in range(4)],
            mean_absolute_error_deg=[statistics.mean(
                r['mean_absolute_error_deg'][i] for r in rows) for i in range(4)],
            sample_std_of_trial_means_deg=[statistics.stdev(
                r['mean_error_deg'][i] for r in rows) if len(rows) > 1 else None
                for i in range(4)],
            max_absolute_error_deg=[max(r['max_absolute_error_deg'][i] for r in rows)
                                    for i in range(4)],
            nominal_passes=sum(r['nominal_pass'] for r in rows),
            tool_error_mean_mm=statistics.mean(
                statistics.mean(r['tool_error_mm']) for r in rows)
            if all(r.get('tool_error_mm') for r in rows) else None)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('events', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.events.read_text().splitlines()]
    result = summarize(rows)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
