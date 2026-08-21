import { ApiError, api, type ApplicationPacket } from "@/lib/api";
import { ErrorPanel } from "@/components/error-panel";
import { FinishQueue } from "@/components/finish-queue";

export const dynamic = "force-dynamic";

/**
 * Finish applications by hand, quickly.
 *
 * §2.5 makes this permanent rather than temporary: every supported ATS puts a
 * captcha on the apply form, this project will not work around one, and so the
 * final click belongs to the owner. The honest response is not to pretend
 * otherwise but to make that click cost as little as possible — which is the
 * difference between the tool being usable at a hundred applications a day
 * and being usable at five.
 */
export default async function FinishPage() {
  let packets: ApplicationPacket[];
  try {
    packets = await api.manualQueue(50);
  } catch (error) {
    if (error instanceof ApiError) return <ErrorPanel error={error} />;
    throw error;
  }

  return (
    <div className="mx-auto max-w-3xl space-y-8">
      <header className="space-y-3">
        <h1 className="font-display text-[length:var(--text-display)] leading-none">Finish</h1>
        <p className="max-w-2xl text-sm text-ink-soft">
          Everything waiting on you, one at a time. The form is filled and screenshotted; you
          solve the captcha and submit. Keys are listed under each card.
        </p>
      </header>

      <FinishQueue initial={packets} />
    </div>
  );
}
