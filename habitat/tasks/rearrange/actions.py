#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from typing import Optional

import magnum as mn
import numpy as np
from gym import spaces

import habitat_sim
from habitat.core.embodied_task import SimulatorTaskAction
from habitat.core.registry import registry
from habitat.sims.habitat_simulator.actions import HabitatSimActions

# flake8: noqa
# These actions need to be imported since there is a Python evaluation
# statement which dynamically creates the desired grip controller.
from habitat.tasks.rearrange.grip_actions import (
    GalaMagicGraspAction,
    GripSimulatorTaskAction,
    MagicGraspAction,
    SuctionGraspAction,
)
from habitat.tasks.rearrange.rearrange_sim import RearrangeSim
from habitat.tasks.rearrange.utils import rearrange_collision, rearrange_logger


def interp_step(scale_val, set_pos):
    set_pos = np.clip(set_pos, -1.0, 1.0)
    # To [0,1]
    set_pos = (set_pos + 1.0) / 2.0

    for i in range(len(set_pos)):
        set_pos[i] = mn.math.lerp(-scale_val[i], scale_val[i], set_pos[i])
    return set_pos


@registry.register_task_action
class EmptyAction(SimulatorTaskAction):
    """A No-op action useful for testing and in some controllers where we want
    to wait before the next operation.
    """

    @property
    def action_space(self):
        return spaces.Dict(
            {
                "empty_action": spaces.Box(
                    shape=(1,),
                    low=-1,
                    high=1,
                    dtype=np.float32,
                )
            }
        )

    def step(self, *args, **kwargs):
        return self._sim.step(HabitatSimActions.EMPTY)


@registry.register_task_action
class RearrangeStopAction(SimulatorTaskAction):
    def reset(self, *args, **kwargs):
        super().reset(*args, **kwargs)
        self.does_want_terminate = False

    def step(self, task, *args, is_last_action, **kwargs):
        should_stop = kwargs.get("REARRANGE_STOP", [1.0])
        if (
            should_stop[0] > self._config.STOP_THRESHOLD
            and self._config.ALLOW_STOP
        ):
            rearrange_logger.debug(
                "Rearrange stop action requesting episode stop."
            )
            self.does_want_terminate = True

        if is_last_action:
            return self._sim.step(HabitatSimActions.REARRANGE_STOP)
        else:
            return {}


@registry.register_task_action
class ArmAction(SimulatorTaskAction):
    """An arm control and grip control into one action space."""

    def __init__(self, *args, config, sim: RearrangeSim, **kwargs):
        super().__init__(*args, config=config, sim=sim, **kwargs)
        arm_controller_cls = eval(self._config.ARM_CONTROLLER)
        self._sim: RearrangeSim = sim
        self.arm_ctrlr = arm_controller_cls(
            *args, config=config, sim=sim, **kwargs
        )

        if self._config.GRIP_CONTROLLER is not None:
            grip_controller_cls = eval(self._config.GRIP_CONTROLLER)
            self.grip_ctrlr: Optional[
                GripSimulatorTaskAction
            ] = grip_controller_cls(*args, config=config, sim=sim, **kwargs)
        else:
            self.grip_ctrlr = None

        self.disable_grip = False
        if "DISABLE_GRIP" in config:
            self.disable_grip = config["DISABLE_GRIP"]

    def reset(self, *args, **kwargs):
        self.arm_ctrlr.reset(*args, **kwargs)
        if self.grip_ctrlr is not None:
            self.grip_ctrlr.reset(*args, **kwargs)

    @property
    def action_space(self):
        action_spaces = {
            "arm_action": self.arm_ctrlr.action_space,
        }
        if self.grip_ctrlr is not None and self.grip_ctrlr.requires_action:
            action_spaces["grip_action"] = self.grip_ctrlr.action_space
        return spaces.Dict(action_spaces)

    def step(
        self, arm_action, is_last_action, grip_action=None, *args, **kwargs
    ):
        self.arm_ctrlr.step(arm_action)
        if self.grip_ctrlr is not None and not self.disable_grip:
            self.grip_ctrlr.step(grip_action)
        if is_last_action:
            return self._sim.step(HabitatSimActions.ARM_ACTION)
        else:
            return {}


@registry.register_task_action
class ArmRelPosAction(SimulatorTaskAction):
    """
    The arm motor targets are offset by the delta joint values specified by the
    action
    """

    @property
    def action_space(self):
        return spaces.Box(
            shape=(self._config.ARM_JOINT_DIMENSIONALITY,),
            low=-1,
            high=1,
            dtype=np.float32,
        )

    def step(self, delta_pos, should_step=True, *args, **kwargs):
        # clip from -1 to 1
        delta_pos = np.clip(delta_pos, -1, 1)
        delta_pos *= self._config.DELTA_POS_LIMIT
        # The actual joint positions
        self._sim: RearrangeSim
        self._sim.robot.arm_motor_pos = (
            delta_pos + self._sim.robot.arm_motor_pos
        )


