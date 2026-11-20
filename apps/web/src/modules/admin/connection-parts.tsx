import { Badge } from "@meldgrove/ui/badge";
import { CONNECTION_MODE_LABELS, CONNECTION_STATUS_LABELS, type ConnectionMode, type ConnectionStatus } from "@iam/shared/labels";
import { StatusBadge } from "@/components/status";

/** The state of a connection: its status when it is on, else "Off". */
export function ConnectionState({ enabled, status }: { enabled: boolean; status: ConnectionStatus }) {
  if (!enabled) return <Badge variant="outline">Off</Badge>;
  return <StatusBadge value={status} labels={CONNECTION_STATUS_LABELS} />;
}

/** The short name of a mode: "Automatic", "Manual", "Read only". */
export function shortMode(mode: ConnectionMode): string {
  return CONNECTION_MODE_LABELS[mode].split(":")[0]!;
}
