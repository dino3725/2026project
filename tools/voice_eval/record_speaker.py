#!/usr/bin/env python3
"""Record prompted speaker enrollment/evaluation locally; no ROS or command publication."""

import argparse
from datetime import datetime
import hashlib
from pathlib import Path
import subprocess
import tempfile
import wave

from speaker_audio import private_json, read_pcm

from voice_command_pkg.speaker_backend import MODEL_SHA256, SpeakerEncoder
from voice_command_pkg.speaker_profile import centroid


PROMPTS = (
    '콜라 가져다 줘', '휴지 가져다 줘', '바세린 가져다 줘', '에어팟 가져다 줘',
    '목마른데 마실 거 가져다 줘', '입술이 건조한데 바세린 가져다 줘',
    '코를 닦게 휴지 좀 가져다 줘', '음악 듣게 에어팟 좀 가져다 줘',
    '나에게 콜라 좀 가져다 줄래', '내 앞으로 휴지를 가져다 줘',
    '바세린이 필요해 가져다 줘', '에어팟이 필요해 가져다 줘',
    '마실 콜라 하나 가져다 줘', '휴지 좀 부탁해',
    '입술에 바를 바세린 부탁해', '에어팟 좀 부탁해',
    '콜라를 나한테 가져와 줘', '휴지를 나한테 가져와 줘',
    '바세린을 나한테 가져와 줘', '에어팟을 나한테 가져와 줘',
)
PURPOSES = ('enrollment', 'genuine-dev', 'other-dev', 'genuine-test', 'other-test')


def record(args):
    """Ask before each six-second clip; retry poor audio without using it in the template."""
    if args.purpose == 'enrollment' and args.profile.expanduser().exists():
        raise ValueError('등록 파일이 이미 있습니다. 새 --profile 경로를 지정하세요.')
    consent = False
    if args.purpose.startswith('other'):
        consent = input('녹음하는 다른 분이 이 화자 인증 시험에 동의했나요? (yes): ').strip() == 'yes'
        if not consent:
            raise ValueError('동의한 분의 음성으로만 진행합니다.')
    print('화자 모델 준비 중입니다. 잠시 기다려 주세요.', flush=True)
    encoder = SpeakerEncoder()
    root = Path('recordings')
    root.mkdir(exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix=f'speaker_{args.purpose}_', dir=root)).resolve()
    rows, vectors = [], []
    count = 20 if args.purpose == 'enrollment' else 10
    print(f'저장 위치: {folder}\n총 {count}문장입니다. 평소 목소리로 혼자 말해 주세요.')
    print('Enter → “지금 말씀하세요” 표시 → 문장 읽기 → 6초 후 자동 종료 순서입니다.')
    for index in range(count):
        prompt = PROMPTS[index]
        while True:
            input(f'\n[{index + 1}/{count}] {prompt}\n준비되면 Enter: ')
            # Discard device startup audio before displaying the speaking cue.
            proc = subprocess.Popen([
                'arecord', '-q', '-D', args.device, '-t', 'raw', '-f', 'S16_LE',
                '-r', '16000', '-c', '1', '-d', '7',
            ], stdout=subprocess.PIPE, bufsize=0)
            try:
                warmup = bytearray()
                while len(warmup) < 32000:
                    block = proc.stdout.read(32000 - len(warmup))
                    if not block:
                        raise ValueError('마이크 시작 실패: 입력 장치를 확인하세요.')
                    warmup.extend(block)
                print('지금 말씀하세요 (6초 녹음)', flush=True)
                pcm, _ = proc.communicate(timeout=12)
                if proc.returncode or len(pcm) != 6 * 32000:
                    raise ValueError('마이크 녹음이 정상적으로 완료되지 않았습니다.')
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
            try:
                vector = encoder.encode(pcm)
            except ValueError as exc:
                print(f'다시 녹음해 주세요: {exc}')
                continue
            answer = input('문장을 잘 읽었나요? Enter=저장, r=다시 녹음: ').strip().lower()
            if answer:
                continue
            path = folder / f'{index + 1:02d}.wav'
            with wave.open(str(path), 'wb') as wav:
                wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                wav.writeframes(pcm)
            path.chmod(0o600)
            digest = hashlib.sha256(read_pcm(path)).hexdigest()
            rows.append({'file': path.name, 'prompt': prompt, 'pcm_sha256': digest})
            vectors.append(vector)
            break
    private_json(folder / 'manifest.json', {
        'schema': 1, 'purpose': args.purpose, 'consent': consent, 'complete': True,
        'created_at': datetime.now().astimezone().isoformat(), 'clips': rows,
    })
    if args.purpose == 'enrollment':
        private_json(args.profile, {
            'schema': 1, 'backend': 'resemblyzer-0.1.4', 'sample_rate': 16000,
            'min_speech_seconds': .6,
            'model_sha256': MODEL_SHA256, 'embedding': centroid(vectors).tolist(),
            'threshold': 0.75, 'calibrated': False,
            'enrollment_hashes': [row['pcm_sha256'] for row in rows],
            'note': 'Provisional threshold; calibration and independent validation pending',
        })
        print(f'등록 완료: {args.profile}\n아직 인증을 켜지 않았습니다. 본인·타인 검증이 남았습니다.')
    print(f'결과 위치: {folder / "manifest.json"}')


def main():
    """Expose one simple registration command and explicit evaluation recording roles."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--purpose', choices=PURPOSES, default='enrollment')
    parser.add_argument('--profile', type=Path, default=Path('speaker_profiles/owner.json'))
    parser.add_argument('--device', default='pulse')
    args = parser.parse_args()
    try:
        record(args)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, f'중단: {exc}\n')
    except (KeyboardInterrupt, EOFError):
        parser.exit(1, '\n녹음을 중단했습니다. 미완료 녹음은 등록에 사용하지 않습니다.\n')


if __name__ == '__main__':
    main()
