"""The Linky (LiXee-TIC-DIN) integration."""
from __future__ import annotations

import asyncio
import logging

from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.discovery import async_load_platform
from homeassistant.helpers.typing import ConfigType

import serial_asyncio
from serial import SerialException
import voluptuous as vol

from .const import (
    DOMAIN,
    SERIAL_READER,

    BYTESIZE,
    PARITY,
    STOPBITS,
    MODE_STANDARD_BAUD_RATE,
    MODE_STANDARD_FIELD_SEPARATOR,
    MODE_HISTORIC_BAUD_RATE,
    MODE_HISTORIC_FIELD_SEPARATOR,
    LINE_END,
    FRAME_END,

    CONF_SERIAL_PORT,
    CONF_STANDARD_MODE,
    DEFAULT_SERIAL_PORT,
    DEFAULT_STANDARD_MODE
)


_LOGGER = logging.getLogger(__name__)


CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_SERIAL_PORT, default=DEFAULT_SERIAL_PORT): cv.string,
                vol.Required(CONF_STANDARD_MODE, default=DEFAULT_STANDARD_MODE): cv.boolean,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Linky LiXee-TIC-DIN component."""
    _LOGGER.debug("init lixee component", config)
    # Debug conf
    conf = config.get(DOMAIN)
    _LOGGER.debug("Serial port: %s", conf[CONF_SERIAL_PORT])
    _LOGGER.debug("Standard mode: %s", conf[CONF_STANDARD_MODE])
    # create the serial controller and schedule it
    sr = AsyncSerialReader(
        port=conf[CONF_SERIAL_PORT], std_mode=conf[CONF_STANDARD_MODE])
    hass.async_create_task(sr.read_serial())  # async_add_job ?
    hass.bus.async_listen_once(
        EVENT_HOMEASSISTANT_STOP, sr.stop_serial_read)
    # setup the plateforms
    hass.async_create_task(async_load_platform(
        hass, SENSOR_DOMAIN, DOMAIN, {SERIAL_READER: sr}, config))
    return True


class AsyncSerialReader():
    def __init__(self, port, std_mode):
        # Build
        self._port = port
        self._baudrate = MODE_STANDARD_BAUD_RATE if std_mode else MODE_HISTORIC_BAUD_RATE
        self._std_mode = std_mode
        # Run
        self._reader = None
        self._writer = None
        self._first_line = True
        self._values = {}

    async def open_serial(self):
        """
        Open the serial connection and save the wrapped reader and writter.
        """
        try:
            self._reader, self._writer = await serial_asyncio.open_serial_connection(
                url=self._port,
                baudrate=self._baudrate,
                bytesize=BYTESIZE,
                parity=PARITY,
                stopbits=STOPBITS,
                timeout=0,
            )
        except SerialException as exc:
            _LOGGER.exception(
                "Unable to connect to the serial device %s: %s. Will retry in 5s",
                self._port,
                exc,
            )
            await self._reset_state()

    async def read_serial(self):
        """
        The main working loop.
        """
        while True:
            # Try to open a connection
            if self._reader is None:
                await self.open_serial()
                continue
            # Use the writer presence to know if we should stop
            if not self._writer:
                _LOGGER.debug("exiting read loop")
                break
            # Now that we have a connection, read its output
            try:
                line = await self._reader.readline()
            except SerialException as exc:
                _LOGGER.exception(
                    "Error while reading serial device %s: %s. Will retry in 5s", self._port, exc
                )
                await self._reset_state()
            else:
                # exiting the readloop will first yield an incomplete line, avoid parsing it
                if self._writer:
                    self.parse_line(line)

    async def _reset_state(self):
        """
        Reinitialize the controller (by nullifying it)
        and wait 5s for other methods to re start init after a pause
        """
        _LOGGER.debug("reseting async serial reader state and wait 5s")
        self._reader = None
        self._writer = None
        self._first_line = True
        self._values = {}
        await asyncio.sleep(5)

    @callback
    def stop_serial_read(self, event):
        """
        Close the underlying transport. It will move the controller to a stopped state.
        """
        if self._writer:
            _LOGGER.info("%s received: closing the serial connection", event)
            self._writer.close()
            self._writer = None
            # leave the reader to indicate read_serial() it should stop (flag)

    def parse_line(self, line):
        """
        Called when a full line has been read from serial.
        It parse it as Linky TIC infos, validate its checksum and save internally the line infos.
        """
        # there is a great chance that the first line is a partial line: skip it
        if self._first_line:
            _LOGGER.debug("skipping first line: %s", repr(line))
            self._first_line = False
            return
        # if not, it should be complete: parse it !
        _LOGGER.debug("line to parse: %s", repr(line))
        # cleanup the line
        line = line.rstrip(LINE_END).rstrip(FRAME_END)
        # extract the fields by parsing the line given the mode
        timestamp = None
        if self._std_mode:
            fields = line.split(MODE_STANDARD_FIELD_SEPARATOR)
            if len(fields) == 4:
                tag = fields[0]
                timestamp = fields[1]
                field_value = fields[2]
                checksum = fields[3]
            elif len(fields) == 3:
                tag = fields[0]
                field_value = fields[1]
                checksum = fields[2]
            else:
                _LOGGER.error(
                    "failed to parse the following line (%d fields detected) in standard mode: %s", len(fields), repr(line))
                return
        else:
            fields = line.split(MODE_HISTORIC_FIELD_SEPARATOR)
            if len(fields) == 3:
                tag = fields[0]
                field_value = fields[1]
                checksum = fields[2]
            elif len(fields) == 4:
                # checksum has the same value as field separator, leading to 4 fields with the last 2 empty
                tag = fields[0]
                field_value = fields[1]
                checksum = MODE_HISTORIC_FIELD_SEPARATOR
            else:
                _LOGGER.error(
                    "failed to parse the following line (%d fields detected) in historic mode: %s", len(fields), repr(line))
                return
        # validate the checksum
        try:
            self.validate_checksum(tag, timestamp, field_value, checksum)
        except InvalidChecksum as ic:
            _LOGGER.error(
                "failed to validate the checksum of line '%s': %s", repr(line), ic)
            return
        _LOGGER.debug("line checksum is valid")
        # transform and store the values
        payload = {"value": field_value.decode("ascii")}
        payload["timestamp"] = timestamp.decode(
            "ascii") if timestamp else timestamp
        tag = tag.decode("ascii")
        self._values[tag] = payload
        _LOGGER.debug("read the following values: %s -> %s",
                      tag, repr(payload))

    def validate_checksum(self, tag: bytes, timestamp: bytes, value: bytes, checksum: bytes):
        # rebuild the frame
        if self._std_mode:
            sep = MODE_STANDARD_FIELD_SEPARATOR
            if timestamp is None:
                frame = tag + sep + value + sep
            else:
                frame = tag + sep + timestamp + sep + value + sep
        else:
            frame = tag + MODE_HISTORIC_FIELD_SEPARATOR + value
        # compute the sum of the frame
        s1 = 0
        for b in frame:
            s1 += b
        # compute checksum for s1
        truncated = s1 & 0x3F
        computed_checksum = truncated + 0x20
        # validate
        if computed_checksum != ord(checksum):
            raise InvalidChecksum(tag, timestamp, value, s1, truncated,
                                  computed_checksum, checksum)

    def is_connected(self) -> bool:
        return self._reader and self._writer

    def get_values(self, tag) -> tuple[str, str] | tuple[None, None]:
        if not self.is_connected:
            return None, None
        try:
            payload = self._values[tag]
            return payload['value'], payload['timestamp']
        except KeyError as ke:
            _LOGGER.warning(
                "encountered KeyError while fetching %s (it could be normal if serial is not open yet): %s", tag, ke)
            return None, None


class InvalidChecksum(Exception):
    def __init__(self, tag: bytes, timestamp: bytes, value: bytes, s1: bytes, s1_truncated: bytes, computed: bytes, expected: bytes):
        self.tag = tag.decode("ascii")
        self.timestamp = timestamp.decode("ascii")
        self.value = value.decode("ascii")
        self.s1 = s1
        self.s1_truncated = s1_truncated
        self.computed = computed
        self.expected = expected
        super().__init__(self.msg())

    def msg(self):
        return "{} -> {} ({}) | s1 {} {} | truncated {} {} {} | computed {} {} {} | expected {} {} {}".format(
            self.tag, self.value, self.timestamp, self.s1, bin(self.s1),
            self.s1_truncated, bin(self.s1_truncated), chr(self.s1_truncated),
            self.computed, bin(self.computed), chr(self.computed), int.from_bytes(
                self.expected, byteorder='big'),
            bin(int.from_bytes(self.expected, byteorder='big')), chr(ord(self.expected)))
