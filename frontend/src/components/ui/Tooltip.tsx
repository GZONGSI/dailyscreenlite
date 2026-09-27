import * as Primitive from "@radix-ui/react-tooltip";
import type { ReactElement } from "react";
export function Tooltip({
  label,
  children,
}: {
  label: string;
  children: ReactElement;
}) {
  return (
    <Primitive.Provider delayDuration={300}>
      <Primitive.Root>
        <Primitive.Trigger asChild>{children}</Primitive.Trigger>
        <Primitive.Portal>
          <Primitive.Content
            sideOffset={6}
            className="popover-content z-[70] max-w-xs rounded-lg border border-border bg-surface px-3 py-2 text-xs text-foreground shadow-md"
          >
            {label}
          </Primitive.Content>
        </Primitive.Portal>
      </Primitive.Root>
    </Primitive.Provider>
  );
}
