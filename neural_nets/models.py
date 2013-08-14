import numpy as np
from itertools import izip
from hashlib import md5
from pycuda import gpuarray
from pycuda.gpuarray import GPUArray
from pycuda.curandom import rand as curand
from pycuda import cumath
from math import sqrt
from scikits.cuda import linalg
from .pycuda_ops import eps
from .pycuda_ops.elementwise import sigmoid, df_sigmoid, \
     tanh, df_tanh, relu, df_relu, linear, df_linear, \
     sample_dropout_mask, apply_dropout_mask, sign, \
     nan_to_zeros
from .pycuda_ops.matrix import add_vec_to_mat
from .pycuda_ops.reductions import matrix_sum_out_axis
from .pycuda_ops.softmax import softmax, cross_entropy

class HiddenLayer(object):
    n_parameters = 2
    W = None
    b = None
    
    def __init__(self, n_in, n_units, 
                 activation_function='sigmoid',
                 dropout=False,
                 W = None, b = None,
                 parameters=None,
                 weights_scale=None,
                 lr_multiplier=None,
                 l1_penalty_weight=0., l2_penalty_weight=0.):

        self._set_activation_fct(activation_function)

        if weights_scale is None:
            self._set_weights_scale(activation_function, n_in, n_units)
        else:
            self.weights_scale = weights_scale

        if parameters is not None:
            if isinstance(parameters, basestring):
                self.parameters = cPickle.loads(open(parameters, 'b'))
            else:
                self.W, self.b = parameters
        else:
            self.W = self.weights_scale * curand((n_in, n_units), dtype=np.float32) \
              - .5 * self.weights_scale

            self.b = gpuarray.zeros((n_units,), dtype=np.float32)

        assert self.W.shape == (n_in, n_units)
        assert self.b.shape == (n_units,)
            
        self.n_in = n_in
        self.n_units = n_units

        self.lr_multiplier = lr_multiplier if lr_multiplier is not None else \
            2 * [1. / np.sqrt(self.n_in, dtype=np.float32)]

        self.l1_penalty_weight = l1_penalty_weight
        self.l2_penalty_weight = l2_penalty_weight

        self.dropout = dropout

    @property
    def parameters(self):
        return (self.W, self.b)

    @parameters.setter
    def parameters(self, value):
        self.W = value[0] if isinstance(value[0], GPUArray) else \
          gpuarray.to_gpu(value[0])
        self.b = value[1] if isinstance(value[0], GPUArray) else \
          gpuarray.to_gpu(value[1])

    def update_parameters(self, values, stream=None):
        assert len(values) == self.n_parameters
        
        for (param, (gparam, mult)) \
            in izip((self.W, self.b), values):
            param._axpbyz(1., gparam, mult, param, 
                          stream=stream)

    @property
    def architecture(self):
        arch= {'class': self.__class__,
               'n_in': self.n_in, 
               'n_units': self.n_units,
               'activation_function': self.activation_function 
                 if hasattr(self, 'activation_function') else None}
        return arch

    @staticmethod
    def _resolve_activation_fct(activation_function):
        if activation_function == 'sigmoid':
            f = sigmoid
            df = df_sigmoid
        elif activation_function == 'tanh':
            f = tanh
            df = df_tanh
        elif activation_function == 'relu':
            f = relu
            df = df_relu
        elif activation_function == 'linear':
            f = linear
            df = df_linear
        else:
            raise ValueError

        return f, df

    def _set_activation_fct(self, activation_function):
        self.activation_function = activation_function
        self.f, self.df = self._resolve_activation_fct(activation_function)

    def _set_weights_scale(self, activation_function, n_in, n_units):
        if activation_function in ('tanh', 'relu', 'linear'):
            self.weights_scale = sqrt(6. / (n_in + n_units))
        elif activation_function == 'sigmoid':
            self.weights_scale = 4 * sqrt(6. / (n_in + n_units))
        else:
            raise ValueError

    @property
    def l1_penalty(self):
        return float(self.l1_penalty_weight) * gpuarray.sum(abs(self.W)).get()

    @property
    def l2_penalty(self):
        return float(self.l2_penalty_weight) * .5 * gpuarray.sum(self.W ** 2.).get()

    def feed_forward(self, input, prediction=False):
        """ Propagate forward through the hidden layer.
        Inputs:
        input -- input from the previous layer
        prediction -- (bool) whether predicting or training

        Outputs:
        lin_activations
        activations

        If self.dropout = True and prediction=False:
        Output:
        lin_activations
        activations
        dropout_mask: binary mask of dropped units

        """

        activations = linalg.dot(input, self.W)
        activations = add_vec_to_mat(activations, self.b, inplace=True)
        
        self.f(activations)

        if self.dropout and prediction:
            activations *= .5
        
        if self.dropout and not prediction:
            dropout_mask = sample_dropout_mask(activations)
            return activations, dropout_mask
        
        return (activations,)

    def backprop(self, input, df_output, cache=None):
        """ Backpropagate through the hidden layer

        Inputs:
        input
        df_output: the gradient wrt the output units
        cache (optional): cache object from the forward pass

        Output:
        df_W: gradient wrt the weights
        df_b: gradient wrt the bias        
        df_input: gradient wrt the input

        """

        # Get cache if it wasn't provided
        if cache is None:
            cache = self.feed_forward(input, dropout=self.dropout,
                                      prediction=False)

        if len(cache) == 2:
            activations, dropout_mask = cache
        else:
            activations = cache[0]

        # Multiply the binary mask with the incoming gradients
        if self.dropout and dropout_mask is not None:
            apply_dropout_mask(df_output, dropout_mask)

        # Get gradient wrt activation function
        df_activations = self.df(activations)
        delta = df_activations * df_output

        df_W = linalg.dot(input, delta, transa='T')     # Gradient wrt weights
        df_b = matrix_sum_out_axis(delta, 0)  # Gradient wrt bias
        df_input = linalg.dot(delta, self.W, transb='T')   # Gradient wrt inputs

        # L1 weight decay
        if self.l1_penalty_weight:
            df_W -= self.l1_penalty_weight * sign(self.W)

        # L2 weight decay
        if self.l2_penalty_weight:
            df_W -= self.l2_penalty_weight * self.W
        
        return (df_W, df_b), df_input

