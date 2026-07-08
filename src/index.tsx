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
  only_when_streaming: boolean;
  debug: boolean;
}

interface ActionResult {
  ok: boolean;
  skipped?: boolean;
  reason?: string;
  error?: string;
}

const getSettings = callable<[], Settings>("get_settings");
const saveSettings = callable<[Settings], Settings>("save_settings");
const wakeHost = callable<[], ActionResult>("wake_host");
const sleepHost = callable<[], ActionResult>("sleep_host");
const onDeckSuspend = callable<[], ActionResult>("on_deck_suspend");
const onDeckResume = callable<[], ActionResult>("on_deck_resume");
const isStreaming = callable<[], boolean>("is_streaming");
const getDebugLog = callable<[], string[]>("get_debug_log");
const clearDebugLog = callable<[], string[]>("clear_debug_log");

function describeResult(res: ActionResult): string {
  if (!res.ok) return `failed: ${res.error}`;
  if (res.skipped) return `skipped (${res.reason ?? "no reason"})`;
  return "sent";
}

function Content() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [streaming, setStreaming] = useState<boolean | null>(null);
  const [debugLog, setDebugLog] = useState<string[]>([]);

  useEffect(() => {
    getSettings().then((s) => {
      setSettings(s);
      if (s.debug) void getDebugLog().then(setDebugLog);
    });
    isStreaming().then(setStreaming);
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
        <PanelSectionRow>
          <ToggleField
            label="Only when using Remote Play"
            description={
              "Sleep only if a Remote Play session is active; wake only if the host was slept by this plugin." +
              (streaming === null ? "" : ` Remote Play now: ${streaming ? "active" : "none"}`)
            }
            checked={settings.only_when_streaming}
            onChange={(v) => update({ only_when_streaming: v })}
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

      <PanelSection title="Debug">
        <PanelSectionRow>
          <ToggleField
            label="Debug mode"
            description="Show the event log below and toast every suspend/resume decision"
            checked={settings.debug}
            onChange={(v) => {
              update({ debug: v });
              if (v) void getDebugLog().then(setDebugLog);
            }}
          />
        </PanelSectionRow>
        {settings.debug && (
          <>
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={() => void getDebugLog().then(setDebugLog)}>
                Refresh log
              </ButtonItem>
            </PanelSectionRow>
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={() => void clearDebugLog().then(setDebugLog)}>
                Clear log
              </ButtonItem>
            </PanelSectionRow>
            <PanelSectionRow>
              <div
                style={{
                  fontSize: "11px",
                  fontFamily: "monospace",
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-all",
                  userSelect: "text",
                  maxHeight: "260px",
                  overflowY: "auto",
                }}
              >
                {debugLog.length === 0
                  ? "(log is empty)"
                  : [...debugLog].reverse().join("\n")}
              </div>
            </PanelSectionRow>
          </>
        )}
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
      const settings = await getSettings();
      if (!res.ok) {
        toaster.toast({ title: "HostSleep", body: `Wake failed: ${res.error}` });
      } else if (settings.debug) {
        toaster.toast({ title: "HostSleep debug", body: `Wake ${describeResult(res)}` });
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
