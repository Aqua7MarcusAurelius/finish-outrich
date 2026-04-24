import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { ModulePill } from "@/components/common/ModulePill";
import { StatusDot } from "@/components/common/StatusDot";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Copy } from "lucide-react";

export function EventDetailDialog({ id, onClose, onOpen }: { id: number | null; onClose: () => void; onOpen: (id: number) => void }) {
  const chainQ = useQuery({ queryKey: ["event-chain", id], queryFn: () => api.eventChain(id!), enabled: id != null });
  const ev = chainQ.data?.event;

  return (
    <Dialog open={id != null} onOpenChange={(v) => { if (!v) onClose(); }}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <span className="mono">Event #{id}</span>
            {ev && <StatusDot status={ev.status} />}
            {ev && <ModulePill module={ev.module} />}
          </DialogTitle>
        </DialogHeader>

        {chainQ.isError && <div className="text-xs text-destructive">не удалось загрузить цепочку: {String(chainQ.error)}</div>}
        {chainQ.isLoading && <div className="text-xs text-muted-foreground">загрузка…</div>}

        {ev && (
          <div className="flex flex-col gap-3 text-xs">
            <div className="grid grid-cols-[110px_1fr] gap-y-1">
              <span className="text-muted-foreground">status</span><span className="mono">{ev.status}</span>
              <span className="text-muted-foreground">time</span><span className="mono">{ev.time}</span>
              <span className="text-muted-foreground">account</span><span className="mono">{ev.account_name || (ev.account_id != null ? `account_${ev.account_id}` : "—")}</span>
              <span className="text-muted-foreground">type</span><span className="mono font-semibold">{ev.type}</span>
              <span className="text-muted-foreground">parent</span>
              <span className="mono">{ev.parent_id ? <button className="hover:underline" onClick={() => onOpen(ev.parent_id!)}>#{ev.parent_id}</button> : "—"}</span>
            </div>

            {chainQ.data && (
              <div>
                <div className="mb-1 text-[10px] uppercase text-muted-foreground">Chain (parent_id)</div>
                <div className="flex flex-col gap-0.5">
                  {chainQ.data.ancestors.map((a) => (
                    <button key={a.id} className="mono flex gap-2 text-left hover:underline" onClick={() => onOpen(a.id)}>
                      <span className="w-12 text-muted-foreground">#{a.id}</span>
                      <span className="w-32">{a.module}</span>
                      <span>{a.type}</span>
                    </button>
                  ))}
                  <div className="mono flex gap-2 bg-accent/50 p-0.5">
                    <span className="w-12">#{ev.id}</span>
                    <span className="w-32">{ev.module}</span>
                    <span>{ev.type}</span>
                    <span className="ml-auto">←</span>
                  </div>
                  {chainQ.data.descendants.map((a) => (
                    <button key={a.id} className="mono flex gap-2 text-left hover:underline" onClick={() => onOpen(a.id)}>
                      <span className="w-12 text-muted-foreground">#{a.id}</span>
                      <span className="w-32">{a.module}</span>
                      <span>{a.type}</span>
                    </button>
                  ))}
                </div>
              </div>
            )}

            <div>
              <div className="mb-1 text-[10px] uppercase text-muted-foreground">data</div>
              <pre className="mono max-h-64 overflow-auto rounded-sm bg-secondary/60 p-2 text-[11px]">
{JSON.stringify(ev.data, null, 2)}
              </pre>
            </div>

            <div className="flex gap-2">
              <Button
                size="sm"
                variant="outline"
                onClick={() => navigator.clipboard?.writeText(JSON.stringify(ev, null, 2))}
              >
                <Copy className="h-3 w-3" /> Копировать JSON
              </Button>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
