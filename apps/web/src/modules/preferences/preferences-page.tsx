import { Button } from "@meldgrove/ui/button";
import { Card, CardDescription, CardFooter, CardHeader, CardTitle } from "@meldgrove/ui/card";
import { Page, PageHeader } from "@meldgrove/ui/page";
import { AboutCard, AppearanceCard, KeyboardShortcutsCard } from "@meldgrove/ui/settings-cards";
import { useThemePreference } from "@meldgrove/ui/theme";
import { useMe } from "@/hooks/use-me";
import { useT } from "@/i18n";
import { REPOSITORY_URL } from "@/lib/repository";
import { messages } from "./messages";

/** The sign-in provider's account page (Keycloak's account console), where people manage their sign-in. */
function AccountCard({ url }: { url: string }) {
  const t = useT(messages);
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("account")}</CardTitle>
        <CardDescription>{t("accountDescription")}</CardDescription>
      </CardHeader>
      <CardFooter>
        <Button variant="outline" size="sm" asChild>
          <a href={url}>{t("manageAccount")}</a>
        </Button>
      </CardFooter>
    </Card>
  );
}

/** The person's own settings (Settings in the user menu): account, appearance, single-letter shortcuts (WCAG 2.1.4), about. */
export function PreferencesPage() {
  const t = useT(messages);
  const me = useMe();
  const [theme, setTheme] = useThemePreference();
  return (
    <Page className="max-w-3xl">
      <PageHeader title={t("title")} description={t("description")} />
      {me.accountUrl ? <AccountCard url={me.accountUrl} /> : null}
      <AppearanceCard value={theme} onChange={setTheme} />
      <KeyboardShortcutsCard />
      {/* The software's name, also when an installation renames the app. */}
      <AboutCard appName="Meldgrove IAM" repositoryUrl={REPOSITORY_URL} />
    </Page>
  );
}
