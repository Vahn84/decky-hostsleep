import asyncio
import json
import os
import re
import socket
import time
import urllib.request
from collections import deque

import decky

SETTINGS_PATH = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json")

DEFAULTS = {
    "mac": "",
    "host_ip": "",
    "broadcast_ip": "255.255.255.255",
    # "http" -> GET http://host:sleep_port/sleep (sleep-on-lan REST API)
    # "udp"  -> reversed-MAC magic packet (sleep-on-lan UDP listener)
    "sleep_mode": "http",
    "sleep_port": 8009,
    "wol_port": 9,
    "sleep_on_suspend": True,
    "wake_on_resume": True,
    # Gate on an active Remote Play client session: only sleep the host if the
    # Deck is streaming at suspend time, and only wake it if we slept it.
    "only_when_streaming": True,
    # Show the event log in the plugin panel and toast debug info on resume
    "debug": False,
}

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")
# Only plain IPs/hostnames may be interpolated into the sleep URL
HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+$")

# Wake is retry-until-acknowledged: broadcast delivery to a sleeping wired NIC
# through a client-isolating router is stochastic (verified: identical packets
# sometimes wake the host, sometimes vanish), so one volley is a coin flip.
# Keep sending until the host is heard announcing itself, or the window closes.
WAKE_WINDOW_S = 45
WAKE_RETRY_S = 3.0
ETH_P_ALL = 0x0003


def _parse_mac(mac):
    mac = (mac or "").strip()
    if not MAC_RE.match(mac):
        raise ValueError(f"Invalid MAC address: {mac!r}")
    return bytes(int(p, 16) for p in re.split("[:-]", mac))


def _magic_packet(mac_bytes):
    return b"\xff" * 6 + mac_bytes * 16


def _is_streaming():
    """True if Steam's Remote Play client process is running on the Deck.

    /proc/<pid>/comm truncates to 15 chars, so read cmdline and match the
    executable's basename against 'streaming_client'.
    """
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv0 = f.read().split(b"\x00", 1)[0]
        except OSError:
            continue
        if os.path.basename(argv0.decode(errors="replace")) == "streaming_client":
            return True
    return False


def _send_udp(packet, targets):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        for target in targets:
            sock.sendto(packet, target)
    finally:
        sock.close()


def _open_host_listener():
    """Raw socket to hear the host announce itself after resume, or None.

    Needs root (the Decky backend has it) and Linux AF_PACKET; returns None
    anywhere that's unavailable so wake falls back to blind bursts.
    """
    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
        sock.setblocking(False)
        return sock
    except (AttributeError, OSError):
        return None


async def _heard_host(sock, mac_bytes, deadline):
    """True if a broadcast/multicast frame sourced from mac_bytes arrives.

    A Windows box resuming from S3 announces itself (gratuitous ARP, DHCP,
    NetBIOS) — all broadcast, which crosses client-isolating routers. Unicast
    from the host is deliberately NOT counted: NICs with ARP offload answer
    ARP unicast while still asleep, which would be a false "awake".
    """
    drained = 0
    while time.monotonic() < deadline:
        try:
            frame = sock.recv(2048)
        except (BlockingIOError, InterruptedError):
            await asyncio.sleep(0.05)
            continue
        except OSError:
            return False
        if len(frame) >= 12 and frame[6:12] == mac_bytes and frame[0] & 1:
            return True
        drained += 1
        if drained % 50 == 0:
            await asyncio.sleep(0)  # don't starve the event loop on busy LANs
    return False


