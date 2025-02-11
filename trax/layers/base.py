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

"""Base layer class."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import copy
import inspect
import traceback

import jax

import numpy as onp
import six

from trax import backend
from trax.backend import nested_map
from trax.shapes import ShapeDtype
from trax.shapes import signature


EMPTY_WEIGHTS = ()
EMPTY_STATE = ()


class Layer(object):
  """Base class for composable layers in a deep learning network.

  Layers are the basic building blocks for deep learning models. A Trax layer
  computes a function from zero or more inputs to zero or more outputs,
  optionally using trainable weights (common) and non-parameter state (not
  common). Authors of new layer subclasses typically override at most two
  methods of the base `Layer` class:

    forward(inputs, weights):
      Computes this layer's output as part of a forward pass through the model.

    new_weights(self, input_signature):
      Returns new weights suitable for inputs with the given signature.

  A small subset of layer types are combinators -- they organize the computation
  of their sublayers, e.g., applying their sublayers in series or in parallel.

  All layers have the following properties, with default values implemented
  in the base `Layer` class:

    - n_in: int (default 1)
    - n_out: int (default 1)
    - weights: tuple (default empty -- the layer has no weights)
    - state: tuple (default empty -- the layer has no non-parameter state)
    - sublayers: tuple (default empty -- the layer has no sublayers)

  The inputs to a layer are tensors, packaged according to how many there are:

    - n_in = 0: an empty tuple ()
    - n_in = 1: one tensor (NOT wrapped in a tuple)
    - n_in > 1: a tuple of tensors

  (The special treatment of the single-input case is meant to simplify the
  work of layer writers; this design choice may be revisited in the future.)

  The outputs from a layer are also tensors, packaged the same as layer inputs:

    - n_out = 0: an empty tuple ()
    - n_out = 1: the tensor (NOT wrapped in a tuple)
    - n_out > 1: a tuple of tensors

  The Trax runtime maintains a data stack with which layer calls are composed.
  For more complex data network architectures, possibly involving multiple data
  flows, one can view each layer as a function from stack state to stack state,
  where the function's inputs are a slice from the stack, and the function's
  outputs are spliced back into the stack.
  """

  def __init__(self, n_in=1, n_out=1):
    """Creates a partially initialized, unconnected layer instance.

    Args:
      n_in: Number of inputs expected by this layer.
      n_out: Number of outputs promised by this layer.
    """
    self._n_in = n_in
    self._n_out = n_out
    self._sublayers = ()  # Default is no sublayers.
    self._input_signature = None
    self._rng = None
    self._weights = EMPTY_WEIGHTS  # cached weights
    self._state = EMPTY_STATE
    # record root call site for custom error messages:
    frame = _find_frame(inspect.currentframe())
    # Turns out that frame can mutate in time, so we just copy what we need.
    self._caller = {'filename': copy.copy(frame.f_code.co_filename),
                    'lineno': int(frame.f_lineno)}
    del frame  # Just in case.
    self._init_finished = False

  def __repr__(self):
    class_str = self.__class__.__name__
    fields_str = 'in={},out={}'.format(self.n_in, self.n_out)
    objs = self.sublayers
    if objs:
      objs_str = ', '.join(str(x) for x in objs)
      return '{}{{{},sublayers=[{}]}}'.format(class_str, fields_str, objs_str)
    else:
      return '{}{{{}}}'.format(class_str, fields_str)

  def __call__(self, x, **kwargs):
    """Makes Layer instances callable; for use in tests or interactive settings.

    This convenience method helps library users play with, test, or otherwise
    probe the behavior of layers outside of a full training environment. It
    presents the layer as callable function from inputs to outputs, with the
    option of manually specifying weights and non-parameter state per individual
    call. For convenience, weights and non-parameter state are cached per layer
    instance, starting from default values of `EMPTY_WEIGHTS` and `EMPTY_STATE`,
    and acquiring non-empty values either by initialization or from values
    explicitly provided via the weights and state keyword arguments.

    Args:
      x: 0 or more input tensors, formatted the same as the inputs to
          Layer.forward.
      **kwargs: Additional keyword arguments if needed/desired for this layer.
          Three possible keyword arguments are especially relevant:
            - weights=... will override any cached weights values
            - state=... will override any cached state values
            - rng=... will supply a PRNG key for use by the layer

    Returns:
      0 or more output tensors, formatted the same as the outputs from
          Layer.forward.
    """
    weights = kwargs.pop('weights', self.weights)
    state = kwargs.pop('state', self.state)
    rng = kwargs.pop('rng', None)
    outputs, _ = self._forward_internal(x, weights, state, rng)
    return outputs

  def forward(self, inputs, weights):
    """Computes this layer's output as part of a forward pass through the model.

    Authors of new Layer subclasses should override this method to define the
    forward computation that their layer performs, unless they need to use
    local non-trainable state or randomness, in which case they should
    override `forward_with_state` instead.

    Args:
      inputs: Input tensors, matching the number (n_in) expected by this
          layer. Specifically:
            - n_in = 0: an empty tuple ()
            - n_in = 1: a tensor (NOT wrapped in a tuple)
            - n_in > 1: a tuple of tensors, with n_in items
      weights: A tuple of trainable weights, with one element for this layer
          if this layer has no sublayers, or one for each sublayer if this
          layer has sublayers. If a layer (or sublayer) has no trainable
          weights, the corresponding weights element is an empty tuple.

    Returns:
      Tensors, matching the number (n_out) promised by this layer.
      Specifically:
        - n_out = 0: an empty tuple
        - n_out = 1: one tensor (NOT wrapped in a tuple)
        - n_out > 1: a tuple of tensors, with n_out items
    """
    raise NotImplementedError

  def forward_with_state(self, inputs, weights=EMPTY_WEIGHTS, state=EMPTY_STATE,
                         **kwargs):
    """Computes this layer's output as part of a forward pass through the model.

    Authors of new Layer subclasses should override this method to define the
    forward computation that their layer performs only if their layer uses
    local state or randomness. Otherwise override `forward` instead.

    Args:
      inputs: Input tensors, matching the number (n_in) expected by this
          layer. Specifically:
            - n_in = 0: an empty tuple ()
            - n_in = 1: a tensor (NOT wrapped in a tuple)
            - n_in > 1: a tuple of tensors, with n_in items
      weights: A tuple of trainable weights, with one element for this layer
          if this layer has no sublayers, or one for each sublayer if this
          layer has sublayers. If a layer (or sublayer) has no trainable
          weights, the corresponding weights element is an empty tuple.
      state: Layer-specific non-parameter state that can update between batches.
      **kwargs: Often empty; main current use is to carry a PRNG key for random
          number generation, using the keyword 'rng'.

    Returns:
      A tuple of (tensors, state). The tensors match the number (n_out) promised
      by this layer, and are formatted according to that number, specifically:
        - n_out = 0: an empty tuple
        - n_out = 1: one tensor (NOT wrapped in a tuple)
        - n_out > 1: a tuple of tensors, with n_out items
    """
    del kwargs
    return self.forward(inputs, weights), state

  def new_weights(self, input_signature):
    """Returns new weights suitable for inputs with the given signature.

    Authors of new Layer subclasses should override this method if their layer
    uses trainable weights. The default implementation works for layers that
    have no weights. Layers that have trainable state should override the
    `new_weights_and_state` method instead.

    Args:
      input_signature: A ShapeDtype instance (if this layer takes one input)
          or a list/tuple of ShapeDtype instances; signatures of inputs.
    """
    del input_signature
    return EMPTY_WEIGHTS

  def new_weights_and_state(self, input_signature):
    """Returns a (weights, state) pair suitable for initializing this layer.

    Authors of new Layer subclasses should override this method if their layer
    uses trainable weights or has non-parameter state that gets updated
    between batches. The default implementation works for layers that have
    no weights or state.

    Args:
      input_signature: A ShapeDtype instance (if this layer takes one input)
          or a list/tuple of ShapeDtype instances.
    """
    return self.new_weights(input_signature), EMPTY_STATE

  @property
  def has_backward(self):
    """Returns True if this layer provides its own (custom) backward pass code.

    A layer subclass that provides custom backward pass code (for custom
    gradients) must override this method to return True.
    """
    return False

  def backward(self, inputs, output, grad, weights, state, new_state, **kwargs):
    """Custom backward pass to propagate gradients in a custom way.

    Args:
      inputs: Input tensors; can be a (possibly nested) tuple.
      output: The result of running this layer on inputs.
      grad: gradient signal (called cotangent in jax) computed based on
        subsequent layers. The structure and shape must match output.
      weights: layer weights
      state: start state.
      new_state: end state computed by running the layer
      **kwargs: kwargs for the layer

    Returns:
      The custom gradient signal for the input. Note that we need to return
      a gradient for each argument of forward, so it will usually be a tuple
      of signals: the gradient for inputs and weights.
    """
    raise NotImplementedError

  # End of public subclassing interface.
  # Begin public callable interface.

  def init(self, input_signature, rng=None):
    """Initializes this layer and its sublayers recursively.

    This method is designed to initialize each layer instance once, even if the
    same layer instance occurs in multiple places in the network. This enables
    weight sharing to be implemented as layer sharing.

    Args:
      input_signature: A `ShapeDtype` instance (if this layer takes one input)
          or a list/tuple of `ShapeDtype` instances.
      rng: A single-use random number generator (JAX PRNG key). If none is
          provided, a default rng based on the integer seed 0 will be used.

    Returns:
      A (weights, state) tuple, in which weights contains newly created weights
          on the first call and `EMPTY_WEIGHTS` on all subsequent calls.
    """
    try:
      if self._rng is None:
        rng = backend.random.get_prng(0) if rng is None else rng
        self._set_rng_recursive(rng)
      # Initialize weights once; store them for use when this layer is called.
      # Needs to call new_weights_and_state regardless of _init_finished because
      # state also needs to be initialized. After jitting, graph pruning should
      # be able to remove unnecessary computation.
      # TODO(lukaszkaiser): Revisit this decision and see whether layers sharing
      #   weights should also share states.
      weights, state = self.new_weights_and_state(input_signature)
      if not self._init_finished:
        self._init_finished = True
        self._weights = weights
        self._state = state
        return (weights, state)
      else:
        return (EMPTY_WEIGHTS, state)
    except Exception:
      name, trace = self.__class__.__name__, _short_traceback(skip=3)
      raise LayerError(name, 'init', self._caller,
                       input_signature, trace)

  def new_rng(self):
    """Returns a new single-use random number generator (JAX PRNG key)."""
    self._rng, rng = backend.random.split(self._rng)
    return rng

  def new_rngs(self, n):
    """Returns `n` single-use random number generators (JAX PRNG keys).

    Args:
      n: The number of rngs to return; must be an integer > 0.

    Returns:
      A tuple of `n` rngs. Successive calls will yield continually new values.
    """
    if n < 1:
      raise ValueError('n must be > 0; received value: {}'.format(n))
    rngs = backend.random.split(self._rng, n + 1)
    self._rng = rngs[0]
    return tuple(rngs[1:])

  # End of public callable methods.
  # Methods and properties below are reserved for internal use.

  @property
  def n_in(self):
    """Returns how many tensors this layer expects as input."""
    return self._n_in

  @property
  def n_out(self):
    """Returns how many tensors this layer promises as output."""
    return self._n_out

  @property
  def sublayers(self):
    """Returns a tuple containing this layer's sublayers; may be empty."""
    return self._sublayers

  @property
  def input_signature(self):
    """Returns this layer's input signature.

    An input signature is a ShapeDtype instance (if the layer takes one input)
    or a tuple of ShapeDtype instances.
    """
    return self._input_signature

  @property
  def weights(self):
    """Returns this layer's weights.

    Depending on the layer, the weights can be in the form of:
      - an empty tuple
      - a tensor (ndarray)
      - a nested structure of tuples and tensors
    TODO(jonni): Simplify this picture (and underlying implementation).
    """
    return self._weights

  @weights.setter
  def weights(self, weights):
    self._weights = weights

  @property
  def state(self):
    """Returns a tuple containing this layer's state; may be empty."""
    return self._state

  @state.setter
  def state(self, state):
    self._state = state

  def _forward_internal(self, x, weights, state, rng):
    """Applies this layer as part of a forward pass; an internal system method.

    This method is reserved for handling plumbing and other internal affairs
    as needed by the overall library. Trax library users should use or override
    the `forward` method instead.

    Args:
      x: See Layer.forward_with_state inputs.
      weights: See Layer.forward_with_state.
      state: See Layer.forward_with_state.
      rng: See Layer.forward_with_state.

    Returns:
      See Layer.forward_with_state.
    """
    try:
      # If weights are nothing, we may be reusing this layer.
      # Use the cached weights to calculate the value.
      # Note: to make sure jit tracers can decide this branch in python we use
      # `weights is EMPTY_WEIGHTS` instead of, e.g., `not weights` or
      # `weights == EMPTY_WEIGHTS`.
      if weights is EMPTY_WEIGHTS:  # pylint: disable=literal-comparison
        weights = self._weights
      else:
        # In this case, we're called for the first time: cache weights.
        self._weights = weights

      if not self.has_backward:
        outputs, s = self.forward_with_state(
            x, weights=weights, state=state, rng=rng)
      else:
        outputs, s = self._do_custom_gradients(x, weights, state, rng=rng)
      self._state = s
      return outputs, s

    except Exception:
      name, trace = self.__class__.__name__, _short_traceback()
      raise LayerError(name, '_forward_internal',
                       self._caller, signature(x), trace)

  def _forward_abstract(self, input_signature):
    """Computes shapes and dtypes this layer would produce in a forward pass.

    Args:
      input_signature: A ShapeDtype instance (if this layer takes one input)
          or a list/tuple of ShapeDtype instances; signatures of inputs.

    Returns:
      A tuple of (output, state).

      The output part of the tuple is a ShapeDtype instance representing the
      shape and type of the output (if this layer has one output) or a tuple
      of ShapeDtype instances (if this layer has more than one output).
    """
    try:
      # Beware: using an actual RNG (as opposed to this ShapeDtype stub) would
      # cause a large number of dropout masks to be computed and permanently
      # stored in global memory.
      rng = ShapeDtype((2,), onp.uint32)
      def call_on_input(x, weights, state, rng):
        return self.forward_with_state(x, weights=weights, state=state, rng=rng)
      weight_signature = nested_map(signature, self.weights)
      s = backend.abstract_eval(call_on_input)(
          input_signature, weight_signature, self.state, rng)
      return s
    except Exception:
      name, trace = self.__class__.__name__, _short_traceback(skip=3)
      raise LayerError(name, '_forward_abstract', self._caller, input_signature,
                       trace)

  # pylint: disable=protected-access
  def _set_rng_recursive(self, rng):
    """Sets the rng (JAX PRNG key) for this layer and sublayers, recursively."""
    self._rng = rng
    sublayers = self.sublayers
    if sublayers:
      rngs = backend.random.split(rng, len(sublayers))
      for sublayer, rng in zip(sublayers, rngs):
        sublayer._rng = rng

  def _set_input_signature_recursive(self, input_signature):
    """Sets input_signatures for this layer and sublayers, recursively.

    General combinators (those that can take multiple sublayers) must override
    this method to calculate and set input signatures for the sublayers. (See
    the `Serial` class in combinators.py for an example.)

    Args:
      input_signature: A `ShapeDtype` instance (if this layer takes one input)
          or a list/tuple of `ShapeDtype` instances
    """
    self._input_signature = input_signature

    # Handle the special case of a single immediate sublayer (which may in turn
    # have its own sublayers).
    sublayers = self.sublayers
    if sublayers and len(sublayers) == 1:
      sublayers[0]._set_input_signature_recursive(input_signature)
    if sublayers and len(sublayers) > 1:
      raise ValueError('A layer class whose instances can have more than one '
                       'sublayer must override the input_signature property '
                       'setter.')
  # pylint: enable=protected-access

  def _do_custom_gradients(self, x, weights, state, **kwargs):
    """Calls this layer for a forward pass, but with custom gradients."""
    assert backend.get_name() == 'jax', (
        'Custom gradients are only supported in JAX for now.')

    # See this link for how custom transformations are defined in JAX:
    # https://jax.readthedocs.io/en/latest/jax.html#jax.custom_transforms
    # Note that we capture the kwargs and don't calculate gradients wrt. them.
    @jax.custom_transforms
    def _do_forward(y, weights):
      res = self.forward_with_state(
          y, weights=weights, state=state, **kwargs)
      return res

    # This is the custom gradient (vector-jacobian product in JAX) function.
    # For the exact specification of this custom transformation see this link:
    # https://jax.readthedocs.io/en/latest/jax.html#jax.defjvp_all
    def do_forward_vjp(y, weights):
      """Custom gradient (vjp) function."""
      output, new_state = self.forward_with_state(
          y, weights=weights, state=state, **kwargs)
      def vjpfun(grad):
        grad = grad[0]  # Ignore dummy gradient wrt state.
        res = self.backward(
            y, output, grad, weights, state, new_state, **kwargs)
        return res
      return (output, state), vjpfun

    jax.defvjp_all(_do_forward, do_forward_vjp)
    output, state = _do_forward(x, weights)
    state = jax.lax.stop_gradient(state)
    return output, state


