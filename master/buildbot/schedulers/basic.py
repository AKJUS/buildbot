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

from collections import defaultdict
from typing import TYPE_CHECKING
from typing import ClassVar

from twisted.internet import defer
from twisted.python import log
from twisted.python.deprecate import deprecatedModuleAttribute
from twisted.python.versions import Version

from buildbot import config
from buildbot import util
from buildbot.changes import changes
from buildbot.changes.filter import ChangeFilter
from buildbot.schedulers import base
from buildbot.schedulers import dependent
from buildbot.util import NotABranch
from buildbot.util.codebase import AbsoluteSourceStampsMixin
from buildbot.warnings import warn_deprecated

if TYPE_CHECKING:
    from collections.abc import Sequence


class BaseBasicScheduler(base.ReconfigurableBaseScheduler):
    """
    @param onlyImportant: If True, only important changes will be added to the
                          buildset.
    @type onlyImportant: boolean

    """

    compare_attrs: ClassVar[Sequence[str]] = (
        'treeStableTimer',
        'change_filter',
        'fileIsImportant',
        'onlyImportant',
        'reason',
    )

    fileIsImportant = None
    reason = ''

    class NotSet:
        pass

    def __init__(self, name, **kwargs):
        super().__init__(name=name, **kwargs)
        # the IDelayedCall used to wake up when this scheduler's
        # treeStableTimer expires.
        self._stable_timers = defaultdict(lambda: None)
        self._stable_timers_lock = defer.DeferredLock()

    def checkConfig(
        self,
        shouldntBeSet=NotSet,
        treeStableTimer=None,
        builderNames=None,
        branch=NotABranch,
        branches=NotABranch,
        fileIsImportant=None,
        categories=None,
        reason="The %(classname)s scheduler named '%(name)s' triggered this build",
        change_filter=None,
        onlyImportant=False,
        **kwargs,
    ):
        if shouldntBeSet is not self.NotSet:
            config.error("pass arguments to schedulers using keyword arguments")
        if fileIsImportant and not callable(fileIsImportant):
            config.error("fileIsImportant must be a callable")

        super().checkConfig(builderNames=builderNames, **kwargs)

    @defer.inlineCallbacks
    def reconfigService(
        self,
        shouldntBeSet=NotSet,
        treeStableTimer=None,
        builderNames=None,
        branch=NotABranch,
        branches=NotABranch,
        fileIsImportant=None,
        categories=None,
        reason="The %(classname)s scheduler named '%(name)s' triggered this build",
        change_filter=None,
        onlyImportant=False,
        **kwargs,
    ):
        yield super().reconfigService(builderNames=builderNames, **kwargs)

        self.treeStableTimer = treeStableTimer
        self.fileIsImportant = fileIsImportant
        self.onlyImportant = onlyImportant
        self.change_filter = self.getChangeFilter(
            branch=branch, branches=branches, change_filter=change_filter, categories=categories
        )

        self.reason = util.bytes2unicode(
            reason % {'name': self.name, 'classname': self.__class__.__name__}
        )

        if self.active:
            yield self._stopConsumingChanges()
            yield self.startConsumingChanges(
                fileIsImportant=self.fileIsImportant,
                change_filter=self.change_filter,
                onlyImportant=self.onlyImportant,
            )

    def getChangeFilter(self, branch, branches, change_filter, categories):
        raise NotImplementedError

    @defer.inlineCallbacks
    def activate(self):
        yield super().activate()

        if not self.enabled:
            return

        yield self.startConsumingChanges(
            fileIsImportant=self.fileIsImportant,
            change_filter=self.change_filter,
            onlyImportant=self.onlyImportant,
        )

        # if we have a treeStableTimer, if there are classified changes
        # out there, start their timers again
        if self.treeStableTimer:
            yield self.scanExistingClassifiedChanges()

        # otherwise, we don't care about classified
        # changes, so get rid of any hanging around from previous
        # configurations
        else:
            yield self.master.db.schedulers.flushChangeClassifications(self.serviceid)

    @defer.inlineCallbacks
    def deactivate(self):
        # the base deactivate will unsubscribe from new changes
        yield super().deactivate()

        if not self.enabled:
            return

        @util.deferredLocked(self._stable_timers_lock)
        def cancel_timers():
            for timer in self._stable_timers.values():
                if timer:
                    timer.cancel()
            self._stable_timers.clear()

        yield cancel_timers()

    @util.deferredLocked('_stable_timers_lock')
    def gotChange(self, change, important):
        if not self.treeStableTimer:
            # if there's no treeStableTimer, we can completely ignore
            # unimportant changes
            if not important:
                return defer.succeed(None)
            # otherwise, we'll build it right away
            return self.addBuildsetForChanges(
                reason=self.reason, changeids=[change.number], priority=self.priority
            )

        timer_name = self.getTimerNameForChange(change)

        # if we have a treeStableTimer
        # - for an important change, start the timer
        # - for an unimportant change, reset the timer if it is running

        if important or self._stable_timers[timer_name]:
            if self._stable_timers[timer_name]:
                self._stable_timers[timer_name].cancel()

            def fire_timer():
                d = self.stableTimerFired(timer_name)
                d.addErrback(log.err, "while firing stable timer")

            self._stable_timers[timer_name] = self.master.reactor.callLater(
                self.treeStableTimer, fire_timer
            )

        # record the change's importance
        return self.master.db.schedulers.classifyChanges(self.serviceid, {change.number: important})

    @defer.inlineCallbacks
    def scanExistingClassifiedChanges(self):
        # call gotChange for each classified change.  This is called at startup
        # and is intended to re-start the treeStableTimer for any changes that
        # had not yet been built when the scheduler was stopped.

        # NOTE: this may double-call gotChange for changes that arrive just as
        # the scheduler starts up.  In practice, this doesn't hurt anything.
        classifications = yield self.master.db.schedulers.getChangeClassifications(self.serviceid)

        # call gotChange for each change, after first fetching it from the db
        for changeid, important in classifications.items():
            chdict = yield self.master.db.changes.getChange(changeid)

            if not chdict:
                continue

            change = yield changes.Change.fromChdict(self.master, chdict)
            yield self.gotChange(change, important)

    def getTimerNameForChange(self, change):
        raise NotImplementedError  # see subclasses

    def getChangeClassificationsForTimer(self, sched_id, timer_name):
        """similar to db.schedulers.getChangeClassifications, but given timer
        name"""
        raise NotImplementedError  # see subclasses

    @util.deferredLocked('_stable_timers_lock')
    @defer.inlineCallbacks
    def stableTimerFired(self, timer_name):
        # delete this now-fired timer, if the service has already been stopped
        # then just bail out
        if not self._stable_timers.pop(timer_name, None):
            return

        classifications = yield self.getChangeClassificationsForTimer(self.serviceid, timer_name)

        # just in case: databases do weird things sometimes!
        if not classifications:  # pragma: no cover
            return

        changeids = sorted(classifications.keys())
        yield self.addBuildsetForChanges(
            reason=self.reason, changeids=changeids, priority=self.priority
        )

        max_changeid = changeids[-1]  # (changeids are sorted)
        yield self.master.db.schedulers.flushChangeClassifications(
            self.serviceid, less_than=max_changeid + 1
        )


