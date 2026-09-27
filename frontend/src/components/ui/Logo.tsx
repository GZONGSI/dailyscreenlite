interface Props {
  className?: string;
  "aria-hidden"?: boolean | "true" | "false";
}

/** 用户选定的蓝金「收束双翼」标志；云白底色与对称折面，跨主题共用 SVG。 */
export function Logo({ className, "aria-hidden": ariaHidden = true }: Props) {
  return (
    <img
      src="/logo.svg"
      alt={ariaHidden === true || ariaHidden === "true" ? "" : "DailyScreen Lite"}
      aria-hidden={ariaHidden}
      className={className}
      width={256}
      height={256}
      draggable={false}
    />
  );
}
