"""
Pure helpers for the manipulator error-baseline measurement (5.5-1).

Nothing here imports ROS, so every function can be tested with pytest.

Units: robot coordinates are meters in ``base_link``; human measurements
(ruler, caliper) and reported errors are millimeters.
"""

from dataclasses import dataclass
import math
import re
from typing import Iterable, List, Optional, Sequence, Tuple

Point = Tuple[float, float, float]

# Fixed geometry from turtlebot3_manipulation_description (URDF).
# base_link sits 10 mm above the floor, between the two wheel axles.
DEFAULT_SURFACE_Z_M = -0.010
# joint1 (arm yaw) axis and joint2 (shoulder pitch) height in base_link.
JOINT1_XY_M = (-0.080, 0.0)
JOINT2_Z_M = 0.1675
# joint2 -> end_effector_link with every link stretched straight.
ARM_MAX_REACH_M = 0.380

_VISION_LOG = re.compile(
    r'Vision target received in base_link: '
    r'\(\s*(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)\)'
)
_MOVEIT_LOG = re.compile(
    r'MoveIt (\w+) reached '
    r'\(\s*(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)\)'
)


def mm_to_m(value_mm: float) -> float:
    """Convert millimeters to meters."""
    return float(value_mm) / 1000.0


def m_to_mm(value_m: float) -> float:
    """Convert meters to millimeters."""
    return float(value_m) * 1000.0


def height_to_base_z(
    height_mm: float,
    surface_z_m: float = DEFAULT_SURFACE_Z_M,
) -> float:
    """Convert a height measured above the surface into base_link z."""
    return surface_z_m + mm_to_m(height_mm)


def grid_mm_to_base(
    x_mm: float,
    y_mm: float,
    height_mm: float,
    surface_z_m: float = DEFAULT_SURFACE_Z_M,
) -> Point:
    """
    Convert a grid-sheet reading into a base_link point in meters.

    The grid origin is drawn directly below base_link: the midpoint of the
    two wheel contact marks. +x is robot forward, +y is robot left.
    """
    return (
        mm_to_m(x_mm),
        mm_to_m(y_mm),
        height_to_base_z(height_mm, surface_z_m),
    )


@dataclass(frozen=True)
class PointError:
    """Measured minus true position, in millimeters."""

    ex: float
    ey: float
    ez: float

    @property
    def horizontal(self) -> float:
        """Return the error length on the table plane."""
        return math.hypot(self.ex, self.ey)

    @property
    def total(self) -> float:
        """Return the full 3D error length."""
        return math.sqrt(self.ex ** 2 + self.ey ** 2 + self.ez ** 2)


def point_error_mm(measured: Sequence[float], truth: Sequence[float]) -> PointError:
    """Return ``measured - truth`` for two base_link points in meters."""
    if len(measured) != 3 or len(truth) != 3:
        raise ValueError('points must have exactly three coordinates')
    values = [m_to_mm(float(a) - float(b)) for a, b in zip(measured, truth)]
    if not all(math.isfinite(value) for value in values):
        raise ValueError('points must be finite')
    return PointError(*values)


@dataclass(frozen=True)
class AxisStats:
    """Bias and spread of one error component, in millimeters."""

    n: int
    mean: float
    std: float
    rms: float
    max_abs: float


def axis_stats(values: Iterable[float]) -> AxisStats:
    """
    Summarize one error component.

    ``mean`` is the systematic error (bias, correctable with an offset).
    ``std`` is the random error (spread, not correctable with an offset).
    ``std`` uses the sample formula (n - 1); it is 0 for a single value.
    """
    data = [float(value) for value in values]
    if not data:
        raise ValueError('at least one value is required')
    n = len(data)
    mean = sum(data) / n
    if n > 1:
        std = math.sqrt(sum((v - mean) ** 2 for v in data) / (n - 1))
    else:
        std = 0.0
    rms = math.sqrt(sum(v * v for v in data) / n)
    max_abs = max(abs(v) for v in data)
    return AxisStats(n, mean, std, rms, max_abs)


def summarize_errors(errors: Sequence[PointError]) -> dict:
    """Return AxisStats for x, y, z, horizontal and total error."""
    if not errors:
        raise ValueError('at least one error is required')
    return {
        'x': axis_stats(e.ex for e in errors),
        'y': axis_stats(e.ey for e in errors),
        'z': axis_stats(e.ez for e in errors),
        'xy': axis_stats(e.horizontal for e in errors),
        'xyz': axis_stats(e.total for e in errors),
    }


