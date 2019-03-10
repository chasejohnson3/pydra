# -*- coding: utf-8 -*-
"""task.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1RRV1gHbGJs49qQB1q1d5tQEycVRtuhw6

## Notes:

### Environment specs
1. neurodocker json
2. singularity file+hash
3. docker hash
4. conda env
5. niceman config
6. environment variables

### Monitors/Audit
1. internal monitor
2. external monitor
3. callbacks

### Resuming
1. internal tracking
2. external tracking (DMTCP)

### Provenance
1. Local fragments
2. Remote server

### Isolation
1. Working directory
2. File (copy to local on write)
3. read only file system
"""


import abc
import cloudpickle as cp
import dataclasses as dc
from filelock import FileLock
import inspect
import json
import os
from pathlib import Path
import pickle as pk
import shutil
from tempfile import mkdtemp
import typing as ty

from .node import Node
from ..utils.messenger import (send_message, make_message, gen_uuid, now,
                               AuditFlag)
from .specs import (BaseSpec, Result, RuntimeSpec, File, SpecInfo,
                    ShellSpec, ShellOutSpec, ContainerSpec, DockerSpec,
                    SingularitySpec)
from .helpers import (make_klass, print_help, ensure_list, gather_runtime_info,
                      save_result, load_result)

develop = True


