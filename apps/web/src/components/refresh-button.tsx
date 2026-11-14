"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Check, RefreshCw } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState, useTransition } from "react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface Props {
  label?: string;
  queryKeys?: readonly (readonly unknown[])[];
  className?: string;
  size?: "sm" | "default";
}

/**
 * Refresh the current server route and invalidate matching client queries.
 *
 * Gives every Server Component dashboard view an explicit reload affordance
 * without forcing a full browser document navigation.
 */
export function RefreshButton({
  label = "Refresh",
  queryKeys,
  className,
  size = "sm",
}: Props) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [pending, startTransition] = useTransition();
  const [done, setDone] = useState(false);

  useEffect(() => {
    if (!done) return;
    const timer = setTimeout(() => setDone(false), 1800);
    return () => clearTimeout(timer);
  }, [done]);

  const handleRefresh = () => {
    setDone(false);
    startTransition(() => {
      if (queryKeys && queryKeys.length > 0) {
        for (const queryKey of queryKeys) {
          void queryClient.invalidateQueries({ queryKey: [...queryKey] });
        }
      } else {
        void queryClient.invalidateQueries();
      }
      router.refresh();
      setDone(true);
    });
  };

  return (
    <Button
      variant="outline"
      size={size}
      onClick={handleRefresh}
      disabled={pending}
      aria-label={label}
      className={cn(done && "border-go/45 text-go", className)}
    >
      {done && !pending ? (
        <Check className="text-go" aria-hidden />
      ) : (
        <RefreshCw className={cn(pending && "motion-safe:animate-spin")} aria-hidden />
      )}
      <span>{pending ? "Refreshing…" : done ? "Refreshed" : label}</span>
    </Button>
  );
}
