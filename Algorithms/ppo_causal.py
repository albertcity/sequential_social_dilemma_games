import copy
import sys

import numpy as np
import scipy

# TODO(@evinitsky) put this in alphabetical order

import ray
from ray.rllib.agents.ppo.ppo_policy import PPOTFPolicy, PPOLoss, BEHAVIOUR_LOGITS, \
    KLCoeffMixin, setup_config, clip_gradients, \
    kl_and_loss_stats
# TODO(@evinitsky) move config vals into a default config
from ray.rllib.agents.ppo.ppo import DEFAULT_CONFIG, choose_policy_optimizer, \
    validate_config, update_kl, warn_about_bad_reward_scales
from ray.rllib.evaluation.postprocessing import compute_advantages, \
    Postprocessing
from ray.rllib.models import ModelCatalog
from ray.rllib.policy.sample_batch import SampleBatch
from ray.rllib.policy.tf_policy import LearningRateSchedule, \
    EntropyCoeffSchedule, ACTION_LOGP
from ray.rllib.utils import try_import_tf
from ray.rllib.policy.tf_policy_template import build_tf_policy
from ray.rllib.agents.trainer_template import build_trainer
from ray.rllib.utils.tf_ops import make_tf_callable

CONFIG = DEFAULT_CONFIG
CONFIG.update({"num_other_agents": 1,
               "moa_weight": 10.0,
               "train_moa_only_when_visible": True,
               "influence_reward_clip": 10,
               "influence_reward_weight": 1.0,
               "influence_curriculum_steps": 10e6,
               "influence_scaledown_start": 100e6,
               "influence_scaledown_end": 300e6,
               "influence_scaledown_final_val": .5,
               "influence_only_when_visible": True,
               "influence_divergence_measure": "kl"})

tf = try_import_tf()

MOA_PREDS = "moa_preds"
OTHERS_ACTIONS = "others_actions"
ALL_ACTIONS = "all_actions"
VISIBILITY = "others_visibility"
VISIBILITY_MATRIX = "visibility_matrix"

# Frozen logits of the policy that computed the action
ACTION_LOGITS = "action_logits"
BEHAVIOUR_LOGITS = "behaviour_logits"
COUNTERFACTUAL_ACTIONS = "counterfactual_actions"
POLICY_SCOPE = "func"


def kl_div(p, q):
    """Kullback-Leibler divergence D(P || Q) for discrete probability dists

    Assumes the probability dist is over the last dimension.

    Taken from: https://gist.github.com/swayson/86c296aa354a555536e6765bbe726ff7

    p, q : array-like, dtype=float
    """
    p = np.asarray(p, dtype=np.float)
    q = np.asarray(q, dtype=np.float)

    kl = np.sum(np.where(p != 0, p * np.log(p / q), 0), axis=-1)

    # Don't return nans or infs
    if np.all(np.isfinite(kl)):
        return kl
    else:
        return np.zeros(kl.shape)


class MOALoss(object):
    def __init__(self, pred_logits, true_actions, num_actions,
                 loss_weight=1.0, others_visibility=None):
        """Train MOA model with supervised cross entropy loss on a trajectory.
        The model is trying to predict others' actions at timestep t+1 given all
        actions at timestep t.
        Returns:
            A scalar loss tensor (cross-entropy loss).
        """
        # Remove the prediction for the final step, since t+1 is not known for
        # this step.
        action_logits = pred_logits[:-1, :, :]  # [B, N, A]

        # # Remove first agent (self) and first action, because we want to predict
        # # the t+1 actions of other agents from all actions at t.
        true_actions = tf.cast(true_actions[1:, 1:], tf.int32)  # [B, N]

        # Compute softmax cross entropy
        self.ce_per_entry = tf.nn.sparse_softmax_cross_entropy_with_logits(labels=true_actions, logits=action_logits)

        # Zero out the loss if the other agent isn't visible to this one.
        if others_visibility is not None:
            # Remove first entry in ground truth visibility and flatten
            others_visibility = others_visibility[1:, :]
            self.ce_per_entry *= tf.cast(others_visibility, tf.float32)

        self.total_loss = tf.reduce_mean(self.ce_per_entry) * loss_weight
        tf.Print(self.total_loss, [self.total_loss], message="MOA CE loss")


