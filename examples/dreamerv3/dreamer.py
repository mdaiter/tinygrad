from functools import partial
import pathlib

import models
import numpy as np
import utils
from env import count_steps, load_episodes, make_dataset, make_envs, simulate

from tinygrad.nn.state import get_state_dict, load_state_dict, safe_load, safe_save
import time


class Dreamer:
    def __init__(self, obs_space, act_space, config, logger, dataset):
        self._config = config
        self._logger = logger
        self._log_every = config.log_every
        batch_steps = config.batch_size * config.batch_length
        self._num_train_steps = batch_steps // config.train_ratio
        if self._num_train_steps == 0:
            raise ValueError(f"Train ratio {config.train_ratio} must be less than batch size {config.batch_size} * batch_length {config.batch_length}s.")
        self._reset_every = config.reset_every
        self._expl_until = config.expl_until
        self.pretrained = False

        self._metrics = {}
        # this is update step
        self._step = logger.step // config.action_repeat
        self._update_count = 0
        self._dataset = dataset
        self.world_model = models.WorldModel(obs_space, act_space, self._step, config)
        self.actor_critic = models.ActorCritic(config, self.world_model)

    def __call__(self, obs, done, state=None, training=True):
        if training:
            num_steps = self._config.pretrain if not self.pretrained else self._num_train_steps
            for _ in range(num_steps):
                self._train(next(self._dataset))
                self._update_count += 1
                self._metrics["update_count"] = self._update_count
            if not self.pretrained or self._step % self._log_every == 0:
                for name, values in self._metrics.items():
                    self._logger.scalar(name, float(np.mean(values)))
                    self._metrics[name] = []
                self._logger.write(fps=True)
            self.pretrained = True
            self._step += len(done)
            self._logger.step = self._config.action_repeat * self._step

        explore = self._step < self._expl_until
        policy_output, state = self.actor_critic.policy(obs, state, training, explore)
        return policy_output, state

    def _train(self, data):
        metrics = {}
        post, context, mets = self.world_model.train(data)
        metrics.update(mets)

        def reward(f, s, a):
            embed = self.world_model.dynamics.get_feat(s)
            return self.world_model.heads["reward"](embed).mode.squeeze(-1)

        metrics.update(self.actor_critic.train(post, reward)[-1])
        for name, value in metrics.items():
            value = value.item()
            if name not in self._metrics.keys():
                self._metrics[name] = [value]
            else:
                self._metrics[name].append(value)


def main():
    config = utils.load_config()
    utils.set_seed_everywhere(config.seed)
    logdir = pathlib.Path(config.logdir).expanduser()
    config.traindir = config.traindir or logdir / "train_eps"
    config.evaldir = config.evaldir or logdir / "eval_eps"
    config.steps //= config.action_repeat
    config.eval_every //= config.action_repeat
    config.log_every //= config.action_repeat

    print("Logdir", logdir)
    logdir.mkdir(parents=True, exist_ok=True)
    config.traindir.mkdir(parents=True, exist_ok=True)
    config.evaldir.mkdir(parents=True, exist_ok=True)
    step = count_steps(config.traindir)
    # step in logger is environmental step
    logger = utils.Logger(logdir, config.action_repeat * step)

    print("Create envs.")
    if config.offline_traindir:
        directory = config.offline_traindir.format(**vars(config))
    else:
        directory = config.traindir
    train_eps = load_episodes(directory, limit=config.dataset_size)
    if config.offline_evaldir:
        directory = config.offline_evaldir.format(**vars(config))
    else:
        directory = config.evaldir
    eval_eps = load_episodes(directory, limit=1)

    train_envs = make_envs(config)
    eval_envs = make_envs(config)
    act_space = train_envs[0].action_space
    print("Action Space", act_space)

    state = None
    if not config.offline_traindir:
        prefill = max(0, config.prefill - count_steps(config.traindir))
        print(f"Prefill dataset ({prefill} steps).")
        random_agent = models.random_agent(config, act_space)

        state = simulate(random_agent, train_envs, train_eps, config.traindir, logger, limit=config.dataset_size, steps=prefill)
        logger.step += prefill * config.action_repeat
        print(f"Logger: ({logger.step} steps).")

    print("Simulate agent.")
    train_dataset = make_dataset(train_eps, config)
    eval_dataset = make_dataset(eval_eps, config)
    agent = Dreamer(train_envs[0].observation_space, train_envs[0].action_space, config, logger, train_dataset)
    print(f"world parameters: {sum(param.numel() for param in agent.world_model.parameters())}")
    print(f"actor parameters: {sum(param.numel() for param in agent.actor_critic.actor_parameters())}")
    print(f"value parameters: {sum(param.numel() for param in agent.actor_critic.value_parameters())}")
    if (logdir / "latest.safetensors").exists():
        state_dict = safe_load(logdir / "latest.safetensors")
        load_state_dict(agent, state_dict)
        agent.pretrained = True

    # make sure eval will be executed once after config.steps
    while agent._step < config.steps + config.eval_every:
        logger.write()
        if config.eval_episode_num > 0:
            print("Start evaluation.")
            start_time = time.time()
            eval_policy = partial(agent, training=False)
            simulate(eval_policy, eval_envs, eval_eps, config.evaldir, logger, is_eval=True, episodes=config.eval_episode_num)
            if config.video_pred_log:
                video = agent.world_model.video_pred(next(eval_dataset)).numpy()
                logger.video("eval_openl", video)
            print(f"Evaluation time: {time.time() - start_time:.2f} sec")
            logger.scalar("eval_time", time.time() - start_time)
        print("Start training.")
        start_time = time.time()
        state = simulate(agent, train_envs, train_eps, config.traindir, logger, limit=config.dataset_size, steps=config.eval_every, state=state)
        state_dict = get_state_dict(agent)
        safe_save(state_dict, logdir / "latest.safetensors")
        print(f"Training time: {time.time() - start_time:.2f} sec")
        logger.scalar("train_time", time.time() - start_time)
    for env in train_envs + eval_envs:
        env.close()


if __name__ == "__main__":
    main()