class BaseTask(Node):
    """This is a base class for Task objects.
    """

    _task_version: ty.Optional[str] = None  # Task writers encouraged to define and increment when implementation changes sufficiently

    audit_flags: AuditFlag = AuditFlag.NONE  # What to audit. See audit flags for details

    _can_resume = False  # Does the task allow resuming from previous state
    _redirect_x = False  # Whether an X session should be created/directed

    _runtime_requirements = RuntimeSpec()
    _runtime_hints = None

    _cache_dir = None  # Working directory in which to operate
    _references = None  # List of references for a task

    def __init__(self,
                 name, splitter=None, combiner=None,
                 other_splitters=None,
                 inputs: ty.Union[ty.Text, File, ty.Dict, None]=None,
                 audit_flags: AuditFlag=AuditFlag.NONE,
                 messengers=None, messenger_args=None, workingdir=None):
        """Initialize task with given args."""
        super(BaseTask, self).__init__(name, splitter=splitter,
                                       combiner=combiner,
                                       other_splitters=other_splitters,
                                       inputs=inputs, workingdir=workingdir)
        self.audit_flags = audit_flags
        self.messengers = ensure_list(messengers)
        self.messenger_args = messenger_args

    def audit(self, message, flags=None):
        if develop:
            with open(Path(os.path.dirname(__file__))
                      / '..' / 'schema/context.jsonld', 'rt') as fp:
                context = json.load(fp)
        else:
            context = {"@context": 'https://raw.githubusercontent.com/satra/pydra/enh/task/pydra/schema/context.jsonld'}
        if self.audit_flags & flags:
            if self.messenger_args:
                send_message(make_message(message, context=context),
                             messengers=self.messengers,
                             **self.messenger_args)
            else:
                send_message(make_message(message, context=context),
                             messengers=self.messengers)

    @property
    def can_resume(self):
        """Task can reuse partial results after interruption
        """
        return self._can_resume

    @abc.abstractmethod
    def _run_task(self):
        pass

    @property
    def cache_dir(self):
        return self._cache_dir

    @cache_dir.setter
    def cache_dir(self, location):
        self._cache_dir = Path(location)

    @property
    def output_dir(self):
        return self._cache_dir / self.checksum

    def audit_check(self, flag):
        return self.audit_flags & flag

    def __call__(self, cache_locations=None, **kwargs):
        return self.run(cache_locations=cache_locations, **kwargs)

    def run(self, cache_locations=None, cache_dir=None, **kwargs):
        self.inputs = dc.replace(self.inputs, **kwargs)
        if cache_dir is not None:
            self.cache_dir = Path(cache_dir)
        if self.cache_dir is None:
            self.cache_dir = mkdtemp()
        checksum = self.checksum
        lockfile = self.cache_dir / (checksum + '.lock')
        """
        Concurrent execution scenarios

        1. prior cache exists -> return result
        2. other process running -> wait
           a. finishes (with or without exception) -> return result
           b. gets killed -> restart
        3. no cache or other process -> start
        4. two or more concurrent new processes get to start
        """
        # TODO add signal handler for processes killed after lock acquisition
        with FileLock(lockfile):
            # Let only one equivalent process run
            #dj: for now not using cache
            # Eagerly retrieve cached
            # result = self.result(cache_locations=cache_locations)
            # if result is not None:
            #     return result
            odir = self.output_dir
            if not self.can_resume and odir.exists():
                shutil.rmtree(odir)
            cwd = os.getcwd()
            odir.mkdir(parents=False, exist_ok=True if self.can_resume else False)

            # start recording provenance, but don't send till directory is created
            # in case message directory is inside task output directory
            if self.audit_check(AuditFlag.PROV):
                aid = "uid:{}".format(gen_uuid())
                start_message = {"@id": aid, "@type": "task", "startedAtTime": now()}
            os.chdir(odir)
            if self.audit_check(AuditFlag.PROV):
                self.audit(start_message, AuditFlag.PROV)
                # audit inputs
            #check_runtime(self._runtime_requirements)
            #isolate inputs if files
            #cwd = os.getcwd()
            if self.audit_check(AuditFlag.RESOURCE):
                from ..utils.profiler import ResourceMonitor
                resource_monitor = ResourceMonitor(os.getpid(), logdir=odir)
            result = Result(output=None, runtime=None)
            try:
                if self.audit_check(AuditFlag.RESOURCE):
                    resource_monitor.start()
                    if self.audit_check(AuditFlag.PROV):
                        mid = "uid:{}".format(gen_uuid())
                        self.audit({"@id": mid, "@type": "monitor",
                                    "startedAtTime": now(),
                                    "wasStartedBy": aid}, AuditFlag.PROV)
                self._run_task()
                result.output = self._collect_outputs()
            except Exception as e:
                print(e)
                #record_error(self, e)
                raise
            finally:
                if self.audit_check(AuditFlag.RESOURCE):
                    resource_monitor.stop()
                    result.runtime = gather_runtime_info(resource_monitor.fname)
                    if self.audit_check(AuditFlag.PROV):
                        self.audit({"@id": mid, "endedAtTime": now(),
                                    "wasEndedBy": aid}, AuditFlag.PROV)
                        # audit resources/runtime information
                        eid = "uid:{}".format(gen_uuid())
                        entity = dc.asdict(result.runtime)
                        entity.update(**{"@id": eid, "@type": "runtime",
                                         "prov:wasGeneratedBy": aid})
                        self.audit(entity, AuditFlag.PROV)
                        self.audit({"@type": "prov:Generation",
                                    "entity_generated": eid,
                                    "hadActivity": mid}, AuditFlag.PROV)
                save_result(odir, result)
                with open(odir / '_node.pklz', 'wb') as fp:
                    cp.dump(self, fp)
                os.chdir(cwd)
                if self.audit_check(AuditFlag.PROV):
                    # audit outputs
                    self.audit({"@id": aid, "endedAtTime": now()}, AuditFlag.PROV)
            return result

    # TODO: Decide if the following two functions should be separated
    @abc.abstractmethod
    def _list_outputs(self):
        pass

    def _collect_outputs(self):
        run_output = ensure_list(self._list_outputs())
        output_klass = make_klass(self.output_spec)
        output = output_klass(**{f.name: None for f in
                                 dc.fields(output_klass)})
        return dc.replace(output, **dict(zip(self.output_names, run_output)))