class DummyLayer(HiddenLayer):
    """ This class has no input and simply passes through its input
    """

    lr_multiplier = []
    n_parameters = 0
    l1_penalty_weight = 0.
    l2_penalty_weight = 0.
    dropout = False

    def __init__(self, n_in):
        self.n_in = n_in
        self.n_units = n_in

    @property
    def parameters(self):
        return []

    @parameters.setter
    def parameters(self, value):
        pass

    def update_parameters(self, values, stream=None):
        pass

    @property
    def l1_penalty(self):
        return 0.

    @property
    def l2_penalty(self):
        return 0.

    def feed_forward(self, input, prediction=False):
        assert input.shape[1] == self.n_in
        return (input,)

    def backprop(self, input, df_output, cache=None):
        return tuple(), df_output

class TopLayer(HiddenLayer):
    n_tasks = 1
    
class LogisticLayer(TopLayer):
    """ A logistic classification layer, using
    cross-entropy and softmax activation.

    """

    act_f = softmax
    loss_f = cross_entropy
    n_parameters = 2
    
    def __init__(self, n_in, n_out, 
                 parameters=None,
                 weights_scale=None,
                 l1_penalty_weight=0., l2_penalty_weight=0.,
                 lr_multiplier=None,
                 test_error_fct='class_error'):
        """ Inputs:
        n_in: number of input units
        n_out: number of output units (classes)
        loss_function: currently only works with cross_entropy

        """

        # Initialize weight using Bengio's rule
        self.weights_scale = 4 * sqrt(6. / (n_in + n_out)) if weights_scale is None \
          else weights_scale

        if parameters is not None:
            if isinstance(parameters, basestring):
                self.parameters = cPickle.loads(open(parameters, 'b'))
            else:
                self.W, self.b = parameters
        else:
            self.W = self.weights_scale * curand((n_in, n_out), dtype=np.float32) \
              - .5 * self.weights_scale

            self.b = gpuarray.zeros((n_out,), dtype=np.float32)

        self.n_in = n_in
        self.n_out = n_out

        self.test_error_fct = test_error_fct

        self.l1_penalty_weight = l1_penalty_weight
        self.l2_penalty_weight = l2_penalty_weight

        self.lr_multiplier = 2 * [1. / np.sqrt(n_in, dtype=np.float32)] \
          if lr_multiplier is None else lr_multiplier
        

    @property
    def architecture(self):
        return {'class': self.__class__,
                'n_in': self.n_in, 
                'n_out': self.n_out}

    def feed_forward(self, input, prediction=False):
        """ Propagate forward through the layer

        Inputs:
        input
        return_cache: (bool) whether to return the cache object
        prediction: (bool) whether to half the weights when 
            the preceding layer uses dropout

        Outputs:
        activations

        """
        activations = linalg.dot(input, self.W)
        activations = add_vec_to_mat(activations, self.b, inplace=True)
        activations = softmax(activations)

        return activations

    def backprop(self, input, targets,
                 cache=None):
        """ Backpropagate through the logistic layer

        Inputs:
        input
        targets
        get_df_input: (bool) whether to compute and return the 
            gradient wrt the inputs
        return_cache: (bool) whether to return the cache
        cache: cache object from forward pass

        """
        
        if cache is not None:
            activations = cache
        else:
            activations = self.feed_forward(input, prediction=False)

        delta = activations - targets
        nan_to_zeros(delta, delta)
        
        df_W = linalg.dot(input, delta, transa='T')    # Gradient wrt weights
        df_b = matrix_sum_out_axis(delta, 0)               # Gradient wrt bias

        df_input = linalg.dot(delta, self.W, transb='T')   # Gradient wrt input

        # L1 penalty
        if self.l1_penalty_weight:
            df_W -= self.l1_penalty_weight * sign(self.W)

        # L2 penalty
        if self.l2_penalty_weight:
            df_W -= self.l2_penalty_weight * self.W

        return (df_W, df_b), df_input

    def test_error(self, input, targets, average=True,
                   cache=None, prediction=True):
        if self.test_error_fct == 'class_error':
            test_error = self.class_error
        elif self.test_error_fct == 'kl_error':
            test_error = self.kl_error
        elif self.test_error_fct == 'cross_entropy_error':
            test_error = self.cross_entropy_error            
        else:
            raise ValueError('unknown test error function "%s"' 
                             % self.test_error_fct)

        return test_error(input, targets, average,
                          cache, prediction)

    def cross_entropy_error(self, input, targets, average=True,
                            cache=None, prediction=False):
        """ Return the cross entropy error
        """
        
        if cache is not None:
            activations = cache
        else:
            activations = \
              self.feed_forward(input, prediction=prediction)

        loss = cross_entropy(activations, targets)

        if average: loss = loss.mean()
        return loss

    def class_error(self, input, targets, average=True, 
                    cache=None, prediction=False):
        """ Return the classification error rate
        """
        
        if cache is not None:
            activations = cache
        else:
            activations = \
              self.feed_forward(input, prediction=prediction)

        targets = targets.get().argmax(1)
        class_error = np.sum(activations.get().argmax(1) != targets)

        if average: class_error = class_error.mean()
        return class_error

    def kl_error(self, input, targets, average=True, 
                 cache=None, prediction=True):
        """ The KL divergence error
        """
        
        if cache is not None:
            activations = cache
        else:
            activations = \
              self.feed_forward(input, prediction=prediction)

        targets_non_nan = gpuarray.empty_like(targets)
        nan_to_zeros(targets, targets_non_nan)
        kl_error = gpuarray.sum(targets_non_nan * 
                                (cumath.log(targets_non_nan + eps) -
                                 cumath.log(activations + eps)))
        if average:
            kl_error /= targets.shape[0]
        return float(kl_error.get())

