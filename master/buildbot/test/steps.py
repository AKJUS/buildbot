# This file is part of Buildbot.  Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright Buildbot Team Members

from __future__ import annotations

import stat
import tarfile
from io import BytesIO
from pathlib import PurePosixPath
from pathlib import PureWindowsPath
from typing import TYPE_CHECKING
from unittest import mock

from twisted.internet import defer
from twisted.python import log
from twisted.python.reflect import namedModule

from buildbot.process import buildstep
from buildbot.process.results import EXCEPTION
from buildbot.process.results import statusToString
from buildbot.test import fakedb
from buildbot.test.fake import connection
from buildbot.test.fake import fakebuild
from buildbot.test.fake import fakemaster
from buildbot.test.fake import logfile
from buildbot.test.fake import worker
from buildbot.util import bytes2unicode
from buildbot.util import runprocess
from buildbot.util import unicode2bytes
from buildbot.util.eventual import flushEventualQueue
from buildbot.warnings import warn_deprecated

if TYPE_CHECKING:
    from twisted.trial import unittest

    _TestBuildStepMixinBase = unittest.TestCase
else:
    _TestBuildStepMixinBase = object


def _dict_diff(d1, d2):
    """
    Given two dictionaries describe their difference
    For nested dictionaries, key-paths are concatenated with the '.' operator

    @return The list of keys missing in d1, the list of keys missing in d2, and the differences
    in any nested keys
    """
    d1_keys = set(d1.keys())
    d2_keys = set(d2.keys())
    both = d1_keys & d2_keys

    missing_in_d1 = {}
    missing_in_d2 = {}
    different = []

    for k in both:
        if isinstance(d1[k], dict) and isinstance(d2[k], dict):
            missing_in_v1, missing_in_v2, different_in_v = _dict_diff(d1[k], d2[k])

            for sub_key in missing_in_v1:
                missing_in_d1[f'{k}.{sub_key}'] = d2[k][sub_key]
            for sub_key in missing_in_v2:
                missing_in_d2[f'{k}.{sub_key}'] = d1[k][sub_key]

            for child_k, left, right in different_in_v:
                different.append((f'{k}.{child_k}', left, right))
            continue
        if d1[k] != d2[k]:
            different.append((k, d1[k], d2[k]))

    for k in d2_keys - both:
        missing_in_d1[k] = d2[k]
    for k in d1_keys - both:
        missing_in_d2[k] = d1[k]
    return missing_in_d1, missing_in_d2, different


def _describe_cmd_difference(exp_command, exp_args, got_command, got_args):
    if exp_command != got_command:
        return f'Expected command type {exp_command} got {got_command}. Expected args {exp_args!r}'
    if exp_args == got_args:
        return ""
    text = ""
    missing_in_exp, missing_in_cmd, diff = _dict_diff(exp_args, got_args)
    if missing_in_exp:
        text += f'Keys in command missing from expectation: {missing_in_exp!r}\n'
    if missing_in_cmd:
        text += f'Keys in expectation missing from command: {missing_in_cmd!r}\n'
    if diff:
        formatted_diff = [f'"{d[0]}":\nexpected: {d[1]!r}\ngot:      {d[2]!r}\n' for d in diff]
        text += 'Key differences between expectation and command: {}\n'.format(
            '\n'.join(formatted_diff)
        )

    return text


class ExpectRemoteRef:
    """
    Define an expected RemoteReference in the args to an L{Expect} class
    """

    def __init__(self, rrclass):
        self.rrclass = rrclass

    def __eq__(self, other):
        return isinstance(other, self.rrclass)


