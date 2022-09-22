"""MIT License

Copyright (c) 2019-2020 PythonistaGuild

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
import aiohttp
import asyncio
import logging
import sys
import traceback
from typing import Any, Dict

from .backoff import ExponentialBackoff
from .events import *
from .stats import Stats


__log__ = logging.getLogger(__name__)


class WebSocket:

    def __init__(self, **attrs):
        self._node = attrs.get('node')
        self.client = self._node._client
        self.bot = self.client.bot
        self.host = attrs.get('host')
        self.port = attrs.get('port')
        self.password = attrs.get('password')
        self.shard_count = attrs.get('shard_count')
        self.user_id = attrs.get('user_id')
        self.secure = attrs.get('secure')
        self.user_agent = attrs.get('user_agent') or ''
        self.auto_reconnect = attrs.get('auto_reconnect', True)
        self._dumps = attrs.get('dumps')

        self._websocket = None
        self._last_exc = None
        self._task = None

    @property
    def headers(self):
        return {'Authorization': self.password,
                'Resume-Key': self._node.resume_key,
                'User-Id': str(self.user_id),
                'Client-Name': 'Wavelink',
                'User-Agent': self.user_agent}

    @property
    def is_connected(self) -> bool:
        return self._websocket is not None and not self._websocket.closed

    async def _connect(self):
        await self.bot.wait_until_ready()

        try:
            if self.secure is True:
                uri = f'wss://{self.host}:{self.port}'
            else:
                uri = f'ws://{self.host}:{self.port}'

            if not self.is_connected:
                self._websocket = await self._node.session.ws_connect(uri, headers=self.headers, heartbeat=self._node.heartbeat)

        except Exception as error:
            self._last_exc = error
            self._node.available = False

            if isinstance(error, aiohttp.WSServerHandshakeError) and error.status == 401:
                print(f'\nAuthorization Failed for Node:: {self._node}\n', file=sys.stderr)
            else:
                __log__.error(f'WEBSOCKET | Connection Failure:: {error}')
                traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)
            return

        if not self._task:
            self._task = self.bot.loop.create_task(self._listen())

        self._last_exc = None
        self._closed = False
        self._node.available = True

        if self.is_connected:
            self.bot.dispatch('wavelink_node_ready', self._node)
            __log__.debug('WEBSOCKET | Connection established...%s', self._node.__repr__())

    async def _listen(self):
        backoff = ExponentialBackoff(base=7)

        while True:
            msg = await self._websocket.receive()

            if msg.type is aiohttp.WSMsgType.CLOSED:

                self._closed = True

                if not self.auto_reconnect:
                    self.bot.dispatch('wavelink_node_connection_closed', self._node)
                    try:
                        self._task.cancel()
                    except:
                        pass
                    self._task = None
                    return

                __log__.debug(f'WEBSOCKET | Close data: {msg.extra}')

                retry = backoff.delay()

                __log__.warning(f'\nWEBSOCKET | Connection closed:: Retrying connection in <{retry}> seconds\n')

                await asyncio.sleep(retry)
                if not self.is_connected:
                    self.bot.loop.create_task(self._connect())
            else:
                __log__.debug(f'WEBSOCKET | Received Payload:: <{msg.data}>')
                self.bot.loop.create_task(self.process_data(msg.json()))

    async def process_data(self, data: Dict[str, Any]):
        op = data.get('op', None)
        if not op:
            return

        if op == 'stats':
            self._node.stats = Stats(self._node, data)
        if op == 'event':
            try:
                data['player'] = self._node.players[int(data['guildId'])]
            except KeyError:
                return

            listener, payload = self._get_event_payload(data['type'], data)

            __log__.debug(f'WEBSOCKET | op: event:: {data}')

            # Dispatch node event/player hooks
            try:
                self.bot.dispatch(listener, self._node, payload)
            except Exception as e:
                traceback.print_exception(type(e), e, e.__traceback__, file=sys.stderr)

        elif op == 'playerUpdate':
            __log__.debug(f'WEBSOCKET | op: playerUpdate:: {data}')
            try:
                await self._node.players[int(data['guildId'])].update_state(data)
            except KeyError:
                pass

    def _get_event_payload(self, name: str, data):
        if name == 'TrackEndEvent':
            return 'wavelink_track_end', TrackEnd(data)
        elif name == 'TrackStartEvent':
            return 'wavelink_track_start', TrackStart(data)
        elif name == 'TrackExceptionEvent':
            return 'wavelink_track_exception', TrackException(data)
        elif name == 'TrackStuckEvent':
            return 'wavelink_track_stuck', TrackStuck(data)
        elif name == 'WebSocketClosedEvent':
            return 'wavelink_websocket_closed', WebsocketClosed(data)

    async def _send(self, **data):
        if self.is_connected:
            __log__.debug(f'WEBSOCKET | Sending Payload:: {data}')
            data_str = self._dumps(data)
            if isinstance(data_str, bytes):
                # Some JSON libraries serialize to bytes
                # Yet Lavalink does not support binary websockets
                # So we need to decode. In the future, maybe
                # self._websocket.send_bytes could be used
                # if Lavalink ever implements it
                data_str = data_str.decode('utf-8')
            await self._websocket.send_str(data_str)