def layer(n_in=1, n_out=1, new_weights_fn=None):
  """Returns a decorator that converts a function into a Layer class builder."""

  def _build_layer_class(raw_fn):
    """Returns a Layer class whose callable instances execute the function."""

    def _init(self, **kwargs):
      self._kwargs = kwargs  # pylint: disable=protected-access
      Layer.__init__(self, n_in=n_in, n_out=n_out)

    def _forward(self, x, weights):
      """Uses this layer as part of a forward pass through the model."""
      _validate_forward_input(x, n_in)
      raw_output = raw_fn(x, weights=weights, **self._kwargs)  # pylint: disable=protected-access
      output = () if _is_empty(raw_output) else raw_output
      return output

    def _new_weights(self, input_signature):
      if new_weights_fn is None:
        return EMPTY_WEIGHTS
      kwargs = self._kwargs  # pylint: disable=protected-access
      return new_weights_fn(input_signature, **kwargs)

    def _is_empty(raw_output):
      return raw_output is None or (isinstance(raw_output, (list, tuple))
                                    and len(raw_output) == 0)  # pylint: disable=g-explicit-length-test

    # Set docstrings and create the class.
    _forward.__doc__ = raw_fn.__doc__
    _new_weights.__doc__ = new_weights_fn.__doc__
    # Note: None.__doc__ is None
    cls = type(raw_fn.__name__, (Layer,),
               {'__init__': _init,
                'forward': _forward,
                'new_weights': _new_weights})
    return cls

  return _build_layer_class


