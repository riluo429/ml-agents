from typing import Dict, cast
import torch

from mlagents.trainers.buffer import AgentBuffer

from mlagents_envs.timers import timed
from mlagents.trainers.policy.torch_policy import TorchPolicy
from mlagents.trainers.optimizer.torch_optimizer import TorchOptimizer
from mlagents.trainers.settings import TrainerSettings, PPOSettings
from mlagents.trainers.torch.utils import ModelUtils


class TorchPPOOptimizer(TorchOptimizer):
    def __init__(self, policy: TorchPolicy, trainer_settings: TrainerSettings):
        """
        Takes a Policy and a Dict of trainer parameters and creates an Optimizer around the policy.
        The PPO optimizer has a value estimator and a loss function.
        :param policy: A TFPolicy object that will be updated by this PPO Optimizer.
        :param trainer_params: Trainer parameters dictionary that specifies the
        properties of the trainer.
        """
        # Create the graph here to give more granular control of the TF graph to the Optimizer.

        super().__init__(policy, trainer_settings)
        params = list(self.policy.actor_critic.parameters())
        self.hyperparameters: PPOSettings = cast(
            PPOSettings, trainer_settings.hyperparameters
        )
        self.decay_schedule = self.hyperparameters.learning_rate_schedule

        self.optimizer = torch.optim.Adam(
            params, lr=self.trainer_settings.hyperparameters.learning_rate
        )
        self.stats_name_to_update_name = {
            "Losses/Value Loss": "value_loss",
            "Losses/Policy Loss": "policy_loss",
        }

        self.stream_names = list(self.reward_signals.keys())

    def ppo_value_loss(
        self,
        values: Dict[str, torch.Tensor],
        old_values: Dict[str, torch.Tensor],
        returns: Dict[str, torch.Tensor],
        epsilon: float,
    ) -> torch.Tensor:
        """
        Creates training-specific Tensorflow ops for PPO models.
        :param returns:
        :param old_values:
        :param values:
        """
        value_losses = []
        for name, head in values.items():
            old_val_tensor = old_values[name]
            returns_tensor = returns[name]
            clipped_value_estimate = old_val_tensor + torch.clamp(
                head - old_val_tensor, -1 * epsilon, epsilon
            )
            v_opt_a = (returns_tensor - head) ** 2
            v_opt_b = (returns_tensor - clipped_value_estimate) ** 2
            value_loss = torch.mean(torch.max(v_opt_a, v_opt_b))
            value_losses.append(value_loss)
        value_loss = torch.mean(torch.stack(value_losses))
        return value_loss

    def ppo_policy_loss(self, advantages, log_probs, old_log_probs, masks):
        """
        Creates training-specific Tensorflow ops for PPO models.
        :param masks:
        :param advantages:
        :param log_probs: Current policy probabilities
        :param old_log_probs: Past policy probabilities
        """
        advantage = advantages.unsqueeze(-1)

        decay_epsilon = self.hyperparameters.epsilon

        r_theta = torch.exp(log_probs - old_log_probs)
        p_opt_a = r_theta * advantage
        p_opt_b = (
            torch.clamp(r_theta, 1.0 - decay_epsilon, 1.0 + decay_epsilon) * advantage
        )
        policy_loss = -torch.mean(torch.min(p_opt_a, p_opt_b))
        return policy_loss

    @timed
    def update(self, batch: AgentBuffer, num_sequences: int) -> Dict[str, float]:
        """
        Performs update on model.
        :param batch: Batch of experiences.
        :param num_sequences: Number of sequences to process.
        :return: Results of update.
        """
        # Get decayed parameters
        decay_learning_rate = ModelUtils.get_decayed_parameter(
            self.decay_schedule,
            self.hyperparameters.learning_rate,
            1e-10,
            self.trainer_settings.max_steps,
            self.policy.get_current_step(),
        )
        decay_epsilon = ModelUtils.get_decayed_parameter(
            self.decay_schedule,
            self.hyperparameters.epsilon,
            0.1,
            self.trainer_settings.max_steps,
            self.policy.get_current_step(),
        )
        decay_beta = ModelUtils.get_decayed_parameter(
            self.decay_schedule,
            self.hyperparameters.beta,
            1e-5,
            self.trainer_settings.max_steps,
            self.policy.get_current_step(),
        )
        returns = {}
        old_values = {}
        for name in self.reward_signals:
            old_values[name] = ModelUtils.list_to_tensor(
                batch[f"{name}_value_estimates"]
            )
            returns[name] = ModelUtils.list_to_tensor(batch[f"{name}_returns"])

        vec_obs = [ModelUtils.list_to_tensor(batch["vector_obs"])]
        act_masks = ModelUtils.list_to_tensor(batch["action_mask"])
        if self.policy.use_continuous_act:
            actions = ModelUtils.list_to_tensor(batch["actions"]).unsqueeze(-1)
        else:
            actions = ModelUtils.list_to_tensor(batch["actions"], dtype=torch.long)

        memories = [
            ModelUtils.list_to_tensor(batch["memory"][i])
            for i in range(0, len(batch["memory"]), self.policy.sequence_length)
        ]
        if len(memories) > 0:
            memories = torch.stack(memories).unsqueeze(0)

        if self.policy.use_vis_obs:
            vis_obs = []
            for idx, _ in enumerate(
                self.policy.actor_critic.network_body.visual_encoders
            ):
                vis_ob = ModelUtils.list_to_tensor(batch["visual_obs%d" % idx])
                vis_obs.append(vis_ob)
        else:
            vis_obs = []
        log_probs, entropy, values = self.policy.evaluate_actions(
            vec_obs,
            vis_obs,
            masks=act_masks,
            actions=actions,
            memories=memories,
            seq_len=self.policy.sequence_length,
        )
        value_loss = self.ppo_value_loss(values, old_values, returns, decay_epsilon)
        policy_loss = self.ppo_policy_loss(
            ModelUtils.list_to_tensor(batch["advantages"]),
            log_probs,
            ModelUtils.list_to_tensor(batch["action_probs"]),
            ModelUtils.list_to_tensor(batch["masks"], dtype=torch.int32),
        )
        loss = policy_loss + 0.5 * value_loss - decay_beta * torch.mean(entropy)

        # Set optimizer learning rate
        ModelUtils.apply_learning_rate(self.optimizer, decay_learning_rate)
        self.optimizer.zero_grad()
        loss.backward()

        self.optimizer.step()
        update_stats = {
            "Losses/Policy Loss": abs(policy_loss.detach().cpu().numpy()),
            "Losses/Value Loss": value_loss.detach().cpu().numpy(),
            "Policy/Learning Rate": decay_learning_rate,
            "Policy/Epsilon": decay_epsilon,
            "Policy/Beta": decay_beta,
        }

        return update_stats
