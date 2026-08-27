import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useParams } from "react-router";

import { IdentityTabs } from "~/components/identity-tabs";
import { Badge } from "~/components/ui/badge";
import { Button } from "~/components/ui/button";
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyTitle,
} from "~/components/ui/empty";
import { LoadingDots } from "~/components/ui/loading-dots";
import {
  fetchArchiveCandidate,
  fetchArchiveCandidateEvidence,
  fetchArchiveCandidates,
  fetchIdentityStatus,
  pollWhileLive,
  reviewArchiveCandidate,
} from "~/lib/api";
import type { ProposalReviewState } from "~/lib/types";
import { cn } from "~/lib/utils";

const REVIEW_STATES: ProposalReviewState[] = [
  "pending",
  "accepted",
  "rejected",
  "dismissed",
];

export function meta() {
  return [{ title: "Headlong · archive candidates" }];
}

export default function ArchiveCandidatesPage() {
  const { identityId = "" } = useParams();
  const [selected, setSelected] = useState<string | null>(null);
  const [evidenceIndex, setEvidenceIndex] = useState<number | null>(null);
  const [reviewError, setReviewError] = useState<string | null>(null);
  const queryClient = useQueryClient();
  const { data: status } = useQuery({
    queryKey: ["status", identityId],
    queryFn: () => fetchIdentityStatus(identityId),
    refetchInterval: 2000,
  });
  const live = status?.live ?? false;
  const { data: candidates, isLoading } = useQuery({
    queryKey: ["archive-candidates", identityId],
    queryFn: () => fetchArchiveCandidates(identityId),
    refetchInterval: pollWhileLive(live),
  });
  const active =
    (selected && candidates?.some((item) => item.candidate_id === selected) && selected) ||
    candidates?.[0]?.candidate_id ||
    null;
  const { data: candidate, isLoading: detailLoading } = useQuery({
    queryKey: ["archive-candidate", identityId, active],
    queryFn: () => fetchArchiveCandidate(identityId, active as string),
    enabled: !!active,
  });
  const { data: evidence, isFetching: evidenceLoading } = useQuery({
    queryKey: ["archive-candidate-evidence", identityId, active, evidenceIndex],
    queryFn: () =>
      fetchArchiveCandidateEvidence(
        identityId,
        active as string,
        evidenceIndex as number
      ),
    enabled: !!active && evidenceIndex !== null,
  });
  const review = useMutation({
    mutationFn: (state: ProposalReviewState) =>
      reviewArchiveCandidate(identityId, active as string, state),
    onMutate: () => setReviewError(null),
    onSuccess: (updated) => {
      queryClient.setQueryData(["archive-candidate", identityId, active], updated);
      void queryClient.invalidateQueries({
        queryKey: ["archive-candidates", identityId],
      });
    },
    onError: (error) => setReviewError(error.message),
  });

  useEffect(() => {
    setEvidenceIndex(null);
    setReviewError(null);
  }, [active]);

  if (isLoading) {
    return (
      <div className="flex justify-center py-20">
        <LoadingDots />
      </div>
    );
  }

  return (
    <div className="mx-auto w-full max-w-7xl px-4">
      <IdentityTabs
        identityId={identityId}
        live={live}
        active="archive-candidates"
      />
      {!candidates?.length ? (
        <Empty>
          <EmptyHeader>
            <EmptyTitle>No archive candidates</EmptyTitle>
            <EmptyDescription>
              Evidence-backed Codex Sessions that appear complete will wait here for
              your review.
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        <div className="flex flex-col gap-4 lg:flex-row">
          <aside className="min-w-0 shrink-0 lg:w-96">
            <div className="mb-1 text-xs text-muted-foreground">
              {candidates.length} candidate{candidates.length === 1 ? "" : "s"}
            </div>
            <div className="max-h-[42vh] overflow-y-auto rounded-lg border lg:max-h-[calc(100vh-10rem)]">
              {candidates.map((item) => (
                <button
                  key={item.candidate_id}
                  type="button"
                  onClick={() => setSelected(item.candidate_id)}
                  className={cn(
                    "block w-full border-b px-3 py-3 text-left last:border-b-0 hover:bg-accent",
                    item.candidate_id === active && "bg-accent"
                  )}
                >
                  <span className="mb-1.5 flex flex-wrap items-center gap-2">
                    <Badge variant="secondary">{item.analysis_state}</Badge>
                    <Badge variant={item.review_state === "pending" ? "secondary" : "default"}>
                      {item.review_state}
                    </Badge>
                  </span>
                  <span className="line-clamp-2 block text-sm font-medium">
                    {item.completion_rationale}
                  </span>
                  <span className="mt-1 block truncate font-mono text-[10px] text-muted-foreground">
                    {item.session_id}
                  </span>
                </button>
              ))}
            </div>
          </aside>
          <main className="min-h-72 min-w-0 flex-1 rounded-lg border bg-card p-4 sm:p-6">
            {detailLoading || !candidate ? (
              <LoadingDots />
            ) : (
              <>
                <div className="border-b pb-4">
                  <div className="mb-2 flex flex-wrap items-center gap-2">
                    <Badge variant="secondary">Archive Candidate</Badge>
                    <Badge variant="outline">{candidate.analysis_state}</Badge>
                    <Badge variant="secondary">{candidate.review_state}</Badge>
                  </div>
                  <h2 className="text-lg font-semibold">Completed work claim</h2>
                  <p className="mt-2 text-sm leading-relaxed">
                    {candidate.completion_rationale}
                  </p>
                  <dl className="mt-3 grid gap-1 font-mono text-[11px] text-muted-foreground">
                    <div>session: {candidate.session_id}</div>
                    <div>project: {candidate.project_id}</div>
                    <div>analysis: {candidate.source_analysis_event_id}</div>
                  </dl>
                </div>

                <section className="py-4">
                  <h3 className="mb-2 text-sm font-semibold">Review state</h3>
                  <div className="flex flex-wrap gap-2">
                    {REVIEW_STATES.map((state) => (
                      <Button
                        key={state}
                        type="button"
                        size="sm"
                        variant={candidate.review_state === state ? "default" : "outline"}
                        disabled={review.isPending}
                        onClick={() => review.mutate(state)}
                      >
                        {state}
                      </Button>
                    ))}
                  </div>
                  <p className="mt-2 text-xs text-muted-foreground">
                    Acceptance records your authority for a later execution slice. This
                    screen does not archive or edit the Codex Session.
                  </p>
                  {reviewError && (
                    <p className="mt-2 text-sm text-destructive">{reviewError}</p>
                  )}
                </section>

                <section className="border-t pt-4">
                  <h3 className="mb-3 text-sm font-semibold">Evidence</h3>
                  <div className="space-y-3">
                    {candidate.evidence_locators.map((locator, index) => (
                      <div
                        key={`${locator.source_identity}:${locator.byte_offset}:${locator.sha256}`}
                        className="rounded-md border bg-muted/30 p-3 text-xs"
                      >
                        <div className="font-mono break-all">
                          {locator.source_root}/{locator.relative_path}:{locator.line}
                        </div>
                        <div className="mt-2">
                          <Button
                            type="button"
                            size="sm"
                            variant="outline"
                            onClick={() => setEvidenceIndex(index)}
                          >
                            Resolve evidence
                          </Button>
                        </div>
                        {evidenceIndex === index && (
                          <pre className="mt-3 max-h-64 overflow-auto whitespace-pre-wrap rounded bg-background p-3 font-mono text-[11px]">
                            {evidenceLoading ? "Resolving…" : evidence?.raw}
                          </pre>
                        )}
                      </div>
                    ))}
                  </div>
                </section>
              </>
            )}
          </main>
        </div>
      )}
    </div>
  );
}
