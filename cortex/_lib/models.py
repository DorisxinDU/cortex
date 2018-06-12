'''Builds arch

'''

__author__ = 'R Devon Hjelm'
__author_email__ = 'erroneus@gmail.com'

import importlib
import logging
import os
import sys
import time

import torch

from . import data, exp, optimizer
from .parsing import parse_docstring, parse_header, parse_kwargs
from .handlers import AliasHandler, Handler, NetworkHandler, LossHandler, ResultsHandler, CallSetterHandler
from .utils import bad_values, update_dict_of_lists
from .viz import VizHandler


logger = logging.getLogger('cortex.models')
MODEL = None

_ROUTINE_PLUGINS = {}
_BUILD_PLUGINS = {}
MODEL_PLUGINS = {}


def check_plugin(plugin, plugin_type_str, D):
    if plugin.plugin_name is None:
        ValueError('Set `plugin_name` static member for plugin.')
    if plugin.plugin_name in D:
        raise KeyError(
            'plugin_name `{}` already registered as a {} plugin in cortex. '
            'Try using another one.'.format(
                plugin_type_str, plugin.plugin_name))

    for k in plugin._protected:
        if hasattr(plugin, k):
            raise AttributeError('{} is a protected attribute.'.format(k))

    for k in plugin._required:
        v = getattr(plugin, k, None)
        if v is None:
            raise AttributeError(
                'Plugin must have {} attribute set.'.format(k))
        else:
            setattr(plugin, k, v)


def register_build(plugin):
    global _BUILD_PLUGINS
    check_plugin(plugin, 'build', _BUILD_PLUGINS)

    plugin.plugin_help = parse_docstring(plugin.build)
    plugin.plugin_kwargs = parse_kwargs(plugin.build)

    _BUILD_PLUGINS[plugin.plugin_name] = plugin


def register_routine(plugin):
    global _ROUTINE_PLUGINS
    check_plugin(plugin, 'routine', _ROUTINE_PLUGINS)

    plugin.plugin_help = parse_docstring(plugin.run)
    plugin.plugin_kwargs = parse_kwargs(plugin.run)

    _ROUTINE_PLUGINS[plugin.plugin_name] = plugin


def register_model(plugin):
    plugin._set_kwargs()
    global MODEL_PLUGINS
    check_plugin(plugin, 'model', MODEL_PLUGINS)

    plugin.plugin_help, plugin.plugin_description = parse_header(plugin)

    MODEL_PLUGINS[plugin.plugin_name] = plugin


class BuildPluginBase():
    def __init__(self, **aliases):
        self._aliases = aliases
        self._data = data.DATA_HANDLER
        self._kwargs = None
        self._help = None
        self._nets = None

    @property
    def kwargs(self):
        return self._kwargs

    def __call__(self):
        if not hasattr(self, 'build'):
            raise ValueError(
                'Build {} does not have `build` method set'.format(
                    self.name))
        self.build(**self.kwargs)


class RoutinePluginBase():
    _training_models = []

    plugin_name = None
    plugin_nets = []
    plugin_vars = []
    plugin_optional_inputs = []

    def __init__(self, name=None, **aliases):
        self._aliases = aliases
        self._kwargs = None
        self._help = None
        self._nets = None

        self._results = ResultsHandler()
        self._losses = LossHandler(self.nets)
        self._training_nets = []
        self.name = name or self.plugin_name


    @property
    def results(self):
        return self._results

    @property
    def losses(self):
        return self._losses

    @property
    def nets(self):
        return self._nets

    @property
    def vars(self):
        return self._vars

    @property
    def kwargs(self):
        return self._kwargs

    def perform(self):
        if not hasattr(self, 'run'):
            raise ValueError(
                'Routine {} does not have `run` method set'.format(
                    self.name))
        self.run(**self.kwargs)

    def __call__(self):
        # Run routine
        if exp.DEVICE == torch.device('cpu'):
            return self.perform()
        else:
            with torch.cuda.device(exp.DEVICE.index):
                return self.perform()

    def reset(self):
        self.results.clear()
        self.losses.clear()
        self.inputs.clear()

    def set_viz(self, viz):
        self._viz = viz


