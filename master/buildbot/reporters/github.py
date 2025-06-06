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

import re
from typing import TYPE_CHECKING

from twisted.internet import defer
from twisted.python import log

from buildbot.process.properties import Interpolate
from buildbot.process.properties import Properties
from buildbot.process.results import CANCELLED
from buildbot.process.results import EXCEPTION
from buildbot.process.results import FAILURE
from buildbot.process.results import RETRY
from buildbot.process.results import SKIPPED
from buildbot.process.results import SUCCESS
from buildbot.process.results import WARNINGS
from buildbot.reporters.base import ReporterBase
from buildbot.reporters.generators.build import BuildStartEndStatusGenerator
from buildbot.reporters.generators.buildrequest import BuildRequestGenerator
from buildbot.reporters.message import MessageFormatterRenderable
from buildbot.util import httpclientservice
from buildbot.util.giturlparse import giturlparse

if TYPE_CHECKING:
    from collections.abc import Generator

HOSTED_BASE_URL = 'https://api.github.com'


class GitHubStatusPush(ReporterBase):
    name: str | None = "GitHubStatusPush"  # type: ignore[assignment]

    def checkConfig(
        self,
        token,
        context=None,
        baseURL=None,
        verbose=False,
        debug=None,
        verify=None,
        generators=None,
        **kwargs,
    ):
        if generators is None:
            generators = self._create_default_generators()

        super().checkConfig(generators=generators, **kwargs)

    @defer.inlineCallbacks
    def reconfigService(
        self,
        token,
        context=None,
        baseURL=None,
        verbose=False,
        debug=None,
        verify=None,
        generators=None,
        **kwargs,
    ):
        self.token = token
        self.debug = debug
        self.verify = verify
        self.verbose = verbose
        self.context = self.setup_context(context)

        if generators is None:
            generators = self._create_default_generators()

        yield super().reconfigService(generators=generators, **kwargs)

        if baseURL is None:
            baseURL = HOSTED_BASE_URL
        if baseURL.endswith('/'):
            baseURL = baseURL[:-1]

        self._http = yield httpclientservice.HTTPSession(
            self.master.httpservice,
            baseURL,
            headers={'User-Agent': 'Buildbot'},
            debug=self.debug,
            verify=self.verify,
        )

    def setup_context(self, context):
        return context or Interpolate('buildbot/%(prop:buildername)s')

    def _create_default_generators(self):
        start_formatter = MessageFormatterRenderable('Build started.')
        end_formatter = MessageFormatterRenderable('Build done.')
        pending_formatter = MessageFormatterRenderable('Build pending.')

        return [
            BuildRequestGenerator(formatter=pending_formatter),
            BuildStartEndStatusGenerator(
                start_formatter=start_formatter, end_formatter=end_formatter
            ),
        ]

    @defer.inlineCallbacks
    def _get_auth_header(
        self, props: Properties
    ) -> Generator[defer.Deferred[str], None, dict[str, str]]:
        token = yield props.render(self.token)
        return {'Authorization': f"token {token}"}

    @defer.inlineCallbacks
    def createStatus(
        self,
        repo_user,
        repo_name,
        sha,
        state,
        props,
        target_url=None,
        context=None,
        issue=None,
        description=None,
    ):
        """
        :param repo_user: GitHub user or organization
        :param repo_name: Name of the repository
        :param sha: Full sha to create the status for.
        :param state: one of the following 'pending', 'success', 'error'
                      or 'failure'.
        :param target_url: Target url to associate with this status.
        :param context: Build context
        :param issue: Pull request number
        :param description: Short description of the status.
        :param props: Properties object of the build (used for render GITHUB_TOKEN secret)
        :return: A deferred with the result from GitHub.

        This code comes from txgithub by @tomprince.
        txgithub is based on twisted's webclient agent, which is much less reliable and featureful
        as txrequest (support for proxy, connection pool, keep alive, retry, etc)
        """
        payload = {'state': state}

        if description is not None:
            payload['description'] = description

        if target_url is not None:
            payload['target_url'] = target_url

        if context is not None:
            payload['context'] = context

        headers = yield self._get_auth_header(props)
        ret = yield self._http.post(
            '/'.join(['/repos', repo_user, repo_name, 'statuses', sha]),
            json=payload,
            headers=headers,
        )
        return ret

    def is_status_2xx(self, code):
        return code // 100 == 2

    def _extract_issue(self, props):
        branch = props.getProperty('branch')
        if branch:
            m = re.search(r"refs/pull/([0-9]*)/(head|merge)", branch)
            if m:
                return m.group(1)
        return None

    def _extract_github_info(self, sourcestamp):
        repo_owner = None
        repo_name = None
        project = sourcestamp['project']
        repository = sourcestamp['repository']
        if project and "/" in project:
            repo_owner, repo_name = project.split('/')
        elif repository:
            giturl = giturlparse(repository)
            if giturl:
                repo_owner = giturl.owner
                repo_name = giturl.repo

        return repo_owner, repo_name

    @defer.inlineCallbacks
    def sendMessage(self, reports):
        report = reports[0]
        build = reports[0]['builds'][0]

        props = Properties.fromDict(build['properties'])
        props.master = self.master

        description = report.get('body', None)

        if build['complete']:
            state = {
                SUCCESS: 'success',
                WARNINGS: 'success',
                FAILURE: 'failure',
                SKIPPED: 'success',
                EXCEPTION: 'error',
                RETRY: 'pending',
                CANCELLED: 'error',
            }.get(build['results'], 'error')
        else:
            state = 'pending'

        context = yield props.render(self.context)

        sourcestamps = build['buildset'].get('sourcestamps')
        if not sourcestamps:
            return

        issue = self._extract_issue(props)

        for sourcestamp in sourcestamps:
            repo_owner, repo_name = self._extract_github_info(sourcestamp)

            if not repo_owner or not repo_name:
                log.msg('Skipped status update because required repo information is missing.')
                continue

            sha = sourcestamp['revision']
            response = None

            # If the scheduler specifies multiple codebases, don't bother updating
            # the ones for which there is no revision
            if not sha:
                log.msg(
                    f"Skipped status update for codebase {sourcestamp['codebase']}, "
                    f"context '{context}', issue {issue}."
                )
                continue

            try:
                if self.verbose:
                    log.msg(
                        f"Updating github status: repo_owner={repo_owner}, repo_name={repo_name}"
                    )

                response = yield self.createStatus(
                    repo_user=repo_owner,
                    repo_name=repo_name,
                    sha=sha,
                    state=state,
                    target_url=build['url'],
                    context=context,
                    issue=issue,
                    description=description,
                    props=props,
                )

                if not response:
                    # the implementation of createStatus refused to post update due to missing data
                    continue

                if not self.is_status_2xx(response.code):
                    raise RuntimeError()

                if self.verbose:
                    log.msg(
                        f'Updated status with "{state}" for {repo_owner}/{repo_name} '
                        f'at {sha}, context "{context}", issue {issue}.'
                    )
            except Exception as e:
                if response:
                    content = yield response.content()
                    code = response.code
                else:
                    content = code = "n/a"
                log.err(
                    e,
                    (
                        f'Failed to update "{state}" for {repo_owner}/{repo_name} '
                        f'at {sha}, context "{context}", issue {issue}. '
                        f'http {code}, {content}'
                    ),
                )