class Plugin:
    def _dbg(self, msg):
        # Ring buffer surfaced in the panel's Debug section; always recorded
        # (cheap), only displayed when the debug toggle is on.
        self.debug_log.append(f"{time.strftime('%H:%M:%S')} {msg}")
        decky.logger.info(msg)

    async def _main(self):
        self.settings = dict(DEFAULTS)
        # Set when we sleep the host on suspend; gates the wake on resume.
        # In-memory only: the backend process survives Deck suspend.
        self.slept_host = False
        self.debug_log = deque(maxlen=100)
        try:
            with open(SETTINGS_PATH) as f:
                stored = json.load(f)
            self.settings.update({k: stored[k] for k in DEFAULTS if k in stored})
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, OSError) as e:
            decky.logger.error("Failed to load settings, using defaults: %s", e)
        self._dbg("plugin loaded (host=%s mac=%s mode=%s)" % (
            self.settings["host_ip"], self.settings["mac"],
            self.settings["sleep_mode"]))

    async def _unload(self):
        decky.logger.info("HostSleep unloaded")

    # ---- settings ----------------------------------------------------

    async def get_settings(self):
        return self.settings

    async def save_settings(self, settings):
        self.settings.update({k: settings[k] for k in DEFAULTS if k in settings})
        os.makedirs(decky.DECKY_PLUGIN_SETTINGS_DIR, exist_ok=True)
        with open(SETTINGS_PATH, "w") as f:
            json.dump(self.settings, f, indent=2)
        return self.settings

    async def is_streaming(self):
        return _is_streaming()

    async def get_debug_log(self):
        return list(self.debug_log)

    async def clear_debug_log(self):
        self.debug_log.clear()
        return []

    # ---- suspend/resume hooks (respect the toggles) --------------------

    async def on_deck_suspend(self):
        self._dbg("suspend hook fired")
        if not self.settings["sleep_on_suspend"]:
            self._dbg("suspend: sleep_on_suspend is off, skipping")
            return {"ok": True, "skipped": True, "reason": "disabled"}
        if self.settings["only_when_streaming"]:
            streaming = _is_streaming()
            self._dbg(f"suspend: Remote Play session {'active' if streaming else 'not found'}")
            if not streaming:
                return {"ok": True, "skipped": True, "reason": "not streaming"}
        result = await self.sleep_host()
        self.slept_host = result.get("ok", False)
        return result

    async def on_deck_resume(self):
        self._dbg("resume hook fired")
        if not self.settings["wake_on_resume"]:
            self._dbg("resume: wake_on_resume is off, skipping")
            return {"ok": True, "skipped": True, "reason": "disabled"}
        if self.settings["only_when_streaming"] and not self.slept_host:
            self._dbg("resume: host was not slept by us, skipping wake")
            return {"ok": True, "skipped": True, "reason": "host not slept by plugin"}
        self.slept_host = False
        return await self.wake_host()

    # ---- actions (unconditional; also used by the Test buttons) --------

    def _bcast_targets(self, ports):
        # Global broadcast, the configured broadcast, and the /24 directed
        # broadcast derived from the host IP — different routers flood different
        # broadcast forms, and some (client-isolating ones) only pass the directed
        # /24 form across the wireless/wired boundary, not global 255.255.255.255.
        bcasts = {"255.255.255.255", (self.settings["broadcast_ip"] or "255.255.255.255")}
        host = (self.settings["host_ip"] or "").strip()
        if host.count(".") == 3:
            bcasts.add(host.rsplit(".", 1)[0] + ".255")
        return [(b, p) for b in bcasts for p in ports]

    async def wake_host(self):
        try:
            mac_bytes = _parse_mac(self.settings["mac"])
        except ValueError as e:
            self._dbg(f"wake ERROR: {e}")
            return {"ok": False, "error": str(e)}
        packet = _magic_packet(mac_bytes)

        port = int(self.settings["wol_port"] or 9)
        # WOL magic packets are port-agnostic (the NIC scans the payload), so hit
        # 9 and 7; add the host unicast for non-isolating networks where that works.
        targets = self._bcast_targets({port, 7})
        host = (self.settings["host_ip"] or "").strip()
        if host:
            targets.append((host, port))

        listener = _open_host_listener()
        if listener is None:
            return await self._wake_blind(packet, targets)

        started = time.monotonic()
        deadline = started + WAKE_WINDOW_S
        attempts = send_errors = 0
        confirmed = False
        try:
            while not confirmed and time.monotonic() < deadline:
                attempts += 1
                try:
                    _send_udp(packet, targets)
                except OSError as e:
                    # Right after Deck resume Wi-Fi may still be reassociating;
                    # sends fail transiently — keep the window open and retry.
                    send_errors += 1
                    self._dbg(f"wake: send failed (attempt {attempts}): {e}")
                confirmed = await _heard_host(
                    listener, mac_bytes,
                    min(time.monotonic() + WAKE_RETRY_S, deadline))
        finally:
            listener.close()

        elapsed = round(time.monotonic() - started, 1)
        if confirmed:
            self._dbg(f"wake: host confirmed awake after {elapsed}s ({attempts} attempts)")
            return {"ok": True, "confirmed": True, "elapsed_s": elapsed, "attempts": attempts}
        if send_errors == attempts:
            self._dbg(f"wake ERROR: every send failed across {elapsed}s")
            return {"ok": False, "error": "network unreachable for the whole wake window"}
        self._dbg(f"wake: NO confirmation after {elapsed}s ({attempts} attempts, {send_errors} send errors)")
        return {"ok": True, "confirmed": False, "elapsed_s": elapsed, "attempts": attempts}

    async def _wake_blind(self, packet, targets):
        # No raw-socket privilege (not root / not Linux): the old fire-and-forget
        # stretched burst, honestly reported as unconfirmed.
        try:
            for i in range(8):
                _send_udp(packet, targets)
                if i < 7:
                    await asyncio.sleep(0.7)
        except OSError as e:
            self._dbg(f"wake ERROR: send failed: {e}")
            return {"ok": False, "error": str(e)}
        self._dbg(f"wake: sent blind burst to {targets} (no listener available)")
        return {"ok": True, "confirmed": None}

    async def sleep_host(self):
        if self.settings["sleep_mode"] == "udp":
            return await self._sleep_udp()
        return await self._sleep_http()

    async def _sleep_http(self):
        host = (self.settings["host_ip"] or "").strip()
        if not host:
            return {"ok": False, "error": "Host IP is not set"}
        if not HOST_RE.match(host):
            return {"ok": False, "error": f"Invalid host: {host!r}"}
        url = f"http://{host}:{int(self.settings['sleep_port'] or 8009)}/sleep"

        def _get():
            with urllib.request.urlopen(url, timeout=3) as resp:
                return resp.status

        try:
            status = await asyncio.to_thread(_get)
        except Exception as e:
            self._dbg(f"sleep ERROR: {url} -> {e}")
            return {"ok": False, "error": str(e)}

        self._dbg(f"sleep: {url} -> HTTP {status}")
        return {"ok": True}

    async def _sleep_udp(self):
        try:
            mac = _parse_mac(self.settings["mac"])
        except ValueError as e:
            self._dbg(f"sleep ERROR: {e}")
            return {"ok": False, "error": str(e)}

        # sleep-on-lan convention: magic packet built from the REVERSED MAC.
        # Sent to the BROADCAST address (not the host's unicast IP): routers that
        # isolate wireless from wired clients drop unicast between them but still
        # flood layer-2 broadcast across, so broadcast reliably reaches a wired
        # host from a wireless client. This mirrors how WOL wake already works.
        packet = _magic_packet(mac[::-1])
        port = int(self.settings["wol_port"] or 9)
        # sol listens on UDP 9 and 7 by default; broadcast to both across every
        # broadcast form (see _bcast_targets).
        targets = self._bcast_targets({port, 7})
        try:
            # Exactly ONE send, unlike the wake burst: extra sleep packets are
            # buffered through the suspend transition and sol replays them on
            # resume, putting the host straight back to sleep (SR-G/sleep-on-lan
            # #22). The host NIC is awake here, so delivery doesn't need
            # redundancy; a lost packet just leaves the host awake.
            _send_udp(packet, targets)
        except OSError as e:
            self._dbg(f"sleep ERROR: send failed: {e}")
            return {"ok": False, "error": str(e)}

        self._dbg(f"sleep: sent reversed-MAC broadcast to {targets}")
        return {"ok": True}
