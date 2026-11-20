import { Card, CardContent } from "@meldgrove/ui/card";
import { UpdatesSettings, useUpdatesController } from "@meldgrove/ui/updates";
import { Page, PageHeader } from "@meldgrove/ui/page";
import { useIsAdmin } from "@/hooks/use-me";

/** Admins: the installed version, the update channel and automatic installs. */
export function UpdatesPage() {
  // The update status exists for admins only (AdminUpdates); the route checks the role on load,
  // this covers a role that changes while the page is open.
  return useIsAdmin() ? <UpdatesContent /> : null;
}

function UpdatesContent() {
  const controller = useUpdatesController();
  return (
    <Page className="max-w-3xl">
      <PageHeader
        title={controller.t("updates")}
        description={controller.t("sectionText", { app: controller.appName })}
      />
      <Card>
        <CardContent>
          <UpdatesSettings />
        </CardContent>
      </Card>
    </Page>
  );
}
