# coding=utf-8
# Copyright 2019 The Trax Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for trax.rl.ppo's training_loop."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import contextlib
import functools
import itertools
import os
import tempfile

import gin
import gym
import numpy as np

from tensor2tensor.envs import gym_env_problem
from tensor2tensor.rl import gym_utils
from tensorflow import test
from tensorflow.io import gfile
from trax import inputs as trax_inputs
from trax import layers
from trax import learning_rate as lr
from trax import models
from trax import optimizers as trax_opt
from trax import trainer_lib
from trax.rl import envs  # pylint: disable=unused-import
from trax.rl import ppo_trainer
from trax.rl import simulated_env_problem


class PpoTrainerTest(test.TestCase):

  def get_wrapped_env(
      self, name='CartPole-v0', max_episode_steps=2, batch_size=1
  ):
    wrapper_fn = functools.partial(
        gym_utils.gym_env_wrapper,
        **{
            'rl_env_max_episode_steps': max_episode_steps,
            'maxskip_env': False,
            'rendered_env': False,
            'rendered_env_resize_to': None,  # Do not resize frames
            'sticky_actions': False,
            'output_dtype': None,
        })

    return gym_env_problem.GymEnvProblem(base_env_name=name,
                                         batch_size=batch_size,
                                         env_wrapper_fn=wrapper_fn,
                                         discrete_rewards=False)

  @contextlib.contextmanager
  def tmp_dir(self):
    tmp = tempfile.mkdtemp(dir=self.get_temp_dir())
    yield tmp
    gfile.rmtree(tmp)

  def _make_trainer(
      self, train_env, eval_env, output_dir, model=None, **kwargs
  ):
    if model is None:
      model = lambda: layers.Serial(layers.Dense(1))
    return ppo_trainer.PPO(
        train_env=train_env,
        eval_env=eval_env,
        policy_and_value_model=model,
        n_optimizer_steps=1,
        output_dir=output_dir,
        random_seed=0,
        max_timestep=3,
        boundary=2,
        save_every_n=1,
        **kwargs
    )

  def test_training_loop_cartpole(self):
    with self.tmp_dir() as output_dir:
      trainer = self._make_trainer(
          train_env=self.get_wrapped_env('CartPole-v0', 2),
          eval_env=self.get_wrapped_env('CartPole-v0', 2),
          output_dir=output_dir,
      )
      trainer.training_loop(n_epochs=2)

  def test_training_loop_cartpole_transformer(self):
    with self.tmp_dir() as output_dir:
      trainer = self._make_trainer(
          train_env=self.get_wrapped_env('CartPole-v0', 2),
          eval_env=self.get_wrapped_env('CartPole-v0', 2),
          output_dir=output_dir,
          model=functools.partial(
              models.TransformerDecoder,
              d_model=1,
              d_ff=1,
              n_layers=1,
              n_heads=1,
              max_len=128,
              mode='train',
          ),
      )
      trainer.training_loop(n_epochs=2)

  def test_training_loop_onlinetune(self):
    with self.tmp_dir() as output_dir:
      gin.bind_parameter('OnlineTuneEnv.model', functools.partial(
          models.MLP,
          n_hidden_layers=0,
          n_output_classes=1,
      ))
      gin.bind_parameter('OnlineTuneEnv.inputs', functools.partial(
          trax_inputs.random_inputs,
          input_shape=(1, 1),
          input_dtype=np.float32,
          output_shape=(1, 1),
          output_dtype=np.float32,
      ))
      gin.bind_parameter('OnlineTuneEnv.train_steps', 1)
      gin.bind_parameter('OnlineTuneEnv.eval_steps', 1)
      gin.bind_parameter(
          'OnlineTuneEnv.output_dir', os.path.join(output_dir, 'envs'))
      trainer = self._make_trainer(
          train_env=self.get_wrapped_env('OnlineTuneEnv-v0', 1),
          eval_env=self.get_wrapped_env('OnlineTuneEnv-v0', 1),
          output_dir=output_dir,
      )
      trainer.training_loop(n_epochs=1)

  def test_training_loop_simulated(self):
    n_actions = 5
    history_shape = (3, 2, 3)
    action_shape = (3,)
    obs_shape = (3, 3)
    reward_shape = (3, 1)

    def model(mode):
      del mode
      return layers.Serial(
          layers.Parallel(
              layers.Flatten(),  # Observation stack.
              layers.Embedding(d_feature=1, vocab_size=n_actions),  # Action.
          ),
          layers.Concatenate(),
          layers.Dense(n_units=1),
          layers.Dup(),
          layers.Parallel(
              layers.Dense(n_units=obs_shape[1]),  # New observation.
              None,  # Reward.
          )
      )

    def inputs(n_devices):
      del n_devices
      stream = itertools.repeat(
          (np.zeros(history_shape), np.zeros(action_shape, dtype=np.int32),
           np.zeros(obs_shape), np.zeros(reward_shape))
      )
      return trax_inputs.Inputs(
          train_stream=lambda: stream,
          train_eval_stream=lambda: stream,
          eval_stream=lambda: stream,
          input_shape=(history_shape[1:], action_shape[1:]),
          input_dtype=(np.float32, np.int32),
          target_shape=(obs_shape[1:], reward_shape[1:]),
          target_dtype=(np.float32, np.float32),
      )

    def loss(mask_id=None, has_weights=False):
      """Cross-entropy loss as scalar compatible with Trax masking."""
      return layers.Serial(
          # Swap from (pred-obs, pred-reward, target-obs, target-reward)
          # to (pred-obs, target-obs, pred-reward, target-reward).
          layers.Parallel([], layers.Swap()),
          # Cross-entropy loss for obs, L2 loss on reward.
          layers.Parallel(layers.CrossEntropyLossScalar(mask_id, has_weights),
                          layers.L2LossScalar(mask_id, has_weights)),
          # Add both losses.
          layers.Add(),
          # Zero out in this test.
          layers.MulConstant(constant=0.0)
      )

    with self.tmp_dir() as output_dir:
      # Run fake training just to save the parameters.
      trainer = trainer_lib.Trainer(
          model=model,
          loss_fn=loss,
          inputs=inputs,
          optimizer=trax_opt.SM3,
          lr_schedule=lr.MultifactorSchedule,
          output_dir=output_dir,
      )
      trainer.train_epoch(n_steps=1, n_eval_steps=1)

      # Repeat the history over and over again.
      stream = itertools.repeat(np.zeros(history_shape))
      env_fn = functools.partial(
          simulated_env_problem.RawSimulatedEnvProblem,
          model=model,
          history_length=history_shape[1],
          trajectory_length=3,
          batch_size=history_shape[0],
          observation_space=gym.spaces.Box(
              low=-np.inf, high=np.inf, shape=(obs_shape[1],)),
          action_space=gym.spaces.Discrete(n=n_actions),
          reward_range=(-1, 1),
          discrete_rewards=False,
          history_stream=stream,
          output_dir=output_dir,
      )

      trainer = self._make_trainer(
          train_env=env_fn(),
          eval_env=env_fn(),
          output_dir=output_dir,
      )
      trainer.training_loop(n_epochs=2)

  def test_restarts(self):
    with self.tmp_dir() as output_dir:
      train_env = self.get_wrapped_env('CartPole-v0', 2)
      eval_env = self.get_wrapped_env('CartPole-v0', 2)

      # Train for 1 epoch and save.
      trainer = self._make_trainer(
          train_env=train_env,
          eval_env=eval_env,
          output_dir=output_dir,
      )
      self.assertEqual(trainer.epoch, 0)
      trainer.training_loop(n_epochs=1)
      self.assertEqual(trainer.epoch, 1)

      # Restore from the saved state.
      trainer = self._make_trainer(
          train_env=train_env,
          eval_env=eval_env,
          output_dir=output_dir,
      )
      self.assertEqual(trainer.epoch, 1)
      # Check that we can continue training from the restored checkpoint.
      trainer.training_loop(n_epochs=2)
      self.assertEqual(trainer.epoch, 2)

  def test_training_loop_multi_control(self):
    gym.register(
        'FakeEnv-v0',
        entry_point='trax.rl.envs.fake_env:FakeEnv',
        kwargs={'n_actions': 3, 'n_controls': 2},
    )
    with self.tmp_dir() as output_dir:
      trainer = self._make_trainer(
          train_env=self.get_wrapped_env('FakeEnv-v0', 2),
          eval_env=self.get_wrapped_env('FakeEnv-v0', 2),
          output_dir=output_dir,
      )
      trainer.training_loop(n_epochs=2)

  def test_training_loop_cartpole_serialized(self):
    gin.bind_parameter('BoxSpaceSerializer.precision', 1)
    with self.tmp_dir() as output_dir:
      trainer = self._make_trainer(
          train_env=self.get_wrapped_env('CartPole-v0', 2),
          eval_env=self.get_wrapped_env('CartPole-v0', 2),
          output_dir=output_dir,
          model=functools.partial(
              models.TransformerDecoder,
              d_model=1,
              d_ff=1,
              n_layers=1,
              n_heads=1,
              max_len=1024,
              mode='train',
          ),
          policy_and_value_vocab_size=4,
      )
      trainer.training_loop(n_epochs=2)

  def test_training_loop_cartpole_minibatch(self):
    with self.tmp_dir() as output_dir:
      trainer = self._make_trainer(
          train_env=self.get_wrapped_env('CartPole-v0', 2, batch_size=4),
          eval_env=self.get_wrapped_env('CartPole-v0', 2),
          output_dir=output_dir,
          optimizer_batch_size=2,
      )
      trainer.training_loop(n_epochs=2)


if __name__ == '__main__':
  test.main()
