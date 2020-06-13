from typing import Callable, Tuple, Any, Dict
import torch as t
import torch.nn as nn

from machin.utils.logging import default_logger as logger
from machin.utils.helper_classes import Switch
from .generator import NormalNoiseGen


class AdaptiveParamNoise(object):
    def __init__(self,
                 initial_stddev: float = 0.1,
                 desired_action_stddev: float = 0.1,
                 adoption_coefficient: float = 1.01):
        """
        Implements the adaptive parameter space method in
        `<<Parameter space noise for exploration>> \
<https://arxiv.org/pdf/1706.01905.pdf>`_.

        Hint:
            Let :math:`\\theta` be the standard deviation of noise,
            and :math:`\\alpha` be the adpotion coefficient, then:

            :math:`\\theta_{n+1} = \\left \\{ \
                \\begin{array}{ll} \
                    \\alpha \\theta_k \
                        & if\\ d(\\pi,\\tilde{\\pi})\\leq\\delta, \\\\ \
                    \\frac{1}{\\alpha} \\theta_k & otherwise, \
                \\end{array} \
            \\right. \\ `

            Noise is directly applied to network parameters.

        Args:
            initial_stddev: Initial noise standard deviation.
            desired_action_stddev: Desired standard deviation for
            adoption_coefficient: Adoption coefficient.
        """

        self.initial_stddev = initial_stddev
        self.desired_action_stddev = desired_action_stddev
        self.adoption_coefficient = adoption_coefficient

        self.current_stddev = initial_stddev

    def adapt(self, distance: float):
        """
        Update noise standard deviation according to distance.

        Args:
            distance: Current distance between the noisy action and clean
                action.
        """
        if distance > self.desired_action_stddev:
            # Decrease stddev.
            self.current_stddev /= self.adoption_coefficient
        else:
            # Increase stddev.
            self.current_stddev *= self.adoption_coefficient

    def get_dev(self) -> float:
        """
        Returns:
            Current noise standard deviation.
        """
        return self.current_stddev

    def __repr__(self):
        fmt = 'AdaptiveParamNoise(i_std={}, da_std={}, adpt_coeff={})'
        return fmt.format(self.initial_stddev,
                          self.desired_action_stddev,
                          self.adoption_coefficient)


def _gen_perturb_hook(module, perturb_switch, reset_switch, perturb_gen):
    org_params = {}
    noisy_params = {}

    def perturb_pre_hook(*_):
        with t.no_grad():
            if perturb_switch.get():
                if noisy_params and not reset_switch.get():
                    # Use generated noisy parameters.
                    for param_name, param_value in module.named_parameters():
                        if t.is_tensor(param_value):
                            param_value.set_(noisy_params[param_name])
                else:
                    # Generate noisy parameters if they don't exist.
                    org_params.clear()
                    noisy_params.clear()
                    for param_name, param_value in module.named_parameters():
                        if t.is_tensor(param_value):
                            org_params[param_name] = param_value.clone()
                            param_value += perturb_gen(
                                param_value.shape, param_value.device
                            ).detach()
                            noisy_params[param_name] = param_value.clone()

            elif not perturb_switch.get():
                # Use original parameters
                if org_params:
                    for param_name, param_value in module.named_parameters():
                        if t.is_tensor(param_value):
                            param_value.set_(org_params[param_name])

    def perturb_post_hook(*_):
        # Called before backward update, swap noisy parameters out,
        # so gradients are applied to original parameters.
        with t.no_grad():
            if org_params:
                for param_name, param_value in module.named_parameters():
                    if t.is_tensor(param_value):
                        param_value.set_(org_params[param_name])

    return perturb_pre_hook, perturb_post_hook


