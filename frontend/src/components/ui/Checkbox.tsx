import * as Primitive from "@radix-ui/react-checkbox";
import { Check } from "lucide-react";
import {
  forwardRef,
  type ComponentPropsWithoutRef,
  type ElementRef,
} from "react";
import { cn } from "../../lib/utils";
export const Checkbox = forwardRef<
  ElementRef<typeof Primitive.Root>,
  ComponentPropsWithoutRef<typeof Primitive.Root>
>(function Checkbox({ className, ...props }, ref) {
  return (
    <Primitive.Root
      ref={ref}
      className={cn(
        "h-5 w-5 shrink-0 rounded border border-border data-[state=checked]:border-primary data-[state=checked]:bg-primary data-[state=checked]:text-primary-foreground disabled:opacity-50",
        className,
      )}
      {...props}
    >
      <Primitive.Indicator className="flex items-center justify-center">
        <Check className="h-4 w-4" />
      </Primitive.Indicator>
    </Primitive.Root>
  );
});
