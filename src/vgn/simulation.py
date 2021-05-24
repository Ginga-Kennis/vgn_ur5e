from pathlib import Path

import numpy as np
import pybullet as p

from robot_tools.bullet import BtSim, BtCamera
from robot_tools.spatial import Rotation, Transform
from vgn.utils import task_lines


def discover_urdfs(root):
    urdfs = {}
    scan_dir = lambda d: [str(f) for f in d.iterdir() if f.suffix == ".urdf"]
    urdfs["blocks"] = scan_dir(root / "blocks")
    urdfs["pile-train"] = scan_dir(root / "pile" / "train")
    urdfs["pile-test"] = scan_dir(root / "pile" / "test")
    urdfs["packed-train"] = scan_dir(root / "packed" / "train")
    urdfs["packed-test"] = scan_dir(root / "packed" / "test")
    return urdfs


class GraspSim(BtSim):
    def __init__(self, gui, random_state=None):
        super().__init__(gui, sleep=gui)
        self.rng = random_state if random_state else np.random
        self.urdfs = discover_urdfs(Path("data/urdfs"))
        self.gripper = Gripper(self)
        self.camera = BtCamera(640, 480, 1.047, 0.1, 2.0, renderer=p.ER_TINY_RENDERER)
        self.size = 0.3  # = 6 * gripper.finger_depth
        self.origin = [0.15, 0.15, 0.05]

    @property
    def num_objects(self):
        return max(0, p.getNumBodies() - 1)  # remove table from body count

    def save_state(self):
        self._snapshot_id = p.saveState()

    def restore_state(self):
        p.restoreState(stateId=self._snapshot_id)

    def reset(self, scene, object_count):
        p.resetSimulation()
        p.setPhysicsEngineParameter(fixedTimeStep=self.dt, numSolverIterations=200)
        p.setGravity(0.0, 0.0, -9.81)
        p.resetDebugVisualizerCamera(1.0, 0.0, -45, [0.15, 0.5, -0.3])
        p.loadURDF("data/urdfs/setup/plane.urdf", self.origin, globalScaling=0.6)
        self.draw_task_space()
        self.object_uids = []
        if scene == "blocks":
            self.spawn_pile(self.urdfs[scene], object_count, 1.67)
        elif scene in ["pile-train", "pile-test"]:
            self.spawn_pile(self.urdfs[scene], object_count)
        elif scene in ["packed-train", "packed-test"]:
            self.spawn_packed(self.urdfs[scene], object_count)
        else:
            raise ValueError("Invalid scene")

    def draw_task_space(self):
        for (p0, p1) in task_lines(self.size):
            p.addUserDebugLine(p0, p1, lineColorRGB=[0.5, 0.5, 0.5])

    def spawn_pile(self, object_urdfs, count, scaling_factor=1.0):
        box_uid = p.loadURDF(
            "data/urdfs/setup/box.urdf",
            [0.02, 0.02, 0.05],
            globalScaling=1.3,
        )
        for urdf in self.rng.choice(object_urdfs, size=count):
            ori = Rotation.random(random_state=self.rng)
            xy = self.rng.uniform(1.0 / 3.0 * self.size, 2.0 / 3.0 * self.size, 2)
            scaling = self.rng.uniform(0.8, 1.0) * scaling_factor
            self.spawn_object(urdf, np.r_[xy, 0.25], ori, scaling)
            self.wait_for_objects_to_rest(timeout=1.0)
        p.removeBody(box_uid)
        self.remove_objects_that_rolled_away()

    def spawn_packed(self, object_urdfs, count, max_attempts=12):
        attempts = 0
        while self.num_objects < count and attempts < max_attempts:
            self.save_state()
            # Sample object and pose
            urdf = self.rng.choice(object_urdfs)
            x = self.rng.uniform(0.08, 0.22)
            y = self.rng.uniform(0.08, 0.22)
            z = 1.0
            angle = self.rng.uniform(0.0, 2.0 * np.pi)
            ori = Rotation.from_rotvec(angle * np.r_[0.0, 0.0, 1.0])
            scaling = self.rng.uniform(0.7, 0.9)
            # Try to place the object (infer height from bounding box)
            uid = self.spawn_object(urdf, [x, y, z], ori, scaling)
            lower, upper = p.getAABB(uid)
            z = self.origin[2] + 0.5 * (upper[2] - lower[2]) + 0.002
            p.resetBasePositionAndOrientation(uid, [x, y, z], ori.as_quat())
            self.step()
            # Reject the placement if it collides with the scene
            if p.getContactPoints(uid):
                self.remove_object(uid)
                self.restore_state()
            else:
                self.wait_for_objects_to_rest()
            attempts += 1
        self.remove_objects_that_rolled_away()

    def spawn_object(self, urdf, pos, ori, scaling=1.0):
        uid = p.loadURDF(urdf, pos, ori.as_quat(), globalScaling=scaling)
        self.object_uids.append(uid)
        return uid

    def remove_object(self, uid):
        p.removeBody(uid)
        self.object_uids.remove(uid)

    def wait_for_objects_to_rest(self, timeout=1.0, tol=0.01):
        objects_resting, elapsed_time = False, 0.0
        while not objects_resting and elapsed_time < timeout:
            self.forward(0.25)
            elapsed_time += 0.25
            objects_resting = True
            for uid in self.object_uids:
                v, _ = p.getBaseVelocity(uid)
                if np.linalg.norm(v) > tol:
                    objects_resting = False
                    break

    def remove_objects_that_rolled_away(self):
        removed_object = True
        while removed_object:
            self.wait_for_objects_to_rest()
            removed_object = False
            for uid in self.object_uids:
                xyz = np.asarray(p.getBasePositionAndOrientation(uid)[0])
                if np.any(xyz < 0.0) or np.any(xyz > self.size):
                    self.remove_object(uid)
                    removed_object = True

    def execute_grasp(self, remove_object=True):
        self.gripper.close()
        retreat_pose = self.gripper.get_pose()
        retreat_pose.translation[2] += 0.1
        self.gripper.move_linear(retreat_pose, stop_on_contact=False)
        if self.check_success():
            score = 1.0
            if remove_object:
                contacts = p.getContactPoints(self.gripper.uid)
                self.remove_object(contacts[0][2])
        else:
            score = 0.0
        return score

    def check_success(self):
        return (
            self.gripper.in_contact
            and self.gripper.read() > 0.1 * self.gripper.max_opening_width
        )


