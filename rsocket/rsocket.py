import abc
import asyncio
from asyncio import Future, QueueEmpty, Task
from datetime import timedelta
from typing import Union, Optional, Dict, Any, Coroutine, Callable, Type, cast, TypeVar

from reactivestreams.publisher import Publisher
from reactivestreams.subscriber import DefaultSubscriber
from rsocket.datetime_helpers import to_milliseconds
from rsocket.error_codes import ErrorCode
from rsocket.exceptions import RSocketProtocolException
from rsocket.extensions.mimetypes import WellKnownMimeTypes, ensure_encoding_name
from rsocket.frame import KeepAliveFrame, \
    MetadataPushFrame, RequestFireAndForgetFrame, RequestResponseFrame, \
    RequestStreamFrame, Frame, exception_to_error_frame, LeaseFrame, ErrorFrame, RequestFrame, \
    initiate_request_frame_types, InvalidFrame, FragmentableFrame
from rsocket.frame import RequestChannelFrame, ResumeFrame, is_fragmentable_frame, CONNECTION_STREAM_ID
from rsocket.frame import SetupFrame
from rsocket.frame_builders import to_payload_frame, to_fire_and_forget_frame
from rsocket.frame_fragment_cache import FrameFragmentCache
from rsocket.frame_logger import log_frame
from rsocket.handlers.request_cahnnel_responder import RequestChannelResponder
from rsocket.handlers.request_channel_requester import RequestChannelRequester
from rsocket.handlers.request_response_requester import RequestResponseRequester
from rsocket.handlers.request_response_responder import RequestResponseResponder
from rsocket.handlers.request_stream_requester import RequestStreamRequester
from rsocket.handlers.request_stream_responder import RequestStreamResponder
from rsocket.helpers import payload_from_frame
from rsocket.lease import DefinedLease, NullLease, Lease
from rsocket.logger import logger
from rsocket.payload import Payload
from rsocket.request_handler import BaseRequestHandler, RequestHandler
from rsocket.rsocket_interface import RSocketInterface
from rsocket.stream_control import StreamControl
from rsocket.streams.backpressureapi import BackpressureApi
from rsocket.streams.stream_handler import StreamHandler
from rsocket.transports.transport import Transport


async def noop_frame_handler(frame):
    pass


T = TypeVar('T')


