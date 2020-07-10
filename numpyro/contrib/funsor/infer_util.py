# Copyright Contributors to the Pyro project.
# SPDX-License-Identifier: Apache-2.0

from contextlib import contextmanager

import funsor
import numpyro
from numpyro.contrib.funsor.enum_messenger import infer_config
from numpyro.contrib.funsor.enum_messenger import plate as enum_plate
from numpyro.contrib.funsor.enum_messenger import trace as packed_trace
from numpyro.distributions.util import is_identically_one
from numpyro.handlers import substitute

funsor.set_backend("jax")


@contextmanager
def plate_to_enum_plate():
    """
    A context manager to replace `numpyro.plate` statement by a funsor-based
    :class:`~numpyro.contrib.funsor.enum_messenger.plate`.

    This is useful when doing inference for the usual NumPyro programs with
    `numpyro.plate` statements. For example, to get trace of a `model` whose discrete
    latent sites are enumerated, we can use

        enum_model = numpyro.contrib.funsor.enum(model)
        with plate_to_enum_plate():
            model_trace = numpyro.contrib.funsor.trace(enum_model).get_trace(
                *model_args, **model_kwargs)

    """
    try:
        numpyro.plate.__new__ = lambda cls, *args, **kwargs: enum_plate(*args, **kwargs)
        yield
    finally:
        numpyro.plate.__new__ = lambda *args, **kwargs: object.__new__(numpyro.plate)


def config_enumerate(fn, default='parallel'):
    """
    Configures enumeration for all relevant sites in a NumPyro model.

    When configuring for exhaustive enumeration of discrete variables, this
    configures all sample sites whose distribution satisfies
    ``.has_enumerate_support == True``.

    This can be used as either a function::

        model = config_enumerate(model)

    or as a decorator::

        @config_enumerate
        def model(*args, **kwargs):
            ...

    .. note:: Currently, only ``default='parallel'`` is supported.

    :param callable fn: Python callable with NumPyro primitives.
    :param str default: Which enumerate strategy to use, one of
        "sequential", "parallel", or None. Defaults to "parallel".
    """
    def config_fn(site):
        if site['type'] == 'sample' and (not site['is_observed']) \
                and site['fn'].has_enumerate_support:
            return {'enumerate': site['infer'].get('enumerate', default)}
        return {}

    return infer_config(fn, config_fn)


def log_density(model, model_args, model_kwargs, params):
    """
    Similar to :func:`numpyro.infer.util.log_density` but works for models
    with discrete latent variables. Internally, this uses :mod:`funsor`
    to marginalize discrete latent sites and evalutate the joint log probability.

    :param model: Python callable containing NumPyro primitives. Typically,
        the model has been enumerated by using
        :class:`~numpyro.contrib.funsor.enum_messenger.enum` handler::

            def model(*args, **kwargs):
                ...

            log_joint = log_density(enum(config_enumerate(model)), args, kwargs, params)

    :param tuple model_args: args provided to the model.
    :param dict model_kwargs: kwargs provided to the model.
    :param dict params: dictionary of current parameter values keyed by site
        name.
    :return: log of joint density and a corresponding model trace
    """
    model = substitute(model, data=params)
    with plate_to_enum_plate():
        model_trace = packed_trace(model).get_trace(*model_args, **model_kwargs)
    log_factors = []
    sum_vars, prod_vars = frozenset(), frozenset()
    for site in model_trace.values():
        if site['type'] == 'sample':
            value = site['value']
            intermediates = site['intermediates']
            scale = site['scale']
            if intermediates:
                log_prob = site['fn'].log_prob(value, intermediates)
            else:
                log_prob = site['fn'].log_prob(value)

            if (scale is not None) and (not is_identically_one(scale)):
                log_prob = scale * log_prob

            log_prob = funsor.to_funsor(log_prob, output=funsor.reals(), dim_to_name=site['infer']['dim_to_name'])
            log_factors.append(log_prob)
            sum_vars |= frozenset({site['name']})
            prod_vars |= frozenset(f.name for f in site['cond_indep_stack'] if f.dim is not None)

    with funsor.interpreter.interpretation(funsor.terms.lazy):
        lazy_result = funsor.sum_product.sum_product(
            funsor.ops.logaddexp, funsor.ops.add, log_factors,
            eliminate=sum_vars | prod_vars, plates=prod_vars)
    result = funsor.optimizer.apply_optimizer(lazy_result)
    return result.data, model_trace
