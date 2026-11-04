"""
Shared ROS helpers for the manipulator measurement scripts.

Everything in this module only listens (TF, /joint_states, /control/state).
It never publishes and never sends action goals.
"""

import csv
import datetime
import math
import os
import threading
import time
from typing import Dict, List, Optional, Sequence, Tuple

import rclpy
from rclpy.duration import Duration
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from rclpy.time import Time
from sensor_msgs.msg import JointState
from std_msgs.msg import String
from tf2_ros import Buffer, TransformListener

ARM_JOINTS = ('joint1', 'joint2', 'joint3', 'joint4')
GRIPPER_JOINT = 'gripper_left_joint'
BASE_FRAME = 'base_link'
EE_FRAME = 'end_effector_link'
CAMERA_OPTICAL_FRAME = 'd435i_color_optical_frame'

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'results', 'raw')

Pose = Tuple[Tuple[float, float, float], Tuple[float, float, float, float]]


def latched_qos() -> QoSProfile:
    """Match control_node's TRANSIENT_LOCAL state publisher."""
    qos = QoSProfile(depth=1)
    qos.reliability = ReliabilityPolicy.RELIABLE
    qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
    return qos


def default_csv_path(prefix: str) -> str:
    """Return results/raw/<prefix>_<YYYY-MM-DD>.csv."""
    today = datetime.date.today().isoformat()
    return os.path.join(RESULTS_DIR, f'{prefix}_{today}.csv')


def append_csv_row(path: str, fieldnames: Sequence[str], row: Dict) -> None:
    """Append one row, writing the header when the file is new."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    new_file = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, 'a', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames))
        if new_file:
            writer.writeheader()
        writer.writerow({key: row.get(key, '') for key in fieldnames})


def next_trial_id(path: str) -> int:
    """Return 1 + the number of data rows already in a CSV file."""
    if not os.path.exists(path):
        return 1
    with open(path, newline='', encoding='utf-8') as handle:
        return sum(1 for _ in csv.DictReader(handle)) + 1


def fmt(value: Optional[float], digits: int = 4) -> str:
    """Format a float for CSV, leaving missing values blank."""
    if value is None or not math.isfinite(value):
        return ''
    return f'{value:.{digits}f}'


def quaternion_to_rpy_deg(
    q: Sequence[float],
) -> Tuple[float, float, float]:
    """Convert an xyzw quaternion to roll, pitch, yaw in degrees."""
    x, y, z, w = q
    roll = math.atan2(2.0 * (w * x + y * z), 1.0 - 2.0 * (x * x + y * y))
    sin_pitch = max(-1.0, min(1.0, 2.0 * (w * y - z * x)))
    pitch = math.asin(sin_pitch)
    yaw = math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
    return tuple(math.degrees(v) for v in (roll, pitch, yaw))


class MeasurementNode(Node):
    """Cache TF, joint states and the control_node state."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self._cache_lock = threading.Lock()
        self._joints: Dict[str, float] = {}
        self._joint_stamp = 0.0
        self._control_state = ''
        self._state_listeners: List = []
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)
        self.create_subscription(
            JointState, '/joint_states', self._joint_callback, 10)
        self.create_subscription(
            String, '/control/state', self._state_callback, latched_qos())

    def _joint_callback(self, msg: JointState) -> None:
        with self._cache_lock:
            for name, position in zip(msg.name, msg.position):
                self._joints[name] = float(position)
            self._joint_stamp = time.monotonic()

    def _state_callback(self, msg: String) -> None:
        with self._cache_lock:
            self._control_state = msg.data
            listeners = list(self._state_listeners)
        for listener in listeners:
            listener(msg.data)

    def add_state_listener(self, callback) -> None:
        """Call ``callback(state)`` on every /control/state message."""
        with self._cache_lock:
            self._state_listeners.append(callback)

    @property
    def control_state(self) -> str:
        """Return the latest /control/state value ('' if none yet)."""
        with self._cache_lock:
            return self._control_state

    def joints(self) -> Dict[str, float]:
        """Return a copy of the latest joint positions (radians / meters)."""
        with self._cache_lock:
            return dict(self._joints)

    def joint_age(self) -> float:
        """Return seconds since the last /joint_states message."""
        with self._cache_lock:
            if not self._joint_stamp:
                return math.inf
            return time.monotonic() - self._joint_stamp

    def lookup(self, target: str, source: str,
               timeout: float = 0.5) -> Optional[Pose]:
        """Return (xyz, xyzw) of ``source`` expressed in ``target``."""
        try:
            tf = self.tf_buffer.lookup_transform(
                target, source, Time(), timeout=Duration(seconds=timeout))
        except Exception:
            return None
        t = tf.transform.translation
        r = tf.transform.rotation
        return (t.x, t.y, t.z), (r.x, r.y, r.z, r.w)

    def sample_position(self, target: str, source: str, count: int = 20,
                        period: float = 0.05) -> List[Tuple[float, ...]]:
        """Collect several TF positions to average out update jitter."""
        samples = []
        for _ in range(count):
            pose = self.lookup(target, source)
            if pose is not None:
                samples.append(pose[0])
            time.sleep(period)
        return samples


def spin_in_background(node: Node):
    """Spin ``node`` on a daemon thread so the main thread can use input()."""
    executor = MultiThreadedExecutor(num_threads=2)
    executor.add_node(node)
    thread = threading.Thread(target=executor.spin, daemon=True)
    thread.start()
    return executor, thread


def shutdown(node: Node, executor, thread) -> None:
    """Stop spinning and release ROS resources."""
    executor.shutdown()
    thread.join(timeout=2.0)
    node.destroy_node()
    if rclpy.ok():
        rclpy.shutdown()


def joint_columns(joints: Dict[str, float]) -> Dict[str, str]:
    """Return CSV columns for arm joints (deg) and the gripper joint (m)."""
    row = {}
    for name in ARM_JOINTS:
        value = joints.get(name)
        row[f'{name}_deg'] = fmt(
            math.degrees(value) if value is not None else None, 2)
    row['gripper_q_m'] = fmt(joints.get(GRIPPER_JOINT), 4)
    return row


JOINT_FIELDS = [f'{name}_deg' for name in ARM_JOINTS] + ['gripper_q_m']
