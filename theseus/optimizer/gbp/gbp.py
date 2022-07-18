# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import abc
import math
import time
import warnings
from dataclasses import dataclass
from enum import Enum
from itertools import count
from typing import Dict, List, Optional, Sequence, Tuple, Type

import numpy as np
import torch

import theseus as th
import theseus.constants
from theseus.core import CostFunction, Objective
from theseus.geometry import Manifold
from theseus.optimizer import Optimizer, VariableOrdering
from theseus.optimizer.nonlinear.nonlinear_optimizer import (
    BackwardMode,
    NonlinearOptimizerInfo,
    NonlinearOptimizerStatus,
)

"""
TODO
 - solving inverse problem to compute message mean
 - factor inherits CF class
"""


"""
Utitily functions
"""


# Same of NonlinearOptimizerParams but without step size
@dataclass
class GBPOptimizerParams:
    abs_err_tolerance: float
    rel_err_tolerance: float
    max_iterations: int

    def update(self, params_dict):
        for param, value in params_dict.items():
            if hasattr(self, param):
                setattr(self, param, value)
            else:
                raise ValueError(f"Invalid nonlinear optimizer parameter {param}.")


class GBPSchedule(Enum):
    SYNCHRONOUS = 0


def synchronous_schedule(max_iters, n_edges) -> torch.Tensor:
    return torch.full([max_iters, n_edges], True)


# def random_schedule(max_iters, n_edges) -> torch.Tensor:
#     schedule = torch.full([max_iters, n_edges], False)
#     # on first step send messages along all edges
#     schedule[0] = True
#     ixs = torch.randint(0, n_edges, [max_iters])
#     schedule[torch.arange(max_iters), ixs] = True
#     return schedule


# Initialises message precision to zero
class Message(th.ManifoldGaussian):
    def __init__(
        self,
        mean: Sequence[Manifold],
        precision: Optional[torch.Tensor] = None,
        name: Optional[str] = None,
    ):
        if precision is None:
            dof = sum([v.dof() for v in mean])
            precision = torch.zeros(mean[0].shape[0], dof, dof).to(
                dtype=mean[0].dtype, device=mean[0].device
            )
        super(Message, self).__init__(mean, precision=precision, name=name)

    # sets mean to the group identity and zero precision matrix
    def zero_message(self):
        new_mean = []
        batch_size = self.mean[0].shape[0]
        for var in self.mean:
            if var.__class__ == th.Vector:
                new_mean_i = var.__class__(var.dof())
            else:
                new_mean_i = var.__class__()
            repeats = torch.ones(var.ndim, dtype=int)
            repeats[0] = batch_size
            new_mean_i = new_mean_i.tensor.repeat(repeats.tolist())
            new_mean_i = new_mean_i.to(dtype=self.dtype, device=self.device)
            new_mean.append(new_mean_i)
        new_precision = torch.zeros(batch_size, self.dof, self.dof).to(
            dtype=self.dtype, device=self.device
        )
        self.update(mean=new_mean, precision=new_precision)


"""
GBP functions
"""


