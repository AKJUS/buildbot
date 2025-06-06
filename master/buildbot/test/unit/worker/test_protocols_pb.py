# This file is part of Buildbot. Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright Buildbot Team Members

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any
from typing import NoReturn
from unittest import mock

from twisted.internet import defer
from twisted.internet.address import IPv4Address
from twisted.spread import pb as twisted_pb
from twisted.spread.pb import RemoteReference
from twisted.trial import unittest

from buildbot.test.fake import fakemaster
from buildbot.test.reactor import TestReactorMixin
from buildbot.test.util import protocols as util_protocols
from buildbot.worker.protocols import base
from buildbot.worker.protocols import pb

if TYPE_CHECKING:
    from twisted.internet.defer import Deferred

    from buildbot.util.twisted import InlineCallbacksType


class TestListener(TestReactorMixin, unittest.TestCase):
    @defer.inlineCallbacks
    def setUp(self) -> InlineCallbacksType[None]:  # type: ignore[override]
        self.setup_test_reactor()
        self.master = yield fakemaster.make_master(self)

    def makeListener(self) -> pb.Listener:
        listener = pb.Listener(self.master)
        return listener

    def test_constructor(self) -> None:
        listener = pb.Listener(self.master)
        self.assertEqual(listener.master, self.master)
        self.assertEqual(listener._registrations, {})

    @defer.inlineCallbacks
    def test_updateRegistration_simple(self) -> InlineCallbacksType[None]:
        listener = pb.Listener(self.master)
        reg = yield listener.updateRegistration('example', 'pass', 'tcp:1234')
        self.assertEqual(self.master.pbmanager._registrations, [('tcp:1234', 'example', 'pass')])
        self.assertEqual(listener._registrations['example'], ('pass', 'tcp:1234', reg))

    @defer.inlineCallbacks
    def test_updateRegistration_pass_changed(self) -> InlineCallbacksType[None]:
        listener = pb.Listener(self.master)
        listener.updateRegistration('example', 'pass', 'tcp:1234')
        reg1 = yield listener.updateRegistration('example', 'pass1', 'tcp:1234')
        self.assertEqual(listener._registrations['example'], ('pass1', 'tcp:1234', reg1))
        self.assertEqual(self.master.pbmanager._unregistrations, [('tcp:1234', 'example')])

    @defer.inlineCallbacks
    def test_updateRegistration_port_changed(self) -> InlineCallbacksType[None]:
        listener = pb.Listener(self.master)
        listener.updateRegistration('example', 'pass', 'tcp:1234')
        reg1 = yield listener.updateRegistration('example', 'pass', 'tcp:4321')
        self.assertEqual(listener._registrations['example'], ('pass', 'tcp:4321', reg1))
        self.assertEqual(self.master.pbmanager._unregistrations, [('tcp:1234', 'example')])

    @defer.inlineCallbacks
    def test_create_connection(self) -> InlineCallbacksType[None]:
        listener = pb.Listener(self.master)
        worker = mock.Mock()
        worker.workername = 'test'
        mind = mock.Mock(spec=RemoteReference)
        mind.broker = mock.Mock()

        listener.updateRegistration('example', 'pass', 'tcp:1234')
        self.master.workers.register(worker)
        conn = yield listener._create_connection(mind, worker.workername)

        mind.broker.transport.setTcpKeepAlive.assert_called_with(1)
        self.assertIsInstance(conn, pb.Connection)


class TestConnectionApi(
    util_protocols.ConnectionInterfaceTest, TestReactorMixin, unittest.TestCase
):
    @defer.inlineCallbacks
    def setUp(self) -> InlineCallbacksType[None]:  # type: ignore[override]
        self.setup_test_reactor()
        self.master = yield fakemaster.make_master(self)
        self.conn = pb.Connection(self.master, mock.Mock(), mock.Mock())


