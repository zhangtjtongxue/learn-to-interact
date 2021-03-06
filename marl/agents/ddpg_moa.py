from collections import defaultdict
import torch
from torch import Tensor
from torch.autograd import Variable
from torch.optim import Adam
from gym.spaces import Box, Discrete, Dict
import torch.distributions as D

from agents.policy import Policy, DEFAULT_ACTION
from utils.networks import MLPNetwork, RecurrentNetwork, rnn_forward_sequence
from utils.misc import hard_update, gumbel_softmax, onehot_from_logits
from utils.noise import OUNoise
from agents.action_selectors import DiscreteActionSelector, ContinuousActionSelector


############################################ ddpg 
class DDPGAgentMOA(object):
    """
    General class for DDPG agents (policy, critic, target policy, target
    critic, exploration noise)
    NOTE: 
    - uses action selector (without manually evaluating log prob or entropy)
    - uses model of other agents 
    """
    def __init__(self, algo_type="MADDPG", act_space=None, obs_space=None, 
                rnn_policy=False, rnn_critic=False, hidden_dim=64, lr=0.01, 
                norm_in=False, constrain_out=False,
                env_obs_space=None, env_act_space=None, 
                model_of_agents=False, **kwargs
    ):
        """
        Inputs:
            act_space: single agent action space (single space or Dict)
            obs_space: single agent observation space (single space Dict)
        """
        self.algo_type = algo_type
        self.act_space = act_space 
        self.obs_space = obs_space

        # continuous or discrete action (only look at `move` action, assume
        # move and comm space both discrete or continuous)
        tmp = act_space.spaces["move"] if isinstance(act_space, Dict) else act_space
        self.discrete_action = False if isinstance(tmp, Box) else True 

        # Exploration noise 
        if not self.discrete_action:
            # `move`, `comm` share same continuous noise source
            self.exploration = OUNoise(self.get_shape(act_space))
        else:
            self.exploration = 0.3  # epsilon for eps-greedy
        
        # Policy (supports multiple outputs)
        self.rnn_policy = rnn_policy
        self.policy_hidden_states = None 

        num_in_pol = obs_space.shape[0]
        if isinstance(act_space, Dict):
            # hard specify now, could generalize later 
            num_out_pol = {
                "move": self.get_shape(act_space, "move"), 
                "comm": self.get_shape(act_space, "comm")
            }
        else:
            num_out_pol = self.get_shape(act_space)

        policy_kwargs = dict(
            hidden_dim=hidden_dim,
            norm_in=norm_in,
            constrain_out=constrain_out,
            discrete_action=self.discrete_action,
            rnn_policy=rnn_policy
        )
        self.policy = Policy(num_in_pol, num_out_pol, **policy_kwargs)
        self.target_policy = Policy(num_in_pol, num_out_pol, **policy_kwargs)
        hard_update(self.target_policy, self.policy)

        # action selector (distribution wrapper)
        if self.discrete_action:
            self.selector = DiscreteActionSelector()
        else:
            self.selector = ContinuousActionSelector()

        # Critic 
        self.rnn_critic = rnn_critic
        self.critic_hidden_states = None 
        
        if algo_type == "MADDPG":
            num_in_critic = 0
            for oobsp in env_obs_space:
                num_in_critic += oobsp.shape[0]
            for oacsp in env_act_space:
                # feed all acts to centralized critic
                num_in_critic += self.get_shape(oacsp)
        else:   # only DDPG, local critic 
            num_in_critic = obs_space.shape[0] + self.get_shape(act_space)

        critic_net_fn = RecurrentNetwork if rnn_critic else MLPNetwork
        critic_kwargs = dict(
            hidden_dim=hidden_dim,
            norm_in=norm_in,
            constrain_out=constrain_out
        )
        self.critic = critic_net_fn(num_in_critic, 1, **critic_kwargs)
        self.target_critic = critic_net_fn(num_in_critic, 1, **critic_kwargs)
        hard_update(self.target_critic, self.critic)

        # Optimizers 
        self.policy_optimizer = Adam(self.policy.parameters(), lr=lr)
        self.critic_optimizer = Adam(self.critic.parameters(), lr=lr)

        # model of agents (approximate models for other agents)
        self.model_of_agents = model_of_agents
        if model_of_agents:
            self.make_moa(hidden_dim=hidden_dim, lr=lr, rnn_policy=rnn_policy,
                        norm_in=norm_in, constrain_out=constrain_out,
                        env_obs_space=env_obs_space, env_act_space=env_act_space)


    def make_moa(self, hidden_dim=64, lr=0.01, rnn_policy=False,
                norm_in=False, constrain_out=False,
                env_obs_space=None, env_act_space=None
    ):
        """ instantiate a policy, target and optimizer for training 
            each of the other agents, assume current agent always have 
            position 0 in env obs and act spaces
        """
        self.moa_policies = {}
        self.moa_target_policies = {}
        self.moa_optimizers = {}
        self.moa_hidden_states = {}

        for i in range(1, len(env_act_space)):
            obs_space, act_space = env_obs_space[i], env_act_space[i]

            num_in_pol = obs_space.shape[0]
            if isinstance(act_space, Dict):
                # hard specify now, could generalize later 
                num_out_pol = {
                    "move": self.get_shape(act_space, "move"), 
                    "comm": self.get_shape(act_space, "comm")
                }
            else:
                num_out_pol = self.get_shape(act_space)

            policy_kwargs = dict(
                hidden_dim=hidden_dim,
                norm_in=norm_in,
                constrain_out=constrain_out,
                discrete_action=self.discrete_action,
                rnn_policy=rnn_policy
            )
            policy = Policy(num_in_pol, num_out_pol, **policy_kwargs)
            target_policy = Policy(num_in_pol, num_out_pol, **policy_kwargs)
            hard_update(target_policy, policy)

            # push to moa containers 
            self.moa_policies[i] = policy
            self.moa_target_policies[i] = target_policy
            self.moa_optimizers[i] = Adam(policy.parameters(), lr=lr)
            self.moa_hidden_states[i] = None


    def get_shape(self, x, key=None):
        """ func to infer action output shape """
        if isinstance(x, Dict):
            if key is None: # sum of action space dims
                return sum([
                    x.spaces[k].n if self.discrete_action else x.spaces[k].shape[0]
                    for k in x.spaces
                ])
            elif key in x.spaces:
                return x.spaces[key].n if self.discrete_action else x.spaces[key].shape[0]
            else:   # key not in action spaces
                return 0
        else:
            return x.n if self.discrete_action else x.shape[0]
    

    def reset_noise(self):
        if not self.discrete_action:
            self.exploration.reset()

    def scale_noise(self, scale):
        if self.discrete_action:
            self.exploration = scale
        else:
            self.exploration.scale = scale


    def init_hidden(self, batch_size):
        # (1,H) -> (B,H)
        # policy.init_hidden().unsqueeze(0).expand(batch_size, self.n_agents, -1)  
        if self.rnn_policy:
            self.policy_hidden_states = self.policy.init_hidden().expand(batch_size, -1)  
        if self.rnn_critic:
            self.critic_hidden_states = self.critic.init_hidden().expand(batch_size, -1) 


    def compute_value(self, vf_in, h_critic=None, target=False, truncate_steps=-1):
        """ training critic forward with specified policy 
        Arguments:
            vf_in: (B,T,K)
            target: if use target network
            truncate_steps: number of BPTT steps to truncate if used
        Returns:
            q: (B*T,1)
        """
        bs, ts, _ = vf_in.shape
        critic = self.target_critic if target else self.critic

        if self.rnn_critic:
            if h_critic is None:
                h_t = self.critic_hidden_states.clone() # (B,H)
            else:
                h_t = h_critic  #.clone()

            # rollout 
            q = rnn_forward_sequence(
                critic, vf_in, h_t, truncate_steps=truncate_steps)
            # q = []   # (B,1)*T
            # for t in range(ts):
            #     q_t, h_t = critic(vf_in[:,t], h_t)
            #     q.append(q_t)
            q = torch.stack(q, 0).permute(1,0,2)   # (T,B,1) -> (B,T,1)
            q = q.reshape(bs*ts, -1)  # (B*T,1)
        else:
            # (B,T,D) -> (B*T,1)
            q, _ = critic(vf_in.reshape(bs*ts, -1))
        return q 

    # # NOTE: V2 now uses action seletor (with backprop support)
    # def _soft_act(self, x, requires_grad=True):    
    #     """ soften action if discrete, x: (B,A) """
    #     if not self.discrete_action:
    #         return x 
    #     if requires_grad:
    #         return gumbel_softmax(x, hard=True)
    #     else:
    #         return onehot_from_logits(x)


    def compute_action(self, obs, h_actor=None, target=False, requires_grad=True, truncate_steps=-1):
        """ traininsg actor forward with specified policy 
        concat all actions to be fed in critics
        Arguments:
            obs: (B,T,O)
            target: if use target network
            requires_grad: if use _soft_act to differentiate discrete action
        Returns:
            act: dict of (B,T,A) 
        """
        bs, ts, _ = obs.shape
        pi = self.target_policy if target else self.policy

        if self.rnn_policy:
            if h_actor is None:
                h_t = self.policy_hidden_states.clone() # (B,H)
            else:
                h_t = h_actor   #.clone()

            # rollout 
            seq_logits = rnn_forward_sequence(
                pi, obs, h_t, truncate_steps=truncate_steps)
            # seq_logits = []  
            # for t in range(ts):
            #     act_t, h_t = pi(obs[:,t], h_t)  # act_t is dict (B,A)
            #     seq_logits.append(act_t)

            # soften deterministic output for backprop 
            act = defaultdict(list)
            for act_t in seq_logits:
                for k, logits in act_t.items():
                    # if requires_grad, need gumbel-softmax, same as in explore with low temperature
                    action, _ = self.selector.select_action(
                        logits, explore=False, hard=True,  
                        reparameterize=requires_grad, temperature=0.5
                    )
                    act[k].append(action)
                    # act[k].append(self._soft_act(logits, requires_grad))
            act = {
                k: torch.stack(ac, 0).permute(1,0,2) 
                for k, ac in act.items()
            }   # dict [(B,A)]*T -> dict (B,T,A)
        else:
            stacked_obs = obs.reshape(bs*ts, -1)  # (B*T,O)
            act, _ = pi(stacked_obs)  # act is dict of (B*T,A)
            # act is dict of (B,T,A)
            for k, logits in act.items():
                action, _ = self.selector.select_action(
                    logits, explore=False, hard=True, 
                    reparameterize=requires_grad, temperature=0.5)
                action = action.reshape(bs, ts, -1) 
                act[k] = action 
                # act[k] = self._soft_act(ac, requires_grad).reshape(bs, ts, -1)  
        return act
 

    def step(self, obs, explore=False):
        """
        Take a step forward in environment for a minibatch of observations
        equivalent to `act` or `compute_actions`
        Arguments:
            obs: (B,O)
            explore: Whether or not to add exploration noise
        Returns:
            action: dict of actions for this agent, (B,A)
        """
        with torch.no_grad():
            logits_d, hidden_states = self.policy(obs, self.policy_hidden_states)
            self.policy_hidden_states = hidden_states   # if mlp, still defafult None

            # customzied noise 
            noise = None
            idx = 0 
            if not self.discrete_action:
                noise = Variable(Tensor(self.exploration.noise()),
                                        requires_grad=False)

             # make distributions 
            act_d = {}
            for k, logits in logits_d.items():
                dim = logits.shape[-1]
                if not self.discrete_action:
                    noise = noise[idx:idx+dim]
                # use action selector with or without external noise
                action, dist = self.selector.select_action(
                    logits, explore=explore, hard=True, 
                    reparameterize=False, noise=noise
                )
                idx += dim 
                # clip action 
                if not self.discrete_action:    # continuous action
                    action = action.clamp(-1,  1)
                act_d[k] = action
        return act_d


    def get_params(self):
        params = {
            'policy': self.policy.state_dict(),
            'critic': self.critic.state_dict(),
            'target_policy': self.target_policy.state_dict(),
            'target_critic': self.target_critic.state_dict(),
            'policy_optimizer': self.policy_optimizer.state_dict(),
            'critic_optimizer': self.critic_optimizer.state_dict()
        }
        # moa 
        if self.model_of_agents:
            for i in self.moa_policies:
                params['moa_policy_{}'.format(i)] = self.moa_policies[i].state_dict()
                params['moa_target_policy_{}'.format(i)] = self.moa_target_policies[i].state_dict()
                params['moa_optimizer_{}'.format(i)] = self.moa_optimizers[i].state_dict()
        return params

    def load_params(self, params):
        self.policy.load_state_dict(params['policy'])
        self.critic.load_state_dict(params['critic'])
        self.target_policy.load_state_dict(params['target_policy'])
        self.target_critic.load_state_dict(params['target_critic'])
        self.policy_optimizer.load_state_dict(params['policy_optimizer'])
        self.critic_optimizer.load_state_dict(params['critic_optimizer'])
        # moa 
        if self.model_of_agents:
            for i in self.moa_policies:
                self.moa_policies[i].load_state_dict(params['moa_policy_{}'.format(i)])
                self.moa_target_policies[i].load_state_dict(params['moa_target_policy_{}'.format(i)])
                self.moa_optimizers[i].load_state_dict(params['moa_optimizer_{}'.format(i)])

    #####################################################################################
    ### MOA stuff 
    ####################################################################################

    def init_moa_hidden(self, batch_size):
        if self.model_of_agents and self.rnn_policy:
            for i, pi in self.moa_policies.items():
                self.moa_hidden_states[i] = pi.init_hidden().expand(batch_size, -1)  


    def evaluate_moa_action(self, agent_j, act_samples, obs, 
            h_actor=None, requires_grad=True, contract_keys=None, truncate_steps=-1
        ):
        """ traininsg actor forward with specified policy 
        concat all actions to be fed in critics
        Arguments:
            agent_j: use j-th moa agent 
            act_samples: dict of (B,T,A), actions in sample
            obs: (B,T,O)
            requires_grad: if use _soft_act to differentiate discrete action
            contract_keys: 
                list of keys to contract dict on
                i.e. sum up all fields in log_prob, entropy, kl on given keys
        Returns:
            log_prob: action log probs (B,T,1)
            entropy: action entropy (B,T,1)
        """
        bs, ts, _ = obs.shape
        pi = self.moa_policies[agent_j]
        log_prob_d, entropy_d = {}, {} 

        # get logits for current policy
        if self.rnn_policy:
            if h_actor is None:
                h_t = self.moa_hidden_states[agent_j].clone() # (B,H)
            else:
                h_t = h_actor   #.clone()

            # rollout 
            seq_logits = rnn_forward_sequence(
                pi, obs, h_t, truncate_steps=truncate_steps)

            # soften deterministic output for backprop 
            act = defaultdict(list)
            for act_t in seq_logits:
                for k, logits in act_t.items():
                    act[k].append(logits)
                    # act[k].append(self._soft_act(logits, requires_grad))
            act = {
                k: torch.stack(ac, 0).permute(1,0,2) 
                for k, ac in act.items()
            }   # dict [(B,A)]*T -> dict (B,T,A)
        else:
            stacked_obs = obs.reshape(bs*ts, -1)  # (B*T,O)
            act, _ = pi(stacked_obs)  # act is dict of (B*T,A)
            act = {
                k: ac.reshape(bs, ts, -1)  
                for k, ac in act.items()
            }   # dict of (B,T,A)

        if contract_keys is None:
            contract_keys = sorted(list(act.keys()))
        log_prob, entropy = 0.0, 0.0

        for k, seq_logits in act.items():
            if k not in contract_keys:
                continue
            action = act_samples[k]
            _, dist = self.selector.select_action(seq_logits, 
                explore=False, hard=False, reparameterize=False
            )
            # evaluate log prob (B,T) -> (B,T,1)
            # NOTE: attention!!! if log_prob on rsample action, backprop is done twice and wrong
            log_prob += dist.log_prob(
                action.clone().detach()
            ).unsqueeze(-1)
            # get current action distrib entropy
            entropy += dist.entropy().unsqueeze(-1)

        return log_prob, entropy


    def compute_moa_action(self, agent_j, obs, h_actor=None, target=False, 
        requires_grad=True, truncate_steps=-1, return_logits=True
    ):
        """ traininsg actor forward with specified policy 
        concat all actions to be fed in critics
        Arguments:
            obs: (B,T,O)
            target: if use target network
            requires_grad: if use _soft_act to differentiate discrete action
        Returns:
            act: dict of (B,T,A) 
        """
        bs, ts, _ = obs.shape
        if target:
            pi = self.moa_target_policies[agent_j]  
        else:
            pi = self.moa_policies[agent_j]

        if self.rnn_policy:
            if h_actor is None:
                h_t = self.policy_hidden_states.clone() # (B,H)
            else:
                h_t = h_actor   #.clone()

            # rollout 
            seq_logits = rnn_forward_sequence(
                pi, obs, h_t, truncate_steps=truncate_steps)

            # soften deterministic output for backprop 
            act = defaultdict(list)
            for act_t in seq_logits:
                for k, logits in act_t.items():
                    # if requires_grad, need gumbel-softmax, same as in explore with low temperature
                    if return_logits:
                        action = logits
                    else:
                        action, _ = self.selector.select_action(
                            logits, explore=False, hard=True,  
                            reparameterize=requires_grad, temperature=0.5
                        )
                    act[k].append(action)
                    # act[k].append(self._soft_act(logits, requires_grad))
            act = {
                k: torch.stack(ac, 0).permute(1,0,2) 
                for k, ac in act.items()
            }   # dict [(B,A)]*T -> dict (B,T,A)
        else:
            stacked_obs = obs.reshape(bs*ts, -1)  # (B*T,O)
            act, _ = pi(stacked_obs)  # act is dict of (B*T,A)
            # act is dict of (B,T,A)
            for k, logits in act.items():
                if return_logits:
                    action = logits
                else:
                    action, _ = self.selector.select_action(
                        logits, explore=False, hard=True, 
                        reparameterize=requires_grad, temperature=0.5
                    )
                action = action.reshape(bs, ts, -1) 
                act[k] = action 
                # act[k] = self._soft_act(ac, requires_grad).reshape(bs, ts, -1)  
        return act 

