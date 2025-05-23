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

import json
import uuid
from typing import TYPE_CHECKING
from typing import Any

from twisted.python import log
from twisted.web import resource
from twisted.web import server

from buildbot.data.exceptions import InvalidPathError
from buildbot.util import bytes2unicode
from buildbot.util import toJson
from buildbot.util import unicode2bytes

if TYPE_CHECKING:
    from buildbot.master import BuildMaster
    from buildbot.mq.base import QueueRef


class Consumer:
    qrefs: dict[bytes, QueueRef]

    def __init__(self, request: server.Request):
        self.request = request
        self.qrefs = {}

    def stopConsuming(self, key: bytes | None = None) -> None:
        if key is not None:
            self.qrefs[key].stopConsuming()
        else:
            for qref in self.qrefs.values():
                qref.stopConsuming()
            self.qrefs = {}

    def onMessage(self, event: list[str], data: Any) -> None:
        request = self.request
        key = [bytes2unicode(e) for e in event]
        msg = {"key": key, "message": data}
        request.write(b"event: " + b"event" + b"\n")
        request.write(b"data: " + unicode2bytes(json.dumps(msg, default=toJson)) + b"\n")
        request.write(b"\n")

    def registerQref(self, path: bytes, qref: QueueRef) -> None:
        self.qrefs[path] = qref


class EventResource(resource.Resource):
    isLeaf = True
    consumers: dict[bytes, Consumer]

    def __init__(self, master: BuildMaster):
        super().__init__()

        self.master = master
        self.consumers = {}

    def decodePath(self, path: list[bytes]) -> list[bytes | None]:
        return [None if p == b'*' else p for p in path]

    def finish(self, request: server.Request, code: int, msg: bytes) -> None:
        request.setResponseCode(code)
        request.setHeader(b'content-type', b'text/plain; charset=utf-8')
        request.write(msg)

    def render(self, request: server.Request) -> int | None:
        consumer: Consumer | None = None
        command = b"listen"
        path: list[bytes] | None = request.postpath
        assert path is not None

        if path and path[-1] == b'':
            path = path[:-1]

        if path and path[0] in (b"listen", b"add", b"remove"):
            command = path[0]
            path = path[1:]

        if command == b"listen":
            cid = unicode2bytes(str(uuid.uuid4()))
            consumer = Consumer(request)

        elif command in (b"add", b"remove"):
            if path:
                cid = path[0]
                path = path[1:]
                if cid not in self.consumers:
                    self.finish(request, 400, b"unknown uuid")
                    return None
                consumer = self.consumers[cid]
            else:
                self.finish(request, 400, b"need uuid")
                return None

        assert consumer is not None

        pathref = b"/".join(path)
        decoded_path = self.decodePath(path)

        if command == b"add" or (command == b"listen" and decoded_path):
            options = request.args
            assert options is not None
            for k in options:
                if len(options[k]) == 1:
                    options[k] = options[k][1]

            try:
                d = self.master.mq.startConsuming(
                    consumer.onMessage, tuple(bytes2unicode(p) for p in decoded_path)
                )

                @d.addCallback
                def register(qref: QueueRef) -> None:
                    consumer.registerQref(pathref, qref)

                d.addErrback(log.err, "while calling startConsuming")
            except NotImplementedError:
                self.finish(request, 404, b"not implemented")
                return None
            except InvalidPathError:
                self.finish(request, 404, b"not implemented")
                return None
        elif command == b"remove":
            try:
                consumer.stopConsuming(pathref)
            except KeyError:
                self.finish(request, 404, b"consumer is not listening to this event")
                return None

        if command == b"listen":
            self.consumers[cid] = consumer
            request.setHeader(b"content-type", b"text/event-stream")
            request.write(b"")
            request.write(b"event: handshake\n")
            request.write(b"data: " + cid + b"\n")
            request.write(b"\n")
            d = request.notifyFinish()

            @d.addBoth
            def onEndRequest(_: Any) -> None:
                consumer.stopConsuming()
                del self.consumers[cid]

            return server.NOT_DONE_YET

        self.finish(request, 200, b"ok")
        return None
