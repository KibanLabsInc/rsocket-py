import asyncio
from asyncio import Task
from contextlib import contextmanager
from typing import Any
from typing import TypeVar
from typing import Union, Callable, Optional, Tuple

from reactivestreams.publisher import DefaultPublisher
from reactivestreams.subscriber import Subscriber
from reactivestreams.subscription import DefaultSubscription
from rsocket.exceptions import RSocketTransportError
from rsocket.frame import Frame
from rsocket.frame_helpers import serialize_128max_value, parse_type
from rsocket.logger import logger
from rsocket.payload import Payload

_default = object()
V = TypeVar('V')


def create_future(value: Optional[Any] = _default) -> asyncio.Future:
    future = asyncio.get_event_loop().create_future()

    if value is not _default:
        future.set_result(value)

    return future


def create_error_future(exception: Exception) -> asyncio.Future:
    future = create_future()
    future.set_exception(exception)
    return future


def payload_from_frame(frame: Frame) -> Payload:
    return Payload(frame.data, frame.metadata)


class DefaultPublisherSubscription(DefaultPublisher, DefaultSubscription):
    def subscribe(self, subscriber: Subscriber):
        super().subscribe(subscriber)
        subscriber.on_subscribe(self)


class WellKnownType:
    __slots__ = (
        'name',
        'id'
    )

    def __init__(self, name: bytes, id_: int):
        self.name = name
        self.id = id_

    def __eq__(self, other):
        return self.name == other.name and self.id == other.id

    def __hash__(self):
        return hash((self.id, self.name))


@contextmanager
def wrap_transport_exception():
    try:
        yield
    except Exception as exception:
        raise RSocketTransportError from exception


async def single_transport_provider(transport):
    yield transport


# noinspection PyUnusedLocal
async def async_noop(*args, **kwargs):
    pass


# noinspection PyUnusedLocal
def noop(*args, **kwargs):
    pass


def serialize_well_known_encoding(
        encoding: Union[bytes, WellKnownType],
        encoding_parser: Callable[[bytes], Optional[WellKnownType]]) -> bytes:
    if isinstance(encoding, (bytes, bytearray, str)):
        known_type = encoding_parser(encoding)
    else:
        known_type = encoding

    if known_type is None:
        serialized = serialize_128max_value(encoding)
    else:
        serialized = ((1 << 7) | known_type.id & 0b1111111).to_bytes(1, 'big')

    return serialized


def parse_well_known_encoding(buffer: bytes, encoding_name_provider: Callable[[WellKnownType], V]) -> Tuple[bytes, int]:
    is_known_mime_id, mime_length_or_type = parse_type(buffer)

    if is_known_mime_id:
        metadata_encoding = encoding_name_provider(mime_length_or_type).name
        offset = 1
    else:
        real_mime_type_length = mime_length_or_type + 1  # mime length cannot be 0
        metadata_encoding = bytes(buffer[1:1 + real_mime_type_length])
        offset = 1 + real_mime_type_length

    return metadata_encoding, offset


async def cancel_if_task_exists(task: Optional[Task]):
    if task is not None and not task.done():
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            logger().debug('Asyncio task cancellation error: %s', task)
        except RuntimeError:
            logger().warning('Runtime error canceling task: %s', task, exc_info=True)