def mean_point(samples: Sequence[Sequence[float]]) -> Point:
    """Average several xyz samples."""
    if not samples:
        raise ValueError('at least one sample is required')
    n = len(samples)
    return tuple(sum(float(s[i]) for s in samples) / n for i in range(3))


def point_spread_mm(samples: Sequence[Sequence[float]]) -> float:
    """Return the largest distance (mm) of any sample from their mean."""
    center = mean_point(samples)
    return max(
        m_to_mm(math.dist([float(v) for v in sample], center))
        for sample in samples
    )


@dataclass(frozen=True)
class GripperMargin:
    """Free space left when an object sits between the open fingers."""

    open_width_mm: float
    object_width_mm: float

    @property
    def total_mm(self) -> float:
        """Return open width minus object width (may be negative)."""
        return self.open_width_mm - self.object_width_mm

    @property
    def per_side_mm(self) -> float:
        """
        Return how far the object center may be off along the close axis.

        A centered object leaves half of the margin on each side, so the
        position error along the closing direction must stay below this.
        """
        return self.total_mm / 2.0

    @property
    def fits(self) -> bool:
        """Return True when the object is narrower than the open gripper."""
        return self.total_mm > 0.0


def gripper_width_from_joint(
    position: float,
    open_position: float,
    open_width_mm: float,
    closed_position: float,
    closed_width_mm: float,
) -> float:
    """
    Linearly map a gripper joint value to the measured finger gap (mm).

    Two calibration readings (open and fully closed) are enough because
    both fingers slide on straight prismatic joints.
    """
    if math.isclose(open_position, closed_position):
        raise ValueError('open and closed positions must differ')
    ratio = (position - closed_position) / (open_position - closed_position)
    return closed_width_mm + ratio * (open_width_mm - closed_width_mm)


# Arm-probe targets must keep end_effector_link at least this far above the
# table surface. The command line may raise it but never lower it.
MIN_CLEARANCE_FLOOR_MM = 50.0


def checked_min_clearance_m(requested_mm: float) -> float:
    """Return the clearance in meters, refusing values below the floor."""
    if not math.isfinite(requested_mm) or \
            requested_mm < MIN_CLEARANCE_FLOOR_MM:
        raise ValueError(
            f'min clearance must be >= {MIN_CLEARANCE_FLOOR_MM:.0f} mm')
    return mm_to_m(requested_mm)


def lift_target(
    current: Sequence[float],
    lift_m: float,
    max_z_m: float,
) -> Point:
    """Return the point straight above ``current``, capped at ``max_z_m``."""
    if lift_m <= 0.0:
        raise ValueError('lift must be positive')
    x, y, z = (float(v) for v in current)
    return (x, y, min(z + lift_m, max(z, max_z_m)))


@dataclass(frozen=True)
class ProbeLimits:
    """
    Conservative box for arm-probe targets, separate from MoveIt.

    MoveIt decides whether a point is reachable. These limits only keep the
    measurement tool away from the robot body and the table surface.
    """

    surface_z_m: float = DEFAULT_SURFACE_Z_M
    min_clearance_m: float = MIN_CLEARANCE_FLOOR_MM / 1000.0
    min_x_m: float = 0.100
    max_abs_y_m: float = 0.200
    max_z_m: float = 0.350


def probe_target_problems(
    point: Sequence[float],
    limits: ProbeLimits = ProbeLimits(),
) -> List[str]:
    """Return reasons why an arm-probe target is refused (empty if OK)."""
    x, y, z = (float(v) for v in point)
    problems = []
    if not all(math.isfinite(v) for v in (x, y, z)):
        return ['target must be finite']
    clearance = z - limits.surface_z_m
    if clearance < limits.min_clearance_m:
        problems.append(
            f'height above surface {m_to_mm(clearance):.0f} mm < '
            f'{m_to_mm(limits.min_clearance_m):.0f} mm'
        )
    if x < limits.min_x_m:
        problems.append(
            f'x {x:.3f} m < {limits.min_x_m:.3f} m (too close to the body)'
        )
    if abs(y) > limits.max_abs_y_m:
        problems.append(f'|y| {abs(y):.3f} m > {limits.max_abs_y_m:.3f} m')
    if z > limits.max_z_m:
        problems.append(f'z {z:.3f} m > {limits.max_z_m:.3f} m')
    if reach_from_shoulder_m(point) > ARM_MAX_REACH_M:
        problems.append(
            f'{reach_from_shoulder_m(point):.3f} m from the shoulder > '
            f'arm length {ARM_MAX_REACH_M:.3f} m'
        )
    return problems


