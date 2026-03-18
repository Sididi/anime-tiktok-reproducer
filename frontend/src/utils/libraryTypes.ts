import type { Account, LibraryType, ProjectManagerRow } from "@/types";

export const LIBRARY_TYPE_OPTIONS: Array<{
  value: LibraryType;
  label: string;
}> = [
  { value: "anime", label: "Anime" },
  { value: "simpsons", label: "Simpsons" },
  { value: "films_series", label: "Films/Séries" },
  { value: "dessin_anime", label: "Dessin Animé" },
];

export const LIBRARY_TYPE_LABELS: Record<LibraryType, string> = {
  anime: "Anime",
  simpsons: "Simpsons",
  films_series: "Films/Séries",
  dessin_anime: "Dessin Animé",
};

export function getLibraryTypeLabel(
  libraryType: LibraryType | string | null | undefined,
): string {
  if (!libraryType) return "Unknown";
  return LIBRARY_TYPE_LABELS[libraryType as LibraryType] ?? libraryType;
}

export function getSupportedTypeLabels(
  supportedTypes: LibraryType[] | null | undefined,
): string {
  if (!supportedTypes || supportedTypes.length === 0) {
    return getLibraryTypeLabel("anime");
  }
  return supportedTypes.map((item) => getLibraryTypeLabel(item)).join(", ");
}

export function accountSupportsLibraryType(
  account: Account,
  libraryType: LibraryType,
): boolean {
  return account.supported_types.includes(libraryType);
}

export function isAccountCompatibleWithProjectRow(
  account: Account,
  row: Pick<ProjectManagerRow, "language" | "library_type">,
): boolean {
  return Boolean(row.language)
    && account.language === row.language
    && accountSupportsLibraryType(account, row.library_type);
}
