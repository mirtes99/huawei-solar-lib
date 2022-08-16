"""
Get production and status information from the Huawei Inverter using Modbus over TCP
"""
import asyncio
import logging
import typing as t
from collections import namedtuple
from hashlib import sha256
import hmac
import secrets
import struct

import backoff
from pymodbus.client.asynchronous.async_io import (
    ReconnectingAsyncioModbusTcpClient,
    AsyncioModbusSerialClient,
    init_tcp_client,
)
from pymodbus.constants import Endian
from pymodbus.exceptions import ConnectionException as ModbusConnectionException
from pymodbus.payload import BinaryPayloadDecoder, BinaryPayloadBuilder
from pymodbus.pdu import ModbusRequest, ModbusResponse, ModbusExceptions
from pymodbus.pdu import ExceptionResponse
from pymodbus.factory import ClientDecoder
from pymodbus.transaction import ModbusSocketFramer, ModbusRtuFramer
from pymodbus.utilities import checkCRC, computeCRC

import huawei_solar.register_names as rn

from .exceptions import (
    ConnectionException,
    HuaweiSolarException,
    PermissionDenied,
    ReadException,
    SlaveBusyException,
    WriteException,
)
from .registers import REGISTERS, RegisterDefinition

LOGGER = logging.getLogger(__name__)

Result = namedtuple("Result", "value unit")

DEFAULT_SLAVE = 0
DEFAULT_TIMEOUT = 5
DEFAULT_WAIT = 2
DEFAULT_COOLDOWN_TIME = 0.05

HEARTBEAT_REGISTER = 49999

FILE_UPLOAD_MAX_RETRIES = 6
FILE_UPLOAD_RETRY_TIMEOUT = 10


def _compute_digest(password, seed):
    hashed_password = sha256(password).digest()

    return hmac.digest(key=hashed_password, msg=seed, digest=sha256)


rtu_bug_patch_applied = False


def monkey_patch_rtu_bug():
    """
    Dirty fix for bug triggered when RTU responses arrive in multiple packets.

    Based on https://github.com/riptideio/pymodbus/pull/707
    """

    global rtu_bug_patch_applied

    if rtu_bug_patch_applied:
        return

    from pymodbus.framer.rtu_framer import ModbusRtuFramer

    origprocessIncomingPacket = ModbusRtuFramer.processIncomingPacket

    def patched_processIncomingPacket(self, data, callback, unit, **kwargs):
        unit = self.decode_data(self._buffer).get("unit", 0)
        return origprocessIncomingPacket(self, data, callback, unit, **kwargs)

    ModbusRtuFramer.processIncomingPacket = patched_processIncomingPacket
    rtu_bug_patch_applied = True


