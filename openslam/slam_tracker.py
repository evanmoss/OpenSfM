import datetime
import logging
import matplotlib.pyplot as plt
import numpy as np

from opensfm import types
from opensfm import csfm
from opensfm.reconstruction import Chronometer
from opensfm import reconstruction
from opensfm import features
from opensfm import feature_loader

from slam_matcher import SlamMatcher
from slam_mapper import SlamMapper
from slam_frame import Frame


def reproject_landmarks(points3D, observations, pose_world_to_cam, 
                        image, camera, data):
    #load the image
    camera_point = pose_world_to_cam.transform_many(points3D)
    points2D = camera.project_many(camera_point)
    fig, ax = plt.subplots(1)
    im = data.load_image(image)
    print("Show image ", image)
    h1, w1, c = im.shape
    pt = features.denormalized_image_coordinates(points2D, w1, h1)
    obs = features.denormalized_image_coordinates(observations, w1, h1)
    ax.imshow(im)
    ax.scatter(pt[:, 0], pt[:, 1], c=[[1, 0, 0]])
    ax.scatter(obs[:, 0], obs[:, 1], c=[[0, 1, 0]])
    plt.show()


class SlamTracker(object):

    def __init__(self,  data, config):
        self.slam_matcher = SlamMatcher(config)
        print("init slam tracker")

    def bundle_tracking(self, points3D, observations, init_pose, camera,
                        config, data):
        """Estimates the 6 DOF pose with respect to 3D points

        Reprojects 3D points to the image plane and minimizes the
        reprojection error to the corresponding observations to 
        find the relative motion.
     
        Args:
            points3D: 3D points to reproject
            observations: their 2D correspondences
            init_pose: initial pose depending on the coord. system of points3D
            camera: intrinsic camera parameters
            config, data

        Returns:
            pose: The estimated (relative) 6 DOF pose
        """
        if len(points3D) != len(observations):
            print("len(points3D) != len(observations): ",
                  len(points3D), len(observations))
            return False
        # reproject_landmarks(points3D, observations, init_pose, camera, data)
        # match "last frame" to "current frame"
        # last frame could be reference frame
        # somehow match world points/landmarks seen in last frame
        # to feature matches
        # fix_cameras = not config['optimize_camera_parameters']
        fix_cameras = True
        chrono = Chronometer()
        ba = csfm.BundleAdjuster()
        # for camera in reconstruction.cameras.values():
        reconstruction._add_camera_to_bundle(ba, camera[1], fix_cameras)
        # init with a constant motion model!
        # shot == last image
        # shot = reconstruction.shots[last_frame]
        # r = shot.pose.rotation
        # t = shot.pose.translation
        # fix the world pose of the last_frame
        # ba.add_shot(shot.id, 0, r, t, True)

        # constant motion velocity -> just say id
        shot_id = str(0)
        camera_id = str(camera[0])
        camera_const = False
        ba.add_shot(shot_id, str(camera_id), init_pose.rotation,
                    init_pose.translation, camera_const)
        points_3D_constant = True
        # Add points in world coordinates
        for (pt_id, pt_coord) in enumerate(points3D):
            ba.add_point(str(pt_id), pt_coord, points_3D_constant)
            ft = observations[pt_id, :]
            ba.add_point_projection_observation(shot_id, str(pt_id),
                                                ft[0], ft[1], ft[2])
        # Assume observations N x 3 (x,y,s)
        ba.add_absolute_up_vector(shot_id, [0, 0, -1], 1e-3)
        print("Added points")
        print("Added add_absolute_up_vector")
        ba.set_point_projection_loss_function(config['loss_function'],
                                              config['loss_function_threshold'])
        print("Added set_point_projection_loss_function")
        ba.set_internal_parameters_prior_sd(
            config['exif_focal_sd'],
            config['principal_point_sd'],
            config['radial_distorsion_k1_sd'],
            config['radial_distorsion_k2_sd'],
            config['radial_distorsion_p1_sd'],
            config['radial_distorsion_p2_sd'],
            config['radial_distorsion_k3_sd'])
        print("Added set_internal_parameters_prior_sd")
        ba.set_num_threads(config['processes'])
        ba.set_max_num_iterations(50)
        ba.set_linear_solver_type("SPARSE_SCHUR")
        print("set_linear_solver_type")
        
        chrono.lap('setup')
        ba.run()
        chrono.lap('run')

        print("BA finished")
        s = ba.get_shot(shot_id)
        pose = types.Pose()
        pose.rotation = [s.r[0], s.r[1], s.r[2]]
        pose.translation = [s.t[0], s.t[1], s.t[2]]
        print("Estimated pose: ", pose.rotation, pose.translation)
        print("Init pose: ", init_pose.rotation, init_pose.translation)
        return True, pose

    def _track_internal(self, frame1: Frame, frame2: Frame,
                        init_pose: types.Pose, camera, config, data):
        """Estimate 6 DOF pose between frame 1 and frame2

        Reprojects the landmarks seen in frame 1 to frame2
        and estimates the relative 6 DOF motion between
        frame1 and frame2 by minimizing the reprojection
        error.

        Arguments:
            landmarks1: 3D points in frame1 to be reprojected
            frame1: image name in dataset
            frame2: image name in dataset
            init_pose: initial 6 DOF estimate
            config, data
        """
        m1, idx1, idx2, matches = self.slam_matcher.match_landmarks_to_image(
                        frame1, frame2, camera, data)

        landmarks1 = frame1.visible_landmarks
        points3D = np.zeros((len(landmarks1), 3))
        for l_id, point in enumerate(landmarks1.values()):
            points3D[l_id, :] = point.coordinates
        print("lengths: idx:", len(m1), len(idx1), len(idx2))
        
        points2D, _, _ = feature_loader.instance. \
            load_points_features_colors(data, frame2.im_name, masked=True)
        points2D = points2D[matches[idx2, 1], :]
        points3D = points3D[idx1, :]

        if len(m1) < 100:
            return None

        # Set up bundle adjustment problem
        success, pose = self.bundle_tracking(points3D, points2D, init_pose,
                                             camera, config, data)
        reproject_landmarks(points3D, points2D, init_pose, frame2.im_name, camera[1],
                            data)
        reproject_landmarks(points3D, points2D, pose, frame2.im_name, camera[1], data)
        return pose

    def track(self, slam_mapper: SlamMapper, frame: Frame, config, camera, data):
        """Tracks the current frame with respect to the reconstruction
        """

        """ last_frame, frame, camera, init_pose, config, data):
        Align the current frame to the already estimated landmarks
            (visible in the last frame)
            landmarks visible in last frame
        """

        # Try to match to last frame first
        init_pose = slam_mapper.estimate_pose()
        pose = self._track_internal(
                                slam_mapper.last_frame, frame,
                                init_pose, camera, config, data)
        # If that fails, match to last kf
        if slam_mapper.last_frame.id != \
           slam_mapper.last_keyframe.id and pose is None:

            init_pose = types.Pose()
            pose = self._track_internal(
                        slam_mapper.last_keyframe.visible_landmarks,
                        slam_mapper.last_keyframe.im_name,
                        frame, init_pose)

        return pose

        # if pose is None:
        #     return False

        # slam_mapper.add_frame_to_reconstruction(frame, pose, camera, data)
        # slam_mapper.paint_reconstruction(data)
        # slam_mapper.save_reconstruction(data, frame)

        # return True
        
        #prepare the bundle
        

        # tracks are the matched landmarks
        # match landmarks to current frame
        # last frame is typically != last keyframe
        # landmarks contain feature id in last frame
        
        #load feature so both frames
        # p1, f1, _ = 
        #landmarks = LandmarkStorage()

        # for landmark in landmarks:
            # feature_id = landmark.fid
            
        

        # if n_matches < 100: # kind of random number
            # return False

        # velocity = T_(N-1)_(N-2) pre last to last
        # init_pose = T_(N_1)_w * T_(N-1)_W * inv(T_(N_2)_W)
        # match "last frame" to "current frame"
        # last frame could be reference frame
        # somehow match world points/landmarks seen in last frame
        # to feature matches
        # fix_cameras = not config['optimize_camera_parameters']