class NeuralNet(object):
    """ A Neural Network Object
    """

    TopLayerClass = LogisticLayer

    def __init__(self, layers, top_layer=None, activation_function='sigmoid', 
                 dropout=False, n_in=None, n_out=None,
                 l1_penalty_weight=0., l2_penalty_weight=0.,
                 **kwargs):
        self.n_layers = len(layers)

        if l1_penalty_weight is not None and \
           not np.isscalar(l1_penalty_weight) and \
           len(l1_penalty_weight) != (self.n_layers + 1):
            raise ValueError("l1_penalty_weight must be a scalar or have length %d",
                             self.n_layers + 1)

        if l2_penalty_weight is not None and \
           not np.isscalar(l2_penalty_weight) and \
           len(l2_penalty_weight) != (self.n_layers + 1):
            raise ValueError("l2_penalty_weight must be a scalar or have length %d",
                             self.n_layers + 1)

        if np.isscalar(l1_penalty_weight):
            self.l1_penalty_weight_hidden = self.n_layers * [l1_penalty_weight]
            self.l1_penalty_weight_output = l1_penalty_weight
        else:
            self.l1_penalty_weight_hidden = l1_penalty_weight[:-1]
            self.l1_penalty_weight_output = l1_penalty_weight[-1]

        if np.isscalar(l2_penalty_weight):
            self.l2_penalty_weight_hidden = self.n_layers * [l2_penalty_weight]
            self.l2_penalty_weight_output = l2_penalty_weight
        else:
            self.l2_penalty_weight_hidden = l2_penalty_weight[:-1]
            self.l2_penalty_weight_output = l2_penalty_weight[-1]
        
        if type(dropout) is not list:
            if self.n_layers:
                dropout = self.n_layers * [dropout]
            else:
                dropout = [False]

        self.hidden_layers = []
        for i, hidden_layer in enumerate(layers):
            if isinstance(hidden_layer, HiddenLayer):
                self.hidden_layers.append(hidden_layer)
            elif isinstance(hidden_layer, int):
                n_in_hidden = self.hidden_layers[-1].n_units if i > 0 else n_in
                self.hidden_layers.append(
                    HiddenLayer(n_in_hidden, hidden_layer,
                                activation_function,
                                dropout=dropout[i],
                                l1_penalty_weight=self.l1_penalty_weight_hidden[i],
                                l2_penalty_weight=self.l2_penalty_weight_hidden[i]))
                
        self.n_units_hidden = [hl.n_units for hl in self.hidden_layers]

        if top_layer is None:
            assert issubclass(self.TopLayerClass, TopLayer)
            n_in_top_layer = self.n_units_hidden[-1] if self.n_units_hidden else n_in
            self.top_layer = self.TopLayerClass(n_in_top_layer, n_out, 
                                                l1_penalty_weight=self.l1_penalty_weight_output,
                                                l2_penalty_weight=self.l2_penalty_weight_output,
                                                **kwargs)
        else:
            self.top_layer = top_layer

        self.n_in = self.hidden_layers[0].n_in
        self.n_out = self.top_layer.n_out

        self.n_parameters = sum(hl.n_parameters for hl in self.hidden_layers) + \
          self.top_layer.n_parameters

        self.lr_multiplier = [lr for hl in self.hidden_layers + [self.top_layer]
                              for lr in hl.lr_multiplier]

    @property
    def parameters(self):
        # Gather the parameters
        parameters = []
        for hl in self.hidden_layers:
            parameters.extend(hl.parameters)
        parameters.extend(self.top_layer.parameters)
        return parameters

    @parameters.setter
    def parameters(self, value):
        if len(value) != self.n_parameters:
            raise ValueError("Incorrect length of parameter vector. Model has %d parameters, but got %d" %
                             (self.n_parameters, len(value)))

        i = 0
        for hl in self.hidden_layers:
            hl.parameters = value[i:i+hl.n_parameters]
            i += hl.n_parameters

        self.top_layer.parameters = value[-self.top_layer.n_parameters:]

    def update_parameters(self, value):
        assert len(value) == self.n_parameters

        i = 0
        for hl in self.hidden_layers:
            hl.update_parameters(value[i:i+hl.n_parameters])
            i += hl.n_parameters

        self.top_layer.update_parameters(value[-self.top_layer.n_parameters:])

    @property
    def checksum(self):
        m = md5()
        for hl in self.hidden_layers:
            m.update(str(hl.architecture))
        m.update(str(self.top_layer.architecture))
        return m.hexdigest()

    def evaluate(self, input, targets, return_cache=False, prediction=True):
        """ Evaluate the loss function without computing gradients
        """

        # Forward pass
        activations, hidden_cache = self.feed_forward(
            input, return_cache=True, prediction=prediction)
        
        loss = self.top_layer.cross_entropy_error(None,
            targets, average=False, cache=activations,
            prediction=prediction)
 
        for hl in self.hidden_layers:
            if hl.l1_penalty_weight: loss += hl.l1_penalty
            if hl.l2_penalty_weight: loss += hl.l2_penalty

        if self.top_layer.l1_penalty_weight: loss += self.top_layer.l1_penalty
        if self.top_layer.l2_penalty_weight: loss += self.top_layer.l2_penalty

        if not return_cache:
            return loss
        else:
            return loss, hidden_cache, activations

    def training_pass(self, input, targets):
        """ Perform a full forward and backward pass through the model
        """
        
        # Forward pass
        loss, hidden_cache, logistic_cache = self.evaluate(
            input, targets, return_cache=True, prediction=False)

        # Backpropagation
        if self.hidden_layers:
            hidden_activations = hidden_cache[-1][0]
        else:
            hidden_activations = input

        df_top_layer = \
          self.top_layer.backprop(hidden_activations, targets,
                                  cache=logistic_cache)
        gradients = list(df_top_layer[0][::-1])
        df_hidden = df_top_layer[1]

        hidden_inputs = [input] + [c[0] for c in hidden_cache[:-1]]
        for hl, hc, hi in \
            zip(self.hidden_layers[::-1], hidden_cache[::-1], 
                hidden_inputs[::-1]):
            g, df_hidden = hl.backprop(hi, df_hidden, cache=hc)
            gradients.extend(g[::-1])

        gradients.reverse()

        return loss, gradients

    def test_error(self, input, targets, average=True, cache=None):
        """ Evaulate performance on a test set

        """
        if cache is None:
            loss, hidden_cache, logistic_cache = self.evaluate(input, targets,
                                                               return_cache=True,
                                                               prediction=True)
        else:
            loss, hidden_cache, logistic_cache = cache

        if self.hidden_layers:
            hidden_activations = hidden_cache[-1]
        else:
            hidden_activations = input

        return self.top_layer.test_error(hidden_activations, targets, average=average,
                                         cache=logistic_cache, prediction=True)

    def feed_forward(self, input, return_cache=False, prediction=True):
        """ Get predictions from the model
        """

        if self.hidden_layers:
            # Forward pass
            hidden_cache = []
            # Input layer never has dropout
            hidden_cache.append(self.hidden_layers[0].feed_forward(input,
                                                                   prediction))

            for i in range(1, self.n_layers):
                hidden_activations = hidden_cache[i-1][0]
                # Use dropout predict if previous layer has dropout
                hidden_cache.append(self.hidden_layers[i]
                                    .feed_forward(hidden_activations,
                                                  prediction=prediction))

            hidden_activations = hidden_cache[-1][0]

        else:
            hidden_activations = input
            
        # Use dropout_predict if last hidden layer has dropout
        activations = \
          self.top_layer.feed_forward(hidden_activations, 
                                      prediction=prediction)

        if return_cache:
            return activations, hidden_cache
        return activations

