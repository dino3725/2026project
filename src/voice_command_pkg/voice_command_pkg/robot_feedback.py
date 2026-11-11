"""ROS-independent robot admission and feedback policy, using a monotonic clock."""

from dataclasses import dataclass
import math
from string import Formatter


ROBOT_STATES = frozenset({
    'IDLE', 'MOVE_TO_RECOGNITION', 'VISION_SEARCH', 'PICKING', 'MOVE_TO_CARGO',
    'DRIVE_TO_PLACE', 'PLACING', 'RETURN_HOME', 'ERROR',
})
OBJECT_NAMES = {'coke': '콜라', 'tissue': '휴지', 'vaseline': '바세린', 'airpod': '에어팟'}
DEFAULT_MESSAGES = {
    'robot_unknown': '로봇 상태를 확인하고 있어요. 상태 확인 후 다시 말씀해 주세요.',
    'robot_busy': '지금 작업 중이에요.',
    'robot_error': '로봇 상태를 확인하고 복구해 주세요.',
    'robot_pending': '앞서 보낸 명령의 처리 결과를 기다리고 있어요.',
    'robot_before_idle': '작업 가능 상태가 되기 전 요청이에요. 다시 말씀해 주세요.',
    'robot_response_timeout': '로봇 응답을 확인하지 못했어요. 로봇 상태를 확인해 주세요.',
    'robot_invalid_timing': '요청 시간을 확인하지 못했어요. 다시 말씀해 주세요.',
    'robot_expired': '요청 유효시간이 지났어요. 다시 말씀해 주세요.',
    'robot_accepted': '{object} 가져올게요.',
    'robot_rejected': '로봇이 요청을 거절했어요. 상태를 확인해 주세요.',
    'robot_carrying': '{object}를 집었어요.',
    'robot_stop_sent': '정지 명령을 보냈어요.',
    'robot_stop_accepted': '로봇이 정지 요청을 수락했어요.',
    'robot_send_error': '명령 전달 여부를 확인하지 못했어요. 로봇 상태를 확인해 주세요.',
    'robot_unknown_command': '할 수 없는 명령이에요.',
    'robot_dry_run': '시험 모드: 명령을 발행하지 않았어요.',
}


def validate_templates(messages):
    """Allow only a plain object placeholder; reject bad templates at startup."""
    if set(messages) != set(DEFAULT_MESSAGES):
        raise ValueError('robot feedback template keys do not match defaults')
    for key, template in messages.items():
        if not isinstance(template, str) or not template.strip():
            raise ValueError(f'{key} must be a nonempty string')
        for _, name, spec, conversion in Formatter().parse(template):
            if name is not None and (name != 'object' or spec or conversion):
                raise ValueError(f'{key}: only plain {{object}} is supported')


def render_feedback(messages, event):
    """Render an event without inferring physical completion from publication."""
    key, target = event
    return messages[key].format(object=OBJECT_NAMES.get(target, '물체'))


@dataclass
class PendingCommand:
    """One locally sent command; the wire protocol has no request identifier."""

    action: str
    target: str
    sent_at: float
    saw_busy: bool = False
    accepted: bool = False
    timed_out: bool = False


class RobotFeedback:
    """Serialize access externally; never infer disconnect from state-message age."""

    def __init__(self, response_timeout_seconds=5.0):
        if not math.isfinite(response_timeout_seconds) or response_timeout_seconds <= 0:
            raise ValueError('response_timeout_seconds must be finite and positive')
        self.response_timeout_seconds = response_timeout_seconds
        self.reset()

    def reset(self):
        """Forget state and command association on controller loss or replacement."""
        self.state = None
        self.idle_since = float('inf')
        self.pending = None
        self.active_target = ''
        self.carrying_announced = False

    def observe_state(self, state, now):
        """Apply a state transition; a busy-to-IDLE cycle retires pending work."""
        old_state = self.state
        self.state = state if state in ROBOT_STATES else None
        events = []
        if self.state != 'IDLE':
            self.idle_since = float('inf')
            if self.pending is not None and self.state is not None:
                self.pending.saw_busy = True
        elif old_state != 'IDLE':
            self.idle_since = now
            if self.pending is not None and self.pending.saw_busy:
                self.pending = None
            self.active_target = ''
            self.carrying_announced = False
        if self.state == 'ERROR' and old_state != 'ERROR':
            if self.pending is None or self.pending.action != 'stop':
                events.append(('robot_error', ''))
        events.extend(self._carrying_event())
        return events

    def fetch_drop_reason(self, utterance_start):
        """Admit only speech begun in the current available IDLE interval."""
        if not math.isfinite(utterance_start):
            return 'robot_invalid_timing'
        if self.state is None:
            return 'robot_unknown'
        if self.state == 'ERROR':
            return 'robot_error'
        if self.state != 'IDLE':
            return 'robot_busy'
        if self.pending is not None:
            return 'robot_response_timeout' if self.pending.timed_out else 'robot_pending'
        if utterance_start <= self.idle_since:
            return 'robot_before_idle'
        return ''

    def mark_sent(self, action, target, now):
        """Reserve the request before publication; stop may replace any fetch."""
        self.pending = PendingCommand(
            action, target, now, saw_busy=self.state not in (None, 'IDLE')
        )
        self.active_target = ''
        self.carrying_announced = False

    def mark_send_uncertain(self):
        """Do not retry a publication whose delivery is uncertain."""
        if self.pending is not None:
            self.pending.timed_out = True

    def tick(self, now):
        """Report response timeout once, retaining the block until state recovery."""
        request = self.pending
        if request is None or request.accepted or request.timed_out:
            return []
        if now - request.sent_at < self.response_timeout_seconds:
            return []
        request.timed_out = True
        return [('robot_response_timeout', '')]

    def observe_status(self, action, target, accepted, now):
        """Match the current local action/target; ignore unmatched or late replies."""
        request = self.pending
        if (
            request is None or request.timed_out or request.accepted
            or (action, target) != (request.action, request.target)
        ):
            return []
        if not accepted:
            self.pending = None
            self.active_target = ''
            if self.state == 'IDLE':
                self.idle_since = now
            return [('robot_rejected', '')]
        request.accepted = True
        if action == 'stop':
            return [('robot_stop_accepted', '')]
        if self.state in ('ERROR', 'RETURN_HOME'):
            # A delayed acknowledgement must not promise a task already failing/ending.
            return []
        self.active_target = target
        return [('robot_accepted', target)] + self._carrying_event()

    def _carrying_event(self):
        if (
            self.state == 'DRIVE_TO_PLACE' and self.active_target
            and not self.carrying_announced
        ):
            self.carrying_announced = True
            return [('robot_carrying', self.active_target)]
        return []
