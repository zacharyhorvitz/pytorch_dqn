from collections import deque, namedtuple
import sys
import random

import torch
from gym.spaces import Space
import numpy as np
import torch.nn.functional as F
from torch.nn.functional import relu

from modules import GCN, CNN_NODE_ATTEN_BLOCK, CNN_2D_NODE_BLOCK,LINEAR_INV_BLOCK, NodeAtten, self_attention, malmo_build_gcn_param, contrastive_loss_func
from utils import sync_networks, conv2d_size_out

Experience = namedtuple('Experience', ['state', 'action', 'reward', 'next_state', 'done'])


class DQN_MALMO_CNN_model(torch.nn.Module):
    """Docstring for DQN CNN model """

    def __init__(
        self,
        device,
        state_space,
        action_space,
        num_actions,
        num_frames=1,
        final_dense_layer=50,
        #input_shape=(9, 9),
        mode="skyline",  #skyline,ling_prior,embed_bl,cnn
        hier=False,
        atten=False,
        one_layer=False,
        emb_size=16,
        multi_edge=False,
        use_glove=False,
        self_attention=False,
        reverse_direction=False,
        use_layers=3):
        """Defining DQN CNN model
        """
        # initialize all parameters
        super(DQN_MALMO_CNN_model, self).__init__()
        print("using MALMO CNN {} {} {}".format(num_frames, final_dense_layer, state_space))
        self.state_space = state_space
        self.action_space = action_space
        self.device = device
        self.num_actions = num_actions
        self.num_frames = num_frames
        self.final_dense_layer = final_dense_layer
        if isinstance(state_space, Space):
            self.input_shape = state_space.shape
        else:
            self.input_shape = state_space
        self.mode = mode
        self.atten = atten
        self.hier = hier
        self.emb_size = emb_size
        self.multi_edge = multi_edge
        self.use_glove = use_glove
        self.self_attention = self_attention
        self.reverse_direction = reverse_direction
        self.use_layers = use_layers
        print("building model")

        self.graph_modes = {
            "skyline",
            "skyline_hier",
            "skyline_atten",
            "skyline_hier_atten",
            "skyline_simple",
            "skyline_simple_atten",
            "skyline_simple_trash",
            "ling_prior",
            "fully_connected",
            "skyline_hier_multi",
            "skyline_hier_multi_atten",
            "skyline_hier_dw_noGCN",
            "skyline_hier_dw_noGCN_dynamic"
        }

        self.build_model()

    def build_env_info(self):

        object_to_char = {
            "air": 0,
            "wall": 1,
            "stone": 2,
            "pickaxe_item": 3,
            "cobblestone_item": 4,
            "log": 5,
            "axe_item": 6,
            "dirt": 7,
            "farmland": 8,
            "hoe_item": 9,
            "water": 10,
            "bucket_item": 11,
            "water_bucket_item": 12,
            "log_item": 13,
            "dirt_item": 14,
            "farmland_item": 15
        }

        self.object_to_char = object_to_char

    def build_model(self):

        self.build_env_info()  #define env mapping

        # if self.mode == 'skyline_hier_dw_noGCN_dynamic':
        #     self.node_embeds_from_dw = torch.tensor(np.load("sky_hier_embeddings_written_8.npy"), dtype=torch.float, requires_grad=True).to(self.device)
        # elif self.mode == 'skyline_hier_dw_noGCN': #default title is static deepwalk embeddings
        #     self.node_embeds_from_dw = torch.FloatTensor(np.load("sky_hier_embeddings_written_8.npy")).to(self.device)

        if self.state_space == (9, 9):

            self.extract_goal, self.extract_inv = True, True

            self.body = torch.nn.Sequential(*[
                torch.nn.Conv2d(
                    self.num_frames if self.mode != "cnn" else 1, 32, kernel_size=(3, 3), stride=1),
                torch.nn.ReLU(),
                torch.nn.Conv2d(32, 32, kernel_size=(3, 3), stride=1),
                torch.nn.ReLU()
            ])

            final_size = conv2d_size_out(self.input_shape, (3, 3), 1)
            final_size = conv2d_size_out(final_size, (3, 3), 1)

            self.head = torch.nn.Sequential(*[
                torch.nn.Linear(final_size[0] * final_size[1] * 32 + self.emb_size * 2,
                                self.final_dense_layer),
                torch.nn.ReLU(),
                torch.nn.Linear(self.final_dense_layer, self.num_actions)
            ])

        elif self.state_space == (2, 3):

            self.extract_goal, self.extract_inv = False, False

            self.body = torch.nn.Sequential(*[
            torch.nn.Conv2d(self.num_frames if self.mode != "cnn" else 1, 8, kernel_size=(2, 2), stride=1), #8
            torch.nn.ReLU()
            ])

            self.head = torch.nn.Sequential(*[
                torch.nn.Linear(8 * 2, self.final_dense_layer),
                torch.nn.ReLU(),
                torch.nn.Linear(self.final_dense_layer, self.num_actions)
            ])

        else:
            raise ValueError("Unexpected state space {}".format(self.state_space))

        if self.mode in self.graph_modes:
            num_nodes,node_2_name,node_to_game,adjacency = malmo_build_gcn_param(self.object_to_char,self.mode, self.hier, self.use_layers, self.reverse_direction,self.multi_edge)

            self.node_2_game_char = node_to_game
            self.node_2_name = node_2_name

            self.gcn = GCN(adjacency,
                           self.device,
                           num_nodes,
                           node_to_game,
                           atten=self.atten,
                           emb_size=self.emb_size,
                           use_layers=self.use_layers)

            self.embeds = torch.nn.Embedding(num_nodes, self.num_frames)
            self.object_list = torch.arange(0, num_nodes)

        else:

            self.embeds = torch.nn.Embedding(len(self.object_to_char), self.num_frames)
            self.object_list = torch.arange(0, len(self.object_to_char))

        self.object_list = self.object_list.to(self.device)
        self.inv_block = LINEAR_INV_BLOCK(self.emb_size, self.emb_size, n=5)

        # pre_init_embeds = None

        # if pre_init_embeds is not None:
        #     print("using preinit embeds!")
        #     self.embeds.weight.data.copy_(pre_init_embeds)
        #     self.embeds.requires_grad = True

        trainable_parameters = sum(p.numel() for p in self.parameters() if p.requires_grad)
        print(f"Number of trainable parameters: {trainable_parameters}")

    def preprocess_state(self, state, extract_goal=True, extract_inv=True):
        inventory = None
        equipped = None
        goals = None

        state = state.long()

        if extract_goal:
            goals = state[:, :, :, 0][:, 0, 0].clone().detach().long()
            state = state[:, :, :, 1:]

        if extract_inv:
            inventory = state[:, :, :, 0].clone().squeeze(1).detach().long()
            state = state[:, :, :, 1:].long()
            equipped = state[:, :, 4, 4].clone().detach().long()
            inventory = torch.cat((equipped, inventory), 1)

        return state, goals, inventory, equipped

    def get_embeddings(self, goals):

        node_embeds = None
        goal_embeddings = None

        if self.mode in self.graph_modes:

            embeds = self.embeds(self.object_list)
            node_embeds = self.gcn.gcn_embed(embeds)

            if not goals is None:
                goal_embeddings = node_embeds[[g.item() for g in goals]]

        elif self.mode == "cnn":
            node_embeds = self.embeds(self.object_list)  #Used for inventory

            if not goals is None:
                goal_embeddings = self.embeds(goals)

        elif self.mode == "embed_bl":
            node_embeds = self.embeds(self.object_list)

            if not goals is None:
                goal_embeddings = self.embeds(goals)

        else:
            raise ValueError("Invalid mode")

        return node_embeds, goal_embeddings

    def forward(self, state):

        #If easy, change model and dont extract goal or inv

        state,goals,inventory,equipped = self.preprocess_state(state, self.extract_goal,self.extract_inv)
        node_embeds, goal_embeddings = self.get_embeddings(goals)

        if self.mode != "cnn":
            #if self.mode == "embed_bl":
            state_flat = state.reshape(-1)
            embedded_state = torch.index_select(node_embeds, 0,
                                                state_flat).reshape(state.shape[0],
                                                                    state.shape[2],
                                                                    state.shape[3],
                                                                    -1).permute(0, 3, 1, 2)
            state = embedded_state

        else:
            state = state.float()

        out = self.body(state)
        cnn_output = out.reshape(out.size(0), -1)

        if self.extract_inv:
            inv_encoded = self.inv_block(inventory, node_embeds, goal_embeddings)
            cnn_output = torch.cat((cnn_output, inv_encoded), -1)

        if self.extract_goal:
            # if self.mode not in ["skyline_hier_dw_noGCN", "skyline_hier_dw_noGCN_dynamic"]:
            #        goal_embeddings = F.relu(goal_embeddings)
            cnn_output = torch.cat((cnn_output, goal_embeddings), -1)

        q_value = self.head(cnn_output)

        return q_value

    def max_over_actions(self, state):
        state = state.to(self.device)
        return torch.max(self(state), dim=1)

    def argmax_over_actions(self, state):
        state = state.to(self.device)
        return torch.argmax(self(state), dim=1)

    def act(self, state, epsilon):
        if random.random() < epsilon:
            return self.action_space.sample()
        else:
            with torch.no_grad():
                state_tensor = torch.Tensor(state).unsqueeze(0)
                action_tensor = self.argmax_over_actions(state_tensor)
                action = action_tensor.cpu().detach().numpy().flatten()[0]
                assert self.action_space.contains(action)
            return action