class TestConnection(TestReactorMixin, unittest.TestCase):
    @defer.inlineCallbacks
    def setUp(self) -> InlineCallbacksType[None]:  # type: ignore[override]
        self.setup_test_reactor()
        self.master = yield fakemaster.make_master(self)
        self.mind = mock.Mock()
        self.worker = mock.Mock()

    def test_constructor(self) -> None:
        conn = pb.Connection(self.master, self.worker, self.mind)

        self.assertEqual(conn.mind, self.mind)
        self.assertEqual(conn.master, self.master)
        self.assertEqual(conn.worker, self.worker)

    @defer.inlineCallbacks
    def test_attached(self) -> InlineCallbacksType[None]:
        conn = pb.Connection(self.master, self.worker, self.mind)
        att = yield conn.attached(self.mind)

        self.worker.attached.assert_called_with(conn)
        self.assertEqual(att, conn)

        self.reactor.pump([10] * 361)
        expected_call = [
            mock.call('print', message="keepalive"),
        ]
        self.assertEqual(self.mind.callRemote.call_args_list, expected_call)

        conn.detached(self.mind)
        yield conn.waitShutdown()

    @defer.inlineCallbacks
    def test_detached(self) -> InlineCallbacksType[None]:
        conn = pb.Connection(self.master, self.worker, self.mind)
        conn.attached(self.mind)
        conn.detached(self.mind)

        self.assertEqual(conn.keepalive_timer, None)
        self.assertEqual(conn.mind, None)
        yield conn.waitShutdown()

    def test_loseConnection(self) -> None:
        conn = pb.Connection(self.master, self.worker, self.mind)
        conn.loseConnection()

        self.assertEqual(conn.keepalive_timer, None)
        assert conn.mind is not None
        conn.mind.broker.transport.loseConnection.assert_called_with()

    def test_remotePrint(self) -> None:
        conn = pb.Connection(self.master, self.worker, self.mind)
        conn.remotePrint(message='test')
        assert conn.mind is not None
        conn.mind.callRemote.assert_called_with('print', message='test')  # type: ignore[attr-defined]

    @defer.inlineCallbacks
    def test_remoteGetWorkerInfo_slave(self) -> InlineCallbacksType[None]:
        def side_effect(*args: Any, **kwargs: Any) -> Deferred[Any] | None:
            if args[0] == 'getWorkerInfo':
                return defer.fail(
                    twisted_pb.RemoteError('twisted.spread.flavors.NoSuchMethod', None, None)
                )
            if args[0] == 'getSlaveInfo':
                return defer.succeed({'info': 'test'})
            if args[0] == 'getCommands':
                return defer.succeed({'x': 1, 'y': 2})
            if args[0] == 'getVersion':
                return defer.succeed('TheVersion')
            return None

        self.mind.callRemote.side_effect = side_effect
        conn = pb.Connection(self.master, self.worker, self.mind)
        info = yield conn.remoteGetWorkerInfo()

        r = {'info': 'test', 'worker_commands': {'y': 2, 'x': 1}, 'version': 'TheVersion'}
        self.assertEqual(info, r)
        expected_calls = [
            mock.call('getWorkerInfo'),
            mock.call(
                'print',
                message='buildbot-slave detected, failing back to deprecated buildslave API. '
                '(Ignoring missing getWorkerInfo method.)',
            ),
            mock.call('getSlaveInfo'),
            mock.call('getCommands'),
            mock.call('getVersion'),
        ]
        self.assertEqual(self.mind.callRemote.call_args_list, expected_calls)

    @defer.inlineCallbacks
    def test_remoteGetWorkerInfo_slave_2_16(self) -> InlineCallbacksType[None]:
        """In buildslave 2.16 all information about worker is retrieved in
        a single getSlaveInfo() call."""

        def side_effect(*args: Any, **kwargs: Any) -> Deferred[Any] | None:
            if args[0] == 'getWorkerInfo':
                return defer.fail(
                    twisted_pb.RemoteError('twisted.spread.flavors.NoSuchMethod', None, None)
                )
            if args[0] == 'getSlaveInfo':
                return defer.succeed({
                    'info': 'test',
                    'slave_commands': {'x': 1, 'y': 2},
                    'version': 'TheVersion',
                })
            if args[0] == 'print':
                return None
            raise ValueError(f"Command unknown: {args}")

        self.mind.callRemote.side_effect = side_effect
        conn = pb.Connection(self.master, self.worker, self.mind)
        info = yield conn.remoteGetWorkerInfo()

        r = {'info': 'test', 'worker_commands': {'y': 2, 'x': 1}, 'version': 'TheVersion'}
        self.assertEqual(info, r)
        expected_calls = [
            mock.call('getWorkerInfo'),
            mock.call(
                'print',
                message='buildbot-slave detected, failing back to deprecated buildslave API. '
                '(Ignoring missing getWorkerInfo method.)',
            ),
            mock.call('getSlaveInfo'),
        ]
        self.assertEqual(self.mind.callRemote.call_args_list, expected_calls)

    @defer.inlineCallbacks
    def test_remoteGetWorkerInfo_worker(self) -> InlineCallbacksType[None]:
        def side_effect(*args: Any, **kwargs: Any) -> Deferred[Any] | None:
            if args[0] == 'getWorkerInfo':
                return defer.succeed({
                    'info': 'test',
                    'worker_commands': {'y': 2, 'x': 1},
                    'version': 'TheVersion',
                })
            raise ValueError(f"Command unknown: {args}")

        self.mind.callRemote.side_effect = side_effect
        conn = pb.Connection(self.master, self.worker, self.mind)
        info = yield conn.remoteGetWorkerInfo()

        r = {'info': 'test', 'worker_commands': {'y': 2, 'x': 1}, 'version': 'TheVersion'}
        self.assertEqual(info, r)
        expected_calls = [mock.call('getWorkerInfo')]
        self.assertEqual(self.mind.callRemote.call_args_list, expected_calls)

    @defer.inlineCallbacks
    def test_remoteGetWorkerInfo_getWorkerInfo_fails(self) -> InlineCallbacksType[None]:
        def side_effect(*args: Any, **kwargs: Any) -> Deferred[Any] | None:
            if args[0] == 'getWorkerInfo':
                return defer.fail(
                    twisted_pb.RemoteError('twisted.spread.flavors.NoSuchMethod', None, None)
                )
            if args[0] == 'getSlaveInfo':
                return defer.fail(
                    twisted_pb.RemoteError('twisted.spread.flavors.NoSuchMethod', None, None)
                )
            if args[0] == 'getCommands':
                return defer.succeed({'x': 1, 'y': 2})
            if args[0] == 'getVersion':
                return defer.succeed('TheVersion')
            if args[0] == 'print':
                return None
            raise ValueError(f"Command unknown: {args}")

        self.mind.callRemote.side_effect = side_effect
        conn = pb.Connection(self.master, self.worker, self.mind)
        info = yield conn.remoteGetWorkerInfo()

        r = {'worker_commands': {'y': 2, 'x': 1}, 'version': 'TheVersion'}
        self.assertEqual(info, r)
        expected_calls = [
            mock.call('getWorkerInfo'),
            mock.call(
                'print',
                message='buildbot-slave detected, failing back to deprecated buildslave API. '
                '(Ignoring missing getWorkerInfo method.)',
            ),
            mock.call('getSlaveInfo'),
            mock.call('getCommands'),
            mock.call('getVersion'),
        ]
        self.assertEqual(self.mind.callRemote.call_args_list, expected_calls)

    @defer.inlineCallbacks
    def test_remoteGetWorkerInfo_no_info(self) -> InlineCallbacksType[None]:
        # All remote commands tried in remoteGetWorkerInfo are unavailable.
        # This should be real old worker...
        def side_effect(*args: Any, **kwargs: Any) -> Deferred[Any] | None:
            if args[0] == 'print':
                return None
            return defer.fail(
                twisted_pb.RemoteError('twisted.spread.flavors.NoSuchMethod', None, None)
            )

        self.mind.callRemote.side_effect = side_effect
        conn = pb.Connection(self.master, self.worker, self.mind)
        info = yield conn.remoteGetWorkerInfo()

        r: dict[Any, Any] = {}
        self.assertEqual(info, r)
        expected_calls = [
            mock.call('getWorkerInfo'),
            mock.call(
                'print',
                message='buildbot-slave detected, failing back to deprecated buildslave API. '
                '(Ignoring missing getWorkerInfo method.)',
            ),
            mock.call('getSlaveInfo'),
            mock.call('getCommands'),
            mock.call('getVersion'),
        ]
        self.assertEqual(self.mind.callRemote.call_args_list, expected_calls)

    @defer.inlineCallbacks
    def test_remoteSetBuilderList(self) -> InlineCallbacksType[None]:
        builders = ['builder1', 'builder2']
        self.mind.callRemote.return_value = defer.succeed(builders)
        conn = pb.Connection(self.master, self.worker, self.mind)
        r = yield conn.remoteSetBuilderList(builders)  # type: ignore[arg-type]

        self.assertEqual(r, builders)
        self.assertEqual(conn.builders, builders)
        self.mind.callRemote.assert_called_with('setBuilderList', builders)

    def test_remoteStartCommand(self) -> None:
        builders = ['builder']
        ret_val = {'builder': mock.Mock()}
        self.mind.callRemote.return_value = defer.succeed(ret_val)
        conn = pb.Connection(self.master, self.worker, self.mind)
        conn.remoteSetBuilderList(builders)  # type: ignore[arg-type]

        RCInstance = base.RemoteCommandImpl()
        builder_name = "builder"
        commandID = None
        remote_command = "command"
        args = {"args": 'args'}

        conn.remoteStartCommand(RCInstance, builder_name, commandID, remote_command, args)

        callargs = ret_val['builder'].callRemote.call_args_list[0][0]
        callargs_without_rc = (callargs[0], callargs[2], callargs[3], callargs[4])
        self.assertEqual(callargs_without_rc, ('startCommand', commandID, remote_command, args))
        self.assertIsInstance(callargs[1], pb.RemoteCommand)
        self.assertEqual(callargs[1].impl, RCInstance)

    @defer.inlineCallbacks
    def test_do_keepalive(self) -> InlineCallbacksType[None]:
        conn = pb.Connection(self.master, self.worker, self.mind)
        yield conn._do_keepalive()

        self.mind.callRemote.assert_called_with('print', message="keepalive")

    def test_remoteShutdown(self) -> None:
        self.mind.callRemote.return_value = defer.succeed(None)
        conn = pb.Connection(self.master, self.worker, self.mind)
        # note that we do not test the "old way", as it is now *very* old.
        conn.remoteShutdown()

        self.mind.callRemote.assert_called_with('shutdown')

    def test_remoteStartBuild(self) -> None:
        conn = pb.Connection(self.master, self.worker, self.mind)
        builders = {'builder': mock.Mock()}
        self.mind.callRemote.return_value = defer.succeed(builders)
        conn = pb.Connection(self.master, self.worker, self.mind)
        conn.remoteSetBuilderList(builders)  # type: ignore[arg-type]

        conn.remoteStartBuild('builder')

        builders['builder'].callRemote.assert_called_with('startBuild')

    @defer.inlineCallbacks
    def test_startStopKeepaliveTimer(self) -> InlineCallbacksType[None]:
        conn = pb.Connection(self.master, self.worker, self.mind)
        conn.startKeepaliveTimer()

        self.mind.callRemote.assert_not_called()
        self.reactor.pump([10] * 361)
        expected_call = [
            mock.call('print', message="keepalive"),
        ]
        self.assertEqual(self.mind.callRemote.call_args_list, expected_call)

        self.reactor.pump([10] * 361)
        expected_calls = [
            mock.call('print', message="keepalive"),
            mock.call('print', message="keepalive"),
        ]
        self.assertEqual(self.mind.callRemote.call_args_list, expected_calls)

        conn.stopKeepaliveTimer()
        yield conn.waitShutdown()

    def test_perspective_shutdown(self) -> None:
        conn = pb.Connection(self.master, self.worker, self.mind)
        conn.perspective_shutdown()

        conn.worker.shutdownRequested.assert_called_with()  # type: ignore[attr-defined]
        conn.worker.messageReceivedFromWorker.assert_called_with()  # type: ignore[attr-defined]

    def test_perspective_keepalive(self) -> None:
        conn = pb.Connection(self.master, self.worker, self.mind)
        conn.perspective_keepalive()

        conn.worker.messageReceivedFromWorker.assert_called_with()  # type: ignore[attr-defined]

    def test_get_peer(self) -> None:
        conn = pb.Connection(self.master, self.worker, self.mind)
        assert conn.mind is not None
        conn.mind.broker.transport.getPeer.return_value = IPv4Address(
            "TCP",
            "ip",
            "port",  # type: ignore[arg-type]
        )
        self.assertEqual(conn.get_peer(), "ip:port")


class Test_wrapRemoteException(unittest.TestCase):
    def test_raises_NoSuchMethod(self) -> None:
        def f() -> NoReturn:
            with pb._wrapRemoteException():
                raise twisted_pb.RemoteError('twisted.spread.flavors.NoSuchMethod', None, None)

        with self.assertRaises(pb._NoSuchMethod):
            f()

    def test_raises_unknown(self) -> None:
        class Error(Exception):
            pass

        def f() -> NoReturn:
            with pb._wrapRemoteException():
                raise Error()

        with self.assertRaises(Error):
            f()

    def test_raises_RemoteError(self) -> None:
        def f() -> NoReturn:
            with pb._wrapRemoteException():
                raise twisted_pb.RemoteError('twisted.spread.flavors.ProtocolError', None, None)

        with self.assertRaises(twisted_pb.RemoteError):
            f()
