import * as Primitive from "@radix-ui/react-popover";
import {
  forwardRef,
  type ComponentPropsWithoutRef,
  type ElementRef,
} from "react";
import { cn } from "../../lib/utils";
export const Popover = Primitive.Root;
export const PopoverTrigger = Primitive.Trigger;
export const PopoverContent = forwardRef<
  ElementRef<typeof Primitive.Content>,
  ComponentPropsWithoutRef<typeof Primitive.Content>
>(function PopoverContent(
  { className, side = "top", align = "start", ...props },
  ref,
) {
  return (
    <Primitive.Portal>
      <Primitive.Content
        ref={ref}
        side={side}
        align={align}
        sideOffset={8}
        collisionPadding={12}
        className={cn(
          "popover-content z-50 w-80 max-w-[calc(100vw-24px)] overflow-y-auto rounded-xl border border-border bg-surface p-4 text-sm shadow-lg outline-none",
          className,
        )}
        style={{ maxHeight: "var(--radix-popover-content-available-height)" }}
        {...props}
      />
    </Primitive.Portal>
  );
});
