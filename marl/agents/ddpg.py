from collections import defaultdict
import torch
from torch import Tensor
from torch.autograd import Variable
from torch.optim import Adam
from gym.spaces import Box, Discrete, Dict

from agents.policy import Policy, DEFAULT_ACTION
from utils.networks import MLPNetwork, RecurrentNetwork, rnn_forward_sequence
from utils.misc import hard_update, gumbel_softmax, onehot_from_logits
from utils.noise import OUNoise


############################################ ddpg 
class DDPGAgent(object):
    """
    General class for DDPG agents (policy, critic, target policy, target
    critic, exploration noise)
    """
    def __init__(self, algo_type="MADDPG", act_space=None, obs_space=None, 
                rnn_policy=False, rnn_critic=False, hidden_dim=64, lr=0.01,
                norm_in=False, constrain_out=False,
                env_obs_space=None, env_act_space=None, **kwargs):
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


    def _soft_act(self, x, requires_grad=True):    
        """ soften action if discrete, x: (B,A) """
        if not self.discrete_action:
            return x 
        if requires_grad:
            return gumbel_softmax(x, hard=True)
        else:
            return onehot_from_logits(x)


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
                for k, a in act_t.items():
                    act[k].append(self._soft_act(a, requires_grad))
            act = {
                k: torch.stack(ac, 0).permute(1,0,2) 
                for k, ac in act.items()
            }   # dict [(B,A)]*T -> dict (B,T,A)
        else:
            stacked_obs = obs.reshape(bs*ts, -1)  # (B*T,O)
            act, _ = pi(stacked_obs)  # act is dict of (B*T,A)
            act = {
                k: self._soft_act(ac, requires_grad).reshape(bs, ts, -1)  
                for k, ac in act.items()
            }   # dict of (B,T,A)
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
            action, hidden_states = self.policy(obs, self.policy_hidden_states)
            self.policy_hidden_states = hidden_states   # if mlp, still defafult None

            if self.discrete_action:
                for k in action:
                    if explore:
                        action[k] = gumbel_softmax(action[k], hard=True)
                    else:
                        action[k] = onehot_from_logits(action[k])
            else:  # continuous action
                idx = 0 
                noise = Variable(Tensor(self.exploration.noise()),
                                    requires_grad=False)
                for k in action:
                    if explore:
                        dim = action[k].shape[-1]
                        action[k] += noise[idx : idx+dim]
                        idx += dim 
                    action[k] = action[k].clamp(-1, 1)
        return action


    def get_params(self):
        return {
            'policy': self.policy.state_dict(),
            'critic': self.critic.state_dict(),
            'target_policy': self.target_policy.state_dict(),
            'target_critic': self.target_critic.state_dict(),
            'policy_optimizer': self.policy_optimizer.state_dict(),
            'critic_optimizer': self.critic_optimizer.state_dict()
        }

    def load_params(self, params):
        self.policy.load_state_dict(params['policy'])
        self.critic.load_state_dict(params['critic'])
        self.target_policy.load_state_dict(params['target_policy'])
        self.target_critic.load_state_dict(params['target_critic'])
        self.policy_optimizer.load_state_dict(params['policy_optimizer'])
        self.critic_optimizer.load_state_dict(params['critic_optimizer'])

    