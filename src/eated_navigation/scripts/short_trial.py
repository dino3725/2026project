#!/usr/bin/env python3
"""One supervised 50 cm Nav2 test with a heartbeat lock on the wheel mux."""

import json
import math
from pathlib import Path
import time

from action_msgs.msg import GoalStatus
from ament_index_python.packages import get_package_share_directory
from control_pkg.control_node import ControlNode, OperationCancelled
from control_pkg.task_state import TaskState
from nav2_msgs.action import NavigateToPose
from nav_msgs.msg import Odometry
import rclpy
from rclpy.action import ActionClient
from rclpy.executors import MultiThreadedExecutor
from rclpy.qos import qos_profile_sensor_data
from rclpy.time import Time
from sensor_msgs.msg import LaserScan
from std_msgs.msg import Bool
from std_srvs.srv import Trigger


def read_reviewed_route(filename):
    """Check the sampled path before enabling a single supervised corridor trial."""
    data = json.loads(Path(filename).read_text())
    path = data['path']
    goal = data['goal']
    if (not 2 <= len(path) <= 2000 or len(goal) != 3 or not all(map(math.isfinite, goal))
            or not all(len(p) == 2 and all(map(math.isfinite, p)) for p in path)):
        raise ValueError('Invalid corridor path or goal')
    steps = [math.dist(a, b) for a, b in zip(path, path[1:])]
    length = sum(steps)
    if (not 0 < length <= 6.0 or max(steps) > .105 or math.dist(path[-1], goal[:2]) > .07
            or not math.isfinite(data['clearance']) or data['clearance'] < .45):
        raise ValueError('Corridor path exceeds trial limits')
    return path, goal, length


