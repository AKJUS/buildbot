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
from pathlib import PurePath
from pathlib import PurePosixPath
from pathlib import PureWindowsPath
from typing import TYPE_CHECKING
from typing import Any

from twisted.internet import defer
from twisted.python import log
from twisted.python.reflect import namedModule

from buildbot.pbutil import decode
from buildbot.process import remotecommand
from buildbot.util import deferwaiter
from buildbot.util import path_expand_user
from buildbot.worker.protocols import base
from buildbot.worker.protocols.base import RemoteCommandImpl

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import PurePath

    from twisted.internet.defer import Deferred

    from buildbot.master import BuildMaster
    from buildbot.util.twisted import InlineCallbacksType
    from buildbot.worker.base import Worker
    from buildbot.worker.protocols.manager.msgpack import BuildbotWebSocketServerProtocol
    from buildbot.worker.protocols.manager.msgpack import MsgManager


class Listener(base.UpdateRegistrationListener):
    name = "MsgPackListener"

    def __init__(self, master: BuildMaster) -> None:
        super().__init__(master=master)
        self.ConnectionClass = Connection

    def get_manager(self) -> MsgManager:
        return self.master.msgmanager

    def before_connection_setup(
        self,
        protocol: BuildbotWebSocketServerProtocol,  # type: ignore[override]
        workerName: str,
    ) -> None:
        log.msg(f"worker '{workerName}' attaching")


class BasicRemoteCommand(RemoteCommandImpl):
    # only has basic functions needed for remoteSetBuilderList in class Connection
    # when waiting for update messages
    def __init__(self, worker_name: str, expected_keys: Iterable[str], error_msg: str) -> None:
        self.worker_name = worker_name
        self.update_results: dict[Any, Any] = {}
        self.expected_keys = expected_keys
        self.error_msg = error_msg
        self.d: Deferred[None] = defer.Deferred()

    def wait_until_complete(self) -> Deferred[None]:
        return self.d

    def remote_update_msgpack(self, args: list[tuple[Any, Any]]) -> Deferred[None]:
        # args is a list of tuples
        # first element of the tuple is a key, second element is a value
        for key, value in args:
            if key not in self.update_results:
                self.update_results[key] = value

        return defer.succeed(None)

    def remote_complete(self, failure: Any | None = None) -> Deferred[None]:
        if 'rc' not in self.update_results:
            self.d.errback(
                Exception(
                    f"Worker {self.worker_name} reconfiguration or connection to "
                    f"master failed. {self.error_msg}. 'rc' did not arrive."
                )
            )
            return defer.succeed(None)

        if self.update_results['rc'] != 0:
            self.d.errback(
                Exception(
                    f"Worker {self.worker_name} reconfiguration or connection to "
                    f"master failed. {self.error_msg}. Error number: "
                    f"{self.update_results['rc']}"
                )
            )
            return defer.succeed(None)

        for key in self.expected_keys:
            if key not in self.update_results:
                self.d.errback(
                    Exception(
                        f"Worker {self.worker_name} reconfiguration or connection "
                        f"to master failed. {self.error_msg} "
                        f"Key '{key}' is missing."
                    )
                )
                return defer.succeed(None)

        self.d.callback(None)
        return defer.succeed(None)


