import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs transition",
  {
    variants: {
      variant: {
        default: "border-rule bg-paper-high text-ink-soft",
        go: "border-go/40 bg-go-soft text-go",
        attn: "border-attn/40 bg-attn-soft text-attn",
        wait: "border-wait/40 bg-wait-soft text-wait",
        stop: "border-stop/40 bg-stop-soft text-stop",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export { badgeVariants };
