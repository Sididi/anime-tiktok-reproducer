/**
 * TikTok-precedence warnings (HTTP 409 from the scheduling routes).
 *
 * Two backend codes:
 *  - "tiktok_precedence": the edited project itself would have a platform
 *    publishing before its TikTok.
 *  - "tiktok_precedence_displaced:<titles>": the move displaces other
 *    projects' TikTok slots after their remaining platforms.
 *
 * Returns null when the error is unrelated; otherwise the user's choice
 * (true = confirmed, retry with confirm_before_tiktok, false = declined).
 */
export function confirmTikTokPrecedence(err: unknown): boolean | null {
  const msg = err instanceof Error ? err.message : "";
  if (msg === "tiktok_precedence") {
    return window.confirm(
      "⚠️ Avec ce créneau, une plateforme publierait AVANT TikTok " +
        "(TikTok doit publier en premier).\n\nContinuer quand même ?",
    );
  }
  if (msg.startsWith("tiktok_precedence_displaced:")) {
    const titles = msg.slice("tiktok_precedence_displaced:".length);
    return window.confirm(
      `⚠️ Ce changement repousserait le TikTok de : ${titles}.\n` +
        "Leurs autres plateformes publieraient alors AVANT TikTok.\n\n" +
        "Continuer quand même ?",
    );
  }
  return null;
}
