import type { Account, LibraryType, PublishingType, ProjectManagerRow, Project } from "@/types";

export function usesIdentitySource(project: Pick<Project, "library_type" | "temporary_source"> | null | undefined): boolean {
  return project?.library_type === "pure" && !project.temporary_source;
}

export const LIBRARY_TYPE_OPTIONS: Array<{
  value: LibraryType;
  label: string;
}> = [
  { value: "anime", label: "Anime" },
  { value: "simpsons", label: "Simpsons" },
  { value: "films_series", label: "Films/Séries" },
  { value: "dessin_anime", label: "Dessin Animé" },
  { value: "pure", label: "Pure" },
];

const LIBRARY_TYPE_LABELS: Record<PublishingType, string> = {
  facecam: "Facecam",
  anime: "Anime",
  simpsons: "Simpsons",
  films_series: "Films/Séries",
  dessin_anime: "Dessin Animé",
  pure: "Pure",
};

export function getLibraryTypeLabel(
  libraryType: LibraryType | string | null | undefined,
): string {
  if (!libraryType) return "Unknown";
  return LIBRARY_TYPE_LABELS[libraryType as PublishingType] ?? libraryType;
}

export function getSupportedTypeLabels(
  supportedTypes: PublishingType[] | null | undefined,
): string {
  if (!supportedTypes || supportedTypes.length === 0) {
    return getLibraryTypeLabel("anime");
  }
  return supportedTypes.map((item) => getLibraryTypeLabel(item)).join(", ");
}

function accountSupportsLibraryType(
  account: Account,
  libraryType: PublishingType,
): boolean {
  return account.supported_types.includes(libraryType);
}

export function isAccountCompatibleWithProjectRow(
  account: Account,
  row: Pick<ProjectManagerRow, "language" | "library_type" | "publishing_type">,
): boolean {
  return Boolean(row.language)
    && account.language === row.language
    && accountSupportsLibraryType(account, row.publishing_type ?? row.library_type);
}
