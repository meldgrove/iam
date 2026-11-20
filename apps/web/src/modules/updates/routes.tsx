import { createRoute, type AnyRoute } from "@tanstack/react-router";
import { appRoute, requireAdmin } from "@/routes/app";
import { UpdatesPage } from "./updates-page";

const updatesRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/admin/updates",
  beforeLoad: requireAdmin(),
  component: UpdatesPage,
});

export const routes: AnyRoute[] = [updatesRoute];