class Connection(base.Connection):
    # TODO: configure keepalive_interval in
    # c['protocols']['msgpack']['keepalive_interval']
    keepalive_timer: None = None
    keepalive_interval = 3600
    info: Any = None

    def __init__(
        self,
        master: BuildMaster,
        worker: Worker,
        protocol: BuildbotWebSocketServerProtocol,
    ) -> None:
        assert worker.workername is not None
        super().__init__(worker.workername)
        self.master = master
        self.worker = worker
        self.protocol: BuildbotWebSocketServerProtocol | None = protocol
        self._keepalive_waiter = deferwaiter.DeferWaiter()
        self._keepalive_action_handler = deferwaiter.RepeatedActionHandler(
            master.reactor, self._keepalive_waiter, self.keepalive_interval, self._do_keepalive
        )
        self.path_cls: type[PurePath] | None = None

    # methods called by the BuildbotWebSocketServerProtocol

    @defer.inlineCallbacks
    def attached(self, protocol: BuildbotWebSocketServerProtocol) -> InlineCallbacksType[None]:
        self.startKeepaliveTimer()
        self.notifyOnDisconnect(self._stop_keepalive_timer)
        yield self.worker.attached(self)

    def detached(self, protocol: BuildbotWebSocketServerProtocol) -> None:
        self.stopKeepaliveTimer()
        self.protocol = None
        self.notifyDisconnected()

    # disconnection handling
    @defer.inlineCallbacks
    def _stop_keepalive_timer(self) -> InlineCallbacksType[None]:
        self.stopKeepaliveTimer()
        yield self._keepalive_waiter.wait()

    def loseConnection(self) -> None:
        self.stopKeepaliveTimer()
        assert self.protocol is not None
        self.protocol.transport.abortConnection()

    # keepalive handling

    def _do_keepalive(self) -> Deferred[None]:
        return self.remoteKeepalive()

    def stopKeepaliveTimer(self) -> None:
        self._keepalive_action_handler.stop()

    def startKeepaliveTimer(self) -> None:
        assert self.keepalive_interval
        self._keepalive_action_handler.start()

    # methods to send messages to the worker

    def remoteKeepalive(self) -> Deferred[None]:
        assert self.protocol is not None
        return self.protocol.get_message_result({'op': 'keepalive'})

    def remotePrint(self, message: str) -> Deferred[None]:
        assert self.protocol is not None
        return self.protocol.get_message_result({'op': 'print', 'message': message})

    @defer.inlineCallbacks
    def remoteGetWorkerInfo(self) -> InlineCallbacksType[Any]:
        assert self.protocol is not None
        info = yield self.protocol.get_message_result({'op': 'get_worker_info'})
        self.info = decode(info)

        worker_system = self.info.get("system", None)
        if worker_system == "nt":
            self.path_module = namedModule("ntpath")
            self.path_expanduser = path_expand_user.nt_expanduser
            self.path_cls = PureWindowsPath
        else:
            # most everything accepts / as separator, so posix should be a reasonable fallback
            self.path_module = namedModule("posixpath")
            self.path_expanduser = path_expand_user.posix_expanduser
            self.path_cls = PurePosixPath
        return self.info

    def _set_worker_settings(self) -> Deferred:
        # the lookahead here (`(?=.)`) ensures that `\r` doesn't match at the end
        # of the buffer
        # we also convert cursor control sequence to newlines
        # and ugly \b+ (use of backspace to implement progress bar)
        newline_re = r'(\r\n|\r(?=.)|\033\[u|\033\[[0-9]+;[0-9]+[Hf]|\033\[2J|\x08+)'
        assert self.protocol is not None
        return self.protocol.get_message_result({
            'op': 'set_worker_settings',
            'args': {
                'newline_re': newline_re,
                'max_line_length': 4096,
                'buffer_timeout': 5,
                'buffer_size': 64 * 1024,
            },
        })

    def create_remote_command(
        self,
        worker_name: str,
        expected_keys: list[str],
        error_msg: str,
    ) -> tuple[BasicRemoteCommand, str]:
        command_id = remotecommand.RemoteCommand.generate_new_command_id()
        command = BasicRemoteCommand(worker_name, expected_keys, error_msg)
        assert self.protocol is not None
        self.protocol.command_id_to_command_map[command_id] = command
        return (command, command_id)

    @defer.inlineCallbacks
    def remoteSetBuilderList(
        self, builders: list[tuple[str, str]]
    ) -> InlineCallbacksType[list[str]]:
        assert self.path_cls is not None

        yield self._set_worker_settings()

        basedir = self.path_cls(self.info['basedir'])
        builder_names = [name for name, _ in builders]
        self.builder_basedirs = {name: basedir.joinpath(builddir) for name, builddir in builders}

        wanted_dirs = {builddir for _, builddir in builders}
        wanted_dirs.add('info')
        dirs_to_mkdir = set(wanted_dirs)
        assert self.worker.workername is not None
        command, command_id = self.create_remote_command(
            self.worker.workername,
            ['files'],
            'Worker could not send a list of builder directories.',
        )

        assert self.protocol is not None
        yield self.protocol.get_message_result({
            'op': 'start_command',
            'command_id': command_id,
            'command_name': 'listdir',
            'args': {'path': str(basedir)},
        })

        # wait until command is over to get the update request message with args['files']
        yield command.wait_until_complete()
        files = command.update_results['files']

        paths_to_rmdir = []

        for dir in files:
            dirs_to_mkdir.discard(dir)
            if dir not in wanted_dirs:
                if self.info['delete_leftover_dirs']:
                    # send 'stat' start_command and wait for status information which comes from
                    # worker in a response message. Status information is saved in update_results
                    # dictionary with key 'stat'. 'stat' value is a tuple of 10 elements, where
                    # first element is File mode. It goes to S_ISDIR(mode) to check if path is
                    # a directory so that files are not deleted
                    path = str(basedir.joinpath(dir))
                    command, command_id = self.create_remote_command(
                        self.worker.workername,
                        ['stat'],
                        "Worker could not send status " + "information about its files.",
                    )
                    yield self.protocol.get_message_result({
                        'op': 'start_command',
                        'command_id': command_id,
                        'command_name': 'stat',
                        'args': {'path': path},
                    })
                    yield command.wait_until_complete()
                    mode = command.update_results['stat'][0]
                    if stat.S_ISDIR(mode):
                        paths_to_rmdir.append(path)

        if paths_to_rmdir:
            log.msg(
                f"Deleting directory '{paths_to_rmdir}' that is not being used by the buildmaster."
            )

            # remove leftover directories from worker
            command, command_id = self.create_remote_command(
                self.worker.workername, [], "Worker could not remove directories."
            )
            assert self.protocol is not None
            yield self.protocol.get_message_result({
                'op': 'start_command',
                'command_id': command_id,
                'command_name': 'rmdir',
                'args': {'paths': paths_to_rmdir},
            })
            yield command.wait_until_complete()

        paths_to_mkdir = [str(basedir.joinpath(dir)) for dir in sorted(list(dirs_to_mkdir))]
        if paths_to_mkdir:
            # make wanted builder directories which do not exist in worker yet
            command, command_id = self.create_remote_command(
                self.worker.workername, [], "Worker could not make directories."
            )
            yield self.protocol.get_message_result({
                'op': 'start_command',
                'command_id': command_id,
                'command_name': 'mkdir',
                'args': {'paths': paths_to_mkdir},
            })
            yield command.wait_until_complete()

        self.builders = builder_names
        return builder_names

    @defer.inlineCallbacks
    def remoteStartCommand(
        self,
        remoteCommand: RemoteCommandImpl,
        builderName: str,
        commandId: str,
        commandName: str,
        args: dict[str, Any],
    ) -> InlineCallbacksType[None]:
        if commandName == "mkdir":
            if isinstance(args['dir'], list):
                builder_basedir = self._get_builder_basedir(builderName)
                args['paths'] = [str(builder_basedir.joinpath(dir)) for dir in args['dir']]
            else:
                builder_basedir = self._get_builder_basedir(builderName)
                args['paths'] = [str(builder_basedir.joinpath(args['dir']))]
            del args['dir']

        if commandName == "rmdir":
            builder_basedir = self._get_builder_basedir(builderName)
            if isinstance(args['dir'], list):
                args['paths'] = [str(builder_basedir.joinpath(dir)) for dir in args['dir']]
            else:
                args['paths'] = [str(builder_basedir.joinpath(args['dir']))]
            del args['dir']

        if commandName == "cpdir":
            builder_basedir = self._get_builder_basedir(builderName)
            args['from_path'] = str(builder_basedir.joinpath(args['fromdir']))
            args['to_path'] = str(builder_basedir.joinpath(args['todir']))
            del args['fromdir']
            del args['todir']

        if commandName == "stat":
            builder_basedir = self._get_builder_basedir(builderName)
            args['path'] = str(builder_basedir.joinpath(args.get('workdir', ''), args['file']))
            del args['file']

        if commandName == "glob":
            builder_basedir = self._get_builder_basedir(builderName)
            args['path'] = str(builder_basedir.joinpath(args['path']))

        if commandName == "listdir":
            builder_basedir = self._get_builder_basedir(builderName)
            args['path'] = str(builder_basedir.joinpath(args['dir']))
            del args['dir']

        if commandName == "rmfile":
            builder_basedir = self._get_builder_basedir(builderName)
            args['path'] = str(
                builder_basedir.joinpath(self.path_expanduser(args['path'], self.info['environ']))
            )

        if commandName == "shell":
            builder_basedir = self._get_builder_basedir(builderName)
            args['workdir'] = str(builder_basedir.joinpath(args['workdir']))

        if commandName == "uploadFile":
            commandName = "upload_file"
            builder_basedir = self._get_builder_basedir(builderName)
            args['path'] = str(
                builder_basedir.joinpath(
                    args['workdir'],
                    self.path_expanduser(args['workersrc'], self.info['environ']),
                )
            )

        if commandName == "uploadDirectory":
            commandName = "upload_directory"
            builder_basedir = self._get_builder_basedir(builderName)
            args['path'] = str(
                builder_basedir.joinpath(
                    args['workdir'],
                    self.path_expanduser(args['workersrc'], self.info['environ']),
                )
            )

        if commandName == "downloadFile":
            commandName = "download_file"
            builder_basedir = self._get_builder_basedir(builderName)
            args['path'] = str(
                builder_basedir.joinpath(
                    args['workdir'],
                    self.path_expanduser(args['workerdest'], self.info['environ']),
                )
            )
        if "want_stdout" in args:
            if args["want_stdout"] == 1:
                args["want_stdout"] = True
            else:
                args["want_stdout"] = False

        if "want_stderr" in args:
            if args["want_stderr"] == 1:
                args["want_stderr"] = True
            else:
                args["want_stderr"] = False

        assert self.protocol is not None
        self.protocol.command_id_to_command_map[commandId] = remoteCommand
        if 'reader' in args:
            self.protocol.command_id_to_reader_map[commandId] = args['reader']
            del args['reader']
        if 'writer' in args:
            self.protocol.command_id_to_writer_map[commandId] = args['writer']
            del args['writer']
        yield self.protocol.get_message_result({
            'op': 'start_command',
            'builder_name': builderName,
            'command_id': commandId,
            'command_name': commandName,
            'args': args,
        })

    @defer.inlineCallbacks
    def remoteShutdown(self) -> InlineCallbacksType[None]:
        assert self.protocol is not None
        yield self.protocol.get_message_result({'op': 'shutdown'})

    def remoteStartBuild(self, builderName: str) -> Deferred[None]:
        return defer.succeed(None)

    @defer.inlineCallbacks
    def remoteInterruptCommand(
        self, builderName: str, commandId: str | int, why: str
    ) -> InlineCallbacksType[None]:
        assert self.protocol is not None
        yield self.protocol.get_message_result({
            'op': 'interrupt_command',
            'builder_name': builderName,
            'command_id': commandId,
            'why': why,
        })

    # perspective methods called by the worker

    def perspective_keepalive(self) -> None:
        self.worker.messageReceivedFromWorker()

    def perspective_shutdown(self) -> None:
        self.worker.messageReceivedFromWorker()
        self.worker.shutdownRequested()

    def get_peer(self) -> str:
        assert self.protocol is not None
        p = self.protocol.transport.getPeer()
        return f"{p.host}:{p.port}"

    def _get_builder_basedir(self, builder_name: str) -> PurePath:
        assert self.path_cls is not None
        return self.path_cls(self.builder_basedirs[builder_name])
