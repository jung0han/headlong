import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "react-router";

import { IdentityTabs } from "~/components/identity-tabs";
import { useControlsEnabled } from "~/components/thinker-controls";
import { Badge } from "~/components/ui/badge";
import { Button } from "~/components/ui/button";
import { LoadingDots } from "~/components/ui/loading-dots";
import {
  fetchIdentityStatus,
  fetchShadowGate,
  fetchShadowGateMemories,
  fetchShadowGateObservations,
  pollWhileLive,
  reviewShadowGateMemory,
  reviewShadowGateObservation,
} from "~/lib/api";

export function meta() {
  return [{ title: "Headlong · shadow gate" }];
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border bg-card p-4">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-1 font-mono text-2xl font-semibold">{value}</div>
    </div>
  );
}

export default function ShadowGatePage() {
  const { identityId = "" } = useParams();
  const controlsEnabled = useControlsEnabled();
  const queryClient = useQueryClient();
  const { data: status } = useQuery({
    queryKey: ["status", identityId],
    queryFn: () => fetchIdentityStatus(identityId),
    refetchInterval: 2000,
  });
  const live = status?.live ?? false;
  const interval = pollWhileLive(live);
  const { data: report, isLoading } = useQuery({
    queryKey: ["shadow-gate", identityId],
    queryFn: () => fetchShadowGate(identityId),
    refetchInterval: interval,
  });
  const { data: observations } = useQuery({
    queryKey: ["shadow-observations", identityId],
    queryFn: () => fetchShadowGateObservations(identityId),
    refetchInterval: interval,
  });
  const { data: memories } = useQuery({
    queryKey: ["shadow-memories", identityId],
    queryFn: () => fetchShadowGateMemories(identityId),
    refetchInterval: interval,
  });
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["shadow-gate", identityId] });
    void queryClient.invalidateQueries({ queryKey: ["shadow-observations", identityId] });
    void queryClient.invalidateQueries({ queryKey: ["shadow-memories", identityId] });
  };
  const observationReview = useMutation({
    mutationFn: ({ id, useful, accurate }: { id: string; useful: boolean; accurate: boolean }) =>
      reviewShadowGateObservation(identityId, id, useful, accurate),
    onSuccess: refresh,
  });
  const memoryReview = useMutation({
    mutationFn: ({ id, correct }: { id: string; correct: boolean }) =>
      reviewShadowGateMemory(identityId, id, correct),
    onSuccess: refresh,
  });

  if (isLoading || !report) {
    return <div className="flex justify-center py-20"><LoadingDots /></div>;
  }
  const rate = report.useful_and_accurate_rate;
  return (
    <div className="mx-auto w-full max-w-7xl px-4">
      <IdentityTabs identityId={identityId} live={live} active="shadow" />
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <h2 className="text-xl font-semibold">Shadow Gate</h2>
        <Badge variant={report.ready ? "default" : "secondary"}>
          {report.status.replace("_", " ")}
        </Badge>
        <span className="text-xs text-muted-foreground">
          Seven days or twenty Final Consolidations, whichever occurs first.
        </span>
      </div>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <Metric label="Elapsed" value={`${report.elapsed_days.toFixed(1)}d`} />
        <Metric label="Final Consolidations" value={String(report.final_consolidation_count)} />
        <Metric label="Reviewed Observations" value={String(report.reviewed_observation_count)} />
        <Metric
          label="Useful + accurate"
          value={rate === null ? "—" : `${Math.round(rate * 100)}%`}
        />
        <Metric
          label="Incorrect Active Memory"
          value={String(report.incorrect_active_memory_count)}
        />
      </div>
      <div className="mt-4 rounded-lg border bg-muted/20 p-4 text-sm">
        <div className="font-medium">Authority remains proposal-only</div>
        <p className="mt-1 text-muted-foreground">
          Passing this report does not enable external writes, agent hooks, or project mounts.
          A separate user decision and implementation would still be required.
        </p>
      </div>

      <section className="mt-6">
        <h3 className="mb-3 text-lg font-semibold">Final Consolidations</h3>
        <div className="space-y-3">
          {!observations?.length && (
            <p className="text-sm text-muted-foreground">
              No Final Consolidations yet.
            </p>
          )}
          {observations?.map((item) => (
            <article key={item.event_id} className="rounded-lg border bg-card p-4">
              <div className="flex flex-wrap items-center gap-2">
                <h4 className="font-medium">{item.title}</h4>
                {item.evaluation && (
                  <Badge
                    variant={
                      item.evaluation.useful && item.evaluation.accurate
                        ? "default"
                        : "secondary"
                    }
                  >
                    {item.evaluation.useful ? "useful" : "not useful"} ·{" "}
                    {item.evaluation.accurate ? "accurate" : "inaccurate"}
                  </Badge>
                )}
              </div>
              <p className="mt-2 text-sm text-muted-foreground">{item.content}</p>
              {controlsEnabled && (
                <div className="mt-3 flex flex-wrap gap-2">
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={observationReview.isPending}
                    onClick={() =>
                      observationReview.mutate({
                        id: item.event_id,
                        useful: true,
                        accurate: true,
                      })
                    }
                  >
                    Useful + accurate
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={observationReview.isPending}
                    onClick={() =>
                      observationReview.mutate({
                        id: item.event_id,
                        useful: false,
                        accurate: true,
                      })
                    }
                  >
                    Not useful
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={observationReview.isPending}
                    onClick={() =>
                      observationReview.mutate({
                        id: item.event_id,
                        useful: true,
                        accurate: false,
                      })
                    }
                  >
                    Inaccurate
                  </Button>
                </div>
              )}
            </article>
          ))}
        </div>
      </section>

      <section className="my-6">
        <h3 className="mb-3 text-lg font-semibold">Active Memory promotions</h3>
        <div className="space-y-3">
          {!memories?.length && (
            <p className="text-sm text-muted-foreground">
              No Active Memory promotions yet.
            </p>
          )}
          {memories?.map((item) => (
            <article key={item.event_id} className="rounded-lg border bg-card p-4">
              <div className="flex flex-wrap items-center gap-2">
                <h4 className="font-medium">{item.memory_key}</h4>
                <Badge variant="outline">{item.memory_kind}</Badge>
                {item.evaluation && (
                  <Badge
                    variant={item.evaluation.correct ? "default" : "secondary"}
                  >
                    {item.evaluation.correct ? "correct" : "incorrect"}
                  </Badge>
                )}
              </div>
              <p className="mt-2 text-sm text-muted-foreground">{item.content}</p>
              {controlsEnabled && (
                <div className="mt-3 flex gap-2">
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={memoryReview.isPending}
                    onClick={() =>
                      memoryReview.mutate({ id: item.event_id, correct: true })
                    }
                  >
                    Correct promotion
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={memoryReview.isPending}
                    onClick={() =>
                      memoryReview.mutate({ id: item.event_id, correct: false })
                    }
                  >
                    Incorrect promotion
                  </Button>
                </div>
              )}
            </article>
          ))}
        </div>
      </section>
    </div>
  );
}