class FunctionTask(BaseTask):

    def __init__(self, func: ty.Callable,
                 output_spec: ty.Optional[BaseSpec]=None,
                 name=None, splitter=None, combiner=None,
                 other_splitters=None,
                 audit_flags: AuditFlag=AuditFlag.NONE,
                 messengers=None, messenger_args=None,
                 workingdir=None, **kwargs):
        self.input_spec = SpecInfo(name='Inputs',
                                   fields=
            [(val.name, val.annotation, val.default)
                  if val.default is not inspect.Signature.empty
                  else (val.name, val.annotation)
             for val in inspect.signature(func).parameters.values()
             ] + [('_func', str, cp.dumps(func))],
                                   bases=(BaseSpec,))
        if name is None:
            name = func.__name__
        super(FunctionTask, self).__init__(name, splitter=splitter,
                                           combiner=combiner,
                                           other_splitters=other_splitters,
                                           inputs=kwargs,
                                           audit_flags=audit_flags,
                                           messengers=messengers,
                                           messenger_args=messenger_args,
                                           workingdir=workingdir)
        if output_spec is None:
            if 'return' not in func.__annotations__:
                output_spec = SpecInfo(name='Output',
                                       fields=[('out', ty.Any)],
                                       bases=(BaseSpec,))
            else:
                return_info = func.__annotations__['return']
                if hasattr(return_info, '__name__'):
                    output_spec = SpecInfo(name=return_info.__name__,
                                        fields=list(return_info.__annotations__.items()),
                                        bases=(BaseSpec,))
                # Objects like int, float, list, tuple, and dict do not have __name__ attribute.
                else:
                    if hasattr(return_info, '__annotations__'):
                        output_spec = SpecInfo(name='Output',
                                            fields=list(return_info.__annotations__.items()),
                                            bases=(BaseSpec,))
                    else:
                        output_spec = SpecInfo(name='Output',
                                            fields=[('out{}'.format(n+1), t) for n, t in enumerate(return_info)],
                                            bases=(BaseSpec,))
        elif 'return' in func.__annotations__:
            raise NotImplementedError('Branch not implemented')
        self.output_spec = output_spec
        self.set_output_keys()

    def _run_task(self):
        inputs = dc.asdict(self.inputs)
        del inputs['_func']
        self.output_ = None
        output = cp.loads(self.inputs._func)(**inputs)
        if not isinstance(output, tuple):
            output = (output,)
        self.output_ = list(output)

    def _list_outputs(self):
        return self.output_


def to_task(func_to_decorate):
    def create_func(**original_kwargs):
        function_task = FunctionTask(func=func_to_decorate,
                                     **original_kwargs)
        return function_task
    return create_func


class ShellCommandTask(BaseTask):
    def __init__(self, name, input_spec: ty.Optional[SpecInfo]=None,
                 output_spec: ty.Optional[SpecInfo]=None,
                 audit_flags: AuditFlag=AuditFlag.NONE,
                 messengers=None, messenger_args=None, **kwargs):
        if input_spec is None:
            field = dc.field(default_factory=list)
            field.metadata = {}
            fields = [('args', ty.List[str], field)]
            input_spec = SpecInfo(name='Inputs', fields=fields,
                                  bases=(ShellSpec,))
        self.input_spec = input_spec
        super(ShellCommandTask, self).__init__(name=name,
                                               inputs=kwargs,
                                               audit_flags=audit_flags,
                                               messengers=messengers,
                                               messenger_args=messenger_args)
        if output_spec is None:
            output_spec = SpecInfo(name='Output',
                                   fields=[],
                                   bases=(ShellOutSpec,))
        self.output_spec = output_spec

    @property
    def command_args(self):
        args = []
        for f in dc.fields(self.inputs):
            if f.name not in ['executable', 'args']:
                continue
            value = getattr(self.inputs, f.name)
            if value is not None:
                args.extend(ensure_list(value))
        return args

    @command_args.setter
    def command_args(self, args: ty.Dict):
        self.inputs = dc.replace(self.inputs, **args)

    @property
    def cmdline(self):
        return ' '.join(self.command_args)

    def _run_task(self):
        self.output_ = None
        args = self.command_args
        if args:
            self.output_ = execute(args)

    def _list_outputs(self):
        return list(self.output_)


