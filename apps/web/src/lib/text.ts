/** Upper-cases the first letter of a stored lowercase name ("skills" becomes "Skills"). */
export function sentenceCase(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/**
 * A moment written the way the Desk writes its date, with the time after it:
 * "Sunday 8 November, 22:08". Pinned to the owner's zone so the server preview
 * and the browser print the same text.
 */
export function deskDateTime(value: string | number | Date): string {
  const zone = "America/Los_Angeles";
  const moment = new Date(value);
  const day = new Intl.DateTimeFormat("en-GB", {
    weekday: "long", day: "numeric", month: "long", timeZone: zone,
  }).format(moment);
  const time = new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: zone,
  }).format(moment);
  return `${day}, ${time}`;
}
