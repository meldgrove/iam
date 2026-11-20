import type { ReactNode } from "react";
import { UpdatesProvider } from "@meldgrove/ui/updates";
import { useIsAdmin } from "@/hooks/use-me";
import { apiUrl, sessionEnded } from "@/lib/api";

/** The deployment guide of the released version, for admins without an update service. */
const DEPLOYMENT_GUIDE =
  "https://github.com/meldgrove/iam/blob/stable/docs/deployment.md#automatic-updates";

/**
 * Admins: the update status that the header button and the Updates page
 * share (polling, installing, following the restart). Everyone else gets
 * 404 from the routes, so nothing asks for them.
 */
export function AdminUpdates({ appName, children }: { appName: string; children: ReactNode }) {
  if (!useIsAdmin()) return children;
  return (
    <UpdatesProvider
      appName={appName}
      // The systemd unit of a server set up as in docs/deployment.md (/srv/iam).
      project="iam"
      url={apiUrl("/admin/updates")}
      guideUrl={DEPLOYMENT_GUIDE}
      // US English dates, as everywhere in the app.
      dateLocale="en-US"
      // An ended session (401, sign-in page): the app's sign-in handler, as for a 401 of any
      // API request. A lost admin role (403, 404) only stops the requests.
      onSignedOut={(reason) => {
        if (reason === "signed-out") sessionEnded();
      }}
    >
      {children}
    </UpdatesProvider>
  );
}
