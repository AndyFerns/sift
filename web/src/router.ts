import { useEffect, useState } from "react";

export type Route = "overview" | "scan" | "explorer" | "history" | "settings";

const ROUTES: readonly Route[] = ["overview", "scan", "explorer", "history", "settings"];

function parse(hash: string): Route {
  const name = hash.replace(/^#\/?/, "");
  return (ROUTES as readonly string[]).includes(name) ? (name as Route) : "overview";
}

export function navigate(route: Route): void {
  window.location.hash = route === "overview" ? "/" : `/${route}`;
}

/** Hash-based routing: five pages do not need a routing library. */
export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parse(window.location.hash));
  useEffect(() => {
    const onChange = () => setRoute(parse(window.location.hash));
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
}