################################################################################
### Multitask-Learning Neural Net
###

class MultitaskTopLayer(TopLayer):

    def __init__(self, n_in=None, n_out=None, test_error_fct='class_error',
                 l1_penalty_weight=0., l2_penalty_weight=0.,
                 tasks=None, task_weights=None, n_tasks=None,
                 lr_multiplier=None):
        """ Inputs:
        n_in: number of input units (size of last hidden layer)
        n_out: sequence of output sizes for the targets
        test_error_fct: name of test error function
        l1_penalty_weight: scalar or sequence of l1 penalty weights
        l2_penalty_weight: scalar or sequence of l2 penalty weights
        tasks: sequence of TopLayer objects; overrides all_other parameters
        """

        if tasks is None and (n_in is None or n_out is None):
            raise ValueError('Either `tasks` or `n_in` and `n_out` ' + 
                             'must be provided')

        if not tasks:
            self.n_in = n_in
            self.n_out = n_out if n_tasks is None else n_tasks * [n_out]
            self.n_tasks = n_tasks if n_tasks is not None else len(n_out)       # Number of output tasks
            self.tasks = []

            if not isinstance(test_error_fct, (list, tuple)):
                test_error_fct = self.n_tasks * [test_error_fct]
            if not isinstance(l1_penalty_weight, (list, tuple)):
                l1_penalty_weight = self.n_tasks * [l1_penalty_weight]
            if not isinstance(l2_penalty_weight, (list, tuple)):
                l2_penalty_weight = self.n_tasks * [l2_penalty_weight]

            for (n_out_task, test_error_task, l1_task, l2_task) in \
              zip(self.n_out, test_error_fct, l1_penalty_weight, l2_penalty_weight):
                self.tasks.append(LogisticLayer(n_in=n_in, 
                                                n_out=n_out_task,
                                                l1_penalty_weight=l1_task, 
                                                l2_penalty_weight=l2_task,
                                                test_error_fct=test_error_task,
                                                lr_multiplier=lr_multiplier))

        else:
            assert all([self.tasks[0].n_in == t.n_in for t in tasks])
            self.tasks = top_layers

            self.n_in = self.tasks[0].n_in
            self.n_out = [t.n_out for t in self.tasks]

        if task_weights is not None:
            self.task_weights = task_weights
        else:
            self.task_weights = self.n_tasks * [1.]

        self.l1_penalty_weight = l1_penalty_weight
        self.l2_penalty_weight = l2_penalty_weight

        self.n_parameters = sum(task.n_parameters for task in self.tasks)
        self.lr_multiplier = [lr for task in self.tasks for lr in task.lr_multiplier]

    @property
    def parameters(self):
        parameters = []
        for task in self.tasks:
            parameters.extend(task.parameters)
        return parameters

    @parameters.setter
    def parameters(self, value):
        assert len(value) == self.n_parameters

        i = 0
        for task in self.tasks:
            task.parameters = value[i:i+task.n_parameters]
            i += task.n_parameters

    def update_parameters(self, value):
        assert len(value) == self.n_parameters
        i = 0
        for task in self.tasks:
            task.update_parameters(value[i:i+task.n_parameters])
            i += task.n_parameters

    @property
    def architecture(self):
        return [task.architecture for task in self.tasks]

    @property
    def l1_penalty(self):
        return sum([task.l1_penalty for task in self.tasks])

    @property
    def l2_penalty(self):
        return sum([task.l2_penalty for task in self.tasks])

    def feed_forward(self, input, prediction=False):
        activations = []

        for task in self.tasks:
            activations_task = task.feed_forward(input, prediction)
            activations.append(activations_task)

        return activations

    def backprop(self, input, targets, cache=None):

        output = []
        df_input = gpuarray.zeros_like(input)
        cache_out = []

        if cache is None: cache = self.n_tasks * [None]

        gradients = []
        for targets_task, cache_task, task, task_weight  in \
          izip(targets, cache, self.tasks, self.task_weights):
            gradients_task, df_input_task = \
              task.backprop(input, targets_task,
                            cache_task)
  
            df_input.mul_add(1., df_input_task, task_weight)

            gradients.extend(gradients_task)
        
        return gradients, df_input

    def test_error(self, input, targets, average=True,
                   cache=None, prediction=False,
                   sum_errors=True):

        test_error = []
        if cache is None:
            cache = self.n_tasks * [None]
        for targets_task, cache_task, task in \
          izip(targets, cache, self.tasks):
          test_error.append(task.test_error(input, targets_task,
                                            average, cache_task,
                                            prediction))

        if sum_errors:
            return sum(test_error)
        else:
            return np.array(test_error)

    def cross_entropy_error(self, input, targets, average=True,
                            cache=None, prediction=False,
                            sum_errors=True):
        """ Return the cross entropy error
        """

        loss = []
        if cache is None:
            cache = self.n_tasks * [None]

        for targets_task, cache_task, task in \
            izip(targets, cache, self.tasks):
            loss.append(task.cross_entropy_error(
                input, targets_task, average=average,
                cache=cache_task, 
                prediction=prediction))

        if sum_errors:
            return sum(loss)
        else:
            return loss

class MultitaskNeuralNet(NeuralNet):
    TopLayerClass = MultitaskTopLayer
    
################################################################################
### Logistic Regression Class
###

class LogisticRegression(NeuralNet):
    """ A logistic regression model

    """
    
    def __init__(self, n_in, n_out, test_error_fct='class_error'):
        super(LogisticRegression, self).__init__(n_in, n_out, [], 
                                                 test_error_fct=test_error_fct)
                                                 