class SingleBranchScheduler(AbsoluteSourceStampsMixin, BaseBasicScheduler):
    createAbsoluteSourceStamps = None

    def checkConfig(self, createAbsoluteSourceStamps=False, **kwargs):
        super().checkConfig(**kwargs)

    @defer.inlineCallbacks
    def reconfigService(
        self,
        createAbsoluteSourceStamps=False,
        **kwargs,
    ):
        self.createAbsoluteSourceStamps = createAbsoluteSourceStamps
        yield super().reconfigService(**kwargs)

    @defer.inlineCallbacks
    def gotChange(self, change, important):
        if self.createAbsoluteSourceStamps:
            yield self.recordChange(change)

        yield super().gotChange(change, important)

    def getCodebaseDict(self, codebase):
        if self.createAbsoluteSourceStamps:
            return super().getCodebaseDict(codebase)
        return self.codebases[codebase]

    def getChangeFilter(self, branch, branches, change_filter, categories):
        if branch is NotABranch and not change_filter:
            config.error(
                "the 'branch' argument to SingleBranchScheduler is "
                + "mandatory unless change_filter is provided"
            )
        elif branches is not NotABranch:
            config.error("the 'branches' argument is not allowed for " + "SingleBranchScheduler")

        return ChangeFilter.fromSchedulerConstructorArgs(
            change_filter=change_filter, branch=branch, categories=categories
        )

    def getTimerNameForChange(self, change):
        return "only"  # this class only uses one timer

    def getChangeClassificationsForTimer(self, sched_id, timer_name):
        return self.master.db.schedulers.getChangeClassifications(sched_id)


class Scheduler(SingleBranchScheduler):
    "alias for SingleBranchScheduler"

    def __init__(self, *args, **kwargs):
        warn_deprecated(
            '4.3.0',
            (
                'the name \'Scheduler\' is deprecated; use '
                'buildbot.schedulers.basic.SingleBranchScheduler instead'
            ),
        )
        super().__init__(*args, **kwargs)


class AnyBranchScheduler(BaseBasicScheduler):
    def getChangeFilter(self, branch, branches, change_filter, categories):
        assert branch is NotABranch
        return ChangeFilter.fromSchedulerConstructorArgs(
            change_filter=change_filter, branch=branches, categories=categories
        )

    def getTimerNameForChange(self, change):
        # Py2.6+: could be a namedtuple
        return (change.codebase, change.project, change.repository, change.branch)

    def getChangeClassificationsForTimer(self, sched_id, timer_name):
        # set in getTimerNameForChange
        codebase, project, repository, branch = timer_name
        return self.master.db.schedulers.getChangeClassifications(
            sched_id, branch=branch, repository=repository, codebase=codebase, project=project
        )


# now at buildbot.schedulers.dependent, but keep the old name alive
Dependent = dependent.Dependent

deprecatedModuleAttribute(
    Version('buildbot', 4, 3, 0),
    message='Use buildbot.schedulers.dependent.Dependent',
    moduleName='buildbot.schedulers.basic',
    name='Dependent',
)
