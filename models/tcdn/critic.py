import torch as t
import torch.nn as nn
from typing import Union
from models.base.tcdnnet import TCDNNet


class SwarmCritic(nn.Module):
    def __init__(self, observe_dim, action_dim, history_depth, neighbor_num, device="cuda:0"):
        super(SwarmCritic, self).__init__()
        in_dim = observe_dim + action_dim + 1
        seq_length = (history_depth + 1) * (neighbor_num + 1)
        self.add_module("net", TCDNNet(in_dim, 1, seq_length,
                                       final_process=None, device=device))
        self.device = device
        self.observe_dim = observe_dim
        self.action_dim = action_dim

    def forward(self,
                observation: t.Tensor,
                action: t.Tensor,
                neighbor_observation: t.Tensor,
                neighbor_action: t.Tensor,
                history: t.Tensor,
                history_time_steps: t.Tensor,
                time_step: t.Tensor):

        full_observe = t.cat((t.unsqueeze(observation, dim=1), neighbor_observation), dim=1)
        full_action = t.cat((t.unsqueeze(action, dim=1), neighbor_action), dim=1)

        # observe and action are known, reward in current step is unknown, set to 0
        curr_state = t.zeros((full_observe.shape[0], full_observe.shape[1],
                              self.observe_dim + self.action_dim + 1),
                             dtype=full_observe.dtype, device=self.device)
        curr_state[:, :, :self.observe_dim] = full_observe.to(self.device)
        curr_state[:, :, self.observe_dim: self.observe_dim + self.action_dim] = full_action.to(self.device)

        curr_state = t.cat((history, curr_state), dim=1)
        time_steps = t.cat((history_time_steps, time_step), dim=1)

        return self.net(curr_state,
                        time_steps=time_steps)[0]


class WrappedCriticNet(nn.Module):
    def __init__(self, critic):
        super(WrappedCriticNet, self).__init__()
        self.add_module("critic", critic)

    def forward(self,
                observation,
                neighbor_observation,
                action,
                neighbor_action_all,
                history,
                history_time_steps,
                time_step):
        if neighbor_action_all.shape[1] > 0:
            return self.critic.forward(observation, action,
                                       neighbor_observation, neighbor_action_all[:, -1], history,
                                       history_time_steps, time_step)