class RSocket(RSocketInterface):
    class LeaseSubscriber(DefaultSubscriber):
        def __init__(self, socket: 'RSocket'):
            super().__init__()
            self._socket = socket

        def on_next(self, value, is_complete=False):
            self._socket.send_lease(value)

    def __init__(self,
                 transport: Transport,
                 handler_factory: Type[RequestHandler] = BaseRequestHandler,
                 honor_lease=False,
                 lease_publisher: Optional[Publisher] = None,
                 request_queue_size: int = 0,
                 data_encoding: Union[str, bytes, WellKnownMimeTypes] = WellKnownMimeTypes.APPLICATION_JSON,
                 metadata_encoding: Union[str, bytes, WellKnownMimeTypes] = WellKnownMimeTypes.APPLICATION_JSON,
                 keep_alive_period: timedelta = timedelta(milliseconds=500),
                 max_lifetime_period: timedelta = timedelta(minutes=10),
                 setup_payload: Optional[Payload] = None
                 ):

        self._transport = transport
        self._handler = handler_factory(self)
        self._stream_control = StreamControl(self._get_first_stream_id())
        self._honor_lease = honor_lease
        self._max_lifetime_period = max_lifetime_period
        self._keep_alive_period = keep_alive_period
        self._setup_payload = setup_payload
        self._resume_token: Optional[bytes] = None
        self._data_encoding = ensure_encoding_name(data_encoding)
        self._metadata_encoding = ensure_encoding_name(metadata_encoding)
        self._is_closing = False

        if self._honor_lease:
            self._requester_lease = DefinedLease(maximum_request_count=0)
        else:
            self._requester_lease = NullLease()

        self._responder_lease = NullLease()
        self._lease_publisher = lease_publisher

        self._frame_fragment_cache = FrameFragmentCache()

        self._send_queue = asyncio.Queue()
        self._request_queue = asyncio.Queue(request_queue_size)

        self._receiver_task = self._start_task_if_not_closing(self._receiver)
        self._sender_task = self._start_task_if_not_closing(self._sender)

        self._async_frame_handler_by_type: Dict[Type[Frame], Any] = {
            RequestResponseFrame: self.handle_request_response,
            RequestStreamFrame: self.handle_request_stream,
            RequestChannelFrame: self.handle_request_channel,
            SetupFrame: self.handle_setup,
            RequestFireAndForgetFrame: self.handle_fire_and_forget,
            MetadataPushFrame: self.handle_metadata_push,
            ResumeFrame: self.handle_resume,
            LeaseFrame: self.handle_lease,
            KeepAliveFrame: self.handle_keep_alive,
            ErrorFrame: self.handle_error
        }

    def set_transport(self, transport: Transport) -> 'RSocket':
        self._transport = transport
        return self

    def connect(self):
        logger().debug('%s: sending setup frame', self._log_identifier())

        self.send_frame(self._create_setup_frame(self._data_encoding,
                                                 self._metadata_encoding,
                                                 self._setup_payload))

        if self._honor_lease:
            self._subscribe_to_lease_publisher()

        return self

    @property
    def resume_token(self) -> Optional[str]:
        return self._resume_token

    def _start_task_if_not_closing(self, task_factory: Callable[[], Coroutine]) -> Optional[Task]:
        if not self._is_closing:
            return asyncio.create_task(task_factory())

    def set_handler_using_factory(self, handler_factory) -> RequestHandler:
        self._handler = handler_factory(self)
        return self._handler

    def _allocate_stream(self) -> int:
        return self._stream_control.allocate_stream()

    @abc.abstractmethod
    def _get_first_stream_id(self) -> int:
        ...

    def finish_stream(self, stream_id: int):
        self._stream_control.finish_stream(stream_id)

    def send_request(self, frame: RequestFrame):
        if self._honor_lease and not self._is_frame_allowed_to_send(frame):
            self._queue_request_frame(frame)
        else:
            self.send_frame(frame)

    def _queue_request_frame(self, frame: RequestFrame):
        logger().debug('%s: lease not allowing to send request. queueing', self._log_identifier())

        self._request_queue.put_nowait(frame)

    def send_frame(self, frame: Frame):
        self._send_queue.put_nowait(frame)

    def send_complete(self, stream_id: int):
        logger().debug('%s: Sending complete', self._log_identifier())
        self.send_payload(stream_id, Payload(), complete=True, is_next=False)

    def send_error(self, stream_id: int, exception: Exception):
        logger().debug('%s: Sending error: %s', self._log_identifier(), str(exception))
        self.send_frame(exception_to_error_frame(stream_id, exception))

    def send_payload(self, stream_id: int, payload: Payload, complete=False, is_next=True):
        logger().debug('%s: Sending payload: %s (complete=%s, next=%s)',
                       self._log_identifier(),
                       payload, complete,
                       is_next)
        self.send_frame(to_payload_frame(stream_id, payload, complete, is_next=is_next))

    def _update_last_keepalive(self):
        pass

    def register_new_stream(self, handler: T) -> T:
        stream_id = self._allocate_stream()
        self._register_stream(stream_id, handler)
        return handler

    def _register_stream(self, stream_id: int, handler: StreamHandler):
        handler.stream_id = stream_id
        self._stream_control.register_stream(stream_id, handler)
        return handler

    async def handle_error(self, frame: ErrorFrame):
        await self._handler.on_error(frame.error_code, payload_from_frame(frame))

    async def handle_keep_alive(self, frame: KeepAliveFrame):
        logger().debug('%s: Received keepalive', self._log_identifier())

        self._update_last_keepalive()

        if frame.flags_respond:
            frame.flags_respond = False
            self.send_frame(frame)
            logger().debug('%s: Responded to keepalive', self._log_identifier())

    async def handle_request_response(self, frame: RequestResponseFrame):
        stream_id = frame.stream_id
        self._stream_control.assert_stream_id_available(stream_id)
        handler = self._handler

        try:
            response_future = await handler.request_response(payload_from_frame(frame))
        except Exception as exception:
            self.send_error(stream_id, exception)
            return

        self._register_stream(stream_id, RequestResponseResponder(self, response_future)).setup()

    async def handle_request_stream(self, frame: RequestStreamFrame):
        stream_id = frame.stream_id
        self._stream_control.assert_stream_id_available(stream_id)
        handler = self._handler

        try:
            publisher = await handler.request_stream(payload_from_frame(frame))
        except Exception as exception:
            self.send_error(stream_id, exception)
            return

        request_responder = RequestStreamResponder(self, publisher)
        self._register_stream(stream_id, request_responder)
        request_responder.frame_received(frame)

    def _resume_available(self) -> bool:
        return self._resume_token is not None

    async def handle_setup(self, frame: SetupFrame):
        if frame.flags_resume:
            self._resume_token = frame.resume_identification_token

        if frame.flags_lease:
            if self._lease_publisher is None:
                raise RSocketProtocolException(ErrorCode.UNSUPPORTED_SETUP, data='Lease not available')
            else:
                self._subscribe_to_lease_publisher()

        handler = self._handler

        try:
            await handler.on_setup(frame.data_encoding,
                                   frame.metadata_encoding,
                                   payload_from_frame(frame))
        except Exception as exception:
            logger().error('%s: Setup error', self._log_identifier(), exc_info=True)
            raise RSocketProtocolException(ErrorCode.REJECTED_SETUP, data=str(exception)) from exception

    def _subscribe_to_lease_publisher(self):
        if self._lease_publisher is not None:
            self._lease_publisher.subscribe(self.LeaseSubscriber(self))

    def send_lease(self, lease: Lease):
        try:
            self._responder_lease = lease

            logger().debug('%s: Sending lease %s', self._log_identifier(), self._responder_lease)

            self.send_frame(self._responder_lease.to_frame())
        except Exception as exception:
            self.send_error(CONNECTION_STREAM_ID, exception)

    async def handle_fire_and_forget(self, frame: RequestFireAndForgetFrame):
        self._stream_control.assert_stream_id_available(frame.stream_id)

        await self._handler.request_fire_and_forget(payload_from_frame(frame))

    async def handle_metadata_push(self, frame: MetadataPushFrame):
        await self._handler.on_metadata_push(Payload(None, frame.metadata))

    async def handle_request_channel(self, frame: RequestChannelFrame):
        stream_id = frame.stream_id
        self._stream_control.assert_stream_id_available(stream_id)
        handler = self._handler

        try:
            publisher, subscriber = await handler.request_channel(payload_from_frame(frame))
        except Exception as exception:
            self.send_error(stream_id, exception)
            return

        channel_responder = RequestChannelResponder(self, publisher)
        self._register_stream(stream_id, channel_responder)
        channel_responder.subscribe(subscriber)
        channel_responder.frame_received(frame)

    async def handle_resume(self, frame: ResumeFrame):
        raise RSocketProtocolException(ErrorCode.REJECTED_RESUME, data='Resume not supported')

    async def handle_lease(self, frame: LeaseFrame):
        logger().debug('%s: received lease frame', self._log_identifier())

        self._requester_lease = DefinedLease(
            frame.number_of_requests,
            timedelta(milliseconds=frame.time_to_live)
        )

        while not self._request_queue.empty() and self._requester_lease.is_request_allowed():
            try:
                self.send_frame(self._request_queue.get_nowait())
                self._request_queue.task_done()
            except QueueEmpty:
                break

    async def _receiver(self):
        try:
            await self._receiver_listen()
        except asyncio.CancelledError:
            logger().debug('%s: Asyncio task canceled: receiver', self._log_identifier())
        except Exception:
            logger().error('%s: Unknown error', self._log_identifier(), exc_info=True)
            raise

    @abc.abstractmethod
    def is_server_alive(self) -> bool:
        ...

    async def _receiver_listen(self):

        while self.is_server_alive():

            next_frame_generator = await self._transport.next_frame_generator(self.is_server_alive())
            if next_frame_generator is None:
                break
            async for frame in next_frame_generator:
                try:
                    await self._handle_next_frame(frame)
                except RSocketProtocolException as exception:
                    logger().error('%s: RSocket Error %s', self._log_identifier(), str(exception))
                    self.send_error(frame.stream_id, exception)
                except Exception as exception:
                    logger().error('%s: Unknown Error', self._log_identifier(), exc_info=True)
                    self.send_error(frame.stream_id, exception)

    async def _handle_next_frame(self, frame: Frame):

        log_frame(frame, self._log_identifier())

        if isinstance(frame, InvalidFrame):
            return

        if is_fragmentable_frame(frame):
            frame = self._frame_fragment_cache.append(cast(FragmentableFrame, frame))
            if frame is None:
                return

        stream_id = frame.stream_id

        if stream_id == CONNECTION_STREAM_ID or isinstance(frame, initiate_request_frame_types):
            await self._handle_frame_by_type(frame)
        elif self._stream_control.handle_stream(stream_id, frame):
            return
        else:
            logger().debug('%s: Dropping frame from unknown stream %d', self._log_identifier(), frame.stream_id)

    async def _handle_frame_by_type(self, frame: Frame):

        self._is_frame_allowed(frame)
        frame_handler = self._async_frame_handler_by_type.get(type(frame),
                                                              noop_frame_handler)
        await frame_handler(frame)

    def _send_new_keepalive(self, data: bytes = b''):
        frame = KeepAliveFrame()
        frame.flags_respond = True
        frame.data = data
        self.send_frame(frame)
        logger().debug('%s: Sent keepalive', self._log_identifier())

    def _before_sender(self):
        pass

    async def _finally_sender(self):
        pass

    async def _sender(self):
        self._before_sender()

        try:
            while self.is_server_alive():
                frame = await self._send_queue.get()

                await self._transport.send_frame(frame)
                self._send_queue.task_done()

                if self._send_queue.empty():
                    await self._transport.on_send_queue_empty()
        except ConnectionResetError as exception:
            logger().debug(str(exception))
        except asyncio.CancelledError:
            logger().debug('%s: Asyncio task canceled: sender', self._log_identifier())
        except Exception:
            logger().error('%s: RSocket error', self._log_identifier(), exc_info=True)
            raise
        finally:
            await self._finally_sender()

    async def close(self):
        self._is_closing = True
        await self._cancel_if_task_exists(self._sender_task)
        await self._cancel_if_task_exists(self._receiver_task)
        await self._transport.close()

    async def _cancel_if_task_exists(self, task):
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                logger().debug('%s: Asyncio task canceled', self._log_identifier())

    async def __aenter__(self) -> 'RSocket':
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    def request_response(self, payload: Payload) -> Future:
        logger().debug('%s: sending request-response: %s', self._log_identifier(), payload)

        requester = RequestResponseRequester(self, payload)
        self.register_new_stream(requester).setup()
        return requester.run()

    def fire_and_forget(self, payload: Payload):
        logger().debug('%s: sending fire-and-forget: %s', self._log_identifier(), payload)

        stream_id = self._allocate_stream()
        self.send_request(to_fire_and_forget_frame(stream_id, payload))
        self.finish_stream(stream_id)

    def request_stream(self, payload: Payload) -> Union[BackpressureApi, Publisher]:
        logger().debug('%s: sending request-stream: %s', self._log_identifier(), payload)

        requester = RequestStreamRequester(self, payload)
        self.register_new_stream(requester)
        return requester

    def request_channel(
            self,
            payload: Payload,
            local_publisher: Optional[Publisher] = None) -> Union[BackpressureApi, Publisher]:

        logger().debug('%s: sending request-channel: %s', self._log_identifier(), payload)

        requester = RequestChannelRequester(self, payload, local_publisher)
        self.register_new_stream(requester)
        return requester

    def metadata_push(self, metadata: bytes):
        frame = MetadataPushFrame()
        frame.metadata = metadata
        self.send_frame(frame)

    def _is_frame_allowed_to_send(self, frame: Frame) -> bool:
        if isinstance(frame, initiate_request_frame_types):
            return self._requester_lease.is_request_allowed(frame.stream_id)

        return True

    def _is_frame_allowed(self, frame: Frame) -> bool:
        if isinstance(frame, (RequestResponseFrame,
                              RequestStreamFrame,
                              RequestChannelFrame,
                              RequestFireAndForgetFrame
                              )):
            return self._responder_lease.is_request_allowed(frame.stream_id)

        return True

    def _create_setup_frame(self,
                            data_encoding: bytes,
                            metadata_encoding: bytes,
                            payload: Optional[Payload] = None) -> SetupFrame:
        setup = SetupFrame()
        setup.flags_lease = self._honor_lease
        setup.keep_alive_milliseconds = to_milliseconds(self._keep_alive_period)
        setup.max_lifetime_milliseconds = to_milliseconds(self._max_lifetime_period)
        setup.data_encoding = data_encoding
        setup.metadata_encoding = metadata_encoding

        if payload is not None:
            setup.data = payload.data
            setup.metadata = payload.metadata

        return setup

    @abc.abstractmethod
    def _log_identifier(self) -> str:
        ...
