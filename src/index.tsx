import {
  ButtonItem,
  DropdownItem,
  PanelSection,
  PanelSectionRow,
  TextField,
  ToggleField,
  staticClasses,
} from "@decky/ui";
import { callable, definePlugin, toaster } from "@decky/api";
import { useEffect, useState } from "react";
import { FaBed } from "react-icons/fa";

interface Settings {
  mac: string;
  host_ip: string;
  broadcast_ip: string;
  sleep_mode: "http" | "udp";
  sleep_port: number;
  wol_port: number;
  sleep_on_suspend: boolean;
  wake_on_resume: boolean;
}

interface ActionResult {
  ok: boolean;
  skipped?: boolean;
  error?: string;
}

const getSettings = callable<[], Settings>("get_settings");
const saveSettings = callable<[Settings], Settings>("save_settings");
const wakeHost = callable<[], ActionResult>("wake_host");
const sleepHost = callable<[], ActionResult>("sleep_host");
const onDeckSuspend = callable<[], ActionResult>("on_deck_suspend");
const onDeckResume = callable<[], ActionResult>("on_deck_resume");

function Content() {
  const [settings, setSettings] = useState<Settings | null>(null);

  useEffect(() => {
    getSettings().then(setSettings);
  }, []);

  if (!settings) {
    return <PanelSection title="Loading..." />;
  }

  const update = (patch: Partial<Settings>) => {
    const next = { ...settings, ...patch };
    setSettings(next);
    void saveSettings(next);
  };

  const act = async (label: string, fn: () => Promise<ActionResult>) => {
    const res = await fn();
    toaster.toast({
      title: "HostSleep",
      body: res.ok ? `${label} command sent` : `${label} failed: ${res.error}`,
    });
  };

  return (
    <>
      <PanelSection title="Behavior">
        <PanelSectionRow>
          <ToggleField
            label="Sleep host when Deck suspends"
            checked={settings.sleep_on_suspend}
            onChange={(v) => update({ sleep_on_suspend: v })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Wake host when Deck resumes"
            checked={settings.wake_on_resume}
            onChange={(v) => update({ wake_on_resume: v })}
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Host PC">
        <PanelSectionRow>
          <TextField
            label="MAC address"
            description="e.g. AA:BB:CC:DD:EE:FF"
            value={settings.mac}
            onChange={(e) => update({ mac: e.target.value })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <TextField
            label="IP address"
            description="Static IP or DHCP reservation recommended"
            value={settings.host_ip}
            onChange={(e) => update({ host_ip: e.target.value })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <DropdownItem
            label="Sleep command"
            rgOptions={[
              { data: "http", label: "HTTP (sleep-on-lan REST)" },
              { data: "udp", label: "UDP reversed-MAC packet" },
            ]}
            selectedOption={settings.sleep_mode}
            onChange={(opt) => update({ sleep_mode: opt.data as Settings["sleep_mode"] })}
          />
        </PanelSectionRow>
        {settings.sleep_mode === "http" && (
          <PanelSectionRow>
            <TextField
              label="sleep-on-lan HTTP port"
              value={String(settings.sleep_port)}
              onChange={(e) => {
                const port = parseInt(e.target.value, 10);
                if (!isNaN(port)) update({ sleep_port: port });
              }}
            />
          </PanelSectionRow>
        )}
      </PanelSection>

      <PanelSection title="Test">
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => void act("Sleep", sleepHost)}>
            Sleep host now
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => void act("Wake", wakeHost)}>
            Wake host now
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}

export default definePlugin(() => {
  // SteamClient is the Steam UI's own global; not typed by @decky/ui
  const steamClient = (window as any).SteamClient;

  const suspendReg = steamClient?.System?.RegisterForOnSuspendRequest?.(async () => {
    try {
      const res = await onDeckSuspend();
      if (!res.ok) console.error("HostSleep: sleep on suspend failed:", res.error);
    } catch (e) {
      console.error("HostSleep: suspend hook error:", e);
    }
  });

  const resumeReg = steamClient?.System?.RegisterForOnResumeFromSuspend?.(async () => {
    try {
      const res = await onDeckResume();
      if (!res.ok) {
        toaster.toast({ title: "HostSleep", body: `Wake failed: ${res.error}` });
      }
    } catch (e) {
      console.error("HostSleep: resume hook error:", e);
    }
  });

  return {
    name: "HostSleep",
    titleView: <div className={staticClasses.Title}>HostSleep</div>,
    content: <Content />,
    icon: <FaBed />,
    onDismount() {
      suspendReg?.unregister();
      resumeReg?.unregister();
    },
  };
});
