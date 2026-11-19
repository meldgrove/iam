// The update button's styles need CSS properties registered in the document.
// @meldgrove/ui 0.1.0 registers them with an inline <style>, which the Content
// Security Policy blocks: the button and the settings then lose their borders,
// shadows and focus rings. This adds the same rules as a constructed style
// sheet, which the policy allows. Runs after vendor/meldgrove-updates.js (module
// scripts run in order); does nothing once the package stops using the <style>.
const blocked = document.getElementById("meldgrove-properties");
if (blocked && !blocked.sheet?.cssRules.length) {
  const sheet = new CSSStyleSheet();
  sheet.replaceSync(blocked.textContent);
  document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
}
