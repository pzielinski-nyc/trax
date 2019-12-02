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

"""Combinators for composing layers."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from trax import backend
from trax.backend import numpy as np
from trax.layers import base
from trax.shapes import ShapeDtype


class Serial(base.Layer):
  """Combinator that applies layers serially (by function composition).

  A Serial combinator uses stack semantics to manage data for its sublayers.
  Each sublayer sees only the inputs it needs and returns only the outputs it
  has generated. The sublayers interact via the data stack. For instance, a
  sublayer k, following sublayer j, gets called with the data stack in the
  state left after layer j has applied. The Serial combinator then:

    - takes n_in items off the top of the stack (n_in = k.n_in) and calls
      layer k, passing those items as arguments; and

    - takes layer k's n_out return values (n_out = k.n_out) and pushes
      them onto the data stack.

  A Serial instance with no sublayers acts as a special-case (but useful)
  1-input 1-output no-op.
  """

  def __init__(self, *sublayers):
    super(Serial, self).__init__()

    sublayers = self._ensure_flat(sublayers)
    self._sublayers = sublayers
    self._n_layers = len(sublayers)

    if sublayers:
      self._n_in, self._n_out = self._n_inputs_n_outputs(sublayers)

  def forward_with_state(self, xs, weights=base.EMPTY_WEIGHTS,
                         state=base.EMPTY_STATE, **kwargs):
    self._validate_forward_inputs(xs)
    rngs = _pop_rng_and_split(kwargs, self._n_layers)
    if not self.sublayers:  # No-op: leave args unchanged.
      return (xs, state)

    stack = xs
    new_state = []
    n_layers = self._n_layers
    if n_layers != 1 and len(weights) != n_layers:
      raise ValueError('number of weights ({}) not equal to number of layers '
                       '({})'.format(len(weights), n_layers))
    if n_layers != 1 and len(state) != n_layers:
      raise ValueError('length of state ({}) not equal to number of layers '
                       '({})'.format(len(state), n_layers))

    for layer, p, s, rng in zip(self.sublayers, weights, state, rngs):
      inputs = _inputs_from_stack(layer, stack)
      outputs, s = layer._forward_internal(inputs, p, s, rng)  # pylint: disable=protected-access
      stack = _outputs_onto_stack(layer, outputs, stack)
      new_state.append(s)
    return stack, new_state

  # pylint: disable=protected-access
  def new_weights_and_state(self, input_signature):
    weights = []
    states = []
    # In the code below, stack, inputs, and outputs are abstract (shapes and
    # dtypes), but weights and states are non-abstract actual values.
    stack = input_signature
    for sublayer in self.sublayers:
      inputs = _inputs_from_stack(sublayer, stack)
      weights_or_empty, state = sublayer.init(inputs)
      outputs, _ = sublayer._forward_abstract(inputs)
      stack = _outputs_onto_stack(sublayer, outputs, stack)

      weights.append(weights_or_empty)
      states.append(state)
    return weights, states
  # pylint: enable=protected-access

  @base.Layer.weights.setter
  def weights(self, weights):
    """Recursively sets weights on this layer and all sublayers."""
    self._weights = weights
    assert len(weights) == self._n_layers
    for layer, sublayer_weights in zip(self.sublayers, weights):
      layer.weights = sublayer_weights

  @base.Layer.state.setter
  def state(self, state):
    """Recursively sets non-param state on this layer and all sublayers."""
    self._state = state
    assert len(state) == self._n_layers
    for layer, sublayer_state in zip(self.sublayers, state):
      layer.state = sublayer_state

  def _ensure_flat(self, layers):
    """Ensures that layers is a single flat list of Layer instances."""
    del self
    if len(layers) == 1 and layers[0] is None:
      layers = ()
    else:
      layers = _deep_flatten(layers)
    for obj in layers:
      if not isinstance(obj, base.Layer):
        raise ValueError(
            'Found nonlayer object ({}) in layers: {}.'.format(obj, layers))
    return layers

  def _n_inputs_n_outputs(self, layers):
    del self
    running_max = 0
    running_total = 0
    for layer in layers:
      running_total += layer.n_in
      running_max = max(running_max, running_total)
      running_total -= layer.n_out
    return running_max, (running_max - running_total)

  def _validate_forward_inputs(self, xs):
    if not isinstance(xs, tuple) and self._n_in != 1:
      raise TypeError(
          'Serial.forward input must be a tuple; instead got {}'.format(xs))
    len_xs = 1 if isinstance(xs, np.ndarray) else len(xs)
    if len_xs < self.n_in:
      raise ValueError(
          'number of inputs ({}) to Serial.forward less than n_in'
          ' ({})'.format(len(xs), self.n_in))

  # pylint: disable=protected-access
  def _set_input_signature_recursive(self, input_signature):
    """Sets input signatures for this layer and sublayers, recursively.

    Args:
      input_signature: A `ShapeDtype` instance (if this layer takes one input)
          or a list/tuple of `ShapeDtype` instances.
    """
    self._input_signature = input_signature

    # Infer shapes and dtypes (signatures) through the successive sublayers.
    stack = input_signature
    for layer in self.sublayers:
      inputs = _inputs_from_stack(layer, stack)
      layer._set_input_signature_recursive(inputs)
      outputs, _ = layer._forward_abstract(inputs)
      stack = _outputs_onto_stack(layer, outputs, stack)
  # pylint: enable=protected-access


class Parallel(base.Layer):
  """Combinator that applies a list of layers in parallel to its inputs.

  Layers in the list apply to successive spans of inputs, where the spans are
  determined how many inputs each layer takes. The resulting output is the
  (flattened) concatenation of the resepective layer outputs.

  For example, suppose one has three layers:

    - F: 1 input, 1 output
    - G: 3 inputs, 1 output
    - H: 2 inputs, 2 outputs (h1, h2)

  Then Parallel(F, G, H) will take 6 inputs and give 4 outputs:

    - inputs: a, b, c, d, e, f
    - outputs: F(a), G(b, c, d), h1, h2

  As an important special case, a None argument to Parallel acts as if it takes
  one argument, which it leaves unchanged. (It acts as a one-arg no-op.) For
  example:

    Parallel(None, F)

  creates a layer that passes its first input unchanged and applies F to the
  following input(s).
  """

  def __init__(self, *sublayers):
    """The constructor.

    Args:
      *sublayers: A list of sublayers.

    Returns:
      A new layer in which each of the given sublayers applies to its
      corresponding span of elements in the dataflow stack.
    """
    super(Parallel, self).__init__()
    sublayers = self._validate(sublayers)
    self._n_layers = len(sublayers)
    self._sublayers = sublayers
    self._n_in = sum(x.n_in for x in sublayers)
    self._n_out = sum(x.n_out for x in sublayers)

  def forward_with_state(self, inputs, weights=base.EMPTY_WEIGHTS,
                         state=base.EMPTY_STATE, **kwargs):
    n_layers, layers = self._n_layers, self.sublayers
    sublayer_inputs = self._allot_to_sublayers(inputs)
    rngs = _pop_rng_and_split(kwargs, n_layers)
    assert len(sublayer_inputs) == n_layers
    assert len(weights) == n_layers
    assert len(state) == n_layers
    assert len(rngs) == n_layers
    outputs = []
    new_state = []
    for layer, x, p, s, r in zip(layers, sublayer_inputs, weights, state, rngs):
      # Note that zip silently truncates its result if lengths don't match.
      sub_outputs, sub_state = layer._forward_internal(x, p, s, r)  # pylint: disable=protected-access
      if layer.n_out == 1:
        outputs.append(sub_outputs)
      else:
        outputs.extend(sub_outputs)
      new_state.append(sub_state)
    output = outputs[0] if self.n_out == 1 else tuple(outputs)
    return output, tuple(new_state)

  def new_weights_and_state(self, input_signature):
    sublayer_signatures = self._allot_to_sublayers(input_signature)
    inits = [layer.init(signature)
             for layer, signature
             in zip(self.sublayers, sublayer_signatures)]
    if inits:
      return tuple(zip(*inits))
    else:
      return (base.EMPTY_WEIGHTS, base.EMPTY_STATE)

  @base.Layer.weights.setter
  def weights(self, weights):
    """Recursively sets weights on this layer and all sublayers."""
    self._weights = weights
    assert len(weights) == self._n_layers
    for layer, sublayer_weights in zip(self.sublayers, weights):
      layer.weights = sublayer_weights

  @base.Layer.state.setter
  def state(self, state):
    """Recursively sets non-param state on this layer and all sublayers."""
    self._state = state
    assert len(state) == self._n_layers
    for layer, sublayer_state in zip(self.sublayers, state):
      layer.state = sublayer_state

  def _validate(self, layers):
    if not layers or len(layers) < 2:
      raise ValueError(
          'layers ({}) must be a list with at least two elements'.format(
              layers))
    layers = list(layers)  # Ensure we can modify layers.
    for i, obj in enumerate(layers):
      if obj is None or obj == []:  # pylint: disable=g-explicit-bool-comparison
        layers[i] = Serial(None)
      elif isinstance(obj, (list, tuple)):
        layers[i] = Serial(obj)
      else:
        if not isinstance(obj, base.Layer):
          raise ValueError(
              'Found nonlayer object ({}) in layers list: [{}].'.format(
                  obj, layers))
      if layers[i].n_in == 0:
        raise ValueError(
            'Sublayer with n_in = 0 not allowed in Parallel:'
            ' {}'.format(layers[i]))
    return layers

  def _allot_to_sublayers(self, inputs):
    """Divides Parallel's inputs for use by the sublayers.

    Args:
      inputs: Tuple of ndarrays or ShapeDtype instances.

    Returns:
      A tuple that partitions this layer's inputs among its sublayers.
      Sublayers that take one argument get that argument directly. All other
      sublayers get a tuple of items.
    """
    start, end = 0, 0
    sub_inputs = []
    for layer in self.sublayers:
      n_in = layer.n_in
      end = start + n_in
      if n_in == 1:
        sub_inputs.append(inputs[start])
      else:
        sub_inputs.append(inputs[start:end])
      start = end
    return tuple(sub_inputs)

  def _set_input_signature_recursive(self, input_signature):
    """Sets input signatures for this layer and sublayers, recursively.

    Args:
      input_signature: A `ShapeDtype` instance (if this layer takes one input)
          or a list/tuple of `ShapeDtype` instances.
    """
    self._input_signature = input_signature

    # Assign signatures to the sublayers.
    sublayer_signatures = self._allot_to_sublayers(input_signature)
    for layer, signature in zip(self.sublayers, sublayer_signatures):
      layer._set_input_signature_recursive(signature)  # pylint: disable=protected-access


class Concatenate(base.Layer):
  """Concatenates n tensors into a single tensor."""

  def __init__(self, n_items=2, axis=-1):
    super(Concatenate, self).__init__(n_in=n_items)
    self._n_items = n_items
    self._axis = axis

  def forward(self, xs, weights):
    del weights
    return backend.numpy.concatenate(xs, self._axis)


class Split(base.Layer):
  """Splits the input into n items along an axis."""

  def __init__(self, n_items=2, axis=-1):
    super(Split, self).__init__(n_out=n_items)
    self._n_items = n_items
    self._axis = axis

  def forward(self, inputs, weights):
    del weights
    return tuple(backend.numpy.split(inputs, self._n_items, self._axis))


class Scan(base.Layer):
  """Scans the given layer over the given axis of the inputs.

  We assume the layer takes a tuple of inputs of the following form:
    (input1, ..., inputN, carry1, ..., carryM)
  and returns
    (output1, ..., outputK, new_carry1, ..., new_carryM)

  The scanned version applies the layer iteratively to a tensor treating values
  at the given axis as if they were a list. For example, to calculate all
  sums of prefixes of a tensor, we can do this:

  @base.layer(n_in=2, n_out=2)
  def add(x)
      input, carry = x
      res = input + carry
      return res, res  # output and carry are the same

  Scan(add)([1, 2, 3], 0) = [1, 3, 6], 6
  """

  def __init__(self, layer, axis=0, n_carry=1):
    super(Scan, self).__init__(n_in=layer.n_in, n_out=layer.n_out)
    self._sublayers = [layer]
    self._n_carry = n_carry
    self._axis = axis

  @property
  def sublayer(self):
    """Returns the unique sublayer managed by this layer."""
    return self._sublayers[0]

  def forward_with_state(self, inputs, weights=base.EMPTY_WEIGHTS,
                         state=base.EMPTY_STATE, **kwargs):
    n_carry = self._n_carry
    def scannable_fn(x, carry_and_state):  # pylint: disable=invalid-name
      carry, state = carry_and_state
      res, new_state = self.sublayer.forward_with_state(
          x + carry, weights=weights, state=state, **kwargs)
      return (res[:-n_carry], (res[-n_carry:], new_state))

    xs = inputs[:-n_carry]  # Split input stack into inputs and carry.
    init = (inputs[-n_carry:], state)
    ys, (carry, new_state) = backend.scan(scannable_fn, xs, init,
                                          axis=self._axis)
    return ys + carry, new_state  # Put outputs and carry back on stack.

  def new_weights_and_state(self, input_signature):
    n_carry = self._n_carry
    xs = input_signature[:-n_carry]
    init = input_signature[-n_carry:]
    xs_slices = [ShapeDtype(_shape_without_axis(x, self._axis), x.dtype)
                 for x in xs]
    layer_signature = tuple(xs_slices + list(init))
    return self.sublayer.new_weights_and_state(layer_signature)


def Branch(*layers):
  """Combinator that applies a list of layers in parallel to copies of inputs.

  Each layer in the input list is applied to as many inputs from the stack
  as it needs, and their outputs are successively combined on stack.

  For example, suppose one has three layers:

    - F: 1 input, 1 output
    - G: 3 inputs, 1 output
    - H: 2 inputs, 2 outputs (h1, h2)

  Then Branch(F, G, H) will take 3 inputs and give 4 outputs:

    - inputs: a, b, c
    - outputs: F(a), G(a, b, c), h1, h2    where h1, h2 = H(a, b)

  As an important special case, a None argument to Branch acts as if it takes
  one argument, which it leaves unchanged. (It acts as a one-arg no-op.)

  Args:
    *layers: list of layers

  Returns:
    the branch layer
  """
  parallel_layer = Parallel(*layers)
  indices = [list(range(layer.n_in)) for layer in parallel_layer.sublayers]
  return Serial(Select(_deep_flatten(indices)), parallel_layer)


@base.layer(n_out=0)
def Drop(x, **unused_kwargs):
  """Drops the top stack element."""
  del x  # Just for the compiler.
  return ()


@base.layer(n_out=2)
def Dup(x, **unused_kwargs):
  """Duplicates (copies) the top element on the data stack."""
  return (x, x)


def Dup2():
  """Copies top 2 stack elements: (a, b, ...) -> (a, b, a, b, ...)."""
  return Select([0, 1, 0, 1])


def Dup3():
  """Copies top 3 stack elements: (a, b, c, ...) -> (a, b, c, a, b, c, ...)."""
  return Select([0, 1, 2, 0, 1, 2])


@base.layer(n_in=2, n_out=2)
def Swap(xs, **unused_kwargs):
  """Swaps the top two stack elements."""
  return (xs[1], xs[0])


def Select(idxs, n_in=None):
  """Permutes stack elements with copies according to provided indices."""
  if n_in is None:
    n_in = max(idxs) + 1
  @base.layer(n_in=n_in, n_out=len(idxs))
  def Selection(xs, **unused_kwargs):  # pylint: disable=invalid-name
    if not isinstance(xs, (tuple, list)):
      xs = (xs,)
    return tuple(xs[i] for i in idxs)
  return Selection()  # pylint: disable=no-value-for-parameter


def SerialWithSideOutputs(layers, n_side_outputs=1):
  """Serial layer with side outputs.

  This layer makes it easier to manage the stack when layers have side outputs.

  In the simplest case of layers with n_in=1, n_out=2 and with
  n_side_outputs=1 this layer runs the following computation on x:
    side_outputs = []
    for i in range(len(layers)):
      x, side_output = layers[i](x)
      side_outputs.append(side_output)
    return [x] + side_outputs

  In the general case of layers with variable n_in and n_out and
  n_side_outputs being a list of N integers, it does the following:
    side_outputs = []
    for i in range(N):
      res = layer[i](cur_stack)  # remove n_in from stack
      cur_stack.append(res[:n_side_outputs[i]])  # put back some on stack
      side_outputs.extend(res[n_side_outputs:])
    return cur_stack + side_outputs

  Args:
    layers: a list of layers to execute
    n_side_outputs: an int or a list of ints, how many outputs of each layer
      to put aside

  Returns:
    a layer that performs the above computation
  """
  if isinstance(n_side_outputs, int):
    n_side_outputs = [n_side_outputs] * len(layers)

  # Calculate the n_in for this layer.
  running_max = 0
  running_total = 0
  for layer, n_side_output in zip(layers, n_side_outputs):
    running_total += layer.n_in
    running_max = max(running_max, running_total)
    running_total -= layer.n_out - n_side_output
  n_in = running_max

  # Create the list of layers to run serially.
  cur_stack_size = n_in
  serial_layers = []
  for layer, n_side_output in zip(layers, n_side_outputs):
    serial_layers.append(layer)
    cur_stack_size += layer.n_out - layer.n_in
    # Indices to move n_side_outputs to the back of the stack.
    # Don't touch first n_out - n_side_outputs.
    move_back_indices = list(range(layer.n_out - n_side_output))
    # Then comes the rest of the stack that we're not moving.
    move_back_indices += [i + layer.n_out
                          for i in range(cur_stack_size - layer.n_out)]
    # Finally the indices we move.
    move_back_indices += [i + layer.n_out - n_side_output
                          for i in range(n_side_output)]
    # Swap them on stack.
    serial_layers.append(Select(move_back_indices))

  return Serial(serial_layers)


@base.layer(n_in=0)
def FlattenList(xs, **unused_kwargs):
  """Flatten lists."""
  # TODO(jonni): Consider renaming layer to DeepFlatten.
  return tuple(_deep_flatten(xs))


@base.layer(n_in=2)
def Add(xs, **unused_kwargs):
  """Adds two tensors."""
  return xs[0] + xs[1]


@base.layer(n_in=2)
def SubtractTop(xs, **unused_kwargs):
  """Subtracts the first tensor from the second."""
  return xs[1] - xs[0]


@base.layer(n_in=2)
def Multiply(xs, **unused_kwargs):
  """Multiplies two tensors."""
  return xs[0] * xs[1]


@base.layer(n_in=3)
def Gate(xs, **unused_kwargs):
  """Implements a gating function on a (memory, gate, candidate) tuple.

  Final update is memory * gate + (1-gate) * candidate

  This gating equation may also be referred to as Highway Network.
  Highway Networks: https://arxiv.org/abs/1505.00387

  Args:
    xs: A tuple of memory, gate, candidate

  Returns:
    The result of applying gating.
  """
  state, gate, candidate = xs
  return gate * state + (1.0 - gate) * candidate


def Residual(*layers, **kwargs):
  """Adds a residual connection in parallel to a series of layers."""
  # TODO(jonni): Change *layers arg to a single layer.
  shortcut = kwargs.get('shortcut')  # default None signals no-op
  return [
      Branch(shortcut, Serial(layers)),
      Add(),  # pylint: disable=no-value-for-parameter
  ]


# All module-private helper functions are below.
# pylint: disable=invalid-name


def _deep_flatten(items):
  """Returns a list of objects, flattening sublists/subtuples along the way.

  Example: _deep_flatten([1, (2, 3, (4, 5), [6, 7]), [[[8]]]]) would return
  the list [1, 2, 3, 4, 5, 6, 7, 8].

  Args:
    items: An iterable. If elements of this iterable are lists or tuples, they
        will be (recursively) flattened until non-list non-tuple objects are
        reached.

  Returns:
    A list of non-list, non-tuple objects.
  """
  def _flat_gen(xs):
    for x in xs:
      if isinstance(x, (list, tuple)):
        for y in _flat_gen(x):
          yield y
      else:
        yield x
  return list(_flat_gen(items))


def _ensure_sublayers(layers):
  """Ensures that elements in a layer list are layers.

  Args:
    layers: A tuple or list whose elements can each be a layer, tuple, or list,
        and so on recursively.

  Returns:
    An analogous collection of layers in which embedded layer lists are
    wrapped in Serial layer instances.
  """
  if not layers:  # None or an empty list can signal a no-op.
    return Serial(None)  # no-op, but still handles shapes and initialization
  elif isinstance(layers, (list, tuple)):
    sublayers_not_lists = []
    for layer in layers:
      sublayers_not_lists.append(
          Serial(layer) if isinstance(layer, (list, tuple)) else layer)
    return sublayers_not_lists
  else:
    raise TypeError(type(layers))


def _pop_rng_and_split(args_dict, n_copies):
  rng = args_dict.pop('rng', None)
  if rng is None:
    return (None,) * n_copies
  return backend.random.split(rng, n_copies)


def _inputs_from_stack(layer, stack):
  """Returns the correct number/format of inputs for the given layer."""
  is_stack_just_one_item = (_count_items(stack) == 1)
  if isinstance(stack, (list, tuple)) and is_stack_just_one_item:
    stack = stack[0]
  n_in = layer.n_in
  if n_in == 1 and is_stack_just_one_item:
    return stack
  elif n_in == 1:
    return stack[0]
  else:
    return stack[:n_in]


def _outputs_onto_stack(layer, outputs, stack):
  """"Returns the new stack after outputs have been pushed onto it."""
  n_in = layer.n_in
  if n_in < _count_items(stack):
    if layer.n_out == 1:
      outputs = (outputs,)
    return outputs + stack[n_in:]
  else:
    return outputs  # NOTE: can be single value or tuple.


def _count_items(xs):
  return len(xs) if isinstance(xs, (list, tuple)) else 1


def _shape_without_axis(x, axis):
  return x.shape[:axis] + x.shape[axis + 1:]
