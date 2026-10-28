/**
 * Who may answer.
 *
 * The hint is not decoration. Chat context carries application URLs, profile
 * fields and recruiter correspondence, so picking anything but `local` sends
 * those to a third party — and the moment to say so is while the owner is
 * choosing, not afterwards in a changelog. `openrouter` gets the longest hint
 * because it is the only one that cannot name who ultimately receives it.
 */
export const PROVIDERS = [
  {
    value: "ollama",
    label: "local",
    hint: "Runs on this machine. Nothing leaves.",
    local: true,
  },
  {
    value: "kaggle",
    label: "Kaggle GPU (auto, remote)",
    hint: "Automatically uses a ready supported model in your running Kaggle notebook. Check the connection for the selected model. Sends your question and retrieved application, profile and résumé skill context to Kaggle. Recruiter mail is withheld unless you share it.",
    local: false,
  },
  {
    value: "gemini",
    label: "gemini",
    hint: "Sends your applications, profile, résumé skill list and recruiter mail to Google.",
    local: false,
  },
  {
    value: "anthropic",
    label: "anthropic",
    hint: "Sends your applications, profile, résumé skill list and recruiter mail to Anthropic.",
    local: false,
  },
  {
    value: "openrouter",
    label: "openrouter",
    hint: "Sends your applications, profile, résumé skill list and recruiter mail to OpenRouter, which forwards them to an upstream provider it does not name.",
    local: false,
  },
  {
    // Separate from `local` on purpose, and the hint has to work harder here
    // than anywhere else in this list: this is the only option that reaches a
    // third party through `localhost:11434`. Someone reading the request would
    // see the same URL the local model uses.
    value: "ollama_cloud",
    label: "ollama cloud",
    hint: "Sends your applications, profile, résumé skill list and recruiter mail to Ollama's servers — not this machine, despite the localhost address. Which model runs is whatever OLLAMA_CLOUD_MODEL names.",
    local: false,
  },
] as const;
