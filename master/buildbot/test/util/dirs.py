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


import os
import shutil
from typing import TYPE_CHECKING

from twisted.internet import defer
from twisted.trial import unittest

if TYPE_CHECKING:
    from twisted.trial import unittest

    _DirsMixinBase = unittest.TestCase
else:
    _DirsMixinBase = object


class DirsMixin(_DirsMixinBase):
    _dirs = None

    def setUpDirs(self, *dirs) -> defer.Deferred[None]:
        """Make sure C{dirs} exist and are empty, and set them up to be deleted
        in tearDown."""
        self._dirs = map(os.path.abspath, dirs)
        for dir in self._dirs:
            if os.path.exists(dir):
                shutil.rmtree(dir)
            os.makedirs(dir)

        def cleanup():
            for dir in self._dirs:
                if os.path.exists(dir):
                    shutil.rmtree(dir)

        self.addCleanup(cleanup)

        # return a deferred to make chaining easier
        return defer.succeed(None)
