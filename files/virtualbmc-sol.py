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

"""Single-client SOL transport and a nonblocking libvirt console bridge.

Lab patch on top of upstream VirtualBMC master:
- VIR_DOMAIN_CONSOLE_FORCE so Cockpit/virsh do not block SOL
- stream event callbacks + output buffer (plain NONBLOCK recv often
  returns -2 forever on RHEL libvirt unless events are registered)
- non-blocking SOL TX: pyghmi's _sendoutput waits for client ACK inside the
  BMC loop, which stalls guest output until the operator types (Enter pump).
  Send once, return, retry from poll_tx(); continue the queue on ACK.
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
# Non-blocking ACK wait: retry interval and budget (was a blocking ~15s loop).
ACK_RETRY_INTERVAL = 0.5
ACK_MAX_RETRIES = 10


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
        # Upstream ServerConsole clears awaitingack on a matching ACK but does
        # not continue the TX queue (client Console relied on blocking
        # _sendoutput returning to the caller). Flush pending immediately so
        # guest output keeps streaming without an Enter/CR pump.
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
        """Send one SOL data packet without blocking for the client ACK.

        Upstream pyghmi waits in a nested wait_for_rsp loop here; that freezes
        libvirt polling until the client ACKs (often only after a keystroke).
        """
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
        """Pump IPMI ACKs, retry unacked TX, or flush pending (BMC tick)."""
        if self.broken or not self.activated or not self.connected:
            return
        # listen() already waits, but drain any ACK that arrived since then
        # before deciding whether to retry or continue the queue.
        try:
            self.wait_for_rsp(0)
        except Exception:
            pass
        with self.outputlock:
            if self.awaitingack:
                if time.monotonic() < self._ack_deadline:
                    return
                if self._ack_retries_left <= 0:
                    self._print_error('SOL ACK timeout')
                    return
                self._ack_retries_left -= 1
                self._ack_deadline = time.monotonic() + ACK_RETRY_INTERVAL
                try:
                    self.send_payload(
                        self.lastpayload, retry=False,
                        needskeepalive=(self.lasttextsize == 0))
                except (ipmi_exception.IpmiException, OSError) as exc:
                    self._print_error(str(exc))
                return
            if self.pendingoutput:
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
    """Nonblocking console stream, serviced in the BMC's event loop."""

    def __init__(self, domain_name, conn_args):
        self.domain_name = domain_name
        self.conn_args = conn_args
        self._resources = contextlib.ExitStack()
        self._conn = None
        self._domain = None
        self._stream = None
        self._pending = b''
        self._outbuf = bytearray()
        self._retry_at = 0
        self._callback_registered = False

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
        self._stream = self._conn.newStream(libvirt.VIR_STREAM_NONBLOCK)
        # FORCE: steal from Cockpit/virsh so lab SOL works with a UI open.
        self._domain.openConsole(None, self._stream,
                                 libvirt.VIR_DOMAIN_CONSOLE_FORCE)
        events = (
            libvirt.VIR_STREAM_EVENT_READABLE
            | libvirt.VIR_STREAM_EVENT_ERROR
            | libvirt.VIR_STREAM_EVENT_HANGUP
        )
        self._stream.eventAddCallback(events, self._on_stream_event, None)
        self._callback_registered = True
        LOG.debug('SOL console stream attached for %s', self.domain_name)

    def _on_stream_event(self, stream, events, _opaque):
        try:
            if events & libvirt.VIR_STREAM_EVENT_READABLE:
                while True:
                    data = stream.recv(CHARACTERS_PER_PACKET)
                    if data == -2:
                        break
                    if not data:
                        # EOF
                        self.close()
                        self._retry_at = time.monotonic() + RECONNECT_INTERVAL
                        return
                    room = MAX_OUTBUF - len(self._outbuf)
                    if room <= 0:
                        LOG.warning(
                            'SOL output buffer full for %s; dropping',
                            self.domain_name)
                        break
                    self._outbuf += data[:room]
            if events & (
                    libvirt.VIR_STREAM_EVENT_ERROR
                    | libvirt.VIR_STREAM_EVENT_HANGUP):
                self.close()
                self._retry_at = time.monotonic() + RECONNECT_INTERVAL
        except libvirt.libvirtError as exc:
            LOG.debug('SOL stream event error for %(domain)s: %(error)s',
                      {'domain': self.domain_name, 'error': exc})
            self.close()
            self._retry_at = time.monotonic() + RECONNECT_INTERVAL

    def write(self, data):
        """Queue input, discarding keystrokes while the stream is absent."""
        if self._stream is None:
            return
        if len(self._pending) + len(data) > MAX_PENDING_INPUT:
            raise BufferError('SOL console input buffer is full')
        self._pending += data

    def poll(self):
        """Flush queued input and return at most one SOL packet of output."""
        if self._stream is None:
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
                return b''
            finally:
                self._retry_at = time.monotonic() + RECONNECT_INTERVAL

        if self._stream is None:
            return b''

        try:
            if self._pending:
                sent = self._stream.send(self._pending)
                if sent > 0:
                    self._pending = self._pending[sent:]
        except libvirt.libvirtError as exc:
            LOG.debug('SOL console send failed for %(domain)s: %(error)s',
                      {'domain': self.domain_name, 'error': exc})
            self.close()
            self._retry_at = time.monotonic() + RECONNECT_INTERVAL
            return b''

        if not self._outbuf:
            return b''
        chunk = bytes(self._outbuf[:CHARACTERS_PER_PACKET])
        del self._outbuf[:len(chunk)]
        return chunk

    def close(self):
        """Release the stream and connection, discarding pending input."""
        stream, self._stream = self._stream, None
        self._conn = self._domain = None
        self._pending = b''
        self._outbuf = bytearray()
        try:
            if stream is not None and self._callback_registered:
                try:
                    stream.eventRemoveCallback()
                except libvirt.libvirtError:
                    pass
            self._callback_registered = False
            if stream is not None:
                stream.abort()
        except libvirt.libvirtError:
            pass
        finally:
            try:
                self._resources.close()
            except libvirt.libvirtError:
                pass