class LayerError(Exception):
  """Exception raised in the layer stack.

  Attributes:
    message: the message corresponding to this exception.
  """

  def __init__(self, layer_name, function_name, caller,
               input_signature, traceback_string):
    self._layer_name = layer_name
    self._function_name = function_name
    self._caller = caller  # Python inspect object with init caller info.
    self._traceback = traceback_string
    self._input_signature = input_signature
    super(LayerError, self).__init__(self.message)

  @property
  def message(self):
    """Create error message."""
    prefix = 'Exception passing through layer '
    prefix += '%s (in %s):\n' % (self._layer_name, self._function_name)
    short_path = '[...]/' + '/'.join(
        self._caller['filename'].split('/')[-3:])
    caller = '  layer created in file %s, line %d\n' % (short_path,
                                                        self._caller['lineno'])
    shapes_str = '  layer input shapes: %s\n\n' % str(self._input_signature)
    return prefix + caller + shapes_str + self._traceback


def check_shape_agreement(layer_obj, input_signature):
  """Compares the layer's __call__ output to its _foward_abstract shape output.

  This function helps test layer mechanics and inter-layer connections that
  aren't dependent on specific data values.

  Args:
    layer_obj: A layer object.
    input_signature: A `ShapeDtype` instance (if `layer_obj` takes one input)
        or a list/tuple of ShapeDtype instances.

  Returns:
    A tuple representing either a single shape (if the layer has one output) or
    a tuple of shape tuples (if the layer has more than one output).
  """
  weights, state = layer_obj.init(input_signature)
  output_signature, _ = layer_obj._forward_abstract(input_signature)  # pylint: disable=protected-access
  if isinstance(output_signature, tuple):
    shape_output = tuple(x.shape for x in output_signature)
  else:
    shape_output = output_signature.shape

  rng1, rng2 = layer_obj.new_rngs(2)
  random_input = _random_values(input_signature, rng1)
  call_output = layer_obj(random_input, weights=weights, state=state, rng=rng2)
  call_output_shape = _shapes(call_output)

  msg = '_foward_abstract shape output %s != __call__ output shape %s' % (
      shape_output, call_output_shape)
  assert shape_output == call_output_shape, msg
  # TODO(jonni): Remove this assert? It makes test logs harder to read.
  return shape_output


