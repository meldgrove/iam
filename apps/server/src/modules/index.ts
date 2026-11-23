import connections from "./connections";
import dashboard from "./dashboard";
import identityRequests from "./identity-requests";
import meta from "./meta";
import people from "./people";
import policy from "./policy";
import settings from "./settings";
import updates from "./updates";
import users from "./users";
import work from "./work";

/** Routers mounted under /api/v1. */
export const API_MODULES = [
  meta,
  dashboard,
  people,
  identityRequests,
  policy,
  work,
  connections,
  users,
  settings,
  updates,
];
