import { NavLink } from "react-router-dom";
import { Activity, MessagesSquare, Moon, Sun } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { useState } from "react";

function toggleTheme() {
  const root = document.documentElement;
  const next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
  root.setAttribute("data-theme", next);
  return next;
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const [theme, setTheme] = useState<string>(document.documentElement.getAttribute("data-theme") ?? "dark");

  const navLinkCls = ({ isActive }: { isActive: boolean }) =>
    cn(
      "inline-flex items-center gap-2 px-2 py-1 rounded-md text-sm font-medium hairline border-transparent",
      isActive ? "bg-accent text-accent-foreground" : "text-muted-foreground hover:text-foreground hover:bg-accent/60",
    );

  return (
    <div className="flex h-screen flex-col">
      <header className="flex h-12 shrink-0 items-center gap-3 px-3 border-b border-border">
        <div className="flex items-center gap-2">
          <span className="mono text-xs text-muted-foreground">tgf</span>
          <span className="text-sm font-semibold">Control Panel</span>
        </div>
        <nav className="ml-4 flex items-center gap-1">
          <NavLink to="/dialogs" className={navLinkCls}>
            <MessagesSquare className="h-4 w-4" />
            Диалоги
          </NavLink>
          <NavLink to="/events" className={navLinkCls}>
            <Activity className="h-4 w-4" />
            Event log
          </NavLink>
        </nav>
        <div className="ml-auto flex items-center gap-2">
          <Button variant="ghost" size="icon" onClick={() => setTheme(toggleTheme())} aria-label="Toggle theme">
            {theme === "dark" ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
          </Button>
        </div>
      </header>
      <main className="flex-1 overflow-hidden">{children}</main>
    </div>
  );
}
