import pytest

from voice_command_pkg.transcript_rules import (
    find_stop_keyword,
    is_stop_command,
    normalize_transcript,
    stop_command,
)


# Stop sentences of tools/voice_eval/test_llm_pick_objects.py (all must stop).
REGRESSION_STOP = [
    '로봇 멈춰', '멈춰', '정지해', '로봇 정지', '지금 당장 멈춰', '움직이지 마', '스톱', 'stop',
    '작동 멈춰', '로봇 그만 움직여', '플랫폼을 지금 정지해', '당장 이동을 중단해 줘',
    '거기 그대로 서', '더 이상 움직이지 마', '주행을 멈춰줘', '비상 정지해', '앞으로 가지 말고 멈춰',
]

# How Whisper tends to write a spoken stop.
WHISPER_STOP_VARIANTS = [
    '멈춰.', '멈춰!', '멈 춰', '멈쳐', '멈처.', '멈춰요', '멈추어', '멈추라고', '스탑',
    'Stop!', 'STOP', '정지!', '로봇, 멈춰!', '일시 멈춤',
]

# Fetch and unknown sentences of the regression set plus real transcripts from
# the 2026-11-04 recordings. None of them may stop the robot.
NOT_STOP = [
    '콜라 가져다줘', '목마른데 마실 거 가져다줘', '목 마린데 마실 거 가져다 줘', '봉마른 데 마실 거 가져다 줘',
    '갈증 나니까 마실 음료가 필요해', '목을 축일 만한 걸 갖다줄래', '바세린 가져다줘',
    '입술이 갈라져서 바를 게 필요해', '손가락 사이 피부가 건조하게 갈라졌는데 보습할 게 없어',
    '휴지 가져다줘', '커피를 엎질러서 닦아야 해', '코를 풀 수 있게 휴지 좀 부탁해',
    '테이블이 끈적하니 닦을 걸 갖다줘', '에어팟 가져다줘', '무선 이어폰이 필요해',
    '내 블루투스 이어폰을 찾아서 줘', '출근길에 음악 들을 이어폰이 필요하다',
    '오늘 날씨 어때', '안녕', '노래 틀어줘', '배고프니까 과자 가져다줘', '텔레비전 리모컨을 가져와',
    '아까 봤던 그거 가져다줘', '콜라와 바세린을 둘 다 가져와', '입술도 텄고 목도 말라서 둘 다 해결해줘',
    '에어팟도 휴지도 필요 없으니 가져오지 마', '구독과 좋아요 부탁드려요!', '1번 성공입니다',
]


@pytest.mark.parametrize('text', REGRESSION_STOP + WHISPER_STOP_VARIANTS)
def test_stop_sentences_are_detected(text):
    assert is_stop_command(text)


@pytest.mark.parametrize('text', NOT_STOP)
def test_other_sentences_do_not_stop(text):
    assert find_stop_keyword(text) is None


@pytest.mark.parametrize('text', ['음악 재생을 멈춰', '노래 일시정지', '멈추지 말고 콜라 가져와'])
def test_stop_wins_even_in_other_context(text):
    # Intended: a false stop only halts the robot, a missed stop is dangerous.
    assert is_stop_command(text)


def test_normalize_drops_spaces_punctuation_and_case():
    assert normalize_transcript(' 로봇,  멈춰! ') == '로봇멈춰'
    assert normalize_transcript('STOP.') == 'stop'


def test_normalize_fixes_misrecognitions():
    assert normalize_transcript('멈 쳐') == '멈춰'
    assert normalize_transcript('스탑!') == '스톱'


def test_empty_transcript_is_not_stop():
    assert not is_stop_command('')
    assert not is_stop_command('...')


def test_stop_command_is_a_fresh_dict():
    first = stop_command()
    first['object'] = 'coke'
    assert stop_command() == {'action': 'stop', 'object': ''}
