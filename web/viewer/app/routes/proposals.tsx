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
  fetchIdentityStatus,
  fetchProposal,
  fetchProposals,
  pollWhileLive,
  reviewProposal,
} from "~/lib/api";
import type { ProposalReviewState } from "~/lib/types";
import { cn } from "~/lib/utils";

const REVIEW_STATES: ProposalReviewState[] = [
  "pending",
  "accepted",
  "rejected",
  "dismissed",
];

function readable(value: string) {
  return value.replaceAll("_", " ");
}

export function meta() {
  return [{ title: "Headlong · proposal inbox" }];
}

export default function ProposalsPage() {
  const { identityId = "" } = useParams();
  const [selected, setSelected] = useState<string | null>(null);
  const [reviewError, setReviewError] = useState<string | null>(null);
  const queryClient = useQueryClient();
  const { data: status } = useQuery({
    queryKey: ["status", identityId],
    queryFn: () => fetchIdentityStatus(identityId),
    refetchInterval: 2000,
  });
  const live = status?.live ?? false;
  const { data: proposals, isLoading } = useQuery({
    queryKey: ["proposals", identityId],
    queryFn: () => fetchProposals(identityId),
    refetchInterval: pollWhileLive(live),
  });
  const active =
    (selected && proposals?.some((item) => item.proposal_id === selected) && selected) ||
    proposals?.[0]?.proposal_id ||
    null;
  const { data: proposal, isLoading: detailLoading } = useQuery({
    queryKey: ["proposal", identityId, active],
    queryFn: () => fetchProposal(identityId, active as string),
    enabled: !!active,
  });
  const review = useMutation({
    mutationFn: (state: ProposalReviewState) =>
      reviewProposal(identityId, active as string, state),
    onMutate: () => setReviewError(null),
    onSuccess: (updated) => {
      queryClient.setQueryData(["proposal", identityId, active], updated);
      void queryClient.invalidateQueries({ queryKey: ["proposals", identityId] });
    },
    onError: (error) => setReviewError(error.message),
  });

  useEffect(() => setReviewError(null), [active]);

  if (isLoading) {
    return (
      <div className="flex justify-center py-20">
        <LoadingDots />
      </div>
    );
  }

  return (
    <div className="mx-auto w-full max-w-7xl px-4">
      <IdentityTabs identityId={identityId} live={live} active="proposals" />
      {!proposals?.length ? (
        <Empty>
          <EmptyHeader>
            <EmptyTitle>No improvement proposals</EmptyTitle>
            <EmptyDescription>
              Evidence-backed work and Observer improvements will appear here for review.
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        <div className="flex flex-col gap-4 lg:flex-row">
          <aside className="min-w-0 shrink-0 lg:w-96">
            <div className="mb-1 text-xs text-muted-foreground">
              {proposals.length} proposal{proposals.length === 1 ? "" : "s"}
            </div>
            <div className="max-h-[42vh] overflow-y-auto rounded-lg border lg:max-h-[calc(100vh-10rem)]">
              {proposals.map((item) => (
                <button
                  key={item.proposal_id}
                  type="button"
                  onClick={() => setSelected(item.proposal_id)}
                  className={cn(
                    "block w-full border-b px-3 py-3 text-left last:border-b-0 hover:bg-accent",
                    item.proposal_id === active && "bg-accent"
                  )}
                >
                  <span className="mb-1.5 flex flex-wrap items-center gap-2">
                    <Badge variant="secondary">{item.proposal_type}</Badge>
                    <Badge variant="outline">{readable(item.evidence_kind)}</Badge>
                    <Badge variant={item.review_state === "pending" ? "secondary" : "default"}>
                      {item.review_state}
                    </Badge>
                  </span>
                  <span className="line-clamp-2 block text-sm font-medium">
                    {item.content}
                  </span>
                </button>
              ))}
            </div>
          </aside>
          <main className="min-h-72 min-w-0 flex-1 rounded-lg border bg-card p-4 sm:p-6">
            {detailLoading || !proposal ? (
              <LoadingDots />
            ) : (
              <>
                <div className="border-b pb-4">
                  <div className="mb-2 flex flex-wrap items-center gap-2">
                    <Badge variant="secondary">{proposal.proposal_label}</Badge>
                    <Badge variant="outline">{readable(proposal.evidence_kind)}</Badge>
                    <Badge variant="secondary">{proposal.review_state}</Badge>
                    <span className="font-mono text-[10px] text-muted-foreground">
                      project: {proposal.knowledge_scope.project_id}
                    </span>
                  </div>
                  <h2 className="text-lg font-semibold">{proposal.title}</h2>
                  <p className="mt-2 text-sm leading-relaxed">{proposal.content}</p>
                </div>

                <section className="py-4">
                  <h3 className="mb-2 text-sm font-semibold">Review state</h3>
                  <div className="flex flex-wrap gap-2">
                    {REVIEW_STATES.map((state) => (
                      <Button
                        key={state}
                        type="button"
                        size="sm"
                        variant={proposal.review_state === state ? "default" : "outline"}
                        disabled={review.isPending}
                        onClick={() => review.mutate(state)}
                      >
                        {state}
                      </Button>
                    ))}
                  </div>
                  <p className="mt-2 text-xs text-muted-foreground">
                    Acceptance records your review only. It does not edit a repository,
                    create work, install a hook, or authorize execution.
                  </p>
                  {reviewError && <p className="mt-2 text-sm text-destructive">{reviewError}</p>}
                </section>

                <section className="border-t pt-4">
                  <h3 className="mb-3 text-sm font-semibold">Evidence</h3>
                  <p className="mb-3 text-xs text-muted-foreground">
                    {proposal.task_root_ids.length} distinct Codex task root
                    {proposal.task_root_ids.length === 1 ? "" : "s"}
                  </p>
                  <div className="space-y-3">
                    {proposal.evidence_locators.map((locator) => (
                      <div
                        key={`${locator.source_identity}:${locator.byte_offset}:${locator.sha256}`}
                        className="rounded-md border bg-muted/30 p-3 text-xs"
                      >
                        <div className="font-mono break-all">
                          {locator.source_root}/{locator.relative_path}:{locator.line}
                        </div>
                        <div className="mt-1 text-muted-foreground">
                          bytes {locator.byte_offset}–{locator.byte_offset + locator.byte_length}
                          {" · "}host {locator.host}
                        </div>
                        <div className="mt-1 font-mono break-all text-[10px] text-muted-foreground">
                          sha256 {locator.sha256}
                        </div>
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
