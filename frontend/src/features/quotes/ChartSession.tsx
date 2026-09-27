import {
  createContext,
  useContext,
  useRef,
  useState,
  type ReactNode,
  type MutableRefObject,
} from "react";
import type { QuoteSeries } from "../../api/client";
import type { Period, ChartInterval } from "./series";
export interface ChartView {
  first: string;
  from: number;
  to: number;
  interval: ChartInterval;
  defaultMax: boolean;
}
interface Cache {
  securityId: string;
  series: QuoteSeries | null;
  view: ChartView | null;
  refreshToken: number;
  historyError: string | null;
  exhausted: boolean;
  historyFlight: Promise<void> | null;
  generation: number;
}
interface Session {
  interval: ChartInterval;
  setInterval: (interval: ChartInterval) => void;
  periods: Period[];
  toggle: (period: Period) => void;
  cache: MutableRefObject<Cache | null>;
}
const Context = createContext<Session | null>(null);
export function ChartSessionProvider({ children }: { children: ReactNode }) {
  const [interval, setInterval] = useState<ChartInterval>("day");
  const [periods, setPeriods] = useState<Period[]>([]);
  const cache = useRef<Cache | null>(null);
  return (
    <Context.Provider
      value={{
        interval,
        setInterval,
        periods,
        toggle: (period) =>
          setPeriods((values) =>
            values.includes(period)
              ? values.filter((v) => v !== period)
              : [...values, period],
          ),
        cache,
      }}
    >
      {children}
    </Context.Provider>
  );
}
export function useChartSession() {
  const value = useContext(Context);
  if (!value) throw new Error("ChartSessionProvider missing");
  return value;
}
