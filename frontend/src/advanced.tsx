import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";

export const ADVANCED_KEY = "archpilot.showAdvanced";

interface AdvancedValue {
  advanced: boolean;
  setAdvanced: (on: boolean) => void;
}

// Without a provider (a component rendered on its own) everything is shown, as before.
const AdvancedContext = createContext<AdvancedValue>({
  advanced: true,
  setAdvanced: () => undefined,
});

function readStored(): boolean {
  try {
    return window.localStorage.getItem(ADVANCED_KEY) === "1";
  } catch {
    return false;
  }
}

export function AdvancedProvider({ children }: { children: ReactNode }) {
  const [advanced, setState] = useState<boolean>(readStored);
  const setAdvanced = useCallback((on: boolean) => {
    setState(on);
    try {
      window.localStorage.setItem(ADVANCED_KEY, on ? "1" : "0");
    } catch {
      // storage unavailable: the choice lasts for this session only
    }
  }, []);
  const value = useMemo(() => ({ advanced, setAdvanced }), [advanced, setAdvanced]);
  return <AdvancedContext.Provider value={value}>{children}</AdvancedContext.Provider>;
}

export function useAdvancedMode(): AdvancedValue {
  return useContext(AdvancedContext);
}

export function useAdvanced(): boolean {
  return useContext(AdvancedContext).advanced;
}

/**
 * Advanced content is rendered in full when advanced mode is on. Otherwise only a one-line
 * summary with a "details" link is shown; the link reveals the same content for this view.
 */
export function AdvancedDetails({
  summary,
  children,
  forceOpen = false,
}: {
  summary: ReactNode;
  children: ReactNode;
  forceOpen?: boolean;
}) {
  const advanced = useAdvanced();
  const [open, setOpen] = useState(false);
  if (advanced) return <>{children}</>;
  const shown = open || forceOpen;
  return (
    <div>
      <p className="sub">
        {summary}{" "}
        <button
          type="button"
          className="link-button"
          aria-expanded={shown}
          onClick={() => setOpen(!shown)}
        >
          {shown ? "hide details" : "details"}
        </button>
      </p>
      {shown && children}
    </div>
  );
}
