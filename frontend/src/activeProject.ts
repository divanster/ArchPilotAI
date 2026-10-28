import type { Page } from "./routes";

const ACTIVE_KEY = "archpilot.activeProject";
const SECTION_KEY = "archpilot.lastSection.";

export type ProjectPage = Extract<Page, "documents" | "requirements" | "architecture">;

export const PROJECT_PAGES: ProjectPage[] = ["documents", "requirements", "architecture"];

function read(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Storage can be unavailable (private mode); the URL still carries the project.
  }
}

export const getActiveProject = (): string | null => read(ACTIVE_KEY);

export function setActiveProject(id: string, section?: ProjectPage) {
  write(ACTIVE_KEY, id);
  if (section) write(SECTION_KEY + id, section);
}

export function getLastSection(id: string): ProjectPage {
  const s = read(SECTION_KEY + id);
  return PROJECT_PAGES.find((p) => p === s) ?? "architecture";
}