class Factor:
    _ids = count(0)

    def __init__(
        self,
        cf: CostFunction,
        var_ixs: torch.Tensor,
        name: Optional[str] = None,
        lin_system_damping: float = 1e-6,
    ):
        self._id = next(Factor._ids)
        if name:
            self.name = name
        else:
            self.name = f"{self.__class__.__name__}__{self._id}"

        self.cf = cf
        self.var_ixs = var_ixs

        # batch_size of the vectorized factor. In general != objective.batch_size.
        # They are equal without vectorization or for unique cost function schema.
        self.batch_size = cf.optim_var_at(0).shape[0]

        device = cf.optim_var_at(0).device
        dtype = cf.optim_var_at(0).dtype
        self._dof = sum([var.dof() for var in cf.optim_vars])
        self.potential_eta = torch.zeros(self.batch_size, self.dof).to(
            dtype=dtype, device=device
        )
        self.potential_lam = torch.zeros(self.batch_size, self.dof, self.dof).to(
            dtype=dtype, device=device
        )
        self.lin_point: List[Manifold] = [
            var.copy(new_name=f"{cf.name}_{var.name}_lp") for var in cf.optim_vars
        ]

        self.steps_since_lin = torch.zeros(
            self.batch_size, device=device, dtype=torch.int
        )

        self.lm_damping = torch.full([self.batch_size], lin_system_damping).to(
            dtype=dtype, device=device
        )
        self.min_damping = torch.Tensor([1e-4]).to(dtype=dtype, device=device)
        self.max_damping = torch.Tensor([1e2]).to(dtype=dtype, device=device)
        self.last_err: torch.Tensor = None
        self.a = 2
        self.b = 10

        self.vtof_msgs: List[Message] = []
        self.ftov_msgs: List[Message] = []
        for var in cf.optim_vars:
            # Set mean of initial message to identity of the group
            # NB doesn't matter what it is as long as precision is zero
            vtof_msg = Message([var.copy()], name=f"msg_{var.name}_to_{cf.name}")
            ftov_msg = Message([var.copy()], name=f"msg_{cf.name}_to_{var.name}")
            vtof_msg.zero_message()
            ftov_msg.zero_message()
            self.vtof_msgs.append(vtof_msg)
            self.ftov_msgs.append(ftov_msg)

        # for vectorized vtof message passing
        self.vectorized_var_ixs: List[torch.Tensor] = [None] * cf.num_optim_vars()

    # Linearizes factors at current belief if beliefs have deviated
    # from the linearization point by more than the threshold.
    def linearize(
        self,
        relin_threshold: float = None,
        lie=True,
    ):
        self.steps_since_lin += 1

        if relin_threshold is None:
            do_lin = torch.full(
                [self.batch_size],
                True,
                device=self.cf.optim_var_at(0).device,
            )
        else:
            lp_dists = torch.cat(
                [
                    lp.local(self.cf.optim_var_at(j)).norm(dim=1)[..., None]
                    for j, lp in enumerate(self.lin_point)
                ],
                dim=1,
            )
            max_dists = lp_dists.max(dim=1)[0]
            do_lin = max_dists > relin_threshold

        if torch.sum(do_lin) > 0:  # if any factor in the batch needs relinearization
            J, error = self.cf.weighted_jacobians_error()

            J_stk = torch.cat(J, dim=-1)

            lam = torch.bmm(J_stk.transpose(-2, -1), J_stk)
            eta = -torch.matmul(J_stk.transpose(-2, -1), error.unsqueeze(-1))
            if lie is False:
                optim_vars_stk = torch.cat(
                    [v.tensor for v in self.cf.optim_vars], dim=-1
                )
                eta = eta + torch.matmul(lam, optim_vars_stk.unsqueeze(-1))
            eta = eta.squeeze(-1)

            # update damping parameter
            err = (self.cf.error() ** 2).sum(dim=1)
            if self.last_err is not None:
                decreased_ixs = err < self.last_err
                self.lm_damping[decreased_ixs] = torch.max(
                    self.lm_damping[decreased_ixs] / self.a, self.min_damping
                )
                self.lm_damping[~decreased_ixs] = torch.min(
                    self.lm_damping[~decreased_ixs] * self.b, self.max_damping
                )
            self.last_err = err
            # damp precision matrix
            damped_D = self.lm_damping[:, None, None] * torch.eye(
                lam.shape[1], device=lam.device, dtype=lam.dtype
            ).unsqueeze(0).repeat(self.batch_size, 1, 1)
            lam = lam + damped_D

            self.potential_eta[do_lin] = eta[do_lin]
            self.potential_lam[do_lin] = lam[do_lin]

            for j, var in enumerate(self.cf.optim_vars):
                self.lin_point[j].update(var.tensor, batch_ignore_mask=~do_lin)

            self.steps_since_lin[do_lin] = 0

    # Compute all outgoing messages from the factor.
    def comp_mess(
        self,
        damping,
        schedule,
    ):
        num_optim_vars = self.cf.num_optim_vars()
        new_messages = []

        sdim = 0
        for v in range(num_optim_vars):
            eta_factor = self.potential_eta.clone()
            lam_factor = self.potential_lam.clone()
            lam_factor_copy = lam_factor.clone()

            # Take product of factor with incoming messages.
            # Convert mesages to tangent space at linearisation point.
            start = 0
            for i in range(num_optim_vars):
                var_dofs = self.cf.optim_var_at(i).dof()
                if i != v:
                    eta_mess, lam_mess = th.local_gaussian(
                        self.lin_point[i], self.vtof_msgs[i], return_mean=False
                    )
                    eta_factor[:, start : start + var_dofs] += eta_mess
                    lam_factor[
                        :, start : start + var_dofs, start : start + var_dofs
                    ] += lam_mess

                start += var_dofs

            dofs = self.cf.optim_var_at(v).dof()

            if torch.allclose(lam_factor, lam_factor_copy) and num_optim_vars > 1:
                # print(self.cf.name, "---> not updating, incoming precision is zero")
                new_mess = Message([self.cf.optim_var_at(v).copy()])
                new_mess.zero_message()

            else:
                # print(self.cf.name, "---> sending message")
                # Divide up parameters of distribution
                eo = eta_factor[:, sdim : sdim + dofs]
                eno = torch.cat(
                    (eta_factor[:, :sdim], eta_factor[:, sdim + dofs :]), dim=1
                )

                loo = lam_factor[:, sdim : sdim + dofs, sdim : sdim + dofs]
                lono = torch.cat(
                    (
                        lam_factor[:, sdim : sdim + dofs, :sdim],
                        lam_factor[:, sdim : sdim + dofs, sdim + dofs :],
                    ),
                    dim=2,
                )
                lnoo = torch.cat(
                    (
                        lam_factor[:, :sdim, sdim : sdim + dofs],
                        lam_factor[:, sdim + dofs :, sdim : sdim + dofs],
                    ),
                    dim=1,
                )
                lnono = torch.cat(
                    (
                        torch.cat(
                            (
                                lam_factor[:, :sdim, :sdim],
                                lam_factor[:, :sdim, sdim + dofs :],
                            ),
                            dim=2,
                        ),
                        torch.cat(
                            (
                                lam_factor[:, sdim + dofs :, :sdim],
                                lam_factor[:, sdim + dofs :, sdim + dofs :],
                            ),
                            dim=2,
                        ),
                    ),
                    dim=1,
                )

                new_mess_lam = loo - lono @ torch.linalg.inv(lnono) @ lnoo
                new_mess_eta = eo - torch.bmm(
                    torch.bmm(lono, torch.linalg.inv(lnono)), eno.unsqueeze(-1)
                ).squeeze(-1)

                # damping in tangent space at linearisation point as message
                # is already in this tangent space. Could equally do damping
                # in the tangent space of the new or old message mean.
                # Damping is applied to the mean parameters.
                do_damping = torch.logical_and(damping[v] > 0, self.steps_since_lin > 0)
                if do_damping.sum() != 0:
                    damping_check = torch.logical_and(
                        new_mess_lam.count_nonzero(1, 2) != 0,
                        self.ftov_msgs[v].precision.count_nonzero(1, 2) != 0,
                    )
                    do_damping = torch.logical_and(do_damping, damping_check)
                    if do_damping.sum() > 0:
                        prev_mess_mean, prev_mess_lam = th.local_gaussian(
                            self.lin_point[v], self.ftov_msgs[v], return_mean=True
                        )
                        new_mess_mean = torch.bmm(
                            torch.inverse(new_mess_lam), new_mess_eta.unsqueeze(-1)
                        ).squeeze(-1)
                        damping[v][~do_damping] = 0.0
                        new_mess_mean = (
                            1 - damping[v][:, None]
                        ) * new_mess_mean + damping[v][:, None] * prev_mess_mean
                        new_mess_eta = torch.bmm(
                            new_mess_lam, new_mess_mean.unsqueeze(-1)
                        ).squeeze(-1)

                # don't send messages if schedule is False
                if not schedule[v].all():
                    # if any are False set these to prev message
                    prev_mess_eta, prev_mess_lam = th.local_gaussian(
                        self.lin_point[v], self.ftov_msgs[v], return_mean=False
                    )
                    no_update = ~schedule[v]
                    new_mess_eta[no_update] = prev_mess_eta[no_update]
                    new_mess_lam[no_update] = prev_mess_lam[no_update]

                # new_mess_lam = th.DenseSolver._apply_damping(
                #     new_mess_lam,
                #     self.lin_system_damping,
                #     ellipsoidal=True,
                #     eps=1e-8,
                # )

                new_mess_mean = th.LUDenseSolver._solve_sytem(
                    new_mess_eta[..., None], new_mess_lam
                )

                new_mess = th.retract_gaussian(
                    self.lin_point[v], new_mess_mean, new_mess_lam
                )

            new_messages.append(new_mess)

            sdim += dofs

        # update messages
        for v in range(num_optim_vars):
            self.ftov_msgs[v].update(
                mean=new_messages[v].mean, precision=new_messages[v].precision
            )

    @property
    def dof(self) -> int:
        return self._dof


