import { ApiError, api, type Profile } from "@/lib/api";
import { ErrorPanel } from "@/components/error-panel";
import { ProfileForm } from "./profile-form";

export const dynamic = "force-dynamic";

export default async function ProfilePage() {
  let profiles: Profile[];
  try {
    profiles = await api.profiles();
  } catch (error) {
    if (error instanceof ApiError) return <ErrorPanel error={error} />;
    throw error;
  }

  return (
    <div className="space-y-10">
      <header>
        <h1 className="font-display text-display leading-[1.05] tracking-tight">Profile</h1>
        <p className="mt-3 max-w-prose text-ink-soft">
          What the agent fills forms with. Everything here is copied onto real applications, so it is
          worth being exact.
        </p>
      </header>

      {profiles.length === 0 ? (
        <p className="border border-dashed border-rule px-6 py-16 text-center font-mono text-sm text-ink-faint">
          no profiles yet
        </p>
      ) : (
        <div className="space-y-14">
          {profiles.map((profile) => (
            <section key={profile.id} aria-labelledby={`p-${profile.id}`}>
              <h2 id={`p-${profile.id}`} className="sr-only">
                {profile.label}
              </h2>
              <ProfileForm profile={profile} />
            </section>
          ))}
        </div>
      )}
    </div>
  );
}
