import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded-xl text-xs font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/45 disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:size-3.5 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default: "bg-brand text-on-accent shadow-xs hover:opacity-90",
        secondary:
          "border border-rule bg-paper-raised text-ink hover:border-ink-faint hover:bg-paper-high",
        outline:
          "border border-rule bg-transparent text-ink-soft hover:border-ink-faint hover:text-ink",
        ghost: "text-ink-soft hover:bg-paper-high hover:text-ink",
        accent:
          "border border-go/45 bg-go-soft text-go hover:border-go hover:bg-go/20",
        danger:
          "border border-stop/40 bg-stop-soft text-stop hover:border-stop",
      },
      size: {
        default: "px-3.5 py-2",
        sm: "px-2.5 py-1.5",
        lg: "px-4 py-2.5 text-sm",
        icon: "h-8 w-8 p-0",
      },
    },
    defaultVariants: {
      variant: "secondary",
      size: "default",
    },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, type = "button", ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return (
      <Comp
        ref={ref}
        type={asChild ? undefined : type}
        className={cn(buttonVariants({ variant, size, className }))}
        {...props}
      />
    );
  },
);
Button.displayName = "Button";

export { buttonVariants };