class Expect:
    """
    Define an expected L{RemoteCommand}, with the same arguments

    Extra behaviors of the remote command can be added to the instance, using
    class methods.  Use L{Expect.log} to add a logfile, L{Expect.update} to add
    an arbitrary update, or add an integer to specify the return code (rc), or
    add a Failure instance to raise an exception. Additionally, use
    L{Expect.behavior}, passing a callable that will be invoked with the real
    command and can do what it likes:

        def custom_behavior(command):
            ...
        Expect('somecommand', { args='foo' })
            + Expect.behavior(custom_behavior),
        ...

        Expect('somecommand', { args='foo' })
            + Expect.log('stdio', stdout='foo!')
            + Expect.log('config.log', stdout='some info')
            + Expect.update('status', 'running').add(0), # (specifies the rc)
        ...

    """

    def __init__(self, remote_command, args, interrupted=False):
        """
        Expect a command named C{remote_command}, with args C{args}.
        """
        self.remote_command = remote_command
        self.args = args
        self.result = None
        self.interrupted = interrupted
        self.connection_broken = False
        self.behaviors = []

    def behavior(self, callable):
        self.behaviors.append(('callable', callable))
        return self

    def error(self, error):
        self.behaviors.append(('err', error))
        return self

    def log(self, name, **streams):
        self.behaviors.append(('log', name, streams))
        return self

    def update(self, name, value):
        self.behaviors.append(('update', name, value))
        return self

    def stdout(self, output):
        self.behaviors.append(('log', 'stdio', {'stdout': output}))
        return self

    def stderr(self, output):
        self.behaviors.append(('log', 'stdio', {'stderr': output}))
        return self

    def exit(self, code):
        self.behaviors.append(('rc', code))
        return self

    def break_connection(self):
        self.connection_broken = True
        return self

    @defer.inlineCallbacks
    def runBehavior(self, behavior, args, command):
        """
        Implement the given behavior.  Returns a Deferred.
        """
        if behavior == 'rc':
            yield command.remoteUpdate('rc', args[0], False)
        elif behavior == 'err':
            raise args[0]
        elif behavior == 'update':
            yield command.remoteUpdate(args[0], args[1], False)
        elif behavior == 'log':
            name, streams = args
            for stream in streams:
                if stream not in ['header', 'stdout', 'stderr']:
                    raise RuntimeError(f'Log stream {stream} is not recognized')

            if name == command.stdioLogName:
                if 'header' in streams:
                    yield command.remote_update([({"header": streams['header']}, 0)])
                if 'stdout' in streams:
                    yield command.remote_update([({"stdout": streams['stdout']}, 0)])
                if 'stderr' in streams:
                    yield command.remote_update([({"stderr": streams['stderr']}, 0)])
            else:
                if 'header' in streams or 'stderr' in streams:
                    raise RuntimeError('Non stdio streams only support stdout')
                yield command.addToLog(name, streams['stdout'])
                if name not in command.logs:
                    raise RuntimeError(f"{command}.addToLog: no such log {name}")

        elif behavior == 'callable':
            yield args[0](command)
        else:
            raise AssertionError(f'invalid behavior {behavior}')
        return None

    @defer.inlineCallbacks
    def runBehaviors(self, command):
        """
        Run all expected behaviors for this command
        """
        for behavior in self.behaviors:
            yield self.runBehavior(behavior[0], behavior[1:], command)

    def _cleanup_args(self, args):
        # we temporarily disable checking of sigtermTime and interruptSignal due to currently
        # ongoing changes to how step testing works. Once all tests are updated for stricter
        # checking this will be removed.
        args = args.copy()
        args.pop('sigtermTime', None)
        args.pop('interruptSignal', None)
        args.pop('usePTY', None)
        env = args.pop('env', None)
        if env is None:
            env = {}
        args['env'] = env
        return args

    def _check(self, case, command):
        case.assertEqual(self.interrupted, command.interrupted)

        if command.remote_command == self.remote_command and self._cleanup_args(
            command.args
        ) == self._cleanup_args(self.args):
            return

        cmd_dif = _describe_cmd_difference(
            self.remote_command, self.args, command.remote_command, command.args
        )
        msg = (
            "Command contents different from expected (command index: "
            f"{case._expected_commands_popped}):\n{cmd_dif}"
        )
        raise AssertionError(msg)

    def __repr__(self):
        return "Expect(" + repr(self.remote_command) + ")"


