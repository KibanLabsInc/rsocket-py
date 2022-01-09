import asyncio
import logging

from examples.response_channel import ResponseChannel
from response_stream import ResponseStream
from rsocket import RSocket, Payload
from rsocket.routing.request_router import RequestRouter
from rsocket.routing.routing_request_handler import RoutingRequestHandler

router = RequestRouter()


@router.response('single_request')
def single_request(socket, payload, composite_metadata):
    print('Got single request')
    future = asyncio.Future()
    future.set_result(Payload(b'single_response'))
    return future


@router.stream('stream')
def stream(socket, payload, composite_metadata):
    return ResponseStream()


@router.fire_and_forget('no_response')
def no_response(socket, payload, composite_metadata):
    print('No response sent to client')


@router.channel('channel')
def channel(socket, payload, composite_metadata):
    return ResponseChannel()


def handler_factory(socket):
    return RoutingRequestHandler(socket, router)


def handle_client(reader, writer):
    RSocket(reader, writer, handler_factory=handler_factory, server=True)


async def run_server():
    logging.basicConfig(level=logging.DEBUG)

    server = await asyncio.start_server(handle_client, 'localhost', 6565)

    async with server:
        await server.serve_forever()


asyncio.run(run_server())
