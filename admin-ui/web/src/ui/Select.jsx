import * as SelectPrimitive from "@radix-ui/react-select";
import { Check, ChevronDown } from "lucide-react";
import { cn } from "./cn";

// Select — Radix primitive + shadcn-style wrapping. Use like:
//   <Select value={x} onValueChange={setX}>
//     <SelectTrigger><SelectValue placeholder="Pick…" /></SelectTrigger>
//     <SelectContent>
//       <SelectItem value="a">A</SelectItem>
//     </SelectContent>
//   </Select>

export const Select = SelectPrimitive.Root;
export const SelectValue = SelectPrimitive.Value;
export const SelectGroup = SelectPrimitive.Group;

export function SelectLabel({ className, ...props }) {
  return (
    <SelectPrimitive.Label
      className={cn("px-2 py-1.5 text-[11px] font-bold uppercase tracking-wider text-[var(--ink-4)]", className)}
      {...props}
    />
  );
}

export function SelectSeparator({ className, ...props }) {
  return (
    <SelectPrimitive.Separator
      className={cn("my-1 h-px bg-[var(--border)]", className)}
      {...props}
    />
  );
}

export function SelectTrigger({ className, children, ...props }) {
  return (
    <SelectPrimitive.Trigger
      className={cn(
        "flex h-9 w-full items-center justify-between rounded-[6px] border border-[var(--border-2)] bg-white px-3 py-1 text-sm",
        "focus:outline-none focus:ring-2 focus:ring-[var(--accent)]/40 focus:border-[var(--accent)]",
        "disabled:cursor-not-allowed disabled:opacity-50",
        "[&>span]:line-clamp-1",
        className
      )}
      {...props}
    >
      {children}
      <SelectPrimitive.Icon asChild>
        <ChevronDown className="h-4 w-4 opacity-60" />
      </SelectPrimitive.Icon>
    </SelectPrimitive.Trigger>
  );
}

export function SelectContent({ className, children, ...props }) {
  return (
    <SelectPrimitive.Portal>
      <SelectPrimitive.Content
        className={cn(
          "relative z-50 min-w-[8rem] rounded-md border border-[var(--border)] bg-white text-sm shadow-md",
          className
        )}
        position="popper"
        sideOffset={4}
        {...props}
      >
        {/* Viewport scrolls when content exceeds max-h — fixes the cut-off
            "17 analytics queries" dropdown (#59). */}
        <SelectPrimitive.Viewport className="p-1 max-h-[60vh] overflow-y-auto">
          {children}
        </SelectPrimitive.Viewport>
      </SelectPrimitive.Content>
    </SelectPrimitive.Portal>
  );
}

export function SelectItem({ className, children, ...props }) {
  return (
    <SelectPrimitive.Item
      className={cn(
        "relative flex w-full cursor-default select-none items-center rounded-sm py-1.5 pl-8 pr-2 outline-none",
        "focus:bg-[var(--surface)] focus:text-[var(--ink)]",
        "data-[disabled]:pointer-events-none data-[disabled]:opacity-50",
        className
      )}
      {...props}
    >
      <span className="absolute left-2 flex h-3.5 w-3.5 items-center justify-center">
        <SelectPrimitive.ItemIndicator>
          <Check className="h-3.5 w-3.5" />
        </SelectPrimitive.ItemIndicator>
      </span>
      <SelectPrimitive.ItemText>{children}</SelectPrimitive.ItemText>
    </SelectPrimitive.Item>
  );
}