class ExpectShell(Expect):
    """
    Define an expected L{RemoteShellCommand}, with the same arguments Any
    non-default arguments must be specified explicitly (e.g., usePTY).
    """

    class NotSet:
        pass

    def __init__(
        self,
        workdir,
        command,
        env=NotSet,
        want_stdout=1,
        want_stderr=1,
        initial_stdin=None,
        timeout=20 * 60,
        max_time=None,
        max_lines=None,
        sigterm_time=None,
        logfiles=None,
        use_pty=False,
        log_environ=True,
        interrupt_signal='KILL',
    ):
        if env is self.NotSet:
            env = {}
        if logfiles is None:
            logfiles = {}
        args = {
            'workdir': workdir,
            'command': command,
            'env': env,
            'want_stdout': want_stdout,
            'want_stderr': want_stderr,
            'initial_stdin': initial_stdin,
            'timeout': timeout,
            'maxTime': max_time,
            'max_lines': max_lines,
            'logfiles': logfiles,
            'usePTY': use_pty,
            'logEnviron': log_environ,
        }

        if sigterm_time is not self.NotSet:
            args['sigtermTime'] = sigterm_time
        if interrupt_signal is not None:
            args['interruptSignal'] = interrupt_signal
        super().__init__("shell", args)

    def __repr__(self):
        return "ExpectShell(" + repr(self.remote_command) + repr(self.args['command']) + ")"


class ExpectStat(Expect):
    def __init__(self, file, workdir=None, log_environ=None):
        args = {'file': file}
        if workdir is not None:
            args['workdir'] = workdir
        if log_environ is not None:
            args['logEnviron'] = log_environ

        super().__init__('stat', args)

    def stat(
        self, mode, inode=99, dev=99, nlink=1, uid=0, gid=0, size=99, atime=0, mtime=0, ctime=0
    ):
        self.update('stat', [mode, inode, dev, nlink, uid, gid, size, atime, mtime, ctime])
        return self

    def stat_file(self, mode=0, size=99, atime=0, mtime=0, ctime=0):
        self.stat(stat.S_IFREG, size=size, atime=atime, mtime=mtime, ctime=ctime)
        return self

    def stat_dir(self, mode=0, size=99, atime=0, mtime=0, ctime=0):
        self.stat(stat.S_IFDIR, size=size, atime=atime, mtime=mtime, ctime=ctime)
        return self

    def __repr__(self):
        return "ExpectStat(" + repr(self.args['file']) + ")"


class ExpectUploadFile(Expect):
    def __init__(
        self,
        blocksize=None,
        maxsize=None,
        workersrc=None,
        workdir=None,
        writer=None,
        keepstamp=None,
        slavesrc=None,
        interrupted=False,
    ):
        args = {'workdir': workdir, 'writer': writer, 'blocksize': blocksize, 'maxsize': maxsize}
        if keepstamp is not None:
            args['keepstamp'] = keepstamp
        if slavesrc is not None:
            args['slavesrc'] = slavesrc
        if workersrc is not None:
            args['workersrc'] = workersrc

        super().__init__('uploadFile', args, interrupted=interrupted)

    def upload_string(self, string, timestamp=None, out_writers=None, error=None):
        def behavior(command):
            writer = command.args['writer']
            if out_writers is not None:
                out_writers.append(writer)

            writer.remote_write(string)
            writer.remote_close()
            if timestamp:
                writer.remote_utime(timestamp)

            if error is not None:
                writer.cancel = mock.Mock(wraps=writer.cancel)
                raise error

        self.behavior(behavior)
        return self

    def __repr__(self):
        return f"ExpectUploadFile({self.args['workdir']!r},{self.args['workersrc']!r})"


class ExpectUploadDirectory(Expect):
    def __init__(
        self,
        compress=None,
        blocksize=None,
        maxsize=None,
        workersrc=None,
        workdir=None,
        writer=None,
        keepstamp=None,
        slavesrc=None,
        interrupted=False,
    ):
        args = {
            'compress': compress,
            'workdir': workdir,
            'writer': writer,
            'blocksize': blocksize,
            'maxsize': maxsize,
        }
        if keepstamp is not None:
            args['keepstamp'] = keepstamp
        if slavesrc is not None:
            args['slavesrc'] = slavesrc
        if workersrc is not None:
            args['workersrc'] = workersrc

        super().__init__('uploadDirectory', args, interrupted=interrupted)

    def upload_tar_file(self, filename, members, error=None, out_writers=None):
        def behavior(command):
            f = BytesIO()
            archive = tarfile.TarFile(fileobj=f, name=filename, mode='w')
            for name, content in members.items():
                content = unicode2bytes(content)
                archive.addfile(tarfile.TarInfo(name), BytesIO(content))

            writer = command.args['writer']
            if out_writers is not None:
                out_writers.append(writer)

            writer.remote_write(f.getvalue())
            writer.remote_unpack()

            if error is not None:
                writer.cancel = mock.Mock(wraps=writer.cancel)
                raise error

        self.behavior(behavior)
        return self

    def __repr__(self):
        return f"ExpectUploadDirectory({self.args['workdir']!r}, {self.args['workersrc']!r})"


