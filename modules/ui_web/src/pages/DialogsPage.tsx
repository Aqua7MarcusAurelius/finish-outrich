import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import { AccountCard } from "@/components/dialogs/AccountCard";
import { NewAccountCard } from "@/components/dialogs/NewAccountCard";
import { NewAccountDialog } from "@/components/dialogs/NewAccountDialog";
import { NewDialogButton } from "@/components/dialogs/NewDialogButton";
import { NewDialogDialog } from "@/components/dialogs/NewDialogDialog";
import { DialogListItem } from "@/components/dialogs/DialogListItem";
import { MessageBubble } from "@/components/dialogs/MessageBubble";
import { ErrorBox } from "@/components/common/ErrorBox";
import { useAccounts, useDialogProfile, useDialogs, useMessages } from "@/hooks/useAccounts";
import { api, describeApiError } from "@/lib/api";
import { useToast } from "@/lib/toast";

export function DialogsPage() {
  const navigate = useNavigate();
  const params = useParams<{ accountId?: string; dialogId?: string }>();
  const toast = useToast();

  const accountsQ = useAccounts();
  const accounts = accountsQ.data ?? [];

  const accountId = params.accountId ? Number(params.accountId) : accounts[0]?.id ?? null;
  const dialogId = params.dialogId ? Number(params.dialogId) : null;

  const dialogsQ = useDialogs(accountId);
  const dialogs = dialogsQ.data?.items ?? [];

  const messagesQ = useMessages(dialogId);
  const dialogQ = useDialogProfile(dialogId);

  const [search, setSearch] = useState("");
  const [newAccOpen, setNewAccOpen] = useState(false);
  const [newDlgOpen, setNewDlgOpen] = useState(false);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return dialogs;
    return dialogs.filter((d) =>
      [d.title, d.username, d.phone].filter(Boolean).some((s) => s!.toLowerCase().includes(q)),
    );
  }, [dialogs, search]);

  // Auto-select first dialog when an account opens
  useEffect(() => {
    if (accountId && !dialogId && filtered.length > 0) {
      navigate(`/dialogs/${accountId}/${filtered[0].id}`, { replace: true });
    }
  }, [accountId, dialogId, filtered, navigate]);

  return (
    <div className="flex h-full flex-col">
      {/* ── Accounts row ─────────────────────────────────────────── */}
      <div className="border-b border-border">
        <ScrollArea className="w-full">
          <div className="flex gap-2 p-2">
            {accountsQ.isError && (
              <ErrorBox title="Не удалось загрузить аккаунты" detail={String(accountsQ.error)} />
            )}
            {accountsQ.isLoading && <div className="text-xs text-muted-foreground">загрузка…</div>}
            {accounts.map((a) => (
              <AccountCard
                key={a.id}
                account={a}
                selected={a.id === accountId}
                onSelect={() => navigate(`/dialogs/${a.id}`)}
                onStart={() => api.startWorker(a.id)
                  .then(() => { toast.success(`Воркер ${a.name || `#${a.id}`} запущен`); accountsQ.refetch(); })
                  .catch((e) => { const d = describeApiError(e); toast.error(`Start: ${d.title}`, d.detail); })}
                onStop={() => api.stopWorker(a.id)
                  .then(() => { toast.success(`Воркер ${a.name || `#${a.id}`} остановлен`); accountsQ.refetch(); })
                  .catch((e) => { const d = describeApiError(e); toast.error(`Stop: ${d.title}`, d.detail); })}
              />
            ))}
            <NewAccountCard onClick={() => setNewAccOpen(true)} />
          </div>
        </ScrollArea>
      </div>

      <NewAccountDialog
        open={newAccOpen}
        onClose={() => setNewAccOpen(false)}
        onCreated={() => accountsQ.refetch()}
      />

      <NewDialogDialog
        open={newDlgOpen}
        accountId={accountId}
        onClose={() => setNewDlgOpen(false)}
        onCreated={() => {
          // Новый диалог-row в history.dialogs появится после первого
          // message.saved (~1-3 сек). Рефетчим с запасом, два тика.
          accountsQ.refetch();
          setTimeout(() => dialogsQ.refetch(), 2500);
        }}
      />

      {/* ── Body: dialogs + messages ─────────────────────────────── */}
      <div className="flex min-h-0 flex-1">
        <aside className="flex w-[260px] shrink-0 flex-col border-r border-border">
          <div className="flex h-12 shrink-0 items-center border-b border-border px-2">
            <Input placeholder="Поиск по диалогам…" value={search} onChange={(e) => setSearch(e.target.value)} />
          </div>
          <ScrollArea className="flex-1">
            {dialogsQ.isError && <div className="p-2"><ErrorBox title="API недоступен" detail={String(dialogsQ.error)} /></div>}
            {dialogsQ.isLoading && <div className="p-3 text-xs text-muted-foreground">загрузка…</div>}
            {!dialogsQ.isLoading && filtered.length === 0 && (
              <div className="p-3 text-xs text-muted-foreground">диалогов пока нет</div>
            )}
            {filtered.map((d) => (
              <DialogListItem
                key={d.id}
                dialog={d}
                selected={d.id === dialogId}
                onSelect={() => navigate(`/dialogs/${accountId}/${d.id}`)}
              />
            ))}
            <NewDialogButton
              onClick={() => setNewDlgOpen(true)}
              disabled={accountId == null}
              hint="Выбери аккаунт сверху"
            />
          </ScrollArea>
        </aside>

        <section className="flex min-w-0 flex-1 flex-col">
          {/* Высота h-12 совпадает с шапкой "Поиск по диалогам" слева —
             нижняя линия бордера идёт сквозной без ступеньки. */}
          <header className="flex h-12 shrink-0 items-center border-b border-border px-2">
            <div className="flex max-w-[780px] items-center gap-3">
              {dialogQ.data ? (
                <>
                  <div className="text-sm font-semibold">
                    {dialogQ.data.first_name || ""} {dialogQ.data.last_name || ""}
                  </div>
                  <div className="mono text-xs text-muted-foreground">
                    {dialogQ.data.username ? `@${dialogQ.data.username}` : "—"} · {dialogQ.data.phone ?? "—"} · dialog #{dialogQ.data.id}
                  </div>
                </>
              ) : (
                <div className="text-xs text-muted-foreground">выберите диалог</div>
              )}
            </div>
          </header>
          {/* Лента шириной 780px прижата к левому краю — пустое поле справа
             свободно под будущие виджеты (профиль собеседника, лента
             связанных событий шины).

             flex-col-reverse: backend отдаёт сообщения в DESC (новые первыми),
             в DOM они идут как пришли, но flex-col-reverse кладёт первый
             элемент визуально в низ. Результат — старые сверху, новые снизу,
             как в любом мессенджере. Скролл по умолчанию в самом низу потому
             что scrollTop=0 в reverse-контейнере = крайнее нижнее положение. */}
          <div className="flex-1 overflow-y-auto">
            <div className="flex max-w-[780px] flex-col-reverse gap-2 p-3">
              {messagesQ.isError && <ErrorBox title="Сообщения не загрузились" detail={String(messagesQ.error)} />}
              {messagesQ.isLoading && <div className="text-xs text-muted-foreground">загрузка…</div>}
              {(messagesQ.data?.items ?? []).map((m) => (
                <MessageBubble key={m.id} m={m} />
              ))}
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}
