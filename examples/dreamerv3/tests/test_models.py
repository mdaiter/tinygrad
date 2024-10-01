import tempfile
import unittest

import gymnasium as gym
import models
import numpy as np
import utils
from tinygrad import Tensor
import time


class TestRewardEMA(unittest.TestCase):
    def test_reward_ema(self):
        reward_ema = models.RewardEMA()
        ema_vals = Tensor([0.0, 1.0])
        mean, std, ema_vals = reward_ema(Tensor([1.0, 2.0, 3.0]), ema_vals)
        print(mean.item(), std.item())
        mean, std, ema_vals = reward_ema(Tensor([1.0, 2.0, 3.0]), ema_vals)
        print(mean.item(), std.item())


class TestWorldModel(unittest.TestCase):
    def test_world_model_init(self):
        obs_space = gym.spaces.Dict({"image": gym.spaces.Box(low=0, high=255, shape=(64, 64, 3), dtype=np.uint8)})
        act_space = gym.spaces.Discrete(3)
        config = utils.load_config()
        world_model = models.WorldModel(obs_space, act_space, 0, config)
        print(f"world model parameters: {sum(param.numel() for param in world_model.parameters())}")

    def test_world_model_preprocess(self):
        B = 8
        T = 6
        obs_space = gym.spaces.Dict({"image": gym.spaces.Box(low=0, high=255, shape=(64, 64, 3), dtype=np.uint8)})
        act_space = gym.spaces.Discrete(3)
        config = utils.load_config()
        world_model = models.WorldModel(obs_space, act_space, 0, config)
        data = {
            "image": np.random.randint(0, 255, (B, T, 64, 64, 3)),
            "action": np.random.randint(0, 3, (B, T), dtype=np.int32),
            "reward": np.random.rand(B, T),
            "discount": np.ones((B, T)),
            "is_first": np.ones((B, T)),
            "is_terminal": np.zeros((B, T)),
        }
        data = world_model.preprocess(data)

    def test_world_model_video_pred(self):
        B = 8
        T = 6
        obs_space = gym.spaces.Dict({"image": gym.spaces.Box(low=0, high=255, shape=(64, 64, 3), dtype=np.uint8)})
        act_space = gym.spaces.Discrete(3)
        config = utils.load_config()
        world_model = models.WorldModel(obs_space, act_space, 0, config)
        data = {
            "image": np.random.randint(0, 255, (B, T, 64, 64, 3)),
            "action": np.random.randint(0, 3, (B, T)),
            "reward": np.random.rand(B, T),
            "discount": np.ones((B, T)),
            "is_first": np.ones((B, T)),
            "is_terminal": np.zeros((B, T)),
        }
        video = world_model.video_pred(data).numpy()
        logger = utils.Logger(tempfile.gettempdir(), 0)
        logger.offline_video("video", video, 0)

    def test_world_model_train(self):
        B = 4
        T = 2
        obs_space = gym.spaces.Dict({"image": gym.spaces.Box(low=0, high=255, shape=(64, 64, 3), dtype=np.uint8)})
        act_space = gym.spaces.Discrete(3)
        config = utils.load_config()
        world_model = models.WorldModel(obs_space, act_space, 0, config)
        data = {
            "image": np.random.randint(0, 255, (B, T, 64, 64, 3)),
            "action": np.random.randint(0, 3, (B, T)),
            "reward": np.random.rand(B, T),
            "discount": np.ones((B, T)),
            "is_first": np.ones((B, T)),
            "is_terminal": np.zeros((B, T)),
        }
        for i in range(5):
            start_time = time.time()
            post, context, metrics = world_model.train(data)
            metrics = {k: v.item() for k, v in metrics.items()}
            print(metrics)
            print(f"world model train time {i}: {time.time() - start_time}")
            print()
        self.assertEqual(post["stoch"].numpy().shape, (B, T, 32, 32))
        self.assertEqual(post["deter"].numpy().shape, (B, T, 512))
        self.assertEqual(context["embed"].numpy().shape, (B, T, 4096))
        self.assertEqual(context["feat"].numpy().shape, (B, T, 1536))


class TestActorCritic(unittest.TestCase):
    def test_actor_critic_init(self):
        obs_space = gym.spaces.Dict({"image": gym.spaces.Box(low=0, high=255, shape=(64, 64, 3), dtype=np.uint8)})
        act_space = gym.spaces.Discrete(3)
        config = utils.load_config()
        world_model = models.WorldModel(obs_space, act_space, 0, config)
        actor_critic = models.ActorCritic(config, world_model)
        print(f"actor parameters: {sum(param.numel() for param in actor_critic.actor_parameters())}")
        print(f"value parameters: {sum(param.numel() for param in actor_critic.value_parameters())}")

    def test_actor_critic_funcs(self):
        B = 8
        T = 6
        obs_space = gym.spaces.Dict({"image": gym.spaces.Box(low=0, high=255, shape=(64, 64, 3), dtype=np.uint8)})
        act_space = gym.spaces.Discrete(3)
        config = utils.load_config()
        H = config.imag_horizon
        world_model = models.WorldModel(obs_space, act_space, 0, config)
        actor_critic = models.ActorCritic(config, world_model)
        start = world_model.dynamics.initial(B * T)
        start = {k: v.reshape((B, T) + v.shape[1:]) for k, v in start.items()}
        feats, states, actions = actor_critic._imagine(actor_critic, **start)
        actor_critic._imagine.reset()
        self.assertEqual(feats.numpy().shape, (H, B * T, 1536))
        self.assertEqual(states["stoch"].numpy().shape, (H, B * T, 32, 32))
        self.assertEqual(states["deter"].numpy().shape, (H, B * T, 512))
        self.assertEqual(actions.numpy().shape, (H, B * T, world_model.num_actions))
        rewards = Tensor.uniform((H, B * T))
        target, weights, base = actor_critic._compute_target(feats, states, rewards)
        actor_loss, _, metrics = actor_critic._compute_actor_loss(feats, actions, target, weights, base, actor_critic.ema_vals)
        actor_loss.mean().backward()  # checks backward pass
        metrics["actor_loss"] = actor_loss.mean()
        metrics = {k: v.item() for k, v in metrics.items()}
        print(metrics)

    def test_actor_critic_train(self):
        B = 8
        T = 6
        obs_space = gym.spaces.Dict({"image": gym.spaces.Box(low=0, high=255, shape=(64, 64, 3), dtype=np.uint8)})
        act_space = gym.spaces.Discrete(3)
        config = utils.load_config()
        world_model = models.WorldModel(obs_space, act_space, 0, config)
        actor_critic = models.ActorCritic(config, world_model)
        start = world_model.dynamics.initial(B * T)
        start = {k: v.reshape((B, T) + v.shape[1:]) for k, v in start.items()}

        def reward_fn(f, s, a):
            feat = world_model.dynamics.get_feat(s)
            return world_model.heads["reward"](feat).mode.squeeze(-1)

        for i in range(5):
            start_time = time.time()
            feat, state, action, weights, metrics = actor_critic.train(start, reward_fn)
            metrics = {k: v.item() for k, v in metrics.items()}
            print(metrics)
            print(f"actor critic train time {i}: {time.time() - start_time}")
            print()


if __name__ == "__main__":
    unittest.main()
