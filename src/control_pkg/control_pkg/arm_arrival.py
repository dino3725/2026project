"""Check measured arm arrival without trusting an action success flag."""

import math


def joint_feedback_error(actual, expected):
    """Validate joint feedback and its target without a deviation limit."""
    if actual is None or len(actual) != 4 or len(expected) != 4:
        return 'Missing complete arm joint state or target'
    if not all(math.isfinite(x) for x in (*actual, *expected)):
        return 'Nonfinite arm state or target'
    return ''


def joint_arrival_error(actual, expected, tolerance):
    """Return a failure reason unless all four finite joints are in range."""
    limits = ([tolerance] * 4 if isinstance(tolerance, (int, float))
              else list(tolerance))
    if len(limits) != 4 or not all(math.isfinite(x) and x > 0 for x in limits):
        raise ValueError('Joint arrival tolerance must be finite and positive')
    reason = joint_feedback_error(actual, expected)
    if reason:
        return reason
    error = max(abs(a - b) for a, b in zip(actual, expected))
    if any(abs(a - b) > limit for a, b, limit in zip(actual, expected, limits)):
        details = ', '.join(
            f'joint{i + 1}: target={math.degrees(b):.3f}, '
            f'actual={math.degrees(a):.3f}, error={math.degrees(a - b):+.3f}'
            for i, (a, b) in enumerate(zip(actual, expected)))
        limit_text = ','.join(f'{math.degrees(x):.3f}' for x in limits)
        return (f'Joint arrival error {math.degrees(error):.3f} deg '
                f'(limits=[{limit_text}]; {details})')
    return ''


def trajectory_endpoint(trajectory, names):
    """Map an executed arm trajectory endpoint into the controller order."""
    if (not trajectory.points or len(trajectory.joint_names) != len(names)
            or len(set(trajectory.joint_names)) != len(names)):
        raise ValueError('Missing or ambiguous executed arm trajectory')
    if set(trajectory.joint_names) != set(names):
        raise ValueError('Executed trajectory has unexpected joints')
    positions = trajectory.points[-1].positions
    if len(positions) != len(names) or not all(map(math.isfinite, positions)):
        raise ValueError('Invalid executed trajectory endpoint')
    by_name = dict(zip(trajectory.joint_names, positions))
    return [by_name[name] for name in names]
