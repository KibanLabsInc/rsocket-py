import asyncio

from quart import websocket

from rsocket.frame import Frame
from rsocket.logger import logger
from rsocket.rsocket_server import RSocketServer
from rsocket.transports.abstract_messaging import AbstractMessagingTransport


async def websocket_handler(*args, on_server_create=None, **kwargs):
    transport = TransportQuartWebsocket()
    server = RSocketServer(transport, *args, **kwargs)

    if on_server_create is not None:
        on_server_create(server)

    await transport.handle_incoming_ws_messages()


class TransportQuartWebsocket(AbstractMessagingTransport):

    async def handle_incoming_ws_messages(self):
        try:
            while True:
                data = await websocket.receive()

                async for frame in self._frame_parser.receive_data(data, 0):
                    self._incoming_frame_queue.put_nowait(frame)
        except asyncio.CancelledError:
            logger().debug('Asyncio task canceled: quart_handle_incoming_ws_messages')

    async def send_frame(self, frame: Frame):
        await websocket.send(frame.serialize())

    async def close(self):
        pass