# noinspection PyTypeChecker
def perturb_model(model: nn.Module,
                  perturb_switch: Switch,
                  reset_switch: Switch,
                  distance_func: Callable =
                  lambda x, y: t.dist(x, y, 2).mean().item(),
                  desired_action_stddev: float = 0.5,
                  noise_generator: Any = NormalNoiseGen,
                  noise_generator_args: Tuple = (),
                  noise_generator_kwargs: Dict = None,
                  noise_generate_function: Callable = None):
    """
    Give model's parameters a little perturbation. Implements
    `<<Parameter Space Noise for Exploration>> <https://arxiv.org/abs/1706.
01905>`_

    Note:
        Only parameters of type ``t.Tensor`` and gettable from
        ``model.named_parameters()`` will be perturbed.

        Original parameters will be automatically swapped in during the
        backward pass, and you can safely call optimizers afterwards.

    Warning:
        You must call the returned reset function after backward
        and before optimizing to ensure that original parameters
        are swapped in. Since pytorch currently does not guarantee
        the correctness of ``register_backward_hook``.

    Hint:
        1. ``noise_generator`` must accept (shape, \*args) in its ``__init__``
        function, where shape is the required shape. it also needs to have
        ``__call__(device=None)`` which produce a noise tensor on the specified
        device when invoked.

        2. ``noise_generate_function`` must accept (shape, device, std:float)
        and return a noise tensor on the specified device.

    Example:
        In order to use this function to perturb your model, you need to::

            from machin.utils.helper_classes import Switch
            from machin.frame.noise.param_space_noise import perturb_model
            from machin.utils.visualize import visualize_graph
            import torch as t

            dims = 5

            t.manual_seed(0)
            model = t.nn.Linear(dims, dims)
            optim = t.optim.Adam(model.parameters(), 1e-3)
            p_switch, r_switch = Switch(), Switch()
            rst_func, dreg_func = perturb_model(model, p_switch, r_switch)

            # you should keep this switch on if you do one training step after
            # every sampling step. otherwise you may turn it off in one episode
            # and turn it on in the next to speed up training.
            r_switch.on()

            # turn off/on the perturbation switch to see the difference
            p_switch.on()

            # do some sampling
            action = model(t.ones([dims]))

            # in order to let parameter noise adapt to generate noisy actions
            # within ``desired_action_stddev``, you must periodically
            # use the original model to generate some actions:
            p_switch.off()
            action = model(t.ones([dims]))

            # visualize will not show any leaf noise tensors
            # because they are created in t.no_grad() context
            # and added in-place.
            visualize_graph(action, exit_after_vis=False)

            # do some training
            loss = (action - t.ones([dims])).sum()
            loss.backward()
            rst_func()
            optim.step()
            print(model.weight)

            # clear hooks
            dreg_func()

    Args:
        model: Neural network model.
        perturb_switch: The switch used to enable perturbation. If switch is
            set to ``False`` (off), then during the forward process, original
            parameters are used.
        reset_switch: The switch used to reset perturbation noise. If switch is
            set to ``True`` (on), and ``perturb_switch`` is also on, then during
            every forward process, a new set of noise is applied to each param.
            If only ``perturb_switch`` is on, then the same set of noisy
            parameters is used in the forward process and they **will not be
            updated**.
        distance_func: Distance function, accepts two tensors produced by
            ``model`` (one is noisy), return the distance as float. Used
            to compare the distance between actions generated by
            noisy parameters and original parameters.
        desired_action_stddev: Desired action standard deviation.
        noise_generator: Noise generator class.
        noise_generator_args: Additional args other than shape of the noise
            generator.
        noise_generator_kwargs: Additional kwargs other than shape of the noise
            generator.
        noise_generate_function: Noise generation function, mutually exclusive
            with ``noise_generator`` and ``noise_generator_args``.

    Returns:
        1. A reset function with no arguments, will swap in original paramters.
        2. A deregister function with no arguments, will deregister all hooks
            applied on your model.
    """
    tmp_action = {}
    reset_hooks = []
    hook_handles = []

    param_noise_spec = AdaptiveParamNoise(
        desired_action_stddev=desired_action_stddev
    )

    def param_noise_gen(shape, device):
        nonlocal noise_generator_args, noise_generator_kwargs
        if noise_generator_kwargs is None:
            noise_generator_kwargs = {}
        gen = noise_generator(shape, *noise_generator_args,
                              **noise_generator_kwargs)
        return gen(device) * param_noise_spec.get_dev()

    def param_noise_custom_gen_wrapper(shape, device):
        std_dev = param_noise_spec.get_dev()
        return noise_generate_function(shape, device, std_dev)

    if noise_generate_function is not None:
        param_noise_gen = param_noise_custom_gen_wrapper

    def perturb_adjust_hook(_model, _input, output):
        if perturb_switch.get():
            tmp_action["with_noise"] = output.clone()
        else:
            tmp_action["without_noise"] = output.clone()
        if "with_noise" in tmp_action and "without_noise" in tmp_action:
            # Compute distance between two actions generated by
            # noisy parameters and original parameters.
            with t.no_grad():
                dist = distance_func(tmp_action["with_noise"],
                                     tmp_action["without_noise"])
                tmp_action.clear()
                param_noise_spec.adapt(dist)
                logger.info("Current output distance: {}".format(dist))
                logger.info("Current param noise stddev: {}"
                            .format(param_noise_spec.get_dev()))

    # Boise generation happens in pre-forward and noise adjust happens
    # in post-forward
    hook_handles.append(model.register_forward_hook(perturb_adjust_hook))

    pre, post = _gen_perturb_hook(model, perturb_switch,
                                  reset_switch, param_noise_gen)
    reset_hooks.append(post)
    hook_handles.append(model.register_forward_pre_hook(pre))

    # currently register_backward_hook is not guaranteed to work correctly
    # hook_handles.append(model.register_backward_hook(post))

    def reset():
        for h in reset_hooks:
            h()

    def deregister():
        for hh in hook_handles:
            hh.remove()

    return reset, deregister
