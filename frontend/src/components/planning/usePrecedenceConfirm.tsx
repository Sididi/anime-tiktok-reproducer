import { useCallback } from "react";
import { useConfirmDialog } from "./ConfirmDialog";

/**
 * TikTok-precedence 409 handling with a proper dialog (no window.confirm).
 *
 * Backend codes:
 *  - "tiktok_precedence": the edited project itself would have a platform
 *    publishing before its TikTok.
 *  - "tiktok_precedence_displaced:<titles>": the move pushes other projects'
 *    TikTok after their remaining platforms.
 *
 * Returns null when the error is unrelated (caller rethrows), otherwise the
 * user's decision.
 */
export function usePrecedenceConfirm() {
  const { confirm, dialog } = useConfirmDialog();

  const confirmPrecedence = useCallback(
    async (err: unknown): Promise<boolean | null> => {
      const msg = err instanceof Error ? err.message : "";
      if (msg === "tiktok_precedence") {
        return confirm({
          title: "TikTok doit publier en premier",
          body: (
            <>
              Avec ce créneau, une plateforme publierait <strong>avant TikTok</strong>.
              Continuer quand même ?
            </>
          ),
          confirmLabel: "Continuer",
        });
      }
      if (msg.startsWith("tiktok_precedence_displaced:")) {
        const titles = msg.slice("tiktok_precedence_displaced:".length);
        return confirm({
          title: "TikTok déplacé après d'autres plateformes",
          body: (
            <>
              Ce changement repousserait le TikTok de : <strong>{titles}</strong>.
              Leurs autres plateformes publieraient alors avant TikTok. Continuer
              quand même ?
            </>
          ),
          confirmLabel: "Continuer",
        });
      }
      return null;
    },
    [confirm],
  );

  return { confirmPrecedence, dialog };
}