class Gripper(object):
    # Floating gripper controlled via a force constraint
    def __init__(self, sim):
        self.sim = sim
        self.max_opening_width = 0.08
        self.finger_depth = 0.05
        # PyBullet can use either the URDF link frame (B) or COM frame as reference
        self.T_EE_B = Transform(Rotation.identity(), [0.0, 0.0, -0.065])
        self.T_EE_COM = Transform(Rotation.identity(), [0.0, 0.0, -0.025])

    @property
    def in_contact(self):
        return len(p.getContactPoints(self.uid)) > 0

    def spawn(self, T_W_EE):
        # loadURDF uses URDF link frame
        T_W_B = T_W_EE * self.T_EE_B
        self.uid = p.loadURDF(
            "data/urdfs/panda/hand.urdf",
            T_W_B.translation,
            T_W_B.rotation.as_quat(),
        )
        # Constraints and resetBasePositionAndOrientation use COM frame
        T_W_COM = T_W_EE * self.T_EE_COM
        self.c_uid = p.createConstraint(
            parentBodyUniqueId=self.uid,
            parentLinkIndex=-1,
            childBodyUniqueId=-1,
            childLinkIndex=-1,
            jointType=p.JOINT_FIXED,
            jointAxis=[0.0, 0.0, 0.0],
            parentFramePosition=[0.0, 0.0, 0.0],
            parentFrameOrientation=Rotation.identity().as_quat(),
            childFramePosition=T_W_COM.translation,
            childFrameOrientation=T_W_COM.rotation.as_quat(),
        )
        self._update_constraint(T_W_COM)

        # Open fingers
        p.resetJointState(self.uid, 0, 0.5 * self.max_opening_width)
        p.resetJointState(self.uid, 1, 0.5 * self.max_opening_width)

        # Mimic fingers
        uid = p.createConstraint(
            self.uid,
            0,
            self.uid,
            1,
            p.JOINT_GEAR,
            [1.0, 0.0, 0.0],
            [0.0, 0.0, 0.0],
            [0.0, 0.0, 0.0],
        )
        p.changeConstraint(uid, gearRatio=-1, erp=0.1, maxForce=50)

    def get_pose(self):
        pos, ori = p.getBasePositionAndOrientation(self.uid)
        return Transform(Rotation.from_quat(ori), pos) * self.T_EE_COM.inv()

    def set_desired_pose(self, T_W_EE):
        T_W_COM = T_W_EE * self.T_EE_COM
        self._update_constraint(T_W_COM)

    def move_linear(self, end, stop_on_contact=True, velocity=0.1, ee_step=0.002):
        start = self.get_pose()
        length = np.linalg.norm(end.translation - start.translation)
        direction = (end.translation - start.translation) / length
        step_count = int(length / ee_step)
        step_duration = ee_step / velocity
        for _ in range(step_count):
            start.translation += ee_step * direction
            self.set_desired_pose(start)
            self.sim.forward(step_duration)
            if stop_on_contact and self.in_contact:
                return

    def close(self):
        p.setJointMotorControl2(self.uid, 0, p.POSITION_CONTROL, 0.0, force=20)
        p.setJointMotorControl2(self.uid, 1, p.POSITION_CONTROL, 0.0, force=20)
        self.sim.forward(0.5)

    def read(self):
        return p.getJointState(self.uid, 0)[0] + p.getJointState(self.uid, 1)[0]

    def remove(self):
        p.removeBody(self.uid)

    def _update_constraint(self, pose):
        p.changeConstraint(
            self.c_uid,
            jointChildPivot=pose.translation,
            jointChildFrameOrientation=pose.rotation.as_quat(),
            maxForce=300,
        )
