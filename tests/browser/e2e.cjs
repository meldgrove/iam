// End-to-end check against the development stack (compose.dev.yaml): signs in
// through the real Keycloak, connects Keycloak and Mailpit in the admin
// section, onboards and offboards a person, and checks Keycloak and Mailpit
// directly. Needs the app on BASE_URL and `manage.py run_worker` running.
//
//   node tests/browser/e2e.cjs
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const BASE_URL = process.env.BASE_URL || "http://localhost:8000";
const KEYCLOAK_URL = process.env.KEYCLOAK_URL || "http://localhost:8098";
const MAILPIT_URL = process.env.MAILPIT_URL || "http://localhost:8027";
const SMTP_HOST = process.env.SMTP_HOST || "localhost";
const SMTP_PORT = process.env.SMTP_PORT || "1027";
const OUTPUT_DIR = process.env.OUTPUT_DIR || path.join(__dirname, "../../test-results");
const RUN = Date.now().toString(36);
const LOGIN = `grace.${RUN}`;
const WORK_EMAIL = `${LOGIN}@example.invalid`;

fs.mkdirSync(OUTPUT_DIR, { recursive: true });

async function keycloakAdmin(pathname) {
  const token = await fetch(`${KEYCLOAK_URL}/realms/master/protocol/openid-connect/token`, {
    method: "POST",
    body: new URLSearchParams({ grant_type: "password", client_id: "admin-cli", username: "admin", password: "admin" }),
  }).then((r) => r.json());
  const response = await fetch(`${KEYCLOAK_URL}/admin/realms/iam${pathname}`, {
    headers: { Authorization: `Bearer ${token.access_token}` },
  });
  return response.json();
}

async function shot(page, name) {
  await page.screenshot({ path: path.join(OUTPUT_DIR, `${name}.png`), fullPage: true });
}

async function waitForWorkflow(page, status) {
  // The start button submits a form that redirects here; a reload before the
  // redirect lands races the navigation and fails.
  await page.waitForURL(/\/workflows\/\d+\/$/);
  for (let i = 0; i < 40; i += 1) {
    await page.reload();
    const text = await page.locator("h1").innerText();
    if (text.includes(status)) return;
    await page.waitForTimeout(1000);
  }
  throw new Error(`Workflow did not reach "${status}": ${await page.locator("h1").innerText()}`);
}

