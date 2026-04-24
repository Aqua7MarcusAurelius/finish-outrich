import { cn } from "@/lib/utils";
import type { Message } from "@/types/api";
import { MediaTag } from "./MediaTag";
import { AsyncBlock } from "./AsyncBlock";

function formatTime(iso: string) {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function MessageBubble({ m }: { m: Message }) {
  const deleted = !!m.deleted_at;
  const edited = !!m.edited_at;

  return (
    <div className={cn("flex w-full", m.is_outgoing ? "justify-end" : "justify-start")}>
      <div
        className={cn(
          "max-w-[78%] rounded-md p-2 hairline",
          m.is_outgoing ? "bg-module-transcription/15 border-module-transcription/40" : "bg-card border-border",
          deleted && "opacity-55",
        )}
      >
        {deleted && (
          <div className="mono mb-1 text-[10px] font-semibold uppercase text-destructive">
            УДАЛЕНО СОБЕСЕДНИКОМ · {formatTime(m.deleted_at!)}
          </div>
        )}
        {m.forward && (
          <div className="mb-1 border-l-2 border-module-telegram/60 pl-2 text-[11px] text-muted-foreground">
            Переслано от {m.forward.from_username ? `@${m.forward.from_username}` : m.forward.from_name ?? "—"}
            {" · "}
            {new Date(m.forward.date).toLocaleString()}
          </div>
        )}
        {m.reply_to && (
          <div className="mb-1 border-l-2 border-border pl-2 text-[11px] text-muted-foreground">
            <div className="font-medium">{m.reply_to.is_outgoing ? "вы:" : "собеседник:"}</div>
            <div className="truncate">{m.reply_to.text_preview}</div>
          </div>
        )}

        {m.media.length > 0 && (
          <div className="mb-1 flex flex-col gap-1">
            {m.media.map((md) => (
              <div key={md.id} className="flex flex-col gap-1">
                <MediaTag m={md} />
                {md.type === "photo" && (
                  <img src={md.preview_url} alt="" className="max-h-64 rounded-sm object-contain" loading="lazy" />
                )}
                <AsyncBlock label="TRANSCRIPTION · Whisper" status={md.transcription_status} text={md.transcription} />
                <AsyncBlock label="DESCRIPTION · GPT-4o" status={md.description_status} text={md.description} />
              </div>
            ))}
          </div>
        )}

        {m.text && <div className="whitespace-pre-wrap break-words text-sm">{m.text}</div>}

        <div className="mt-1 flex items-center gap-2 text-[10px] text-muted-foreground">
          <span className="mono">{formatTime(m.date)}</span>
          {edited && <span className="italic">изменено {formatTime(m.edited_at!)}</span>}
        </div>

        {m.reactions.length > 0 && (
          <div className="mt-1 flex flex-wrap gap-1">
            {m.reactions.map((r, i) => (
              <span key={i} className="rounded-full bg-secondary px-1.5 py-0.5 text-[11px] hairline border-border">
                {r.emoji} {r.count}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
