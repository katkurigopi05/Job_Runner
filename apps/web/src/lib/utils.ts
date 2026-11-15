import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** Merge Tailwind utility classes cleanly, matching shadcn/ui conventions. */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