class ExpectDownloadFile(Expect):
    def __init__(
        self,
        blocksize=None,
        maxsize=None,
        workerdest=None,
        workdir=None,
        reader=None,
        mode=None,
        interrupted=False,
        slavesrc=None,
        slavedest=None,
    ):
        args = {
            'workdir': workdir,
            'reader': reader,
            'mode': mode,
            'blocksize': blocksize,
            'maxsize': maxsize,
        }
        if slavesrc is not None:
            args['slavesrc'] = slavesrc
        if slavedest is not None:
            args['slavedest'] = slavedest
        if workerdest is not None:
            args['workerdest'] = workerdest

        super().__init__('downloadFile', args, interrupted=interrupted)

    def download_string(self, dest_callable, size=1000, timestamp=None):
        def behavior(command):
            reader = command.args['reader']
            read = reader.remote_read(size)

            dest_callable(read)

            reader.remote_close()
            if timestamp:
                reader.remote_utime(timestamp)
            return read

        self.behavior(behavior)
        return self

    def __repr__(self):
        return f"ExpectUploadDirectory({self.args['workdir']!r}, {self.args['workerdest']!r})"


class ExpectMkdir(Expect):
    def __init__(self, dir=None, log_environ=None):
        args = {'dir': dir}
        if log_environ is not None:
            args['logEnviron'] = log_environ

        super().__init__('mkdir', args)

    def __repr__(self):
        return f"ExpectMkdir({self.args['dir']!r})"


class ExpectRmdir(Expect):
    def __init__(self, dir=None, log_environ=None, timeout=None, path=None):
        args = {'dir': dir}
        if log_environ is not None:
            args['logEnviron'] = log_environ
        if timeout is not None:
            args['timeout'] = timeout
        if path is not None:
            args['path'] = path

        super().__init__('rmdir', args)

    def __repr__(self):
        return f"ExpectRmdir({self.args['dir']!r})"


class ExpectCpdir(Expect):
    def __init__(self, fromdir=None, todir=None, log_environ=None, timeout=None, max_time=None):
        args = {'fromdir': fromdir, 'todir': todir}
        if log_environ is not None:
            args['logEnviron'] = log_environ
        if timeout is not None:
            args['timeout'] = timeout
        if max_time is not None:
            args['maxTime'] = max_time

        super().__init__('cpdir', args)

    def __repr__(self):
        return f"ExpectCpdir({self.args['fromdir']!r}, {self.args['todir']!r})"


class ExpectGlob(Expect):
    def __init__(self, path=None, log_environ=None):
        args = {'path': path}
        if log_environ is not None:
            args['logEnviron'] = log_environ

        super().__init__('glob', args)

    def files(self, files=None):
        if files is None:
            files = []
        self.update('files', files)
        return self

    def __repr__(self):
        return f"ExpectGlob({self.args['path']!r})"


class ExpectListdir(Expect):
    def __init__(self, dir=None):
        args = {'dir': dir}

        super().__init__('listdir', args)

    def files(self, files=None):
        if files is None:
            files = []
        self.update('files', files)
        return self

    def __repr__(self):
        return f"ExpectListdir({self.args['dir']!r})"


class ExpectRmfile(Expect):
    def __init__(self, path=None, log_environ=None):
        args = {'path': path}
        if log_environ is not None:
            args['logEnviron'] = log_environ

        super().__init__('rmfile', args)

    def __repr__(self):
        return f"ExpectRmfile({self.args['path']!r})"


def _check_env_is_expected(test, expected_env, env):
    if expected_env is None:
        return

    env = env or {}
    for var, value in expected_env.items():
        test.assertEqual(env.get(var), value, f'Expected environment to have {var} = {value!r}')


