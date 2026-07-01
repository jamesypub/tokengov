import { Slot } from "@radix-ui/react-slot";
import { cva } from "class-variance-authority";
import { cn } from "./cn";

// Button — PD-11 design standard.
// Three sizes: sm (28px) · md (36px) · lg (44px). Icon variant = 28px square.
// Four variants: primary (filled brand), secondary (outlined), ghost (text only),
// destructive (red secondary).
// Rule: at most ONE primary button per page/modal context.

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-[6px] " +
  "font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 " +
  "focus-visible:ring-[var(--accent)]/40 disabled:pointer-events-none disabled:opacity-50 " +
  "[&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        primary:     "bg-[var(--accent)] text-white hover:brightness-110 shadow-sm",
        secondary:   "border border-[var(--border)] bg-white text-[var(--ink)] hover:bg-[var(--surface)]",
        ghost:       "text-[var(--ink)] hover:bg-[var(--surface)]",
        destructive: "border border-[var(--red)] bg-white text-[var(--red)] hover:bg-[color-mix(in_srgb,var(--red)_8%,white)]",
        link:        "text-[var(--accent)] hover:underline underline-offset-4 p-0 h-auto",
      },
      size: {
        sm:   "h-7 px-3 text-[13px]",
        md:   "h-9 px-4 text-sm",
        lg:   "h-11 px-6 text-base",
        icon: "h-7 w-7 p-0",
      },
    },
    defaultVariants: {
      variant: "secondary",
      size:    "md",
    },
  }
);

export function Button({
  className,
  variant,
  size,
  asChild = false,
  ...props
}) {
  const Comp = asChild ? Slot : "button";
  return (
    <Comp className={cn(buttonVariants({ variant, size, className }))} {...props} />
  );
}

export { buttonVariants };
