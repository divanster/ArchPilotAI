import { useEffect, useState } from "react";

export type Page =
  "dashboard" | "projects" | "documents" | "requirements" | "architecture" | "playground";

export interface Route {
  page: Page;
  /** Page-specific argument: a project id, or "new" on the projects page. */
  arg?: string;
}

const PAGES: Page[] = [
  "dashboard",
  "projects",
  "documents",
  "requirements",
  "architecture",
  "playground",
];

export function parseHash(hash: string): Route {
  const [, page, arg] = hash.split("/");
  if (PAGES.includes(page as Page)) {
    return {
      page: page as Page,
      arg: arg ? decodeURIComponent(arg) : undefined,
    };
  }
  return { page: "dashboard" };
}

export function hrefFor(page: Page, arg?: string): string {
  return `#/${page}${arg ? `/${encodeURIComponent(arg)}` : ""}`;
}

export function navigate(page: Page, arg?: string) {
  window.location.hash = hrefFor(page, arg);
}

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() =>
    parseHash(window.location.hash),
  );
  useEffect(() => {
    const onChange = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
}
