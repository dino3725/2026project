"""Compare board RGB-PnP plane with synchronized aligned depth; offline only."""
import sys,json,cv2,numpy as np
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tools/vision_eval'))
from handeye import transform
root=Path(__file__).parent
candidate=json.loads((root/'candidate.json').read_text())
new=np.array(candidate['T_ee_optical'])
rows=[]
for path in sorted(root.glob('*/observation.json')):
    d=json.loads(path.read_text())
    if d.get('status')!='accepted_observation_not_calibration':continue
    depth=cv2.imread(str(path.parent/'depth.png'),cv2.IMREAD_UNCHANGED)
    h,w=depth.shape;k=np.array(d['color_info']['k']).reshape(3,3)
    hull=cv2.convexHull(np.array(d['corners_px'],dtype=np.float32)).astype(np.int32)
    mask=np.zeros((h,w),np.uint8);cv2.fillConvexPoly(mask,hull,255)
    mask=cv2.erode(mask,np.ones((9,9),np.uint8))
    vv,uu=np.where((mask>0)&(depth>200)&(depth<1200))
    uu,vv=uu[::4],vv[::4]
    z=depth[vv,uu].astype(float)*.001
    points=np.column_stack(((uu-k[0,2])*z/k[0,0],(vv-k[1,2])*z/k[1,1],z))
    keep=np.ones(len(points),dtype=bool)
    for _ in range(5):
        center=points[keep].mean(axis=0);_,_,vh=np.linalg.svd(points[keep]-center,full_matrices=False)
        normal=vh[-1]
        errors=(points-center)@normal
        mad=np.median(np.abs(errors[keep]-np.median(errors[keep])))
        keep=np.abs(errors)<max(.0025,3*1.4826*mad)
    plane_offset=-normal@center
    ob=np.array(d['T_optical_board']);pnp_center=(ob@np.array([.0875,.0625,0,1]))[:3]
    pnp_normal=ob[:3,2]
    if normal@pnp_normal<0:normal=-normal;plane_offset=-plane_offset
    ray=pnp_center/pnp_center[2];depth_center=ray*(-plane_offset/(normal@ray))
    ee=np.array(d['T_base_ee']);old=transform(np.eye(3),[-.065,.0195,.052])@d['T_camera_optical']
    row={'pose':path.parent.name,'points':int(keep.sum()),'depth_plane_rms_mm':float(np.sqrt(np.mean(errors[keep]**2))*1000),'pnp_depth_plane_center_distance_mm':float((normal@pnp_center+plane_offset)*1000),'pnp_depth_plane_normal_angle_deg':float(np.degrees(np.arccos(np.clip(normal@pnp_normal,-1,1)))),'pnp_center_optical_z_mm':float(pnp_center[2]*1000),'depth_center_optical_z_mm':float(depth_center[2]*1000)}
    for label,x in [('original',old),('candidate',new)]:
        bc=ee@x@np.r_[depth_center,1]
        bn=(ee@x)[:3,:3]@normal
        row[label+'_depth_board_center_above_support_mm']=float((bc[2]+.01)*1000)
        row[label+'_depth_board_tilt_deg']=float(np.degrees(np.arccos(np.clip(abs(bn[2]),-1,1))))
    rows.append(row)
out={'method':'Iteratively clipped least-squares plane inside detected inner-corner hull, 9px erosion, depth 0.2..1.2m; 16UC1 interpreted as mm. Compare at ray through PnP board center. No fitting of new mount.','rows':rows}
with (root/'depth_plane_review.json').open('x') as f:json.dump(out,f,indent=2);f.write('\n')
for r in rows:print(r['pose'],'PnP-depth plane mm',round(r['pnp_depth_plane_center_distance_mm'],2),'normal deg',round(r['pnp_depth_plane_normal_angle_deg'],2),'depth board height original/candidate mm',round(r['original_depth_board_center_above_support_mm'],2),round(r['candidate_depth_board_center_above_support_mm'],2))
for key in ['pnp_depth_plane_center_distance_mm','pnp_depth_plane_normal_angle_deg','original_depth_board_center_above_support_mm','candidate_depth_board_center_above_support_mm','candidate_depth_board_tilt_deg']:
 vals=[r[key] for r in rows];print(key,'mean/min/max',np.mean(vals),min(vals),max(vals))
