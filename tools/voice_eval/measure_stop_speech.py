#!/usr/bin/env python3
"""
Measure live stop recognition and stop latency with your own voice.

For each trial the script shows a phrase, records one VAD utterance from the
default microphone, transcribes it with Whisper and applies the stop rule
(transcript_rules). The LLM is never called, so Ollama does not have to run.

Nothing is published to ROS. WAV files go to a temporary directory and are
deleted at the end, except failed trials, which are kept in the git-ignored
recordings/stop_speech_fail/ for diagnosis. Transcripts are written only to
the local *_files.csv, which is git-ignored; the .md report holds counts and
times only. A trial with no detected voice start within --start-timeout
seconds is recorded as a failure (no_start) and the next trial begins.

    PYTHONPATH=src/voice_command_pkg .venv/bin/python tools/voice_eval/measure_stop_speech.py

--diagnose prints, for every 300 ms window, how many 30 ms frames WebRTC VAD
called speech, how many also passed the start_min_rms gate, and the peak
frame RMS. Voice start needs 8 qualified frames out of 10.
"""

from __future__ import annotations

import argparse
import csv
from collections import deque
from datetime import datetime
from pathlib import Path
import statistics
import subprocess
import tempfile
import threading
import time

import audioop
import shutil

from voice_command_pkg.audio_capture import (
    get_default_source, read_frame, SAMPLE_RATE, SAMPLE_WIDTH, start_capture, stop_capture,
)
from voice_command_pkg.parameters import build_pipeline_config
from voice_command_pkg.pipeline import (
    capture_utterance,
    load_whisper_model,
    PipelineError,
    PipelineStopped,
    save_wav,
    transcribe_audio,
)
from voice_command_pkg.transcript_rules import find_stop_keyword


ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = Path(__file__).resolve().parent / 'results'
FAIL_DIR = ROOT / 'recordings' / 'stop_speech_fail'

STOP_PHRASES = ['멈춰', '정지', '스톱', '로봇 멈춰', '그만 움직여']
CONTROL_PHRASES = ['콜라 가져다줘', '목마른데 마실 거 가져다줘', '휴지 좀 줘',
                   '에어팟 가져와', '입술이 건조해']


def summary(values: list[float]) -> str:
    if not values:
        return '-'
    ordered = sorted(values)
    p90 = ordered[min(len(ordered) - 1, round(0.9 * (len(ordered) - 1)))]
    return (
        f'중앙값 {statistics.median(ordered):.2f} s, 평균 {statistics.mean(ordered):.2f} s, '
        f'최소 {ordered[0]:.2f} s, p90 {p90:.2f} s, 최대 {ordered[-1]:.2f} s (n={len(ordered)})'
    )


def git_commit() -> str:
    result = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', '--short', 'HEAD'],
                            capture_output=True, text=True, check=False)
    return result.stdout.strip() or 'unknown'


def capture_with_timeout(config, timeout: float):
    """Capture one utterance; return None when no voice start within timeout."""
    stop_event = threading.Event()
    timer = threading.Timer(timeout, stop_event.set)

    def log(line: str) -> None:
        print('   ' + line, flush=True)
        if line.startswith('[LISTENING]'):
            timer.start()
        elif line.startswith('[VOICE START]'):
            timer.cancel()

    try:
        return capture_utterance(config.vad, log, stop_event)
    except PipelineStopped:
        return None
    finally:
        timer.cancel()


