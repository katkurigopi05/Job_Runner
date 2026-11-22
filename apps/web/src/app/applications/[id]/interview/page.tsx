import { Interview } from "./interview";

export default async function InterviewPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <Interview applicationId={id} />;
}
