"""Robot kinematics: chains, structure templates, forward and inverse kinematics.

Numpy only; nothing here imports the product. Importing the package registers
the templates' closed-form solvers, so `ik` uses them for every structure.
"""
from nagasim.core.robotics.chain import (
    ANALYTIC_IK, IK_POS_TOL, IK_ROT_TOL, SINGULAR_TOL, Axis, Joint, Robot, chain_robot,
    coupling_matrix, fk, ik, ik_numeric, jacobian, joint_origins, joint_values, pose,
    task_sigma_min, within_limits)
from nagasim.core.robotics.delta import delta_display, delta_fk, delta_ik
from nagasim.core.robotics.templates import PARAMS, STRUCTURES, Template, reach, template_robot

__all__ = [
    "ANALYTIC_IK", "IK_POS_TOL", "IK_ROT_TOL", "PARAMS", "SINGULAR_TOL", "STRUCTURES", "Axis",
    "Joint", "Robot", "Template", "chain_robot", "coupling_matrix", "delta_display", "delta_fk",
    "delta_ik", "fk", "ik", "ik_numeric", "jacobian", "joint_origins", "joint_values", "pose",
    "reach", "task_sigma_min", "template_robot", "within_limits",
]