def run_trial(config, model, workdir: Path, index: int, kind: str, phrase: str,
              timeout: float, stamp: str) -> dict:
    print(f'\n[{index:02d}] ({kind}) "{phrase}" 라고 말하세요.', flush=True)
    row = {'index': index, 'kind': kind, 'phrase': phrase, 'result': '', 'transcript': '',
           'keyword': '', 'ok': False, 'utterance_seconds': 0.0, 'end_silence_seconds': 0.0,
           'stt_seconds': 0.0, 'voice_end_to_decision_seconds': 0.0,
           'speech_end_to_decision_seconds': 0.0, 'kept_wav': ''}
    captured = capture_with_timeout(config, timeout)
    if captured is None:
        row['result'] = 'no_start'
        row['ok'] = kind != 'stop'
        print(f'   [시작 감지 실패] {timeout:.0f}s 동안 발화 시작을 감지하지 못했습니다.'
              f'  {"OK" if row["ok"] else "FAIL"}')
        return row

    audio, capture = captured
    path = workdir / f'trial_{index:03d}.wav'
    save_wav(path, audio)
    try:
        text, stt_seconds = transcribe_audio(model, path, config.language)
    except PipelineError as exc:
        text, stt_seconds = '', 0.0
        print(f'   [STT] 실패: {exc.describe()}')
    keyword = find_stop_keyword(text) if text else None
    decided = time.perf_counter()
    after_end = decided - float(capture['voice_end_wall'])
    end_silence = float(capture['vad_end_silence_seconds'])
    is_stop = keyword is not None
    ok = is_stop if kind == 'stop' else not is_stop
    row.update({
        'result': 'stop' if is_stop else ('empty' if not text else 'not_stop'),
        'transcript': text,
        'keyword': keyword or '',
        'ok': ok,
        'utterance_seconds': round(float(capture['utterance_seconds']), 3),
        'end_silence_seconds': round(end_silence, 3),
        'stt_seconds': round(stt_seconds, 3),
        'voice_end_to_decision_seconds': round(after_end, 3),
        'speech_end_to_decision_seconds': round(end_silence + after_end, 3),
    })
    if not ok:
        FAIL_DIR.mkdir(parents=True, exist_ok=True)
        kept = FAIL_DIR / f'{stamp}_trial_{index:03d}.wav'
        shutil.copy(path, kept)
        row['kept_wav'] = str(kept.relative_to(ROOT))
    print(f'   [STT] {text!r}  -> {"STOP rule:" + keyword if is_stop else "stop 아님"}'
          f'  {"OK" if ok else "FAIL"}  말끝→판정 {end_silence + after_end:.2f}s')
    return row


def diagnose(config, seconds: float) -> None:
    """Print per-window VAD and RMS-gate counts (what voice start sees)."""
    import webrtcvad

    vad = webrtcvad.Vad(config.vad.vad_mode)
    window = config.vad.start_window_frames
    process = start_capture(get_default_source())
    assert process.stdout is not None
    try:
        for _ in range(config.vad.stream_warmup_frames):
            read_frame(process.stdout)
        print(f'{seconds:.0f}초 동안 듣습니다. "스톱", "정지", "멈춰"를 몇 번씩 말해 보세요.')
        print(f'시작 조건: 창 {window}프레임 중 {config.vad.start_required_speech_frames}개 이상이 '
              f'VAD 음성 + RMS >= {config.vad.start_min_rms}')
        print('slide = 실제 판정처럼 프레임마다 미는 10프레임 창의 최대값')
        frames = int(seconds * 1000 / 30)
        need = config.vad.start_required_speech_frames
        recent: deque[bool] = deque(maxlen=window)
        speech = gated = peak = slide = 0
        for i in range(1, frames + 1):
            frame = read_frame(process.stdout)
            if frame is None:
                break
            rms = audioop.rms(frame, SAMPLE_WIDTH)
            is_speech = vad.is_speech(frame, SAMPLE_RATE)
            speech += is_speech
            passed = is_speech and rms >= config.vad.start_min_rms
            gated += passed
            recent.append(passed)
            slide = max(slide, sum(recent))
            peak = max(peak, rms)
            if i % window == 0:
                if speech or peak >= config.vad.start_min_rms:
                    mark = '  <-- 시작 조건 충족' if slide >= need else ''
                    print(f'{i * 0.03:6.2f}s  vad={speech:2d}/{window}  '
                          f'vad+rms={gated:2d}/{window}  slide={slide:2d}/{window}  '
                          f'peak_rms={peak:5d}{mark}', flush=True)
                speech = gated = peak = slide = 0
    finally:
        stop_capture(process)


