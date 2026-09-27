import * as Dialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import {
  forwardRef,
  type ComponentPropsWithoutRef,
  type ElementRef,
} from "react";
import { cn } from "../../lib/utils";
import { buttonVariants } from "./Button";

export const Sheet = Dialog.Root;
export const SheetTrigger = Dialog.Trigger;
export const SheetTitle = Dialog.Title;
export const SheetDescription = Dialog.Description;
export const SheetContent = forwardRef<
  ElementRef<typeof Dialog.Content>,
  ComponentPropsWithoutRef<typeof Dialog.Content>
>(function SheetContent({ children, className, ...props }, ref) {
  return (
    <Dialog.Portal>
      <Dialog.Overlay className="sheet-overlay fixed inset-0 z-40 bg-slate-950/35" />
      <Dialog.Content
        ref={ref}
        className={cn(
          "sheet-content fixed inset-y-0 right-0 z-50 flex w-full max-w-2xl flex-col border-l border-border bg-surface shadow-xl outline-none",
          className,
        )}
        {...props}
      >
        {children}
        <Dialog.Close
          aria-label="关闭面板"
          className={cn(
            buttonVariants({ variant: "ghost", size: "icon" }),
            "absolute right-4 top-4",
          )}
        >
          <X className="h-4 w-4" aria-hidden="true" />
        </Dialog.Close>
      </Dialog.Content>
    </Dialog.Portal>
  );
});
