import { useState } from "react";
import { cn } from "@/lib/utils";
import { DialogStatusesPanel } from "@/components/settings/DialogStatusesPanel";

// Страница /settings — общий контейнер с левым саб-навом. Сейчас
// внутри один пункт «Статусы диалогов». Когда понадобится — добавим
// «AutoChat», «Глобальные параметры» и т.п. без изменения роутинга.

type SectionKey = "statuses";

const SECTIONS: { key: SectionKey; label: string; description: string }[] = [
  {
    key: "statuses",
    label: "Статусы диалогов",
    description: "Каталог операторских пузырей: лейбл, цвет, порядок",
  },
];

export function SettingsPage() {
  const [section, setSection] = useState<SectionKey>("statuses");

  return (
    <div className="flex h-full">
      <aside className="w-[240px] shrink-0 border-r border-border">
        <div className="flex h-12 items-center border-b border-border px-3">
          <span className="text-sm font-semibold">Настройки</span>
        </div>
        <nav className="flex flex-col gap-0.5 p-2">
          {SECTIONS.map((s) => (
            <button
              key={s.key}
              onClick={() => setSection(s.key)}
              className={cn(
                "flex flex-col gap-0.5 rounded-md px-3 py-2 text-left text-sm hairline border-transparent",
                section === s.key
                  ? "bg-accent text-accent-foreground"
                  : "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
              )}
            >
              <span className="font-medium">{s.label}</span>
              <span className="text-[11px] opacity-70">{s.description}</span>
            </button>
          ))}
        </nav>
      </aside>

      <section className="min-w-0 flex-1 overflow-auto">
        {section === "statuses" && <DialogStatusesPanel />}
      </section>
    </div>
  );
}
