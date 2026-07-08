import asyncio
import json
import os
import re
import socket
import urllib.request

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
}

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")
# Only plain IPs/hostnames may be interpolated into the sleep URL
HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+$")


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


class Plugin:
    async def _main(self):
        self.settings = dict(DEFAULTS)
        # Set when we sleep the host on suspend; gates the wake on resume.
        # In-memory only: the backend process survives Deck suspend.
        self.slept_host = False
        try:
            with open(SETTINGS_PATH) as f:
                stored = json.load(f)
            self.settings.update({k: stored[k] for k in DEFAULTS if k in stored})
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, OSError) as e:
            decky.logger.error("Failed to load settings, using defaults: %s", e)
        decky.logger.info("HostSleep loaded (host=%s mac=%s mode=%s)",
                          self.settings["host_ip"], self.settings["mac"],
                          self.settings["sleep_mode"])

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

    # ---- suspend/resume hooks (respect the toggles) --------------------

    async def on_deck_suspend(self):
        if not self.settings["sleep_on_suspend"]:
            return {"ok": True, "skipped": True}
        if self.settings["only_when_streaming"]:
            if not _is_streaming():
                decky.logger.info("on_deck_suspend: no Remote Play session, skipping")
                return {"ok": True, "skipped": True}
        result = await self.sleep_host()
        self.slept_host = result.get("ok", False)
        return result

    async def on_deck_resume(self):
        if not self.settings["wake_on_resume"]:
            return {"ok": True, "skipped": True}
        if self.settings["only_when_streaming"] and not self.slept_host:
            decky.logger.info("on_deck_resume: host was not slept by us, skipping wake")
            return {"ok": True, "skipped": True}
        self.slept_host = False
        return await self.wake_host()

    # ---- actions (unconditional; also used by the Test buttons) --------

    async def wake_host(self):
        try:
            packet = _magic_packet(_parse_mac(self.settings["mac"]))
        except ValueError as e:
            decky.logger.error("wake_host: %s", e)
            return {"ok": False, "error": str(e)}

        port = int(self.settings["wol_port"] or 9)
        targets = [(self.settings["broadcast_ip"] or "255.255.255.255", port)]
        host = (self.settings["host_ip"] or "").strip()
        if host:
            targets.append((host, port))

        try:
            # burst of 3 — WOL is fire-and-forget, redundancy is cheap
            for i in range(3):
                _send_udp(packet, targets)
                if i < 2:
                    await asyncio.sleep(0.3)
        except OSError as e:
            decky.logger.error("wake_host: send failed: %s", e)
            return {"ok": False, "error": str(e)}

        decky.logger.info("wake_host: sent WOL to %s", targets)
        return {"ok": True}

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
            decky.logger.error("sleep_host: %s -> %s", url, e)
            return {"ok": False, "error": str(e)}

        decky.logger.info("sleep_host: %s -> HTTP %s", url, status)
        return {"ok": True}

    async def _sleep_udp(self):
        try:
            mac = _parse_mac(self.settings["mac"])
        except ValueError as e:
            decky.logger.error("sleep_host: %s", e)
            return {"ok": False, "error": str(e)}

        # sleep-on-lan convention: magic packet built from the REVERSED MAC
        packet = _magic_packet(mac[::-1])
        host = (self.settings["host_ip"] or "").strip()
        target = (host or self.settings["broadcast_ip"] or "255.255.255.255",
                  int(self.settings["wol_port"] or 9))
        try:
            _send_udp(packet, [target])
            await asyncio.sleep(0.2)
            _send_udp(packet, [target])
        except OSError as e:
            decky.logger.error("sleep_host: send failed: %s", e)
            return {"ok": False, "error": str(e)}

        decky.logger.info("sleep_host: sent reversed-MAC packet to %s", target)
        return {"ok": True}
