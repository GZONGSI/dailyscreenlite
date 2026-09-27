import * as Primitive from "@radix-ui/react-dialog";
import {
  forwardRef,
  type ComponentPropsWithoutRef,
  type ElementRef,
} from "react";
import { cn } from "../../lib/utils";
export const Dialog = Primitive.Root;
export const DialogTitle = Primitive.Title;
export const DialogDescription = Primitive.Description;
export const DialogContent = forwardRef<
  ElementRef<typeof Primitive.Content>,
  ComponentPropsWithoutRef<typeof Primitive.Content>
>(function DialogContent({ className, ...props }, ref) {
  return (
    <Primitive.Portal>
      <Primitive.Overlay className="dialog-overlay fixed inset-0 z-50 bg-slate-950/35" />
      <Primitive.Content
        ref={ref}
        className={cn(
          "dialog-content fixed left-1/2 top-1/2 z-50 max-h-[calc(100dvh-32px)] w-[calc(100%-32px)] max-w-[560px] -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-xl border border-border bg-surface p-4 shadow-xl outline-none sm:p-6",
          className,
        )}
        {...props}
      />
    </Primitive.Portal>
  );
});
