import { callable, definePlugin } from "@decky/api";
import {
  ButtonItem,
  Field,
  PanelSection,
  PanelSectionRow,
  ProgressBar,
  staticClasses,
} from "@decky/ui";
import { useEffect, useState } from "react";
import { FaRobot } from "react-icons/fa";

interface RateWindow {
  label: string;
  used_percent: number;
  resets_in: number | null;
}

interface ProviderUsage {
  provider: string;
  plan: string;
  email: string;
  windows: RateWindow[];
  cost: { used: number; limit: number; currency: string } | null;
  error: string | null;
}

interface UsageSnapshot {
  providers: ProviderUsage[];
  updated_at: number;
}

const getUsage = callable<[force: boolean], UsageSnapshot>("get_usage");

const formatDuration = (seconds: number): string => {
  if (seconds <= 0) return "now";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return d > 0 ? `${d}d ${h}h` : `${h}h ${m}m`;
};

function ProviderSection({ usage }: { usage: ProviderUsage }) {
  const title = usage.plan ? `${usage.provider} [${usage.plan}]` : usage.provider;
  return (
    <PanelSection title={title}>
      {usage.error && (
        <PanelSectionRow>
          <Field label="Error" description={usage.error} bottomSeparator="none" />
        </PanelSectionRow>
      )}
      {usage.windows.map((w) => {
        const left = Math.max(0, 100 - w.used_percent);
        return (
          <PanelSectionRow key={w.label}>
            {/* ProgressBarWithInfo forces a 270px min-width beside the label, which overflows the QAM panel */}
            <Field
              label={w.label}
              description={w.resets_in != null ? `resets in ${formatDuration(w.resets_in)}` : undefined}
              bottomSeparator="none"
            >
              {`${left.toFixed(0)}% left`}
            </Field>
            <ProgressBar nProgress={left} />
          </PanelSectionRow>
        );
      })}
      {usage.cost && (
        <PanelSectionRow>
          <Field label="Extra usage (monthly)" bottomSeparator="none">
            {`$${usage.cost.used.toFixed(2)} / $${usage.cost.limit.toFixed(2)}`}
          </Field>
        </PanelSectionRow>
      )}
    </PanelSection>
  );
}

function Content() {
  const [snapshot, setSnapshot] = useState<UsageSnapshot | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = async (force: boolean) => {
    setLoading(true);
    try {
      setSnapshot(await getUsage(force));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh(false);
  }, []);

  return (
    <>
      {snapshot?.providers.map((p) => <ProviderSection key={p.provider} usage={p} />)}
      <PanelSection>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={loading}
            onClick={() => refresh(true)}
            description={
              snapshot ? `Updated ${new Date(snapshot.updated_at * 1000).toLocaleTimeString()}` : undefined
            }
          >
            {loading ? "Refreshing..." : "Refresh"}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}

export default definePlugin(() => ({
  name: "AI Usage",
  titleView: <div className={staticClasses.Title}>AI Usage</div>,
  content: <Content />,
  icon: <FaRobot />,
}));
