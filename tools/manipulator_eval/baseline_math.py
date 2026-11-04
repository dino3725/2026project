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


@dataclass(frozen=True)
class ProbeLimits:
    """
    Conservative box for arm-probe targets, separate from MoveIt.

    MoveIt decides whether a point is reachable. These limits only keep the
    measurement tool away from the robot body and the table surface.
    """

    surface_z_m: float = DEFAULT_SURFACE_Z_M
    min_clearance_m: float = 0.030
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