class ModelPluginBase():
    def __init__(self):

        self._nets = NetworkHandler()
        self._vars = Handler()
        self._kwargs = Handler()
        self._help = Handler()

        self._builds = CallSetterHandler(self._add_build)
        self._routines = CallSetterHandler(self._add_routine)
        self._defaults = dict(
            data=self.data_defaults,
            optimizer=self.optimizer_defaults,
            train=self.train_defaults)
        self._train_procedures = []
        self._eval_procedures = []

        self._setup = None

        self._results = ResultsHandler(time=dict(), losses=dict())
        self._losses = LossHandler(self._nets)
        self._data = data.DATA_HANDLER

    @property
    def defaults(self):
        return self._defaults

    @property
    def kwargs(self):
        return self._kwargs

    @property
    def help(self):
        return self._help

    @property
    def setup(self):
        return self._setup

    @property
    def train_procedures(self):
        return self._train_procedures

    @property
    def eval_procedures(self):
        return self._eval_procedures

    @property
    def results(self):
        return self._results

    def _add_routine(self, routine):
        routine._kwargs = AliasHandler(self.kwargs)
        routine._nets = AliasHandler(self._nets)
        routine._vars = AliasHandler(self._vars)
        routine._help = AliasHandler(self._help)

        for k, v in routine._aliases.items():
            if k in routine.plugin_nets:
                routine.nets.set_alias(k, v)
            elif k in routine.plugin_vars:
                routine.vars.set_alias(k, v)
            elif k in routine.plugin_kwargs:
                routine.kwargs.set_alias(k, v)
                routine.help.set_alias(k, v)
            else:
                raise KeyError(k, v)

    def _add_build(self, build):
        build._kwargs = AliasHandler(self.kwargs)
        build._nets = AliasHandler(self._nets)
        build._help = AliasHandler(self._help)

        for k, v in build._aliases.items():
            if k in build.plugin_nets:
                build.nets.set_alias(k, v)
            elif k in build.plugin_kwargs:
                build.kwargs.set_alias(k, v)
                build.help.set_alias(k, v)
            else:
                raise KeyError(k, v)

    def check(self):
        for key, routine in self._routines.items():
            if isinstance(routine, RoutinePluginBase):
                pass
            else:
                raise ValueError

        for key, build in self._builds.items():
            if isinstance(build, BuildPluginBase):
                pass
            else:
                raise ValueError

    def _set_kwargs(self):

        def add_kwargs(obj):
            for k, v in obj.plugin_kwargs.items():
                try:
                    obj.kwargs[k] = v
                except ValueError:
                    logger.warning('Multiple default values found for {}. '
                                   'This may have unintended effects. '
                                   'Using {}'.format(k, obj.kwargs[k]))

        for build in self._builds.values():
            add_kwargs(build)

        for routine in self._routines.values():
            add_kwargs(routine)

    def _set_help(self):

        def add_help(obj):
            for k, v in obj.plugin_help.items():
                try:
                    obj.help[k] = v
                except:
                    logger.warning('Multiple '
                                   'default values found'
                                   'for {} help.'
                                   'This may have'
                                   'unintended'
                                   'effects. Using {}'
                                   .format(k, obj.help[k]))

        for build in self.builds.values():
            add_help(build)

        for routine in self.routines.values():
            add_help(routine)


    def unpack_args(self):
        builds = Handler()
        routines = Handler()

        kwargs = self.get_kwargs()

        for key, build in self.builds.items():
            for k_, v in kwargs.items():
                if k_ in build.kwargs:
                    if key in builds:
                        builds[key][k_] = v
                    else:
                        builds[key] = {k_: v}

        for key, routine in self._routines.items():
            for k_, v in kwargs.items():
                if k_ in routine.kwargs:
                    if key in routines:
                        routines[key][k_] = v
                    else:
                        routines[key] = {k_: v}

        return Handler(builds=builds, routines=routines)

    def train(self, i, quit_on_bad_values=False):
        return self.run_procedure(
            i, quit_on_bad_values=quit_on_bad_values, train=True)

    def run_procedure(self, i, quit_on_bad_values=False, train=False):
        self._data.next()
        self.reset_routines()
        inputs = Handler()
        mode, procedure, updates = self._train_procedures[i]

        for k, v in self._data.batch.items():
            inputs['data.' + k] = v

        for key, update in zip(procedure, updates):
            if not train:
                update = 1
            for u in range(update):
                if u > 0:
                    self._data.next()

                routine = self._routines[key]
                kwargs = self._kwargs[key]
                routine.reset()

                # Set to `requires_grad` for models that are trained with this
                # routine.
                if train:
                    for k in routine.training_nets:
                        k_ = routine._names.get(k, k)
                        optimizer.OPTIMIZERS[k_].zero_grad()
                        net = routine.nets[k]
                        for p in net.parameters():
                            p.requires_grad = True

                # Required inputs
                receives = routine.plugin_inputs
                sends = [routine._names.get(k, k) for k in receives]
                for send, receive in zip(sends, receives):
                    try:
                        if isinstance(send, (list, tuple)):
                            send_ = [inputs[s] for s in send]
                            routine.inputs[receive] = send_
                        else:
                            routine.inputs[receive] = inputs[send]
                    except KeyError:
                        raise KeyError(
                            '{} not found in inputs. Available: {}'.format(
                                send, tuple(
                                    inputs.keys())))

                # Optional inputs
                receives = routine.plugin_optional_inputs
                sends = [routine._names.get(k, k) for k in receives]
                for send, receive in zip(sends, receives):
                    try:
                        if isinstance(send, (list, tuple)):
                            send_ = [inputs[s] for s in send]
                            routine.inputs[receive] = send_
                        else:
                            routine.inputs[receive] = inputs[send]
                    except BaseException:
                        routine.inputs[receive] = None

                start_time = time.time()
                outputs = routine(**kwargs)

                # Backprop the losses.
                if train:
                    for k, loss in routine.losses.items():
                        if loss is not None:
                            loss.backward()
                            k_ = routine._names.get(k, k)
                            optimizer.OPTIMIZERS[k_].step()

                end_time = time.time()

                # Populate the inputs with the outputs
                if u == update - 1:
                    for k, v in outputs.items():
                        k_ = key + '.' + k
                        if k_ in inputs:
                            raise KeyError('{} already in'
                                           ' inputs. Use a '
                                           'different name.'.format(k_))
                        inputs[k_] = v.detach()

                # Add losses to the results.
                for loss_key in routine.losses.keys():
                    if loss_key not in routine.training_nets:
                        routine.training_nets.append(loss_key)

                # Check for bad numbers
                bads = bad_values(routine.results)
                if bads and quit_on_bad_values:
                    print(
                        'Bad values found (quitting): {} \n All:{}'.format(
                            bads, routine.results))
                    exit(0)

            routine_losses = dict((k, v.item())
                                  for k, v in routine.losses.items())

            # Update results
            update_dict_of_lists(self._results, **routine.results)
            update_dict_of_lists(self._results['losses'], **routine_losses)
            update_dict_of_lists(
                self._results['time'], **{key: end_time - start_time})

    def reset_routines(self):
        self._losses.clear()
        for routine in self._routines.values():
            routine.reset()

    def reset(self):
        self.reset_routines()
        self._results.clear()
        self._results.update(losses=dict(), time=dict())

    def set_train(self):
        for net in self._nets.values():
            net.train()

    def set_eval(self):
        for net in self._nets.values():
            net.eval()


