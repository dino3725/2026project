#!/usr/bin/env python3
"""
Measure voice-pipeline baseline numbers that do not need live speech.

A. First-command delay: unload the Ollama model, then time the first
   classification (what happens after keep_alive expires).
B. Per-utterance STT + LLM time on existing recorded WAV files, plus the
   transcript/command of each file for later manual labelling.
C. Re-listen gap: time to reopen the microphone and discard the warm-up
   audio, as the original pipeline did after every command.

Nothing is published to ROS. Reproduces the original 120 s / 10m LLM settings;
use measure_llm_readiness.py to measure the current asynchronous warmup path.

    PYTHONPATH=src/voice_command_pkg .venv/bin/python tools/voice_eval/measure_baseline.py
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime
import json
import platform
import statistics
import subprocess
import time
from pathlib import Path
import urllib.request
import wave

from voice_command_pkg.audio_capture import read_frame, start_capture, stop_capture
from voice_command_pkg.audio_capture import get_default_source
from voice_command_pkg.command_classifier import classify_command_with_metrics
from voice_command_pkg.parameters import build_pipeline_config
from voice_command_pkg.pipeline import load_whisper_model, transcribe_audio


ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = Path(__file__).resolve().parent / 'results'
COLD_TEXT = '목마른데 마실 거 가져다줘'


def summary(values: list[float]) -> str:
    if not values:
        return '-'
    ordered = sorted(values)
    p90 = ordered[min(len(ordered) - 1, round(0.9 * (len(ordered) - 1)))]
    return (
        f'중앙값 {statistics.median(ordered):.2f} s, 평균 {statistics.mean(ordered):.2f} s, '
        f'최소 {ordered[0]:.2f} s, p90 {p90:.2f} s, 최대 {ordered[-1]:.2f} s (n={len(ordered)})'
    )


def ollama_post(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.load(response)


def loaded_models(base_url: str) -> list[str]:
    with urllib.request.urlopen(base_url + '/api/ps', timeout=10) as response:
        return [m.get('name', '') for m in json.load(response).get('models', [])]


def unload_model(base_url: str, model: str) -> None:
    ollama_post(base_url + '/api/generate', {'model': model, 'keep_alive': 0})
    deadline = time.monotonic() + 30
    while model in loaded_models(base_url):
        if time.monotonic() > deadline:
            raise RuntimeError(f'Ollama did not unload {model}')
        time.sleep(0.5)


def measure_cold_start(config, repeats: int) -> list[dict]:
    base = config.classifier.ollama_url.rstrip('/')
    rows = []
    for index in range(1, repeats + 1):
        unload_model(base, config.classifier.model)
        result, metrics = classify_command_with_metrics(COLD_TEXT, config.classifier)
        _, warm = classify_command_with_metrics(COLD_TEXT, config.classifier)
        rows.append({
            'run': index,
            'cold_wall_seconds': metrics['wall_seconds'],
            'cold_load_seconds': metrics['load_seconds'],
            'cold_prompt_eval_seconds': metrics['prompt_eval_seconds'],
            'warm_wall_seconds': warm['wall_seconds'],
            'command': result,
        })
        print(
            f"[A] run {index}: cold={metrics['wall_seconds']:.2f}s "
            f"(load {metrics['load_seconds']:.2f}s, "
            f"prompt {metrics['prompt_eval_seconds']:.2f}s) warm={warm['wall_seconds']:.2f}s",
            flush=True,
        )
    return rows


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), 'rb') as handle:
        return handle.getnframes() / handle.getframerate()


def measure_files(config, files: list[Path]) -> tuple[float, list[dict]]:
    model, load_seconds = load_whisper_model(config)
    print(f'[B] whisper load {load_seconds:.2f}s', flush=True)
    # Warm both models so per-file numbers match a running node.
    transcribe_audio(model, files[0], config.language)
    classify_command_with_metrics(COLD_TEXT, config.classifier)

    rows = []
    for index, path in enumerate(files, 1):
        row = {'file': str(path), 'audio_seconds': round(wav_seconds(path), 3)}
        try:
            text, stt_seconds = transcribe_audio(model, path, config.language)
            command, metrics = classify_command_with_metrics(text, config.classifier)
            row.update({
                'transcript': text,
                'action': command['action'],
                'object': command['object'],
                'stt_seconds': round(stt_seconds, 3),
                'llm_seconds': round(metrics['wall_seconds'], 3),
                'error': '',
            })
        except Exception as exc:  # keep going; record the failure
            row.update({
                'transcript': '', 'action': '', 'object': '',
                'stt_seconds': '', 'llm_seconds': '',
                'error': f'{type(exc).__name__}: {exc}',
            })
        row['expected'] = ''  # filled in by hand later
        rows.append(row)
        print(
            f"[B] {index}/{len(files)} {path.name} stt={row['stt_seconds']} "
            f"llm={row['llm_seconds']} {row['action']}/{row['object']} {row['transcript']}"
            f"{' ERROR ' + row['error'] if row['error'] else ''}",
            flush=True,
        )
    return load_seconds, rows


def measure_relisten_gap(config, repeats: int) -> list[float]:
    gaps = []
    for index in range(1, repeats + 1):
        started = time.perf_counter()
        process = start_capture(get_default_source())
        try:
            assert process.stdout is not None
            for _ in range(config.vad.stream_warmup_frames):
                if read_frame(process.stdout) is None:
                    raise RuntimeError('microphone stream ended during warm-up')
            gaps.append(time.perf_counter() - started)
        finally:
            stop_capture(process)
        print(f'[C] run {index}: {gaps[-1]:.2f}s', flush=True)
    return gaps


def git_commit() -> str:
    return subprocess.run(
        ['git', '-C', str(ROOT), 'rev-parse', '--short', 'HEAD'],
        capture_output=True, text=True, check=False,
    ).stdout.strip()


def cpu_name() -> str:
    for line in Path('/proc/cpuinfo').read_text().splitlines():
        if line.startswith('model name'):
            return line.split(':', 1)[1].strip()
    return platform.processor()


def write_report(path: Path, config, files, cold, load_seconds, rows, gaps, csv_path) -> None:
    ok = [r for r in rows if not r['error']]
    stt = [r['stt_seconds'] for r in ok]
    llm = [r['llm_seconds'] for r in ok]
    end_wait = config.vad.end_silence_frames * 0.03
    total = [end_wait + s + m for s, m in zip(stt, llm)]
    counts: dict[str, int] = {}
    for r in ok:
        key = f"{r['action']}/{r['object']}" if r['object'] else r['action']
        counts[key] = counts.get(key, 0) + 1

    lines = [
        f'# 음성 파이프라인 기준선 (녹음 기반) — {datetime.now():%Y-%m-%d %H:%M}',
        '',
        '말 없이 측정할 수 있는 항목만 담았습니다. 정지 지연·정지 인식률은 실제 발화로 따로 측정합니다.',
        '',
        '## 조건',
        '',
        f'- 커밋 `{git_commit()}`, CPU {cpu_name()}, GPU 없음',
        f'- Whisper `{config.whisper_model}` / {config.whisper_device} / {config.whisper_compute_type}, '
        f'LLM `{config.classifier.model}` (keep_alive {config.classifier.keep_alive})',
        f'- VAD 종료 무음 {config.vad.end_silence_ms} ms, 스트림 시작 후 버림 {config.vad.stream_warmup_ms} ms',
        f'- 입력: 기존 발화 WAV {len(files)}개 (정해진 측정 조건에서 모은 녹음 아님)',
        '',
        '## A. 오래 쉰 뒤 첫 명령 (Ollama 모델을 내린 직후 첫 분류)',
        '',
        f'- 첫 분류: {summary([c["cold_wall_seconds"] for c in cold])}',
        f'  - 그중 모델 로드: {summary([c["cold_load_seconds"] for c in cold])}',
        f'  - 그중 시스템 프롬프트 처리: {summary([c["cold_prompt_eval_seconds"] for c in cold])}',
        f'- 바로 다음 분류(로드된 상태): {summary([c["warm_wall_seconds"] for c in cold])}',
        '- 참고: OS 파일 캐시는 비우지 않았습니다. 재부팅 직후처럼 디스크에서 처음 읽는 경우는 더 느릴 수 있습니다.',
        f'- 참고: Whisper 모델 로드(노드 시작 시 1회) {load_seconds:.2f} s',
        '',
        '## B. 명령 처리 지연 (모델이 올라간 상태, 발화당)',
        '',
        f'- STT: {summary(stt)}',
        f'- LLM: {summary(llm)}',
        f'- 말 끝 → 명령 (종료 대기 {end_wait:.2f} s + STT + LLM): {summary(total)}',
        f'- 실패: {len(rows) - len(ok)}개',
        '',
        '분류 결과 분포 (정답 여부는 CSV의 `expected` 열에 직접 표시):',
        '',
    ]
    lines += [f'- {key}: {count}' for key, count in sorted(counts.items())]
    lines += [
        '',
        f'파일별 결과: `{csv_path.name}`',
        '',
        '## C. 다시 듣기까지 (명령마다 마이크를 다시 열고 시작 구간을 버리는 시간)',
        '',
        f'- {summary(gaps)}',
        '- 이 시간 동안 한 말은 녹음되지 않습니다. 상시 녹음(5.1 ①) 후에는 0에 가까워야 합니다.',
        '',
    ]
    path.write_text('\n'.join(lines), encoding='utf-8')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--recordings', type=Path, nargs='+',
                        default=[Path('~/2026project/recordings').expanduser()],
                        help='Folders or WAV files to transcribe')
    parser.add_argument('--limit', type=int, default=0, help='Use at most this many WAV files')
    parser.add_argument('--cold-repeats', type=int, default=3)
    parser.add_argument('--gap-repeats', type=int, default=5)
    parser.add_argument('--skip-mic', action='store_true', help='Skip part C')
    args = parser.parse_args()

    config = build_pipeline_config({'llm.timeout_seconds': 120.0, 'llm.keep_alive': '10m'})
    files: list[Path] = []
    for entry in args.recordings:
        entry = entry.expanduser()
        files += sorted(entry.glob('*.wav')) if entry.is_dir() else [entry]
    if args.limit:
        files = files[:args.limit]
    if not files:
        parser.error('no WAV files found')

    stamp = datetime.now().strftime('%Y%m%d_%H%M')
    RESULTS_DIR.mkdir(exist_ok=True)
    report_path = RESULTS_DIR / f'baseline_{stamp}.md'
    csv_path = RESULTS_DIR / f'baseline_{stamp}_files.csv'

    cold = measure_cold_start(config, args.cold_repeats)
    load_seconds, rows = measure_files(config, files)
    gaps = [] if args.skip_mic else measure_relisten_gap(config, args.gap_repeats)

    with csv_path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    write_report(report_path, config, files, cold, load_seconds, rows, gaps, csv_path)
    print(f'\nreport: {report_path}\ncsv:    {csv_path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
