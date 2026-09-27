import { Tooltip } from "../../components/ui/Tooltip";
import { useEffect, useState } from "react";
import * as Menu from "@radix-ui/react-dropdown-menu";
import { Check, Sun } from "lucide-react";
import { Button } from "../../components/ui/Button";
type Theme = "light" | "dark" | "system";
const choices: { value: Theme; label: string }[] = [
  { value: "light", label: "浅色" },
  { value: "dark", label: "深色" },
  { value: "system", label: "跟随系统" },
];
function savedTheme(): Theme {
  try {
    const value = localStorage.getItem("dslite-theme");
    return value === "dark" || value === "system" ? value : "light";
  } catch {
    return "light";
  }
}
export function ThemeMenu() {
  const [theme, setTheme] = useState<Theme>(savedTheme);
  useEffect(() => {
    const media = matchMedia("(prefers-color-scheme: dark)");
    const apply = () => {
      document.documentElement.classList.toggle(
        "dark",
        theme === "dark" || (theme === "system" && media.matches),
      );
    };
    apply();
    try {
      localStorage.setItem("dslite-theme", theme);
    } catch {
      /* Session-only when storage is unavailable. */
    }
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, [theme]);
  return (
    <Menu.Root>
      <Tooltip label="切换外观">
        <Menu.Trigger asChild>
          <Button variant="ghost" size="icon" aria-label="主题">
            <Sun className="h-4 w-4" aria-hidden="true" />
          </Button>
        </Menu.Trigger>
      </Tooltip>
      <Menu.Portal>
        <Menu.Content
          align="end"
          sideOffset={6}
          className="popover-content z-[70] min-w-36 rounded-lg border border-border bg-surface p-1 shadow-md"
        >
          <Menu.RadioGroup
            value={theme}
            onValueChange={(value) => {
              const choice = choices.find((c) => c.value === value);
              if (choice) setTheme(choice.value);
            }}
          >
            {choices.map((choice) => (
              <Menu.RadioItem
                key={choice.value}
                value={choice.value}
                className="relative flex cursor-pointer items-center rounded-md py-2 pl-8 pr-3 text-sm outline-none data-[highlighted]:bg-selected"
              >
                <Menu.ItemIndicator className="absolute left-2">
                  <Check className="h-4 w-4" />
                </Menu.ItemIndicator>
                {choice.label}
              </Menu.RadioItem>
            ))}
          </Menu.RadioGroup>
        </Menu.Content>
      </Menu.Portal>
    </Menu.Root>
  );
}
