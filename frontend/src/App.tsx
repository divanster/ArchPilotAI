import { useEffect, useState } from "react";
import { fetchHealth } from "./api";

type State = "checking" | "ok" | "unavailable";

export default function App() {
  const [state, setState] = useState<State>("checking");

  useEffect(() => {
    let active = true;
    fetchHealth()
      .then(() => active && setState("ok"))
      .catch(() => active && setState("unavailable"));
    return () => {
      active = false;
    };
  }, []);

  return (
    <main>
      <h1>ArchPilotAI</h1>
      <p role="status">Backend: {state}</p>
    </main>
  );
}