class AsyncHuaweiSolar:
    """Async interface to the Huawei solar inverter"""

    def __init__(
        self,
        client: t.Union[ReconnectingAsyncioModbusTcpClient, AsyncioModbusSerialClient],
        slave: int = DEFAULT_SLAVE,
        timeout: int = DEFAULT_TIMEOUT,
        cooldown_time: int = DEFAULT_COOLDOWN_TIME,
    ):
        """DO NOT USE THIS CONSTRUCTOR DIRECTLY. Use AsyncHuaweiSolar.create() instead"""
        self._client = client
        self._timeout = timeout
        self._cooldown_time = cooldown_time
        self.slave = slave

        # use this lock to prevent concurrent requests, as the
        # Huawei inverters can't cope with those
        self._communication_lock = asyncio.Lock()

        # These values are set by the `initialize()` method
        self.time_zone = None
        self.battery_type = None
        self.detected_smart_logger = False
             
    async def _initialize(self):

        #try to determine if device with slaveid=0 is Smartlogger
        if self.slave == 0:
            try:
                # we try to read active power of 1 connected  inverter using "Remapped Modbus definitions" from smartlogger modbus definitions
                self.inv_1_power = (await self.get(rn.SMARTLOGGER_FIRST_SLAVE_POWER)).value
                if self.inv_1_power >= 0:
                    self.detected_smart_logger = True
                    LOGGER.info("Smartlogger detected")
            except ReadException as rerr:
                LOGGER.debug("No SmartLogger detected")

        # get some registers which are needed to correctly decode all values
        if self.detected_smart_logger:
            self.time_zone = (await self.get(rn.SMARTLOGGER_TIME_ZONE)).value
        else:
            self.time_zone = (await self.get(rn.TIME_ZONE)).value

            try:
                # we assume that when at least one battery is present, it will
                # always be put in storage_unit_1 first
                self.battery_type = (await self.get(rn.STORAGE_UNIT_1_PRODUCT_MODEL)).value
            except ReadException as rerr:
                if "IllegalAddress" in str(rerr):
                    LOGGER.info(
                        "Received IllegalAddress-error while determining battery support. Setting it to None.",
                        exc_info=rerr,
                    )
                    # inverter doesn't seem to support a battery
                    self.battery_type = None
                else:
                    LOGGER.exception(f"Got error {rerr} while trying to determine battery.")
                raise rerr

    @classmethod
    async def create(
        cls,
        host,
        port="502",
        slave: int = DEFAULT_SLAVE,
        timeout: int = DEFAULT_TIMEOUT,
        cooldown_time: int = DEFAULT_COOLDOWN_TIME,
        loop=None,
    ):  # pylint: disable=too-many-arguments
        """Creates an AsyncHuaweiSolar instance."""

        client = None
        try:
            client = await cls.__get_tcp_client(host, port, loop)

            huawei_solar = cls(client, slave, timeout, cooldown_time)

            await huawei_solar._initialize()

            return huawei_solar
        except Exception as err:
            # if an error occurs, we need to make sure that the Modbus-client is stopped,
            # otherwise it can stay active and cause even more problems ...
            LOGGER.exception("Aborting client creation due to error.")

            if client is not None:
                client.stop()
            raise err

    @classmethod
    async def create_rtu(
        cls,
        port,
        slave: int = DEFAULT_SLAVE,
        timeout: int = DEFAULT_TIMEOUT,
        cooldown_time: int = DEFAULT_COOLDOWN_TIME,
        loop=None,
        **serial_kwargs,
    ):
        client = None
        try:
            client = await cls.__get_rtu_client(port, loop, **serial_kwargs)
            await client.connect()
            huawei_solar = cls(client, slave, timeout, cooldown_time)
            await huawei_solar._initialize()

            return huawei_solar
        except Exception as err:
            # if an error occurs, we need to make sure that the Modbus-client is stopped,
            # otherwise it can stay active and cause even more problems ...
            LOGGER.exception("Aborting client creation due to error.")

            if client is not None:
                client.stop()
            raise err

    @classmethod
    async def __get_rtu_client(cls, port, loop=None, **serial_kwargs):
        monkey_patch_rtu_bug()
        return AsyncioModbusSerialClient(
            port,
            framer=ModbusRtuFramer(decoder=cls.__get_modbus_decoder()),
            loop=loop,
            **serial_kwargs,
        )

    @classmethod
    async def __get_tcp_client(
        cls, host, port, loop
    ) -> ReconnectingAsyncioModbusTcpClient:

        client = await init_tcp_client(
            None,
            loop,
            host,
            port,
            reset_socket=False,
            framer=ModbusSocketFramer(decoder=cls.__get_modbus_decoder()),
        )
        # wait a little bit to prevent a timeout on the first request
        await asyncio.sleep(1)

        return client

    @classmethod
    def __get_modbus_decoder(cls):
        decoder = ClientDecoder()
        decoder.register(PrivateHuaweiModbusResponse)

        return decoder

    def is_client_connected(self):
        if isinstance(self._client, AsyncioModbusSerialClient):
            return self._client._connected
        return self._client.connected

    async def stop(self):
        """Stop the modbus client."""
        self._client.stop()

    async def _decode_response(
        self, reg: RegisterDefinition, decoder: BinaryPayloadDecoder
    ):
        """Decodes a modbus register and puts it into a Result object."""
        result = reg.decode(decoder, self)

        if not hasattr(reg, "unit") or callable(reg.unit) or isinstance(reg.unit, dict):
            return Result(result, None)
        return Result(result, reg.unit)

    async def get(self, name, slave=None):
        """get named register from device"""
        return (await self.get_multiple([name], slave))[0]

    async def get_multiple(self, names: list[str], slave=None):
        """Read multiple registers at the same time.

        This is only possible if the registers are consecutively available in the
        inverters' memory.
        """

        if len(names) == 0:
            raise ValueError("Expected at least one register name")

        registers = list(map(REGISTERS.get, names))

        if None in registers:
            raise ValueError("Did not recognize all register names")

        for idx in range(1, len(names)):
            if (
                registers[idx - 1].register + registers[idx - 1].length
                > registers[idx].register
            ):
                raise ValueError(
                    f"Requested registers must be in monotonically increasing order, "
                    f"but {registers[idx-1].register} + {registers[idx-1].length} > {registers[idx].register}!"
                )

            register_distance = (
                registers[idx - 1].register
                + registers[idx - 1].length
                - registers[idx].register
            )

            if register_distance > 64:
                raise ValueError(
                    "Gap between requested registers is too large. Split it in two requests"
                )

        total_length = (
            registers[-1].register + registers[-1].length - registers[0].register
        )

        response = await self._read_registers(
            registers[0].register, total_length, slave
        )

        decoder = BinaryPayloadDecoder.fromRegisters(
            response.registers, byteorder=Endian.Big, wordorder=Endian.Big
        )

        result = [await self._decode_response(registers[0], decoder)]
        for idx in range(1, len(registers)):
            skip_registers = registers[idx].register - (
                registers[idx - 1].register + registers[idx - 1].length
            )
            decoder.skip_bytes(
                skip_registers * 2
            )  # registers are 16-bit, so we need to multiply by two
            result.append(await self._decode_response(registers[idx], decoder))

        return result

    async def _read_registers(
        self, register: RegisterDefinition, length: int, slave: t.Optional[int]
    ):
        """
        Async read register from device.

        The device needs a bit of time between the connection and the first request
        and between requests if there is a long time between them, else it will fail.

        This is solved by sleeping between the first connection and a request,
        and up to 5 retries between following requests.

        It seems to only support connections from one device at the same time.
        """

        def backoff_giveup(details):
            raise ReadException(
                f"Failed to read register {register} after {details['tries']} tries"
            )

        @backoff.on_exception(
            backoff.constant,
            (asyncio.TimeoutError, SlaveBusyException),
            interval=DEFAULT_WAIT,
            max_tries=5,
            jitter=None,
            on_backoff=lambda details: LOGGER.debug(
                "Backing off reading for %0.1f seconds after %d tries",
                details["wait"],
                details["tries"],
            ),
            on_giveup=backoff_giveup,
        )
        async def _do_read():

            if not self.is_client_connected():
                message = "Modbus client is not connected to the inverter."
                LOGGER.exception(message)
                raise ConnectionException(message)
            try:
                response = await self._client.protocol.read_holding_registers(
                    register,
                    length,
                    unit=slave or self.slave,
                    timeout=self._timeout,
                )

                # trigger a backoff if we get a SlaveBusy-exception
                if isinstance(response, ExceptionResponse):
                    if response.exception_code == ModbusExceptions.SlaveBusy:
                        raise SlaveBusyException()

                    # Not a slavebusy-exception
                    raise ReadException(
                        f"Got error while reading from register {register} with length {length}: {response}"
                    )

                return response

            except ModbusConnectionException as err:
                message = "Could not read register value, has another device interrupted the connection?"
                LOGGER.error(message)
                raise ReadException(message) from err
            # errors are different with async pymodbus,
            # we should not be able to reach this code. Keep it for debugging
            message = "could not read register value for unknown reason"
            LOGGER.error(message)
            raise ReadException(message)

        async with self._communication_lock:
            LOGGER.debug("Reading register %s", register)
            result = await _do_read()
            await asyncio.sleep(
                self._cooldown_time
            )  # throttle requests to prevent errors
            return result

    async def get_file(
        self, file_type, customized_data=None, slave: t.Optional[int] = None
    ) -> bytes:
        """Reads a 'file' as defined by the 'Uploading Files'
        process described in 6.3.7.1 of the
        Solar Inverter Modbus Interface Definitions"""

        def backoff_giveup(details):
            raise ReadException(
                f"Failed to read file {file_type} after {details['tries']} tries"
            )

        @backoff.on_exception(
            backoff.constant,
            (asyncio.TimeoutError, SlaveBusyException),
            interval=FILE_UPLOAD_RETRY_TIMEOUT,
            max_tries=FILE_UPLOAD_MAX_RETRIES,
            jitter=None,
            on_backoff=lambda details: LOGGER.debug(
                "Backing off file upload for %0.1f seconds after %d tries",
                details["wait"],
                details["tries"],
            ),
            on_giveup=backoff_giveup,
        )
        async def _perform_request(request: ModbusRequest, response_type):
            response = await self._client.protocol.execute(request)

            if isinstance(response, ExceptionResponse):
                if response.exception_code == 0x80:
                    raise PermissionDenied("Permission denied")
                elif response.exception_code == 0x06:
                    raise SlaveBusyException()
                raise ReadException(
                    f"Exception occured while trying to read file {hex(file_type)}: {hex(response.exception_code)}"
                )

            return response_type(response.content)

        async def _do_read_file():
            # Start the upload
            start_upload_response = await _perform_request(
                StartUploadModbusRequest(
                    file_type, customized_data, unit=slave or self.slave
                ),
                StartUploadModbusResponse,
            )

            data_frame_length = start_upload_response.data_frame_length
            file_length = start_upload_response.file_length

            # Request the data in 'frames'

            file_data = bytes()
            next_frame_no = 0

            while (next_frame_no * data_frame_length) < file_length:
                data_upload_response = await _perform_request(
                    UploadModbusRequest(
                        file_type, next_frame_no, unit=slave or self.slave
                    ),
                    UploadModbusResponse,
                )

                file_data += data_upload_response.frame_data
                next_frame_no += 1

            # Complete the upload and check the CRC
            complete_upload_response = await _perform_request(
                CompleteUploadModbusRequest(file_type, unit=slave or self.slave),
                CompleteUploadModbusResponse,
            )

            file_crc = complete_upload_response.file_crc
            # swap upper and lower two bytes to match how computeCRC works
            swapped_crc = ((file_crc << 8) & 0xFF00) | ((file_crc >> 8) & 0x00FF)

            if not checkCRC(file_data, swapped_crc):
                raise ReadException(
                    "Computed CRC %#x for file %#x does not match expected value %#x"
                    % (computeCRC(file_data), file_type, swapped_crc)
                )

            return file_data

        async with self._communication_lock:
            LOGGER.debug("Reading file %#x", file_type)
            result = await _do_read_file()
            await asyncio.sleep(
                self._cooldown_time
            )  # throttle requests to prevent errors

            return result

    async def set(self, name, value, slave=None):
        """set named register from device"""
        try:
            reg = REGISTERS[name]
        except KeyError as err:
            raise ValueError("Invalid Register Name") from err

        if not reg.writeable:
            raise WriteException("Register is not writable")

        builder = BinaryPayloadBuilder(byteorder=Endian.Big, wordorder=Endian.Big)
        reg.encode(value, builder)
        value = builder.to_registers()

        if len(value) != reg.length:
            raise WriteException("Wrong number of registers to write")

        async with self._communication_lock:
            response = await self.write_registers(
                reg.register, builder.to_registers(), slave
            )
            await asyncio.sleep(self._cooldown_time)

        if isinstance(response, ExceptionResponse):
            raise WriteException(
                f"Got error while writing from register {reg.register} : {response}"
            )

        return response.address == reg.register and response.count == reg.length

    async def write_registers(self, register, value, slave=None):
        """
        Async write register to device.
        """

        if not self.is_client_connected():
            message = "Modbus client is not connected to the inverter."
            LOGGER.exception(message)
            raise ConnectionException(message)
        try:
            LOGGER.debug("Writing to %s: %s", register, value)

            response = await self._client.protocol.write_registers(
                register,
                value,
                unit=slave or self.slave,
                timeout=self._timeout,
            )
            if isinstance(response, ExceptionResponse):
                if response.exception_code == 0x80:
                    raise PermissionDenied("Permission denied")
                raise WriteException(ModbusExceptions.decode(response.exception_code))
            return response
        except ModbusConnectionException as err:
            LOGGER.exception("Failed to connect to device, is the host correct?")
            raise ConnectionException(err) from err

    async def login(self, username: str, password: str, slave: t.Optional[int] = None):
        """Login into the inverter."""

        # Get challenge
        challenge_request = PrivateHuaweiModbusRequest(
            36, bytes([1, 0]), unit=slave or self.slave
        )

        challenge_response = await self._client.protocol.execute(challenge_request)

        assert challenge_response.content[0] == 0x11
        inverter_challenge = challenge_response.content[1:17]

        client_challenge = secrets.token_bytes(16)

        encoded_username = username.encode("utf-8")
        hashed_password = _compute_digest(password.encode("utf-8"), inverter_challenge)

        login_bytes = bytes(
            [
                len(client_challenge)
                + 1
                + len(encoded_username)
                + 1
                + len(hashed_password),
                *client_challenge,
                len(encoded_username),
                *encoded_username,
                len(hashed_password),
                *hashed_password,
            ]
        )
        await asyncio.sleep(0.05)
        login_request = PrivateHuaweiModbusRequest(
            37, login_bytes, unit=slave or self.slave
        )
        login_response = await self._client.protocol.execute(login_request)

        if login_response.content[1] == 0:

            # check if inverter returned the right hash of the password as well
            inverter_mac_response_lengths = login_response.content[2]

            inverter_mac_response = login_response.content[
                3 : 3 + inverter_mac_response_lengths
            ]

            if (
                not _compute_digest(password.encode("utf-8"), client_challenge)
                == inverter_mac_response
            ):
                LOGGER.error(
                    "Inverter response contains an invalid challenge answer. This could indicate a MitM-attack!"
                )

            return True
        return False

    async def heartbeat(self, slave_id):
        """Performs the heartbeat command. Only useful when maintaining a session."""
        if not self.is_client_connected():
            return False
        try:
            # 49999 is the magic register used to keep the connection alive
            response = await self._client.protocol.write_register(
                HEARTBEAT_REGISTER, 0x1, slave=slave_id or self.slave
            )
            if isinstance(response, ExceptionResponse):
                LOGGER.warning(
                    "Received an error after sending the heartbeat command: %s",
                    response,
                )
                return False
            LOGGER.debug("Heartbeat succeeded")
            return True
        except HuaweiSolarException as err:
            LOGGER.exception("Exception during heartbeat: %s", err)
            return False