def loss_with_moa(policy, model, dist_class, train_batch):
    # you need to override this bit to pull out the right bits from train_batch
    logits, state = model.from_batch(train_batch)
    action_dist = dist_class(logits, model)

    # Instantiate the prediction loss
    moa_preds = model.moa_preds_from_batch(train_batch)
    moa_preds = tf.reshape(moa_preds, [-1, policy.model.num_other_agents, logits.shape[-1]])
    others_actions = train_batch[ALL_ACTIONS]
    # 0/1 multiplier array representing whether each agent is visible to
    # the current agent.
    if policy.train_moa_only_when_visible:
        # if VISIBILITY in train_batch:
        others_visibility = train_batch[VISIBILITY]
    else:
        others_visibility = None
    moa_loss = MOALoss(moa_preds, others_actions,
                       logits.shape[-1], loss_weight=policy.moa_weight,
                       others_visibility=others_visibility)
    policy.moa_loss = moa_loss.total_loss

    if state:
        max_seq_len = tf.reduce_max(train_batch["seq_lens"])
        mask = tf.sequence_mask(train_batch["seq_lens"], max_seq_len)
        mask = tf.reshape(mask, [-1])
    else:
        mask = tf.ones_like(
            train_batch[Postprocessing.ADVANTAGES], dtype=tf.bool)

    policy.loss_obj = PPOLoss(
        policy.action_space,
        dist_class,
        model,
        train_batch[Postprocessing.VALUE_TARGETS],
        train_batch[Postprocessing.ADVANTAGES],
        train_batch[SampleBatch.ACTIONS],
        train_batch[BEHAVIOUR_LOGITS],
        train_batch[ACTION_LOGP],
        train_batch[SampleBatch.VF_PREDS],
        action_dist,
        model.value_function(),
        policy.kl_coeff,
        mask,
        entropy_coeff=policy.entropy_coeff,
        clip_param=policy.config["clip_param"],
        vf_clip_param=policy.config["vf_clip_param"],
        vf_loss_coeff=policy.config["vf_loss_coeff"],
        use_gae=policy.config["use_gae"],
        model_config=policy.config["model"])

    policy.loss_obj.loss += moa_loss.total_loss
    return policy.loss_obj.loss


def postprocess_trajectory(policy,
                           sample_batch,
                           other_agent_batches=None,
                           episode=None):
    # Extract matrix of self and other agents' actions.
    own_actions = np.atleast_2d(np.array(sample_batch['actions']))
    own_actions = np.reshape(own_actions, [-1, 1])
    all_actions = np.hstack((own_actions, sample_batch[OTHERS_ACTIONS]))
    sample_batch[ALL_ACTIONS] = all_actions

    # Compute causal social influence reward and add to batch.
    sample_batch = compute_influence_reward(policy, sample_batch)

    completed = sample_batch["dones"][-1]
    if completed:
        last_r = 0.0
    else:
        next_state = []
        for i in range(policy.num_state_tensors()):
            next_state.append([sample_batch["state_out_{}".format(i)][-1]])
        last_r = policy._value(sample_batch[SampleBatch.NEXT_OBS][-1],
                               sample_batch[SampleBatch.ACTIONS][-1],
                               sample_batch[SampleBatch.REWARDS][-1],
                               *next_state)
    sample_batch = compute_advantages(
        sample_batch,
        last_r,
        policy.config["gamma"],
        policy.config["lambda"],
        use_gae=policy.config["use_gae"])

    return sample_batch


def compute_influence_reward(policy, trajectory):
    """Compute influence of this agent on other agents and add to rewards.
    """
    # Probability of the next action for all other agents. Shape is [B, N, A]. This is the predicted probability
    # given the actions that we DID take. 
    # extract out the probability under the actions we actually did take
    true_probs = trajectory[COUNTERFACTUAL_ACTIONS]
    traj_index = list(range(len(trajectory['obs'])))
    true_probs = true_probs[traj_index, :, trajectory['actions'], :]
    true_probs = np.reshape(true_probs, [true_probs.shape[0], policy.num_other_agents, -1])
    true_probs = scipy.special.softmax(true_probs, axis=-1)
    true_probs = true_probs / true_probs.sum(axis=-1, keepdims=1)  # reduce numerical inaccuracies

    # Get marginal predictions where effect of self is marginalized out
    marginal_probs = marginalize_predictions_over_own_actions(policy, trajectory)  # [B, Num agents, Num actions]

    # Compute influence per agent/step ([B, N]) using different metrics
    if policy.influence_divergence_measure == 'kl':
        influence_per_agent_step = kl_div(true_probs, marginal_probs)
    elif policy.influence_divergence_measure == 'jsd':
        mean_probs = 0.5 * (true_probs + marginal_probs)
        influence_per_agent_step = (0.5 * kl_div(true_probs, mean_probs) +
                                    0.5 * kl_div(marginal_probs, mean_probs))
    else:
        sys.exit("Please specify an influence divergence measure from [kl, jsd]")

    # Zero out influence for steps where the other agent isn't visible.
    if policy.influence_only_when_visible:
        # if VISIBILITY in trajectory.keys():
        visibility = trajectory[VISIBILITY]
        # else:
        #     visibility = get_agent_visibility_multiplier(trajectory, policy.num_other_agents)
        influence_per_agent_step *= visibility

    # Summarize and clip influence reward
    influence = np.sum(influence_per_agent_step, axis=-1)
    influence = np.clip(influence, -policy.influence_reward_clip, policy.influence_reward_clip)

    # Get influence curriculum weight
    # TODO(@evinitsky) move this into a schedule mixin
    policy.steps_processed += len(trajectory['obs'])
    inf_weight = policy.curr_influence_weight

    # Add to trajectory
    trajectory['total_influence'] = influence
    trajectory['reward_without_influence'] = trajectory['rewards']
    trajectory['rewards'] = trajectory['rewards'] + (influence * inf_weight)

    return trajectory