class ContainerTask(ShellCommandTask):

    def __init__(self, name, input_spec: ty.Optional[ContainerSpec]=None,
                 output_spec: ty.Optional[ShellOutSpec]=None,
                 audit_flags: AuditFlag=AuditFlag.NONE,
                 messengers=None, messenger_args=None, **kwargs):

        if input_spec is None:
            field = dc.field(default_factory=list)
            field.metadata = {}
            fields = [('args', ty.List[str], field)]
            input_spec = SpecInfo(name='Inputs', fields=fields,
                                  bases=(ContainerSpec,))
        super(ContainerTask, self).__init__(name=name,
                                            input_spec=input_spec,
                                            audit_flags=audit_flags,
                                            messengers=messengers,
                                            messenger_args=messenger_args,
                                            **kwargs)

    @property
    def cmdline(self):
        return ' '.join(self.container_args + self.command_args)

    @property
    def container_args(self):
        if self.inputs.container is None:
            raise AttributeError('Container software is not specified')
        cargs = [self.inputs.container, 'run']
        if self.inputs.container_xargs is not None:
            cargs.extend(self.inputs.container_xargs)
        if self.inputs.image is None:
            raise AttributeError('Container image is not specified')
        cargs.append(self.inputs.image)
        return cargs

    def binds(self, opt):
        """Specify mounts to bind from local filesystems to container

        `bindings` are tuples of (local path, container path, bind mode)
        """
        bargs = []
        for binding in self.inputs.bindings:
            lpath, cpath, mode = binding
            if mode is None:
                mode = 'rw'  # default
            bargs.extend([opt, '{0}:{1}:{2}'.format(lpath, cpath, mode)])
        return bargs

    def _run_task(self):
        self.output_ = None
        args = self.container_args + self.command_args
        if args:
            self.output_ = execute(args)


class DockerTask(ContainerTask):
    def __init__(self, name, input_spec: ty.Optional[ContainerSpec]=None,
                 output_spec: ty.Optional[ShellOutSpec]=None,
                 audit_flags: AuditFlag=AuditFlag.NONE,
                 messengers=None, messenger_args=None, **kwargs):
        if input_spec is None:
            field = dc.field(default_factory=list)
            field.metadata = {}
            fields = [('args', ty.List[str], field)]
            input_spec = SpecInfo(name='Inputs', fields=fields,
                                  bases=(DockerSpec,))
        super(ContainerTask, self).__init__(name=name,
                                            input_spec=input_spec,
                                            audit_flags=audit_flags,
                                            messengers=messengers,
                                            messenger_args=messenger_args,
                                            **kwargs)

    @property
    def container_args(self):
        cargs = super().container_args
        assert self.inputs.container == 'docker'
        if self.inputs.bindings:
            # insert bindings before image
            idx = len(cargs) - 1
            cargs[idx:-1] = self.binds('-v')
        return cargs


class SingularityTask(ContainerTask):
    def __init__(self, input_spec: ty.Optional[ContainerSpec]=None,
                 output_spec: ty.Optional[ShellOutSpec]=None,
                 audit_flags: AuditFlag=AuditFlag.NONE,
                 messengers=None, messenger_args=None, **kwargs):
        if input_spec is None:
            field = dc.field(default_factory=list)
            field.metadata = {}
            fields = [('args', ty.List[str], field)]
            input_spec = SpecInfo(name='Inputs', fields=fields,
                                  bases=(SingularitySpec,))
        super(ContainerTask, self).__init__(input_spec=input_spec,
                                            audit_flags=audit_flags,
                                            messengers=messengers,
                                            messenger_args=messenger_args,
                                            **kwargs)

    @property
    def container_args(self):
        cargs = super().container_args
        assert self.inputs.container == 'singularity'
        if self.inputs.bindings:
            # insert bindings before image
            idx = len(cargs) - 1
            cargs[idx:-1] = self.binds('-B')
        return cargs
