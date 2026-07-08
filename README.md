# HostSleep — Decky plugin

Sleep your Steam Remote Play **host PC** when the Steam Deck suspends, and wake it
(Wake-on-LAN) when the Deck resumes — **without closing the running game**.
Windows sleep keeps every process frozen in RAM, so the game is exactly where you
left it when the host wakes up.

Designed for **native Steam Remote Play** (not MoonDeck/Moonlight): Remote Play keeps
the game running when the stream disconnects, so suspend → wake → reconnect resumes
your session.

## How it works

| Deck event | Plugin action |
|---|---|
| Suspend (power button / lid / idle) | Sends a sleep command to the host **before** Wi-Fi drops |
| Resume | Sends a burst of 3 WOL magic packets to wake the host |

The sleep command targets [sleep-on-lan](https://github.com/SR-G/sleep-on-lan)
running on the host, in one of two modes (configurable in the plugin panel):

- **HTTP** (default): `GET http://<host>:8009/sleep`
- **UDP**: a magic packet built from the *reversed* MAC address

## Host PC setup (Windows)

1. Download [sleep-on-lan](https://github.com/SR-G/sleep-on-lan/releases) and set it
   up to run at boot (it ships a `sol.exe --install-service` style setup; see its README).
   Default config listens on UDP 9 (reversed-MAC) and HTTP 8009 — both work as-is.
2. Allow `sol.exe` through Windows Firewall (private network).
3. Wake-on-LAN must already work (it does if MoonDeck wakes this PC): NIC power
   management → "Only allow a magic packet to wake the computer", wired Ethernet recommended.
4. Give the PC a static IP or DHCP reservation.

## Deck setup

1. Install the plugin (see Deploy below).
2. Open the HostSleep panel in Decky (⋯ menu) and fill in the host's **MAC** and **IP**.
3. Use the **Test** buttons to verify both directions before trusting the automation:
   - "Sleep host now" → PC should suspend, game still running after manual wake.
   - "Wake host now" → PC should wake.

## Build

```bash
npm install
npm run build        # produces dist/index.js
```

## Deploy to the Deck

Prereq (one-time, on the Deck in Desktop Mode): set a user password (`passwd`) and
enable SSH (`sudo systemctl enable --now sshd`).

```bash
DECK_HOST=deck@<deck-ip> ./deploy.sh
```

The script builds, packages, copies to the Deck, installs into
`/home/deck/homebrew/plugins/HostSleep`, and restarts the Decky loader.
It also leaves `build/HostSleep.zip`, installable via
Decky → Settings → Developer → *Install plugin from ZIP*.

## Troubleshooting

- Plugin logs: `/home/deck/homebrew/logs/HostSleep/` on the Deck.
- Suspend fired but host didn't sleep → test `http://<host>:8009/sleep` from a browser
  on the LAN; check sol is running and the firewall rule.
- Host wakes but Remote Play doesn't auto-reconnect → known Steam quirk; reconnect
  from the Deck, the game is still running.
- Online games will drop their server connection while the host sleeps — that's
  inherent to sleeping the PC, not fixable client-side.

## Perimeter (ops notes)

- **Rollback / kill switch**: toggle off both behaviors in the panel, or uninstall the
  plugin from Decky. No host-side state beyond the sol service.
- **Testing**: manual Test buttons in the panel exercise the exact code paths the
  automation uses.
- **Security**: backend validates MAC/host formats before building packets/URLs; sol's
  HTTP endpoint is LAN-only — do not port-forward it.
- **Revisit if**: SteamOS changes the `SteamClient.System.RegisterForOnSuspendRequest`
  API (plugin logs will show hook registration failures), or the host moves to a
  network where UDP broadcast is filtered.