class ExpectMasterShell:
    _stdout = b""
    _stderr = b""
    _exit = 0
    _workdir = None
    _env = None

    def __init__(self, command):
        self._command = command

    def stdout(self, stdout):
        assert isinstance(stdout, bytes)
        self._stdout = stdout
        return self

    def stderr(self, stderr):
        assert isinstance(stderr, bytes)
        self._stderr = stderr
        return self

    def exit(self, exit):
        self._exit = exit
        return self

    def workdir(self, workdir):
        self._workdir = workdir
        return self

    def env(self, env):
        self._env = env
        return self

    def _check(self, test, command, workdir, env):
        test.assertDictEqual(
            {'command': command, 'workdir': workdir},
            {'command': self._command, 'workdir': self._workdir},
            "unexpected command run",
        )

        _check_env_is_expected(test, self._env, env)
        return (self._exit, self._stdout, self._stderr)

    def __repr__(self):
        return f"<ExpectMasterShell(command={self._command})>"


class FakeRunProcess:
    def __init__(self, start_retval, result_rc):
        self.start_retval = start_retval
        self.result_rc = result_rc
        self.result_signal = None

    def start(self):
        return defer.succeed(self.start_retval)

    def interrupt(self, signal_name):
        pass


class TestBuildStepMixin(_TestBuildStepMixinBase):
    """
    @ivar build: the fake build containing the step
    @ivar progress: mock progress object
    @ivar worker: mock worker object
    @ivar properties: build properties (L{Properties} instance)
    """

    @defer.inlineCallbacks
    def setup_test_build_step(
        self,
        want_data=True,
        want_db=False,
        want_mq=False,
        with_secrets: dict | None = None,
    ):
        if not hasattr(self, 'reactor'):
            raise RuntimeError('Reactor has not yet been setup for step')

        self._interrupt_remote_command_numbers: list[int] = []

        self._expected_commands: list[Expect] = []
        self._expected_commands_popped = 0

        self.master = yield fakemaster.make_master(
            self,
            wantData=want_data,
            wantDb=want_db,
            wantMq=want_mq,
            with_secrets=with_secrets,
        )

        yield self.master.db.insert_test_data([
            fakedb.Master(id=fakedb.FakeDBConnector.MASTER_ID),
            fakedb.Worker(id=400, name='linux'),
            fakedb.Builder(id=100),
            fakedb.Buildset(id=200),
            fakedb.BuildRequest(id=300, buildsetid=200, builderid=100),
            fakedb.Build(
                id=92,
                buildrequestid=300,
                number=7,
                masterid=fakedb.FakeDBConnector.MASTER_ID,
                builderid=100,
                workerid=400,
            ),
        ])

        self.patch(runprocess, "create_process", self._patched_create_process)
        self._master_run_process_expect_env: dict[str, str] = {}

        self._worker_version = None
        self._worker_env = None
        self._build_files = None

        self.worker = worker.FakeWorker(self.master)
        self.worker.attached(None)

        self._steps: list[buildstep.BuildStep] = []
        self.build = None

        # expectations

        self._exp_results: list[tuple[int, str | None]] = []
        self.exp_properties: dict[str, tuple[object, str | None]] = {}
        self.exp_missing_properties: list[str] = []
        self._exp_logfiles: dict[int, dict[str, bytes]] = {}
        self._exp_logfiles_stderr: dict[int, dict[str, bytes]] = {}
        self.exp_hidden = False
        self.exp_exception = None
        self._exp_test_result_sets: list[tuple[str, str, str]] = []
        self._exp_test_results: list[tuple[int, object, str, str, int, int]] = []
        self._exp_build_data: dict[str, tuple[object, str]] = {}
        self._exp_result_summaries: list[str] = []
        self._exp_build_result_summaries: list[str] = []

        self.addCleanup(flushEventualQueue)

    def tear_down_test_build_step(self):  # pragma: no cover
        warn_deprecated(
            '4.2.0',
            'tear_down_test_build_step() no longer needs to be called, '
            + 'test tear down is run automatically',
        )

    def _setup_fake_build(self, worker_version, worker_env, build_files):
        if worker_version is None:
            worker_version = {'*': '99.99'}

        if worker_env is None:
            worker_env = {}

        if build_files is None:
            build_files = []

        build = fakebuild.FakeBuild(master=self.master)
        build.allFiles = lambda: build_files
        build.master = self.master

        def getWorkerVersion(cmd, oldversion):
            if cmd in worker_version:
                return worker_version[cmd]
            if '*' in worker_version:
                return worker_version['*']
            return oldversion

        build.getWorkerCommandVersion = getWorkerVersion
        build.workerEnvironment = worker_env.copy()
        build.env = worker_env.copy()

        return build

    def setup_build(self, worker_version=None, worker_env=None, build_files=None):
        self._worker_version = worker_version
        self._worker_env = worker_env
        self._build_files = build_files

    def setup_step(
        self,
        step,
        worker_version=None,
        worker_env=None,
        build_files=None,
        want_default_work_dir=True,
    ):
        if worker_version is not None:
            warn_deprecated(
                "4.1.0",
                "worker_version has been deprecated, use setup_build() to pass this information",
            )
        if worker_env is not None:
            warn_deprecated(
                "4.1.0",
                "worker_env has been deprecated, use setup_build() to pass this information",
            )
        if build_files is not None:
            warn_deprecated(
                "4.1.0",
                "build_files has been deprecated, use setup_build() to pass this information",
            )

        if worker_version is not None or worker_env is not None or build_files is not None:
            self.setup_build(
                worker_version=worker_version, worker_env=worker_env, build_files=build_files
            )

        step = buildstep.create_step_from_step_or_factory(step)

        # set defaults
        if want_default_work_dir:
            step.workdir = step._workdir or 'wkdir'

        if self.build is None:
            self.build = self._setup_fake_build(
                self._worker_version, self._worker_env, self._build_files
            )

        step.setBuild(self.build)
        step.progress = mock.Mock(name="progress")
        step.worker = self.worker

        # step overrides

        def addLog(name, type='s', logEncoding=None):
            _log = logfile.FakeLogFile(name)
            step.logs[name] = _log
            step._connectPendingLogObservers()
            return defer.succeed(_log)

        step.addLog = addLog

        def addHTMLLog(name, html):
            _log = logfile.FakeLogFile(name)
            html = bytes2unicode(html)
            _log.addStdout(html)
            return defer.succeed(None)

        step.addHTMLLog = addHTMLLog

        def addCompleteLog(name, text):
            _log = logfile.FakeLogFile(name)
            if name in step.logs:
                raise RuntimeError(f'Attempt to add log {name} twice to the logs')
            step.logs[name] = _log
            _log.addStdout(text)
            return defer.succeed(None)

        step.addCompleteLog = addCompleteLog

        self._got_test_result_sets = []
        self._next_test_result_set_id = 1000

        def add_test_result_set(description, category, value_unit):
            self._got_test_result_sets.append((description, category, value_unit))

            setid = self._next_test_result_set_id
            self._next_test_result_set_id += 1
            return defer.succeed(setid)

        step.addTestResultSet = add_test_result_set

        self._got_test_results = []

        def add_test_result(
            setid, value, test_name=None, test_code_path=None, line=None, duration_ns=None
        ):
            self._got_test_results.append((
                setid,
                value,
                test_name,
                test_code_path,
                line,
                duration_ns,
            ))

        step.addTestResult = add_test_result

        self._got_build_data = {}

        def set_build_data(name, value, source):
            self._got_build_data[name] = (value, source)
            return defer.succeed(None)

        step.setBuildData = set_build_data

        # check that the step's name is not None
        self.assertNotEqual(step.name, None)

        self._steps.append(step)
        return step

    @property
    def step(self):
        warn_deprecated(
            "4.1.0",
            "step attribute has been deprecated, use get_nth_step(0) as a replacement",
        )
        return self.get_nth_step(0)

    def get_nth_step(self, index):
        return self._steps[index]

    def expect_commands(self, *exp):
        self._expected_commands.extend(exp)

    def expect_outcome(self, result, state_string=None):
        self._exp_results.append((result, state_string))

    def expect_property(self, property, value, source=None):
        self.exp_properties[property] = (value, source)

    def expect_no_property(self, property):
        self.exp_missing_properties.append(property)

    def expect_log_file(self, logfile, contents, step_index=0):
        self._exp_logfiles.setdefault(step_index, {})[logfile] = contents

    def expect_log_file_stderr(self, logfile, contents, step_index=0):
        self._exp_logfiles_stderr.setdefault(step_index, {})[logfile] = contents

    def expect_build_data(self, name, value, source):
        self._exp_build_data[name] = (value, source)

    def expect_hidden(self, hidden=True):
        self.exp_hidden = hidden

    def expect_exception(self, exception_class):
        self.exp_exception = exception_class
        self.expect_outcome(EXCEPTION)

    def expect_test_result_sets(self, sets):
        self._exp_test_result_sets = sets

    def expect_test_results(self, results):
        self._exp_test_results = results

    def expect_result_summary(self, *summaries):
        self._exp_result_summaries.extend(summaries)

    def expect_build_result_summary(self, *summaries):
        self._exp_build_result_summaries.extend(summaries)

    def add_run_process_expect_env(self, d):
        self._master_run_process_expect_env.update(d)

    def _dump_logs(self, step):
        for l in step.logs.values():
            if l.stdout:
                log.msg(f"{l.name} stdout:\n{l.stdout}")
            if l.stderr:
                log.msg(f"{l.name} stderr:\n{l.stderr}")

    @defer.inlineCallbacks
    def run_step(self):
        """
        Run the step set up with L{setup_step}, and check the results.

        @returns: Deferred
        """
        for step_i, step in enumerate(self._steps):
            self.conn = connection.FakeConnection(
                self, "WorkerForBuilder(connection)", step, self._interrupt_remote_command_numbers
            )
            step.setupProgress()
            result = yield step.startStep(self.conn)

            # finish up the debounced updateSummary before checking
            self.reactor.advance(1)

            exp_result, exp_state_string = self._exp_results.pop(0)

            # in case of unexpected result, display logs in stdout for
            # debugging failing tests
            if result != exp_result:
                msg = (
                    "unexpected result from step; "
                    f"expected {exp_result} ({statusToString(exp_result)}), "
                    f"got {result} ({statusToString(result)})"
                )
                log.msg(f"{msg}; dumping logs")
                self._dump_logs(step)
                raise AssertionError(f"{msg}; see logs")

            if exp_state_string:
                step_data = yield self.master.data.get(('steps', step.stepid))
                state_string = step_data['state_string']
                self.assertIsNotNone(state_string, 'no step state strings were set')
                self.assertEqual(
                    exp_state_string,
                    state_string,
                    f"expected state_string {exp_state_string!r}, got {state_string!r}",
                )

            if self._exp_result_summaries and (exp_summary := self._exp_result_summaries.pop(0)):
                step_result_summary = yield step.getResultSummary()
                self.assertEqual(exp_summary, step_result_summary)

            if self._exp_build_result_summaries and (
                exp_build_summary := self._exp_build_result_summaries.pop(0)
            ):
                step_build_result_summary = yield step.getBuildResultSummary()
                self.assertEqual(exp_build_summary, step_build_result_summary)

            properties = self.build.getProperties()

            for pn, (pv, ps) in self.exp_properties.items():
                self.assertTrue(properties.hasProperty(pn), f"missing property '{pn}'")
                self.assertEqual(properties.getProperty(pn), pv, f"property '{pn}'")
                if ps is not None:
                    self.assertEqual(
                        properties.getPropertySource(pn),
                        ps,
                        f"property {pn!r} source has source {properties.getPropertySource(pn)!r}",
                    )

            for pn in self.exp_missing_properties:
                self.assertFalse(properties.hasProperty(pn), f"unexpected property '{pn}'")

            if step_i in self._exp_logfiles:
                for l, exp in self._exp_logfiles[step_i].items():
                    got = step.logs[l].stdout
                    self._match_log(exp, got, 'stdout')

            if step_i in self._exp_logfiles_stderr:
                for l, exp in self._exp_logfiles_stderr[step_i].items():
                    got = step.logs[l].stderr
                    self._match_log(exp, got, 'stderr')

        if self._expected_commands:
            log.msg("un-executed remote commands:")
            for rc in self._expected_commands:
                log.msg(repr(rc))
            raise AssertionError("un-executed remote commands; see logs")

        if self.exp_exception:
            self.assertEqual(len(self.flushLoggedErrors(self.exp_exception)), 1)

        self.assertEqual(self._exp_test_result_sets, self._got_test_result_sets)
        self.assertEqual(self._exp_test_results, self._got_test_results)
        self.assertEqual(self._exp_build_data, self._got_build_data)

        # XXX TODO: hidden
        # self.step_status.setHidden.assert_called_once_with(self.exp_hidden)

    def _match_log(self, exp, got, log_type):
        if hasattr(exp, 'match'):
            if exp.match(got) is None:
                log.msg(f"Unexpected {log_type} log output:\n{exp}")
                log.msg(f"Expected {log_type} to match:\n{got}")
                raise AssertionError(f"Unexpected {log_type} log output; see logs")
        else:
            if got != exp:
                log.msg(f"Unexpected {log_type} log output:\n{exp}")
                log.msg(f"Expected {log_type} log output:\n{got}")
                raise AssertionError(f"Unexpected {log_type} log output; see logs")

    # callbacks from the running step

    @defer.inlineCallbacks
    def _connection_remote_start_command(self, command, conn, builder_name):
        self.assertEqual(conn, self.conn)

        exp = None
        if self._expected_commands:
            exp = self._expected_commands.pop(0)
            self._expected_commands_popped += 1
        else:
            self.fail(
                f"got remote command {command.remote_command} {command.args!r} when no "
                "further commands were expected"
            )

        if not isinstance(exp, Expect):
            self.fail(
                f"got command {command.remote_command} {command.args!r} but the "
                f"expectation is not instance of Expect: {exp!r}"
            )

        try:
            exp._check(self, command)
            yield exp.runBehaviors(command)
        except AssertionError as e:
            # log this error, as the step may swallow the AssertionError or
            # otherwise obscure the failure.  Trial will see the exception in
            # the log and print an [ERROR].  This may result in
            # double-reporting, but that's better than non-reporting!
            log.err()
            raise e

        if not exp.connection_broken:
            yield command.remote_complete()

    def _patched_create_process(
        self,
        reactor,
        command,
        workdir=None,
        env=None,
        collect_stdout=True,
        collect_stderr=True,
        stderr_is_error=False,
        io_timeout=300,
        runtime_timeout=3600,
        sigterm_timeout=5,
        initial_stdin=None,
        use_pty=False,
    ):
        _check_env_is_expected(self, self._master_run_process_expect_env, env)

        exp = None
        if self._expected_commands:
            exp = self._expected_commands.pop(0)
            self._expected_commands_popped += 1
        else:
            self.fail(
                f"got master command {command!r} at {workdir} ({env!r}) when no "
                "further commands were expected"
            )

        if not isinstance(exp, ExpectMasterShell):
            self.fail(
                f"got command {command!r} at {workdir} ({env!r}) but the "
                f"expectation is not instance of ExpectMasterShell: {exp!r}"
            )

        try:
            rc, stdout, stderr = exp._check(self, command, workdir, env)
            result_rc = rc

        except AssertionError as e:
            # log this error, as the step may swallow the AssertionError or
            # otherwise obscure the failure.  Trial will see the exception in
            # the log and print an [ERROR].  This may result in
            # double-reporting, but that's better than non-reporting!
            log.err()
            raise e

        if stderr_is_error and stderr:
            rc = -1

        return_stdout = None
        if collect_stdout is True:
            return_stdout = stdout
        elif callable(collect_stdout):
            collect_stdout(stdout)

        return_stderr = None
        if collect_stderr is True:
            return_stderr = stderr
        elif callable(collect_stderr):
            collect_stderr(stderr)

        if return_stdout is not None and return_stderr is not None:
            start_retval = (rc, return_stdout, return_stderr)
        elif return_stdout is not None:
            start_retval = (rc, return_stdout)
        elif return_stderr is not None:
            start_retval = (rc, return_stderr)
        else:
            start_retval = rc

        return FakeRunProcess(start_retval, result_rc)

    def change_worker_system(self, system):
        assert system != 'win32'
        self.worker.worker_system = system
        if system == 'nt':
            self.build.path_module = namedModule('ntpath')
            self.build.path_cls = PureWindowsPath
            self.worker.worker_basedir = '\\wrk'
        else:
            self.build.path_module = namedModule('posixpath')
            self.build.path_cls = PurePosixPath
            self.worker.worker_basedir = '/wrk'

    def interrupt_nth_remote_command(self, number):
        self._interrupt_remote_command_numbers.append(number)