@registry.register_task_action
class GalaArmDynAction(SimulatorTaskAction):
    @property
    def action_space(self):
        return spaces.Box(
            shape=(self._config.ARM_JOINT_DIMENSIONALITY,),
            low=-1,
            high=1,
            dtype=np.float32,
        )

    def step(self, set_pos, *args, **kwargs):
        # No clipping because the arm is being set to exactly where it needs to
        # go.

        delta_pos = interp_step(self._config.SCALING, set_pos)
        desired_pos = delta_pos + self._sim.robot.arm_joint_pos

        # self._sim.robot._interpolate_arm_control(
        #     desired_pos, self._sim.robot.params.arm_joints, 0.1, 120.0
        # )

        self._sim.robot.arm_motor_pos = desired_pos
        # self._sim.robot.arm_velocity = delta_pos


@registry.register_task_action
class ArmRelPosKinematicAction(SimulatorTaskAction):
    """
    The arm motor targets are offset by the delta joint values specified by the
    action
    """

    @property
    def action_space(self):
        return spaces.Box(
            shape=(self._config.ARM_JOINT_DIMENSIONALITY,),
            low=0,
            high=1,
            dtype=np.float32,
        )

    def step(self, delta_pos, *args, **kwargs):
        if self._config.get("SHOULD_CLIP", True):
            # clip from -1 to 1
            delta_pos = np.clip(delta_pos, -1, 1)
        delta_pos *= self._config.DELTA_POS_LIMIT
        self._sim: RearrangeSim

        set_arm_pos = delta_pos + self._sim.robot.arm_joint_pos
        self._sim.robot.arm_joint_pos = set_arm_pos
        self._sim.robot.fix_joint_values = set_arm_pos


@registry.register_task_action
class ArmAbsPosAction(SimulatorTaskAction):
    """
    The arm motor targets are directly set to the joint configuration specified
    by the action.
    """

    @property
    def action_space(self):
        return spaces.Box(
            shape=(self._config.ARM_JOINT_DIMENSIONALITY,),
            low=0,
            high=1,
            dtype=np.float32,
        )

    def step(self, set_pos, *args, **kwargs):
        # No clipping because the arm is being set to exactly where it needs to
        # go.
        self._sim: RearrangeSim
        self._sim.robot.arm_motor_pos = set_pos


@registry.register_task_action
class GalaArmKinematicAction(SimulatorTaskAction):
    @property
    def action_space(self):
        return spaces.Box(
            shape=(self._config.ARM_JOINT_DIMENSIONALITY,),
            low=-1,
            high=1,
            dtype=np.float32,
        )

    def step(self, set_pos, *args, **kwargs):
        # No clipping because the arm is being set to exactly where it needs to
        # go.

        set_pos = interp_step(self._config.SCALING, set_pos)

        set_arm_pos = set_pos + self._sim.robot.arm_joint_pos
        self._sim.robot.arm_joint_pos = set_arm_pos
        self._sim.robot.fix_joint_values = set_arm_pos