def agent_name_to_idx(agent_num, self_id):
    """split agent id around the index and return its appropriate position in terms of the other agents"""
    agent_num = int(agent_num)
    if agent_num > self_id:
        return agent_num - 1
    else:
        return agent_num


def get_agent_visibility_multiplier(trajectory, num_other_agents, agent_ids):
    traj_len = len(trajectory['obs'])
    visibility = np.zeros((traj_len, num_other_agents))
    for i, v in enumerate(trajectory[VISIBILITY]):
        vis_agents = [agent_name_to_idx(a, agent_ids[i]) for a in v]
        visibility[i, vis_agents] = 1
    return visibility


def marginalize_predictions_over_own_actions(policy, trajectory):
    # Probability of each action in original trajectory
    action_probs = scipy.special.softmax(trajectory[ACTION_LOGITS], axis=-1)

    # Normalize to reduce numerical inaccuracies
    action_probs = action_probs / action_probs.sum(axis=1, keepdims=1)

    # Indexing of this is [B, Num agents, Agent actions, other agent logits] before we marginalize
    counter_probs = trajectory[COUNTERFACTUAL_ACTIONS]
    counter_probs = np.reshape(counter_probs, [counter_probs.shape[0], policy.num_other_agents, -1, action_probs.shape[-1]])
    counter_probs = scipy.special.softmax(counter_probs, axis=-1)
    marginal_probs = np.sum(counter_probs, axis=-2)

    # Multiply by probability of each action to renormalize probability
    tiled_probs = np.tile(action_probs, [1, policy.num_other_agents, 1])
    marginal_probs = np.multiply(marginal_probs, tiled_probs)

    # Normalize to reduce numerical inaccuracies
    marginal_probs = marginal_probs / marginal_probs.sum(axis=2, keepdims=1)

    return marginal_probs


def extract_last_actions_from_episodes(episodes, batch_type=False,
                                       own_actions=None):
    """Pulls every other agent's previous actions out of structured data.
    Args:
        episodes: the structured data type. Typically a dict of episode
            objects.
        batch_type: if True, the structured data is a dict of tuples,
            where the second tuple element is the relevant dict containing
            previous actions.
        own_actions: an array of the agents own actions. If provided, will
            be the first column of the created action matrix.
    Returns: a real valued array of size [batch, num_other_agents] (meaning
        each agents' actions goes down one column, each row is a timestep)
    """
    if episodes is None:
        print("Why are there no episodes?")
        import ipdb;
        ipdb.set_trace()

    # Need to sort agent IDs so same agent is consistently in
    # same part of input space.
    agent_ids = sorted(episodes.keys())
    prev_actions = []

    for agent_id in agent_ids:
        if batch_type:
            prev_actions.append(episodes[agent_id][1]['actions'])
        else:
            prev_actions.append(
                [e.prev_action for e in episodes[agent_id]])

    all_actions = np.transpose(np.array(prev_actions))

    # Attach agents own actions as column 1
    if own_actions is not None:
        all_actions = np.hstack((own_actions, all_actions))

    return all_actions


def extra_fetches(policy):
    """Adds value function, logits, moa predictions of counterfactual actions to experience train_batches."""
    return {
        # TODO(@evinitsky) are there any other of these that shouldn't be frozen?
        SampleBatch.VF_PREDS: policy.model.value_function(),
        BEHAVIOUR_LOGITS: policy.model.last_output(),
        ACTION_LOGITS: policy.model.action_logits(),
        COUNTERFACTUAL_ACTIONS: policy.model.counterfactual_actions(),

        # TODO(@evinitsky) remove this once we figure out how to split the obs
        OTHERS_ACTIONS: policy.model.other_agent_actions(),
        VISIBILITY: policy.model.visibility()
    }


class ConfigInitializerMixIn(object):
    def __init__(self, config):
        self.train_moa_only_when_visible = config['train_moa_only_when_visible']
        self.num_other_agents = config['num_other_agents']
        self.moa_weight = config['moa_weight']
        self.train_moa_only_when_visible = config['train_moa_only_when_visible']
        self.influence_divergence_measure = config['influence_divergence_measure']
        self.influence_only_when_visible = config['influence_only_when_visible']
        self.influence_reward_clip = config['influence_reward_clip']


