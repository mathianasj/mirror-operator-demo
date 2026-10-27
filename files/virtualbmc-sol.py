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
- dedicated libvirt connection + blocking stream reader thread (same-conn
  NONBLOCK/virEvent only drips guest output when the SOL client transmits)
- non-blocking SOL TX with soft ACK timeout via poll_tx()
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
TX_PACKETS_PER_POLL = 16
# Marker checked by setup-bmc.yml verify task.
LAB_SOL_READER_THREAD = True
LAB_SOL_BLOCKING_CONSOLE = True
LAB_SOL_NO_ACK_WAIT = True


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

    def _got_sol_payload(self, payload):
        if (not isinstance(payload, dict)
                and not 4 <= len(payload) <= PAYLOAD_SIZE):
            return
        self.last_activity = time.monotonic()
        was_awaiting = self.awaitingack
        super(ServerConsole, self)._got_sol_payload(payload)
        if (was_awaiting and not self.awaitingack and not self.broken
                and self.activated and self.pendingoutput):
            self._sendpendingoutput()

    def _print_error(self, error):
        self.broken = True
        self.close(notify=False)
        self.out_handler(error if isinstance(error, dict)
                         else {'error': error})

    def _sendoutput(self, output, sendbreak=False):
        """Send one SOL packet without stop-and-wait (lab streaming).

        Combined with the blocking console reader thread, this is what makes
        guest output flow without an Enter/CR pump. Real BMCs/ipmitool still
        exchange ACKs; we simply do not stall the TX queue on them.
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
        self.lastpayload = payload
        self.awaitingack = False  # LAB_SOL_NO_ACK_WAIT
        self.send_payload(payload, retry=False, needskeepalive=needskeepalive)

    def poll_tx(self):
        """Pump IPMI RX and drain pending SOL TX."""
        if self.broken or not self.activated or not self.connected:
            return
        try:
            self.wait_for_rsp(0)
        except Exception:
            pass
        with self.outputlock:
            for _ in range(TX_PACKETS_PER_POLL):
                if not self.pendingoutput:
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
        session, self.ipmi_session = self.ipmi_session, None
        self.activated = False
        self.connected = False
        self.awaitingack = False
        self.pendingoutput = []
        self.lastpayload = None
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
    """Console bridge using a dedicated connection and blocking reader thread.

    Probed on the lab hypervisor: ``newStream(0)`` + ``recv`` on its own
    ``qemu:///system`` connection streams boot logs without client TX.
    Same-connection NONBLOCK/virEvent (upstream) only dripped after Enter/CR.
    """

    def __init__(self, domain_name, conn_args):
        self.domain_name = domain_name
        self.conn_args = conn_args
        self._pending = b''
        self._outbuf = bytearray()
        self._lock = threading.Lock()
        self._retry_at = 0
        self._stop = threading.Event()
        self._thread = None
        self._stream = None
        self._attached = False

    def open(self):
        """Validate the domain has a console, then start the reader thread."""
        try:
            with utils.libvirt_open(readonly=True, **self.conn_args) as conn:
                domain = utils.get_libvirt_domain(conn, self.domain_name)
                tree = ET.fromstring(domain.XMLDesc())
                if (tree.find('./devices/console') is None
                        and tree.find('./devices/serial') is None):
                    raise exception.ConsoleNotFound(domain=self.domain_name)
                if not domain.isActive():
                    return
            self._attach()
        except Exception:
            self.close()
            raise

    def _attach(self):
        self._stop_reader()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._io_loop,
            name='vbmc-sol-%s' % self.domain_name,
            daemon=True)
        self._thread.start()
        # Wait briefly for openConsole to succeed or fail.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with self._lock:
                if self._attached:
                    break
            if self._thread is not None and not self._thread.is_alive():
                break
            time.sleep(0.05)
        with self._lock:
            ok = self._attached
        if not ok:
            self._stop_reader()
            raise exception.VirtualBMCError(
                message='SOL console reader failed to attach for %s'
                % self.domain_name)
        LOG.debug('SOL blocking console reader attached for %s',
                  self.domain_name)

    def _stop_reader(self):
        self._stop.set()
        with self._lock:
            stream = self._stream
            self._stream = None
        if stream is not None:
            try:
                stream.abort()
            except libvirt.libvirtError:
                pass
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        with self._lock:
            self._attached = False

    def _io_loop(self):
        conn = None
        stream = None
        try:
            conn = libvirt.open(self.conn_args.get('uri') or 'qemu:///system')
            # Match utils.libvirt_open SASL if present.
            # (Lab uses qemu:///system without SASL.)
            domain = utils.get_libvirt_domain(conn, self.domain_name)
            if not domain.isActive():
                return
            # Blocking recv on a dedicated connection streams without client TX.
            # Do NOT stream.send() here: a single blocked send (e.g. GRUB not
            # reading) freezes RX. Keystrokes are dropped at write(); clients
            # may still type (wakes ipmitool select) but guest input is best
            # done via virsh console in this lab.
            stream = conn.newStream(0)
            domain.openConsole(None, stream,
                               libvirt.VIR_DOMAIN_CONSOLE_FORCE)
            with self._lock:
                self._attached = True
                self._stream = stream
            LOG.info('SOL console reader attached for %s', self.domain_name)

            while not self._stop.is_set():
                try:
                    data = stream.recv(CHARACTERS_PER_PACKET)
                except libvirt.libvirtError as exc:
                    if self._stop.is_set():
                        break
                    LOG.debug(
                        'SOL console recv failed for %(domain)s: %(error)s',
                        {'domain': self.domain_name, 'error': exc})
                    break
                if not data:
                    break
                with self._lock:
                    room = MAX_OUTBUF - len(self._outbuf)
                    if room <= 0:
                        LOG.warning(
                            'SOL output buffer full for %s; dropping',
                            self.domain_name)
                        continue
                    self._outbuf += data[:room]
        except Exception as exc:
            LOG.warning('SOL console thread failed for %(domain)s: %(error)s',
                        {'domain': self.domain_name, 'error': exc})
        finally:
            self._retry_at = time.monotonic() + RECONNECT_INTERVAL
            with self._lock:
                self._attached = False
            if stream is not None:
                try:
                    stream.abort()
                except libvirt.libvirtError:
                    pass
            if conn is not None:
                try:
                    conn.close()
                except libvirt.libvirtError:
                    pass

    def write(self, data):
        """Accept SOL input without forwarding to the guest console.

        Forwarding via stream.send on the blocking reader connection can stall
        RX forever. Lab interactive input should use virsh console.
        """
        return

    def poll(self):
        """Return at most one SOL packet of buffered guest output."""
        with self._lock:
            if self._outbuf:
                chunk = bytes(self._outbuf[:CHARACTERS_PER_PACKET])
                del self._outbuf[:len(chunk)]
                return chunk
            alive = self._attached or (
                self._thread is not None and self._thread.is_alive())

        if alive:
            return b''

        if time.monotonic() < self._retry_at:
            return b''
        try:
            self._attach()
        except (libvirt.libvirtError, exception.VirtualBMCError) as exc:
            LOG.debug('SOL console reconnect for %(domain)s: %(error)s',
                      {'domain': self.domain_name, 'error': exc})
            self.close()
        finally:
            self._retry_at = time.monotonic() + RECONNECT_INTERVAL
        return b''

    def close(self):
        """Stop the reader thread and discard buffers."""
        self._stop_reader()
        with self._lock:
            self._pending = b''
            self._outbuf = bytearray()
