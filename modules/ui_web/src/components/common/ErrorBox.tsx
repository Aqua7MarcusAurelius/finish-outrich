import { AlertCircle } from "lucide-react";

export function ErrorBox({ title, detail }: { title: string; detail?: string }) {
  return (
    <div className="flex items-start gap-2 rounded-md bg-destructive/10 p-2 text-xs text-destructive hairline border-destructive/40">
      <AlertCircle className="h-4 w-4 shrink-0" />
      <div>
        <div className="font-semibold">{title}</div>
        {detail && <div className="mono mt-0.5 break-all opacity-80">{detail}</div>}
      </div>
    </div>
  );
}
