import { QueryClientProvider } from "@tanstack/react-query";

import { ChartSessionProvider } from "./features/quotes/ChartSession";
import { AppShell } from "./features/shell/AppShell";
import { queryClient } from "./lib/queryClient";

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <ChartSessionProvider>
        <AppShell />
      </ChartSessionProvider>
    </QueryClientProvider>
  );
}
