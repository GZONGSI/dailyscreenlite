import { forwardRef, type InputHTMLAttributes } from "react";
import { cn } from "../../lib/utils";
export const Input = forwardRef<
  HTMLInputElement,
  InputHTMLAttributes<HTMLInputElement>
>(function Input({ className, type, ...props }, ref) {
  return (
    <input
      ref={ref}
      type={type}
      className={cn(
        type === "checkbox" || type === "radio"
          ? "h-4 w-4 accent-primary"
          : "ui-input",
        className,
      )}
      {...props}
    />
  );
});
