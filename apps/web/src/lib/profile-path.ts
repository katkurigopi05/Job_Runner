/** A server-action argument must identify one profile, never another API route. */
export function profilePath(profileId: string): string | null {
  if (typeof profileId !== "string" || profileId.length !== 36 ||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(profileId)) {
    return null;
  }
  return `/profiles/${encodeURIComponent(profileId)}`;
}
