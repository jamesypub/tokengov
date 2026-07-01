import { cva } from "class-variance-authority";
import { cn } from "./cn";

const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-semibold",
  {
    variants: {
      variant: {
        default:     "bg-[var(--surface)] text-[var(--ink-3)]",
        brand:       "bg-[var(--accent)]/10 text-[var(--accent)]",
        success:     "bg-emerald-50 text-emerald-700",
        warning:     "bg-amber-50 text-amber-700",
        danger:      "bg-red-50 text-[var(--red)]",
        outline:     "border border-[var(--border)] text-[var(--ink-3)]",
      },
    },
    defaultVariants: { variant: "default" },
  }
);

export function Badge({ className, variant, ...props }) {
  return <span className={cn(badgeVariants({ variant, className }))} {...props} />;
}
