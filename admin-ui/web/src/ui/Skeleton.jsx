import { useEffect, useState } from "react";
import { cn } from "./cn";

// Only render a skeleton after ~120ms — faster loads show nothing, which
// avoids the "element appeared then changed" flash (see feedback_ui_taste.md).
export function useDelayedVisible(delay = 120) {
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setVisible(true), delay);
    return () => clearTimeout(t);
  }, [delay]);
  return visible;
}

export function SkeletonBlock({ className }) {
  return (
    <div
      className={cn(
        "animate-pulse rounded-md bg-[var(--border)]/70",
        className
      )}
    />
  );
}

export default function Skeleton({ children, delay = 120 }) {
  const visible = useDelayedVisible(delay);
  if (!visible) return null;
  return <div className="space-y-3">{children}</div>;
}
