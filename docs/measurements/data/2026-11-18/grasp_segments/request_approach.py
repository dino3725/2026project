"""Send one authorized reviewed approach request, with live preconditions."""
from pathlib import Path
import hashlib,json,time
import xml.etree.ElementTree as ET
import cv2,rclpy
from cv_bridge import CvBridge
from std_msgs.msg import String
from sensor_msgs.msg import Image,JointState,Joy
from std_srvs.srv import Trigger
from rcl_interfaces.srv import GetParameters
from rclpy.qos import qos_profile_sensor_data,QoSProfile,DurabilityPolicy
from eated_interfaces.msg import RobotCommand
root=Path('log/grasp_segments_20261118'); destination=root/'approach_execution'
if (destination/'request.json').exists(): raise RuntimeError('Request already attempted')
rclpy.init();n=rclpy.create_node('request_one_reviewed_approach');received={};at={};record={};requested=False
latched=QoSProfile(depth=1,durability=DurabilityPolicy.TRANSIENT_LOCAL)
def save(k,msg):received[k]=msg;at[k]=time.monotonic()
for typ,topic,key,qos in [(String,'/robot_description','model',latched),(String,'/measurement/model_approach_state','state',latched),(Image,'/realsense/d435i/color/image_raw','rgb',qos_profile_sensor_data),(JointState,'/joint_states','joints',qos_profile_sensor_data),(Joy,'/joy','joy',qos_profile_sensor_data)]:
 n.create_subscription(typ,topic,lambda msg,k=key:save(k,msg),qos)
client=n.create_client(Trigger,'/measurement/execute_model_approach')
params=n.create_client(GetParameters,'/model_approach_control/get_parameters')
stop=n.create_publisher(RobotCommand,'/voice/robot_command',10)
old_state={}
n.create_subscription(String,'/control/state',lambda m:old_state.update(state=m.data),latched)
try:
 deadline=time.monotonic()+15
 while time.monotonic()<deadline:
  rclpy.spin_once(n,timeout_sec=.05)
  if len(received)==5 and 'state' in old_state and client.service_is_ready() and params.service_is_ready() and stop.get_subscription_count()>=2 and all(time.monotonic()-at[k]<.5 for k in ['rgb','joints','joy']):break
 else:raise RuntimeError('Live inputs/controller missing')
 plan=json.loads((destination/'plan.json').read_text())
 expected_model=Path('log/measured_model_20261111/measured.urdf').read_text()
 if hashlib.sha256(expected_model.encode()).hexdigest()!=plan['model_sha256'] or ET.canonicalize(received['model'].data,strip_text=True)!=ET.canonicalize(expected_model,strip_text=True):raise RuntimeError('Active robot model mismatch')
 record['active_model_raw_sha256']=hashlib.sha256(received['model'].data.encode()).hexdigest()
 record['active_model_canonical_equal']=True
 if received['state'].data!='IDLE' or old_state.get('state')!='ERROR':raise RuntimeError('Recovery state/old controller stop not confirmed')
 if any(received['joy'].buttons):raise RuntimeError('Joystick button held')
 future=params.call_async(GetParameters.Request(names=['pick_enabled','home_on_startup']))
 rclpy.spin_until_future_complete(n,future,timeout_sec=3)
 if not future.done() or future.result() is None or any(v.type!=1 or v.bool_value for v in future.result().values) or len(future.result().values)!=2:raise RuntimeError('Wrong control mode')
 cv2.imwrite(str(root/'before_model_approach_rgb.png'),CvBridge().imgmsg_to_cv2(received['rgb'],'bgr8'))
 record.update(before_joints=dict(zip(received['joints'].name,received['joints'].position)),model_sha256=plan['model_sha256'],sent_wall_time=time.time())
 (destination/'request.json').write_text(json.dumps(record,indent=2)+'\n')
 requested=True
 future=client.call_async(Trigger.Request())
 rclpy.spin_until_future_complete(n,future,timeout_sec=4)
 if not future.done() or future.result() is None:raise RuntimeError('Request outcome unknown')
 response=future.result();record['acceptance']={'success':response.success,'message':response.message}
 print(record['acceptance'],flush=True)
 if not response.success:
  requested=False;raise RuntimeError(response.message)
 deadline=time.monotonic()+25
 while time.monotonic()<deadline:
  rclpy.spin_once(n,timeout_sec=.05)
  result=destination/'execution.json'
  if result.exists():
   progress=json.loads(result.read_text())
   if progress.get('completed'):
    record['result']=progress;requested=False;print('Approach completed and holding',flush=True);break
   if progress.get('error'):
    record['result']=progress;raise RuntimeError(progress['error'])
 else:raise RuntimeError('Execution completion timeout')
except Exception as exc:
 record['error']=str(exc)
 if requested:
  stop.publish(RobotCommand(action='stop',transcript='reviewed approach request monitor failure'))
  until=time.monotonic()+.5
  while time.monotonic()<until:rclpy.spin_once(n,timeout_sec=.05)
  record['stop_requested']=True
 raise
finally:
 (destination/'request_outcome.json').write_text(json.dumps(record,indent=2)+'\n')
 n.destroy_node();rclpy.shutdown()