# Follows notation from https://arxiv.org/pdf/2202.03314.pdf
class GaussianBeliefPropagation(Optimizer, abc.ABC):
    def __init__(
        self,
        objective: Objective,
        vectorize: bool = True,
        abs_err_tolerance: float = 1e-10,
        rel_err_tolerance: float = 1e-8,
        max_iterations: int = 20,
    ):
        super().__init__(objective, vectorize=vectorize)

        # ordering is required to identify which messages to send where
        self.ordering = VariableOrdering(objective, default_order=True)

        self.params = GBPOptimizerParams(
            abs_err_tolerance, rel_err_tolerance, max_iterations
        )

        self.beliefs: List[th.ManifoldGaussian] = []
        self.factors: List[Factor] = []

    """
    Copied and slightly modified from nonlinear optimizer class
    """

    def set_params(self, **kwargs):
        self.params.update(kwargs)

    def _check_convergence(self, err: torch.Tensor, last_err: torch.Tensor):
        assert not torch.is_grad_enabled()
        if err.abs().mean() < theseus.constants.EPS:
            return torch.ones_like(err).bool()

        abs_error = (last_err - err).abs()
        rel_error = abs_error / last_err
        return (abs_error < self.params.abs_err_tolerance).logical_or(
            rel_error < self.params.rel_err_tolerance
        )

    def _maybe_init_best_solution(
        self, do_init: bool = False
    ) -> Optional[Dict[str, torch.Tensor]]:
        if not do_init:
            return None
        solution_dict = {}
        for var in self.ordering:
            solution_dict[var.name] = var.tensor.detach().clone().cpu()
        return solution_dict

    def _init_info(
        self,
        track_best_solution: bool,
        track_err_history: bool,
        track_state_history: bool,
    ) -> NonlinearOptimizerInfo:
        with torch.no_grad():
            last_err = self.objective.error_squared_norm() / 2
        best_err = last_err.clone() if track_best_solution else None
        if track_err_history:
            err_history = (
                torch.ones(self.objective.batch_size, self.params.max_iterations + 1)
                * math.inf
            )
            assert last_err.grad_fn is None
            err_history[:, 0] = last_err.clone().cpu()
        else:
            err_history = None

        if track_state_history:
            state_history = {}
            for var in self.objective.optim_vars.values():
                state_history[var.name] = (
                    torch.ones(
                        self.objective.batch_size,
                        *var.shape[1:],
                        self.params.max_iterations + 1,
                    )
                    * math.inf
                )
                state_history[var.name][..., 0] = var.tensor.detach().clone().cpu()
        else:
            state_history = None

        return NonlinearOptimizerInfo(
            best_solution=self._maybe_init_best_solution(do_init=track_best_solution),
            last_err=last_err,
            best_err=best_err,
            status=np.array(
                [NonlinearOptimizerStatus.START] * self.objective.batch_size
            ),
            converged_iter=torch.zeros_like(last_err, dtype=torch.long),
            best_iter=torch.zeros_like(last_err, dtype=torch.long),
            err_history=err_history,
            state_history=state_history,
        )

    def _update_state_history(self, iter_idx: int, info: NonlinearOptimizerInfo):
        for var in self.objective.optim_vars.values():
            info.state_history[var.name][..., iter_idx + 1] = (
                var.tensor.detach().clone().cpu()
            )

    def _update_info(
        self,
        info: NonlinearOptimizerInfo,
        current_iter: int,
        err: torch.Tensor,
        converged_indices: torch.Tensor,
    ):
        info.converged_iter += 1 - converged_indices.long()
        if info.err_history is not None:
            assert err.grad_fn is None
            info.err_history[:, current_iter + 1] = err.clone().cpu()
        if info.state_history is not None:
            self._update_state_history(current_iter, info)

        if info.best_solution is not None:
            # Only copy best solution if needed (None means track_best_solution=False)
            assert info.best_err is not None
            good_indices = err < info.best_err
            info.best_iter[good_indices] = current_iter
            for var in self.ordering:
                info.best_solution[var.name][good_indices] = (
                    var.tensor.detach().clone()[good_indices].cpu()
                )

            info.best_err = torch.minimum(info.best_err, err)

        converged_indices = self._check_convergence(err, info.last_err)
        info.status[
            np.array(converged_indices.detach().cpu())
        ] = NonlinearOptimizerStatus.CONVERGED

    # Modifies the (no grad) info in place to add data of grad loop info
    def _merge_infos(
        self,
        grad_loop_info: NonlinearOptimizerInfo,
        num_no_grad_iters: int,
        num_grad_iters: int,
        info: NonlinearOptimizerInfo,
    ):
        total_iters = num_no_grad_iters + num_grad_iters
        # we add + 1 to all indices to account for the initial values
        info_idx = slice(num_no_grad_iters + 1, total_iters + 1)
        grad_info_idx = slice(1, num_grad_iters + 1)
        # Concatenate error histories
        if info.err_history is not None:
            info.err_history[:, info_idx] = grad_loop_info.err_history[:, grad_info_idx]
        if info.state_history is not None:
            for var in self.objective.optim_vars.values():
                info.state_history[var.name][
                    ..., info_idx
                ] = grad_loop_info.state_history[var.name][..., grad_info_idx]

        # Merge best solution and best error
        if info.best_solution is not None:
            best_solution = {}
            best_err_no_grad = info.best_err
            best_err_grad = grad_loop_info.best_err
            idx_no_grad = (best_err_no_grad < best_err_grad).cpu().view(-1, 1)
            best_err = torch.minimum(best_err_no_grad, best_err_grad)
            for var_name in info.best_solution:
                sol_no_grad = info.best_solution[var_name]
                sol_grad = grad_loop_info.best_solution[var_name]
                best_solution[var_name] = torch.where(
                    idx_no_grad, sol_no_grad, sol_grad
                )
            info.best_solution = best_solution
            info.best_err = best_err

        # Merge the converged status into the info from the detached loop,
        M = info.status == NonlinearOptimizerStatus.MAX_ITERATIONS
        assert np.all(
            (grad_loop_info.status[M] == NonlinearOptimizerStatus.MAX_ITERATIONS)
            | (grad_loop_info.status[M] == NonlinearOptimizerStatus.CONVERGED)
        )
        info.status[M] = grad_loop_info.status[M]
        info.converged_iter[M] = (
            info.converged_iter[M] + grad_loop_info.converged_iter[M]
        )
        # If didn't coverge in either loop, remove misleading converged_iter value
        info.converged_iter[
            M & (grad_loop_info.status == NonlinearOptimizerStatus.MAX_ITERATIONS)
        ] = -1

    """
    GBP functions
    """

    def _pass_var_to_fac_messages_loop(self, update_belief=True):
        for i, var in enumerate(self.ordering):

            # Collect all incoming messages in the tangent space at the current belief
            etas_tp = []  # message etas
            lams_tp = []  # message lams
            for factor in self.factors:
                for j, msg in enumerate(factor.ftov_msgs):
                    if factor.var_ixs[j] == i:
                        eta_tp, lam_tp = th.local_gaussian(var, msg, return_mean=False)
                        etas_tp.append(eta_tp[None, ...])
                        lams_tp.append(lam_tp[None, ...])

            etas_tp = torch.cat(etas_tp)
            lams_tp = torch.cat(lams_tp)

            lam_tau = lams_tp.sum(dim=0)

            # Compute outgoing messages
            ix = 0  # index for msg in list of msgs to variables
            for factor in self.factors:
                for j, msg in enumerate(factor.vtof_msgs):
                    if factor.var_ixs[j] == i:
                        etas_inc = torch.cat((etas_tp[:ix], etas_tp[ix + 1 :]))
                        lams_inc = torch.cat((lams_tp[:ix], lams_tp[ix + 1 :]))

                        lam_a = lams_inc.sum(dim=0)
                        if lam_a.count_nonzero() == 0:
                            msg.zero_message()
                        else:
                            inv_lam_a = torch.linalg.inv(lam_a)
                            sum_etas = etas_inc.sum(dim=0)
                            mean_a = torch.matmul(
                                inv_lam_a, sum_etas.unsqueeze(-1)
                            ).squeeze(-1)
                            new_mess = th.retract_gaussian(var, mean_a, lam_a)
                            msg.update(new_mess.mean, new_mess.precision)
                        ix += 1

            # update belief mean and variance
            # if no incoming messages then leave current belief unchanged
            if update_belief and lam_tau.count_nonzero() != 0:
                inv_lam_tau = torch.inverse(lam_tau)
                sum_taus = etas_tp.sum(dim=0)
                tau = torch.matmul(inv_lam_tau, sum_taus.unsqueeze(-1)).squeeze(-1)

                new_belief = th.retract_gaussian(var, tau, lam_tau)
                self.beliefs[i].update(new_belief.mean, new_belief.precision)

    def _pass_var_to_fac_messages_vectorized(self, update_belief=True):
        # Each (variable-type, dof) gets mapped to a tuple with:
        #   - the variable that will hold the vectorized data
        #   - all the variables of that type that will be vectorized together
        #   - list of variable indices from ordering
        #   - tensor that will hold incoming messages [eta, lam] in the belief tangent plane
        var_info: Dict[
            Tuple[Type[Manifold], int],
            Tuple[Manifold, List[Manifold], List[int], List[torch.Tensor]],
        ] = {}

        batch_size = -1
        # Create var info by looping variables in the given order
        # All variables of the same type get grouped together
        for ix, var in enumerate(self.ordering):
            if batch_size == -1:
                batch_size = var.shape[0]
            else:
                assert batch_size == var.shape[0]

            var_type = (var.__class__, var.dof())
            if var_type not in var_info:
                var_info[var_type] = (var.copy(), [], [], [])
            var_info[var_type][1].append(var)
            var_info[var_type][2].append(ix)

        for var_type, (vectorized_var, var_list, _, eta_lam) in var_info.items():
            n_vars, dof = len(var_list), vectorized_var.dof()

            # Get the vectorized tensor that has the current variable data.
            # The resulting shape is (N * b, M), b is batch size, N is the number of
            # variables in the group, and M is the data shape for this class
            vectorized_data = torch.cat([v.tensor for v in var_list], dim=0)
            assert (
                vectorized_data.shape
                == (n_vars * batch_size,) + vectorized_data.shape[1:]
            )
            vectorized_var.update(vectorized_data)

            eta_tp_acc = torch.zeros(n_vars * batch_size, dof)
            lam_tp_acc = torch.zeros(n_vars * batch_size, dof, dof)
            eta_tp_acc = eta_tp_acc.to(vectorized_data.device, vectorized_data.dtype)
            lam_tp_acc = lam_tp_acc.to(vectorized_data.device, vectorized_data.dtype)
            eta_lam.extend([eta_tp_acc, lam_tp_acc])

        # add ftov messages to eta_tp and lam_tp accumulator tensors
        for factor in self.factors:
            for i, msg in enumerate(factor.ftov_msgs):

                # transform messages to tangent plane at the current variable value
                eta_tp, lam_tp = th.local_gaussian(
                    factor.cf.optim_var_at(i), msg, return_mean=False
                )

                receiver_var_type = (msg.mean[0].__class__, msg.mean[0].dof())
                # get indices of the vectorized variables that receive each message
                if factor.vectorized_var_ixs[i] is None:
                    receiver_var_ixs = factor.var_ixs[
                        :, i
                    ]  # ixs of the receiver variables
                    var_type_ixs = torch.tensor(
                        var_info[receiver_var_type][2]
                    )  # all ixs for variables of this type
                    var_type_ixs = var_type_ixs[None, :].repeat(
                        len(receiver_var_ixs), 1
                    )
                    is_receiver = (var_type_ixs - receiver_var_ixs[:, None]) == 0
                    indices = is_receiver.nonzero()[:, 1]
                    # expand indices for all batch variables when batch size > 1
                    if self.objective.batch_size != 1:
                        indices = indices[:, None].repeat(1, self.objective.batch_size)
                        shift = (
                            torch.arange(self.objective.batch_size)[None, :]
                            .long()
                            .to(indices.device)
                        )
                        indices = indices + shift
                        indices = indices.flatten()
                    indices = indices.to(factor.cf.optim_var_at(0).device)
                    factor.vectorized_var_ixs[i] = indices

                #  add messages to correct variable using indices
                eta_tp_acc = var_info[receiver_var_type][3][0]
                lam_tp_acc = var_info[receiver_var_type][3][1]
                eta_tp_acc.index_add_(0, factor.vectorized_var_ixs[i], eta_tp)
                lam_tp_acc.index_add_(0, factor.vectorized_var_ixs[i], lam_tp)

        # compute variable to factor messages, now all incoming messages are accumulated
        for factor in self.factors:
            for i, msg in enumerate(factor.vtof_msgs):

                # transform messages to tangent plane at the current variable value
                ftov_msg = factor.ftov_msgs[i]
                eta_tp, lam_tp = th.local_gaussian(
                    factor.cf.optim_var_at(i), ftov_msg, return_mean=False
                )

                # new outgoing message is belief - last incoming mesage (in log space parameters)
                receiver_var_type = (msg.mean[0].__class__, msg.mean[0].dof())
                eta_tp_acc = var_info[receiver_var_type][3][0]
                lam_tp_acc = var_info[receiver_var_type][3][1]
                sum_etas = eta_tp_acc[factor.vectorized_var_ixs[i]] - eta_tp
                lam_a = lam_tp_acc[factor.vectorized_var_ixs[i]] - lam_tp

                if lam_a.count_nonzero() == 0:
                    msg.zero_message()
                else:
                    valid_lam = lam_a.count_nonzero(1, 2) != 0
                    inv_lam_a = torch.zeros_like(
                        lam_a, dtype=lam_a.dtype, device=lam_a.device
                    )
                    inv_lam_a[valid_lam] = torch.linalg.inv(lam_a[valid_lam])
                    mean_a = torch.matmul(inv_lam_a, sum_etas.unsqueeze(-1)).squeeze(-1)
                    new_mess = th.retract_gaussian(
                        factor.cf.optim_var_at(i), mean_a, lam_a
                    )
                    msg.update(new_mess.mean, new_mess.precision)

        # compute the new belief for the vectorized variables
        for (vectorized_var, _, var_ixs, eta_lam) in var_info.values():
            eta_tp_acc = eta_lam[0]
            lam_tau = eta_lam[1]

            if update_belief and lam_tau.count_nonzero() != 0:
                inv_lam_tau = torch.inverse(lam_tau)
                tau = torch.matmul(inv_lam_tau, eta_tp_acc.unsqueeze(-1)).squeeze(-1)

                new_belief = th.retract_gaussian(vectorized_var, tau, lam_tau)
                # update non vectorized beliefs with slices
                start_idx = 0
                for ix in var_ixs:
                    belief_mean_slice = new_belief.mean[0][
                        start_idx : start_idx + batch_size
                    ]
                    belief_precision_slice = new_belief.precision[
                        start_idx : start_idx + batch_size
                    ]
                    self.beliefs[ix].update([belief_mean_slice], belief_precision_slice)
                    start_idx += batch_size

    def _linearize_factors(self, relin_threshold: float = None):
        relins = 0
        for factor in self.factors:
            factor.linearize(relin_threshold=relin_threshold)
            relins += int((factor.steps_since_lin == 0).sum().item())
        return relins

    def _pass_fac_to_var_messages(self, schedule: torch.Tensor, damping: torch.Tensor):
        start_d = 0
        for j, factor in enumerate(self.factors):
            num_optim_vars = factor.cf.num_optim_vars()
            n_edges = num_optim_vars * factor.batch_size
            damping_tsr = damping[start_d : start_d + n_edges]
            schedule_tsr = schedule[start_d : start_d + n_edges]
            damping_tsr = damping_tsr.reshape(num_optim_vars, factor.batch_size)
            schedule_tsr = schedule_tsr.reshape(num_optim_vars, factor.batch_size)
            start_d += n_edges

            if schedule_tsr.sum() != 0:
                factor.comp_mess(damping_tsr, schedule_tsr)

    """
    Optimization loop functions
    """

    # loop for the iterative optimizer
    def _optimize_loop(
        self,
        num_iter: int,
        info: NonlinearOptimizerInfo,
        verbose: bool,
        truncated_grad_loop: bool,
        relin_threshold: float,
        damping: float,
        dropout: float,
        schedule: GBPSchedule,
        lin_system_damping: float,
        clear_messages: bool = True,
        **kwargs,
    ):
        if damping > 1.0 or damping < 0.0:
            raise ValueError(f"Damping must be between 0 and 1. Got {damping}.")
        if dropout > 1.0 or dropout < 0.0:
            raise ValueError(
                f"Dropout probability must be between 0 and 1. Got {dropout}."
            )
        if dropout > 0.9:
            print(
                "Disabling vectorization due to dropout > 0.9 in GBP message schedule."
            )
            self.objective.disable_vectorization()

        # initialise beliefs
        for var in self.ordering:
            self.beliefs.append(th.ManifoldGaussian([var]))

        if clear_messages:
            self.n_individual_factors = (
                len(self.objective.cost_functions) * self.objective.batch_size
            )

            if self.objective.vectorized:
                cf_iterator = iter(self.objective.vectorized_cost_fns)
                self._pass_var_to_fac_messages = (
                    self._pass_var_to_fac_messages_vectorized
                )
            else:
                cf_iterator = self.objective._get_iterator()
                self._pass_var_to_fac_messages = self._pass_var_to_fac_messages_loop

            # compute factor potentials for the first time
            for i, cost_function in enumerate(cf_iterator):

                if self.objective.vectorized:
                    # create array for indexing the messages
                    base_cf_names = self.objective.vectorized_cf_names[i]
                    base_cfs = [
                        self.objective.get_cost_function(name) for name in base_cf_names
                    ]
                    var_ixs = torch.tensor(
                        [
                            [self.ordering.index_of(var.name) for var in cf.optim_vars]
                            for cf in base_cfs
                        ]
                    ).long()
                else:
                    var_ixs = torch.tensor(
                        [
                            self.ordering.index_of(var.name)
                            for var in cost_function.optim_vars
                        ]
                    ).long()

                self.factors.append(
                    Factor(
                        cost_function,
                        name=cost_function.name,
                        var_ixs=var_ixs,
                        lin_system_damping=lin_system_damping,
                    )
                )
            relins = self._linearize_factors()

        self.n_edges = sum(
            [factor.cf.num_optim_vars() * factor.batch_size for factor in self.factors]
        )
        if schedule == GBPSchedule.SYNCHRONOUS:
            ftov_schedule = synchronous_schedule(
                self.params.max_iterations, self.n_edges
            )

        self.ftov_msgs_history = {}

        converged_indices = torch.zeros_like(info.last_err).bool()
        iters_done = 0
        for it_ in range(num_iter):
            iters_done += 1
            curr_ftov_msgs = []
            for factor in self.factors:
                curr_ftov_msgs.extend([msg.copy() for msg in factor.ftov_msgs])
            self.ftov_msgs_history[it_] = curr_ftov_msgs

            # damping
            damping_arr = torch.full(
                [self.n_edges],
                damping,
                device=self.ordering[0].device,
                dtype=self.ordering[0].dtype,
            )
            # dropout is implemented by changing the schedule
            if dropout != 0.0 and it_ != 0:
                dropout_ixs = torch.rand(self.n_edges) < dropout
                ftov_schedule[it_, dropout_ixs] = False

            t0 = time.time()
            relins = self._linearize_factors(relin_threshold)
            t_relin = time.time() - t0

            t1 = time.time()
            self._pass_fac_to_var_messages(ftov_schedule[it_], damping_arr)
            t_ftov = time.time() - t1

            t1 = time.time()
            self._pass_var_to_fac_messages(update_belief=True)
            t_vtof = time.time() - t1

            t_vec = 0.0
            if self.objective.vectorized:
                t1 = time.time()
                self.objective.update_vectorization_if_needed()
                t_vec = time.time() - t1

            t_tot = time.time() - t0
            if verbose:
                print(
                    f"Timings ----- relin {t_relin:.4f}, ftov {t_ftov:.4f}, vtof {t_vtof:.4f},"
                    f" vectorization {t_vec:.4f}, TOTAL {t_tot:.4f}"
                )

            # check for convergence
            if it_ >= 0:
                with torch.no_grad():
                    err = self.objective.error_squared_norm() / 2
                    self._update_info(info, it_, err, converged_indices)
                    if verbose:
                        print(
                            f"GBP. Iteration: {it_+1}. Error: {err.mean().item():.4f}. "
                            f"Relins: {relins} / {self.n_individual_factors}"
                        )
                    converged_indices = self._check_convergence(err, info.last_err)
                    info.status[
                        converged_indices.cpu().numpy()
                    ] = NonlinearOptimizerStatus.CONVERGED
                    if converged_indices.all() and it_ > 1:
                        break  # nothing else will happen at this point
                    info.last_err = err

        info.status[
            info.status == NonlinearOptimizerStatus.START
        ] = NonlinearOptimizerStatus.MAX_ITERATIONS
        return iters_done

    # `track_best_solution` keeps a **detached** copy (as in no gradient info)
    # of the best variables found, but it is optional to avoid unnecessary copying
    # if this is not needed
    def _optimize_impl(
        self,
        track_best_solution: bool = False,
        track_err_history: bool = False,
        track_state_history: bool = False,
        verbose: bool = False,
        backward_mode: BackwardMode = BackwardMode.FULL,
        relin_threshold: float = 0.1,
        damping: float = 0.0,
        dropout: float = 0.0,
        schedule: GBPSchedule = GBPSchedule.SYNCHRONOUS,
        lin_system_damping: float = 1e-6,
        **kwargs,
    ) -> NonlinearOptimizerInfo:
        with torch.no_grad():
            info = self._init_info(
                track_best_solution, track_err_history, track_state_history
            )

        if verbose:
            print(
                f"GBP optimizer. Iteration: 0. " f"Error: {info.last_err.mean().item()}"
            )

        if backward_mode == BackwardMode.FULL:
            self._optimize_loop(
                num_iter=self.params.max_iterations,
                info=info,
                verbose=verbose,
                truncated_grad_loop=False,
                relin_threshold=relin_threshold,
                damping=damping,
                dropout=dropout,
                schedule=schedule,
                lin_system_damping=lin_system_damping,
                **kwargs,
            )

            # If didn't coverge, remove misleading converged_iter value
            info.converged_iter[
                info.status == NonlinearOptimizerStatus.MAX_ITERATIONS
            ] = -1
            return info

        elif backward_mode in [BackwardMode.IMPLICIT, BackwardMode.TRUNCATED]:
            if backward_mode == BackwardMode.IMPLICIT:
                backward_num_iterations = 1
            else:
                if "backward_num_iterations" not in kwargs:
                    raise ValueError(
                        "backward_num_iterations expected but not received"
                    )
                if kwargs["backward_num_iterations"] > self.params.max_iterations:
                    warnings.warn(
                        f"Input backward_num_iterations "
                        f"(={kwargs['backward_num_iterations']}) > "
                        f"max_iterations (={self.params.max_iterations}). "
                        f"Using backward_num_iterations=max_iterations."
                    )
                backward_num_iterations = min(
                    kwargs["backward_num_iterations"], self.params.max_iterations
                )

            num_no_grad_iter = self.params.max_iterations - backward_num_iterations
            with torch.no_grad():
                # actual_num_iters could be < num_iter due to early convergence
                no_grad_iters_done = self._optimize_loop(
                    num_iter=num_no_grad_iter,
                    info=info,
                    verbose=verbose,
                    truncated_grad_loop=False,
                    relin_threshold=relin_threshold,
                    damping=damping,
                    dropout=dropout,
                    schedule=schedule,
                    lin_system_damping=lin_system_damping,
                    **kwargs,
                )

            grad_loop_info = self._init_info(
                track_best_solution, track_err_history, track_state_history
            )
            grad_iters_done = self._optimize_loop(
                num_iter=backward_num_iterations,
                info=grad_loop_info,
                verbose=verbose,
                truncated_grad_loop=True,
                relin_threshold=relin_threshold,
                damping=damping,
                dropout=dropout,
                schedule=schedule,
                lin_system_damping=lin_system_damping,
                clear_messages=False,
                **kwargs,
            )

            # Adds grad_loop_info results to original info
            self._merge_infos(grad_loop_info, no_grad_iters_done, grad_iters_done, info)

            return info
        else:
            raise ValueError("Unrecognized backward mode")