class PrivateHuaweiModbusResponse(ModbusResponse):
    """Response with the private Huawei Solar function code"""

    function_code = 0x41
    _rtu_byte_count_pos = 3

    def __init__(self, **kwargs):
        ModbusResponse.__init__(self, **kwargs)

        self.sub_command = None
        self.content = bytes()

    def decode(self, data):
        self.sub_command = int(data[0])
        self.content = data[1:]

    def __str__(self):
        return f"{self.__class__.__name__}({self.sub_command})"


class PrivateHuaweiModbusRequest(ModbusRequest):
    """Request with the private Huawei Solar function code"""

    function_code = 0x41
    _rtu_byte_count_pos = 3

    def __init__(self, sub_command, content: bytes, **kwargs):
        ModbusRequest.__init__(self, **kwargs)
        self.sub_command = sub_command
        self.content = content

    def encode(self):
        return bytes([self.sub_command, *self.content])

    def decode(self, data):
        self.sub_command = int(data[0])
        self.content = data[1:]

    def __str__(self):
        return f"{self.__class__.__name__}({self.sub_command})"


class StartUploadModbusRequest(ModbusRequest):
    function_code = 0x41
    sub_function_code = 0x05

    def __init__(self, file_type, customized_data: t.Optional[bytes] = None, **kwargs):
        ModbusRequest.__init__(self, **kwargs)
        self.file_type = file_type

        if customized_data is None:
            self.customised_data = bytes()
        else:
            self.customised_data = customized_data

    def encode(self):
        data_length = 1 + len(self.customised_data)
        return (
            struct.pack(">BBB", self.sub_function_code, data_length, self.file_type)
            + self.customised_data
        )

    def decode(self, data):
        sub_function_code, data_length, self.file_type = struct.unpack(">BBB", data)
        self.customised_data = data[3:]

        assert sub_function_code == self.sub_function_code
        assert len(self.customised_data) == data_length - 1


