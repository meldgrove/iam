/** Shortens a text to at most `max` characters, ending in "…" (Django's `truncatechars`). */
export function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

/** Shortens a text to at most `max` words, ending in "…" (Django's `truncatewords`). */
export function truncateWords(text: string, max: number): string {
  const words = text.trim().split(/\s+/);
  return words.length > max ? `${words.slice(0, max).join(" ")} …` : text;
}
