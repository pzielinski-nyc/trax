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

"""RNNs."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from trax import layers as tl


def RNNLM(vocab_size,
          d_model=512,
          n_layers=2,
          rnn_cell=tl.LSTMCell,
          rnn_cell_d_state_multiplier=2,
          dropout=0.1,
          mode='train'):
  """Returns an RNN language model.

  The input to the model is a tensor of tokens (ints).

  Args:
    vocab_size: int: vocab size
    d_model: int:  depth of embedding (n_units in the RNN cell)
    n_layers: int: number of RNN layers
    rnn_cell: the RNN cell
    rnn_cell_d_state_multiplier: how many times is RNN cell state larger
    dropout: float: dropout rate (how much to drop out)
    mode: str: 'train', 'eval' or 'predict', predict mode is for fast inference

  Returns:
    An RNN language model as a layer that maps from a tensor of tokens
    to activations over a vocab set.
  """
  def MultiRNNCell():
    """Multi-layer RNN cell."""
    assert n_layers == 2
    return tl.Serial(
        tl.Parallel([], tl.Split(n_items=n_layers)),
        tl.SerialWithSideOutputs(
            [rnn_cell(n_units=d_model) for _ in range(n_layers)]),
        tl.Parallel([], tl.Concatenate(n_items=n_layers))
    )

  return tl.Serial(
      tl.ShiftRight(mode=mode),
      tl.Embedding(d_model, vocab_size),
      tl.Dropout(rate=dropout, name='embedding', mode=mode),
      tl.Dup(),  # Duplicate to create parallel state.
      tl.Parallel([], tl.MakeZeroState(  # pylint: disable=no-value-for-parameter
          depth_multiplier=n_layers * rnn_cell_d_state_multiplier)),
      tl.Scan(MultiRNNCell(), axis=1),
      tl.Parallel([], tl.Drop()),  # Drop RNN state.
      tl.Dense(vocab_size),
      tl.LogSoftmax()
  )