class Trial(ControlNode):
    """Only an explicit trigger can start motion. A stop spends the trial."""

    def __init__(self):
        self.moving = False
        self.spent = False
        self.joy_at = self.scan_at = self.odom_at = 0.
        self.odom_xy = None
        self.odom_travel = 0.
        super().__init__()
        self.declare_parameter('resume_original_goal', False)
        self.declare_parameter('original_goal_xy_yaw', [0., 0., 0.])
        route_file = self.declare_parameter('reviewed_route_file', '').value
        self.route = read_reviewed_route(route_file) if route_file else None
        if self.velocity_publisher.topic_name != '/navigation/trial_stop':
            raise RuntimeError('Trial stop output must be remapped to the mux')
        self.lock_pub = self.create_publisher(Bool, '/navigation/locked', 10)
        self.navigator = ActionClient(self, NavigateToPose, '/navigate_to_pose',
                                      callback_group=self.callback_group)
        self.create_subscription(LaserScan, '/scan', self.scan, qos_profile_sensor_data)
        self.create_subscription(Odometry, '/odom', self.odom, qos_profile_sensor_data)
        self.create_timer(.05, self.guard)
        service = '/navigation/start_corridor_trial' if self.route else '/navigation/start_short_trial'
        self.create_service(Trigger, service, self.start)

    def odom(self, msg):
        p = msg.pose.pose.position
        if math.isfinite(p.x) and math.isfinite(p.y):
            if self.moving and self.odom_xy is not None:
                self.odom_travel += math.dist(self.odom_xy, (p.x, p.y))
            self.odom_xy = (p.x, p.y)
            self.odom_at = time.monotonic()

    def scan(self, msg):
        if sum(math.isfinite(r) and msg.range_min <= r <= msg.range_max
               for r in msg.ranges) > 10:
            self.scan_at = time.monotonic()

    def joy_callback(self, msg):
        neutral = (len(msg.buttons) >= 4 and len(msg.axes) >= 2 and
                   all(abs(a) < .08 for a in msg.axes[:2]) and not any(msg.buttons))
        self.joy_at = time.monotonic() if neutral else 0.
        if (self.moving and not neutral) or (len(msg.buttons) >= 4 and msg.buttons[3]):
            self.stop_trial('F710')

    def command_callback(self, msg):
        if msg.action == 'stop':
            self.stop_trial('voice')

    def stop_trial(self, reason):
        with self._lock:
            if self.spent and not self.moving and self._stop_event.is_set():
                return
            self.spent = True
            self.moving = False
            self._handle_emergency_stop(reason)

    def guard(self):
        with self._lock:
            if self.moving:
                if not self.fresh():
                    self.stop_trial('lost sensor feedback')
                elif self.route:
                    try:
                        tf = self.tf_buffer.lookup_transform('map', 'base_link', Time())
                        age = (self.get_clock().now() - Time.from_msg(tf.header.stamp)).nanoseconds / 1e9
                        point = tf.transform.translation
                        offset = min(math.dist((point.x, point.y), p) for p in self.route[0])
                        if not -1.2 <= age <= .5 or not offset <= .18:
                            self.stop_trial('lost localization or left the reviewed corridor path')
                        elif self.odom_travel > self.route[2] + .45:
                            self.stop_trial('exceeded reviewed corridor travel distance')
                    except Exception as exc:
                        self.stop_trial(str(exc))
                elif math.dist(self.odom_xy, self.start_xy) > .65:
                    self.stop_trial('exceeded short-trial distance')
            self.lock_pub.publish(Bool(data=not self.moving))
            if not self.moving:
                self._publish_zero_velocity()

    def fresh(self):
        return time.monotonic() - min(self.joy_at, self.scan_at, self.odom_at,
                                      self._joint_received_at) < .5

    def start(self, request, response):
        with self._lock:
            if (self.spent or self.state != TaskState.IDLE or not self.fresh()
                    or not self.navigator.server_is_ready()):
                response.message = 'Requires ready Nav2, HOME, neutral fresh inputs and unused trial'
                return response
            # Humble remaps graph queries too. Check the actual DDS wheel wire.
            info = self.get_publishers_info_by_topic('rt/cmd_vel', no_mangle=True)
            if len(info) != 1 or info[0].node_name != 'twist_mux':
                response.message = 'Wheel input must have exactly one publisher: twist_mux'
                return response
            subscribers = self.get_subscriptions_info_by_topic('rt/cmd_vel', no_mangle=True)
            if not any(sub.node_name == 'diff_drive_controller' for sub in subscribers):
                response.message = 'Wheel controller is disconnected from twist_mux'
                return response
            try:
                tf = self.tf_buffer.lookup_transform('map', 'base_link', Time())
                age = (self.get_clock().now()-Time.from_msg(tf.header.stamp)).nanoseconds/1e9
                if not -1.2 <= age <= .5:
                    raise ValueError('Stale localization')
            except Exception as exc:
                response.message = str(exc)
                return response
            q = tf.transform.rotation
            yaw = math.atan2(2*(q.w*q.z+q.x*q.y), 1-2*(q.y*q.y+q.z*q.z))
            goal = NavigateToPose.Goal()
            goal.pose.header.frame_id = 'map'
            goal.pose.header.stamp = self.get_clock().now().to_msg()
            goal.pose.pose.position.x = tf.transform.translation.x + .5*math.cos(yaw)
            goal.pose.pose.position.y = tf.transform.translation.y + .5*math.sin(yaw)
            if self.route:
                point = tf.transform.translation
                if math.dist((point.x, point.y), self.route[0][0]) > .10:
                    response.message = 'Robot has moved from the reviewed route start'
                    return response
                x, y, yaw = self.route[1]
                goal.pose.pose.position.x = x
                goal.pose.pose.position.y = y
            elif self.get_parameter('resume_original_goal').value:
                x, y, yaw = self.get_parameter('original_goal_xy_yaw').value
                current = (tf.transform.translation.x, tf.transform.translation.y)
                if not all(map(math.isfinite, (x, y, yaw))) or math.dist(current, (x, y)) > .5:
                    response.message = 'Saved goal must be within the short trial distance'
                    return response
                goal.pose.pose.position.x = x
                goal.pose.pose.position.y = y
            goal.pose.pose.orientation.z = math.sin(yaw/2)
            goal.pose.pose.orientation.w = math.cos(yaw/2)
            goal.behavior_tree = str(Path(get_package_share_directory('eated_navigation'))
                                     / 'config/short_trial.xml')
            self.start_xy = self.odom_xy
            self.spent = True
            self.moving = True
            self._start_operation(lambda op: self.navigate(goal, op))
            response.success = True
            response.message = f'Goal requested at {goal.pose.pose.position}'
            return response

    def navigate(self, goal, op):
        try:
            handle = self._send_goal(self.navigator, goal, op, 'short navigation')
            timeout = 15. + self.route[2] / .04 if self.route else 25.
            result = self._wait_result(handle, op, 'navigation', timeout)
            with self._lock:
                self._ensure_active(op)
                self.moving = False
                self._publish_zero_velocity()
                self.get_logger().info(
                    f'Nav2 status={result.status} success={result.status == GoalStatus.STATUS_SUCCEEDED} '
                    f'odom displacement={math.dist(self.start_xy, self.odom_xy):.3f}m')
        except OperationCancelled:
            return
        except Exception as exc:
            self.stop_trial(str(exc))


def main():
    rclpy.init()
    node = Trial()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        if rclpy.ok():
            node.stop_trial('shutdown')
        executor.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
