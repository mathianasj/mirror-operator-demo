# Portions of ServerConsole adapted from pyghmi.ipmi.console:
# Copyright 2014 IBM Corporation
# Copyright 2015-2019 Lenovo
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""Single-client SOL transport and a threaded libvirt console bridge.

Lab patch on top of upstream VirtualBMC master:
- VIR_DOMAIN_CONSOLE_FORCE so Cockpit/virsh do not block SOL
- dedicated I/O thread with a blocking libvirt stream so guest→host serial
  is not gated on client TX / virEvent delivery (the Enter/CR pump)
- non-blocking SOL TX: send one packet, return to the BMC loop, continue the
  queue on ACK (or a short soft timeout) via poll_tx()
"""

import contextlib
import threading
import time
import xml.etree.ElementTree as ET

import libvirt
from pyghmi import exceptions as ipmi_exception
from pyghmi.ipmi import console

from virtualbmc import exception
from virtualbmc import log
from virtualbmc import utils

LOG = log.get_logger()

# The SOL accepted-character count is only one byte wide.
CHARACTERS_PER_PACKET = 255
PAYLOAD_SIZE = CHARACTERS_PER_PACKET + 4
MAX_PENDING_INPUT = 4096
MAX_OUTBUF = 256 * 1024
RECONNECT_INTERVAL = 1
IDLE_TIMEOUT = 60
POLL_INTERVAL = 0.02
SOL_INACTIVE = 0x10
# Soft ACK wait: retry once, then continue so a silent client cannot freeze SOL.
ACK_RETRY_INTERVAL = 0.15
ACK_MAX_RETRIES = 2
TX_PACKETS_PER_POLL = 4
# Marker checked by setup-bmc.yml verify task.
LAB_SOL_READER_THREAD = True


class ServerConsole(console.ServerConsole):
    """Adapt pyghmi's SOL transport to a BMC-owned console.

    ``iohandler(console, data)`` receives bytes or a transport error dict.
    """

    def __init__(self, session, iohandler):
        # Adapted from pyghmi 1.2.0--1.6.19, without wait_for_rsp(0):
        # reentering the IPMI loop here can process a retransmitted activation
        # before we have replied to the first request.
        self.outputlock = threading.RLock()
        self.keepaliveid = None
        self.connected = True
        self.broken = False
        self.out_handler = lambda data: iohandler(self, data)
        self.remseq = 0
        self.myseq = 0
        self.lastsize = 0
        self.retriedpayload = 0
        self.pendingoutput = []
        self.awaitingack = False
        self.activated = True
        self.force_session = False
        self.ipmi_session = session
        self.ipmi_session.sol_handler = self._got_sol_payload
        self.poweredon = True
        self.last_activity = time.monotonic()
        self.maxoutcount = CHARACTERS_PER_PACKET
        self.lastpayload = None
        self.lasttextsize = 0
        self._ack_deadline = 0.0
        self._ack_retries_left = 0

    def _got_sol_payload(self, payload):
        if (not isinstance(payload, dict)
                and not 4 <= len(payload) <= PAYLOAD_SIZE):
            return
        self.last_activity = time.monotonic()
        was_awaiting = self.awaitingack
        super(ServerConsole, self)._got_sol_payload(payload)
        # Upstream clears awaitingack on ACK but does not continue the TX queue.
        if (was_awaiting and not self.awaitingack and not self.broken
                and self.activated and self.pendingoutput):
            self._sendpendingoutput()

    def _print_error(self, error):
        # ServerSession lacks the client keepalive state used by pyghmi's
        # inherited error handler.
        self.broken = True
        self.close(notify=False)
        self.out_handler(error if isinstance(error, dict)
                         else {'error': error})

    def _sendoutput(self, output, sendbreak=False):
        """Send one SOL data packet without blocking the BMC event loop."""
        self.myseq += 1
        self.myseq &= 0xf
        if self.myseq == 0:
            self.myseq = 1
        breakbyte = 0b10000 if sendbreak else 0
        try:
            payload = bytearray((self.myseq, 0, 0, breakbyte)) + output
        except TypeError:
            payload = (bytearray((self.myseq, 0, 0, breakbyte))
                       + output.encode('utf8'))
        self.lasttextsize = len(output)
        needskeepalive = self.lasttextsize == 0
        self.awaitingack = True
        self.lastpayload = payload
        self._ack_deadline = time.monotonic() + ACK_RETRY_INTERVAL
        self._ack_retries_left = ACK_MAX_RETRIES
        self.send_payload(payload, retry=False, needskeepalive=needskeepalive)

    def poll_tx(self):
        """Pump IPMI RX, retry/skip ACK wait, and drain pending TX."""
        if self.broken or not self.activated or not self.connected:
            return
        try:
            self.wait_for_rsp(0)
        except Exception:
            pass
        with self.outputlock:
            if self.awaitingack:
                if time.monotonic() < self._ack_deadline:
                    return
                if self._ack_retries_left > 0:
                    self._ack_retries_left -= 1
                    self._ack_deadline = time.monotonic() + ACK_RETRY_INTERVAL
                    try:
                        self.send_payload(
                            self.lastpayload, retry=False,
                            needskeepalive=(self.lasttextsize == 0))
                    except (ipmi_exception.IpmiException, OSError) as exc:
                        self._print_error(str(exc))
                    return
                # Soft timeout: continue streaming rather than freeze on Enter.
                self.awaitingack = False
                self.lastpayload = None
            for _ in range(TX_PACKETS_PER_POLL):
                if self.awaitingack or not self.pendingoutput:
                    break
                self._sendpendingoutput()

    def send_payload(self, payload, payload_type=1, retry=True,
                     needskeepalive=False):
        if not self.activated:
            return
        try:
            super(ServerConsole, self).send_payload(
                payload, payload_type=payload_type, retry=retry,
                needskeepalive=needskeepalive)
        except (ipmi_exception.IpmiException, OSError) as exc:
            self._print_error(str(exc))

    def close(self, notify=True):
        """Release the session callback and cancel any outstanding output."""
        session, self.ipmi_session = self.ipmi_session, None
        self.activated = False
        self.connected = False
        self.awaitingack = False
        self.pendingoutput = []
        self.lastpayload = None
        self._ack_retries_left = 0
        if session is None or session.sol_handler != self._got_sol_payload:
            return
        session.sol_handler = None
        if notify:
            try:
                session.send_payload(bytearray([0, 0, 0, SOL_INACTIVE]),
                                     payload_type=1, retry=False)
            except (ipmi_exception.IpmiException, OSError) as exc:
                LOG.debug('Could not notify disconnected SOL client: %s', exc)


class LibvirtConsole(object):
    """Console bridge with a dedicated blocking reader thread.

    Upstream VirtualBMC uses NONBLOCK + virEvent callbacks in the BMC loop.
    On this lab stack, guest output effectively drips only when the SOL client
    transmits (Enter/CR pump). Owning the stream in a thread with a blocking
    ``recv`` decouples serial RX from IPMI client TX.
    """

    def __init__(self, domain_name, conn_args):
        self.domain_name = domain_name
        self.conn_args = conn_args
        self._resources = contextlib.ExitStack()
        self._conn = None
        self._domain = None
        self._stream = None
        self._pending = b''
        self._outbuf = bytearray()
        self._lock = threading.Lock()
        self._retry_at = 0
        self._stop = threading.Event()
        self._thread = None

    def open(self):
        """Connect to libvirt and attach the console if the guest is active."""
        try:
            self._conn = self._resources.enter_context(
                utils.libvirt_open(**self.conn_args))
            self._domain = utils.get_libvirt_domain(
                self._conn, self.domain_name)
            tree = ET.fromstring(self._domain.XMLDesc())
            if (tree.find('./devices/console') is None
                    and tree.find('./devices/serial') is None):
                raise exception.ConsoleNotFound(domain=self.domain_name)
            self._attach()
        except Exception:
            self.close()
            raise

    def _attach(self):
        if not self._domain.isActive():
            return
        self._stop_reader()
        # NONBLOCK in a dedicated thread (tight poll). Main-thread NONBLOCK
        # recv without that poll returns -2 forever on RHEL; virEvent delivery
        # here effectively needed client TX. A side thread avoids both.
        self._stream = self._conn.newStream(libvirt.VIR_STREAM_NONBLOCK)
        self._domain.openConsole(None, self._stream,
                                 libvirt.VIR_DOMAIN_CONSOLE_FORCE)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._io_loop,
            name='vbmc-sol-%s' % self.domain_name,
            daemon=True)
        self._thread.start()
        LOG.debug('SOL console reader thread attached for %s', self.domain_name)

    def _stop_reader(self):
        self._stop.set()
        stream = self._stream
        if stream is not None:
            try:
                stream.abort()
            except libvirt.libvirtError:
                pass
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def _io_loop(self):
        stream = self._stream
        if stream is None:
            return
        try:
            while not self._stop.is_set():
                # Flush host→guest keystrokes without blocking the BMC loop.
                with self._lock:
                    pending = self._pending
                    self._pending = b''
                if pending:
                    try:
                        while pending and not self._stop.is_set():
                            sent = stream.send(pending)
                            if sent == -2:
                                break
                            if sent <= 0:
                                break
                            pending = pending[sent:]
                        if pending:
                            with self._lock:
                                self._pending = pending + self._pending
                    except libvirt.libvirtError as exc:
                        LOG.debug(
                            'SOL console send failed for %(domain)s: %(error)s',
                            {'domain': self.domain_name, 'error': exc})
                        break

                got = False
                try:
                    while not self._stop.is_set():
                        data = stream.recv(CHARACTERS_PER_PACKET)
                        if data == -2:
                            break
                        if not data:
                            return
                        got = True
                        with self._lock:
                            room = MAX_OUTBUF - len(self._outbuf)
                            if room <= 0:
                                LOG.warning(
                                    'SOL output buffer full for %s; dropping',
                                    self.domain_name)
                                break
                            self._outbuf += data[:room]
                            if room < len(data):
                                break
                except libvirt.libvirtError as exc:
                    if self._stop.is_set():
                        break
                    LOG.debug(
                        'SOL console recv failed for %(domain)s: %(error)s',
                        {'domain': self.domain_name, 'error': exc})
                    break
                if not got:
                    self._stop.wait(0.01)
        finally:
            self._retry_at = time.monotonic() + RECONNECT_INTERVAL
            with self._lock:
                if self._stream is stream:
                    self._stream = None

    def write(self, data):
        """Queue input for the I/O thread (keystrokes from the SOL client)."""
        with self._lock:
            if self._stream is None and self._thread is None:
                return
            if len(self._pending) + len(data) > MAX_PENDING_INPUT:
                raise BufferError('SOL console input buffer is full')
            self._pending += data

    def poll(self):
        """Return at most one SOL packet of buffered guest output."""
        with self._lock:
            if self._outbuf:
                chunk = bytes(self._outbuf[:CHARACTERS_PER_PACKET])
                del self._outbuf[:len(chunk)]
                return chunk
            alive = (
                self._stream is not None
                or (self._thread is not None and self._thread.is_alive())
            )

        if alive:
            return b''

        if time.monotonic() < self._retry_at:
            return b''
        try:
            if self._conn is None:
                self.open()
            else:
                self._attach()
        except (libvirt.libvirtError, exception.VirtualBMCError) as exc:
            LOG.debug('SOL console reconnect for %(domain)s: %(error)s',
                      {'domain': self.domain_name, 'error': exc})
            self.close()
        finally:
            self._retry_at = time.monotonic() + RECONNECT_INTERVAL
        return b''

    def close(self):
        """Stop the reader thread and release libvirt resources."""
        self._stop_reader()
        self._conn = self._domain = None
        with self._lock:
            self._pending = b''
            self._outbuf = bytearray()
            self._stream = None
        try:
            self._resources.close()
        except libvirt.libvirtError:
            pass
