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

import os
import sys
from typing import Callable
from typing import Mapping
from typing import NoReturn
from typing import cast

from twisted.python import log
from twisted.python import usage
from twisted.trial import unittest

from buildbot_worker.scripts import runner
from buildbot_worker.test.util import misc

try:
    from unittest import mock
except ImportError:
    from unittest import mock


class OptionsMixin:
    def assertOptions(self, opts: Mapping, exp: Mapping) -> None:
        assert isinstance(self, unittest.TestCase)
        got = {k: opts[k] for k in exp}
        if got != exp:
            msg = []
            for k in exp:
                if opts[k] != exp[k]:
                    msg.append(f" {k}: expected {exp[k]!r}, got {opts[k]!r}")
            self.fail("did not get expected options\n" + ("\n".join(msg)))


class BaseDirTestsMixin:
    """
    Common tests for Options classes with 'basedir' parameter
    """

    GETCWD_PATH = "test-dir"
    ABSPATH_PREFIX = "test-prefix-"
    MY_BASEDIR = "my-basedir"

    # the options class to instantiate for test cases
    options_class: type[usage.Options] | None = None

    def setUp(self) -> None:
        assert isinstance(self, unittest.TestCase)
        self.patch(os, "getcwd", lambda: self.GETCWD_PATH)
        self.patch(os.path, "abspath", lambda path: self.ABSPATH_PREFIX + path)

    def parse(self, *args: str) -> usage.Options:
        assert self.options_class is not None

        opts = self.options_class()
        opts.parseOptions(args)
        return opts

    def test_defaults(self) -> None:
        assert isinstance(self, unittest.TestCase)
        opts = self.parse()
        self.assertEqual(
            opts["basedir"], self.ABSPATH_PREFIX + self.GETCWD_PATH, "unexpected basedir path"
        )

    def test_basedir_arg(self) -> None:
        assert isinstance(self, unittest.TestCase)
        opts = self.parse(self.MY_BASEDIR)
        self.assertEqual(
            opts["basedir"], self.ABSPATH_PREFIX + self.MY_BASEDIR, "unexpected basedir path"
        )

    def test_too_many_args(self) -> None:
        assert isinstance(self, unittest.TestCase)
        with self.assertRaisesRegex(usage.UsageError, "I wasn't expecting so many arguments"):
            self.parse("arg1", "arg2")


class TestMakerBase(BaseDirTestsMixin, unittest.TestCase):
    """
    Test buildbot_worker.scripts.runner.MakerBase class.
    """

    options_class = runner.MakerBase


class TestStopOptions(BaseDirTestsMixin, unittest.TestCase):
    """
    Test buildbot_worker.scripts.runner.StopOptions class.
    """

    options_class = runner.StopOptions

    def test_synopsis(self) -> None:
        opts = runner.StopOptions()
        self.assertIn('buildbot-worker stop', opts.getSynopsis())


class TestStartOptions(OptionsMixin, BaseDirTestsMixin, unittest.TestCase):
    """
    Test buildbot_worker.scripts.runner.StartOptions class.
    """

    options_class = runner.StartOptions

    def test_synopsis(self) -> None:
        opts = runner.StartOptions()
        self.assertIn('buildbot-worker start', opts.getSynopsis())

    def test_all_args(self) -> None:
        opts = self.parse("--quiet", "--nodaemon", self.MY_BASEDIR)
        self.assertOptions(
            opts,
            {"quiet": True, "nodaemon": True, "basedir": self.ABSPATH_PREFIX + self.MY_BASEDIR},
        )


class TestRestartOptions(OptionsMixin, BaseDirTestsMixin, unittest.TestCase):
    """
    Test buildbot_worker.scripts.runner.RestartOptions class.
    """

    options_class = runner.RestartOptions

    def test_synopsis(self) -> None:
        opts = runner.RestartOptions()
        self.assertIn('buildbot-worker restart', opts.getSynopsis())

    def test_all_args(self) -> None:
        opts = self.parse("--quiet", "--nodaemon", self.MY_BASEDIR)
        self.assertOptions(
            opts,
            {"quiet": True, "nodaemon": True, "basedir": self.ABSPATH_PREFIX + self.MY_BASEDIR},
        )