class StartUploadModbusResponse:
    function_code = 0x41
    sub_function_code = 0x05

    def __init__(self, data):
        ModbusResponse.__init__(self)

        (
            data_length,
            self.file_type,
            self.file_length,
            self.data_frame_length,
        ) = struct.unpack_from(">BBLB", data, 0)
        self.customised_data = data[7:]

        assert len(self.customised_data) == data_length - 6


class UploadModbusRequest(ModbusRequest):
    function_code = 0x41
    sub_function_code = 0x06

    def __init__(self, file_type, frame_no, **kwargs):
        ModbusRequest.__init__(self, **kwargs)
        self.file_type = file_type
        self.frame_no = frame_no

    def encode(self):
        data_length = 3
        return struct.pack(
            ">BBBH", self.sub_function_code, data_length, self.file_type, self.frame_no
        )

    def decode(self, data):
        sub_function_code, data_length, self.file_type, self.frame_no = struct.unpack(
            ">BBBH", data
        )

        assert sub_function_code == self.sub_function_code
        assert data_length == 3


class UploadModbusResponse:
    function_code = 0x41
    sub_function_code = 0x06

    def __init__(self, data):

        (
            data_length,
            self.file_type,
            self.frame_no,
        ) = struct.unpack_from(">BBH", data, 0)
        self.frame_data = data[4:]

        assert len(self.frame_data) == data_length - 3


class CompleteUploadModbusRequest(ModbusRequest):
    function_code = 0x41
    sub_function_code = 0x0C

    def __init__(self, file_type, **kwargs):
        ModbusRequest.__init__(self, **kwargs)
        self.file_type = file_type

    def encode(self):
        data_length = 1
        return struct.pack(">BBB", self.sub_function_code, data_length, self.file_type)

    def decode(self, data):
        sub_function_code, data_length, self.file_type = struct.unpack(">BBB", data)

        assert sub_function_code == self.sub_function_code
        assert data_length == 1


class CompleteUploadModbusResponse:
    function_code = 0x41
    sub_function_code = 0x0C

    def __init__(self, data):
        (
            data_length,
            self.file_type,
            self.file_crc,
        ) = struct.unpack_from(">BBH", data, 0)

        assert data_length == 3