class GitHubCommentPush(GitHubStatusPush):
    name = "GitHubCommentPush"

    def setup_context(self, context):
        return ''

    def _create_default_generators(self):
        start_formatter = MessageFormatterRenderable(None)
        end_formatter = MessageFormatterRenderable('Build done.')

        return [
            BuildStartEndStatusGenerator(
                start_formatter=start_formatter, end_formatter=end_formatter
            )
        ]

    @defer.inlineCallbacks
    def sendMessage(self, reports):
        report = reports[0]
        if 'body' not in report or report['body'] is None:
            return
        yield super().sendMessage(reports)

    @defer.inlineCallbacks
    def createStatus(
        self,
        repo_user,
        repo_name,
        sha,
        state,
        props,
        target_url=None,
        context=None,
        issue=None,
        description=None,
    ):
        """
        :param repo_user: GitHub user or organization
        :param repo_name: Name of the repository
        :param sha: Full sha to create the status for.
        :param state: unused
        :param target_url: unused
        :param context: unused
        :param issue: Pull request number
        :param description: Short description of the status.
        :param props: Properties object of the build (used for render GITHUB_TOKEN secret)
        :return: A deferred with the result from GitHub.

        This code comes from txgithub by @tomprince.
        txgithub is based on twisted's webclient agent, which is much less reliable and featureful
        as txrequest (support for proxy, connection pool, keep alive, retry, etc)
        """
        payload = {'body': description}

        if issue is None:
            log.msg(
                f'Skipped status update for repo {repo_name} sha {sha} as issue is not specified'
            )
            return None

        url = '/'.join(['/repos', repo_user, repo_name, 'issues', issue, 'comments'])
        headers = yield self._get_auth_header(props)
        ret = yield self._http.post(url, json=payload, headers=headers)
        return ret
