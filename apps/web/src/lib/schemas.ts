import { z } from "zod";

const optionalText = z.string().nullable().optional();
const scoreMessage = "Minimum match score must be a number from 0 to 1.";
const yearsMessage =
  "Required experience must be a number from 0 to 50, or blank for no limit.";

// Numeric controls retain their text representation until the existing Server
// Action builds its payload. In particular, a blank years limit is never zero.
const numericControl = (max: number, message: string) =>
  z
    .union([z.string(), z.number()])
    .refine((value) => {
      if (typeof value === "string" && value.trim() === "") return true;
      const number = Number(value);
      return Number.isFinite(number) && number >= 0 && number <= max;
    }, message)
    .optional();

export const profileFormSchema = z.object({
  label: optionalText,
  phone: optionalText,
  location: optionalText,
  salary_expectation: optionalText,
  work_auth: optionalText,
  needs_sponsorship: z.boolean().optional(),
  auto_submit: z.boolean().optional(),
  citizenship_status: z
    .enum([
      "",
      "us_citizen",
      "permanent_resident",
      "other_authorized",
      "not_authorized",
    ])
    .nullable()
    .optional(),
  target_seniority: z
    .enum(["", "intern", "junior", "mid", "senior", "staff", "principal"])
    .nullable()
    .optional(),
  min_match_score: numericControl(1, scoreMessage),
  max_required_experience_years: numericControl(50, yearsMessage),
  // Shared settings validation; the current profile editor submits only fields
  // supported by its Server Action, leaving candidate/search settings separate.
  email: z
    .union([z.email("Enter a valid email address."), z.literal("")])
    .optional(),
  min_salary: z
    .number()
    .nonnegative("Minimum pay must be zero or a positive number.")
    .optional(),
  target_roles: z.array(z.string().trim().min(1)).optional(),
  target_locations: z.array(z.string().trim().min(1)).optional(),
  visa_status: optionalText,
});
export type ProfileFormValues = z.infer<typeof profileFormSchema>;

// Validate required evidence without rewriting the owner's words or toggling
// confirmation. The API remains the authority for accepting saved evidence.
const requiredText = (message: string) =>
  z.string().refine((text) => text.trim().length > 0, message);
export const internshipSchema = z.object({
  id: z.string().optional(),
  company: requiredText("Enter a company."),
  role: requiredText("Enter a role."),
  location: z.string().nullable(),
  started: z.string(),
  ended: z.string().nullable(),
  stack: z.array(z.string()),
  bullets: z.array(z.string()),
  verified: z.boolean(),
});
export const certificationSchema = z.object({
  id: z.string().optional(),
  name: requiredText("Enter a certification name."),
  issuer: z.string(),
  issued: z.string().nullable(),
  expires: z.string().nullable(),
  credential_id: z.string().nullable(),
  skills: z.array(z.string()),
  verified: z.boolean(),
});
export const evidenceFormSchema = z.object({
  internships: z.array(internshipSchema),
  certifications: z.array(certificationSchema),
});
export type EvidenceFormValues = z.infer<typeof evidenceFormSchema>;