@registry.register_task_action
class BaseVelAction(SimulatorTaskAction):
    """
    The robot base motion is constrained to the NavMesh and controlled with velocity commands integrated with the VelocityControl interface.

    Optionally cull states with active collisions if config parameter `ALLOW_DYN_SLIDE` is True
    """

    def __init__(self, *args, config, sim: RearrangeSim, **kwargs):
        super().__init__(*args, config=config, sim=sim, **kwargs)
        self._sim: RearrangeSim = sim
        self.base_vel_ctrl = habitat_sim.physics.VelocityControl()
        self.base_vel_ctrl.controlling_lin_vel = True
        self.base_vel_ctrl.lin_vel_is_local = True
        self.base_vel_ctrl.controlling_ang_vel = True
        self.base_vel_ctrl.ang_vel_is_local = True

    @property
    def end_on_stop(self):
        return self._config.END_ON_STOP

    @property
    def action_space(self):
        lim = 20
        return spaces.Dict(
            {
                "base_vel": spaces.Box(
                    shape=(2,), low=-lim, high=lim, dtype=np.float32
                )
            }
        )

    def _capture_robot_state(self, sim):
        return {
            "forces": sim.robot.sim_obj.joint_forces,
            "vel": sim.robot.sim_obj.joint_velocities,
            "pos": sim.robot.sim_obj.joint_positions,
        }

    def _set_robot_state(self, sim: RearrangeSim, set_dat):
        sim.robot.sim_obj.joint_positions = set_dat["forces"]
        sim.robot.sim_obj.joint_velocities = set_dat["vel"]
        sim.robot.sim_obj.joint_forces = set_dat["pos"]

    def reset(self, *args, **kwargs):
        super().reset(*args, **kwargs)
        self.does_want_terminate = False

    def update_base(self):
        ctrl_freq = self._sim.ctrl_freq

        before_trans_state = self._capture_robot_state(self._sim)

        trans = self._sim.robot.sim_obj.transformation
        rigid_state = habitat_sim.RigidState(
            mn.Quaternion.from_matrix(trans.rotation()), trans.translation
        )

        target_rigid_state = self.base_vel_ctrl.integrate_transform(
            # 1 / ctrl_freq, rigid_state
            1,
            rigid_state,
        )
        end_pos = self._sim.step_filter(
            rigid_state.translation, target_rigid_state.translation
        )

        target_trans = mn.Matrix4.from_(
            target_rigid_state.rotation.to_matrix(), end_pos
        )
        self._sim.robot.sim_obj.transformation = target_trans

        if not self._config.get("ALLOW_DYN_SLIDE", True):
            # Check if in the new robot state the arm collides with anything.
            # If so we have to revert back to the previous transform
            self._sim.internal_step(-1)
            colls = self._sim.get_collisions()
            did_coll, _ = rearrange_collision(
                colls, self._sim.snapped_obj_id, False
            )
            if did_coll:
                # Don't allow the step, revert back.
                self._set_robot_state(self._sim, before_trans_state)
                self._sim.robot.sim_obj.transformation = trans
        if self._sim.grasp_mgr.snap_idx is not None:
            # Holding onto an object, also kinematically update the object.
            # object.
            self._sim.grasp_mgr.update_object_to_grasp()

    def step(self, base_vel, *args, is_last_action, **kwargs):
        base_vel = interp_step(
            [self._config.ANG_SPEED, self._config.LIN_SPEED], base_vel
        )
        ang_vel, lin_vel = base_vel

        self.does_want_terminate = False

        self.base_vel_ctrl.linear_velocity = mn.Vector3(lin_vel, 0, 0)
        self.base_vel_ctrl.angular_velocity = mn.Vector3(0, ang_vel, 0)

        if lin_vel != 0.0 or ang_vel != 0.0:
            self.update_base()

        if is_last_action:
            return self._sim.step(HabitatSimActions.BASE_VELOCITY)
        else:
            return {}


@registry.register_task_action
class ArmEEAction(SimulatorTaskAction):
    """Uses inverse kinematics (requires pybullet) to apply end-effector position control for the robot's arm."""

    def __init__(self, *args, config, sim: RearrangeSim, **kwargs):
        self.ee_target: Optional[np.ndarray] = None
        super().__init__(*args, config=config, sim=sim, **kwargs)
        self._sim: RearrangeSim = sim

    def reset(self, *args, **kwargs):
        super().reset()
        cur_ee = self._sim.ik_helper.calc_fk(
            np.array(self._sim.robot.arm_joint_pos)
        )

        self.ee_target = cur_ee

    @property
    def action_space(self):
        return spaces.Box(shape=(3,), low=-1, high=1, dtype=np.float32)

    def apply_ee_constraints(self):
        self.ee_target = np.clip(
            self.ee_target,
            self._sim.robot.params.ee_constraint[:, 0],
            self._sim.robot.params.ee_constraint[:, 1],
        )

    def set_desired_ee_pos(self, ee_pos: np.ndarray) -> None:
        self.ee_target += np.array(ee_pos)

        self.apply_ee_constraints()

        ik = self._sim.ik_helper

        joint_pos = np.array(self._sim.robot.arm_joint_pos)
        joint_vel = np.zeros(joint_pos.shape)

        ik.set_arm_state(joint_pos, joint_vel)

        des_joint_pos = ik.calc_ik(self.ee_target)
        des_joint_pos = list(des_joint_pos)
        self._sim.robot.arm_motor_pos = des_joint_pos

    def step(self, ee_pos, **kwargs):
        ee_pos = np.clip(ee_pos, -1, 1)
        ee_pos *= self._config.EE_CTRL_LIM
        self.set_desired_ee_pos(ee_pos)

        if self._config.get("RENDER_EE_TARGET", False):
            global_pos = self._sim.robot.base_transformation.transform_point(
                self.ee_target
            )
            self._sim.viz_ids["ee_target"] = self._sim.visualize_position(
                global_pos, self._sim.viz_ids["ee_target"]
            )
