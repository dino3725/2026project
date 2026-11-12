#!/usr/bin/env python3
"""Join one command trial; optionally add human annotations from one video timeline."""

import argparse
import hashlib
import json
from pathlib import Path

from latency_analysis import summarize_trial, video_measurement


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--voice-log', type=Path, required=True)
    parser.add_argument('--laptop', type=Path)
    parser.add_argument('--jetson', type=Path)
    parser.add_argument('--video-markers', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    traces = [json.loads(line.split('[LATENCY] ', 1)[1])
              for line in args.voice_log.read_text().splitlines() if '[LATENCY] ' in line]

    def read(path):
        return json.loads(path.read_text()) if path else None

    result = summarize_trial(traces, read(args.laptop), read(args.jetson))
    evidence = [args.voice_log] + [p for p in (args.laptop, args.jetson, args.video_markers) if p]
    if args.video_markers:
        markers = read(args.video_markers)
        if result.get('action') != 'stop':
            raise ValueError('physical stop annotation requires a published stop trial')
        video = args.video_markers.parent / markers['video_file']
        evidence.append(video)
        result.update(video_measurement(markers, result.get('command_stamp_ns')))
    result['evidence_sha256'] = {
        str(p): file_sha256(p) for p in evidence
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as output:
        json.dump(result, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.write('\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