def reach_from_shoulder_m(point: Sequence[float]) -> float:
    """Return the straight-line distance from joint2 to a base_link point."""
    x, y, z = (float(v) for v in point)
    radius = math.hypot(x - JOINT1_XY_M[0], y - JOINT1_XY_M[1])
    return math.hypot(radius, z - JOINT2_Z_M)


def parse_vision_log(text: str) -> Optional[Point]:
    """Extract the base_link point from control_node's vision log line."""
    match = _VISION_LOG.search(text)
    if match is None:
        return None
    return tuple(float(match.group(i)) for i in range(1, 4))


def parse_moveit_log(text: str) -> Optional[Tuple[str, Point]]:
    """Extract (label, target) from control_node's 'MoveIt X reached' log."""
    match = _MOVEIT_LOG.search(text)
    if match is None:
        return None
    return match.group(1), tuple(float(match.group(i)) for i in range(2, 5))


# --- Forward kinematics (URDF: turtlebot3_manipulation_description) -------

# URDF joint limits in degrees (joint1..joint4).
JOINT_LIMITS_DEG = (
    (-162.0, 162.0),
    (-102.6, 90.0),
    (-54.0, 79.2),
    (-102.6, 117.0),
)
# Camera mount (vision2.launch.py defaults): end_effector_link -> d435i_link.
CAMERA_MOUNT_XYZ_M = (-0.065, 0.0195, 0.052)


def _rot_z(angle: float):
    c, s = math.cos(angle), math.sin(angle)
    return ((c, -s, 0.0), (s, c, 0.0), (0.0, 0.0, 1.0))


def _rot_y(angle: float):
    c, s = math.cos(angle), math.sin(angle)
    return ((c, 0.0, s), (0.0, 1.0, 0.0), (-s, 0.0, c))


_IDENTITY = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def _compose(pose, rotation, offset):
    """Return pose * (rotation, offset) for (R, p) tuples."""
    r, p = pose
    new_p = tuple(p[i] + sum(r[i][k] * offset[k] for k in range(3))
                  for i in range(3))
    new_r = tuple(tuple(sum(r[i][k] * rotation[k][j] for k in range(3))
                        for j in range(3)) for i in range(3))
    return new_r, new_p


@dataclass(frozen=True)
class ArmPoints:
    """Hand and camera positions in base_link for one joint configuration."""

    ee: Point
    camera: Point
    camera_pitch_deg: float  # downward tilt of the camera view axis


def forward_kinematics(joints_deg: Sequence[float]) -> ArmPoints:
    """Return end_effector_link and d435i_link positions (URDF geometry)."""
    q = [math.radians(float(v)) for v in joints_deg]
    pose = (_IDENTITY, (0.0, 0.0, 0.0))
    pose = _compose(pose, _IDENTITY, (-0.092, 0.0, 0.091))   # link1
    pose = _compose(pose, _rot_z(q[0]), (0.012, 0.0, 0.017))  # joint1
    pose = _compose(pose, _rot_y(q[1]), (0.0, 0.0, 0.0595))   # joint2
    pose = _compose(pose, _rot_y(q[2]), (0.024, 0.0, 0.128))  # joint3
    pose = _compose(pose, _rot_y(q[3]), (0.124, 0.0, 0.0))    # joint4
    ee = _compose(pose, _IDENTITY, (0.126, 0.0, 0.0))
    camera = _compose(ee, _IDENTITY, CAMERA_MOUNT_XYZ_M)
    return _arm_points(ee, camera)


def _camera_pose(joints_deg: Sequence[float]):
    """Return (R, p) of d435i_link in base_link (x forward, z up)."""
    q = [math.radians(float(v)) for v in joints_deg]
    pose = (_IDENTITY, (0.0, 0.0, 0.0))
    pose = _compose(pose, _IDENTITY, (-0.092, 0.0, 0.091))
    pose = _compose(pose, _rot_z(q[0]), (0.012, 0.0, 0.017))
    pose = _compose(pose, _rot_y(q[1]), (0.0, 0.0, 0.0595))
    pose = _compose(pose, _rot_y(q[2]), (0.024, 0.0, 0.128))
    pose = _compose(pose, _rot_y(q[3]), (0.124, 0.0, 0.0))
    pose = _compose(pose, _IDENTITY, (0.126, 0.0, 0.0))
    return _compose(pose, _IDENTITY, CAMERA_MOUNT_XYZ_M)


