import { ErrorNote } from "./components/bits";
import { Explorer } from "./pages/Explorer";
import { History } from "./pages/History";
import { Overview } from "./pages/Overview";
import { Scan } from "./pages/Scan";
import { Settings } from "./pages/Settings";
import { navigate, useRoute, type Route } from "./router";
import { useApp } from "./state";

const NAV: { route: Route; label: string }[] = [
  { route: "overview", label: "Overview" },
  { route: "scan", label: "Scan" },
  { route: "explorer", label: "Artifacts" },
  { route: "history", label: "History" },
  { route: "settings", label: "Settings" },
];

function Page({ route }: { route: Route }) {
  switch (route) {
    case "scan":
      return <Scan />;
    case "explorer":
      return <Explorer />;
    case "history":
      return <History />;
    case "settings":
      return <Settings />;
    default:
      return <Overview />;
  }
}

export function App() {
  const route = useRoute();
  const { loading, error } = useApp();

  return (
    <div className="shell">
      <nav className="nav" aria-label="Main">
        <div className="brand">
          <strong>Sift</strong>
          <span>Find the clutter. Keep the code.</span>
        </div>
        <ul>
          {NAV.map((item) => (
            <li key={item.route}>
              <a
                href={item.route === "overview" ? "#/" : `#/${item.route}`}
                aria-current={route === item.route ? "page" : undefined}
                onClick={(event) => {
                  event.preventDefault();
                  navigate(item.route);
                }}
              >
                {item.label}
              </a>
            </li>
          ))}
        </ul>
        <p className="nav-foot">Local only. Nothing leaves this machine.</p>
      </nav>
      <main>
        {loading ? (
          <p className="muted">Loading…</p>
        ) : error !== null ? (
          <ErrorNote message={error} />
        ) : (
          <Page route={route} />
        )}
      </main>
    </div>
  );
}