def _validate_forward_input(x, n_in):
  if n_in != 1:
    if not isinstance(x, tuple):
      raise TypeError(
          'expected input to be a tuple; instead received {}'.format(type(x)))
    if len(x) != n_in:
      raise ValueError(
          'input tuple length ({}) does not equal required number of inputs'
          ' ({})'.format(len(x), n_in))


def _find_frame(frame):
  """Find the frame with the caller on the stack."""
  # TODO(lukaszkaiser): rewrite this function in a systematic way.
  # We want to find the first place where the layer was called
  # that is *not* an __init__ function of an inheriting layer.
  # We also need to exclude a few decorator functions.
  while frame.f_code.co_name in ['__init__', 'gin_wrapper', '_validate',
                                 '_validate_forward_inputs', '_init']:
    # We only skip __init__ in internal layers, return otherwise.
    dirname = frame.f_code.co_filename.split('/')[-2]
    if dirname != 'layers' and frame.f_code.co_name == '__init__':
      return frame
    # If we are in an init, move up.
    frame = frame.f_back
  return frame


def _shorten_file_path(line):
  """Shorten file path in error lines for more readable tracebacks."""
  start = line.lower().find('file')
  if start < 0:
    return line
  first_quote = line.find('"', start)
  if first_quote < 0:
    return line
  second_quote = line.find('"', first_quote + 1)
  if second_quote < 0:
    return line
  path = line[first_quote + 1:second_quote]
  new_path = '/'.join(path.split('/')[-3:])
  return line[:first_quote] + '[...]/' + new_path + line[second_quote + 1:]


