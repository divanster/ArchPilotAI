import { useEffect, useState } from "react";

export type Page =
  | "dashboard"
  | "projects"
  | "documents"
  | "requirements"
  | "architecture"
  | "plan"
  | "repository"
  | "playground"
  | "providers";

export interface Route {
  page: Page;
  /** Page-specific argument: a project id, or "new" on the projects page. */
  arg?: string;
  /** Optional second argument, e.g. a document id to open on the documents page. */
  sub?: string;
}

const PAGES: Page[] = [
  "dashboard",
  "projects",
  "documents",
  "requirements",
  "architecture",
  "plan",
  "repository",
  "playground",
  "providers",
];

export function parseHash(hash: string): Route {
  const [, page, arg, sub] = hash.split("/");
  if (PAGES.includes(page as Page)) {
    return {
      page: page as Page,
      arg: arg ? decodeURIComponent(arg) : undefined,
      ...(sub ? { sub: decodeURIComponent(sub) } : {}),
    };
  }
  return { page: "dashboard" };
}

export function hrefFor(page: Page, arg?: string, sub?: string): string {
  const tail = arg && sub ? `/${encodeURIComponent(sub)}` : "";
  return `#/${page}${arg ? `/${encodeURIComponent(arg)}` : ""}${tail}`;
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