class DQN_agent:
    """Docstring for DQN agent """
    def __init__(self,
                 device,
                 state_space,
                 action_space,
                 num_actions,
                 target_moving_average,
                 gamma,
                 replay_buffer_size,
                 epsilon_decay,
                 epsilon_decay_end,
                 warmup_period,
                 double_DQN,
                 model_type="mlp",
                 num_frames=None,
                 mode="skyline",
                 hier=False,
                 atten=False,
                 one_layer=False,
                 emb_size=16,
                 multi_edge=False,
                 use_glove=False,
                 self_attention=False,
                 use_layers=3,
                 reverse_direction=False):
        """Defining DQN agent
        """
        self.replay_buffer = deque(maxlen=replay_buffer_size)

        if model_type == "cnn":
            assert num_frames
            self.num_frames = num_frames
            self.online = DQN_MALMO_CNN_model(device,
                                              state_space,
                                              action_space,
                                              num_actions,
                                              num_frames=num_frames,
                                              mode=mode,
                                              hier=hier,
                                              atten=atten,
                                              multi_edge=multi_edge,
                                              use_glove=use_glove,
                                              emb_size=emb_size,
                                              use_layers=use_layers,
                                              reverse_direction=reverse_direction)
            self.target = DQN_MALMO_CNN_model(device,
                                              state_space,
                                              action_space,
                                              num_actions,
                                              num_frames=num_frames,
                                              mode=mode,
                                              hier=hier,
                                              atten=atten,
                                              multi_edge=multi_edge,
                                              use_glove=use_glove,
                                              emb_size=emb_size,
                                              use_layers=use_layers,
                                              reverse_direction=reverse_direction)

        else:
            raise NotImplementedError(model_type)

        self.online = self.online.to(device)
        self.target = self.target.to(device)

        self.target.load_state_dict(self.online.state_dict())
        self.target.eval()

        self.gamma = gamma
        self.target_moving_average = target_moving_average
        self.epsilon_decay = epsilon_decay
        self.epsilon_decay_end = epsilon_decay_end
        self.warmup_period = warmup_period
        self.device = device

        self.model_type = model_type
        self.double_DQN = double_DQN

    def loss_func(self, minibatch, writer=None, writer_step=None):
        # Make tensors
        state_tensor = torch.Tensor(np.array(minibatch.state)).to(self.device)
        next_state_tensor = torch.Tensor(np.array(minibatch.next_state)).to(self.device)

        action_tensor = torch.Tensor(minibatch.action).to(self.device)
        reward_tensor = torch.Tensor(minibatch.reward).to(self.device)
        done_tensor = torch.Tensor(minibatch.done).to(self.device)

        # Get q value predictions
        q_pred_batch = self.online(state_tensor).gather(
            dim=1, index=action_tensor.long().unsqueeze(1)).squeeze(1)
        with torch.no_grad():
            if self.double_DQN:
                selected_actions = self.online.argmax_over_actions(next_state_tensor)
                q_target = self.target(next_state_tensor).gather(
                    dim=1, index=selected_actions.long().unsqueeze(1)).squeeze(1)
            else:
                q_target = self.target.max_over_actions(next_state_tensor.detach()).values

        q_label_batch = reward_tensor + (self.gamma) * (1 - done_tensor) * q_target
        q_label_batch = q_label_batch.detach()

        # Logging
        if writer:
            writer.add_scalar('training/batch_q_label', q_label_batch.mean(), writer_step)
            writer.add_scalar('training/batch_q_pred', q_pred_batch.mean(), writer_step)
            writer.add_scalar('training/batch_reward', reward_tensor.mean(), writer_step)

        rl_loss = torch.nn.functional.mse_loss(q_label_batch, q_pred_batch)

        node_embeds = self.online.get_embeddings(None)
        adjacency = self.online.adjacency[0] # NOTE does not support multiple edge types
        edges = self.online.edges[0] # NOTE does not support multiple edge types
        latent_nodes = self.online.latent_nodes
        conversion_dict = self.online.node_2_name

        contrastive_loss = contrastive_loss_func(self.device, 
                                                 node_embeds,
                                                 adjacency,
                                                 latent_nodes,
                                                 conversion_dict,
                                                 self.positive_margin,
                                                 self.negative_margin)
        print("RL Loss: {:.2f}, Contrastive Loss: {:.2f}".format(rl_loss.item(),
           contrastive_loss.item()))
        loss = rl_loss + self.contrastive_loss_coeff * contrastive_loss
        return loss

    def sync_networks(self):
        sync_networks(self.target, self.online, self.target_moving_average)

    def set_epsilon(self, global_steps, writer=None):
        if global_steps < self.warmup_period:
            self.online.epsilon = 1
            self.target.epsilon = 1
        else:
            self.online.epsilon = max(self.epsilon_decay_end,
                                      1 - (global_steps - self.warmup_period) / self.epsilon_decay)
            self.target.epsilon = max(self.epsilon_decay_end,
                                      1 - (global_steps - self.warmup_period) / self.epsilon_decay)
        if writer:
            writer.add_scalar('training/epsilon', self.online.epsilon, global_steps)