async function fillConnection(page, values) {
  for (const [name, value] of Object.entries(values)) {
    const field = page.locator(`[name="cfg_${name}"], [name="${name}"]`).first();
    const type = await field.getAttribute("type");
    if (type === "checkbox") {
      if (value) await field.check();
      else await field.uncheck();
    } else if ((await field.evaluate((el) => el.tagName)) === "SELECT") {
      await field.selectOption(value);
    } else {
      await field.fill(value);
    }
  }
}

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 860 } });
  page.on("pageerror", (error) => console.error("Page error:", error));

  // Sign in through Keycloak; /iam/admins makes the user an admin.
  await page.goto(`${BASE_URL}/`);
  assert.match(page.url(), /\/login\//);
  await shot(page, "01-login");
  await page.getByRole("link", { name: "Sign in with Keycloak" }).click();
  await page.waitForURL(/localhost:8098/);
  await page.fill("#username", "admin");
  await page.fill("#password", "admin");
  await page.click("#kc-login");
  await page.waitForURL(`${BASE_URL}/`);
  assert.match(await page.locator(".who").innerText(), /Admin/);
  console.log("Signed in through Keycloak as admin");

  // Connect Keycloak in the admin section.
  await page.goto(`${BASE_URL}/admin/connections/new/keycloak/`);
  await fillConnection(page, {
    name: `Keycloak ${RUN}`,
    base_url: KEYCLOAK_URL,
    realm: "iam",
    client_id: "iam-provisioner",
    client_secret: "iam-dev-provisioner-secret",
    send_setup_email: true,
    enabled: true,
  });
  await page.getByRole("button", { name: "Save" }).click();
  await page.getByRole("button", { name: "Test connection" }).click();
  await page.waitForSelector(".messages li");
  const testMessage = await page.locator(".messages li").first().innerText();
  assert.match(testMessage, /user/i, testMessage);
  console.log(`Keycloak test: ${testMessage}`);
  await page.getByRole("button", { name: "Import groups" }).click();
  await page.getByText("/apps/wiki", { exact: true }).waitFor();
  await shot(page, "02-keycloak-connection");
  const keycloakUrl = page.url();

  // Connect Mailpit as the email connection.
  await page.goto(`${BASE_URL}/admin/connections/new/smtp/`);
  await fillConnection(page, {
    name: `Mailpit ${RUN}`,
    host: SMTP_HOST,
    port: SMTP_PORT,
    security: "none",
    from_address: "iam@example.invalid",
    enabled: true,
  });
  await page.getByRole("button", { name: "Save" }).click();
  await page.getByRole("button", { name: "Test connection" }).click();
  await page.waitForSelector(".messages li.success");

  // Policy: an access profile with two app groups and a team with its group.
  await page.goto(`${BASE_URL}/profiles/new/`);
  await page.fill("[name=name]", `Staff ${RUN}`);
  await page.locator(".grant", { hasText: `Keycloak ${RUN}` }).locator("input[name=grant]").check();
  const grant = page.locator(".grant", { hasText: `Keycloak ${RUN}` });
  await grant.locator("label", { hasText: "/apps/wiki" }).locator("input").check();
  await grant.locator("label", { hasText: "/apps/chat" }).locator("input").check();
  await page.getByRole("button", { name: "Save" }).click();
  await page.getByRole("heading", { name: "Grants" }).waitFor();
  await page.goto(`${BASE_URL}/teams/new/`);
  await page.fill("[name=name]", `Product ${RUN}`);
  await page.locator(".grant", { hasText: `Keycloak ${RUN}` }).locator("label", { hasText: "/teams/product" }).locator("input").check();
  await page.getByRole("button", { name: "Save" }).click();

  // Add a person and onboard them.
  await page.goto(`${BASE_URL}/people/new/`);
  await page.fill("[name=first_name]", "Grace");
  await page.fill("[name=last_name]", `Hopper ${RUN}`);
  await page.fill("[name=work_email]", WORK_EMAIL);
  await page.fill("[name=personal_email]", `${LOGIN}@personal.example.invalid`);
  await page.selectOption("[name=team]", { label: `Product ${RUN}` });
  await page.selectOption("[name=access_profile]", { label: `Staff ${RUN}` });
  await page.getByRole("button", { name: "Save" }).click();
  await page.getByRole("button", { name: "Start onboarding" }).click();
  await waitForWorkflow(page, "Completed");
  await shot(page, "03-onboarding-completed");
  console.log("Onboarding completed");

  let [user] = await keycloakAdmin(`/users?username=${LOGIN}&exact=true`);
  assert.ok(user, "Keycloak user was created");
  assert.equal(user.enabled, true);
  assert.equal(user.email, WORK_EMAIL);
  let groups = (await keycloakAdmin(`/users/${user.id}/groups`)).map((g) => g.path).sort();
  assert.deepEqual(groups, ["/apps/chat", "/apps/wiki", "/teams/product"]);
  console.log(`Keycloak user ${LOGIN}: enabled, groups ${groups.join(", ")}`);

  const messages = await fetch(`${MAILPIT_URL}/api/v1/search?query=${encodeURIComponent(`to:${WORK_EMAIL}`)}`).then((r) => r.json());
  assert.ok(messages.messages_count >= 1, "Keycloak sent the setup email");
  console.log(`Mailpit: setup email "${messages.messages[0].Subject}" to ${WORK_EMAIL}`);

  const personUrl = page.url().replace(/workflows\/\d+\//, "");
  await page.getByRole("link", { name: `Grace Hopper ${RUN}` }).first().click();
  await shot(page, "04-person");

  // Offboard: the account is disabled and loses its managed groups.
  await page.getByRole("button", { name: "Start offboarding" }).click();
  await waitForWorkflow(page, "Completed");
  [user] = await keycloakAdmin(`/users?username=${LOGIN}&exact=true`);
  assert.equal(user.enabled, false);
  groups = await keycloakAdmin(`/users/${user.id}/groups`);
  assert.deepEqual(groups, []);
  console.log("Offboarding completed: Keycloak user disabled, managed groups removed");

  // The account comparison finds the account and reports no unexpected access.
  await page.goto(keycloakUrl);
  await page.getByRole("button", { name: "Compare accounts now" }).click();
  await page.waitForSelector(".messages li.success");
  await shot(page, "05-connection-after-compare");

  await page.goto(`${BASE_URL}/audit/`);
  await shot(page, "06-audit");
  await page.goto(`${BASE_URL}/`);
  await shot(page, "07-dashboard");

  await page.getByRole("button", { name: "Sign out" }).click();
  await page.waitForURL(/\/login\//);
  console.log("Signed out through Keycloak");
  await browser.close();
  console.log(`End-to-end check passed (${personUrl ? "ok" : ""}); screenshots in ${OUTPUT_DIR}`);
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