class TestCreateWorkerOptions(OptionsMixin, unittest.TestCase):
    """
    Test buildbot_worker.scripts.runner.CreateWorkerOptions class.
    """

    req_args = ["bdir", "mstr:5678", "name", "pswd"]

    def parse(self, *args: str) -> runner.CreateWorkerOptions:
        opts = runner.CreateWorkerOptions()
        opts.parseOptions(args)
        return opts

    def test_defaults(self) -> None:
        with self.assertRaisesRegex(usage.UsageError, "incorrect number of arguments"):
            self.parse()

    def test_synopsis(self) -> None:
        opts = runner.CreateWorkerOptions()
        self.assertIn('buildbot-worker create-worker', opts.getSynopsis())

    def test_min_args(self) -> None:
        # patch runner.MakerBase.postOptions() so that 'basedir'
        # argument will not be converted to absolute path
        self.patch(runner.MakerBase, "postOptions", mock.Mock())

        self.assertOptions(
            self.parse(*self.req_args),
            {"basedir": "bdir", "host": "mstr", "port": 5678, "name": "name", "passwd": "pswd"},
        )

    def test_all_args(self) -> None:
        # patch runner.MakerBase.postOptions() so that 'basedir'
        # argument will not be converted to absolute path
        self.patch(runner.MakerBase, "postOptions", mock.Mock())

        opts = self.parse(
            "--force",
            "--relocatable",
            "--no-logrotate",
            "--keepalive=4",
            "--umask=0o22",
            "--maxdelay=3",
            "--numcpus=4",
            "--log-size=2",
            "--log-count=1",
            "--allow-shutdown=file",
            *self.req_args,
        )
        self.assertOptions(
            opts,
            {
                "force": True,
                "relocatable": True,
                "no-logrotate": True,
                "umask": "0o22",
                "maxdelay": 3,
                "numcpus": "4",
                "log-size": 2,
                "log-count": "1",
                "allow-shutdown": "file",
                "basedir": "bdir",
                "host": "mstr",
                "port": 5678,
                "name": "name",
                "passwd": "pswd",
            },
        )

    def test_master_url(self) -> None:
        with self.assertRaisesRegex(usage.UsageError, "<master> is not a URL - do not use URL"):
            self.parse("a", "http://b.c", "d", "e")

    def test_inv_keepalive(self) -> None:
        with self.assertRaisesRegex(usage.UsageError, "keepalive parameter needs to be a number"):
            self.parse("--keepalive=X", *self.req_args)

    def test_inv_maxdelay(self) -> None:
        with self.assertRaisesRegex(usage.UsageError, "maxdelay parameter needs to be a number"):
            self.parse("--maxdelay=X", *self.req_args)

    def test_inv_log_size(self) -> None:
        with self.assertRaisesRegex(usage.UsageError, "log-size parameter needs to be a number"):
            self.parse("--log-size=X", *self.req_args)

    def test_inv_log_count(self) -> None:
        with self.assertRaisesRegex(
            usage.UsageError, "log-count parameter needs to be a number or None"
        ):
            self.parse("--log-count=X", *self.req_args)

    def test_inv_numcpus(self) -> None:
        with self.assertRaisesRegex(
            usage.UsageError, "numcpus parameter needs to be a number or None"
        ):
            self.parse("--numcpus=X", *self.req_args)

    def test_inv_umask(self) -> None:
        with self.assertRaisesRegex(
            usage.UsageError, "umask parameter needs to be a number or None"
        ):
            self.parse("--umask=X", *self.req_args)

    def test_inv_umask2(self) -> None:
        with self.assertRaisesRegex(
            usage.UsageError, "umask parameter needs to be a number or None"
        ):
            self.parse("--umask=022", *self.req_args)

    def test_inv_allow_shutdown(self) -> None:
        with self.assertRaisesRegex(
            usage.UsageError, "allow-shutdown needs to be one of 'signal' or 'file'"
        ):
            self.parse("--allow-shutdown=X", *self.req_args)

    def test_too_few_args(self) -> None:
        with self.assertRaisesRegex(usage.UsageError, "incorrect number of arguments"):
            self.parse("arg1", "arg2")

    def test_too_many_args(self) -> None:
        with self.assertRaisesRegex(usage.UsageError, "incorrect number of arguments"):
            self.parse("extra_arg", *self.req_args)

    def test_validateMasterArgument_no_port(self) -> None:
        """
        test calling CreateWorkerOptions.validateMasterArgument()
        on <master> argument without port specified.
        """
        opts = runner.CreateWorkerOptions()
        self.assertEqual(
            opts.validateMasterArgument("mstrhost"),
            ("mstrhost", 9989),
            "incorrect master host and/or port",
        )

    def test_validateMasterArgument_empty_master(self) -> None:
        """
        test calling CreateWorkerOptions.validateMasterArgument()
        on <master> without host part specified.
        """
        opts = runner.CreateWorkerOptions()
        with self.assertRaisesRegex(usage.UsageError, "invalid <master> argument ':1234'"):
            opts.validateMasterArgument(":1234")

    def test_validateMasterArgument_inv_port(self) -> None:
        """
        test calling CreateWorkerOptions.validateMasterArgument()
        on <master> without with unparsable port part
        """
        opts = runner.CreateWorkerOptions()
        with self.assertRaisesRegex(
            usage.UsageError, "invalid master port 'apple', needs to be a number"
        ):
            opts.validateMasterArgument("host:apple")

    def test_validateMasterArgument_ok(self) -> None:
        """
        test calling CreateWorkerOptions.validateMasterArgument()
        on <master> with host and port parts specified.
        """
        opts = runner.CreateWorkerOptions()
        self.assertEqual(
            opts.validateMasterArgument("mstrhost:4321"),
            ("mstrhost", 4321),
            "incorrect master host and/or port",
        )

    def test_validateMasterArgument_ipv4(self) -> None:
        """
        test calling CreateWorkerOptions.validateMasterArgument()
        on <master> with ipv4 host specified.
        """
        opts = runner.CreateWorkerOptions()
        self.assertEqual(
            opts.validateMasterArgument("192.168.0.0"),
            ("192.168.0.0", 9989),
            "incorrect master host and/or port",
        )

    def test_validateMasterArgument_ipv4_port(self) -> None:
        """
        test calling CreateWorkerOptions.validateMasterArgument()
        on <master> with ipv4 host and port parts specified.
        """
        opts = runner.CreateWorkerOptions()
        self.assertEqual(
            opts.validateMasterArgument("192.168.0.0:4321"),
            ("192.168.0.0", 4321),
            "incorrect master host and/or port",
        )

    def test_validateMasterArgument_ipv6(self) -> None:
        """
        test calling CreateWorkerOptions.validateMasterArgument()
        on <master> with ipv6 host specified.
        """
        opts = runner.CreateWorkerOptions()
        self.assertEqual(
            opts.validateMasterArgument("[2001:1:2:3:4::1]"),
            ("2001:1:2:3:4::1", 9989),
            "incorrect master host and/or port",
        )

    def test_validateMasterArgument_ipv6_port(self) -> None:
        """
        test calling CreateWorkerOptions.validateMasterArgument()
        on <master> with ipv6 host and port parts specified.
        """
        opts = runner.CreateWorkerOptions()
        self.assertEqual(
            opts.validateMasterArgument("[2001:1:2:3:4::1]:4321"),
            ("2001:1:2:3:4::1", 4321),
            "incorrect master host and/or port",
        )

    def test_validateMasterArgument_ipv6_no_bracket(self) -> None:
        """
        test calling CreateWorkerOptions.validateMasterArgument()
        on <master> with ipv6 without [] specified.
        """
        opts = runner.CreateWorkerOptions()
        with self.assertRaisesRegex(
            usage.UsageError,
            r"invalid <master> argument '2001:1:2:3:4::1:4321', "
            r"if it is an ipv6 address, it must be enclosed by \[\]",
        ):
            opts.validateMasterArgument("2001:1:2:3:4::1:4321")


