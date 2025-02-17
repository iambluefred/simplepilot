import numpy as np
import cv2
import math
import laika.orientation as orientation
import onnx
import onnxruntime
plot_img_width=640
plot_img_height=480

FULL_FRAME_SIZE = (1164, 874)
W, H = FULL_FRAME_SIZE[0], FULL_FRAME_SIZE[1]
zoom = FULL_FRAME_SIZE[0] / plot_img_width
eon_focal_length = FOCAL = 910.0

zoom = FULL_FRAME_SIZE[0] / plot_img_width
CALIB_BB_TO_FULL = np.asarray([
    [zoom, 0., 0.],
    [0., zoom, 0.],
    [0., 0., 1.]])
X_IDXs = [
    0.,   0.1875,   0.75,   1.6875,   3.,   4.6875,
    6.75,   9.1875,  12.,  15.1875,  18.75,  22.6875,
    27.,  31.6875,  36.75,  42.1875,  48.,  54.1875,
    60.75,  67.6875,  75.,  82.6875,  90.75,  99.1875,
    108., 117.1875, 126.75, 136.6875, 147., 157.6875,
    168.75, 180.1875, 192.]


# MED model
MEDMODEL_INPUT_SIZE = (512, 256)
MEDMODEL_YUV_SIZE = (MEDMODEL_INPUT_SIZE[0], MEDMODEL_INPUT_SIZE[1] * 3 // 2)
MEDMODEL_CY = 47.6

medmodel_zoom = 1.
medmodel_intrinsics = np.array(
  [[ eon_focal_length / medmodel_zoom,    0. ,  0.5 * MEDMODEL_INPUT_SIZE[0]],
   [   0. ,  eon_focal_length / medmodel_zoom,  MEDMODEL_CY],
   [   0. ,                            0. ,   1.]])

# aka 'K' aka camera_frame_from_view_frame
eon_intrinsics = np.array([
    [FOCAL,   0.,   W/2.],
    [0.,  FOCAL,  H/2.],
    [0.,    0.,     1.]])


def transform_frames(frames):
    imgs_med_model = np.zeros((len(frames), 384, 512), dtype=np.uint8)
    for i, img in enumerate(frames):
        if img.any():
          imgs_med_model[i] = transform_img(img, 
                                            from_intr=eon_intrinsics,
                                            to_intr=medmodel_intrinsics, 
                                            yuv=True,
                                            output_size=(512, 256))

    reshaped = reshape_yuv(imgs_med_model)

    return reshaped

#https://github.com/commaai/openpilot/blob/master/common/transformations/camera.py#L104
# latest version doesn't have this fuction, but it's used in v0.7
def normalize(img_pts, intrinsics=eon_intrinsics):
  # normalizes image coordinates
  # accepts single pt or array of pts
  intrinsics_inv = np.linalg.inv(intrinsics)
  img_pts = np.array(img_pts)
  input_shape = img_pts.shape
  img_pts = np.atleast_2d(img_pts)
  img_pts = np.hstack((img_pts, np.ones((img_pts.shape[0],1))))
  img_pts_normalized = img_pts.dot(intrinsics_inv.T)
  img_pts_normalized[(img_pts < 0).any(axis=1)] = np.nan
  return img_pts_normalized[:,:2].reshape(input_shape)


# https://github.com/commaai/openpilot/blob/v0.7/common/transformations/camera.py
# latest version doesn't have this fuction, but it's used in v0.7
def transform_img(base_img,
                 augment_trans=np.array([0,0,0]),
                 augment_eulers=np.array([0,0,0]),
                 from_intr=eon_intrinsics,
                 to_intr=eon_intrinsics,
                 output_size=None,
                 pretransform=None,
                 top_hacks=False,
                 yuv=False,
                 alpha=1.0,
                 beta=0,
                 blur=0):
  import cv2  # pylint: disable=import-error
  cv2.setNumThreads(1)

  if yuv:
    base_img = cv2.cvtColor(base_img, cv2.COLOR_YUV2RGB_I420)

  size = base_img.shape[:2]
  if not output_size:
    output_size = size[::-1]

  cy = from_intr[1,2]
  def get_M(h=1.22):
    quadrangle = np.array([[0, cy + 20],
                           [size[1]-1, cy + 20],
                           [0, size[0]-1],
                           [size[1]-1, size[0]-1]], dtype=np.float32)
    quadrangle_norm = np.hstack((normalize(quadrangle, intrinsics=from_intr), np.ones((4,1))))
    quadrangle_world = np.column_stack((h*quadrangle_norm[:,0]/quadrangle_norm[:,1],
                                        h*np.ones(4),
                                        h/quadrangle_norm[:,1]))
    rot = orientation.rot_from_euler(augment_eulers)
    to_extrinsics = np.hstack((rot.T, -augment_trans[:,None]))
    to_KE = to_intr.dot(to_extrinsics)
    warped_quadrangle_full = np.einsum('jk,ik->ij', to_KE, np.hstack((quadrangle_world, np.ones((4,1)))))
    warped_quadrangle = np.column_stack((warped_quadrangle_full[:,0]/warped_quadrangle_full[:,2],
                                         warped_quadrangle_full[:,1]/warped_quadrangle_full[:,2])).astype(np.float32)
    M = cv2.getPerspectiveTransform(quadrangle, warped_quadrangle.astype(np.float32))
    return M

  M = get_M()
  if pretransform is not None:
    M = M.dot(pretransform)
  augmented_rgb = cv2.warpPerspective(base_img, M, output_size, borderMode=cv2.BORDER_REPLICATE)

  if top_hacks:
    cyy = int(math.ceil(to_intr[1,2]))
    M = get_M(1000)
    if pretransform is not None:
      M = M.dot(pretransform)
    augmented_rgb[:cyy] = cv2.warpPerspective(base_img, M, (output_size[0], cyy), borderMode=cv2.BORDER_REPLICATE)

  # brightness and contrast augment
  augmented_rgb = np.clip((float(alpha)*augmented_rgb + beta), 0, 255).astype(np.uint8)

  # gaussian blur
  if blur > 0:
    augmented_rgb = cv2.GaussianBlur(augmented_rgb,(blur*2+1,blur*2+1),cv2.BORDER_DEFAULT)

  if yuv:
    augmented_img = cv2.cvtColor(augmented_rgb, cv2.COLOR_RGB2YUV_I420)
  else:
    augmented_img = augmented_rgb
  return augmented_img



def reshape_yuv(frames):
    H = (frames.shape[1]*2)//3
    W = frames.shape[2]
    in_img1 = np.zeros((frames.shape[0], 6, H//2, W//2), dtype=np.uint8)

    in_img1[:, 0] = frames[:, 0:H:2, 0::2]
    in_img1[:, 1] = frames[:, 1:H:2, 0::2]
    in_img1[:, 2] = frames[:, 0:H:2, 1::2]
    in_img1[:, 3] = frames[:, 1:H:2, 1::2]
    in_img1[:, 4] = frames[:, H:H+H//4].reshape((-1, H//2, W//2))
    in_img1[:, 5] = frames[:, H+H//4:H+H//2].reshape((-1, H//2, W//2))
    return in_img1

def create_image_canvas(img_rgb, zoom_matrix, plot_img_height, plot_img_width):
    '''Transform with a correct warp/zoom transformation.'''
    img_plot = np.zeros((plot_img_height, plot_img_width, 3), dtype='uint8')
    cv2.warpAffine(img_rgb, zoom_matrix[:2], (img_plot.shape[1], img_plot.shape[0]), dst=img_plot, flags=cv2.WARP_INVERSE_MAP)
    return img_plot

def bgr_to_yuv(bgr):
    yuv = cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV_I420)
    assert yuv.shape == ((874*3//2, 1164))
    return yuv


def load_inference_model(path_to_model):

    if path_to_model.endswith('.onnx'):
        onnx_graph = onnx.load(path_to_model)
        output_names = [node.name for node in onnx_graph.graph.output]
        model = onnxruntime.InferenceSession(path_to_model, providers=['CPUExecutionProvider'])

        def run_model(inputs):
            outs =  model.run(output_names, inputs)[0]
            recurrent_state = outs[:, -512:]
            return outs, recurrent_state


    return model, run_model

def sigmoid(x):
    return 1 / (1 + np.exp(-x))


def extract_preds(outputs, best_plan_only=True):
    # N is batch_size

    plan_start_idx = 0
    plan_end_idx = 4955

    lanes_start_idx = plan_end_idx
    lanes_end_idx = lanes_start_idx + 528

    lane_lines_prob_start_idx = lanes_end_idx
    lane_lines_prob_end_idx = lane_lines_prob_start_idx + 8

    road_start_idx = lane_lines_prob_end_idx
    road_end_idx = road_start_idx + 264

    # plan
    plan = outputs[:, plan_start_idx:plan_end_idx]  # (N, 4955)
    plans = plan.reshape((-1, 5, 991))  # (N, 5, 991)
    plan_probs = plans[:, :, -1]  # (N, 5)
    plans = plans[:, :, :-1].reshape(-1, 5, 2, 33, 15)  # (N, 5, 2, 33, 15)
    best_plan_idx = np.argmax(plan_probs, axis=1)[0]  # (N,)
    best_plan = plans[:, best_plan_idx, ...]  # (N, 2, 33, 15)

    # lane lines
    lane_lines = outputs[:, lanes_start_idx:lanes_end_idx]  # (N, 528)
    lane_lines_deflat = lane_lines.reshape((-1, 2, 264))  # (N, 2, 264)
    lane_lines_means = lane_lines_deflat[:, 0, :]  # (N, 264)
    lane_lines_means = lane_lines_means.reshape(-1, 4, 33, 2)  # (N, 4, 33, 2)

    outer_left_lane = lane_lines_means[:, 0, :, :]  # (N, 33, 2)
    inner_left_lane = lane_lines_means[:, 1, :, :]  # (N, 33, 2)
    inner_right_lane = lane_lines_means[:, 2, :, :]  # (N, 33, 2)
    outer_right_lane = lane_lines_means[:, 3, :, :]  # (N, 33, 2)

    # lane lines probs
    lane_lines_probs = outputs[:, lane_lines_prob_start_idx:lane_lines_prob_end_idx]  # (N, 8)
    lane_lines_probs = lane_lines_probs.reshape((-1, 4, 2))  # (N, 4, 2)
    lane_lines_probs = sigmoid(lane_lines_probs[:, :, 1])  # (N, 4), 0th is deprecated

    outer_left_prob = lane_lines_probs[:, 0]  # (N,)
    inner_left_prob = lane_lines_probs[:, 1]  # (N,)
    inner_right_prob = lane_lines_probs[:, 2]  # (N,)
    outer_right_prob = lane_lines_probs[:, 3]  # (N,)

    # road edges
    road_edges = outputs[:, road_start_idx:road_end_idx]
    road_edges_deflat = road_edges.reshape((-1, 2, 132))  # (N, 2, 132)
    road_edge_means = road_edges_deflat[:, 0, :].reshape(-1, 2, 33, 2)  # (N, 2, 33, 2)
    road_edge_stds = road_edges_deflat[:, 1, :].reshape(-1, 2, 33, 2)  # (N, 2, 33, 2)

    left_edge = road_edge_means[:, 0, :, :]  # (N, 33, 2)
    right_edge = road_edge_means[:, 1, :, :]
    left_edge_std = road_edge_stds[:, 0, :, :]  # (N, 33, 2)
    right_edge_std = road_edge_stds[:, 1, :, :]

    batch_size = best_plan.shape[0]

    result_batch = []

    for i in range(batch_size):
        lanelines = [outer_left_lane[i], inner_left_lane[i], inner_right_lane[i], outer_right_lane[i]]
        lanelines_probs = [outer_left_prob[i], inner_left_prob[i], inner_right_prob[i], outer_right_prob[i]]
        road_edges = [left_edge[i], right_edge[i]]
        road_edges_probs = [left_edge_std[i], right_edge_std[i]]

        if best_plan_only:
            plan = best_plan[i]
        else:
            plan = (plans[i], plan_probs[i])

        result_batch.append(((lanelines, lanelines_probs), (road_edges, road_edges_probs), plan))

    return result_batch

def project_path(path, calibration, z_off):
    '''Projects paths from calibration space (model input/output) to image space.'''

    x = path[:, 0]
    y = path[:, 1]
    z = path[:, 2] + z_off
    pts = calibration.car_space_to_bb(x, y, z)
    pts[pts < 0] = np.nan
    valid = np.isfinite(pts).all(axis=1)
    pts = pts[valid].astype(int)

    return pts

def draw_path(lane_lines, road_edges, path_plan, img_plot, calibration, lane_line_color_list, width=1, height=1.22, fill_color=(128, 0, 255), line_color=(0, 255, 0)):
    
    '''Draw model predictions on an image.'''    

    overlay = img_plot.copy()
    alpha = 0.4
    fixed_distances = np.array(X_IDXs)[:,np.newaxis]

    # lane_lines are sequentially parsed ::--> means--> std's
    if lane_lines is not None:
        (oll, ill, irl, orl), (oll_prob, ill_prob, irl_prob, orl_prob) = lane_lines

        calib_pts_oll = np.hstack((fixed_distances, oll)) # (33, 3)
        calib_pts_ill = np.hstack((fixed_distances, ill)) # (33, 3)
        calib_pts_irl = np.hstack((fixed_distances, irl)) # (33, 3)
        calib_pts_orl = np.hstack((fixed_distances, orl)) # (33, 3)

        img_pts_oll = project_path(calib_pts_oll, calibration, z_off=0).reshape(-1,1,2)
        img_pts_ill = project_path(calib_pts_ill, calibration, z_off=0).reshape(-1,1,2)
        img_pts_irl = project_path(calib_pts_irl, calibration, z_off=0).reshape(-1,1,2)
        img_pts_orl = project_path(calib_pts_orl, calibration, z_off=0).reshape(-1,1,2)

        lane_lines_with_probs = [(img_pts_oll, oll_prob), (img_pts_ill, ill_prob), (img_pts_irl, irl_prob), (img_pts_orl, orl_prob)]

         # plot lanelines
        for i, (line_pts, prob) in enumerate(lane_lines_with_probs):
            line_overlay = overlay.copy()
            cv2.polylines(line_overlay,[line_pts],False,lane_line_color_list[i],thickness=2)
            img_plot = cv2.addWeighted(line_overlay, prob, img_plot, 1 - prob, 0)

    # road edges
    if road_edges is not None:
        (left_road_edge, right_road_edge), _ = road_edges

        calib_pts_ledg = np.hstack((fixed_distances, left_road_edge))
        calib_pts_redg = np.hstack((fixed_distances, right_road_edge))
        
        img_pts_ledg = project_path(calib_pts_ledg, calibration, z_off=0).reshape(-1,1,2)
        img_pts_redg = project_path(calib_pts_redg, calibration, z_off=0).reshape(-1,1,2)

        # plot road_edges
        cv2.polylines(overlay,[img_pts_ledg],False,(255,128,0),thickness=1)
        cv2.polylines(overlay,[img_pts_redg],False,(255,234,0),thickness=1)
    
    # path plan
    if path_plan is not None:

        path_plan_l = path_plan - np.array([0, width, 0])
        path_plan_r = path_plan + np.array([0, width, 0])
    
        img_pts_l = project_path(path_plan_l, calibration, z_off=height)
        img_pts_r = project_path(path_plan_r, calibration, z_off=height)

        for i in range(1, len(img_pts_l)):
            if i >= len(img_pts_r): break

            u1, v1, u2, v2 = np.append(img_pts_l[i-1], img_pts_r[i-1])
            u3, v3, u4, v4 = np.append(img_pts_l[i], img_pts_r[i])
            pts = np.array([[u1, v1], [u2, v2], [u4, v4], [u3, v3]], np.int32).reshape((-1, 1, 2))
            cv2.fillPoly(overlay, [pts], fill_color)
            cv2.polylines(overlay, [pts], True, line_color)

    # drawing the plots on original iamge
    img_plot = cv2.addWeighted(overlay, alpha, img_plot, 1 - alpha, 0)

    return img_plot

class Calibration:
    device_frame_from_view_frame = np.array([
      [ 0.,  0.,  1.],
      [ 1.,  0.,  0.],
      [ 0.,  1.,  0.]
    ])

    def get_view_frame_from_calib_frame(self, roll, pitch, yaw, height):
      device_from_calib= orientation.rot_from_euler([roll, pitch, yaw])
      view_from_calib = self.device_frame_from_view_frame.T.dot(device_from_calib)
      return np.hstack((view_from_calib, [[0], [height], [0]]))

    def __init__(self, rpy, intrinsic=eon_intrinsics, plot_img_width=640, plot_img_height=480):
        self.intrinsic = intrinsic
        self.extrinsics_matrix = self.get_view_frame_from_calib_frame(rpy[0], rpy[1], rpy[2], 0)[:, :3]
        self.plot_img_width = plot_img_width
        self.plot_img_height = plot_img_height
        self.zoom = W / plot_img_width
        self.CALIB_BB_TO_FULL = np.asarray([
            [self.zoom, 0., 0.],
            [0., self.zoom, 0.],
            [0., 0., 1.]])

    def car_space_to_ff(self, x, y, z):
        car_space_projective = np.column_stack((x, y, z)).T
        ep = self.extrinsics_matrix.dot(car_space_projective)
        kep = self.intrinsic.dot(ep)
        # TODO: fix numerical instability (add 1e-16)
        # UPD: this turned out to slow things down a lot. How do we do it then?
        return (kep[:-1, :] / kep[-1, :]).T

    def car_space_to_bb(self, x, y, z):
        pts = self.car_space_to_ff(x, y, z)
        return pts / self.zoom