def write_report(path: Path, csv_path: Path, config, rows: list[dict]) -> None:
    stops = [r for r in rows if r['kind'] == 'stop']
    controls = [r for r in rows if r['kind'] == 'control']
    stop_ok = [r for r in stops if r['ok']]
    false_stops = [r for r in controls if not r['ok']]
    no_start = [r for r in rows if r['result'] == 'no_start']
    empty = [r for r in rows if r['result'] == 'empty']
    lines = [
        f'# 음성 정지 인식·지연 측정 — {datetime.now():%Y-%m-%d %H:%M}',
        '',
        f'- 커밋 `{git_commit()}`, Whisper `{config.whisper_model}` '
        f'({config.whisper_device}/{config.whisper_compute_type}), '
        f'end_silence_ms {config.vad.end_silence_ms}, '
        f'시작 조건 {config.vad.start_required_speech_frames}/{config.vad.start_window_frames} '
        f'(start_speech_ratio {config.vad.start_speech_ratio}), '
        f'start_min_rms {config.vad.start_min_rms}',
        '- 판정: 정지 규칙(`transcript_rules.py`)만 사용, LLM 호출 없음, ROS 발행 없음',
        f'- 전사문은 로컬 `{csv_path.name}`에만 기록(커밋 제외)',
        '',
        '## 결과',
        '',
        '| 항목 | 결과 |',
        '| --- | --- |',
        f'| 정지 인식 | **{len(stop_ok)}/{len(stops)}** |',
        f'| 일반 문장 오정지 | {len(false_stops)}/{len(controls)} |',
        f'| 발화 시작 감지 실패 (no_start) | {len(no_start)} |',
        f'| 빈 전사(STT 실패) | {len(empty)} |',
        f'| 전사 결과가 다른 문장 | {sum(r["result"] == "not_stop" for r in stops)} |',
        f'| 말 끝 → 정지 판정 (정지 성공 시행) | '
        f"{summary([r['speech_end_to_decision_seconds'] for r in stop_ok])} |",
        f'| 　└ 종료 무음 대기 | {summary([r["end_silence_seconds"] for r in stop_ok])} |',
        f'| 　└ STT | {summary([r["stt_seconds"] for r in stop_ok])} |',
        '',
        '말 끝 → 정지 판정은 노트북 안의 시간입니다. ROS 전송과 Jetson 상태 전환은 포함하지 않습니다(5.4 ④에서 측정).',
        '',
        '## 문구별 정지 인식',
        '',
        '| 문구 | 인식 |',
        '| --- | --- |',
    ]
    for phrase in dict.fromkeys(r['phrase'] for r in stops):
        group = [r for r in stops if r['phrase'] == phrase]
        lines.append(f"| {phrase} | {sum(r['ok'] for r in group)}/{len(group)} |")
    lines.append('')
    path.write_text('\n'.join(lines), encoding='utf-8')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--trials', type=int, default=20, help='Stop trials')
    parser.add_argument('--controls', type=int, default=5, help='Non-stop trials (false stop)')
    parser.add_argument('--start-timeout', type=float, default=8.0,
                        help='Seconds to wait for voice start before marking no_start')
    parser.add_argument('--diagnose', type=float, nargs='?', const=30.0, default=None,
                        metavar='SECONDS', help='Only print VAD start statistics')
    parser.add_argument('--start-ratio', type=float, default=None,
                        help='Override vad.start_speech_ratio (experiment only)')
    parser.add_argument('--start-min-rms', type=int, default=None,
                        help='Override vad.start_min_rms (experiment only)')
    args = parser.parse_args()

    overrides = {}
    if args.start_ratio is not None:
        overrides['vad.start_speech_ratio'] = args.start_ratio
    if args.start_min_rms is not None:
        overrides['vad.start_min_rms'] = args.start_min_rms
    config = build_pipeline_config(overrides)
    if args.diagnose is not None:
        try:
            diagnose(config, args.diagnose)
        except KeyboardInterrupt:
            pass
        return 0
    print('Whisper 모델 로드 중...', flush=True)
    model, load_seconds = load_whisper_model(config)
    print(f'로드 {load_seconds:.1f}s. 각 문구를 평소 목소리로 한 번씩 말하세요. Ctrl+C로 중단.')

    plan = [('stop', STOP_PHRASES[i % len(STOP_PHRASES)]) for i in range(args.trials)]
    plan += [('control', CONTROL_PHRASES[i % len(CONTROL_PHRASES)]) for i in range(args.controls)]

    stamp = datetime.now().strftime('%Y%m%d_%H%M')
    rows: list[dict] = []
    with tempfile.TemporaryDirectory(prefix='stop_speech_') as tmp:
        try:
            for index, (kind, phrase) in enumerate(plan, start=1):
                rows.append(run_trial(config, model, Path(tmp), index, kind, phrase,
                                      args.start_timeout, stamp))
        except KeyboardInterrupt:
            print('\n중단했습니다. 지금까지의 결과만 기록합니다.')
    if not rows:
        return 1

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = RESULTS_DIR / f'stop_speech_{stamp}_files.csv'
    with csv_path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    report = RESULTS_DIR / f'stop_speech_{stamp}.md'
    write_report(report, csv_path, config, rows)
    print(f'\n보고서: {report}\n전사문(로컬 전용): {csv_path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