def _arm_points(ee, camera) -> 'ArmPoints':
    forward_z = camera[0][2][0]
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, -forward_z))))
    return ArmPoints(ee[1], camera[1], pitch)


def jog_target_problems(
    joints_deg: Sequence[float],
    surface_z_m: float = DEFAULT_SURFACE_Z_M,
    min_clearance_m: float = MIN_CLEARANCE_FLOOR_MM / 1000.0,
    margin_deg: float = 1.5,
) -> List[str]:
    """
    Return reasons a jog target is refused (empty if allowed).

    Checks URDF limits with a margin, the hand and camera height above the
    surface, and keeps both above the robot body when they are over it.
    """
    problems = []
    for index, (value, (low, high)) in enumerate(
            zip(joints_deg, JOINT_LIMITS_DEG), start=1):
        if not low + margin_deg <= value <= high - margin_deg:
            problems.append(
                f'joint{index} {value:.1f}° outside '
                f'[{low + margin_deg:.1f}, {high - margin_deg:.1f}]')
    if problems:
        return problems
    points = forward_kinematics(joints_deg)
    for name, (x, _, z) in (('hand', points.ee), ('camera', points.camera)):
        if z - surface_z_m < min_clearance_m:
            problems.append(
                f'{name} {m_to_mm(z - surface_z_m):.0f} mm above surface '
                f'< {m_to_mm(min_clearance_m):.0f} mm')
        if x < 0.10 and z < 0.20:
            problems.append(
                f'{name} over the robot body too low (x {x:.3f}, z {z:.3f})')
    return problems


# D435i colour intrinsics at 1280x720 (camera_info, 2026-11-04).
CAMERA_INTRINSICS = (911.7, 911.5, 632.2, 373.0, 1280, 720)


def project_to_image(
    joints_deg: Sequence[float],
    point: Sequence[float],
    intrinsics: Sequence[float] = CAMERA_INTRINSICS,
) -> Optional[Tuple[float, float, float]]:
    """
    Return (column, row, depth_m) of a base_link point, or None if behind.

    Uses the URDF chain and the nominal camera mount; the real image can be
    offset by mount errors (about 100 rows were seen on 2026-11-04).
    """
    fx, fy, cx, cy = (float(v) for v in intrinsics[:4])
    rotation, origin = _camera_pose(joints_deg)
    d = [float(point[i]) - origin[i] for i in range(3)]
    # Point in d435i_link: x forward, y left, z up (R transposed).
    local = [sum(rotation[k][i] * d[k] for k in range(3)) for i in range(3)]
    if local[0] <= 0.0:
        return None
    return (cx - fx * local[1] / local[0], cy - fy * local[2] / local[0],
            local[0])


def sweep_candidates(
    target: Sequence[float],
    joint2_deg: Sequence[float],
    joint3_deg: Sequence[float],
    joint4_deg: Sequence[float],
    surface_z_m: float,
    first: Optional[Sequence[float]] = None,
    row_band: Tuple[float, float] = (120.0, 600.0),
) -> List[Tuple[float, float, float, float]]:
    """
    Return safe joint poses that put ``target`` inside the image row band.

    joint1 stays 0. Poses failing jog_target_problems are dropped. The list
    is ordered in a serpentine so consecutive poses differ by one step, and
    ``first`` (the current recognition pose) is placed at the front.
    """
    poses = []
    for i, j2 in enumerate(joint2_deg):
        j3_values = list(joint3_deg) if i % 2 == 0 else list(joint3_deg)[::-1]
        for k, j3 in enumerate(j3_values):
            j4_values = list(joint4_deg) if k % 2 == 0 else \
                list(joint4_deg)[::-1]
            for j4 in j4_values:
                pose = (0.0, float(j2), float(j3), float(j4))
                if jog_target_problems(pose, surface_z_m):
                    continue
                pixel = project_to_image(pose, target)
                if pixel is None:
                    continue
                column, row, _ = pixel
                if not (row_band[0] <= row <= row_band[1]
                        and 100.0 <= column <= 1180.0):
                    continue
                poses.append(pose)
    if first is not None:
        head = tuple(float(v) for v in first)
        poses = [head] + [p for p in poses if p != head]
    return poses
