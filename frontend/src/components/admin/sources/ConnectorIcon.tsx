import clsx from "clsx";
import type { CatalogEntry } from "./catalog";

/** Brand mark when `logo` is set; otherwise Lucide fallback. */
export function ConnectorIcon({
  entry,
  size = "sm",
  className,
}: {
  entry: CatalogEntry;
  size?: "sm" | "lg";
  className?: string;
}) {
  const box = size === "lg" ? "h-11 w-11 rounded-xl" : "h-7 w-7 rounded-lg";
  const img = size === "lg" ? "h-7 w-7" : "h-4 w-4";
  const lucide = size === "lg" ? "h-6 w-6" : "h-4 w-4";

  if (entry.logo) {
    return (
      <span className={clsx("flex shrink-0 items-center justify-center border border-border bg-surface", box, className)} aria-hidden>
        <img src={entry.logo} alt="" className={clsx(img, "object-contain")} />
      </span>
    );
  }

  const Icon = entry.icon;
  return (
    <span className={clsx("flex shrink-0 items-center justify-center", box, entry.tint, className)} aria-hidden>
      <Icon className={lucide} strokeWidth={size === "lg" ? 1.6 : 1.8} />
    </span>
  );
}
