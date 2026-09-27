import * as Alert from "@radix-ui/react-alert-dialog";
import { useEffect, useRef } from "react";
import { Button } from "./Button";
interface Props {
  open: boolean;
  title: string;
  description: string;
  confirmLabel: string;
  cancelLabel?: string;
  busy?: boolean;
  error?: string | null;
  onCancel: () => void;
  onConfirm: () => void;
  onReturnFocus?: () => void;
}
export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel,
  cancelLabel = "取消",
  busy,
  error,
  onCancel,
  onConfirm,
  onReturnFocus,
}: Props) {
  const content = useRef<HTMLDivElement>(null);

  /**
   * 确认框自己处理 Escape，而不是只依赖 Radix 的默认行为。
   *
   * Radix 只在焦点位于最上层图层时才处理 Escape；确认框可能是在外层浮层的
   * Escape 处理里同步挂载的，焦点尚未落到它里面时按下的 Escape 会被忽略，
   * 表现为"再按一次 Esc 关不掉"。这里在最上层确认框上直接接管，取消语义一致。
   */
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      const dialogs = document.querySelectorAll('[role="alertdialog"]');
      if (dialogs[dialogs.length - 1] !== content.current) return;
      event.preventDefault();
      if (!busy) onCancel();
    };
    document.addEventListener("keydown", onKeyDown, true);
    return () => document.removeEventListener("keydown", onKeyDown, true);
  }, [open, busy, onCancel]);

  return (
    <Alert.Root
      open={open}
      onOpenChange={(value) => {
        if (!value && !busy) onCancel();
      }}
    >
      <Alert.Portal>
        <Alert.Overlay className="dialog-overlay fixed inset-0 z-[60] bg-slate-950/35" />
        <Alert.Content
          ref={content}
          className="dialog-content fixed left-1/2 top-1/2 z-[70] max-h-[calc(100dvh-32px)] w-[calc(100%-32px)] max-w-md -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-xl border border-border bg-surface p-6 shadow-xl"
          onEscapeKeyDown={(e) => {
            if (busy) e.preventDefault();
          }}
          onCloseAutoFocus={(e) => {
            if (onReturnFocus) {
              e.preventDefault();
              onReturnFocus();
            }
          }}
        >
          <Alert.Title className="text-base font-semibold">{title}</Alert.Title>
          <Alert.Description className="mt-2 text-sm text-foreground/70">
            {description}
          </Alert.Description>
          {error && (
            <p role="alert" className="mt-2 text-sm text-danger">
              {error}
            </p>
          )}
          <div className="mt-6 flex justify-end gap-2">
            <Alert.Cancel asChild>
              <Button disabled={busy} variant="outline">
                {cancelLabel}
              </Button>
            </Alert.Cancel>
            <Button
              disabled={busy}
              variant="destructive"
              aria-busy={busy}
              onClick={onConfirm}
            >
              {confirmLabel}
            </Button>
          </div>
        </Alert.Content>
      </Alert.Portal>
    </Alert.Root>
  );
}