class TestOptions(misc.StdoutAssertionsMixin, unittest.TestCase):
    """
    Test buildbot_worker.scripts.runner.Options class.
    """

    def setUp(self) -> None:
        self.setUpStdoutAssertions()

    def parse(self, *args: str) -> runner.Options:
        opts = runner.Options()
        opts.parseOptions(args)
        return opts

    def test_defaults(self) -> None:
        with self.assertRaisesRegex(usage.UsageError, "must specify a command"):
            self.parse()

    def test_version(self) -> None:
        exception = self.assertRaises(SystemExit, self.parse, '--version')
        self.assertEqual(exception.code, 0, "unexpected exit code")
        self.assertInStdout('worker version:')

    def test_verbose(self) -> None:
        self.patch(log, 'startLogging', mock.Mock())
        with self.assertRaises(usage.UsageError):
            self.parse("--verbose")
        cast("mock.Mock", log.startLogging).assert_called_once_with(sys.stderr)


# used by TestRun.test_run_good to patch in a callback
functionPlaceholder: Callable | None = None


class TestRun(misc.StdoutAssertionsMixin, unittest.TestCase):
    """
    Test buildbot_worker.scripts.runner.run()
    """

    def setUp(self) -> None:
        self.setUpStdoutAssertions()

    class TestSubCommand(usage.Options):
        subcommandFunction = __name__ + ".functionPlaceholder"
        optFlags = [["test-opt", None, None]]

    class TestOptions(usage.Options):
        """
        Option class that emulates usage error. The 'suboptions' flag
        enables emulation of usage error in a sub-option.
        """

        optFlags = [["suboptions", None, None]]

        def postOptions(self) -> NoReturn:
            if self["suboptions"]:
                self.subOptions = "SubOptionUsage"
            raise usage.UsageError("usage-error-message")

        def __str__(self) -> str:
            return "GeneralUsage"

    def test_run_good(self) -> None:
        """
        Test successful invocation of worker command.
        """

        self.patch(sys, "argv", ["command", 'test', '--test-opt'])

        # patch runner module to use our test subcommand class
        self.patch(runner.Options, 'subCommands', [['test', None, self.TestSubCommand, None]])

        # trace calls to subcommand function
        subcommand_func = mock.Mock(return_value=42)
        self.patch(sys.modules[__name__], "functionPlaceholder", subcommand_func)

        # check that subcommand function called with correct arguments
        # and that it's return value is used as exit code
        exception = self.assertRaises(SystemExit, runner.run)
        subcommand_func.assert_called_once_with({'test-opt': 1})
        self.assertEqual(exception.code, 42, "unexpected exit code")

    def test_run_bad_noargs(self) -> None:
        """
        Test handling of invalid command line arguments.
        """
        self.patch(sys, "argv", ["command"])

        # patch runner module to use test Options class
        self.patch(runner, "Options", self.TestOptions)

        exception = self.assertRaises(SystemExit, runner.run)
        self.assertEqual(exception.code, 1, "unexpected exit code")
        self.assertStdoutEqual(
            "command:  usage-error-message\n\nGeneralUsage\n",
            "unexpected error message on stdout",
        )

    def test_run_bad_suboption(self) -> None:
        """
        Test handling of invalid command line arguments in a suboption.
        """

        self.patch(sys, "argv", ["command", "--suboptions"])

        # patch runner module to use test Options class
        self.patch(runner, "Options", self.TestOptions)

        exception = self.assertRaises(SystemExit, runner.run)
        self.assertEqual(exception.code, 1, "unexpected exit code")

        # check that we get error message for a sub-option
        self.assertStdoutEqual(
            "command:  usage-error-message\n\nSubOptionUsage\n",
            "unexpected error message on stdout",
        )