def _short_traceback(skip=3):
  """Cleaned-up form of traceback."""
  counter, res = 0, []
  # Skipping 3 lines by default: the top (useless) and self-call.
  # In python 3, we need to set chain to False (it doesn't exist in python 2).
  if six.PY2:
    lines = traceback.format_exc().splitlines()[skip:]
  else:
    lines = traceback.format_exc(chain=False).splitlines()[skip:]  # pylint: disable=unexpected-keyword-arg
  for l in lines:
    if l.startswith('trax.layers.base.LayerError'):
      l = l[len('trax.layers.base.'):]  # Remove the trax.layers.base prefix.
    res.append(_shorten_file_path(l))
    if counter % 2 == 1:
      res.append('')
    counter += 1
    # If we see a LayerError, the traceback has already been processed.
    if l.startswith('LayerError'):
      # Skip 4 back except last as these are internal base-layer calls.
      res = res[:-4] + [res[-1]]
      res += lines[counter:]
      break
  return '\n'.join(res)


def _random_values(input_signature, rng):
  """Creates random floats or ints of the given shape.

  Args:
    input_signature: A `ShapeDtype` instance (if `layer_obj` takes one input)
        or a list/tuple of ShapeDtype instances.
    rng: A random number generator.

  Returns:
    Random values with the shape and type specified.
  """
  if isinstance(input_signature, ShapeDtype):
    shape, dtype = input_signature.shape, input_signature.dtype
    if onp.issubdtype(dtype, onp.integer):
      return backend.random.bernoulli(rng, 0.5, shape).astype(onp.int32)
    else:
      return backend.random.uniform(rng, shape, minval=-1.0, maxval=1.0)
  elif isinstance(input_signature, (list, tuple)):
    return tuple(_random_values(x, rng) for x in input_signature)
  else:
    raise TypeError(type(input_signature))


def _shapes(x):
  """Get a structure of shapes for a structure of nested arrays."""
  def shape(x):
    try:
      return tuple([int(i) for i in x.shape])
    except Exception:  # pylint: disable=broad-except
      return ()
  return tuple(nested_map(shape, x))