class InfluenceScheduleMixIn(object):
    def __init__(self, config):
        self.influence_reward_weight = config['influence_reward_weight']
        self.influence_curriculum_steps = config['influence_curriculum_steps']
        self.inf_scale_start = config['influence_scaledown_start']
        self.inf_scale_end = config['influence_scaledown_end']
        self.inf_scale_final_val = config['influence_scaledown_final_val']
        self.steps_processed = 0
        self.curr_influence_weight = self.influence_reward_weight

    def current_influence_curriculum_weight(self):
        """ Computes multiplier for influence reward based on training steps
        taken and curriculum parameters.

        Returns: scalar float influence weight
        """
        if self.steps_processed < self.influence_curriculum_steps:
            percent = float(self.steps_processed) / self.influence_curriculum_steps
            self.curr_influence_weight = percent * self.influence_reward_weight
        elif self.steps_processed > self.inf_scale_start:
            percent = (self.steps_processed - self.inf_scale_start) \
                      / float(self.inf_scale_end - self.inf_scale_start)
            diff = self.influence_reward_weight - self.inf_scale_final_val
            scaled = self.influence_reward_weight - diff * percent
            self.curr_influence_weight = max(self.inf_scale_final_val, scaled)
        else:
            self.curr_influence_weight = self.influence_reward_weight


def extra_stats(policy, train_batch):
    base_stats = kl_and_loss_stats(policy, train_batch)
    base_stats["total_influence"] = train_batch["total_influence"]
    base_stats['reward_without_influence'] = train_batch['reward_without_influence']
    base_stats['moa_loss'] = policy.moa_loss
    return base_stats


def extra_grad_fn(policy, train_batch, grads):
    import ipdb; ipdb.set_trace()
    return {}


def build_ppo_model(policy, obs_space, action_space, config):
    _, logit_dim = ModelCatalog.get_action_dist(action_space, config["model"])

    policy.model = ModelCatalog.get_model_v2(
        obs_space,
        action_space,
        logit_dim,
        config["model"],
        name=POLICY_SCOPE,
        framework="tf")

    return policy.model


class ValueNetworkMixin(object):
    def __init__(self, obs_space, action_space, config):
        if config["use_gae"]:

            @make_tf_callable(self.get_session())
            def value(ob, prev_action, prev_reward, *state):
                model_out, _ = self.model({
                    SampleBatch.CUR_OBS: tf.convert_to_tensor([ob]),
                    SampleBatch.PREV_ACTIONS: tf.convert_to_tensor(
                        [prev_action]),
                    SampleBatch.PREV_REWARDS: tf.convert_to_tensor(
                        [prev_reward]),
                    "is_training": tf.convert_to_tensor(False),
                }, [tf.convert_to_tensor([s]) for s in state],
                                          tf.convert_to_tensor([1]))
                return self.model.value_function()[0]

        else:
            @make_tf_callable(self.get_session())
            def value(ob, prev_action, prev_reward, *state):
                return tf.constant(0.0)

        self._value = value


def setup_mixins(policy, obs_space, action_space, config):
    ValueNetworkMixin.__init__(policy, obs_space, action_space, config)
    KLCoeffMixin.__init__(policy, config)
    EntropyCoeffSchedule.__init__(policy, config["entropy_coeff"],
                                  config["entropy_coeff_schedule"])
    LearningRateSchedule.__init__(policy, config["lr"], config["lr_schedule"])
    InfluenceScheduleMixIn.__init__(policy, config)
    ConfigInitializerMixIn.__init__(policy, config)


CausalMOA_PPOPolicy = build_tf_policy(
    name="CausalTFPolicy",
    get_default_config=lambda: ray.rllib.agents.ppo.ppo.DEFAULT_CONFIG,
    loss_fn=loss_with_moa,
    make_model=build_ppo_model,
    stats_fn=extra_stats,
    #grad_stats_fn=extra_grad_fn,
    extra_action_fetches_fn=extra_fetches,
    postprocess_fn=postprocess_trajectory,
    gradients_fn=clip_gradients,
    before_init=setup_config,
    before_loss_init=setup_mixins,
    mixins=[
        LearningRateSchedule, EntropyCoeffSchedule, KLCoeffMixin,
        ValueNetworkMixin, ConfigInitializerMixIn, InfluenceScheduleMixIn
    ])

CausalMOATrainer = build_trainer(
    name="CausalMOA",
    default_config=DEFAULT_CONFIG,
    default_policy=CausalMOA_PPOPolicy,
    make_policy_optimizer=choose_policy_optimizer,
    validate_config=validate_config,
    after_optimizer_step=update_kl,
    after_train_result=warn_about_bad_reward_scales)