def setup_model(model_key):
    global MODEL
    logger.info('Using model `{}`'.format(model_key))
    MODEL = MODEL_PLUGINS[model_key]
    return MODEL


_arch_keys_optional = dict(
    TEST_ROUTINES='test_routines',
    FINISH_TRAIN_ROUTINES='finish_train_routines',
    FINISH_TEST_ROUTINES='finish_test_routines',
    SETUP='setup',
    Dataset='Dataset',
    DataLoader='DataLoader',
    transform='transform'
)

_ignore = ['__init__.py', '__pycache__']


def import_directory(p, name):
    '''
    Adds custom directories to the framwework
    '''

    global ARCHS

    if p.endswith('/'):
        p = p[:-1]

    logger.info('Adding {} to `sys.path`.'.format(p))
    sys.path.append(p)

    for fn in os.listdir(p):
        if fn.endswith('.py') and fn not in _ignore:
            fnp = fn[:-3]
            importlib.import_module(fnp)
            try:
                importlib.import_module(fnp)
            except Exception as e:
                logger.warning(
                    'Import of architecture (module) {} failed ({})'.format(
                        fnp, e))

        '''
        elif os.path.isdir(fn):
            if fn.endswith('/'):
                fn = fn[:-1]
            if fn not in _ignore:
                add_directory(fn, name + '.' + os.path.basename(fn))
        '''


def find_models(model_paths):
    for k, p in model_paths.items():
        import_directory(p, k)

    global MODEL_PLUGINS
    keys = list(MODEL_PLUGINS.keys())
    for k in keys:
        v = MODEL_PLUGINS[k]
        v.check()
        try:
            v.check()
        except Exception as e:
            logger.warning('`{}` checks failed ({}).'.format(k, e))
            MODEL_PLUGINS.pop(k)


def build_networks(**build_args):
    '''Builds the generator and discriminator.

    If architecture module contains a `build_model` function, use that,
    otherwise, use the one found in this module.

    '''
    for build_key, build in MODEL.builds.items():
        args = build_args[build_key]
        logger.debug('{} build args: {}'.format(build_key, args))
        build(**args)

    MODEL.setup_routine_nets()


def reload_models(**reload_models):
    global MODEL_HANDLER
    if MODEL_HANDLER is None:
        raise RuntimeError(
            'MODEL_HANDLER not set. `reload_models` should only be used after '
            '`models.setup_models` has been called.')
    for k, v in reload_models.items():
        logger.info('Reloading model {}'.format(k))
        logger.debug(v)
        MODEL_HANDLER[k] = v
