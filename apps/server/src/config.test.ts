import { afterEach, describe, expect, it } from "vitest";
import { config, resetConfigForTests } from "./config";

const original = { ...process.env };

function configWith(values: Record<string, string | undefined>) {
  for (const [key, value] of Object.entries(values)) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
  resetConfigForTests();
  return () => config();
}

const KEY = "zIi4BBQnf3ojuqE3UQQ1yUmP0zpMlTv44ATyCo-nt9E=";

describe("config", () => {
  afterEach(() => {
    process.env = { ...original };
    resetConfigForTests();
  });

  it("needs the secrets in production", () => {
    expect(
      configWith({ NODE_ENV: "production", APP_URL: "https://iam.example.invalid", AUTH_SECRET: undefined, ENCRYPTION_KEY: undefined }),
    ).toThrow(/AUTH_SECRET: Set a random secret[\s\S]*ENCRYPTION_KEY: Set a Fernet key/);
  });

  it("reads the variables of the Django version", () => {
    const settings = configWith({
      NODE_ENV: "production",
      APP_URL: undefined,
      AUTH_SECRET: undefined,
      CSRF_TRUSTED_ORIGINS: "https://iam.example.invalid/, https://other.example.invalid",
      DJANGO_SECRET_KEY: "x".repeat(50),
      ENCRYPTION_KEY: KEY,
    })();
    expect(settings.APP_URL).toBe("https://iam.example.invalid");
    expect(settings.AUTH_SECRET).toBe("x".repeat(50));
  });

  it("needs Fernet keys", () => {
    expect(configWith({ ENCRYPTION_KEY: "not-a-key" })).toThrow(/ENCRYPTION_KEY: Must be Fernet keys/);
    expect(configWith({ ENCRYPTION_KEY: `${KEY},${KEY}` })().encryptionKeys).toEqual([KEY, KEY]);
  });

  it("refuses a read-only value that is no clear yes or no", () => {
    expect(configWith({ READ_ONLY: "maybe" })).toThrow(/READ_ONLY: Must be 1 or 0/);
    expect(configWith({ READ_ONLY: "yes" })().READ_ONLY).toBe(true);
    expect(configWith({ READ_ONLY: "" })().READ_ONLY).toBe(false);
  });

  it("builds the issuer from Keycloak's URL and realm", () => {
    const settings = configWith({ OIDC_ISSUER: undefined, KEYCLOAK_URL: "https://sso.example.invalid/", KEYCLOAK_REALM: "staff" })();
    expect(settings.oidcIssuer).toBe("https://sso.example.invalid/realms/staff");
    expect(settings.oidcProviderKind).toBe("keycloak");
    expect(settings.oidcProviderName).toBe("Keycloak");
  });

  it("reads the role groups and bootstrap admins", () => {
    const settings = configWith({
      OIDC_ADMIN_GROUPS: " /iam/admins , admins",
      BOOTSTRAP_ADMIN_EMAILS: " Ada@Example.invalid ",
    })();
    expect(settings.adminGroups).toEqual(["/iam/admins", "admins"]);
    expect(settings.bootstrapAdminEmails).toEqual(["ada@example.invalid"]);
  });

  it("allows the development sign-in only in development", () => {
    expect(configWith({ NODE_ENV: "production", APP_URL: "https://iam.example.invalid", ENCRYPTION_KEY: KEY, DEV_LOGIN: "1" })).toThrow(
      /DEV_LOGIN: Only for NODE_ENV=development/,
    );
  });

  it("checks the time zone", () => {
    expect(configWith({ TZ: "Europe/Berlin" })().TZ).toBe("Europe/Berlin");
    expect(configWith({ TZ: "Mars/Olympus" })).toThrow(/Not a time zone/);
  });

  it("switches the update button off with an empty UPDATER_URL, and defaults it otherwise", () => {
    expect(configWith({ UPDATER_URL: undefined })().UPDATER_URL).toBe("http://updater:8080");
    expect(configWith({ UPDATER_URL: "" })().UPDATER_URL).toBe("");
    expect(configWith({ UPDATER_URL: "http://localhost:8080/" })().UPDATER_URL).toBe("http://localhost:8080");
  });
});
