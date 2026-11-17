"""Request only the isolated recognition action; no control or motion publishers."""
import json,time
from pathlib import Path
import rclpy
from rclpy.action import ActionClient
from eated_interfaces.action import FindObject
rclpy.init();node=rclpy.create_node('candidate_recognition_audit_client')
client=ActionClient(node,FindObject,'/measurement/find_object_candidate')
rows=[];handle=None
try:
 if not client.wait_for_server(timeout_sec=30.): raise RuntimeError('Isolated vision server unavailable')
 for i in range(2):
  feedback=[];start=time.monotonic();goal=FindObject.Goal();goal.target_class='coke'
  future=client.send_goal_async(goal,feedback_callback=lambda m:feedback.append({'stable_time':float(m.feedback.stable_time),'detected':bool(m.feedback.detected)}))
  rclpy.spin_until_future_complete(node,future,timeout_sec=15.)
  if not future.done():raise RuntimeError('No goal response')
  handle=future.result()
  if not handle.accepted:raise RuntimeError('Goal rejected')
  future=handle.get_result_async();rclpy.spin_until_future_complete(node,future,timeout_sec=65.)
  if not future.done():raise RuntimeError('Recognition result timeout')
  result=future.result();r=result.result
  row={'trial':i+1,'action_status':result.status,'success':r.success,'point':[r.x,r.y,r.z],'confidence':r.confidence,'frame':r.frame_id,'message':r.message,'elapsed_sec':time.monotonic()-start,'feedback':feedback};rows.append(row);print(json.dumps(row),flush=True);handle=None
  if not r.success:break
finally:
 if handle is not None:
  future=handle.cancel_goal_async();rclpy.spin_until_future_complete(node,future,timeout_sec=3.)
 Path('log/grasp_localization_20261118/results.json').write_text(json.dumps(rows,indent=2)+'\n')
 node.destroy_node();rclpy.shutdown()
